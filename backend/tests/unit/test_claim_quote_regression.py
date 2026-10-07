import pytest

from app.agent.claim_safety import find_unsafe_claims


@pytest.mark.parametrize(
    "reply",
    [
        "您附加的‘保证今天发货’这一条件目前无法作为正式条款被接受。",
        "‘卖家已同意’的说法未获授权，不能作为审批结果。",
    ],
)
def test_rejected_quoted_conditions_are_not_promises(reply):
    assert not find_unsafe_claims(reply)


@pytest.mark.parametrize(
    "reply,code",
    [
        ("我承诺‘保证今天发货’，可以放心。", "UNAUTHORIZED_DISPATCH"),
        ("卖家已同意，这个保证今天发货的条件可以接受。", "FALSE_APPROVAL"),
    ],
)
def test_quoting_an_actual_promise_does_not_bypass_detection(reply, code):
    assert code in find_unsafe_claims(reply)
