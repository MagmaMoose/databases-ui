"""External databases beside the CloudNativePG ones, and scripts on either: who may, what the Job
holds, and the second person in production."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from conftest import CATALOG, JOBS, World, make_client, sign_in, write_headers
from test_api import env_of, finish, only_job

EXTERNAL = """
    external:
      - name: ledger
        namespace: dev-payments
        host: ledger-db.example.internal
        database: Ledger
        user: ledger_owner
        passwordSecret: ledger-db
        caSecret: ledger-db-ca
      - name: archive
        namespace: dev-identity
        host: 10.20.30.40
        port: 6432
        database: archive
        user: archive_owner
        passwordSecret: archive-db
        tls: require
"""
CHECK = {"script": "insert into menu (code, label) values ('A', 'Results');", "mode": "check", "reason": "add the missing menu items"}


@pytest.fixture
def ext_env(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    catalog = tmp_path / "external.yaml"
    # dev is the first environment: its block ends where acc's begins.
    catalog.write_text(CATALOG.replace("    cloneFrom: [acc]\n", "    cloneFrom: [acc]\n" + EXTERNAL.lstrip("\n"), 1))
    return {**env, "DATABASES_UI_CATALOG": str(catalog)}


@pytest.fixture
def ext(world: World, ext_env: dict[str, str]):
    return make_client(world, ext_env)


def post(client, me: dict[str, Any], path: str, body: dict[str, Any]):
    return client.post(path, json=body, headers=write_headers(me))


def test_external_databases_are_listed_beside_cloudnativepg_ones(ext, world):
    sign_in(ext, world)
    found = {d["name"]: d for d in ext.get("/api/environments/dev/databases").json()["databases"]}
    assert sorted(found) == ["ledger", "payments_dev"]
    assert found["ledger"] == {
        "name": "ledger", "kind": "external", "namespace": "dev-payments", "owner": "ledger_owner", "owner_secret": "ledger-db", "ready": True, "message": "",
        "server": {"host": "ledger-db.example.internal", "port": 5432, "database": "Ledger", "tls": "verify-full"},
        "workloads": [], "copy": None, "can_snapshot": True, "can_restore": False, "can_script": True,
    }  # fmt: skip
    assert found["payments_dev"]["kind"] == "cloudnativepg" and found["payments_dev"]["can_script"] is True
    users = {u["name"]: u for u in ext.get("/api/environments/dev/users").json()["users"]}
    assert users["ledger_owner"] == {"name": "ledger_owner", "managed": False, "login": True, "secret": "ledger-db", "databases": ["ledger"], "namespaces": ["dev-payments"], "server": "ledger-db.example.internal:5432/Ledger"}
    sign_in(ext, world, "vera@example.com")
    assert [(d["name"], d["can_snapshot"], d["can_script"]) for d in ext.get("/api/environments/dev/databases").json()["databases"]] == [("ledger", False, False), ("payments_dev", False, False)]
    sign_in(ext, world, "carol@example.com")
    assert [d["name"] for d in ext.get("/api/environments/dev/databases").json()["databases"]] == ["archive", "keycloak"]


def test_a_cluster_without_cloudnativepg_shows_its_external_databases(ext, world):
    world.kube.objects.pop("/apis/postgresql.cnpg.io/v1/namespaces/database/databases")
    world.kube.objects.pop("/apis/postgresql.cnpg.io/v1/namespaces/database/clusters")
    real = world.kube.handle

    def no_cnpg(request):
        if "/apis/postgresql.cnpg.io/" in request.url.path:
            return httpx.Response(404, json={"message": "the server could not find the requested resource"})
        return real(request)

    world.kube.handle = no_cnpg
    client = ext
    sign_in(client, world, "root@example.com")
    assert [d["name"] for d in client.get("/api/environments/dev/databases").json()["databases"]] == ["archive", "ledger"]


def test_a_snapshot_of_an_external_database_logs_in_as_the_catalog_says(ext, world):
    me = sign_in(ext, world)
    assert post(ext, me, "/api/environments/dev/databases/ledger/snapshots", {"note": "before"}).status_code == 201
    job = only_job(world)
    env = env_of(job)
    assert env["PGHOST"] == "ledger-db.example.internal" and env["PGPORT"] == "5432" and env["PGDATABASE"] == "Ledger" and env["PGUSER"] == "ledger_owner"
    assert env["PGPASSWORD"] == {"secretKeyRef": {"name": "ledger-db", "key": "password"}}
    assert env["PGSSLMODE"] == "verify-full" and env["PGSSLROOTCERT"] == "/etc/databases-ui/ca/ca.crt"
    volumes = {v["name"]: v for v in job["spec"]["template"]["spec"]["volumes"]}
    assert volumes["ca"]["secret"] == {"secretName": "ledger-db-ca", "defaultMode": 0o440, "items": [{"key": "ca.crt", "path": "ca.crt"}]}
    assert job["metadata"]["labels"]["databases-ui/database"] == "ledger" and job["metadata"]["labels"]["databases-ui/namespace"] == "dev-payments"


def test_tls_without_a_ca_secret(ext, world):
    me = sign_in(ext, world, "carol@example.com")
    assert post(ext, me, "/api/environments/dev/databases/archive/snapshots", {"note": ""}).status_code == 201
    job = only_job(world)
    env = env_of(job)
    assert env["PGHOST"] == "10.20.30.40" and env["PGPORT"] == "6432" and env["PGSSLMODE"] == "require" and "PGSSLROOTCERT" not in env
    assert [v["name"] for v in job["spec"]["template"]["spec"]["volumes"]] == ["tmp", "azure-identity-token"]


def test_an_external_database_is_not_restored(ext, world):
    world.snapshot("dev", "ledger", "20261022T080000Z-bbbbbb")
    me = sign_in(ext, world)
    body = {"source_environment": "dev", "source_database": "ledger", "snapshot": "20261022T080000Z-bbbbbb", "into": "database", "reason": "roll back"}
    refused = post(ext, me, "/api/environments/dev/databases/ledger/restore", body)
    assert refused.status_code == 409 and refused.json()["error"] == "external"
    assert ext.delete("/api/environments/dev/databases/ledger/copy", headers=write_headers(me)).status_code == 404
    assert world.kube.objects.get(JOBS, {}) == {}


def test_a_check_runs_at_once_rolls_back_and_keeps_a_copy_of_the_script(ext, world):
    me = sign_in(ext, world)
    asked = post(ext, me, "/api/environments/dev/databases/ledger/scripts", CHECK)
    assert asked.status_code == 201, asked.json()
    job = only_job(world)
    env = env_of(job)
    assert job["spec"]["suspend"] is False and job["metadata"]["labels"]["databases-ui/action"] == "script"
    assert env["DBUI_SCRIPT"] == CHECK["script"] and env["DBUI_SCRIPT_MODE"] == "check" and env["DBUI_SCRIPT_OUTPUT"] == "full"
    # A check changes nothing, so it takes no snapshot.
    assert "DBUI_SNAPSHOT_FIRST" not in env and env["DBUI_BLOB"] == ""
    sid = job["metadata"]["annotations"]["databases-ui/script"]
    stored = world.storage.blobs[f"dev/{env['DBUI_SCRIPT_BLOB']}"]
    assert env["DBUI_SCRIPT_BLOB"] == f"scripts/ledger/{sid}.sql" and stored.data.decode() == CHECK["script"]
    meta = {k: unquote(v) for k, v in stored.metadata.items()}
    assert meta["createdby"] == "alice@example.com" and meta["mode"] == "check" and meta["reason"] == CHECK["reason"]
    assert job["metadata"]["annotations"]["databases-ui/script-sha256"] == stored.metadata["sha256"]
    entry = world.storage.audit("dev")[-1]
    assert entry["action"] == "script.requested" and entry["script"] == f"scripts/ledger/{sid}.sql" and entry["detail"] == f"check: {CHECK['reason']}"
    assert post(ext, me, "/api/environments/dev/databases/ledger/scripts", CHECK).json()["error"] == "busy"
    finish(world, job["metadata"]["name"], message={"result": "succeeded", "mode": "check", "output": "BEGIN\nSET\nINSERT 0 1\nROLLBACK", "seconds": 1})
    listed = ext.get("/api/environments/dev/jobs").json()["jobs"][0]
    assert listed["action"] == "script" and listed["mode"] == "check" and listed["script_text"] == CHECK["script"] and listed["result"]["output"].endswith("ROLLBACK")


def test_a_run_can_take_a_snapshot_first(ext, world):
    me = sign_in(ext, world)
    assert post(ext, me, "/api/environments/dev/databases/payments_dev/scripts", {**CHECK, "mode": "run", "snapshot": True}).status_code == 201
    job = only_job(world)
    env = env_of(job)
    sid = job["metadata"]["annotations"]["databases-ui/script"]
    assert env["DBUI_SCRIPT_MODE"] == "run" and env["DBUI_SNAPSHOT_FIRST"] == "1" and env["DBUI_BLOB"] == f"snapshots/payments_dev/{sid}.dump"
    assert json.loads(env["DBUI_METADATA"])["note"] == f"before script {sid}: {CHECK['reason']}"
    # A CloudNativePG database: its owner, over verified TLS to the cluster.
    assert env["PGHOST"] == "dev-rw.database.svc" and env["PGUSER"] == "payments_dev" and env["PGSSLMODE"] == "verify-full"


@pytest.mark.parametrize(
    ("script", "said"),
    [
        ("begin;\ninsert into t values (1);\ncommit;", "Line 1: BEGIN."),
        ("select 1;\n\\! cat /etc/passwd", "Line 2: a backslash"),
        ("   ", "The script is empty."),
    ],
)
def test_scripts_that_would_escape_their_transaction_are_refused(ext, world, script, said):
    me = sign_in(ext, world)
    refused = post(ext, me, "/api/environments/dev/databases/ledger/scripts", {**CHECK, "script": script})
    assert refused.status_code == 422 and refused.json()["error"] == "script_refused" and said in refused.json()["message"], refused.json()
    assert world.kube.objects.get(JOBS, {}) == {}


def test_who_may_run_a_script(ext, world):
    vera = sign_in(ext, world, "vera@example.com")
    refused = post(ext, vera, "/api/environments/dev/databases/ledger/scripts", CHECK)
    assert refused.status_code == 403 and "get secrets" in refused.json()["message"]
    carol = sign_in(ext, world, "carol@example.com")
    # To whom the database does not exist.
    assert post(ext, carol, "/api/environments/dev/databases/ledger/scripts", CHECK).status_code == 404
    assert ext.post("/api/environments/dev/databases/archive/scripts", json=CHECK).status_code == 403


def test_a_production_script_waits_for_a_second_person_with_admin(world, ext_env):
    alice, bob, pat = (make_client(world, ext_env) for _ in range(3))
    a = sign_in(alice, world)
    b = sign_in(bob, world, "bob@example.com")
    p = sign_in(pat, world, "pat@example.com")
    refused = post(pat, p, "/api/environments/prd/databases/payments_prd/scripts", CHECK)
    assert refused.status_code == 403 and "create rolebindings.rbac.authorization.k8s.io" in refused.json()["message"]
    asked = post(alice, a, "/api/environments/prd/databases/payments_prd/scripts", {**CHECK, "mode": "run"}).json()
    job = only_job(world)
    env = env_of(job)
    assert job["spec"]["suspend"] is True and asked["state"] == "waiting" and asked["approval"] == "required"
    # Production output: tags and messages, never rows.
    assert env["DBUI_SCRIPT_OUTPUT"] == "tags" and env["DBUI_SNAPSHOT_FIRST"] == "1"
    name = job["metadata"]["name"]
    assert alice.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(a)).json()["error"] == "own_request"
    assert pat.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(p)).status_code == 403
    listed = next(j for j in bob.get("/api/environments/prd/jobs").json()["jobs"] if j["name"] == name)
    # The approver sees exactly what will run.
    assert listed["can_approve"] and listed["script_text"] == CHECK["script"] and listed["mode"] == "run"
    approved = bob.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(b)).json()
    assert approved["approved_by"] == "bob@example.com" and world.kube.get(JOBS, name)["spec"]["suspend"] is False
    assert [e["action"] for e in world.storage.audit("prd")] == ["script.requested", "script.approved"]


def test_a_production_script_can_be_declined_or_withdrawn(world, ext_env):
    alice, bob = make_client(world, ext_env), make_client(world, ext_env)
    a = sign_in(alice, world)
    b = sign_in(bob, world, "bob@example.com")
    post(alice, a, "/api/environments/prd/databases/payments_prd/scripts", CHECK)
    name = only_job(world)["metadata"]["name"]
    assert bob.post(f"/api/environments/prd/jobs/{name}/decline", json={"reason": "not on a Friday"}, headers=write_headers(b)).status_code == 204
    assert world.kube.objects[JOBS] == {}
    post(alice, a, "/api/environments/prd/databases/payments_prd/scripts", CHECK)
    name = only_job(world)["metadata"]["name"]
    assert alice.post(f"/api/environments/prd/jobs/{name}/decline", json={"reason": ""}, headers=write_headers(a)).status_code == 204
    assert [e["action"] for e in world.storage.audit("prd")] == ["script.requested", "script.declined", "script.requested", "script.declined"]
