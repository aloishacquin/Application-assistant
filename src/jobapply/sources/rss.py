"""RSS 2.0 and Atom job feeds (standard library parser, no extra dependency)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import httpx

from jobapply.config import SourceSpec
from jobapply.sources.base import CollectedOffer, SourceError, fetch, html_to_text

ATOM = "{http://www.w3.org/2005/Atom}"


def _child_text(element: ET.Element, *names: str) -> str:
    for name in names:
        child = element.find(name)
        if child is not None and child.text:
            return child.text.strip()
    return ""


def collect(spec: SourceSpec, client: httpx.Client) -> list[CollectedOffer]:
    assert spec.url is not None
    try:
        root = ET.fromstring(fetch(client, spec.url).content)
    except ET.ParseError as exc:
        raise SourceError(f"flux RSS/Atom illisible : {exc}") from exc

    offers = []
    for item in root.iter("item"):  # RSS 2.0
        link = _child_text(item, "link")
        offers.append(
            CollectedOffer(
                external_id=_child_text(item, "guid") or link,
                url=link or None,
                title=_child_text(item, "title"),
                company=spec.name,
                location=_child_text(item, "location", "category"),
                text=html_to_text(_child_text(item, "description")),
            )
        )
    for entry in root.iter(f"{ATOM}entry"):  # Atom
        link_el = entry.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        offers.append(
            CollectedOffer(
                external_id=_child_text(entry, f"{ATOM}id") or link,
                url=link or None,
                title=_child_text(entry, f"{ATOM}title"),
                company=spec.name,
                location="",
                text=html_to_text(_child_text(entry, f"{ATOM}content", f"{ATOM}summary")),
            )
        )
    return [o for o in offers if o.external_id]
