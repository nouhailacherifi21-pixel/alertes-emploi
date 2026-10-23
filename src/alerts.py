"""Détecte les nouvelles offres LinkedIn correspondant au profil et les envoie par email.

Chaque annonce est lue et jugée par Claude Haiku (missions, expérience, contrat, lieu, niveau),
comme pour l'automatisation de candidature d'origine.

Usage :
    python -m src.alerts            # un passage (GitHub Actions)
    python -m src.alerts --dry-run  # affiche les offres retenues sans envoyer d'email
"""

from __future__ import annotations

import argparse
import html
import json
import os
import smtplib
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path

import anthropic
import yaml

from . import linkedin

ROOT = Path(__file__).resolve().parent.parent
SEEN_FILE = ROOT / "data" / "seen.json"
KEEP_DAYS = 45  # offres mémorisées (pour ne jamais renvoyer deux fois la même)


# ── Tri par Claude ────────────────────────────────────────────

_STR = {"type": "string"}


def _obj(**props) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


SCREEN_SCHEMA = _obj(
    relevant={"type": "boolean"},
    domains={"type": "array", "items": _STR},
    level_ok={"type": "boolean"},
    experience_required=_STR,
    experience_ok={"type": "boolean"},
    contract=_STR,
    contract_ok={"type": "boolean"},
    location_ok={"type": "boolean"},
    fit_score={"type": "integer"},
    reason=_STR,
)

SCREEN_SYSTEM = """Tu tries des offres d'emploi pour une candidate. Juge l'offre sur ses MISSIONS,
responsabilités et compétences demandées, jamais sur son seul intitulé : un titre inhabituel
peut cacher un poste pertinent, et inversement.

"relevant" = true si les missions principales relèvent d'au moins un de ces domaines, ou
d'un domaine proche :
{domains}

"domains" : les domaines ci-dessus effectivement couverts par les missions.

"level_ok" = true pour un poste d'ingénieur(e), de cadre, de chargé(e) d'études ou de mission,
de chimiste ; false pour un poste de technicien(ne), laborantin(e), agent, opérateur ou assistant.

Expérience : "experience_required" recopie ce que l'offre demande ("" si rien n'est dit).
"experience_ok" = true seulement si TOUTE l'expérience demandée reste à 3 ans ou moins :
débutant, junior, jeune diplômé, 0-1 an, 1-2 ans, 2-3 ans, 3 ans → true ;
une fourchette qui dépasse 3 ans (« 3 à 5 ans », « 2 à 4 ans ») → false ;
« 4 ans et plus », « 5 ans minimum » → false ; « senior », « expérimenté » ou « confirmé »
sans durée, ou management d'équipe → false ; rien de précisé → true.

Contrat : "contract" recopie le type de contrat indiqué ("" si absent).
"contract_ok" = true pour un CDI ou si non précisé ; false pour un CDD, intérim, stage,
alternance, freelance ou temps partiel.

"location_ok" = true si le poste est basé à Paris ou en Île-de-France (75, 77, 78, 91, 92,
93, 94, 95) ; false pour toute autre ville ou région, ou un autre pays.

"fit_score" (0-100) : adéquation entre le profil de la candidate (<profil>) et l'offre —
missions, compétences, secteur, niveau. 80+ = excellente, 60-79 = bonne, moins de 60 =
moyenne. Sois exigeant et différencie bien les offres.

"reason" : une phrase qui justifie la décision."""


def screen(client: anthropic.Anthropic, job: linkedin.Job, cfg: dict) -> dict:
    system = SCREEN_SYSTEM.format(domains="\n".join(f"- {d}" for d in cfg["domains"]))
    if cfg.get("preferences"):
        system += f"\n\nPréférences de la candidate, à respecter :\n{cfg['preferences']}"
    user = (f"<profil>\n{cfg.get('profile', '')}\n</profil>\n\n<offre>\nPoste : {job.title}\n"
            f"Entreprise : {job.company}\nLieu : {job.location}\n\n"
            f"{job.description or '(description indisponible : juge sur le titre)'}\n</offre>")
    with client.messages.stream(
        model=cfg.get("screen_model", "claude-haiku-5-5"),
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCREEN_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"tri interrompu ({response.stop_reason})")
    return json.loads(next(b.text for b in response.content if b.type == "text"))


def accepted(verdict: dict) -> bool:
    return all(verdict[k] for k in ("relevant", "level_ok", "experience_ok", "contract_ok", "location_ok"))


# ── Filtres simples ───────────────────────────────────────────

