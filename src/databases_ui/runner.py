"""What a Job's pod does: `python -m databases_ui job`, configured by the variables the server put
in the Job (jobs.py).

snapshot  pg_dump in the custom format, streamed into a block blob that is committed, with who
          asked and why as its metadata, only once pg_dump has succeeded.
restore   the snapshot streamed into pg_restore in one transaction, so on any error nothing
          changes. In place, the workloads that use the database go to 0 replicas first and back
          to what they had afterwards; for a copy, it first waits for the database to exist.

Nothing goes through `kubectl exec` or a disk. The outcome goes to the pod's termination
message, which the server shows, and to the environment's audit log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess  # nosec B404 - runs pg_dump, pg_restore and psql with fixed arguments, no shell
import time
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from . import audit
from .blob import BlobError, Container
from .entra import AppToken, federated_credential
from .kube import Kube, KubeError
from .oidc import SignInError

log = logging.getLogger("databases_ui")
TERMINATION_LOG = Path("/dev/termination-log")
SCALED = "databases-ui/replicas-before-restore"
READ = 1024 * 1024
WAIT_SECONDS = 300
REACH_SECONDS = 60


class JobFailed(Exception):
    pass


def _say(msg: str, **fields: Any) -> None:
    log.info(json.dumps({"msg": msg, **fields}))


def _tail(text: bytes) -> str:
    lines = [line for line in text.decode(errors="replace").splitlines() if line.strip()]
    return " | ".join(lines[-6:])[-900:]


async def _exec(*args: str, stdin: int | None = None) -> asyncio.subprocess.Process:
    # The arguments are fixed here; the connection comes from the PG* variables.
    return await asyncio.create_subprocess_exec(*args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


async def psql(sql: str) -> str:
    proc = await _exec("psql", "--no-password", "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-Atqc", sql)
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise JobFailed(f"psql: {_tail(err)}")
    return out.decode().strip()


def _container(env: Mapping[str, str], url: str, http: httpx.AsyncClient) -> Container:
    if env.get("DBUI_STORAGE_SAS"):
        return Container(http, url, sas=env["DBUI_STORAGE_SAS"])
    authority = env.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/")
    token = AppToken(
        http,
        token_url=f"{authority}/{env['AZURE_TENANT_ID']}/oauth2/v2.0/token",
        client_id=env["AZURE_CLIENT_ID"],
        credential=lambda: federated_credential(env["AZURE_FEDERATED_TOKEN_FILE"]),
    )
    return Container(http, url, token=token)


async def snapshot(env: Mapping[str, str], container: Container) -> dict[str, Any]:
    await wait_for_database(REACH_SECONDS)
    proc = await _exec("pg_dump", "--format=custom", "--no-password", "--exclude-extension=*")
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:
        raise JobFailed("pg_dump started without its pipes")
    errors = asyncio.create_task(stderr.read())

    async def chunks() -> AsyncIterator[bytes]:
        while data := await stdout.read(READ):
            yield data

    async def finished() -> None:
        if await proc.wait() != 0:
            raise JobFailed(f"pg_dump exited {proc.returncode}: {_tail(await errors)}")

    metadata = json.loads(env.get("DBUI_METADATA") or "{}")
    try:
        size, digest = await container.upload(env["DBUI_BLOB"], chunks(), metadata, before_commit=finished)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return {"bytes": size, "sha256": digest}


async def wait_for_database(seconds: float) -> None:
    """Until the database answers: a new pod's network rules, a failover or a new copy take a moment."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            await psql("select 1")
            return
        except JobFailed as err:
            if time.monotonic() > deadline:
                raise JobFailed(f"the database never became reachable: {err}") from err
        await asyncio.sleep(3)


