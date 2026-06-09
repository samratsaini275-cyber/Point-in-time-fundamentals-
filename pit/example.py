"""Worked example (acceptance criterion c): revenue and total assets as of
three different dates, each with full provenance.

Run:  python -m pit.example
"""
from __future__ import annotations

from datetime import date

from .store import build_store
from .query import point_in_time

AS_OF_DATES = [date(2010, 6, 1), date(2019, 3, 1), date(2025, 6, 1)]


def _fmt(con, concept: str, as_of: date) -> str:
    r = point_in_time(con, concept, as_of, periodicity="annual")
    if r is None:
        return f"    {concept:14s}: (no annual figure known as of {as_of})"
    return (f"    {concept:14s}: ${r.value/1e9:,.3f}B  "
            f"[FY{r.fiscal_year} {r.fiscal_period}, period_end {r.period_end}]\n"
            f"        provenance: {r.provenance()}")


def main() -> None:
    con = build_store()
    print("Apple Inc. (CIK 320193) — point-in-time fundamentals")
    print("Latest annual figure publicly known as of each date:\n")
    for as_of in AS_OF_DATES:
        print(f"AS OF {as_of}")
        print(_fmt(con, "revenue", as_of))
        print(_fmt(con, "total_assets", as_of))
        print()


if __name__ == "__main__":
    main()
