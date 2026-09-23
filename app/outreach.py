"""Recruiter outreach: find a contact address, compose the note, draft or send it.

Three separate jobs, deliberately kept apart:

1. **Discovery** - `harvest_from_descriptions()` scans job descriptions already in the database
   for a contact address. It is free and instant. Paid enrichment (Instahyre's `enrichEmails`,
   Naukri's detailed mode) can fill `recruiter_email` too; nothing here depends on where the
   address came from, only that `email_source` records it.

2. **Composition** - `compose()` turns a posting into a real email: subject, plain-text body
   built from the same tailored cover-letter paragraphs the PDF uses, and the tailored resume
   PDF attached. No new claims, same as everywhere else in this app.

3. **Delivery** - Gmail, over your own account, using an app password in `.env`
   (`GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`). Two modes:
     - `save_draft()` puts it in Gmail Drafts over IMAP. Nothing is sent; you press send in
       Gmail after reading it. This is the default the UI offers.
     - `send()` sends over SMTP, and requires `confirm=True` from the caller.

   There is no send-to-everyone call here on purpose. A blast to scraped addresses burns the
   sender's domain reputation, trips Gmail's ~500/day limit, and reads as spam to the exact
   people being asked for a job. One posting at a time, each one read first.
"""
from __future__ import annotations

import base64
import imaplib
import json
import os
import re
import smtplib
import ssl
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from . import cover_letter, resume

ROOT = Path(__file__).resolve().parent.parent

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

# Addresses that are never a person to write to about a job.
_JUNK_EMAIL = re.compile(
    r"@(example|test|sentry|wixpress|sentry\.io)\b|"
    r"^(no-?reply|donotreply|privacy|dpo|legal|abuse|postmaster|webmaster|support|billing)@|"
    r"\.(png|jpe?g|gif|svg|webp)$", re.I)


def emails_in_text(text: str) -> list[str]:
    seen, out = set(), []
    for e in EMAIL_RE.findall(text or ""):
        e = e.strip(" .,;:)")
        if _JUNK_EMAIL.search(e) or e.lower() in seen:
            continue
        seen.add(e.lower())
        out.append(e)
    return out


def harvest_from_descriptions(conn) -> dict:
    """Populate recruiter_email from JD text already stored. Free; run it whenever."""
    rows = conn.execute(
        "SELECT id, description FROM jobs "
        "WHERE (recruiter_email IS NULL OR recruiter_email='') AND description IS NOT NULL"
    ).fetchall()
    found = 0
    for r in rows:
        hits = emails_in_text(r["description"])
        if hits:
            conn.execute(
                "UPDATE jobs SET recruiter_email=?, email_source='job description',"
                " updated_at=datetime('now') WHERE id=?", (hits[0], r["id"]))
            found += 1
    conn.commit()
    return {"scanned": len(rows), "found": found}


# ---------------------------------------------------------------- composition

_DELATEX = [
    (r"\textperiodcentered{}", "\u00b7"), (r"\ldots{}", "\u2026"),
    ("---", "\u2014"), ("--", "\u2013"),
    (r"\&", "&"), (r"\%", "%"), (r"\_", "_"), (r"\#", "#"), (r"\$", "$"),
]


def _plain(s: str) -> str:
    """The letter paragraphs are LaTeX-safe; email bodies are not LaTeX."""
    s = re.sub(r"\\text(?:bf|it)\{([^{}]*)\}", lambda m: m.group(1) or "", s)
    for a, b in _DELATEX:
        s = s.replace(a, b)
    return re.sub(r"[ \t]+", " ", s).strip()


def compose(job: dict, attach_cover_letter: bool = False) -> dict:
    """Subject, body and attachments for this posting. Raises if LaTeX can't build the PDF."""
    t = cover_letter.tailor(job)
    basics = t["basics"]
    role = (job.get("role_title") or "").strip()
    company = (job.get("company") or "").strip()

    greeting = f"Dear {job['recruiter_name'].strip()}," if job.get("recruiter_name") else "Dear Hiring Team,"
    paragraphs = [_plain(p) for p in t["letter_paragraphs"]]
    links = [basics["phone"], basics["email"]]
    links += [basics[k] for k in ("website", "github") if basics.get(k)]
    sign_off = "\n".join(["Best regards,", basics["name"], " \u00b7 ".join(links)])
    body = "\n\n".join([greeting, *paragraphs,
                        "My CV is attached. I would welcome the chance to discuss the role.",
                        sign_off])

    attachments = []
    r = resume.build(job, save=False)
    attachments.append((_attachment_name(job, "Resume"), resume.compile_pdf(r["tex"])))
    if attach_cover_letter:
        cl = cover_letter.build(job, save=False)
        attachments.append((_attachment_name(job, "CoverLetter"), resume.compile_pdf(cl["tex"])))

    return {
        "to": job.get("recruiter_email"),
        "subject": f"{role} - application from {basics['name']}" if role else f"Application - {basics['name']}",
        "body": body,
        "attachments": attachments,
        "company": company,
    }


