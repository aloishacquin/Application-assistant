from datetime import date
from pathlib import Path

import pytest

from jobapply.config import ConfigError, default_root, load_config


def test_load_real_config(project_root: Path, tmp_path: Path) -> None:
    cfg = load_config(project_root, env_file=tmp_path / "missing.env")

    assert cfg.paths.root == project_root
    assert cfg.paths.profile == project_root / "data" / "profile.yaml"
    assert cfg.settings.cv.max_pages in (1, 2)
    assert cfg.settings.llm.max_retries <= 2
    assert cfg.visa.effective_from == date(2026, 1, 1)
    assert cfg.visa.ep.min_salary_sgd.other.base == 5600
    assert cfg.visa.ep.min_salary_sgd.financial_services.at_45_plus == 11800
    assert cfg.visa.ep.upcoming is not None
    assert cfg.visa.ep.upcoming.effective_from == date(2027, 1, 1)
    assert cfg.secrets.anthropic_api_key is None


def test_default_root_is_repository(project_root: Path) -> None:
    assert default_root() == project_root


def test_root_env_var_override(monkeypatch: pytest.MonkeyPatch, tmp_root: Path) -> None:
    monkeypatch.setenv("JOBAPPLY_ROOT", str(tmp_root))
    assert load_config().paths.root == tmp_root.resolve()


def test_env_file_is_read(tmp_root: Path) -> None:
    (tmp_root / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-test\nANTHROPIC_MODEL=test-model\n", encoding="utf-8"
    )
    cfg = load_config(tmp_root)

    assert cfg.secrets.anthropic_model == "test-model"
    assert cfg.secrets.anthropic_api_key is not None
    assert cfg.secrets.anthropic_api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(cfg)


def test_environment_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_root: Path) -> None:
    (tmp_root / ".env").write_text("ANTHROPIC_MODEL=from-file\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_MODEL", "from-env")
    assert load_config(tmp_root).secrets.anthropic_model == "from-env"


def test_missing_config_file(tmp_root: Path) -> None:
    (tmp_root / "config" / "visa.yaml").unlink()
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(tmp_root)


def test_invalid_yaml_syntax(tmp_root: Path) -> None:
    (tmp_root / "config" / "settings.yaml").write_text("cv: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML invalide"):
        load_config(tmp_root)


@pytest.mark.parametrize(
    ("content", "location"),
    [
        ("cv: { max_pages: 3 }\n", "cv.max_pages"),
        ("matching: { weights: { keywords: 0.5, llm: 0.6 } }\n", "matching.weights"),
        ("cover_letter: { min_words: 400, max_words: 300 }\n", "cover_letter"),
        ("unknown_key: 1\n", "unknown_key"),
    ],
)
def test_invalid_settings_are_reported(tmp_root: Path, content: str, location: str) -> None:
    (tmp_root / "config" / "settings.yaml").write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_root)
    assert "settings.yaml" in str(exc_info.value)
    assert location in str(exc_info.value)


def test_empty_settings_use_defaults(tmp_root: Path) -> None:
    (tmp_root / "config" / "settings.yaml").write_text("", encoding="utf-8")
    cfg = load_config(tmp_root)
    assert cfg.settings.cv.max_pages == 1
    assert cfg.settings.matching.weights.keywords == pytest.approx(0.4)


def test_upcoming_must_be_after_current(tmp_root: Path) -> None:
    visa = tmp_root / "config" / "visa.yaml"
    visa.write_text(
        visa.read_text(encoding="utf-8").replace('"2027-01-01"', '"2025-01-01"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="upcoming"):
        load_config(tmp_root)
