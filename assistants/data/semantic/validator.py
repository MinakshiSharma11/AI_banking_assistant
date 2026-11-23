"""SQL validator: every statement passes here, compiled or LLM-written.

1. Parse (Postgres). One statement, SELECT / WITH ... SELECT / set operations only; no
   DML, DDL, COPY, SELECT INTO, row locks, or data-modifying CTEs anywhere in the tree.
2. Tables and columns must be in model.yaml's allowlist (schema-qualified). Stars expand
   to allowed columns only, so PII and payload columns can never be selected.
3. Functions must be on an allowlist (no pg_sleep, dblink, lo_*, set_config, ...).
4. LLM-written SQL: every base table with branch data is replaced by a subquery that
   applies the row-security predicate. A table without a resolvable branch rule is
   rejected.
5. LIMIT forced to <= 500. The EXPLAIN cost ceiling is checked at execution
   (assistants/data/db.py), because it needs the database.

The Postgres RLS policies remain the last line of defence behind all of this.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from assistants.data.semantic.compiler import MAX_ROWS, check_branches
from assistants.data.semantic.loader import SemanticModel
from assistants.data.semantic.sqlutil import DIALECT, render, table

MAX_SQL_CHARS = 8000

# sqlglot node keys (or names, for functions sqlglot does not model) that may appear.
ALLOWED_FUNCTIONS = frozenset(
    {
        # aggregates
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "stddev",
        "stddevsamp",
        "stddevpop",
        "variance",
        "percentilecont",
        "percentiledisc",
        "groupconcat",
        "arrayagg",
        "bool_and",
        "bool_or",
        "logicaland",
        "logicalor",
        "countif",
        # windows
        "rownumber",
        "rank",
        "denserank",
        "lag",
        "lead",
        "ntile",
        "firstvalue",
        "lastvalue",
        "percentrank",
        "cumedist",
        # scalar
        "abs",
        "round",
        "floor",
        "ceil",
        "sign",
        "sqrt",
        "power",
        "pow",
        "ln",
        "log",
        "exp",
        "mod",
        "coalesce",
        "nullif",
        "greatest",
        "least",
        "if",
        "case",
        "cast",
        "trycast",
        "upper",
        "lower",
        "length",
        "substring",
        "trim",
        "concat",
        "concatws",
        "replace",
        "left",
        "right",
        "lpad",
        "rpad",
        "initcap",
        "split_part",
        "splitpart",
        "position",
        "strposition",
        "extract",
        "datetrunc",
        "date_trunc",
        "timestamptrunc",
        "timetostr",
        "date_part",
        "datepart",
        "age",
        "currentdate",
        "currenttimestamp",
        "now",
        "date",
        "todate",
        "tochar",
        "makedate",
        "generate_series",
        "generateseries",
        "explodinggenerateseries",
        "interval",
        "dateadd",
        "datesub",
        "datediff",
        "tsordstodate",
        "tsordstotimestamp",
        "timestampadd",
        "timestampsub",
        "array",
        "arraycontains",
        "arraysize",
        "struct",
        # boolean/structural nodes sqlglot models as functions
        "and",
        "or",
        "xor",
        "not",
        "exists",
        "in",
        "between",
        "like",
        "ilike",
        "jsonextract",
        "jsonextractscalar",
        "jsonbextract",
        "jsonbextractscalar",
    }
)

_FORBIDDEN_NODES: tuple[type[exp.Expr], ...] = tuple(
    t
    for t in (
        getattr(exp, n, None)
        for n in (
            "Insert",
            "Update",
            "Delete",
            "Merge",
            "Create",
            "Drop",
            "Alter",
            "AlterTable",
            "Command",
            "Copy",
            "Into",
            "Lock",
            "Set",
            "TruncateTable",
            "Grant",
            "Revoke",
            "Commit",
            "Rollback",
            "Transaction",
            "Use",
            "Pragma",
            "LoadData",
            "Analyze",
            "Cache",
            "Uncache",
            "Refresh",
            "Describe",
            "Show",
            "Summarize",
        )
    )
    if isinstance(t, type)
)
_SETOP = getattr(exp, "SetOperation", exp.Union)


# Rejections that mean "never allowed" (refuse, no retry) rather than "fix and retry".
SECURITY_CODES = frozenset(
    {
        "blocked_column",
        "blocked_table",
        "not_select",
        "multi_statement",
        "function_not_allowed",
        "placeholders",
        "table_function",
    }
)
_BLOCKED_SCHEMAS = frozenset(
    {"audit", "app", "pg_catalog", "information_schema", "sec", "public", "pg_toast"}
)


class SQLRejected(ValueError):
    """Message is safe to show the user and to feed back to the planner."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedSQL:
    sql: str  # readable, :name binds
    exec_sql: str  # psycopg binds
    params: dict[str, Any]
    tables: frozenset[str]
    wrapped: bool  # row-security wrapper applied (LLM SQL)


