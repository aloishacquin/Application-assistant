"""Offers: list, submission, detail, analysis status, decisions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session

from jobapply.models.db import Offer, OfferStatus
from jobapply.pipeline import analyze_offer_in_background, submit_offer
from jobapply.tracking import service
from jobapply.tracking.service import OfferStateError
from jobapply.web.auth import verify_csrf
from jobapply.web.routes.applications import schedule_generation
from jobapply.web.templating import flash, render

router = APIRouter(prefix="/offers")
csrf = [Depends(verify_csrf)]


def _schedule_analysis(
    request: Request, tasks: BackgroundTasks, offer_id: int, *, reextract: bool = False
) -> None:
    state = request.app.state
    tasks.add_task(
        analyze_offer_in_background,
        cfg=state.cfg,
        engine=state.engine,
        offer_id=offer_id,
        llm_factory=state.llm_factory,
        fetch=state.fetch,
        reextract=reextract,
    )


def _get_offer(session: Session, offer_id: int) -> Offer:
    offer = service.get_offer(session, offer_id)
    if offer is None:
        raise HTTPException(status_code=404, detail="Offre introuvable.")
    return offer


def _redirect(offer_id: int) -> RedirectResponse:
    return RedirectResponse(f"/offers/{offer_id}", status_code=303)


@router.get("", response_class=HTMLResponse)
def list_offers(
    request: Request, status: str = "", visa: str = "", min_score: str = ""
) -> HTMLResponse:
    status_filter = OfferStatus(status) if status in OfferStatus._value2member_map_ else None
    score_filter = int(min_score) if min_score.isdigit() else None
    with Session(request.app.state.engine) as session:
        rows = service.list_offers(
            session, status=status_filter, visa_verdict=visa or None, min_score=score_filter
        )
        if status_filter is None:  # pre-filtered offers only show when asked for
            rows = [(o, a) for o, a in rows if o.status is not OfferStatus.FILTERED]
    return render(
        request,
        "offers_list.html",
        rows=rows,
        filters={"status": status, "visa": visa, "min_score": min_score},
    )


@router.get("/new", response_class=HTMLResponse)
def new_offer_form(request: Request) -> HTMLResponse:
    return render(request, "offer_new.html", url="", text="")


@router.post("", dependencies=csrf)
def create_offer(
    request: Request,
    tasks: BackgroundTasks,
    url: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
) -> Response:
    try:
        result = submit_offer(request.app.state.engine, url=url, text=text)
    except ValueError as exc:
        return render(
            request, "offer_new.html", status_code=400, error=str(exc), url=url, text=text
        )
    if result.created:
        _schedule_analysis(request, tasks, result.offer_id)
        flash(request, "Offre ajoutée : analyse en cours.")
    else:
        flash(request, "Cette offre est déjà enregistrée.", "warning")
    return _redirect(result.offer_id)


@router.get("/{offer_id}", response_class=HTMLResponse)
def offer_detail(request: Request, offer_id: int) -> HTMLResponse:
    with Session(request.app.state.engine) as session:
        offer = _get_offer(session, offer_id)
        return render(
            request,
            "offer_detail.html",
            offer=offer,
            job=offer.to_job_offer(),
            analysis=service.latest_analysis(session, offer_id),
            application=service.get_application_for_offer(session, offer_id),
            events=service.list_events(session, offer_id),
        )


@router.get("/{offer_id}/status", response_class=HTMLResponse)
def offer_status(request: Request, offer_id: int) -> Response:
    """Polled by the detail page while the analysis runs."""
    with Session(request.app.state.engine) as session:
        offer = _get_offer(session, offer_id)
        if offer.status in (OfferStatus.NEW, OfferStatus.ANALYZING):
            return render(request, "_analysis_pending.html", offer=offer)
    return Response(status_code=200, headers={"HX-Refresh": "true"})


@router.post("/{offer_id}/reanalyze", dependencies=csrf)
def reanalyze(
    request: Request,
    tasks: BackgroundTasks,
    offer_id: int,
    reextract: Annotated[bool, Form()] = False,
) -> Response:
    with Session(request.app.state.engine) as session:
        offer = _get_offer(session, offer_id)
        if offer.status is OfferStatus.ANALYZING:
            flash(request, "Une analyse est déjà en cours.", "warning")
            return _redirect(offer_id)
        if offer.status is OfferStatus.ACCEPTED:
            flash(request, "Offre déjà acceptée : l'analyse n'est plus modifiée.", "warning")
            return _redirect(offer_id)
        service.set_offer_status(session, offer, OfferStatus.NEW)
    _schedule_analysis(request, tasks, offer_id, reextract=reextract)
    flash(request, "Nouvelle analyse lancée.")
    return _redirect(offer_id)


@router.post("/{offer_id}/text", dependencies=csrf)
def provide_text(
    request: Request, tasks: BackgroundTasks, offer_id: int, text: Annotated[str, Form()]
) -> Response:
    """Fallback when the link could not be fetched: the user pastes the offer text."""
    text = text.strip()
    if not text:
        flash(request, "Le texte de l'offre est vide.", "error")
        return _redirect(offer_id)
    with Session(request.app.state.engine) as session:
        offer = _get_offer(session, offer_id)
        duplicate = service.find_offer_by_text(session, text)
        if duplicate is not None and duplicate.id != offer_id:
            flash(request, f"Ce texte correspond déjà à l'offre #{duplicate.id}.", "warning")
            return _redirect(duplicate.id or offer_id)
        offer.raw_text = text
        offer.text_hash = service.text_hash(text)
        offer.duplicate_of = None
        service.set_offer_status(session, offer, OfferStatus.NEW)
    _schedule_analysis(request, tasks, offer_id, reextract=True)
    flash(request, "Texte enregistré : analyse en cours.")
    return _redirect(offer_id)


def _decide(request: Request, offer_id: int, action: str) -> Response:
    with Session(request.app.state.engine) as session:
        offer = _get_offer(session, offer_id)
        try:
            if action == "dismiss":
                service.dismiss_offer(session, offer)
                flash(request, "Offre ignorée.")
            else:
                service.restore_offer(session, offer)
                flash(request, "Offre rétablie.")
        except OfferStateError as exc:
            flash(request, str(exc), "error")
    return _redirect(offer_id)


@router.post("/{offer_id}/accept", dependencies=csrf)
def accept(request: Request, tasks: BackgroundTasks, offer_id: int) -> Response:
    """Create the application and start generating the CV and the letter right away."""
    with Session(request.app.state.engine) as session:
        offer = _get_offer(session, offer_id)
        try:
            application = service.accept_offer(session, offer)
        except OfferStateError as exc:
            flash(request, str(exc), "error")
            return _redirect(offer_id)
    assert application.id is not None
    schedule_generation(request, tasks, application.id)
    flash(request, "Candidature créée : génération du CV et de la lettre en cours.", "success")
    return RedirectResponse(f"/applications/{application.id}", status_code=303)


@router.post("/{offer_id}/dismiss", dependencies=csrf)
def dismiss(request: Request, offer_id: int) -> Response:
    return _decide(request, offer_id, "dismiss")


@router.post("/{offer_id}/restore", dependencies=csrf)
def restore(request: Request, offer_id: int) -> Response:
    return _decide(request, offer_id, "restore")
