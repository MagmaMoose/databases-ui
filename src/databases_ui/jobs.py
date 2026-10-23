"""The Kubernetes Jobs that do the work, and what their state means.

Every action is a Job in the environment's namespace, running this app's own image as
`python -m databases_ui job` (runner.py). Its pod connects as the database's owner role with the
password from that managed role's Secret, mounted by the kubelet: neither this server nor its
Kubernetes role ever reads a password. A restore that needs a second person is created
suspended; approving it lets it start, and a Job's pod template can't change after creation, so
what runs is exactly what was asked for.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from .catalog import Environment
from .config import Config

PREFIX = "databases-ui/"
MANAGED_BY = "app.kubernetes.io/managed-by"
APP = "databases-ui"
TOKEN_DIR = "/var/run/secrets/azure/tokens"  # nosec B105 - where the kubelet mounts the token, not a token
DEADLINE_SECONDS = 6 * 3600
KEEP_SECONDS = 7 * 86400


@dataclass(frozen=True)
class Target:
    """A database as its owner role reaches it."""

    environment: str
    database: str
    owner: str
    password_secret: str
    ca_secret: str
    host: str
    # The app's namespace, whose people may act on the database.
    namespace: str


def name_for(action: str) -> str:
    return f"dbui-{action}-{secrets.token_hex(4)}"


def selector(env: str, database: str = "") -> str:
    return f"{MANAGED_BY}={APP},{PREFIX}environment={env}" + (f",{PREFIX}database={database}" if database else "")


def _storage_env(cfg: Config) -> list[dict[str, Any]]:
    if cfg.storage_sas_secret:
        secret, key = cfg.storage_sas_secret.split("/", 1)
        return [{"name": "DBUI_STORAGE_SAS", "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}}]
    return [
        {"name": "AZURE_TENANT_ID", "value": cfg.tenant_id},
        {"name": "AZURE_CLIENT_ID", "value": cfg.storage_client_id or cfg.client_id},
        {"name": "AZURE_AUTHORITY_HOST", "value": cfg.authority},
        {"name": "AZURE_FEDERATED_TOKEN_FILE", "value": f"{TOKEN_DIR}/azure-identity-token"},
    ]


def build(
    cfg: Config,
    env: Environment,
    *,
    name: str,
    action: str,
    target: Target,
    database: str,
    blob_url: str,
    blob: str,
    requested_by: str,
    annotations: dict[str, str],
    suspend: bool = False,
    metadata: dict[str, str] | None = None,
    workloads: tuple[tuple[str, str, str], ...] = (),
    wait_for_database: bool = False,
) -> dict[str, Any]:
    """The Job for one action. `database` is the catalog's name, `target.database` the one written to."""
    labels = {MANAGED_BY: APP, f"{PREFIX}environment": env.name, f"{PREFIX}database": database, f"{PREFIX}namespace": target.namespace, f"{PREFIX}action": action}
    variables: list[dict[str, Any]] = [
        {"name": "DBUI_ACTION", "value": action},
        {"name": "DBUI_JOB", "value": name},
        {"name": "DBUI_ENVIRONMENT", "value": env.name},
        {"name": "DBUI_DATABASE", "value": database},
        {"name": "DBUI_NAMESPACE", "value": target.namespace},
        {"name": "DBUI_REQUESTED_BY", "value": requested_by},
        {"name": "DBUI_BLOB_URL", "value": blob_url},
        {"name": "DBUI_BLOB", "value": blob},
        {"name": "DBUI_AUDIT_URL", "value": env.storage.url},
        {"name": "PGHOST", "value": target.host},
        {"name": "PGPORT", "value": "5432"},
        {"name": "PGDATABASE", "value": target.database},
        {"name": "PGUSER", "value": target.owner},
        {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {"name": target.password_secret, "key": "password"}}},
        {"name": "PGSSLMODE", "value": "verify-full"},
        {"name": "PGSSLROOTCERT", "value": "/etc/databases-ui/ca/ca.crt"},
        {"name": "PGAPPNAME", "value": "databases-ui"},
        {"name": "PGCONNECT_TIMEOUT", "value": "15"},
        {"name": "HOME", "value": "/tmp"},  # nosec B108 - the pod's own emptyDir, mounted below
        *_storage_env(cfg),
    ]
    if metadata:
        variables.append({"name": "DBUI_METADATA", "value": json.dumps(metadata)})
    if workloads:
        variables.append({"name": "DBUI_SCALE", "value": json.dumps([list(w) for w in workloads])})
    if wait_for_database:
        variables.append({"name": "DBUI_WAIT_FOR_DATABASE", "value": "1"})
    volumes: list[dict[str, Any]] = [
        {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
        # The server's CA certificate only: the Secret also holds the CA's key.
        {"name": "ca", "secret": {"secretName": target.ca_secret, "defaultMode": 0o440, "items": [{"key": "ca.crt", "path": "ca.crt"}]}},
    ]
    mounts = [{"name": "tmp", "mountPath": "/tmp"}, {"name": "ca", "mountPath": "/etc/databases-ui/ca", "readOnly": True}]  # nosec B108 - an emptyDir no other pod shares
    if not cfg.storage_sas_secret:
        volumes.append(
            {
                "name": "azure-identity-token",
                "projected": {"defaultMode": 0o440, "sources": [{"serviceAccountToken": {"path": "azure-identity-token", "audience": "api://AzureADTokenExchange", "expirationSeconds": 3600}}]},
            }
        )
        mounts.append({"name": "azure-identity-token", "mountPath": TOKEN_DIR, "readOnly": True})
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": env.namespace, "labels": labels, "annotations": {f"{PREFIX}{k}": v for k, v in annotations.items() if v}},
        "spec": {
            "suspend": suspend,
            "backoffLimit": 0,
            "activeDeadlineSeconds": DEADLINE_SECONDS,
            "ttlSecondsAfterFinished": KEEP_SECONDS,
            "template": {
                "metadata": {"labels": {**labels, "app.kubernetes.io/name": APP, "app.kubernetes.io/component": "job"}},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": cfg.job_service_account,
                    # Only a restore in place talks to the Kubernetes API, to scale the workloads.
                    "automountServiceAccountToken": bool(workloads),
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [
                        {
                            "name": "job",
                            "image": cfg.job_image,
                            "imagePullPolicy": "IfNotPresent",
                            "args": ["job"],
                            "env": variables,
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                            "terminationMessagePolicy": "FallbackToLogsOnError",
                            "volumeMounts": mounts,
                        }
                    ],
                    "volumes": volumes,
                },
            },
        },
    }


