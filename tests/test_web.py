import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from jobapply.config import AppConfig, ConfigError, Secrets, load_config
from jobapply.ingest.fetch import FetchError
from jobapply.models.db import OfferStatus
from jobapply.tracking import service
from jobapply.web.app import create_app
from jobapply.web.auth import hash_password
from tests.conftest import OFFER_NAMES, FakeLLM, full_responder, offer_text

PASSWORD = "a long test password"
PASSWORD_HASH = hash_password(PASSWORD)
FINANCE, TECH, ANALYST = OFFER_NAMES


def web_config(root: Path) -> AppConfig:
    cfg = load_config(root, env_file=root / "missing.env")
    secrets = Secrets(
        app_password_hash=PASSWORD_HASH, session_secret="test-secret", cookie_secure=False
    )
    return cfg.model_copy(update={"secrets": secrets})


class Fetcher:
    def __init__(self) -> None:
        self.pages: dict[str, str] = {}

    def __call__(self, url: str) -> str:
        if url not in self.pages:
            raise FetchError(f"Erreur HTTP 403 pour {url} : colle le texte de l'offre à la place.")
        return self.pages[url]


@pytest.fixture
def fetcher() -> Fetcher:
    return Fetcher()


@pytest.fixture
def app_and_client(profile_root: Path, fetcher: Fetcher):
    app = create_app(
        web_config(profile_root), llm_factory=lambda: FakeLLM(full_responder), fetch=fetcher
    )
    with TestClient(app) as client:
        yield app, client


@pytest.fixture
def anon(app_and_client) -> TestClient:
    return app_and_client[1]


def csrf_of(client: TestClient) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/offers/new").text)
    assert match
    return match.group(1)


@pytest.fixture
def client(anon: TestClient) -> TestClient:
    response = anon.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    anon.headers["X-Test-Csrf"] = csrf_of(anon)
    return anon


def post(client: TestClient, url: str, data: dict | None = None, **kwargs):
    data = {**(data or {}), "csrf_token": client.headers["X-Test-Csrf"]}
    return client.post(url, data=data, follow_redirects=False, **kwargs)


def add_offer(client: TestClient, **data) -> int:
    response = post(client, "/offers", data)
    assert response.status_code == 303, response.text
    return int(response.headers["location"].rsplit("/", 1)[1])


# --- Configuration and auth ---------------------------------------------------------------


def test_requires_secrets(profile_root: Path) -> None:
    with pytest.raises(ConfigError, match="APP_PASSWORD_HASH"):
        create_app(load_config(profile_root, env_file=profile_root / "missing.env"))


