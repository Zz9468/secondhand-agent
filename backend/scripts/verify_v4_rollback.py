"""Verify mode rollback using a completed synthetic task, with no new model calls."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from sqlalchemy import func, select

from app.db.models import AgentCallSnapshot, AgentMessage, Message, ModelExecutionTask
from app.db.session import get_session_factory
from scripts.evaluate_live_workflow import ApiClient


def fingerprint(task):
    return hashlib.sha256(
        json.dumps(task.input_snapshot, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def snapshot(db, sid, task_id):
    task = db.get(ModelExecutionTask, task_id)
    assert task and task.session_id == sid and task.runtime_kind == "V4"
    assert task.status.value == "SUCCEEDED"
    return {
        "task_id": task.id,
        "runtime_kind": task.runtime_kind,
        "input_fingerprint": fingerprint(task),
        "public_messages": db.scalar(
            select(func.count()).select_from(Message).where(Message.session_id == sid)
        ),
        "stream_messages": db.scalar(
            select(func.count()).select_from(AgentMessage).where(AgentMessage.session_id == sid)
        ),
        "model_calls": db.scalar(
            select(func.count())
            .select_from(AgentCallSnapshot)
            .where(AgentCallSnapshot.model_task_id == task.id)
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evaluation/results/v4-live-final-01"))
    parser.add_argument("--expected-runtime", choices=["V3", "V4"], required=True)
    args = parser.parse_args()
    setup = json.loads((args.output / "setup.json").read_text(encoding="utf-8"))
    live = json.loads((args.output / "report.json").read_text(encoding="utf-8"))
    # Read only these public configuration fields from the running container.
    command = [
        "docker",
        "compose",
        "exec",
        "-T",
        "api",
        "python",
        "-c",
        "import json; from app.core.config import get_settings; s=get_settings(); "
        "print(json.dumps({'runtime':s.agent_runtime,'session_ids':s.agent_v4_session_ids}))",
    ]
    config = json.loads(subprocess.check_output(command, cwd=Path.cwd().parent, text=True))
    assert config["runtime"] == args.expected_runtime, config
    sid, task_id = setup["session_id"], live["native_task_id"]
    with get_session_factory()() as db:
        before = snapshot(db, sid, task_id)
    buyer = ApiClient(setup["base_url"])
    credentials = json.loads((args.output / "buyer-credentials.json").read_text(encoding="utf-8"))
    buyer.request("POST", "/api/auth/login", credentials)
    _, reply = buyer.request(
        "POST",
        f"/api/negotiations/{sid}/messages",
        {"request_id": "v4-live-inquiry", "content": "我可以自提，请介绍一下商品成色。"},
        expected_status=201,
    )
    assert reply["idempotent_replay"]
    with get_session_factory()() as db:
        after = snapshot(db, sid, task_id)
    assert before == after, (before, after)
    report = {
        "status": "PASS",
        "config": config,
        "checks": [
            "completed_v4_task_keeps_frozen_runtime",
            "http_replay_adds_no_model_call_or_message",
        ],
        "snapshot": after,
        "additional_model_cost": "0",
        "scope": "completed replay; interrupted and leased recovery covered by MySQL tests",
    }
    target = args.output / f"rollback-{args.expected_runtime.lower()}.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
