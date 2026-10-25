"""Persist real native callback events before optional remote delivery; never reconstruct spans."""

import json
import threading
from datetime import datetime
from pathlib import Path

from urllib3.util.retry import Retry

from app.p0.tracing import SafeTraceClient, safe_content, safe_run


class JournalTraceClient(SafeTraceClient):
    def __init__(self, path, *, mirror_remote=False, **kwargs):
        self.journal_path = Path(path)
        self.journal_lock = threading.Lock()
        self.mirror_remote = mirror_remote
        self.journal_path.open("x", encoding="utf-8").close()
        super().__init__(
            auto_batch_tracing=mirror_remote,
            omit_traced_runtime_info=True,
            hide_inputs=safe_content,
            hide_outputs=safe_content,
            hide_metadata=safe_content,
            timeout_ms=(1000, 1500),
            retry_config=Retry(total=0),
            **kwargs,
        )

    def record(self, operation, run):
        with self.journal_lock, self.journal_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {"operation": operation, "run": safe_run(run)},
                    default=lambda value: (
                        value.isoformat() if isinstance(value, datetime) else str(value)
                    ),
                )
                + "\n"
            )

    def create_run(self, name, inputs, run_type, **kwargs):
        run = {**kwargs, "name": name, "inputs": inputs, "run_type": run_type}
        self.record("create", run)
        if self.mirror_remote:
            return super().create_run(name, inputs, run_type, **kwargs)

    def update_run(self, run_id, **kwargs):
        self.record("update", {**kwargs, "id": run_id})
        if self.mirror_remote:
            return super().update_run(run_id, **kwargs)


def read_native_runs(path):
    created, updates = {}, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        run = safe_run(event["run"])
        if event["operation"] == "create":
            if run["id"] in created and created[run["id"]] != run:
                raise ValueError("conflicting native create event")
            created[run["id"]] = run
        elif event["operation"] == "update":
            updates.append(run)
        else:
            raise ValueError("unknown native journal event")
    for update in updates:
        if update["id"] not in created:
            raise ValueError("native update has no actual create event")
        created[update["id"]].update(update)
    return list(created.values())


def journal_summary(path, expected_refs):
    runs = read_native_runs(path)
    roots = [r for r in runs if r.get("name") == "LangGraph" and r.get("end_time")]
    refs = {r.get("extra", {}).get("metadata", {}).get("run_ref") for r in roots}
    incomplete = sum(not r.get("end_time") for r in runs)
    return {
        "status": "PASS" if expected_refs <= refs and not incomplete else "INSUFFICIENT_EVIDENCE",
        "scope": "Actual sanitized local callback journal, NOT remote readback",
        "spans": len(runs),
        "completed_graph_runs": len(expected_refs & refs),
        "expected_graph_runs": len(expected_refs),
        "missing_refs": sorted(expected_refs - refs),
        "incomplete_spans": incomplete,
    }
