"""Shared pieces for collectors: the collected offer, HTTP access, HTML to text."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

USER_AGENT = "jobapply/0.1 (personal job search assistant; public job board API)"
TIMEOUT_S = 30.0


class SourceError(Exception):
    """A source could not be collected this time (network, HTTP error, unexpected format)."""


@dataclass(frozen=True)
class CollectedOffer:
    external_id: str
    url: str | None
    title: str
    company: str
    location: str
    text: str


def make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True
    )


def fetch(client: httpx.Client, url: str) -> httpx.Response:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        raise SourceError(f"connexion impossible : {exc}") from exc
    if response.status_code == 429:
        raise SourceError("trop de requêtes (429) : la source sera réessayée plus tard")
    if response.status_code >= 400:
        raise SourceError(f"erreur HTTP {response.status_code}")
    return response


def fetch_json(client: httpx.Client, url: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    try:
        return fetch(client, url).json()
    except ValueError as exc:
        raise SourceError("réponse JSON invalide") from exc


BLOCK_END_RE = re.compile(r"</(p|div|h[1-6]|ul|ol|tr|section)>|<br\s*/?>", re.IGNORECASE)
LI_START_RE = re.compile(r"<li[^>]*>", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    """Readable plain text from an HTML fragment (also handles entity-escaped HTML)."""
    if "&lt;" in raw:  # Greenhouse escapes the HTML of its job descriptions
        raw = html.unescape(raw)
    text = LI_START_RE.sub("\n- ", raw)
    text = BLOCK_END_RE.sub("\n", text)
    text = html.unescape(TAG_RE.sub("", text)).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
