"""Signed-in state, sealed into cookies with AES-256-GCM. The server keeps no session store.

Any replica can serve any request and a restart signs nobody out. The cookie holds who the
person is, their app roles and those of their groups the catalog names. It carries no token:
every call to Kubernetes and to storage runs as the app.
"""

from __future__ import annotations

import base64
import json
import os
import time
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from starlette.requests import Request
from starlette.responses import Response

# Browsers drop a cookie over 4096 bytes without a word, which shows up as a sign-in loop.
_CHUNK = 3800
_MAX_CHUNKS = 6


class Sealer:
    def __init__(self, key: bytes):
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"databases-ui cookie v1").derive(key)
        self._aead = AESGCM(derived)

    def seal(self, purpose: str, data: dict[str, Any], ttl: int) -> str:
        body = json.dumps({**data, "exp": int(time.time()) + ttl}, separators=(",", ":")).encode()
        nonce = os.urandom(12)
        sealed = nonce + self._aead.encrypt(nonce, body, purpose.encode())
        return base64.urlsafe_b64encode(sealed).rstrip(b"=").decode()

    def open(self, purpose: str, token: str) -> dict[str, Any] | None:
        """The data, or None for anything forged, altered, sealed for another purpose or expired."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            body = json.loads(self._aead.decrypt(raw[:12], raw[12:], purpose.encode()))
        except (ValueError, TypeError, InvalidTag):
            return None
        if not isinstance(body, dict) or not isinstance(body.get("exp"), int) or body["exp"] <= time.time():
            return None
        return body


class Cookies:
    """Cookie names and attributes. `__Host-` pins a cookie to this exact host, https and path /."""

    def __init__(self, sealer: Sealer, secure: bool):
        self._sealer = sealer
        self._secure = secure
        prefix = "__Host-" if secure else ""
        self.session = f"{prefix}databases-ui"
        self.login = f"{prefix}databases-ui-login"

    def _set(self, response: Response, name: str, value: str, max_age: int, samesite: str) -> None:
        response.set_cookie(name, value, max_age=max_age, path="/", secure=self._secure, httponly=True, samesite=samesite)  # type: ignore[arg-type]

    def read_session(self, request: Request) -> dict[str, Any] | None:
        parts = []
        for i in range(_MAX_CHUNKS):
            part = request.cookies.get(f"{self.session}.{i}")
            if part is None:
                break
            parts.append(part)
        return self._sealer.open("session", "".join(parts)) if parts else None

    def write_session(self, request: Request, response: Response, data: dict[str, Any], ttl: int) -> None:
        token = self._sealer.seal("session", data, ttl)
        chunks = [token[i : i + _CHUNK] for i in range(0, len(token), _CHUNK)]
        if len(chunks) > _MAX_CHUNKS:
            raise ValueError("the session does not fit in cookies")
        for i, chunk in enumerate(chunks):
            # Strict: no other site can make the browser send it, which is half the CSRF defence.
            self._set(response, f"{self.session}.{i}", chunk, ttl, "strict")
        for i in range(len(chunks), _MAX_CHUNKS):
            if f"{self.session}.{i}" in request.cookies:
                self.clear(response, f"{self.session}.{i}")

    def clear_session(self, request: Request, response: Response) -> None:
        for i in range(_MAX_CHUNKS):
            if f"{self.session}.{i}" in request.cookies:
                self.clear(response, f"{self.session}.{i}")

    def read_login(self, request: Request) -> dict[str, Any] | None:
        return self._sealer.open("login", request.cookies.get(self.login, ""))

    def write_login(self, response: Response, data: dict[str, Any], ttl: int) -> None:
        # Lax, not strict: it has to come back on the top-level redirect from Microsoft.
        self._set(response, self.login, self._sealer.seal("login", data, ttl), ttl, "lax")

    def clear(self, response: Response, name: str) -> None:
        response.delete_cookie(name, path="/", secure=self._secure, httponly=True, samesite="lax")

