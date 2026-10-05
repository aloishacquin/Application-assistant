"""URL -> raw text with httpx + trafilatura.

Used only for a link the user pasted: one request, made on their explicit demand (SPEC
section 2). Automated collection goes through `jobapply.sources`, never through this module.
"""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
import trafilatura

USER_AGENT = "jobapply/0.1 (personal job application assistant; single page fetch)"
MIN_TEXT_LENGTH = 200


class FetchError(Exception):
    """Raised when an offer cannot be fetched or no text can be extracted."""


def _check_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise FetchError(f"URL invalide (http/https attendu) : {url}")


def fetch_offer_text(url: str, *, client: httpx.Client | None = None, timeout: float = 20.0) -> str:
    _check_url(url)
    own_client = client is None
    http = client or httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
    )
    try:
        response = http.get(url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            f"Erreur HTTP {exc.response.status_code} pour {url} : colle le texte de l'offre "
            "à la place."
        ) from exc
    except httpx.HTTPError as exc:
        raise FetchError(f"Impossible de récupérer {url} : {exc}") from exc
    finally:
        if own_client:
            http.close()

    text = trafilatura.extract(response.text, url=url, include_comments=False, include_tables=True)
    if not text or len(text.strip()) < MIN_TEXT_LENGTH:
        raise FetchError(
            f"Pas assez de texte extrait de {url} (page dynamique ou protégée ?) : "
            "colle le texte de l'offre à la place."
        )
    return text.strip()
