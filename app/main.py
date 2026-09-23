"""JG Job Tracker - local FastAPI + SQLite app.

Run:  python -m uvicorn app.main:app --reload --port 8765
Open: http://127.0.0.1:8765
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from . import (cover_letter, db, indeed, instahyre, linkedin, naukri, outreach, resume,
               scoring, wellfound)

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="JG Job Tracker", version="1.0.0")

STATUSES = ["Not Applied", "Applied", "Recruiter Screen", "Interviewing",
            "Offer", "Rejected", "Withdrawn", "Ghosted", "Do Not Apply"]

# schema.org JobPosting -> our field names, so scraped structured data maps cleanly
ALIASES = {
    "title": "role_title", "jobTitle": "role_title", "role": "role_title",
    "hiringOrganization": "company", "organization": "company", "employer": "company",
    "datePosted": "date_posted", "posted": "date_posted",
    "jobLocation": "location", "city": "location",
    "url": "apply_url", "link": "apply_url", "application_link": "apply_url",
    "employmentType": "work_mode", "jobLocationType": "work_mode",
    "description": "description", "jd": "description",
    "experienceRequirements": "experience_asked",
    "baseSalary": "comp_raw", "salary": "comp_raw", "compensation": "comp_raw",
}

JOB_FIELDS = [
    "job_id", "dedupe_key", "date_found", "date_posted", "date_confidence",
    "company", "role_title", "archetype", "archetype_match", "location", "location_tier", "work_mode",
    "experience_asked", "exp_min", "exp_max", "comp_raw", "comp_currency",
    "comp_min", "comp_max", "comp_confidence", "company_stage", "domains",
    "unmet_mandatories", "missing_keywords", "description", "s_archetype",
    "s_domain", "s_seniority", "s_gaps", "s_stage", "s_comp", "s_location",
    "fit_score", "location_multiplier", "final_score", "band", "why_this_score",
    "apply_url", "source", "status", "date_applied", "referral_path", "notes",
    "run_id", "rejected_reason",
]


@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    scoring.load_config(force=True)
    _seed_if_empty()
    _mark_config_favourites()
    _load_favourites()
    if scoring.load_config().get("favourite_weight") and _needs_affinity_backfill():
        rescore_all()


def _needs_affinity_backfill() -> bool:
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM jobs WHERE s_affinity IS NULL").fetchone()["n"]
    c.close()
    return n > 0


def _seed_if_empty() -> None:
    """First run: load the verified postings shipped in imports/ so the Apply list is not empty."""
    if not scoring.load_config().get("seed_on_first_run", True):
        return
    c = conn()
    n = c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
    c.close()
    if n:
        return
    for f in sorted((ROOT / "imports").glob("*.json")):
        try:
            ingest(json.loads(f.read_text(encoding="utf-8")))
        except Exception as exc:  # noqa: BLE001
            print(f"seed {f.name} failed: {exc}")


def conn():
    return db.connect()


# ------------------------------------------------------------------ helpers

def _norm_posting(raw: dict) -> dict:
    p: dict[str, Any] = {}
    for k, v in raw.items():
        key = ALIASES.get(k, k)
        if isinstance(v, dict):
            v = v.get("name") or v.get("value") or v.get("address") or json.dumps(v)
        p[key] = v
    for k in ("domains", "unmet_mandatories", "missing_keywords"):
        val = p.get(k)
        if isinstance(val, str):
            p[k] = [x.strip() for x in val.split(",") if x.strip()]
        elif val is None:
            p[k] = []
    p["company"] = (p.get("company") or "").strip()
    p["role_title"] = (p.get("role_title") or "").strip()
    if p.get("experience_asked") and p.get("exp_min") is None:
        nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", str(p["experience_asked"]))]
        if nums:
            p["exp_min"] = nums[0]
            p["exp_max"] = nums[1] if len(nums) > 1 else None
    return p


def _next_job_id(c, run_date: str) -> str:
    stamp = run_date.replace("-", "")
    row = c.execute(
        "SELECT MAX(CAST(substr(job_id,10) AS INTEGER)) m FROM jobs WHERE job_id LIKE ?",
        (f"{stamp}-%",)).fetchone()
    return f"{stamp}-{(row['m'] or 0) + 1:03d}"


# ------------------------------------------------------------------ import

@app.post("/api/import")
async def import_payload(request: Request):
    """The one endpoint the research agent writes to. Idempotent by dedupe_key."""
    payload = await request.json()
    if linkedin.looks_like_apify(payload):
        payload = linkedin.to_payload(payload, agent="Apify (pasted)")
    return ingest(payload)


def ingest(payload) -> dict:
    """Pure import - used by the HTTP endpoint, the LinkedIn sync and the CLI."""
    if isinstance(payload, list):
        payload = {"postings": payload}

    run = payload.get("run") or {}
    run_date = run.get("run_date") or date.today().isoformat()
    postings = payload.get("postings") or payload.get("jobs") or []

    c = conn()
    cur = c.cursor()
    cur.execute(
        """INSERT INTO runs (run_date, agent, sources_checked, sources_empty,
                             scanned, passed_filter, notes)
           VALUES (?,?,?,?,?,?,?)""",
        (run_date, run.get("agent"), json.dumps(run.get("sources_checked") or []),
         json.dumps(run.get("sources_empty") or []), run.get("scanned") or len(postings),
         run.get("passed_filter") or len(postings), run.get("notes")))
    run_id = cur.lastrowid

    added, dupes, rejected, errors = [], [], [], []
    for raw in postings:
        try:
            p = _norm_posting(raw)
            if not p["company"] or not p["role_title"]:
                errors.append({"posting": raw, "error": "missing company or role_title"})
                continue
            key = scoring.dedupe_key(p["company"], p["role_title"])
            existing = cur.execute(
                "SELECT id, job_id, status FROM jobs WHERE dedupe_key=?", (key,)).fetchone()
            if existing:
                dupes.append({"job_id": existing["job_id"], "company": p["company"],
                              "role_title": p["role_title"], "status": existing["status"]})
                if p.get("apply_url"):
                    cur.execute("UPDATE jobs SET apply_url=COALESCE(NULLIF(apply_url,''),?),"
                                " updated_at=datetime('now') WHERE id=?",
                                (p["apply_url"], existing["id"]))
                continue

            sc = scoring.evaluate(p, ref_date=run_date)
            row = {
                "job_id": _next_job_id(c, run_date),
                "dedupe_key": key,
                "date_found": run_date,
                "date_posted": p.get("date_posted"),
                "date_confidence": p.get("date_confidence") or "Unverified",
                "company": p["company"], "role_title": p["role_title"],
                "archetype": p.get("archetype"), "archetype_match": p.get("archetype_match"),
                "location": p.get("location"),
                "location_tier": sc["location_tier"], "work_mode": p.get("work_mode"),
                "experience_asked": p.get("experience_asked"),
                "exp_min": p.get("exp_min"), "exp_max": p.get("exp_max"),
                "comp_raw": p.get("comp_raw"), "comp_currency": (p.get("comp_currency") or "").upper() or None,
                "comp_min": p.get("comp_min"), "comp_max": p.get("comp_max"),
                "comp_confidence": p.get("comp_confidence") or "Not stated",
                "company_stage": p.get("company_stage"),
                "domains": json.dumps(p.get("domains") or []),
                "unmet_mandatories": json.dumps(p.get("unmet_mandatories") or []),
                "missing_keywords": json.dumps(p.get("missing_keywords") or []),
                "description": p.get("description"),
                "s_archetype": sc["s_archetype"], "s_domain": sc["s_domain"],
                "s_seniority": sc["s_seniority"], "s_gaps": sc["s_gaps"],
                "s_stage": sc["s_stage"], "s_comp": sc["s_comp"], "s_location": sc["s_location"],
                "s_affinity": sc["s_affinity"], "affinity": sc["affinity"],
                "fit_score": sc["fit_score"], "location_multiplier": sc["location_multiplier"],
                "final_score": sc["final_score"], "band": sc["band"],
                "why_this_score": sc["why_this_score"],
                "apply_url": p.get("apply_url"), "source": p.get("source"),
                "status": "Not Applied", "date_applied": None,
                "referral_path": p.get("referral_path"), "notes": p.get("notes"),
                "run_id": run_id, "rejected_reason": sc["rejected_reason"],
            }
            cols = ",".join(row.keys())
            qs = ",".join("?" * len(row))
            cur.execute(f"INSERT INTO jobs ({cols}) VALUES ({qs})", list(row.values()))
            rec = {"job_id": row["job_id"], "company": row["company"],
                   "role_title": row["role_title"], "final_score": row["final_score"],
                   "band": row["band"], "rejected_reason": row["rejected_reason"]}
            (rejected if row["rejected_reason"] else added).append(rec)
        except Exception as exc:  # noqa: BLE001 - one bad posting must not kill the run
            errors.append({"posting": raw.get("role_title", "?"), "error": str(exc)})

    # keywords
    kw_payload = payload.get("keywords") or {}
    kw_rows = kw_payload.get("keywords", []) if isinstance(kw_payload, dict) else kw_payload
    gaps = kw_payload.get("resume_gaps", []) if isinstance(kw_payload, dict) else []
    resume_kw = {k.lower() for k in scoring.load_config()["profile"]["resume_keywords"]}
    for k in list(kw_rows) + list(gaps):
        term = (k.get("term") or "").strip()
        if not term:
            continue
        on_resume = k.get("on_my_resume")
        if on_resume is None:
            on_resume = term.lower() in resume_kw
        cur.execute(
            """INSERT INTO keywords (run_id, run_date, term, category, frequency,
                 pct_postings, criticality, archetypes, can_i_claim_it, on_my_resume, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id, run_date, term, k.get("category"), k.get("frequency") or 0,
             k.get("percent_of_postings"), k.get("criticality"),
             json.dumps(k.get("archetypes") or []), k.get("can_i_claim_it"),
             int(bool(on_resume)), k.get("note")))

    # target companies
    for t in payload.get("target_companies") or []:
        cur.execute(
            """INSERT INTO target_companies (company, why, careers_url, region,
                   last_checked, open_roles_found, notes)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(company) DO UPDATE SET
                   last_checked=excluded.last_checked,
                   open_roles_found=excluded.open_roles_found,
                   careers_url=COALESCE(excluded.careers_url, careers_url),
                   notes=COALESCE(excluded.notes, notes)""",
            (t.get("company"), t.get("why"), t.get("careers_url"), t.get("region"),
             t.get("last_checked") or run_date, t.get("open_roles_found") or 0, t.get("notes")))

    extra = {k: payload.get(k) for k in ("linkedin_searches", "naukri_searches") if payload.get(k)}
    if extra:
        cur.execute("UPDATE runs SET notes = COALESCE(notes,'') || ? WHERE id=?",
                    ("\n" + json.dumps(extra, indent=2), run_id))

    cur.execute("""UPDATE runs SET added=?, duplicates=?, rejected=? WHERE id=?""",
                (len(added), len(dupes), len(rejected), run_id))
    c.commit()
    c.close()

    added.sort(key=lambda r: r["final_score"] or 0, reverse=True)
    return {
        "run_id": run_id, "run_date": run_date,
        "added": len(added), "duplicates": len(dupes),
        "auto_rejected": len(rejected), "errors": errors,
        "top": added[:5], "rejected_detail": rejected, "duplicate_detail": dupes,
    }


