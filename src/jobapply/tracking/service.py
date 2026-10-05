"""Persistence services: offers, decisions, applications and their history."""

from __future__ import annotations

import hashlib
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlmodel import Session, col, select

from jobapply.models.db import (
    Analysis,
    Application,
    ApplicationStatus,
    Document,
    DocumentKind,
    Event,
    Offer,
    OfferSource,
    OfferStatus,
    utcnow,
)

TRACKING_PARAMS = ("utm_", "ref", "trk", "src", "source", "gclid", "fbclid")


class OfferStateError(ValueError):
    """The requested action is not allowed in the offer's current status."""


def text_hash(raw_text: str) -> str:
    """Whitespace-insensitive hash, used to detect an offer that was already added."""
    normalized = " ".join(raw_text.split()).casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_url(url: str) -> str:
    """Drop fragment, tracking parameters and trailing slash, so the same offer has one URL."""
    parts = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(TRACKING_PARAMS)]
    path = parts.path.rstrip("/") or "/"
    return urlunparse((parts.scheme.lower(), parts.netloc.lower(), path, "", urlencode(query), ""))


# --- Offers -------------------------------------------------------------------------------


def find_offer_by_url(session: Session, url: str) -> Offer | None:
    return session.exec(select(Offer).where(Offer.url == normalize_url(url))).first()


def find_offer_by_text(session: Session, raw_text: str) -> Offer | None:
    return session.exec(select(Offer).where(Offer.text_hash == text_hash(raw_text))).first()


def create_offer(
    session: Session,
    *,
    url: str | None = None,
    raw_text: str | None = None,
    source: OfferSource = OfferSource.MANUAL,
    external_id: str | None = None,
) -> Offer:
    offer = Offer(
        source=source,
        external_id=external_id,
        url=normalize_url(url) if url else None,
        raw_text=raw_text,
        text_hash=text_hash(raw_text) if raw_text else None,
    )
    session.add(offer)
    session.flush()
    assert offer.id is not None
    add_event(session, offer.id, "status", "Offre ajoutée")
    session.commit()
    session.refresh(offer)
    return offer


def get_offer(session: Session, offer_id: int) -> Offer | None:
    return session.get(Offer, offer_id)


def set_offer_status(
    session: Session, offer: Offer, status: OfferStatus, message: str | None = None
) -> None:
    offer.status = status
    offer.status_message = message
    offer.updated_at = utcnow()
    session.add(offer)
    session.commit()


def latest_analysis(session: Session, offer_id: int) -> Analysis | None:
    return session.exec(
        select(Analysis)
        .where(Analysis.offer_id == offer_id)
        .order_by(col(Analysis.created_at).desc(), col(Analysis.id).desc())
    ).first()


def list_offers(
    session: Session,
    *,
    status: OfferStatus | None = None,
    visa_verdict: str | None = None,
    min_score: int | None = None,
) -> list[tuple[Offer, Analysis | None]]:
    """Offers with their latest analysis, best score first, then newest."""
    query = select(Offer)
    if status is not None:
        query = query.where(Offer.status == status)
    rows = [(o, latest_analysis(session, o.id)) for o in session.exec(query) if o.id is not None]
    if visa_verdict:
        rows = [(o, a) for o, a in rows if a is not None and a.visa_verdict == visa_verdict]
    if min_score is not None:
        rows = [(o, a) for o, a in rows if a is not None and a.score >= min_score]
    rows.sort(key=lambda r: (r[1].score if r[1] else -1, r[0].created_at), reverse=True)
    return rows


def offer_status_counts(session: Session) -> Counter[OfferStatus]:
    return Counter(o.status for o in session.exec(select(Offer)))


def reset_interrupted_analyses(session: Session) -> int:
    """Analyses still 'analyzing' at startup were interrupted by a restart."""
    stuck = list(session.exec(select(Offer).where(Offer.status == OfferStatus.ANALYZING)))
    for offer in stuck:
        offer.status = OfferStatus.ERROR
        offer.status_message = "Analyse interrompue (redémarrage) : relance-la."
        session.add(offer)
    session.commit()
    return len(stuck)


def reset_interrupted_generations(session: Session) -> int:
    stuck = list(session.exec(select(Application).where(col(Application.generating).is_(True))))
    for application in stuck:
        application.generating = False
        application.generation_error = "Génération interrompue (redémarrage) : relance-la."
        session.add(application)
    session.commit()
    return len(stuck)


# --- Decisions ----------------------------------------------------------------------------


def accept_offer(session: Session, offer: Offer) -> Application:
    if offer.status is not OfferStatus.ANALYZED:
        raise OfferStateError("Seule une offre analysée peut être acceptée.")
    assert offer.id is not None
    application = Application(offer_id=offer.id)
    session.add(application)
    offer.status = OfferStatus.ACCEPTED
    offer.updated_at = utcnow()
    session.add(offer)
    session.flush()
    add_event(session, offer.id, "status", "Je postule : candidature créée", application.id)
    session.commit()
    session.refresh(application)
    return application


