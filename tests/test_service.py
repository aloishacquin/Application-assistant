import pytest
from sqlmodel import Session

from jobapply.config import AppConfig
from jobapply.models.db import Analysis, ApplicationStatus, OfferStatus, get_engine
from jobapply.tracking import service
from jobapply.tracking.service import OfferStateError, normalize_url


@pytest.fixture
def session(tmp_config: AppConfig):
    with Session(get_engine(tmp_config.paths.db)) as s:
        yield s


def make_offer(session, status=OfferStatus.NEW, score=None, verdict="ok", text=None):
    offer = service.create_offer(session, raw_text=text or f"offer {status} {score} {verdict}")
    service.set_offer_status(session, offer, status)
    if score is not None:
        session.add(
            Analysis(offer_id=offer.id, visa={}, match={}, score=score, visa_verdict=verdict)
        )
        session.commit()
    return offer


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Jobs.Example.com/a/b/", "https://jobs.example.com/a/b"),
        ("https://x.example/job?id=4&utm_medium=mail&ref=feed", "https://x.example/job?id=4"),
        ("https://x.example/job#apply", "https://x.example/job"),
        ("https://x.example", "https://x.example/"),
    ],
)
def test_normalize_url(url: str, expected: str) -> None:
    assert normalize_url(url) == expected


def test_list_offers_sorted_and_filtered(session) -> None:
    low = make_offer(session, OfferStatus.ANALYZED, 40, "incompatible")
    high = make_offer(session, OfferStatus.ANALYZED, 85, "ok")
    pending = make_offer(session, OfferStatus.NEW)

    assert [o.id for o, _ in service.list_offers(session)] == [high.id, low.id, pending.id]
    assert [o.id for o, _ in service.list_offers(session, min_score=50)] == [high.id]
    assert [o.id for o, _ in service.list_offers(session, visa_verdict="incompatible")] == [low.id]
    assert [o.id for o, _ in service.list_offers(session, status=OfferStatus.NEW)] == [pending.id]


def test_latest_analysis(session) -> None:
    offer = make_offer(session, OfferStatus.ANALYZED, 50)
    session.add(Analysis(offer_id=offer.id, visa={}, match={}, score=70, visa_verdict="ok"))
    session.commit()
    assert service.latest_analysis(session, offer.id).score == 70


def test_accept_creates_application(session) -> None:
    offer = make_offer(session, OfferStatus.ANALYZED, 80)
    application = service.accept_offer(session, offer)
    assert application.status is ApplicationStatus.PREPARING
    assert offer.status is OfferStatus.ACCEPTED
    assert service.get_application_for_offer(session, offer.id).id == application.id
    assert service.application_status_counts(session)[ApplicationStatus.PREPARING] == 1
    assert service.list_events(session, offer.id)[0].message.startswith("Je postule")


@pytest.mark.parametrize("status", [OfferStatus.NEW, OfferStatus.ERROR, OfferStatus.ACCEPTED])
def test_accept_requires_analyzed(session, status) -> None:
    with pytest.raises(OfferStateError):
        service.accept_offer(session, make_offer(session, status))


def test_dismiss_and_restore(session) -> None:
    offer = make_offer(session, OfferStatus.ANALYZED, 30)
    service.dismiss_offer(session, offer)
    assert offer.status is OfferStatus.DISMISSED
    service.restore_offer(session, offer)
    assert offer.status is OfferStatus.ANALYZED


def test_restore_without_analysis_goes_back_to_new(session) -> None:
    offer = make_offer(session, OfferStatus.ERROR)
    service.dismiss_offer(session, offer)
    service.restore_offer(session, offer)
    assert offer.status is OfferStatus.NEW


def test_invalid_transitions(session) -> None:
    with pytest.raises(OfferStateError):
        service.dismiss_offer(session, make_offer(session, OfferStatus.ANALYZING))
    with pytest.raises(OfferStateError):
        service.restore_offer(session, make_offer(session, OfferStatus.ANALYZED, 50))


def test_reset_interrupted_analyses(session) -> None:
    stuck = make_offer(session, OfferStatus.ANALYZING)
    done = make_offer(session, OfferStatus.ANALYZED, 50)
    assert service.reset_interrupted_analyses(session) == 1
    session.refresh(stuck)
    assert stuck.status is OfferStatus.ERROR
    assert "interrompue" in stuck.status_message
    assert done.status is OfferStatus.ANALYZED


def test_status_counts(session) -> None:
    make_offer(session, OfferStatus.ANALYZED, 50)
    make_offer(session, OfferStatus.ANALYZED, 60)
    make_offer(session, OfferStatus.ERROR)
    counts = service.offer_status_counts(session)
    assert counts[OfferStatus.ANALYZED] == 2
    assert counts[OfferStatus.ERROR] == 1
    assert counts[OfferStatus.DISMISSED] == 0