def _func_name(f: exp.Func) -> str:
    return f.name.lower() if isinstance(f, exp.Anonymous) else f.key


def _schema(model: SemanticModel) -> dict[str, object]:
    out: dict[str, dict[str, dict[str, str]]] = {}
    for name, rule in model.allowlist.items():
        schema, tbl = name.split(".")
        out.setdefault(schema, {})[tbl] = {c: "TEXT" for c in rule.columns}
    return dict(out)


def _parse(sql: str) -> exp.Expr:
    text = sql.strip()
    if not text:
        raise SQLRejected("empty", "Empty SQL.")
    if len(text) > MAX_SQL_CHARS:
        raise SQLRejected("too_long", "SQL is too long.")
    try:
        stmts = [s for s in sqlglot.parse(text, read=DIALECT) if s is not None]
    except SqlglotError as e:
        raise SQLRejected(
            "unparseable", f"SQL could not be parsed: {str(e).splitlines()[0]}"
        ) from e
    if len(stmts) != 1:
        raise SQLRejected("multi_statement", "Exactly one SQL statement is allowed.")
    root: exp.Expr = stmts[0]
    return root


def _structure(root: exp.Expr) -> None:
    if not isinstance(root, exp.Select | _SETOP):
        raise SQLRejected("not_select", "Only SELECT queries are allowed.")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLRejected(
                "not_select", f"Only read-only SELECT is allowed ({node.key.upper()} found)."
            )


def _tables(root: exp.Expr, model: SemanticModel) -> dict[int, str]:
    """Map id(Table node) -> allowlisted table name. CTE references are skipped."""
    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    found: dict[int, str] = {}
    for t in root.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise SQLRejected("table_function", "Table functions are not allowed in FROM.")
        if t.args.get("catalog"):
            raise SQLRejected("table_not_allowed", "Cross-database references are not allowed.")
        db, name = t.db.lower(), t.name.lower()
        if not db:
            if name in ctes:
                continue
            raise SQLRejected(
                "unqualified_table",
                f"Table '{name}' must be schema-qualified, e.g. core.cust_account.",
            )
        full = f"{db}.{name}"
        if db in _BLOCKED_SCHEMAS or name.startswith("pg_") or full == "atum.decode_error":
            raise SQLRejected(
                "blocked_table", f"Table '{full}' is not available to this assistant."
            )
        if full not in model.allowlist:
            raise SQLRejected(
                "table_not_allowed",
                f"Table '{full}' is not available. Allowed tables: {sorted(model.allowlist)}",
            )
        found[id(t)] = full
    return found


def _functions(root: exp.Expr) -> None:
    for f in root.find_all(exp.Func):
        name = _func_name(f)
        if name not in ALLOWED_FUNCTIONS:
            raise SQLRejected("function_not_allowed", f"Function '{name}' is not allowed.")


def _blocked_columns(root: exp.Expr, model: SemanticModel) -> None:
    """Any reference to a PII / payload / document column name is refused outright."""
    names = {b.rsplit(".", 1)[1] for b in model.blocked_columns}
    for col in root.find_all(exp.Column):
        if col.name.lower() in names:
            raise SQLRejected(
                "blocked_column",
                f"Column '{col.name}' holds personal data, raw payloads or "
                "document content and is never queryable.",
            )


