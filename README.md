# JG Job Tracker

A local job-search pipeline. Runs entirely on your machine — FastAPI + SQLite, no cloud,
no account, no Google Sheet. One file on disk (`data/jobtrack.db`) holds everything.

The split that makes this work: **the research agent supplies verified facts, the app
supplies judgement.** Scoring, deduplication, floor filters and freshness checks are
arithmetic in `app/scoring.py`, not something an LLM re-derives each week. Retune
`config.json`, hit **Re-score all**, and your whole history re-scores under the new rubric.

## Setup

Your own data never leaves your machine — `config.json`, `resume/master.json`, `data/` and
`.env` are all gitignored. Start from the committed examples:

```bash
cp config.example.json config.json            # profile, locations, comp floors, scoring
cp resume/master.example.json resume/master.json   # the only source of resume/letter text
cp .env.example .env                          # APIFY_TOKEN, optional Gmail app password
```

Then fill them in. `config.json` drives the search and scoring; `resume/master.json` is the
sole source of every line the resume and cover-letter generators can use.

## Run it

```bash
./run.sh          # macOS / Linux
run.bat           # Windows
```

First run builds a venv and installs FastAPI + uvicorn (~15 MB). Then open
<http://127.0.0.1:8765>.

Manual alternative:

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8765
```

Python 3.10+. Nothing listens on a public interface.

## Apply list + tailored resumes

The **Apply list** tab (the default view) shows every Strong / Worth-a-shot role you haven't
applied to, best first. Each card has **Create resume**, **Apply ↗**, **Mark applied** and
**Details**. On first run the app seeds itself from `imports/*.json` (verified postings), so
the list is never empty on day one.

**Create resume** builds a LaTeX resume for that specific role from `resume/master.json`:

- picks the headline + summary for the role's archetype (A–E)
- scores every bullet by archetype tag and keyword overlap with the JD, keeps the best per role
- includes the MCP orchestrator project only when the JD rewards it
- reorders skill groups and **bolds** skills the posting mentions
- shows three chip rows: JD terms you cover · terms you have but haven't worded in · real gaps

It **selects and orders — it never writes new claims.** Every line in `master.json` is from
your CV (`"source": "cv"`) or a fact you stated (`"source": "stated"`). Add bullets there
(with `tags` and `keywords`) and every future resume can use them.

Output: single-column, ATS-safe, standard packages only. **Download .tex**, **Copy LaTeX**,
**Download PDF** (compiles locally via `pdflatex`/`tectonic` if installed — MacTeX, MiKTeX or
TeX Live all work; this machine has MiKTeX, so PDF generation is native, not just an Overleaf
hand-off), or **Open in Overleaf** (one click, no install) as a fallback if no compiler is
found. Generated files are kept in `data/resumes/`.

## Tailored cover letters

**Create cover letter**, next to Create resume on every Apply card and in the Details drawer,
builds a LaTeX cover letter for that role from `app/cover_letter.py`. Same principle as the
resume generator - select and order, never invent:

- opening paragraph is the archetype summary from `resume/master.json`, verbatim
- body paragraphs are your most relevant roles (ranked by how well this specific JD scores
  their bullets, not raw CV order), each bullet turned into a first-person sentence
- **anything that doesn't clear the relevance bar is left out, not padded in.** A resume lists
  a role you held so it pads to `min_bullets`; a letter has no such obligation, so bullets
  scoring under 3 (the same bar `resume.py` uses) simply don't appear, and a role with no
  qualifying bullet gets no paragraph
- availability/relocation line, sign-off

It deliberately does **not** write a "here are my gaps" paragraph. `coverage.real_gaps` comes
from a keyword scan of the JD which throws false positives — it flagged "insurance" off a
benefits blurb and "wms" off a passing mention — and a letter telling an employer your
background doesn't cover "wms" is worse than one that stays quiet. The flagged terms are shown
in the drawer instead, so you can judge them and write a candour line by hand if one is
genuinely worth naming.

Two things that leak into documents if you aren't careful, both now handled centrally in
`resume.py` — reuse these rather than re-deriving them:

- `clean_location(job)` — some boards send location as a nested object (Indeed sends
  `{'countryCode': 'IN', ..., 'fullAddress': '...'}`). Unhandled, it gets `str()`'d into the
  database and printed on a resume as a raw Python dict. This rescues both live dicts and rows
  already stored as a stringified one.
- `relocation_city(job)` — matched against `config.json`'s location tiers, **not** the first
  comma-segment of the address, because boards routinely put a street address there ("2nd Floor
  Quay Building Bagmane Tech Park…"). Returns `None` for Hyderabad/remote (nothing to offer) and
  for any location that isn't a targeted city, in which case the line is omitted entirely.

This is a mechanical composition, not a rewrite of the hand-written letters you may already
have - it will read more generic. Read it before sending. Same output options as resumes
(.tex / PDF / Overleaf), saved to `data/cover_letters/`.

**A note on file encoding, since it bit this feature once:** `resume/master.json` has a literal
"·" (middle dot, U+00B7) in some headlines. Every place that reads or writes a `.tex` file, or
reads `master.json`/`config.json`/a `--file` import, must specify `encoding="utf-8"` explicitly.
Windows' Python defaults `read_text()`/`write_text()` to the system codepage (cp1252 on a
typical English Windows install), which silently misdecodes that character into "Â·" and, in
one case, produced bytes invalid enough that `pdflatex` refused to compile at all. Every file
I/O call in this codebase now passes `encoding="utf-8"` explicitly - keep that up in anything
new you add here.

## Emailing recruiters

**Email recruiter** on an Apply card composes a real email for that posting: subject, a
plain-text body built from the same tailored cover-letter paragraphs the PDF uses, and the
tailored resume PDF attached (cover letter PDF optional). You edit any of it before it goes.

### Three delivery paths

**Queue for Claude to send** needs no credentials at all. It writes the composed message plus
the PDFs to `data/outbox/job-<id>.json`; you then ask Claude to send the outbox and it goes out
through Claude's authenticated Gmail connector. The catch is real and worth knowing before you
rely on it: the app and Claude are separate processes, so the attachment has to travel through
Claude's context as base64 — about 73 KB of it for a one-page resume. That is slow, costs a
chunk of context per email, and is the kind of long verbatim payload that can be transcribed
wrongly. Fine for a handful; not the way to send thirty.

The other two go straight from your own Gmail using an **app password** in `.env`
(`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` — create one at
<https://myaccount.google.com/apppasswords>; never put your real password there):

- **Save to Gmail Drafts** (the default) — IMAP-appends to Drafts. Nothing is sent; you read it
  in Gmail and press send yourself.
- **Send now** — SMTP, immediate. Requires an explicit confirmation in the browser *and*
  `confirm: true` on the API call, and refuses to email the same posting twice without `force`.

**Finding addresses is the hard part, and mostly fails.** `Find emails in job descriptions`
(Import tab) scans stored JDs for free. On this pipeline that found **5 addresses across 240
postings**, four of them staffing agencies. Apify is not a way around it: Naukri returned an
empty `email` field on all 791 scraped items, and 0 of 50 even with `fetchDetails` on NaukriGulf,
because the boards keep recruiter contact behind their own apply flow deliberately — that is
their business model. Instahyre's `enrichEmails` add-on ($0.04/job) finds the *company website's*
generic inbox, not a named recruiter. So treat the composer as "I found an address myself, write
the note for me" rather than something that will reach 240 recruiters.

There is deliberately **no send-to-all button**. A blast to scraped addresses trips Gmail's
~500/day cap, burns sender reputation, and reads as spam to the people being asked for a job.
One posting at a time, each read before it goes.

## Starred roles ("more like this")

Star a role (☆ on an Apply card or in Details) to tell the tracker *this is the kind of job I
want*. Seeded stars: **Botsync — Group Product Manager, AMR** and **Capgemini — AI Lab Lead,
Physical Hardware & Edge Robotics** (`config.json → favourite_roles`).

- **Scoring:** every posting gets a *Like ★ roles* sub-score (0–10) = share of a starred
  role's key terms found in its JD (6 hits = full). The rest of the rubric is scaled to 90,
  so totals stay /100. Starring or unstarring re-scores everything.
- **Apply list:** starred roles first, then Strong / Worth a shot, **plus any role ≥60%
  similar to a star even if it is a Stretch** (`apply_list_min_affinity`).
- **Services labs:** an AI-lab / innovation / R&D / CoE title at a services firm scores
  stage 7 instead of 3 (`services_lab_title_pattern`) — delivery roles still score 3.
- **Search:** three LinkedIn queries modelled on the two stars run first; the title
  classifier now recognises "Group Product Manager – robotics" as hardware product
  leadership and "Edge/Physical AI … Lead" roles as engineering leadership; the research
  prompt has a REFERENCE ROLES section; **Agent brief** lists your stars with key terms.

## LinkedIn sync (Apify)

1. Copy `.env.example` to `.env` and paste your Apify token: `APIFY_TOKEN=apify_api_...`
2. Import tab → **Run LinkedIn sync**. Takes 3–10 minutes; the log shows progress.

What it does: builds 40 LinkedIn guest-search URLs (5 archetype queries × 8 locations, last
14 days, Mid-Senior/Director/Executive), runs them through
[`curious_coder/linkedin-jobs-scraper`](https://apify.com/curious_coder/linkedin-jobs-scraper)
(no login, public job pages only — your LinkedIn account is never used), then:

- **pre-filters** junior/sales/HR titles, banking/insurance/retail industries, roles whose
  experience ceiling is under 10 years, titles matching no archetype
- **enriches** archetype + match strength (title rules), domains (JD keyword patterns),
  experience (regex), salary (₹/$/AED/SGD, lakh/K/monthly), company stage (headcount +
  industry), work mode, posting date
- **flags unmet mandatories** by matching `linkedin.known_gaps` in the JD — crude but harsh,
  which is the right direction. Verify before applying.
- **aggregates keywords** into the Keyword gaps tab using your resume list, `known_gaps`
  and `claimable_unstated`

Cost: roughly USD 1 per 1,000 results. The run is capped by `max_results_per_run` (300 ≈
USD 0.30). Tune queries, locations, cap and gap terms in `config.json → linkedin`.

Other ways in: paste a dataset ID from a run you did in the Apify console, paste raw Apify
JSON into the Import box (auto-detected), or schedule it:

```bash
python -m app.linkedin --dry-run      # print the 40 search URLs
python -m app.linkedin                # run + import (cron / Task Scheduler friendly)
python -m app.linkedin --dataset ID   # import an existing Apify dataset
```

Weekly cron example (Mondays 8am): `0 8 * * 1 cd ~/jobtrack && .venv/bin/python -m app.linkedin`

## Indeed sync (Apify) and Naukri sync (Apify)

Same idea, same `.env` token, same Import tab. Two differences from LinkedIn worth knowing:

- **One query+location per Apify run**, not one batched run. `app/indeed.py` and `app/naukri.py`
  fan out into several small runs (`config.json → indeed` / `→ naukri`) and poll them together,
  throttled to 3 concurrent runs each - this Apify account allows only 5 concurrent runs total
  across every actor, so don't run all three syncs at once expecting them to overlap freely.
- **Naukri.com searches are built as direct URLs** (`naukri.com/<keyword>-jobs-in-<city>`), the
  same trick `linkedin.py` uses for LinkedIn guest search. **NaukriGulf** (UAE/Oman) goes through
  the actor's own `jobBoard`/`location` fields instead, since Gulf city filters aren't slug-based.

CLI:

```bash
python -m app.indeed --dry-run     # print the query/location specs
python -m app.indeed               # run + import
python -m app.naukri --dry-run
python -m app.naukri
```

Both reuse `linkedin.py`'s enrichment heuristics (domain/archetype/experience/salary parsing,
keyword aggregation) rather than duplicating them - see that module's docstring. If a source's
raw item shape doesn't match the generic field names those heuristics expect (Indeed nests the
company name under `companyDetails.name`, for instance), fix it with a small normalizer in that
source's own module, the way `indeed.py::_normalize_item` does - not in the shared code.

## Instahyre sync (Apify) and Wellfound sync (Apify)

Two more sources, same `.env` token, same Import tab.

- **Instahyre** (`app/instahyre.py`) is a single Apify call - its actor takes keyword and
  location arrays natively, so there's no fan-out. Curated India tech jobs; the actor caps
  `keywords` at 10 items (enforced in `config.json → instahyre`).
- **Wellfound** (`app/wellfound.py`) fans out one Apify run per location, same throttling as
  Indeed/Naukri. Startup-heavy, most relevant to the founder/deep-tech archetypes (C/E) - but
  in testing, this particular actor returned the same generic junior/sales/intern listings
  regardless of location or an explicit keyword filter, so treat it as a low-yield source for a
  senior hardware/product profile until proven otherwise on a larger sample. Kept in the app
  since it's cheap to run and occasionally worth a check, not because it has shown strong signal.

Other India/startup job boards exist on Apify (Hirist, CutShort, Foundit/Monster) but weren't
wired in - `config.json` follows the same shape for all five sources if you want to add one.

## The weekly loop

0. Import tab → **Run LinkedIn sync** (volume, heuristic scoring).
1. Open the tracker → **Agent brief** → the dedupe list is copied to your clipboard.
2. Paste it at the bottom of `RESEARCH_PROMPT.md`, run that prompt in a deep-research agent.
3. Copy the JSON it returns → **Import** tab → paste → Import.
4. Work the **Follow-ups** tab. Update statuses as you go.
5. Monthly: read the **Funnel** tab and kill whichever archetype converts at zero.

## Tabs

| Tab | What it is for |
|---|---|
| **Pipeline** | Every posting, scored. Click a row to see the sub-score breakdown, log a referral path, change status. Full-text search over titles, companies and JD text (SQLite FTS5). |
| **Follow-ups** | Applications sitting at `Applied` for 10+ days, and 21+ days (likely dead). Replaces the Apps Script email. |
| **Keyword gaps** | `yes_but_unstated` terms first — things you have that your CV doesn't say. Then real gaps you cannot claim. Then everything seen. |
| **Funnel** | Application-to-screen conversion **by archetype**. The reason to keep the tracker at all. |
| **Target companies** | Standing watchlist. The agent checks these every run whether or not they are hiring. |
| **Run log** | Per-run counts and which sources returned nothing — so a silently broken source becomes visible. |
| **Import** | Paste the agent's JSON. Reports what was added, deduped and auto-rejected. |

## Scoring

`fit_score` (out of 100) × location multiplier = `final_score`.

| Dimension | Max | Rule |
|---|---|---|
| Archetype match | 25 | direct 25 · adjacent 15 · tangential 5 |
| Domain overlap | 20 | 2+ hard domains 20 · one 12 · platform/AI only 8 · unrelated 0 |
| Seniority fit | 15 | asks 10-15y → 15 · 8-10y → 12 · 15y+ → 8 · under 8y → 3 |
| Unmet mandatories | 15 | none 15 · one 8 · two 3 · three+ 0 |
| Company stage | 10 | seed–Series C or hardware product org 10 · large product co 7 · services 3 |
| Compensation | 10 | at/above target 10 · above floor 6 · not stated 5 · below floor 0 + auto-reject |
| Location tier | 5 | T1 5 · T2 4 · T3/T4 3 · T5/T6 2 |

Multipliers: Hyderabad/Remote 1.00 · Bangalore 0.90 · UAE 0.85 · Mumbai/NCR and Singapore
0.80 · Oman 0.75 · anywhere else auto-rejected.

**A calibration warning.** These bands are generous — a clean archetype match in Hyderabad
lands at 100, and most genuinely-relevant postings clear 80. After ~20 rows, if more than a
third are landing in `Strong`, raise the thresholds in `config.json` (try Strong 88, Worth
a shot 76, Stretch 62) and click **Re-score all**. A tracker full of inflated Strongs is
worse than an empty one.

Two harshness guards on top of the rubric: **3+ unmet mandatories forces `Do not apply`**
whatever the score (`unmet_mandatories_hard_cap`), and a remote role limited to the US,
UK, EU etc. is auto-rejected (`remote_restricted_tokens`). A named city beats a remote
flag: "Singapore, Remote" is scored as Singapore.

Auto-rejected rows are stored, not discarded — they stay in the run log and are hidden
behind the "show auto-rejected" checkbox, so you can see what the filter threw away.

## Tuning without touching code

Everything lives in `config.json`: location tiers and multipliers, comp floors per
currency, band thresholds, scoring weights, freshness window, follow-up and stale
thresholds, and your resume keyword list. Edit, then **Re-score all** in the UI (which
reloads config and re-evaluates every stored row).

Freshness is only applied at import. Re-scoring never retroactively rejects a posting for
having aged.

## Deduplication

Key is normalised company + normalised title: legal suffixes (Pvt, Ltd, GmbH, Inc),
seniority words (Senior, Lead, Principal, Staff), bracketed suffixes and roman numerals
are all stripped. "Example Robotics Pvt Ltd — Head of Product" and "Example Robotics —
Head of Product (Robotics)" collapse to one row. Re-importing the same run is safe and
idempotent; existing rows keep their status.

## API

Everything the UI does is a plain HTTP endpoint, so you can script against it — including
from the MCP orchestrator you are building.

```
POST   /api/import               the one endpoint the research agent writes to
GET    /api/jobs                 ?status= &band= &archetype= &q= &min_score= &include_rejected=
GET    /api/jobs/{id}            single posting + status history
PATCH  /api/jobs/{id}            status, date_applied, notes, referral_path, …
POST   /api/jobs                 manual add (defaults source to "Inbound")
DELETE /api/jobs/{id}
POST   /api/rescore              re-apply config.json to every row
GET    /api/alerts               follow-up and stale queues
GET    /api/analytics/funnel     conversion by archetype
GET    /api/analytics/keywords   coverage score, quick wins, real gaps
GET    /api/targets  POST /api/targets  DELETE /api/targets/{id}
GET    /api/runs
GET    /api/agent-brief          plain-text dedupe list for the research prompt
GET    /api/export.csv           Sheets-ready, if you ever want the Apps Script back
GET    /api/export.json          full backup
GET    /api/apply-list           ?include_stretch=
POST   /api/jobs/{id}/resume     tailored LaTeX + coverage report
GET    /api/jobs/{id}/resume.tex / resume.pdf
GET    /docs                     interactive OpenAPI docs (FastAPI built-in)
```

## Files

```
config.json           all tuning. edit this, not the code
RESEARCH_PROMPT.md    the deep-research agent prompt
sample_run.json       fake payload for smoke-testing the import
app/scoring.py        the rubric
app/linkedin.py       Apify client, LinkedIn enrichment, CLI
app/resume.py         role-tailored LaTeX resume generator
resume/master.json    your master resume - the only source of resume text
imports/              verified postings, auto-loaded on first run
.env                  APIFY_TOKEN (never committed, never zipped)
app/db.py             schema + migrations
app/main.py           routes
app/static/           the UI (vanilla JS, no build step)
data/jobtrack.db      your data. back this up.
```

## Backing up

`data/jobtrack.db` is the whole thing. Copy it, or use `/api/export.json`. It is a
standard SQLite file — open it in any SQLite browser, query it from Python, point a
notebook at it.

## Log inbound recruiters

Recruiters emailing you unprompted is warm pipeline. Add them via `POST /api/jobs` (source
defaults to `Inbound`) so they show up in the funnel alongside cold applications. After
20-odd rows you will be able to see whether inbound converts better than boards — it
usually does, by a wide margin.
