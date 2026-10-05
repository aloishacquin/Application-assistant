from pathlib import Path

import pytest
from pydantic import ValidationError

from jobapply.config import AppConfig, MatchingSettings
from jobapply.matching.scorer import (
    MatchJudgment,
    ProfileVocabulary,
    keyword_overlap,
    normalize,
    score_offer,
)
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile, load_profile
from tests.conftest import JUDGMENT, OFFER_NAMES, FakeLLM, expected_offer, offer_text


@pytest.fixture(scope="module")
def profile() -> Profile:
    return load_profile(Path(__file__).parent / "fixtures" / "profile.yaml")


def offer(name: str, **overrides) -> JobOffer:
    return JobOffer(**(expected_offer(name) | overrides), raw_text=offer_text(name))


FINANCE, TECH, ANALYST = OFFER_NAMES


# --- Matching rules ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Data-Quality", "data quality"),
        ("C++", "c++"),
        ("CI/CD", "ci/cd"),
        ("  Power  BI ", "power bi"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("term", "matches"),
    [
        ("python", True),  # skill name, case-insensitive
        ("Spark", True),  # tag
        ("Apache Airflow", True),  # contains a skill name
        ("PostgreSQL", True),  # found in a bullet
        ("CI/CD", True),  # one alternative is enough (tag "ci")
        ("APIs", True),  # plural of tag "api"
        ("English", True),  # spoken language
        ("Snowflake", False),
        ("Kafka", False),
        ("data", True),  # exact tag
        ("big data platform", False),
        ("", False),
        ("!!!", False),
    ],
)
def test_vocabulary(profile: Profile, term: str, matches: bool) -> None:
    assert ProfileVocabulary.from_profile(profile).matches(term) is matches


def test_tags_are_not_matched_inside_longer_terms(profile: Profile) -> None:
    # "data" is a tag but not a skill name: it must not match any term that merely contains it.
    assert not ProfileVocabulary.from_profile(profile).matches("data governance")


# --- Keyword overlap --------------------------------------------------------------------


def test_overlap_finance(profile: Profile) -> None:
    result = keyword_overlap(profile, offer(FINANCE), required_share=0.7)
    assert result.matched_required == ["Python", "SQL", "Spark", "Airflow"]
    assert result.missing_required == []
    assert "Snowflake" in result.missing_keywords
    assert "Apache Airflow" in result.matched_keywords
    # required terms are not counted twice in keywords
    assert "Python" not in result.matched_keywords + result.missing_keywords


def test_overlap_ranking_is_coherent(profile: Profile) -> None:
    scores = {n: keyword_overlap(profile, offer(n), 0.7).score for n in OFFER_NAMES}
    assert scores[FINANCE] > scores[TECH] > scores[ANALYST]


def test_overlap_without_required_skills(profile: Profile) -> None:
    result = keyword_overlap(profile, offer(FINANCE, required_skills=[]), 0.7)
    assert result.score == round(100 * len(result.matched_keywords) / 14, 1)


def test_overlap_empty_offer(profile: Profile) -> None:
    assert keyword_overlap(profile, offer(FINANCE, required_skills=[], keywords=[]), 0.7).score == 0


def test_overlap_deduplicates(profile: Profile) -> None:
    result = keyword_overlap(profile, offer(ANALYST, required_skills=["SQL", "sql", "Excel"]), 1.0)
    assert result.matched_required == ["SQL"]
    assert result.score == 50.0


# --- Final score ------------------------------------------------------------------------


def test_score_without_llm(profile: Profile) -> None:
    result = score_offer(profile, offer(FINANCE), MatchingSettings())
    match = result.match
    assert match.llm_score is None
    assert match.score == round(match.keyword_score)
    assert match.strengths == []
    assert result.calls == []


def test_score_with_llm(profile: Profile, tmp_config: AppConfig) -> None:
    llm = FakeLLM(lambda prompt: JUDGMENT)
    result = score_offer(
        profile,
        offer(FINANCE),
        MatchingSettings(),
        llm=llm,
        prompts_dir=tmp_config.paths.prompts_dir,
    )
    match = result.match
    assert match.llm_score == 80
    assert match.score == round(0.4 * match.keyword_score + 0.6 * 80)
    assert match.strengths == JUDGMENT["strengths"]
    assert match.gaps == JUDGMENT["gaps"]
    assert match.summary == JUDGMENT["summary"]
    assert match.model == "fake-model"
    assert match.prompt_version == 1
    assert len(result.calls) == 1


def test_llm_prompt_content(profile: Profile, tmp_config: AppConfig) -> None:
    llm = FakeLLM(lambda prompt: JUDGMENT)
    score_offer(
        profile,
        offer(FINANCE),
        MatchingSettings(),
        llm=llm,
        prompts_dir=tmp_config.paths.prompts_dir,
    )
    prompt = llm.prompts[0]
    assert "exp-airbus-1" in prompt  # profile facts with their ids
    assert "Lion City Bank" in prompt  # raw offer text
    assert '"strengths"' in prompt  # schema injected
    for private in ("camille.martin@example.com", "+33 6", "birth_year", "min_salary_sgd"):
        assert private not in prompt


def test_llm_requires_prompts_dir(profile: Profile) -> None:
    with pytest.raises(ValueError, match="prompts_dir"):
        score_offer(profile, offer(FINANCE), MatchingSettings(), llm=FakeLLM(lambda p: JUDGMENT))


@pytest.mark.parametrize(("years", "penalty"), [(None, 0), (0, 0), (2, 20), (5, 50)])
def test_experience_penalty(profile: Profile, years: int | None, penalty: float) -> None:
    base = score_offer(profile, offer(FINANCE), MatchingSettings()).match
    result = score_offer(profile, offer(FINANCE, min_years_experience=years), MatchingSettings())
    assert result.match.experience_penalty == penalty
    assert result.match.score == max(0, round(base.keyword_score - penalty))


def test_experience_counts_profile_years(profile: Profile) -> None:
    senior_profile = profile.model_copy(
        update={"constraints": profile.constraints.model_copy(update={"years_experience": 2})}
    )
    result = score_offer(senior_profile, offer(FINANCE, min_years_experience=3), MatchingSettings())
    assert result.match.experience_gap_years == 1


def test_score_is_clamped(profile: Profile) -> None:
    result = score_offer(profile, offer(ANALYST, min_years_experience=10), MatchingSettings())
    assert result.match.score == 0


@pytest.mark.parametrize(
    "bad",
    [{"score": 101}, {"score": -1}, {"strengths": ["a", "b"]}, {"gaps": ["a", "b", "c", "d"]}],
)
def test_judgment_validation(bad: dict) -> None:
    with pytest.raises(ValidationError):
        MatchJudgment.model_validate(JUDGMENT | bad)
