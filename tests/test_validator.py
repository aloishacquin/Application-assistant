import copy
from pathlib import Path

import pytest

from jobapply.config import CoverLetterSettings, CVSettings
from jobapply.generate.validator import normalize_number, numbers_in, validate_cv, validate_letter
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile, load_profile
from jobapply.models.tailored import CoverLetter, TailoredCV
from tests.conftest import COVER_LETTER, OFFER_NAMES, TAILORED_CV, expected_offer, offer_text


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile(Path(__file__).parent / "fixtures" / "profile.yaml")


@pytest.fixture(scope="module")
def offer() -> JobOffer:
    return JobOffer(**expected_offer(OFFER_NAMES[0]), raw_text=offer_text(OFFER_NAMES[0]))


def cv_issues(profile, offer, mutate=None, settings=None) -> list[str]:
    data = copy.deepcopy(TAILORED_CV)
    if mutate:
        mutate(data)
    cv = TailoredCV.model_validate(data)
    return [str(i) for i in validate_cv(cv, profile, offer, settings or CVSettings())]


def letter_issues(profile, offer, mutate=None) -> list[str]:
    data = copy.deepcopy(COVER_LETTER)
    if mutate:
        mutate(data)
    letter = CoverLetter.model_validate(data)
    return [str(i) for i in validate_letter(letter, profile, offer, CoverLetterSettings())]


@pytest.mark.parametrize(
    ("raw", "expected"), [("5,000", "5000"), ("2,5", "2.5"), ("94", "94"), ("1,234,567", "1234567")]
)
def test_normalize_number(raw: str, expected: str) -> None:
    assert normalize_number(raw) == expected


def test_numbers_in() -> None:
    assert numbers_in("from 6 hours to 45 minutes, 5,000 requests, 30%") == {
        "6",
        "45",
        "5000",
        "30",
    }


# --- CV -----------------------------------------------------------------------------------


def test_valid_cv(profile, offer) -> None:
    assert cv_issues(profile, offer) == []


def set_bullet(text=None, source_id=None, entry=0, bullet=0, section="experiences"):
    def mutate(data):
        target = data[section][entry]["bullets"][bullet]
        if text is not None:
            target["text"] = text
        if source_id is not None:
            target["source_id"] = source_id

    return mutate


def test_invented_bullet_is_rejected(profile, offer) -> None:
    issues = cv_issues(profile, offer, set_bullet(source_id="exp-airbus-9"))
    assert issues == ["experiences[0].bullets[0]: 'exp-airbus-9' is not a bullet of 'exp-airbus'"]


def test_bullet_from_another_entry_is_rejected(profile, offer) -> None:
    issues = cv_issues(profile, offer, set_bullet(source_id="exp-startup-1"))
    assert any("is not a bullet of 'exp-airbus'" in i for i in issues)


def test_modified_number_is_rejected(profile, offer) -> None:
    text = "Built an Airflow data pipeline ingesting 3 TB of flight-test data per week"
    issues = cv_issues(profile, offer, set_bullet(text=text))
    assert issues == ["experiences[0].bullets[0]: numbers not found in the source: 3"]


def test_invented_percentage_is_rejected(profile, offer) -> None:
    text = "Cut a nightly batch job runtime by 87% by rewriting it with PySpark"
    assert any("87" in i for i in cv_issues(profile, offer, set_bullet(text=text, bullet=1)))


def test_invented_technology_is_rejected(profile, offer) -> None:
    text = "Built an Airflow pipeline on Kubernetes ingesting 2 TB of flight-test data per week"
    issues = cv_issues(profile, offer, set_bullet(text=text))
    assert issues == [
        "experiences[0].bullets[0]: names not found in the profile or the offer: Kubernetes"
    ]


def test_offer_vocabulary_is_allowed(profile, offer) -> None:
    # "Snowflake" is in the offer, so it is not an invented name (the number check still applies)
    text = "Built an Airflow pipeline ingesting 2 TB of data per week, like Snowflake loads"
    assert cv_issues(profile, offer, set_bullet(text=text)) == []


def test_first_word_of_sentence_is_not_a_name(profile, offer) -> None:
    text = "Engineered an Airflow pipeline ingesting 2 TB of flight-test data per week"
    assert cv_issues(profile, offer, set_bullet(text=text)) == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["headline"].update(source_id="hl-nope"), "unknown headline id 'hl-nope'"),
        (lambda d: d["skills"].append("sk-rust"), "unknown skill id 'sk-rust'"),
        (lambda d: d["experiences"][0].update(source_id="edu-supaero"), "unknown experience id"),
        (lambda d: d["projects"][0].update(source_id="proj-nope"), "unknown project id"),
        (lambda d: d["section_order"].append("skills"), "duplicate sections"),
        (set_bullet(text="   "), "empty text"),
        (
            lambda d: d["experiences"][1]["bullets"].append(d["experiences"][0]["bullets"][0]),
            "used twice",
        ),
    ],
)
def test_structural_errors(profile, offer, mutate, expected) -> None:
    assert any(expected in i for i in cv_issues(profile, offer, mutate))


def test_bullet_limits(profile, offer) -> None:
    issues = cv_issues(profile, offer, settings=CVSettings(max_bullets=4, max_bullets_per_entry=2))
    assert "cv: 6 bullets, maximum 4" in issues
    assert "experiences[0]: more than 2 bullets" in issues


# --- Letter -------------------------------------------------------------------------------


def test_valid_letter(profile, offer) -> None:
    assert letter_issues(profile, offer) == []


def set_paragraph(index, text=None, source_ids=None):
    def mutate(data):
        if text is not None:
            data["paragraphs"][index]["text"] = text
        if source_ids is not None:
            data["paragraphs"][index]["source_ids"] = source_ids

    return mutate


def test_letter_unknown_source(profile, offer) -> None:
    issues = letter_issues(profile, offer, set_paragraph(1, source_ids=["exp-airbus-1", "exp-x"]))
    assert any("unknown source id 'exp-x'" in i for i in issues)


def test_letter_invented_number(profile, offer) -> None:
    text = COVER_LETTER["paragraphs"][1]["text"].replace("2 TB", "20 TB")
    issues = letter_issues(profile, offer, set_paragraph(1, text=text))
    assert any("paragraphs[1]: numbers not found in the source: 20" in i for i in issues)


def test_letter_number_without_cited_source(profile, offer) -> None:
    # the numbers are right, but the paragraph no longer cites the bullets they come from
    issues = letter_issues(profile, offer, set_paragraph(1, source_ids=[]))
    assert any("paragraphs[1]: numbers not found" in i for i in issues)


def test_letter_can_use_offer_numbers(profile, offer) -> None:
    text = (
        COVER_LETTER["paragraphs"][0]["text"]
        + " You welcome profiles with 0–2 years of experience."
    )
    assert letter_issues(profile, offer, set_paragraph(0, text=text)) == []


def test_letter_invented_company(profile, offer) -> None:
    text = COVER_LETTER["paragraphs"][1]["text"].replace("At Airbus", "At Google")
    issues = letter_issues(profile, offer, set_paragraph(1, text=text))
    assert any("Google" in i for i in issues)


def test_letter_requires_motivation(profile, offer) -> None:
    issues = letter_issues(profile, offer, set_paragraph(2, source_ids=["exp-airbus-3"]))
    assert any("motivations" in i for i in issues)


def test_letter_word_count(profile, offer) -> None:
    def shorten(data):
        for p in data["paragraphs"]:
            p["text"] = "I want this Data Engineer role."

    assert any("words, expected 250–350" in i for i in letter_issues(profile, offer, shorten))