# ------------------------------------------------------------------ jobs

@app.get("/api/jobs")
def list_jobs(status: str | None = None, band: str | None = None,
              archetype: str | None = None, q: str | None = None,
              include_rejected: bool = False, min_score: float = 0,
              sort: str = "final_score", limit: int = 500):
    c = conn()
    sql = "SELECT * FROM jobs WHERE 1=1"
    args: list[Any] = []
    if not include_rejected:
        sql += " AND (rejected_reason IS NULL OR rejected_reason='')"
    if status:
        sql += " AND status=?"; args.append(status)
    if band:
        sql += " AND band=?"; args.append(band)
    if archetype:
        sql += " AND archetype=?"; args.append(archetype)
    if min_score:
        sql += " AND final_score>=?"; args.append(min_score)
    if q:
        sql += (" AND id IN (SELECT rowid FROM jobs_fts WHERE jobs_fts MATCH ?)")
        args.append(q.replace('"', ' '))
    sort_col = sort if sort in ("final_score", "fit_score", "date_found", "date_posted",
                                "company", "band", "status") else "final_score"
    sql += f" ORDER BY {sort_col} DESC LIMIT ?"
    args.append(limit)
    rows = [db.row_to_dict(r) for r in c.execute(sql, args)]
    c.close()
    return {"count": len(rows), "jobs": rows}