def test_pages_require_login(anon: TestClient) -> None:
    response = anon.get("/offers/3", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/offers/3"


def test_htmx_requests_get_redirect_header(anon: TestClient) -> None:
    response = anon.get("/offers/1/status", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_public_routes(anon: TestClient) -> None:
    assert anon.get("/healthz").json() == {"status": "ok"}
    assert anon.get("/static/style.css").status_code == 200
    assert "Mot de passe" in anon.get("/login").text


def test_wrong_password(anon: TestClient) -> None:
    response = anon.post("/login", data={"password": "nope"})
    assert response.status_code == 401
    assert "Mot de passe incorrect" in response.text


def test_login_is_throttled(anon: TestClient) -> None:
    for _ in range(5):
        anon.post("/login", data={"password": "nope"})
    response = anon.post("/login", data={"password": PASSWORD})
    assert response.status_code == 429


def test_login_redirects_to_next(anon: TestClient) -> None:
    response = anon.post(
        "/login", data={"password": PASSWORD, "next": "/profile"}, follow_redirects=False
    )
    assert response.headers["location"] == "/profile"
    open_redirect = anon.post(
        "/login",
        data={"password": PASSWORD, "next": "https://evil.example"},
        follow_redirects=False,
    )
    assert open_redirect.headers["location"] == "/"


def test_logged_in_user_skips_login_page(client: TestClient) -> None:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 303


def test_session_cookie_flags(anon: TestClient) -> None:
    response = anon.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_csrf_is_required(client: TestClient) -> None:
    response = client.post("/offers", data={"text": "x"}, follow_redirects=False)
    assert response.status_code == 403
    bad = client.post("/offers", data={"text": "x", "csrf_token": "forged"})
    assert bad.status_code == 403


def test_csrf_header_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/offers",
        data={"text": offer_text(TECH)},
        headers={"X-CSRF-Token": client.headers["X-Test-Csrf"]},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_logout(client: TestClient) -> None:
    assert post(client, "/logout").status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_security_headers(client: TestClient) -> None:
    headers = client.get("/").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"


# --- Offers -------------------------------------------------------------------------------


def test_add_text_offer_and_see_analysis(client: TestClient) -> None:
    offer_id = add_offer(client, text=offer_text(FINANCE))
    page = client.get(f"/offers/{offer_id}").text
    assert "Data Engineer" in page
    assert "Lion City Bank" in page
    assert "À poursuivre" in page
    assert "Visa OK" in page
    assert "6 455 SGD/mois" in page
    assert "Airflow en stage" in page  # LLM strengths
    assert "Postuler" in page


def test_add_url_offer(client: TestClient, fetcher: Fetcher) -> None:
    fetcher.pages["https://careers.example.com/merlion"] = offer_text(TECH)
    offer_id = add_offer(client, url="https://careers.example.com/merlion")
    page = client.get(f"/offers/{offer_id}").text
    assert "Merlion Robotics" in page
    assert "Salaire inconnu" in page


def test_blocked_url_then_pasted_text(client: TestClient) -> None:
    offer_id = add_offer(client, url="https://www.linkedin.com/jobs/view/42")
    page = client.get(f"/offers/{offer_id}").text
    assert "L&#39;analyse a échoué" in page or "L'analyse a échoué" in page
    assert "Erreur HTTP 403" in page

    response = post(client, f"/offers/{offer_id}/text", {"text": offer_text(FINANCE)})
    assert response.status_code == 303
    assert "Lion City Bank" in client.get(f"/offers/{offer_id}").text


def test_pasted_text_duplicate(client: TestClient) -> None:
    original = add_offer(client, text=offer_text(FINANCE))
    blocked = add_offer(client, url="https://www.linkedin.com/jobs/view/7")
    response = post(client, f"/offers/{blocked}/text", {"text": offer_text(FINANCE)})
    assert response.headers["location"] == f"/offers/{original}"


def test_empty_pasted_text(client: TestClient) -> None:
    blocked = add_offer(client, url="https://www.linkedin.com/jobs/view/8")
    post(client, f"/offers/{blocked}/text", {"text": "   "})
    assert "vide" in client.get(f"/offers/{blocked}").text


def test_add_offer_validation(client: TestClient) -> None:
    response = post(client, "/offers", {"url": "", "text": ""})
    assert response.status_code == 400
    assert "soit un lien" in response.text


def test_duplicate_offer(client: TestClient) -> None:
    first = add_offer(client, text=offer_text(FINANCE))
    second = add_offer(client, text=offer_text(FINANCE))
    assert first == second
    assert "déjà enregistrée" in client.get(f"/offers/{second}").text


def test_accept_offer(client: TestClient, app_and_client) -> None:
    app, _ = app_and_client
    offer_id = add_offer(client, text=offer_text(FINANCE))
    post(client, f"/offers/{offer_id}/accept")
    page = client.get(f"/offers/{offer_id}").text
    assert "Candidature créée" in page
    assert "candidature #1" in page
    with Session(app.state.engine) as session:
        assert service.get_offer(session, offer_id).status is OfferStatus.ACCEPTED
    # accepting twice is refused with a message
    post(client, f"/offers/{offer_id}/accept")
    assert "Seule une offre analysée" in client.get(f"/offers/{offer_id}").text


def test_dismiss_and_restore(client: TestClient) -> None:
    offer_id = add_offer(client, text=offer_text(ANALYST))
    assert "Déconseillé" in client.get(f"/offers/{offer_id}").text
    post(client, f"/offers/{offer_id}/dismiss")
    assert "Rétablir" in client.get(f"/offers/{offer_id}").text
    post(client, f"/offers/{offer_id}/restore")
    assert "Postuler" in client.get(f"/offers/{offer_id}").text


def test_reanalyze(client: TestClient) -> None:
    offer_id = add_offer(client, text=offer_text(FINANCE))
    post(client, f"/offers/{offer_id}/reanalyze", {"reextract": "true"})
    page = client.get(f"/offers/{offer_id}").text
    assert "Nouvelle analyse lancée" in page
    assert page.count("Analyse : score") == 2


def test_reanalyze_accepted_is_refused(client: TestClient) -> None:
    offer_id = add_offer(client, text=offer_text(FINANCE))
    post(client, f"/offers/{offer_id}/accept")
    post(client, f"/offers/{offer_id}/reanalyze")
    assert "déjà acceptée" in client.get(f"/offers/{offer_id}").text


def test_reanalyze_while_running(client: TestClient, app_and_client) -> None:
    app, _ = app_and_client
    offer_id = add_offer(client, text=offer_text(FINANCE))
    with Session(app.state.engine) as session:
        service.set_offer_status(
            session, service.get_offer(session, offer_id), OfferStatus.ANALYZING
        )
    post(client, f"/offers/{offer_id}/reanalyze")
    assert "déjà en cours" in client.get(f"/offers/{offer_id}").text


def test_status_polling(client: TestClient, app_and_client) -> None:
    app, _ = app_and_client
    offer_id = add_offer(client, text=offer_text(FINANCE))
    done = client.get(f"/offers/{offer_id}/status")
    assert done.headers["HX-Refresh"] == "true"

    with Session(app.state.engine) as session:
        service.set_offer_status(
            session, service.get_offer(session, offer_id), OfferStatus.ANALYZING
        )
    pending = client.get(f"/offers/{offer_id}/status")
    assert "Analyse en cours" in pending.text
    assert 'hx-trigger="every 2s"' in pending.text


def test_offer_list_and_filters(client: TestClient) -> None:
    finance = add_offer(client, text=offer_text(FINANCE))
    analyst = add_offer(client, text=offer_text(ANALYST))
    page = client.get("/offers").text
    assert page.index(f"/offers/{finance}") < page.index(f"/offers/{analyst}")  # sorted by score
    incompatible = client.get("/offers?visa=incompatible").text
    assert f'/offers/{analyst}"' in incompatible
    assert f'/offers/{finance}"' not in incompatible
    # FakeLLM gives 80 to every offer: finance 0.4 * 75.5 + 48 = 78, analyst 0.4 * 35 + 48 = 62
    assert f'/offers/{analyst}"' not in client.get("/offers?min_score=70").text
    assert "Aucune offre" in client.get("/offers?status=dismissed").text


def test_unknown_offer_404(client: TestClient) -> None:
    assert client.get("/offers/999").status_code == 404


# --- Dashboard and profile ----------------------------------------------------------------


def test_dashboard(client: TestClient) -> None:
    assert "Aucune offre à trier" in client.get("/").text
    offer_id = add_offer(client, text=offer_text(FINANCE))
    page = client.get("/").text
    assert f"/offers/{offer_id}" in page
    assert "coût LLM aujourd&#39;hui" in page or "coût LLM aujourd'hui" in page


def test_profile_page(client: TestClient, profile_root: Path) -> None:
    assert "Camille Martin" in client.get("/profile").text
    (profile_root / "data" / "profile.yaml").write_text("identity: {}\n", encoding="utf-8")
    assert "Profil invalide" in client.get("/profile").text


def test_interrupted_analysis_is_reset_on_startup(profile_root: Path, fetcher: Fetcher) -> None:
    cfg = web_config(profile_root)
    app = create_app(cfg, llm_factory=lambda: FakeLLM(full_responder), fetch=fetcher)
    with TestClient(app), Session(app.state.engine) as session:
        offer = service.create_offer(session, raw_text="stuck")
        service.set_offer_status(session, offer, OfferStatus.ANALYZING)
        offer_id = offer.id
    restarted = create_app(cfg, llm_factory=lambda: FakeLLM(full_responder), fetch=fetcher)
    with TestClient(restarted), Session(restarted.state.engine) as session:
        assert service.get_offer(session, offer_id).status is OfferStatus.ERROR


# --- Applications and documents ---------------------------------------------------------


def accept(client: TestClient, offer_id: int) -> int:
    response = post(client, f"/offers/{offer_id}/accept")
    assert response.status_code == 303
    return int(response.headers["location"].rsplit("/", 1)[1])


def test_accept_generates_documents(client: TestClient) -> None:
    offer_id = add_offer(client, text=offer_text(FINANCE))
    application_id = accept(client, offer_id)
    page = client.get(f"/applications/{application_id}").text
    assert "Candidature créée : génération du CV et de la lettre en cours." in page
    assert "Prête" in page
    assert page.count("Télécharger le PDF") == 2
    assert "Contrôle ATS : texte lisible" in page
    assert "Texte brut (235 mots)" in page
    assert f"/applications/{application_id}" in client.get(f"/offers/{offer_id}").text


def test_pdf_preview_and_download(client: TestClient) -> None:
    accept(client, add_offer(client, text=offer_text(FINANCE)))
    inline = client.get("/documents/1/pdf")
    assert inline.status_code == 200
    assert inline.headers["content-type"] == "application/pdf"
    assert inline.headers["content-disposition"].startswith("inline")
    assert inline.content.startswith(b"%PDF")
    download = client.get("/documents/2/pdf?download=1")
    disposition = download.headers["content-disposition"]
    assert disposition.startswith("attachment")
    assert "Cover%20letter%20-%20Camille%20Martin%20-%20Lion%20City%20Bank.pdf" in disposition


def test_pdf_outside_output_dir_is_refused(client: TestClient, app_and_client) -> None:
    app, _ = app_and_client
    accept(client, add_offer(client, text=offer_text(FINANCE)))
    from jobapply.models.db import Document

    with Session(app.state.engine) as session:
        document = session.get(Document, 1)
        document.path = "/etc/passwd"
        session.add(document)
        session.commit()
    assert client.get("/documents/1/pdf").status_code == 404


def test_edit_document_page(client: TestClient) -> None:
    application_id = accept(client, add_offer(client, text=offer_text(FINANCE)))
    form = client.get("/documents/1/edit").text
    assert 'name="experiences.0.bullets.1.text"' in form
    text = (
        "Rewrote a nightly batch job with PySpark, cutting its runtime from 6 hours to 45 minutes"
    )
    response = post(client, "/documents/1/edit", {"experiences.0.bullets.1.text": text})
    assert response.headers["location"] == f"/applications/{application_id}"
    page = client.get(f"/applications/{application_id}").text
    assert "Version 2 enregistrée." in page
    assert "modifiée à la main" in page
    assert "Toutes les versions" in page


def test_edit_with_invented_fact_shows_warning(client: TestClient) -> None:
    application_id = accept(client, add_offer(client, text=offer_text(FINANCE)))
    post(client, "/documents/2/edit", {"paragraphs.1.text": "At Google I processed 99 TB per day."})
    page = client.get(f"/applications/{application_id}").text
    assert "avertissement(s) du validateur" in page
    assert "Google" in page


def test_edit_without_change(client: TestClient) -> None:
    application_id = accept(client, add_offer(client, text=offer_text(FINANCE)))
    post(client, "/documents/1/edit", {})
    assert "Aucune modification" in client.get(f"/applications/{application_id}").text


def test_regenerate_letter(client: TestClient) -> None:
    application_id = accept(client, add_offer(client, text=offer_text(FINANCE)))
    post(client, f"/applications/{application_id}/generate", {"kind": "letter"})
    page = client.get(f"/applications/{application_id}").text
    assert "Génération lancée" in page
    assert "Lettre v2" in page
    assert (
        post(client, f"/applications/{application_id}/generate", {"kind": "x"}).status_code == 400
    )


def test_generation_in_progress(client: TestClient, app_and_client) -> None:
    app, _ = app_and_client
    application_id = accept(client, add_offer(client, text=offer_text(FINANCE)))
    assert client.get(f"/applications/{application_id}/status").headers["HX-Refresh"] == "true"
    from jobapply.pipeline import start_generation

    start_generation(app.state.engine, application_id)
    assert "Génération en cours" in client.get(f"/applications/{application_id}/status").text
    post(client, f"/applications/{application_id}/generate", {"kind": "cv"})
    assert "déjà en cours" in client.get(f"/applications/{application_id}").text


def test_generation_error_is_shown(profile_root: Path, fetcher: Fetcher) -> None:
    def responder(prompt: str) -> dict:
        if "tailor a candidate's CV" in prompt:
            raise RuntimeError("API down")
        return full_responder(prompt)

    app = create_app(
        web_config(profile_root), llm_factory=lambda: FakeLLM(responder), fetch=fetcher
    )
    with TestClient(app) as anon:
        anon.post("/login", data={"password": PASSWORD})
        anon.headers["X-Test-Csrf"] = csrf_of(anon)
        application_id = accept(anon, add_offer(anon, text=offer_text(FINANCE)))
        page = anon.get(f"/applications/{application_id}").text
        assert "La génération a échoué" in page
        assert "API down" in page
        assert "Relancer la génération" in page


def test_unknown_application_and_document(client: TestClient) -> None:
    assert client.get("/applications/9").status_code == 404
    assert client.get("/documents/9/pdf").status_code == 404
    assert client.get("/documents/9/edit").status_code == 404


# --- Tracking ------------------------------------------------------------------------------


TEN_DAYS_AGO = (date.today() - timedelta(days=10)).isoformat()  # follow-up due, not ghosted yet


def ready_application(client: TestClient) -> int:
    return accept(client, add_offer(client, text=offer_text(FINANCE)))


def test_full_application_lifecycle(client: TestClient) -> None:
    application_id = ready_application(client)
    page = client.get(f"/applications/{application_id}").text
    assert "Marquer comme envoyée" in page

    post(client, f"/applications/{application_id}/applied", {"applied_on": TEN_DAYS_AGO})
    page = client.get(f"/applications/{application_id}").text
    assert "Candidature marquée comme envoyée" in page
    sent_on = date.fromisoformat(TEN_DAYS_AGO).strftime("%d/%m/%Y")
    assert f"Envoyée le</dt><dd>{sent_on}" in page
    assert "Relance faite" in page

    # sent 10 days ago: the follow-up (after 7 days) is due and shows on the dashboard
    dashboard = client.get("/").text
    assert "Relances dues (1)" in dashboard
    assert f"/applications/{application_id}" in dashboard

    post(client, f"/applications/{application_id}/follow-up")
    assert "Relances dues (0)" in client.get("/").text

    post(client, f"/applications/{application_id}/interview", {"when": "2099-03-02T10:30"})
    page = client.get(f"/applications/{application_id}").text
    assert "02/03/2099 10:30" in page
    assert "Entretien obtenu" in page or "Entretien prévu" in page
    assert "Entretiens à venir" in client.get("/").text

    post(client, f"/applications/{application_id}/set-status", {"status": "offer"})
    page = client.get(f"/applications/{application_id}").text
    assert "Offre d&#39;emploi reçue" in page or "Offre d'emploi reçue" in page
    assert f"/applications/{application_id}" in client.get("/applications?view=done").text
    assert f"/applications/{application_id}" not in client.get("/applications").text


def test_applied_date_validation(client: TestClient) -> None:
    application_id = ready_application(client)
    post(client, f"/applications/{application_id}/applied", {"applied_on": "2999-01-01"})
    assert "futur" in client.get(f"/applications/{application_id}").text
    post(client, f"/applications/{application_id}/applied", {"applied_on": "pas une date"})
    assert "Date invalide" in client.get(f"/applications/{application_id}").text


def test_applied_defaults_to_today(client: TestClient) -> None:
    application_id = ready_application(client)
    post(client, f"/applications/{application_id}/applied")
    assert "Candidature marquée comme envoyée" in client.get(f"/applications/{application_id}").text


def test_tracking_errors_are_flashed(client: TestClient) -> None:
    application_id = ready_application(client)
    post(client, f"/applications/{application_id}/follow-up")
    assert "Aucune relance prévue" in client.get(f"/applications/{application_id}").text
    assert (
        post(client, f"/applications/{application_id}/set-status", {"status": "x"}).status_code
        == 400
    )
    assert post(client, f"/applications/{application_id}/snooze", {"days": "0"}).status_code == 400
    post(client, f"/applications/{application_id}/interview", {"when": "demain"})
    assert "Date d&#39;entretien invalide" in client.get(f"/applications/{application_id}").text


def test_snooze_and_clear_interview(client: TestClient) -> None:
    application_id = ready_application(client)
    post(client, f"/applications/{application_id}/applied", {"applied_on": TEN_DAYS_AGO})
    post(client, f"/applications/{application_id}/snooze", {"days": "7"})
    assert "Relance reportée" in client.get(f"/applications/{application_id}").text
    assert "Relances dues (0)" in client.get("/").text
    post(client, f"/applications/{application_id}/interview", {"when": "2099-03-02T10:30"})
    post(client, f"/applications/{application_id}/interview", {"when": ""})
    assert "Date d&#39;entretien supprimée" in client.get(f"/applications/{application_id}").text


def test_notes_and_events(client: TestClient) -> None:
    application_id = ready_application(client)
    post(client, f"/applications/{application_id}/notes", {"notes": "Recruteuse : Jane"})
    post(client, f"/applications/{application_id}/note", {"text": "Appel de présélection"})
    page = client.get(f"/applications/{application_id}").text
    assert "Recruteuse : Jane" in page
    assert "Appel de présélection" in page
    post(client, f"/applications/{application_id}/note", {"text": " "})
    assert "La note est vide" in client.get(f"/applications/{application_id}").text


def test_applications_list(client: TestClient) -> None:
    application_id = ready_application(client)
    page = client.get("/applications").text
    assert f"/applications/{application_id}" in page
    assert "Prête" in page
    assert f"/applications/{application_id}" in client.get("/applications?status=ready").text
    assert "Aucune candidature" in client.get("/applications?status=interview").text
    assert "Candidatures" in client.get("/").text  # nav link


def test_ghosted_automatically(client: TestClient) -> None:
    application_id = ready_application(client)
    post(client, f"/applications/{application_id}/applied", {"applied_on": "2025-01-01"})
    page = client.get(f"/applications/{application_id}").text
    assert "Sans réponse" in page
    assert "sans nouvelles" in page


# --- Sources ------------------------------------------------------------------------------


def test_sources_page_without_sources(client: TestClient) -> None:
    page = client.get("/sources").text
    assert "Aucune source" in page
    post(client, "/sources/sync")
    assert "Aucune source active" in client.get("/sources").text


def test_sources_sync(profile_root: Path, fetcher: Fetcher, monkeypatch) -> None:
    from jobapply import worker
    from tests.sources_fixtures import SOURCES_YAML, mock_client

    (profile_root / "config" / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    monkeypatch.setattr(worker, "make_client", mock_client)
    cfg = web_config(profile_root)
    cfg = cfg.model_copy(
        update={
            "sources": cfg.sources.model_copy(
                update={"schedule": cfg.sources.schedule.model_copy(update={"enabled": False})}
            )
        }
    )
    app = create_app(cfg, llm_factory=lambda: FakeLLM(full_responder), fetch=fetcher)
    with TestClient(app) as anon:
        anon.post("/login", data={"password": PASSWORD})
        anon.headers["X-Test-Csrf"] = csrf_of(anon)
        assert "jamais" in anon.get("/sources").text
        post(anon, "/sources/sync")
        page = anon.get("/sources").text
        assert "Collecte lancée" in page
        assert "Merlion" in page and "1 nouvelle(s), 2 filtrée(s)" in page
        # filtered offers are hidden by default, shown on demand
        assert "Senior Software Engineer" not in anon.get("/offers").text
        assert "Senior Software Engineer" in anon.get("/offers?status=filtered").text
        assert "Greenhouse" in anon.get("/offers").text


def test_schedule_task_starts_and_stops(profile_root: Path, fetcher: Fetcher, monkeypatch) -> None:
    from jobapply.web import app as app_module
    from tests.sources_fixtures import SOURCES_YAML

    started = []

    async def fake_loop(cfg, engine, llm_factory):
        started.append(True)

    monkeypatch.setattr(app_module, "schedule_loop", fake_loop)
    (profile_root / "config" / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    app = create_app(
        web_config(profile_root), llm_factory=lambda: FakeLLM(full_responder), fetch=fetcher
    )
    with TestClient(app):
        pass
    assert started == [True]
