"""Shared generation loop: structured LLM call -> fact validation -> one regeneration with the
errors as feedback -> explicit failure (SPEC section 8.3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from jobapply.generate.validator import Issue
from jobapply.llm.client import CallInfo, StructuredLLM
from jobapply.llm.prompts import load_prompt
from jobapply.models.offer import JobOffer

RETRY_PROMPT = "retry_feedback"


class GenerationError(Exception):
    """The generated content still failed fact validation after one regeneration."""

    def __init__(self, message: str, issues: list[Issue]) -> None:
        super().__init__(message + "\n" + "\n".join(f"- {issue}" for issue in issues))
        self.issues = issues


@dataclass(frozen=True)
class GenerationResult[T: BaseModel]:
    value: T
    model: str
    prompt_version: int
    calls: list[CallInfo]
    attempts: int


def offer_for_llm(offer: JobOffer) -> str:
    return json.dumps(offer.model_dump(exclude={"url"}), indent=2, ensure_ascii=False)


def generate_validated[T: BaseModel](
    *,
    llm: StructuredLLM,
    prompts_dir: Path,
    prompt_name: str,
    output_model: type[T],
    validate: Callable[[T], list[Issue]],
    variables: dict[str, str],
    label: str,
) -> GenerationResult[T]:
    prompt = load_prompt(prompt_name, prompts_dir)
    rendered = prompt.render(
        schema=json.dumps(output_model.model_json_schema(), indent=2), **variables
    )
    calls: list[CallInfo] = []
    content = rendered
    for attempt in (1, 2):
        result = llm.structured(
            content, output_model, prompt_name=prompt.name, prompt_version=prompt.version
        )
        calls += result.calls
        issues = validate(result.value)
        if not issues:
            return GenerationResult(result.value, result.model, prompt.version, calls, attempt)
        content = load_prompt(RETRY_PROMPT, prompts_dir).render(
            prompt=rendered,
            previous_answer=result.value.model_dump_json(indent=2),
            errors="\n".join(f"- {issue}" for issue in issues),
        )
    raise GenerationError(f"{label} rejeté par le validateur après régénération :", issues)