def dismiss_offer(session: Session, offer: Offer) -> None:
    if offer.status not in (OfferStatus.ANALYZED, OfferStatus.ERROR, OfferStatus.FILTERED):
        raise OfferStateError("Cette offre ne peut pas être ignorée dans son état actuel.")
    assert offer.id is not None
    add_event(session, offer.id, "status", "Offre ignorée")
    set_offer_status(session, offer, OfferStatus.DISMISSED)


def restore_offer(session: Session, offer: Offer) -> None:
    if offer.status is not OfferStatus.DISMISSED:
        raise OfferStateError("Seule une offre ignorée peut être rétablie.")
    assert offer.id is not None
    analysed = latest_analysis(session, offer.id) is not None
    add_event(session, offer.id, "status", "Offre rétablie")
    set_offer_status(session, offer, OfferStatus.ANALYZED if analysed else OfferStatus.NEW)


def get_application_for_offer(session: Session, offer_id: int) -> Application | None:
    return session.exec(select(Application).where(Application.offer_id == offer_id)).first()


def get_application(session: Session, application_id: int) -> Application | None:
    return session.get(Application, application_id)


def list_documents(session: Session, application_id: int) -> list[Document]:
    """All versions, newest first."""
    return list(
        session.exec(
            select(Document)
            .where(Document.application_id == application_id)
            .order_by(col(Document.version).desc(), col(Document.id).desc())
        )
    )


def latest_documents(session: Session, application_id: int) -> dict[DocumentKind, Document]:
    latest: dict[DocumentKind, Document] = {}
    for document in list_documents(session, application_id):
        latest.setdefault(document.kind, document)
    return latest


def next_document_version(session: Session, application_id: int, kind: DocumentKind) -> int:
    versions = [d.version for d in list_documents(session, application_id) if d.kind is kind]
    return max(versions, default=0) + 1


def application_status_counts(session: Session) -> Counter[ApplicationStatus]:
    return Counter(a.status for a in session.exec(select(Application)))


# --- History ------------------------------------------------------------------------------


def add_event(
    session: Session,
    offer_id: int,
    kind: str,
    message: str,
    application_id: int | None = None,
) -> None:
    session.add(Event(offer_id=offer_id, application_id=application_id, kind=kind, message=message))


def list_events(session: Session, offer_id: int) -> list[Event]:
    return list(
        session.exec(
            select(Event)
            .where(Event.offer_id == offer_id)
            .order_by(col(Event.created_at).desc(), col(Event.id).desc())
        )
    )


# --- Application tracking (SPEC section 8.4) ---------------------------------------------

ACTIVE_STATUSES = frozenset(
    {
        ApplicationStatus.PREPARING,
        ApplicationStatus.READY,
        ApplicationStatus.APPLIED,
        ApplicationStatus.INTERVIEW,
    }
)
FOLLOW_UP_STATUSES = frozenset({ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW})
# Statuses the user can pick by hand. PREPARING is set by the generation only.
MANUAL_STATUSES = tuple(s for s in ApplicationStatus if s is not ApplicationStatus.PREPARING)
STATUS_EVENT = {
    ApplicationStatus.READY: "Candidature remise à « prête »",
    ApplicationStatus.APPLIED: "Candidature envoyée",
    ApplicationStatus.INTERVIEW: "Entretien obtenu",
    ApplicationStatus.OFFER: "Offre d'emploi reçue",
    ApplicationStatus.REJECTED: "Candidature refusée",
    ApplicationStatus.GHOSTED: "Sans réponse",
    ApplicationStatus.WITHDRAWN: "Candidature retirée",
}


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; they are stored in UTC."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _touch(session: Session, application: Application, now: datetime) -> None:
    application.updated_at = now
    session.add(application)
    session.commit()


def _event(session: Session, application: Application, kind: str, message: str) -> None:
    add_event(session, application.offer_id, kind, message, application.id)


def mark_applied(
    session: Session, application: Application, applied_at: datetime, follow_up_days: int
) -> None:
    """I sent the application myself: record the date, the documents sent, the first follow-up."""
    if application.status not in (ApplicationStatus.PREPARING, ApplicationStatus.READY):
        raise OfferStateError("Cette candidature est déjà marquée comme envoyée.")
    assert application.id is not None
    application.status = ApplicationStatus.APPLIED
    application.applied_at = applied_at
    application.next_follow_up_at = applied_at + timedelta(days=follow_up_days)
    for document in latest_documents(session, application.id).values():
        document.sent = True
        session.add(document)
    _event(session, application, "status", f"Candidature envoyée le {applied_at:%d/%m/%Y}")
    _touch(session, application, applied_at)


