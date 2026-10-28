"""The catalog: the environments this app reaches. One YAML file.

    environments:
      - name: dev                    # an environment in this app's own cluster
        cluster: dev                 # the CloudNativePG Cluster (default: the name)
        namespace: database          # its namespace: Database resources, role Secrets and Jobs
        storage:
          url: https://<account>.blob.core.windows.net/dev-snapshots
        cloneFrom: [acc]             # environments whose snapshots may be restored here
      - name: acc
        kubernetes: none             # another cluster's: only snapshots shared with this one
        storage:
          url: https://<account>.blob.core.windows.net/acc-snapshots
      - name: prd
        production: true             # a restore or a script waits for a second person; no downloads
        access:                      # what each action takes in the database's namespace
          restore: {verb: create, group: rbac.authorization.k8s.io, resource: rolebindings}
        external:                    # PostgreSQL servers outside CloudNativePG
          - name: ledger             # what this app calls it
            namespace: prd-ledger    # whose people may act on it
            host: ledger-db.example.internal
            database: ledger
            user: ledger_owner
            passwordSecret: ledger-db  # a Secret in the environment's namespace, key `password`
            caSecret: ledger-db-ca     # its `ca.crt` verifies the server (tls: verify-full)

Who may act on a database is not here: it follows from Kubernetes RBAC in the namespace the
Database resource names in a label, or an external database names here. Everything here is plain
data with no I/O, so `check` validates the same file the server reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

NAME = r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$"
# A database name that is also a valid label value; `_copy` must still fit in PostgreSQL's 63.
DATABASE = r"^[a-z0-9]([a-z0-9_-]{0,56}[a-z0-9])?$"
NAMESPACE = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
OBJECT_NAME = r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$"
# A host name or an IPv4 address, as libpq takes it in PGHOST.
HOST = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$"
# A role or database name as PostgreSQL stores it, unquoted or not: no NUL, no control characters.
POSTGRES_NAME = r"^[^\x00-\x1f\x7f]{1,63}$"
# Plain http only for an emulator inside the cluster or on this machine.
_LOCAL = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\]|[a-z0-9.-]+\.svc(\.cluster\.local)?)$")  # DevSkim: ignore DS162092 - plain http is allowed only for a local emulator


class CatalogError(ValueError):
    """The catalog does not hold together; `problems` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Storage(_Model):
    # The container that holds the environment's snapshots and its audit log.
    url: str = Field(max_length=300)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.query or parts.fragment or parts.username or not host or len(parts.path.strip("/")) == 0:
            raise ValueError("storage.url is the container's URL: https://<account>.blob.core.windows.net/<container>")
        if parts.scheme != "https" and not (parts.scheme == "http" and _LOCAL.match(host)):
            raise ValueError("storage.url must be https (plain http only for a local emulator)")
        return value.rstrip("/")


class Check(_Model):
    """A Kubernetes permission in the database's namespace, asked as a SubjectAccessReview."""

    verb: str = Field(pattern=r"^[a-z]+$")
    resource: str = Field(pattern=r"^[a-z0-9-]+(/[a-z]+)?$")
    group: str = Field(default="", pattern=r"^$|^[a-z0-9.-]+$")


# See a namespace's databases with `view`; snapshot, restore and run scripts with `edit`; restore
# or run a script in production with `admin`. Snapshots carry all the data, so they take what
# reading Secrets takes; a script can change any of it, so it takes what a restore takes.
READ = Check(verb="list", resource="pods")
SNAPSHOT = Check(verb="get", resource="secrets")
RESTORE = Check(verb="get", resource="secrets")
RESTORE_PRODUCTION = Check(verb="create", resource="rolebindings", group="rbac.authorization.k8s.io")
SCRIPT = RESTORE
SCRIPT_PRODUCTION = RESTORE_PRODUCTION


class Access(_Model):
    read: Check | None = None
    snapshot: Check | None = None
    restore: Check | None = None
    script: Check | None = None


