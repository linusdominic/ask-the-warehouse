"""Shared fixtures: the real metric catalogue, a scripted model, and a small known warehouse."""

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest

from askwh import seed
from askwh.config import ROOT
from askwh.db import all_ddl, create_pool
from askwh.llm import Turn
from askwh.semantic import Catalog

# Tuesday 17 November 2026, 12:00 in Chicago. Every test that needs "now" uses this.
NOW = datetime(2026, 11, 17, 18, 0, tzinfo=UTC)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL is not set")

# id, ordered_at (UTC), product, region, channel, quantity, unit price, discount, status, test
ORDERS = [
    (1, "2026-11-17 15:00+00", 1, "West", "retail", 10, 9.50, 5.00, "completed", False),
    (2, "2026-11-17 16:00+00", 6, "Midwest", "wholesale", 100, 15.84, 158.40, "completed", False),
    (3, "2026-11-17 17:00+00", 1, "West", "online", 2, 9.98, 0, "cancelled", False),
    (4, "2026-11-17 17:30+00", 4, "Northeast", "retail", 3, 14.00, 0, "returned", False),
    (5, "2026-11-17 17:45+00", 1, "West", "retail", 1000, 9.50, 0, "completed", True),
    # 05:30 UTC is 23:30 the evening before in Chicago, so this one belongs to yesterday.
    (6, "2026-11-17 05:30+00", 8, "Southeast", "online", 4, 32.55, 0, "completed", False),
    (7, "2026-11-16 20:00+00", 3, "West", "retail", 5, 16.00, 8.00, "completed", False),
    (8, "2026-11-10 20:00+00", 3, "West", "retail", 1, 16.00, 0, "completed", False),
]
# id, produced_at (UTC), plant, product, produced, scrapped, downtime minutes
RUNS = [
    (1, "2026-11-17 14:00+00", 1, 1, 1000, 10, 0),
    (2, "2026-11-17 15:00+00", 3, 6, 3000, 150, 30),
    (3, "2026-11-16 20:00+00", 1, 1, 2000, 20, 15),
]


@pytest.fixture(scope="session")
def catalog() -> Catalog:
    return Catalog.load(ROOT / "semantic" / "metrics.yml")


def turn(*blocks: dict, stop_reason: str | None = None) -> Turn:
    """A scripted model response made of text and tool-use blocks."""
    stop = stop_reason or (
        "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    )
    return Turn(
        list(blocks), stop, input_tokens=100, output_tokens=20, cost_usd=0.001, latency_s=0.5
    )


def text(value: str) -> dict:
    return {"type": "text", "text": value}


def tool(name: str, call_id: str = "call_1", **tool_input) -> dict:
    return {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}


class FakeLLM:
    """Returns scripted turns in order and records every request."""

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = list(turns)
        self.requests: list[dict] = []
        self.ledger = SimpleNamespace(total=0.0)
        self.budget_usd = 5.0

    async def respond(self, *, model, system, messages, tools, on_text=None, **_):
        self.requests.append(
            {
                "model": model,
                "system": system,
                "messages": [dict(m) for m in messages],
                "tools": tools,
            }
        )
        reply = self.turns.pop(0)
        if on_text and reply.text:
            await on_text(reply.text)
        return reply


@pytest.fixture(scope="session")
def _database() -> str:
    """Create the schema and load the known rows once per test session."""
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        for statement in all_ddl():
            conn.execute(statement)
        conn.execute(
            "TRUNCATE analytics.fact_orders, analytics.fact_production, analytics.dim_product, "
            "analytics.dim_plant, analytics.ingest_watermark CASCADE"
        )
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO analytics.dim_product VALUES (%s,%s,%s,%s)", seed.PRODUCTS)
            cur.executemany(
                "INSERT INTO analytics.dim_plant VALUES (%s,%s,%s)", [p[:3] for p in seed.PLANTS]
            )
            cur.executemany(
                "INSERT INTO analytics.fact_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                [(*row, "2026-11-17 17:59+00") for row in ORDERS],
            )
            cur.executemany(
                "INSERT INTO analytics.fact_production VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                [(*row, "2026-11-17 17:59+00") for row in RUNS],
            )
            cur.execute(
                "INSERT INTO analytics.ingest_watermark VALUES "
                "('orders', '2026-11-17 17:59:30+00'), ('production', '2026-11-17 17:59:40+00')"
            )
    return TEST_DATABASE_URL


@pytest.fixture
async def pools(_database):
    """(owner pool, read-only pool) on the test database, with app tables emptied."""
    readonly_url = _database.replace("askwh:askwh@", "askwh_ro:askwh_ro@")
    owner = await create_pool(_database, max_size=4)
    readonly = await create_pool(readonly_url, max_size=4)
    async with owner.connection() as conn:
        await conn.execute(
            "TRUNCATE app.threads, app.subscriptions, app.notifications, app.audit_log "
            "RESTART IDENTITY CASCADE"
        )
    try:
        yield owner, readonly
    finally:
        await readonly.close()
        await owner.close()