def _has_term(title_words: list[str], term: str) -> bool:
    """Le terme (« technicien* » = préfixe, « head of » = suite de mots) est-il dans le titre ?"""
    prefix = term.endswith("*")
    term = linkedin.normalize(term.rstrip("*"))
    if prefix:
        return any(w.startswith(term) for w in title_words)
    return f" {term} " in f" {' '.join(title_words)} "


def title_excluded(title: str, cfg: dict) -> bool:
    words = linkedin.normalize(title).split()
    return any(_has_term(words, t) for t in cfg.get("title_excluded", []))


def offer_key(job: linkedin.Job) -> str:
    """Clé de doublon : intitulé + premier mot de l'entreprise (« X France » = « X Group »)."""
    company = linkedin.normalize(job.company).split()
    return f"{linkedin.normalize(job.title)}|{company[0] if company else ''}"


# ── Email ─────────────────────────────────────────────────────

def send_offer(job: linkedin.Job, verdict: dict) -> None:
    """Email volontairement sobre (objet sans émoji, un seul lien) pour éviter le dossier spam."""
    sender, to = os.environ["GMAIL_ADDRESS"].strip(), os.environ["NOTIFY_EMAIL"].strip()
    excerpt = job.description[:600].rsplit(" ", 1)[0] + "…" if len(job.description) > 600 else job.description
    fit = verdict.get("fit_score")
    experience = verdict.get("experience_required") or "non précisée"
    contract = verdict.get("contract") or "non précisé"
    lines = [
        f"{job.title}",
        f"{job.company} - {job.location}",
        "",
        *( [f"Adéquation avec ton profil : {fit}/100"] if fit is not None else [] ),
        *( [f"Avis : {verdict['reason']}"] if verdict.get("reason") else [] ),
        f"Expérience demandée : {experience}",
        f"Contrat : {contract}",
        "",
        f"Voir l'offre : {job.url}",
        "",
        excerpt,
    ]
    e = html.escape
    msg = EmailMessage()
    msg["From"] = formataddr(("Alertes emploi", sender))
    msg["To"] = to
    msg["Subject"] = f"Nouvelle offre : {job.title} - {job.company}"
    msg["Message-ID"] = make_msgid()
    msg.set_content("\n".join(lines))
    body = "".join(f"<p style=\"margin:0 0 6px\">{e(l)}</p>" if l else "<br>" for l in lines[:-3])
    msg.add_alternative(f"""<div style="font-family:Arial,sans-serif;font-size:14px;color:#222">{body}
<p><a href="{e(job.url)}">Voir l'offre sur LinkedIn</a></p>
<p style="color:#555">{e(excerpt)}</p></div>""", subtype="html")
    _send(msg)


def mask(address: str) -> str:
    """« najlaaee29@gmail.com » → « naj…29@gmail.com (20 car.) » : vérifiable sans être exposée."""
    local, _, domain = address.strip().partition("@")
    return f"{local[:3]}…{local[-2:]}@{domain} ({len(address)} car.{' ⚠️ espaces' if address != address.strip() else ''})"


def _send(msg: EmailMessage) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(os.environ["GMAIL_ADDRESS"].strip(), os.environ["GMAIL_APP_PASSWORD"])
        refused = smtp.send_message(msg)
    print(f"   ✉️  envoyé à {mask(msg['To'])}" + (f" — REFUSÉ : {refused}" if refused else " — accepté par Gmail"))


def send_test_email() -> None:
    """Email de test à la destinataire, avec copie à l'expéditrice."""
    sender, to = os.environ["GMAIL_ADDRESS"].strip(), os.environ["NOTIFY_EMAIL"].strip()
    print(f"Expéditrice : {mask(sender)}\nDestinataire : {mask(os.environ['NOTIFY_EMAIL'])}")
    for dest, note in ((to, ""), (sender, "\n\n(Copie de contrôle envoyée à l'expéditrice.)")):
        msg = EmailMessage()
        msg["From"] = formataddr(("Alertes emploi", sender))
        msg["To"] = dest
        msg["Subject"] = "✅ Test — Alertes emploi"
        msg["Message-ID"] = make_msgid()
        msg.set_content("Ceci est un email de test : les alertes emploi LinkedIn arriveront à cette adresse "
                        f"({to}).{note}")
        _send(msg)


def resend_sent(hours: int = 48) -> None:
    """Renvoie les offres déjà envoyées ces dernières heures (relues sur LinkedIn)."""
    seen = load_seen()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    for job_id, v in seen["jobs"].items():
        if v["status"] == "sent" and v.get("at", "") >= cutoff:
            job = linkedin.fetch_details(linkedin.new_job(job_id, v.get("title", "")))
            job.company = job.company or v.get("company", "")
            print(f"↻ {job.title} — {job.company}")
            send_offer(job, v)


