"""Everything the server reads from its environment, parsed and checked once at startup.

A value that would make the server unsafe or useless fails startup rather than the first
request: a pod in CrashLoopBackOff with the reason in its log is found in minutes.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import Catalog, CatalogError, load_catalog

_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")
_ROLE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
_K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
_SECRET_REF = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?/[A-Za-z0-9._-]{1,253}$")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}  # DevSkim: ignore DS162092 - plain http is allowed on loopback, for local development


class ConfigError(ValueError):
    """A configuration value the server refuses to start with."""


@dataclass(frozen=True)
class Branding:
    """Colours as #rrggbb; empty keeps the default. The dark ones fall back to the light ones."""

    accent: str = ""
    accent_text: str = ""
    accent_dark: str = ""
    accent_text_dark: str = ""


@dataclass(frozen=True)
class Config:
    tenant_id: str
    client_id: str
    origin: str
    authority: str
    session_key: bytes
    session_seconds: int
    catalog: Catalog
    # The image every Job runs: this app's own, which carries the PostgreSQL client.
    job_image: str
    job_service_account: str = "databases-ui-job"
    federated_token_file: Path | None = None
    client_secret: str | None = None
    # Storage through a SAS instead of Entra ID: the token for this server, and <secret>/<key> of
    # the Secret that holds the same token in each environment's namespace, for the Jobs.
    storage_sas: str = ""
    storage_sas_secret: str = ""
    admin_role: str = "Admin"
    auditor_role: str = "Auditor"
    static_dir: Path | None = None
    port: int = 8080
    version: str = "dev"
    branding: Branding = field(default_factory=Branding)

    @property
    def redirect_uri(self) -> str:
        return f"{self.origin}/auth/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.origin.startswith("https://")

    @property
    def token_url(self) -> str:
        return f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token"


def _guid(env: dict[str, str], name: str) -> str:
    value = env.get(name, "").strip().lower()
    if not _GUID.match(value):
        raise ConfigError(f"{name} must be a GUID")
    return value


def _origin(value: str, name: str) -> str:
    parts = urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or not host:
        raise ConfigError(f"{name} must be scheme://host[:port], with no path")
    if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK):
        raise ConfigError(f"{name} must be https (plain http only on a loopback address)")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _authority(value: str) -> str:
    parts = urlsplit(value.strip() or "https://login.microsoftonline.com")
    if parts.scheme != "https" or not _HOST.match((parts.hostname or "").lower()) or parts.path not in ("", "/"):
        raise ConfigError("AZURE_AUTHORITY_HOST must be an https host, such as https://login.microsoftonline.com")
    return f"https://{parts.netloc.lower()}"


def _secret(env: dict[str, str], name: str) -> str:
    """NAME, or the contents of the file NAME_FILE points at."""
    value = env.get(name, "")
    if not value and env.get(f"{name}_FILE"):
        try:
            value = Path(env[f"{name}_FILE"]).read_text().strip()
        except OSError as err:
            raise ConfigError(f"cannot read {name}_FILE: {err.__class__.__name__}") from err
    return value


def read_catalog(path: str) -> Catalog:
    try:
        text = Path(path).read_text()
    except OSError as err:
        raise ConfigError(f"cannot read the catalog {path}: {err.__class__.__name__}") from err
    try:
        return load_catalog(text, Path(path).name)
    except CatalogError as err:
        raise ConfigError(f"the catalog does not hold together: {err}") from err


