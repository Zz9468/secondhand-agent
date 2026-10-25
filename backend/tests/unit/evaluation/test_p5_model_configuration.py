from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import pytest
from openai import LengthFinishReasonError

from evaluation.p5 import official
from evaluation.p5.legacy_config import aligned_config, configuration_audit, public_call_config


def test_legacy_alignment_changes_only_model_dependency_configuration():
    config = {
        "model": {"name": "model", "temperature": 0.3, "timeout": 30, "thinking": True},
        "budget": {
            "total_calls": 10,
            "total_tokens": 180000,
            "wall_seconds": 120,
            "output_tokens": 1024,
        },
        "reply_review": {"output_tokens": 3072, "thinking": True},
        "prompt": "old original prompt",
        "policy": {"minimum": 750},
    }
    original = deepcopy(config)
    result = aligned_config(config)
    assert config == original
    assert result["budget"] == {**original["budget"], "output_tokens": 2048}
    assert result["prompt"] == original["prompt"] and result["policy"] == original["policy"]
    assert result["reply_review"]["output_tokens"] == 2048


def test_actual_callback_config_is_allowlisted_and_mismatch_fails_closed():
    config = public_call_config(
        {"kwargs": {"request_timeout": 45, "max_retries": 0, "api_key": "SECRET"}},
        {
            "model": "m",
            "max_completion_tokens": 2048,
            "temperature": 0,
            "extra_body": {"enable_thinking": False, "private": "SECRET"},
        },
    )
    assert "SECRET" not in str(config)
    rows = [{"recorded_calls": [{"model_config": config}]}]
    assert configuration_audit(rows, "m")["status"] == "PASS"
    config["max_tokens"] = 1024
    assert configuration_audit(rows, "m")["status"] == "FAIL"
    del config["max_tokens"]
    assert configuration_audit(rows, "m")["status"] == "INSUFFICIENT_EVIDENCE"
    assert configuration_audit([], "m")["status"] == "INSUFFICIENT_EVIDENCE"


def truncation():
    return LengthFinishReasonError(
        completion=SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=20, completion_tokens=30, total_tokens=50),
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"reasoning":"truncated'))],
        )
    )


def test_truncated_offline_judge_usage_and_raw_output_remain_accounted(monkeypatch):
    monkeypatch.setattr(official.SemanticModel, "prices", lambda self: (Decimal("1"), Decimal("2")))
    rows = []
    recorder = official.UsageRecorder("openevals_judge", rows)
    recorder.on_llm_error(truncation(), run_id="run")
    assert rows[0]["status"] == "ERROR" and rows[0]["tokens"] == 50
    assert Decimal(rows[0]["estimated_cny"]) == Decimal("0.00008")
    assert rows[0]["raw_truncated_output"] == ['{"reasoning":"truncated']
    recorder.on_llm_error(TimeoutError(), run_id="timeout")
    assert rows[1]["tokens"] is None and rows[1]["estimated_cny"] is None


def test_judge_retries_only_truncation_and_keeps_negative_score(monkeypatch):
    attempts = []

    def once(case, trajectory, usage, limit):
        attempts.append(limit)
        if len(attempts) == 1:
            raise truncation()
        return {"score": False}

    monkeypatch.setattr(official, "judge_once", once)
    result = official.judge({}, [], [])
    assert attempts == [8192, 16384]
    assert result["score"] is False and result["attempts"] == 2
    assert result["recovered_errors"][0]["error_type"] == "LengthFinishReasonError"
    attempts.clear()
    monkeypatch.setattr(official, "judge_once", lambda *args: {"score": False})
    assert official.judge({}, [], [])["attempts"] == 1


def test_second_truncation_remains_error(monkeypatch):
    def fail(*args):
        raise truncation()

    monkeypatch.setattr(official, "judge_once", fail)
    with pytest.raises(LengthFinishReasonError):
        official.judge({}, [], [])
