"""The semantic layer: a structured request in, governed SQL out.

The agent never writes SQL for a governed metric. It names a metric, a time range, optional
dimensions and filters. This module checks the request against the catalogue and compiles
the one query that request can mean.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from askwh.clock import PRESET_HELP, TimeRange, Window, previous, resolve

MAX_DIMENSIONS = 3
MAX_ROWS = 500
GRAINS = ("hour", "day", "week", "month")


class SpecError(ValueError):
    """The request is not something the semantic layer allows. Safe to show to the model."""


class Filter(BaseModel):
    dimension: str
    op: Literal["in", "not_in"] = "in"
    values: list[str] = Field(min_length=1, max_length=50)


class QuerySpec(BaseModel):
    metric: str
    time_range: TimeRange
    dimensions: list[str] = Field(default_factory=list)
    filters: list[Filter] = Field(default_factory=list)
    grain: Literal["hour", "day", "week", "month"] | None = None
    order: Literal["value_desc", "value_asc"] = "value_desc"
    limit: int = Field(default=100, ge=1, le=MAX_ROWS)
    compare_previous: bool = False


@dataclass(frozen=True)
class Dimension:
    name: str
    expr: str
    description: str
    join: str | None = None


@dataclass(frozen=True)
class Entity:
    name: str
    table: str
    alias: str
    time_column: str
    base_filter: str | None
    joins: dict[str, str]
    dimensions: dict[str, Dimension]


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    entity: str
    unit: str
    description: str
    expr: str
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Compiled:
    sql: str
    params: dict[str, Any]
    window: Window
    metric: Metric
    columns: list[str]

    @property
    def display_sql(self) -> str:
        """The SQL with its parameters written in, for showing to a person."""
        text = self.sql
        for name, value in self.params.items():
            text = text.replace(f"%({name})s", _literal(value))
        return text


@dataclass
class Result:
    compiled: Compiled
    rows: list[dict[str, Any]]
    truncated: bool
    filters: list[Filter] = field(default_factory=list)
    comparison: dict[str, Any] | None = None


def _literal(value: Any) -> str:
    if isinstance(value, list):
        return "ARRAY[" + ", ".join(_literal(item) for item in value) + "]"
    if isinstance(value, datetime):
        return f"TIMESTAMPTZ '{value.isoformat()}'"
    if isinstance(value, int | float):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _plain(value: Any) -> Any:
    """Make a database value safe to serialise as JSON."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | time):  # datetime is a date too
        return value.isoformat()
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return str(value)