def _columns(root: exp.Expr, model: SemanticModel) -> exp.Expr:
    """Qualify against the allowlisted schema; any other column is an error."""
    try:
        q = qualify(
            root,
            schema=_schema(model),
            dialect=DIALECT,
            validate_qualify_columns=True,
            quote_identifiers=False,
            identify=False,
        )
    except OptimizeError as e:
        raise SQLRejected(
            "column_not_allowed",
            f"Column not available: {e}. PII, payload and document columns are never queryable.",
        ) from e
    sources = {t.alias_or_name.lower() for t in q.find_all(exp.Table)} | {
        a.name.lower() for a in q.find_all(exp.TableAlias) if a.name
    }
    if getattr(exp, "TableColumn", None) and any(True for _ in q.find_all(exp.TableColumn)):
        raise SQLRejected(
            "column_not_allowed", "Whole-row references are not allowed; name the columns."
        )
    for col in q.find_all(exp.Column):
        if not col.table and col.name.lower() in sources:
            raise SQLRejected(
                "column_not_allowed", "Whole-row references are not allowed; name the columns."
            )
    for scope in traverse_scope(q):
        for col in scope.columns:
            src = scope.sources.get(col.table)
            if isinstance(src, exp.Table):
                full = f"{src.db}.{src.name}".lower()
                rule = model.allowlist.get(full)
                if rule is None or col.name not in rule.columns:
                    raise SQLRejected(
                        "column_not_allowed", f"Column '{full}.{col.name}' is not available."
                    )
    return q


def _wrap(root: exp.Expr, model: SemanticModel) -> None:
    """Replace each branch-bearing base table with a row-security subquery."""
    ctes = {c.alias_or_name.lower() for c in root.find_all(exp.CTE)}
    for t in list(root.find_all(exp.Table)):
        if not t.db and t.name.lower() in ctes:
            continue
        full = f"{t.db}.{t.name}".lower()
        rule = model.allowlist[full]
        if rule.reference:
            continue
        if rule.row_security is None:  # loader forbids this; keep the rule explicit
            raise SQLRejected("no_branch_rule", f"Cannot apply branch security to {full}.")
        inner = (
            exp.Select()
            .select(*[exp.column(c, table="t") for c in rule.columns])
            .from_(table(full, "t"))
            .where(rule.row_security.copy())
        )
        t.replace(
            exp.Subquery(this=inner, alias=exp.TableAlias(this=exp.to_identifier(t.alias_or_name)))
        )


def _limit(root: exp.Expr, max_rows: int) -> exp.Expr:
    lim = root.args.get("limit")
    current = None
    if lim is not None and isinstance(lim.expression, exp.Literal) and lim.expression.is_int:
        current = int(lim.expression.this)
    if current is None or current > max_rows:
        root = root.limit(max_rows)  # type: ignore[attr-defined]
    return root


def validate_sql(
    model: SemanticModel,
    sql: str | exp.Expr,
    *,
    user_branches: Sequence[str],
    source: Literal["compiled", "llm"],
    params: dict[str, Any] | None = None,
    max_rows: int = MAX_ROWS,
) -> ValidatedSQL:
    branches = check_branches(user_branches)
    root = _parse(sql) if isinstance(sql, str) else sql.copy()
    _structure(root)
    phs = {p.name for p in root.find_all(exp.Placeholder)}
    if source == "llm" and phs:
        raise SQLRejected("placeholders", "Bind parameters are not allowed in written SQL.")
    if source == "compiled" and "user_branches" not in phs:
        raise SQLRejected("no_row_security", "Compiled SQL is missing the row-security predicate.")
    tables = _tables(root, model)
    _blocked_columns(root, model)
    _functions(root)
    root = _columns(root, model)
    if source == "llm":
        _wrap(root, model)
    root = _limit(root, max_rows)
    display, run = render(root)
    all_params = {**(params or {}), "user_branches": branches}
    missing = phs - set(all_params)
    if missing:
        raise SQLRejected("missing_params", f"Missing bind parameters: {sorted(missing)}")
    return ValidatedSQL(display, run, all_params, frozenset(tables.values()), source == "llm")
