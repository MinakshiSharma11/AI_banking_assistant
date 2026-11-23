"""Load and check the semantic layer (model.yaml, metrics/*.yaml, examples.yaml).

Loading fails fast on anything inconsistent: unknown entities, dimensions or lookups,
expressions that do not parse, columns outside the allowlist, or blocked columns that
leak into the allowlist. The result is an immutable SemanticModel used by the compiler,
the validator and (phase 3) the planner prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any, Literal

import yaml
from sqlglot import exp

from assistants.data.semantic.query import MetricQuery
from assistants.data.semantic.sqlutil import aliases_used, parse_fragment, qualify_base
from assistants.data.settings import SEMANTIC_DIR, get_settings


class SemanticModelError(ValueError):
    """The YAML is inconsistent. Raised at load time, never at query time."""


@dataclass(frozen=True)
class Join:
    alias: str
    table: str
    on: exp.Expr
    kind: Literal["inner", "left"]
    requires: tuple[str, ...]


@dataclass(frozen=True)
class Dimension:
    name: str
    expr: exp.Expr  # raw column expression (codes)
    description: str
    values: tuple[str, ...] | None  # allowed filter values (labels for lookups)
    lookup: dict[str, str] | None  # code -> label
    sort: exp.Expr | None
    type: Literal["string", "number"]

    def display_expr(self) -> exp.Expr:
        """Expression shown in results: lookup codes become labels."""
        if not self.lookup:
            return self.expr.copy()
        return exp.Case(
            this=self.expr.copy(),
            ifs=[
                exp.If(this=exp.Literal.string(k), true=exp.Literal.string(v))
                for k, v in self.lookup.items()
            ],
            default=self.expr.copy(),
        )


@dataclass(frozen=True)
class TimeDimension:
    name: str
    expr: exp.Expr
    type: Literal["date", "timestamp"]


@dataclass(frozen=True)
class Entity:
    name: str
    table: str
    description: str
    joins: dict[str, Join]  # in declaration (dependency) order
    row_security: exp.Expr
    time_dimensions: dict[str, TimeDimension]
    dimensions: dict[str, Dimension]


@dataclass(frozen=True)
class Measure:
    agg: Literal["count", "count_distinct", "sum", "avg", "ratio"]
    expr: exp.Expr | None = None
    numerator: exp.Expr | None = None
    denominator: exp.Expr | None = None


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    description: str
    entity: str
    kind: Literal["period", "snapshot", "current"]
    measure: Measure
    time_dimension: str | None
    snapshot: tuple[str, str] | None  # (valid_from, valid_to) time dimensions
    default_filters: tuple[exp.Expr, ...]
    dimensions: tuple[str, ...]
    synonyms: tuple[str, ...]
    unit: str
    decimals: int


@dataclass(frozen=True)
class TableRule:
    name: str
    columns: tuple[str, ...]  # declared order (used for star expansion)
    reference: bool
    row_security: exp.Expr | None  # predicate over alias `t`, for LLM-written SQL


@dataclass(frozen=True)
class Example:
    question: str
    query: MetricQuery


@dataclass(frozen=True)
class SemanticModel:
    tz: str
    lookups: dict[str, dict[str, str]]
    entities: dict[str, Entity]
    metrics: dict[str, Metric]
    allowlist: dict[str, TableRule]
    blocked_columns: frozenset[str]
    examples: tuple[Example, ...] = field(default_factory=tuple)

    # ---- planner-facing views (phase 3 uses these in prompts and tool schemas) ----
    def catalog(self) -> list[dict[str, Any]]:
        out = []
        for m in self.metrics.values():
            ent = self.entities[m.entity]
            out.append(
                {
                    "metric": m.name,
                    "label": m.label,
                    "description": m.description,
                    "kind": m.kind,
                    "unit": m.unit,
                    "synonyms": list(m.synonyms),
                    "needs_period": m.kind != "current",
                    "dimensions": {
                        d: {
                            "description": ent.dimensions[d].description,
                            **({"values": list(v)} if (v := ent.dimensions[d].values) else {}),
                        }
                        for d in m.dimensions
                    },
                }
            )
        return out

    def metric_query_schema(self) -> dict[str, Any]:
        """MetricQuery JSON schema with enums, so models can't invent names."""
        schema = MetricQuery.model_json_schema(by_alias=True)
        dims = sorted({d for m in self.metrics.values() for d in m.dimensions})
        props = schema["properties"]
        props["metric"]["enum"] = sorted(self.metrics)
        props["dimensions"]["items"]["enum"] = dims
        schema["$defs"]["Filter"]["properties"]["dimension"]["enum"] = dims
        return schema

    def table_columns(self) -> dict[str, list[str]]:
        """Allowed tables and columns, for the write_sql fallback prompt."""
        return {t: list(r.columns) for t, r in self.allowlist.items()}


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _frag(sql: str, tz: str) -> exp.Expr:
    return parse_fragment(str(sql).replace("{tz}", tz))


