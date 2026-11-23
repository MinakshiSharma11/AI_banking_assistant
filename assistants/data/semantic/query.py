"""MetricQuery: the only thing the planner LLM produces on the default path.

The same Pydantic model is the tool's JSON schema (see SemanticModel.metric_query_schema,
which adds enums for metric and dimension names).
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Period = Literal[
    "today",
    "yesterday",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
    "this_quarter",
    "last_quarter",
    "ytd",
    "last_12_months",
    "custom",
]
Grain = Literal["day", "week", "month", "quarter"]
FilterOp = Literal["in", "not_in", "eq", "neq", "gt", "gte", "lt", "lte"]
Scalar = str | int | float


class TimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    period: Period
    from_: date | None = Field(default=None, alias="from", description="custom only, inclusive")
    to: date | None = Field(default=None, description="custom only, inclusive")

    @model_validator(mode="after")
    def _custom_range(self) -> TimeSpec:
        if self.period == "custom":
            if self.from_ is None or self.to is None:
                raise ValueError("period 'custom' needs both 'from' and 'to' dates")
            if self.from_ > self.to:
                raise ValueError("'from' must not be after 'to'")
        return self


class Filter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    op: FilterOp = "in"
    values: list[Scalar] = Field(min_length=1, max_length=50)


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(default="value", description="'value', 'period' or a dimension name")
    dir: Literal["asc", "desc"] = "desc"


class MetricQuery(BaseModel):
    """A metric, how to slice it, which period, and how to sort it."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    dimensions: list[str] = Field(default_factory=list, max_length=3)
    filters: list[Filter] = Field(default_factory=list, max_length=10)
    time: TimeSpec | None = None
    grain: Grain | None = None
    order_by: OrderBy | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
