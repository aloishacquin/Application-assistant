"""Administration CLI. Thin layer over pipeline.py and the services.

User-facing messages are in French (see SPEC section 14).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from sqlmodel import Session

from jobapply import __version__
from jobapply.config import AppConfig, ConfigError, load_config
from jobapply.llm.client import AnthropicLLM, StructuredLLM
from jobapply.models.db import Analysis, get_engine
from jobapply.models.profile import load_profile
from jobapply.pipeline import AnalysisError, OfferNotFoundError, analyze_offer, submit_offer
from jobapply.tracking import service
from jobapply.web.templating import VISA_LABELS, format_salary

app = typer.Typer(
    name="jobapply",
    help="Plateforme de candidatures CDI Singapour : collecte, analyse, CV, lettre, suivi.",
    no_args_is_help=True,
    add_completion=False,
)
profile_app = typer.Typer(help="Base de connaissances (data/profile.yaml).", no_args_is_help=True)
offer_app = typer.Typer(help="Offres d'emploi (debug, sans l'interface web).", no_args_is_help=True)
app.add_typer(profile_app, name="profile")
app.add_typer(offer_app, name="offer")
sources_app = typer.Typer(help="Collecte automatique (config/sources.yaml).", no_args_is_help=True)
app.add_typer(sources_app, name="sources")


def _fail(message: str) -> NoReturn:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _load_config() -> AppConfig:
    try:
        return load_config()
    except ConfigError as exc:
        _fail(str(exc))


def _build_llm(cfg: AppConfig) -> StructuredLLM:
    return AnthropicLLM.from_config(cfg)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"jobapply {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_version_callback, is_eager=True, help="Affiche la version."
        ),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Affiche les logs détaillés (appels LLM).")
    ] = False,
) -> None:
    """Plateforme de candidatures CDI Singapour."""
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


# --- Platform -----------------------------------------------------------------------------


@app.command("serve")
def serve(
    host: Annotated[str, typer.Option(help="Adresse d'écoute.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port d'écoute.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Rechargement auto (développement).")] = False,
) -> None:
    """Lance la plateforme web."""
    import uvicorn

    from jobapply.web.app import create_app

    try:
        create_app(_load_config())  # fail fast with a clear message on bad configuration
    except ConfigError as exc:
        _fail(str(exc))
    typer.secho(f"Plateforme disponible sur http://{host}:{port}", fg=typer.colors.GREEN)
    uvicorn.run(
        "jobapply.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
    )


@app.command("hash-password")
def hash_password_cmd() -> None:
    """Produit la valeur APP_PASSWORD_HASH à mettre dans .env."""
    from jobapply.web.auth import hash_password

    password = typer.prompt("Mot de passe", hide_input=True, confirmation_prompt=True)
    if len(password) < 12:
        _fail("Choisis un mot de passe d'au moins 12 caractères.")
    typer.echo(f"APP_PASSWORD_HASH={hash_password(password)}")


@app.command("backup")
def backup(
    keep: Annotated[int, typer.Option(help="Nombre de sauvegardes conservées.")] = 14,
) -> None:
    """Sauvegarde data/ (base, profil, documents) dans data/backups/."""
    from datetime import datetime

    from jobapply.backup import create_backup

    archive = create_backup(_load_config().paths, datetime.now(), keep=keep)
    size_kb = archive.stat().st_size / 1024
    typer.secho(f"Sauvegarde créée : {archive} ({size_kb:.0f} Ko)", fg=typer.colors.GREEN)


# --- Profile ------------------------------------------------------------------------------


@profile_app.command("check")
def profile_check(
    path: Annotated[
        Path | None,
        typer.Option("--path", dir_okay=False, help="Autre fichier profil à valider."),
    ] = None,
) -> None:
    """Valide data/profile.yaml."""
    profile_path = path or _load_config().paths.profile
    try:
        profile = load_profile(profile_path)
    except ConfigError as exc:
        _fail(str(exc))
    typer.secho(f"Profil valide : {profile_path}", fg=typer.colors.GREEN)
    typer.echo(f"  {profile.identity.name}")
    typer.echo(f"  Accroches : {len(profile.headline_variants)}")
    typer.echo(f"  Formations : {len(profile.education)}")
    typer.echo(f"  Expériences : {len(profile.experiences)}")
    typer.echo(f"  Projets : {len(profile.projects)}")
    typer.echo(f"  Bullets : {len(profile.bullets)}")
    typer.echo(f"  Compétences : {len(profile.skills)}")
    typer.echo(f"  Motivations : {len(profile.motivations)}")


# --- Offers (debug) -----------------------------------------------------------------------


def _print_analysis(cfg: AppConfig, offer_id: int, analysis: Analysis) -> None:
    with Session(get_engine(cfg.paths.db)) as session:
        offer = service.get_offer(session, offer_id)
        job = offer.to_job_offer() if offer else None
    assert job is not None
    visa, match = analysis.visa, analysis.match
    typer.secho(f"Offre #{offer_id} : {job.title} — {job.company}", bold=True)
    typer.echo(f"  Lieu : {job.location}")
    years = job.min_years_experience
    typer.echo(f"  Expérience min. : {'non indiquée' if years is None else f'{years} an(s)'}")
    typer.echo(f"  Salaire : {format_salary(job.salary_min_sgd, job.salary_max_sgd)}")
    typer.echo(f"  Compétences requises : {', '.join(job.required_skills) or '-'}")
    typer.echo(
        f"  {VISA_LABELS[analysis.visa_verdict]} (seuil {visa['threshold_sgd']} SGD/mois, "
        f"{visa['age_at_start']} ans au {visa['start_date']})"
    )
    for note in visa["notes"]:
        typer.echo(f"    ! {note}")
    llm = f", LLM {match['llm_score']}" if match["llm_score"] is not None else ""
    typer.secho(f"  Score : {analysis.score}/100", bold=True, nl=False)
    typer.echo(f" (mots-clés {match['keyword_score']}{llm})")
    if match["missing_required"]:
        typer.echo(f"  Compétences manquantes : {', '.join(match['missing_required'])}")
    for item in match["strengths"]:
        typer.echo(f"    + {item}")
    for item in match["gaps"]:
        typer.echo(f"    - {item}")
    if match["summary"]:
        typer.echo(f"  Avis : {match['summary']}")
    if analysis.stop_reasons:
        typer.secho(f"  => Déconseillé : {' ; '.join(analysis.stop_reasons)}", fg=typer.colors.RED)
    else:
        typer.secho("  => À poursuivre", fg=typer.colors.GREEN)
    if analysis.cost_usd is not None:
        typer.echo(f"  Coût LLM : {analysis.cost_usd:.4f} $")


def _run_analysis(cfg: AppConfig, offer_id: int, *, use_llm_score: bool) -> Analysis:
    try:
        return analyze_offer(
            cfg=cfg,
            engine=get_engine(cfg.paths.db),
            offer_id=offer_id,
            llm_factory=lambda: _build_llm(cfg),
            use_llm_score=use_llm_score,
        )
    except (AnalysisError, OfferNotFoundError) as exc:
        _fail(str(exc))


@offer_app.command("add")
def offer_add(
    url: Annotated[str | None, typer.Option("--url", help="Lien de l'offre.")] = None,
    file: Annotated[
        Path | None,
        typer.Option("--file", exists=True, dir_okay=False, help="Fichier texte de l'offre."),
    ] = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Score par mots-clés uniquement.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Affiche l'offre structurée.")] = False,
) -> None:
    """Ajoute une offre et l'analyse."""
    if (url is None) == (file is None):
        raise typer.BadParameter("Indique exactement une source : --url ou --file.")
    cfg = _load_config()
    text = file.read_text(encoding="utf-8") if file is not None else None
    try:
        result = submit_offer(get_engine(cfg.paths.db), url=url, text=text)
    except ValueError as exc:
        _fail(str(exc))
    if not result.created:
        typer.secho(f"Offre déjà enregistrée (#{result.offer_id}).", fg=typer.colors.YELLOW)
        return
    analysis = _run_analysis(cfg, result.offer_id, use_llm_score=not no_llm)
    _print_analysis(cfg, result.offer_id, analysis)
    if as_json:
        with Session(get_engine(cfg.paths.db)) as session:
            offer = service.get_offer(session, result.offer_id)
            job = offer.to_job_offer() if offer else None
        if job is not None:
            payload = job.model_dump(exclude={"raw_text"})
            typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))


