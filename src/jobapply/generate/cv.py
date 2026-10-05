"""Content selection + light rephrasing -> TailoredCV (SPEC section 8.3)."""

from __future__ import annotations

from pathlib import Path

from jobapply.config import CVSettings
from jobapply.generate.runner import GenerationResult, generate_validated, offer_for_llm
from jobapply.generate.validator import validate_cv
from jobapply.llm.client import StructuredLLM
from jobapply.models.offer import JobOffer
from jobapply.models.profile import Profile
from jobapply.models.tailored import TailoredCV

PROMPT_NAME = "select_cv_content"


def generate_cv(
    profile: Profile,
    offer: JobOffer,
    *,
    llm: StructuredLLM,
    prompts_dir: Path,
    settings: CVSettings,
) -> GenerationResult[TailoredCV]:
    return generate_validated(
        llm=llm,
        prompts_dir=prompts_dir,
        prompt_name=PROMPT_NAME,
        output_model=TailoredCV,
        validate=lambda cv: validate_cv(cv, profile, offer, settings),
        variables={
            "profile": profile.for_llm(),
            "offer": offer_for_llm(offer),
            "max_bullets": str(settings.max_bullets),
            "max_bullets_per_entry": str(settings.max_bullets_per_entry),
            "max_pages": str(settings.max_pages),
        },
        label="CV",
    )
