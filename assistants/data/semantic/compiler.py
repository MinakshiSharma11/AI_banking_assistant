"""MetricQuery -> SQL, built as a sqlglot expression tree. The LLM never writes this SQL.

Every query gets, in code: the metric's default filters, the time range, the user's
filters as bind parameters, and the row-security predicate
`<branch expr> = ANY(:user_branches)`. Errors raise SemanticError with a message the
planner can act on (it is fed back once for a retry).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from sqlglot import exp

from assistants.data.semantic.loader import Dimension, Entity, Metric, SemanticModel
from assistants.data.semantic.periods import WEEK_START, ResolvedPeriod, resolve_period
from assistants.data.semantic.query import Filter, MetricQuery
from assistants.data.semantic.sqlutil import aliases_used, placeholder, render, table

MAX_ROWS = 500
BRANCH_CODE = re.compile(r"^\d{3}$")
_CMP = {"eq": exp.EQ, "neq": exp.NEQ, "gt": exp.GT, "gte": exp.GTE, "lt": exp.LT, "lte": exp.LTE}


class SemanticError(ValueError):
    """The MetricQuery does not fit the semantic model. Message is planner-facing."""


class UnknownValue(SemanticError):
    """A filter value that is not in the dimension's list (e.g. a misspelt branch)."""

    def __init__(self, dimension: str, value: str, allowed: list[str]) -> None:
        super().__init__(f"'{value}' is not a valid {dimension}; use one of {allowed}")
        self.dimension, self.value, self.allowed = dimension, value, allowed


@dataclass(frozen=True)
class CompiledQuery:
    metric: str
    sql: str  # readable, :name binds (shown in the UI)
    exec_sql: str  # psycopg %(name)s binds
    params: dict[str, Any]
    columns: list[str]
    dimensions: list[str]
    grain: str | None
    period: ResolvedPeriod | None
    definition: str
    tree: exp.Select


def check_branches(user_branches: Sequence[str]) -> list[str]:
    branches = list(user_branches)
    if not branches or not all(isinstance(b, str) and BRANCH_CODE.match(b) for b in branches):
        raise ValueError("user_branches must be a non-empty list of 3-digit branch codes")
    return branches


def row_security_predicate(branch_expr: exp.Expr) -> exp.Expr:
    """`<branch expr> = ANY(:user_branches)`."""
    return exp.EQ(
        this=branch_expr.copy(), expression=exp.Any(this=exp.paren(placeholder("user_branches")))
    )


def _grain_expr(col: exp.Expr, kind: str, grain: str, tz: str) -> exp.Expr:
    src = (
        exp.AtTimeZone(this=col.copy(), zone=exp.Literal.string(tz))
        if kind == "timestamp"
        else col.copy()
    )
    if grain == "week" and WEEK_START == 6:  # Sunday-start weeks: shift, truncate, shift back
        shifted = exp.Add(
            this=src,
            expression=exp.Interval(this=exp.Literal.string("1"), unit=exp.Var(this="DAY")),
        )
        trunc = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string("week"), shifted])
        return exp.Sub(
            this=exp.Cast(this=trunc, to=exp.DataType.build("date")),
            expression=exp.Literal.number(1),
        )
    trunc = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string(grain), src])
    return exp.Cast(this=trunc, to=exp.DataType.build("date"))


def _measure(metric: Metric) -> exp.Expr:
    m = metric.measure
    if m.agg == "count":
        value: exp.Expr = exp.Count(this=exp.Star())
    elif m.agg == "count_distinct":
        assert m.expr is not None  # noqa: S101 - guaranteed by the loader
        value = exp.Count(this=exp.Distinct(expressions=[m.expr.copy()]))
    elif m.agg == "sum":
        assert m.expr is not None  # noqa: S101
        value = exp.Sum(this=m.expr.copy())
    elif m.agg == "avg":
        assert m.expr is not None  # noqa: S101
        value = exp.Avg(this=m.expr.copy())
    else:  # ratio: SUM(CASE ...) / NULLIF(SUM(CASE ...), 0)
        assert m.numerator is not None and m.denominator is not None  # noqa: S101

        def flag(cond: exp.Expr) -> exp.Expr:
            case = exp.Case(
                ifs=[exp.If(this=cond.copy(), true=exp.Literal.number(1))],
                default=exp.Literal.number(0),
            )
            return exp.Sum(this=case)

        num = exp.Cast(this=flag(m.numerator), to=exp.DataType.build("numeric"))
        value = exp.Div(
            this=num,
            expression=exp.Nullif(this=flag(m.denominator), expression=exp.Literal.number(0)),
        )
    if m.agg in ("avg", "ratio"):
        value = exp.Round(
            this=exp.Cast(this=value, to=exp.DataType.build("numeric")),
            decimals=exp.Literal.number(metric.decimals),
        )
    return value


