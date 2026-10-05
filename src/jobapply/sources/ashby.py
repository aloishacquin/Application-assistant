"""Ashby public job board API: GET /posting-api/job-board/{board}?includeCompensation=true"""

from __future__ import annotations

import httpx

from jobapply.config import SourceSpec
from jobapply.sources.base import CollectedOffer, SourceError, fetch_json, html_to_text

API = "https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"


def _text(job: dict) -> str:
    text = job.get("descriptionPlain") or html_to_text(job.get("descriptionHtml") or "")
    compensation = job.get("compensation")
    if isinstance(compensation, dict):
        summary = compensation.get("compensationTierSummary")
        if isinstance(summary, str) and summary:
            text += f"\n\nCompensation: {summary}"
    return text.strip()


def collect(spec: SourceSpec, client: httpx.Client) -> list[CollectedOffer]:
    data = fetch_json(client, API.format(board=spec.board))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise SourceError("format de réponse Ashby inattendu")
    offers = []
    for job in data["jobs"]:
        if job.get("isListed") is False:
            continue
        locations = [job.get("location") or ""]
        locations += [s.get("location") or "" for s in job.get("secondaryLocations") or []]
        offers.append(
            CollectedOffer(
                external_id=str(job["id"]),
                url=job.get("jobUrl"),
                title=job.get("title") or "",
                company=spec.name,
                location=" / ".join(loc for loc in locations if loc),
                text=_text(job),
            )
        )
    return offers
