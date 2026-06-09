"""SEC EDGAR ingestion.

Fetches the per-company Company Facts JSON and the FULL submissions history
(the `recent` block plus every paginated overflow file) for one CIK, and caches
the raw JSON to disk. Designed to be company-agnostic so the same code path runs
on a delisted company.

SEC fair-access rules enforced here:
  * a descriptive User-Agent on every request (REQUIRED by SEC),
  * a global rate limit kept comfortably under 10 requests/second.

Fill in CONTACT below with your real contact email before running.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import requests

# --- SEC fair-access configuration -----------------------------------------
# REQUIRED: SEC blocks requests without a descriptive User-Agent identifying a
# real contact. Pre-filled from the session; verify/replace with your preferred
# contact if needed.
CONTACT = "Samrat Saini samratsaini275@gmail.com"
USER_AGENT = f"PIT-fundamentals-research {CONTACT}"

MAX_REQUESTS_PER_SEC = 8  # stay safely under the SEC's 10 req/s ceiling

DATA_HOST = "https://data.sec.gov"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / "data" / "raw"


def cik10(cik: int | str) -> str:
    """Zero-pad a CIK to the 10-digit form used in SEC URLs."""
    return f"{int(cik):010d}"


class _RateLimiter:
    """Minimum-interval throttle, thread-safe."""

    def __init__(self, max_per_sec: float):
        self._min_interval = 1.0 / max_per_sec
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
            self._next_allowed = max(now, self._next_allowed) + self._min_interval


class SecClient:
    def __init__(self, user_agent: str = USER_AGENT, max_per_sec: float = MAX_REQUESTS_PER_SEC):
        if "your-email@example.com" in user_agent:
            raise RuntimeError(
                "Set CONTACT in pit/ingest.py to your real email before hitting SEC. "
                "SEC requires a descriptive User-Agent and will throttle/deny otherwise."
            )
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self._limiter = _RateLimiter(max_per_sec)

    def get_json(self, url: str) -> Any:
        self._limiter.wait()
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_bytes(self, url: str) -> bytes:
        self._limiter.wait()
        resp = self._session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content


def fetch_company(cik: int | str, cache_dir: Path = DEFAULT_CACHE,
                  client: SecClient | None = None, refresh: bool = False) -> dict[str, Any]:
    """Fetch + cache companyfacts and the complete submissions history.

    Returns {"companyfacts": <json>, "submissions": [<recent json>, *overflow]}.
    Cached files are reused unless refresh=True.
    """
    client = client or SecClient()
    padded = cik10(cik)
    cache_dir.mkdir(parents=True, exist_ok=True)

    def _load_or_fetch(url: str, fname: str) -> Any:
        path = cache_dir / fname
        if path.exists() and not refresh:
            return json.loads(path.read_text())
        data = client.get_json(url)
        path.write_text(json.dumps(data))
        return data

    facts = _load_or_fetch(
        f"{DATA_HOST}/api/xbrl/companyfacts/CIK{padded}.json",
        f"companyfacts_CIK{padded}.json",
    )

    # submissions: main file holds `recent`; older filings are paginated into
    # filings.files[*].name. We need ALL of them to resolve acceptanceDateTime
    # for historical accessions (e.g. the FY2008 restatement).
    main = _load_or_fetch(
        f"{DATA_HOST}/submissions/CIK{padded}.json",
        f"submissions_CIK{padded}.json",
    )
    submissions = [main]
    for ref in main.get("filings", {}).get("files", []):
        name = ref["name"]
        submissions.append(_load_or_fetch(f"{DATA_HOST}/submissions/{name}", name))

    return {"companyfacts": facts, "submissions": submissions}


if __name__ == "__main__":
    bundle = fetch_company(320193)
    subs = sum(len(s.get("filings", {}).get("recent", {}).get("accessionNumber", []))
               if i == 0 else len(s.get("accessionNumber", []))
               for i, s in enumerate(bundle["submissions"]))
    print("companyfacts entity:", bundle["companyfacts"]["entityName"])
    print("submissions files:", len(bundle["submissions"]), "| total filings:", subs)
