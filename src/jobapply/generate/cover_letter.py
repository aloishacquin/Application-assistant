"""Cover letter generation -> CoverLetter (SPEC section 8.3)."""

from __future__ import annotations

from pathlib import Path

from jobapply.config import CoverLetterSettings
from jobapply.generate.runner import GenerationResult, generate_validated, offer_for_llm
from jobapply.generate.validator import validate_letter
from jobapply.llm.client import StructuredLLM
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile
from jobapply.models.tailored import CoverLetter

PROMPT_NAME = "write_cover_letter"


def generate_cover_letter(
    profile: Profile,
    offer: JobOffer,
    *,
    llm: StructuredLLM,
    prompts_dir: Path,
    settings: CoverLetterSettings,
) -> GenerationResult[CoverLetter]:
    return generate_validated(
        llm=llm,
        prompts_dir=prompts_dir,
        prompt_name=PROMPT_NAME,
        output_model=CoverLetter,
        validate=lambda letter: validate_letter(letter, profile, offer, settings),
        variables={
            "profile": profile.for_llm(),
            "offer": offer_for_llm(offer),
            "candidate_name": profile.identity.name,
            "min_words": str(settings.min_words),
            "max_words": str(settings.max_words),
        },
        label="Lettre",
    )
