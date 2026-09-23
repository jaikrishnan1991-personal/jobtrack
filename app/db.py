"""SQLite storage. stdlib sqlite3 only - no ORM, no migrations framework."""
import sqlite3
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.environ.get("JOBTRACK_DB", ROOT / "data" / "jobtrack.db"))

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id            TEXT UNIQUE,              -- YYYYMMDD-###
    dedupe_key        TEXT UNIQUE NOT NULL,     -- normalised company::title
    date_found        TEXT NOT NULL,
    date_posted       TEXT,
    date_confidence   TEXT DEFAULT 'Unverified',-- Verified / Unverified
    company           TEXT NOT NULL,
    role_title        TEXT NOT NULL,
    archetype         TEXT,                     -- A..E
    archetype_match   TEXT,                     -- direct / adjacent / tangential
    location          TEXT,
    location_tier     INTEGER,
    work_mode         TEXT,                     -- Onsite / Hybrid / Remote
    experience_asked  TEXT,
    exp_min           REAL,
    exp_max           REAL,
    comp_raw          TEXT,
    comp_currency     TEXT,
    comp_min          REAL,
    comp_max          REAL,
    comp_confidence   TEXT DEFAULT 'Not stated',-- Stated / Estimated / Not stated
    company_stage     TEXT,                     -- seed/series_a.../large_product/services
    domains           TEXT,                     -- json array
    unmet_mandatories TEXT,                     -- json array
    missing_keywords  TEXT,                     -- json array
    description       TEXT,
    -- scoring (computed server-side)
    s_archetype       REAL, s_domain REAL, s_seniority REAL, s_gaps REAL,
    s_stage           REAL, s_comp REAL, s_location REAL,
    fit_score         REAL,
    location_multiplier REAL,
    final_score       REAL,
    band              TEXT,
    why_this_score    TEXT,
    -- workflow
    apply_url         TEXT,
    source            TEXT,
    status            TEXT DEFAULT 'Not Applied',
    date_applied      TEXT,
    referral_path     TEXT,
    notes             TEXT,
    run_id            INTEGER,
    rejected_reason   TEXT,                     -- set when filtered out pre-insert (kept for audit)
    created_at        TEXT DEFAULT (datetime('now')),
    updated_at        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_band   ON jobs(band);
CREATE INDEX IF NOT EXISTS idx_jobs_arch   ON jobs(archetype);

CREATE TABLE IF NOT EXISTS status_history (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_db_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    old_status TEXT,
    new_status TEXT,
    changed_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    agent           TEXT,
    sources_checked TEXT,
    sources_empty   TEXT,
    scanned         INTEGER DEFAULT 0,
    passed_filter   INTEGER DEFAULT 0,
    added           INTEGER DEFAULT 0,
    duplicates      INTEGER DEFAULT 0,
    rejected        INTEGER DEFAULT 0,
    notes           TEXT,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS keywords (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    run_date      TEXT,
    term          TEXT NOT NULL,
    category      TEXT,
    frequency     INTEGER DEFAULT 0,
    pct_postings  REAL,
    criticality   TEXT,
    archetypes    TEXT,
    can_i_claim_it TEXT,      -- yes / yes_but_unstated / no
    on_my_resume  INTEGER DEFAULT 0,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_kw_term ON keywords(term);

CREATE TABLE IF NOT EXISTS target_companies (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company       TEXT UNIQUE NOT NULL,
    why           TEXT,
    careers_url   TEXT,
    region        TEXT,
    last_checked  TEXT,
    open_roles_found INTEGER DEFAULT 0,
    notes         TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(
    company, role_title, description, notes, content='jobs', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS jobs_ai AFTER INSERT ON jobs BEGIN
  INSERT INTO jobs_fts(rowid, company, role_title, description, notes)
  VALUES (new.id, new.company, new.role_title, coalesce(new.description,''), coalesce(new.notes,''));
END;
CREATE TRIGGER IF NOT EXISTS jobs_ad AFTER DELETE ON jobs BEGIN
  INSERT INTO jobs_fts(jobs_fts, rowid, company, role_title, description, notes)
  VALUES('delete', old.id, old.company, old.role_title, coalesce(old.description,''), coalesce(old.notes,''));
END;
CREATE TRIGGER IF NOT EXISTS jobs_au AFTER UPDATE ON jobs BEGIN
  INSERT INTO jobs_fts(jobs_fts, rowid, company, role_title, description, notes)
  VALUES('delete', old.id, old.company, old.role_title, coalesce(old.description,''), coalesce(old.notes,''));
  INSERT INTO jobs_fts(rowid, company, role_title, description, notes)
  VALUES (new.id, new.company, new.role_title, coalesce(new.description,''), coalesce(new.notes,''));
END;
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# columns added after the first release; applied on every startup
MIGRATIONS = [
    ("jobs", "archetype_match", "TEXT"),
    ("jobs", "s_affinity", "REAL"),
    ("jobs", "affinity", "REAL"),
    ("jobs", "favourite", "INTEGER DEFAULT 0"),
    ("jobs", "favourite_terms", "TEXT"),
    ("jobs", "recruiter_email", "TEXT"),
    ("jobs", "recruiter_name", "TEXT"),
    ("jobs", "email_source", "TEXT"),
    ("jobs", "email_status", "TEXT"),       # drafted | sent
    ("jobs", "email_sent_at", "TEXT"),
]


def init_db() -> None:
    conn = connect()
    with conn:
        conn.executescript(SCHEMA)
        for table, col, coltype in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("domains", "unmet_mandatories", "missing_keywords", "archetypes",
              "sources_checked", "sources_empty"):
        if k in d and isinstance(d[k], str) and d[k]:
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d
