"""Tests against PostgreSQL with eight known orders and three known production runs.

Every expected number here was worked out by hand from the rows in conftest.py.
Run with ``make test-integration``; skipped when TEST_DATABASE_URL is not set.
"""

import json

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from askwh.api import Deps, create_app
from askwh.config import Settings
from askwh.db import schema_description
from askwh.semantic import QuerySpec, SpecError
from askwh.subscriptions import AlertSpec, DigestSpec, Subscriptions, describe_result
from askwh.tools import Toolbox
from tests.conftest import NOW, FakeLLM, needs_db, text, tool, turn

pytestmark = needs_db


def spec(metric="net_sales", preset="today", **fields) -> QuerySpec:
    return QuerySpec.model_validate({"metric": metric, "time_range": {"preset": preset}, **fields})


async def value(catalog, readonly, metric, preset="today", **fields):
    result = await catalog.run(readonly, spec(metric, preset, **fields), NOW)
    return result.rows[0]["value"]


def box(catalog, pools, **kwargs) -> Toolbox:
    owner, readonly = pools
    return Toolbox(catalog, readonly, lambda: NOW, app_pool=owner, **kwargs)


# --- the metric definitions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("metric", "expected"),
    [
        ("net_sales", 1515.60),  # orders 1 and 2: completed, after discounts
        ("gross_sales", 1721.00),  # orders 1, 2 and the returned order 4, before discounts
        ("discount_total", 163.40),
        ("order_count", 2),
        ("units_sold", 110),
        ("average_order_value", 757.80),
        ("cancellation_rate", 0.25),  # 1 cancelled of 4 real orders; the test order is not counted
        ("return_rate", 1 / 3),  # 1 returned of 3 fulfilled; the cancelled order is in neither side
        ("units_produced", 4000),
        ("good_units", 3840),
        ("scrap_rate", 0.04),  # 160 of 4000, not the average of 1% and 5%
        ("downtime_minutes", 30),
    ],
)
async def test_every_metric_matches_its_definition(catalog, pools, metric, expected):
    assert await value(catalog, pools[1], metric) == pytest.approx(expected)


async def test_business_day_boundary_and_other_periods(catalog, pools):
    readonly = pools[1]
    # Order 6 was placed at 05:30 UTC today, which is 23:30 yesterday in Chicago.
    assert await value(catalog, readonly, "net_sales", "yesterday") == pytest.approx(202.20)
    assert await value(catalog, readonly, "net_sales", "this_week") == pytest.approx(1717.80)
    assert await value(catalog, readonly, "net_sales", "last_week") == pytest.approx(16.00)
    assert await value(catalog, readonly, "net_sales", "last_7_days") == pytest.approx(218.20)
    assert await value(catalog, readonly, "net_sales", "last_month") == 0


async def test_breakdowns_filters_and_time_series(catalog, pools):
    readonly = pools[1]
    by_region = await catalog.run(readonly, spec(dimensions=["region"]), NOW)
    assert by_region.rows == [
        {"region": "Midwest", "value": 1425.60},
        {"region": "West", "value": 90.00},
        {"region": "Northeast", "value": 0.0},
    ]
    assert await value(
        catalog, readonly, "net_sales", filters=[{"dimension": "category", "values": ["Juice"]}]
    ) == pytest.approx(1425.60)

    by_plant = await catalog.run(readonly, spec("scrap_rate", dimensions=["plant"]), NOW)
    assert [(r["plant"], round(r["value"], 4)) for r in by_plant.rows] == [
        ("Fresno", 0.05),
        ("Dallas", 0.01),
    ]

    daily = await catalog.run(readonly, spec("net_sales", "this_week", grain="day"), NOW)
    assert [(r["period"][:10], r["value"]) for r in daily.rows] == [
        ("2026-11-16", 202.20),
        ("2026-11-17", 1515.60),
    ]

    capped = await catalog.run(readonly, spec(dimensions=["region"], limit=2), NOW)
    assert len(capped.rows) == 2 and capped.truncated


async def test_comparison_with_the_previous_period(catalog, pools):
    result = await catalog.run(pools[1], spec("net_sales", "yesterday", compare_previous=True), NOW)
    assert result.comparison["previous_value"] == 0  # nothing was sold on Sunday the 15th
    assert result.comparison["change"] == pytest.approx(202.20)
    assert result.comparison["change_pct"] is None  # no percentage against zero

    weekly = await catalog.run(pools[1], spec("net_sales", "this_week", compare_previous=True), NOW)
    assert weekly.comparison["previous_value"] == 0  # order 8 came later in that week
    scrap = await catalog.run(pools[1], spec("scrap_rate", "today", compare_previous=True), NOW)
    assert scrap.comparison["previous_value"] is None  # no production by noon yesterday
    assert describe_result(scrap) == "Scrap rate, today (Tue 17 Nov), up to 12:00 CST: 4.0%"


async def test_unknown_filter_value_is_refused_with_the_valid_ones(catalog, pools):
    with pytest.raises(SpecError, match=r"Unknown region value\(s\) \['Northwest'\]. Valid values"):
        await catalog.run(
            pools[1], spec(filters=[{"dimension": "region", "values": ["Northwest"]}]), NOW
        )


# --- the read-only role -------------------------------------------------------------------


async def test_the_agents_role_cannot_write_or_see_app_tables(pools):
    _, readonly = pools
    async with readonly.connection() as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            await conn.execute("DELETE FROM analytics.fact_orders")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("SELECT * FROM app.threads")
        # Even with the read-only setting switched off, the role has no write privilege.
        await conn.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute("DELETE FROM analytics.fact_orders")


async def test_schema_description_carries_the_comments(pools):
    described = await schema_description(pools[1])
    assert "TABLE analytics.fact_orders  -- One row per order line." in described
    assert "is_test boolean  -- True for test orders created by QA." in described


# --- the tools ----------------------------------------------------------------------------


async def test_query_metric_tool_returns_rows_definition_sql_and_freshness(catalog, pools):
    outcome = await box(catalog, pools).call(
        "query_metric", {"metric": "net_sales", "time_range": {"preset": "today"}}
    )
    seen = json.loads(outcome.content)
    assert seen["rows"] == [{"value": 1515.60}] and seen["unit"] == "usd"
    assert seen["time_range"] == "today (Tue 17 Nov), up to 12:00 CST"
    assert (seen["lag_seconds"], seen["stale"]) == (30, False)  # the older of the two watermarks
    assert "Cancelled and returned orders are excluded" in seen["definition"]
    assert outcome.event["governed"] and "FROM analytics.fact_orders o" in outcome.event["sql"]
    assert outcome.event["spec"] == {"metric": "net_sales", "time_range": {"preset": "today"}}


async def test_stale_data_is_flagged(catalog, pools):
    outcome = await box(catalog, pools, stale_after_seconds=10).call(
        "query_metric", {"metric": "order_count", "time_range": {"preset": "today"}}
    )
    assert json.loads(outcome.content)["stale"] is True


async def test_bad_requests_come_back_as_errors_the_model_can_read(catalog, pools):
    toolbox = box(catalog, pools)
    unknown = await toolbox.call(
        "query_metric", {"metric": "profit", "time_range": {"preset": "today"}}
    )
    malformed = await toolbox.call("query_metric", {"metric": "net_sales", "time_range": {}})
    missing = await toolbox.call("no_such_tool", {})

    assert unknown.is_error and "Unknown metric 'profit'" in unknown.content
    assert malformed.is_error and malformed.content.startswith("Invalid request: time_range")
    assert missing.is_error and "Unknown tool" in missing.content

    async with pools[0].connection() as conn:
        rows = await (
            await conn.execute("SELECT tool, ok FROM app.audit_log ORDER BY id")
        ).fetchall()
    assert [(r["tool"], r["ok"]) for r in rows] == [
        ("query_metric", False),
        ("query_metric", False),
        ("no_such_tool", False),
    ]


async def test_run_sql_tool_runs_selects_and_blocks_the_rest(catalog, pools):
    toolbox = box(catalog, pools)
    ok = await toolbox.call(
        "run_sql", {"sql": "SELECT count(*) AS n FROM analytics.fact_orders WHERE NOT is_test"}
    )
    assert json.loads(ok.content)["rows"] == [{"n": 7}] and ok.event["governed"] is False

    blocked = await toolbox.call("run_sql", {"sql": "DELETE FROM analytics.fact_orders"})
    broken = await toolbox.call("run_sql", {"sql": "SELECT nope FROM analytics.fact_orders"})
    assert blocked.is_error and "Only SELECT" in blocked.content
    assert broken.is_error and "The database rejected the query" in broken.content

    async with pools[1].connection() as conn:
        count = await (
            await conn.execute("SELECT count(*) AS n FROM analytics.fact_orders")
        ).fetchone()
    assert count["n"] == 8


# --- digests and alerts -------------------------------------------------------------------


async def test_digest_is_proposed_approved_and_delivered(catalog, pools):
    owner, readonly = pools
    subscriptions = Subscriptions(owner, readonly, catalog)
    toolbox = box(catalog, pools, subscriptions=subscriptions, thread_id="t1")

    outcome = await toolbox.call(
        "propose_digest",
        {
            "name": "Morning sales",
            "query": {
                "metric": "net_sales",
                "time_range": {"preset": "yesterday"},
                "dimensions": ["region"],
            },
            "frequency": "daily",
            "time": "08:00",
        },
    )
    proposal_id = outcome.event["id"]
    assert "not active" in outcome.content

    assert await subscriptions.run_due(NOW) == 0  # nothing runs before approval
    approved = await subscriptions.decide(proposal_id, True, NOW)
    assert approved["status"] == "active"
    assert approved["next_run_at"].isoformat() == "2026-11-18T14:00:00+00:00"  # 08:00 Chicago
    assert await subscriptions.decide(proposal_id, True, NOW) is None  # cannot decide twice

    assert await subscriptions.run_due(NOW) == 0  # not due until tomorrow morning
    assert await subscriptions.run_due(approved["next_run_at"]) == 1
    note = (await subscriptions.notifications())[0]
    assert note["title"] == "Morning sales" and note["delivered_to"] == "in-app"
    # Run at 08:00 on the 18th, "yesterday" is the 17th.
    assert note["body"].startswith("Net sales, yesterday (Tue 17 Nov):\n  Midwest: $1,426")


async def test_alert_fires_once_when_the_threshold_is_crossed(catalog, pools):
    owner, readonly = pools
    subscriptions = Subscriptions(owner, readonly, catalog)
    alert = AlertSpec(query=spec("scrap_rate"), direction="above", threshold=0.03)
    alert_id = await subscriptions.propose("alert", "Scrap too high", alert, None)
    await subscriptions.decide(alert_id, True, NOW)

    assert await subscriptions.run_due(NOW) == 1  # 4.0% is above 3%
    assert await subscriptions.run_due(NOW.replace(hour=19)) == 0  # still above: no repeat
    note = (await subscriptions.notifications())[0]
    assert note["title"] == "Alert: Scrap too high"
    assert "Scrap rate, today" in note["body"] and "above the threshold of 3.0%" in note["body"]

    quiet = AlertSpec(query=spec("scrap_rate"), direction="above", threshold=0.10)
    quiet_id = await subscriptions.propose("alert", "Never", quiet, None)
    await subscriptions.decide(quiet_id, True, NOW)
    assert await subscriptions.run_due(NOW) == 0

    assert await subscriptions.run_now(alert_id, NOW) is False
    assert await subscriptions.cancel(alert_id) and not await subscriptions.cancel(alert_id)


async def test_declined_and_invalid_proposals(catalog, pools):
    owner, readonly = pools
    subscriptions = Subscriptions(owner, readonly, catalog)
    toolbox = box(catalog, pools, subscriptions=subscriptions)

    grouped = await toolbox.call(
        "propose_alert",
        {
            "name": "Bad",
            "query": {
                "metric": "net_sales",
                "time_range": {"preset": "today"},
                "dimensions": ["region"],
            },
            "direction": "below",
            "threshold": 100,
        },
    )
    assert grouped.is_error and "single figure" in grouped.content

    digest = DigestSpec(query=spec(), schedule={"frequency": "hourly"})
    digest_id = await subscriptions.propose("digest", "Hourly", digest, None)
    declined = await subscriptions.decide(digest_id, False, NOW)
    assert declined["status"] == "declined" and await subscriptions.open_items() == []


async def test_webhook_delivery_and_its_failure(catalog, pools, monkeypatch):
    owner, readonly = pools
    posted = []

    class FakeClient:
        def __init__(self, **_):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, json):
            posted.append((url, json))
            if "broken" in url:
                raise httpx.ConnectError("no route")
            return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    digest = DigestSpec(query=spec("order_count"), schedule={"frequency": "hourly"})

    for url, expected in [
        ("https://hooks.example/ok", "webhook"),
        ("https://hooks.example/broken", "in-app (webhook failed)"),
    ]:
        subscriptions = Subscriptions(owner, readonly, catalog, webhook_url=url)
        digest_id = await subscriptions.propose("digest", "Hourly orders", digest, None)
        await subscriptions.decide(digest_id, True, NOW)
        assert await subscriptions.run_now(digest_id, NOW) is True
        # A failed webhook must not lose the message: it is still kept in the app.
        assert (await subscriptions.notifications())[0]["delivered_to"] == expected

    assert posted[0] == (
        "https://hooks.example/ok",
        {"text": "*Hourly orders*\nOrders, today (Tue 17 Nov), up to 12:00 CST: 2"},
    )


# --- the web app --------------------------------------------------------------------------


def events(body: str) -> list[tuple[str, dict]]:
    parsed = []
    for block in body.strip().split("\n\n"):
        name, data = block.split("\n", 1)
        parsed.append((name.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return parsed


async def test_chat_endpoint_streams_a_governed_answer_and_remembers_the_thread(catalog, pools):
    owner, readonly = pools
    llm = FakeLLM([
        turn(tool("query_metric", metric="net_sales", time_range={"preset": "today"})),
        turn(text("Net sales today are $1,516.")),
        turn(text("As I said, $1,516.")),
    ])  # fmt: skip
    deps = Deps(
        settings=Settings(_env_file=None),
        catalog=catalog,
        pool=owner,
        readonly_pool=readonly,
        llm=llm,
        subscriptions=Subscriptions(owner, readonly, catalog),
        schema="",
    )
    with TestClient(create_app(deps)) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert "Ask the warehouse" in client.get("/").text
        status = client.get("/api/status").json()
        assert status["company"] == "Northfield Beverages" and len(status["metrics"]) == 12

        first = events(client.post("/api/chat", json={"message": "Sales today?"}).text)
        kinds = [name for name, _ in first]
        assert kinds == ["tool_call", "result", "text", "done"]
        result = dict(first)["result"]
        assert result["rows"] == [{"value": 1515.6}] and result["governed"] is True
        done = dict(first)["done"]
        assert done["text"] == "Net sales today are $1,516." and done["stopped"] == "answer"

        client.post("/api/chat", json={"message": "Again?", "thread_id": done["thread_id"]})
        # The follow-up request carried the whole first exchange.
        assert [m["role"] for m in llm.requests[2]["messages"]] == [
            "user", "assistant", "user", "assistant", "user",
        ]  # fmt: skip

        assert client.post("/api/chat", json={"message": ""}).status_code == 422
        assert client.post("/api/subscriptions/999/approve").status_code == 409
        assert client.get("/api/subscriptions").json() == []
        assert client.get("/api/notifications").json() == []
        assert client.get("/api/gaps").json() == []