def _attachment_name(job: dict, kind: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (job.get("company") or "")).strip("_")[:30]
    return f"{resume.name_slug()}_{kind}{'_' + slug if slug else ''}.pdf"


def build_message(draft: dict, from_addr: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = draft["to"]
    msg["Subject"] = draft["subject"]
    msg.set_content(draft["body"])
    for name, data in draft["attachments"]:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return msg


# ---------------------------------------------------------------- outbox (Claude's Gmail connector)

# The Claude session has an authenticated Gmail connector, but it lives in Claude - this app is
# a separate local process and cannot call it. So the app writes a composed message here and
# Claude picks it up and delivers it through the connector. That keeps the button in the app
# without needing an app password, at the cost of Claude being the transport.
OUTBOX = ROOT / "data" / "outbox"


def queue(job: dict, draft: dict) -> Path:
    OUTBOX.mkdir(parents=True, exist_ok=True)
    payload = {
        "job_id": job.get("id"),
        "company": job.get("company"),
        "role_title": job.get("role_title"),
        "to": draft["to"],
        "subject": draft["subject"],
        "body": draft["body"],
        "attachments": [{"filename": name, "mimeType": "application/pdf",
                         "content": base64.b64encode(data).decode()}
                        for name, data in draft["attachments"]],
        "queued_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path = OUTBOX / f"job-{job.get('id')}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def outbox_items() -> list[dict]:
    """Queued messages, without the base64 payloads (those are big)."""
    if not OUTBOX.exists():
        return []
    out = []
    for p in sorted(OUTBOX.glob("job-*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"file": p.name, "job_id": d.get("job_id"), "company": d.get("company"),
                    "role_title": d.get("role_title"), "to": d.get("to"),
                    "subject": d.get("subject"), "queued_at": d.get("queued_at"),
                    "attachments": [a.get("filename") for a in d.get("attachments", [])]})
    return out


# ---------------------------------------------------------------- delivery (your Gmail)

def gmail_credentials() -> tuple[str, str] | None:
    """(address, app password) from the environment or .env - never stored anywhere else."""
    env = {}
    f = ROOT / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    addr = os.environ.get("GMAIL_ADDRESS") or env.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD") or env.get("GMAIL_APP_PASSWORD")
    if addr and pw:
        return addr, pw.replace(" ", "")  # Google prints app passwords in groups of four
    return None


def save_draft(draft: dict) -> dict:
    """Put the message in Gmail Drafts. Nothing is sent."""
    creds = gmail_credentials()
    if not creds:
        raise RuntimeError("No Gmail credentials. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to .env")
    addr, pw = creds
    msg = build_message(draft, addr)
    with imaplib.IMAP4_SSL("imap.gmail.com", ssl_context=ssl.create_default_context()) as im:
        im.login(addr, pw)
        folder = _drafts_folder(im)
        status, _ = im.append(folder, r"\Draft", imaplib.Time2Internaldate(time.time()),
                              msg.as_bytes())
    if status != "OK":
        raise RuntimeError(f"Gmail rejected the draft (IMAP said {status})")
    return {"drafted": True, "to": draft["to"], "folder": folder}


def _drafts_folder(im: imaplib.IMAP4_SSL) -> str:
    """Gmail localises this folder, so find the one flagged \\Drafts rather than guessing."""
    status, boxes = im.list()
    if status == "OK":
        for raw in boxes:
            line = raw.decode(errors="replace")
            if "\\Drafts" in line:
                return '"' + line.split(' "/" ')[-1].strip().strip('"') + '"'
    return '"[Gmail]/Drafts"'


def send(draft: dict, confirm: bool = False) -> dict:
    """Actually send. `confirm` must be passed explicitly by the caller."""
    if not confirm:
        raise RuntimeError("send() needs confirm=True - this puts mail in someone's inbox")
    creds = gmail_credentials()
    if not creds:
        raise RuntimeError("No Gmail credentials. Add GMAIL_ADDRESS and GMAIL_APP_PASSWORD to .env")
    addr, pw = creds
    msg = build_message(draft, addr)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(addr, pw)
        s.send_message(msg)
    return {"sent": True, "to": draft["to"], "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
