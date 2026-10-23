from __future__ import annotations

import json
from typing import Any

from conftest import (
    CNPG,
    IDENTITY,
    JOBS,
    PAYMENTS,
    PODS,
    STORAGE,
    UNRELATED,
    World,
    make_client,
    sign_in,
    write_headers,
)


def env_of(job: dict[str, Any]) -> dict[str, Any]:
    container = job["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e.get("value", e.get("valueFrom")) for e in container["env"]}


def only_job(world: World) -> dict[str, Any]:
    jobs = list(world.kube.objects.get(JOBS, {}).values())
    assert len(jobs) == 1, [j["metadata"]["name"] for j in jobs]
    return jobs[0]


def finish(world: World, name: str, *, ok: bool = True, message: dict[str, Any] | None = None) -> None:
    job = world.kube.get(JOBS, name)
    job["status"] = {"conditions": [{"type": "Complete" if ok else "Failed", "status": "True", "lastTransitionTime": "2026-10-23T14:05:00Z"}], "startTime": "2026-10-23T14:00:01Z", "completionTime": "2026-10-23T14:05:00Z" if ok else None}
    world.kube.put(PODS, {"metadata": {"name": f"{name}-x", "labels": {**job["spec"]["template"]["metadata"]["labels"], "job-name": name}},
                          "status": {"containerStatuses": [{"state": {"terminated": {"message": json.dumps(message or {"result": "succeeded", "bytes": 1234, "seconds": 9})}}}]}})  # fmt: skip


def test_me_lists_teams_and_environments(client, world):
    me = sign_in(client, world, groups=[PAYMENTS, UNRELATED])
    assert me["upn"] == "alice@example.com" and me["teams"] == [{"id": "payments", "name": "Payments"}]
    assert [(e["name"], e["production"], e["runs_jobs"], e["downloads"]) for e in me["environments"]] == [("dev", False, True, True), ("acc", False, False, True), ("prd", True, True, False)]


def test_databases_follow_the_teams(client, world):
    sign_in(client, world)
    found = client.get("/api/environments/dev/databases").json()["databases"]
    assert [d["name"] for d in found] == ["payments_dev"]
    assert found[0] | {"teams": found[0]["teams"]} == {
        "name": "payments_dev", "owner": "payments_dev", "ready": True, "message": "", "teams": ["payments"],
        "workloads": ["deployment/dev-payments/api"], "copy": None, "can_act": True,
    }  # fmt: skip
    # prd's Cluster is called prod; acc runs nothing here, so the catalog says what it holds.
    assert [d["name"] for d in client.get("/api/environments/prd/databases").json()["databases"]] == ["payments_prd"]
    assert client.get("/api/environments/acc/databases").json()["databases"] == [
        {"name": "payments_acc", "owner": "", "ready": None, "message": "", "teams": ["payments"], "workloads": ["deployment/acc-payments/api"], "copy": None, "can_act": False}
    ]
    sign_in(client, world, upn="nobody@example.com", groups=[UNRELATED])
    assert client.get("/api/environments/dev/databases").json()["databases"] == []
    sign_in(client, world, upn="audrey@example.com", groups=[], roles=["Auditor"])
    found = client.get("/api/environments/dev/databases").json()["databases"]
    assert [(d["name"], d["can_act"]) for d in found] == [("keycloak", False), ("payments_dev", False), ("platform_only", False)]
    assert client.get("/api/environments/nowhere/databases").status_code == 404


def test_a_snapshot_is_a_job_that_reads_the_owners_password_itself(client, world):
    me = sign_in(client, world)
    response = client.post("/api/environments/dev/databases/payments_dev/snapshots", json={"note": " before the 2.4 migration "}, headers=write_headers(me))
    assert response.status_code == 201, response.text
    job = only_job(world)
    meta, spec = job["metadata"], job["spec"]
    assert meta["name"].startswith("dbui-snapshot-") and meta["namespace"] == "database"
    assert meta["labels"] == {"app.kubernetes.io/managed-by": "databases-ui", "databases-ui/environment": "dev", "databases-ui/database": "payments_dev", "databases-ui/action": "snapshot"}
    assert meta["annotations"]["databases-ui/note"] == "before the 2.4 migration" and meta["annotations"]["databases-ui/requested-by"] == "alice@example.com"
    assert spec["suspend"] is False and spec["backoffLimit"] == 0
    pod = spec["template"]["spec"]
    assert pod["serviceAccountName"] == "databases-ui-job" and pod["automountServiceAccountToken"] is False
    assert pod["containers"][0]["image"] == "ghcr.io/magmamoose/databases-ui:v0.1.0" and pod["containers"][0]["args"] == ["job"]
    env = env_of(job)
    assert env["PGHOST"] == "dev-rw.database.svc" and env["PGDATABASE"] == "payments_dev" and env["PGUSER"] == "payments_dev"
    assert env["PGPASSWORD"] == {"secretKeyRef": {"name": "payments_dev-db", "key": "password"}}
    assert env["PGSSLMODE"] == "verify-full"
    assert env["DBUI_BLOB_URL"] == f"{STORAGE}/dev" and env["DBUI_BLOB"].startswith("snapshots/payments_dev/20261023T140000Z-")
    assert json.loads(env["DBUI_METADATA"])["createdby"] == "alice@example.com"
    # Workload identity: the Job exchanges its own projected token, nothing stored.
    assert env["AZURE_CLIENT_ID"] == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" and env["AZURE_FEDERATED_TOKEN_FILE"].endswith("/azure-identity-token")
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["ca"]["secret"] == {"secretName": "dev-ca", "defaultMode": 0o440, "items": [{"key": "ca.crt", "path": "ca.crt"}]}
    assert volumes["azure-identity-token"]["projected"]["sources"][0]["serviceAccountToken"]["audience"] == "api://AzureADTokenExchange"
    assert [(e["action"], e["actor"], e["database"]) for e in world.storage.audit("dev")] == [("snapshot.requested", "alice@example.com", "payments_dev")]
    body = response.json()
    assert body["state"] == "starting" and body["note"] == "before the 2.4 migration"


def test_a_database_with_a_job_in_flight_is_busy(client, world):
    me = sign_in(client, world)
    assert client.post("/api/environments/dev/databases/payments_dev/snapshots", json={}, headers=write_headers(me)).status_code == 201
    busy = client.post("/api/environments/dev/databases/payments_dev/snapshots", json={}, headers=write_headers(me))
    assert busy.status_code == 409 and busy.json()["error"] == "busy"
    finish(world, only_job(world)["metadata"]["name"])
    assert client.post("/api/environments/dev/databases/payments_dev/snapshots", json={}, headers=write_headers(me)).status_code == 201


def test_who_may_act(client, world):
    me = sign_in(client, world, upn="carol@example.com", groups=[IDENTITY])
    assert client.post("/api/environments/dev/databases/payments_dev/snapshots", json={}, headers=write_headers(me)).status_code == 403
    assert client.post("/api/environments/dev/databases/keycloak/snapshots", json={}, headers=write_headers(me)).status_code == 201
    me = sign_in(client, world, upn="audrey@example.com", groups=[], roles=["Auditor"])
    assert client.post("/api/environments/dev/databases/payments_dev/snapshots", json={}, headers=write_headers(me)).status_code == 403
    me = sign_in(client, world, upn="root@example.com", groups=[], roles=["Admin"])
    response = client.post("/api/environments/dev/databases/platform_only/snapshots", json={}, headers=write_headers(me))
    assert response.status_code == 409 and response.json()["error"] == "no_credential"
    assert client.post("/api/environments/dev/databases/gone/snapshots", json={}, headers=write_headers(me)).status_code == 404
    assert client.post("/api/environments/acc/databases/payments_acc/snapshots", json={}, headers=write_headers(me)).json()["error"] == "no_jobs_here"


def test_writes_need_the_csrf_token_and_this_origin(client, world):
    me = sign_in(client, world)
    path = "/api/environments/dev/databases/payments_dev/snapshots"
    assert client.post(path, json={}).status_code == 403
    assert client.post(path, json={}, headers={"X-CSRF-Token": me["csrf"], "Origin": "https://evil.example.test"}).status_code == 403
    assert client.post(path, json={}, headers={"X-CSRF-Token": "forged", "Origin": "https://databases.example.test"}).status_code == 403
    assert world.kube.objects.get(JOBS, {}) == {}


def test_snapshots_list_newest_first_and_only_well_formed(client, world):
    world.snapshot("dev", "payments_dev", "20261020T080000Z-aaaaaa")
    world.snapshot("dev", "payments_dev", "20261022T080000Z-bbbbbb", note="na%C3%AFve")
    world.storage.blobs["dev/snapshots/payments_dev/stray.txt"] = world.storage.blobs["dev/snapshots/payments_dev/20261020T080000Z-aaaaaa.dump"]
    world.storage.page = 1  # every blob on its own page
    sign_in(client, world)
    found = client.get("/api/environments/dev/databases/payments_dev/snapshots").json()["snapshots"]
    assert [s["id"] for s in found] == ["20261022T080000Z-bbbbbb", "20261020T080000Z-aaaaaa"]
    assert found[0]["note"] == "naïve" and found[0]["size"] == len(b"PGDMP-test") and found[0]["created_by"] == "alice@example.com"
    assert client.get("/api/environments/dev/databases/keycloak/snapshots").status_code == 403


def test_restore_in_place_scales_the_catalogs_workloads(client, world):
    world.snapshot("dev", "payments_dev", "20261022T080000Z-bbbbbb")
    me = sign_in(client, world)
    body = {"source_environment": "dev", "source_database": "payments_dev", "snapshot": "20261022T080000Z-bbbbbb", "into": "database", "reason": "undo the bad batch"}
    response = client.post("/api/environments/dev/databases/payments_dev/restore", json=body, headers=write_headers(me))
    assert response.status_code == 201, response.text
    job = only_job(world)
    env = env_of(job)
    assert job["spec"]["suspend"] is False and job["spec"]["template"]["spec"]["automountServiceAccountToken"] is True
    assert json.loads(env["DBUI_SCALE"]) == [["deployment", "dev-payments", "api"]]
    assert env["DBUI_BLOB"] == "snapshots/payments_dev/20261022T080000Z-bbbbbb.dump" and env["PGDATABASE"] == "payments_dev"
    assert "DBUI_WAIT_FOR_DATABASE" not in env
    assert job["metadata"]["annotations"]["databases-ui/reason"] == "undo the bad batch"
    assert world.storage.audit("dev")[-1]["action"] == "restore.requested"


def test_restore_into_a_copy_makes_a_database_resource(client, world):
    world.snapshot("dev", "payments_dev", "20261022T080000Z-bbbbbb")
    me = sign_in(client, world)
    body = {"source_environment": "dev", "source_database": "payments_dev", "snapshot": "20261022T080000Z-bbbbbb", "into": "copy", "reason": "look at old rows"}
    assert client.post("/api/environments/dev/databases/payments_dev/restore", json=body, headers=write_headers(me)).status_code == 201
    copy = world.kube.get(f"{CNPG}/databases", "payments-dev-copy")
    assert copy["metadata"]["labels"] == {"app.kubernetes.io/managed-by": "databases-ui", "databases-ui/environment": "dev", "databases-ui/copy-of": "payments_dev"}
    assert copy["spec"] == {"name": "payments_dev_copy", "owner": "payments_dev", "cluster": {"name": "dev"}, "ensure": "present", "databaseReclaimPolicy": "delete", "extensions": [{"name": "citext"}]}
    env = env_of(only_job(world))
    assert env["PGDATABASE"] == "payments_dev_copy" and env["DBUI_WAIT_FOR_DATABASE"] == "1" and "DBUI_SCALE" not in env
    finish(world, only_job(world)["metadata"]["name"])
    listed = client.get("/api/environments/dev/databases").json()["databases"]
    assert [d["name"] for d in listed] == ["payments_dev"] and listed[0]["copy"] == {"name": "payments_dev_copy", "ready": False}
    # Again: the same copy is reused.
    assert client.post("/api/environments/dev/databases/payments_dev/restore", json=body, headers=write_headers(me)).status_code == 201
    assert len([d for d in world.kube.objects[f"{CNPG}/databases"] if d.endswith("-copy")]) == 1
    # Dropping it waits for the restore, then deletes the resource, which drops the database.
    assert client.delete("/api/environments/dev/databases/payments_dev/copy", headers=write_headers(me)).status_code == 409
    for name in list(world.kube.objects[JOBS]):
        finish(world, name)
    assert client.delete("/api/environments/dev/databases/payments_dev/copy", headers=write_headers(me)).status_code == 204
    assert world.kube.get(f"{CNPG}/databases", "payments-dev-copy") is None
    assert client.delete("/api/environments/dev/databases/payments_dev/copy", headers=write_headers(me)).status_code == 404
    assert world.storage.audit("dev")[-1]["action"] == "copy.dropped"


def test_a_copy_name_this_app_did_not_make_is_left_alone(client, world):
    world.snapshot("dev", "payments_dev", "20261022T080000Z-bbbbbb")
    world.kube.put(f"{CNPG}/databases", {"metadata": {"name": "payments-dev-copy", "labels": {}}, "spec": {"name": "someone_elses", "owner": "x", "cluster": {"name": "dev"}}})
    me = sign_in(client, world)
    body = {"source_environment": "dev", "source_database": "payments_dev", "snapshot": "20261022T080000Z-bbbbbb", "into": "copy", "reason": "look at old rows"}
    response = client.post("/api/environments/dev/databases/payments_dev/restore", json=body, headers=write_headers(me))
    assert response.status_code == 409 and response.json()["error"] == "copy_taken"


def test_clone_only_from_listed_environments_and_existing_snapshots(client, world):
    world.snapshot("acc", "payments_acc", "20261021T080000Z-cccccc")
    world.snapshot("prd", "payments_prd", "20261021T080000Z-dddddd")
    me = sign_in(client, world)
    body = {"source_environment": "acc", "source_database": "payments_acc", "snapshot": "20261021T080000Z-cccccc", "into": "database", "reason": "reproduce the acc bug"}
    response = client.post("/api/environments/dev/databases/payments_dev/restore", json=body, headers=write_headers(me))
    assert response.status_code == 201, response.text
    env = env_of(only_job(world))
    # The snapshot is read from acc's container; the outcome is written to dev's audit log.
    assert env["DBUI_BLOB_URL"] == f"{STORAGE}/acc" and env["DBUI_AUDIT_URL"] == f"{STORAGE}/dev"
    assert response.json()["source_environment"] == "acc"
    finish(world, only_job(world)["metadata"]["name"])
    # prd does not take dev's snapshots.
    body = {"source_environment": "dev", "source_database": "payments_dev", "snapshot": "20261021T080000Z-cccccc", "into": "database", "reason": "nope nope"}
    assert client.post("/api/environments/prd/databases/payments_prd/restore", json=body, headers=write_headers(me)).json()["error"] == "no_clone"
    missing = {**body, "source_environment": "acc", "source_database": "payments_acc", "snapshot": "20261021T080000Z-ffffff"}
    assert client.post("/api/environments/dev/databases/payments_dev/restore", json=missing, headers=write_headers(me)).status_code == 404
    carol = sign_in(client, world, upn="carol@example.com", groups=[IDENTITY])
    theirs = {**body, "source_environment": "acc", "source_database": "payments_acc", "snapshot": "20261021T080000Z-cccccc"}
    assert client.post("/api/environments/dev/databases/keycloak/restore", json=theirs, headers=write_headers(carol)).status_code == 403
    assert client.post("/api/environments/dev/databases/keycloak/restore", json={**theirs, "into": "everywhere"}, headers=write_headers(carol)).status_code == 422


def test_a_production_restore_waits_for_a_second_person(world, env):
    world.snapshot("prd", "payments_prd", "20261021T080000Z-dddddd")
    alice, bob, carol = (make_client(world, env) for _ in range(3))
    a = sign_in(alice, world)
    b = sign_in(bob, world, upn="bob@example.com")
    c = sign_in(carol, world, upn="carol@example.com", groups=[IDENTITY])
    body = {"source_environment": "prd", "source_database": "payments_prd", "snapshot": "20261021T080000Z-dddddd", "into": "database", "reason": "after the bad deploy"}
    asked = alice.post("/api/environments/prd/databases/payments_prd/restore", json=body, headers=write_headers(a)).json()
    job = only_job(world)
    assert job["spec"]["suspend"] is True and asked["state"] == "waiting" and asked["approval"] == "required"
    name = job["metadata"]["name"]
    seen = {j["name"]: j for j in alice.get("/api/environments/prd/jobs").json()["jobs"]}
    assert seen[name]["can_approve"] is False and seen[name]["can_decline"] is True
    assert bob.get("/api/environments/prd/jobs").json()["jobs"][0]["can_approve"] is True
    assert carol.get("/api/environments/prd/jobs").json()["jobs"] == []
    assert alice.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(a)).json()["error"] == "own_request"
    assert carol.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(c)).status_code == 403
    approved = bob.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(b))
    assert approved.status_code == 200 and approved.json()["approved_by"] == "bob@example.com"
    assert world.kube.get(JOBS, name)["spec"]["suspend"] is False
    assert bob.post(f"/api/environments/prd/jobs/{name}/approve", headers=write_headers(b)).json()["error"] == "not_waiting"
    assert [e["action"] for e in world.storage.audit("prd")] == ["restore.requested", "restore.approved"]


