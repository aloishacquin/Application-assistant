from datetime import date

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from jobapply.config import AppConfig
from jobapply.ingest.extract import PROMPT_NAME, extract_offer
from jobapply.ingest.fetch import FetchError
from jobapply.llm.client import LLMError
from jobapply.llm.prompts import load_prompt
from jobapply.models.db import OfferStatus, get_engine
from jobapply.pipeline import (
    AnalysisError,
    OfferNotFoundError,
    analyze_offer,
    analyze_offer_in_background,
    submit_offer,
)
from jobapply.tracking import service
from jobapply.visa.checker import VisaVerdict
from tests.conftest import OFFER_NAMES, FakeLLM, expected_offer, full_responder, offer_text

FINANCE, TECH, ANALYST = OFFER_NAMES
TODAY = date(2026, 6, 1)


@pytest.fixture
def engine(profile_config: AppConfig) -> Engine:
    return get_engine(profile_config.paths.db)


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM(full_responder)


def analyze(cfg, engine, offer_id, llm, **kwargs):
    kwargs.setdefault("today", TODAY)
    return analyze_offer(
        cfg=cfg, engine=engine, offer_id=offer_id, llm_factory=lambda: llm, **kwargs
    )


def get(engine, offer_id):
    with Session(engine) as session:
        return service.get_offer(session, offer_id)


# --- Extraction ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", OFFER_NAMES)
def test_extract_offer(name: str, tmp_config: AppConfig, fake_llm: FakeLLM) -> None:
    raw = offer_text(name)
    result = extract_offer(raw, llm=fake_llm, prompts_dir=tmp_config.paths.prompts_dir)

    assert result.offer.raw_text == raw
    assert result.offer.model_dump(exclude={"raw_text", "url"}) == expected_offer(name)
    assert result.prompt_version == load_prompt(PROMPT_NAME, tmp_config.paths.prompts_dir).version
    assert '"salary_min_sgd"' in fake_llm.prompts[0]  # schema injected from Pydantic


# --- Submission ---------------------------------------------------------------------------


def test_submit_text_and_deduplicate(engine: Engine) -> None:
    first = submit_offer(engine, text=offer_text(FINANCE))
    assert first.created
    again = submit_offer(engine, text="  " + offer_text(FINANCE).replace("\n", "\n\n"))
    assert not again.created
    assert again.offer_id == first.offer_id
    assert get(engine, first.offer_id).status is OfferStatus.NEW


def test_submit_url_is_normalized(engine: Engine) -> None:
    first = submit_offer(engine, url="https://Jobs.example.com/123/?utm_source=x#apply")
    again = submit_offer(engine, url="https://jobs.example.com/123")
    assert not again.created
    assert again.offer_id == first.offer_id
    assert get(engine, first.offer_id).url == "https://jobs.example.com/123"


@pytest.mark.parametrize(
    ("url", "text", "message"),
    [
        (None, None, "soit un lien"),
        ("https://x.example", "texte", "soit un lien"),
        ("  ", "   ", "soit un lien"),
        ("jobs.example.com/1", None, "http"),
    ],
)
def test_submit_validation(engine: Engine, url, text, message) -> None:
    with pytest.raises(ValueError, match=message):
        submit_offer(engine, url=url, text=text)


# --- Analysis -----------------------------------------------------------------------------


def test_analyze_finance_offer(profile_config: AppConfig, engine: Engine, llm: FakeLLM) -> None:
    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    analysis = analyze(profile_config, engine, offer_id, llm)

    assert analysis.visa_verdict == VisaVerdict.OK
    assert analysis.visa["threshold_sgd"] == 6455  # financial services, 24 at start
    assert analysis.visa["start_date"] == "2026-09-01"  # available_from, later than today
    assert analysis.match["llm_score"] == 80
    assert analysis.recommended
    assert analysis.cost_usd == pytest.approx(0.02)  # extraction + scoring, 0.01 each
    assert analysis.prompt_versions == {"extract_offer": 2, "score_match": 1}

    offer = get(engine, offer_id)
    assert offer.status is OfferStatus.ANALYZED
    assert (offer.title, offer.company) == ("Data Engineer", "Lion City Bank")
    assert offer.to_job_offer().salary_min_sgd == 7000


def test_analyze_incompatible_offer(
    profile_config: AppConfig, engine: Engine, llm: FakeLLM
) -> None:
    offer_id = submit_offer(engine, text=offer_text(ANALYST)).offer_id
    analysis = analyze(profile_config, engine, offer_id, llm, use_llm_score=False)
    assert analysis.visa_verdict == VisaVerdict.INCOMPATIBLE
    assert not analysis.recommended
    assert any("Visa incompatible" in r for r in analysis.stop_reasons)
    assert any("Score" in r for r in analysis.stop_reasons)
    assert analysis.match["llm_score"] is None


def test_unknown_salary_is_not_blocking(profile_config: AppConfig, engine: Engine, llm: FakeLLM):
    offer_id = submit_offer(engine, text=offer_text(TECH)).offer_id
    analysis = analyze(profile_config, engine, offer_id, llm)
    assert analysis.visa_verdict == VisaVerdict.UNKNOWN
    assert analysis.recommended


