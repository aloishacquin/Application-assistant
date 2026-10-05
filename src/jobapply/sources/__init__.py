"""Automatic offer collectors (public APIs and feeds only, SPEC section 6)."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from jobapply.config import SourceSpec
from jobapply.models.db import OfferSource
from jobapply.sources import ashby, greenhouse, lever, rss
from jobapply.sources.base import CollectedOffer

Collector = Callable[[SourceSpec, httpx.Client], list[CollectedOffer]]

COLLECTORS: dict[str, Collector] = {
    "greenhouse": greenhouse.collect,
    "lever": lever.collect,
    "ashby": ashby.collect,
    "rss": rss.collect,
}
OFFER_SOURCES = {
    "greenhouse": OfferSource.GREENHOUSE,
    "lever": OfferSource.LEVER,
    "ashby": OfferSource.ASHBY,
    "rss": OfferSource.RSS,
}
