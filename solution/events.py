"""Event and pattern primitives for the incremental partition matcher.

No timestamps, watermarks or late corrections exist: a partition's rows are
identified only by their strictly increasing ``seq``.  ``event_id`` is used
solely for the uniqueness check (kept in a side set, never in the row buffer).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class RejectedInput(ValueError):
    """Raised when a row/END would violate the stream contract."""


@dataclass(frozen=True, slots=True)
class Event:
    partition: str
    seq: int
    event_id: str
    kind: str
    value: int
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Event":
        try:
            return cls(
                partition=obj["partition"],
                seq=int(obj["seq"]),
                event_id=obj["event_id"],
                kind=obj.get("kind", ""),
                value=int(obj.get("value", 0)),
                raw=dict(obj),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RejectedInput(f"invalid event record: {exc}") from exc


class _Predicate:
    """A variable definition: a single field that is either equal to a
    constant or >= an integer bound."""

    __slots__ = ("field", "eq", "ge")

    def __init__(self, field_name: str, eq: Any = None, ge: int | None = None):
        if (eq is None) == (ge is None):
            raise ValueError("a definition needs exactly one of 'eq'/'ge'")
        self.field = field_name
        self.eq = eq
        self.ge = ge

    def matches(self, event: Event) -> bool:
        # Look at the event's own fields first (kind/value/...), falling back to
        # an arbitrary extra attribute supplied on the raw row.
        if hasattr(event, self.field) and self.field not in ("partition", "seq", "event_id", "raw"):
            value = getattr(event, self.field)
        elif self.field in event.raw:
            value = event.raw[self.field]
        else:
            return False
        if self.eq is not None:
            return value == self.eq
        return isinstance(value, (int, float)) and value >= self.ge


@dataclass(slots=True)
class Pattern:
    id: str
    variables: list[str]
    mins: list[int]
    maxs: list[int]
    definitions: dict[str, _Predicate]
    skip: str  # "next_row" | "past_last"

    @classmethod
    def from_dict(cls, obj: dict[str, Any]) -> "Pattern":
        items = obj["pattern"]
        variables = [it["var"] for it in items]
        if not 1 <= len(items) <= 4:
            raise ValueError("pattern must have between 1 and 4 variables")
        if len(set(variables)) != len(variables):
            raise ValueError("pattern variables must be distinct")
        mins, maxs = [], []
        for it in items:
            lo, hi = int(it["min"]), int(it["max"])
            if not 1 <= lo <= hi <= 3:
                raise ValueError("need 1 <= min <= max <= 3")
            mins.append(lo)
            maxs.append(hi)
        defines = obj["define"]
        if set(defines) != set(variables):
            raise ValueError("define must cover exactly the pattern variables")
        predicates = {}
        for var, spec in defines.items():
            predicates[var] = _Predicate(
                spec.get("field", "kind"),
                eq=spec.get("eq"),
                ge=None if "ge" not in spec else int(spec["ge"]),
            )
        skip = obj.get("skip", "past_last")
        if skip not in ("next_row", "past_last"):
            raise ValueError(f"unknown skip policy: {skip!r}")
        return cls(obj["id"], variables, mins, maxs, predicates, skip)

    @property
    def total_max(self) -> int:
        return sum(self.maxs)

    def allowed_counts(self) -> list[tuple[int, ...]]:
        """All repetition-count vectors in variable order."""
        result: list[tuple[int, ...]] = [()]
        for lo, hi in zip(self.mins, self.maxs):
            result = [vec + (n,) for vec in result for n in range(lo, hi + 1)]
        return result

    def max_lex_vector(self) -> tuple[int, ...]:
        return tuple(self.maxs)