def test_a_waiting_restore_can_be_declined_or_withdrawn(world, env):
    world.snapshot("prd", "payments_prd", "20261021T080000Z-dddddd")
    alice, bob = make_client(world, env), make_client(world, env)
    a, b = sign_in(alice, world), sign_in(bob, world, upn="bob@example.com")
    body = {"source_environment": "prd", "source_database": "payments_prd", "snapshot": "20261021T080000Z-dddddd", "into": "database", "reason": "after the bad deploy"}
    alice.post("/api/environments/prd/databases/payments_prd/restore", json=body, headers=write_headers(a))
    name = only_job(world)["metadata"]["name"]
    assert bob.post(f"/api/environments/prd/jobs/{name}/decline", json={"reason": "not during the release"}, headers=write_headers(b)).status_code == 204
    assert world.kube.objects[JOBS] == {}
    assert world.storage.audit("prd")[-1] | {"at": ""} == {"at": "", "environment": "prd", "actor": "bob@example.com", "action": "restore.declined", "database": "payments_prd", "job": name, "detail": "not during the release"}
    alice.post("/api/environments/prd/databases/payments_prd/restore", json=body, headers=write_headers(a))
    name = only_job(world)["metadata"]["name"]
    assert alice.post(f"/api/environments/prd/jobs/{name}/decline", json={}, headers=write_headers(a)).status_code == 204
    assert bob.post(f"/api/environments/prd/jobs/{name}/decline", json={}, headers=write_headers(b)).status_code == 404


