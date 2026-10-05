"""Applications: generated documents (preview, download, regeneration, manual edits) and
tracking (sent date, statuses, follow-ups, interview, notes)."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from jobapply.config import AppConfig, ConfigError
from jobapply.generate.documents import editable_fields
from jobapply.models.db import Application, ApplicationStatus, Document, DocumentKind
from jobapply.models.profile import load_profile
from jobapply.pipeline import (
    DocumentError,
    GenerationInProgressError,
    edit_document,
    generate_documents_in_background,
    start_generation,
)
from jobapply.tracking import service
from jobapply.tracking.service import OfferStateError
from jobapply.web.auth import verify_csrf
from jobapply.web.templating import flash, render

router = APIRouter()
csrf = [Depends(verify_csrf)]
KINDS = {
    "cv": (DocumentKind.CV,),
    "letter": (DocumentKind.LETTER,),
    "all": (DocumentKind.CV, DocumentKind.LETTER),
}


def schedule_generation(
    request: Request,
    tasks: BackgroundTasks,
    application_id: int,
    kinds: tuple[DocumentKind, ...] = (DocumentKind.CV, DocumentKind.LETTER),
) -> None:
    state = request.app.state
    start_generation(state.engine, application_id)
    tasks.add_task(
        generate_documents_in_background,
        cfg=state.cfg,
        engine=state.engine,
        application_id=application_id,
        kinds=kinds,
        llm_factory=state.llm_factory,
    )


def _get_application(session: Session, application_id: int) -> Application:
    application = service.get_application(session, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Candidature introuvable.")
    return application


def _get_document(session: Session, document_id: int) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document introuvable.")
    return document


def _redirect(application_id: int) -> RedirectResponse:
    return RedirectResponse(f"/applications/{application_id}", status_code=303)


def _now() -> datetime:
    return datetime.now(UTC)


def _tz(request: Request) -> ZoneInfo:
    return ZoneInfo(request.app.state.cfg.settings.timezone)


def _follow_up_days(request: Request) -> int:
    return request.app.state.cfg.settings.tracking.follow_up_days


def refresh_ghosted(request: Request, session: Session) -> None:
    service.mark_ghosted(
        session, _now(), request.app.state.cfg.settings.tracking.ghosted_after_days
    )


@router.get("/applications", response_class=HTMLResponse)
def application_list(request: Request, view: str = "active", status: str = "") -> HTMLResponse:
    status_filter = (
        ApplicationStatus(status) if status in ApplicationStatus._value2member_map_ else None
    )
    active = {"active": True, "done": False}.get(view)
    with Session(request.app.state.engine) as session:
        refresh_ghosted(request, session)
        rows = service.list_applications(
            session, status=status_filter, active=None if status_filter else active
        )
        return render(
            request,
            "applications_list.html",
            rows=rows,
            view=view,
            status_filter=status_filter,
            FOLLOW_UP_STATUSES=service.FOLLOW_UP_STATUSES,
        )


@router.get("/applications/{application_id}", response_class=HTMLResponse)
def application_detail(request: Request, application_id: int) -> HTMLResponse:
    with Session(request.app.state.engine) as session:
        refresh_ghosted(request, session)
        application = _get_application(session, application_id)
        offer = service.get_offer(session, application.offer_id)
        assert offer is not None
        return render(
            request,
            "application_detail.html",
            application=application,
            offer=offer,
            job=offer.to_job_offer(),
            latest=service.latest_documents(session, application_id),
            documents=service.list_documents(session, application_id),
            events=service.list_events(session, offer.id or 0),
            DocumentKind=DocumentKind,
            ApplicationStatus=ApplicationStatus,
            MANUAL_STATUSES=service.MANUAL_STATUSES,
            FOLLOW_UP_STATUSES=service.FOLLOW_UP_STATUSES,
            today=_now().astimezone(_tz(request)).date().isoformat(),
            now=_now(),
        )


TrackingAction = Callable[[Session, Application, datetime], None]


def _track(request: Request, application_id: int, action: TrackingAction, message: str) -> Response:
    """Run one tracking action on an application, flash the outcome, back to its page."""
    with Session(request.app.state.engine) as session:
        application = _get_application(session, application_id)
        try:
            action(session, application, _now())
        except OfferStateError as exc:
            flash(request, str(exc), "error")
            return _redirect(application_id)
    flash(request, message, "success")
    return _redirect(application_id)


@router.get("/applications/{application_id}/status", response_class=HTMLResponse)
def generation_status(request: Request, application_id: int) -> Response:
    """Polled by the application page while documents are being generated."""
    with Session(request.app.state.engine) as session:
        application = _get_application(session, application_id)
        if application.generating:
            return render(request, "_generation_pending.html", application=application)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/applications/{application_id}/generate", dependencies=csrf)
def regenerate(
    request: Request,
    tasks: BackgroundTasks,
    application_id: int,
    kind: Annotated[str, Form()] = "all",
) -> Response:
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail="Type de document inconnu.")
    with Session(request.app.state.engine) as session:
        _get_application(session, application_id)
    try:
        schedule_generation(request, tasks, application_id, KINDS[kind])
    except GenerationInProgressError as exc:
        flash(request, str(exc), "warning")
        return _redirect(application_id)
    flash(request, "Génération lancée.")
    return _redirect(application_id)


def _download_name(cfg: AppConfig, document: Document, company: str | None) -> str:
    try:
        name = load_profile(cfg.paths.profile).identity.name
    except ConfigError:  # the file name is cosmetic; never fail a download because of it
        name = "Candidate"
    label = "CV" if document.kind is DocumentKind.CV else "Cover letter"
    raw = f"{label} - {name} - {company or 'Company'}.pdf"
    return re.sub(r"[^\w .()-]", "", raw)


@router.get("/documents/{document_id}/pdf")
def document_pdf(request: Request, document_id: int, download: bool = False) -> Response:
    cfg: AppConfig = request.app.state.cfg
    with Session(request.app.state.engine) as session:
        document = _get_document(session, document_id)
        application = _get_application(session, document.application_id)
        offer = service.get_offer(session, application.offer_id)
    path = Path(document.path).resolve()
    if not path.is_relative_to(cfg.paths.output_dir.resolve()) or not path.is_file():
        raise HTTPException(status_code=404, detail="Fichier PDF introuvable.")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=_download_name(cfg, document, offer.company if offer else None),
        content_disposition_type="attachment" if download else "inline",
    )


@router.get("/documents/{document_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, document_id: int) -> HTMLResponse:
    with Session(request.app.state.engine) as session:
        document = _get_document(session, document_id)
        fields = editable_fields(document.kind, document.json_payload["content"])
        return render(request, "document_edit.html", document=document, fields=fields)


@router.post("/documents/{document_id}/edit", dependencies=csrf)
async def edit_submit(request: Request, document_id: int) -> Response:
    form = await request.form()
    with Session(request.app.state.engine) as session:
        document = _get_document(session, document_id)
        current = {
            f.path: f.text for f in editable_fields(document.kind, document.json_payload["content"])
        }
        application_id = document.application_id
    edits = {
        path: value
        for path, value in form.items()
        if path in current and isinstance(value, str) and value.strip() != current[path]
    }
    if not edits:
        flash(request, "Aucune modification.")
        return _redirect(application_id)
    try:
        new = edit_document(
            cfg=request.app.state.cfg,
            engine=request.app.state.engine,
            document_id=document_id,
            edits=edits,
        )
    except DocumentError as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(f"/documents/{document_id}/edit", status_code=303)
    issues = new.json_payload["issues"]
    if issues:
        flash(
            request,
            f"Version {new.version} enregistrée, avec {len(issues)} avertissement(s) du "
            "validateur : vérifie les faits avant d'envoyer.",
            "warning",
        )
    else:
        flash(request, f"Version {new.version} enregistrée.", "success")
    return _redirect(application_id)


# --- Tracking actions ---------------------------------------------------------------------


@router.post("/applications/{application_id}/applied", dependencies=csrf)
def mark_applied(
    request: Request, application_id: int, applied_on: Annotated[str, Form()] = ""
) -> Response:
    tz = _tz(request)
    today = _now().astimezone(tz).date()
    try:
        day = date.fromisoformat(applied_on) if applied_on else today
    except ValueError:
        flash(request, "Date invalide.", "error")
        return _redirect(application_id)
    if day > today:
        flash(request, "La date d'envoi ne peut pas être dans le futur.", "error")
        return _redirect(application_id)
    applied_at = datetime.combine(day, time(12, 0), tzinfo=tz).astimezone(UTC)
    days = _follow_up_days(request)
    return _track(
        request,
        application_id,
        lambda session, app, now: service.mark_applied(session, app, applied_at, days),
        "Candidature marquée comme envoyée. Première relance programmée.",
    )


@router.post("/applications/{application_id}/set-status", dependencies=csrf)
def set_status(request: Request, application_id: int, status: Annotated[str, Form()]) -> Response:
    if status not in ApplicationStatus._value2member_map_:
        raise HTTPException(status_code=400, detail="Statut inconnu.")
    new_status, days = ApplicationStatus(status), _follow_up_days(request)
    return _track(
        request,
        application_id,
        lambda session, app, now: service.change_status(session, app, new_status, now, days),
        "Statut mis à jour.",
    )


@router.post("/applications/{application_id}/follow-up", dependencies=csrf)
def follow_up(request: Request, application_id: int) -> Response:
    days = _follow_up_days(request)
    return _track(
        request,
        application_id,
        lambda session, app, now: service.record_follow_up(session, app, now, days),
        "Relance enregistrée. Prochaine relance programmée.",
    )


@router.post("/applications/{application_id}/snooze", dependencies=csrf)
def snooze(request: Request, application_id: int, days: Annotated[int, Form()] = 3) -> Response:
    if not 1 <= days <= 60:
        raise HTTPException(status_code=400, detail="Report entre 1 et 60 jours.")
    return _track(
        request,
        application_id,
        lambda session, app, now: service.snooze_follow_up(session, app, days, now),
        "Relance reportée.",
    )


@router.post("/applications/{application_id}/interview", dependencies=csrf)
def interview(request: Request, application_id: int, when: Annotated[str, Form()] = "") -> Response:
    try:
        local = datetime.fromisoformat(when) if when else None
    except ValueError:
        flash(request, "Date d'entretien invalide.", "error")
        return _redirect(application_id)
    moment = local.replace(tzinfo=_tz(request)).astimezone(UTC) if local else None
    return _track(
        request,
        application_id,
        lambda session, app, now: service.set_interview(session, app, moment, now),
        "Entretien enregistré." if moment else "Date d'entretien supprimée.",
    )


@router.post("/applications/{application_id}/notes", dependencies=csrf)
def save_notes(
    request: Request, application_id: int, notes: Annotated[str, Form()] = ""
) -> Response:
    return _track(
        request,
        application_id,
        lambda session, app, now: service.update_notes(session, app, notes, now),
        "Notes enregistrées.",
    )


@router.post("/applications/{application_id}/note", dependencies=csrf)
def add_note(request: Request, application_id: int, text: Annotated[str, Form()] = "") -> Response:
    return _track(
        request,
        application_id,
        lambda session, app, now: service.add_note(session, app, text, now),
        "Note ajoutée à l'historique.",
    )
