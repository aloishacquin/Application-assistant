import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from jobapply.config import AppConfig, load_config
from jobapply.models.db import Offer, OfferSource, OfferStatus, SourceRun, get_engine
from jobapply.tracking import service
from jobapply.worker import (
    CollectionInProgressError,
    analyze_pending,
    run_collection,
    seconds_until_next_run,
)
from tests.conftest import FakeLLM, full_responder, offer_text
from tests.sources_fixtures import SOURCES_YAML, mock_client

TODAY = date(2026, 10, 4)


@pytest.fixture
def cfg(profile_root: Path) -> AppConfig:
    (profile_root / "config" / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    return load_config(profile_root, env_file=profile_root / "missing.env")


@pytest.fixture
def engine(cfg: AppConfig) -> Engine:
    return get_engine(cfg.paths.db)


def offers(engine: Engine) -> list[Offer]:
    with Session(engine) as session:
        return list(session.exec(select(Offer).order_by(Offer.id)))


def collect(cfg, engine, **kwargs):
    llm = FakeLLM(full_responder)
    return run_collection(
        cfg, engine, lambda: llm, client=mock_client(), today=TODAY, **kwargs
    ), llm


def test_collection_filters_and_stores(cfg: AppConfig, engine: Engine) -> None:
    report, _ = collect(cfg, engine, analyze=False)
    merlion, acme = report.runs
    assert (merlion.fetched, merlion.new, merlion.filtered, merlion.out_of_area) == (4, 1, 2, 1)
    assert (acme.fetched, acme.new) == (1, 1)
    assert report.new == 2

    stored = {o.external_id: o for o in offers(engine)}
    assert set(stored) == {"101", "102", "104", "lv-1"}  # Dublin offer not stored
    assert stored["101"].status is OfferStatus.NEW
    assert stored["101"].source is OfferSource.GREENHOUSE
    assert stored["101"].title == "Software Engineer"  # visible before the analysis
    assert stored["102"].status is OfferStatus.FILTERED
    assert stored["102"].status_message == "Titre exclu (senior)"
    assert stored["104"].status_message == "Titre sans mot-clé cible"

    with Session(engine) as session:
        assert len(list(session.exec(select(SourceRun)))) == 2


def test_second_collection_has_no_duplicates(cfg: AppConfig, engine: Engine) -> None:
    collect(cfg, engine, analyze=False)
    report, _ = collect(cfg, engine, analyze=False)
    assert report.new == 0
    assert report.runs[0].duplicates == 3
    assert len(offers(engine)) == 4


def test_offer_already_added_by_hand_is_a_duplicate(cfg: AppConfig, engine: Engine) -> None:
    with Session(engine) as session:
        service.create_offer(session, url="https://jobs.lever.co/acme/lv-1")
    report, _ = collect(cfg, engine, analyze=False)
    assert report.runs[1].duplicates == 1


def test_collection_analyzes_new_offers(cfg: AppConfig, engine: Engine) -> None:
    def responder(prompt: str) -> dict:
        # every collected offer is answered with the Merlion extraction
        if "<offer>" in prompt and "<profile>" not in prompt and "tailor" not in prompt:
            from tests.conftest import expected_offer

            return expected_offer("02_tech_no_salary")
        return full_responder(prompt)

    report = run_collection(
        cfg, engine, lambda: FakeLLM(responder), client=mock_client(), today=TODAY
    )
    assert report.analyzed == 2 and report.analysis_errors == 0
    statuses = {o.external_id: o.status for o in offers(engine)}
    assert statuses["101"] is OfferStatus.ANALYZED
    assert statuses["102"] is OfferStatus.FILTERED  # never analysed


def test_manual_offers_are_not_analyzed_by_the_worker(cfg: AppConfig, engine: Engine) -> None:
    with Session(engine) as session:
        service.create_offer(session, raw_text=offer_text("01_finance_data_engineer"))
    analyzed, errors, _ = analyze_pending(cfg, engine, lambda: FakeLLM(full_responder), today=TODAY)
    assert (analyzed, errors) == (0, 0)


def test_budget_stops_analyses(cfg: AppConfig, engine: Engine) -> None:
    cfg.paths.llm_log.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.llm_log.write_text(
        json.dumps({"timestamp": "2026-10-04T08:00:00+00:00", "cost_usd": 1.5}) + "\n",
        encoding="utf-8",
    )
    report, llm = collect(cfg, engine)
    assert report.budget_reached
    assert report.analyzed == 0 and llm.prompts == []
    assert all(o.status in (OfferStatus.NEW, OfferStatus.FILTERED) for o in offers(engine))


def test_max_analyses_per_run(cfg: AppConfig, engine: Engine) -> None:
    capped = cfg.model_copy(
        update={
            "sources": cfg.sources.model_copy(
                update={"budget": cfg.sources.budget.model_copy(update={"max_analyses_per_run": 1})}
            )
        }
    )
    collect(capped, engine, analyze=False)
    analyzed, errors, reached = analyze_pending(
        capped, engine, lambda: FakeLLM(full_responder), today=TODAY
    )
    assert analyzed + errors == 1 and not reached


def test_failing_source_does_not_stop_others(cfg: AppConfig, engine: Engine) -> None:
    import httpx

    client = mock_client({"boards-api.greenhouse.io": lambda: httpx.Response(503)})
    report = run_collection(
        cfg, engine, lambda: FakeLLM(full_responder), client=client, analyze=False
    )
    merlion, acme = report.runs
    assert merlion.error == "erreur HTTP 503" and merlion.fetched == 0
    assert acme.new == 1


def test_short_description_is_stored_without_text(cfg: AppConfig, engine: Engine) -> None:
    rss_cfg = cfg.model_copy(
        update={
            "sources": cfg.sources.model_copy(
                update={
                    "sources": [
                        cfg.sources.sources[0].model_copy(
                            update={
                                "type": "rss",
                                "name": "Feed",
                                "board": None,
                                "url": "https://feed.example/r",
                            }
                        )
                    ],
                    "filters": cfg.sources.filters.model_copy(update={"title_keywords": []}),
                }
            )
        }
    )
    run_collection(
        rss_cfg, engine, lambda: FakeLLM(full_responder), client=mock_client(), analyze=False
    )
    short = next(o for o in offers(engine) if o.external_id == "job-2")
    assert short.status is OfferStatus.FILTERED
    assert short.raw_text is None and short.url == "https://feed.example/jobs/2"


def test_runs_are_serialised(cfg: AppConfig, engine: Engine) -> None:
    from jobapply import worker

    with worker._run_lock:
        assert worker.is_running()
        with pytest.raises(CollectionInProgressError):
            run_collection(cfg, engine, lambda: FakeLLM(full_responder), client=mock_client())
    assert not worker.is_running()


def test_background_wrapper_survives_errors(cfg: AppConfig, engine: Engine) -> None:
    from jobapply import worker

    def boom(**kwargs):
        raise RuntimeError("boom")

    original = worker.run_collection
    worker.run_collection = boom
    try:
        worker.run_collection_in_background(cfg=cfg, engine=engine)  # logged, not raised
    finally:
        worker.run_collection = original
    with worker._run_lock:  # a run already in progress is skipped, not raised
        worker.run_collection_in_background(cfg=cfg, engine=engine, llm_factory=None)


def test_schedule(cfg: AppConfig, engine: Engine) -> None:
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    assert seconds_until_next_run(cfg, engine, now) == 60.0  # never ran
    with Session(engine) as session:
        session.add(SourceRun(source_name="Merlion", started_at=now - timedelta(hours=1)))
        session.commit()
    assert seconds_until_next_run(cfg, engine, now) == pytest.approx(5 * 3600)
    assert seconds_until_next_run(cfg, engine, now + timedelta(hours=10)) == 60.0
