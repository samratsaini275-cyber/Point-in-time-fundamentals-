"""DuckDB store for bitemporal fundamental facts.

The table keeps MULTIPLE rows per (canonical_concept, period) — one per filing
that reported that period. It is never collapsed to a single 'current' value;
collapsing happens only inside the point-in-time query, as-of a chosen date.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from .ingest import fetch_company
from .normalize import normalize, rows_as_dicts, load_concept_map
from .validate import assert_synonym_invariant, find_synonym_violations

TABLE = "facts"

SCHEMA = f"""
CREATE TABLE {TABLE} (
    canonical_concept       VARCHAR  NOT NULL,
    raw_tag                 VARCHAR  NOT NULL,
    taxonomy                VARCHAR  NOT NULL,
    tag_rank                INTEGER  NOT NULL,
    unit                    VARCHAR  NOT NULL,
    period_start            DATE,                 -- null for instant (balance-sheet) facts
    period_end              DATE     NOT NULL,
    fiscal_year             INTEGER  NOT NULL,
    fiscal_period           VARCHAR  NOT NULL,    -- Q1..Q4, FY, Q2YTD, Q3YTD, COVER
    periodicity             VARCHAR,              -- annual | quarterly | ytd | null
    joinable_to_statements  BOOLEAN  NOT NULL,    -- false => period_end not a fiscal date
    value                   DOUBLE   NOT NULL,
    form_type               VARCHAR  NOT NULL,
    accession_no            VARCHAR  NOT NULL,
    filing_date             DATE     NOT NULL,
    acceptance_datetime     VARCHAR,              -- ISO8601 from submissions join (nullable)
    is_amendment            BOOLEAN  NOT NULL
);
"""

COLUMNS = [
    "canonical_concept", "raw_tag", "taxonomy", "tag_rank", "unit",
    "period_start", "period_end", "fiscal_year", "fiscal_period", "periodicity",
    "joinable_to_statements", "value", "form_type", "accession_no",
    "filing_date", "acceptance_datetime", "is_amendment",
]


def build_store(cik: int | str = 320193, db_path: str | Path = ":memory:",
                refresh: bool = False, hard_fail: bool = True) -> duckdb.DuckDBPyConnection:
    """Ingest -> normalize -> validate -> load into a fresh DuckDB table.

    Enforces the synonym invariant before loading. With hard_fail=True (default)
    a violation raises SynonymInvariantError and NOTHING is stored. With
    hard_fail=False the store is still built (use find_synonym_violations to
    inspect) — intended only for diagnostics.
    """
    concept_map = load_concept_map()
    bundle = fetch_company(cik, refresh=refresh)
    rows = normalize(bundle, concept_map)

    if hard_fail:
        assert_synonym_invariant(rows)

    con = duckdb.connect(str(db_path))
    con.execute(f"DROP TABLE IF EXISTS {TABLE}")
    con.execute(SCHEMA)

    payload = [tuple(d[c] for c in COLUMNS) for d in rows_as_dicts(rows)]
    placeholders = ", ".join(["?"] * len(COLUMNS))
    con.executemany(f"INSERT INTO {TABLE} VALUES ({placeholders})", payload)

    # indexes that match the query access pattern
    con.execute(f"CREATE INDEX idx_concept ON {TABLE}(canonical_concept)")
    return con


if __name__ == "__main__":
    con = build_store()
    n = con.execute(f"SELECT count(*) FROM {TABLE}").fetchone()[0]
    print("rows in store:", n)
    print(con.execute(
        f"SELECT canonical_concept, count(*) FROM {TABLE} GROUP BY 1 ORDER BY 1"
    ).fetchall())
