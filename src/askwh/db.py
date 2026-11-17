"""PostgreSQL: the warehouse schema, the app's own tables, and the two connection pools.

Two roles matter. ``askwh`` owns everything and is used to load data and keep app state.
``askwh_ro`` can only SELECT from the ``analytics`` schema, and every query the agent runs
goes through it. That grant is the real safety control; SQL validation sits in front of it.
"""

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

READONLY_ROLE = "askwh_ro"

ANALYTICS_DDL = (
    "CREATE SCHEMA IF NOT EXISTS analytics",
    """
    CREATE TABLE IF NOT EXISTS analytics.dim_product (
        product_id   integer PRIMARY KEY,
        product_name text NOT NULL,
        category     text NOT NULL,
        list_price   numeric(10, 2) NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analytics.dim_plant (
        plant_id     integer PRIMARY KEY,
        plant_name   text NOT NULL,
        plant_region text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analytics.fact_orders (
        order_id        bigint PRIMARY KEY,
        ordered_at      timestamptz NOT NULL,
        product_id      integer NOT NULL REFERENCES analytics.dim_product,
        region          text NOT NULL,
        channel         text NOT NULL,
        quantity        integer NOT NULL,
        unit_price      numeric(10, 2) NOT NULL,
        discount_amount numeric(12, 2) NOT NULL,
        status          text NOT NULL,
        is_test         boolean NOT NULL DEFAULT false,
        ingested_at     timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS fact_orders_ordered_at ON analytics.fact_orders (ordered_at)",
    "CREATE INDEX IF NOT EXISTS fact_orders_ingested_at ON analytics.fact_orders (ingested_at)",
    """
    CREATE TABLE IF NOT EXISTS analytics.fact_production (
        run_id           bigint PRIMARY KEY,
        produced_at      timestamptz NOT NULL,
        plant_id         integer NOT NULL REFERENCES analytics.dim_plant,
        product_id       integer NOT NULL REFERENCES analytics.dim_product,
        units_produced   integer NOT NULL,
        units_scrapped   integer NOT NULL,
        downtime_minutes integer NOT NULL,
        ingested_at      timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS fact_production_produced_at "
    "ON analytics.fact_production (produced_at)",
    # The ingest pipeline's watermark: each source is complete up to this time. Freshness is
    # read from here, so a quiet period with no new orders is not mistaken for a stalled feed.
    """
    CREATE TABLE IF NOT EXISTS analytics.ingest_watermark (
        source    text PRIMARY KEY,
        watermark timestamptz NOT NULL
    )
    """,
)

# What a reasonably documented warehouse tells an analyst. The text-to-SQL baselines see
# these comments; they do not see the metric definitions in semantic/metrics.yml.
COMMENTS = {
    "analytics.fact_orders": "One row per order line. Timestamps are stored in UTC; the "
    "business reports in the America/Chicago time zone.",
    "analytics.fact_orders.ordered_at": "When the order was placed (UTC).",
    "analytics.fact_orders.region": "Customer sales region: Northeast, Southeast, Midwest, West.",
    "analytics.fact_orders.channel": "Sales channel: retail, wholesale, online.",
    "analytics.fact_orders.quantity": "Cases ordered.",
    "analytics.fact_orders.unit_price": "Price per case charged on this order, in USD.",
    "analytics.fact_orders.discount_amount": "Total discount on the order line, in USD.",
    "analytics.fact_orders.status": "completed, cancelled or returned.",
    "analytics.fact_orders.is_test": "True for test orders created by QA.",
    "analytics.fact_orders.ingested_at": "When the row arrived in the warehouse.",
    "analytics.fact_production": "One row per production run. Timestamps are stored in UTC; "
    "the business reports in the America/Chicago time zone.",
    "analytics.fact_production.produced_at": "When the run finished (UTC).",
    "analytics.fact_production.units_produced": "Cases produced, including scrapped ones.",
    "analytics.fact_production.units_scrapped": "Cases rejected by quality control.",
    "analytics.fact_production.downtime_minutes": "Unplanned line stoppage during the run.",
    "analytics.dim_product.list_price": "Standard price per case in USD, before channel pricing.",
    "analytics.dim_plant.plant_region": "Where the plant is: South, Midwest or West.",
    "analytics.ingest_watermark": "Pipeline bookkeeping, not business data: each source "
    "is loaded up to this time.",
}

APP_DDL = (
    "CREATE SCHEMA IF NOT EXISTS app",
    """
    CREATE TABLE IF NOT EXISTS app.meta (
        key   text PRIMARY KEY,
        value text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app.threads (
        thread_id  text PRIMARY KEY,
        messages   jsonb NOT NULL DEFAULT '[]',
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app.subscriptions (
        id          bigserial PRIMARY KEY,
        kind        text NOT NULL CHECK (kind IN ('digest', 'alert')),
        name        text NOT NULL,
        spec        jsonb NOT NULL,
        status      text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'active', 'declined', 'paused')),
        thread_id   text,
        created_at  timestamptz NOT NULL DEFAULT now(),
        next_run_at timestamptz,
        last_run_at timestamptz,
        last_value  double precision
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app.notifications (
        id              bigserial PRIMARY KEY,
        subscription_id bigint REFERENCES app.subscriptions ON DELETE CASCADE,
        created_at      timestamptz NOT NULL DEFAULT now(),
        title           text NOT NULL,
        body            text NOT NULL,
        delivered_to    text NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app.audit_log (
        id         bigserial PRIMARY KEY,
        at         timestamptz NOT NULL DEFAULT now(),
        thread_id  text,
        tool       text NOT NULL,
        input      jsonb NOT NULL,
        ok         boolean NOT NULL,
        detail     text
    )
    """,
)

ROLE_DDL = (
    f"""
    DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{READONLY_ROLE}') THEN
            CREATE ROLE {READONLY_ROLE} LOGIN PASSWORD '{READONLY_ROLE}';
        END IF;
    END $$
    """,
    f"GRANT USAGE ON SCHEMA analytics TO {READONLY_ROLE}",
    f"GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO {READONLY_ROLE}",
    f"ALTER ROLE {READONLY_ROLE} SET default_transaction_read_only = on",
    f"ALTER ROLE {READONLY_ROLE} SET statement_timeout = '10s'",
)


def comment_statements() -> list[str]:
    statements = []
    for target, text in COMMENTS.items():
        kind = "TABLE" if target.count(".") == 1 else "COLUMN"
        escaped = text.replace("'", "''")
        statements.append(f"COMMENT ON {kind} {target} IS '{escaped}'")
    return statements


def all_ddl() -> list[str]:
    return [*ANALYTICS_DDL, *comment_statements(), *APP_DDL, *ROLE_DDL]


async def create_pool(url: str, max_size: int = 8) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open()
    return pool


async def init_schema(pool: AsyncConnectionPool) -> None:
    async with pool.connection() as conn:
        for statement in all_ddl():
            await conn.execute(statement)


async def schema_description(pool: AsyncConnectionPool) -> str:
    """The analytics tables as commented DDL, as shown to the text-to-SQL baselines."""
    query = """
        SELECT c.table_name, c.column_name, c.data_type,
               col_description(format('%I.%I', c.table_schema, c.table_name)::regclass,
                               c.ordinal_position) AS comment,
               obj_description(format('%I.%I', c.table_schema, c.table_name)::regclass)
                   AS table_comment
        FROM information_schema.columns c
        WHERE c.table_schema = 'analytics'
        ORDER BY c.table_name, c.ordinal_position
    """
    async with pool.connection() as conn:
        rows = await (await conn.execute(query)).fetchall()
    lines: list[str] = []
    table = None
    for row in rows:
        if row["table_name"] != table:
            table = row["table_name"]
            note = f"  -- {row['table_comment']}" if row["table_comment"] else ""
            lines.append(f"\nTABLE analytics.{table}{note}")
        note = f"  -- {row['comment']}" if row["comment"] else ""
        lines.append(f"  {row['column_name']} {row['data_type']}{note}")
    return "\n".join(lines).strip()
