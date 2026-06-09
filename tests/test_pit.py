"""Acceptance tests for the point-in-time fundamentals slice.

Uses cached SEC JSON (data/raw) if present; otherwise fetches once.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from pit.store import TABLE
from pit.query import point_in_time
from pit.normalize import load_concept_map

CONCEPTS = list(load_concept_map()["concepts"].keys())


# `con` is provided by tests/conftest.py (session-scoped).


# --- (a) No-lookahead property test ----------------------------------------

def test_no_lookahead_property(con):
    """For many random (concept, as_of, periodicity), the returned fact must
    never have a filing_date later than as_of."""
    rng = random.Random(1234)
    start, end = date(2009, 1, 1), date(2026, 6, 1)
    span = (end - start).days

    checks = 0
    for _ in range(2000):
        as_of = start + timedelta(days=rng.randint(0, span))
        concept = rng.choice(CONCEPTS)
        periodicity = rng.choice(["annual", "quarterly", None])
        res = point_in_time(con, concept, as_of, periodicity=periodicity)
        if res is None:
            continue
        checks += 1
        assert res.filing_date <= as_of, (
            f"LOOK-AHEAD: {concept} as_of {as_of} returned a fact filed "
            f"{res.filing_date} ({res.provenance()})"
        )
        # period must also be knowable: it ends on/before as_of
        assert res.period_end <= as_of
    assert checks > 500, f"too few non-empty results to be meaningful ({checks})"


# --- (b) Restatement test --------------------------------------------------

# Apple retrospectively adopted ASU 2009-13/14 (iPhone revenue recognition),
# restating FY2008 total assets from $39.572B to $36.171B via a 10-K/A.
FY2008_END = date(2008, 9, 27)
ORIGINAL = 39_572_000_000.0
RESTATED = 36_171_000_000.0


def test_restatement_flips_with_as_of(con):
    # The raw data really does contain the same period at two different values.
    vals = {r[0] for r in con.execute(
        f"SELECT DISTINCT value FROM {TABLE} "
        f"WHERE canonical_concept='total_assets' AND period_end=?", [FY2008_END]
    ).fetchall()}
    assert vals == {ORIGINAL, RESTATED}, vals

    # Earlier as-of (after original 10-K, before the amendment): ORIGINAL value.
    early = point_in_time(con, "total_assets", date(2009, 12, 1),
                          target_period_end=FY2008_END)
    assert early is not None and early.value == ORIGINAL
    assert early.form_type == "10-K" and not early.is_amendment
    assert early.filing_date == date(2009, 10, 27)

    # Later as-of (after the 10-K/A): RESTATED value, flagged as an amendment.
    late = point_in_time(con, "total_assets", date(2010, 6, 1),
                         target_period_end=FY2008_END)
    assert late is not None and late.value == RESTATED
    assert late.is_amendment and late.form_type == "10-K/A"
    assert late.filing_date == date(2010, 1, 25)

    # And the no-lookahead guarantee holds at the boundary: one day before the
    # amendment was filed, we still get the original value.
    just_before = point_in_time(con, "total_assets", date(2010, 1, 24),
                                target_period_end=FY2008_END)
    assert just_before.value == ORIGINAL


def test_duplicate_reporting_disambiguated(con):
    """Same period reported in both a 10-Q and a later 10-K (same value here):
    the query must return the latest-filed reporting deterministically."""
    end = date(2023, 9, 30)  # FY2023 year-end, reported across many filings
    rows = con.execute(
        f"SELECT count(DISTINCT accession_no) FROM {TABLE} "
        f"WHERE canonical_concept='total_assets' AND period_end=?", [end]
    ).fetchone()[0]
    assert rows >= 2  # genuinely reported by multiple filings

    res = point_in_time(con, "total_assets", date(2025, 1, 1),
                        target_period_end=end)
    # latest filing on/before as_of is the FY2024 10-Q filed 2024-08-02
    latest_filed = con.execute(
        f"SELECT max(filing_date) FROM {TABLE} "
        f"WHERE canonical_concept='total_assets' AND period_end=? AND filing_date<=?",
        [end, date(2025, 1, 1)]
    ).fetchone()[0]
    assert res.filing_date == latest_filed


# --- invariant: bitemporal store keeps multiple rows per period ------------

def test_store_keeps_multiple_rows_per_period(con):
    n = con.execute(
        f"SELECT count(*) FROM ("
        f"  SELECT canonical_concept, period_end, count(*) c FROM {TABLE} "
        f"  GROUP BY 1,2 HAVING c > 1)"
    ).fetchone()[0]
    assert n > 0, "store appears collapsed; expected multiple filings per period"
