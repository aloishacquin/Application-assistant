"""Profile/offer score (SPEC section 6.2): deterministic keyword overlap + short LLM judgement.

final = weights.keywords * keyword_score + weights.llm * llm_score - experience penalty,
clamped to 0–100. Without the LLM, the keyword score alone is used.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from jobapply.config import MatchingSettings
from jobapply.llm.client import CallInfo, StructuredLLM
from jobapply.llm.prompts import load_prompt
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile

PROMPT_NAME = "score_match"


class MatchJudgment(BaseModel):
    """LLM output."""

    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    strengths: list[str] = Field(min_length=3, max_length=3)
    gaps: list[str] = Field(min_length=3, max_length=3)
    summary: str


class MatchScore(BaseModel):
    score: int
    keyword_score: float
    llm_score: int | None
    matched_required: list[str]
    missing_required: list[str]
    matched_keywords: list[str]
    missing_keywords: list[str]
    experience_gap_years: float
    experience_penalty: float
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    summary: str | None = None
    model: str | None = None
    prompt_version: int | None = None


# --- Deterministic part -------------------------------------------------------------------


def normalize(term: str) -> str:
    text = term.casefold().replace("-", " ").replace("_", " ")
    text = re.sub(r"[^\w+#./ ]", " ", text)
    return " ".join(text.split())


def _contains(haystack: str, needle: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None


@dataclass(frozen=True)
class ProfileVocabulary:
    exact: frozenset[str]  # skill names, tags, spoken languages
    skill_names: frozenset[str]  # also matched inside longer offer terms ("Apache Airflow")
    corpus: str  # profile text, for multi-word terms written in bullets

    @classmethod
    def from_profile(cls, profile: Profile) -> ProfileVocabulary:
        skill_names = {normalize(s.name) for s in profile.skills}
        tags = {normalize(t) for el in profile.index.values() for t in getattr(el, "tags", [])}
        languages = {normalize(lang.name) for lang in profile.languages}
        texts = [h.text for h in profile.headline_variants]
        texts += [b.text for b in profile.bullets.values()]
        texts += [e.title for e in profile.experiences]
        texts += [e.degree for e in profile.education]
        texts += [f"{p.name} {p.description}" for p in profile.projects]
        return cls(
            exact=frozenset(skill_names | tags | languages) - {""},
            skill_names=frozenset(skill_names) - {""},
            corpus=" | ".join(normalize(t) for t in texts),
        )

    def matches(self, term: str) -> bool:
        needle = normalize(term)
        if not needle:
            return False
        # "CI/CD", "Power BI/Tableau": any alternative is enough.
        candidates = {needle, *(part.strip() for part in needle.split("/") if part.strip())}
        # Tolerate plurals: "APIs" -> "api".
        candidates |= {c[:-1] for c in candidates if len(c) > 3 and c.endswith("s")}
        return any(self._matches_one(c) for c in candidates)

    def _matches_one(self, needle: str) -> bool:
        if needle in self.exact:
            return True
        if any(_contains(needle, name) for name in self.skill_names):
            return True
        return _contains(self.corpus, needle)


def _unique(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for term in terms:
        key = normalize(term)
        if key and key not in seen:
            seen.add(key)
            result.append(term)
    return result


@dataclass(frozen=True)
class KeywordOverlap:
    score: float  # 0–100
    matched_required: list[str]
    missing_required: list[str]
    matched_keywords: list[str]
    missing_keywords: list[str]


def keyword_overlap(profile: Profile, offer: JobOffer, required_share: float) -> KeywordOverlap:
    vocab = ProfileVocabulary.from_profile(profile)
    required = _unique(offer.required_skills)
    required_keys = {normalize(t) for t in required}
    keywords = [k for k in _unique(offer.keywords) if normalize(k) not in required_keys]

    matched_req = [t for t in required if vocab.matches(t)]
    matched_kw = [t for t in keywords if vocab.matches(t)]

    parts = []
    if required:
        parts.append((required_share, len(matched_req) / len(required)))
    if keywords:
        parts.append((1 - required_share, len(matched_kw) / len(keywords)))
    total_weight = sum(w for w, _ in parts)
    score = 100 * sum(w * r for w, r in parts) / total_weight if total_weight else 0.0

    return KeywordOverlap(
        score=round(score, 1),
        matched_required=matched_req,
        missing_required=[t for t in required if t not in matched_req],
        matched_keywords=matched_kw,
        missing_keywords=[t for t in keywords if t not in matched_kw],
    )


def experience_gap(profile: Profile, offer: JobOffer) -> float:
    if offer.min_years_experience is None:
        return 0.0
    return max(0.0, offer.min_years_experience - profile.constraints.years_experience)


# --- LLM part -----------------------------------------------------------------------------


@dataclass(frozen=True)
class JudgmentResult:
    judgment: MatchJudgment
    model: str
    prompt_version: int
    calls: list[CallInfo]


def judge_match(
    profile: Profile, offer: JobOffer, *, llm: StructuredLLM, prompts_dir: Path
) -> JudgmentResult:
    prompt = load_prompt(PROMPT_NAME, prompts_dir)
    rendered = prompt.render(
        schema=json.dumps(MatchJudgment.model_json_schema(), indent=2),
        profile=profile.for_llm(),
        offer=offer.raw_text,
    )
    result = llm.structured(
        rendered, MatchJudgment, prompt_name=prompt.name, prompt_version=prompt.version
    )
    return JudgmentResult(result.value, result.model, prompt.version, result.calls)


# --- Final score --------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoreResult:
    match: MatchScore
    calls: list[CallInfo]


def score_offer(
    profile: Profile,
    offer: JobOffer,
    settings: MatchingSettings,
    *,
    llm: StructuredLLM | None = None,
    prompts_dir: Path | None = None,
) -> ScoreResult:
    """Score an offer; pass `llm` (and `prompts_dir`) to include the LLM judgement."""
    overlap = keyword_overlap(profile, offer, settings.required_share)
    gap = experience_gap(profile, offer)
    penalty = gap * settings.experience_penalty_per_year

    judgment: JudgmentResult | None = None
    if llm is not None:
        if prompts_dir is None:
            raise ValueError("prompts_dir is required with an LLM")
        judgment = judge_match(profile, offer, llm=llm, prompts_dir=prompts_dir)
        weights = settings.weights
        raw = weights.keywords * overlap.score + weights.llm * judgment.judgment.score
    else:
        raw = overlap.score

    match = MatchScore(
        score=max(0, min(100, round(raw - penalty))),
        keyword_score=overlap.score,
        llm_score=judgment.judgment.score if judgment else None,
        matched_required=overlap.matched_required,
        missing_required=overlap.missing_required,
        matched_keywords=overlap.matched_keywords,
        missing_keywords=overlap.missing_keywords,
        experience_gap_years=gap,
        experience_penalty=penalty,
        strengths=judgment.judgment.strengths if judgment else [],
        gaps=judgment.judgment.gaps if judgment else [],
        summary=judgment.judgment.summary if judgment else None,
        model=judgment.model if judgment else None,
        prompt_version=judgment.prompt_version if judgment else None,
    )
    return ScoreResult(match=match, calls=judgment.calls if judgment else [])