@app.get("/api/jobs/{job_db_id}")
def get_job(job_db_id: int):
    c = conn()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (job_db_id,)).fetchone()
    hist = [dict(h) for h in c.execute(
        "SELECT * FROM status_history WHERE job_db_id=? ORDER BY id", (job_db_id,))]
    c.close()
    if not r:
        raise HTTPException(404, "not found")
    d = db.row_to_dict(r)
    d["history"] = hist
    return d


EDITABLE = {"status", "date_applied", "notes", "referral_path", "apply_url",
            "archetype", "company_stage", "comp_currency", "comp_min", "comp_max",
            "comp_confidence", "location", "work_mode", "date_posted",
            "date_confidence", "rejected_reason", "why_this_score"}


@app.patch("/api/jobs/{job_db_id}")
async def update_job(job_db_id: int, request: Request):
    body = await request.json()
    c = conn()
    old = c.execute("SELECT * FROM jobs WHERE id=?", (job_db_id,)).fetchone()
    if not old:
        raise HTTPException(404, "not found")
    fields = {k: v for k, v in body.items() if k in EDITABLE}
    if body.get("status") == "Applied" and not old["date_applied"] and "date_applied" not in fields:
        fields["date_applied"] = date.today().isoformat()
    if fields:
        sets = ",".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE jobs SET {sets}, updated_at=datetime('now') WHERE id=?",
                  [*fields.values(), job_db_id])
    if "status" in fields and fields["status"] != old["status"]:
        c.execute("INSERT INTO status_history (job_db_id, old_status, new_status) VALUES (?,?,?)",
                  (job_db_id, old["status"], fields["status"]))
    c.commit()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (job_db_id,)).fetchone()
    c.close()
    return db.row_to_dict(r)


@app.post("/api/jobs")
async def add_job(request: Request):
    """Manual add - e.g. an inbound recruiter approach. Same shape as an import posting."""
    body = await request.json()
    body.setdefault("source", "Inbound")
    return ingest({"postings": [body], "run": {"agent": "manual", "scanned": 1}})


@app.delete("/api/jobs/{job_db_id}")
def delete_job(job_db_id: int):
    c = conn()
    c.execute("DELETE FROM jobs WHERE id=?", (job_db_id,))
    c.commit(); c.close()
    return {"deleted": job_db_id}


@app.post("/api/rescore")
def rescore_all():
    """Re-apply the rubric to every stored posting after editing config.json or starring a role."""
    scoring.load_config(force=True)
    _load_favourites()
    c = conn()
    rows = c.execute("SELECT * FROM jobs").fetchall()
    n = 0
    for r in rows:
        d = db.row_to_dict(r)
        sc = scoring.evaluate(d, check_freshness=False)
        reject = sc["rejected_reason"]
        if not reject and (r["rejected_reason"] or "").startswith("Posting is"):
            reject = r["rejected_reason"]      # freshness is judged at discovery time - keep it
        c.execute("""UPDATE jobs SET location_tier=?, location_multiplier=?,
                     s_archetype=?, s_domain=?, s_seniority=?, s_gaps=?, s_stage=?,
                     s_comp=?, s_location=?, s_affinity=?, affinity=?, fit_score=?, final_score=?, band=?,
                     rejected_reason=?, updated_at=datetime('now') WHERE id=?""",
                  (sc["location_tier"], sc["location_multiplier"], sc["s_archetype"],
                   sc["s_domain"], sc["s_seniority"], sc["s_gaps"], sc["s_stage"],
                   sc["s_comp"], sc["s_location"], sc["s_affinity"], sc["affinity"],
                   sc["fit_score"], sc["final_score"], sc["band"], reject, r["id"]))
        n += 1
    c.commit(); c.close()
    return {"rescored": n}


