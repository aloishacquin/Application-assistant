import json
from pathlib import Path
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from jobapply.config import AppConfig, LLMSettings
from jobapply.llm.client import (
    FALLBACK_BETA,
    AnthropicLLM,
    LLMError,
    LLMRefusalError,
    LLMValidationError,
)
from jobapply.models.offer import JobOfferExtraction
from tests.conftest import OFFER_NAMES, expected_offer

VALID = json.dumps(expected_offer(OFFER_NAMES[0]))
INVALID = json.dumps(expected_offer(OFFER_NAMES[0]) | {"salary_min_sgd": 9999})  # min > max


def message(
    text: str, stop_reason: str = "end_turn", model: str = "claude-opus-5-5"
) -> BetaMessage:
    return BetaMessage.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": text},
            ],
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 2000,
                "output_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        }
    )


class FakeMessages:
    def __init__(self, responses: list[BetaMessage]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> BetaMessage:
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeAnthropic:
    def __init__(self, responses: list[BetaMessage]) -> None:
        self.messages = FakeMessages(responses)
        self.beta = self  # client.beta.messages.create(...)


def make_llm(cfg: AppConfig, responses: list[BetaMessage], **overrides: Any) -> AnthropicLLM:
    settings = cfg.settings.llm.model_copy(update=overrides)
    return AnthropicLLM(
        model="claude-opus-5-5",
        settings=settings,
        prompts_dir=cfg.paths.prompts_dir,
        client=FakeAnthropic(responses),  # type: ignore[arg-type]
        log_path=cfg.paths.llm_log,
    )


def requests_of(llm: AnthropicLLM) -> list[dict[str, Any]]:
    return llm._client.messages.requests  # type: ignore[attr-defined]


def run(llm: AnthropicLLM):
    return llm.structured(
        "PROMPT", JobOfferExtraction, prompt_name="extract_offer", prompt_version=1
    )


def test_success_first_try(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message(VALID)])
    result = run(llm)

    assert result.value.company == "Lion City Bank"
    assert len(result.calls) == 1
    # Opus 5.5 pricing from settings.yaml: 2000 * 4 + 1000 * 20 per million tokens
    assert result.cost_usd == pytest.approx(0.028)
    assert result.model == "claude-opus-5-5"


def test_request_shape(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message(VALID)])
    run(llm)
    request = requests_of(llm)[0]

    assert request["model"] == "claude-opus-5-5"
    assert request["max_tokens"] == tmp_config.settings.llm.max_tokens
    assert request["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert request["output_config"]["effort"] == "medium"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "salary_min_sgd" in request["output_config"]["format"]["schema"]["properties"]
    assert request["betas"] == [FALLBACK_BETA]
    assert request["fallbacks"] == "default"
    assert "thinking" not in request  # always on for Opus 5.5: controlled by effort


def test_options_can_be_disabled(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message(VALID)], effort=None, refusal_fallback=False)
    run(llm)
    request = requests_of(llm)[0]
    assert "effort" not in request["output_config"]
    assert "betas" not in request
    assert "fallbacks" not in request


def test_retry_with_feedback_then_success(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message(INVALID), message(VALID)])
    result = run(llm)

    assert [c.valid for c in result.calls] == [False, True]
    retry_prompt = requests_of(llm)[1]["messages"][0]["content"]
    assert retry_prompt.startswith("PROMPT")
    assert INVALID in retry_prompt
    assert "salary_min_sgd must be <= salary_max_sgd" in retry_prompt


def test_invalid_json_triggers_retry(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message("not json"), message(VALID)])
    assert len(run(llm).calls) == 2


def test_fails_after_max_retries(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message(INVALID)] * 3)
    with pytest.raises(LLMValidationError, match="3 essais"):
        run(llm)
    assert len(requests_of(llm)) == 3  # 1 attempt + max_retries (2)


def test_refusal_is_not_retried(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message("", stop_reason="refusal")])
    with pytest.raises(LLMRefusalError):
        run(llm)


def test_truncated_output(tmp_config: AppConfig) -> None:
    llm = make_llm(tmp_config, [message('{"title": "Da', stop_reason="max_tokens")])
    with pytest.raises(LLMError, match="tronquée"):
        run(llm)


def test_calls_are_logged_to_jsonl(tmp_config: AppConfig) -> None:
    run(make_llm(tmp_config, [message(INVALID), message(VALID)]))
    lines = tmp_config.paths.llm_log.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]

    assert [r["attempt"] for r in records] == [1, 2]
    assert records[0]["input_tokens"] == 2000
    assert records[0]["prompt_name"] == "extract_offer"
    assert records[0]["cost_usd"] == pytest.approx(0.028)


def test_unknown_model_pricing_gives_unknown_cost(tmp_config: AppConfig) -> None:
    result = run(make_llm(tmp_config, [message(VALID, model="claude-future-9")]))
    assert result.calls[0].cost_usd is None
    assert result.cost_usd is None
    assert result.model == "claude-future-9"


def test_from_config_requires_model(tmp_config: AppConfig) -> None:
    with pytest.raises(LLMError, match="ANTHROPIC_MODEL"):
        AnthropicLLM.from_config(tmp_config)


def test_from_config_with_env(tmp_root: Path) -> None:
    from jobapply.config import load_config

    (tmp_root / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-test\nANTHROPIC_MODEL=claude-opus-5-5\n", encoding="utf-8"
    )
    llm = AnthropicLLM.from_config(load_config(tmp_root))
    assert llm.model == "claude-opus-5-5"
    assert isinstance(llm.settings, LLMSettings)


def test_summarize_costs(tmp_path: Path) -> None:
    from datetime import date

    from jobapply.llm.client import summarize_costs

    log = tmp_path / "llm_calls.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"timestamp": "2026-10-04T08:00:00+00:00", "cost_usd": 0.02}),
                json.dumps({"timestamp": "2026-10-04T09:00:00+00:00", "cost_usd": 0.03}),
                json.dumps({"timestamp": "2026-10-01T09:00:00+00:00", "cost_usd": 0.10}),
                json.dumps({"timestamp": "2026-09-30T09:00:00+00:00", "cost_usd": 5.0}),
                json.dumps({"timestamp": "2026-10-02T09:00:00+00:00", "cost_usd": None}),
                "not json",
            ]
        ),
        encoding="utf-8",
    )
    summary = summarize_costs(log, date(2026, 10, 4))
    assert summary.today_usd == pytest.approx(0.05)
    assert summary.month_usd == pytest.approx(0.15)
    assert summary.unknown_calls == 1
    assert summarize_costs(tmp_path / "missing.jsonl", date(2026, 10, 4)).month_usd == 0
