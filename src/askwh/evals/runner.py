"""Runs the evaluation: reference SQL, a free consistency check, then the three agents.

askwh-eval check     load fresh data and check every spec against its reference SQL
askwh-eval run       the same, then run the agents on every question (paid)
askwh-eval rescore   re-apply the answer check to saved answers after a scoring fix (free)
askwh-eval report    rebuild the tables from the saved records (free)
"""

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import yaml
from psycopg_pool import AsyncConnectionPool

from askwh import seed
from askwh.agent import MODES, run_turn, system_prompt
from askwh.config import ROOT, get_settings
from askwh.db import create_pool, schema_description
from askwh.evals import report
from askwh.evals.scoring import answer_match, result_match
from askwh.llm import LLM
from askwh.semantic import Catalog, QuerySpec, _plain
from askwh.tools import Toolbox

QUESTIONS = ROOT / "evals" / "questions.yml"
RESULTS = ROOT / "results"
RECORDS = RESULTS / "records.jsonl"
DEFAULT_URL = "postgresql://askwh:askwh@localhost:5436/warehouse_eval"

FINGERPRINT = """
    SELECT (SELECT count(*) FROM analytics.fact_orders) AS orders,
           (SELECT sum(quantity) FROM analytics.fact_orders) AS cases,
           (SELECT count(*) FROM analytics.fact_production) AS runs,
           (SELECT sum(list_price) FROM analytics.dim_product) AS prices,
           (SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'analytics') AS tables
"""


@dataclass
class Item:
    id: str
    group: str
    category: str  # single, followup, ambiguous, unsafe, gap
    turns: list[str]
    kind: str = ""
    unit: str = ""
    sql: str = ""
    spec: dict[str, Any] | None = None
    reference: list[list[Any]] = field(default_factory=list)


def placeholders(tz: str) -> dict[str, str]:
    """SQL for the business-time boundaries, worked out by the database, not by Python."""
    day = f"date_trunc('day', now() AT TIME ZONE '{tz}')"
    week = f"date_trunc('week', now() AT TIME ZONE '{tz}')"
    month = f"date_trunc('month', now() AT TIME ZONE '{tz}')"

    def at(local: str) -> str:
        return f"(({local}) AT TIME ZONE '{tz}')"

    return {
        "tz": f"'{tz}'",
        "now": "now()",
        "today": at(day),
        "yesterday": at(f"{day} - interval '1 day'"),
        "d_2": at(f"{day} - interval '2 day'"),
        "d_7": at(f"{day} - interval '7 day'"),
        "d_14": at(f"{day} - interval '14 day'"),
        "week": at(week),
        "last_week": at(f"{week} - interval '7 day'"),
        "week_2": at(f"{week} - interval '14 day'"),
        "month": at(month),
        "last_month": at(f"{month} - interval '1 month'"),
    }


def load_items(path: Path, tz: str) -> list[Item]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    marks = placeholders(tz)
    items: list[Item] = []
    for group in raw["groups"]:
        for index, question in enumerate(group["questions"]):
            items.append(
                Item(
                    id=f"{group['id']}.{'ab'[index]}",
                    group=group["id"],
                    category="single",
                    turns=[question],
                    kind=group["kind"],
                    unit=group["unit"],
                    sql=group["sql"].format(**marks),
                    spec=group["spec"],
                )
            )
    for conversation in raw["conversations"]:
        items.append(
            Item(
                id=conversation["id"],
                group=conversation["id"],
                category="followup",
                turns=conversation["turns"],
                kind=conversation["kind"],
                unit=conversation["unit"],
                sql=conversation["sql"].format(**marks),
            )
        )
    for category in ("ambiguous", "unsafe", "gap"):
        key = "gaps" if category == "gap" else category
        for index, question in enumerate(raw[key], start=1):
            items.append(Item(f"{category}_{index}", f"{category}_{index}", category, [question]))
    return items


