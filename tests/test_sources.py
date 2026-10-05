import httpx
import pytest
from pydantic import ValidationError

from jobapply.config import SourceFilters, SourcesConfig, SourceSpec
from jobapply.sources import ashby, greenhouse, lever, rss
from jobapply.sources.base import CollectedOffer, SourceError, html_to_text
from jobapply.sources.filters import prefilter
from tests.sources_fixtures import DESCRIPTION, HTML_DESCRIPTION, mock_client


def test_html_to_text() -> None:
    assert html_to_text(HTML_DESCRIPTION) == "About us\nWe build robots.\n\n- Python\n- Docker"
    assert html_to_text("&lt;p&gt;Escaped &amp;amp; fine&lt;/p&gt;") == "Escaped & fine"


def test_greenhouse() -> None:
    calls = []
    spec = SourceSpec(type="greenhouse", name="Merlion", board="merlion")
    offers = greenhouse.collect(spec, mock_client(calls=calls))
    assert calls == ["https://boards-api.greenhouse.io/v1/boards/merlion/jobs?content=true"]
    assert len(offers) == 4
    first = offers[0]
    assert (first.external_id, first.title, first.location) == (
        "101",
        "Software Engineer",
        "Singapore",
    )
    assert first.company == "Merlion Robotics"
    assert first.text.startswith("Software Engineer (Python, Backend)")
    assert "&lt;" not in first.text and "<p>" not in first.text


def test_lever() -> None:
    offers = lever.collect(SourceSpec(type="lever", name="Acme", company="acme"), mock_client())
    offer = offers[0]
    assert offer.location == "Singapore, Singapore / Remote"
    assert offer.url == "https://jobs.lever.co/acme/lv-1"
    assert "What you'll do" in offer.text and "- Build APIs in Python" in offer.text
    assert offer.text.endswith("Visa sponsorship available.")


def test_ashby() -> None:
    offers = ashby.collect(SourceSpec(type="ashby", name="Kopi", board="kopi"), mock_client())
    assert [o.external_id for o in offers] == ["ab-1"]  # unlisted job skipped
    assert offers[0].location == "Remote (Asia) / Singapore"
    assert offers[0].text.endswith("Compensation: SGD 7K – 9K per month")


def test_rss_and_atom() -> None:
    items = rss.collect(
        SourceSpec(type="rss", name="Feed", url="https://feed.example/rss"), mock_client()
    )
    assert [(o.external_id, o.title) for o in items] == [
        ("job-1", "Python Developer"),
        ("job-2", "Data Analyst"),
    ]
    assert items[0].text.startswith("Software Engineer (Python, Backend)")
    assert "<p>" not in items[0].text
    entries = rss.collect(
        SourceSpec(type="rss", name="Atom", url="https://atom.example/f"), mock_client()
    )
    assert entries[0].external_id == "urn:job:9"
    assert entries[0].url == "https://atom.example/9"


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (lambda: httpx.Response(429), "429"),
        (lambda: httpx.Response(404), "HTTP 404"),
        (lambda: httpx.Response(200, text="not json"), "JSON invalide"),
        (lambda: httpx.Response(200, json={"unexpected": True}), "inattendu"),
    ],
)
def test_greenhouse_errors(response, message) -> None:
    client = mock_client({"boards-api.greenhouse.io": response})
    with pytest.raises(SourceError, match=message):
        greenhouse.collect(SourceSpec(type="greenhouse", name="X", board="x"), client)


def test_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(SourceError, match="connexion impossible"):
        lever.collect(SourceSpec(type="lever", name="X", company="x"), client)


def test_lever_and_ashby_bad_format() -> None:
    client = mock_client(
        {
            "api.lever.co": lambda: httpx.Response(200, json={"a": 1}),
            "api.ashbyhq.com": lambda: httpx.Response(200, json=[1]),
        }
    )
    with pytest.raises(SourceError):
        lever.collect(SourceSpec(type="lever", name="X", company="x"), client)
    with pytest.raises(SourceError):
        ashby.collect(SourceSpec(type="ashby", name="X", board="x"), client)


def test_bad_rss() -> None:
    client = mock_client({"feed.example": lambda: httpx.Response(200, text="<rss><item>")})
    with pytest.raises(SourceError, match="illisible"):
        rss.collect(SourceSpec(type="rss", name="X", url="https://feed.example/x"), client)


# --- Pre-filter ---------------------------------------------------------------------------

FILTERS = SourceFilters(
    locations=["Singapore"], title_keywords=["engineer", "developer"], title_excludes=["senior"]
)


def offer(title="Software Engineer", location="Singapore", text=DESCRIPTION) -> CollectedOffer:
    return CollectedOffer("1", "https://x.example/1", title, "X", location, text)


@pytest.mark.parametrize(
    ("item", "keep", "store", "reason"),
    [
        (offer(), True, True, None),
        (offer(location="Dublin"), False, False, "Lieu hors cible : Dublin"),
        (offer(location=""), True, True, None),  # unknown location: kept
        (offer(location="Remote / SINGAPORE"), True, True, None),
        (offer(title="Senior Software Engineer"), False, True, "Titre exclu (senior)"),
        (offer(title="Account Executive"), False, True, "Titre sans mot-clé cible"),
        (offer(text="Short."), False, True, "Description trop courte"),
        (offer(title="Internal Tools Engineer"), True, True, None),  # "intern" is a whole word
        (offer(title="Software Engineer, Intern"), True, True, None),  # not in these excludes
        (offer(title="Engineering Manager"), False, True, "Titre sans mot-clé cible"),
    ],
)
def test_prefilter(item, keep, store, reason) -> None:
    result = prefilter(item, FILTERS)
    assert (result.keep, result.store) == (keep, store)
    if reason:
        assert result.reason.startswith(reason)


def test_empty_filters_keep_everything() -> None:
    assert prefilter(offer(title="Chef", location="Paris"), SourceFilters(locations=[])).keep


# --- Configuration ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        {"type": "greenhouse", "name": "X"},
        {"type": "lever", "name": "X", "board": "b"},
        {"type": "rss", "name": "X"},
        {"type": "monster", "name": "X", "url": "u"},
    ],
)
def test_invalid_source_spec(spec) -> None:
    with pytest.raises(ValidationError):
        SourceSpec.model_validate(spec)


def test_duplicate_source_names() -> None:
    with pytest.raises(ValidationError, match="unique"):
        SourcesConfig.model_validate(
            {
                "sources": [
                    {"type": "lever", "name": "A", "company": "a"},
                    {"type": "lever", "name": "A", "company": "b"},
                ]
            }
        )


def test_active_sources() -> None:
    cfg = SourcesConfig.model_validate(
        {
            "sources": [
                {"type": "lever", "name": "A", "company": "a"},
                {"type": "lever", "name": "B", "company": "b", "enabled": False},
            ]
        }
    )
    assert [s.name for s in cfg.active] == ["A"]


def test_repository_sources_file_is_valid(project_root) -> None:
    from jobapply.config import load_config

    cfg = load_config(project_root, env_file=project_root / "missing.env")
    assert cfg.sources.filters.locations == ["Singapore"]
    assert all(s.type in ("greenhouse", "lever", "ashby", "rss") for s in cfg.sources.sources)