def test_reanalysis_keeps_history_and_skips_extraction(
    profile_config: AppConfig, engine: Engine, llm: FakeLLM
) -> None:
    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    analyze(profile_config, engine, offer_id, llm)
    analyze(profile_config, engine, offer_id, llm, use_llm_score=False)
    # 2 calls for the first analysis (extract + score), none for the keyword-only re-run
    assert len(llm.prompts) == 2
    with Session(engine) as session:
        latest = service.latest_analysis(session, offer_id)
        assert latest is not None and latest.match["llm_score"] is None
        assert len(service.list_events(session, offer_id)) == 3  # added + 2 analyses


def test_reextract(profile_config: AppConfig, engine: Engine, llm: FakeLLM) -> None:
    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    analyze(profile_config, engine, offer_id, llm, use_llm_score=False)
    analyze(profile_config, engine, offer_id, llm, use_llm_score=False, reextract=True)
    assert len(llm.prompts) == 2  # extraction ran twice


def test_analyze_url_fetches_once(profile_config: AppConfig, engine: Engine, llm: FakeLLM) -> None:
    url = "https://jobs.example.com/merlion"
    fetched = []

    def fetch(u: str) -> str:
        fetched.append(u)
        return offer_text(TECH)

    offer_id = submit_offer(engine, url=url).offer_id
    analyze(profile_config, engine, offer_id, llm, fetch=fetch)
    analyze(profile_config, engine, offer_id, llm, fetch=fetch, use_llm_score=False)
    assert fetched == [url]
    offer = get(engine, offer_id)
    assert offer.raw_text == offer_text(TECH)
    assert offer.to_job_offer().url == url


def test_fetch_error_sets_error_status(profile_config: AppConfig, engine: Engine, llm: FakeLLM):
    def fetch(url: str) -> str:
        raise FetchError("Erreur HTTP 999")

    offer_id = submit_offer(engine, url="https://www.linkedin.com/jobs/view/1").offer_id
    with pytest.raises(AnalysisError, match="999"):
        analyze(profile_config, engine, offer_id, llm, fetch=fetch)
    offer = get(engine, offer_id)
    assert offer.status is OfferStatus.ERROR
    assert offer.status_message == "Erreur HTTP 999"


def test_fetched_duplicate_is_detected(profile_config: AppConfig, engine: Engine, llm: FakeLLM):
    original = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    offer_id = submit_offer(engine, url="https://other.example/same-offer").offer_id
    with pytest.raises(AnalysisError, match=f"Doublon de l'offre #{original}"):
        analyze(profile_config, engine, offer_id, llm, fetch=lambda u: offer_text(FINANCE))
    offer = get(engine, offer_id)
    assert offer.status is OfferStatus.ERROR
    assert offer.duplicate_of == original


def test_llm_error_sets_error_status(profile_config: AppConfig, engine: Engine) -> None:
    def broken() -> FakeLLM:
        raise LLMError("ANTHROPIC_MODEL n'est pas défini")

    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    with pytest.raises(AnalysisError, match="ANTHROPIC_MODEL"):
        analyze_offer(cfg=profile_config, engine=engine, offer_id=offer_id, llm_factory=broken)
    assert get(engine, offer_id).status is OfferStatus.ERROR


def test_invalid_profile_sets_error_status(tmp_config: AppConfig, llm: FakeLLM) -> None:
    engine = get_engine(tmp_config.paths.db)  # tmp_config has no data/profile.yaml
    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    with pytest.raises(AnalysisError, match="introuvable"):
        analyze(tmp_config, engine, offer_id, llm)
    assert get(engine, offer_id).status is OfferStatus.ERROR


def test_unexpected_error_is_stored(profile_config: AppConfig, engine: Engine) -> None:
    def explode(prompt: str) -> dict:
        raise RuntimeError("boom")

    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    with pytest.raises(AnalysisError, match="Erreur inattendue : boom"):
        analyze(profile_config, engine, offer_id, FakeLLM(explode))
    assert get(engine, offer_id).status_message == "Erreur inattendue : boom"


def test_accepted_offer_is_not_reanalyzed(profile_config, engine, llm) -> None:
    offer_id = submit_offer(engine, text=offer_text(FINANCE)).offer_id
    analyze(profile_config, engine, offer_id, llm)
    with Session(engine) as session:
        service.accept_offer(session, service.get_offer(session, offer_id))
    with pytest.raises(AnalysisError, match="déjà acceptée"):
        analyze(profile_config, engine, offer_id, llm)
    assert get(engine, offer_id).status is OfferStatus.ACCEPTED


def test_unknown_offer(profile_config: AppConfig, engine: Engine, llm: FakeLLM) -> None:
    with pytest.raises(OfferNotFoundError):
        analyze(profile_config, engine, 99, llm)


def test_background_wrapper_swallows_analysis_errors(
    profile_config: AppConfig, engine: Engine
) -> None:
    offer_id = submit_offer(engine, url="https://jobs.example.com/x").offer_id

    def fetch(url: str) -> str:
        raise FetchError("down")

    analyze_offer_in_background(
        cfg=profile_config,
        engine=engine,
        offer_id=offer_id,
        llm_factory=lambda: FakeLLM(full_responder),
        fetch=fetch,
    )
    assert get(engine, offer_id).status is OfferStatus.ERROR
