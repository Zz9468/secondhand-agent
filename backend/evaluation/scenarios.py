import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evaluation.schemas import EvaluationScenario


class _ScenarioSetDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_set_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    scenarios: list[EvaluationScenario] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class ScenarioSet:
    version: str
    content_hash: str
    scenarios: tuple[EvaluationScenario, ...]


def default_scenario_path() -> Path:
    return Path(__file__).resolve().parent / "scenarios" / "v1.json"


def load_scenarios(path: Path | None = None) -> ScenarioSet:
    source = path or default_scenario_path()
    raw = source.read_bytes()
    document = _ScenarioSetDocument.model_validate_json(raw)
    identities = [
        (scenario.scenario_id, scenario.scenario_version)
        for scenario in document.scenarios
    ]
    if len(identities) != len(set(identities)):
        raise ValueError("场景 ID 与版本组合必须唯一")
    canonical = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return ScenarioSet(
        version=document.scenario_set_version,
        content_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        scenarios=tuple(document.scenarios),
    )
