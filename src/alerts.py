"""Détecte les nouvelles offres LinkedIn correspondant au profil et les envoie par email.

Usage :
    python -m src.alerts            # un passage (GitHub Actions)
    python -m src.alerts --dry-run  # affiche les offres retenues sans envoyer d'email
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import yaml

from . import linkedin

ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = ROOT / "data" / "seen.json"
KEEP_DAYS = 45  # offres mémorisées (pour ne jamais renvoyer deux fois la même)


# ── Filtres ───────────────────────────────────────────────────

def _has_term(title_words: list[str], term: str) -> bool:
    """Le terme (« chimi* » = préfixe, « r d » = suite de mots) apparaît-il dans le titre ?"""
    term = linkedin.normalize(term.rstrip("*")) + ("*" if term.endswith("*") else "")
    if term.endswith("*"):
        return any(w.startswith(term[:-1]) for w in title_words)
    return f" {term} " in f" {' '.join(title_words)} "


def title_matches(title: str, cfg: dict) -> bool:
    words = linkedin.normalize(title).split()
    if any(_has_term(words, t) for t in cfg["title_excluded"]):
        return False
    return (any(_has_term(words, t) for t in cfg["title_roles"])
            and any(_has_term(words, t) for t in cfg["title_domains"]))


_YEARS = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:à|a|-|–|to)\s*(\d{1,2})\s*)?(?:ans?|années?|years?)\b", re.IGNORECASE)
_EXP = re.compile(r"exp[ée]rience|experience|\bexp\b", re.IGNORECASE)


def experience_required(description: str) -> tuple[int | None, str]:
    """Années d'expérience demandées, en retenant la borne HAUTE d'une fourchette
    (« 3 à 5 ans » → 5) ; None si non précisé. Retourne aussi l'extrait concerné."""
    found = []
    for m in _YEARS.finditer(description):
        window = description[max(0, m.start() - 60):m.end() + 60]
        if _EXP.search(window):
            years = int(m.group(2) or m.group(1))
            if 0 < years < 25:
                start = description.rfind(".", 0, m.start()) + 1
                found.append((years, description[start:m.end() + 40].strip().split("\n")[0]))
    return min(found) if found else (None, "")


def temporary_contract(description: str) -> bool:
    text = linkedin.normalize(description)
    temp = re.search(r"\b(cdd|interim|mission temporaire|contrat a duree determinee)\b", text)
    return bool(temp) and not re.search(r"\b(cdi|contrat a duree indeterminee)\b", text)


# ── Email ─────────────────────────────────────────────────────

def send_offer(job: linkedin.Job, experience: str) -> None:
    sender, to = os.environ["GMAIL_ADDRESS"], os.environ["NOTIFY_EMAIL"]
    excerpt = job.description[:700].rsplit(" ", 1)[0] + "…" if len(job.description) > 700 else job.description
    published = (f"il y a {round(job.age_days * 1440)} min" if job.age_days is not None and job.age_days < 1 / 24
                 else "dans la dernière heure")
    e = html.escape
    msg = EmailMessage()
    msg["From"] = formataddr(("Alertes emploi", sender))
    msg["To"] = to
    msg["Subject"] = f"🧪 {job.title} — {job.company}"
    msg["Message-ID"] = make_msgid()
    msg.set_content(f"""Nouvelle offre : {job.title}
Entreprise : {job.company}
Lieu : {job.location}
Publiée : {published}
Expérience demandée : {experience or "non précisée"}

Postuler : {job.url}

{excerpt}
""")
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#1f2933;max-width:620px">
<h2 style="margin:0 0 4px;color:#0b6e69">{e(job.title)}</h2>
<div style="color:#52606d">{e(job.company)} · {e(job.location)} · publiée {e(published)}</div>
<p><b>Expérience demandée :</b> {e(experience or "non précisée")}</p>
<p><a href="{e(job.url)}" style="display:inline-block;background:#0b6e69;color:#fff;padding:10px 18px;
border-radius:6px;text-decoration:none;font-weight:bold">Voir l'offre et postuler →</a></p>
<p style="color:#52606d;white-space:pre-line">{e(excerpt)}</p></div>""", subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(sender, os.environ["GMAIL_APP_PASSWORD"])
        smtp.send_message(msg)


# ── Passage ───────────────────────────────────────────────────

def load_seen() -> dict:
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {"jobs": {}}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    seen["jobs"] = {k: v for k, v in seen["jobs"].items() if v.get("at", "") >= cutoff}
    return seen


def run(dry_run: bool = False) -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def mark(job: linkedin.Job, status: str, **extra) -> None:
        seen["jobs"][job.id] = {"status": status, "title": job.title, "company": job.company, "at": now, **extra}

    candidates: dict[str, linkedin.Job] = {}
    for search in cfg["searches"]:
        found = linkedin.search_recent_jobs(search, cfg["search_max_age_minutes"], cfg.get("max_pages", 2))
        print(f"🔎 {search['keywords'][:60]}… : {len(found)} offre(s)")
        for job in found:
            if job.id not in seen["jobs"]:
                candidates.setdefault(job.id, job)
    print(f"🆕 {len(candidates)} offre(s) jamais vue(s)")

    sent = 0
    for job in candidates.values():
        if linkedin.rate_limited:
            print("⏸️  LinkedIn limite les requêtes : la suite au prochain passage.")
            break
        if not title_matches(job.title, cfg):
            mark(job, "title_ignored")
            continue
        job = linkedin.fetch_details(job)
        if not title_matches(job.title, cfg):
            mark(job, "title_ignored")
            continue
        years, extract = experience_required(job.description)
        if years is not None and years > cfg.get("max_years_experience", 3):
            print(f"✖️  {job.title} — {job.company} : {extract}")
            mark(job, "too_much_experience", experience=extract)
            continue
        if cfg.get("exclude_temporary_contracts", True) and temporary_contract(job.description):
            print(f"✖️  {job.title} — {job.company} : CDD / intérim")
            mark(job, "temporary_contract")
            continue
        print(f"✅ {job.title} — {job.company} ({job.location})")
        if not dry_run:
            send_offer(job, extract)
        mark(job, "sent")
        sent += 1
    print(f"📧 {sent} offre(s) envoyée(s)")
    if not dry_run:
        SEEN_FILE.write_text(json.dumps(seen, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="n'envoie aucun email")
    run(parser.parse_args().dry_run)