def _canonical(dim: Dimension, f: Filter) -> list[Any]:
    """Map user values to the stored values; reject anything not in the dimension's list."""
    if dim.type == "number":
        try:
            return [int(v) for v in f.values]
        except (TypeError, ValueError) as e:
            raise SemanticError(f"filter on '{dim.name}' needs numbers") from e
    if f.op not in ("in", "not_in", "eq", "neq"):
        raise SemanticError(f"filter on '{dim.name}' supports in, not_in, eq, neq only")
    out: list[Any] = []
    for v in f.values:
        sv = str(v).strip()
        if dim.lookup:  # accept code or label, compare on the code
            by_label = {lbl.casefold(): code for code, lbl in dim.lookup.items()}
            by_code = {code.casefold(): code for code in dim.lookup}
            code = by_label.get(sv.casefold()) or by_code.get(sv.casefold())
            if code is None:
                raise UnknownValue(dim.name, sv, list(dim.lookup.values()))
            out.append(code)
        elif dim.values:
            match = {x.casefold(): x for x in dim.values}.get(sv.casefold())
            if match is None:
                raise UnknownValue(dim.name, sv, list(dim.values))
            out.append(match)
        else:
            out.append(sv)
    return out


def _filter_pred(dim: Dimension, f: Filter, values: list[Any], params: dict[str, Any]) -> exp.Expr:
    names = []
    for v in values:
        name = f"f_{sum(k.startswith('f_') for k in params) + 1}"
        params[name] = v
        names.append(placeholder(name))
    col = dim.expr.copy()
    if f.op in ("in", "not_in"):
        pred: exp.Expr = exp.In(this=col, expressions=names)
        return exp.Not(this=pred) if f.op == "not_in" else pred
    if len(names) != 1:
        raise SemanticError(f"operator '{f.op}' takes exactly one value")
    return _CMP[f.op](this=col, expression=names[0])


def _joins_for(entity: Entity, nodes: list[exp.Expr]) -> list[str]:
    need = aliases_used(*nodes) - {"t"}
    unknown = need - set(entity.joins)
    if unknown:  # cannot happen with a loaded model; defensive
        raise SemanticError(f"internal: unknown aliases {sorted(unknown)}")
    stack = list(need)
    while stack:
        for r in entity.joins[stack.pop()].requires:
            if r not in need:
                need.add(r)
                stack.append(r)
    return [a for a in entity.joins if a in need]  # declaration order = dependency order


