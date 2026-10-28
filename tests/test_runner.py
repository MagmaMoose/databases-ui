"""The Job's own work, with stand-ins for pg_dump, pg_restore and psql on PATH."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import AUTHORITY, STORAGE, TENANT, FakeKube, World

from databases_ui import runner

APPS = "/apis/apps/v1/namespaces/dev-payments/deployments"
SNAPSHOT = "snapshots/payments_dev/20261023T140000Z-abcdef.dump"


@pytest.fixture
def tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """pg_dump writes DUMP_BYTES bytes (or fails with DUMP_FAIL), pg_restore keeps what it read and
    its arguments, psql answers 2 and keeps its SQL. Run as a script's psql (its last argument
    COMMIT or ROLLBACK), psql keeps its arguments and the script, prints a tag, a row and its last
    command, or fails at line 2 with SCRIPT_FAIL."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "pg_dump": '#!/bin/sh\nprintf PGDMP\nhead -c "${DUMP_BYTES:-100}" /dev/zero | tr "\\000" x\nif [ -n "$DUMP_FAIL" ]; then echo "pg_dump: error: $DUMP_FAIL" >&2; exit 1; fi\n',
        # Its script is the archive itself, which is all psql needs to show here.
        "pg_restore": '#!/bin/sh\necho "$@" > "$OUT/restore-args"\nif [ -n "$RESTORE_FAIL" ]; then head -c 3; echo "pg_restore: error: $RESTORE_FAIL" >&2; exit 1; fi\ncat\n',
        "psql": (
            '#!/bin/sh\nfor last; do :; done\n'
            'if [ "$last" = "COMMIT" ] || [ "$last" = "ROLLBACK" ]; then echo "$@" > "$OUT/script-args"; cat > "$OUT/script-text"; '
            'printf "BEGIN\\nSET\\nINSERT 0 1\\n customer \\n----------\\n Ada\\n(1 row)\\n\\n"; '
            'if [ -n "$SCRIPT_FAIL" ]; then echo "psql:<stdin>:2: ERROR:  $SCRIPT_FAIL" >&2; exit 3; fi; echo "$last"; exit 0; fi\n'
            'if [ "$last" = "--file=-" ]; then echo "$@" > "$OUT/psql-args"; cat > "$OUT/script"; '
            'if [ -n "$SCRIPT_FAIL" ]; then echo "psql:<stdin>:9: ERROR: $SCRIPT_FAIL" >&2; exit 3; fi; exit 0; fi\n'
            'echo "$last" >> "$OUT/psql"\nif [ -n "$PSQL_FAIL" ]; then echo "psql: error: $PSQL_FAIL" >&2; exit 2; fi\necho 2\n'
        ),
    }
    for name, body in scripts.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OUT", str(out))
    monkeypatch.setattr(runner, "TERMINATION_LOG", tmp_path / "termination-log")
    monkeypatch.setattr(runner, "WAIT_SECONDS", 2)
    monkeypatch.setattr(runner, "REACH_SECONDS", 1)
    return out


def job_env(tmp_path: Path, action: str, **extra: str) -> dict[str, str]:
    return {
        "DBUI_ACTION": action,
        "DBUI_JOB": f"dbui-{action}-12345678",
        "DBUI_ENVIRONMENT": "dev",
        "DBUI_DATABASE": "payments_dev",
        "DBUI_NAMESPACE": "dev-payments",
        "DBUI_REQUESTED_BY": "alice@example.com",
        "DBUI_BLOB_URL": f"{STORAGE}/dev",
        "DBUI_BLOB": SNAPSHOT,
        "DBUI_AUDIT_URL": f"{STORAGE}/dev",
        "DBUI_STORAGE_SAS": "sv=2022-11-02&sig=test",
        "PGDATABASE": "payments_dev",
        **extra,
    }


def run(world: World, env: dict[str, str], kube: FakeKube | None = None) -> tuple[int, dict[str, Any]]:
    code = asyncio.run(runner.run(env, transport=httpx.MockTransport(world.handler), kube_transport=httpx.MockTransport((kube or world.kube).handle)))
    return code, json.loads(runner.TERMINATION_LOG.read_text())


