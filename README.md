# Point-in-Time Fundamentals — single-company vertical slice

A correctness-first foundation for a survivorship-bias-free fundamentals dataset
from SEC EDGAR. This is **one vertical slice**: ingest → normalize → store →
point-in-time query, proven end-to-end for **Apple Inc. (CIK 0000320193)**.

The single design goal is: **never return a number that was not publicly known
as of a given date, and correctly distinguish an original figure from a later
restatement.**

## Layout

```
pit/
  config/concepts.yaml   # concept map AS DATA (canonical -> ordered tag list)
  ingest.py              # SEC fetch: companyfacts + full submissions history
  normalize.py           # flatten -> bitemporal rows; derive fiscal period etc.
  validate.py            # build-time synonym invariant (hard-fail)
  store.py               # DuckDB table (multiple rows per period; never collapsed)
  query.py               # the point-in-time query + provenance
  identities.py          # point-in-time accounting-identity reconciliation
  reconcile_xlsx.py      # reproduce-the-filing check vs rendered statements
  example.py             # worked example (acceptance criterion c)
tests/test_pit.py        # no-lookahead, restatement, duplicate-reporting, invariant
tests/test_hardening.py  # synonym invariant, shares split, identities, xlsx recon
```

## Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# (optional) set your SEC contact in pit/ingest.py -> CONTACT
python -m pit.example          # worked example, three as-of dates with provenance
python -m pit.identities       # accounting-identity reconciliation report
python -m pit.reconcile_xlsx   # reproduce-the-filing reconciliation report
python -m pytest -q            # all tests

streamlit run pit/gui.py       # interactive GUI (all of the above, in a browser)
```

First run fetches and caches raw JSON under `data/raw/`; later runs reuse the
cache (`build_store(..., refresh=True)` to refetch).

## GUI

`streamlit run pit/gui.py` opens a browser app that is a thin presentation
layer over the same `point_in_time()` API — no query logic is duplicated, so
the numbers match the CLI exactly.

A sidebar **company picker** (searchable by ticker or name, backed by SEC's
`company_tickers.json`, with a direct-CIK fallback) lets you run all of this
for **any XBRL-era filer**, not just Apple — the ingest/normalize/store/query
path is already CIK-agnostic. Companies whose two mapped synonym tags disagree
within a filing trip the build-time synonym invariant; an "Enforce synonym
invariant" toggle lets you build anyway for inspection. Four tabs:

- **Point-in-time query** — pick concept, as-of date, periodicity; see the
  value with full provenance.
- **Restatement timeline** — fix a period and slide the as-of date to watch a
  value flip across a restatement (e.g. Apple `total_assets` @ 2008-09-27).
- **Identity reconciliation** — the `Assets = Liabilities + Equity` and
  gross-profit checks as of a chosen date.
- **Reconcile & browse** — the reproduce-the-filing XLSX reconciliation
  (Apple-only; it is tuned to Apple's specific 10-K row labels) and a
  filterable view of the raw bitemporal facts table (any company).

The store is built once per session (`@st.cache_resource`); the first run
fetches from SEC and caches under `data/raw/` as usual.

## Data sources (verified)

- Company Facts: `https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json`
- Submissions:   `https://data.sec.gov/submissions/CIK0000320193.json` plus the
  paginated overflow files it references (older filings) — needed to resolve
  `acceptanceDateTime` for historical accessions.

Every request carries a descriptive `User-Agent` (SEC requirement) and a global
throttle holds us under SEC's 10 req/s ceiling (`MAX_REQUESTS_PER_SEC = 8`).
Company Facts already returns **consolidated, non-dimensional** values, so
segment/dimensional data never enters this slice.

## Bitemporal data model

One row **per filing that reported a (concept, period)** — the store is never
collapsed to a single "current" value. Each row keeps both `canonical_concept`
and the original `raw_tag`, plus the two time axes:

- **valid time** — `period_start` (nullable), `period_end`, `fiscal_year`,
  `fiscal_period`, `periodicity`, `joinable_to_statements`.
- **decision time** — `filing_date`, `acceptance_datetime`, `form_type`,
  `accession_no`, `is_amendment`.

