"""Instahyre discovery via Apify.

Runs getascraper/instahyre-jobs-scraper on Apify - a curated India tech job board. Unlike
Indeed/Naukri/Wellfound, this actor's keywords and locations are both arrays it searches in
one call, so a sync here is a single Apify run, not a fan-out.

Reuses linkedin.py for everything actor-agnostic - see indeed.py's docstring for why that is
safe. This actor's raw fields are close to what linkedin.item_to_posting() already expects
(title, companyName, description all match directly); the only mismatch is the location
(array `locations` vs a flat string - the actor also gives `locationsRaw`, a string, which we
map across) and the apply URL (`publicUrl` instead of one of the usual alias keys).

Token: shared APIFY_TOKEN (see linkedin.get_token).

CLI (for cron / Task Scheduler):
    python -m app.instahyre              # run configured search, import results
    python -m app.instahyre --dry-run    # print the actor input and exit
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


def _cfg() -> dict:
    return scoring.load_config().get("instahyre", {})


def _normalize_item(item: dict) -> dict:
    it = dict(item)
    if not it.get("location") and it.get("locationsRaw"):
        it["location"] = it["locationsRaw"]
    if not it.get("applyUrl") and it.get("publicUrl"):
        it["applyUrl"] = it["publicUrl"]
    return it


# ---------------------------------------------------------------- search spec (single call)

def build_actor_input() -> dict:
    cfg = _cfg()
    return {
        "keywords": [q for q in cfg.get("keywords", [])],
        "keywordMatch": "any",
        "locations": [l for l in cfg.get("locations", [])],
        "includeDetails": True,
        "maxItems": int(cfg.get("max_results_per_run", 150)),
    }


# ---------------------------------------------------------------- Apify

def run_actor(token: str, progress=None) -> tuple[list[dict], dict]:
    cfg = _cfg()
    actor = cfg.get("actor", "getascraper/instahyre-jobs-scraper")
    actor_input = dict(cfg.get("actor_input_extra", {}))
    actor_input.update(build_actor_input())
    run = li._http("POST", f"{li.API}/acts/{li._actor_path(actor)}/runs", token, actor_input)["data"]
    run_id = run["id"]
    if progress:
        progress(f"Apify run {run_id} started")
    deadline = time.time() + int(cfg.get("timeout_minutes", 10)) * 60
    while True:
        info = li._http("GET", f"{li.API}/actor-runs/{run_id}", token)["data"]
        status = info["status"]
        if progress:
            progress(f"Apify run {status.lower()}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        if time.time() > deadline:
            raise RuntimeError(f"Apify run {run_id} still {status} after timeout; "
                               f"dataset id {info.get('defaultDatasetId')}")
        time.sleep(6)
    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify run {run_id} ended {status}")
    items = li.fetch_dataset(info["defaultDatasetId"], token)
    return items, {"run_id": run_id, "dataset_id": info["defaultDatasetId"]}


# ---------------------------------------------------------------- payload (reuses linkedin heuristics)

def to_payload(items: list[dict], agent: str = "Apify Instahyre", meta: dict | None = None) -> dict:
    postings, skipped = [], {}
    seen = set()
    for it in items:
        p, why = li.item_to_posting(_normalize_item(it))
        if p is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        p["source"] = "Instahyre (Apify)"
        p["notes"] = "Auto-enriched from Instahyre - unmet mandatories are keyword-matched, verify before applying."
        key = scoring.dedupe_key(p["company"], p["role_title"])
        if key in seen:
            skipped["duplicate within run"] = skipped.get("duplicate within run", 0) + 1
            continue
        seen.add(key)
        postings.append(p)
    notes = f"Instahyre items: {len(items)}. Kept {len(postings)}. Pre-filtered: " + \
            (", ".join(f"{k} x{v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none")
    if meta:
        notes += f". Apify run {meta.get('run_id')}, dataset {meta.get('dataset_id')}"
    return {
        "run": {"run_date": date.today().isoformat(), "agent": agent,
                "sources_checked": ["Instahyre (Apify)"],
                "sources_empty": [] if items else ["Instahyre (Apify)"],
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
        _log("Running Instahyre search")
        items, meta = run_actor(token, progress=_log)
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
    ap = argparse.ArgumentParser(description="Sync Instahyre jobs via Apify into the tracker")
    ap.add_argument("--dry-run", action="store_true", help="print the actor input and exit")
    ap.add_argument("--file", help="import a local JSON file of raw Apify items")
    a = ap.parse_args()
    scoring.load_config(force=True)
    if a.dry_run:
        print(json.dumps(build_actor_input(), indent=2))
        return
    from . import db
    from .main import ingest
    db.init_db()
    if a.file:
        items = json.loads(Path(a.file).read_text(encoding="utf-8"))
        print(json.dumps(ingest(to_payload(items, agent="Apify Instahyre (file)")), indent=2, default=str)[:3000])
        return
    token = li.get_token()
    if not token:
        raise SystemExit("No APIFY_TOKEN. Add APIFY_TOKEN=... to .env")
    items, meta = run_actor(token, progress=print)
    res = ingest(to_payload(items, meta=meta))
    print(f"added {res['added']} · duplicates {res['duplicates']} · auto-rejected {res['auto_rejected']}")
    for t in res["top"]:
        print(f"  {t['final_score']:>5}  {t['band']:<13} {t['company']} - {t['role_title']}")


if __name__ == "__main__":
    _main()
