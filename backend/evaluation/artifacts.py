import json
import re
from pathlib import Path

from evaluation.reporting import write_derived_artifacts
from evaluation.runner import BatchResult

_SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def default_results_dir() -> Path:
    return Path(__file__).resolve().parent / "results"


def write_batch_artifacts(
    result: BatchResult,
    output_root: Path | None = None,
) -> Path:
    """将单一事实源写成 JSON/JSONL；已有批次目录不会被覆盖。"""

    if not _SAFE_BATCH_ID.fullmatch(result.manifest.batch_id):
        raise ValueError("batch_id 只能包含字母、数字、点、下划线和连字符")
    root = (output_root or default_results_dir()).resolve()
    batch_dir = (root / result.manifest.batch_id).resolve()
    if batch_dir.parent != root:
        raise ValueError("batch_id 不能逃逸评测结果目录")
    batch_dir.mkdir(parents=True, exist_ok=False)

    _write_json(batch_dir / "manifest.json", result.manifest.model_dump(mode="json"))
    _write_jsonl(
        batch_dir / "runs.jsonl",
        [run.model_dump(mode="json", exclude={"events"}) for run in result.runs],
    )
    event_rows: list[dict[str, object]] = []
    for run in result.runs:
        for event in run.events:
            event_rows.append(
                {
                    "contract_version": run.contract_version,
                    "result_schema_version": run.result_schema_version,
                    "batch_id": run.batch_id,
                    "run_id": run.run_id,
                    "experiment_group": run.experiment_group.value,
                    "scenario_id": run.scenario_id,
                    "scenario_version": run.scenario_version,
                    **event.model_dump(mode="json"),
                }
            )
    _write_jsonl(batch_dir / "events.jsonl", event_rows)
    write_derived_artifacts(batch_dir)
    return batch_dir


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    content = "".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
        for row in rows
    )
    path.write_text(content, encoding="utf-8")