# ── Passage ───────────────────────────────────────────────────

def load_seen() -> dict:
    seen = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else {"jobs": {}}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat()
    seen["jobs"] = {k: v for k, v in seen["jobs"].items() if v.get("at", "") >= cutoff}
    return seen


def run(dry_run: bool = False) -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    if os.environ.get("MAX_AGE_MINUTES", "").strip():  # test ponctuel depuis « Run workflow »
        cfg["search_max_age_minutes"] = cfg["max_post_age_minutes"] = int(os.environ["MAX_AGE_MINUTES"])
    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def mark(job: linkedin.Job, status: str, **extra) -> None:
        seen["jobs"][job.id] = {"status": status, "title": job.title, "company": job.company,
                                "url": job.url, "key": offer_key(job), "at": now, **extra}

    # 1. Recherche des offres récentes.
    candidates: dict[str, linkedin.Job] = {}
    for search in cfg["searches"]:
        found = linkedin.search_recent_jobs(search, cfg["search_max_age_minutes"], cfg.get("max_pages", 2))
        print(f"🔎 {search['keywords'][:60]}… : {len(found)} offre(s)")
        for job in found:
            if job.id not in seen["jobs"]:
                candidates.setdefault(job.id, job)
    print(f"🆕 {len(candidates)} offre(s) jamais vue(s)")

    # 2. Lecture des annonces (une par une, pour ménager LinkedIn) et filtres simples.
    known_keys = {v.get("key") for v in seen["jobs"].values()}
    to_screen: list[linkedin.Job] = []
    fetched = 0
    for job in candidates.values():
        if linkedin.rate_limited:
            print("⏸️  LinkedIn limite les requêtes : la suite au prochain passage.")
            break
        if title_excluded(job.title, cfg):
            mark(job, "title_excluded")
            continue
        if fetched >= cfg.get("max_fetch_per_run", 25):
            print("⏸️  Limite d'annonces lues par passage atteinte : la suite au prochain passage.")
            break
        fetched += 1
        job = linkedin.fetch_details(job)
        if not job.description:
            continue  # page illisible (limite passagère) : nouvel essai au prochain passage
        if title_excluded(job.title, cfg):
            mark(job, "title_excluded")
        elif job.age_days is not None and job.age_days * 1440 > cfg.get("max_post_age_minutes", 60):
            mark(job, "too_old")
        elif job.company and offer_key(job) in known_keys:
            mark(job, "duplicate")
        else:
            known_keys.add(offer_key(job))
            to_screen.append(job)

    # 3. Tri par Claude, en parallèle.
    client = anthropic.Anthropic()

    def run_screen(job: linkedin.Job):
        try:
            return job, screen(client, job, cfg), None
        except (anthropic.APIError, RuntimeError, json.JSONDecodeError, KeyError) as exc:
            return job, None, exc

    with ThreadPoolExecutor(max_workers=cfg.get("screen_workers", 6)) as pool:
        results = list(pool.map(run_screen, to_screen))

    # 4. Envoi des offres retenues, les mieux notées d'abord.
    sent = 0
    keep = []
    for job, verdict, exc in results:
        if exc:  # pas mémorisée : nouvel essai au prochain passage
            print(f"⚠️  Tri impossible pour {job.title} ({exc!r})")
            continue
        print(f"{'✅' if accepted(verdict) else '✖️ '} {job.title} — {job.company} : {verdict['reason']}")
        if accepted(verdict):
            keep.append((job, verdict))
        else:
            mark(job, "screened_out", reason=verdict["reason"])
    for job, verdict in sorted(keep, key=lambda kv: kv[1].get("fit_score", 0), reverse=True):
        if not dry_run:
            send_offer(job, verdict)
        mark(job, "sent", fit_score=verdict.get("fit_score"), reason=verdict["reason"],
             experience_required=verdict.get("experience_required", ""), contract=verdict.get("contract", ""))
        sent += 1
    print(f"📧 {sent} offre(s) envoyée(s)")
    if not dry_run:
        SEEN_FILE.write_text(json.dumps(seen, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="n'envoie aucun email")
    parser.add_argument("--test-email", action="store_true", help="envoie seulement un email de test")
    parser.add_argument("--resend", action="store_true", help="renvoie les offres envoyées ces 48 dernières heures")
    args = parser.parse_args()
    if args.test_email:
        send_test_email()
    elif args.resend:
        resend_sent()
    else:
        run(args.dry_run)
