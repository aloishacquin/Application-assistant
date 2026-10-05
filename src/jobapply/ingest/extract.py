"""Raw text -> JobOffer via the LLM (prompt prompts/extract_offer.md, SPEC section 6.1)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jobapply.llm.client import CallInfo, StructuredLLM
from jobapply.llm.prompts import load_prompt
from jobapply.models.offer import JobOffer, JobOfferExtraction

PROMPT_NAME = "extract_offer"


@dataclass(frozen=True)
class ExtractionResult:
    offer: JobOffer
    model: str
    prompt_version: int
    calls: list[CallInfo]


def extract_offer(
    raw_text: str, *, llm: StructuredLLM, prompts_dir: Path, url: str | None = None
) -> ExtractionResult:
    prompt = load_prompt(PROMPT_NAME, prompts_dir)
    rendered = prompt.render(
        schema=json.dumps(JobOfferExtraction.model_json_schema(), indent=2),
        offer_text=raw_text,
    )
    result = llm.structured(
        rendered, JobOfferExtraction, prompt_name=prompt.name, prompt_version=prompt.version
    )
    offer = JobOffer(**result.value.model_dump(), url=url, raw_text=raw_text)
    return ExtractionResult(
        offer=offer, model=result.model, prompt_version=prompt.version, calls=result.calls
    )
