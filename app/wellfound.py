"""Wellfound (AngelList) discovery via Apify.

Runs orgupdate/wellfound-jobs-scraper on Apify - startup-heavy listings, relevant to the
founder/deep-tech archetypes (C/E) more than the other sources. One country+location per run
(no keyword array in this actor's schema), fanned out and throttled exactly like indeed.py -
see that module's docstring for why (shared 5-concurrent-run Apify account cap).

No keyword filter is sent to the actor: its `includeKeyword` field is a single comma-separated
string with unclear AND/OR semantics for a multi-archetype search, so this pulls broadly per
location instead and lets the tracker's own archetype/title classification narrow it down - the
same approach that worked well for Naukri's higher-volume, less-curated results.

Reuses linkedin.py for everything actor-agnostic - see indeed.py's docstring for why that is
safe. This actor's raw fields (job_title, company_name, location, description, salary) mostly
already match linkedin.item_to_posting()'s aliases; the two mismatches are `URL` (capitalised,
so a case-sensitive alias lookup misses it) and `date` (a relative string like "2 days ago",
which the posted-date parser understands once it arrives under a recognised key).

Token: shared APIFY_TOKEN (see linkedin.get_token).

CLI (for cron / Task Scheduler):
    python -m app.wellfound              # run configured searches, import results
    python -m app.wellfound --dry-run    # print the search specs and exit
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

MAX_CONCURRENT = 3  # shared Apify-account concurrency ceiling - see indeed.py


def _cfg() -> dict:
    return scoring.load_config().get("wellfound", {})


def _normalize_item(item: dict) -> dict:
    it = dict(item)
    if not it.get("title") and it.get("job_title"):
        it["title"] = it["job_title"]
    if not it.get("applyUrl") and it.get("URL"):
        it["applyUrl"] = it["URL"]
    if not it.get("postedAt") and it.get("date"):
        it["postedAt"] = it["date"]
    return it


# ---------------------------------------------------------------- search specs

def build_search_specs() -> list[dict]:
    cfg = _cfg()
    return [{"countryName": loc["country"], "locationName": loc["name"],
             "pagesToFetch": int(cfg.get("pages_per_run", 2))}
            for loc in cfg.get("locations", [])]


# ---------------------------------------------------------------- Apify (throttled fan out / fan in)

def run_actor(specs: list[dict], token: str, progress=None) -> tuple[list[dict], dict]:
    cfg = _cfg()
    actor = cfg.get("actor", "orgupdate/wellfound-jobs-scraper")
    queue = list(specs)
    pending: dict[str, dict] = {}
    items: list[dict] = []
    run_metas: list[dict] = []
    deadline = time.time() + int(cfg.get("timeout_minutes", 10)) * 60

    def _launch_more():
        while queue and len(pending) < MAX_CONCURRENT:
            spec = queue.pop(0)
            actor_input = dict(cfg.get("actor_input_extra", {}))
            actor_input.update(spec)
            try:
                run = li._http("POST", f"{li.API}/acts/{li._actor_path(actor)}/runs",
                               token, actor_input)["data"]
                pending[run["id"]] = spec
            except RuntimeError as exc:
                if "concurrent-runs-limit-exceeded" in str(exc):
                    queue.insert(0, spec)
                    break
                if progress:
                    progress(f"  failed to start {spec.get('locationName')}: {exc}")

    _launch_more()
    if progress:
        progress(f"Started {len(pending)} of {len(specs)} Wellfound searches (max {MAX_CONCURRENT} at a time)")
    while pending or queue:
        for run_id in list(pending):
            info = li._http("GET", f"{li.API}/actor-runs/{run_id}", token)["data"]
            status = info["status"]
            if status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                continue
            spec = pending.pop(run_id)
            label = f"{spec.get('locationName')}, {spec.get('countryName')}"
            if status == "SUCCEEDED":
                ds_items = li.fetch_dataset(info["defaultDatasetId"], token)
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

def to_payload(items: list[dict], agent: str = "Apify Wellfound", meta: dict | None = None) -> dict:
    postings, skipped = [], {}
    seen = set()
    for it in items:
        p, why = li.item_to_posting(_normalize_item(it))
        if p is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        p["source"] = "Wellfound (Apify)"
        p["notes"] = "Auto-enriched from Wellfound - unmet mandatories are keyword-matched, verify before applying."
        key = scoring.dedupe_key(p["company"], p["role_title"])
        if key in seen:
            skipped["duplicate within run"] = skipped.get("duplicate within run", 0) + 1
            continue
        seen.add(key)
        postings.append(p)
    notes = f"Wellfound items: {len(items)}. Kept {len(postings)}. Pre-filtered: " + \
            (", ".join(f"{k} x{v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none")
    if meta:
        notes += f". {meta.get('run_count', 0)} Apify runs"
    return {
        "run": {"run_date": date.today().isoformat(), "agent": agent,
                "sources_checked": ["Wellfound (Apify)"],
                "sources_empty": [] if items else ["Wellfound (Apify)"],
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
        _log(f"Running {len(specs)} Wellfound searches")
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
    ap = argparse.ArgumentParser(description="Sync Wellfound jobs via Apify into the tracker")
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
        print(json.dumps(ingest(to_payload(items, agent="Apify Wellfound (file)")), indent=2, default=str)[:3000])
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