@offer_app.command("score")
def offer_score(
    offer_id: Annotated[int, typer.Argument(help="Id de l'offre.")],
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Score par mots-clés uniquement (gratuit).")
    ] = False,
) -> None:
    """Réanalyse une offre enregistrée (visa + score) avec le profil actuel."""
    cfg = _load_config()
    analysis = _run_analysis(cfg, offer_id, use_llm_score=not no_llm)
    _print_analysis(cfg, offer_id, analysis)


# --- Sources ------------------------------------------------------------------------------


@sources_app.command("sync")
def sources_sync(
    no_analyze: Annotated[
        bool, typer.Option("--no-analyze", help="Collecte sans analyse LLM (gratuit).")
    ] = False,
) -> None:
    """Collecte maintenant toutes les sources actives, puis analyse dans la limite du budget."""
    from jobapply.worker import CollectionInProgressError, run_collection

    cfg = _load_config()
    if not cfg.sources.active:
        _fail("Aucune source active dans config/sources.yaml.")
    try:
        report = run_collection(
            cfg, get_engine(cfg.paths.db), lambda: _build_llm(cfg), analyze=not no_analyze
        )
    except CollectionInProgressError as exc:
        _fail(str(exc))
    for run in report.runs:
        if run.error:
            typer.secho(f"  {run.source_name} : erreur — {run.error}", fg=typer.colors.RED)
        else:
            typer.echo(
                f"  {run.source_name} : {run.fetched} lues, {run.new} nouvelles, "
                f"{run.filtered} filtrées, {run.duplicates} déjà vues, {run.out_of_area} hors zone"
            )
    if not no_analyze:
        typer.echo(f"Analysées : {report.analyzed} (erreurs : {report.analysis_errors})")
        if report.budget_reached:
            typer.secho("Budget LLM du jour atteint : la suite demain.", fg=typer.colors.YELLOW)


if __name__ == "__main__":
    app()
