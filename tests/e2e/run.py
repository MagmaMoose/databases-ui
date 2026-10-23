"""End to end on a real cluster: CloudNativePG, Azurite, this chart and this image.

    k3d cluster create dbui --k3s-arg "--disable=traefik@server:0"
    helm install cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts \
      --version 0.29.1 --namespace cnpg-system --create-namespace --wait
    docker buildx bake app-local && k3d image import databases-ui:dev -c dbui
    uv run python tests/e2e/run.py

It installs fixtures.yaml and the chart, signs people in by sealing their session cookies with the
chart's own session key (Entra ID is the one thing not in the loop), and walks every action
through the API: snapshots, a restore in place with the app scaled down, a restore into a copy, a
clone across environments, a production restore and its second person, a decline, a download,
the audit log and who may do what. Each step checks the database itself, not just the answers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import subprocess  # nosec B404 - drives kubectl and helm for a test
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from databases_ui.session import Sealer

HERE = Path(__file__).resolve().parent
CHART = HERE.parents[1] / "charts" / "databases-ui"
ACCOUNT = "e2eaccount"
ORIGIN = "http://localhost:18080"  # DevSkim: ignore DS162092 - the port-forwarded test server
# People and their groups, as the API server would name them from an ID token.
PEOPLE = {
    "alice": ["k8s-dev-payments-write", "k8s-tst-payments-admin"],
    "bob": ["k8s-dev-payments-write", "k8s-tst-payments-admin"],
    "pat": ["k8s-tst-payments-write"],
    "carol": ["k8s-dev-other-write"],
    "audrey": ["cloud-read"],
}
passed: list[str] = []


def sh(*args: str, input: str | None = None, check: bool = True) -> str:
    done = subprocess.run(args, input=input, capture_output=True, text=True, check=False)  # nosec B603 - fixed test commands
    if check and done.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def kubectl(*args: str, input: str | None = None, check: bool = True) -> str:
    return sh("kubectl", *args, input=input, check=check)


def ok(what: str) -> None:
    passed.append(what)
    print(f"  ok  {what}", flush=True)


def psql(cluster: str, database: str, sql: str) -> str:
    return kubectl("exec", "-n", "database", f"{cluster}-1", "-c", "postgres", "--", "psql", "-v", "ON_ERROR_STOP=1", "-Atq", "-d", database, "-c", sql).strip()


def wait_for(what: str, test, timeout: float = 300, every: float = 3) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            found = test()
        except (RuntimeError, httpx.HTTPError):
            found = None
        if found:
            return found
        if time.monotonic() > deadline:
            raise SystemExit(f"timed out waiting for {what}")
        time.sleep(every)


def account_sas(key: str) -> str:
    """An account SAS for Azurite: blob service, every resource type, everything this app does."""
    fields = {"sv": "2022-11-02", "ss": "b", "srt": "sco", "sp": "rwdlac", "se": "2030-01-01T00:00:00Z", "spr": "https,http"}
    to_sign = "\n".join([ACCOUNT, fields["sp"], fields["ss"], fields["srt"], "", fields["se"], "", fields["spr"], fields["sv"], "", ""])
    sig = base64.b64encode(hmac.new(base64.b64decode(key), to_sign.encode(), hashlib.sha256).digest()).decode()
    return "&".join(f"{k}={quote(v, safe='')}" for k, v in {**fields, "sig": sig}.items())


@contextmanager
def port_forward(namespace: str, target: str, ports: str):
    proc = subprocess.Popen(["kubectl", "port-forward", "-n", namespace, target, ports], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)  # nosec B603 B607
    try:
        time.sleep(2)
        yield
    finally:
        proc.terminate()
        proc.wait()


def apply(*objects: dict[str, Any]) -> None:
    kubectl("apply", "-f", "-", input="\n---\n".join(json.dumps(o) for o in objects))


def setup() -> tuple[str, dict[str, str]]:
    print("fixtures", flush=True)
    apply(*({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": n}} for n in ("database", "storage", "dev-payments", "tst-payments", "databases-ui")))
    # Leftovers of an earlier run.
    kubectl("delete", "jobs", "-n", "database", "-l", "app.kubernetes.io/managed-by=databases-ui", "--ignore-not-found", check=False)
    kubectl("delete", "databases.postgresql.cnpg.io", "-n", "database", "-l", "databases-ui/copy-of", "--ignore-not-found", check=False)
    key = base64.b64encode(secrets.token_bytes(64)).decode()
    apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "azurite-account", "namespace": "storage"}, "stringData": {"AZURITE_ACCOUNTS": f"{ACCOUNT}:{key}"}})
    for env in ("dev", "tst"):
        # Kept across runs: CloudNativePG sets a role's password when it creates the role.
        kept = kubectl("get", "secret", f"payments-{env}-db", "-n", "database", "-o", "jsonpath={.data.password}", check=False)
        password = base64.b64decode(kept).decode() if kept else secrets.token_urlsafe(24)
        apply(
            {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/basic-auth", "metadata": {"name": f"payments-{env}-db", "namespace": "database"}, "stringData": {"username": f"payments_{env}", "password": password}},
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "payments-db", "namespace": f"{env}-payments"}, "stringData": {"password": password}},
        )
    kubectl("apply", "-f", str(HERE / "fixtures.yaml"))
    # A new key each run: Azurite starts empty with it.
    kubectl("rollout", "restart", "deployment/azurite", "-n", "storage")
    for env in ("dev", "tst"):
        # The app: two replicas, each holding a session open the whole time.
        kubectl("apply", "-f", "-", input=json.dumps({
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "payments-api", "namespace": f"{env}-payments"},
            "spec": {"replicas": 2, "selector": {"matchLabels": {"app": "payments-api"}}, "template": {
                "metadata": {"labels": {"app": "payments-api"}},
                "spec": {"terminationGracePeriodSeconds": 2, "securityContext": {"runAsNonRoot": True, "runAsUser": 10001}, "containers": [{
                    "name": "api", "image": "databases-ui:dev", "imagePullPolicy": "IfNotPresent", "command": ["sh", "-c", "while true; do psql -Atqc 'select pg_sleep(600)'; sleep 1; done"],
                    "env": [{"name": "PGHOST", "value": f"{env}-rw.database.svc"}, {"name": "PGUSER", "value": f"payments_{env}"}, {"name": "PGDATABASE", "value": f"payments_{env}"},
                            {"name": "PGSSLMODE", "value": "require"}, {"name": "HOME", "value": "/tmp"},
                            {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": "payments-db", "key": "password"}}}],
                }]},
            }},
        }))  # fmt: skip
        kubectl("rollout", "restart", "deployment/payments-api", "-n", f"{env}-payments")
    for env in ("dev", "tst"):
        kubectl("wait", "-n", "database", f"cluster/{env}", "--for=condition=Ready", "--timeout=600s")
        wait_for(f"database payments_{env}", lambda env=env: kubectl("get", "-n", "database", f"database/payments-{env}", "-o", "jsonpath={.status.applied}") == "true")
        wait_for(f"role payments_{env}", lambda env=env: psql(env, "postgres", f"select 1 from pg_roles where rolname = 'payments_{env}'") == "1")
        password = base64.b64decode(kubectl("get", "secret", f"payments-{env}-db", "-n", "database", "-o", "jsonpath={.data.password}")).decode()
        psql(env, "postgres", f"alter role payments_{env} password '{password}'")
        psql(env, f"payments_{env}", f"""
            set role payments_{env};
            drop table if exists orders, later;
            create table orders (id int primary key, customer citext not null, total numeric(10,2) not null);
            insert into orders values (1, '{env}-Ada', 12.50), (2, '{env}-Grace', 99.00), (3, '{env}-Katherine', 7.25);
        """)  # fmt: skip
    kubectl("rollout", "status", "deployment/azurite", "-n", "storage", "--timeout=180s")
    sas = account_sas(key)
    with port_forward("storage", "svc/azurite", "10010:10000"):
        for env in ("dev", "tst"):
            response = httpx.put(f"http://127.0.0.1:10010/{ACCOUNT}/{env}?restype=container&{sas}", headers={"x-ms-version": "2022-11-02"})  # DevSkim: ignore DS137138,DS162092 - Azurite, port-forwarded to this machine
            assert response.status_code in (201, 409), response.text
    for namespace in ("databases-ui", "database"):
        kubectl("apply", "-f", "-", input=json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "databases-ui-storage", "namespace": namespace}, "stringData": {"sas": sas}}))
    kubectl("apply", "-f", "-", input=json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "databases-ui-entra", "namespace": "databases-ui"}, "stringData": {"client-secret": "unused-here"}}))

    print("chart", flush=True)
    values = {
        "replicaCount": 1,
        "image": {"repository": "databases-ui", "tag": "dev", "pullPolicy": "IfNotPresent"},
        "azure": {"tenantId": "11111111-2222-3333-4444-555555555555", "clientId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "workloadIdentity": {"enabled": False}, "clientSecret": {"existingSecret": "databases-ui-entra"}},
        "config": {"publicOrigin": ORIGIN},
        "storage": {"sas": {"existingSecret": "databases-ui-storage"}},
        "rbac": {"namespaces": ["database"]},
        "jobs": {"scaleNamespaces": ["dev-payments", "tst-payments"]},
        "networkPolicy": {"databases": {"enabled": True}},
        "catalog": {"content": {
            "environments": [
                {"name": "dev", "storage": {"url": f"http://azurite.storage.svc:10000/{ACCOUNT}/dev"}, "cloneFrom": ["tst"]},  # DevSkim: ignore DS137138
                {"name": "tst", "production": True, "storage": {"url": f"http://azurite.storage.svc:10000/{ACCOUNT}/tst"}, "cloneFrom": ["dev"]},  # DevSkim: ignore DS137138
            ],
        }},
    }  # fmt: skip
    sh("helm", "upgrade", "--install", "databases-ui", str(CHART), "-n", "databases-ui", "--values", "-", "--wait", "--timeout", "180s", input=json.dumps(values))
    kubectl("rollout", "restart", "deployment/databases-ui", "-n", "databases-ui")
    kubectl("rollout", "status", "deployment/databases-ui", "-n", "databases-ui", "--timeout=180s")
    session_key = base64.b64decode(kubectl("get", "secret", "databases-ui-session", "-n", "databases-ui", "-o", "jsonpath={.data.session-key}"))
    sealer = Sealer(session_key)
    cookies = {}
    for person, groups in PEOPLE.items():
        data = {
            "oid": hashlib.sha256(person.encode()).hexdigest()[:32], "upn": f"{person}@example.com", "name": person.title(), "csrf": f"csrf-{person}",
            "user": f"oidc:{person}@example.com", "groups": [f"oidc:{g}" for g in groups],
        }  # fmt: skip
        cookies[person] = sealer.seal("session", data, 3600)
    return sas, cookies


class Client:
    def __init__(self, person: str, token: str):
        self.person = person
        self.http = httpx.Client(base_url=ORIGIN, cookies={"databases-ui.0": token}, timeout=60)
        self.headers = {"Origin": ORIGIN, "X-CSRF-Token": f"csrf-{person}"}

    def get(self, path: str) -> httpx.Response:
        return self.http.get(path)

    def post(self, path: str, body: dict[str, Any] | None = None, *, csrf: bool = True) -> httpx.Response:
        return self.http.post(path, json=body or {}, headers=self.headers if csrf else {})

    def delete(self, path: str) -> httpx.Response:
        return self.http.delete(path, headers=self.headers)


def finished(c: Client, env: str, job: str) -> dict[str, Any]:
    def done():
        found = next((j for j in c.get(f"/api/environments/{env}/jobs").json()["jobs"] if j["name"] == job), None)
        return found if found and found["state"] in ("succeeded", "failed") else None

    found = wait_for(f"job {job}", done, timeout=300)
    if found["state"] != "succeeded":
        raise SystemExit(f"{job} failed: {found['result']}")
    return found


def orders(env: str, database: str) -> list[str]:
    return psql(env, database, "select customer from orders order by id").splitlines()


def replicas(env: str) -> str:
    return kubectl("get", "deployment/payments-api", "-n", f"{env}-payments", "-o", "jsonpath={.spec.replicas}")


def scenarios(cookies: dict[str, str]) -> None:
    alice, bob, pat, carol, audrey = (Client(p, cookies[p]) for p in ("alice", "bob", "pat", "carol", "audrey"))

    print("who sees what: Kubernetes RBAC in each database's namespace", flush=True)
    me = alice.get("/api/me").json()
    assert me["kubernetes"]["user"] == "oidc:alice@example.com" and [e["name"] for e in me["environments"]] == ["dev", "tst"], me
    dev = alice.get("/api/environments/dev/databases").json()["databases"]
    assert [(d["name"], d["namespace"], d["can_snapshot"], d["can_restore"]) for d in dev] == [("payments_dev", "dev-payments", True, True)], dev
    assert carol.get("/api/environments/dev/databases").json()["databases"] == []
    assert [(d["name"], d["can_snapshot"]) for d in audrey.get("/api/environments/dev/databases").json()["databases"]] == [("payments_dev", False)]
    assert [(d["name"], d["can_snapshot"], d["can_restore"]) for d in pat.get("/api/environments/tst/databases").json()["databases"]] == [("payments_tst", True, False)]
    ok("databases: edit in the namespace acts, edit elsewhere sees nothing, cluster view only reads, production restore takes admin")
    users = alice.get("/api/environments/dev/users").json()["users"]
    assert [(u["name"], u["secret"]) for u in users] == [("payments_dev", "payments-dev-db")], users
    assert carol.get("/api/environments/dev/users").json()["users"] == []
    ok("users: the owner role and its password Secret, only for databases you see")
    assert carol.post("/api/environments/dev/databases/payments_dev/snapshots").status_code == 404
    assert audrey.post("/api/environments/dev/databases/payments_dev/snapshots").status_code == 403
    assert alice.post("/api/environments/dev/databases/payments_dev/snapshots", csrf=False).status_code == 403
    ok("refused: another namespace (to whom the database does not exist), a reader, and a write without the CSRF token")

    print("snapshot", flush=True)
    job = alice.post("/api/environments/dev/databases/payments_dev/snapshots", {"note": "before the test"}).json()
    snap = finished(alice, "dev", job["name"])
    assert snap["result"]["bytes"] > 0, snap
    snapshots = alice.get("/api/environments/dev/databases/payments_dev/snapshots").json()["snapshots"]
    assert len(snapshots) == 1 and snapshots[0]["note"] == "before the test" and snapshots[0]["namespace"] == "dev-payments", snapshots
    dev_snapshot = snapshots[0]["id"]
    ok(f"snapshot of dev: {snapshots[0]['size']} bytes in blob storage, with who, why and the namespace")

    print("restore in place", flush=True)
    psql("dev", "payments_dev", "set role payments_dev; delete from orders where id > 1; update orders set total = 0; create table later (id int)")
    assert orders("dev", "payments_dev") == ["dev-Ada"]
    sessions = int(psql("dev", "payments_dev", "select count(*) from pg_stat_activity where usename = 'payments_dev'"))
    assert sessions >= 2, sessions
    body = {"source_environment": "dev", "source_database": "payments_dev", "snapshot": dev_snapshot, "into": "database", "reason": "undo the bad batch"}
    job = alice.post("/api/environments/dev/databases/payments_dev/restore", body).json()
    assert job["state"] in ("starting", "running"), job
    assert alice.post("/api/environments/dev/databases/payments_dev/restore", body).status_code == 409
    ok("a second action on a busy database is refused")
    seen_zero = wait_for("the app to scale down", lambda: replicas("dev") == "0", timeout=120, every=1)
    finished(alice, "dev", job["name"])
    assert orders("dev", "payments_dev") == ["dev-Ada", "dev-Grace", "dev-Katherine"]
    assert psql("dev", "payments_dev", "select sum(total) from orders") == "118.75"
    assert psql("dev", "payments_dev", "select to_regclass('later') is null") == "t"
    assert seen_zero and replicas("dev") == "2"
    assert kubectl("get", "deployment/payments-api", "-n", "dev-payments", "-o", "jsonpath={.metadata.annotations}").find("replicas-before-restore") == -1
    ok("restore in place: exactly the snapshot (a table made later is gone), the app went to 0 and back to 2")
    wait_for("the app's sessions to return", lambda: int(psql("dev", "payments_dev", "select count(*) from pg_stat_activity where usename = 'payments_dev'")) >= 2, timeout=120)
    ok("the app reconnected")

    print("restore into a copy", flush=True)
    psql("dev", "payments_dev", "set role payments_dev; delete from orders where id = 3")
    job = alice.post("/api/environments/dev/databases/payments_dev/restore", {**body, "into": "copy", "reason": "look at last week's rows"}).json()
    finished(alice, "dev", job["name"])
    assert orders("dev", "payments_dev_copy") == ["dev-Ada", "dev-Grace", "dev-Katherine"]
    assert orders("dev", "payments_dev") == ["dev-Ada", "dev-Grace"]
    assert psql("dev", "payments_dev_copy", "select extname from pg_extension where extname = 'citext'") == "citext"
    db = alice.get("/api/environments/dev/databases").json()["databases"][0]
    assert db["copy"]["name"] == "payments_dev_copy", db
    ok("restore into a copy: payments_dev_copy holds the snapshot with its extension, payments_dev untouched")

    print("clone from production, once shared", flush=True)
    job = alice.post("/api/environments/tst/databases/payments_tst/snapshots", {"note": "for dev"}).json()
    finished(alice, "tst", job["name"])
    tst_snapshot = alice.get("/api/environments/tst/databases/payments_tst/snapshots").json()["snapshots"][0]["id"]
    clone = {"source_environment": "tst", "source_database": "payments_tst", "snapshot": tst_snapshot, "into": "database", "reason": "reproduce the tst bug"}
    assert alice.post("/api/environments/dev/databases/payments_dev/restore", clone).json()["error"] == "not_shared"
    assert alice.get("/api/environments/dev/databases/payments_dev/shared").json()["snapshots"] == []
    shared = alice.post(f"/api/environments/tst/databases/payments_tst/snapshots/{tst_snapshot}/share", {"environment": "dev", "namespace": "dev-payments"}).json()
    assert shared["shared_with"] == ["dev/dev-payments"], shared
    assert [s["id"] for s in alice.get("/api/environments/dev/databases/payments_dev/shared").json()["snapshots"]] == [tst_snapshot]
    job = alice.post("/api/environments/dev/databases/payments_dev/restore", clone).json()
    assert job["source_environment"] == "tst", job
    finished(alice, "dev", job["name"])
    assert orders("dev", "payments_dev") == ["tst-Ada", "tst-Grace", "tst-Katherine"]
    ok("clone: refused until shared, then tst's snapshot restored into dev")

    print("production restore needs a second person with admin", flush=True)
    psql("tst", "payments_tst", "set role payments_tst; truncate orders")
    body = {"source_environment": "tst", "source_database": "payments_tst", "snapshot": tst_snapshot, "into": "database", "reason": "restore after the truncate"}
    assert pat.post("/api/environments/tst/databases/payments_tst/restore", body).status_code == 403
    job = alice.post("/api/environments/tst/databases/payments_tst/restore", body).json()
    assert job["state"] == "waiting" and job["approval"] == "required", job
    time.sleep(5)
    assert replicas("tst") == "2" and orders("tst", "payments_tst") == []
    ok("production restore waits: nothing ran, the app untouched; edit alone may not ask")
    assert alice.post(f"/api/environments/tst/jobs/{job['name']}/approve").status_code == 403
    assert pat.post(f"/api/environments/tst/jobs/{job['name']}/approve").status_code == 403
    assert carol.post(f"/api/environments/tst/jobs/{job['name']}/approve").status_code == 403
    listed = next(j for j in bob.get("/api/environments/tst/jobs").json()["jobs"] if j["name"] == job["name"])
    assert listed["can_approve"] and not next(j for j in alice.get("/api/environments/tst/jobs").json()["jobs"] if j["name"] == job["name"])["can_approve"]
    ok("refused: approving your own request, with edit only, or from another namespace")
    approved = bob.post(f"/api/environments/tst/jobs/{job['name']}/approve").json()
    assert approved["approved_by"] == "bob@example.com", approved
    finished(bob, "tst", job["name"])
    assert orders("tst", "payments_tst") == ["tst-Ada", "tst-Grace", "tst-Katherine"] and replicas("tst") == "2"
    ok("bob approved; the restore ran")
    job = alice.post("/api/environments/tst/databases/payments_tst/restore", body).json()
    assert bob.post(f"/api/environments/tst/jobs/{job['name']}/decline", {"reason": "not now"}).status_code == 204
    assert kubectl("get", "job", job["name"], "-n", "database", "--ignore-not-found", "-o", "name") == ""
    ok("bob declined another; its Job is gone")

    print("downloads", flush=True)
    response = alice.get(f"/api/environments/dev/databases/payments_dev/snapshots/{dev_snapshot}/download")
    assert response.status_code == 200 and response.content[:5] == b"PGDMP", response.status_code
    assert alice.get(f"/api/environments/tst/databases/payments_tst/snapshots/{tst_snapshot}/download").status_code == 403
    assert audrey.get(f"/api/environments/dev/databases/payments_dev/snapshots/{dev_snapshot}/download").status_code == 403
    ok(f"download from dev ({len(response.content)} bytes, a pg_dump archive); none from production or for a reader")

    print("drop the copy", flush=True)
    assert alice.delete("/api/environments/dev/databases/payments_dev/copy").status_code == 204
    wait_for("the copy to go", lambda: psql("dev", "postgres", "select count(*) from pg_database where datname = 'payments_dev_copy'") == "0", timeout=120)
    ok("dropped the copy: CloudNativePG dropped payments_dev_copy")

    print("audit log", flush=True)
    dev_log = [(e["action"], e["actor"]) for e in alice.get("/api/environments/dev/audit").json()["entries"]]
    tst_log = [(e["action"], e["actor"]) for e in bob.get("/api/environments/tst/audit").json()["entries"]]
    for entry in [("snapshot.requested", "alice@example.com"), ("snapshot.succeeded", "alice@example.com"), ("restore.requested", "alice@example.com"), ("restore.succeeded", "alice@example.com"), ("snapshot.downloaded", "alice@example.com"), ("copy.dropped", "alice@example.com")]:
        assert entry in dev_log, (entry, dev_log)
    for entry in [("snapshot.shared", "alice@example.com"), ("restore.approved", "bob@example.com"), ("restore.declined", "bob@example.com"), ("restore.succeeded", "alice@example.com")]:
        assert entry in tst_log, (entry, tst_log)
    assert carol.get("/api/environments/dev/audit").json()["entries"] == []
    ok(f"audit log: {len(dev_log)} entries in dev, {len(tst_log)} in tst, who and what; none shown outside the namespace")


def forged(base: dict[str, Any], change) -> str:
    """Create a changed copy of a Job as this app's own service account; what Kubernetes said."""
    job = json.loads(json.dumps(base))
    job["metadata"] = {"name": f"forged-{secrets.token_hex(3)}", "namespace": "database", "labels": job["metadata"]["labels"], "annotations": job["metadata"].get("annotations", {})}
    job.pop("status", None)
    job["spec"].pop("selector", None)
    job["spec"]["template"]["metadata"]["labels"] = {k: v for k, v in job["spec"]["template"]["metadata"]["labels"].items() if "controller-uid" not in k and "job-name" not in k}
    job["spec"]["suspend"] = True
    change(job["spec"]["template"]["spec"])
    done = subprocess.run(["kubectl", "--as=system:serviceaccount:databases-ui:databases-ui", "create", "-f", "-"], input=json.dumps(job), capture_output=True, text=True, check=False)  # nosec B603 B607
    if done.returncode == 0:
        kubectl("delete", "job", job["metadata"]["name"], "-n", "database", "--wait=false")
        return "created"
    return done.stderr