# ------------------------------------------------------------------ favourites

def _load_favourites() -> None:
    c = conn()
    rows = [db.row_to_dict(r) for r in c.execute(
        "SELECT company, role_title, favourite_terms FROM jobs WHERE favourite=1")]
    c.close()
    favs = []
    for r in rows:
        terms = r.get("favourite_terms")
        terms = json.loads(terms) if isinstance(terms, str) and terms else (terms or [])
        favs.append({"company": r["company"], "role_title": r["role_title"], "key_terms": terms})
    scoring.set_dynamic_favourites(favs)


def _mark_config_favourites() -> None:
    """Flag rows that match config favourite_roles so the star shows in the UI."""
    c = conn()
    for f in scoring.load_config().get("favourite_roles", []):
        key = scoring.dedupe_key(f.get("company", ""), f.get("role_title", ""))
        c.execute("UPDATE jobs SET favourite=1, favourite_terms=COALESCE(favourite_terms, ?) WHERE dedupe_key=?",
                  (json.dumps(f.get("key_terms", [])), key))
    c.commit(); c.close()


@app.post("/api/jobs/{job_db_id}/favourite")
async def toggle_favourite(job_db_id: int, request: Request):
    """Star/unstar a role as 'this is the kind of job I want'. Re-scores everything."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    c = conn()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (job_db_id,)).fetchone()
    if not r:
        raise HTTPException(404, "not found")
    on = body.get("favourite", not bool(r["favourite"]))
    terms = scoring.extract_terms(db.row_to_dict(r)) if on else None
    c.execute("UPDATE jobs SET favourite=?, favourite_terms=? WHERE id=?",
              (1 if on else 0, json.dumps(terms) if terms else None, job_db_id))
    c.commit(); c.close()
    res = rescore_all()
    return {"favourite": bool(on), "key_terms": terms, **res}


@app.get("/api/favourites")
def list_favourites():
    _load_favourites()
    return {"favourites": scoring.favourites()}


# ------------------------------------------------------------------ alerts

@app.get("/api/alerts")
def alerts():
    cfg = scoring.load_config()
    c = conn()
    rows = [db.row_to_dict(r) for r in c.execute(
        "SELECT * FROM jobs WHERE status='Applied' AND date_applied IS NOT NULL")]
    c.close()
    today = date.today()
    follow, stale = [], []
    for r in rows:
        try:
            d = datetime.strptime(r["date_applied"][:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        days = (today - d).days
        r["days_since_applied"] = days
        if days >= cfg["stale_days"]:
            stale.append(r)
        elif days >= cfg["followup_days"]:
            follow.append(r)
    follow.sort(key=lambda r: -r["days_since_applied"])
    stale.sort(key=lambda r: -r["days_since_applied"])
    return {"followup_days": cfg["followup_days"], "stale_days": cfg["stale_days"],
            "follow_up": follow, "stale": stale}


# ------------------------------------------------------------------ analytics

FUNNEL_ORDER = ["Not Applied", "Applied", "Recruiter Screen", "Interviewing", "Offer"]


@app.get("/api/analytics/funnel")
def funnel():
    c = conn()
    jobs = [db.row_to_dict(r) for r in c.execute(
        "SELECT * FROM jobs WHERE rejected_reason IS NULL OR rejected_reason=''")]
    hist = [dict(h) for h in c.execute("SELECT * FROM status_history")]
    c.close()

    reached: dict[int, set[str]] = {}
    for h in hist:
        reached.setdefault(h["job_db_id"], set()).add(h["new_status"])
    for j in jobs:
        reached.setdefault(j["id"], set()).add(j["status"])

    def ever(j, *statuses):
        s = reached.get(j["id"], set())
        return any(x in s for x in statuses)

    by_arch: dict[str, dict] = {}
    for j in jobs:
        a = j.get("archetype") or "?"
        b = by_arch.setdefault(a, {"archetype": a, "logged": 0, "applied": 0,
                                   "screen": 0, "interview": 0, "offer": 0, "rejected": 0})
        b["logged"] += 1
        if ever(j, "Applied", "Recruiter Screen", "Interviewing", "Offer", "Rejected", "Ghosted"):
            b["applied"] += 1
        if ever(j, "Recruiter Screen", "Interviewing", "Offer"):
            b["screen"] += 1
        if ever(j, "Interviewing", "Offer"):
            b["interview"] += 1
        if ever(j, "Offer"):
            b["offer"] += 1
        if ever(j, "Rejected"):
            b["rejected"] += 1
    for b in by_arch.values():
        b["screen_rate"] = round(100 * b["screen"] / b["applied"], 1) if b["applied"] else None
        b["interview_rate"] = round(100 * b["interview"] / b["applied"], 1) if b["applied"] else None

    overall = {"logged": len(jobs)}
    for stage, key in (("applied", ("Applied", "Recruiter Screen", "Interviewing", "Offer", "Rejected", "Ghosted")),
                       ("screen", ("Recruiter Screen", "Interviewing", "Offer")),
                       ("interview", ("Interviewing", "Offer")),
                       ("offer", ("Offer",))):
        overall[stage] = sum(1 for j in jobs if ever(j, *key))
    overall["screen_rate"] = round(100 * overall["screen"] / overall["applied"], 1) if overall["applied"] else None

    by_status = {s: 0 for s in STATUSES}
    for j in jobs:
        by_status[j["status"]] = by_status.get(j["status"], 0) + 1

    by_band: dict[str, int] = {}
    for j in jobs:
        by_band[j["band"] or "?"] = by_band.get(j["band"] or "?", 0) + 1

    return {"overall": overall,
            "by_archetype": sorted(by_arch.values(), key=lambda b: -b["logged"]),
            "by_status": by_status, "by_band": by_band,
            "archetype_labels": scoring.load_config()["archetypes"]}


@app.get("/api/analytics/keywords")
def keyword_analysis(run_id: int | None = None):
    c = conn()
    if run_id:
        rows = [dict(r) for r in c.execute("SELECT * FROM keywords WHERE run_id=?", (run_id,))]
    else:
        rows = [dict(r) for r in c.execute("SELECT * FROM keywords")]
    runs = [dict(r) for r in c.execute("SELECT id, run_date, agent FROM runs ORDER BY id DESC")]
    c.close()

    agg: dict[str, dict] = {}
    for r in rows:
        t = r["term"].lower()
        a = agg.setdefault(t, {"term": r["term"], "category": r["category"],
                               "frequency": 0, "criticality": r["criticality"],
                               "can_i_claim_it": r["can_i_claim_it"],
                               "on_my_resume": bool(r["on_my_resume"]),
                               "note": r["note"], "runs": 0, "pct": []})
        a["frequency"] += r["frequency"] or 0
        a["runs"] += 1
        if r["pct_postings"] is not None:
            a["pct"].append(r["pct_postings"])
        if r["criticality"] == "high":
            a["criticality"] = "high"
        if r["can_i_claim_it"]:
            a["can_i_claim_it"] = r["can_i_claim_it"]
    out = []
    for a in agg.values():
        a["avg_pct"] = round(sum(a["pct"]) / len(a["pct"]), 1) if a["pct"] else None
        a.pop("pct")
        out.append(a)
    out.sort(key=lambda a: -a["frequency"])

    unstated = [a for a in out if a["can_i_claim_it"] == "yes_but_unstated"]
    real_gaps = [a for a in out if a["can_i_claim_it"] == "no"]
    covered = [a for a in out if a["can_i_claim_it"] == "yes" or a["on_my_resume"]]
    total_w = sum(a["frequency"] for a in out) or 1
    coverage = round(100 * sum(a["frequency"] for a in covered) / total_w, 1)
    return {"coverage_score": coverage, "all": out, "runs": runs,
            "cv_quick_wins": unstated[:20], "real_gaps": real_gaps[:20],
            "covered": covered[:30]}


# ------------------------------------------------------------------ targets / runs

@app.get("/api/targets")
def list_targets():
    c = conn()
    rows = [dict(r) for r in c.execute("SELECT * FROM target_companies ORDER BY company")]
    c.close()
    return {"count": len(rows), "targets": rows}


@app.post("/api/targets")
async def upsert_target(request: Request):
    body = await request.json()
    items = body if isinstance(body, list) else [body]
    c = conn()
    for t in items:
        c.execute("""INSERT INTO target_companies (company, why, careers_url, region,
                       last_checked, open_roles_found, notes)
                     VALUES (?,?,?,?,?,?,?)
                     ON CONFLICT(company) DO UPDATE SET
                       why=COALESCE(excluded.why, why),
                       careers_url=COALESCE(excluded.careers_url, careers_url),
                       region=COALESCE(excluded.region, region),
                       last_checked=COALESCE(excluded.last_checked, last_checked),
                       open_roles_found=excluded.open_roles_found,
                       notes=COALESCE(excluded.notes, notes)""",
                  (t.get("company"), t.get("why"), t.get("careers_url"), t.get("region"),
                   t.get("last_checked"), t.get("open_roles_found") or 0, t.get("notes")))
    c.commit(); c.close()
    return {"upserted": len(items)}


@app.delete("/api/targets/{target_id}")
def delete_target(target_id: int):
    c = conn(); c.execute("DELETE FROM target_companies WHERE id=?", (target_id,))
    c.commit(); c.close()
    return {"deleted": target_id}


@app.get("/api/runs")
def list_runs():
    c = conn()
    rows = [db.row_to_dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC")]
    c.close()
    return {"runs": rows}


@app.get("/api/config")
def get_config():
    return scoring.load_config()


@app.post("/api/config/reload")
def reload_config():
    scoring.load_config(force=True)
    return {"reloaded": True}


@app.get("/api/meta")
def meta():
    return {"statuses": STATUSES, "archetypes": scoring.load_config()["archetypes"],
            "bands": [b["label"] for b in scoring.load_config()["bands"]]}


# ------------------------------------------------------------------ export

CSV_COLS = ["job_id", "date_found", "date_posted", "date_confidence", "company",
            "role_title", "archetype", "location", "location_tier", "work_mode",
            "experience_asked", "comp_raw", "comp_confidence", "fit_score",
            "final_score", "band", "why_this_score", "missing_keywords",
            "apply_url", "source", "status", "date_applied", "referral_path", "notes"]


@app.get("/api/export.csv")
def export_csv(include_rejected: bool = False):
    c = conn()
    sql = "SELECT * FROM jobs"
    if not include_rejected:
        sql += " WHERE rejected_reason IS NULL OR rejected_reason=''"
    rows = [db.row_to_dict(r) for r in c.execute(sql + " ORDER BY final_score DESC")]
    c.close()
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_COLS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        r = dict(r)
        for k in ("missing_keywords",):
            if isinstance(r.get(k), list):
                r[k] = ", ".join(r[k])
        w.writerow(r)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=jobtracker_{date.today()}.csv"})


@app.get("/api/export.json")
def export_json():
    c = conn()
    data = {
        "exported": datetime.now().isoformat(timespec="seconds"),
        "jobs": [db.row_to_dict(r) for r in c.execute("SELECT * FROM jobs")],
        "keywords": [dict(r) for r in c.execute("SELECT * FROM keywords")],
        "target_companies": [dict(r) for r in c.execute("SELECT * FROM target_companies")],
        "runs": [db.row_to_dict(r) for r in c.execute("SELECT * FROM runs")],
    }
    c.close()
    return JSONResponse(data, headers={
        "Content-Disposition": f"attachment; filename=jobtracker_{date.today()}.json"})


@app.get("/api/agent-brief", response_class=PlainTextResponse)
def agent_brief():
    """Paste this into the deep-research agent so it never re-finds what you already have."""
    c = conn()
    seen = [dict(r) for r in c.execute(
        "SELECT company, role_title, status FROM jobs ORDER BY id DESC LIMIT 400")]
    targets = [dict(r) for r in c.execute("SELECT company, careers_url FROM target_companies")]
    c.close()
    lines = ["# ALREADY IN THE TRACKER - do not return these again",
             f"# generated {date.today().isoformat()}", ""]
    for s in seen:
        lines.append(f"- {s['company']} | {s['role_title']} | {s['status']}")
    _load_favourites()
    lines += ["", "# ROLES I WANT MORE OF (starred) - prioritise postings that resemble these", ""]
    for f in scoring.favourites():
        lines.append(f"- {f.get('company')} | {f.get('role_title')} | key terms: {', '.join(f.get('key_terms', [])[:14])}")
    lines += ["", "# STANDING TARGET COMPANIES - check careers pages every run", ""]
    for t in targets:
        lines.append(f"- {t['company']} | {t.get('careers_url') or 'no url on file'}")
    return "\n".join(lines)


# ------------------------------------------------------------------ LinkedIn (Apify)

@app.get("/api/linkedin/status")
def linkedin_status():
    st = dict(linkedin.STATE)
    st["token_configured"] = bool(linkedin.get_token())
    st["search_count"] = len(linkedin.build_search_urls())
    st["max_results_per_run"] = scoring.load_config().get("linkedin", {}).get("max_results_per_run")
    return st


@app.get("/api/linkedin/searches")
def linkedin_searches():
    return {"urls": linkedin.build_search_urls()}


@app.post("/api/linkedin/sync")
async def linkedin_sync(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty body is fine
        pass
    scoring.load_config(force=True)
    if not linkedin.get_token():
        raise HTTPException(400, "No APIFY_TOKEN. Add APIFY_TOKEN=... to the .env file next to config.json.")
    started = linkedin.start_sync(ingest, dataset_id=(body or {}).get("dataset_id"))
    if not started:
        raise HTTPException(409, "A LinkedIn sync is already running")
    return {"started": True}


# ------------------------------------------------------------------ Indeed (Apify)

@app.get("/api/indeed/status")
def indeed_status():
    st = dict(indeed.STATE)
    st["token_configured"] = bool(linkedin.get_token())
    st["search_count"] = len(indeed.build_search_specs())
    st["max_results_per_run"] = scoring.load_config().get("indeed", {}).get("max_results_per_run")
    return st


@app.get("/api/indeed/searches")
def indeed_searches():
    return {"specs": indeed.build_search_specs()}


@app.post("/api/indeed/sync")
async def indeed_sync():
    scoring.load_config(force=True)
    if not linkedin.get_token():
        raise HTTPException(400, "No APIFY_TOKEN. Add APIFY_TOKEN=... to the .env file next to config.json.")
    started = indeed.start_sync(ingest)
    if not started:
        raise HTTPException(409, "An Indeed sync is already running")
    return {"started": True}


# ------------------------------------------------------------------ Naukri (Apify)

@app.get("/api/naukri/status")
def naukri_status():
    st = dict(naukri.STATE)
    st["token_configured"] = bool(linkedin.get_token())
    st["search_count"] = len(naukri.build_search_specs())
    st["max_results_per_run"] = scoring.load_config().get("naukri", {}).get("max_results_per_run")
    return st


@app.get("/api/naukri/searches")
def naukri_searches():
    return {"specs": naukri.build_search_specs()}


@app.post("/api/naukri/sync")
async def naukri_sync():
    scoring.load_config(force=True)
    if not linkedin.get_token():
        raise HTTPException(400, "No APIFY_TOKEN. Add APIFY_TOKEN=... to the .env file next to config.json.")
    started = naukri.start_sync(ingest)
    if not started:
        raise HTTPException(409, "A Naukri sync is already running")
    return {"started": True}


# ------------------------------------------------------------------ Instahyre (Apify)

@app.get("/api/instahyre/status")
def instahyre_status():
    st = dict(instahyre.STATE)
    st["token_configured"] = bool(linkedin.get_token())
    cfg = scoring.load_config().get("instahyre", {})
    st["search_count"] = len(cfg.get("keywords", [])) * len(cfg.get("locations", []))
    st["max_results_per_run"] = cfg.get("max_results_per_run")
    return st


@app.get("/api/instahyre/searches")
def instahyre_searches():
    return {"input": instahyre.build_actor_input()}


@app.post("/api/instahyre/sync")
async def instahyre_sync():
    scoring.load_config(force=True)
    if not linkedin.get_token():
        raise HTTPException(400, "No APIFY_TOKEN. Add APIFY_TOKEN=... to the .env file next to config.json.")
    started = instahyre.start_sync(ingest)
    if not started:
        raise HTTPException(409, "An Instahyre sync is already running")
    return {"started": True}


# ------------------------------------------------------------------ Wellfound (Apify)

@app.get("/api/wellfound/status")
def wellfound_status():
    st = dict(wellfound.STATE)
    st["token_configured"] = bool(linkedin.get_token())
    st["search_count"] = len(wellfound.build_search_specs())
    st["max_results_per_run"] = scoring.load_config().get("wellfound", {}).get("pages_per_run")
    return st


@app.get("/api/wellfound/searches")
def wellfound_searches():
    return {"specs": wellfound.build_search_specs()}


@app.post("/api/wellfound/sync")
async def wellfound_sync():
    scoring.load_config(force=True)
    if not linkedin.get_token():
        raise HTTPException(400, "No APIFY_TOKEN. Add APIFY_TOKEN=... to the .env file next to config.json.")
    started = wellfound.start_sync(ingest)
    if not started:
        raise HTTPException(409, "A Wellfound sync is already running")
    return {"started": True}


# ------------------------------------------------------------------ apply list + resumes

@app.get("/api/apply-list")
def apply_list(include_stretch: bool = False):
    """Strong + Worth a shot, plus anything that closely resembles a starred role (even if Stretch)."""
    cfg = scoring.load_config()
    min_aff = float(cfg.get("apply_list_min_affinity", 0.6))
    c = conn()
    rows = [db.row_to_dict(r) for r in c.execute(
        "SELECT * FROM jobs WHERE status='Not Applied' AND (rejected_reason IS NULL OR rejected_reason='') "
        "AND band != 'Do not apply' ORDER BY final_score DESC")]
    c.close()
    out = []
    for r in rows:
        like_fav = (r.get("affinity") or 0) >= min_aff
        if r["band"] in ("Strong", "Worth a shot") or like_fav or (include_stretch and r["band"] == "Stretch"):
            r["like_favourite"] = like_fav
            r["resume_exists"] = (resume.OUT_DIR / resume.filename_for(r)).exists()
            r["cover_letter_exists"] = (cover_letter.OUT_DIR / cover_letter.filename_for(r)).exists()
            out.append(r)
    out.sort(key=lambda r: (-(1 if r.get("favourite") else 0), -(r.get("final_score") or 0)))
    return {"count": len(out), "jobs": out, "compiler": resume.compiler()}


def _job(job_db_id: int) -> dict:
    c = conn()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (job_db_id,)).fetchone()
    c.close()
    if not r:
        raise HTTPException(404, "not found")
    return db.row_to_dict(r)


@app.post("/api/jobs/{job_db_id}/resume")
def create_resume(job_db_id: int):
    """Generate (and save to data/resumes/) a LaTeX resume tailored to this posting."""
    out = resume.build(_job(job_db_id))
    out["compiler"] = resume.compiler()
    return out


@app.get("/api/jobs/{job_db_id}/resume.tex")
def resume_tex(job_db_id: int):
    out = resume.build(_job(job_db_id))
    return PlainTextResponse(out["tex"], media_type="application/x-tex",
                             headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'})


@app.get("/api/jobs/{job_db_id}/resume.pdf")
def resume_pdf(job_db_id: int):
    from fastapi.responses import Response
    out = resume.build(_job(job_db_id))
    try:
        pdf = resume.compile_pdf(out["tex"])
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    fname = out["filename"].replace(".tex", ".pdf")
    (resume.OUT_DIR / fname).write_bytes(pdf)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


@app.post("/api/jobs/{job_db_id}/cover-letter")
def create_cover_letter(job_db_id: int):
    """Generate (and save to data/cover_letters/) a LaTeX cover letter tailored to this posting."""
    out = cover_letter.build(_job(job_db_id))
    out["compiler"] = resume.compiler()
    return out


@app.get("/api/jobs/{job_db_id}/cover-letter.tex")
def cover_letter_tex(job_db_id: int):
    out = cover_letter.build(_job(job_db_id))
    return PlainTextResponse(out["tex"], media_type="application/x-tex",
                             headers={"Content-Disposition": f'attachment; filename="{out["filename"]}"'})


@app.get("/api/jobs/{job_db_id}/cover-letter.pdf")
def cover_letter_pdf(job_db_id: int):
    from fastapi.responses import Response
    out = cover_letter.build(_job(job_db_id))
    try:
        pdf = resume.compile_pdf(out["tex"])
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    fname = out["filename"].replace(".tex", ".pdf")
    (cover_letter.OUT_DIR / fname).write_bytes(pdf)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{fname}"'})


# ------------------------------------------------------------------ recruiter outreach

@app.post("/api/outreach/harvest")
def outreach_harvest():
    """Scan stored job descriptions for contact addresses. Free, no Apify call."""
    c = conn()
    try:
        return outreach.harvest_from_descriptions(c)
    finally:
        c.close()


@app.get("/api/outreach/status")
def outreach_status():
    c = conn()
    row = c.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN recruiter_email IS NOT NULL AND recruiter_email<>'' THEN 1 ELSE 0 END) with_email,"
        " SUM(CASE WHEN email_status='sent' THEN 1 ELSE 0 END) sent,"
        " SUM(CASE WHEN email_status='drafted' THEN 1 ELSE 0 END) drafted FROM jobs").fetchone()
    c.close()
    return {"total": row["total"], "with_email": row["with_email"] or 0,
            "sent": row["sent"] or 0, "drafted": row["drafted"] or 0,
            "gmail_configured": bool(outreach.gmail_credentials())}


@app.post("/api/jobs/{job_db_id}/email/preview")
async def email_preview(job_db_id: int, request: Request):
    """Compose without delivering anything. Always call this before draft/send."""
    body = await _json(request)
    job = _job(job_db_id)
    if body.get("to"):
        job["recruiter_email"] = body["to"].strip()
    try:
        draft = outreach.compose(job, attach_cover_letter=bool(body.get("attach_cover_letter")))
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    return {"to": draft["to"], "subject": draft["subject"], "body": draft["body"],
            "attachments": [n for n, _ in draft["attachments"]],
            "gmail_configured": bool(outreach.gmail_credentials()),
            "already": job.get("email_status"), "sent_at": job.get("email_sent_at")}


@app.get("/api/outreach/outbox")
def outreach_outbox():
    """Queued messages waiting for Claude's Gmail connector to deliver them."""
    return {"items": outreach.outbox_items()}


