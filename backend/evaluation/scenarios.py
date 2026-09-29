import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from evaluation.schemas import EvaluationScenario


class _ScenarioSetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_set_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    defaults: dict[str, JsonValue] = Field(default_factory=dict)
    scenarios: list[dict[str, JsonValue]] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ScenarioSet:
    version: str
    content_hash: str
    scenarios: tuple[EvaluationScenario, ...]


def default_scenario_path() -> Path:
    return Path(__file__).resolve().parent / "scenarios" / "v2.json"


def load_scenarios(path: Path | None = None) -> ScenarioSet:
    source = path or default_scenario_path()
    raw = source.read_bytes()
    document = _ScenarioSetDocument.model_validate_json(raw)
    scenarios = tuple(
        EvaluationScenario.model_validate(
            _deep_merge(document.defaults, scenario_payload)
        )
        for scenario_payload in document.scenarios
    )
    identities = [
        (scenario.scenario_id, scenario.scenario_version)
        for scenario in scenarios
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("场景 ID 与版本组合必须唯一")
    canonical = json.dumps(
        {
            "scenario_set_version": document.scenario_set_version,
            "scenarios": [scenario.model_dump(mode="json") for scenario in scenarios],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ScenarioSet(
        version=document.scenario_set_version,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        scenarios=scenarios,
    )


def _deep_merge(
    defaults: dict[str, JsonValue],
    override: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    merged = deepcopy(defaults)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = deepcopy(value)
    return merged
