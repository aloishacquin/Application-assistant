import json
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from jobapply.config import AppConfig, load_config
from jobapply.llm.client import CallInfo, StructuredResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"
OFFERS_DIR = FIXTURES_DIR / "offers"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFER_NAMES = sorted(p.stem for p in OFFERS_DIR.glob("*.txt"))


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def project_root() -> Path:
    return PROJECT_ROOT


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's real environment leak into tests."""
    for var in (
        "JOBAPPLY_ROOT",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_MODEL",
        "APP_PASSWORD_HASH",
        "SESSION_SECRET",
        "COOKIE_SECURE",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    """A throwaway project root with copies of config/, prompts/ and templates/."""
    shutil.copytree(PROJECT_ROOT / "config", tmp_path / "config")
    shutil.copytree(PROJECT_ROOT / "prompts", tmp_path / "prompts")
    shutil.copytree(PROJECT_ROOT / "templates", tmp_path / "templates")
    # No real source in tests: nothing may reach the network or start a background collection.
    (tmp_path / "config" / "sources.yaml").write_text("sources: []\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def tmp_config(tmp_root: Path) -> AppConfig:
    return load_config(tmp_root, env_file=tmp_root / "missing.env")


def offer_text(name: str) -> str:
    return (OFFERS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def expected_offer(name: str) -> dict:
    return json.loads((OFFERS_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))


class FakeLLM:
    """Stands in for AnthropicLLM: answers with a canned JSON chosen from the prompt."""

    def __init__(self, responder: Callable[[str], dict]) -> None:
        self.responder = responder
        self.prompts: list[str] = []

    def structured[T: BaseModel](
        self, prompt: str, output_model: type[T], *, prompt_name: str, prompt_version: int
    ) -> StructuredResult[T]:
        self.prompts.append(prompt)
        call = CallInfo(
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            attempt=1,
            model_requested="fake-model",
            model_served="fake-model",
            stop_reason="end_turn",
            input_tokens=1000,
            output_tokens=500,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            cost_usd=0.01,
            duration_s=0.0,
            valid=True,
        )
        return StructuredResult(
            value=output_model.model_validate(self.responder(prompt)), calls=[call]
        )


JUDGMENT = {
    "score": 80,
    "strengths": ["Airflow en stage", "PySpark", "Python avancé"],
    "gaps": ["Pas de Snowflake", "Pas de dbt", "Pas d'expérience bancaire"],
    "summary": "Bon profil junior pour ce poste.",
}


def offer_responder(prompt: str) -> dict:
    """Return the expected extraction of whichever fixture offer appears in the prompt."""
    for name in OFFER_NAMES:
        if offer_text(name).strip() in prompt:
            return expected_offer(name)
    raise AssertionError("No fixture offer found in prompt")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM(offer_responder)


TAILORED_CV = json.loads((FIXTURES_DIR / "tailored_cv.json").read_text(encoding="utf-8"))
COVER_LETTER = json.loads((FIXTURES_DIR / "cover_letter.json").read_text(encoding="utf-8"))


def full_responder(prompt: str) -> dict:
    """A valid answer for each prompt type (generation answers fit the finance offer)."""
    if "tailor a candidate's CV" in prompt:
        return TAILORED_CV
    if "You write a cover letter" in prompt:
        return COVER_LETTER
    if "<profile>" in prompt:
        return JUDGMENT
    return offer_responder(prompt)


@pytest.fixture
def profile_root(tmp_root: Path) -> Path:
    """tmp_root with the fixture profile installed as data/profile.yaml."""
    (tmp_root / "data").mkdir(exist_ok=True)
    shutil.copy(FIXTURES_DIR / "profile.yaml", tmp_root / "data" / "profile.yaml")
    return tmp_root


@pytest.fixture
def profile_config(profile_root: Path) -> AppConfig:
    return load_config(profile_root, env_file=profile_root / "missing.env")
