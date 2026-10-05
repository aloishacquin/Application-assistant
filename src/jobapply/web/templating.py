"""Jinja2 environment, French labels and display filters."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobapply.models.db import ApplicationStatus, OfferSource, OfferStatus
from jobapply.web.auth import csrf_token, is_authenticated

TEMPLATES_DIR = Path(__file__).parent / "templates"

OFFER_STATUS_LABELS = {
    OfferStatus.NEW: "Nouvelle",
    OfferStatus.ANALYZING: "Analyse en cours",
    OfferStatus.ANALYZED: "À trier",
    OfferStatus.ACCEPTED: "Je postule",
    OfferStatus.DISMISSED: "Ignorée",
    OfferStatus.FILTERED: "Filtrée",
    OfferStatus.ERROR: "Erreur",
}
APPLICATION_STATUS_LABELS = {
    ApplicationStatus.PREPARING: "En préparation",
    ApplicationStatus.READY: "Prête",
    ApplicationStatus.APPLIED: "Envoyée",
    ApplicationStatus.INTERVIEW: "Entretien",
    ApplicationStatus.OFFER: "Offre reçue",
    ApplicationStatus.REJECTED: "Refusée",
    ApplicationStatus.GHOSTED: "Sans réponse",
    ApplicationStatus.WITHDRAWN: "Retirée",
}
VISA_LABELS = {
    "ok": "Visa OK",
    "at_risk": "Visa à risque",
    "incompatible": "Visa incompatible",
    "unknown": "Salaire inconnu",
}
SOURCE_LABELS = {
    OfferSource.MANUAL: "Ajout manuel",
    OfferSource.GREENHOUSE: "Greenhouse",
    OfferSource.LEVER: "Lever",
    OfferSource.ASHBY: "Ashby",
    OfferSource.RSS: "RSS",
    OfferSource.EMAIL: "Alerte email",
}
SENIORITY_LABELS = {
    "internship": "Stage",
    "entry": "Débutant",
    "junior": "Junior",
    "mid": "Confirmé",
    "senior": "Senior",
    "lead": "Lead",
    "unknown": "Non précisée",
}


def format_sgd(amount: int | float | None) -> str:
    return "-" if amount is None else f"{amount:,.0f}".replace(",", " ")


def format_salary(low: int | None, high: int | None) -> str:
    if low is None and high is None:
        return "Non indiqué"
    if low is not None and high is not None:
        if low == high:
            return f"{format_sgd(low)} SGD/mois"
        return f"{format_sgd(low)} – {format_sgd(high)} SGD/mois"
    if low is not None:
        return f"À partir de {format_sgd(low)} SGD/mois"
    return f"Jusqu'à {format_sgd(high)} SGD/mois"


def score_class(score: int | None) -> str:
    if score is None:
        return "neutral"
    return "good" if score >= 70 else "medium" if score >= 50 else "bad"


def visa_class(verdict: str | None) -> str:
    return {"ok": "good", "at_risk": "medium", "incompatible": "bad"}.get(verdict or "", "neutral")


def build_templates(timezone: str) -> Jinja2Templates:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    tz = ZoneInfo(timezone)

    def format_dt(value: datetime | None) -> str:
        if value is None:
            return "-"
        if value.tzinfo is None:  # SQLite returns naive datetimes, stored in UTC
            value = value.replace(tzinfo=UTC)
        return value.astimezone(tz).strftime("%d/%m/%Y %H:%M")

    def local(value: datetime) -> datetime:
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).astimezone(tz)

    def format_date(value: datetime | None) -> str:
        return "-" if value is None else local(value).strftime("%d/%m/%Y")

    def input_datetime(value: datetime | None) -> str:
        """Value for an <input type="datetime-local">."""
        return "" if value is None else local(value).strftime("%Y-%m-%dT%H:%M")

    def relative_days(value: datetime | None) -> str:
        if value is None:
            return ""
        days = (local(value).date() - datetime.now(tz).date()).days
        if days == 0:
            return "aujourd'hui"
        if days < 0:
            return f"en retard de {-days} j"
        return "demain" if days == 1 else f"dans {days} j"

    env = templates.env
    env.filters.update(
        sgd=format_sgd,
        dt=format_dt,
        date=format_date,
        input_datetime=input_datetime,
        relative_days=relative_days,
        score_class=score_class,
        visa_class=visa_class,
    )
    env.globals.update(
        format_salary=format_salary,
        OFFER_STATUS_LABELS=OFFER_STATUS_LABELS,
        APPLICATION_STATUS_LABELS=APPLICATION_STATUS_LABELS,
        VISA_LABELS=VISA_LABELS,
        SOURCE_LABELS=SOURCE_LABELS,
        SENIORITY_LABELS=SENIORITY_LABELS,
        OfferStatus=OfferStatus,
    )
    return templates


def flash(request: Request, message: str, kind: str = "info") -> None:
    # Reassign rather than mutate in place: the session only saves keys that are set.
    flashes = [*request.session.get("flashes", []), {"message": message, "kind": kind}]
    request.session["flashes"] = flashes


def render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    templates: Jinja2Templates = request.app.state.templates
    context.update(
        csrf_token=csrf_token(request),
        authenticated=is_authenticated(request),
        flashes=request.session.pop("flashes", []),
        path=request.url.path,
    )
    return templates.TemplateResponse(request, name, context, status_code=status_code)
