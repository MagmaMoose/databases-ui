"""Signing in with OpenID Connect: authorization code flow with PKCE, as a confidential client.

What differs between identity providers lives in a subclass: the endpoints, the app's own
credential, which claims have to hold, and where a person's roles and groups come from. Entra ID
is in entra.py. Signing in needs only the ID token.

The ID token is verified against the provider's published keys although it arrives straight
from the token endpoint over TLS (OIDC Core 3.1.3.7 would allow skipping that): issuer,
audience, expiry and nonce, plus the subclass's own claims, all have to hold.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

_JWKS_TTL = 3600.0
_JWKS_COOLDOWN = 30.0


class SignInError(Exception):
    def __init__(self, message: str, *, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SignIn:
    oid: str
    upn: str
    name: str
    roles: list[str]
    groups: list[str]


def pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _reason(body: Any) -> str:
    """The provider's own reason, trimmed to the line a person can search for (AADSTS…)."""
    if not isinstance(body, dict):
        return "unknown error"
    return str(body.get("error_description") or body.get("error") or "unknown error").splitlines()[0][:300]


class OIDC:
    # Who people sign in with, for messages and the sign-in button: "Microsoft".
    name = ""

    def __init__(self, http: httpx.AsyncClient, *, client_id: str, redirect_uri: str, scope: str, endpoints: dict[str, str], issuers: tuple[str, ...]):
        self._http = http
        self.client_id = client_id
        self._redirect_uri = redirect_uri
        self._scope = scope
        self._authorize = endpoints["authorize"]
        self._token = endpoints["token"]
        self._jwks_url = endpoints["jwks"]
        self._issuers = issuers
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_at = float("-inf")

    def _authorize_extra(self) -> dict[str, str]:
        return {}

    def _credential(self) -> dict[str, str]:
        """The app's own credential on the token endpoint."""
        raise NotImplementedError

    def _check_claims(self, claims: dict[str, Any]) -> None:
        """Claims of a verified ID token that also have to hold; raise SignInError if not."""

    async def _sign_in(self, body: dict[str, Any], claims: dict[str, Any]) -> SignIn:
        """The signed-in person, from a redeemed code's tokens and its verified ID token's claims."""
        raise NotImplementedError

    def authorize_url(self, *, state: str, nonce: str, verifier: str) -> str:
        query = {
            "client_id": self.client_id,
            "response_type": "code",
            "response_mode": "query",
            "redirect_uri": self._redirect_uri,
            "scope": self._scope,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(verifier),
            "code_challenge_method": "S256",
            **self._authorize_extra(),
        }
        return f"{self._authorize}?{urlencode(query)}"

    async def _redeem(self, fields: dict[str, str]) -> dict[str, Any]:
        data = {**fields, "client_id": self.client_id, **self._credential()}
        try:
            response = await self._http.post(self._token, data=data, headers={"Accept": "application/json"}, timeout=15)
        except httpx.HTTPError as err:
            raise SignInError(f"cannot reach {self.name} sign-in: {err.__class__.__name__}") from err
        try:
            body = response.json()
        except ValueError as err:
            raise SignInError(f"{self.name} sign-in answered HTTP {response.status_code} without JSON") from err
        if response.status_code != 200 or not isinstance(body, dict):
            grant = isinstance(body, dict) and body.get("error") in ("invalid_grant", "interaction_required")
            raise SignInError(f"{self.name} refused the sign-in: {_reason(body)}", status=401 if grant else 502)
        return body

    async def redeem_code(self, *, code: str, verifier: str, nonce: str) -> SignIn:
        body = await self._redeem(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._redirect_uri,
                "code_verifier": verifier,
                "scope": self._scope,
            }
        )
        claims = await self.verify_id_token(str(body.get("id_token") or ""), nonce=nonce)
        return await self._sign_in(body, claims)

    async def _signing_key(self, kid: str) -> jwt.PyJWK:
        now = time.monotonic()
        stale = now - self._keys_at > _JWKS_TTL
        unknown = kid not in self._keys and now - self._keys_at > _JWKS_COOLDOWN
        if stale or unknown:
            try:
                response = await self._http.get(self._jwks_url, timeout=15)
                response.raise_for_status()
                keys = {}
                for data in response.json().get("keys", []):
                    try:
                        keys[str(data.get("kid"))] = jwt.PyJWK(data)
                    except jwt.PyJWKError:
                        continue
                self._keys, self._keys_at = keys, now
            except (httpx.HTTPError, ValueError, AttributeError) as err:
                raise SignInError(f"cannot read {self.name}'s signing keys: {err.__class__.__name__}") from err
        if kid not in self._keys:
            raise SignInError(f"the ID token is signed with a key {self.name} does not publish", status=401)
        return self._keys[kid]

    async def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        try:
            kid = str(jwt.get_unverified_header(id_token).get("kid") or "")
            key = await self._signing_key(kid)
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self._issuers,
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "nonce"]},
            )
        except jwt.PyJWTError as err:
            raise SignInError(f"the ID token did not verify: {err.__class__.__name__}", status=401) from err
        if not hmac.compare_digest(str(claims.get("nonce")).encode(), nonce.encode()):
            raise SignInError("the ID token belongs to another sign-in", status=401)
        self._check_claims(claims)
        return claims
