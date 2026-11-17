"""Simulated business data: a beverage maker's orders and production runs.

``generate`` builds 90 days of history ending at ``now`` from a seed, so the same seed gives
the same business. ``live_tick`` adds a few new rows stamped with the current time; the app
calls it on a timer to stand in for a change-data-capture stream into the warehouse.
"""

import argparse
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import psycopg

from askwh.config import get_settings
from askwh.db import all_ddl

PRODUCTS = [  # id, name, category, list price per case
    (1, "Sparkling Lime 12-pack", "Sparkling Water", 9.50),
    (2, "Sparkling Grapefruit 12-pack", "Sparkling Water", 9.50),
    (3, "Sparkling Original 24-pack", "Sparkling Water", 16.00),
    (4, "Cold Brew Black 6-pack", "Cold Brew", 14.00),
    (5, "Cold Brew Oat Latte 6-pack", "Cold Brew", 17.50),
    (6, "Orange Juice 1L 8-pack", "Juice", 22.00),
    (7, "Apple Juice 1L 8-pack", "Juice", 19.00),
    (8, "Green Juice 500ml 12-pack", "Juice", 31.00),
]
PLANTS = [  # id, name, region, typical scrap rate
    (1, "Dallas", "South", 0.012),
    (2, "Columbus", "Midwest", 0.018),
    (3, "Fresno", "West", 0.034),
]
REGIONS = [("Northeast", 0.30), ("Southeast", 0.22), ("Midwest", 0.26), ("West", 0.22)]
CHANNELS = {  # name: (share, quantity range, price factor)
    "retail": (0.50, (1, 12), 1.00),
    "wholesale": (0.18, (40, 240), 0.72),
    "online": (0.32, (1, 6), 1.05),
}
STATUSES = [("completed", 0.91), ("cancelled", 0.05), ("returned", 0.04)]
WEEKDAY_FACTOR = [1.00, 1.04, 1.06, 1.08, 1.15, 0.78, 0.62]  # Monday first
PRODUCT_WEIGHTS = [14, 11, 16, 12, 9, 15, 13, 10]
DAILY_ORDERS = 450


@dataclass(frozen=True)
class Dataset:
    orders: list[tuple]
    production: list[tuple]


def _pick(rng: random.Random, weighted: list[tuple]) -> str:
    return rng.choices([name for name, _ in weighted], [weight for _, weight in weighted])[0]


def _order(rng: random.Random, order_id: int, at: datetime) -> tuple:
    product = rng.choices(PRODUCTS, PRODUCT_WEIGHTS)[0]
    channel = rng.choices(list(CHANNELS), [share for share, _, _ in CHANNELS.values()])[0]
    _, (low, high), factor = CHANNELS[channel]
    quantity = rng.randint(low, high)
    unit_price = round(product[3] * factor, 2)
    gross = quantity * unit_price
    discounted = rng.random() < (0.45 if channel == "wholesale" else 0.15)
    discount = round(gross * rng.choice([0.05, 0.10, 0.15]), 2) if discounted else 0.0
    return (
        order_id,
        at,
        product[0],
        _pick(rng, REGIONS),
        channel,
        quantity,
        unit_price,
        discount,
        _pick(rng, STATUSES),
        rng.random() < 0.01,  # QA test order
        min(at + timedelta(seconds=rng.randint(2, 20)), datetime.now(UTC)),
    )


def _run(rng: random.Random, run_id: int, at: datetime, plant: tuple) -> tuple:
    produced = rng.randint(2000, 8000)
    scrap_rate = max(0.0, rng.gauss(plant[3], plant[3] / 3))
    downtime = rng.choice([0, 0, 0, 0, 5, 10, 15, 30, 45, 60])
    return (
        run_id,
        at,
        plant[0],
        rng.choice(PRODUCTS)[0],
        produced,
        int(produced * scrap_rate),
        downtime,
        min(at + timedelta(seconds=rng.randint(5, 60)), datetime.now(UTC)),
    )


def _order_times(rng: random.Random, day_start: datetime, count: int) -> Iterator[datetime]:
    """Order times across one business day, busiest from mid-morning to early evening."""
    for _ in range(count):
        hour = min(max(rng.gauss(13.5, 4.2), 0.0), 23.99)
        yield day_start + timedelta(hours=hour)


