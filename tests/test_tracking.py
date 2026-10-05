from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session

from jobapply.config import AppConfig
from jobapply.models.db import (
    Application,
    ApplicationStatus,
    Document,
    DocumentKind,
    OfferStatus,
    get_engine,
)
from jobapply.tracking import service
from jobapply.tracking.service import OfferStateError

NOW = datetime(2026, 10, 10, 9, 0, tzinfo=UTC)
DAYS = 7


@pytest.fixture
def session(tmp_config: AppConfig):
    with Session(get_engine(tmp_config.paths.db)) as s:
        yield s


def make_application(session, status=ApplicationStatus.READY, label="offer") -> Application:
    offer = service.create_offer(session, raw_text=f"text of {label}")
    offer.title, offer.company = label, "Company"
    service.set_offer_status(session, offer, OfferStatus.ACCEPTED)
    application = Application(offer_id=offer.id, status=status)
    session.add(application)
    session.commit()
    session.refresh(application)
    return application


def add_document(session, application, kind, version):
    document = Document(
        application_id=application.id,
        kind=kind,
        path="x.pdf",
        json_payload={},
        version=version,
        model="m",
        prompt_version=1,
    )
    session.add(document)
    session.commit()
    return document


def messages(session, application) -> list[str]:
    return [e.message for e in service.list_events(session, application.offer_id)]


def test_mark_applied(session) -> None:
    application = make_application(session)
    old_cv = add_document(session, application, DocumentKind.CV, 1)
    cv = add_document(session, application, DocumentKind.CV, 2)
    letter = add_document(session, application, DocumentKind.LETTER, 1)

    service.mark_applied(session, application, NOW, DAYS)

    assert application.status is ApplicationStatus.APPLIED
    assert service.as_utc(application.applied_at) == NOW
    assert service.as_utc(application.next_follow_up_at) == NOW + timedelta(days=7)
    for document in (old_cv, cv, letter):
        session.refresh(document)
    assert cv.sent and letter.sent and not old_cv.sent  # only the versions actually sent
    assert "Candidature envoyée le 10/10/2026" in messages(session, application)


def test_mark_applied_twice_is_refused(session) -> None:
    application = make_application(session)
    service.mark_applied(session, application, NOW, DAYS)
    with pytest.raises(OfferStateError, match="déjà marquée"):
        service.mark_applied(session, application, NOW, DAYS)


def test_follow_up_cycle(session) -> None:
    application = make_application(session)
    service.mark_applied(session, application, NOW, DAYS)
    assert service.due_follow_ups(session, NOW + timedelta(days=6)) == []
    due_at = NOW + timedelta(days=7)
    assert [a.id for a, _ in service.due_follow_ups(session, due_at)] == [application.id]

    service.record_follow_up(session, application, due_at, DAYS)
    assert application.follow_up_count == 1
    assert service.as_utc(application.next_follow_up_at) == due_at + timedelta(days=7)
    assert service.due_follow_ups(session, due_at) == []
    assert "Relance n°1 faite" in messages(session, application)


def test_snooze(session) -> None:
    application = make_application(session)
    service.mark_applied(session, application, NOW, DAYS)
    service.snooze_follow_up(session, application, 3, NOW + timedelta(days=7))
    assert service.as_utc(application.next_follow_up_at) == NOW + timedelta(days=10)
    assert "Relance reportée de 3 jour(s)" in messages(session, application)


def test_no_follow_up_before_sending(session) -> None:
    application = make_application(session)
    with pytest.raises(OfferStateError):
        service.record_follow_up(session, application, NOW, DAYS)
    with pytest.raises(OfferStateError):
        service.snooze_follow_up(session, application, 3, NOW)


