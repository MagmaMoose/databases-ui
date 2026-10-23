"""Signing in with Entra ID, as a confidential client (the flow itself is in oidc.py), and the
app's own token for Azure Storage.

The credential is a workload identity token (AZURE_FEDERATED_TOKEN_FILE, which Entra trusts
through a federated credential on the app registration) or a client secret. People sign in to the
app registration the cluster's API server trusts, so their ID token names them the way Kubernetes
does: the user and groups claims, with the API server's prefixes, are what access reviews ask about.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import httpx

from .config import Config
from .oidc import OIDC, SignIn, SignInError

_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
STORAGE_SCOPE = "https://storage.azure.com/.default"


def federated_credential(token_file: str) -> dict[str, str]:
    try:
        # The kubelet rotates the projected token in place.
        with open(token_file) as f:
            assertion = f.read().strip()
    except OSError as err:
        raise SignInError(f"cannot read the workload identity token: {err.__class__.__name__}", status=500) from err
    return {"client_assertion_type": _ASSERTION_TYPE, "client_assertion": assertion}


def app_credential(cfg: Config) -> dict[str, str]:
    """The app's credential on Entra ID's token endpoint, read fresh each time."""
    if cfg.federated_token_file is None:
        return {"client_secret": cfg.client_secret or ""}
    return federated_credential(str(cfg.federated_token_file))


class AppToken:
    """The app's own access token for one scope, kept until shortly before it expires."""

    def __init__(self, http: httpx.AsyncClient, *, token_url: str, client_id: str, credential: Callable[[], dict[str, str]], scope: str = STORAGE_SCOPE):
        self._http = http
        self._url = token_url
        self._client_id = client_id
        self._credential = credential
        self._scope = scope
        self._token = ""
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def __call__(self) -> str:
        async with self._lock:
            if self._token and self._expires - 120 > time.time():
                return self._token
            data = {"grant_type": "client_credentials", "client_id": self._client_id, "scope": self._scope, **self._credential()}
            try:
                response = await self._http.post(self._url, data=data, headers={"Accept": "application/json"}, timeout=15)
                body = response.json()
            except (httpx.HTTPError, ValueError) as err:
                raise SignInError(f"cannot get the app's storage token: {err.__class__.__name__}") from err
            if response.status_code != 200 or not isinstance(body, dict) or not body.get("access_token"):
                reason = str(body.get("error_description") or body.get("error") or response.status_code) if isinstance(body, dict) else response.status_code
                raise SignInError(f"Entra ID refused the app's storage token: {str(reason).splitlines()[0][:300]}")
            self._token = str(body["access_token"])
            self._expires = time.time() + int(body.get("expires_in") or 3600)
            return self._token


class Entra(OIDC):
    name = "Microsoft"

    def __init__(self, cfg: Config, http: httpx.AsyncClient):
        base = f"{cfg.authority}/{cfg.tenant_id}"
        super().__init__(
            http,
            client_id=cfg.client_id,
            redirect_uri=cfg.redirect_uri,
            scope="openid profile",
            endpoints={"authorize": f"{base}/oauth2/v2.0/authorize", "token": f"{base}/oauth2/v2.0/token", "jwks": f"{base}/discovery/v2.0/keys"},
            issuers=(f"{base}/v2.0",),
        )
        self._cfg = cfg

    def _credential(self) -> dict[str, str]:
        return app_credential(self._cfg)

    def _check_claims(self, claims: dict[str, Any]) -> None:
        cfg = self._cfg
        if str(claims.get("tid", "")).lower() != cfg.tenant_id or not claims.get("oid"):
            raise SignInError("that account belongs to another tenant", status=401)
        if not isinstance(claims.get(cfg.username_claim), str) or not claims[cfg.username_claim]:
            raise SignInError(f"the ID token has no {cfg.username_claim}, which is how Kubernetes names you", status=401)
        # Over 200 groups, Entra leaves them out and points at Graph instead.
        if cfg.groups_claim in (claims.get("_claim_names") or {}):
            raise SignInError(f"your {cfg.groups_claim} do not fit in the ID token, so Kubernetes would not know them either", status=401)

    async def _sign_in(self, body: dict[str, Any], claims: dict[str, Any]) -> SignIn:
        cfg = self._cfg
        found = claims.get(cfg.groups_claim) or []
        return SignIn(
            oid=str(claims["oid"]).lower(),
            upn=str(claims.get("preferred_username") or claims.get("upn") or claims["oid"])[:120],
            name=str(claims.get("name") or "")[:120],
            # As the API server takes them: the claim as it is, with its prefix.
            user=f"{cfg.username_prefix}{claims[cfg.username_claim]}",
            groups=[f"{cfg.groups_prefix}{g}" for g in (found if isinstance(found, list) else [found]) if isinstance(g, str)][:200],
        )
