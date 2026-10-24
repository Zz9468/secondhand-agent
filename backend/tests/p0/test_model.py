import json

import httpx
import pytest
from openai import APITimeoutError, AuthenticationError, InternalServerError, OpenAI, RateLimitError
from pydantic import ValidationError

from app.p0.model import ProbeBudget, parse_proposal


@pytest.mark.parametrize(
    "status,error", [(401, AuthenticationError), (429, RateLimitError), (500, InternalServerError)]
)
def test_sdk_error_classification_and_no_hidden_retry(status, error):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": "synthetic"}})

    sdk = OpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(error):
        sdk.chat.completions.create(model="qwen3.7-flash", messages=[])
    assert len(calls) == 1


def test_sdk_timeout_and_budget_keep_unknown_reservation():
    def timeout(request):
        raise httpx.ReadTimeout("synthetic", request=request)

    budget = ProbeBudget()
    budget.reserve("probe")
    sdk = OpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(timeout)),
    )
    with pytest.raises(APITimeoutError):
        sdk.chat.completions.create(model="qwen3.7-flash", messages=[])
    assert budget.calls == 1 and budget.reserved > 0
    for _ in range(5):
        budget.reserve("probe")
    with pytest.raises(RuntimeError, match="budget"):
        budget.reserve("probe")


def test_structured_output_rejects_missing_fields_and_false_evidence():
    with pytest.raises(ValidationError):
        parse_proposal('{"intent":"PROPOSE_OFFER"}', "800自提")
    with pytest.raises(ValueError):
        parse_proposal(
            json.dumps(
                {
                    "intent": "PROPOSE_OFFER",
                    "amount": "800",
                    "delivery": "pickup",
                    "source_quote": "900自提",
                }
            ),
            "800自提",
        )
