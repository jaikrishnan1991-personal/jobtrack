"""LinkedIn discovery via Apify.

Runs a no-login LinkedIn jobs actor on Apify (default: curious_coder/linkedin-jobs-scraper,
public guest job pages only - your LinkedIn account is never touched), then turns raw items
into tracker postings with heuristic enrichment so the scoring engine can rank them.

Heuristics are deliberately conservative. They find volume; the deep-research prompt is
still the tool for careful requirement analysis on the shortlist.

Token: APIFY_TOKEN environment variable, or a line APIFY_TOKEN=... in <project>/.env.
It is never written to config.json, the database, logs or API responses.

CLI (for cron / Task Scheduler):
    python -m app.linkedin              # run configured searches, import results
    python -m app.linkedin --dry-run    # print the search URLs and exit
    python -m app.linkedin --dataset <id>   # import an existing Apify dataset
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import scoring

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("APIFY_API_BASE", "https://api.apify.com/v2")

# ---------------------------------------------------------------- token

def get_token() -> str | None:
    tok = os.environ.get("APIFY_TOKEN")
    if tok:
        return tok.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("APIFY_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def _cfg() -> dict:
    return scoring.load_config().get("linkedin", {})


# ---------------------------------------------------------------- search URLs

def build_search_urls() -> list[str]:
    """Every query x every location, as LinkedIn guest search URLs."""
    cfg = _cfg()
    seconds = int(cfg.get("posted_within_days", 14)) * 86400
    exp_levels = ",".join(str(x) for x in cfg.get("experience_levels", [4, 5, 6]))
    urls = []
    for q in cfg.get("queries", []):
        for loc in cfg.get("locations", []):
            params = {"keywords": q["keywords"], "location": loc["name"],
                      "f_TPR": f"r{seconds}", "f_E": exp_levels, "sortBy": "DD"}
            if loc.get("remote"):
                params["f_WT"] = "2"
            if loc.get("geoId"):
                params["geoId"] = loc["geoId"]
            urls.append("https://www.linkedin.com/jobs/search/?" + urllib.parse.urlencode(params))
    return urls


# ---------------------------------------------------------------- Apify client (stdlib only)

def _http(method: str, url: str, token: str, body: Any = None, timeout: int = 60) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:400]
        raise RuntimeError(f"Apify HTTP {e.code}: {detail}") from None


def _actor_path(actor: str) -> str:
    return actor.replace("/", "~")


def run_actor(urls: list[str], token: str, progress=None) -> tuple[list[dict], dict]:
    cfg = _cfg()
    actor = cfg.get("actor", "curious_coder/linkedin-jobs-scraper")
    actor_input = dict(cfg.get("actor_input_extra", {}))
    actor_input.update({"urls": urls, "count": int(cfg.get("max_results_per_run", 300))})
    run = _http("POST", f"{API}/acts/{_actor_path(actor)}/runs", token, actor_input)["data"]
    run_id = run["id"]
    if progress:
        progress(f"Apify run {run_id} started")
    deadline = time.time() + int(cfg.get("timeout_minutes", 20)) * 60
    while True:
        info = _http("GET", f"{API}/actor-runs/{run_id}", token)["data"]
        status = info["status"]
        if progress:
            progress(f"Apify run {status.lower()}")
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
        if time.time() > deadline:
            raise RuntimeError(f"Apify run {run_id} still {status} after timeout; "
                               f"import it later with its dataset id {info.get('defaultDatasetId')}")
        time.sleep(10)
    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify run {run_id} ended {status}")
    items = fetch_dataset(info["defaultDatasetId"], token)
    usage = info.get("usageTotalUsd") or info.get("stats", {}).get("computeUnits")
    return items, {"run_id": run_id, "dataset_id": info["defaultDatasetId"], "usage": usage}


def fetch_dataset(dataset_id: str, token: str) -> list[dict]:
    items, offset = [], 0
    while True:
        page = _http("GET", f"{API}/datasets/{dataset_id}/items?clean=true&format=json"
                            f"&offset={offset}&limit=1000", token)
        if not page:
            break
        items.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return items


# ---------------------------------------------------------------- field access (actor-agnostic)

def _first(item: dict, *keys, default=None):
    for k in keys:
        v = item.get(k)
        if v not in (None, "", []):
            return v
    return default


def looks_like_apify(payload: Any) -> bool:
    rows = payload if isinstance(payload, list) else None
    if not rows or not isinstance(rows[0], dict):
        return False
    r = rows[0]
    return ("companyName" in r or "company_name" in r) and ("link" in r or "jobUrl" in r or "url" in r)


# ---------------------------------------------------------------- enrichment heuristics

DOMAIN_PATTERNS = {
    "hardware": r"\bhardware\b|\bpcb\b|\belectronics\b|\bconsumer devices?\b|\bdevices?\b",
    "robotics": r"\brobot(ic|ics|s)?\b|\bautonomous\b|\bdrones?\b|\buav\b|\bros2?\b",
    "iot": r"\biot\b|\bconnected (devices?|products?|home)\b|\bsmart home\b|\bedge devices?\b",
    "embedded": r"\bembedded\b|\bfirmware\b|\bmicrocontroller|\brtos\b|\bstm32\b",
    "ai": r"\b(ai|a\.i\.)\b|\bmachine learning\b|\bdeep learning\b|\bgenai\b|\bgenerative ai\b|\bcomputer vision\b",
    "llm": r"\bllms?\b|\blarge language models?\b|\brag\b",
    "agentic": r"\bagentic\b|\bai agents?\b|\bmcp\b|\bmodel context protocol\b",
    "platform": r"\bplatform\b|\bapis?\b|\bdeveloper (tools|experience|platform)\b|\bsdk\b",
    "appliances": r"\bappliances?\b|\bkitchen\b|\bhvac\b|\bwhite goods\b",
    "industrial": r"\bindustrial\b|\bmanufacturing\b|\bindustry 4\.0\b|\bautomation\b",
    "mechatronics": r"\bmechatronics?\b",
}

ARCHETYPE_TITLE = [
    ("C", r"\b(cto|cpo|chief (technology|product|technical) officer|co-?founder)\b"),
    ("D", r"\b(engineering manager|head of (embedded|firmware|hardware engineering)|"
          r"(embedded|firmware).*(manager|head|director|lead)|agm)\b"),
    ("E", r"\b(programme|program) (director|head|lead)\b|\bhead of programmes?\b|\bincubat|\baccelerator\b"),
    ("B", r"\b(staff|principal|group|lead)\s+(product manager|pm)\b|\bproduct.*\b(ai|ml|platform|agent)"),
    ("A", r"\b(head|director|vp|vice president|avp)\b.*\bproduct\b|\bproduct\b.*\b(head|director|lead)\b|\bcpo\b"),
    # physical / edge AI leads - the Capgemini AI Lab pattern (starred)
    ("D", r"\b(lead|head|manager|architect|principal)\b.*\b(robotics|edge|physical ai|embodied|physical hardware)\b|"
          r"\b(robotics|edge ai|physical ai|embodied ai|edge computing)\b.*\b(lead|head|manager|architect|principal)\b"),
]

TITLE_EXCLUDE = re.compile(
    r"\b(intern|internship|junior|jr\.?|associate product manager|apm|trainee|fresher|"
    r"sales|business development|marketing manager|account manager|recruit|"
    r"hr\b|human resources|customer success|content|copywriter|teacher|tutor)\b", re.I)

SERVICES_INDUSTRIES = re.compile(
    r"it services|consulting|staffing|recruiting|outsourcing|human resources services", re.I)
EXCLUDE_INDUSTRIES = re.compile(
    r"banking|financial services|insurance|capital markets|investment management|"
    r"retail|e-?commerce|advertising services", re.I)

EXP_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*(\d{1,2})\s*\+?\s*)?(?:years|yrs)", re.I)

CURRENCY_SYMBOLS = {"₹": "INR", "$": "USD", "€": "EUR", "S$": "SGD", "SGD": "SGD",
                    "AED": "AED", "OMR": "OMR", "INR": "INR", "USD": "USD", "EUR": "EUR"}


def _norm_text(*parts) -> str:
    return " ".join(str(p) for p in parts if p).lower()


def detect_domains(text: str) -> list[str]:
    return [d for d, pat in DOMAIN_PATTERNS.items() if re.search(pat, text, re.I)]


def classify_archetype(title: str, domains: list[str]) -> tuple[str | None, str]:
    t = title.lower()
    hard = set(domains) & {"hardware", "robotics", "iot", "embedded", "appliances", "industrial"}
    ai = set(domains) & {"ai", "llm", "agentic", "platform"}
    for code, pat in ARCHETYPE_TITLE:
        if re.search(pat, t, re.I):
            if code == "A":
                if hard:
                    return "A", "direct"
                if ai:
                    return "B", "adjacent"
                return "A", "tangential"
            if code == "B":
                # "Group Product Manager - AMR" is hardware product leadership, not an AI-platform PM
                if hard and (not ai or re.search(r"\b(amr|robot|hardware|device|iot)", t)):
                    return "A", "direct"
                return "B", "direct" if ai else "adjacent"
            if code in ("C", "D"):
                return code, "direct" if (hard or ai) else "adjacent"
            return code, "adjacent"
    return None, "tangential"


def parse_experience(text: str) -> tuple[float | None, float | None]:
    best = None
    for m in EXP_RE.finditer(text or ""):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if 3 <= lo <= 25 and (hi is None or lo <= hi <= 30):
            if best is None or lo > best[0]:  # the senior-most stated requirement
                best = (lo, hi)
    return (float(best[0]), float(best[1]) if best and best[1] else None) if best else (None, None)


def _num(tok: str) -> float | None:
    tok = tok.replace(",", "").strip().lower()
    mult = 1
    if tok.endswith("k"):
        mult, tok = 1_000, tok[:-1]
    elif tok.endswith(("l", "lpa", "lakh", "lakhs")):
        mult, tok = 100_000, re.sub(r"(lpa|lakhs?|l)$", "", tok)
    elif tok.endswith(("cr", "crore")):
        mult, tok = 10_000_000, re.sub(r"(crore|cr)$", "", tok)
    elif tok.endswith("m"):
        mult, tok = 1_000_000, tok[:-1]
    try:
        return float(tok) * mult
    except ValueError:
        return None


def parse_salary(raw: Any) -> dict:
    if not raw:
        return {}
    text = " - ".join(raw) if isinstance(raw, list) else str(raw)
    cur = None
    for sym, code in sorted(CURRENCY_SYMBOLS.items(), key=lambda kv: -len(kv[0])):
        if sym in text:
            cur = code
            break
    nums = [n for n in (_num(t) for t in re.findall(
        r"\d[\d,]*(?:\.\d+)?\s*(?:k|lpa|lakhs?|l|cr|crore|m)?\b", text, re.I)) if n]
    if not nums or not cur:
        return {"comp_raw": text}
    period = text.lower()
    factor = 12 if "/mo" in period or "month" in period else (2080 if "/hr" in period or "hour" in period else 1)
    nums = [n * factor for n in nums]
    return {"comp_raw": text, "comp_currency": cur, "comp_min": min(nums),
            "comp_max": max(nums) if len(nums) > 1 else None, "comp_confidence": "Stated"}


def company_stage(item: dict) -> str | None:
    industries = str(_first(item, "industries", "companyIndustry", default=""))
    if SERVICES_INDUSTRIES.search(industries):
        return "services"
    size = _first(item, "companyEmployeesCount", "companySize", "employeeCount")
    try:
        n = int(re.sub(r"[^\d]", "", str(size).split("-")[-1])) if size else None
    except ValueError:
        n = None
    if n is None:
        return None
    if n <= 50:
        return "seed"
    if n <= 200:
        return "series_a"
    if n <= 1000:
        return "series_c"
    return "large_product"


def work_mode(item: dict, location: str) -> str | None:
    wt = str(_first(item, "workplaceTypes", "workplaceType", "workType", default="")).lower()
    loc = location.lower()
    if "remote" in wt or "remote" in loc:
        return "Remote"
    if "hybrid" in wt or "hybrid" in loc:
        return "Hybrid"
    if "on-site" in wt or "onsite" in wt:
        return "Onsite"
    return None


def posted_date(item: dict) -> str | None:
    v = _first(item, "postedAt", "publishedAt", "postedDate", "listedAt", "datePosted")
    if not v:
        return None
    if isinstance(v, (int, float)):  # epoch ms
        return datetime.utcfromtimestamp(v / 1000 if v > 1e11 else v).date().isoformat()
    s = str(v)
    m = re.match(r"\d{4}-\d{2}-\d{2}", s)
    if m:
        return m.group(0)
    rel = re.match(r"(\d+)\s*(minute|hour|day|week|month)", s.lower())
    if rel:
        n, unit = int(rel.group(1)), rel.group(2)
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n}[unit]
        return (date.today() - timedelta(days=days)).isoformat()
    return None


def item_to_posting(item: dict) -> tuple[dict | None, str | None]:
    """Return (posting, skip_reason)."""
    cfg = scoring.load_config()
    title = str(_first(item, "title", "jobTitle", "position", default="")).strip()
    company = str(_first(item, "companyName", "company", "company_name", default="")).strip()
    if not title or not company:
        return None, "missing title/company"
    if TITLE_EXCLUDE.search(title):
        return None, "title excluded"
    industries = str(_first(item, "industries", "companyIndustry", default=""))
    if EXCLUDE_INDUSTRIES.search(industries):
        return None, f"industry excluded ({industries[:40]})"

    desc = str(_first(item, "descriptionText", "description", "jobDescription", default=""))
    location = str(_first(item, "location", "jobLocation", "formattedLocation", default=""))
    text = _norm_text(title, desc, industries)
    domains = detect_domains(text)
    arch, match = classify_archetype(title, domains)
    if arch is None:
        return None, "no archetype match"
    exp_min, exp_max = parse_experience(desc)
    if exp_max is not None and exp_max < 10:
        return None, f"experience ceiling {int(exp_max)}y"

    gaps_vocab = [g.lower() for g in cfg.get("linkedin", {}).get("known_gaps", [])]
    found_gaps = [g for g in gaps_vocab if re.search(r"\b" + re.escape(g) + r"\b", text)]
    comp = parse_salary(_first(item, "salaryInfo", "salary", "salaryRange", "compensation"))

    posting = {
        "company": company,
        "role_title": title,
        "archetype": arch,
        "archetype_match": match,
        "domains": domains,
        "location": location,
        "work_mode": work_mode(item, location),
        "date_posted": posted_date(item),
        "date_confidence": "Verified" if posted_date(item) else "Unverified",
        "experience_asked": (f"{int(exp_min)}{'-' + str(int(exp_max)) if exp_max else '+'} years"
                             if exp_min else _first(item, "seniorityLevel")),
        "exp_min": exp_min, "exp_max": exp_max,
        "comp_confidence": "Not stated",
        "company_stage": company_stage(item),
        "unmet_mandatories": found_gaps[:3],
        "missing_keywords": found_gaps[:3],
        "apply_url": _first(item, "applyUrl", "link", "jobUrl", "url"),
        "source": "LinkedIn (Apify)",
        "description": desc[:6000] or None,
        "notes": "Auto-enriched from LinkedIn - unmet mandatories are keyword-matched, verify before applying.",
    }
    posting.update(comp)
    return posting, None


# ---------------------------------------------------------------- keyword aggregation

def aggregate_keywords(postings: list[dict]) -> dict:
    cfg = scoring.load_config()
    have = {k.lower() for k in cfg["profile"]["resume_keywords"]}
    unstated = {k.lower() for k in cfg.get("linkedin", {}).get("claimable_unstated", [])}
    gaps = {k.lower() for k in cfg.get("linkedin", {}).get("known_gaps", [])}
    vocab = have | unstated | gaps
    n = len(postings) or 1
    counts: dict[str, int] = {}
    for p in postings:
        text = _norm_text(p.get("role_title"), p.get("description"))
        for term in vocab:
            if len(term) <= 2:
                continue  # 'ai', 'ce', 'ti' are too noisy for bare matching
            if re.search(r"\b" + re.escape(term) + r"\b", text):
                counts[term] = counts.get(term, 0) + 1
    kws, resume_gaps = [], []
    for term, f in sorted(counts.items(), key=lambda kv: -kv[1]):
        claim = "no" if term in gaps else ("yes_but_unstated" if term in unstated else "yes")
        row = {"term": term, "category": "technical", "frequency": f,
               "percent_of_postings": round(100 * f / n), "criticality": "low",
               "can_i_claim_it": claim, "on_my_resume": claim == "yes"}
        (resume_gaps if claim == "no" else kws).append(row)
    return {"generated": date.today().isoformat(), "postings_analysed": len(postings),
            "keywords": kws, "resume_gaps": resume_gaps}


def to_payload(items: list[dict], agent: str = "Apify LinkedIn", meta: dict | None = None) -> dict:
    postings, skipped = [], {}
    seen = set()
    for it in items:
        p, why = item_to_posting(it)
        if p is None:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        key = scoring.dedupe_key(p["company"], p["role_title"])
        if key in seen:
            skipped["duplicate within run"] = skipped.get("duplicate within run", 0) + 1
            continue
        seen.add(key)
        postings.append(p)
    notes = f"LinkedIn items: {len(items)}. Kept {len(postings)}. Pre-filtered: " + \
            (", ".join(f"{k} x{v}" for k, v in sorted(skipped.items(), key=lambda kv: -kv[1])) or "none")
    if meta:
        notes += f". Apify run {meta.get('run_id')}, dataset {meta.get('dataset_id')}"
        if meta.get("usage") is not None:
            notes += f", usage ${meta['usage']:.3f}" if isinstance(meta["usage"], float) else ""
    return {
        "run": {"run_date": date.today().isoformat(), "agent": agent,
                "sources_checked": ["LinkedIn Jobs (Apify)"],
                "sources_empty": [] if items else ["LinkedIn Jobs (Apify)"],
                "scanned": len(items), "passed_filter": len(postings), "notes": notes},
        "postings": postings,
        "keywords": aggregate_keywords(postings),
    }


# ---------------------------------------------------------------- background sync state

STATE: dict[str, Any] = {"running": False, "log": [], "result": None, "error": None,
                         "started": None, "finished": None}
_LOCK = threading.Lock()


def _log(msg: str) -> None:
    STATE["log"].append(f"{datetime.now():%H:%M:%S} {msg}")
    STATE["log"] = STATE["log"][-50:]


def start_sync(ingest_fn, dataset_id: str | None = None) -> bool:
    with _LOCK:
        if STATE["running"]:
            return False
        STATE.update(running=True, log=[], result=None, error=None,
                     started=datetime.now().isoformat(timespec="seconds"), finished=None)
    threading.Thread(target=_sync_worker, args=(ingest_fn, dataset_id), daemon=True).start()
    return True


def _sync_worker(ingest_fn, dataset_id: str | None) -> None:
    try:
        token = get_token()
        if not token:
            raise RuntimeError("No APIFY_TOKEN found. Put APIFY_TOKEN=... in the .env file next to config.json.")
        if dataset_id:
            _log(f"Fetching dataset {dataset_id}")
            items, meta = fetch_dataset(dataset_id, token), {"dataset_id": dataset_id}
        else:
            urls = build_search_urls()
            _log(f"Running {len(urls)} LinkedIn searches, cap {_cfg().get('max_results_per_run', 300)} results")
            items, meta = run_actor(urls, token, progress=_log)
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
    ap = argparse.ArgumentParser(description="Sync LinkedIn jobs via Apify into the tracker")
    ap.add_argument("--dry-run", action="store_true", help="print search URLs and exit")
    ap.add_argument("--dataset", help="import an existing Apify dataset id instead of running the actor")
    ap.add_argument("--file", help="import a local JSON file of raw Apify items")
    a = ap.parse_args()
    scoring.load_config(force=True)
    if a.dry_run:
        for u in build_search_urls():
            print(u)
        return
    from . import db
    from .main import ingest
    db.init_db()
    if a.file:
        items = json.loads(Path(a.file).read_text(encoding="utf-8"))
        print(json.dumps(ingest(to_payload(items, agent="Apify (file)")), indent=2, default=str)[:3000])
        return
    token = get_token()
    if not token:
        raise SystemExit("No APIFY_TOKEN. Add APIFY_TOKEN=... to .env")
    if a.dataset:
        items, meta = fetch_dataset(a.dataset, token), {"dataset_id": a.dataset}
    else:
        items, meta = run_actor(build_search_urls(), token, progress=print)
    res = ingest(to_payload(items, meta=meta))
    print(f"added {res['added']} · duplicates {res['duplicates']} · auto-rejected {res['auto_rejected']}")
    for t in res["top"]:
        print(f"  {t['final_score']:>5}  {t['band']:<13} {t['company']} - {t['role_title']}")


if __name__ == "__main__":
    _main()
