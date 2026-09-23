"""Scoring engine. The research agent supplies FACTS; this module supplies JUDGEMENT.

Keeping the rubric here (not in the LLM prompt) means every posting - from any agent,
any run, any model - is scored by identical arithmetic, and retuning the rubric
re-scores history instead of forcing a re-run.
"""
from __future__ import annotations
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"

_CONFIG: dict[str, Any] = {}

HARD_DOMAINS = {"hardware", "robotics", "iot", "embedded", "ai", "ml",
                "mechatronics", "devices", "appliances", "industrial"}
PLATFORM_DOMAINS = {"platform", "developer platform", "ai platform", "data",
                    "infrastructure", "agentic", "llm"}

STAGE_SCORES = {
    "pre_seed": 10, "seed": 10, "series_a": 10, "series_b": 10, "series_c": 10,
    "deeptech": 10, "growth": 8, "large_product": 7, "public": 7,
    "enterprise": 7, "services": 3, "consulting": 3, "staffing": 0,
}


def load_config(force: bool = False) -> dict:
    global _CONFIG
    if not _CONFIG or force:
        _CONFIG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _CONFIG


# ---------------------------------------------------------------- normalising

_TITLE_NOISE = re.compile(
    r"\b(senior|sr|junior|jr|lead|principal|staff|group|global|regional|"
    r"m/f/d|all genders|remote|hybrid|onsite|full[- ]time|contract|"
    r"i{1,3}|iv|v|[0-9]+)\b", re.I)


def norm_company(name: str) -> str:
    n = (name or "").lower()
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = re.sub(r"\b(pvt|private|ltd|limited|inc|llc|llp|gmbh|plc|corp|corporation|"
               r"technologies|technology|labs|systems|india|the)\b", " ", n)
    return re.sub(r"\s+", " ", n).strip()