def change_status(
    session: Session,
    application: Application,
    status: ApplicationStatus,
    now: datetime,
    follow_up_days: int,
) -> None:
    if status not in MANUAL_STATUSES:
        raise OfferStateError("Statut non modifiable à la main.")
    if status is application.status:
        return
    if status is ApplicationStatus.APPLIED and application.applied_at is None:
        mark_applied(session, application, now, follow_up_days)
        return
    application.status = status
    if status in FOLLOW_UP_STATUSES:
        application.next_follow_up_at = now + timedelta(days=follow_up_days)
    else:
        application.next_follow_up_at = None
    _event(session, application, "status", STATUS_EVENT[status])
    _touch(session, application, now)


def record_follow_up(
    session: Session, application: Application, now: datetime, follow_up_days: int
) -> None:
    if application.status not in FOLLOW_UP_STATUSES:
        raise OfferStateError("Aucune relance prévue pour cette candidature.")
    application.follow_up_count += 1
    application.last_follow_up_at = now
    application.next_follow_up_at = now + timedelta(days=follow_up_days)
    _event(session, application, "follow_up", f"Relance n°{application.follow_up_count} faite")
    _touch(session, application, now)


def snooze_follow_up(session: Session, application: Application, days: int, now: datetime) -> None:
    if application.status not in FOLLOW_UP_STATUSES or application.next_follow_up_at is None:
        raise OfferStateError("Aucune relance prévue pour cette candidature.")
    application.next_follow_up_at = now + timedelta(days=days)
    _event(session, application, "follow_up", f"Relance reportée de {days} jour(s)")
    _touch(session, application, now)


def set_interview(
    session: Session, application: Application, when: datetime | None, now: datetime
) -> None:
    application.interview_at = when
    if when is not None:
        if application.status is not ApplicationStatus.INTERVIEW:
            application.status = ApplicationStatus.INTERVIEW
        application.next_follow_up_at = None  # follow up after the interview instead
        _event(session, application, "interview", f"Entretien prévu le {when:%d/%m/%Y à %H:%M}")
    else:
        _event(session, application, "interview", "Date d'entretien supprimée")
    _touch(session, application, now)


def update_notes(session: Session, application: Application, notes: str, now: datetime) -> None:
    application.notes = notes.strip()
    _touch(session, application, now)


def add_note(session: Session, application: Application, text: str, now: datetime) -> None:
    text = text.strip()
    if not text:
        raise OfferStateError("La note est vide.")
    _event(session, application, "note", text)
    _touch(session, application, now)


def mark_ghosted(session: Session, now: datetime, ghosted_after_days: int) -> int:
    """Applications with no news for `ghosted_after_days` since the last contact."""
    limit = now - timedelta(days=ghosted_after_days)
    count = 0
    query = select(Application).where(Application.status == ApplicationStatus.APPLIED)
    for application in session.exec(query):
        contacts = [as_utc(application.applied_at), as_utc(application.last_follow_up_at)]
        last_contact = max((c for c in contacts if c is not None), default=None)
        if last_contact is not None and last_contact <= limit:
            application.status = ApplicationStatus.GHOSTED
            application.next_follow_up_at = None
            application.updated_at = now
            session.add(application)
            _event(
                session,
                application,
                "status",
                f"Passée en « sans réponse » après {ghosted_after_days} jours sans nouvelles",
            )
            count += 1
    session.commit()
    return count


def list_applications(
    session: Session, *, status: ApplicationStatus | None = None, active: bool | None = None
) -> list[tuple[Application, Offer]]:
    """Applications with their offer, most recently updated first."""
    query = select(Application, Offer).where(Application.offer_id == Offer.id)
    if status is not None:
        query = query.where(Application.status == status)
    rows = [(a, o) for a, o in session.exec(query)]
    if active is not None:
        rows = [(a, o) for a, o in rows if (a.status in ACTIVE_STATUSES) is active]
    rows.sort(
        key=lambda r: as_utc(r[0].updated_at) or datetime.min.replace(tzinfo=UTC), reverse=True
    )
    return rows


def _sort_key(value: datetime | None) -> datetime:
    return as_utc(value) or datetime.max.replace(tzinfo=UTC)


def due_follow_ups(session: Session, now: datetime) -> list[tuple[Application, Offer]]:
    due = [
        (a, o)
        for a, o in list_applications(session)
        if a.status in FOLLOW_UP_STATUSES
        and a.next_follow_up_at is not None
        and _sort_key(a.next_follow_up_at) <= now
    ]
    return sorted(due, key=lambda r: _sort_key(r[0].next_follow_up_at))


def upcoming_interviews(session: Session, now: datetime) -> list[tuple[Application, Offer]]:
    upcoming = [
        (a, o)
        for a, o in list_applications(session, status=ApplicationStatus.INTERVIEW)
        if a.interview_at is not None and _sort_key(a.interview_at) >= now
    ]
    return sorted(upcoming, key=lambda r: _sort_key(r[0].interview_at))