### The concept map and the synonym rule

The concept map (`config/concepts.yaml`) is data. Each canonical concept maps to
an **ordered list of tags**, and `tag_rank` is used by the query only as the
**final** tie-break (after `filing_date`). That is sound *only* if the tags in a
list denote the **same** accounting line item — i.e. true synonyms / sequential
renames. So the project enforces a **synonym invariant**:

> Within any single `(accession_no, canonical_concept, period_start, period_end)`,
> every mapped tag that appears must carry the **same value**. A disagreement
> means the tags are not synonyms; it is a **hard build-time error**
> (`SynonymInvariantError`), never a silent tie-break.

Concepts that look similar but measure different things are kept **separate**:

| Canonical concept | Tag | Note |
|---|---|---|
| `revenue` | `RevenueFromContract…` / `SalesRevenueNet` / `Revenues` | true renames — one concept |
| `cash_and_equivalents` | `CashAndCashEquivalentsAtCarryingValue` | balance-sheet cash, excl. restricted |
| `cash_incl_restricted` | `CashCashEquivalentsRestrictedCash…` | cash-flow total, **incl. restricted** |
| `accounts_payable` | `AccountsPayableCurrent` | current |
| `accounts_payable_total` | `AccountsPayable` | total (sparse: 2 rows for Apple) |
| `cost_of_revenue` | `CostOfGoodsAndServicesSold` | for the gross-profit identity |

The cash split is not cosmetic: for Apple the two cash tags **disagree within the
same filing in 42 periods** (e.g. a \$772M restricted-cash difference at
2023-09-30). Merging them would trip the invariant — which is exactly the point.

### The share-count timing split

Two tags both look like "shares outstanding" but use different measurement dates:

- `shares_outstanding` ← `us-gaap:CommonStockSharesOutstanding` — measured at the
  **balance-sheet date** (a real fiscal period end). Joinable to the statements;
  periodicity derived as usual.
- `shares_outstanding_cover` ← `dei:EntityCommonStockSharesOutstanding` — measured
  at the **filing cover date** (e.g. 2025-04-18), which is *not* a fiscal period
  end. Marked `joinable_to_statements = false`, `periodicity = NULL`,
  `fiscal_period = 'COVER'`, so it can never be silently joined to a statement
  period. The point-in-time query still works for it — by as-of recency only
  (a `periodicity='annual'|'quarterly'` query returns nothing for it, by design).

### Other things that are easy to get wrong

- **The row's `fy`/`fp` is the filing's context, not the period's.** The restated
  FY2008 Assets fact is tagged `fy=2009/fp=Q3` in the raw JSON. We **derive**
  `fiscal_year` / `fiscal_period` from the period's start/end dates and the
  company's fiscal-year-end month, never from the row's `fy`/`fp`.
- **acceptanceDateTime / is_amendment** aren't in Company Facts: the former is
  joined from submissions by accession; the latter is read off the form (`.../A`).

## The point-in-time query

`point_in_time(con, concept, as_of, periodicity=None, target_period_end=None)`:

1. filter to the canonical concept;
2. drop rows with `filing_date > as_of` (no look-ahead);
3. pick the most recent `period_end <= as_of` for that `periodicity`
   (`periodicity=None` means "any"); **or**, if `target_period_end` is given, fix
   that exact period instead;
4. among the surviving rows for that period, take the latest `filing_date`, tie-
   broken by `acceptance_datetime`, then `accession_no`, then `tag_rank`;
5. return the value with full provenance (form, accession, filing/acceptance
   dates, raw tag, period, amendment flag).

### Why `target_period_end` exists

The plain "most recent period" rule (step 3) cannot surface a restatement of an
*older* period: by the time any filing restates FY2008, FY2009 has already been
filed and is the "most recent" annual period. To interrogate a specific
historical period and watch its value flip across a restatement as `as_of`
advances, fix the period with `target_period_end` (periodicity then defaults to
"any" so the annual 10-K/10-K-A and quarterly comparatives compete purely on
filing recency). The default behaviour is the spec's most-recent-period query.

