"""Checks on model-written SQL, used only by the text-to-SQL baselines.

The governed path never needs this, because the semantic layer writes its own SQL. For the
baselines, this is the outer fence: one read-only statement, analytics tables only, a row
cap. The inner fence is the database role, which cannot write no matter what gets through.
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

ALLOWED_SCHEMA = "analytics"
MAX_ROWS = 500

_WRITES = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,
    exp.Copy,
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
)
_BLOCKED_FUNCTIONS = {"dblink", "lo_import", "lo_export", "set_config", "query_to_xml"}


class SqlRejected(ValueError):
    """The statement is not allowed. The message is safe to show to the model."""


def validate(sql: str) -> str:
    """Return ``sql`` wrapped in a row cap, or raise SqlRejected saying why not."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except SqlglotError as error:
        raise SqlRejected(f"Could not parse the SQL: {str(error).splitlines()[0]}") from None
    if len(statements) != 1:
        raise SqlRejected("Send exactly one SQL statement.")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        raise SqlRejected("Only SELECT statements are allowed.")

    for node in statement.walk():
        if isinstance(node, _WRITES):
            raise SqlRejected("Only SELECT statements are allowed.")
        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name.startswith("pg_") or name in _BLOCKED_FUNCTIONS:
                raise SqlRejected(f"Function {name} is not allowed.")

    cte_names = {cte.alias_or_name for cte in statement.find_all(exp.CTE)}
    for table in statement.find_all(exp.Table):
        if not table.db and table.name in cte_names:
            continue
        if table.db != ALLOWED_SCHEMA:
            raise SqlRejected(
                f"Table {table.sql()} is not available. Query tables in the "
                f"{ALLOWED_SCHEMA} schema and qualify them, for example analytics.fact_orders."
            )

    inner = statement.sql(dialect="postgres")
    return f"SELECT * FROM ({inner}) AS guarded LIMIT {MAX_ROWS + 1}"
