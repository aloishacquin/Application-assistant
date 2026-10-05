import copy
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session

from jobapply.config import AppConfig
from jobapply.models.db import ApplicationStatus, DocumentKind, get_engine
from jobapply.pipeline import (
    DocumentError,
    GenerationInProgressError,
    analyze_offer,
    edit_document,
    generate_documents,
    generate_documents_in_background,
    start_generation,
    submit_offer,
)
from jobapply.tracking import service
from tests.conftest import OFFER_NAMES, TAILORED_CV, FakeLLM, full_responder, offer_text

TODAY = date(2026, 10, 4)


@pytest.fixture
def engine(profile_config: AppConfig) -> Engine:
    return get_engine(profile_config.paths.db)


@pytest.fixture
def application_id(profile_config: AppConfig, engine: Engine) -> int:
    offer_id = submit_offer(engine, text=offer_text(OFFER_NAMES[0])).offer_id
    analyze_offer(
        cfg=profile_config,
        engine=engine,
        offer_id=offer_id,
        llm_factory=lambda: FakeLLM(full_responder),
        use_llm_score=False,
    )
    with Session(engine) as session:
        application = service.accept_offer(session, service.get_offer(session, offer_id))
        return application.id


def generate(cfg, engine, application_id, responder=full_responder, **kwargs):
    llm = FakeLLM(responder)
    return generate_documents(
        cfg=cfg,
        engine=engine,
        application_id=application_id,
        llm_factory=lambda: llm,
        today=TODAY,
        **kwargs,
    ), llm


def test_generate_both_documents(profile_config, engine, application_id) -> None:
    documents, llm = generate(profile_config, engine, application_id)
    assert [d.kind for d in documents] == [DocumentKind.CV, DocumentKind.LETTER]
    assert all(d.version == 1 and not d.edited for d in documents)
    assert len(llm.prompts) == 2
    for document in documents:
        path = Path(document.path)
        assert path.is_file()
        assert path.parent == profile_config.paths.output_dir / str(application_id)
        assert document.json_payload["ats_warnings"] == []
    assert documents[0].prompt_version == 1

    with Session(engine) as session:
        application = service.get_application(session, application_id)
        assert application.status is ApplicationStatus.READY
        assert not application.generating and application.generation_error is None
        messages = [e.message for e in service.list_events(session, application.offer_id)]
        assert {"CV v1 généré", "Lettre v1 généré", "Candidature prête"} <= set(messages)


def test_regenerate_one_document(profile_config, engine, application_id) -> None:
    generate(profile_config, engine, application_id)
    documents, _ = generate(profile_config, engine, application_id, kinds=(DocumentKind.CV,))
    assert documents[0].version == 2
    with Session(engine) as session:
        latest = service.latest_documents(session, application_id)
        assert latest[DocumentKind.CV].version == 2
        assert latest[DocumentKind.LETTER].version == 1
        assert len(service.list_documents(session, application_id)) == 3


def test_failure_is_stored_and_cv_kept(profile_config, engine, application_id) -> None:
    def responder(prompt: str) -> dict:
        if "You write a cover letter" in prompt:
            return {
                "subject": "x",
                "greeting": "x",
                "closing": "x",
                "paragraphs": [{"text": "Hello from Google.", "source_ids": []}] * 3,
            }
        return full_responder(prompt)

    with pytest.raises(DocumentError, match="Lettre rejeté"):
        generate(profile_config, engine, application_id, responder)
    with Session(engine) as session:
        application = service.get_application(session, application_id)
        assert "Google" in application.generation_error
        assert not application.generating
        assert application.status is ApplicationStatus.PREPARING  # letter missing
        assert set(service.latest_documents(session, application_id)) == {DocumentKind.CV}


def test_unexpected_error_is_stored(profile_config, engine, application_id) -> None:
    def explode(prompt: str) -> dict:
        raise RuntimeError("boom")

    with pytest.raises(DocumentError, match="Erreur inattendue : boom"):
        generate(profile_config, engine, application_id, explode)
    with Session(engine) as session:
        assert service.get_application(session, application_id).generation_error


def test_background_wrapper(profile_config, engine, application_id) -> None:
    generate_documents_in_background(
        cfg=profile_config,
        engine=engine,
        application_id=application_id,
        llm_factory=lambda: FakeLLM(lambda p: {}),
        today=TODAY,
    )
    with Session(engine) as session:
        assert service.get_application(session, application_id).generation_error


def test_start_generation_guard(engine, application_id) -> None:
    start_generation(engine, application_id)
    with pytest.raises(GenerationInProgressError):
        start_generation(engine, application_id)


def test_unknown_application(profile_config, engine) -> None:
    with pytest.raises(DocumentError, match="Aucune candidature"):
        generate(profile_config, engine, 42)


def test_edit_document(profile_config, engine, application_id) -> None:
    documents, _ = generate(profile_config, engine, application_id)
    cv = documents[0]
    text = (
        "Rewrote a nightly batch job with PySpark, cutting its runtime from 6 hours to 45 minutes"
    )
    new = edit_document(
        cfg=profile_config,
        engine=engine,
        document_id=cv.id,
        edits={"experiences.0.bullets.1.text": text},
        today=TODAY,
    )
    assert new.version == 2 and new.edited
    assert new.json_payload["issues"] == []
    assert new.json_payload["content"]["experiences"][0]["bullets"][1]["text"] == text
    assert new.model == cv.model
    assert Path(new.path).name == "cv_v2.pdf"


def test_edit_with_invented_fact_is_saved_with_warnings(profile_config, engine, application_id):
    documents, _ = generate(profile_config, engine, application_id)
    letter = documents[1]
    new = edit_document(
        cfg=profile_config,
        engine=engine,
        document_id=letter.id,
        edits={"paragraphs.1.text": "At Google I processed 99 TB per day."},
        today=TODAY,
    )
    issues = new.json_payload["issues"]
    assert any("99" in i for i in issues)
    assert any("Google" in i for i in issues)
    assert new.version == 2


def test_edit_errors(profile_config, engine, application_id) -> None:
    documents, _ = generate(profile_config, engine, application_id)
    with pytest.raises(DocumentError, match="non modifiable"):
        edit_document(
            cfg=profile_config,
            engine=engine,
            document_id=documents[0].id,
            edits={"skills.0": "sk-rust"},
        )
    with pytest.raises(DocumentError, match="Aucun document"):
        edit_document(cfg=profile_config, engine=engine, document_id=999, edits={})


def test_generation_needs_analyzed_offer(profile_config, engine) -> None:
    offer_id = submit_offer(engine, text="raw offer, never analysed").offer_id
    with Session(engine) as session:
        from jobapply.models.db import Application

        application = Application(offer_id=offer_id)
        session.add(application)
        session.commit()
        application_id = application.id
    with pytest.raises(DocumentError, match="pas été analysée"):
        generate(profile_config, engine, application_id)


def test_cv_bullet_limit_triggers_regeneration(profile_config, engine, application_id) -> None:
    too_long = copy.deepcopy(TAILORED_CV)
    too_long["experiences"][0]["bullets"] *= 2  # duplicates -> "used twice" + per-entry limit
    answers = iter([too_long, TAILORED_CV, *[full_responder("You write a cover letter")] * 2])
    documents, llm = generate(profile_config, engine, application_id, lambda p: next(answers))
    assert len(llm.prompts) == 3  # CV twice, letter once
    assert documents[0].version == 1
