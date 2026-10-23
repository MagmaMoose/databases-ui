"""A small Kubernetes API client: the handful of calls this app and its Jobs make, over httpx.

In a pod it uses the pod's service-account token and CA; for another cluster, the server, CA file
and token file from the catalog. The token is read from its file on every call, because the
kubelet rotates a projected token in place.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path
from typing import Any

import httpx

IN_CLUSTER = Path("/var/run/secrets/kubernetes.io/serviceaccount")
CNPG = "/apis/postgresql.cnpg.io/v1"


class KubeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Kube:
    def __init__(self, *, server: str, token_file: str, ca_file: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        verify: ssl.SSLContext | bool = True
        if transport is None and ca_file and Path(ca_file).exists():
            verify = ssl.create_default_context(cafile=ca_file)
        self._http = httpx.AsyncClient(base_url=server.rstrip("/"), verify=verify, transport=transport, timeout=20, follow_redirects=False)
        self._token_file = Path(token_file)

    @classmethod
    def in_cluster(cls, transport: httpx.AsyncBaseTransport | None = None) -> Kube:
        host, port = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"), os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        host = f"[{host}]" if ":" in host else host
        return cls(server=f"https://{host}:{port}", token_file=str(IN_CLUSTER / "token"), ca_file=str(IN_CLUSTER / "ca.crt"), transport=transport)

    def _headers(self, content_type: str | None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if content_type:
            headers["Content-Type"] = content_type
        try:
            headers["Authorization"] = f"Bearer {self._token_file.read_text().strip()}"
        except OSError:
            pass  # no token: the API server says so (401), which names the problem better than this could
        return headers

    async def request(self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None, content_type: str | None = None) -> Any:
        try:
            response = await self._http.request(method, path, params=params, json=body, headers=self._headers(content_type if body is not None else None))
        except httpx.HTTPError as err:
            raise KubeError(502, f"cannot reach the Kubernetes API: {err.__class__.__name__}") from err
        if response.status_code >= 400:
            try:
                message = str(response.json().get("message") or response.status_code)
            except ValueError:
                message = f"HTTP {response.status_code}"
            raise KubeError(response.status_code, message[:500])
        return response.json() if response.content else None

    async def get(self, path: str) -> dict[str, Any] | None:
        try:
            return await self.request("GET", path)
        except KubeError as err:
            if err.status == 404:
                return None
            raise

    async def list(self, path: str, selector: str = "") -> list[dict[str, Any]]:
        found = await self.request("GET", path, params={"labelSelector": selector} if selector else None)
        return list((found or {}).get("items") or [])

    async def create(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("POST", path, body=body, content_type="application/json")

    async def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self.request("PATCH", path, body=body, content_type="application/merge-patch+json")

    async def delete(self, path: str) -> None:
        try:
            # Background: the Job's pods go with it.
            await self.request("DELETE", path, params={"propagationPolicy": "Background"})
        except KubeError as err:
            if err.status != 404:
                raise

    async def aclose(self) -> None:
        await self._http.aclose()
