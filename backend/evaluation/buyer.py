import hashlib
import json
from dataclasses import dataclass

from evaluation.schemas import BuyerTurn, EvaluationScenario


@dataclass(frozen=True, slots=True)
class SimulatedBuyer:
    """按版本化脚本回放合成买家输入；同一场景在三组中完全一致。"""

    scenario: EvaluationScenario
    random_seed: int

    def turns(self) -> tuple[BuyerTurn, ...]:
        return tuple(
            BuyerTurn.model_validate(item.model_dump(mode="json"))
            for item in self.scenario.turns
        )

    def input_fingerprint(self) -> str:
        canonical = json.dumps(
            [item.model_dump(mode="json") for item in self.turns()],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