def _check_columns(
    where: str, node: exp.Expr, aliases: dict[str, str], allow: dict[str, TableRule]
) -> None:
    """Every column must be alias.column with the column allowlisted for that table."""
    for col in node.find_all(exp.Column):
        if col.table not in aliases:
            raise SemanticModelError(f"{where}: unknown table alias in {col.sql()!r}")
        table = aliases[col.table]
        if col.name not in allow[table].columns:
            raise SemanticModelError(f"{where}: column {table}.{col.name} is not allowlisted")


def _parse_allowlist(raw: dict[str, Any], blocked: frozenset[str]) -> dict[str, TableRule]:
    rules: dict[str, TableRule] = {}
    for name, spec in raw.items():
        cols = tuple(dict.fromkeys(spec["columns"]))
        leaked = {f"{name}.{c}" for c in cols} & blocked
        if leaked:
            raise SemanticModelError(f"blocked columns in allowlist: {sorted(leaked)}")
        reference = bool(spec.get("reference", False))
        rs = spec.get("row_security")
        if not reference and not rs:
            raise SemanticModelError(f"{name}: needs row_security or reference: true")
        rules[name] = TableRule(name, cols, reference, parse_fragment(rs) if rs else None)
    return rules


def _parse_entity(
    name: str, spec: dict[str, Any], model: dict[str, Any], allow: dict[str, TableRule], tz: str
) -> Entity:
    where = f"entity {name}"
    if spec["table"] not in allow:
        raise SemanticModelError(f"{where}: table {spec['table']} is not allowlisted")
    aliases = {"t": spec["table"]}
    joins: dict[str, Join] = {}
    for alias, j in spec.get("joins", {}).items():
        if j["table"] not in allow:
            raise SemanticModelError(f"{where}: join table {j['table']} is not allowlisted")
        aliases[alias] = j["table"]
        requires = tuple(j.get("requires", []))
        for r in requires:
            if r not in joins:
                raise SemanticModelError(f"{where}: join {alias} requires undeclared {r}")
        joins[alias] = Join(
            alias, j["table"], _frag(j["join_on"], tz), j.get("type", "inner"), requires
        )
    for alias, j in joins.items():
        _check_columns(f"{where} join {alias}", j.on, aliases, allow)

    def checked(node: exp.Expr, ctx: str) -> exp.Expr:
        _check_columns(f"{where} {ctx}", node, aliases, allow)
        return node

    lookups: dict[str, dict[str, str]] = model.get("lookups", {})
    shared: dict[str, Any] = model.get("shared_dimensions", {})
    dims: dict[str, Dimension] = {}
    for dname, d in spec.get("dimensions", {}).items():
        base = {**shared.get(d.get("shared", ""), {}), **d}
        lookup = None
        if "lookup" in base:
            if base["lookup"] not in lookups:
                raise SemanticModelError(f"{where} dimension {dname}: unknown lookup")
            lookup = {str(k): str(v) for k, v in lookups[base["lookup"]].items()}
        values = base.get("values")
        dims[dname] = Dimension(
            name=dname,
            expr=checked(_frag(base["expr"], tz), f"dimension {dname}"),
            description=str(base.get("description", dname)),
            values=tuple(lookup.values())
            if lookup
            else (tuple(map(str, values)) if values else None),
            lookup=lookup,
            sort=checked(_frag(base["sort"], tz), f"dimension {dname} sort")
            if "sort" in base
            else None,
            type=base.get("type", "string"),
        )
    tdims = {
        tn: TimeDimension(tn, checked(_frag(t["expr"], tz), f"time {tn}"), t["type"])
        for tn, t in spec.get("time_dimensions", {}).items()
    }
    return Entity(
        name=name,
        table=spec["table"],
        description=str(spec.get("description", "")),
        joins=joins,
        row_security=checked(_frag(spec["row_security"], tz), "row_security"),
        time_dimensions=tdims,
        dimensions=dims,
    )