def test_requests_expire_after_a_day(world, env):
    world.snapshot("prd", "payments_prd", "20261021T080000Z-dddddd")
    alice, bob = make_client(world, env), make_client(world, env)
    a, b = sign_in(alice, world), sign_in(bob, world, upn="bob@example.com")
    body = {"source_environment": "prd", "source_database": "payments_prd", "snapshot": "20261021T080000Z-dddddd", "into": "database", "reason": "after the bad deploy"}
    alice.post("/api/environments/prd/databases/payments_prd/restore", json=body, headers=write_headers(a))
    job = only_job(world)
    job["metadata"]["annotations"]["databases-ui/requested-at"] = "2026-10-22T13:00:00Z"
    assert bob.post(f"/api/environments/prd/jobs/{job['metadata']['name']}/approve", headers=write_headers(b)).json()["error"] == "expired"
    # Expired, it no longer blocks the database, and the next listing clears it away.
    assert alice.post("/api/environments/prd/databases/payments_prd/restore", json=body, headers=write_headers(a)).status_code == 201
    assert len(alice.get("/api/environments/prd/jobs").json()["jobs"]) == 1
    assert job["metadata"]["name"] not in world.kube.objects[JOBS]
    assert "restore.expired" in [e["action"] for e in world.storage.audit("prd")]


