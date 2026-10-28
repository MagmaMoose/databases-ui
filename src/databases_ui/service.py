"""What people may do, and doing it: an environment's databases and their owners, snapshots, and
the Jobs that take, restore and clone them and run scripts on them.

Who may act on a database is Kubernetes' answer. A Database resource names, in a label, the
namespace of the app it belongs to (without one, its own namespace: the platform's), and so does
an external database in the catalog; the server asks the API server, with a SubjectAccessReview,
whether the signed-in person may do there what the action takes (catalog.py: `view` to see,
`edit` to snapshot, restore and run scripts, `admin` to restore or run a script in production).
Answers are kept for a minute. In a production environment a restore or a script also waits for
a second person, and nobody downloads a snapshot.

Another environment's snapshot reaches this one only when someone who may snapshot it there shares
it with a namespace here; whoever may restore in that namespace can then clone it.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from . import audit, jobs, sql
from .blob import Container
from .catalog import DATABASE, NAMESPACE, Environment
from .config import Config
from .kube import CNPG, Kube, KubeError

APPROVAL_HOURS = 24
REVIEW_SECONDS = 60
SNAPSHOT_ID = r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$"
WORKLOADS = f"{jobs.PREFIX}workloads"
_WORKLOAD = re.compile(r"^(deployment|statefulset)/([a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?)$")
_ACTIVE = ("waiting", "starting", "running")
_COPY = "_copy"
Action = Literal["read", "snapshot", "restore", "script"]


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
    # Who Kubernetes takes them for.
    user: str
    groups: tuple[str, ...]


def snapshot_blob(database: str, snapshot: str) -> str:
    return f"snapshots/{database}/{snapshot}.dump"


def script_blob(database: str, script: str) -> str:
    return f"scripts/{database}/{script}.sql"


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _decides(action: str) -> Action:
    """What approving or declining a request takes: what asking for it took."""
    return "script" if action == "script" else "restore"


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _shares(metadata: dict[str, str]) -> list[str]:
    return sorted(set(metadata.get("sharedwith", "").split()))


class Console:
    def __init__(self, cfg: Config, kube: dict[str, Kube], storage: dict[str, Container], now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.cfg = cfg
        self.catalog = cfg.catalog
        self._kube = kube
        self._storage = storage
        self._now = now
        self._reviews: dict[tuple[Any, ...], tuple[bool, float]] = {}

    # ── Who may do what ─────────────────────────────────────────────────────────────

    def environment(self, name: str) -> Environment:
        env = self.catalog.environments.get(name)
        if env is None:
            raise ConsoleError(404, "unknown_environment", f"There is no environment {name!r}.")
        return env

    def _kube_for(self, env: Environment) -> Kube:
        kube = self._kube.get(env.name)
        if kube is None:
            raise ConsoleError(409, "no_jobs_here", f"{env.name} is another cluster's: only snapshots shared from it are read here.")
        return kube

    def namespace_of(self, env: Environment, resource: dict[str, Any]) -> str:
        found = ((resource.get("metadata") or {}).get("labels") or {}).get(self.cfg.namespace_label, "")
        return found if re.match(NAMESPACE, found) else env.namespace

    async def allowed(self, actor: Actor, env: Environment, namespace: str, action: Action) -> bool:
        """Whether Kubernetes lets this person do what `action` takes in `namespace`."""
        check = env.check(action)
        key = (env.name, actor.user, actor.groups, namespace, check)
        now = time.monotonic()
        if (hit := self._reviews.get(key)) is not None and hit[1] > now:
            return hit[0]
        resource, _, subresource = check.resource.partition("/")
        review = await self._kube_for(env).create(
            "/apis/authorization.k8s.io/v1/subjectaccessreviews",
            {
                "apiVersion": "authorization.k8s.io/v1",
                "kind": "SubjectAccessReview",
                "spec": {
                    "user": actor.user,
                    # As the API server would: every person it authenticates is in this group too.
                    "groups": [*actor.groups, "system:authenticated"],
                    "resourceAttributes": {"namespace": namespace, "verb": check.verb, "group": check.group, "resource": resource, "subresource": subresource},
                },
            },
        )
        answer = bool((review.get("status") or {}).get("allowed"))
        if len(self._reviews) > 10_000:
            self._reviews.clear()
        self._reviews[key] = (answer, now + REVIEW_SECONDS)
        return answer

    async def _answers(self, actor: Actor, env: Environment, namespaces: Iterable[str], action: Action) -> dict[str, bool]:
        found = sorted(set(namespaces))
        return dict(zip(found, await asyncio.gather(*(self.allowed(actor, env, ns, action) for ns in found)), strict=True))

    async def _require(self, actor: Actor, env: Environment, namespace: str, action: Action) -> None:
        if not await self.allowed(actor, env, namespace, action):
            check = env.check(action)
            what = f"{check.verb} {check.resource}" + (f".{check.group}" if check.group else "")
            raise ConsoleError(403, "not_allowed", f"That takes {what} in the namespace {namespace}, which Kubernetes does not give you.")

    # ── Databases and their owners ───────────────────────────────────────────────────

    def _check_name(self, database: str) -> None:
        if not re.match(DATABASE, database):
            raise ConsoleError(404, "unknown_database", "That is not a database name this app takes.")

    async def _resources(self, env: Environment) -> list[dict[str, Any]]:
        try:
            found = await self._kube_for(env).list(f"{CNPG}/namespaces/{env.namespace}/databases")
        except KubeError as err:
            # No CloudNativePG in this cluster: only the catalog's external databases.
            if err.status == 404:
                return []
            raise
        return [
            d for d in found
            if ((d.get("spec") or {}).get("cluster") or {}).get("name") == env.cnpg_cluster and (d.get("spec") or {}).get("ensure") != "absent"
            # The catalog's external database of the same name is the one this app shows.
            and env.server((d.get("spec") or {}).get("name", "")) is None
        ]  # fmt: skip

    def _copy_of(self, resource: dict[str, Any]) -> str:
        return ((resource.get("metadata") or {}).get("labels") or {}).get(f"{jobs.PREFIX}copy-of", "")

    async def _resource(self, env: Environment, database: str) -> dict[str, Any]:
        self._check_name(database)
        found = next((d for d in await self._resources(env) if d["spec"].get("name") == database and not self._copy_of(d)), None)
        if found is None:
            raise ConsoleError(404, "unknown_database", f"{env.name} has no database {database!r}.")
        return found

    async def _database(self, actor: Actor, env: Environment, database: str) -> tuple[dict[str, Any] | None, str]:
        """A database's Database resource (None for an external one) and its namespace, if this
        person may see it: to anyone else it does not exist."""
        self._check_name(database)
        if (server := env.server(database)) is not None and env.runs_jobs:
            resource, namespace = None, server.namespace or env.namespace
        else:
            resource = await self._resource(env, database)
            namespace = self.namespace_of(env, resource)
        if not await self.allowed(actor, env, namespace, "read"):
            raise ConsoleError(404, "unknown_database", f"{env.name} has no database {database!r}.")
        return resource, namespace

    async def _cluster(self, env: Environment) -> dict[str, Any]:
        cluster = await self._kube_for(env).get(f"{CNPG}/namespaces/{env.namespace}/clusters/{env.cnpg_cluster}")
        if cluster is None:
            raise ConsoleError(409, "no_cluster", f"There is no Cluster {env.cnpg_cluster} in {env.namespace}.")
        return cluster

    def _roles(self, cluster: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {r["name"]: r for r in ((cluster.get("spec") or {}).get("managed") or {}).get("roles") or [] if r.get("name")}

    def workloads(self, resource: dict[str, Any], namespace: str) -> tuple[tuple[str, str, str], ...]:
        """What a restore in place scales to 0: the Database resource's databases-ui/workloads
        annotation, `deployment/<name>,statefulset/<name>` in the app's namespace."""
        text = ((resource.get("metadata") or {}).get("annotations") or {}).get(WORKLOADS, "")
        found = []
        for item in filter(None, (part.strip() for part in text.split(","))):
            if not (m := _WORKLOAD.match(item)):
                raise ConsoleError(409, "bad_workloads", f"The annotation {WORKLOADS} names {item!r}; it takes deployment/<name> or statefulset/<name>.")
            found.append((m.group(1), namespace, m.group(2)))
        return tuple(found)

    async def databases(self, actor: Actor, name: str) -> list[dict[str, Any]]:
        env = self.environment(name)
        if not env.runs_jobs:
            return []
        resources = await self._resources(env)
        own = {d["spec"].get("name", ""): d for d in resources if not self._copy_of(d)}
        copies = {self._copy_of(d): d for d in resources if self._copy_of(d)}
        spaces = {db: self.namespace_of(env, d) for db, d in own.items()} | {s.name: s.namespace or env.namespace for s in env.external}
        seen = await self._answers(actor, env, spaces.values(), "read")
        visible = [db for db in sorted(spaces) if seen[spaces[db]]]
        snapshot = await self._answers(actor, env, (spaces[db] for db in visible), "snapshot")
        restore = await self._answers(actor, env, (spaces[db] for db in visible), "restore")
        script = await self._answers(actor, env, (spaces[db] for db in visible), "script")
        roles = self._roles(await self._cluster(env)) if any(db in own for db in visible) else {}
        out = []
        for db in visible:
            ns = spaces[db]
            if (server := env.server(db)) is not None:
                out.append(
                    {
                        "name": db, "kind": "external", "namespace": ns, "owner": server.user, "owner_secret": server.password_secret, "ready": True, "message": "",
                        "server": {"host": server.host, "port": server.port, "database": server.database, "tls": server.tls},
                        "workloads": [], "copy": None, "can_snapshot": snapshot[ns], "can_restore": False, "can_script": script[ns],
                    }
                )  # fmt: skip
                continue
            d = own[db]
            copy = copies.get(db)
            try:
                workloads = [f"{kind}/{wname}" for kind, _, wname in self.workloads(d, ns)]
            except ConsoleError:
                workloads = []
            owner = d["spec"].get("owner", "")
            out.append(
                {
                    "name": db,
                    "kind": "cloudnativepg",
                    "namespace": ns,
                    "owner": owner,
                    "owner_secret": ((roles.get(owner) or {}).get("passwordSecret") or {}).get("name", ""),
                    "ready": bool((d.get("status") or {}).get("applied")),
                    "message": (d.get("status") or {}).get("message", ""),
                    "server": None,
                    "workloads": workloads,
                    "copy": None if copy is None else {"name": copy["spec"].get("name", ""), "ready": bool((copy.get("status") or {}).get("applied"))},
                    "can_snapshot": snapshot[ns],
                    "can_restore": restore[ns],
                    "can_script": script[ns],
                }
            )
        return out

    async def users(self, actor: Actor, name: str) -> list[dict[str, Any]]:
        """The owner roles of the databases this person sees, from the Cluster's managed roles, and
        the logins the catalog gives external databases."""
        found = await self.databases(actor, name)
        if not found:
            return []
        native = [d for d in found if d["kind"] != "external"]
        roles = self._roles(await self._cluster(self.environment(name))) if native else {}
        out = []
        for owner in sorted({d["owner"] for d in native}):
            role = roles.get(owner)
            out.append(
                {
                    "name": owner,
                    "managed": role is not None,
                    "login": bool((role or {}).get("login")),
                    "secret": ((role or {}).get("passwordSecret") or {}).get("name", ""),
                    "databases": [d["name"] for d in native if d["owner"] == owner],
                    "namespaces": sorted({d["namespace"] for d in native if d["owner"] == owner}),
                    "server": "",
                }
            )
        for d in found:
            if d["kind"] == "external":
                server = d["server"]
                out.append(
                    {
                        "name": d["owner"], "managed": False, "login": True, "secret": d["owner_secret"], "databases": [d["name"]], "namespaces": [d["namespace"]],
                        "server": f"{server['host']}:{server['port']}/{server['database']}",
                    }
                )  # fmt: skip
        return out

    async def _target(self, env: Environment, resource: dict[str, Any] | None, database: str, namespace: str) -> jobs.Target:
        if resource is None:
            server = env.server(database)
            if server is None:
                raise ConsoleError(404, "unknown_database", f"{env.name} has no database {database!r}.")
            return jobs.Target(
                environment=env.name, database=server.database, owner=server.user, password_secret=server.password_secret, ca_secret=server.ca_secret,
                host=server.host, namespace=namespace, port=server.port, sslmode=server.tls,
            )  # fmt: skip
        owner = resource["spec"].get("owner", "")
        cluster = await self._cluster(env)
        secret = ((self._roles(cluster).get(owner) or {}).get("passwordSecret") or {}).get("name")
        if not secret:
            raise ConsoleError(409, "no_credential", f"The owner role {owner!r} has no password Secret among the Cluster's managed roles, so this app can't connect as it.")
        ca = ((cluster.get("status") or {}).get("certificates") or {}).get("serverCASecret") or f"{env.cnpg_cluster}-ca"
        return jobs.Target(environment=env.name, database=database, owner=owner, password_secret=secret, ca_secret=ca, host=f"{env.cnpg_cluster}-rw.{env.namespace}.svc", namespace=namespace)

    # ── Snapshots ───────────────────────────────────────────────────────────────────

    def _snapshot_json(self, environment: str, database: str, sid: str, size: int, created: str, metadata: dict[str, str]) -> dict[str, Any]:
        return {
            "environment": environment, "database": database, "id": sid, "created": metadata.get("created") or created, "size": size,
            "created_by": metadata.get("createdby", ""), "note": metadata.get("note", ""), "namespace": metadata.get("namespace", ""), "shared_with": _shares(metadata),
        }  # fmt: skip

    async def snapshots(self, actor: Actor, name: str, database: str) -> list[dict[str, Any]]:
        env = self.environment(name)
        await self._database(actor, env, database)
        out = []
        for blob in await self._storage[env.name].list(f"snapshots/{database}/"):
            sid = blob.name.rsplit("/", 1)[-1].removesuffix(".dump")
            if re.match(SNAPSHOT_ID, sid) and blob.name == snapshot_blob(database, sid):
                out.append(self._snapshot_json(env.name, database, sid, blob.size, blob.created, blob.metadata))
        return sorted(out, key=lambda s: s["id"], reverse=True)

    async def shared(self, actor: Actor, name: str, database: str) -> list[dict[str, Any]]:
        """Other environments' snapshots shared with this database's namespace."""
        env = self.environment(name)
        _, namespace = await self._database(actor, env, database)
        out = []
        for source in env.clone_from:
            for blob in await self._storage[source].list("snapshots/"):
                parts = blob.name.split("/")
                sid = parts[-1].removesuffix(".dump")
                if len(parts) == 3 and re.match(SNAPSHOT_ID, sid) and f"{env.name}/{namespace}" in _shares(blob.metadata):
                    out.append(self._snapshot_json(source, parts[1], sid, blob.size, blob.created, blob.metadata))
        return sorted(out, key=lambda s: s["id"], reverse=True)

    async def _busy(self, env: Environment, database: str) -> None:
        for job in await self._kube_for(env).list(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", jobs.selector(env.name, database)):
            if jobs.state(job) in _ACTIVE and not self._expired(job):
                raise ConsoleError(409, "busy", f"{database} has an action running or waiting for approval ({job['metadata']['name']}).")

    async def take_snapshot(self, actor: Actor, name: str, database: str, note: str) -> dict[str, Any]:
        env = self.environment(name)
        resource, namespace = await self._database(actor, env, database)
        await self._require(actor, env, namespace, "snapshot")
        await self._busy(env, database)
        target = await self._target(env, resource, database, namespace)
        now = self._now()
        sid = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        job_name = jobs.name_for("snapshot")
        blob = snapshot_blob(database, sid)
        manifest = jobs.build(
            self.cfg, env, name=job_name, action="snapshot", target=target, database=database, blob_url=env.storage.url, blob=blob, requested_by=actor.upn,
            annotations={"requested-by": actor.upn, "requested-at": _iso(now), "note": note, "snapshot": sid},
            metadata={"createdby": actor.upn, "created": _iso(now), "note": note, "environment": env.name, "database": database, "namespace": namespace, "job": job_name},
        )  # fmt: skip
        job = await self._kube_for(env).create(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", manifest)
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="snapshot.requested", at=now, database=database, namespace=namespace, job=job_name, snapshot=blob, detail=note)
        return self._job_json(env, job, None, can_approve=False, can_decline=False)

    async def share(self, actor: Actor, name: str, database: str, snapshot: str, *, environment: str, namespace: str) -> dict[str, Any]:
        """Let `namespace` in `environment` clone this snapshot. Only someone who may snapshot the
        database here may hand its data on, and only to an environment that takes this one's."""
        env = self.environment(name)
        target = self.environment(environment)
        if env.name not in target.clone_from:
            raise ConsoleError(403, "no_clone", f"{target.name} takes no snapshots from {env.name}.")
        if not re.match(NAMESPACE, namespace):
            raise ConsoleError(422, "invalid_request", "That is not a namespace name.")
        _, source = await self._database(actor, env, database)
        await self._require(actor, env, source, "snapshot")
        blob = snapshot_blob(database, self._snapshot_id(snapshot))
        found = await self._storage[env.name].properties(blob)
        if found is None:
            raise ConsoleError(404, "unknown_snapshot", f"{env.name} has no snapshot {snapshot} of {database}.")
        shares = sorted(set(_shares(found.metadata)) | {f"{target.name}/{namespace}"})
        await self._storage[env.name].set_metadata(blob, {**found.metadata, "sharedwith": " ".join(shares)})
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="snapshot.shared", database=database, namespace=source, snapshot=blob, target=f"{target.name}/{namespace}")
        return self._snapshot_json(env.name, database, snapshot, found.size, found.created, {**found.metadata, "sharedwith": " ".join(shares)})

    def _snapshot_id(self, snapshot: str) -> str:
        if not re.match(SNAPSHOT_ID, snapshot):
            raise ConsoleError(404, "unknown_snapshot", "That is not a snapshot.")
        return snapshot

    async def restore(self, actor: Actor, name: str, database: str, *, source_environment: str, source_database: str, snapshot: str, into: str, reason: str) -> dict[str, Any]:
        env = self.environment(name)
        source = self.environment(source_environment)
        self._check_name(source_database)
        blob = snapshot_blob(source_database, self._snapshot_id(snapshot))
        resource, namespace = await self._database(actor, env, database)
        await self._require(actor, env, namespace, "restore")
        if resource is None:
            raise ConsoleError(409, "external", f"{database} is an external database: this app snapshots it and runs scripts on it, but restores only CloudNativePG databases.")
        if source.name != env.name and source.name not in env.clone_from:
            raise ConsoleError(403, "no_clone", f"{env.name} takes no snapshots from {source.name}.")
        if source.name == env.name and source_database != database:
            # Another database's data: what snapshotting that one takes.
            await self._require(actor, env, (await self._database(actor, env, source_database))[1], "snapshot")
        found = await self._storage[source.name].properties(blob)
        if found is None:
            raise ConsoleError(404, "unknown_snapshot", f"{source.name} has no snapshot {snapshot} of {source_database}.")
        if source.name != env.name and f"{env.name}/{namespace}" not in _shares(found.metadata):
            raise ConsoleError(403, "not_shared", f"That snapshot is not shared with {namespace} in {env.name}; someone who may snapshot it in {source.name} shares it.")
        await self._busy(env, database)
        workloads: tuple[tuple[str, str, str], ...] = ()
        if into == "copy":
            target = await self._target(env, await self._ensure_copy(env, resource, database), database + _COPY, namespace)
        else:
            workloads = self.workloads(resource, namespace)
            target = await self._target(env, resource, database, namespace)
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
            self._storage[env.name], environment=env.name, actor=actor.upn, action="restore.requested", at=now, database=database, namespace=namespace, job=job_name,
            snapshot=f"{source.name}/{blob}", target=target.database, detail=reason,
        )  # fmt: skip
        return self._job_json(env, job, None, can_approve=False, can_decline=env.production)

    async def _ensure_copy(self, env: Environment, resource: dict[str, Any], database: str) -> dict[str, Any]:
        """The Database resource for `<database>_copy`: CloudNativePG creates the database, and drops it with the resource."""
        name = f"{resource['metadata']['name']}-copy"[:253]
        path = f"{CNPG}/namespaces/{env.namespace}/databases"
        existing = await self._kube_for(env).get(f"{path}/{name}")
        if existing is not None:
            if self._copy_of(existing) != database:
                raise ConsoleError(409, "copy_taken", f"A Database resource {name} exists that this app did not create.")
            return existing
        labels = {jobs.MANAGED_BY: jobs.APP, f"{jobs.PREFIX}environment": env.name, f"{jobs.PREFIX}copy-of": database}
        if label := ((resource.get("metadata") or {}).get("labels") or {}).get(self.cfg.namespace_label):
            labels[self.cfg.namespace_label] = label
        extensions = [{k: v for k, v in e.items() if k in ("name", "version", "schema")} for e in resource["spec"].get("extensions") or [] if e.get("ensure") != "absent"]
        body = {
            "apiVersion": "postgresql.cnpg.io/v1",
            "kind": "Database",
            "metadata": {"name": name, "namespace": env.namespace, "labels": labels},
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
        resource, namespace = await self._database(actor, env, database)
        await self._require(actor, env, namespace, "restore")
        if resource is None:
            raise ConsoleError(404, "no_copy", f"{database} has no copy.")
        await self._busy(env, database)
        path = f"{CNPG}/namespaces/{env.namespace}/databases"
        copy = next(iter(await self._kube_for(env).list(path, f"{jobs.MANAGED_BY}={jobs.APP},{jobs.PREFIX}copy-of={database}")), None)
        if copy is None:
            raise ConsoleError(404, "no_copy", f"{database} has no copy.")
        await self._kube_for(env).delete(f"{path}/{copy['metadata']['name']}")
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="copy.dropped", database=database, namespace=namespace, target=copy["spec"].get("name", ""))

    async def download(self, actor: Actor, name: str, database: str, snapshot: str) -> tuple[Container, str, str]:
        """The container and blob to stream, and a file name, once the download is recorded."""
        env = self.environment(name)
        if env.production:
            raise ConsoleError(403, "no_downloads", f"Snapshots of {env.name} stay in {env.name}.")
        _, namespace = await self._database(actor, env, database)
        await self._require(actor, env, namespace, "snapshot")
        blob = snapshot_blob(database, self._snapshot_id(snapshot))
        if await self._storage[env.name].properties(blob) is None:
            raise ConsoleError(404, "unknown_snapshot", f"{env.name} has no snapshot {snapshot} of {database}.")
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action="snapshot.downloaded", database=database, namespace=namespace, snapshot=blob)
        return self._storage[env.name], blob, f"{env.name}-{database}-{snapshot}.dump"

    # ── Scripts ─────────────────────────────────────────────────────────────────────

    async def run_script(self, actor: Actor, name: str, database: str, *, script: str, mode: Literal["check", "run"], reason: str, snapshot_first: bool) -> dict[str, Any]:
        """A Job that runs `script` in one transaction as the database's owner: rolled back (check)
        or committed (run), after a snapshot when asked. In production it waits for a second person,
        who sees the script; a copy of it is kept in storage beside the snapshots."""
        env = self.environment(name)
        resource, namespace = await self._database(actor, env, database)
        await self._require(actor, env, namespace, "script")
        if found := sql.problems(script):
            raise ConsoleError(422, "script_refused", " ".join(found[:5]))
        await self._busy(env, database)
        target = await self._target(env, resource, database, namespace)
        now = self._now()
        sid = f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        job_name = jobs.name_for("script")
        first = snapshot_first and mode == "run"
        blob = script_blob(database, sid)
        _, digest = await self._storage[env.name].upload(
            blob, _once(script.encode()),
            {"createdby": actor.upn, "created": _iso(now), "reason": reason, "mode": mode, "environment": env.name, "database": database, "namespace": namespace, "job": job_name},
        )  # fmt: skip
        manifest = jobs.build(
            self.cfg, env, name=job_name, action="script", target=target, database=database, blob_url=env.storage.url,
            blob=snapshot_blob(database, sid) if first else "", requested_by=actor.upn,
            annotations={
                "requested-by": actor.upn, "requested-at": _iso(now), "reason": reason, "mode": mode, "script": sid, "script-sha256": digest,
                "snapshot": sid if first else "", "approval": "required" if env.production else "",
            },
            metadata={
                "createdby": actor.upn, "created": _iso(now), "note": f"before script {sid}: {reason}"[:300], "environment": env.name, "database": database,
                "namespace": namespace, "job": job_name,
            } if first else None,
            suspend=env.production, script=jobs.Script(text=script, mode=mode, blob=blob, snapshot_first=first),
        )  # fmt: skip
        job = await self._kube_for(env).create(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", manifest)
        await audit.record(
            self._storage[env.name], environment=env.name, actor=actor.upn, action="script.requested", at=now, database=database, namespace=namespace, job=job_name,
            script=blob, detail=f"{mode}: {reason}",
        )  # fmt: skip
        return self._job_json(env, job, None, can_approve=False, can_decline=env.production)

    # ── Jobs ────────────────────────────────────────────────────────────────────────

    def _expired(self, job: dict[str, Any]) -> bool:
        if jobs.state(job) != "waiting":
            return False
        at = _when((job["metadata"].get("annotations") or {}).get(f"{jobs.PREFIX}requested-at", ""))
        return at is None or self._now() - at > timedelta(hours=APPROVAL_HOURS)

    def _job_json(self, env: Environment, job: dict[str, Any], pod: dict[str, Any] | None, *, can_approve: bool, can_decline: bool) -> dict[str, Any]:
        meta = job.get("metadata") or {}
        a = {k.removeprefix(jobs.PREFIX): v for k, v in (meta.get("annotations") or {}).items() if k.startswith(jobs.PREFIX)}
        labels = meta.get("labels") or {}
        state = "expired" if self._expired(job) else jobs.state(job)
        status = job.get("status") or {}
        return {
            "name": meta.get("name", ""),
            "action": labels.get(f"{jobs.PREFIX}action", ""),
            "database": labels.get(f"{jobs.PREFIX}database", ""),
            "namespace": labels.get(f"{jobs.PREFIX}namespace", ""),
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
            "mode": a.get("mode", ""),
            "script": a.get("script", ""),
            "script_sha256": a.get("script-sha256", ""),
            # What the Job runs, from the Job itself: what an approver approves is what runs.
            "script_text": jobs.script_of(job),
            "started": status.get("startTime", ""),
            "finished": status.get("completionTime", "") or next((c.get("lastTransitionTime", "") for c in status.get("conditions") or [] if c.get("type") == "Failed"), ""),
            "result": jobs.result(pod),
            "can_approve": state == "waiting" and can_approve,
            "can_decline": state == "waiting" and can_decline,
        }

    def _namespace_of_job(self, env: Environment, job: dict[str, Any]) -> str:
        found = ((job.get("metadata") or {}).get("labels") or {}).get(f"{jobs.PREFIX}namespace", "")
        return found if re.match(NAMESPACE, found) else env.namespace

    async def jobs(self, actor: Actor, name: str) -> list[dict[str, Any]]:
        env = self.environment(name)
        if not env.runs_jobs:
            return []
        kube = self._kube_for(env)
        found = await kube.list(f"/apis/batch/v1/namespaces/{env.namespace}/jobs", jobs.selector(env.name))
        pods = {}
        for pod in await kube.list(f"/api/v1/namespaces/{env.namespace}/pods", jobs.selector(env.name)):
            pods[(pod.get("metadata") or {}).get("labels", {}).get("job-name", "")] = pod
        live = []
        for job in found:
            if self._expired(job):
                await self._drop(env, job, actor="databases-ui", action=f"{jobs.action_of(job)}.expired")
            else:
                live.append(job)
        spaces = [self._namespace_of_job(env, j) for j in live]
        seen = await self._answers(actor, env, spaces, "read")
        decides = {
            action: await self._answers(actor, env, (ns for ns in spaces if seen[ns]), action)  # type: ignore[arg-type]
            for action in ("restore", "script")
        }
        out = []
        for job, ns in zip(live, spaces, strict=True):
            if seen[ns]:
                mine = (job["metadata"].get("annotations") or {}).get(f"{jobs.PREFIX}requested-by") == actor.upn
                may = decides[_decides(jobs.action_of(job))][ns]
                out.append(self._job_json(env, job, pods.get(job["metadata"]["name"]), can_approve=may and not mine, can_decline=may or mine))
        return sorted(out, key=lambda j: j["requested_at"], reverse=True)

    async def _waiting(self, actor: Actor, env: Environment, job_name: str) -> tuple[dict[str, Any], str]:
        job = await self._kube_for(env).get(f"/apis/batch/v1/namespaces/{env.namespace}/jobs/{job_name}")
        labels = (job or {}).get("metadata", {}).get("labels") or {}
        if job is None or labels.get(jobs.MANAGED_BY) != jobs.APP or labels.get(f"{jobs.PREFIX}environment") != env.name:
            raise ConsoleError(404, "unknown_job", "There is no such request.")
        namespace = self._namespace_of_job(env, job)
        await self._require(actor, env, namespace, "read")
        if self._expired(job):
            raise ConsoleError(409, "expired", f"Requests wait {APPROVAL_HOURS} hours for approval; ask again.")
        if jobs.state(job) != "waiting":
            raise ConsoleError(409, "not_waiting", "This request is not waiting for approval.")
        return job, namespace

    async def approve(self, actor: Actor, name: str, job_name: str) -> dict[str, Any]:
        env = self.environment(name)
        job, namespace = await self._waiting(actor, env, job_name)
        asked_by = (job["metadata"].get("annotations") or {}).get(f"{jobs.PREFIX}requested-by", "")
        if actor.upn == asked_by:
            raise ConsoleError(403, "own_request", "A second person approves this, not the one who asked.")
        action = jobs.action_of(job)
        await self._require(actor, env, namespace, _decides(action))
        now = self._now()
        patch = {"metadata": {"annotations": {f"{jobs.PREFIX}approved-by": actor.upn, f"{jobs.PREFIX}approved-at": _iso(now)}}, "spec": {"suspend": False}}
        job = await self._kube_for(env).patch(f"/apis/batch/v1/namespaces/{env.namespace}/jobs/{job_name}", patch)
        database = (job["metadata"].get("labels") or {}).get(f"{jobs.PREFIX}database", "")
        await audit.record(self._storage[env.name], environment=env.name, actor=actor.upn, action=f"{action}.approved", at=now, database=database, namespace=namespace, job=job_name, detail=f"asked by {asked_by}")
        return self._job_json(env, job, None, can_approve=False, can_decline=False)

    async def decline(self, actor: Actor, name: str, job_name: str, reason: str) -> None:
        env = self.environment(name)
        job, namespace = await self._waiting(actor, env, job_name)
        action = jobs.action_of(job)
        if (job["metadata"].get("annotations") or {}).get(f"{jobs.PREFIX}requested-by") != actor.upn:
            await self._require(actor, env, namespace, _decides(action))
        await self._drop(env, job, actor=actor.upn, action=f"{action}.declined", detail=reason)

    async def _drop(self, env: Environment, job: dict[str, Any], *, actor: str, action: str, detail: str = "") -> None:
        meta = job["metadata"]
        labels = meta.get("labels") or {}
        await self._kube_for(env).delete(f"/apis/batch/v1/namespaces/{env.namespace}/jobs/{meta['name']}")
        await audit.record(
            self._storage[env.name], environment=env.name, actor=actor, action=action, database=labels.get(f"{jobs.PREFIX}database", ""),
            namespace=self._namespace_of_job(env, job), job=meta["name"], detail=detail,
        )  # fmt: skip

    # ── Audit ───────────────────────────────────────────────────────────────────────

    async def audit_log(self, actor: Actor, name: str, limit: int = 500) -> list[dict[str, Any]]:
        env = self.environment(name)
        container = self._storage[env.name]
        months = sorted((b.name for b in await container.list(audit.PREFIX)), reverse=True)[:2]
        entries = []
        for month in months:
            entries += audit.parse(await container.read(month) or b"")
        space = {id(e): str(e.get("namespace") or "") if re.match(NAMESPACE, str(e.get("namespace") or "")) else env.namespace for e in entries}
        seen = await self._answers(actor, env, space.values(), "read") if env.runs_jobs else {}
        visible = [e for e in entries if seen.get(space[id(e)])]
        return sorted(visible, key=lambda e: str(e.get("at", "")), reverse=True)[:limit]
