"""Normalize raw SEC JSON into bitemporal fact rows.

Key correctness decisions encoded here:

1. The companyfacts row's `fy`/`fp` describe the *filing's* fiscal context, NOT
   the period the value belongs to (e.g. the restated FY2008 Assets row carries
   fy=2009/fp=Q3). We therefore DERIVE fiscal_year / fiscal_period / periodicity
   from the period geometry (start/end dates) and never trust the row's fy/fp.

2. companyfacts carries `filed` and `form` but not acceptanceDateTime, so we
   join accession -> acceptanceDateTime from the submissions history.

3. is_amendment is taken from the form type (`.../A`).

4. periodicity:
     * duration concepts  -> intrinsic to span length
         ~90d  -> quarterly,  ~365d -> annual,  ~180/270d -> ytd (Q2YTD/Q3YTD)
     * instant concepts   -> derived from reporting form
         10-K family -> annual,  10-Q family -> quarterly  (per project decision;
         see README "Limitations" for the known fragility of this rule).

companyfacts is already consolidated / non-dimensional, so segment data never
enters this slice; we assert that invariant in tests.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "concepts.yaml"


@dataclass(frozen=True)
class FactRow:
    canonical_concept: str
    raw_tag: str
    taxonomy: str
    tag_rank: int           # 0 = most-preferred tag for the concept
    unit: str
    period_start: date | None
    period_end: date
    fiscal_year: int
    fiscal_period: str      # Q1..Q4, FY, Q2YTD, Q3YTD, COVER
    periodicity: str | None  # 'annual' | 'quarterly' | 'ytd' | None
    joinable_to_statements: bool  # false => period_end is not a fiscal-statement date
    value: float
    form_type: str
    accession_no: str
    filing_date: date
    acceptance_datetime: str | None
    is_amendment: bool


# --- concept map -----------------------------------------------------------

def load_concept_map(path: Path = CONFIG_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def build_tag_index(concept_map: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """(taxonomy, tag) -> {canonical, period_type, rank}."""
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for canonical, spec in concept_map["concepts"].items():
        for rank, t in enumerate(spec["tags"]):
            index[(t["taxonomy"], t["tag"])] = {
                "canonical": canonical,
                "period_type": spec["period_type"],
                "rank": rank,
                "joinable": spec.get("joinable_to_statements", True),
            }
    return index


# --- submissions join ------------------------------------------------------

def build_acceptance_index(submissions: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """accession_no -> {acceptance_datetime, form, filing_date} across all files."""
    out: dict[str, dict[str, str]] = {}

    def ingest(block: dict[str, Any]) -> None:
        accns = block.get("accessionNumber", [])
        for i, accn in enumerate(accns):
            out[accn] = {
                "acceptance_datetime": block["acceptanceDateTime"][i],
                "form": block["form"][i],
                "filing_date": block["filingDate"][i],
            }

    for i, sub in enumerate(submissions):
        if i == 0:
            ingest(sub["filings"]["recent"])
        else:
            ingest(sub)  # overflow files are flat parallel arrays
    return out


# --- fiscal-period derivation ----------------------------------------------

def _fye_month(submissions_main: dict[str, Any]) -> int:
    """Fiscal-year-end month (1-12). Falls back to September if unparseable."""
    raw = submissions_main.get("fiscalYearEnd") or ""
    try:
        return int(raw[:2])
    except (ValueError, IndexError):
        return 9


def _fiscal_year_and_quarter(period_end: date, fye_month: int) -> tuple[int, int]:
    """Return (fiscal_year, quarter_index 1..4) for a date, given the FYE month.

    fiscal_year is labelled by the calendar year in which the fiscal year ends.
    Month-based (robust to the 52/53-week day drift in Apple's calendar).
    """
    fy = period_end.year if period_end.month <= fye_month else period_end.year + 1
    quarter = ((period_end.month - fye_month - 1) % 12) // 3 + 1
    return fy, quarter


def _classify_duration(days: int) -> tuple[str | None, str | None]:
    """(periodicity, period_kind) from a span length in days."""
    if 80 <= days <= 100:
        return "quarterly", "Q"
    if 160 <= days <= 200:
        return "ytd", "Q2YTD"
    if 250 <= days <= 290:
        return "ytd", "Q3YTD"
    if 340 <= days <= 380:
        return "annual", "FY"
    return None, None  # unusual span (e.g. partial/stub period) — not selectable


def _instant_periodicity(form_type: str) -> str | None:
    base = form_type.split("/")[0]  # strip amendment suffix: 10-K/A -> 10-K
    if base.startswith("10-K"):
        return "annual"
    if base.startswith("10-Q"):
        return "quarterly"
    return None


# --- main normalization ----------------------------------------------------

def normalize(bundle: dict[str, Any], concept_map: dict[str, Any] | None = None) -> list[FactRow]:
    concept_map = concept_map or load_concept_map()
    tag_index = build_tag_index(concept_map)
    acceptance = build_acceptance_index(bundle["submissions"])
    fye_month = _fye_month(bundle["submissions"][0])

    facts = bundle["companyfacts"]["facts"]
    rows: list[FactRow] = []

    for taxonomy, concepts in facts.items():
        for tag, obj in concepts.items():
            meta = tag_index.get((taxonomy, tag))
            if meta is None:
                continue  # tag not in our concept map
            period_type = meta["period_type"]
            for unit, raw_rows in obj["units"].items():
                for r in raw_rows:
                    rows.append(_make_row(r, taxonomy, tag, meta, period_type,
                                          unit, acceptance, fye_month))
    return rows


def _make_row(r, taxonomy, tag, meta, period_type, unit, acceptance, fye_month) -> FactRow:
    period_end = date.fromisoformat(r["end"])
    period_start = date.fromisoformat(r["start"]) if r.get("start") else None
    form_type = r["form"]
    accn = r["accn"]

    joinable = meta["joinable"]
    fy, quarter = _fiscal_year_and_quarter(period_end, fye_month)

    if not joinable:
        # Cover-date facts: period_end is the measurement date, not a fiscal
        # statement date. Never label them with a statement periodicity, so they
        # can never be silently joined to a fiscal period_end.
        periodicity = None
        fiscal_period = "COVER"
    elif period_type == "duration":
        days = (period_end - period_start).days if period_start else 0
        periodicity, kind = _classify_duration(days)
        if kind == "Q":
            fiscal_period = f"Q{quarter}"
        elif kind == "FY":
            fiscal_period = "FY"
        elif kind in ("Q2YTD", "Q3YTD"):
            fiscal_period = kind
        else:
            fiscal_period = f"Q{quarter}"  # label best-effort even if not selectable
    else:  # instant
        periodicity = _instant_periodicity(form_type)
        fiscal_period = "FY" if periodicity == "annual" else f"Q{quarter}"

    sub = acceptance.get(accn, {})
    return FactRow(
        canonical_concept=meta["canonical"],
        raw_tag=tag,
        taxonomy=taxonomy,
        tag_rank=meta["rank"],
        unit=unit,
        period_start=period_start,
        period_end=period_end,
        fiscal_year=fy,
        fiscal_period=fiscal_period,
        periodicity=periodicity,
        joinable_to_statements=joinable,
        value=float(r["val"]),
        form_type=form_type,
        accession_no=accn,
        filing_date=date.fromisoformat(r["filed"]),
        acceptance_datetime=sub.get("acceptance_datetime"),
        is_amendment=form_type.endswith("/A"),
    )


def rows_as_dicts(rows: Iterable[FactRow]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]