## Proven correctness (the acceptance tests)

- **(a) No-lookahead property** — 2000 random `(concept, as_of, periodicity)`
  draws; every non-empty result satisfies `filing_date <= as_of` (and
  `period_end <= as_of`).
- **(b) Real restatement** — Apple's retrospective adoption of the iPhone
  revenue-recognition change restated **total assets at 2008-09-27 from \$39.572B
  to \$36.171B** via a **10-K/A filed 2010-01-25**. The query returns \$39.572B
  for `as_of = 2009-12-01` (original 10-K) and \$36.171B for `as_of = 2010-06-01`
  (the amendment), and still returns the original value the day before the
  amendment was filed.
- **(b′) Duplicate reporting** — the FY2023 year-end balance reported across a
  10-K and several later 10-Qs is disambiguated to the latest filing on/before
  the as-of date.
- **(c) Worked example** — `python -m pit.example` prints revenue and total
  assets at three dates, each with provenance (also shows the revenue tag-switch
  across `SalesRevenueNet → Revenues → RevenueFromContract...`).

## Hardening validations

These were added in the correctness-hardening pass (`tests/test_hardening.py`):

- **Synonym invariant** (`pit/validate.py`) — enforced at build time; the real
  concept map yields **zero violations**, and a test that deliberately re-merges
  the two cash tags proves the invariant **rejects** the conflation (42
  violations → `SynonymInvariantError`).
- **Share-count timing** — a test asserts **no** `shares_outstanding_cover` fact
  carries a statement periodicity (`periodicity` is `NULL`, `joinable_to_statements`
  is false), while the joinable `shares_outstanding` does.
- **Accounting identities, point-in-time** (`pit/identities.py`) — every term is
  resolved with the *same* as-of date. For Apple:
  - `Assets = Liabilities + Equity` — **0 violations across 69 periods** (asserted
    exactly; any nonzero count is a real regression).
  - `GrossProfit = Revenue − CostOfRevenue` — reported with a tolerance over **109
    checked periods** (12 skipped for tagging gaps), 0 violations.
- **Reproduce-the-filing reconciliation** (`pit/reconcile_xlsx.py`) — **independent
  ground truth**: the SEC-rendered `Financial_Report.xlsx` for the FY2021–FY2024
  10-Ks. Six headline lines × four filings (24 checks) all reproduce exactly, as of
  each filing's `filing_date`. This catches wrong-tag / wrong-period / sign /
  scale bugs that a companyfacts-only test cannot. It validates that the pipeline
  reproduces the filing's **rendered statements** — not the correctness of the
  underlying XBRL (both derive from the same XBRL). Workbooks are cached under
  `data/raw/` for offline reruns.

## Current limitations (by design, for this slice)

- **One company, XBRL era only.** Company Facts covers ~2009 onward; pre-XBRL
  paper filings are absent. The code path is company-agnostic (pad any CIK), so
  it would run unchanged on a delisted company.
- **Instant periodicity is form-derived and fragile.** A balance-sheet snapshot
  is labelled `annual` if it came from a 10-K, `quarterly` if from a 10-Q. A
  10-Q's prior-fiscal-year-end *comparative* (a year-end date) is therefore
  labelled `quarterly`. This is the project-chosen rule; fiscal-year-end-date
  alignment would be more robust.
- **Fiscal-period derivation assumes a stable fiscal-year-end month.** Month-
  based, robust to Apple's 52/53-week day drift, but a company that *changes* its
  fiscal year-end would need extra handling.
- **YTD periods** (6-/9-month cumulatives) are stored and labelled `Q2YTD`/
  `Q3YTD` but are not selectable via the `periodicity` argument.
- **No segment/dimensional data** (Company Facts excludes it). "Latest filing
  wins at the as-of date" is treated as point-in-time truth; we do not attempt
  semantic reconciliation of restatement *reasons*.
- `value` is stored as `DOUBLE` (exact for these magnitudes, all < 2⁵³).
- `acceptance_datetime` is nullable (null only if an accession is missing from
  the submissions history); the query tie-break sorts nulls last.
