"""Accounting-identity reconciliation, run POINT-IN-TIME.

Every term of an identity is resolved with the same as-of date and the same
filing-recency tie-break as the main query, so we are checking the figures that
were jointly known on that date — not a mix of original and restated numbers.

Implemented identities:
  * balance sheet : Assets = Liabilities + StockholdersEquity   (instant, per period_end)
  * gross profit  : GrossProfit = Revenue - CostOfGoodsAndServicesSold
                    (duration, matched on BOTH period_start and period_end)

Violations beyond a small tolerance are reported; periods where a term is simply
not tagged are skipped (counted separately), never failed — tagging gaps are not
identity violations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import duckdb

from .store import TABLE

TOL = 1.0  # dollars; values are integer dollars, so this is just float slack

# Same precedence as pit.query.point_in_time step 4.
_ORDER = ("ORDER BY filing_date DESC, acceptance_datetime DESC NULLS LAST, "
          "accession_no DESC, tag_rank ASC LIMIT 1")


def _pit_value(con, concept: str, as_of: date, period_end: date,
               period_start: date | None, match_start: bool) -> float | None:
    """Latest value of `concept` for an exact period, known as of `as_of`."""
    conds = ["canonical_concept = ?", "filing_date <= ?", "period_end = ?"]
    params: list = [concept, as_of, period_end]
    if match_start:
        if period_start is None:
            conds.append("period_start IS NULL")
        else:
            conds.append("period_start = ?")
            params.append(period_start)
    sql = f"SELECT value FROM {TABLE} WHERE {' AND '.join(conds)} {_ORDER}"
    row = con.execute(sql, params).fetchone()
    return None if row is None else row[0]


@dataclass
class IdentityReport:
    name: str
    as_of: date
    periods_checked: int = 0
    periods_skipped: int = 0      # a term was not tagged for this period
    violations: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def summary(self) -> str:
        status = "OK" if self.ok else f"{len(self.violations)} VIOLATION(S)"
        return (f"{self.name} @ {self.as_of}: {status} "
                f"(checked {self.periods_checked}, skipped {self.periods_skipped})")


def check_balance_sheet_identity(con: duckdb.DuckDBPyConnection, as_of: date,
                                 tol: float = TOL) -> IdentityReport:
    rep = IdentityReport("Assets = Liabilities + Equity", as_of)
    ends = [r[0] for r in con.execute(
        f"SELECT DISTINCT period_end FROM {TABLE} "
        f"WHERE canonical_concept='total_assets' AND filing_date <= ? ORDER BY 1",
        [as_of]).fetchall()]
    for end in ends:
        a = _pit_value(con, "total_assets", as_of, end, None, match_start=True)
        l = _pit_value(con, "total_liabilities", as_of, end, None, match_start=True)
        e = _pit_value(con, "total_equity", as_of, end, None, match_start=True)
        if a is None or l is None or e is None:
            rep.periods_skipped += 1
            continue
        rep.periods_checked += 1
        diff = a - (l + e)
        if abs(diff) > tol:
            rep.violations.append({"period_end": end, "assets": a,
                                   "liabilities": l, "equity": e, "diff": diff})
    return rep


def check_gross_profit_identity(con: duckdb.DuckDBPyConnection, as_of: date,
                                tol: float = TOL) -> IdentityReport:
    rep = IdentityReport("GrossProfit = Revenue - CostOfRevenue", as_of)
    periods = con.execute(
        f"SELECT DISTINCT period_start, period_end FROM {TABLE} "
        f"WHERE canonical_concept='gross_profit' AND filing_date <= ? "
        f"AND period_start IS NOT NULL ORDER BY 2, 1", [as_of]).fetchall()
    for p_start, p_end in periods:
        gp = _pit_value(con, "gross_profit", as_of, p_end, p_start, match_start=True)
        rev = _pit_value(con, "revenue", as_of, p_end, p_start, match_start=True)
        cogs = _pit_value(con, "cost_of_revenue", as_of, p_end, p_start, match_start=True)
        if gp is None or rev is None or cogs is None:
            rep.periods_skipped += 1
            continue
        rep.periods_checked += 1
        diff = gp - (rev - cogs)
        if abs(diff) > tol:
            rep.violations.append({"period_start": p_start, "period_end": p_end,
                                   "gross_profit": gp, "revenue": rev,
                                   "cost_of_revenue": cogs, "diff": diff})
    return rep


def run_all(con: duckdb.DuckDBPyConnection, as_of: date) -> list[IdentityReport]:
    return [check_balance_sheet_identity(con, as_of),
            check_gross_profit_identity(con, as_of)]


if __name__ == "__main__":
    from .store import build_store
    con = build_store()
    for rep in run_all(con, date(2026, 6, 1)):
        print(rep.summary())
        for v in rep.violations[:5]:
            print("   ", v)
