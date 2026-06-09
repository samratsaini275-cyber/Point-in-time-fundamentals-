"""The point-in-time query — the core of the project.

point_in_time(con, concept, as_of, periodicity) implements exactly:

  1. filter to the canonical concept,
  2. drop rows with filing_date > as_of            (no look-ahead),
  3. pick the most recent period_end <= as_of for that periodicity,
  4. among rows for that period, take the latest filing_date,
     tie-broken by acceptance_datetime, then accession_no, then tag_rank
     (the last only matters when one filing reports the same period under two
     acceptable tags),
  5. return the value WITH full provenance.

`periodicity` may be 'annual', 'quarterly', or None (= no restriction, "any").

`target_period_end` is an optional override of step 3: instead of "most recent
period", it fixes the period to a specific period_end date. This is what lets
us interrogate a *specific* historical period (e.g. FY2008 total assets) and
watch the answer flip across a restatement as the as-of date advances — see the
restatement test. With it set, periodicity defaults to None so that annual and
quarterly reportings of the same balance-sheet date compete on filing recency.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb

from .store import TABLE


@dataclass(frozen=True)
class PITResult:
    canonical_concept: str
    value: float
    unit: str
    period_start: date | None
    period_end: date
    fiscal_year: int
    fiscal_period: str
    periodicity: str | None
    # provenance
    raw_tag: str
    taxonomy: str
    form_type: str
    accession_no: str
    filing_date: date
    acceptance_datetime: str | None
    is_amendment: bool

    def provenance(self) -> str:
        amend = " (AMENDMENT)" if self.is_amendment else ""
        return (f"{self.form_type}{amend} accn={self.accession_no} "
                f"filed={self.filing_date} accepted={self.acceptance_datetime} "
                f"[{self.raw_tag}]")


# Deterministic precedence for step 4. NULLS LAST so a missing acceptance time
# never outranks a known one.
_ORDER_BY = (
    "ORDER BY filing_date DESC, "
    "acceptance_datetime DESC NULLS LAST, "
    "accession_no DESC, "
    "tag_rank ASC"
)


def point_in_time(con: duckdb.DuckDBPyConnection, concept: str, as_of: date,
                  periodicity: str | None = None,
                  target_period_end: date | None = None) -> PITResult | None:
    """Return the value of `concept` as it was publicly known on `as_of`, or None."""
    conds = ["canonical_concept = ?", "filing_date <= ?"]
    params: list = [concept, as_of]
    if periodicity is not None:
        conds.append("periodicity = ?")
        params.append(periodicity)
    where = " AND ".join(conds)

    if target_period_end is not None:
        period_clause = "period_end = ?"
        period_params = [target_period_end]
    else:
        # most recent period_end that is on/before as_of, among surviving rows
        period_clause = f"period_end = (SELECT max(period_end) FROM {TABLE} WHERE {where} AND period_end <= ?)"
        period_params = list(params) + [as_of]

    sql = f"""
        SELECT canonical_concept, value, unit, period_start, period_end,
               fiscal_year, fiscal_period, periodicity,
               raw_tag, taxonomy, form_type, accession_no, filing_date,
               acceptance_datetime, is_amendment
        FROM {TABLE}
        WHERE {where} AND {period_clause}
        {_ORDER_BY}
        LIMIT 1
    """
    row = con.execute(sql, params + period_params).fetchone()
    if row is None:
        return None
    return PITResult(*row)
