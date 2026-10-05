"""Sanity checks on test fixtures (full profile validation comes with the models in phase 1)."""

from pathlib import Path
from typing import Any

import yaml


def _collect_ids(node: Any) -> list[str]:
    if isinstance(node, dict):
        ids = [node["id"]] if "id" in node else []
        return ids + [i for value in node.values() for i in _collect_ids(value)]
    if isinstance(node, list):
        return [i for item in node for i in _collect_ids(item)]
    return []


def test_profile_fixture_ids_are_unique(fixtures_dir: Path) -> None:
    profile = yaml.safe_load((fixtures_dir / "profile.yaml").read_text(encoding="utf-8"))
    ids = _collect_ids(profile)
    assert ids
    assert len(ids) == len(set(ids))


def test_profile_fixture_has_all_sections(fixtures_dir: Path) -> None:
    profile = yaml.safe_load((fixtures_dir / "profile.yaml").read_text(encoding="utf-8"))
    expected = {
        "identity",
        "headline_variants",
        "education",
        "experiences",
        "projects",
        "skills",
        "languages",
        "motivations",
        "constraints",
    }
    assert expected <= profile.keys()


def test_three_offer_fixtures(fixtures_dir: Path) -> None:
    offers = sorted((fixtures_dir / "offers").glob("*.txt"))
    assert len(offers) == 3
    assert all(o.read_text(encoding="utf-8").strip() for o in offers)
