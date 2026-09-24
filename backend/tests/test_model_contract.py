import pytest

from app.agent.model_contract import ModelCapabilities, require_negotiation_capabilities


def test_model_contract_accepts_required_capabilities() -> None:
    capabilities = ModelCapabilities(tool_calling=True, structured_output=True)

    require_negotiation_capabilities(capabilities)


@pytest.mark.parametrize(
    ("tool_calling", "structured_output", "missing"),
    [
        (False, True, "tool_calling"),
        (True, False, "structured_output"),
        (False, False, "tool_calling, structured_output"),
    ],
)
def test_model_contract_rejects_missing_capabilities(
    tool_calling: bool,
    structured_output: bool,
    missing: str,
) -> None:
    capabilities = ModelCapabilities(
        tool_calling=tool_calling,
        structured_output=structured_output,
    )

    with pytest.raises(ValueError, match=missing):
        require_negotiation_capabilities(capabilities)

