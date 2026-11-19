"""The web app: a chat endpoint that streams, plus the digest, alert and status endpoints."""

import asyncio
import json
import logging
import random
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic
import psycopg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from askwh import __version__, seed
from askwh.agent import COMPANY, run_turn, system_prompt
from askwh.config import Settings, get_settings
from askwh.db import create_pool, init_schema, schema_description
from askwh.llm import LLM, BudgetExceeded
from askwh.semantic import Catalog
from askwh.subscriptions import Subscriptions, as_json
from askwh.tools import Toolbox

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    """Everything a request needs. Built once at startup, or handed in by tests."""

    settings: Settings
    catalog: Catalog
    pool: AsyncConnectionPool
    readonly_pool: AsyncConnectionPool
    llm: LLM
    subscriptions: Subscriptions
    schema: str
    clock: Callable[[], datetime] = utcnow


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _seed_if_empty(settings: Settings, catalog: Catalog) -> None:
    def work() -> None:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            if conn.execute("SELECT 1 FROM analytics.fact_orders LIMIT 1").fetchone():
                return
            now = utcnow()
            seed.load(conn, seed.generate(now, catalog.tz), now, seed=7)
            logger.warning("Warehouse was empty: loaded simulated data.")

    await asyncio.to_thread(work)


async def _live_ingest(settings: Settings) -> None:
    """Stand-in for a change-data-capture stream: a few new rows every two seconds."""
    rng = random.Random()

    def tick(conn: psycopg.Connection) -> None:
        seed.live_tick(conn, rng)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        while True:
            try:
                await asyncio.to_thread(tick, conn)
            except psycopg.Error:
                logger.exception("Live ingest tick failed")
            await asyncio.sleep(2)


async def build_deps(settings: Settings) -> Deps:
    catalog = Catalog.load(settings.metrics_path)
    pool = await create_pool(settings.database_url)
    await init_schema(pool)
    await _seed_if_empty(settings, catalog)
    readonly_pool = await create_pool(settings.readonly_database_url)
    return Deps(
        settings=settings,
        catalog=catalog,
        pool=pool,
        readonly_pool=readonly_pool,
        llm=LLM(ledger_path=Path("results/app_spend.json"), budget_usd=settings.budget_usd),
        subscriptions=Subscriptions(pool, readonly_pool, catalog, settings.webhook_url),
        schema=await schema_description(readonly_pool),
    )


