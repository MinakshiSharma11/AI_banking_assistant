"""sqlglot helpers shared by the compiler and the validator."""

from __future__ import annotations

from sqlglot import exp, parse_one

DIALECT = "postgres"


class FragmentError(ValueError):
    """A YAML SQL fragment could not be parsed."""


def parse_fragment(sql: str) -> exp.Expr:
    """Parse a trusted SQL fragment from the semantic YAML into an expression tree."""
    try:
        return parse_one(sql, read=DIALECT)
    except Exception as e:  # sqlglot raises several error types
        raise FragmentError(f"cannot parse SQL fragment {sql!r}: {e}") from e


def qualify_base(node: exp.Expr, alias: str = "t") -> exp.Expr:
    """Qualify bare column names with the entity's base alias (YAML convenience)."""
    node = node.copy()
    for col in node.find_all(exp.Column):
        if not col.table:
            col.set("table", exp.to_identifier(alias))
    return node


def table(name: str, alias: str) -> exp.Table:
    schema, tbl = name.split(".")
    return exp.Table(
        this=exp.to_identifier(tbl),
        db=exp.to_identifier(schema),
        alias=exp.TableAlias(this=exp.to_identifier(alias)),
    )


def aliases_used(*nodes: exp.Expr) -> set[str]:
    """Table qualifiers referenced by columns in the given expressions."""
    out: set[str] = set()
    for n in nodes:
        for col in n.find_all(exp.Column):
            if col.table:
                out.add(col.table)
    return out


def placeholder(name: str) -> exp.Placeholder:
    return exp.Placeholder(this=name)


def render(tree: exp.Expr) -> tuple[str, str]:
    """(display SQL with :name binds, psycopg SQL with %(name)s binds)."""
    shown = tree.copy()
    for ph in list(shown.find_all(exp.Placeholder)):
        ph.replace(exp.Var(this=f":{ph.name}"))
    display = shown.sql(dialect=DIALECT, pretty=True)

    run = tree.copy()
    for lit in list(run.find_all(exp.Literal)):  # psycopg treats % as a bind marker
        if lit.is_string and "%" in lit.this:
            lit.replace(exp.Literal.string(lit.this.replace("%", "%%")))
    return display, run.sql(dialect=DIALECT)