def state(job: dict[str, Any]) -> str:
    """waiting (for approval), starting, running, succeeded or failed."""
    status = job.get("status") or {}
    conditions = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
    if conditions.get("Complete") == "True" or conditions.get("SuccessCriteriaMet") == "True":
        return "succeeded"
    if conditions.get("Failed") == "True" or conditions.get("FailureTarget") == "True":
        return "failed"
    if (job.get("spec") or {}).get("suspend"):
        return "waiting"
    return "running" if status.get("active") else "starting"


def result(pod: dict[str, Any] | None) -> dict[str, Any]:
    """What the pod reported in its termination message, or the reason it never ran."""
    if pod is None:
        return {}
    for c in (pod.get("status") or {}).get("containerStatuses") or []:
        terminated = (c.get("state") or {}).get("terminated")
        if terminated:
            try:
                found = json.loads(terminated.get("message") or "")
                if isinstance(found, dict):
                    return found
            except ValueError:
                pass
            return {"reason": (terminated.get("message") or terminated.get("reason") or "").strip()[-1000:]}
        waiting = (c.get("state") or {}).get("waiting")
        if waiting and waiting.get("reason") not in (None, "ContainerCreating", "PodInitializing"):
            return {"reason": f"{waiting.get('reason')}: {waiting.get('message', '')}".strip(": ")[:500]}
    return {}
