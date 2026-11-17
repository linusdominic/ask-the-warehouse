"""Digests and alerts: the part of a dashboard that tells you things without being asked.

The model is used once, to turn "send me yesterday's sales every morning" into a stored
query and schedule, which the user then approves. After that every run is deterministic:
the stored query goes through the semantic layer and the message is formatted in code. No
model call is made on the schedule, so a digest costs nothing and cannot drift.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from askwh.semantic import Catalog, QuerySpec, Result

logger = logging.getLogger(__name__)


class Schedule(BaseModel):
    frequency: Literal["hourly", "daily", "weekly"]
    time: str = Field(default="08:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekday: int = Field(default=0, ge=0, le=6, description="0 is Monday; weekly only")


class DigestSpec(BaseModel):
    query: QuerySpec
    schedule: Schedule


class AlertSpec(BaseModel):
    query: QuerySpec
    direction: Literal["above", "below"]
    threshold: float
    check_every_minutes: int = Field(default=15, ge=1, le=1440)


def format_value(value: Any, unit: str) -> str:
    if value is None:
        return "no data"
    if unit == "usd":
        return f"${value:,.0f}" if abs(value) >= 1000 else f"${value:,.2f}"
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    return f"{value:,.0f} {unit}" if unit != "count" else f"{value:,.0f}"


def describe_result(result: Result, top: int = 5) -> str:
    """A result as a short plain-text message."""
    metric, window = result.compiled.metric, result.compiled.window
    head = f"{metric.label}, {window.label}"
    scope = "; ".join(
        f"{f.dimension} {'not ' if f.op == 'not_in' else ''}in {', '.join(f.values)}"
        for f in result.filters
    )
    if scope:
        head += f" ({scope})"
    keys = [column for column in result.compiled.columns if column != "value"]
    if not keys:
        value = result.rows[0]["value"] if result.rows else None
        text = f"{head}: {format_value(value, metric.unit)}"
        comparison = result.comparison
        if comparison and comparison["change"] is not None:
            before = format_value(comparison["previous_value"], metric.unit)
            if metric.unit == "ratio":  # a rate changes in percentage points
                moved = f"{abs(comparison['change']) * 100:.2f} points"
            elif comparison["change_pct"] is not None:
                moved = f"{abs(comparison['change_pct']) * 100:.1f}%"
            else:
                moved = format_value(abs(comparison["change"]), metric.unit)
            direction = "up" if comparison["change"] >= 0 else "down"
            text += f" ({direction} {moved} from {before} in {comparison['previous_time_range']})"
        return text
    lines = [head + ":"]
    for row in result.rows[:top]:
        label = " / ".join(str(row[key])[:10] if key == "period" else str(row[key]) for key in keys)
        lines.append(f"  {label}: {format_value(row['value'], metric.unit)}")
    if len(result.rows) > top:
        lines.append(f"  ... and {len(result.rows) - top} more")
    return "\n".join(lines)


def next_run(schedule: Schedule, after: datetime, tz: ZoneInfo) -> datetime:
    """The first scheduled time strictly after ``after``, in the business time zone."""
    local = after.astimezone(tz)
    if schedule.frequency == "hourly":
        return (local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).astimezone(
            after.tzinfo
        )
    hour, minute = (int(part) for part in schedule.time.split(":"))
    candidate = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule.frequency == "weekly":
        candidate += timedelta(days=(schedule.weekday - candidate.weekday()) % 7)
    step = timedelta(days=7 if schedule.frequency == "weekly" else 1)
    while candidate <= local:
        candidate += step
    return candidate.astimezone(after.tzinfo)


def is_breached(spec: AlertSpec, value: float | None) -> bool:
    if value is None:
        return False
    return value > spec.threshold if spec.direction == "above" else value < spec.threshold


class Subscriptions:
    def __init__(
        self,
        pool: AsyncConnectionPool,
        readonly_pool: AsyncConnectionPool,
        catalog: Catalog,
        webhook_url: str | None = None,
    ) -> None:
        self._pool = pool
        self._readonly = readonly_pool
        self._catalog = catalog
        self._webhook_url = webhook_url

    async def _fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn:
            return await (await conn.execute(sql, params)).fetchall()

    async def propose(
        self, kind: str, name: str, spec: DigestSpec | AlertSpec, thread_id: str | None
    ) -> int:
        rows = await self._fetch(
            "INSERT INTO app.subscriptions (kind, name, spec, thread_id) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            kind,
            name,
            spec.model_dump_json(),
            thread_id,
        )
        return rows[0]["id"]

    async def decide(self, subscription_id: int, approve: bool, now: datetime) -> dict | None:
        """Approve or decline a pending proposal. Returns the row, or None if not pending."""
        rows = await self._fetch(
            "SELECT * FROM app.subscriptions WHERE id = %s AND status = 'pending'", subscription_id
        )
        if not rows:
            return None
        row = rows[0]
        if not approve:
            status, due = "declined", None
        elif row["kind"] == "digest":
            schedule = DigestSpec.model_validate(row["spec"]).schedule
            status, due = "active", next_run(schedule, now, self._catalog.tz)
        else:
            status, due = "active", now  # an alert is checked straight away
        updated = await self._fetch(
            "UPDATE app.subscriptions SET status = %s, next_run_at = %s WHERE id = %s RETURNING *",
            status,
            due,
            subscription_id,
        )
        return updated[0]

    async def cancel(self, subscription_id: int) -> bool:
        rows = await self._fetch(
            "UPDATE app.subscriptions SET status = 'paused', next_run_at = NULL "
            "WHERE id = %s AND status = 'active' RETURNING id",
            subscription_id,
        )
        return bool(rows)

    async def open_items(self) -> list[dict[str, Any]]:
        return await self._fetch(
            "SELECT id, kind, name, spec, status, created_at, next_run_at, last_run_at, last_value "
            "FROM app.subscriptions WHERE status IN ('pending', 'active') ORDER BY id DESC"
        )

    async def notifications(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._fetch(
            "SELECT id, subscription_id, created_at, title, body, delivered_to "
            "FROM app.notifications ORDER BY id DESC LIMIT %s",
            limit,
        )

    async def _deliver(self, subscription_id: int, title: str, body: str) -> None:
        delivered_to = "in-app"
        if self._webhook_url:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(
                        self._webhook_url, json={"text": f"*{title}*\n{body}"}
                    )
                    response.raise_for_status()
                delivered_to = "webhook"
            except httpx.HTTPError as error:
                logger.warning("Webhook delivery failed: %s", error)
                delivered_to = "in-app (webhook failed)"
        await self._fetch(
            "INSERT INTO app.notifications (subscription_id, title, body, delivered_to) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            subscription_id,
            title,
            body,
            delivered_to,
        )

    async def run_one(self, row: dict[str, Any], now: datetime) -> bool:
        """Run one due subscription. Returns True if it sent a notification."""
        if row["kind"] == "digest":
            spec = DigestSpec.model_validate(row["spec"])
            result = await self._catalog.run(self._readonly, spec.query, now)
            await self._deliver(row["id"], row["name"], describe_result(result))
            due = next_run(spec.schedule, now, self._catalog.tz)
            await self._fetch(
                "UPDATE app.subscriptions SET last_run_at = %s, next_run_at = %s "
                "WHERE id = %s RETURNING id",
                now,
                due,
                row["id"],
            )
            return True

        spec = AlertSpec.model_validate(row["spec"])
        result = await self._catalog.run(self._readonly, spec.query, now)
        value = result.rows[0]["value"] if result.rows else None
        breached = is_breached(spec, value)
        was_breached = row["last_value"] is not None and is_breached(spec, row["last_value"])
        # Notify when the condition starts being true, not on every check while it stays true.
        fire = breached and not was_breached
        if fire:
            unit = result.compiled.metric.unit
            body = (
                f"{describe_result(result)}\n"
                f"This is {spec.direction} the threshold of {format_value(spec.threshold, unit)}."
            )
            await self._deliver(row["id"], f"Alert: {row['name']}", body)
        await self._fetch(
            "UPDATE app.subscriptions SET last_run_at = %s, next_run_at = %s, last_value = %s "
            "WHERE id = %s RETURNING id",
            now,
            now + timedelta(minutes=spec.check_every_minutes),
            value,
            row["id"],
        )
        return fire

    async def run_now(self, subscription_id: int, now: datetime) -> bool | None:
        """Run an active subscription immediately. None if it is not active."""
        rows = await self._fetch(
            "SELECT * FROM app.subscriptions WHERE id = %s AND status = 'active'", subscription_id
        )
        if not rows:
            return None
        return await self.run_one(rows[0], now)

    async def run_due(self, now: datetime) -> int:
        due = await self._fetch(
            "SELECT * FROM app.subscriptions WHERE status = 'active' AND next_run_at <= %s "
            "ORDER BY next_run_at",
            now,
        )
        sent = 0
        for row in due:
            try:
                sent += await self.run_one(row, now)
            except Exception:  # one broken subscription must not stop the others
                logger.exception("Subscription %s failed", row["id"])
        return sent

    async def run_forever(self, now_fn, interval_s: float = 20.0) -> None:
        while True:
            try:
                await self.run_due(now_fn())
            except Exception:
                logger.exception("Scheduler pass failed")
            await asyncio.sleep(interval_s)


def spec_summary(kind: str, spec: dict[str, Any]) -> str:
    """One line describing a stored subscription, for the app's side panel."""
    query = spec["query"]
    time_range = query["time_range"]
    when = time_range.get("preset") or f"{time_range['start_date']} to {time_range['end_date']}"
    by = f" by {', '.join(query['dimensions'])}" if query.get("dimensions") else ""
    what = f"{query['metric']}{by}, {when.replace('_', ' ')}"
    if kind == "alert":
        return (
            f"{what} {spec['direction']} {spec['threshold']:g}, "
            f"checked every {spec['check_every_minutes']} min"
        )
    schedule = spec["schedule"]
    if schedule["frequency"] == "hourly":
        return f"{what}, every hour"
    day = ""
    if schedule["frequency"] == "weekly":
        day = " on " + ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][schedule["weekday"]]
    return f"{what}, {schedule['frequency']}{day} at {schedule['time']}"


def as_json(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("created_at", "next_run_at", "last_run_at"):
        if out.get(key) is not None:
            out[key] = out[key].isoformat()
    if isinstance(out.get("spec"), str):
        out["spec"] = json.loads(out["spec"])
    if "spec" in out:
        out["summary"] = spec_summary(out["kind"], out["spec"])
    return out
