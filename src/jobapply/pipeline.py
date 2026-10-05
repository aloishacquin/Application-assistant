"""Orchestration, see SPEC section 8. The web routes and the CLI only call this module and
`tracking.service`.

- `submit_offer`: register an offer (link or pasted text), deduplicated.
- `analyze_offer`: fetch -> extract -> visa -> score, stored as an `Analysis`.
- `generate_documents`: CV and/or cover letter -> validated -> PDF, stored as `Document`s.
- `edit_document`: manual edit of a document -> new version (validator warns, never blocks).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from sqlalchemy.engine import Engine
from sqlmodel import Session

from jobapply.config import AppConfig, ConfigError
from jobapply.generate.cover_letter import generate_cover_letter
from jobapply.generate.cv import generate_cv
from jobapply.generate.documents import apply_edits, build_document
from jobapply.generate.render import RenderError
from jobapply.generate.runner import GenerationError
from jobapply.generate.validator import Issue, validate_cv, validate_letter
from jobapply.ingest.extract import extract_offer
from jobapply.ingest.fetch import FetchError, fetch_offer_text
from jobapply.llm.client import LLMError, StructuredLLM
from jobapply.llm.prompts import PromptError
from jobapply.matching.scorer import score_offer
from jobapply.models.db import (
    Analysis,
    Application,
    ApplicationStatus,
    Document,
    DocumentKind,
    Offer,
    OfferSource,
    OfferStatus,
    utcnow,
)
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile, load_profile
from jobapply.models.tailored import CoverLetter, TailoredCV
from jobapply.tracking import service
from jobapply.visa.checker import VisaVerdict, check_visa

logger = logging.getLogger(__name__)


class OfferNotFoundError(LookupError):
    pass


class AnalysisError(Exception):
    """The analysis failed; the offer is left in `error` status with the same message."""


@dataclass(frozen=True)
class SubmitResult:
    offer_id: int
    created: bool  # False when the same link or text was already stored


def submit_offer(
    engine: Engine,
    *,
    url: str | None = None,
    text: str | None = None,
    source: OfferSource = OfferSource.MANUAL,
) -> SubmitResult:
    url = url.strip() if url else None
    text = text.strip() if text else None
    if bool(url) == bool(text):
        raise ValueError("Indique soit un lien, soit le texte de l'offre.")
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError("Le lien doit commencer par http:// ou https://.")

    with Session(engine) as session:
        existing = (
            service.find_offer_by_url(session, url)
            if url
            else service.find_offer_by_text(session, text or "")
        )
        if existing is not None:
            assert existing.id is not None
            return SubmitResult(existing.id, created=False)
        offer = service.create_offer(session, url=url, raw_text=text, source=source)
        assert offer.id is not None
        return SubmitResult(offer.id, created=True)


def analyze_offer(
    *,
    cfg: AppConfig,
    engine: Engine,
    offer_id: int,
    llm_factory: Callable[[], StructuredLLM],
    use_llm_score: bool = True,
    reextract: bool = False,
    profile: Profile | None = None,
    fetch: Callable[[str], str] = fetch_offer_text,
    today: date | None = None,
) -> Analysis:
    """Run the full analysis of a stored offer. Raises AnalysisError on any expected failure."""
    with Session(engine, expire_on_commit=False) as session:
        offer = service.get_offer(session, offer_id)
        if offer is None:
            raise OfferNotFoundError(f"Aucune offre avec l'id {offer_id}.")
        if offer.status is OfferStatus.ACCEPTED:
            raise AnalysisError("Offre déjà acceptée : l'analyse n'est plus modifiée.")
        service.set_offer_status(session, offer, OfferStatus.ANALYZING)
        try:
            return _analyze(
                cfg, session, offer, llm_factory, use_llm_score, reextract, profile, fetch, today
            )
        except (FetchError, LLMError, PromptError, ConfigError, AnalysisError) as exc:
            session.rollback()
            service.set_offer_status(session, offer, OfferStatus.ERROR, str(exc))
            raise AnalysisError(str(exc)) from exc
        except Exception as exc:
            session.rollback()
            logger.exception("Unexpected error while analysing offer %s", offer_id)
            message = f"Erreur inattendue : {exc}"
            service.set_offer_status(session, offer, OfferStatus.ERROR, message)
            raise AnalysisError(message) from exc


def analyze_offer_in_background(**kwargs: object) -> None:
    """Background-task wrapper: failures are already stored on the offer."""
    try:
        analyze_offer(**kwargs)  # type: ignore[arg-type]
    except AnalysisError as exc:
        logger.warning("Analysis of offer %s failed: %s", kwargs.get("offer_id"), exc)


def _analyze(
    cfg: AppConfig,
    session: Session,
    offer: Offer,
    llm_factory: Callable[[], StructuredLLM],
    use_llm_score: bool,
    reextract: bool,
    profile: Profile | None,
    fetch: Callable[[str], str],
    today: date | None,
) -> Analysis:
    assert offer.id is not None
    profile = profile or load_profile(cfg.paths.profile)
    llm: StructuredLLM | None = None

    def get_llm() -> StructuredLLM:
        nonlocal llm
        llm = llm or llm_factory()
        return llm

    if offer.raw_text is None:
        if offer.url is None:
            raise AnalysisError("L'offre n'a ni texte ni lien.")
        raw_text = fetch(offer.url)
        duplicate = service.find_offer_by_text(session, raw_text)
        if duplicate is not None and duplicate.id != offer.id:
            offer.duplicate_of = duplicate.id
            session.add(offer)
            session.commit()  # kept despite the rollback that follows the error
            raise AnalysisError(f"Doublon de l'offre #{duplicate.id}.")
        offer.raw_text = raw_text
        offer.text_hash = service.text_hash(raw_text)

    calls = []
    job = None if reextract else offer.to_job_offer()
    if job is None:
        extraction = extract_offer(
            offer.raw_text, llm=get_llm(), prompts_dir=cfg.paths.prompts_dir, url=offer.url
        )
        job = extraction.offer
        calls += extraction.calls
        offer.payload = job.model_dump(mode="json")
        offer.title, offer.company = job.title, job.company
        offer.model, offer.prompt_version = extraction.model, extraction.prompt_version

    visa = check_visa(
        cfg.visa,
        birth_year=profile.identity.birth_year,
        start_date=max(profile.constraints.available_from_date, today or date.today()),
        sector=job.sector,
        salary_min_sgd=job.salary_min_sgd,
        salary_max_sgd=job.salary_max_sgd,
    )
    scored = score_offer(
        profile,
        job,
        cfg.settings.matching,
        llm=get_llm() if use_llm_score else None,
        prompts_dir=cfg.paths.prompts_dir,
    )
    calls += scored.calls

    stop_reasons = _stop_reasons(cfg, visa.verdict, visa.threshold_sgd, scored.match.score)
    costs = [c.cost_usd for c in calls]
    prompt_versions = {c.prompt_name: c.prompt_version for c in calls}
    if offer.prompt_version is not None:
        prompt_versions.setdefault("extract_offer", offer.prompt_version)

    analysis = Analysis(
        offer_id=offer.id,
        visa=visa.model_dump(mode="json"),
        match=scored.match.model_dump(mode="json"),
        score=scored.match.score,
        visa_verdict=visa.verdict.value,
        stop_reasons=stop_reasons,
        model=calls[-1].model_served if calls else None,
        prompt_versions=prompt_versions,
        cost_usd=None if None in costs else sum(c or 0 for c in costs),
    )
    session.add(analysis)
    offer.status = OfferStatus.ANALYZED
    offer.status_message = None
    service.add_event(
        session,
        offer.id,
        "analysis",
        f"Analyse : score {analysis.score}/100, visa {analysis.visa_verdict}",
    )
    session.add(offer)
    session.commit()
    session.refresh(analysis)
    return analysis


def _stop_reasons(cfg: AppConfig, verdict: VisaVerdict, threshold: int, score: int) -> list[str]:
    reasons = []
    if verdict is VisaVerdict.INCOMPATIBLE:
        reasons.append(f"Visa incompatible (seuil {threshold} SGD/mois)")
    min_score = cfg.settings.matching.min_score
    if score < min_score:
        reasons.append(f"Score {score} sous le seuil de {min_score}")
    return reasons


# --- Documents ----------------------------------------------------------------------------

KIND_LABELS = {DocumentKind.CV: "CV", DocumentKind.LETTER: "Lettre"}


class DocumentError(Exception):
    """Generation or edit failed; for a generation, the message is stored on the application."""


class GenerationInProgressError(DocumentError):
    pass


def start_generation(engine: Engine, application_id: int) -> None:
    """Flag the application as generating, so the interface shows it before the task starts."""
    with Session(engine) as session:
        application = _get_application(session, application_id)
        if application.generating:
            raise GenerationInProgressError("Une génération est déjà en cours.")
        application.generating = True
        application.generation_error = None
        session.add(application)
        session.commit()


def generate_documents(
    *,
    cfg: AppConfig,
    engine: Engine,
    application_id: int,
    kinds: tuple[DocumentKind, ...] = (DocumentKind.CV, DocumentKind.LETTER),
    llm_factory: Callable[[], StructuredLLM],
    profile: Profile | None = None,
    today: date | None = None,
) -> list[Document]:
    with Session(engine, expire_on_commit=False) as session:
        application = _get_application(session, application_id)
        try:
            documents = _generate(
                cfg, session, application, kinds, llm_factory, profile, today or date.today()
            )
        except (GenerationError, LLMError, PromptError, ConfigError, RenderError) as exc:
            session.rollback()
            _finish_generation(session, application, error=str(exc))
            raise DocumentError(str(exc)) from exc
        except Exception as exc:
            session.rollback()
            logger.exception("Unexpected error while generating for application %s", application_id)
            _finish_generation(session, application, error=f"Erreur inattendue : {exc}")
            raise DocumentError(f"Erreur inattendue : {exc}") from exc
        _finish_generation(session, application, error=None)
        return documents


def generate_documents_in_background(**kwargs: object) -> None:
    """Background-task wrapper: failures are already stored on the application."""
    try:
        generate_documents(**kwargs)  # type: ignore[arg-type]
    except DocumentError as exc:
        logger.warning(
            "Generation for application %s failed: %s", kwargs.get("application_id"), exc
        )


def edit_document(
    *,
    cfg: AppConfig,
    engine: Engine,
    document_id: int,
    edits: dict[str, str],
    profile: Profile | None = None,
    today: date | None = None,
) -> Document:
    """Save edited texts as a new version. Validation issues are stored as warnings."""
    with Session(engine, expire_on_commit=False) as session:
        base = session.get(Document, document_id)
        if base is None:
            raise DocumentError(f"Aucun document avec l'id {document_id}.")
        application = _get_application(session, base.application_id)
        try:
            profile = profile or load_profile(cfg.paths.profile)
            job = _job_offer(session, application)
            content_data = apply_edits(base.json_payload["content"], edits)
            if base.kind is DocumentKind.CV:
                content: TailoredCV | CoverLetter = TailoredCV.model_validate(content_data)
                issues = validate_cv(content, profile, job, cfg.settings.cv)
            else:
                content = CoverLetter.model_validate(content_data)
                issues = validate_letter(content, profile, job, cfg.settings.cover_letter)
            document = _store_document(
                cfg,
                session,
                application,
                base.kind,
                content,
                profile,
                job,
                issues,
                today or date.today(),
                model=base.model,
                prompt_version=base.prompt_version,
                edited=True,
            )
        except (ValueError, ConfigError, RenderError) as exc:
            session.rollback()
            raise DocumentError(str(exc)) from exc
        session.commit()
        session.refresh(document)
        return document


def _get_application(session: Session, application_id: int) -> Application:
    application = service.get_application(session, application_id)
    if application is None:
        raise DocumentError(f"Aucune candidature avec l'id {application_id}.")
    return application


def _job_offer(session: Session, application: Application) -> JobOffer:
    offer = service.get_offer(session, application.offer_id)
    job = offer.to_job_offer() if offer else None
    if job is None:
        raise DocumentError("L'offre de cette candidature n'a pas été analysée.")
    return job


def _generate(
    cfg: AppConfig,
    session: Session,
    application: Application,
    kinds: tuple[DocumentKind, ...],
    llm_factory: Callable[[], StructuredLLM],
    profile: Profile | None,
    today: date,
) -> list[Document]:
    profile = profile or load_profile(cfg.paths.profile)
    job = _job_offer(session, application)
    llm = llm_factory()
    documents = []
    for kind in kinds:
        if kind is DocumentKind.CV:
            result = generate_cv(
                profile, job, llm=llm, prompts_dir=cfg.paths.prompts_dir, settings=cfg.settings.cv
            )
        else:
            result = generate_cover_letter(
                profile,
                job,
                llm=llm,
                prompts_dir=cfg.paths.prompts_dir,
                settings=cfg.settings.cover_letter,
            )
        documents.append(
            _store_document(
                cfg,
                session,
                application,
                kind,
                result.value,
                profile,
                job,
                [],
                today,
                model=result.model,
                prompt_version=result.prompt_version,
                edited=False,
            )
        )
        session.commit()  # keep the CV even if the letter fails afterwards
    return documents


def _store_document(
    cfg: AppConfig,
    session: Session,
    application: Application,
    kind: DocumentKind,
    content: TailoredCV | CoverLetter,
    profile: Profile,
    job: JobOffer,
    issues: list[Issue],
    today: date,
    *,
    model: str,
    prompt_version: int,
    edited: bool,
) -> Document:
    assert application.id is not None
    version = service.next_document_version(session, application.id, kind)
    output = cfg.paths.output_dir / str(application.id) / f"{kind.value}_v{version}.pdf"
    payload = build_document(cfg, kind, content, profile, job, output, issues, today)
    document = Document(
        application_id=application.id,
        kind=kind,
        path=str(output),
        json_payload=payload,
        version=version,
        model=model,
        prompt_version=prompt_version,
        edited=edited,
    )
    session.add(document)
    action = "modifié à la main" if edited else "généré"
    service.add_event(
        session,
        application.offer_id,
        "generation",
        f"{KIND_LABELS[kind]} v{version} {action}",
        application.id,
    )
    return document


def _finish_generation(session: Session, application: Application, *, error: str | None) -> None:
    assert application.id is not None
    application.generating = False
    application.generation_error = error
    latest = service.latest_documents(session, application.id)
    if application.status is ApplicationStatus.PREPARING and len(latest) == len(DocumentKind):
        application.status = ApplicationStatus.READY
        service.add_event(
            session, application.offer_id, "status", "Candidature prête", application.id
        )
    application.updated_at = utcnow()
    session.add(application)
    session.commit()
