"""Sources page: configured collectors, recent runs, manual collection."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlmodel import Session, col, select

from jobapply.config import AppConfig
from jobapply.llm.client import summarize_costs
from jobapply.models.db import SourceRun
from jobapply.web.auth import verify_csrf
from jobapply.web.templating import flash, render
from jobapply.worker import is_running, run_collection_in_background

router = APIRouter(prefix="/sources")


@router.get("", response_class=HTMLResponse)
def sources_page(request: Request) -> HTMLResponse:
    cfg: AppConfig = request.app.state.cfg
    with Session(request.app.state.engine) as session:
        runs = list(
            session.exec(select(SourceRun).order_by(col(SourceRun.started_at).desc()).limit(30))
        )
    last_by_source: dict[str, SourceRun] = {}
    for run in runs:
        last_by_source.setdefault(run.source_name, run)
    return render(
        request,
        "sources.html",
        sources=cfg.sources,
        runs=runs,
        last_by_source=last_by_source,
        running=is_running(),
        costs=summarize_costs(cfg.paths.llm_log, datetime.now(UTC).date()),
    )


@router.post("/sync", dependencies=[Depends(verify_csrf)])
def sync_now(request: Request, tasks: BackgroundTasks) -> Response:
    state = request.app.state
    if not state.cfg.sources.active:
        flash(request, "Aucune source active dans config/sources.yaml.", "warning")
    elif is_running():
        flash(request, "Une collecte est déjà en cours.", "warning")
    else:
        tasks.add_task(
            run_collection_in_background,
            cfg=state.cfg,
            engine=state.engine,
            llm_factory=state.llm_factory,
        )
        flash(request, "Collecte lancée : les nouvelles offres apparaîtront dans « Offres ».")
    return RedirectResponse("/sources", status_code=303)
