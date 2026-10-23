"""What people may do, and doing it: an environment's databases, their snapshots, and the Jobs
that take, restore and clone them.

Who acts on which database comes from the catalog's teams and the person's groups, checked here
on every call; the browser only shows what this server decided. App role Admin acts on every
database, Auditor reads everything and changes nothing. In a production environment a restore
waits for a second person, and nobody downloads a snapshot.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import audit, jobs
from .blob import Container
from .catalog import DATABASE, Environment, Grant
from .config import Config
from .kube import CNPG, Kube

APPROVAL_HOURS = 24
SNAPSHOT_ID = r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$"
_ACTIVE = ("waiting", "starting", "running")
_COPY = "_copy"


class ConsoleError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class Actor:
    oid: str
    upn: str
    name: str
    groups: frozenset[str]
    admin: bool = False
    auditor: bool = False


def snapshot_blob(database: str, snapshot: str) -> str:
    return f"snapshots/{database}/{snapshot}.dump"


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


class Console:
    def __init__(self, cfg: Config, kube: dict[str, Kube], storage: dict[str, Container], now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.cfg = cfg
        self.catalog = cfg.catalog
        self._kube = kube
        self._storage = storage
        self._now = now

    # ── Who may do what ─────────────────────────────────────────────────────────────

    def environment(self, name: str) -> Environment:
        env = self.catalog.environments.get(name)
        if env is None:
            raise ConsoleError(404, "unknown_environment", f"There is no environment {name!r}.")
        return env

    def teams(self, actor: Actor) -> set[str]:
        return {t.id for t in self.catalog.teams_of(actor.groups)}

    def grant(self, actor: Actor, env: str, database: str) -> Grant | None:
        return self.catalog.grant(self.teams(actor), env, database)

    def may_read(self, actor: Actor, env: str, database: str) -> bool:
        return actor.admin or actor.auditor or self.grant(actor, env, database) is not None

    def may_act(self, actor: Actor, env: str, database: str) -> bool:
        return actor.admin or self.grant(actor, env, database) is not None

    def _require(self, allowed: bool) -> None:
        if not allowed:
            raise ConsoleError(403, "not_allowed", "You may not do that with this database.")

    def _kube_for(self, env: Environment) -> Kube:
        kube = self._kube.get(env.name)
        if kube is None:
            raise ConsoleError(409, "no_jobs_here", f"This app only reads {env.name}'s snapshots; nothing runs there.")
        return kube

    # ── Databases ───────────────────────────────────────────────────────────────────

    async def _resources(self, env: Environment) -> list[dict[str, Any]]:
        found = await self._kube_for(env).list(f"{CNPG}/namespaces/{env.namespace}/databases")
        return [d for d in found if ((d.get("spec") or {}).get("cluster") or {}).get("name") == env.cnpg_cluster and (d.get("spec") or {}).get("ensure") != "absent"]

    async def _resource(self, env: Environment, database: str) -> dict[str, Any]:
        found = next((d for d in await self._resources(env) if d["spec"].get("name") == database), None)
        if found is None:
            raise ConsoleError(404, "unknown_database", f"{env.name} has no database {database!r}.")
        return found

    async def databases(self, actor: Actor, name: str) -> list[dict[str, Any]]:
        env = self.environment(name)
        granted = self.catalog.databases(env.name, self.teams(actor))
        everything = actor.admin or actor.auditor
        if not env.runs_jobs:
            listed = self.catalog.databases(env.name) if everything else granted
            return [self._database_json(actor, env, db, None, None) for db in sorted(listed)]
        resources = await self._resources(env)
        copies = {(d.get("metadata") or {}).get("labels", {}).get(f"{jobs.PREFIX}copy-of"): d for d in resources}
        out = []
        for d in sorted(resources, key=lambda d: d["spec"].get("name", "")):
            db = d["spec"].get("name", "")
            if d["metadata"].get("labels", {}).get(f"{jobs.PREFIX}copy-of") or not (everything or db in granted):
                continue
            out.append(self._database_json(actor, env, db, d, copies.get(db)))
        return out

    def _database_json(self, actor: Actor, env: Environment, db: str, resource: dict[str, Any] | None, copy: dict[str, Any] | None) -> dict[str, Any]:
        grants = [g for g in self.catalog.grants if g.environment == env.name and g.database == db]
        status = (resource or {}).get("status") or {}
        return {
            "name": db,
            "owner": ((resource or {}).get("spec") or {}).get("owner", ""),
            "ready": bool(status.get("applied")) if resource else None,
            "message": status.get("message", ""),
            "teams": sorted({g.team for g in grants}),
            "workloads": sorted({"/".join(w) for g in grants for w in g.workloads}),
            "copy": None if copy is None else {"name": copy["spec"].get("name", ""), "ready": bool((copy.get("status") or {}).get("applied"))},
            "can_act": self.may_act(actor, env.name, db) and env.runs_jobs,
        }

    async def _target(self, env: Environment, resource: dict[str, Any], database: str) -> jobs.Target:
        owner = resource["spec"].get("owner", "")
        cluster = await self._kube_for(env).get(f"{CNPG}/namespaces/{env.namespace}/clusters/{env.cnpg_cluster}")
        if cluster is None:
            raise ConsoleError(409, "no_cluster", f"There is no Cluster {env.cnpg_cluster} in {env.namespace}.")
        roles = ((cluster.get("spec") or {}).get("managed") or {}).get("roles") or []
        secret = next((((r.get("passwordSecret") or {}).get("name")) for r in roles if r.get("name") == owner), None)
        if not secret:
            raise ConsoleError(409, "no_credential", f"The owner role {owner!r} has no password Secret among the Cluster's managed roles, so this app can't connect as it.")
        ca = ((cluster.get("status") or {}).get("certificates") or {}).get("serverCASecret") or f"{env.cnpg_cluster}-ca"
        return jobs.Target(environment=env.name, database=database, owner=owner, password_secret=secret, ca_secret=ca, host=f"{env.cnpg_cluster}-rw.{env.namespace}.svc")

    # ── Snapshots ───────────────────────────────────────────────────────────────────

    def _check_name(self, database: str) -> None:
        if not re.match(DATABASE, database):
            raise ConsoleError(404, "unknown_database", "That is not a database name this app takes.")

    async def snapshots(self, actor: Actor, name: str, database: str) -> list[dict[str, Any]]:
        env = self.environment(name)
        self._check_name(database)
        self._require(self.may_read(actor, env.name, database))
        found = await self._storage[env.name].list(f"snapshots/{database}/")
        out = []
        for blob in found:
            sid = blob.name.rsplit("/", 1)[-1].removesuffix(".dump")
            if re.match(SNAPSHOT_ID, sid) and blob.name == snapshot_blob(database, sid):
                m = blob.metadata
                out.append({"id": sid, "created": m.get("created") or blob.created, "size": blob.size, "created_by": m.get("createdby", ""), "note": m.get("note", ""), "sha256": m.get("sha256", "")})
        return sorted(out, key=lambda s: s["id"], reverse=True)

    async def _busy(self, env: Environment, database: str) -> None:
        for job in await self._kube_for(env).list(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", jobs.selector(env.name, database)):
            if jobs.state(job) in _ACTIVE and not self._expired(job):
                raise ConsoleError(409, "busy", f"{database} has an action running or waiting for approval ({job['metadata']['name']}).")

    async def take_snapshot(self, actor: Actor, name: str, database: str, note: str) -> dict[str, Any]:
        env = self.environment(name)
        self._check_name(database)
        self._require(self.may_act(actor, env.name, database))
        await self._busy(env, database)
        target = await self._target(env, await self._resource(env, database), database)
        now = self._now()
        sid = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        job_name = jobs.name_for("snapshot")
        blob = snapshot_blob(database, sid)
        manifest = jobs.build(
            self.cfg, env, name=job_name, action="snapshot", target=target, database=database, blob_url=env.storage.url, blob=blob, requested_by=actor.upn,
            annotations={"requested-by": actor.upn, "requested-at": _iso(now), "note": note, "snapshot": sid},
            metadata={"createdby": actor.upn, "created": _iso(now), "note": note, "environment": env.name, "database": database, "job": job_name},
        )  # fmt: skip
        job = await self._kube_for(env).create(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", manifest)
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="snapshot.requested", at=now, database=database, job=job_name, snapshot=blob, detail=note)
        return self._job_json(actor, env, job, None)

    async def restore(self, actor: Actor, name: str, database: str, *, source_environment: str, source_database: str, snapshot: str, into: str, reason: str) -> dict[str, Any]:
        env = self.environment(name)
        source = self.environment(source_environment)
        self._check_name(database)
        self._check_name(source_database)
        if not re.match(SNAPSHOT_ID, snapshot):
            raise ConsoleError(404, "unknown_snapshot", "That is not a snapshot.")
        self._require(self.may_act(actor, env.name, database) and self.may_read(actor, source.name, source_database))
        if source.name != env.name and source.name not in env.clone_from:
            raise ConsoleError(403, "no_clone", f"{env.name} takes no snapshots from {source.name}.")
        blob = snapshot_blob(source_database, snapshot)
        if await self._storage[source.name].properties(blob) is None:
            raise ConsoleError(404, "unknown_snapshot", f"{source.name} has no snapshot {snapshot} of {source_database}.")
        await self._busy(env, database)
        resource = await self._resource(env, database)
        workloads: tuple[tuple[str, str, str], ...] = ()
        if into == "copy":
            target = await self._target(env, await self._ensure_copy(env, resource, database), database + _COPY)
        else:
            target = await self._target(env, resource, database)
            workloads = tuple(sorted({w for g in self.catalog.grants if g.environment == env.name and g.database == database for w in g.workloads}))
        now = self._now()
        job_name = jobs.name_for("restore")
        manifest = jobs.build(
            self.cfg, env, name=job_name, action="restore", target=target, database=database, blob_url=source.storage.url, blob=blob, requested_by=actor.upn,
            annotations={
                "requested-by": actor.upn, "requested-at": _iso(now), "reason": reason, "snapshot": snapshot, "source-environment": source.name,
                "source-database": source_database, "into": into, "target-database": target.database, "approval": "required" if env.production else "",
            },
            suspend=env.production, workloads=workloads, wait_for_database=into == "copy",
        )  # fmt: skip
        job = await self._kube_for(env).create(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", manifest)
        await audit.record(
            self._storage[env.name], environment=env.name, actor=actor.upn, action="restore.requested", at=now, database=database, job=job_name,
            snapshot=f"{source.name}/{blob}", target=target.database, detail=reason,
        )  # fmt: skip
        return self._job_json(actor, env, job, None)

    async def _ensure_copy(self, env: Environment, resource: dict[str, Any], database: str) -> dict[str, Any]:
        """The Database resource for `<database>_copy`: CloudNativePG creates the database, and drops it with the resource."""
        name = f"{resource['metadata']['name']}-copy"[:253]
        path = f"{CNPG}/namespaces/{env.namespace}/databases"
        existing = await self._kube_for(env).get(f"{path}/{name}")
        if existing is not None:
            if existing["metadata"].get("labels", {}).get(f"{jobs.PREFIX}copy-of") != database:
                raise ConsoleError(409, "copy_taken", f"A Database resource {name} exists that this app did not create.")
            return existing
        extensions = [{k: v for k, v in e.items() if k in ("name", "version", "schema")} for e in resource["spec"].get("extensions") or [] if e.get("ensure") != "absent"]
        body = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Database",
            "metadata": {"name": name, "namespace": env.namespace, "labels": {jobs.MANAGED_BY: jobs.APP, f"{jobs.PREFIX}environment": env.name, f"{jobs.PREFIX}copy-of": database}},
            "spec": {
                "name": database + _COPY,
                "owner": resource["spec"]["owner"],
                "cluster": {"name": env.cnpg_cluster},
                "ensure": "present",
                "databaseReclaimPolicy": "delete",
                **({"extensions": extensions} if extensions else {}),
            },
        }
        return await self._kube_for(env).create(path, body)

    async def drop_copy(self, actor: Actor, name: str, database: str) -> None:
        env = self.environment(name)
        self._check_name(database)
        self._require(self.may_act(actor, env.name, database))
        await self._busy(env, database)
        path = f"{CNPG}/namespaces/{env.namespace}/databases"
        copy = next((d for d in await self._kube_for(env).list(path, f"{jobs.MANAGED_BY}={jobs.APP},{jobs.PREFIX}copy-of={database}")), None)
        if copy is None:
            raise ConsoleError(404, "no_copy", f"{database} has no copy.")
        await self._kube_for(env).delete(f"{path}/{copy['metadata']['name']}")
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="copy.dropped", database=database, target=copy["spec"].get("name", ""))

    async def download(self, actor: Actor, name: str, database: str, snapshot: str) -> tuple[Container, str, str]:
        """The container and blob to stream, and a file name, once the download is recorded."""
        env = self.environment(name)
        self._check_name(database)
        if env.production:
            raise ConsoleError(403, "no_downloads", f"Snapshots of {env.name} stay in {env.name}.")
        self._require(self.may_act(actor, env.name, database))
        if not re.match(SNAPSHOT_ID, snapshot):
            raise ConsoleError(404, "unknown_snapshot", "That is not a snapshot.")
        blob = snapshot_blob(database, snapshot)
        if await self._storage[env.name].properties(blob) is None:
            raise ConsoleError(404, "unknown_snapshot", f"{env.name} has no snapshot {snapshot} of {database}.")
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="snapshot.downloaded", database=database, snapshot=blob)
        return self._storage[env.name], blob, f"{env.name}-{database}-{snapshot}.dump"

    # ── Jobs ────────────────────────────────────────────────────────────────────────

    def _expired(self, job: dict[str, Any]) -> bool:
        if jobs.state(job) != "waiting":
            return False
        at = _when((job["metadata"].get("annotations") or {}).get(f"{jobs.PREFIX}requested-at", ""))
        return at is None or self._now() - at > timedelta(hours=APPROVAL_HOURS)

    def _job_json(self, actor: Actor, env: Environment, job: dict[str, Any], pod: dict[str, Any] | None) -> dict[str, Any]:
        meta = job.get("metadata") or {}
        a = {k.removeprefix(jobs.PREFIX): v for k, v in (meta.get("annotations") or {}).items() if k.startswith(jobs.PREFIX)}
        labels = meta.get("labels") or {}
        database = labels.get(f"{jobs.PREFIX}database", "")
        state = "expired" if self._expired(job) else jobs.state(job)
        status = job.get("status") or {}
        waiting = state == "waiting"
        return {
            "name": meta.get("name", ""),
            "action": labels.get(f"{jobs.PREFIX}action", ""),
            "database": database,
            "state": state,
            "requested_by": a.get("requested-by", ""),
            "requested_at": a.get("requested-at", ""),
            "approved_by": a.get("approved-by", ""),
            "approved_at": a.get("approved-at", ""),
            "reason": a.get("reason", ""),
            "note": a.get("note", ""),
            "snapshot": a.get("snapshot", ""),
            "source_environment": a.get("source-environment", ""),
            "source_database": a.get("source-database", ""),
            "into": a.get("into", ""),
            "target_database": a.get("target-database", ""),
            "approval": a.get("approval", ""),
            "started": status.get("startTime", ""),
            "finished": status.get("completionTime", "") or next((c.get("lastTransitionTime", "") for c in status.get("conditions") or [] if c.get("type") == "Failed"), ""),
            "result": jobs.result(pod),
            "can_approve": waiting and actor.upn != a.get("requested-by") and self.may_act(actor, env.name, database),
            "can_decline": waiting and (actor.upn == a.get("requested-by") or self.may_act(actor, env.name, database)),
        }

    async def jobs(self, actor: Actor, name: str) -> list[dict[str, Any]]:
        env = self.environment(name)
        if not env.runs_jobs:
            return []
        kube = self._kube_for(env)
        found = await kube.list(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", jobs.selector(env.name))
        pods = {}
        for pod in await kube.list(f"/api/v1/namespaces/{env.namespace}/pods", jobs.selector(env.name)):
            pods[(pod.get("metadata") or {}).get("labels", {}).get("job-name", "")] = pod
        out = []
        for job in found:
            if self._expired(job):
                await self._drop(env, job, actor="databases-ui", action="restore.expired")
                continue
            if self.may_read(actor, env.name, (job["metadata"].get("labels") or {}).get(f"{jobs.PREFIX}database", "")):
                out.append(self._job_json(actor, env, job, pods.get(job["metadata"]["name"])))
        return sorted(out, key=lambda j: j["requested_at"], reverse=True)

    async def _waiting(self, actor: Actor, env: Environment, job_name: str) -> dict[str, Any]:
        job = await self._kube_for(env).get(f"/apis/batch/v1/namespaces/{env.namespace}/jobs/{job_name}")
        labels = (job or {}).get("metadata", {}).get("labels") or {}
        if job is None or labels.get(jobs.MANAGED_BY) != jobs.APP or labels.get(f"{jobs.PREFIX}environment") != env.name:
            raise ConsoleError(404, "unknown_job", "There is no such request.")
        self._require(self.may_read(actor, env.name, labels.get(f"{jobs.PREFIX}database", "")))
        if self._expired(job):
            raise ConsoleError(409, "expired", f"Requests wait {APPROVAL_HOURS} hours for approval; ask again.")
        if jobs.state(job) != "waiting":
            raise ConsoleError(409, "not_waiting", "This request is not waiting for approval.")
        return job

    async def approve(self, actor: Actor, name: str, job_name: str) -> dict[str, Any]:
        env = self.environment(name)
        job = await self._waiting(actor, env, job_name)
        view = self._job_json(actor, env, job, None)
        if actor.upn == view["requested_by"]:
            raise ConsoleError(403, "own_request", "A second person approves this, not the one who asked.")
        self._require(view["can_approve"])
        now = self._now()
        patch = {"metadata": {"annotations": {f"{jobs.PREFIX}approved-by": actor.upn, f"{jobs.PREFIX}approved-at": _iso(now)}}, "spec": {"suspend": False}}
        job = await self._kube_for(env).patch(f"/apis/batch/v1/namespaces/{env.namespace}/jobs/{job_name}", patch)
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="restore.approved", at=now, database=view["database"], job=job_name, detail=f"asked by {view['requested_by']}")
        return self._job_json(actor, env, job, None)

    async def decline(self, actor: Actor, name: str, job_name: str, reason: str) -> None:
        env = self.environment(name)
        job = await self._waiting(actor, env, job_name)
        self._require(self._job_json(actor, env, job, None)["can_decline"])
        await self._drop(env, job, actor=actor.upn, action="restore.declined", detail=reason)

    async def _drop(self, env: Environment, job: dict[str, Any], *, actor: str, action: str, detail: str = "") -> None:
        meta = job["metadata"]
        await self._kube_for(env).delete(f"/apis/batch/v1/namespaces/{env.namespace}/jobs/{meta['name']}")
        await audit.record(self._storage[env.name], environment=env.name, actor=actor, action=action, database=(meta.get("labels") or {}).get(f"{jobs.PREFIX}database", ""), job=meta["name"], detail=detail)

    # ── Audit ───────────────────────────────────────────────────────────────────────

    async def audit_log(self, actor: Actor, name: str, limit: int = 500) -> list[dict[str, Any]]:
        env = self.environment(name)
        container = self._storage[env.name]
        months = sorted((b.name for b in await container.list(audit.PREFIX)), reverse=True)[:2]
        entries = []
        for month in months:
            entries += audit.parse(await container.read(month) or b"")
        visible = [e for e in entries if actor.admin or actor.auditor or (e.get("database") and self.may_read(actor, env.name, str(e["database"])))]
        return sorted(visible, key=lambda e: str(e.get("at", "")), reverse=True)[:limit]
