"""Anthropic wrapper: structured JSON output validated by Pydantic, retry, token/cost logging.

Structured outputs (`output_config.format`) guarantee the answer follows the JSON schema; the
Pydantic model is still the source of truth (business validators such as salary ranges), and a
failed validation triggers up to `max_retries` new attempts with the errors as feedback.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

import anthropic
from anthropic.types.beta import BetaMessage
from pydantic import BaseModel, ValidationError

from jobapply.config import AppConfig, LLMSettings, ModelPricing
from jobapply.llm.prompts import load_prompt

logger = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
RETRY_PROMPT = "retry_feedback"


class LLMError(Exception):
    """Base error for LLM calls."""


class LLMRefusalError(LLMError):
    """The model (and its fallbacks) declined the request."""


class LLMValidationError(LLMError):
    """The output still failed validation after all retries."""


@dataclass(frozen=True)
class CallInfo:
    prompt_name: str
    prompt_version: int
    attempt: int
    model_requested: str
    model_served: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    cost_usd: float | None
    duration_s: float
    valid: bool
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass(frozen=True)
class StructuredResult[T: BaseModel]:
    value: T
    calls: list[CallInfo]

    @property
    def model(self) -> str:
        return self.calls[-1].model_served

    @property
    def cost_usd(self) -> float | None:
        costs = [c.cost_usd for c in self.calls]
        return None if any(c is None for c in costs) else sum(c for c in costs if c is not None)


class StructuredLLM(Protocol):
    def structured[T: BaseModel](
        self, prompt: str, output_model: type[T], *, prompt_name: str, prompt_version: int
    ) -> StructuredResult[T]: ...


def estimate_cost(
    pricing: ModelPricing | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float | None:
    if pricing is None:
        return None
    return (
        input_tokens * pricing.input
        + output_tokens * pricing.output
        + cache_creation_input_tokens * pricing.cache_write
        + cache_read_input_tokens * pricing.cache_read
    ) / 1_000_000


def format_errors(exc: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()
    )


class AnthropicLLM:
    def __init__(
        self,
        *,
        model: str,
        settings: LLMSettings,
        prompts_dir: Path,
        client: anthropic.Anthropic | None = None,
        log_path: Path | None = None,
    ) -> None:
        self.model = model
        self.settings = settings
        self.prompts_dir = prompts_dir
        self.log_path = log_path
        self._client = client or anthropic.Anthropic()

    @classmethod
    def from_config(cls, cfg: AppConfig) -> AnthropicLLM:
        if not cfg.secrets.anthropic_model:
            raise LLMError("ANTHROPIC_MODEL n'est pas défini (voir .env.example).")
        key = cfg.secrets.anthropic_api_key
        # Without an explicit key, the SDK resolves credentials from the environment.
        client = anthropic.Anthropic(api_key=key.get_secret_value()) if key else None
        return cls(
            model=cfg.secrets.anthropic_model,
            settings=cfg.settings.llm,
            prompts_dir=cfg.paths.prompts_dir,
            client=client,
            log_path=cfg.paths.llm_log,
        )

    def structured[T: BaseModel](
        self, prompt: str, output_model: type[T], *, prompt_name: str, prompt_version: int
    ) -> StructuredResult[T]:
        output_format = {"type": "json_schema", "schema": anthropic.transform_schema(output_model)}
        calls: list[CallInfo] = []
        content = prompt
        last_error: ValidationError | None = None

        for attempt in range(1, self.settings.max_retries + 2):
            start = time.monotonic()
            response = self._client.beta.messages.create(**self._request(content, output_format))
            text = next((b.text for b in response.content if b.type == "text"), "")

            value: T | None = None
            if response.stop_reason not in ("refusal", "max_tokens"):
                try:
                    value = output_model.model_validate_json(text)
                except ValidationError as exc:
                    last_error = exc

            calls.append(
                self._record(
                    response, prompt_name, prompt_version, attempt, start, value is not None
                )
            )
            if response.stop_reason == "refusal":
                raise LLMRefusalError(f"Requête refusée par le modèle ({prompt_name}).")
            if response.stop_reason == "max_tokens":
                raise LLMError(
                    f"Réponse tronquée (max_tokens={self.settings.max_tokens}) pour {prompt_name}."
                )
            if value is not None:
                return StructuredResult(value=value, calls=calls)

            assert last_error is not None
            logger.warning(
                "Invalid output for %s (attempt %d):\n%s",
                prompt_name,
                attempt,
                format_errors(last_error),
            )
            content = load_prompt(RETRY_PROMPT, self.prompts_dir).render(
                prompt=prompt, previous_answer=text, errors=format_errors(last_error)
            )

        assert last_error is not None
        raise LLMValidationError(
            f"Sortie invalide pour {prompt_name} après {len(calls)} essais :\n"
            + format_errors(last_error)
        )

    def _request(self, content: str, output_format: dict[str, Any]) -> dict[str, Any]:
        output_config: dict[str, Any] = {"format": output_format}
        if self.settings.effort:
            output_config["effort"] = self.settings.effort
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.max_tokens,
            "messages": [{"role": "user", "content": content}],
            "output_config": output_config,
        }
        if self.settings.refusal_fallback:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def _record(
        self,
        response: BetaMessage,
        prompt_name: str,
        prompt_version: int,
        attempt: int,
        start: float,
        valid: bool,
    ) -> CallInfo:
        usage = response.usage
        tokens = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens or 0,
            "cache_read_input_tokens": usage.cache_read_input_tokens or 0,
        }
        pricing = self.settings.pricing.get(response.model)
        if pricing is None:
            logger.warning("No pricing configured for model %s: cost unknown", response.model)
        info = CallInfo(
            prompt_name=prompt_name,
            prompt_version=prompt_version,
            attempt=attempt,
            model_requested=self.model,
            model_served=response.model,
            stop_reason=response.stop_reason,
            cost_usd=estimate_cost(pricing, **tokens),
            duration_s=round(time.monotonic() - start, 2),
            valid=valid,
            **tokens,
        )
        logger.info(
            "LLM %s v%d #%d %s: in=%d out=%d cache_w=%d cache_r=%d cost=%s",
            prompt_name,
            prompt_version,
            attempt,
            info.model_served,
            info.input_tokens,
            info.output_tokens,
            info.cache_creation_input_tokens,
            info.cache_read_input_tokens,
            f"${info.cost_usd:.4f}" if info.cost_usd is not None else "?",
        )
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(info)) + "\n")
        return info


@dataclass(frozen=True)
class CostSummary:
    today_usd: float
    month_usd: float
    unknown_calls: int  # calls whose cost could not be estimated (no pricing configured)


def summarize_costs(log_path: Path, today: date) -> CostSummary:
    """Totals from the JSONL call log, by UTC day and month."""
    day_total = month_total = 0.0
    unknown = 0
    if log_path.is_file():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                when = datetime.fromisoformat(record["timestamp"]).date()
            except (ValueError, KeyError, TypeError):
                continue
            if (when.year, when.month) != (today.year, today.month):
                continue
            cost = record.get("cost_usd")
            if cost is None:
                unknown += 1
                continue
            month_total += cost
            if when == today:
                day_total += cost
    return CostSummary(round(day_total, 4), round(month_total, 4), unknown)