class Catalog:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.timezone_name: str = raw["business_timezone"]
        self.tz = ZoneInfo(self.timezone_name)
        self.entities = {
            name: Entity(
                name=name,
                table=body["table"],
                alias=body["alias"],
                time_column=body["time_column"],
                base_filter=body.get("base_filter"),
                joins=body.get("joins", {}),
                dimensions={
                    dim: Dimension(dim, spec["expr"], spec["description"], spec.get("join"))
                    for dim, spec in body["dimensions"].items()
                },
            )
            for name, body in raw["entities"].items()
        }
        self.metrics = {
            body["name"]: Metric(
                name=body["name"],
                label=body["label"],
                entity=body["entity"],
                unit=body["unit"],
                description=" ".join(body["description"].split()),
                expr=" ".join(body["expr"].split()),
                synonyms=tuple(body.get("synonyms", ())),
            )
            for body in raw["metrics"]
        }
        for metric in self.metrics.values():
            if metric.entity not in self.entities:
                raise ValueError(f"metric {metric.name} names unknown entity {metric.entity}")

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")))

    # --- what the model is told -----------------------------------------------------

    def describe(self) -> str:
        """The catalogue as text: metrics, their definitions, dimensions and time ranges."""
        lines = ["METRICS"]
        for metric in self.metrics.values():
            also = f" (also called: {', '.join(metric.synonyms)})" if metric.synonyms else ""
            dims = ", ".join(self.entities[metric.entity].dimensions)
            lines.append(f"- {metric.name} [{metric.unit}]{also}: {metric.description}")
            lines.append(f"  dimensions: {dims}")
        lines.append("\nDIMENSIONS")
        seen: dict[str, str] = {}
        for entity in self.entities.values():
            for dim in entity.dimensions.values():
                seen.setdefault(dim.name, dim.description)
        lines += [f"- {name}: {description}" for name, description in seen.items()]
        lines.append(f"\nTIME RANGES (business time zone {self.timezone_name}, weeks start Monday)")
        lines += [f"- {name}: {meaning}" for name, meaning in PRESET_HELP.items()]
        lines.append("- or explicit start_date and end_date, both inclusive")
        return "\n".join(lines)

    # --- compiling ------------------------------------------------------------------

    def compile(self, spec: QuerySpec, now: datetime, window: Window | None = None) -> Compiled:
        metric = self.metrics.get(spec.metric)
        if metric is None:
            raise SpecError(
                f"Unknown metric '{spec.metric}'. Available: {', '.join(self.metrics)}."
            )
        entity = self.entities[metric.entity]
        if len(spec.dimensions) > MAX_DIMENSIONS:
            raise SpecError(f"At most {MAX_DIMENSIONS} dimensions per query.")
        if len(set(spec.dimensions)) != len(spec.dimensions):
            raise SpecError("A dimension is listed twice.")

        def dimension(name: str) -> Dimension:
            found = entity.dimensions.get(name)
            if found is None:
                raise SpecError(
                    f"'{name}' is not a dimension of {metric.name}. "
                    f"Available: {', '.join(entity.dimensions)}."
                )
            return found

        if spec.compare_previous and (spec.dimensions or spec.grain):
            raise SpecError("compare_previous needs a single figure: no dimensions and no grain.")
        window = window or resolve(spec.time_range, now, self.tz)
        params: dict[str, Any] = {"start": window.start, "end": window.end}
        joins: dict[str, str] = {}
        select: list[str] = []
        group: list[str] = []
        columns: list[str] = []

        if spec.grain:
            params["tz"] = self.timezone_name
            select.append(
                f"date_trunc('{spec.grain}', {entity.time_column} AT TIME ZONE %(tz)s) AS period"
            )
            group.append("1")
            columns.append("period")
        for name in spec.dimensions:
            dim = dimension(name)
            if dim.join:
                joins[dim.join] = entity.joins[dim.join]
            select.append(f"{dim.expr} AS {name}")
            group.append(str(len(select)))
            columns.append(name)
        select.append(f"{metric.expr} AS value")
        columns.append("value")

        where = [f"{entity.time_column} >= %(start)s", f"{entity.time_column} < %(end)s"]
        if entity.base_filter:
            where.insert(0, entity.base_filter)
        for index, item in enumerate(spec.filters):
            dim = dimension(item.dimension)
            if dim.join:
                joins[dim.join] = entity.joins[dim.join]
            params[f"f{index}"] = item.values
            negate = "NOT " if item.op == "not_in" else ""
            where.append(f"{negate}{dim.expr} = ANY(%(f{index})s)")

        direction = "ASC" if spec.order == "value_asc" else "DESC"
        order = "period" if spec.grain else f"value {direction} NULLS LAST"
        if spec.grain and spec.dimensions:
            order += f", value {direction} NULLS LAST"
        params["limit"] = spec.limit + 1  # one extra row tells us whether we truncated

        sql = "\n".join(
            part
            for part in (
                "SELECT " + ",\n       ".join(select),
                f"FROM {entity.table} {entity.alias}",
                *joins.values(),
                "WHERE " + "\n  AND ".join(where),
                f"GROUP BY {', '.join(group)}" if group else "",
                f"ORDER BY {order}" if group else "",
                "LIMIT %(limit)s",
            )
            if part
        )
        return Compiled(sql, params, window, metric, columns)

    async def dimension_values(self, pool: AsyncConnectionPool, name: str) -> list[str]:
        """Every value a dimension takes, for checking filters and resolving names."""
        for entity in self.entities.values():
            dim = entity.dimensions.get(name)
            if dim is None:
                continue
            join = entity.joins[dim.join] if dim.join else ""
            sql = (
                f"SELECT DISTINCT {dim.expr} AS value FROM {entity.table} {entity.alias} "
                f"{join} ORDER BY 1"
            )
            async with pool.connection() as conn:
                rows = await (await conn.execute(sql)).fetchall()
            return [row["value"] for row in rows]
        raise SpecError(f"Unknown dimension '{name}'.")

    async def run(self, pool: AsyncConnectionPool, spec: QuerySpec, now: datetime) -> Result:
        """Validate, compile and execute on the read-only pool."""
        compiled = self.compile(spec, now)
        for item in spec.filters:
            known = await self.dimension_values(pool, item.dimension)
            unknown = [value for value in item.values if value not in known]
            if unknown:
                raise SpecError(
                    f"Unknown {item.dimension} value(s) {unknown}. Valid values: {known}."
                )
        async with pool.connection() as conn:
            rows = await (await conn.execute(compiled.sql, compiled.params)).fetchall()
        truncated = len(rows) > spec.limit
        plain = [{key: _plain(value) for key, value in row.items()} for row in rows[: spec.limit]]
        result = Result(compiled, plain, truncated, spec.filters)
        if spec.compare_previous:
            result.comparison = await self._compare(pool, spec, now, compiled, plain)
        return result

    async def _compare(
        self,
        pool: AsyncConnectionPool,
        spec: QuerySpec,
        now: datetime,
        compiled: Compiled,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The same figure one period earlier, with the change worked out in code."""
        earlier = self.compile(spec, now, previous(spec.time_range, compiled.window, self.tz))
        async with pool.connection() as conn:
            row = await (await conn.execute(earlier.sql, earlier.params)).fetchone()
        current = rows[0]["value"] if rows else None
        before = _plain(row["value"]) if row else None
        change = current - before if current is not None and before is not None else None
        return {
            "previous_value": before,
            "previous_time_range": earlier.window.label,
            "change": change,
            "change_pct": change / before if change is not None and before else None,
        }
