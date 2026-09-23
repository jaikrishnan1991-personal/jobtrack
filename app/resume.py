"""Role-tailored LaTeX resume generator.

Principle: SELECT and ORDER, never invent. Every sentence in the output comes from
resume/master.json (your CV plus facts you have stated). For a given posting the
generator:

  1. picks the headline and summary for the posting's archetype (A-E)
  2. scores every bullet by archetype tag + keyword overlap with the job text
  3. keeps the best N bullets per role (min/max per role in master.json)
  4. includes the MCP project only when the role rewards it
  5. reorders skill groups and bolds skills the posting mentions
  6. reports which JD terms you cover, which you could honestly add, and which are
     real gaps (never added)

Output compiles with plain pdflatex (standard packages only), is single-column and
ATS-parseable (no tables, no icons, no multi-column layout).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from datetime import date
from pathlib import Path

from . import scoring

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "resume" / "master.json"
OUT_DIR = ROOT / "data" / "resumes"

_UNI = {"·": r"\textperiodcentered{}", "—": "---", "–": "--", "→": r"$\rightarrow$",
        "₹": "INR ", "’": "'", "‘": "`", "“": "``", "”": "''", "…": r"\ldots{}", "ö": r"\"o"}

_ESC = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
        "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def uni(s: str) -> str:
    """Master text is trusted LaTeX; only normalise unicode punctuation."""
    for k, v in _UNI.items():
        s = s.replace(k, v)
    return s


def esc(s: str) -> str:
    """Untrusted text (company names, titles from job boards) - full escape."""
    s = "".join(_ESC.get(ch, ch) for ch in str(s or ""))
    return uni(s)


def load_master() -> dict:
    return json.loads(MASTER.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- location hygiene

# Some boards return location as a nested object rather than a string (Indeed sends
# {'countryCode': 'IN', ..., 'fullAddress': 'Bangalore City, Bengaluru, Karnataka'}). If that
# ever reaches a document it prints as a raw dict, so clean it both live and for rows already
# stored as a stringified dict.
def clean_location(job: dict) -> str:
    loc = job.get("location")
    if isinstance(loc, dict):
        loc = loc.get("fullAddress") or (loc.get("formatted") or {}).get("long") or ""
    loc = str(loc or "").strip()
    if loc.startswith("{") or "countryCode" in loc:
        m = re.search(r"'fullAddress':\s*'([^']+)'", loc)
        loc = m.group(1).strip() if m else ""
    return loc


def relocation_city(job: dict) -> str | None:
    """City to offer relocating to, or None when already local/remote or the posting's location
    is not one of the targeted cities. Matched against config.json's location tiers rather than
    the first comma-segment, because boards often put a street address there ("2nd Floor Quay
    Building ...") and offering to relocate to a street is worse than saying nothing."""
    loc = clean_location(job).lower()
    if not loc:
        return None
    for tier in scoring.load_config().get("location_tiers", []):
        for token in sorted(tier.get("match", []), key=len, reverse=True):
            if token in loc:
                if tier.get("tier") == 1:  # Hyderabad / remote - no relocation to offer
                    return None
                return token.upper() if len(token) <= 3 else token.title()
    return None


# ---------------------------------------------------------------- matching

def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9+#/ .-]", " ", (s or "").lower()))


def _has(text: str, term: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    if len(term) <= 3:
        return re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", text) is not None
    return term in text


def job_text(job: dict) -> str:
    # only what the employer wrote (plus extracted tags) - never our own notes/verdicts
    parts = [job.get("role_title"), job.get("description"),
             " ".join(job.get("domains") or []), " ".join(job.get("missing_keywords") or []),
             " ".join(job.get("unmet_mandatories") or [])]
    return _norm(" ".join(str(p) for p in parts if p))


def _score_item(item: dict, arch: str | None, text: str, domains: set[str]) -> float:
    s = 0.0
    tags = item.get("tags", [])
    if arch and arch in tags:
        s += 3 + (1.5 if tags and tags[0] == arch else 0)   # first tag = primary archetype
    hits = [k for k in item.get("keywords", []) if _has(text, k)]
    s += 2 * len(hits)
    s += 0.5 * len(domains & {k.lower() for k in item.get("keywords", [])})
    return s


# ---------------------------------------------------------------- tailoring

def tailor(job: dict) -> dict:
    m = load_master()
    arch = job.get("archetype") if job.get("archetype") in m["summaries"] else None
    text = job_text(job)
    domains = {d.lower() for d in (job.get("domains") or [])}

    roles = []
    for exp in m["experience"]:
        ranked = sorted(
            ((_score_item(b, arch, text, domains), i, b) for i, b in enumerate(exp["bullets"])),
            key=lambda t: (-t[0], t[1]))
        lo, hi = exp.get("min_bullets", 2), exp.get("max_bullets", 4)
        chosen = [t for t in ranked if t[0] >= 3][:hi]
        if len(chosen) < lo:
            chosen = ranked[:lo]
        chosen.sort(key=lambda t: -t[0])
        roles.append({**exp, "chosen": [c[2] for c in chosen],
                      "dropped": [t[2]["text"] for t in ranked if t not in chosen]})

    projects = [p for p in m.get("projects", [])
                if arch in ("B", "C") or any(_has(text, k) for k in p.get("keywords", []))]

    compact = m.get("compact_roles", [])  # keep CV order

    skills = []
    for g in m["skills"]:
        matched = [it for it in g["items"] if _has(text, _norm(it).replace("\\", "").strip())]
        rest = [it for it in g["items"] if it not in matched]
        skills.append({"group": g["group"], "matched": matched, "items": matched + rest})
    skills.sort(key=lambda g: -len(g["matched"]))

    # coverage report against the tracker's vocabulary
    cfg = scoring.load_config()
    li = cfg.get("linkedin", {})
    master_blob = _norm(json.dumps(m))
    vocab = set(k.lower() for k in cfg["profile"]["resume_keywords"]) | \
        set(k.lower() for k in li.get("claimable_unstated", [])) | set(k.lower() for k in li.get("known_gaps", []))
    in_jd = sorted(t for t in vocab if len(t) > 2 and _has(text, t))
    gaps = set(k.lower() for k in li.get("known_gaps", []))
    covered = [t for t in in_jd if t not in gaps and _has(master_blob, t)]
    addable = [t for t in in_jd if t not in gaps and t not in covered]
    real_gaps = sorted(set([t for t in in_jd if t in gaps] +
                           [g.lower() for g in (job.get("unmet_mandatories") or [])]))

    return {
        "archetype": arch, "headline": m["headlines"].get(arch or "default", m["headlines"]["default"]),
        "summary": m["summaries"].get(arch or "default"),
        "roles": roles, "projects": projects, "compact": compact, "skills": skills,
        "coverage": {"covered": covered, "honest_additions": addable, "real_gaps": real_gaps},
        "basics": m["basics"], "education": m["education"], "patents": m["patents"],
    }


# ---------------------------------------------------------------- LaTeX

PREAMBLE = r"""%% Tailored for: %(role)s @ %(company)s  (job %(job_id)s, generated %(today)s)
%% Built by JG Job Tracker from resume/master.json - every line is from your CV or stated facts.
%% Compile: pdflatex resume.tex   (or paste into Overleaf)
\documentclass[10pt,a4paper]{article}
\usepackage[a4paper,margin=0.62in,top=0.5in,bottom=0.5in]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage[scaled=0.95]{helvet}
\renewcommand{\familydefault}{\sfdefault}
\usepackage{xcolor}
\usepackage{enumitem}
\usepackage{titlesec}
\usepackage[hidelinks]{hyperref}
\usepackage{microtype}
\definecolor{accent}{HTML}{B0351E}
\definecolor{muted}{HTML}{555555}
\pagestyle{empty}
\setlength{\parindent}{0pt}
\titleformat{\section}{\color{accent}\small\bfseries\scshape\lsstyle}{}{0em}{}[\vspace{1pt}{\color{accent}\titlerule}]
\titlespacing*{\section}{0pt}{9pt}{4pt}
\setlist[itemize]{leftmargin=1.1em,itemsep=1.5pt,topsep=2pt,parsep=0pt,label={\color{accent}\textbullet}}
\newcommand{\role}[4]{\par\vspace{2pt}\textbf{#1} --- #2 \hfill {\color{muted}\small #3 \textperiodcentered{} #4}\par\nobreak}
\begin{document}
"""


def render_tex(job: dict, t: dict) -> str:
    b = t["basics"]
    def _c(v):  # safe inside a LaTeX comment line
        return re.sub(r"[%\r\n]", " ", str(v or ""))
    out = [PREAMBLE % {"role": _c(job.get("role_title")), "company": _c(job.get("company")),
                       "job_id": job.get("job_id") or job.get("id"), "today": date.today().isoformat()}]
    out.append(r"{\LARGE\bfseries %s}\\[2pt]" % uni(b["name"]))
    out.append(r"{\color{accent}\bfseries %s}\\[3pt]" % uni(t["headline"]))
    contact = [uni(b["location"]), uni(b["phone"]),
               r"\href{mailto:%s}{%s}" % (b["email"], b["email"]),
               r"\href{https://%s}{%s}" % (b["linkedin"], b["linkedin"])]
    # optional, so a master.json without it still renders
    for key in ("github", "website"):
        if b.get(key):
            contact.append(r"\href{https://%s}{%s}" % (b[key], b[key]))
    out.append(r"{\small " + r" \textperiodcentered{} ".join(contact) + r"}\\[1pt]")
    avail = b.get("availability") or ""
    city = relocation_city(job)
    if city:
        avail += (" \u00b7 " if avail else "") + f"Open to relocating to {city}"
    if avail:
        out.append(r"{\small\color{muted} %s}" % esc(avail))
    out.append("")
    out.append(r"\section*{Summary}")
    out.append(uni(t["summary"]))
    out.append("")

    out.append(r"\section*{Experience}")
    for r in t["roles"]:
        out.append(r"\role{%s}{%s}{%s}{%s}" % (uni(r["title"]), uni(r["company"]), uni(r["dates"]), uni(r["location"])))
        if r.get("intro"):
            out.append(r"{\small %s}" % uni(r["intro"]))
        out.append(r"\begin{itemize}")
        for bl in r["chosen"]:
            out.append(r"  \item " + uni(bl["text"]))
        out.append(r"\end{itemize}")
    for c in t["compact"]:
        out.append(r"\role{%s}{%s}{%s}{%s}" % (uni(c["title"]), uni(c["company"]), uni(c["dates"]), uni(c["location"])))
        out.append(r"{\small %s}\par" % uni(c["text"]))

    if t["projects"]:
        out.append(r"\section*{Selected Project}")
        for p in t["projects"]:
            out.append(r"\textbf{%s}\\" % uni(p["title"]))
            out.append(r"{\small %s}" % uni(p["text"]))

    out.append(r"\section*{Capabilities}")
    for g in t["skills"]:
        items = [(r"\textbf{%s}" % uni(i)) if i in g["matched"] else uni(i) for i in g["items"]]
        out.append(r"{\small \textbf{%s:} %s}\\[1pt]" % (uni(g["group"]), ", ".join(items)))

    out.append(r"\section*{Education}")
    for e in t["education"]:
        out.append(r"{\small \textbf{%s} --- %s \hfill {\color{muted}%s}}\\" % (uni(e["degree"]), uni(e["school"]), uni(e["year"])))

    out.append(r"\section*{Patents \& IP}")
    for p in t["patents"]:
        out.append(r"{\small \textbf{%s:} %s}\\" % (uni(p["title"]), uni(p["text"])))

    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


def name_slug() -> str:
    """Your name, as a filename prefix - taken from master.json so nothing is hardcoded."""
    name = (load_master().get("basics", {}) or {}).get("name", "Resume")
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_") or "Resume"


def filename_for(job: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job.get('company','')}-{job.get('role_title','')}".lower()).strip("-")[:60]
    return f"{name_slug()}_{slug}.tex"


def build(job: dict, save: bool = True) -> dict:
    t = tailor(job)
    tex = render_tex(job, t)
    fname = filename_for(job)
    if save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / fname).write_text(tex, encoding="utf-8")
    return {
        "filename": fname, "tex": tex, "archetype": t["archetype"], "headline": t["headline"],
        "coverage": t["coverage"],
        "selected": {r["company"]: [re.sub(r"\\[a-z]+\{|\}|\\\\|\$\\rightarrow\$", "", b["text"])[:90]
                                    for b in r["chosen"]] for r in t["roles"]},
        "project_included": bool(t["projects"]),
    }


# ---------------------------------------------------------------- PDF (optional)

def compiler() -> str | None:
    for c in ("pdflatex", "xelatex", "tectonic"):
        if shutil.which(c):
            return c
    return None


def compile_pdf(tex: str) -> bytes:
    c = compiler()
    if not c:
        raise RuntimeError("No LaTeX compiler found (pdflatex / tectonic). Use 'Open in Overleaf' instead.")
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "resume.tex"
        src.write_text(tex, encoding="utf-8")
        cmd = [c, "resume.tex"] if c == "tectonic" else [c, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"]
        r = subprocess.run(cmd, cwd=d, capture_output=True, text=True, timeout=90)
        pdf = Path(d) / "resume.pdf"
        if r.returncode != 0 or not pdf.exists():
            tail = (r.stdout or "")[-1500:]
            raise RuntimeError(f"{c} failed:\n{tail}")
        return pdf.read_bytes()
