import json
import socket

import pytest
from dotenv import dotenv_values
from sqlalchemy.engine import make_url

from scripts.backup_p6 import backup
from scripts.bootstrap_p6 import prepare
from scripts.predeploy_delivery import require_free_ports
from scripts.verify_p6_backup import verify


def test_bootstrap_imports_only_allowed_fields_and_preserves_existing_secrets(tmp_path):
    source, target = tmp_path / "source.env", tmp_path / "target.env"
    source.write_text(
        "MODEL_API_KEY=model-secret\nMYSQL_PASSWORD=old-project-password\n"
        "AUTH_SECRET=old-project-auth\nMODEL_NAME=test-model\n",
        encoding="utf-8",
    )
    target.write_text(
        'MYSQL_PASSWORD="special@/#password"\nAUTH_SECRET=keep-my-auth\n', encoding="utf-8"
    )
    prepare(target, source)
    first = dotenv_values(target)
    assert first["MODEL_API_KEY"] == "model-secret"
    assert first["AUTH_SECRET"] == "keep-my-auth"
    assert make_url(first["DATABASE_URL"]).password == "special@/#password"
    assert first["POSTGRES_PASSWORD"] != first["GRAPH_CLEANER_PASSWORD"]
    assert not prepare(target, source)
    assert dotenv_values(target) == first


def test_backup_refuses_running_writers(monkeypatch, tmp_path):
    monkeypatch.setattr("scripts.backup_p6.subprocess.check_output", lambda *a, **k: "api\nmysql\n")
    with pytest.raises(RuntimeError, match="Stop api"):
        backup(["docker", "compose"], tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_restore_refuses_tampered_dump_before_creating_databases(tmp_path):
    (tmp_path / "mysql.sql").write_text("altered", encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"sha256": {"mysql.sql": "bad"}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        verify(["never-run"], tmp_path)


def test_delivery_port_preflight_rejects_an_existing_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(OSError):
            require_free_ports([listener.getsockname()[1]])


def test_delivery_preflight_failure_preserves_existing_output(monkeypatch, tmp_path):
    from scripts import run_p6_delivery

    def occupied(ports):
        raise OSError("port occupied")

    marker = tmp_path / "previous-evidence.json"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(run_p6_delivery, "require_free_ports", occupied)
    with pytest.raises(OSError, match="occupied"):
        run_p6_delivery.run(tmp_path)
    assert marker.read_text(encoding="utf-8") == "keep"