def load_config(env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)

    token_file = env.get("AZURE_FEDERATED_TOKEN_FILE", "").strip()
    secret = _secret(env, "AZURE_CLIENT_SECRET")
    if not token_file and not secret:
        raise ConfigError("set AZURE_FEDERATED_TOKEN_FILE (workload identity), AZURE_CLIENT_SECRET or AZURE_CLIENT_SECRET_FILE")

    session_key = _secret(env, "DATABASES_UI_SESSION_KEY")
    if len(session_key) < 32:
        raise ConfigError("DATABASES_UI_SESSION_KEY (or DATABASES_UI_SESSION_KEY_FILE) must hold at least 32 characters")

    if not env.get("DATABASES_UI_CATALOG", "").strip():
        raise ConfigError("DATABASES_UI_CATALOG is required: the path of the catalog file")
    catalog = read_catalog(env["DATABASES_UI_CATALOG"].strip())

    job_image = env.get("DATABASES_UI_JOB_IMAGE", "").strip()
    if not job_image or any(c.isspace() for c in job_image):
        raise ConfigError("DATABASES_UI_JOB_IMAGE is required: the image the Jobs run, this app's own")
    job_sa = env.get("DATABASES_UI_JOB_SERVICE_ACCOUNT", "databases-ui-job").strip()
    if not _K8S_NAME.match(job_sa):
        raise ConfigError("DATABASES_UI_JOB_SERVICE_ACCOUNT must be a service account name")

    sas = _secret(env, "DATABASES_UI_STORAGE_SAS").strip().lstrip("?")
    sas_secret = env.get("DATABASES_UI_STORAGE_SAS_SECRET", "").strip()
    if bool(sas) != bool(sas_secret):
        raise ConfigError("a storage SAS needs both DATABASES_UI_STORAGE_SAS (or _FILE) and DATABASES_UI_STORAGE_SAS_SECRET (<secret>/<key> for the Jobs)")
    if sas_secret and not _SECRET_REF.match(sas_secret):
        raise ConfigError("DATABASES_UI_STORAGE_SAS_SECRET is <secret name>/<key>")
    if sas and "sig=" not in sas:
        raise ConfigError("DATABASES_UI_STORAGE_SAS is a SAS token: sv=…&sig=…")
    if not sas and not token_file and any(e.runs_jobs for e in catalog.environments.values()):
        # A Job can't use the client secret: workload identity is how it reaches storage.
        raise ConfigError("the Jobs reach storage through workload identity (AZURE_FEDERATED_TOKEN_FILE) or a SAS (DATABASES_UI_STORAGE_SAS)")

    roles = {}
    for key, default in (("admin", "Admin"), ("auditor", "Auditor")):
        name = f"DATABASES_UI_{key.upper()}_ROLE"
        roles[f"{key}_role"] = env.get(name, default).strip()
        if not _ROLE.match(roles[f"{key}_role"]):
            raise ConfigError(f"{name} must be an app role value, such as {default}")

    try:
        hours = int(env.get("DATABASES_UI_SESSION_HOURS", "8"))
        port = int(env.get("PORT", "8080"))
    except ValueError as err:
        raise ConfigError("DATABASES_UI_SESSION_HOURS and PORT must be whole numbers") from err
    if not 1 <= hours <= 24:
        raise ConfigError("DATABASES_UI_SESSION_HOURS must be between 1 and 24")

    if "DATABASES_UI_PUBLIC_ORIGIN" not in env:
        raise ConfigError("DATABASES_UI_PUBLIC_ORIGIN is required, such as https://databases.example.com")
    static = env.get("DATABASES_UI_STATIC_DIR", "/app/static")

    colours = {}
    for key in ("accent", "accent_text", "accent_dark", "accent_text_dark"):
        name = f"DATABASES_UI_BRAND_{key.upper()}"
        colours[key] = env.get(name, "").strip().lower()
        if colours[key] and not _COLOUR.match(colours[key]):
            raise ConfigError(f"{name} must be a colour as #rrggbb, such as #0f766e")

    return Config(
        tenant_id=_guid(env, "AZURE_TENANT_ID"),
        client_id=_guid(env, "AZURE_CLIENT_ID"),
        authority=_authority(env.get("AZURE_AUTHORITY_HOST", "")),
        federated_token_file=Path(token_file) if token_file else None,
        client_secret=secret or None,
        origin=_origin(env["DATABASES_UI_PUBLIC_ORIGIN"], "DATABASES_UI_PUBLIC_ORIGIN"),
        session_key=session_key.encode(),
        session_seconds=hours * 3600,
        catalog=catalog,
        job_image=job_image,
        job_service_account=job_sa,
        storage_sas=sas,
        storage_sas_secret=sas_secret,
        static_dir=Path(static) if static and Path(static).is_dir() else None,
        port=port,
        version=env.get("APP_VERSION", "dev"),
        branding=Branding(**colours),
        **roles,
    )
