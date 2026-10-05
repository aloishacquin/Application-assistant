"""Free pre-filter applied before any LLM call (SPEC section 6)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jobapply.config import SourceFilters
from jobapply.sources.base import CollectedOffer

MIN_TEXT_CHARS = 200


@dataclass(frozen=True)
class FilterResult:
    keep: bool  # analyse it
    store: bool  # keep a trace (filtered offers stay visible); False for out-of-area offers
    reason: str | None = None


def _has_term(text: str, term: str) -> bool:
    """Whole-word match: "intern" must not match "Internal", "lead" not "Leader"..."""
    term = term.strip().casefold()
    return bool(term) and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text) is not None


def prefilter(offer: CollectedOffer, filters: SourceFilters) -> FilterResult:
    location = offer.location.casefold()
    # RSS feeds often have no location: only an explicit mismatch excludes an offer.
    if (
        filters.locations
        and location
        and not any(loc.casefold() in location for loc in filters.locations)
    ):
        return FilterResult(False, False, f"Lieu hors cible : {offer.location}")

    title = offer.title.casefold()
    excluded = [w for w in filters.title_excludes if _has_term(title, w)]
    if excluded:
        return FilterResult(False, True, f"Titre exclu ({excluded[0].strip()})")
    if filters.title_keywords and not any(_has_term(title, k) for k in filters.title_keywords):
        return FilterResult(False, True, "Titre sans mot-clé cible")
    if len(offer.text) < MIN_TEXT_CHARS:
        return FilterResult(
            False, True, "Description trop courte : ouvre l'offre et colle son texte"
        )
    return FilterResult(True, True)