@app.post("/api/jobs/{job_db_id}/email/queue")
async def email_queue(job_db_id: int, request: Request):
    """Compose and park it in data/outbox/ for Claude to send via the Gmail connector.
    Nothing is delivered here - this only writes a file."""
    body = await _json(request)
    job = _job(job_db_id)
    to = (body.get("to") or job.get("recruiter_email") or "").strip()
    if not to:
        raise HTTPException(400, "No recipient address for this posting")
    job["recruiter_email"] = to
    try:
        draft = outreach.compose(job, attach_cover_letter=bool(body.get("attach_cover_letter")))
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    if body.get("subject"):
        draft["subject"] = body["subject"]
    if body.get("body"):
        draft["body"] = body["body"]
    path = outreach.queue(job, draft)
    c = conn()
    with c:
        c.execute("UPDATE jobs SET recruiter_email=?, email_status='queued',"
                  " updated_at=datetime('now') WHERE id=?", (to, job_db_id))
    c.close()
    return {"queued": True, "to": to, "file": path.name}


@app.post("/api/jobs/{job_db_id}/email/draft")
async def email_draft(job_db_id: int, request: Request):
    """Put it in Gmail Drafts. Nothing leaves the account until you press send in Gmail."""
    return await _deliver(job_db_id, request, mode="draft")


