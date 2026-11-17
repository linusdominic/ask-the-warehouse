"""The tools the model can call, and the code that runs them.

Governed mode has no tool that accepts SQL. The model names a metric, a time range,
dimensions and filters, and the semantic layer writes the query. ``run_sql`` exists only
for the text-to-SQL baselines the evaluation compares against.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool
from pydantic import ValidationError

from askwh import sqlguard
from askwh.clock import PRESET_HELP
from askwh.semantic import GRAINS, Catalog, QuerySpec, SpecError, _plain
from askwh.subscriptions import AlertSpec, DigestSpec, Schedule, Subscriptions

ROWS_SHOWN_TO_MODEL = 60


@dataclass
class Outcome:
    content: str  # what the model reads
    is_error: bool = False
    event: dict[str, Any] | None = None  # what the app shows and the evaluation scores
    ends_turn: bool = False


def _query_schema(catalog: Catalog) -> dict[str, Any]:
    dimensions = sorted({d for e in catalog.entities.values() for d in e.dimensions})
    return {
        "type": "object",
        "properties": {
            "metric": {"type": "string", "enum": list(catalog.metrics)},
            "time_range": {
                "type": "object",
                "description": "Either a preset, or both start_date and end_date.",
                "properties": {
                    "preset": {"type": "string", "enum": list(PRESET_HELP)},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
                },
            },
            "dimensions": {
                "type": "array",
                "items": {"type": "string", "enum": dimensions},
                "description": "Group by these. Leave empty for a single total.",
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": "string", "enum": dimensions},
                        "op": {"type": "string", "enum": ["in", "not_in"]},
                        "values": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["dimension", "values"],
                },
            },
            "grain": {
                "type": "string",
                "enum": list(GRAINS),
                "description": "Split the result over time. Omit for one figure per group.",
            },
            "order": {"type": "string", "enum": ["value_desc", "value_asc"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
            "compare_previous": {
                "type": "boolean",
                "description": (
                    "Also return the same figure for the previous period and the change. "
                    "Only for a single figure: no dimensions and no grain."
                ),
            },
        },
        "required": ["metric", "time_range"],
    }


CLARIFY_TOOL = {
    "name": "ask_clarification",
    "description": (
        "Ask the user one short question when you cannot tell which metric they mean. "
        "This ends your turn. Do not use it when a stated default applies."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Two to four short answers the user can pick from.",
            },
        },
        "required": ["question"],
    },
}


def semantic_tools(catalog: Catalog) -> list[dict[str, Any]]:
    query = _query_schema(catalog)
    return [
        {
            "name": "query_metric",
            "description": (
                "Get a governed metric from the warehouse. Returns rows, the exact time "
                "window used and how fresh the data is. This is the only source of numbers."
            ),
            "input_schema": query,
        },
        {
            "name": "list_dimension_values",
            "description": "List the exact values a dimension takes, to filter on them.",
            "input_schema": {
                "type": "object",
                "properties": {"dimension": {"type": "string"}},
                "required": ["dimension"],
            },
        },
        CLARIFY_TOOL,
        {
            "name": "report_gap",
            "description": (
                "Record a question the metric catalogue cannot answer, so the data team can "
                "see what is missing. Call this before telling the user you cannot answer."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "request": {"type": "string", "description": "What the user wanted"},
                    "missing": {"type": "string", "description": "The metric or dimension needed"},
                },
                "required": ["request", "missing"],
            },
        },
        {
            "name": "propose_digest",
            "description": (
                "Propose a recurring message with the result of a query. The user must "
                "approve it in the app before it starts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short title for the digest"},
                    "query": query,
                    "frequency": {"type": "string", "enum": ["hourly", "daily", "weekly"]},
                    "time": {"type": "string", "description": "HH:MM, business time zone"},
                    "weekday": {"type": "integer", "description": "0 is Monday; weekly only"},
                },
                "required": ["name", "query", "frequency"],
            },
        },
        {
            "name": "propose_alert",
            "description": (
                "Propose an alert that fires when a single figure goes above or below a "
                "threshold. The query must have no dimensions and no grain. The user must "
                "approve it in the app before it starts."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "query": query,
                    "direction": {"type": "string", "enum": ["above", "below"]},
                    "threshold": {
                        "type": "number",
                        "description": "Ratios as fractions: 0.03 is 3%",
                    },
                    "check_every_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
                },
                "required": ["name", "query", "direction", "threshold"],
            },
        },
    ]


def sql_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "run_sql",
            "description": (
                "Run one read-only PostgreSQL SELECT against the analytics schema and get "
                "the rows back. Qualify tables, for example analytics.fact_orders."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"sql": {"type": "string"}},
                "required": ["sql"],
            },
        },
        CLARIFY_TOOL,
    ]


class Toolbox:
    def __init__(
        self,
        catalog: Catalog,
        readonly_pool: AsyncConnectionPool,
        now_fn: Callable[[], datetime],
        app_pool: AsyncConnectionPool | None = None,
        subscriptions: Subscriptions | None = None,
        thread_id: str | None = None,
        stale_after_seconds: int = 300,
    ) -> None:
        self.catalog = catalog
        self._readonly = readonly_pool
        self._now = now_fn
        self._app = app_pool
        self._subscriptions = subscriptions
        self._thread_id = thread_id
        self._stale_after = stale_after_seconds

    async def call(self, name: str, tool_input: dict[str, Any]) -> Outcome:
        handler = getattr(self, f"_{name}", None)
        try:
            if handler is None:
                raise SpecError(f"Unknown tool '{name}'.")
            outcome = await handler(tool_input)
        except (SpecError, sqlguard.SqlRejected) as error:
            outcome = Outcome(str(error), is_error=True)
        except ValidationError as error:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in error.errors()
            )
            outcome = Outcome(f"Invalid request: {problems}", is_error=True)
        except psycopg.Error as error:
            outcome = Outcome(f"The database rejected the query: {error}".strip(), is_error=True)
        await self._audit(name, tool_input, outcome)
        return outcome

    async def _audit(self, name: str, tool_input: dict[str, Any], outcome: Outcome) -> None:
        if self._app is None:
            return
        async with self._app.connection() as conn:
            await conn.execute(
                "INSERT INTO app.audit_log (thread_id, tool, input, ok, detail) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    self._thread_id,
                    name,
                    json.dumps(tool_input),
                    not outcome.is_error,
                    outcome.content[:500] if outcome.is_error else None,
                ),
            )

    async def freshness(self) -> dict[str, Any]:
        """How far the ingest pipeline's watermark is behind now."""
        async with self._readonly.connection() as conn:
            row = await (
                await conn.execute("SELECT min(watermark) AS at FROM analytics.ingest_watermark")
            ).fetchone()
        watermark = row["at"]
        if watermark is None:
            return {"data_as_of": None, "lag_seconds": None, "stale": True}
        lag = max(0, int((self._now() - watermark).total_seconds()))
        local = watermark.astimezone(self.catalog.tz)
        return {
            "data_as_of": local.strftime("%Y-%m-%d %H:%M:%S %Z"),
            "lag_seconds": lag,
            "stale": lag > self._stale_after,
        }

    # --- governed tools ---------------------------------------------------------------

    async def _query_metric(self, tool_input: dict[str, Any]) -> Outcome:
        spec = QuerySpec.model_validate(tool_input)
        result = await self.catalog.run(self._readonly, spec, self._now())
        compiled = result.compiled
        metric, window = compiled.metric, compiled.window
        fresh = await self.freshness()
        shared = {
            "metric": metric.name,
            "label": metric.label,
            "unit": metric.unit,
            "definition": metric.description,
            "time_range": window.label,
            "columns": compiled.columns,
            "row_count": len(result.rows),
            "truncated": result.truncated,
            **({"comparison": result.comparison} if result.comparison else {}),
            **fresh,
        }
        event = {
            "type": "result",
            "governed": True,
            **shared,
            "rows": result.rows,
            "sql": compiled.display_sql,
            "window_start": window.start.isoformat(),
            "window_end": window.end.isoformat(),
            "spec": spec.model_dump(mode="json", exclude_defaults=True),
        }
        for_model = {**shared, "rows": result.rows[:ROWS_SHOWN_TO_MODEL]}
        return Outcome(json.dumps(for_model), event=event)

    async def _list_dimension_values(self, tool_input: dict[str, Any]) -> Outcome:
        name = str(tool_input.get("dimension", ""))
        values = await self.catalog.dimension_values(self._readonly, name)
        return Outcome(json.dumps({"dimension": name, "values": values}))

    async def _ask_clarification(self, tool_input: dict[str, Any]) -> Outcome:
        question = str(tool_input.get("question", "")).strip()
        if not question:
            raise SpecError("A question is required.")
        options = [str(option) for option in tool_input.get("options", [])][:4]
        event = {"type": "clarify", "question": question, "options": options}
        return Outcome("The question was shown to the user.", event=event, ends_turn=True)

    async def _report_gap(self, tool_input: dict[str, Any]) -> Outcome:
        event = {
            "type": "gap",
            "request": tool_input.get("request"),
            "missing": tool_input.get("missing"),
        }
        return Outcome("Recorded for the data team.", event=event)

    async def _propose(self, kind: str, name: str, spec: DigestSpec | AlertSpec) -> Outcome:
        if self._subscriptions is None:
            raise SpecError("Digests and alerts are not available here.")
        self.catalog.compile(spec.query, self._now())  # reject a bad query before storing it
        subscription_id = await self._subscriptions.propose(kind, name, spec, self._thread_id)
        event = {
            "type": "proposal",
            "id": subscription_id,
            "kind": kind,
            "name": name,
            "spec": spec.model_dump(mode="json"),
        }
        text = (
            f"Proposal #{subscription_id} was recorded. It is not active: the user has to "
            "approve it with the button shown in the app."
        )
        return Outcome(text, event=event)

    async def _propose_digest(self, tool_input: dict[str, Any]) -> Outcome:
        schedule = Schedule.model_validate(
            {k: tool_input[k] for k in ("frequency", "time", "weekday") if k in tool_input}
        )
        spec = DigestSpec(
            query=QuerySpec.model_validate(tool_input.get("query")), schedule=schedule
        )
        return await self._propose("digest", str(tool_input.get("name", "Digest")), spec)

    async def _propose_alert(self, tool_input: dict[str, Any]) -> Outcome:
        spec = AlertSpec.model_validate({k: v for k, v in tool_input.items() if k != "name"})
        if spec.query.dimensions or spec.query.grain:
            raise SpecError("An alert needs a single figure: no dimensions and no grain.")
        return await self._propose("alert", str(tool_input.get("name", "Alert")), spec)

    # --- baseline tool ----------------------------------------------------------------

    async def _run_sql(self, tool_input: dict[str, Any]) -> Outcome:
        sql = str(tool_input.get("sql", ""))
        guarded = sqlguard.validate(sql)
        async with self._readonly.connection() as conn:
            cursor = await conn.execute(guarded)
            rows = await cursor.fetchall()
            columns = [column.name for column in cursor.description]
        truncated = len(rows) > sqlguard.MAX_ROWS
        plain = [
            {key: _plain(value) for key, value in row.items()} for row in rows[: sqlguard.MAX_ROWS]
        ]
        shared = {"columns": columns, "row_count": len(plain), "truncated": truncated}
        event = {"type": "result", "governed": False, **shared, "rows": plain, "sql": sql}
        return Outcome(json.dumps({**shared, "rows": plain[:ROWS_SHOWN_TO_MODEL]}), event=event)
