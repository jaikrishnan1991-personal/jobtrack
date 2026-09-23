"""Role-tailored LaTeX cover letter generator.

Same principle as resume.py: SELECT and ORDER, never invent. Every sentence comes from
resume/master.json - the archetype summary (used verbatim as the opening paragraph), then the
top-scoring bullets from the two or three most relevant roles turned into first-person
sentences ("Own the X" -> "I own the X" - master.json already writes bullets in the correct
tense per role, present for the current job, past for former ones, so no tense rewriting is
needed here), then a candour paragraph naming real gaps (resume.tailor()'s coverage.real_gaps)
if there are any, then availability/relocation and a sign-off.

This reuses resume.tailor() for all of the actual matching/scoring logic and resume.uni()/esc()
for LaTeX-safety - it does not re-implement any of that. Compiles with the same PREAMBLE
(same look as the resume: name, headline, contact line) via resume.compile_pdf().
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from . import resume
from .resume import esc, uni

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "data" / "cover_letters"


# resume.py treats a bullet as relevant to a JD at score >= 3; a letter should clear the same
# bar. A resume still lists a role you held, so it pads to min_bullets - a letter has no such
# obligation, so anything under the bar is simply left out rather than padded in.
RELEVANT = 3.0
MAX_ROLE_PARAGRAPHS = 3
MAX_SENTENCES_PER_ROLE = 2


def _lower_first(s: str) -> str:
    return s[0].lower() + s[1:] if s else s


def _prose(s: str) -> str:
    """Resume-bullet LaTeX -> plain prose. Bold/italic emphasis and the arrow notation read as
    markup in a flowing sentence, so drop them."""
    s = re.sub(r"\\text(?:bf|it)\{([^{}]*)\}", lambda m: m.group(1) or "", s)
    s = s.replace(r"$\rightarrow$", "to")
    return re.sub(r"\s+", " ", s).strip()


def tailor(job: dict) -> dict:
    t = resume.tailor(job)
    arch = t["archetype"]
    text = resume.job_text(job)
    domains = {d.lower() for d in (job.get("domains") or [])}

    def score(bullet: dict) -> float:
        return resume._score_item(bullet, arch, text, domains)

    # Rank roles by how relevant THIS job made them, not raw CV order - a hand-written letter
    # leads with the strongest proof point, not the most recent one - and keep only the bullets
    # that actually clear the relevance bar.
    ranked = []
    for r in t["roles"]:
        good = [b for b in r["chosen"] if score(b) >= RELEVANT][:MAX_SENTENCES_PER_ROLE]
        if good:
            ranked.append((max(score(b) for b in good), r, good))
    ranked.sort(key=lambda x: -x[0])

    # If nothing cleared the bar, fall back to the single best-matching role so the letter still
    # says something concrete rather than being summary-only.
    if not ranked:
        scored = [(score(r["chosen"][0]), r) for r in t["roles"] if r["chosen"]]
        if scored:
            best_score, best = max(scored, key=lambda x: x[0])
            ranked = [(best_score, best, best["chosen"][:1])]

    paragraphs = []
    # 1. hook - the archetype summary, already hand-written prose
    paragraphs.append(uni(t["summary"]))

    # 2. body - one paragraph per relevant role, so a reader can skim by employer
    for _, role, bullets in ranked[:MAX_ROLE_PARAGRAPHS]:
        sentences = [f"I {_lower_first(_prose(b['text']))}" for b in bullets]
        paragraphs.append(f"At {uni(role['company'])}, " + " ".join(sentences))

    # Deliberately no auto-written candour paragraph. real_gaps comes from a keyword scan over
    # the JD, which throws false positives ("insurance" matched from a benefits blurb, "wms"
    # from a passing mention) - and a letter that tells an employer your background doesn't
    # cover "wms" is worse than one that stays silent. The gaps are returned for the UI to show
    # so they can be judged, and written up by hand if they are genuinely worth naming.
    return {**t, "letter_paragraphs": paragraphs}


# ---------------------------------------------------------------- LaTeX

def render_tex(job: dict, t: dict) -> str:
    b = t["basics"]
    def _c(v):
        return re.sub(r"[%\r\n]", " ", str(v or ""))
    out = [resume.PREAMBLE % {"role": _c(job.get("role_title")), "company": _c(job.get("company")),
                              "job_id": job.get("job_id") or job.get("id"), "today": date.today().isoformat()}]
    out.append(r"{\LARGE\bfseries %s}\\[2pt]" % uni(b["name"]))
    out.append(r"{\color{accent}\bfseries %s}\\[3pt]" % uni(t["headline"]))
    contact = [uni(b["location"]), uni(b["phone"]),
               r"\href{mailto:%s}{%s}" % (b["email"], b["email"]),
               r"\href{https://%s}{%s}" % (b["linkedin"], b["linkedin"])]
    if b.get("website"):
        contact.append(r"\href{https://%s}{%s}" % (b["website"], b["website"]))
    out.append(r"{\small " + r" \textperiodcentered{} ".join(contact) + r"}\\[1pt]")
    # GitHub on its own line - see resume.py for why it is not on the contact line
    if b.get("github"):
        out.append(r"{\small\color{muted} \href{https://%s}{%s}}" % (b["github"], b["github"]))
    out.append(r"{\color{accent}\rule{\linewidth}{0.6pt}}")
    out.append("")
    out.append(date.today().strftime("%d %B %Y"))
    out.append("")
    company = esc(job.get("company") or "")
    out.append(f"{company} --- Hiring Team\\\\")
    loc = resume.clean_location(job)
    if loc:
        out.append(esc(loc))
    out.append("")
    out.append(r"\textbf{Re: %s}" % esc(job.get("role_title") or ""))
    out.append("")
    out.append("Dear Hiring Team,")
    out.append("")
    for para in t["letter_paragraphs"]:
        out.append(para)
        out.append("")

    city = resume.relocation_city(job)
    notice = re.sub(r"^Notice period:\s*", "", b.get("availability") or "").strip()
    bits = []
    if city:
        bits.append(f"I am open to relocating to {esc(city)}")
    if notice:
        bits.append(("and can join in " if bits else "I can join in ") + esc(notice))
    if bits:
        out.append(" ".join(bits) + ".")
    out.append("I would welcome the chance to discuss how I could contribute.")
    out.append("")
    out.append(r"Yours sincerely,\\")
    out.append(r"\textbf{%s}" % uni(b["name"]))
    out.append(r"\end{document}")
    return "\n".join(out) + "\n"


def filename_for(job: dict) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", f"{job.get('company','')}-{job.get('role_title','')}".lower()).strip("-")[:60]
    return f"{resume.name_slug()}_CoverLetter_{slug}.tex"


def build(job: dict, save: bool = True) -> dict:
    t = tailor(job)
    tex = render_tex(job, t)
    fname = filename_for(job)
    if save:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / fname).write_text(tex, encoding="utf-8")
    return {"filename": fname, "tex": tex, "archetype": t["archetype"],
            "paragraphs": t["letter_paragraphs"], "real_gaps": t["coverage"]["real_gaps"]}