def generate(now: datetime, tz: ZoneInfo, seed: int = 7, days: int = 90) -> Dataset:
    """History for ``days`` days plus today up to ``now``. Nothing is dated after ``now``."""
    rng = random.Random(seed)
    today = now.astimezone(tz).date()
    orders: list[tuple] = []
    production: list[tuple] = []
    for days_ago in range(days, -1, -1):
        day = today - timedelta(days=days_ago)
        day_start = datetime(day.year, day.month, day.day, tzinfo=tz)
        count = int(DAILY_ORDERS * WEEKDAY_FACTOR[day.weekday()] * rng.uniform(0.92, 1.08))
        for at in sorted(_order_times(rng, day_start, count)):
            row = _order(rng, len(orders) + 1, at.astimezone(UTC))
            if at <= now:  # draw the row either way, so history is the same at any hour
                orders.append(row)
        for plant in PLANTS:
            for shift_start in (6, 14, 22):
                for _ in range(3):
                    at = day_start + timedelta(hours=shift_start + rng.uniform(1.5, 7.5))
                    row = _run(rng, len(production) + 1, at.astimezone(UTC), plant)
                    if at <= now:
                        production.append(row)
    return Dataset(orders, production)


def load(conn: psycopg.Connection, dataset: Dataset, now: datetime, seed: int) -> None:
    """Replace the warehouse contents with ``dataset``."""
    for statement in all_ddl():
        conn.execute(statement)
    conn.execute(
        "TRUNCATE analytics.fact_orders, analytics.fact_production, "
        "analytics.dim_product, analytics.dim_plant, analytics.ingest_watermark CASCADE"
    )
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO analytics.dim_product VALUES (%s, %s, %s, %s)", PRODUCTS)
        cur.executemany(
            "INSERT INTO analytics.dim_plant VALUES (%s, %s, %s)", [p[:3] for p in PLANTS]
        )
        with cur.copy("COPY analytics.fact_orders FROM STDIN") as copy:
            for row in dataset.orders:
                copy.write_row(row)
        with cur.copy("COPY analytics.fact_production FROM STDIN") as copy:
            for row in dataset.production:
                copy.write_row(row)
        _advance_watermark(cur, now)
        for key, value in {"seeded_at": now.isoformat(), "seed": str(seed)}.items():
            cur.execute(
                "INSERT INTO app.meta VALUES (%s, %s) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value),
            )
    conn.execute("ANALYZE")


def _advance_watermark(cur: psycopg.Cursor, now: datetime) -> None:
    for source in ("orders", "production"):
        cur.execute(
            "INSERT INTO analytics.ingest_watermark VALUES (%s, %s) "
            "ON CONFLICT (source) DO UPDATE SET watermark = EXCLUDED.watermark",
            (source, now),
        )


# Chance of a new order on each two-second tick. About 0.7 orders a minute, which is the
# midday rate of the generated history, so "today" grows at a believable pace.
LIVE_ORDER_CHANCE = 0.024
LIVE_RUN_CHANCE = 0.002


def live_tick(conn: psycopg.Connection, rng: random.Random) -> int:
    """One pass of the simulated feed: maybe a new order or production run, stamped with the
    current time, and always an advanced watermark."""
    now = datetime.now(UTC)
    with conn.cursor() as cur:
        rows = []
        if rng.random() < LIVE_ORDER_CHANCE:
            next_order = cur.execute(
                "SELECT COALESCE(MAX(order_id), 0) + 1 FROM analytics.fact_orders"
            ).fetchone()[0]
            rows = [_order(rng, next_order, now)]
            cur.executemany(
                "INSERT INTO analytics.fact_orders VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows
            )
        _advance_watermark(cur, now)
        if rng.random() < LIVE_RUN_CHANCE:
            next_run = cur.execute(
                "SELECT COALESCE(MAX(run_id), 0) + 1 FROM analytics.fact_production"
            ).fetchone()[0]
            cur.execute(
                "INSERT INTO analytics.fact_production VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                _run(rng, next_run, now, rng.choice(PLANTS)),
            )
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load simulated data into the warehouse.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    from askwh.semantic import Catalog

    settings = get_settings()
    tz = Catalog.load(settings.metrics_path).tz
    now = datetime.now(UTC)
    dataset = generate(now, tz, seed=args.seed, days=args.days)
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        load(conn, dataset, now, args.seed)
    print(f"Loaded {len(dataset.orders):,} orders and {len(dataset.production):,} production runs.")


if __name__ == "__main__":
    main()
