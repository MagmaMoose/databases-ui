"""Fake Entra ID, Kubernetes API and Blob Storage behind httpx MockTransports, and an app wired to them.

The Kubernetes fake keeps objects by API path and answers list, get, create, merge-patch and
delete the way the API server does; the storage fake keeps block, block-list and append blobs.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from databases_ui.app import create_app
from databases_ui.config import load_config

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ORIGIN = "https://databases.example.test"
AUTHORITY = "https://login.example.test"
STORAGE = "https://snapshots.blob.example.test"
KUBE = "https://kubernetes.example.test"
NOW = datetime(2026, 10, 23, 14, 0, 0, tzinfo=UTC)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PAYMENTS = "6f1c2b7e-3a44-4d0e-9a51-2b8e0c7d9f10"
IDENTITY = "0b9d8e7c-6f5a-4b3c-8d2e-1f0a9b8c7d6e"
UNRELATED = "99999999-9999-4999-8999-999999999999"

CATALOG = f"""
environments:
  - name: dev
    storage:
      url: {STORAGE}/dev
    cloneFrom: [acc, prd]
  - name: acc
    kubernetes: none
    storage:
      url: {STORAGE}/acc
  - name: prd
    production: true
    cluster: prod
    storage:
      url: {STORAGE}/prd
teams:
  - id: payments
    name: Payments
    groups: [{PAYMENTS}]
    databases:
      - name: payments_{{env}}
        workloads:
          - deployment/{{env}}-payments/api
  - id: identity
    name: Identity
    groups: [{IDENTITY}]
    databases:
      - name: keycloak
        environments: [dev]
