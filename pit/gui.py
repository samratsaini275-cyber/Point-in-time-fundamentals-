"""Streamlit GUI for the point-in-time fundamentals slice.

A thin presentation layer over the existing pit API — no query logic is
reimplemented here. Every number comes from `point_in_time()`,
`pit.identities`, or `pit.reconcile_xlsx`, so the GUI inherits the project's
correctness guarantees unchanged.

Run:  streamlit run pit/gui.py
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

# `streamlit run pit/gui.py` execs this file as a script with no package
# context, so make the project root importable and use absolute imports.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pit.store import build_store, TABLE
from pit.query import point_in_time, PITResult
from pit.normalize import load_concept_map
from pit.identities import run_all
from pit.reconcile_xlsx import reconcile
from pit.ingest import SecClient, fetch_company
from pit.validate import SynonymInvariantError

APPLE_CIK = 320193  # the only company the XLSX reconciliation is tuned to


# --- shared setup ----------------------------------------------------------

@st.cache_resource(show_spinner="Building store (first run fetches from SEC)…")
def get_store(cik: int, hard_fail: bool = True):
    """Ingest -> normalize -> validate -> load for one CIK, cached per (cik,
    hard_fail).

    build_store() is expensive (network on first run) and returns a live
    in-memory DuckDB connection; caching it keeps every widget interaction
    cheap and shares one connection per company.
    """
    return build_store(cik, hard_fail=hard_fail)


@st.cache_data(show_spinner="Loading SEC company list…")
def company_list() -> list[tuple[int, str, str]]:
    """(cik, ticker, title) for every SEC-registered ticker, sorted by ticker."""
    data = SecClient().get_json("https://www.sec.gov/files/company_tickers.json")
    rows = [(int(v["cik_str"]), v["ticker"], v["title"]) for v in data.values()]
    rows.sort(key=lambda r: r[1])
    return rows


@st.cache_data
def entity_name(cik: int) -> str:
    """Human-readable issuer name from the cached companyfacts (best-effort)."""
    try:
        return fetch_company(cik)["companyfacts"].get("entityName", f"CIK {cik}")
    except Exception:
        return f"CIK {cik}"


@st.cache_data
def concept_options() -> dict[str, str]:
    """canonical_concept -> 'concept — label' for the dropdowns."""
    cmap = load_concept_map()
    return {c: f"{c} — {spec.get('label', '')}"
            for c, spec in cmap["concepts"].items()}


def fmt_value(value: float, unit: str) -> str:
    """Match example.py: dollars as $X.XXXB, share counts as raw integers."""
    if unit == "USD":
        return f"${value / 1e9:,.3f}B"
    return f"{value:,.0f} {unit}"


def render_result(r: PITResult | None, as_of: date) -> None:
    if r is None:
        st.warning(f"No figure known as of {as_of}.")
        return
    st.metric(r.canonical_concept, fmt_value(r.value, r.unit))
    st.caption(
        f"FY{r.fiscal_year} {r.fiscal_period} · period_end {r.period_end}"
        + (f" · period_start {r.period_start}" if r.period_start else "")
        + (f" · periodicity {r.periodicity}" if r.periodicity else "")
    )
    st.code(r.provenance(), language=None)


PERIODICITY_LABELS = {"any": None, "annual": "annual", "quarterly": "quarterly"}


# --- page ------------------------------------------------------------------

st.set_page_config(page_title="Point-in-Time Fundamentals", layout="wide")
st.title("Point-in-Time Fundamentals")
st.caption("Never returns a number that was not publicly known as of the "
           "chosen date; distinguishes an original figure from a restatement.")

# --- company picker (sidebar) ----------------------------------------------
with st.sidebar:
    st.header("Company")
    companies = company_list()
    labels = {cik: f"{ticker} — {title}" for cik, ticker, title in companies}
    cik_options = [c for c, _, _ in companies]
    default_idx = cik_options.index(APPLE_CIK) if APPLE_CIK in cik_options else 0
    picked = st.selectbox("Search by ticker or name", cik_options,
                          index=default_idx,
                          format_func=lambda c: labels.get(c, f"CIK {c}"))
    manual = st.text_input("…or enter a CIK directly", "")
    cik = int(manual) if manual.strip().isdigit() else picked
    hard_fail = st.checkbox(
        "Enforce synonym invariant (hard-fail)", value=True,
        help="On by default. Some companies report two mapped synonym tags with "
             "different values in one filing, which legitimately trips the "
             "invariant. Uncheck to build the store anyway (diagnostic mode).")

# --- build the store for the chosen company --------------------------------
try:
    con = get_store(cik, hard_fail)
except SynonymInvariantError as e:
    st.error(f"Synonym invariant violated for CIK {cik}. Two mapped synonym "
             f"tags disagree within a filing, so they may not be true synonyms "
             f"for this company. Uncheck **Enforce synonym invariant** in the "
             f"sidebar to inspect anyway.\n\n```\n{e}\n```")
    st.stop()
except Exception as e:
    st.error(f"Could not build a store for CIK {cik}. SEC may have no XBRL "
             f"Company Facts for it (e.g. pre-2009 or non-filer).\n\n```\n{e}\n```")
    st.stop()

st.subheader(f"{entity_name(cik)} — CIK {cik}")

CONCEPTS = concept_options()
CONCEPT_KEYS = list(CONCEPTS)


def _concept_selectbox(label: str, key: str, default: str | None = None):
    index = CONCEPT_KEYS.index(default) if default in CONCEPT_KEYS else 0
    return st.selectbox(label, CONCEPT_KEYS, index=index,
                        format_func=lambda c: CONCEPTS[c], key=key)


def _period_ends(concept: str) -> list[date]:
    return [r[0] for r in con.execute(
        f"SELECT DISTINCT period_end FROM {TABLE} "
        f"WHERE canonical_concept = ? ORDER BY period_end DESC",
        [concept]).fetchall()]


def _filing_date_bounds() -> tuple[date, date]:
    lo, hi = con.execute(
        f"SELECT min(filing_date), max(filing_date) FROM {TABLE}").fetchone()
    return lo, hi


tab_query, tab_timeline, tab_identities, tab_recon = st.tabs(
    ["Point-in-time query", "Restatement timeline",
     "Identity reconciliation", "Reconcile & browse"])


# --- Tab 1: point-in-time query --------------------------------------------

with tab_query:
    st.subheader("What was this figure, as publicly known on a given date?")
    c1, c2, c3 = st.columns(3)
    with c1:
        concept = _concept_selectbox("Concept", key="q_concept", default="revenue")
    with c2:
        as_of = st.date_input("As of", value=date(2025, 6, 1), key="q_asof")
    with c3:
        per_label = st.radio("Periodicity", list(PERIODICITY_LABELS),
                             index=1, horizontal=True, key="q_per")
    r = point_in_time(con, concept, as_of, periodicity=PERIODICITY_LABELS[per_label])
    render_result(r, as_of)


# --- Tab 2: restatement timeline -------------------------------------------

with tab_timeline:
    st.subheader("Watch one period's value flip across a restatement")
    st.caption("Fix a specific period, then slide the as-of date. A "
               "restatement shows as a step change. Try total_assets, "
               "period_end 2008-09-27 (Apple's $39.572B → $36.171B 10-K/A).")
    c1, c2 = st.columns(2)
    with c1:
        t_concept = _concept_selectbox("Concept", key="t_concept",
                                       default="total_assets")
    ends = _period_ends(t_concept)
    with c2:
        default_end = date(2008, 9, 27)
        end_index = ends.index(default_end) if default_end in ends else 0
        t_end = st.selectbox("Period end (fixed)", ends, index=end_index,
                             key="t_end")

    lo, hi = _filing_date_bounds()
    # only as-of dates from the period end onward can know this period
    slider_lo = max(t_end, lo)
    slider_hi = hi + timedelta(days=1)
    sel_asof = st.slider("As of", min_value=slider_lo, max_value=slider_hi,
                         value=slider_hi, key="t_asof")

    # series across the whole range for the chart
    days = (slider_hi - slider_lo).days
    step = max(days // 200, 1)
    points = []
    d = slider_lo
    while d <= slider_hi:
        rr = point_in_time(con, t_concept, d, target_period_end=t_end)
        if rr is not None:
            points.append({"as_of": d, "value": rr.value})
        d += timedelta(days=step)
    if points:
        df = pd.DataFrame(points).set_index("as_of")
        st.line_chart(df, y="value")
    else:
        st.info("No values known for this period across the date range.")

    st.markdown(f"**As of {sel_asof}:**")
    render_result(point_in_time(con, t_concept, sel_asof,
                                target_period_end=t_end), sel_asof)


# --- Tab 3: identity reconciliation ----------------------------------------

with tab_identities:
    st.subheader("Accounting identities, resolved point-in-time")
    st.caption("Every term is resolved with the same as-of date and the same "
               "filing-recency tie-break as the main query.")
    id_asof = st.date_input("As of", value=date(2026, 6, 1), key="id_asof")
    for rep in run_all(con, id_asof):
        if rep.ok:
            st.success(rep.summary())
        else:
            st.error(rep.summary())
            st.dataframe(pd.DataFrame(rep.violations), width='stretch')


# --- Tab 4: xlsx reconciliation + raw facts browser ------------------------

with tab_recon:
    st.subheader("Reproduce-the-filing reconciliation")
    st.caption("Compares the SEC-rendered Financial_Report.xlsx headline lines "
               "against the store, as of each filing's filing_date. Fetches "
               "(and caches) the workbooks — runs on demand.")
    if cik != APPLE_CIK:
        st.info("Reproduce-the-filing reconciliation is tuned to Apple's "
                "specific 10-K filings and headline row labels, so it is only "
                "available for Apple (CIK 320193). The raw facts browser below "
                "works for any company.")
    elif st.button("Run reconciliation"):
        with st.spinner("Fetching rendered statements and reconciling…"):
            results = reconcile(con)
        df = pd.DataFrame([{
            "fiscal_year": r.fiscal_year,
            "metric": r.metric,
            "rendered_$B": round(r.rendered / 1e9, 3),
            "store_$B": None if r.store is None else round(r.store / 1e9, 3),
            "match": "OK" if r.matched else "MISMATCH",
        } for r in results])
        n_ok = int((df["match"] == "OK").sum())
        st.write(f"{n_ok} / {len(df)} matched")
        st.dataframe(df, width='stretch')

    st.divider()
    st.subheader("Raw bitemporal facts")
    f1, f2 = st.columns(2)
    with f1:
        fc = st.selectbox("Filter concept", ["(all)"] + CONCEPT_KEYS,
                          key="b_concept")
    forms = [r[0] for r in con.execute(
        f"SELECT DISTINCT form_type FROM {TABLE} ORDER BY 1").fetchall()]
    with f2:
        ff = st.selectbox("Filter form type", ["(all)"] + forms, key="b_form")

    conds, params = [], []
    if fc != "(all)":
        conds.append("canonical_concept = ?")
        params.append(fc)
    if ff != "(all)":
        conds.append("form_type = ?")
        params.append(ff)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    facts = con.execute(
        f"SELECT * FROM {TABLE} {where} "
        f"ORDER BY canonical_concept, period_end DESC, filing_date DESC "
        f"LIMIT 5000", params).df()
    st.caption(f"{len(facts)} rows (capped at 5000)")
    st.dataframe(facts, width='stretch')
