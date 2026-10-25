import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from evaluation.p5.trace_journal import JournalTraceClient, journal_summary, read_native_runs


def test_real_callback_journal_is_sanitized_and_retains_native_identity(tmp_path):
    path = tmp_path / "traces.jsonl"
    client = JournalTraceClient(path, api_url="http://localhost:1", api_key="SECRET")
    rid, child, ref = uuid4(), uuid4(), "a" * 64
    now = datetime.now(UTC)
    client.create_run(
        "LangGraph",
        {"secret": "SECRET"},
        "chain",
        id=rid,
        trace_id=rid,
        start_time=now,
        extra={"metadata": {"run_ref": ref, "private": "SECRET"}},
    )
    client.create_run(
        "parse_intents", {}, "chain", id=child, trace_id=rid, parent_run_id=rid, start_time=now
    )
    assert journal_summary(path, {ref})["status"] == "INSUFFICIENT_EVIDENCE"
    client.update_run(rid, end_time=now)
    assert journal_summary(path, {ref})["status"] == "INSUFFICIENT_EVIDENCE"
    client.update_run(child, end_time=now, outputs={"secret": "SECRET"})
    rows = read_native_runs(path)
    assert "SECRET" not in path.read_text()
    assert rows[1]["parent_run_id"] == str(rid)
    assert journal_summary(path, {ref})["status"] == "PASS"
    assert journal_summary(path, {ref})["incomplete_spans"] == 0


def test_missing_native_create_cannot_be_reconstructed(tmp_path):
    path = tmp_path / "traces.jsonl"
    path.write_text(json.dumps({"operation": "update", "run": {"id": str(uuid4())}}))
    with pytest.raises(ValueError, match="no actual create"):
        read_native_runs(path)


def test_replay_rejects_modified_or_unfrozen_native_source(tmp_path):
    from hashlib import sha256

    from app.runtime.contracts import digest
    from evaluation.p5.replay_remote import validate_source

    journal = tmp_path / "native-traces.jsonl"
    client = JournalTraceClient(journal, api_url="http://localhost:1", api_key="SECRET")
    rid = uuid4()
    now = datetime.now(UTC)
    client.create_run(
        "LangGraph",
        {},
        "chain",
        id=rid,
        start_time=now,
        end_time=now,
        extra={"metadata": {"run_ref": digest("input")}},
    )
    report = {
        "finished_at": now.isoformat(),
        "frozen_runtime": {"status": "PASS"},
        "local_native_trace": {"sha256": sha256(journal.read_bytes()).hexdigest()},
        "results": [{"runs": [{"run_id": "input"}]}],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    assert len(validate_source(path)[1]) == 1
    report["frozen_runtime"]["status"] = "FAIL"
    path.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="frozen"):
        validate_source(path)
    report["frozen_runtime"]["status"] = "PASS"
    path.write_text(json.dumps(report))
    journal.write_text(journal.read_text() + "\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_source(path)