"""


def oid(upn: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, upn))


@dataclass
class FakeEntra:
    user: str = "alice@example.com"
    roles: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    overage: bool = False
    tenant: str = TENANT
    challenge: str = ""
    nonce: str = ""
    token_requests: list[dict[str, list[str]]] = field(default_factory=list)

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key()))
        return {"keys": [{**jwk, "kid": "test-key", "use": "sig", "alg": "RS256"}]}

    def id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": f"{AUTHORITY}/{TENANT}/v2.0",
            "aud": CLIENT,
            "iat": now,
            "nbf": now,
            "exp": now + 3600,
            "nonce": self.nonce,
            "tid": self.tenant,
            "oid": oid(self.user),
            "name": self.user.split("@")[0].title(),
            "preferred_username": self.user,
            "roles": self.roles,
            "groups": self.groups,
        }
        if self.overage:
            claims.pop("groups")
            claims["_claim_names"] = {"groups": "src1"}
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "test-key"})

    def token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.token_requests.append(form)
        grant = form["grant_type"][0]
        if grant == "client_credentials":
            return httpx.Response(200, json={"access_token": "storage-token", "expires_in": 3600})
        if grant == "authorization_code":
            verifier = form["code_verifier"][0]
            digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if form["code"][0] != "good-code" or digest != self.challenge:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "AADSTS70008: bad code"})
            return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3600, "id_token": self.id_token()})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


@dataclass
class StoredBlob:
    data: bytes
    metadata: dict[str, str]
    created: str
    kind: str = "BlockBlob"


@dataclass
class FakeStorage:
    blobs: dict[str, StoredBlob] = field(default_factory=dict)
    blocks: dict[tuple[str, str], bytes] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    # Answers to fail with, by (method, comp): a status code each time.
    fail: dict[tuple[str, str], int] = field(default_factory=dict)
    page: int = 1000

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer storage-token" and "sig" not in request.url.params:
            return httpx.Response(403, headers={"x-ms-error-code": "AuthenticationFailed"})
        path = unquote(request.url.path).lstrip("/")
        container, _, name = path.partition("/")
        key = f"{container}/{name}"
        comp = request.url.params.get("comp", "")
        self.calls.append((request.method, f"{key}?{comp}" if comp else key))
        if (request.method, comp) in self.fail:
            return httpx.Response(self.fail[(request.method, comp)], headers={"x-ms-error-code": "ServerBusy"})
        if request.method == "GET" and comp == "list":
            prefix = f"{container}/{request.url.params.get('prefix', '')}"
            names = sorted(k for k in self.blobs if k.startswith(prefix))
            start = int(request.url.params.get("marker") or 0)
            chunk = names[start : start + self.page]
            items = "".join(
                f"<Blob><Name>{escape(n.split('/', 1)[1])}</Name><Properties><Creation-Time>{self.blobs[n].created}</Creation-Time>"
                f"<Content-Length>{len(self.blobs[n].data)}</Content-Length></Properties>"
                f"<Metadata>{''.join(f'<{k}>{escape(v)}</{k}>' for k, v in self.blobs[n].metadata.items())}</Metadata></Blob>"
                for n in chunk
            )
            marker = str(start + self.page) if start + self.page < len(names) else ""
            return httpx.Response(200, content=f"<?xml version='1.0'?><EnumerationResults><Blobs>{items}</Blobs><NextMarker>{marker}</NextMarker></EnumerationResults>".encode())
        if request.method == "PUT" and comp == "block":
            self.blocks[(key, request.url.params["blockid"])] = request.content
            return httpx.Response(201)
        if request.method == "PUT" and comp == "blocklist":
            ids = re.findall(r"<Latest>([^<]+)</Latest>", request.content.decode())
            meta = {k[len("x-ms-meta-") :]: v for k, v in request.headers.items() if k.startswith("x-ms-meta-")}
            self.blobs[key] = StoredBlob(b"".join(self.blocks.pop((key, i)) for i in ids), meta, formatdate(usegmt=True))
            return httpx.Response(201)
        if request.method == "PUT" and comp == "appendblock":
            if key not in self.blobs:
                return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
            self.blobs[key].data += request.content
            return httpx.Response(201)
        if request.method == "PUT" and request.headers.get("x-ms-blob-type") == "AppendBlob":
            if key in self.blobs:
                return httpx.Response(409, headers={"x-ms-error-code": "BlobAlreadyExists"})
            self.blobs[key] = StoredBlob(b"", {}, formatdate(usegmt=True), "AppendBlob")
            return httpx.Response(201)
        blob = self.blobs.get(key)
        if request.method in ("GET", "HEAD"):
            if blob is None:
                return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
            headers = {"content-length": str(len(blob.data)), "x-ms-creation-time": blob.created, **{f"x-ms-meta-{k}": v for k, v in blob.metadata.items()}}
            return httpx.Response(200, headers=headers, content=blob.data if request.method == "GET" else b"")
        return httpx.Response(400, headers={"x-ms-error-code": "Unhandled"})

    def text(self, key: str) -> str:
        return self.blobs[key].data.decode()

    def audit(self, env: str) -> list[dict[str, Any]]:
        found = [b for k, b in self.blobs.items() if k.startswith(f"{env}/audit/")]
        return [json.loads(line) for b in found for line in b.data.decode().splitlines()]


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if v is None:
            target.pop(k, None)
        elif isinstance(v, dict) and isinstance(target.get(k), dict):
            _merge(target[k], v)
        else:
            target[k] = copy.deepcopy(v)


def _matches(obj: dict[str, Any], selector: str) -> bool:
    labels = obj.get("metadata", {}).get("labels") or {}
    for term in filter(None, selector.split(",")):
        k, _, v = term.partition("=")
        if ("=" in term and labels.get(k) != v) or ("=" not in term and k not in labels):
            return False
    return True


@dataclass
class FakeKube:
    # Collections by path, items by name.
    objects: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    # Answer every call with this status, as an API server that refuses this app would.
    refuse: int = 0

    def put(self, collection: str, obj: dict[str, Any]) -> dict[str, Any]:
        obj.setdefault("metadata", {}).setdefault("labels", {})
        self.objects.setdefault(collection, {})[obj["metadata"]["name"]] = obj
        return obj

    def get(self, collection: str, name: str) -> dict[str, Any] | None:
        return self.objects.get(collection, {}).get(name)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if self.refuse:
            return httpx.Response(self.refuse, json={"message": "jobs.batch is forbidden"})
        collection, _, name = path.rpartition("/")
        if name in ("databases", "clusters", "jobs", "pods", "deployments", "statefulsets"):
            collection, name = path, ""
        items = self.objects.setdefault(collection, {})
        if request.method == "GET" and not name:
            selector = request.url.params.get("labelSelector", "")
            return httpx.Response(200, json={"items": [copy.deepcopy(o) for o in items.values() if _matches(o, selector)]})
        if request.method == "GET":
            found = items.get(name)
            return httpx.Response(200, json=copy.deepcopy(found)) if found else httpx.Response(404, json={"message": f"{name} not found"})
        if request.method == "POST":
            body = json.loads(request.content)
            if body["metadata"]["name"] in items:
                return httpx.Response(409, json={"message": "already exists"})
            body["metadata"].setdefault("creationTimestamp", "2026-10-23T14:00:00Z")
            return httpx.Response(201, json=copy.deepcopy(self.put(collection, body)))
        if request.method == "PATCH":
            found = items.get(name)
            if found is None:
                return httpx.Response(404, json={"message": "not found"})
            assert request.headers["content-type"] == "application/merge-patch+json"
            _merge(found, json.loads(request.content))
            return httpx.Response(200, json=copy.deepcopy(found))
        if request.method == "DELETE":
            return httpx.Response(200, json={}) if items.pop(name, None) else httpx.Response(404, json={"message": "not found"})
        return httpx.Response(405, json={"message": "unhandled"})


CNPG = "/apis/postgresql.cnpg.io/v1/namespaces/database"
JOBS = "/apis/batch/v1/namespaces/database/jobs"
PODS = "/api/v1/namespaces/database/pods"


def database(name: str, owner: str, cluster: str, **spec: Any) -> dict[str, Any]:
    return {"metadata": {"name": name.replace("_", "-"), "labels": {}}, "spec": {"name": name, "owner": owner, "cluster": {"name": cluster}, **spec}, "status": {"applied": True}}


@dataclass
class World:
    entra: FakeEntra = field(default_factory=FakeEntra)
    storage: FakeStorage = field(default_factory=FakeStorage)
    kube: FakeKube = field(default_factory=FakeKube)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{AUTHORITY}/{TENANT}/oauth2/v2.0/token"):
            return self.entra.token(request)
        if url.startswith(f"{AUTHORITY}/{TENANT}/discovery/v2.0/keys"):
            return httpx.Response(200, json=self.entra.jwks())
        if url.startswith(STORAGE):
            return self.storage.handle(request)
        return httpx.Response(404, json={"error": "unhandled", "url": url})

    def seed(self) -> None:
        for name, cluster, roles in (("dev", "dev", ["payments_dev", "keycloak"]), ("prod", "prod", ["payments_prd"])):
            self.kube.put(
                f"{CNPG}/clusters",
                {"metadata": {"name": name}, "spec": {"managed": {"roles": [{"name": r, "passwordSecret": {"name": f"{r}-db"}} for r in roles] + [{"name": "nopass"}]}}, "status": {"certificates": {"serverCASecret": f"{cluster}-ca"}}},
            )
        for d in (
            database("payments_dev", "payments_dev", "dev", extensions=[{"name": "citext", "ensure": "present"}, {"name": "old", "ensure": "absent"}]),
            database("keycloak", "keycloak", "dev"),
            database("platform_only", "nopass", "dev"),
            database("payments_prd", "payments_prd", "prod"),
            database("gone", "payments_dev", "dev", ensure="absent"),
        ):
            self.kube.put(f"{CNPG}/databases", d)

    def snapshot(self, env: str, database: str, sid: str, data: bytes = b"PGDMP-test", **meta: str) -> None:
        self.storage.blobs[f"{env}/snapshots/{database}/{sid}.dump"] = StoredBlob(data, {"createdby": "alice@example.com", "note": "kept", **meta}, "Fri, 23 Oct 2026 12:00:00 GMT")


@pytest.fixture
def world() -> World:
    w = World()
    w.seed()
    return w


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text(CATALOG)
    token = tmp_path / "token"
    token.write_text("federated-token")
    return {
        "AZURE_TENANT_ID": TENANT,
        "AZURE_CLIENT_ID": CLIENT,
        "AZURE_AUTHORITY_HOST": AUTHORITY,
        "AZURE_FEDERATED_TOKEN_FILE": str(token),
        "DATABASES_UI_PUBLIC_ORIGIN": ORIGIN,
        "DATABASES_UI_SESSION_KEY": "k" * 40,
        "DATABASES_UI_CATALOG": str(catalog),
        "DATABASES_UI_JOB_IMAGE": "ghcr.io/magmamoose/databases-ui:v0.1.0",
        "DATABASES_UI_STATIC_DIR": str(tmp_path / "none"),
    }


def make_client(world: World, env: dict[str, str]) -> TestClient:
    app = create_app(load_config(env), transport=httpx.MockTransport(world.handler), kube_transport=httpx.MockTransport(world.kube.handle), now=lambda: NOW)
    return TestClient(app, base_url=ORIGIN, follow_redirects=False)


@pytest.fixture
def client(world: World, env: dict[str, str]) -> TestClient:
    return make_client(world, env)


def sign_in(client: TestClient, world: World, upn: str = "alice@example.com", groups: list[str] | tuple[str, ...] = (PAYMENTS,), roles: list[str] | tuple[str, ...] = ()) -> dict[str, Any]:
    client.cookies.clear()
    world.entra.user, world.entra.groups, world.entra.roles = upn, list(groups), list(roles)
    response = client.get("/auth/login")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    world.entra.challenge, world.entra.nonce = query["code_challenge"][0], query["nonce"][0]
    done = client.get(f"/auth/callback?code=good-code&state={query['state'][0]}")
    assert done.status_code == 303 and "auth_error" not in done.headers["location"], done.headers["location"]
    return client.get("/api/me").json()


def write_headers(me: dict[str, Any], origin: str = ORIGIN) -> dict[str, str]:
    return {"X-CSRF-Token": me["csrf"], "Origin": origin}
