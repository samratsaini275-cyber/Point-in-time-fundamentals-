"""Build-time correctness invariants.

Synonym invariant
-----------------
An ordered tag list maps tags that are supposed to denote the SAME accounting
line item. We prove that empirically: within any single
(accession_no, canonical_concept, period_start, period_end), every mapped tag
that actually appears must carry the SAME value. If two tags mapped to one
concept report different values for the same period in the same filing, they are
NOT synonyms and the concept map is wrong. This is a hard error — never a silent
tie-break.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .normalize import FactRow

# Dollar/value tolerance: stored values are integer dollars (or share counts) as
# floats; anything above this is a genuine disagreement, not float noise.
VALUE_TOL = 0.5


@dataclass(frozen=True)
class SynonymViolation:
    canonical_concept: str
    accession_no: str
    period_start: object  # date | None
    period_end: object    # date
    tag_values: dict[str, float]  # raw_tag -> value

    def __str__(self) -> str:
        span = f"{self.period_start}..{self.period_end}" if self.period_start else str(self.period_end)
        pairs = ", ".join(f"{t}={v:,.0f}" for t, v in sorted(self.tag_values.items()))
        return (f"[{self.canonical_concept}] accn {self.accession_no} period {span}: "
                f"non-synonymous tags disagree -> {pairs}")


class SynonymInvariantError(Exception):
    """Raised when the concept map conflates non-synonymous tags."""

    def __init__(self, violations: list[SynonymViolation]):
        self.violations = violations
        body = "\n  ".join(str(v) for v in violations)
        super().__init__(
            f"{len(violations)} synonym-invariant violation(s) — the concept map "
            f"maps tags that are not synonyms:\n  {body}"
        )


def find_synonym_violations(rows: Iterable[FactRow]) -> list[SynonymViolation]:
    """Group by (accession, concept, period) and flag disagreeing tag values."""
    groups: dict[tuple, dict[str, float]] = defaultdict(dict)
    for r in rows:
        key = (r.accession_no, r.canonical_concept, r.period_start, r.period_end)
        # If the same tag appears twice in one accession/period (it shouldn't),
        # keep the first; a same-tag disagreement is a different (SEC) problem.
        groups[key].setdefault(r.raw_tag, r.value)

    violations: list[SynonymViolation] = []
    for (accn, concept, p_start, p_end), tag_values in groups.items():
        if len(tag_values) < 2:
            continue
        vals = list(tag_values.values())
        if max(vals) - min(vals) > VALUE_TOL:
            violations.append(SynonymViolation(concept, accn, p_start, p_end, dict(tag_values)))
    return violations


def assert_synonym_invariant(rows: Iterable[FactRow]) -> None:
    """Raise SynonymInvariantError if any concept conflates non-synonymous tags."""
    violations = find_synonym_violations(rows)
    if violations:
        raise SynonymInvariantError(violations)