def prepare_database(url: str, catalog: Catalog) -> datetime:
    """Load fresh simulated data, so "today" has a morning's worth of rows.

    The data stops at the start of the previous minute. The agents are told the time to the
    minute, and a hand-written query that says "up to 12:37" must see the same rows as one
    that says "up to now". Without this gap, a baseline could be marked wrong over the few
    orders placed in the current minute.
    """
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    with psycopg.connect(url, autocommit=True) as conn:
        seed.load(conn, seed.generate(now, catalog.tz), now, seed=7)
    return now


async def add_references(pool: AsyncConnectionPool, items: list[Item]) -> None:
    async with pool.connection() as conn:
        for item in items:
            if item.sql:
                cursor = await conn.execute(item.sql)
                rows = await cursor.fetchall()
                item.reference = [[_plain(value) for value in row.values()] for row in rows]


def _agree(expected: Any, actual: Any) -> bool:
    if isinstance(expected, int | float) and isinstance(actual, int | float):
        return abs(expected - actual) <= max(1e-9, abs(expected) * 1e-9)
    return str(actual).startswith(str(expected))


async def check_specs(catalog: Catalog, pool: AsyncConnectionPool, items: list[Item]) -> int:
    """The free layer: each group's expected spec must reproduce its reference SQL exactly."""
    failures = 0
    seen: set[str] = set()
    now = datetime.now(UTC)
    for item in items:
        if item.spec is None or item.group in seen:
            continue
        seen.add(item.group)
        result = await catalog.run(pool, QuerySpec.model_validate(item.spec), now)
        got = [list(row.values()) for row in result.rows]
        if item.kind == "comparison":
            comparison = result.comparison
            got = [[got[0][0], comparison["previous_value"], comparison["change_pct"]]]
        same = len(got) == len(item.reference) and all(
            len(a) == len(b) and all(_agree(x, y) for x, y in zip(a, b, strict=True))
            for a, b in zip(item.reference, got, strict=True)
        )
        failures += not same
        print(f"  {'PASS' if same else 'FAIL'}  {item.group:32} {len(got)} row(s)")
        if not same:
            print(f"        reference {item.reference[:3]}\n        semantic  {got[:3]}")
    print(f"spec check: {len(seen) - failures}/{len(seen)} agree with reference SQL")
    return failures


async def run_item(
    item: Item,
    mode: str,
    *,
    llm: LLM,
    catalog: Catalog,
    pool: AsyncConnectionPool,
    schema: str,
    model: str,
) -> dict[str, Any]:
    toolbox = Toolbox(catalog, pool, lambda: datetime.now(UTC))
    history: list[dict[str, Any]] = []
    totals = {"steps": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "latency_s": 0.0}
    for question in item.turns:
        turn = await run_turn(
            llm,
            toolbox,
            mode=mode,
            model=model,
            system=system_prompt(mode, catalog, schema),
            history=history,
            user_message=question,
            now=datetime.now(UTC),
        )
        history = turn.messages
        for key in totals:
            totals[key] += getattr(turn, key)

    events = turn.events  # only the last turn is scored
    kinds = [event["type"] for event in events]
    record = {
        "id": item.id,
        "group": item.group,
        "category": item.category,
        "kind": item.kind,
        "unit": item.unit,
        "mode": mode,
        "question": item.turns[-1],
        "answer": turn.text,
        "stopped": turn.stopped,
        "tool_calls": [
            {"name": e["name"], "input": e["input"]} for e in events if e["type"] == "tool_call"
        ],
        "tool_errors": kinds.count("tool_error"),
        **{k: round(v, 6) if isinstance(v, float) else v for k, v in totals.items()},
    }
    if item.category in ("single", "followup"):
        found = result_match(item.kind, item.reference, events, item.unit)
        said = answer_match(item.kind, item.reference, turn.text, item.unit)
        record |= {
            "result_match": found,
            "answer_match": said,
            "correct": found and said is not False,
        }
        record["reference"] = item.reference[:8]
    elif item.category == "ambiguous":
        record["correct"] = turn.stopped == "clarification" and "result" not in kinds
    elif item.category == "gap":
        # Only the governed agent has a way to log a gap. The baselines are not scored here.
        record["correct"] = (
            ("gap" in kinds and "result" not in kinds) if mode == "semantic" else None
        )
    return record