@app.post("/api/jobs/{job_db_id}/email/send")
async def email_send(job_db_id: int, request: Request):
    """Send it. The request must carry confirm=true, and won't re-send without force=true."""
    return await _deliver(job_db_id, request, mode="send")


async def _json(request: Request) -> dict:
    try:
        return await request.json() or {}
    except Exception:  # noqa: BLE001 - empty body is fine
        return {}


async def _deliver(job_db_id: int, request: Request, mode: str) -> dict:
    body = await _json(request)
    job = _job(job_db_id)
    to = (body.get("to") or job.get("recruiter_email") or "").strip()
    if not to:
        raise HTTPException(400, "No recipient address for this posting")
    job["recruiter_email"] = to
    if mode == "send":
        if not body.get("confirm"):
            raise HTTPException(400, "Sending needs an explicit confirmation")
        if job.get("email_status") == "sent" and not body.get("force"):
            raise HTTPException(409, f"Already emailed on {job.get('email_sent_at')}. Pass force to send again.")

    try:
        draft = outreach.compose(job, attach_cover_letter=bool(body.get("attach_cover_letter")))
    except RuntimeError as exc:
        raise HTTPException(501, str(exc))
    # let the caller override the text they just reviewed
    if body.get("subject"):
        draft["subject"] = body["subject"]
    if body.get("body"):
        draft["body"] = body["body"]

    try:
        result = (outreach.save_draft(draft) if mode == "draft"
                  else outreach.send(draft, confirm=True))
    except RuntimeError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 - smtplib/imaplib raise their own types
        raise HTTPException(502, f"Gmail refused the message: {exc}")

    c = conn()
    with c:
        c.execute("UPDATE jobs SET recruiter_email=?, email_status=?, email_sent_at=?,"
                  " updated_at=datetime('now') WHERE id=?",
                  (to, "sent" if mode == "send" else "drafted",
                   result.get("at") if mode == "send" else job.get("email_sent_at"), job_db_id))
    c.close()
    return result


# ------------------------------------------------------------------ static

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