def test_a_snapshot_streams_pg_dump_into_blocks_and_commits_once_it_succeeded(world, tools, tmp_path, monkeypatch):
    monkeypatch.setenv("DUMP_BYTES", str(20 * 1024 * 1024))
    env = job_env(tmp_path, "snapshot", DBUI_METADATA=json.dumps({"createdby": "alice@example.com", "note": "naïve note"}))
    code, outcome = run(world, env)
    assert code == 0 and outcome["result"] == "succeeded" and outcome["bytes"] == 5 + 20 * 1024 * 1024
    blob = world.storage.blobs[f"dev/{SNAPSHOT}"]
    assert blob.data.startswith(b"PGDMP") and len(blob.data) == outcome["bytes"]
    assert blob.metadata["sha256"] == hashlib.sha256(blob.data).hexdigest() == outcome["sha256"]
    assert blob.metadata["note"] == "na%C3%AFve%20note" and blob.metadata["size"] == str(outcome["bytes"])
    # Three blocks of at most 8 MiB, then the block list.
    assert [c for c in world.storage.calls if c[1].startswith("dev/snapshots")] == [("PUT", f"dev/{SNAPSHOT}?block")] * 3 + [("PUT", f"dev/{SNAPSHOT}?blocklist")]
    assert [(e["action"], e["actor"], e["job"]) for e in world.storage.audit("dev")] == [("snapshot.succeeded", "alice@example.com", "dbui-snapshot-12345678")]


def test_a_failed_pg_dump_commits_nothing(world, tools, tmp_path, monkeypatch):
    monkeypatch.setenv("DUMP_FAIL", "permission denied for table secrets")
    code, outcome = run(world, job_env(tmp_path, "snapshot"))
    assert code == 1 and outcome["result"] == "failed" and "permission denied for table secrets" in outcome["reason"]
    assert f"dev/{SNAPSHOT}" not in world.storage.blobs
    assert world.storage.audit("dev")[0]["action"] == "snapshot.failed" and "permission denied" in world.storage.audit("dev")[0]["detail"]


def test_storage_through_workload_identity(world, tools, tmp_path):
    token = tmp_path / "azure-token"
    token.write_text("projected-token")
    env = job_env(tmp_path, "snapshot", AZURE_TENANT_ID=TENANT, AZURE_CLIENT_ID="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", AZURE_AUTHORITY_HOST=AUTHORITY, AZURE_FEDERATED_TOKEN_FILE=str(token))
    del env["DBUI_STORAGE_SAS"]
    code, _ = run(world, env)
    assert code == 0 and f"dev/{SNAPSHOT}" in world.storage.blobs
    sent = world.entra.token_requests[0]
    assert sent["client_assertion"] == ["projected-token"] and sent["scope"] == ["https://storage.azure.com/.default"]


def test_restore_in_place_scales_the_workloads_down_and_back(world, tools, tmp_path):
    world.snapshot("dev", "payments_dev", "20261023T140000Z-abcdef", data=b"PGDMP" + b"y" * 3_000_000)
    world.kube.put(APPS, {"metadata": {"name": "api"}, "spec": {"replicas": 3, "selector": {"matchLabels": {"app": "api"}}}})
    env = job_env(tmp_path, "restore", DBUI_SCALE=json.dumps([["deployment", "dev-payments", "api"], ["statefulset", "dev-payments", "gone"]]))
    code, outcome = run(world, env)
    assert code == 0 and outcome == {"result": "succeeded", "bytes": 3_000_005, "seconds": outcome["seconds"]}
    # One transaction: what the owner owns goes, the snapshot's script runs, then COMMIT.
    assert (tools / "script").read_bytes() == b"BEGIN;\nDROP OWNED BY CURRENT_USER;\nPGDMP" + b"y" * 3_000_000 + b"\nCOMMIT;\n"
    assert (tools / "restore-args").read_text().split() == ["--format=custom", "--clean", "--if-exists", "--no-owner", "--no-acl", "--file=-"]
    assert (tools / "psql-args").read_text().split() == ["--no-password", "--no-psqlrc", "--quiet", "-v", "ON_ERROR_STOP=1", "--dbname=payments_dev", "--file=-"]
    assert "pg_terminate_backend" in (tools / "psql").read_text()
    patches = [c for c in world.kube.calls if c[0] == "PATCH"]
    assert patches == [("PATCH", f"{APPS}/api"), ("PATCH", f"{APPS}/api")]
    api = world.kube.get(APPS, "api")
    assert api["spec"]["replicas"] == 3 and "databases-ui/replicas-before-restore" not in (api["metadata"].get("annotations") or {})
    assert world.storage.audit("dev")[0]["action"] == "restore.succeeded" and world.storage.audit("dev")[0]["namespace"] == "dev-payments"