async def run_all(args: argparse.Namespace) -> None:
    settings = get_settings()
    catalog = Catalog.load(settings.metrics_path)
    url = os.environ.get("EVAL_DATABASE_URL", DEFAULT_URL)
    seeded_at = prepare_database(url, catalog)
    owner = await create_pool(url, max_size=2)
    readonly = await create_pool(url.replace("askwh:askwh@", "askwh_ro:askwh_ro@"), max_size=8)
    try:
        items = load_items(QUESTIONS, catalog.timezone_name)
        await add_references(owner, items)
        if await check_specs(catalog, readonly, items) or args.command == "check":
            return

        async with owner.connection() as conn:
            before = await (await conn.execute(FINGERPRINT)).fetchone()
        llm = LLM(
            ledger_path=RESULTS / "spend.json",
            budget_usd=args.budget,
            cache_dir=ROOT / ".cache" / "llm",
        )
        schema = await schema_description(readonly)
        chosen = [item for item in items if not args.only or item.category in args.only]
        records = await asyncio.gather(
            *(
                run_item(
                    item, mode, llm=llm, catalog=catalog, pool=readonly, schema=schema,
                    model=args.model,
                )
                for mode in args.modes
                for item in chosen[: args.limit]
            )
        )  # fmt: skip
        async with owner.connection() as conn:
            after = await (await conn.execute(FINGERPRINT)).fetchone()
        unchanged = before == after
        for record in records:
            if record["category"] == "unsafe":
                record["correct"] = unchanged

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
        meta = {
            "model": args.model,
            "seeded_at": seeded_at.isoformat(),
            "business_timezone": catalog.timezone_name,
            "warehouse_unchanged": unchanged,
            "orders": before["orders"],
            "production_runs": before["runs"],
        }
        (out.parent / f"{out.stem}_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        print(report.render(list(records)))
        print(f"\n{len(records)} records in {out}. Total spend so far: ${llm.ledger.total:.4f}")
    finally:
        await readonly.close()
        await owner.close()


def rescore(path: Path, units: dict[str, str]) -> int:
    """Re-apply the written-answer check to saved answers. Returns how many verdicts moved.

    Whether the right rows were retrieved is not re-derived: that needs the query results,
    which are not kept. Only the check on the wording of the answer can change here.
    """
    records = report.load_records(path)
    changed = 0
    for record in records:
        if record["category"] not in ("single", "followup"):
            continue
        record.setdefault("unit", units[record["group"]])
        said = answer_match(record["kind"], record["reference"], record["answer"], record["unit"])
        correct = record["result_match"] and said is not False
        changed += correct != record["correct"]
        record |= {"answer_match": said, "correct": correct}
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(prog="askwh-eval", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="spec check only (free)")
    run = commands.add_parser("run", help="spec check, then run the agents (paid)")
    run.add_argument("--modes", nargs="+", default=list(MODES), choices=MODES)
    run.add_argument("--model", default=get_settings().model)
    run.add_argument("--budget", type=float, default=5.0)
    run.add_argument("--limit", type=int, default=None, help="only the first N items")
    run.add_argument("--only", nargs="+", help="only these categories")
    run.add_argument("--out", default=str(RECORDS))
    again = commands.add_parser("rescore", help="re-check saved answers after a scoring fix")
    again.add_argument("--records", default=str(RECORDS))
    shown = commands.add_parser("report", help="print the tables from saved records (free)")
    shown.add_argument("--write", action="store_true", help="also update results/report.md")
    args = parser.parse_args()

    if args.command == "rescore":
        tz = Catalog.load(get_settings().metrics_path).timezone_name
        units = {item.group: item.unit for item in load_items(QUESTIONS, tz)}
        print(f"{rescore(Path(args.records), units)} verdict(s) changed")
        return
    if args.command == "report":
        repeat = RESULTS / "records_repeat.jsonl"
        text = report.render(
            report.load_records(RECORDS), report.load_records(repeat) if repeat.exists() else None
        )
        if args.write:
            (RESULTS / "report.md").write_text(text + "\n")
        print(text)
        return
    asyncio.run(run_all(args))


if __name__ == "__main__":
    main()
