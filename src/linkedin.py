"""Détection des offres LinkedIn : recherche publique des offres récentes, emails d'alerte,
et récupération de la description.

Tout passe par les pages publiques de LinkedIn (sans connexion à votre compte) :
aucune action n'est effectuée avec votre profil LinkedIn.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import asdict, dataclass

import requests
from bs4 import BeautifulSoup

JOB_ID_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?\"']*-)?(\d{6,})")
GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PUBLIC_URL ="https://www.linkedin.com/jobs/view/{job_id}/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}


@dataclass
class Job:
    id: str
    url: str
    title: str = ""
    company: str = ""
    location: str = ""
    description: str = ""
    age_days: float | None = None  # ancienneté de l'offre, d'après « il y a 3 jours »

    def to_dict(self) -> dict:
        return asdict(self)


def extract_jobs_from_alert(html: str) -> dict[str, str]:
    """Retourne {job_id: titre indicatif} pour chaque offre citée dans l'email."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        match = JOB_ID_RE.search(a["href"])
        if not match:
            continue
        job_id = match.group(1)
        text = a.get_text(" ", strip=True)
        if text and len(text) < 150 and not jobs.get(job_id):
            jobs[job_id] = text
        else:
            jobs.setdefault(job_id, "")
    return jobs


def job_id_from_url(url: str) -> str | None:
    match = JOB_ID_RE.search(url) or re.search(r"currentJobId=(\d+)", url)
    return match.group(1) if match else None


def new_job(job_id: str, title: str = "") -> Job:
    return Job(id=job_id, url=PUBLIC_URL.format(job_id=job_id), title=title)


# Passe à True quand LinkedIn continue de limiter malgré les nouvelles tentatives :
# le passage en cours s'arrête alors et reprendra au suivant.
rate_limited = False


def _get(url: str, params: dict | None = None) -> requests.Response | None:
    global rate_limited
    if rate_limited:
        return None
    """GET poli : pause entre requêtes, nouvelles tentatives espacées si LinkedIn limite (429)."""
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=20)
        except requests.RequestException as exc:
            print(f"  ⚠️  {exc}")
            return None
        finally:
            time.sleep(4)
        if resp.status_code == 429 and attempt < 2:
            time.sleep(45 * (attempt + 1))
            continue
        if resp.ok:
            return resp
        rate_limited = resp.status_code == 429
        print(f"  ⚠️  LinkedIn a répondu {resp.status_code} pour {resp.url}")
        return None
    return None


def search_recent_jobs(search: dict, max_age_minutes: int, max_pages: int = 2) -> list[Job]:
    """Offres publiées depuis moins de `max_age_minutes`, les plus récentes d'abord.

    `search` reprend un bloc `searches` de config.yaml : keywords, location et,
    en option, geo_id (identifiant LinkedIn du lieu, plus fiable que le texte), remote (onsite/remote/hybrid), experience (internship/entry/associate/
    mid_senior/director), job_type (full_time/contract/temporary/part_time).
    """
    params = {
        "keywords": search["keywords"],
        "location": search.get("location", ""),
        "f_TPR": f"r{max_age_minutes * 60}",
        **({"geoId": str(search["geo_id"])} if search.get("geo_id") else {}),
        "sortBy": "DD",
    }
    for key, param, codes in (
        ("remote", "f_WT", {"onsite": "1", "remote": "2", "hybrid": "3"}),
        ("experience", "f_E", {"internship": "1", "entry": "2", "associate": "3",
                                "mid_senior": "4", "director": "5"}),
        ("job_type", "f_JT", {"full_time": "F", "contract": "C", "temporary": "T", "part_time": "P"}),
    ):
        values = search.get(key) or []
        values = [values] if isinstance(values, str) else values
        if values:
            params[param] = ",".join(codes[v] for v in values)

    jobs: list[Job] = []
    start, page_size = 0, None
    for _ in range(max_pages):  # LinkedIn renvoie 10 offres par page
        resp = _get(SEARCH_URL, params | {"start": start})
        if not resp:
            break
        cards = BeautifulSoup(resp.text, "html.parser").select("[data-entity-urn*=jobPosting]")
        for card in cards:
            job_id = card["data-entity-urn"].rsplit(":", 1)[-1]
            job = new_job(job_id, _text(card, ".base-search-card__title"))
            job.company = _text(card, ".base-search-card__subtitle")
            job.location = _text(card, ".job-search-card__location")
            jobs.append(job)
        page_size = page_size or len(cards)
        if not cards or len(cards) < page_size:
            break
        start += len(cards)
    return jobs


def _text(soup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else ""


def fetch_details(job: Job) -> Job:
    """Complète l'offre (titre, entreprise, lieu, description) depuis sa page publique."""
    resp = _get(GUEST_URL.format(job_id=job.id))
    if not resp:
        print(f"  ⚠️  Description indisponible pour l'offre {job.id}")
        return job
    soup = BeautifulSoup(resp.text, "html.parser")
    job.title = _text(soup, ".top-card-layout__title") or _text(soup, "h2") or job.title
    job.company = _text(soup, ".topcard__org-name-link") or _text(soup, ".topcard__flavor") or job.company
    job.location = _text(soup, ".topcard__flavor--bullet") or job.location
    job.age_days = parse_age_days(_text(soup, ".posted-time-ago__text") or _text(soup, "time"))
    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    if desc:
        job.description = desc.get_text("\n", strip=True)
    return job


# Formes féminines ramenées au masculin pour comparer les titres d'offres.
_FEMININE = {
    "chargee": "charge", "cheffe": "chef", "commerciale": "commercial",
    "consultante": "consultant", "ingenieure": "ingenieur", "responsables": "responsable",
}


_AGE_UNITS = {"minute": 1 / 1440, "heure": 1 / 24, "hour": 1 / 24, "jour": 1, "day": 1,
              "semaine": 7, "week": 7, "mois": 30, "month": 30, "an": 365, "year": 365}


def parse_age_days(text: str) -> float | None:
    """« il y a 2 semaines » / « 3 days ago » → nombre de jours (None si illisible)."""
    m = re.search(r"(\d+)\s*(minute|heure|hour|jour|day|semaine|week|mois|month|an|year)", text.lower())
    return int(m.group(1)) * _AGE_UNITS[m.group(2)] if m else None


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation, marques de genre retirées, au masculin."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    text = re.sub(r"\((?:e|fe|ne|euse)\)|[·.](?:e|fe)\b", "", text)  # chargé(e), chef·fe
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(_FEMININE.get(word, word) for word in text.split())


def _contains(title: str, term: str) -> bool:
    return f" {normalize(term)} " in f" {title} "


def title_excluded(title: str, excluded: list[str]) -> bool:
    t = normalize(title)
    return any(_contains(t, word) for word in excluded)
