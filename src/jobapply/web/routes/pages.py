"""Dashboard and profile pages."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from jobapply.config import AppConfig, ConfigError
from jobapply.llm.client import summarize_costs
from jobapply.models.db import ApplicationStatus, OfferStatus
from jobapply.models.profile import load_profile
from jobapply.tracking import service
from jobapply.web.templating import render

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    cfg: AppConfig = request.app.state.cfg
    now = datetime.now(UTC)
    with Session(request.app.state.engine) as session:
        service.mark_ghosted(session, now, cfg.settings.tracking.ghosted_after_days)
        return render(
            request,
            "dashboard.html",
            offer_counts=service.offer_status_counts(session),
            application_counts=service.application_status_counts(session),
            to_review=service.list_offers(session, status=OfferStatus.ANALYZED)[:5],
            due=service.due_follow_ups(session, now),
            interviews=service.upcoming_interviews(session, now),
            costs=summarize_costs(cfg.paths.llm_log, now.date()),
            ApplicationStatus=ApplicationStatus,
        )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request) -> HTMLResponse:
    cfg: AppConfig = request.app.state.cfg
    try:
        profile, error = load_profile(cfg.paths.profile), None
    except ConfigError as exc:
        profile, error = None, str(exc)
    return render(
        request, "profile.html", profile=profile, error=error, profile_path=cfg.paths.profile
    )
