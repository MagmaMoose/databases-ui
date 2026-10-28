"""What a Job's pod does: `python -m databases_ui job`, configured by the variables the server put
in the Job (jobs.py).

snapshot  pg_dump in the custom format, streamed into a block blob that is committed, with who
          asked and why as its metadata, only once pg_dump has succeeded.
restore   in one transaction, everything the owner role owns is dropped and the snapshot's
          script, from pg_restore, runs: the database ends up as it was when the snapshot was
          taken, or on any error as it was before. In place, the workloads that use it go to 0
          replicas first and back afterwards; for a copy, it first waits for the database.
script    someone's SQL, in one transaction that psql opens and, once every statement has
          succeeded, commits (run) or rolls back (check); on the first error nothing changes. A
          lock it can't get within LOCK_TIMEOUT fails the script, rather than queueing the app's
          own queries behind it. Optionally a snapshot first, as above.

Nothing goes through `kubectl exec` or a disk. The outcome goes to the pod's termination
message, which the server shows, and to the environment's audit log.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess  # nosec B404 - runs pg_dump, pg_restore and psql with fixed arguments, no shell
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx

from . import audit, sql
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
LOCK_TIMEOUT = "15s"
# What of a script's output reaches the termination message, which Kubernetes caps at 4096 bytes.
OUTPUT_CHARS = 2400
OUTPUT_LINES = 200
# Production output: command tags (UPDATE 3) and psql's messages, never a row.
_TAG = re.compile(
    r"^(?:(?:INSERT \d+ \d+|UPDATE \d+|DELETE \d+|SELECT \d+|MERGE \d+|COPY \d+|FETCH \d+|MOVE \d+)"
    r"|(?:CREATE|ALTER|DROP|COMMENT|GRANT|REVOKE|SET|RESET|DO|CALL|TRUNCATE|LOCK|ANALYZE|VACUUM|REINDEX|CLUSTER|REFRESH|BEGIN|COMMIT|ROLLBACK"
    r"|SECURITY|IMPORT|NOTIFY|LISTEN|UNLISTEN|DISCARD|PREPARE|EXECUTE|DEALLOCATE)(?: [A-Z ]+)?)$"
)
_MESSAGE = re.compile(r"^(?:psql:|ERROR:|WARNING:|NOTICE:|INFO:|DETAIL:|HINT:|CONTEXT:|QUERY:|LINE \d+:|\s+\^)")


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


def _stop(*procs: asyncio.subprocess.Process) -> None:
    for proc in procs:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()


async def apply(chunks: AsyncIterator[bytes], database: str) -> None:
    """The snapshot into `database` in one transaction: everything the owner role owns is dropped,
    then pg_restore's script runs, and COMMIT follows only once pg_restore has succeeded. On any
    error psql stops, its connection closes and PostgreSQL rolls the lot back."""
    run = await asyncio.create_subprocess_exec(
        "psql", "--no-password", "--no-psqlrc", "--quiet", "-v", "ON_ERROR_STOP=1", f"--dbname={database}", "--file=-",
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )  # fmt: skip
    script = await asyncio.create_subprocess_exec(
        "pg_restore", "--format=custom", "--clean", "--if-exists", "--no-owner", "--no-acl", "--file=-",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )  # fmt: skip
    if run.stdin is None or run.stderr is None or script.stdin is None or script.stdout is None or script.stderr is None:
        raise JobFailed("psql or pg_restore started without its pipes")
    run_errors, script_errors = asyncio.create_task(run.stderr.read()), asyncio.create_task(script.stderr.read())
    to_psql, to_restore, from_restore = run.stdin, script.stdin, script.stdout

    async def feed() -> None:
        try:
            async for chunk in chunks:
                to_restore.write(chunk)
                await to_restore.drain()
            to_restore.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # pg_restore stopped reading; its exit status says why
        except BaseException:
            # The download broke off: psql goes before it sees COMMIT, so nothing changes.
            _stop(run, script)
            raise

    async def relay() -> None:
        try:
            to_psql.write(b"BEGIN;\nDROP OWNED BY CURRENT_USER;\n")
            while data := await from_restore.read(READ):
                to_psql.write(data)
                await to_psql.drain()
            if await script.wait() == 0:
                to_psql.write(b"\nCOMMIT;\n")
                await to_psql.drain()
            to_psql.close()
        except (BrokenPipeError, ConnectionResetError):
            # psql stopped on an error: pg_restore must not block on a full pipe.
            _stop(script)

    await asyncio.gather(feed(), relay())
    if await script.wait() != 0:
        await run.wait()
        raise JobFailed(f"pg_restore exited {script.returncode}, nothing changed: {_tail(await script_errors)}")
    if await run.wait() != 0:
        raise JobFailed(f"the restore failed, nothing changed: {_tail(await run_errors)}")


async def restore(env: Mapping[str, str], container: Container, kube: Kube | None) -> dict[str, Any]:
    await wait_for_database(WAIT_SECONDS if env.get("DBUI_WAIT_FOR_DATABASE") else REACH_SECONDS)
    scaled: list[tuple[str, int]] = []
    try:
        if kube is not None:
            await scale_down(kube, json.loads(env["DBUI_SCALE"]), scaled)
        ended = await psql("select count(pg_terminate_backend(pid)) from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid() and usename = current_user")
        _say("sessions ended", count=ended)
        async with container.download(env["DBUI_BLOB"]) as (size, chunks):
            await apply(chunks, env["PGDATABASE"])
    finally:
        if kube is not None:
            await scale_up(kube, scaled)
    return {"bytes": size}


class ScriptFailed(JobFailed):
    """A script psql stopped, with what it said."""

    def __init__(self, message: str, outcome: dict[str, Any]):
        super().__init__(message)
        self.outcome = outcome


def _shown(line: str) -> bool:
    return bool(_TAG.match(line) or _MESSAGE.match(line))


async def apply_script(text: str, *, commit: bool, full: bool) -> tuple[int, str]:
    """psql's exit status and the tail of what it said, running `text` between BEGIN and COMMIT
    (or ROLLBACK). ON_ERROR_STOP ends psql at the first error, before either: its connection
    closes and PostgreSQL rolls everything back. The script's line numbers are psql's own, as
    `psql:<stdin>:<line>:`."""
    proc = await asyncio.create_subprocess_exec(
        "psql", "--no-password", "--no-psqlrc", "-v", "ON_ERROR_STOP=1",
        "-c", "BEGIN", "-c", f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'", "--file=-", "-c", "COMMIT" if commit else "ROLLBACK",
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    if proc.stdin is None or proc.stdout is None:
        raise JobFailed("psql started without its pipes")
    to_psql, from_psql = proc.stdin, proc.stdout
    kept: deque[str] = deque(maxlen=OUTPUT_LINES)

    async def feed() -> None:
        try:
            to_psql.write(text.encode())
            await to_psql.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # psql stopped on an error; its exit status says so
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                to_psql.close()

    async def read() -> None:
        # In chunks rather than lines: a row can be longer than any line limit.
        rest = b""
        while data := await from_psql.read(READ):
            *lines, rest = (rest + data).split(b"\n")
            rest = rest[-4096:]
            for raw in lines:
                line = raw.decode(errors="replace").rstrip("\r")
                if full or _shown(line):
                    kept.append(line[:500])
        if rest and (full or _shown(rest.decode(errors="replace"))):
            kept.append(rest.decode(errors="replace")[:500])

    await asyncio.gather(feed(), read())
    status = await proc.wait()
    return status, "\n".join(kept)[-OUTPUT_CHARS:]


async def script(env: Mapping[str, str], container: Container) -> dict[str, Any]:
    text, mode = env["DBUI_SCRIPT"], env.get("DBUI_SCRIPT_MODE", "check")
    # The server checked it too; a Job is only what it was built to be, but this costs nothing.
    if found := sql.problems(text):
        raise JobFailed(" ".join(found[:3]))
    await wait_for_database(REACH_SECONDS)
    outcome: dict[str, Any] = {"mode": mode}
    if env.get("DBUI_SNAPSHOT_FIRST"):
        taken = await snapshot(env, container)
        outcome["bytes"] = taken["bytes"]
        _say("snapshot taken before the script", blob=env.get("DBUI_BLOB", ""), bytes=taken["bytes"])
    status, output = await apply_script(text, commit=mode == "run", full=env.get("DBUI_SCRIPT_OUTPUT") == "full")
    outcome["output"] = output
    if status != 0:
        error = next((line for line in output.splitlines() if "ERROR:" in line), "") or (output.splitlines() or [f"psql exited {status}"])[-1]
        raise ScriptFailed(f"nothing changed: {error.strip()}"[:600], outcome)
    return outcome


def termination_message(outcome: dict[str, Any]) -> str:
    """The outcome as JSON within Kubernetes' 4096 bytes: the output's tail gives way first."""
    message = json.dumps(outcome)
    while len(message.encode()) > 4000 and outcome.get("output"):
        outcome["output"] = outcome["output"][len(outcome["output"]) // 4 :]
        message = json.dumps(outcome)
    return message[:4000]


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
            elif action == "script":
                outcome = {"result": "succeeded", **await script(env, source)}
            else:
                raise JobFailed(f"unknown action {action!r}")
        except ScriptFailed as err:
            outcome = {"result": "failed", "reason": str(err), **err.outcome}
        except (JobFailed, BlobError, KubeError, SignInError, KeyError, ValueError, OSError) as err:
            outcome = {"result": "failed", "reason": str(err)[:1500] or err.__class__.__name__}
        outcome["seconds"] = round(time.monotonic() - started)
        # A check that went through rolled back: it is recorded as checked, not as a change.
        ended = "checked" if action == "script" and outcome["result"] == "succeeded" and outcome.get("mode") == "check" else outcome["result"]
        try:
            # The environment's own container, which for a clone is not the snapshot's.
            await audit.record(
                _container(env, env["DBUI_AUDIT_URL"], http), environment=env.get("DBUI_ENVIRONMENT", ""), actor=env.get("DBUI_REQUESTED_BY", ""), action=f"{action}.{ended}",
                database=env.get("DBUI_DATABASE", ""), namespace=env.get("DBUI_NAMESPACE", ""), job=env.get("DBUI_JOB", ""), snapshot=env.get("DBUI_BLOB", ""),
                script=env.get("DBUI_SCRIPT_BLOB", ""), detail=outcome.get("reason", ""),
            )  # fmt: skip
        except (SignInError, KeyError) as err:
            _say("audit write failed", reason=str(err))
    _say("finished", action=action, **{k: v for k, v in outcome.items() if k != "output"})
    try:
        TERMINATION_LOG.write_text(termination_message(outcome))
    except OSError:
        pass  # outside a pod
    return 0 if outcome["result"] == "succeeded" else 1


def main() -> int:
    return asyncio.run(run(dict(os.environ)))
