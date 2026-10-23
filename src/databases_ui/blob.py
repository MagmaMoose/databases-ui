"""Azure Blob Storage through its REST API: one container per environment, holding its snapshots
and its audit log.

An Entra ID token (workload identity) or a SAS authorises every call. A snapshot is uploaded in
blocks as pg_dump writes it, so nothing touches a disk; an upload that fails commits nothing, and
Azure drops the uncommitted blocks after a week.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import xml.etree.ElementTree as ET  # nosec B405 - parses Azure Storage's own listings, nothing a person writes
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import formatdate
from urllib.parse import parse_qsl, quote, unquote

import httpx

API_VERSION = "2024-08-04"
BLOCK = 8 * 1024 * 1024
_RETRIES = 3


class BlobError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Blob:
    name: str
    size: int
    created: str
    metadata: dict[str, str]


class Container:
    def __init__(self, http: httpx.AsyncClient, url: str, *, token: Callable[[], Awaitable[str]] | None = None, sas: str = ""):
        self._http = http
        self.url = url.rstrip("/")
        self._token = token
        self._sas = dict(parse_qsl(sas.lstrip("?")))

    async def _headers(self, **extra: str) -> dict[str, str]:
        headers = {"x-ms-version": API_VERSION, "x-ms-date": formatdate(usegmt=True), **extra}
        if self._token is not None and not self._sas:
            headers["Authorization"] = f"Bearer {await self._token()}"
        return headers

    def _target(self, name: str = "", **query: str) -> tuple[str, dict[str, str]]:
        url = f"{self.url}/{quote(name)}" if name else self.url
        return url, {**query, **self._sas}

    async def _send(self, method: str, name: str = "", *, query: dict[str, str] | None = None, headers: dict[str, str] | None = None, content: bytes | None = None, ok: tuple[int, ...] = (200, 201)) -> httpx.Response:
        url, params = self._target(name, **(query or {}))
        for attempt in range(_RETRIES):
            try:
                response = await self._http.request(method, url, params=params, headers=await self._headers(**(headers or {})), content=content, timeout=120)
            except httpx.HTTPError as err:
                if attempt + 1 == _RETRIES:
                    raise BlobError(502, f"cannot reach storage: {err.__class__.__name__}") from err
            else:
                if response.status_code in ok or response.status_code < 500 or attempt + 1 == _RETRIES:
                    break
            await asyncio.sleep(2**attempt)
        if response.status_code not in ok:
            raise BlobError(response.status_code, f"storage answered {response.status_code} {response.headers.get('x-ms-error-code', '')}".strip())
        return response

    async def list(self, prefix: str) -> list[Blob]:
        found: list[Blob] = []
        marker = ""
        while True:
            query = {"restype": "container", "comp": "list", "prefix": prefix, "include": "metadata"}
            if marker:
                query["marker"] = marker
            response = await self._send("GET", query=query)
            root = ET.fromstring(response.content)  # nosec B314 - Azure Storage's own XML; expat limits entity expansion
            for item in root.iter("Blob"):
                props = item.find("Properties")
                meta = item.find("Metadata")
                found.append(
                    Blob(
                        name=item.findtext("Name", ""),
                        size=int(props.findtext("Content-Length", "0") if props is not None else 0),
                        created=(props.findtext("Creation-Time", "") or props.findtext("Last-Modified", "")) if props is not None else "",
                        metadata={child.tag.lower(): unquote(child.text or "") for child in meta} if meta is not None else {},
                    )
                )
            marker = root.findtext("NextMarker", "")
            if not marker:
                return found

    async def properties(self, name: str) -> Blob | None:
        try:
            response = await self._send("HEAD", name)
        except BlobError as err:
            if err.status == 404:
                return None
            raise
        meta = {k[len("x-ms-meta-") :].lower(): unquote(v) for k, v in response.headers.items() if k.lower().startswith("x-ms-meta-")}
        return Blob(name=name, size=int(response.headers.get("content-length", "0")), created=response.headers.get("x-ms-creation-time", ""), metadata=meta)

    async def upload(self, name: str, chunks: AsyncIterator[bytes], metadata: dict[str, str], before_commit: Callable[[], Awaitable[None]] | None = None) -> tuple[int, str]:
        """Write `chunks` to a block blob, committed with `metadata` once all are in and
        `before_commit` has not raised: its size and sha256."""
        ids: list[str] = []
        digest = hashlib.sha256()
        size = 0
        buffer = bytearray()

        async def put(data: bytes) -> None:
            block = base64.b64encode(f"{len(ids):08d}".encode()).decode()
            await self._send("PUT", name, query={"comp": "block", "blockid": block}, content=data)
            ids.append(block)

        async for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
            buffer += chunk
            while len(buffer) >= BLOCK:
                await put(bytes(buffer[:BLOCK]))
                del buffer[:BLOCK]
        if buffer:
            await put(bytes(buffer))
        if before_commit is not None:
            await before_commit()
        body = "<?xml version='1.0' encoding='utf-8'?><BlockList>" + "".join(f"<Latest>{i}</Latest>" for i in ids) + "</BlockList>"
        meta = {f"x-ms-meta-{k}": quote(v, safe="") for k, v in {**metadata, "size": str(size), "sha256": digest.hexdigest()}.items()}
        await self._send("PUT", name, query={"comp": "blocklist"}, headers={"x-ms-blob-content-type": "application/octet-stream", **meta}, content=body.encode())
        return size, digest.hexdigest()

    @asynccontextmanager
    async def download(self, name: str) -> AsyncIterator[tuple[int, AsyncIterator[bytes]]]:
        """The blob's size and its bytes, streamed."""
        url, params = self._target(name)
        try:
            async with self._http.stream("GET", url, params=params, headers=await self._headers(), timeout=httpx.Timeout(60, read=300)) as response:
                if response.status_code != 200:
                    raise BlobError(response.status_code, f"storage answered {response.status_code} {response.headers.get('x-ms-error-code', '')}".strip())
                yield int(response.headers.get("content-length", "0")), response.aiter_bytes(1024 * 1024)
        except httpx.HTTPError as err:
            raise BlobError(502, f"the download broke off: {err.__class__.__name__}") from err

    async def read(self, name: str) -> bytes | None:
        try:
            return (await self._send("GET", name)).content
        except BlobError as err:
            if err.status == 404:
                return None
            raise

    async def append(self, name: str, data: bytes) -> None:
        """Add `data` to an append blob, created on first use."""
        try:
            await self._send("PUT", name, query={"comp": "appendblock"}, content=data)
            return
        except BlobError as err:
            if err.status != 404:
                raise
        # 409: someone else created it in the meantime, which is as good.
        await self._send("PUT", name, headers={"x-ms-blob-type": "AppendBlob", "If-None-Match": "*"}, content=b"", ok=(201, 409))
        await self._send("PUT", name, query={"comp": "appendblock"}, content=data)