def test_change_status(session) -> None:
    application = make_application(session)
    service.mark_applied(session, application, NOW, DAYS)

    service.change_status(session, application, ApplicationStatus.INTERVIEW, NOW, DAYS)
    assert application.status is ApplicationStatus.INTERVIEW
    assert application.next_follow_up_at is not None

    service.change_status(session, application, ApplicationStatus.REJECTED, NOW, DAYS)
    assert application.next_follow_up_at is None  # final status: no follow-up
    assert service.due_follow_ups(session, NOW + timedelta(days=60)) == []

    service.change_status(session, application, ApplicationStatus.REJECTED, NOW, DAYS)  # no-op
    assert messages(session, application).count("Candidature refusée") == 1

    # going back is always possible
    service.change_status(session, application, ApplicationStatus.APPLIED, NOW, DAYS)
    assert application.status is ApplicationStatus.APPLIED


def test_change_status_to_applied_sets_date(session) -> None:
    application = make_application(session)
    service.change_status(session, application, ApplicationStatus.APPLIED, NOW, DAYS)
    assert service.as_utc(application.applied_at) == NOW


def test_preparing_is_not_manual(session) -> None:
    application = make_application(session)
    with pytest.raises(OfferStateError, match="non modifiable"):
        service.change_status(session, application, ApplicationStatus.PREPARING, NOW, DAYS)


def test_interview(session) -> None:
    application = make_application(session)
    service.mark_applied(session, application, NOW, DAYS)
    when = NOW + timedelta(days=3)
    service.set_interview(session, application, when, NOW)
    assert application.status is ApplicationStatus.INTERVIEW
    assert application.next_follow_up_at is None
    assert [a.id for a, _ in service.upcoming_interviews(session, NOW)] == [application.id]
    assert service.upcoming_interviews(session, when + timedelta(hours=1)) == []
    service.set_interview(session, application, None, NOW)
    assert application.interview_at is None
    assert "Date d'entretien supprimée" in messages(session, application)


def test_notes(session) -> None:
    application = make_application(session)
    service.update_notes(session, application, "  Contact: Jane  ", NOW)
    assert application.notes == "Contact: Jane"
    service.add_note(session, application, "Appel du recruteur", NOW)
    assert "Appel du recruteur" in messages(session, application)
    with pytest.raises(OfferStateError, match="vide"):
        service.add_note(session, application, "   ", NOW)


def test_mark_ghosted(session) -> None:
    quiet = make_application(session, label="quiet")
    followed = make_application(session, label="followed")
    interview = make_application(session, label="interview")
    for application in (quiet, followed, interview):
        service.mark_applied(session, application, NOW, DAYS)
    service.record_follow_up(session, followed, NOW + timedelta(days=20), DAYS)
    service.change_status(session, interview, ApplicationStatus.INTERVIEW, NOW, DAYS)

    assert service.mark_ghosted(session, NOW + timedelta(days=29), 30) == 0
    assert service.mark_ghosted(session, NOW + timedelta(days=30), 30) == 1
    assert quiet.status is ApplicationStatus.GHOSTED and quiet.next_follow_up_at is None
    assert followed.status is ApplicationStatus.APPLIED  # last contact was the follow-up
    assert interview.status is ApplicationStatus.INTERVIEW  # only "applied" ones
    assert any("sans réponse" in m for m in messages(session, quiet))


def test_list_applications(session) -> None:
    ready = make_application(session, label="ready")
    sent = make_application(session, label="sent")
    service.mark_applied(session, sent, NOW, DAYS)
    rejected = make_application(session, ApplicationStatus.REJECTED, label="rejected")

    assert {a.id for a, _ in service.list_applications(session)} == {ready.id, sent.id, rejected.id}
    assert {a.id for a, _ in service.list_applications(session, active=True)} == {ready.id, sent.id}
    assert [a.id for a, _ in service.list_applications(session, active=False)] == [rejected.id]
    by_status = service.list_applications(session, status=ApplicationStatus.APPLIED)
    assert [(a.id, o.title) for a, o in by_status] == [(sent.id, "sent")]
