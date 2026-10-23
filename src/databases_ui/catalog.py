"""The catalog: the environments this app reaches, and which team acts on which database. One
YAML file.

    environments:
      - name: dev
        cluster: dev                 # the CloudNativePG Cluster (default: the name)
        namespace: database          # its namespace: Database resources, Secrets and Jobs
        storage:
          url: https://<account>.blob.core.windows.net/dev-snapshots
        cloneFrom: [acc]             # environments whose snapshots may be restored here
      - name: prd
        production: true             # a restore waits for a second person; no downloads
        ...
    teams:
      - id: payments
        name: Payments
        groups: [<Entra ID group object id>]
        databases:
          - name: payments           # the Database resource's spec.name; {env} is the environment
            workloads: [deployment/{env}-payments/api]   # scaled to 0 for a restore in place

Everything here is plain data with no I/O, so `check` validates the same file the server reads.
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
_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_WORKLOAD = re.compile(r"^(deployment|statefulset)/([a-z0-9{}][a-z0-9{}-]{0,62})/([a-z0-9{}][a-z0-9.{}-]{0,252})$")
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


class Kubernetes(_Model):
    """Another cluster's API server, with a CA and a token file read on every call."""

    server: str = Field(pattern=r"^https://\S+$")
    ca_file: str = Field(alias="caFile", min_length=1)
    token_file: str = Field(alias="tokenFile", min_length=1)


class Environment(_Model):
    name: str = Field(pattern=NAME)
    production: bool = False
    cluster: str = Field(default="", pattern=r"^$|^[a-z0-9]([a-z0-9-]{0,48}[a-z0-9])?$")
    namespace: str = Field(default="database", pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
    # in-cluster: this app's own cluster. none: snapshots only, to clone from; nothing runs there.
    kubernetes: Literal["in-cluster", "none"] | Kubernetes = "in-cluster"
    storage: Storage
    clone_from: list[str] = Field(default_factory=list, alias="cloneFrom")

    @property
    def cnpg_cluster(self) -> str:
        return self.cluster or self.name

    @property
    def runs_jobs(self) -> bool:
        return self.kubernetes != "none"


class DatabaseRef(_Model):
    name: str = Field(min_length=1, max_length=63)
    # Empty: every environment.
    environments: list[str] = Field(default_factory=list)
    workloads: list[str] = Field(default_factory=list)

    @field_validator("workloads")
    @classmethod
    def _workloads(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _WORKLOAD.match(value):
                raise ValueError(f"{value!r} is not deployment/<namespace>/<name> or statefulset/<namespace>/<name>")
        return values


class Team(_Model):
    id: str = Field(pattern=NAME)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    # Entra ID group object ids; their members act on the team's databases.
    groups: list[str] = Field(min_length=1)
    databases: list[DatabaseRef] = Field(default_factory=list)

    @field_validator("groups")
    @classmethod
    def _groups(cls, values: list[str]) -> list[str]:
        for value in values:
            if not _GUID.match(value.lower()):
                raise ValueError(f"{value!r} is not a group object id (a GUID)")
        return [v.lower() for v in values]


class _File(_Model):
    environments: list[Environment] = Field(min_length=1)
    teams: list[Team] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> _File:
        names = [e.name for e in self.environments]
        problems = [f"environment {n!r} is listed twice" for n in sorted({n for n in names if names.count(n) > 1})]
        for env in self.environments:
            problems += [f"environment {env.name!r} clones from unknown environment {s!r}" for s in env.clone_from if s not in names]
            if env.name in env.clone_from:
                problems.append(f"environment {env.name!r} lists itself in cloneFrom")
        ids = [t.id for t in self.teams]
        problems += [f"team {i!r} is listed twice" for i in sorted({i for i in ids if ids.count(i) > 1})]
        for team in self.teams:
            for ref in team.databases:
                problems += [f"team {team.id!r} names unknown environment {e!r}" for e in ref.environments if e not in names]
                for env in ref.environments or names:
                    if not re.match(DATABASE, ref.name.replace("{env}", env)):
                        problems.append(f"team {team.id!r}: {ref.name.replace('{env}', env)!r} is not a database name this app takes (a-z, 0-9, _ and -, at most 58)")
        if problems:
            raise ValueError("; ".join(problems))
        return self


@dataclass(frozen=True)
class Grant:
    """A database a team acts on in one environment."""

    team: str
    environment: str
    database: str
    workloads: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class Catalog:
    environments: dict[str, Environment]
    teams: dict[str, Team]
    grants: tuple[Grant, ...]

    @property
    def groups(self) -> frozenset[str]:
        """Every group the catalog names: the only ones a session needs to carry."""
        return frozenset(g for t in self.teams.values() for g in t.groups)

    def teams_of(self, groups: frozenset[str]) -> list[Team]:
        return [t for t in self.teams.values() if groups & set(t.groups)]

    def grant(self, team_ids: set[str], environment: str, database: str) -> Grant | None:
        return next((g for g in self.grants if g.team in team_ids and g.environment == environment and g.database == database), None)

    def databases(self, environment: str, team_ids: set[str] | None = None) -> dict[str, Grant]:
        """The databases of `environment` the catalog names, for these teams or for any."""
        found: dict[str, Grant] = {}
        for g in self.grants:
            if g.environment == environment and (team_ids is None or g.team in team_ids):
                found.setdefault(g.database, g)
        return found


def _workload(value: str, env: str) -> tuple[str, str, str]:
    kind, namespace, name = value.replace("{env}", env).split("/")
    return kind, namespace, name


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
    names = [e.name for e in parsed.environments]
    grants = tuple(
        Grant(team.id, env, ref.name.replace("{env}", env), tuple(_workload(w, env) for w in ref.workloads))
        for team in parsed.teams
        for ref in team.databases
        for env in (ref.environments or names)
    )
    return Catalog(environments={e.name: e for e in parsed.environments}, teams={t.id: t for t in parsed.teams}, grants=grants)