def _parse_metric(
    spec: dict[str, Any], entities: dict[str, Entity], tz: str, allow: dict[str, TableRule]
) -> Metric:
    name = spec["metric"]
    where = f"metric {name}"
    ent = entities.get(spec["entity"])
    if ent is None:
        raise SemanticModelError(f"{where}: unknown entity {spec['entity']}")
    aliases = {"t": ent.table, **{a: j.table for a, j in ent.joins.items()}}

    def frag(sql: str, ctx: str) -> exp.Expr:
        node = qualify_base(_frag(sql, tz))
        _check_columns(f"{where} {ctx}", node, aliases, allow)
        return node

    m = spec["measure"]
    agg = m["agg"]
    if agg == "ratio":
        measure = Measure(
            agg,
            numerator=frag(m["numerator"], "numerator"),
            denominator=frag(m["denominator"], "denominator"),
        )
    elif agg == "count":
        measure = Measure(agg)
    elif agg in ("count_distinct", "sum", "avg"):
        src = m.get("expr") or m.get("column")
        if not src:
            raise SemanticModelError(f"{where}: measure needs expr or column")
        measure = Measure(agg, expr=frag(src, "measure"))
    else:
        raise SemanticModelError(f"{where}: unknown agg {agg}")

    kind = spec.get("kind", "period")
    tdim = spec.get("time_dimension")
    snapshot = None
    if kind == "period" and tdim not in ent.time_dimensions:
        raise SemanticModelError(f"{where}: period metric needs a valid time_dimension")
    if kind == "snapshot":
        s = spec.get("snapshot") or {}
        snapshot = (s.get("valid_from"), s.get("valid_to"))
        if not all(x in ent.time_dimensions for x in snapshot):
            raise SemanticModelError(f"{where}: snapshot needs valid_from/valid_to time dims")
    if kind not in ("period", "snapshot", "current"):
        raise SemanticModelError(f"{where}: unknown kind {kind}")

    dims = tuple(spec.get("dimensions", []))
    unknown = [d for d in dims if d not in ent.dimensions]
    if unknown:
        raise SemanticModelError(f"{where}: dimensions {unknown} not defined on {ent.name}")
    return Metric(
        name=name,
        label=spec["label"],
        description=" ".join(str(spec["description"]).split()),
        entity=ent.name,
        kind=kind,
        measure=measure,
        time_dimension=tdim if kind == "period" else None,
        snapshot=snapshot,  # type: ignore[arg-type]
        default_filters=tuple(frag(f, "default_filter") for f in spec.get("default_filters", [])),
        dimensions=dims,
        synonyms=tuple(str(s) for s in spec.get("synonyms", [])),
        unit=str(spec.get("unit", "")),
        decimals=int(spec.get("decimals", 4 if agg == "ratio" else 0)),
    )


def load_semantic_model(directory: Path | None = None, tz: str | None = None) -> SemanticModel:
    directory = directory or SEMANTIC_DIR
    tz = tz or get_settings().tz_name
    model = _load_yaml(directory / "model.yaml")
    blocked = frozenset(model.get("blocked_columns", []))
    allow = _parse_allowlist(model["allowlist"], blocked)
    entities = {n: _parse_entity(n, s, model, allow, tz) for n, s in model["entities"].items()}
    metrics: dict[str, Metric] = {}
    for path in sorted((directory / "metrics").glob("*.yaml")):
        metric = _parse_metric(_load_yaml(path), entities, tz, allow)
        if metric.name != path.stem:
            raise SemanticModelError(f"{path.name}: metric name must match the file name")
        metrics[metric.name] = metric
    examples: tuple[Example, ...] = ()
    if (directory / "examples.yaml").exists():
        raw = _load_yaml(directory / "examples.yaml") or []
        examples = tuple(
            Example(e["question"], MetricQuery.model_validate(e["query"])) for e in raw
        )
    # Every join alias referenced by a row-security expression must exist (cheap sanity).
    for ent in entities.values():
        missing = aliases_used(ent.row_security) - {"t"} - set(ent.joins)
        if missing:
            raise SemanticModelError(f"entity {ent.name}: row_security uses {missing}")
    return SemanticModel(tz, model.get("lookups", {}), entities, metrics, allow, blocked, examples)


@cache
def get_model() -> SemanticModel:
    return load_semantic_model()
