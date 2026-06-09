"""Correctness-hardening tests (Fixes 1-4)."""
from __future__ import annotations

from datetime import date

import pytest

from pit.ingest import fetch_company
from pit.normalize import normalize, load_concept_map
from pit.validate import (find_synonym_violations, assert_synonym_invariant,
                          SynonymInvariantError)
from pit.identities import check_balance_sheet_identity, check_gross_profit_identity
from pit.reconcile_xlsx import reconcile
from pit.store import TABLE

IDENTITY_AS_OF = date(2026, 6, 1)


# --- Fix 1: synonym invariant ----------------------------------------------

def test_real_concept_map_has_zero_synonym_violations():
    rows = normalize(fetch_company(320193), load_concept_map())
    violations = find_synonym_violations(rows)
    assert violations == [], "\n".join(str(v) for v in violations)


def test_invariant_catches_a_real_conflation():
    """Deliberately re-merge the two non-synonymous cash tags into one concept;
    the invariant must reject it (they disagree within the same filing)."""
    cmap = load_concept_map()
    del cmap["concepts"]["cash_incl_restricted"]
    cmap["concepts"]["cash_and_equivalents"]["tags"] = [
        {"taxonomy": "us-gaap", "tag": "CashAndCashEquivalentsAtCarryingValue"},
        {"taxonomy": "us-gaap", "tag": "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"},
    ]
    rows = normalize(fetch_company(320193), cmap)
    violations = find_synonym_violations(rows)
    assert len(violations) > 0
    with pytest.raises(SynonymInvariantError):
        assert_synonym_invariant(rows)


# --- Fix 2: share-count timing split ---------------------------------------

def test_cover_date_facts_never_have_statement_periodicity(con):
    # Every cover-date fact: not joinable, periodicity NULL, labelled COVER.
    bad = con.execute(
        f"SELECT count(*) FROM {TABLE} WHERE canonical_concept='shares_outstanding_cover' "
        f"AND (periodicity IS NOT NULL OR joinable_to_statements = TRUE "
        f"     OR fiscal_period <> 'COVER')").fetchone()[0]
    assert bad == 0

    # And the joinable share count IS labelled with a statement periodicity.
    ok = con.execute(
        f"SELECT count(*) FROM {TABLE} WHERE canonical_concept='shares_outstanding' "
        f"AND periodicity IN ('annual','quarterly') AND joinable_to_statements = TRUE"
    ).fetchone()[0]
    assert ok > 0

    # The cover convention genuinely uses non-statement measurement dates: many
    # cover dates are NOT balance-sheet dates (the whole reason they aren't
    # joinable). (Some very old filings happened to use the period end as the
    # cover date, so the sets are not fully disjoint — hence "many", not "all".)
    cover_dates = {r[0] for r in con.execute(
        f"SELECT DISTINCT period_end FROM {TABLE} WHERE canonical_concept='shares_outstanding_cover'"
    ).fetchall()}
    stmt_dates = {r[0] for r in con.execute(
        f"SELECT DISTINCT period_end FROM {TABLE} WHERE canonical_concept='shares_outstanding'"
    ).fetchall()}
    assert len(cover_dates - stmt_dates) > 0.5 * len(cover_dates)


# --- Fix 3a: accounting identities -----------------------------------------

def test_balance_sheet_identity_zero_violations(con):
    rep = check_balance_sheet_identity(con, IDENTITY_AS_OF)
    assert rep.periods_checked >= 60
    assert rep.violations == [], rep.summary()  # zero for Apple — any nonzero is a regression


def test_gross_profit_identity_reported_within_tolerance(con):
    rep = check_gross_profit_identity(con, IDENTITY_AS_OF)
    assert rep.periods_checked > 0
    # Reported with tolerance; tagging-gap periods are skipped, not failed.
    assert rep.violations == [], rep.summary()


# --- Fix 3b: independent reproduce-the-filing reconciliation ----------------

def test_xlsx_reconciliation_matches_rendered_statements(con):
    results = reconcile(con)  # uses cached workbooks if present
    assert len(results) == 24  # 4 filings x 6 headline metrics
    mismatches = [r for r in results if not r.matched]
    assert not mismatches, "\n".join(
        f"FY{r.fiscal_year} {r.metric}: rendered={r.rendered} store={r.store}"
        for r in mismatches)