def create_app(deps: Deps | None = None) -> FastAPI:
    """Application factory. Tests pass ``deps`` to control the model and the clock."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            app.state.deps = deps
            yield
            return
        settings = get_settings()
        built = await build_deps(settings)
        app.state.deps = built
        tasks = [asyncio.create_task(built.subscriptions.run_forever(utcnow))]
        if settings.live_ingest:
            tasks.append(asyncio.create_task(_live_ingest(settings)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            await built.readonly_pool.close()
            await built.pool.close()

    app = FastAPI(title="Ask the warehouse", version=__version__, lifespan=lifespan)

    def get(request: Request) -> Deps:
        return request.app.state.deps

    def toolbox(d: Deps, thread_id: str | None = None) -> Toolbox:
        return Toolbox(
            d.catalog,
            d.readonly_pool,
            d.clock,
            app_pool=d.pool,
            subscriptions=d.subscriptions,
            thread_id=thread_id,
            stale_after_seconds=d.settings.stale_after_seconds,
        )

    async def load_thread(d: Deps, thread_id: str) -> list[dict[str, Any]]:
        async with d.pool.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT messages FROM app.threads WHERE thread_id = %s", (thread_id,)
                )
            ).fetchone()
        return row["messages"] if row else []

    async def save_thread(d: Deps, thread_id: str, messages: list[dict[str, Any]]) -> None:
        async with d.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO app.threads (thread_id, messages) VALUES (%s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE "
                "SET messages = EXCLUDED.messages, updated_at = now()",
                (thread_id, json.dumps(messages)),
            )

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        d = get(request)
        local = d.clock().astimezone(d.catalog.tz)
        return {
            "company": COMPANY,
            "model": d.settings.model,
            "business_time": local.strftime("%a %d %b %Y, %H:%M %Z"),
            "freshness": await toolbox(d).freshness(),
            "live_ingest": d.settings.live_ingest,
            "spend_usd": d.llm.ledger.total,
            "budget_usd": d.llm.budget_usd,
            "metrics": [
                {"name": m.name, "label": m.label, "unit": m.unit, "description": m.description}
                for m in d.catalog.metrics.values()
            ],
        }

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
        """Answer one message. The reply is a stream of server-sent events."""
        d = get(request)
        thread_id = body.thread_id or uuid.uuid4().hex

        async def stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

            async def work() -> None:
                try:
                    result = await run_turn(
                        d.llm,
                        toolbox(d, thread_id),
                        mode="semantic",
                        model=d.settings.model,
                        system=system_prompt("semantic", d.catalog, d.schema),
                        history=await load_thread(d, thread_id),
                        user_message=body.message,
                        now=d.clock(),
                        emit=queue.put,
                    )
                    await save_thread(d, thread_id, result.messages)
                    await queue.put(
                        {
                            "type": "done",
                            "thread_id": thread_id,
                            "stopped": result.stopped,
                            "text": result.text,
                            "cost_usd": round(result.cost_usd, 6),
                            "latency_s": round(result.latency_s, 2),
                            "steps": result.steps,
                        }
                    )
                except BudgetExceeded as error:
                    await queue.put({"type": "error", "message": str(error)})
                except (anthropic.APIError, TimeoutError, psycopg.Error) as error:
                    logger.exception("Chat turn failed")
                    await queue.put(
                        {
                            "type": "error",
                            "message": f"The request failed ({type(error).__name__}).",
                        }
                    )
                finally:
                    await queue.put(None)

            task = asyncio.create_task(work())
            try:
                while (event := await queue.get()) is not None:
                    yield _sse(event["type"], event)
            finally:
                task.cancel()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/subscriptions")
    async def subscriptions(request: Request) -> list[dict[str, Any]]:
        return [as_json(row) for row in await get(request).subscriptions.open_items()]

    @app.post("/api/subscriptions/{subscription_id}/{action}")
    async def decide(subscription_id: int, action: str, request: Request) -> dict[str, Any]:
        d = get(request)
        if action in ("approve", "decline"):
            row = await d.subscriptions.decide(subscription_id, action == "approve", d.clock())
            if row is None:
                raise HTTPException(409, "That proposal is not waiting for a decision.")
            return as_json(row)
        if action == "cancel":
            if not await d.subscriptions.cancel(subscription_id):
                raise HTTPException(409, "That subscription is not active.")
            return {"id": subscription_id, "status": "paused"}
        if action == "run":
            sent = await d.subscriptions.run_now(subscription_id, d.clock())
            if sent is None:
                raise HTTPException(409, "That subscription is not active.")
            return {"id": subscription_id, "sent": sent}
        raise HTTPException(404, "Unknown action.")

    @app.get("/api/notifications")
    async def notifications(request: Request) -> list[dict[str, Any]]:
        rows = await get(request).subscriptions.notifications()
        return [{**row, "created_at": row["created_at"].isoformat()} for row in rows]

    @app.get("/api/gaps")
    async def gaps(request: Request) -> list[dict[str, Any]]:
        """Questions the catalogue could not answer: a backlog for the data team."""
        async with get(request).pool.connection() as conn:
            rows = await (
                await conn.execute(
                    "SELECT at, input FROM app.audit_log WHERE tool = 'report_gap' "
                    "ORDER BY id DESC LIMIT 20"
                )
            ).fetchall()
        return [{"at": row["at"].isoformat(), **row["input"]} for row in rows]

    return app
