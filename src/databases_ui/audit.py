"""The audit log: one JSON line per action, in an append blob per month in the environment's
container. The server writes what people ask for and decide; each Job writes how it ended.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from .blob import BlobError, Container

log = logging.getLogger("databases_ui")
PREFIX = "audit/"


def blob_name(at: datetime) -> str:
    return f"{PREFIX}{at:%Y-%m}.jsonl"


async def record(container: Container, *, environment: str, actor: str, action: str, at: datetime | None = None, **detail: Any) -> dict[str, Any]:
    """Append one entry, and log it; a storage error is logged, not raised, after the action happened."""
    at = at or datetime.now(UTC)
    entry = {"at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "environment": environment, "actor": actor, "action": action, **{k: v for k, v in detail.items() if v not in (None, "")}}
    log.info(json.dumps({"msg": "audit", **entry}))
    try:
        await container.append(blob_name(at), (json.dumps(entry, separators=(",", ":")) + "\n").encode())
    except BlobError as err:
        log.error(json.dumps({"msg": "audit write failed", "status": err.status, "reason": str(err), "entry": entry}))
    return entry


def parse(text: bytes) -> list[dict[str, Any]]:
    entries = []
    for line in text.decode(errors="replace").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries
