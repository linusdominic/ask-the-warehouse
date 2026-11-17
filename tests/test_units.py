"""Tests that need no database and no network: time, compilation, SQL checks, schedules."""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from askwh import seed, sqlguard
from askwh.clock import TimeRange, previous, resolve
from askwh.semantic import QuerySpec, SpecError
from askwh.subscriptions import (
    AlertSpec,
    Schedule,
    format_value,
    is_breached,
    next_run,
    spec_summary,
)
from tests.conftest import NOW

TZ = ZoneInfo("America/Chicago")


def local(year, month, day, hour=0, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def window(preset: str, now: datetime = NOW):
    return resolve(TimeRange(preset=preset), now, TZ)


# --- business time ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("preset", "start", "end"),
    [
        ("today", local(2026, 11, 17), NOW),
        ("yesterday", local(2026, 11, 16), local(2026, 11, 17)),
        ("this_week", local(2026, 11, 16), NOW),  # the 17th is a Tuesday; weeks start Monday
        ("last_week", local(2026, 11, 9), local(2026, 11, 16)),
        ("this_month", local(2026, 11, 1), NOW),
        ("last_month", local(2026, 10, 1), local(2026, 11, 1)),
        ("last_7_days", local(2026, 11, 10), local(2026, 11, 17)),
        ("last_30_days", local(2026, 10, 18), local(2026, 11, 17)),
    ],
)
def test_presets_resolve_to_exact_bounds(preset, start, end):
    resolved = window(preset)
    assert (resolved.start, resolved.end) == (start, end)


def test_the_business_day_is_not_the_utc_day():
    # 03:00 UTC on the 18th is still 21:00 on the 17th in Chicago.
    late = datetime(2026, 11, 18, 3, 0, tzinfo=UTC)
    assert window("today", late).start == local(2026, 11, 17)


def test_january_looks_back_into_the_previous_year():
    january = datetime(2027, 1, 5, 18, 0, tzinfo=UTC)
    assert window("last_month", january).start == local(2026, 12, 1)


def test_day_with_a_clock_change_is_25_hours_long():
    # Clocks went back on Sunday 1 November 2026.
    monday = datetime(2026, 11, 2, 18, 0, tzinfo=UTC)
    resolved = window("yesterday", monday)
    # Compare in UTC: subtracting two times in the same zone ignores the clock change.
    assert resolved.end.astimezone(UTC) - resolved.start.astimezone(UTC) == timedelta(hours=25)


def test_explicit_dates_are_inclusive():
    resolved = resolve(TimeRange(start_date=date(2026, 11, 3), end_date=date(2026, 11, 5)), NOW, TZ)
    assert (resolved.start, resolved.end) == (local(2026, 11, 3), local(2026, 11, 6))
    assert resolved.label == "03 Nov 2026 to 05 Nov 2026"


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"preset": "today", "start_date": "2026-11-01", "end_date": "2026-11-02"},
        {"start_date": "2026-11-01"},
        {"start_date": "2026-11-05", "end_date": "2026-11-01"},
        {"preset": "last_quarter"},
    ],
)
def test_time_range_must_be_one_well_formed_thing(bad):
    with pytest.raises(ValidationError):
        TimeRange.model_validate(bad)


def test_today_is_compared_with_the_same_point_yesterday():
    time_range = TimeRange(preset="today")
    earlier = previous(time_range, resolve(time_range, NOW, TZ), TZ)
    assert (earlier.start, earlier.end) == (local(2026, 11, 16), local(2026, 11, 16, 12))
    assert earlier.label == "the previous period (Mon 16 Nov up to 12:00)"


def test_month_to_date_is_compared_with_the_same_point_last_month():
    # The clock change on 1 November must not move the comparison by an hour.
    time_range = TimeRange(preset="this_month")
    earlier = previous(time_range, resolve(time_range, NOW, TZ), TZ)
    assert (earlier.start, earlier.end) == (local(2026, 10, 1), local(2026, 10, 17, 12))


def test_whole_periods_are_compared_with_the_period_before():
    for preset, start, end in [
        ("yesterday", local(2026, 11, 15), local(2026, 11, 16)),
        ("last_week", local(2026, 11, 2), local(2026, 11, 9)),
        ("last_month", local(2026, 9, 1), local(2026, 10, 1)),
    ]:
        time_range = TimeRange(preset=preset)
        earlier = previous(time_range, resolve(time_range, NOW, TZ), TZ)
        assert (earlier.start, earlier.end) == (start, end), preset


# --- the semantic layer's compiler -------------------------------------------------------


def spec(**fields) -> QuerySpec:
    return QuerySpec.model_validate(
        {"metric": "net_sales", "time_range": {"preset": "today"}, **fields}
    )


def test_scalar_query_has_the_base_filter_and_time_bounds(catalog):
    compiled = catalog.compile(spec(), NOW)
    assert "FROM analytics.fact_orders o" in compiled.sql
    assert "WHERE o.is_test = false" in compiled.sql
    assert "o.ordered_at >= %(start)s" in compiled.sql and "o.ordered_at < %(end)s" in compiled.sql
    assert "GROUP BY" not in compiled.sql and "JOIN" not in compiled.sql
    assert compiled.params["start"] == local(2026, 11, 17) and compiled.params["end"] == NOW
    assert compiled.columns == ["value"]


def test_dimensions_filters_and_grain_compile_with_only_the_joins_they_need(catalog):
    compiled = catalog.compile(
        spec(
            dimensions=["category"],
            filters=[{"dimension": "region", "op": "not_in", "values": ["West"]}],
            grain="day",
        ),
        NOW,
    )
    assert compiled.sql.count("JOIN analytics.dim_product") == 1
    assert "date_trunc('day', o.ordered_at AT TIME ZONE %(tz)s) AS period" in compiled.sql
    assert "NOT o.region = ANY(%(f0)s)" in compiled.sql
    assert "GROUP BY 1, 2" in compiled.sql and "ORDER BY period" in compiled.sql
    assert compiled.params["f0"] == ["West"] and compiled.params["tz"] == "America/Chicago"
    assert compiled.columns == ["period", "category", "value"]


def test_display_sql_writes_the_parameters_in(catalog):
    shown = catalog.compile(
        spec(filters=[{"dimension": "region", "values": ["West", "O'Hare"]}]), NOW
    ).display_sql
    assert "%(" not in shown
    assert "ARRAY['West', 'O''Hare']" in shown
    assert "TIMESTAMPTZ '2026-11-17T00:00:00-06:00'" in shown


def test_ratio_metric_keeps_numerator_and_denominator_in_one_aggregate(catalog):
    compiled = catalog.compile(spec(metric="scrap_rate", dimensions=["plant"]), NOW)
    assert "SUM(r.units_scrapped)::numeric / NULLIF(SUM(r.units_produced), 0) AS value" in (
        compiled.sql
    )
    assert "is_test" not in compiled.sql  # that filter belongs to orders only


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"metric": "profit"}, "Unknown metric 'profit'"),
        ({"dimensions": ["plant"]}, "'plant' is not a dimension of net_sales"),
        ({"filters": [{"dimension": "plant", "values": ["Dallas"]}]}, "not a dimension"),
        ({"dimensions": ["region", "region"]}, "listed twice"),
        ({"dimensions": ["region", "channel", "product", "category"]}, "At most 3 dimensions"),
        ({"compare_previous": True, "dimensions": ["region"]}, "single figure"),
    ],
)
def test_requests_outside_the_catalogue_are_refused(catalog, fields, message):
    with pytest.raises(SpecError, match=message):
        catalog.compile(spec(**fields), NOW)


def test_limit_is_capped_by_validation():
    with pytest.raises(ValidationError):
        spec(limit=5000)


def test_catalogue_text_names_definitions_synonyms_and_time_ranges(catalog):
    described = catalog.describe()
    assert "- net_sales [usd] (also called: sales, revenue, total sales):" in described
    assert "Cancelled and returned orders are excluded" in described
    assert "- last_7_days: the 7 complete days before today (today excluded)" in described
    assert "business time zone America/Chicago, weeks start Monday" in described


# --- the SQL guard used by the baselines -------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM analytics.fact_orders",
        "WITH t AS (SELECT region, sum(quantity) q FROM analytics.fact_orders GROUP BY 1) "
        "SELECT * FROM t ORDER BY q DESC",
        "SELECT 1 FROM analytics.fact_orders UNION ALL SELECT 2 FROM analytics.dim_plant",
        "SELECT date_trunc('day', ordered_at AT TIME ZONE 'America/Chicago') FROM "
        "analytics.fact_orders o JOIN analytics.dim_product p USING (product_id)",
    ],
)
def test_read_only_queries_on_analytics_are_allowed_and_capped(sql):
    guarded = sqlguard.validate(sql)
    assert guarded.startswith("SELECT * FROM (") and guarded.endswith("AS guarded LIMIT 501")


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("DELETE FROM analytics.fact_orders", "Only SELECT"),
        ("UPDATE analytics.fact_orders SET quantity = 0", "Only SELECT"),
        ("DROP TABLE analytics.fact_orders", "Only SELECT"),
        ("SELECT 1 FROM analytics.fact_orders; DROP TABLE analytics.fact_orders", "exactly one"),
        (
            "WITH d AS (DELETE FROM analytics.fact_orders RETURNING *) SELECT * FROM d",
            "Only SELECT",
        ),
        ("SELECT * FROM app.threads", "not available"),
        ("SELECT * FROM pg_catalog.pg_user", "not available"),
        ("SELECT * FROM fact_orders", "qualify them"),
        ("SELECT pg_sleep(60) FROM analytics.fact_orders", "pg_sleep is not allowed"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file is not allowed"),
        ("SELEC oops", "Could not parse|Only SELECT"),
    ],
)
def test_everything_else_is_rejected(sql, message):
    with pytest.raises(sqlguard.SqlRejected, match=message):
        sqlguard.validate(sql)


# --- simulated data ---------------------------------------------------------------------


def test_generated_history_is_repeatable_and_never_in_the_future():
    first = seed.generate(NOW, TZ, seed=7, days=10)
    again = seed.generate(NOW, TZ, seed=7, days=10)
    other = seed.generate(NOW, TZ, seed=8, days=10)

    def facts(rows):  # everything except the arrival time, which is stamped at load
        return [row[:-1] for row in rows]

    assert facts(first.orders) == facts(again.orders)
    assert facts(first.orders) != facts(other.orders)
    assert max(row[1] for row in first.orders) <= NOW
    assert max(row[1] for row in first.production) <= NOW
    assert [row[0] for row in first.orders] == list(range(1, len(first.orders) + 1))
    assert {row[8] for row in first.orders} == {"completed", "cancelled", "returned"}
    assert 3000 < len(first.orders) < 6000  # about 450 a day for eleven days


def test_an_earlier_now_gives_a_prefix_of_the_same_history():
    morning = seed.generate(NOW.replace(hour=15), TZ, seed=7, days=3)
    noon = seed.generate(NOW, TZ, seed=7, days=3)
    finished = [row[1:-1] for row in morning.orders if row[1].date() < NOW.date()]
    assert (
        finished
        == [row[1:-1] for row in noon.orders if row[1].date() < NOW.date()][: len(finished)]
    )
    assert len(morning.orders) < len(noon.orders)


# --- digests and alerts ------------------------------------------------------------------


def test_next_run_times():
    daily = Schedule(frequency="daily", time="08:00")
    assert next_run(daily, NOW, TZ) == local(2026, 11, 18, 8)  # 08:00 today has passed
    assert next_run(daily, local(2026, 11, 17, 7).astimezone(UTC), TZ) == local(2026, 11, 17, 8)
    weekly = Schedule(frequency="weekly", time="09:30", weekday=0)
    assert next_run(weekly, NOW, TZ) == local(2026, 11, 23, 9, 30)
    assert next_run(Schedule(frequency="hourly"), NOW, TZ) == local(2026, 11, 17, 13)


def test_schedule_time_must_be_a_real_time():
    with pytest.raises(ValidationError):
        Schedule(frequency="daily", time="25:00")


def test_alert_breach_and_formatting():
    query = {"metric": "scrap_rate", "time_range": {"preset": "today"}}
    above = AlertSpec(query=query, direction="above", threshold=0.03)
    below = AlertSpec(query=query, direction="below", threshold=0.03)
    assert (
        is_breached(above, 0.04) and not is_breached(above, 0.03) and not is_breached(above, None)
    )
    assert is_breached(below, 0.02) and not is_breached(below, 0.04)

    assert format_value(1234567.891, "usd") == "$1,234,568"
    assert format_value(12.5, "usd") == "$12.50"
    assert format_value(0.0338, "ratio") == "3.4%"
    assert format_value(12400, "cases") == "12,400 cases"
    assert format_value(42, "count") == "42"
    assert format_value(None, "usd") == "no data"


def test_subscription_summaries_read_naturally():
    query = {"metric": "net_sales", "time_range": {"preset": "yesterday"}, "dimensions": ["region"]}
    digest = {"query": query, "schedule": {"frequency": "weekly", "time": "08:00", "weekday": 4}}
    assert (
        spec_summary("digest", digest) == "net_sales by region, yesterday, weekly on Fri at 08:00"
    )
    alert = {
        "query": {"metric": "scrap_rate", "time_range": {"preset": "today"}},
        "direction": "above",
        "threshold": 0.03,
        "check_every_minutes": 15,
    }
    assert spec_summary("alert", alert) == "scrap_rate, today above 0.03, checked every 15 min"