def test_jobs_show_what_their_pods_reported(client, world):
    me = sign_in(client, world)
    client.post("/api/environments/dev/databases/payments_dev/snapshots", json={"note": "n"}, headers=write_headers(me))
    name = only_job(world)["metadata"]["name"]
    world.kube.get(JOBS, name)["status"] = {"active": 1}
    assert client.get("/api/environments/dev/jobs").json()["jobs"][0]["state"] == "running"
    finish(world, name, ok=False, message={"result": "failed", "reason": "pg_dump exited 1: permission denied for table x"})
    job = client.get("/api/environments/dev/jobs").json()["jobs"][0]
    assert job["state"] == "failed" and job["result"]["reason"].endswith("permission denied for table x") and job["finished"] == "2026-10-23T14:05:00Z"
    sign_in(client, world, upn="carol@example.com", groups=[IDENTITY])
    assert client.get("/api/environments/dev/jobs").json()["jobs"] == []
    assert client.get("/api/environments/acc/jobs").json()["jobs"] == []


def test_downloads_stream_outside_production_only(client, world):
    world.snapshot("dev", "payments_dev", "20261022T080000Z-bbbbbb", data=b"PGDMP" + bytes(range(256)) * 9000)
    world.snapshot("prd", "payments_prd", "20261021T080000Z-dddddd")
    sign_in(client, world)
    response = client.get("/api/environments/dev/databases/payments_dev/snapshots/20261022T080000Z-bbbbbb/download")
    assert response.status_code == 200 and response.content == b"PGDMP" + bytes(range(256)) * 9000
    assert response.headers["content-disposition"] == 'attachment; filename="dev-payments_dev-20261022T080000Z-bbbbbb.dump"'
    assert world.storage.audit("dev")[-1]["action"] == "snapshot.downloaded"
    assert client.get("/api/environments/prd/databases/payments_prd/snapshots/20261021T080000Z-dddddd/download").json()["error"] == "no_downloads"
    assert client.get("/api/environments/dev/databases/payments_dev/snapshots/20261022T080000Z-ffffff/download").status_code == 404
    sign_in(client, world, upn="audrey@example.com", groups=[], roles=["Auditor"])
    assert client.get("/api/environments/dev/databases/payments_dev/snapshots/20261022T080000Z-bbbbbb/download").status_code == 403


