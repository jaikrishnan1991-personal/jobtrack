"""Indeed discovery via Apify.

Runs curious_coder/indeed-scraper on Apify (no login, public Indeed search pages) across
every query x location in config.json -> indeed. Unlike the LinkedIn actor, this one takes a
single query/location/country per run, so a sync here fans out into several small Apify runs
(started concurrently, polled together) instead of one batched run.

Reuses linkedin.py for everything actor-agnostic: the Apify HTTP client, the field-access
heuristics (_first), and the enrichment/scoring heuristics (domains, archetype, experience,
salary, company stage, keyword aggregation). Those operate on generic title/company/location/
description text and already read known_gaps/claimable_unstated from config - there is nothing
LinkedIn-specific about them despite living in that module.

Token: shared APIFY_TOKEN (see linkedin.get_token).

CLI (for cron / Task Scheduler):
    python -m app.indeed              # run configured searches, import results
    python -m app.indeed --dry-run    # print the search specs and exit
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import linkedin as li
from . import scoring

ROOT = Path(__file__).resolve().parent.parent

# curious_coder/indeed-scraper nests company/company-size info under "companyDetails" and
# gives a relative "viewJobLink" instead of an absolute URL - none of that matches the flat
# key names linkedin.item_to_posting()'s _first() looks for. Flatten it here rather than
# touching that generic (and already-working) parsing code.
_COUNTRY_DOMAIN = {"in": "in.indeed.com", "ae": "ae.indeed.com", "sg": "sg.indeed.com",
                   "om": "om.indeed.com", "us": "www.indeed.com", "uk": "uk.indeed.com"}


_SALARY_PERIOD = {"hour": "per hour", "day": "per day", "week": "per week",
                  "month": "per month", "year": "per year"}


def _salary_text(sal: dict) -> str:
    """{'min': 7000, 'max': 9500, 'type': 'MONTH', 'currencyCode': 'SGD'} ->
    'SGD 7,000 - 9,500 per month'. Empty string when nothing was actually stated."""
    cur = (sal.get("currencyCode") or "").strip()
    nums = [f"{v:,.0f}" for v in (sal.get("min"), sal.get("max"))
            if isinstance(v, (int, float))]
    if not nums or not cur:
        return ""
    period = _SALARY_PERIOD.get((sal.get("type") or "").strip().lower(), "")
    uniq = list(dict.fromkeys(nums))
    return " ".join(x for x in [cur, " - ".join(uniq), period] if x)


def _normalize_item(item: dict) -> dict:
    it = dict(item)
    # `location` here is a nested object, and it is checked before `formattedLocation` by the
    # shared field-alias lookup - left alone it gets str()'d into a raw dict in the tracker and
    # then printed verbatim on a resume. Replace it with the human-readable string.
    loc = it.get("location")
    if not isinstance(loc, str) or not loc.strip():
        nested = loc if isinstance(loc, dict) else {}
        it["location"] = (it.get("formattedLocation")
                          or nested.get("fullAddress")
                          or (nested.get("formatted") or {}).get("long") or "")
    # `salary` is a nested object too, and str()'ing it puts
    # "{'max': 9500, 'min': 7000, 'type': 'MONTH', 'currencyCode': 'SGD'}" on screen as the
    # stated compensation. Render it the way a human would write it; the shared salary parser
    # then reads the currency, the numbers and the period out of that string correctly.
    sal = it.get("salary")
    if isinstance(sal, dict):
        it["salary"] = _salary_text(sal)
    company = it.get("companyDetails") or {}
    if not it.get("companyName") and company.get("name"):
        it["companyName"] = company["name"]
    if not it.get("companyEmployeesCount") and company.get("employeeRange"):
        it["companyEmployeesCount"] = company["employeeRange"]
    if not it.get("industries") and company.get("industry"):
        it["industries"] = company["industry"]
    if not it.get("postedAt") and it.get("pubDate"):
        it["postedAt"] = it["pubDate"]
    if not it.get("applyUrl") and it.get("viewJobLink", "").startswith("/"):
        domain = _COUNTRY_DOMAIN.get(it.get("_country") or "in", "in.indeed.com")
        it["applyUrl"] = f"https://{domain}{it['viewJobLink']}"
    return it


def _cfg() -> dict:
    return scoring.load_config().get("indeed", {})


# ---------------------------------------------------------------- search specs

def build_search_specs() -> list[dict]:
    """Every query x every location, as Indeed actor inputs (one run each)."""
    cfg = _cfg()
    days = cfg.get("posted_within_days")
    specs = []
    for q in cfg.get("queries", []):
        for loc in cfg.get("locations", []):
            spec = {"country": loc.get("country", "in"), "query": q["query"], "location": loc["name"]}
            if days:
                spec["postedWithinDays"] = str(days)
            specs.append(spec)
    return specs


# ---------------------------------------------------------------- Apify (throttled fan out / fan in)

# This Apify account allows a handful of concurrent Actor runs across ALL actors, shared with
# Naukri (and LinkedIn, though that one only ever uses one run). Keep this low and safe rather
# than reading the account limit - a 402 mid-sync loses no data (whatever already finished is
# still imported) but it's wasted time.
MAX_CONCURRENT = 3


def run_actor(specs: list[dict], token: str, progress=None) -> tuple[list[dict], dict]:
    cfg = _cfg()
    actor = cfg.get("actor", "curious_coder/indeed-scraper")
    cap = int(cfg.get("max_results_per_run", 25))
    queue = list(specs)
    pending: dict[str, dict] = {}
    items: list[dict] = []
    run_metas: list[dict] = []
    deadline = time.time() + int(cfg.get("timeout_minutes", 15)) * 60

    def _launch_more():
        while queue and len(pending) < MAX_CONCURRENT:
            spec = queue.pop(0)
            actor_input = dict(cfg.get("actor_input_extra", {}))
            actor_input.update(spec)
            actor_input["count"] = cap
            try:
                run = li._http("POST", f"{li.API}/acts/{li._actor_path(actor)}/runs",
                               token, actor_input)["data"]
                pending[run["id"]] = spec
            except RuntimeError as exc:
                if "concurrent-runs-limit-exceeded" in str(exc):
                    queue.insert(0, spec)  # retry later, once a slot frees up
                    break
                if progress:
                    progress(f"  failed to start {spec.get('query')} @ {spec.get('location')}: {exc}")

    _launch_more()
    if progress:
        progress(f"Started {len(pending)} of {len(specs)} Indeed searches (max {MAX_CONCURRENT} at a time)")
    while pending or queue:
        for run_id in list(pending):
            info = li._http("GET", f"{li.API}/actor-runs/{run_id}", token)["data"]
            status = info["status"]
            if status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                continue
            spec = pending.pop(run_id)
            label = f"{spec.get('query')} @ {spec.get('location')}"
            if status == "SUCCEEDED":
                ds_items = li.fetch_dataset(info["defaultDatasetId"], token)
                for it in ds_items:
                    it["_country"] = spec.get("country")
                items.extend(ds_items)
                run_metas.append({"run_id": run_id, "dataset_id": info["defaultDatasetId"],
                                  "spec": spec, "count": len(ds_items)})
                if progress:
                    progress(f"  done: {label} -> {len(ds_items)} results")
            elif progress:
                progress(f"  {status.lower()}: {label}")
        _launch_more()
        if (pending or queue) and time.time() > deadline:
            if progress:
                progress(f"Timeout - {len(pending) + len(queue)} search(es) not finished, "
                         f"proceeding with what finished")
            break
        if pending or queue:
            time.sleep(8)
    return items, {"runs": run_metas, "run_count": len(run_metas)}


# ---------------------------------------------------------------- payload (reuses linkedin heuristics)

def to_payload(items: list[dict], agent: str = "Apify Indeed", meta: dict | None = None) -> dict:
    postings, skipped = [], {}
    seen = set()
    for it in items:
        p, why = li.item_to_posting(_normalize_item(it))
        if p is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        p["source"] = "Indeed (Apify)"
        p["notes"] = "Auto-enriched from Indeed - unmet mandatories are keyword-matched, verify before applying."
        key = scoring.dedupe_key(p["company"], p["role_title"])
        if key in seen:
            skipped["duplicate within run"] = skipped.get("duplicate within run", 0) + 1
            continue
        seen.add(key)
        postings.append(p)
    notes = f"Indeed items: {len(items)}. Kept {len(postings)}. Pre-filtered: " + \
            (", ".join(f"{k} x{v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none")
    if meta:
        notes += f". {meta.get('run_count', 0)} Apify runs"
    return {
        "run": {"run_date": date.today().isoformat(), "agent": agent,
                "sources_checked": ["Indeed (Apify)"],
                "sources_empty": [] if items else ["Indeed (Apify)"],
                "scanned": len(items), "passed_filter": len(postings), "notes": notes},
        "postings": postings,
        "keywords": li.aggregate_keywords(postings),
    }


# ---------------------------------------------------------------- background sync state

STATE: dict[str, Any] = {"running": False, "log": [], "result": None, "error": None,
                         "started": None, "finished": None}
_LOCK = threading.Lock()


def _log(msg: str) -> None:
    STATE["log"].append(f"{datetime.now():%H:%M:%S} {msg}")
    STATE["log"] = STATE["log"][-50:]


def start_sync(ingest_fn) -> bool:
    with _LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, log=[], result=None, error=None,
                     started=datetime.now().isoformat(timespec="seconds"), finished=None)
    threading.Thread(target=_sync_worker, args=(ingest_fn,), daemon=True).start()
    return True


def _sync_worker(ingest_fn) -> None:
    try:
        token = li.get_token()
        if not token:
            raise RuntimeError("No APIFY_TOKEN found. Put APIFY_TOKEN=... in the .env file next to config.json.")
        specs = build_search_specs()
        _log(f"Running {len(specs)} Indeed searches, cap {_cfg().get('max_results_per_run', 25)} results each")
        items, meta = run_actor(specs, token, progress=_log)
        _log(f"Received {len(items)} items; enriching and scoring")
        STATE["result"] = ingest_fn(to_payload(items, meta=meta))
        _log(f"Done: {STATE['result']['added']} added, {STATE['result']['duplicates']} already tracked, "
             f"{STATE['result']['auto_rejected']} auto-rejected")
    except Exception as exc:  # noqa: BLE001
        STATE["error"] = str(exc)
        _log(f"ERROR {exc}")
    finally:
        STATE["running"] = False
        STATE["finished"] = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------- CLI

def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Sync Indeed jobs via Apify into the tracker")
    ap.add_argument("--dry-run", action="store_true", help="print search specs and exit")
    ap.add_argument("--file", help="import a local JSON file of raw Apify items")
    a = ap.parse_args()
    scoring.load_config(force=True)
    if a.dry_run:
        for s in build_search_specs():
            print(json.dumps(s))
        return
    from . import db
    from .main import ingest
    db.init_db()
    if a.file:
        items = json.loads(Path(a.file).read_text(encoding="utf-8"))
        print(json.dumps(ingest(to_payload(items, agent="Apify Indeed (file)")), indent=2, default=str)[:3000])
        return
    token = li.get_token()
    if not token:
        raise SystemExit("No APIFY_TOKEN. Add APIFY_TOKEN=... to .env")
    items, meta = run_actor(build_search_specs(), token, progress=print)
    res = ingest(to_payload(items, meta=meta))
    print(f"added {res['added']} · duplicates {res['duplicates']} · auto-rejected {res['auto_rejected']}")
    for t in res["top"]:
        print(f"  {t['final_score']:>5}  {t['band']:<13} {t['company']} - {t['role_title']}")


if __name__ == "__main__":
    _main()
