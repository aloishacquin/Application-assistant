from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jobapply.models.offer import JobOffer, JobOfferExtraction
from jobapply.models.profile import ProfileError, load_profile
from tests.conftest import OFFER_NAMES, expected_offer


@pytest.fixture
def profile_data(fixtures_dir: Path) -> dict:
    return yaml.safe_load((fixtures_dir / "profile.yaml").read_text(encoding="utf-8"))


def write_profile(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# --- Profile -----------------------------------------------------------------------------


def test_fixture_profile_loads(fixtures_dir: Path) -> None:
    profile = load_profile(fixtures_dir / "profile.yaml")
    assert profile.identity.name == "Camille Martin"
    assert "exp-airbus-1" in profile.bullets
    assert "edu-supaero-1" in profile.bullets
    assert "proj-saas-1" in profile.bullets
    assert "sk-python" in profile.index
    assert "exp-airbus" not in profile.bullets  # parents are indexed, but are not bullets
    assert profile.constraints.available_from_date == date(2026, 9, 1)


def test_duplicate_id_is_rejected(tmp_path: Path, profile_data: dict) -> None:
    profile_data["projects"][0]["bullets"][0]["id"] = "exp-airbus-1"
    with pytest.raises(ProfileError) as exc_info:
        load_profile(write_profile(tmp_path, profile_data))
    message = str(exc_info.value)
    assert "dupliqué 'exp-airbus-1'" in message
    assert "projects[0].bullets[0]" in message
    assert "experiences[0].bullets[0]" in message


def test_invalid_id_format(tmp_path: Path, profile_data: dict) -> None:
    profile_data["skills"][0]["id"] = "Sk Python"
    with pytest.raises(ProfileError, match=r"skills\.0\.id"):
        load_profile(write_profile(tmp_path, profile_data))


def test_missing_required_field(tmp_path: Path, profile_data: dict) -> None:
    del profile_data["experiences"][0]["company"]
    with pytest.raises(ProfileError, match=r"experiences\.0\.company"):
        load_profile(write_profile(tmp_path, profile_data))


def test_unknown_field_is_rejected(tmp_path: Path, profile_data: dict) -> None:
    profile_data["identity"]["nationality"] = "French"
    with pytest.raises(ProfileError, match=r"identity\.nationality"):
        load_profile(write_profile(tmp_path, profile_data))


def test_invalid_available_from(tmp_path: Path, profile_data: dict) -> None:
    profile_data["constraints"]["available_from"] = "septembre 2026"
    with pytest.raises(ProfileError, match="available_from"):
        load_profile(write_profile(tmp_path, profile_data))


def test_missing_profile_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="introuvable"):
        load_profile(tmp_path / "nope.yaml")


# --- JobOffer ----------------------------------------------------------------------------


@pytest.mark.parametrize("name", OFFER_NAMES)
def test_expected_offers_are_valid(name: str) -> None:
    JobOfferExtraction.model_validate(expected_offer(name))


def test_salary_min_above_max_is_rejected() -> None:
    data = expected_offer(OFFER_NAMES[0]) | {"salary_min_sgd": 9000, "salary_max_sgd": 7000}
    with pytest.raises(ValidationError, match="salary_min_sgd must be <= salary_max_sgd"):
        JobOfferExtraction.model_validate(data)


def test_negative_salary_is_rejected() -> None:
    data = expected_offer(OFFER_NAMES[0]) | {"salary_min_sgd": -1}
    with pytest.raises(ValidationError, match="positive"):
        JobOfferExtraction.model_validate(data)


@pytest.mark.parametrize("years", [-1, 31])
def test_min_years_experience_bounds(years: int) -> None:
    data = expected_offer(OFFER_NAMES[0]) | {"min_years_experience": years}
    with pytest.raises(ValidationError, match="between 0 and 30"):
        JobOfferExtraction.model_validate(data)


def test_unknown_sector_is_rejected() -> None:
    with pytest.raises(ValidationError):
        JobOfferExtraction.model_validate(expected_offer(OFFER_NAMES[0]) | {"sector": "banking"})


def test_extraction_schema_excludes_code_filled_fields() -> None:
    properties = JobOfferExtraction.model_json_schema()["properties"]
    assert "raw_text" not in properties
    assert "url" not in properties
    assert {"raw_text", "url"} <= JobOffer.model_json_schema()["properties"].keys()