def policy() -> None:
    print("what the server's own account may do", flush=True)
    name = kubectl("get", "jobs", "-n", "database", "-l", "databases-ui/action=snapshot", "-o", "jsonpath={.items[0].metadata.name}")
    base = json.loads(kubectl("get", "job", name, "-n", "database", "-o", "json"))

    def env(var: str, value: Any):
        def change(pod: dict[str, Any]) -> None:
            for e in pod["containers"][0]["env"]:
                if e["name"] == var:
                    e.clear()
                    e.update({"name": var, **value})
        return change

    assert forged(base, lambda pod: None) == "created"
    for what, change, said in [
        ("the superuser's password", env("PGPASSWORD", {"valueFrom": {"secretKeyRef": {"name": "dev-superuser", "key": "password"}}}), "never a superuser's"),
        ("another image", lambda pod: pod["containers"][0].update(image="busybox:1.36"), "only this app's own image"),
        ("a shell", lambda pod: pod["containers"][0].update(command=["sh", "-c", "cat /etc/databases-ui/ca/*"]), "only this app's own image"),
        ("the CA's key", lambda pod: next(v for v in pod["volumes"] if v["name"] == "ca")["secret"].update(items=[{"key": "ca.key", "path": "ca.key"}]), "a CA certificate, nothing else"),
        ("someone else's storage", env("DBUI_BLOB_URL", {"value": "https://attacker.blob.core.windows.net/loot"}), "only the catalog's storage"),
        ("a database elsewhere", env("PGHOST", {"value": "postgres.attacker.example"}), "in their own namespace"),
        ("TLS without checks", env("PGSSLMODE", {"value": "disable"}), "verified TLS"),
    ]:
        answer = forged(base, change)
        assert said in answer, (what, answer)
    ok("admission policy: a Job as built passes; with a superuser's password, another image or command, the CA key, other storage, another host or no TLS checks it is refused")
    for verb, resource in (("get", "secrets"), ("create", "pods"), ("delete", "clusters.postgresql.cnpg.io")):
        assert kubectl("auth", "can-i", verb, resource, "-n", "database", "--as=system:serviceaccount:databases-ui:databases-ui", check=False).strip() == "no"
    assert kubectl("auth", "can-i", "create", "subjectaccessreviews.authorization.k8s.io", "--as=system:serviceaccount:databases-ui:databases-ui", check=False).strip() == "yes"
    ok("RBAC: the server's account reads no Secret, creates no pod and deletes no Cluster; it asks access reviews")


def main() -> None:
    _, cookies = setup()
    with port_forward("databases-ui", "svc/databases-ui", "18080:80"):
        wait_for("the server", lambda: httpx.get(f"{ORIGIN}/healthz").status_code == 200, timeout=60, every=1)
        scenarios(cookies)
    policy()
    print(f"\n{len(passed)} checks passed")


if __name__ == "__main__":
    sys.exit(main())
