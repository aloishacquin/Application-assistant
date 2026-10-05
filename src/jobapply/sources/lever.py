"""Lever public postings API: GET /v0/postings/{company}?mode=json"""

from __future__ import annotations

import httpx

from jobapply.config import SourceSpec
from jobapply.sources.base import CollectedOffer, SourceError, fetch_json, html_to_text

API = "https://api.lever.co/v0/postings/{company}?mode=json"


def _text(job: dict) -> str:
    parts = [job.get("descriptionPlain") or html_to_text(job.get("description") or "")]
    for section in job.get("lists") or []:
        parts.append(section.get("text") or "")
        parts.append(html_to_text(section.get("content") or ""))
    parts.append(job.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def collect(spec: SourceSpec, client: httpx.Client) -> list[CollectedOffer]:
    data = fetch_json(client, API.format(company=spec.company))
    if not isinstance(data, list):
        raise SourceError("format de réponse Lever inattendu")
    offers = []
    for job in data:
        categories = job.get("categories") or {}
        locations = categories.get("allLocations") or [categories.get("location") or ""]
        offers.append(
            CollectedOffer(
                external_id=str(job["id"]),
                url=job.get("hostedUrl"),
                title=job.get("text") or "",
                company=spec.name,
                location=" / ".join(loc for loc in locations if loc),
                text=_text(job),
            )
        )
    return offers
