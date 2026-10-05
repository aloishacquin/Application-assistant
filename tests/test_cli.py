import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobapply import __version__, cli
from jobapply.cli import app
from tests.conftest import OFFER_NAMES, FakeLLM, expected_offer, full_responder

runner = CliRunner()
FINANCE, TECH, ANALYST = OFFER_NAMES


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, profile_root: Path) -> Path:
    monkeypatch.setenv("JOBAPPLY_ROOT", str(profile_root))
    monkeypatch.setattr(cli, "_build_llm", lambda cfg: FakeLLM(full_responder))
    return profile_root


def offer_file(fixtures_dir: Path, name: str) -> str:
    return str(fixtures_dir / "offers" / f"{name}.txt")


def test_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("serve", "hash-password", "profile", "offer"):
        assert command in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_hash_password() -> None:
    result = runner.invoke(app, ["hash-password"], input="a long password\na long password\n")
    assert result.exit_code == 0
    assert "APP_PASSWORD_HASH=scrypt$" in result.output


def test_hash_password_too_short() -> None:
    result = runner.invoke(app, ["hash-password"], input="short\nshort\n")
    assert result.exit_code == 1
    assert "12 caractères" in result.output


def test_serve_requires_secrets(cli_env: Path) -> None:
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "APP_PASSWORD_HASH" in result.output


# --- profile check -------------------------------------------------------------------------


def test_profile_check_default(cli_env: Path) -> None:
    result = runner.invoke(app, ["profile", "check"])
    assert result.exit_code == 0, result.output
    assert "Profil valide" in result.output
    assert "Expériences : 2" in result.output


def test_profile_check_missing(monkeypatch: pytest.MonkeyPatch, tmp_root: Path) -> None:
    monkeypatch.setenv("JOBAPPLY_ROOT", str(tmp_root))
    result = runner.invoke(app, ["profile", "check"])
    assert result.exit_code == 1
    assert "introuvable" in result.output


def test_profile_check_invalid(tmp_path: Path) -> None:
    bad = tmp_path / "profile.yaml"
    bad.write_text("identity: {name: x}\n", encoding="utf-8")
    result = runner.invoke(app, ["profile", "check", "--path", str(bad)])
    assert result.exit_code == 1
    assert "Contenu invalide" in result.output


# --- offer add / score -------------------------------------------------------------------


@pytest.mark.parametrize("name", OFFER_NAMES)
def test_offer_add_file(cli_env: Path, fixtures_dir: Path, name: str) -> None:
    result = runner.invoke(
        app, ["offer", "add", "--file", offer_file(fixtures_dir, name), "--json"]
    )
    assert result.exit_code == 0, result.output
    expected = expected_offer(name)
    assert f"Offre #1 : {expected['title']}" in result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert {k: payload[k] for k in expected} == expected


def test_offer_add_summary(cli_env: Path, fixtures_dir: Path) -> None:
    result = runner.invoke(app, ["offer", "add", "--file", offer_file(fixtures_dir, FINANCE)])
    assert "7 000 – 9 000 SGD/mois" in result.output
    assert "Visa OK" in result.output
    assert "Expérience min. : 0 an(s)" in result.output
    assert "LLM 80" in result.output
    assert "À poursuivre" in result.output
    assert "Coût LLM : 0.0200 $" in result.output


def test_offer_add_twice(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["offer", "add", "--file", offer_file(fixtures_dir, FINANCE)])
    result = runner.invoke(app, ["offer", "add", "--file", offer_file(fixtures_dir, FINANCE)])
    assert result.exit_code == 0
    assert "déjà enregistrée (#1)" in result.output


def test_offer_add_requires_one_source(cli_env: Path, fixtures_dir: Path) -> None:
    assert runner.invoke(app, ["offer", "add"]).exit_code != 0
    both = ["offer", "add", "--url", "https://x.example", "--file", offer_file(fixtures_dir, TECH)]
    assert runner.invoke(app, both).exit_code != 0


def test_offer_add_invalid_url(cli_env: Path) -> None:
    result = runner.invoke(app, ["offer", "add", "--url", "jobs.example.com/1"])
    assert result.exit_code == 1
    assert "http" in result.output


def test_offer_add_without_model(monkeypatch: pytest.MonkeyPatch, profile_root, fixtures_dir):
    monkeypatch.setenv("JOBAPPLY_ROOT", str(profile_root))
    result = runner.invoke(app, ["offer", "add", "--file", offer_file(fixtures_dir, FINANCE)])
    assert result.exit_code == 1
    assert "ANTHROPIC_MODEL" in result.output


def test_offer_score(cli_env: Path, fixtures_dir: Path) -> None:
    runner.invoke(app, ["offer", "add", "--no-llm", "--file", offer_file(fixtures_dir, ANALYST)])
    result = runner.invoke(app, ["offer", "score", "1", "--no-llm"])
    assert result.exit_code == 0, result.output
    assert "Visa incompatible" in result.output
    assert "Déconseillé" in result.output
    assert "LLM" not in result.output.split("Score")[1].splitlines()[0]


def test_offer_score_unknown(cli_env: Path) -> None:
    result = runner.invoke(app, ["offer", "score", "42", "--no-llm"])
    assert result.exit_code == 1
    assert "Aucune offre" in result.output


def test_sources_sync_without_sources(cli_env: Path) -> None:
    result = runner.invoke(app, ["sources", "sync"])
    assert result.exit_code == 1
    assert "Aucune source active" in result.output


def test_sources_sync(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jobapply import worker
    from tests.sources_fixtures import SOURCES_YAML, mock_client

    (cli_env / "config" / "sources.yaml").write_text(SOURCES_YAML, encoding="utf-8")
    monkeypatch.setattr(worker, "make_client", mock_client)
    result = runner.invoke(app, ["sources", "sync", "--no-analyze"])
    assert result.exit_code == 0, result.output
    assert "Merlion : 4 lues, 1 nouvelles, 2 filtrées, 0 déjà vues, 1 hors zone" in result.output
    assert "Analysées" not in result.output