def test_a_failed_pg_restore_never_commits_and_still_scales_back(world, tools, tmp_path, monkeypatch):
    monkeypatch.setenv("RESTORE_FAIL", "unsupported version (1.16) in file header")
    world.snapshot("dev", "payments_dev", "20261023T140000Z-abcdef")
    # An earlier restore died before scaling back: the annotation has the real count.
    world.kube.put(APPS, {"metadata": {"name": "api", "annotations": {"databases-ui/replicas-before-restore": "4"}}, "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "api"}}}})
    code, outcome = run(world, job_env(tmp_path, "restore", DBUI_SCALE=json.dumps([["deployment", "dev-payments", "api"]])))
    assert code == 1 and "pg_restore exited 1, nothing changed" in outcome["reason"] and "unsupported version" in outcome["reason"]
    assert b"COMMIT" not in (tools / "script").read_bytes()
    assert world.kube.get(APPS, "api")["spec"]["replicas"] == 4


def test_a_failed_statement_rolls_the_restore_back(world, tools, tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_FAIL", 'type "citext" does not exist')
    world.snapshot("dev", "payments_dev", "20261023T140000Z-abcdef")
    code, outcome = run(world, job_env(tmp_path, "restore"))
    assert code == 1 and outcome["reason"].startswith("the restore failed, nothing changed") and "citext" in outcome["reason"]


def test_a_download_that_breaks_off_commits_nothing(world, tools, tmp_path):
    world.snapshot("dev", "payments_dev", "20261023T140000Z-abcdef", data=b"PGDMP" + b"z" * 100_000)
    world.storage.break_after = 50_000
    code, outcome = run(world, job_env(tmp_path, "restore"))
    assert code == 1 and "broke off" in outcome["reason"]
    # psql was stopped, maybe before it read a byte: either way it never saw COMMIT.
    assert not (tools / "script").exists() or b"COMMIT" not in (tools / "script").read_bytes()


def test_restore_waits_for_pods_to_go_and_gives_up(world, tools, tmp_path):
    world.snapshot("dev", "payments_dev", "20261023T140000Z-abcdef")
    world.kube.put(APPS, {"metadata": {"name": "api"}, "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "api"}}}})
    world.kube.put("/api/v1/namespaces/dev-payments/pods", {"metadata": {"name": "api-1", "labels": {"app": "api"}}})
    code, outcome = run(world, job_env(tmp_path, "restore", DBUI_SCALE=json.dumps([["deployment", "dev-payments", "api"]])))
    assert code == 1 and "still running" in outcome["reason"]
    assert not (tools / "restored").exists() and world.kube.get(APPS, "api")["spec"]["replicas"] == 2


def test_restore_into_a_copy_waits_for_the_database(world, tools, tmp_path, monkeypatch):
    world.snapshot("dev", "payments_dev", "20261023T140000Z-abcdef")
    code, _ = run(world, job_env(tmp_path, "restore", DBUI_WAIT_FOR_DATABASE="1", PGDATABASE="payments_dev_copy"))
    assert code == 0 and (tools / "psql").read_text().splitlines()[0] == "select 1"
    assert "--dbname=payments_dev_copy" in (tools / "psql-args").read_text().split()
    monkeypatch.setenv("PSQL_FAIL", "database does not exist")
    code, outcome = run(world, job_env(tmp_path, "restore", DBUI_WAIT_FOR_DATABASE="1", PGDATABASE="payments_dev_copy"))
    assert code == 1 and "never became reachable" in outcome["reason"]


def test_a_missing_snapshot_or_action_fails_cleanly(world, tools, tmp_path):
    code, outcome = run(world, job_env(tmp_path, "restore"))
    assert code == 1 and "404" in outcome["reason"]
    code, outcome = run(world, job_env(tmp_path, "vacuum"))
    assert code == 1 and outcome["reason"] == "unknown action 'vacuum'"


SCRIPT = "insert into menu (code) values ('A');\ninsert into menu (code) values ('B');\n"


def script_env(tmp_path: Path, mode: str = "check", **extra: str) -> dict[str, str]:
    return job_env(
        tmp_path, "script", DBUI_SCRIPT=SCRIPT, DBUI_SCRIPT_MODE=mode, DBUI_SCRIPT_OUTPUT="full", DBUI_SCRIPT_BLOB="scripts/payments_dev/20261023T140000Z-abcdef.sql",
        DBUI_BLOB="", **extra,
    )  # fmt: skip


def test_a_check_runs_the_script_between_begin_and_rollback(world, tools, tmp_path):
    code, outcome = run(world, script_env(tmp_path))
    assert code == 0 and outcome["result"] == "succeeded" and outcome["mode"] == "check", outcome
    assert (tools / "script-args").read_text().strip() == "--no-password --no-psqlrc -v ON_ERROR_STOP=1 -c BEGIN -c SET LOCAL lock_timeout = '15s' --file=- -c ROLLBACK"
    assert (tools / "script-text").read_text() == SCRIPT
    assert outcome["output"].splitlines() == ["BEGIN", "SET", "INSERT 0 1", " customer ", "----------", " Ada", "(1 row)", "", "ROLLBACK"]
    assert not any(k.startswith("dev/snapshots/") for k in world.storage.blobs)
    entry = world.storage.audit("dev")[0]
    assert entry["action"] == "script.checked" and entry["script"] == "scripts/payments_dev/20261023T140000Z-abcdef.sql" and "snapshot" not in entry


def test_a_run_takes_its_snapshot_first_and_commits(world, tools, tmp_path):
    code, outcome = run(world, script_env(tmp_path, "run", DBUI_SNAPSHOT_FIRST="1", DBUI_BLOB=SNAPSHOT, DBUI_METADATA=json.dumps({"note": "before script"})))
    assert code == 0 and outcome["bytes"] == 105 and outcome["output"].endswith("COMMIT"), outcome
    assert world.storage.blobs[f"dev/{SNAPSHOT}"].data.startswith(b"PGDMP")
    assert (tools / "script-args").read_text().split()[-2:] == ["-c", "COMMIT"]
    assert [e["action"] for e in world.storage.audit("dev")] == ["script.succeeded"]


def test_a_failed_snapshot_runs_no_script(world, tools, tmp_path, monkeypatch):
    monkeypatch.setenv("DUMP_FAIL", "permission denied for schema private")
    code, outcome = run(world, script_env(tmp_path, "run", DBUI_SNAPSHOT_FIRST="1", DBUI_BLOB=SNAPSHOT))
    assert code == 1 and "permission denied" in outcome["reason"] and not (tools / "script-args").exists()


def test_production_output_keeps_tags_and_messages_never_rows(world, tools, tmp_path, monkeypatch):
    code, outcome = run(world, script_env(tmp_path, "run", DBUI_SCRIPT_OUTPUT="tags"))
    assert code == 0 and outcome["output"].splitlines() == ["BEGIN", "SET", "INSERT 0 1", "COMMIT"], outcome
    monkeypatch.setenv("SCRIPT_FAIL", 'duplicate key value violates unique constraint "menu_pkey"')
    code, outcome = run(world, script_env(tmp_path, "run", DBUI_SCRIPT_OUTPUT="tags"))
    assert code == 1 and "Ada" not in outcome["output"] and outcome["output"].splitlines()[-1].startswith("psql:<stdin>:2: ERROR:")


def test_a_failed_script_changes_nothing_and_says_where(world, tools, tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIPT_FAIL", 'relation "menu" does not exist')
    code, outcome = run(world, script_env(tmp_path, "run"))
    assert code == 1 and outcome["result"] == "failed"
    assert outcome["reason"] == 'nothing changed: psql:<stdin>:2: ERROR:  relation "menu" does not exist', outcome["reason"]
    assert "ROLLBACK" not in outcome["output"] and "COMMIT" not in outcome["output"]
    entry = world.storage.audit("dev")[0]
    assert entry["action"] == "script.failed" and "does not exist" in entry["detail"]


def test_a_script_that_should_have_been_refused_never_reaches_psql(world, tools, tmp_path):
    code, outcome = run(world, {**script_env(tmp_path, "run"), "DBUI_SCRIPT": "select 1;\ncommit;"})
    assert code == 1 and "Line 2: COMMIT" in outcome["reason"] and not (tools / "script-args").exists()


def test_the_termination_message_stays_valid_json_under_kubernetes_cap():
    outcome = {"result": "failed", "reason": "x" * 600, "output": "\n".join(f'"row {n}"\t\\' for n in range(2000)), "seconds": 3}
    message = runner.termination_message(dict(outcome))
    assert len(message.encode()) <= 4000 and json.loads(message)["output"].endswith('"row 1999"\t\\')
