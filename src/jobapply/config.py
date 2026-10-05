"""Configuration loading: .env (secrets) + config/*.yaml -> validated Pydantic objects."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_ENV_VAR = "JOBAPPLY_ROOT"


class ConfigError(Exception):
    """Raised when a configuration file is missing or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --- Secrets (.env) -------------------------------------------------------------------------


class Secrets(BaseSettings):
    """Values read from the environment or the .env file."""

    model_config = SettingsConfigDict(env_file_encoding="utf-8", extra="ignore", frozen=True)

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    app_password_hash: SecretStr | None = None
    session_secret: SecretStr | None = None
    cookie_secure: bool = True  # set to false only for local development over plain HTTP


# --- Visa (config/visa.yaml) ----------------------------------------------------------------


class SalaryThreshold(_Strict):
    base: int = Field(gt=0)
    at_45_plus: int | None = Field(default=None, gt=0)


class SectorThresholds(_Strict):
    other: SalaryThreshold
    financial_services: SalaryThreshold


class UpcomingThresholds(SectorThresholds):
    effective_from: date


class AgeCurve(_Strict):
    base_until_age: int = Field(default=23, gt=0)
    max_from_age: int = Field(default=45, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.base_until_age >= self.max_from_age:
            raise ValueError("base_until_age must be < max_from_age")
        return self


class EPConfig(_Strict):
    min_salary_sgd: SectorThresholds
    upcoming: UpcomingThresholds | None = None
    age_curve: AgeCurve = AgeCurve()
    compass_pass_mark: int = Field(gt=0)
    compass_exempt_salary_sgd: int = Field(gt=0)


class VisaConfig(_Strict):
    effective_from: date
    ep: EPConfig

    @model_validator(mode="after")
    def _upcoming_after_current(self) -> Self:
        if self.ep.upcoming and self.ep.upcoming.effective_from <= self.effective_from:
            raise ValueError("ep.upcoming.effective_from must be after effective_from")
        return self


# --- Settings (config/settings.yaml) --------------------------------------------------------


class CVSettings(_Strict):
    max_pages: int = Field(default=1, ge=1, le=2)
    max_bullets: int = Field(default=14, ge=1)
    max_bullets_per_entry: int = Field(default=5, ge=1)


class CoverLetterSettings(_Strict):
    min_words: int = Field(default=250, gt=0)
    max_words: int = Field(default=350, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min_words > self.max_words:
            raise ValueError("min_words must be <= max_words")
        return self


class ScoreWeights(_Strict):
    keywords: float = Field(default=0.4, ge=0, le=1)
    llm: float = Field(default=0.6, ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> Self:
        if abs(self.keywords + self.llm - 1.0) > 1e-6:
            raise ValueError("matching weights must sum to 1")
        return self


class MatchingSettings(_Strict):
    min_score: int = Field(default=60, ge=0, le=100)
    weights: ScoreWeights = ScoreWeights()
    required_share: float = Field(default=0.7, ge=0, le=1)
    experience_penalty_per_year: float = Field(default=10, ge=0, le=100)


class ModelPricing(_Strict):
    """USD per million tokens."""

    input: float = Field(ge=0)
    output: float = Field(ge=0)
    cache_write: float = Field(ge=0)
    cache_read: float = Field(ge=0)


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class LLMSettings(_Strict):
    max_retries: int = Field(default=2, ge=0, le=2)
    max_tokens: int = Field(default=16000, gt=0)
    effort: Effort | None = "medium"
    refusal_fallback: bool = True
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)


class TrackingSettings(_Strict):
    follow_up_days: int = Field(default=7, gt=0)
    ghosted_after_days: int = Field(default=30, gt=0)


class AppSettings(_Strict):
    language: str = "en"
    timezone: str = "Europe/Paris"
    cv: CVSettings = CVSettings()
    cover_letter: CoverLetterSettings = CoverLetterSettings()
    matching: MatchingSettings = MatchingSettings()
    llm: LLMSettings = LLMSettings()
    tracking: TrackingSettings = TrackingSettings()


# --- Sources (config/sources.yaml) -------------------------------------------------------

SourceType = Literal["greenhouse", "lever", "ashby", "rss"]
REQUIRED_SOURCE_FIELD = {"greenhouse": "board", "ashby": "board", "lever": "company", "rss": "url"}


class SourceSpec(_Strict):
    type: SourceType
    name: str = Field(min_length=1)
    board: str | None = None  # greenhouse board token, ashby job board name
    company: str | None = None  # lever company
    url: str | None = None  # rss / atom feed
    enabled: bool = True

    @model_validator(mode="after")
    def _required_field(self) -> Self:
        field = REQUIRED_SOURCE_FIELD[self.type]
        if not getattr(self, field):
            raise ValueError(f"a '{self.type}' source needs '{field}'")
        return self


class SourceFilters(_Strict):
    locations: list[str] = Field(default_factory=lambda: ["Singapore"])
    title_keywords: list[str] = Field(default_factory=list)
    title_excludes: list[str] = Field(default_factory=list)


class SourceSchedule(_Strict):
    enabled: bool = True
    interval_hours: float = Field(default=6, ge=1)


class SourceBudget(_Strict):
    daily_usd: float = Field(default=1.0, ge=0)
    max_analyses_per_run: int = Field(default=20, ge=0)


class SourcesConfig(_Strict):
    schedule: SourceSchedule = SourceSchedule()
    filters: SourceFilters = SourceFilters()
    budget: SourceBudget = SourceBudget()
    sources: list[SourceSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> Self:
        names = [s.name for s in self.sources]
        if len(names) != len(set(names)):
            raise ValueError("source names must be unique")
        return self

    @property
    def active(self) -> list[SourceSpec]:
        return [s for s in self.sources if s.enabled]


# --- Paths ------------------------------------------------------------------------------------


class Paths(_Strict):
    root: Path

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def profile(self) -> Path:
        return self.data_dir / "profile.yaml"

    @property
    def db(self) -> Path:
        return self.data_dir / "app.db"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "output"

    @property
    def templates_dir(self) -> Path:
        return self.root / "templates"

    @property
    def prompts_dir(self) -> Path:
        return self.root / "prompts"

    @property
    def env_file(self) -> Path:
        return self.root / ".env"

    @property
    def llm_log(self) -> Path:
        return self.data_dir / "llm_calls.jsonl"


class AppConfig(_Strict):
    paths: Paths
    secrets: Secrets
    settings: AppSettings
    visa: VisaConfig
    sources: SourcesConfig = SourcesConfig()


# --- Loading --------------------------------------------------------------------------------


def default_root() -> Path:
    """Project root: $JOBAPPLY_ROOT if set, else the repository containing this package."""
    if env_root := os.environ.get(ROOT_ENV_VAR):
        return Path(env_root).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML file whose root must be a mapping. Raises ConfigError with a clear message."""
    if not path.is_file():
        raise ConfigError(f"Fichier introuvable : {path}")
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}") from exc
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ConfigError(f"{path} doit contenir un dictionnaire YAML à la racine")
    return content


def format_validation_error(path: Path, exc: ValidationError) -> str:
    """Render a Pydantic error as a readable French message, one line per problem."""
    lines = [f"Contenu invalide dans {path} :"]
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"]) or "<racine>"
        lines.append(f"  - {location} : {err['msg']}")
    return "\n".join(lines)


def _validate[M: BaseModel](model: type[M], path: Path) -> M:
    try:
        return model.model_validate(read_yaml(path))
    except ValidationError as exc:
        raise ConfigError(format_validation_error(path, exc)) from exc


def load_config(root: Path | None = None, env_file: Path | None = None) -> AppConfig:
    """Load and validate the whole configuration.

    `env_file` defaults to `<root>/.env`; a missing .env is not an error (secrets are only
    required by the features that use them).
    """
    paths = Paths(root=(root or default_root()).resolve())
    secrets = Secrets(_env_file=env_file or paths.env_file)  # type: ignore[call-arg]
    return AppConfig(
        paths=paths,
        secrets=secrets,
        settings=_validate(AppSettings, paths.config_dir / "settings.yaml"),
        visa=_validate(VisaConfig, paths.config_dir / "visa.yaml"),
        sources=(
            _validate(SourcesConfig, sources_file)
            if (sources_file := paths.config_dir / "sources.yaml").is_file()
            else SourcesConfig()
        ),
    )