async def scale_down(kube: Kube, workloads: list[list[str]], scaled: list[tuple[str, int]]) -> None:
    """Each workload to 0, its count kept in an annotation and in `scaled` as it goes, so a failure
    halfway still scales back what was scaled; then wait for its pods to go."""
    pods: list[tuple[str, str]] = []
    for kind, namespace, name in workloads:
        path = f"/apis/apps/v1/namespaces/{namespace}/{kind}s/{name}"
        found = await kube.get(path)
        if found is None:
            _say("workload not found, left alone", workload=f"{kind}/{namespace}/{name}")
            continue
        # A restore that died before scaling back left the real count here.
        kept = ((found.get("metadata") or {}).get("annotations") or {}).get(SCALED)
        replicas = int(kept) if kept and kept.isdigit() else int((found.get("spec") or {}).get("replicas", 1))
        await kube.patch(path, {"metadata": {"annotations": {SCALED: str(replicas)}}, "spec": {"replicas": 0}})
        scaled.append((path, replicas))
        labels = ((found.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        pods.append((f"/api/v1/namespaces/{namespace}/pods", ",".join(f"{k}={v}" for k, v in sorted(labels.items()))))
        _say("scaled down", workload=f"{kind}/{namespace}/{name}", replicas=replicas)
    deadline = time.monotonic() + WAIT_SECONDS
    for path, labels in pods:
        while labels and await kube.list(path, labels):
            if time.monotonic() > deadline:
                raise JobFailed(f"pods matching {labels} were still running after {WAIT_SECONDS}s")
            await asyncio.sleep(3)


async def scale_up(kube: Kube, scaled: list[tuple[str, int]]) -> None:
    for path, replicas in scaled:
        try:
            await kube.patch(path, {"metadata": {"annotations": {SCALED: None}}, "spec": {"replicas": replicas}})
            _say("scaled back", workload=path, replicas=replicas)
        except KubeError as err:
            _say("could not scale back", workload=path, replicas=replicas, reason=str(err))


async def restore(env: Mapping[str, str], container: Container, kube: Kube | None) -> dict[str, Any]:
    await wait_for_database(WAIT_SECONDS if env.get("DBUI_WAIT_FOR_DATABASE") else REACH_SECONDS)
    scaled: list[tuple[str, int]] = []
    try:
        if kube is not None:
            await scale_down(kube, json.loads(env["DBUI_SCALE"]), scaled)
        ended = await psql("select count(pg_terminate_backend(pid)) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid() and usename = current_user")
        _say("sessions ended", count=ended)
        async with container.download(env["DBUI_BLOB"]) as (size, chunks):
            proc = await _exec(
                "pg_restore", "--format=custom", "--clean", "--if-exists", "--no-owner", "--no-acl", "--single-transaction", "--no-password",
                f"--dbname={env['PGDATABASE']}", stdin=subprocess.PIPE,
            )  # fmt: skip
            stdin, stderr = proc.stdin, proc.stderr
            if stdin is None or stderr is None:
                raise JobFailed("pg_restore started without its pipes")
            errors = asyncio.create_task(stderr.read())
            fed = 0
            try:
                async for chunk in chunks:
                    stdin.write(chunk)
                    await stdin.drain()
                    fed += len(chunk)
                stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass  # pg_restore stopped reading; its exit status says why
            if await proc.wait() != 0:
                raise JobFailed(f"pg_restore exited {proc.returncode}, nothing changed: {_tail(await errors)}")
            if fed != size:
                raise JobFailed(f"read {fed} of {size} bytes of the snapshot")
    finally:
        if kube is not None:
            await scale_up(kube, scaled)
    return {"bytes": size}


async def run(env: Mapping[str, str], *, transport: httpx.AsyncBaseTransport | None = None, kube_transport: httpx.AsyncBaseTransport | None = None) -> int:
    action = env.get("DBUI_ACTION", "")
    started = time.monotonic()
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as http:
        try:
            source = _container(env, env.get("DBUI_BLOB_URL", ""), http)
            if action == "snapshot":
                outcome = {"result": "succeeded", **await snapshot(env, source)}
            elif action == "restore":
                kube = Kube.in_cluster(transport=kube_transport) if env.get("DBUI_SCALE") else None
                try:
                    outcome = {"result": "succeeded", **await restore(env, source, kube)}
                finally:
                    if kube is not None:
                        await kube.aclose()
            else:
                raise JobFailed(f"unknown action {action!r}")
        except (JobFailed, BlobError, KubeError, SignInError, KeyError, ValueError, OSError) as err:
            outcome = {"result": "failed", "reason": str(err)[:1500] or err.__class__.__name__}
        outcome["seconds"] = round(time.monotonic() - started)
        try:
            # The environment's own container, which for a clone is not the snapshot's.
            await audit.record(
                _container(env, env["DBUI_AUDIT_URL"], http), environment=env.get("DBUI_ENVIRONMENT", ""), actor=env.get("DBUI_REQUESTED_BY", ""), action=f"{action}.{outcome['result']}",
                database=env.get("DBUI_DATABASE", ""), job=env.get("DBUI_JOB", ""), snapshot=env.get("DBUI_BLOB", ""), detail=outcome.get("reason", ""),
            )  # fmt: skip
        except (SignInError, KeyError) as err:
            _say("audit write failed", reason=str(err))
    _say("finished", action=action, **outcome)
    try:
        TERMINATION_LOG.write_text(json.dumps(outcome)[:4000])
    except OSError:
        pass  # outside a pod
    return 0 if outcome["result"] == "succeeded" else 1


def main() -> int:
    return asyncio.run(run(dict(os.environ)))
