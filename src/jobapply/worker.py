"""Background collection (SPEC sections 6 and 8.5).

A run = collect every enabled source -> pre-filter -> store new offers -> analyse the pending
automatic offers while the daily LLM budget allows. Runs are serialised by a lock, whether
they come from the schedule, the "Collecter maintenant" button or the CLI.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from jobapply.config import AppConfig, SourceSpec
from jobapply.llm.client import StructuredLLM, summarize_costs
from jobapply.models.db import Offer, OfferSource, OfferStatus, SourceRun, utcnow
from jobapply.pipeline import AnalysisError, analyze_offer
from jobapply.sources import COLLECTORS, OFFER_SOURCES
from jobapply.sources.base import CollectedOffer, SourceError, make_client
from jobapply.sources.filters import prefilter
from jobapply.tracking import service

logger = logging.getLogger(__name__)
_run_lock = threading.Lock()


class CollectionInProgressError(Exception):
    pass


@dataclass
class SyncReport:
    runs: list[SourceRun] = field(default_factory=list)
    analyzed: int = 0
    analysis_errors: int = 0
    budget_reached: bool = False

    @property
    def new(self) -> int:
        return sum(r.new for r in self.runs)


def is_running() -> bool:
    return _run_lock.locked()


# --- Collection ---------------------------------------------------------------------------


def _is_known(session: Session, source: OfferSource, offer: CollectedOffer) -> bool:
    by_id = session.exec(
        select(Offer).where(Offer.source == source, Offer.external_id == offer.external_id)
    ).first()
    if by_id is not None:
        return True
    if offer.url and service.find_offer_by_url(session, offer.url) is not None:
        return True
    return bool(offer.text) and service.find_offer_by_text(session, offer.text) is not None


def collect_source(
    cfg: AppConfig, engine: Engine, spec: SourceSpec, client: httpx.Client
) -> SourceRun:
    run = SourceRun(source_name=spec.name)
    source = OFFER_SOURCES[spec.type]
    try:
        collected = COLLECTORS[spec.type](spec, client)
    except SourceError as exc:
        run.error = str(exc)
        collected = []
    run.fetched = len(collected)

    with Session(engine) as session:
        for item in collected:
            result = prefilter(item, cfg.sources.filters)
            if not result.store:
                run.out_of_area += 1
                continue
            if _is_known(session, source, item):
                run.duplicates += 1
                continue
            # Too-short descriptions are stored without text: "Réanalyser" fetches the page once.
            has_text = len(item.text) >= 200
            offer = service.create_offer(
                session,
                url=item.url,
                raw_text=item.text if has_text else None,
                source=source,
                external_id=item.external_id,
            )
            offer.title, offer.company = item.title or None, item.company or None
            if result.keep:
                run.new += 1
                session.add(offer)
                session.commit()
            else:
                run.filtered += 1
                service.set_offer_status(session, offer, OfferStatus.FILTERED, result.reason)
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        session.refresh(run)
    if run.error:
        logger.warning("Source %s failed: %s", spec.name, run.error)
    return run


# --- Analysis within budget ---------------------------------------------------------------


def analyze_pending(
    cfg: AppConfig,
    engine: Engine,
    llm_factory: Callable[[], StructuredLLM],
    *,
    today: date | None = None,
) -> tuple[int, int, bool]:
    """Analyse automatic offers still `new`. Returns (analysed, errors, budget_reached)."""
    budget = cfg.sources.budget
    today = today or datetime.now(UTC).date()
    with Session(engine) as session:
        pending = [
            o.id
            for o in session.exec(
                select(Offer)
                .where(Offer.status == OfferStatus.NEW, Offer.source != OfferSource.MANUAL)
                .order_by(col(Offer.created_at), col(Offer.id))
            )
        ]
    analyzed = errors = 0
    for offer_id in pending:
        if analyzed + errors >= budget.max_analyses_per_run:
            break
        if summarize_costs(cfg.paths.llm_log, today).today_usd >= budget.daily_usd:
            return analyzed, errors, True
        try:
            analyze_offer(cfg=cfg, engine=engine, offer_id=offer_id or 0, llm_factory=llm_factory)
            analyzed += 1
        except AnalysisError:
            errors += 1
    return analyzed, errors, False


def run_collection(
    cfg: AppConfig,
    engine: Engine,
    llm_factory: Callable[[], StructuredLLM],
    *,
    client: httpx.Client | None = None,
    analyze: bool = True,
    today: date | None = None,
) -> SyncReport:
    if not _run_lock.acquire(blocking=False):
        raise CollectionInProgressError("Une collecte est déjà en cours.")
    try:
        report = SyncReport()
        http = client or make_client()
        try:
            for spec in cfg.sources.active:
                report.runs.append(collect_source(cfg, engine, spec, http))
        finally:
            if client is None:
                http.close()
        if analyze:
            report.analyzed, report.analysis_errors, report.budget_reached = analyze_pending(
                cfg, engine, llm_factory, today=today
            )
        logger.info(
            "Collection done: %d new offers, %d analysed, budget reached: %s",
            report.new,
            report.analyzed,
            report.budget_reached,
        )
        return report
    finally:
        _run_lock.release()


def run_collection_in_background(**kwargs: object) -> None:
    try:
        run_collection(**kwargs)  # type: ignore[arg-type]
    except CollectionInProgressError:
        logger.info("Collection already running, skipped")
    except Exception:
        logger.exception("Collection failed")


# --- Schedule -----------------------------------------------------------------------------


def last_run_at(engine: Engine) -> datetime | None:
    with Session(engine) as session:
        run = session.exec(select(SourceRun).order_by(col(SourceRun.started_at).desc())).first()
    return service.as_utc(run.started_at) if run else None


def seconds_until_next_run(cfg: AppConfig, engine: Engine, now: datetime) -> float:
    interval = timedelta(hours=cfg.sources.schedule.interval_hours)
    last = last_run_at(engine)
    if last is None:
        return 60.0  # first run shortly after startup, not during it
    return max(60.0, (last + interval - now).total_seconds())


async def schedule_loop(
    cfg: AppConfig, engine: Engine, llm_factory: Callable[[], StructuredLLM]
) -> None:
    """Runs forever inside the web app (cancelled at shutdown)."""
    while True:
        await asyncio.sleep(seconds_until_next_run(cfg, engine, datetime.now(UTC)))
        await asyncio.to_thread(
            run_collection_in_background, cfg=cfg, engine=engine, llm_factory=llm_factory
        )