def compile_query(
    model: SemanticModel, q: MetricQuery, user_branches: Sequence[str], today: date
) -> CompiledQuery:
    branches = check_branches(user_branches)
    metric = model.metrics.get(q.metric)
    if metric is None:
        raise SemanticError(f"Unknown metric '{q.metric}'. Valid metrics: {sorted(model.metrics)}")
    ent = model.entities[metric.entity]
    tz = model.tz

    dims: list[Dimension] = []
    for dname in dict.fromkeys(q.dimensions):
        if dname not in metric.dimensions:
            raise SemanticError(
                f"Metric '{metric.name}' cannot be broken down by '{dname}'. "
                f"Valid dimensions: {list(metric.dimensions)}"
            )
        dims.append(ent.dimensions[dname])

    period: ResolvedPeriod | None = None
    if metric.kind == "current":
        if q.grain:
            raise SemanticError(f"'{metric.name}' is a live snapshot; it has no time grain")
    else:
        if q.time is None:
            raise SemanticError(
                f"Metric '{metric.name}' needs a period (e.g. last_month). "
                "If the question does not say, ask a clarification."
            )
        period = resolve_period(q.time, today)
        if metric.kind == "snapshot" and q.grain:
            raise SemanticError(f"'{metric.name}' is a snapshot at period end; drop the grain")

    params: dict[str, Any] = {}
    where: list[exp.Expr] = [f.copy() for f in metric.default_filters]
    grain_expr: exp.Expr | None = None
    if period is not None:
        if metric.kind == "snapshot":
            assert metric.snapshot is not None  # noqa: S101
            vf, vt = (ent.time_dimensions[n] for n in metric.snapshot)
            _, params["t_to"] = period.bounds(vf.type, ZoneInfo(tz))
            to = placeholder("t_to")
            where.append(exp.LT(this=vf.expr.copy(), expression=to))
            where.append(
                exp.paren(
                    exp.or_(
                        exp.Is(this=vt.expr.copy(), expression=exp.Null()),
                        exp.GTE(this=vt.expr.copy(), expression=to.copy()),
                    )
                )
            )
        else:
            assert metric.time_dimension is not None  # noqa: S101
            td = ent.time_dimensions[metric.time_dimension]
            params["t_from"], params["t_to"] = period.bounds(td.type, ZoneInfo(tz))
            where.append(exp.GTE(this=td.expr.copy(), expression=placeholder("t_from")))
            where.append(exp.LT(this=td.expr.copy(), expression=placeholder("t_to")))
            if q.grain:
                grain_expr = _grain_expr(td.expr, td.type, q.grain, tz)

    fdims_desc = []
    for f in q.filters:
        if f.dimension not in metric.dimensions:
            raise SemanticError(
                f"Metric '{metric.name}' cannot be filtered by "
                f"'{f.dimension}'. Valid: {list(metric.dimensions)}"
            )
        dim = ent.dimensions[f.dimension]
        values = _canonical(dim, f)
        where.append(_filter_pred(dim, f, values, params))
        shown = [dim.lookup.get(v, v) if dim.lookup else v for v in values]
        fdims_desc.append(f"{f.dimension} {f.op.replace('_', ' ')} {', '.join(map(str, shown))}")

    params["user_branches"] = branches
    where.append(row_security_predicate(ent.row_security))

    # SELECT list
    projections: list[exp.Expr] = []
    group: list[exp.Expr] = []
    columns: list[str] = []
    if grain_expr is not None:
        projections.append(exp.alias_(grain_expr.copy(), "period"))
        group.append(grain_expr.copy())
        columns.append("period")
    for d in dims:
        projections.append(exp.alias_(d.display_expr(), d.name))
        group.append(d.expr.copy())
        if d.sort is not None:
            group.append(d.sort.copy())
        columns.append(d.name)
    projections.append(exp.alias_(_measure(metric), "value"))
    columns.append("value")

    # ORDER BY
    order: list[exp.Ordered] = []
    if q.order_by:
        fld, desc = q.order_by.field, q.order_by.dir == "desc"
        if fld == "value":
            order.append(exp.Ordered(this=exp.column("value"), desc=desc))
        elif fld == "period" and grain_expr is not None:
            order.append(exp.Ordered(this=exp.column("period"), desc=desc))
        elif fld in q.dimensions:
            od = ent.dimensions[fld]
            order.append(
                exp.Ordered(this=od.sort.copy() if od.sort else exp.column(fld), desc=desc)
            )
        else:
            raise SemanticError(
                f"order_by.field must be 'value', 'period' (with a grain) or one "
                f"of the requested dimensions {q.dimensions}"
            )
    elif grain_expr is not None:
        order.append(exp.Ordered(this=exp.column("period")))
        order += [exp.Ordered(this=d.sort.copy() if d.sort else exp.column(d.name)) for d in dims]
    elif any(d.sort is not None for d in dims):
        order += [exp.Ordered(this=d.sort.copy() if d.sort else exp.column(d.name)) for d in dims]
    elif dims:
        order.append(exp.Ordered(this=exp.column("value"), desc=True))

    # Joins: only those referenced (plus their prerequisites)
    used = _joins_for(ent, [*projections, *where, *group, *order])
    sel = exp.Select().select(*projections).from_(table(ent.table, "t"))
    for alias in used:
        j = ent.joins[alias]
        sel = sel.join(
            table(j.table, alias), on=j.on.copy(), join_type="left" if j.kind == "left" else None
        )
    sel = sel.where(exp.and_(*where))
    if group:
        sel = sel.group_by(*group)
    if order:
        sel = sel.order_by(*order)
    sel = sel.limit(min(q.limit or MAX_ROWS, MAX_ROWS))

    display, run = render(sel)
    by = ([f"{q.grain}"] if q.grain else []) + [d.name for d in dims]
    definition = f"{metric.label}: {metric.description}"
    if metric.kind == "current":
        definition += " As of now."
    elif period is not None:
        definition += (
            f" As of the end of {period.label()}."
            if metric.kind == "snapshot"
            else f" Period: {period.label()}."
        )
    if by:
        definition += f" By {', '.join(by)}."
    if fdims_desc:
        definition += f" Filters: {'; '.join(fdims_desc)}."
    definition += f" Branch scope: {', '.join(branches)}."
    return CompiledQuery(
        metric=metric.name,
        sql=display,
        exec_sql=run,
        params=params,
        columns=columns,
        dimensions=[d.name for d in dims],
        grain=q.grain,
        period=period,
        definition=definition,
        tree=sel,
    )