def test_the_audit_log_shows_your_databases(client, world):
    me = sign_in(client, world)
    client.post("/api/environments/dev/databases/payments_dev/snapshots", json={"note": "one"}, headers=write_headers(me))
    finish(world, only_job(world)["metadata"]["name"])
    carol = sign_in(client, world, upn="carol@example.com", groups=[IDENTITY])
    client.post("/api/environments/dev/databases/keycloak/snapshots", json={}, headers=write_headers(carol))
    world.storage.blobs["dev/audit/2026-09.jsonl"] = type(world.storage.blobs["dev/audit/2026-10.jsonl"])(b'{"at":"2026-09-01T00:00:00Z","action":"old","database":"keycloak"}\nnot json\n', {}, "")
    assert [e["action"] for e in client.get("/api/environments/dev/audit").json()["entries"]] == ["snapshot.requested", "old"]
    sign_in(client, world)
    assert [(e["action"], e["database"]) for e in client.get("/api/environments/dev/audit").json()["entries"]] == [("snapshot.requested", "payments_dev")]
    sign_in(client, world, upn="audrey@example.com", groups=[], roles=["Auditor"])
    assert len(client.get("/api/environments/dev/audit").json()["entries"]) == 3


def test_storage_and_kubernetes_errors_answer_502(client, world):
    me = sign_in(client, world)
    world.storage.fail[("GET", "list")] = 503
    response = client.get("/api/environments/dev/databases/payments_dev/snapshots")
    assert response.status_code == 502 and response.json()["error"] == "storage_unavailable"
    world.kube.objects[f"{CNPG}/clusters"].clear()
    response = client.post("/api/environments/dev/databases/payments_dev/snapshots", json={}, headers=write_headers(me))
    assert response.status_code == 409 and response.json()["error"] == "no_cluster"
    world.kube.refuse = 403
    response = client.get("/api/environments/dev/jobs")
    assert response.status_code == 502 and response.json() == {"error": "kubernetes_unavailable", "message": "Kubernetes refused: jobs.batch is forbidden"}


def test_validation_errors_name_the_field(client, world):
    me = sign_in(client, world)
    response = client.post("/api/environments/dev/databases/payments_dev/restore", json={"into": "copy"}, headers=write_headers(me))
    assert response.status_code == 422 and {f["field"] for f in response.json()["fields"]} >= {"source_environment", "snapshot", "reason"}
    assert client.get("/api/environments/dev/databases/Not-A-Name!/snapshots").status_code == 422