class External(_Model):
    """A PostgreSQL database outside CloudNativePG: any server the environment's pods reach.

    The Jobs log in as `user` with the password in `passwordSecret` (key `password`) in the
    environment's namespace, so that role is what a script can do: give it what the scripts need
    (the schema's owner), not a superuser. Snapshots and scripts work as for any database; a
    restore, which drops everything the role owns first, does not run here.
    """

    name: str = Field(pattern=DATABASE)
    # The app's namespace, whose people may act on it; empty: the environment's own (the platform's).
    namespace: str = Field(default="", pattern=r"^$|" + NAMESPACE)
    host: str = Field(max_length=253, pattern=HOST)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(pattern=POSTGRES_NAME)
    user: str = Field(pattern=POSTGRES_NAME)
    password_secret: str = Field(alias="passwordSecret", pattern=OBJECT_NAME)
    # verify-full checks the server's certificate and name: against `ca.crt` in caSecret, or the
    # image's public roots without one. require encrypts without checking; disable, for a server
    # without TLS on a network you trust.
    tls: Literal["verify-full", "require", "disable"] = "verify-full"
    ca_secret: str = Field(default="", alias="caSecret", pattern=r"^$|" + OBJECT_NAME)

    @model_validator(mode="after")
    def _ca(self) -> External:
        if self.ca_secret and self.tls != "verify-full":
            raise ValueError(f"external database {self.name!r}: caSecret is for tls: verify-full")
        return self


class Environment(_Model):
    name: str = Field(pattern=NAME)
    production: bool = False
    cluster: str = Field(default="", pattern=r"^$|^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$")
    namespace: str = Field(default="database", pattern=NAMESPACE)
    # in-cluster: this app's own cluster. none: another cluster's, whose snapshots are shared here.
    kubernetes: Literal["in-cluster", "none"] = "in-cluster"
    storage: Storage
    clone_from: list[str] = Field(default_factory=list, alias="cloneFrom")
    access: Access = Field(default_factory=Access)
    external: list[External] = Field(default_factory=list)

    @model_validator(mode="after")
    def _external(self) -> Environment:
        names = [d.name for d in self.external]
        problems = [f"environment {self.name!r} lists the external database {n!r} twice" for n in sorted({n for n in names if names.count(n) > 1})]
        if self.external and not self.runs_jobs:
            problems.append(f"environment {self.name!r} is another cluster's (kubernetes: none), so no Job here reaches its external databases")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    @property
    def cnpg_cluster(self) -> str:
        return self.cluster or self.name

    @property
    def runs_jobs(self) -> bool:
        return self.kubernetes != "none"

    def server(self, name: str) -> External | None:
        """The external database by this app's name for it."""
        return next((d for d in self.external if d.name == name), None)

    def check(self, action: Literal["read", "snapshot", "restore", "script"]) -> Check:
        found = getattr(self.access, action)
        if found is not None:
            return found
        return {
            "read": READ,
            "snapshot": SNAPSHOT,
            "restore": RESTORE_PRODUCTION if self.production else RESTORE,
            "script": SCRIPT_PRODUCTION if self.production else SCRIPT,
        }[action]


class _File(_Model):
    environments: list[Environment] = Field(min_length=1)

    @model_validator(mode="after")
    def _references(self) -> _File:
        names = [e.name for e in self.environments]
        problems = [f"environment {n!r} is listed twice" for n in sorted({n for n in names if names.count(n) > 1})]
        for env in self.environments:
            problems += [f"environment {env.name!r} clones from unknown environment {s!r}" for s in env.clone_from if s not in names]
            if env.name in env.clone_from:
                problems.append(f"environment {env.name!r} lists itself in cloneFrom")
        if not any(e.runs_jobs for e in self.environments):
            problems.append("no environment runs here: at least one has kubernetes: in-cluster")
        if problems:
            raise ValueError("; ".join(problems))
        return self


@dataclass(frozen=True)
class Catalog:
    environments: dict[str, Environment]

    def cloning_from(self, source: str) -> list[str]:
        """The environments that take `source`'s snapshots."""
        return [e.name for e in self.environments.values() if source in e.clone_from]


def load_catalog(text: str, source: str = "catalog") -> Catalog:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise CatalogError([f"{source}: not YAML: {err}"]) from err
    try:
        parsed = _File.model_validate(data if isinstance(data, dict) else {})
    except ValidationError as err:
        problems = []
        for e in err.errors():
            where = ".".join(str(p) for p in e.get("loc", ()))
            problems.append(f"{source}: {where + ': ' if where else ''}{e.get('msg', '')}".replace("Value error, ", ""))
        raise CatalogError(problems) from err
    return Catalog(environments={e.name: e for e in parsed.environments})
