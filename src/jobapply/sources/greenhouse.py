"""Greenhouse public Job Board API: GET /v1/boards/{board}/jobs?content=true"""

from __future__ import annotations

import httpx

from jobapply.config import SourceSpec
from jobapply.sources.base import CollectedOffer, SourceError, fetch_json, html_to_text

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"


def collect(spec: SourceSpec, client: httpx.Client) -> list[CollectedOffer]:
    data = fetch_json(client, API.format(board=spec.board))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceError("format de réponse Greenhouse inattendu")
    offers = []
    for job in data["jobs"]:
        location = (job.get("location") or {}).get("name") or ""
        offers.append(
            CollectedOffer(
                external_id=str(job["id"]),
                url=job.get("absolute_url"),
                title=job.get("title") or "",
                company=job.get("company_name") or spec.name,
                location=location,
                text=html_to_text(job.get("content") or ""),
            )
        )
    return offers