def norm_title(title: str) -> str:
    t = (title or "").lower()
    t = re.sub(r"\(.*?\)", " ", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    t = _TITLE_NOISE.sub(" ", t)
    t = t.replace("vice president", "vp").replace("product manager", "pm")
    return re.sub(r"\s+", " ", t).strip()


def dedupe_key(company: str, title: str) -> str:
    return f"{norm_company(company)}::{norm_title(title)}"


# ---------------------------------------------------------------- location

def resolve_location(location: str, work_mode: str = "") -> tuple[int | None, float, str]:
    """Return (tier, multiplier, reason). tier None => reject.

    A named city/country wins over a 'Remote' work mode: "Singapore, Remote" is tier 6,
    not tier 1. Remote counts as tier 1 only when India-eligible or unrestricted.
    """
    cfg = load_config()

    def match(text: str):
        best = None
        for entry in cfg["location_tiers"]:
            if any(tok in text for tok in entry["match"]):
                if best is None or entry["tier"] < best["tier"]:
                    best = entry
        return best

    loc = (location or "").lower()
    hay = f"{loc} {(work_mode or '').lower()}"
    named = match(loc.replace("remote", ""))
    if named is not None:
        return named["tier"], named["multiplier"], ""
    best = match(hay)
    if best is not None and best["tier"] == 1 and "india" not in hay:
        for tok in cfg.get("remote_restricted_tokens", []):
            if tok in hay:
                return None, 0.0, f"Remote role restricted to '{tok}' - not India-eligible"
    if best is None:
        return None, 0.0, f"Location '{location}' is outside the target geographies"
    return best["tier"], best["multiplier"], ""


LOC_SUBSCORE = {1: 5, 2: 4, 3: 3, 4: 3, 5: 2, 6: 2}


# ---------------------------------------------------------------- compensation

def comp_bucket(currency: str | None, cmin: float | None, cmax: float | None,
                confidence: str) -> tuple[str, float]:
    """Return (bucket, subscore). bucket in below_floor|between|at_target|unknown."""
    cfg = load_config()
    if not currency or (cmin is None and cmax is None):
        return "unknown", 5.0
    floors = cfg["comp_floors"].get(currency.upper())
    if not floors:
        return "unknown", 5.0
    top = cmax if cmax is not None else cmin
    bottom = cmin if cmin is not None else cmax
    if top < floors["hard_floor"]:
        return "below_floor", 0.0
    if top >= floors["target_low"] or bottom >= floors["target_low"]:
        return "at_target", 10.0
    return "between", 6.0


def comp_usd(currency: str | None, cmin: float | None, cmax: float | None) -> float | None:
    cfg = load_config()
    if not currency:
        return None
    rate = cfg.get("fx_per_usd", {}).get(currency.upper())
    if not rate:
        return None
    val = cmax if cmax is not None else cmin
    if val is None:
        return None
    usd = val / rate
    if cfg["comp_floors"].get(currency.upper(), {}).get("tax_free"):
        usd *= cfg.get("tax_free_uplift", 1.0)
    return round(usd)


# ---------------------------------------------------------------- sub-scores

def score_archetype(match: str | None, archetype: str | None) -> float:
    m = (match or "").lower()
    if m in ("direct", "exact"):
        return 25.0
    if m == "adjacent":
        return 15.0
    if m in ("tangential", "weak"):
        return 5.0
    return 25.0 if archetype in ("A", "B", "C", "D", "E") else 5.0


def score_domain(domains: list[str] | None) -> float:
    d = {str(x).lower().strip() for x in (domains or [])}
    hard = d & HARD_DOMAINS
    if len(hard) >= 2:
        return 20.0
    if len(hard) == 1:
        return 12.0
    if d & PLATFORM_DOMAINS:
        return 8.0
    return 0.0


def score_seniority(exp_min: float | None, exp_max: float | None) -> float:
    if exp_min is None and exp_max is None:
        return 10.0
    lo = exp_min if exp_min is not None else exp_max
    if lo >= 15:
        return 8.0
    if lo >= 10:
        return 15.0
    if lo >= 8:
        return 12.0
    return 3.0


def score_gaps(unmet: list[str] | None) -> float:
    n = len(unmet or [])
    return {0: 15.0, 1: 8.0, 2: 3.0}.get(n, 0.0)


def score_stage(stage: str | None, domains: list[str] | None) -> float:
    s = (stage or "").lower().replace(" ", "_").replace("-", "_")
    if s in STAGE_SCORES:
        base = STAGE_SCORES[s]
    else:
        base = 7.0
    # "a product org with hardware" also earns 10
    if base == 7 and ({str(x).lower() for x in (domains or [])} & {"hardware", "robotics", "iot", "embedded"}):
        base = 10.0
    return float(base)


# ---------------------------------------------------------------- top level

# ---------------------------------------------------------------- favourites / affinity

_DYNAMIC_FAVS: list[dict] = []   # set by main from the DB (roles starred in the app)


def set_dynamic_favourites(favs: list[dict]) -> None:
    global _DYNAMIC_FAVS
    _DYNAMIC_FAVS = favs or []


def favourites() -> list[dict]:
    seen, out = set(), []
    for f in list(load_config().get("favourite_roles", [])) + _DYNAMIC_FAVS:
        k = dedupe_key(f.get("company", ""), f.get("role_title", ""))
        if k not in seen:
            seen.add(k); out.append(f)
    return out


def _posting_text(p: dict) -> str:
    parts = [p.get("role_title"), p.get("description"), " ".join(p.get("domains") or []),
             " ".join(p.get("missing_keywords") or [])]
    return " ".join(str(x) for x in parts if x).lower()


def _term_in(text: str, term: str) -> bool:
    term = term.lower().strip()
    if len(term) <= 3:
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None
    return term in text


def affinity(p: dict) -> tuple[float, str | None]:
    """0..1 similarity to the closest favourite role, and which one."""
    cfg = load_config()
    full = max(1, int(cfg.get("favourite_full_match_hits", 6)))
    text = _posting_text(p)
    key = dedupe_key(p.get("company", ""), p.get("role_title", ""))
    best, who = 0.0, None
    for f in favourites():
        if dedupe_key(f.get("company", ""), f.get("role_title", "")) == key:
            return 1.0, f"{f.get('company')} - {f.get('role_title')}"
        hits = sum(1 for t in f.get("key_terms", []) if _term_in(text, t))
        score = min(1.0, hits / full)
        if score > best:
            best, who = score, f"{f.get('company')} - {f.get('role_title')}"
    return round(best, 2), who


def extract_terms(p: dict, limit: int = 25) -> list[str]:
    """Key terms for a newly starred role: domain vocabulary found in its text + title words."""
    cfg = load_config()
    li = cfg.get("linkedin", {})
    vocab = set(k.lower() for k in cfg["profile"]["resume_keywords"]) | \
        set(k.lower() for k in li.get("claimable_unstated", []))
    for f in cfg.get("favourite_roles", []):
        vocab |= set(t.lower() for t in f.get("key_terms", []))
    text = _posting_text(p)
    found = [t for t in sorted(vocab) if len(t) > 2 and _term_in(text, t)]
    title_words = [w for w in re.findall(r"[a-z][a-z/+-]{2,}", (p.get("role_title") or "").lower())
                   if w not in {"and", "the", "for", "with", "senior", "lead", "product", "manager", "group",
                                "head", "director", "principal", "staff", "vp", "vice", "president", "engineer",
                                "engineering", "management", "team", "sr", "jr", "associate", "chief", "officer"}]
    return list(dict.fromkeys(found + title_words))[:limit]


def band_for(final_score: float) -> tuple[str, str]:
    for b in load_config()["bands"]:
        if final_score >= b["min"]:
            return b["label"], b["action"]
    return "Do not apply", ""


def days_old(date_posted: str | None, ref: date | None = None) -> int | None:
    if not date_posted:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            d = datetime.strptime(date_posted[:10], fmt).date()
            return ((ref or date.today()) - d).days
        except ValueError:
            continue
    return None


def evaluate(p: dict, check_freshness: bool = True, ref_date: str | None = None) -> dict:
    """p = one posting dict from the agent. Returns scoring fields + reject reason."""
    cfg = load_config()
    w = cfg["scoring_weights"]

    tier, mult, loc_reason = resolve_location(p.get("location", ""), p.get("work_mode", ""))
    s_arch = score_archetype(p.get("archetype_match"), p.get("archetype"))
    s_dom = score_domain(p.get("domains"))
    s_sen = score_seniority(p.get("exp_min"), p.get("exp_max"))
    s_gap = score_gaps(p.get("unmet_mandatories"))
    s_stg = score_stage(p.get("company_stage"), p.get("domains"))
    # an AI/innovation LAB inside a services firm is closer to a product org than to delivery work
    pat = cfg.get("services_lab_title_pattern")
    if pat and s_stg <= 3 and re.search(pat, (p.get("role_title") or "").lower()):
        s_stg = float(cfg.get("services_lab_stage_score", 7))
    bucket, s_cmp = comp_bucket(p.get("comp_currency"), p.get("comp_min"),
                                p.get("comp_max"), p.get("comp_confidence", "Not stated"))
    s_loc = float(LOC_SUBSCORE.get(tier, 0))

    # weights are the rubric maxima; rescale if the user edits them
    def rs(val, max_default, key):
        target = w.get(key, max_default)
        return val if target == max_default else round(val / max_default * target, 2)

    s_arch = rs(s_arch, 25, "archetype")
    s_dom = rs(s_dom, 20, "domain")
    s_sen = rs(s_sen, 15, "seniority")
    s_gap = rs(s_gap, 15, "requirement_gaps")
    s_stg = rs(s_stg, 10, "company_stage")
    s_cmp = rs(s_cmp, 10, "compensation")
    s_loc = rs(s_loc, 5, "location")

    base = s_arch + s_dom + s_sen + s_gap + s_stg + s_cmp + s_loc
    fav_w = float(cfg.get("favourite_weight", 0) or 0)
    aff, aff_ref = affinity(p) if fav_w else (0.0, None)
    s_aff = round(aff * fav_w, 1)
    fit = round(base * (100 - fav_w) / 100 + s_aff, 1)
    final = round(fit * mult, 1)
    label, _action = band_for(final)
    # a role with N+ mandatories you don't meet is not a "stretch", whatever else it scores
    cap = cfg.get("unmet_mandatories_hard_cap", 3)
    if cap and len(p.get("unmet_mandatories") or []) >= cap:
        label = "Do not apply"

    # hard rejects
    reject = None
    if tier is None:
        reject = loc_reason
    elif bucket == "below_floor":
        reject = f"Compensation below the {p.get('comp_currency')} hard floor"
    elif check_freshness:
        ref = None
        if ref_date:
            try:
                ref = datetime.strptime(ref_date[:10], "%Y-%m-%d").date()
            except ValueError:
                ref = None
        age = days_old(p.get("date_posted"), ref)
        if age is not None and age > cfg["freshness_days"]:
            reject = f"Posting is {age} days old (limit {cfg['freshness_days']})"

    why = p.get("why_this_score") or _auto_why(p, s_gap, s_dom, s_sen, bucket)

    return {
        "location_tier": tier, "location_multiplier": mult,
        "s_archetype": s_arch, "s_domain": s_dom, "s_seniority": s_sen,
        "s_gaps": s_gap, "s_stage": s_stg, "s_comp": s_cmp, "s_location": s_loc,
        "s_affinity": s_aff, "affinity": aff, "affinity_ref": aff_ref,
        "fit_score": fit, "final_score": final, "band": label,
        "why_this_score": why, "rejected_reason": reject,
        "comp_bucket": bucket, "comp_usd_equiv": comp_usd(
            p.get("comp_currency"), p.get("comp_min"), p.get("comp_max")),
    }


def _auto_why(p: dict, s_gap: float, s_dom: float, s_sen: float, bucket: str) -> str:
    unmet = p.get("unmet_mandatories") or []
    if unmet:
        return f"Unmet mandatory: {unmet[0]}" + (f" (+{len(unmet)-1} more)" if len(unmet) > 1 else "")
    if s_dom <= 8:
        return "Domain is software-only; no hardware, robotics or embedded surface to lean on."
    if s_sen <= 8:
        return "Seniority band is off - the role asks for a different experience range."
    if bucket == "between":
        return "Clears the floor but sits below the target comp band."
    if bucket == "unknown":
        return "Compensation not stated; the rest of the fit is clean."
    return "No blocking gap identified."
