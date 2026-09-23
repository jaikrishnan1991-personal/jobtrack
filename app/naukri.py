"""Naukri discovery via Apify.

Runs muhammetakkurtt/naukri-job-scraper on Apify across every keyword x city in
config.json -> naukri: Naukri.com (India) searches are built as direct search-result URLs
(the actor accepts a searchUrl override, same trick linkedin.py uses for LinkedIn guest
search), and NaukriGulf (UAE/Oman/etc.) searches use the actor's own jobBoard + location
fields since Gulf city codes aren't slug-based. One query+location per run, fanned out and
polled together like indeed.py.

Reuses linkedin.py for everything actor-agnostic - see indeed.py's docstring for why that
is safe (the heuristics operate on generic text, not LinkedIn's schema).

Token: shared APIFY_TOKEN (see linkedin.get_token).

CLI (for cron / Task Scheduler):
    python -m app.naukri              # run configured searches, import results
    python -m app.naukri --dry-run    # print the search specs and exit
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import linkedin as li
from . import scoring

ROOT = Path(__file__).resolve().parent.parent


def _cfg() -> dict:
    return scoring.load_config().get("naukri", {})


def _slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", s.lower())).strip("-")


# ---------------------------------------------------------------- search specs

def build_search_specs() -> list[dict]:
    """Every keyword x every India city (as a naukri.com search URL) plus keyword x Gulf
    location (as jobBoard=naukrigulf + location) - one Apify actor input per run."""
    cfg = _cfg()
    specs = []
    for q in cfg.get("queries", []):
        kw_slug = _slug(q["keyword"])
        for city in cfg.get("cities", []):
            url = f"https://www.naukri.com/{kw_slug}-jobs-in-{_slug(city)}"
            specs.append({"jobBoard": "naukri", "searchUrl": url, "sortBy": "date"})
        for gulf_loc in cfg.get("gulf_locations", []):
            specs.append({"jobBoard": "naukrigulf", "keyword": q["keyword"], "location": gulf_loc})
    return specs


# ---------------------------------------------------------------- Apify (throttled fan out / fan in)

# Shared Apify-account concurrency ceiling - see indeed.py's MAX_CONCURRENT for why.
MAX_CONCURRENT = 3
MIN_MAX_JOBS = 50  # this actor rejects maxJobs < 50


def run_actor(specs: list[dict], token: str, progress=None) -> tuple[list[dict], dict]:
    cfg = _cfg()
    actor = cfg.get("actor", "muhammetakkurtt/naukri-job-scraper")
    cap = max(MIN_MAX_JOBS, int(cfg.get("max_results_per_run", MIN_MAX_JOBS)))
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
            actor_input["maxJobs"] = cap
            try:
                run = li._http("POST", f"{li.API}/acts/{li._actor_path(actor)}/runs",
                               token, actor_input)["data"]
                pending[run["id"]] = spec
            except RuntimeError as exc:
                if "concurrent-runs-limit-exceeded" in str(exc):
                    queue.insert(0, spec)
                    break
                label = spec.get("searchUrl") or f"{spec.get('keyword')} @ {spec.get('location')}"
                if progress:
                    progress(f"  failed to start {label}: {exc}")

    _launch_more()
    if progress:
        progress(f"Started {len(pending)} of {len(specs)} Naukri searches (max {MAX_CONCURRENT} at a time)")
    while pending or queue:
        for run_id in list(pending):
            info = li._http("GET", f"{li.API}/actor-runs/{run_id}", token)["data"]
            status = info["status"]
            if status not in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
                continue
            spec = pending.pop(run_id)
            label = spec.get("searchUrl") or f"{spec.get('keyword')} @ {spec.get('location')}"
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

def to_payload(items: list[dict], agent: str = "Apify Naukri", meta: dict | None = None) -> dict:
    postings, skipped = [], {}
    seen = set()
    for it in items:
        p, why = li.item_to_posting(it)
        if p is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        p["source"] = "Naukri (Apify)"
        p["notes"] = "Auto-enriched from Naukri - unmet mandatories are keyword-matched, verify before applying."
        key = scoring.dedupe_key(p["company"], p["role_title"])
        if key in seen:
            skipped["duplicate within run"] = skipped.get("duplicate within run", 0) + 1
            continue
        seen.add(key)
        postings.append(p)
    notes = f"Naukri items: {len(items)}. Kept {len(postings)}. Pre-filtered: " + \
            (", ".join(f"{k} x{v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none")
    if meta:
        notes += f". {meta.get('run_count', 0)} Apify runs"
    return {
        "run": {"run_date": date.today().isoformat(), "agent": agent,
                "sources_checked": ["Naukri (Apify)"],
                "sources_empty": [] if items else ["Naukri (Apify)"],
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
        _log(f"Running {len(specs)} Naukri searches, cap {_cfg().get('max_results_per_run', 25)} results each")
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
    ap = argparse.ArgumentParser(description="Sync Naukri jobs via Apify into the tracker")
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
        print(json.dumps(ingest(to_payload(items, agent="Apify Naukri (file)")), indent=2, default=str)[:3000])
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
