"""The web app: sign-in routes, the JSON API the frontend calls, and the built frontend itself.

Writes need the session's CSRF token in X-CSRF-Token and this origin in Origin, on top of the
SameSite=Strict session cookie. What a person may see and do is Kubernetes' answer, asked in
service.py for the user and groups the session carries.
"""

import hmac
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .blob import BlobError, Container
from .catalog import DATABASE
from .config import Branding, Config
from .entra import AppToken, Entra, app_credential, federated_credential
from .kube import Kube, KubeError
from .oidc import SignInError
from .service import SNAPSHOT_ID, Actor, Console, ConsoleError
from .session import Cookies, Sealer

log = logging.getLogger("databases_ui")

LOGIN_SECONDS = 600
SOURCE = "https://github.com/MagmaMoose/databases-ui"

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "manifest-src 'self'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def release_notes(version: str) -> str | None:
    """Where a version's changes are written up: its GitHub release, or the pull request it was built from."""
    if re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        return f"{SOURCE}/releases/tag/{version}"
    if found := re.fullmatch(r"pr-([0-9]+)", version):
        return f"{SOURCE}/pull/{found.group(1)}"
    return None


def theme_css(brand: Branding) -> str:
    """The operator's colours over styles.css, whose `:root` loses to `html:root` in any order."""

    def rules(accent: str, text: str, soft: int) -> str:
        found = [f"--accent:{accent}", f"--accent-soft:color-mix(in srgb,{accent} {soft}%,var(--surface))"] if accent else []
        return ";".join(found + ([f"--accent-text:{text}"] if text else []))

    light = rules(brand.accent, brand.accent_text, 10)
    dark = rules(brand.accent_dark or brand.accent, brand.accent_text_dark or brand.accent_text, 14)
    return (f"html:root{{{light}}}\n" if light else "") + (f"@media (prefers-color-scheme: dark){{html:root{{{dark}}}}}\n" if dark else "")


def favicon_svg(brand: Branding) -> str:
    """The tab icon in the operator's accent. Unbranded, it is frontend/public/favicon.svg."""
    tile, glyph = brand.accent or "#0f766e", brand.accent_text or "#fff"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="7" fill="{tile}"/>'
        f'<g fill="none" stroke="{glyph}" stroke-width="2.2"><ellipse cx="16" cy="10" rx="7" ry="3"/>'
        '<path d="M9 10v12c0 1.7 3.1 3 7 3s7-1.3 7-3V10M9 16c0 1.7 3.1 3 7 3s7-1.3 7-3"/></g></svg>'
    )


EnvName = Annotated[str, Path(pattern=r"^[a-z0-9-]{1,32}$")]
DbName = Annotated[str, Path(pattern=DATABASE)]
JobName = Annotated[str, Path(pattern=r"^dbui-(snapshot|restore)-[0-9a-f]{8}$")]
SnapshotId = Annotated[str, Path(pattern=SNAPSHOT_ID)]


class Note(BaseModel):
    note: str = Field(default="", max_length=300)


class Restore(BaseModel):
    source_environment: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    source_database: str = Field(pattern=DATABASE)
    snapshot: str = Field(pattern=SNAPSHOT_ID)
    into: Literal["database", "copy"]
    reason: str = Field(min_length=3, max_length=300)


class Share(BaseModel):
    environment: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    namespace: str = Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


class Reason(BaseModel):
    reason: str = Field(default="", max_length=300)


def _log(event: str, **fields: Any) -> None:
    log.info(json.dumps({"msg": event, **fields}))


def build_console(cfg: Config, http: httpx.AsyncClient, *, kube_transport: httpx.AsyncBaseTransport | None = None, now=lambda: datetime.now(UTC)) -> Console:
    """This cluster's API server for the environments that run here, and a container per environment."""
    token = None
    if not cfg.storage_sas:
        # A storage identity of its own trusts the same service-account token as the sign-in app.
        own = cfg.storage_client_id not in ("", cfg.client_id)
        credential = (lambda: federated_credential(str(cfg.federated_token_file))) if own else (lambda: app_credential(cfg))
        token = AppToken(http, token_url=cfg.token_url, client_id=cfg.storage_client_id or cfg.client_id, credential=credential)
    here = Kube.in_cluster(transport=kube_transport)
    kube = {env.name: here for env in cfg.catalog.environments.values() if env.runs_jobs}
    storage = {env.name: Container(http, env.storage.url, token=token, sas=cfg.storage_sas) for env in cfg.catalog.environments.values()}
    return Console(cfg, kube, storage, now=now)


def create_app(cfg: Config, transport: httpx.AsyncBaseTransport | None = None, *, kube_transport: httpx.AsyncBaseTransport | None = None, now=lambda: datetime.now(UTC)) -> FastAPI:
    http = httpx.AsyncClient(transport=transport, follow_redirects=False)
    idp = Entra(cfg, http)
    cookies = Cookies(Sealer(cfg.session_key), secure=cfg.secure_cookies)
    console = build_console(cfg, http, kube_transport=kube_transport, now=now)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await http.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.console = console

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        if cfg.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/assets/"):
            # Vite names every asset by its content hash.
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith(("/api/", "/auth/")):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [{"field": ".".join(str(p) for p in e.get("loc", ())[1:]), "message": e.get("msg", "")} for e in exc.errors()]
        return JSONResponse({"error": "invalid_request", "message": "The request is not valid.", "fields": fields}, 422)

    @app.exception_handler(ConsoleError)
    async def console_error(_: Request, exc: ConsoleError) -> JSONResponse:
        return JSONResponse({"error": exc.code, "message": str(exc)}, exc.status)

    @app.exception_handler(KubeError)
    async def kube_error(_: Request, exc: KubeError) -> JSONResponse:
        _log("kubernetes failed", status=exc.status, reason=str(exc))
        return JSONResponse({"error": "kubernetes_unavailable", "message": f"Kubernetes refused: {exc}"}, 502)

    @app.exception_handler(BlobError)
    async def blob_error(_: Request, exc: BlobError) -> JSONResponse:
        _log("storage failed", status=exc.status, reason=str(exc))
        return JSONResponse({"error": "storage_unavailable", "message": f"Storage refused: {exc}"}, 502)

    @app.exception_handler(SignInError)
    async def token_error(_: Request, exc: SignInError) -> JSONResponse:
        _log("app token failed", status=exc.status, reason=str(exc))
        return JSONResponse({"error": "storage_unavailable", "message": str(exc)}, 502)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, exc.status_code)

    # ── Sessions ──────────────────────────────────────────────────────────────────────

    def session(request: Request) -> dict[str, Any]:
        data = cookies.read_session(request)
        if data is None:
            raise HTTPException(401, "signed_out")
        return data

    Session = Annotated[dict[str, Any], Depends(session)]

    def writable(request: Request, data: Session) -> dict[str, Any]:
        origin = request.headers.get("origin", "").encode()
        token = request.headers.get("x-csrf-token", "").encode()
        if not hmac.compare_digest(origin, cfg.origin.encode()) or not hmac.compare_digest(token, str(data.get("csrf", "")).encode()):
            raise HTTPException(403, "csrf")
        return data

    Writable = Annotated[dict[str, Any], Depends(writable)]

    def actor(data: dict[str, Any]) -> Actor:
        return Actor(
            oid=str(data["oid"]), upn=str(data.get("upn", "")), name=str(data.get("name", "")),
            user=str(data.get("user", "")), groups=tuple(str(g) for g in data.get("groups") or []),
        )  # fmt: skip

    # ── Sign-in ───────────────────────────────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    theme = theme_css(cfg.branding)
    favicon = favicon_svg(cfg.branding)

    @app.get("/theme.css")
    async def theme_stylesheet() -> Response:
        return Response(theme, media_type="text/css")

    @app.get("/favicon.svg")
    async def favicon_image() -> Response:
        return Response(favicon, media_type="image/svg+xml")

    @app.get("/auth/login")
    async def login() -> Response:
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        response = RedirectResponse(idp.authorize_url(state=state, nonce=nonce, verifier=verifier), 302)
        cookies.write_login(response, {"state": state, "nonce": nonce, "verifier": verifier}, LOGIN_SECONDS)
        return response

    def _back(**query: str) -> Response:
        response = RedirectResponse("/" + (f"?{urlencode(query)}" if query else ""), 303)
        cookies.clear(response, cookies.login)
        return response

    @app.get("/auth/callback")
    async def callback(request: Request) -> Response:
        params = request.query_params
        if params.get("error"):
            reason = (params.get("error_description") or params["error"]).splitlines()[0][:300]
            _log("sign-in refused", reason=reason)
            return _back(auth_error=reason)
        flow = cookies.read_login(request)
        if flow is None or not hmac.compare_digest(params.get("state", "").encode(), str(flow.get("state", "")).encode()):
            return _back(auth_error="This sign-in expired or was not started here. Sign in again.")
        try:
            signed = await idp.redeem_code(code=params.get("code", ""), verifier=str(flow["verifier"]), nonce=str(flow["nonce"]))
        except SignInError as err:
            _log("sign-in failed", status=err.status, reason=str(err))
            return _back(auth_error=str(err))
        data = {"oid": signed.oid, "upn": signed.upn, "name": signed.name, "user": signed.user, "groups": signed.groups, "csrf": secrets.token_urlsafe(32)}
        response = _back()
        cookies.write_session(request, response, data, cfg.session_seconds)
        _log("signed in", oid=signed.oid, user=signed.user, groups=len(signed.groups))
        return response

    @app.post("/auth/logout")
    async def logout(request: Request, data: Writable) -> Response:
        response = JSONResponse({"signed_out": True})
        cookies.clear_session(request, response)
        return response

    # ── API ───────────────────────────────────────────────────────────────────────────

    @app.get("/api/me")
    async def me(data: Session) -> dict[str, Any]:
        who = actor(data)
        return {
            "name": who.name,
            "upn": who.upn,
            "csrf": data.get("csrf", ""),
            "expires": data.get("exp"),
            "version": cfg.version,
            "release_notes": release_notes(cfg.version),
            "kubernetes": {"user": who.user, "groups": list(who.groups)},
            "environments": [
                {
                    "name": e.name,
                    "production": e.production,
                    "runs_jobs": e.runs_jobs,
                    "clone_from": e.clone_from,
                    "shares_to": cfg.catalog.cloning_from(e.name),
                    "downloads": not e.production,
                }
                for e in cfg.catalog.environments.values()
            ],
        }

    @app.get("/api/environments/{env}/databases")
    async def databases(env: EnvName, data: Session) -> dict[str, Any]:
        return {"databases": await console.databases(actor(data), env)}

    @app.get("/api/environments/{env}/databases/{db}/snapshots")
    async def snapshots(env: EnvName, db: DbName, data: Session) -> dict[str, Any]:
        return {"snapshots": await console.snapshots(actor(data), env, db)}

    @app.post("/api/environments/{env}/databases/{db}/snapshots", status_code=201)
    async def take_snapshot(env: EnvName, db: DbName, body: Note, data: Writable) -> dict[str, Any]:
        return await console.take_snapshot(actor(data), env, db, body.note.strip())

    @app.get("/api/environments/{env}/users")
    async def users(env: EnvName, data: Session) -> dict[str, Any]:
        return {"users": await console.users(actor(data), env)}

    @app.post("/api/environments/{env}/databases/{db}/snapshots/{sid}/share")
    async def share(env: EnvName, db: DbName, sid: SnapshotId, body: Share, data: Writable) -> dict[str, Any]:
        return await console.share(actor(data), env, db, sid, environment=body.environment, namespace=body.namespace)

    @app.get("/api/environments/{env}/databases/{db}/shared")
    async def shared(env: EnvName, db: DbName, data: Session) -> dict[str, Any]:
        return {"snapshots": await console.shared(actor(data), env, db)}

    @app.post("/api/environments/{env}/databases/{db}/restore", status_code=201)
    async def restore(env: EnvName, db: DbName, body: Restore, data: Writable) -> dict[str, Any]:
        return await console.restore(
            actor(data), env, db, source_environment=body.source_environment, source_database=body.source_database, snapshot=body.snapshot, into=body.into, reason=body.reason.strip(),
        )  # fmt: skip

    @app.delete("/api/environments/{env}/databases/{db}/copy", status_code=204)
    async def drop_copy(env: EnvName, db: DbName, data: Writable) -> Response:
        await console.drop_copy(actor(data), env, db)
        return Response(status_code=204)

    @app.get("/api/environments/{env}/databases/{db}/snapshots/{sid}/download")
    async def download(env: EnvName, db: DbName, sid: SnapshotId, data: Session) -> Response:
        container, blob, filename = await console.download(actor(data), env, db, sid)

        async def body() -> AsyncIterator[bytes]:
            async with container.download(blob) as (_, chunks):
                async for chunk in chunks:
                    yield chunk

        return StreamingResponse(body(), media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/environments/{env}/jobs")
    async def list_jobs(env: EnvName, data: Session) -> dict[str, Any]:
        return {"jobs": await console.jobs(actor(data), env)}

    @app.post("/api/environments/{env}/jobs/{name}/approve")
    async def approve(env: EnvName, name: JobName, data: Writable) -> dict[str, Any]:
        return await console.approve(actor(data), env, name)

    @app.post("/api/environments/{env}/jobs/{name}/decline", status_code=204)
    async def decline(env: EnvName, name: JobName, body: Reason, data: Writable) -> Response:
        await console.decline(actor(data), env, name, body.reason.strip())
        return Response(status_code=204)

    @app.get("/api/environments/{env}/audit")
    async def audit_log(env: EnvName, data: Session) -> dict[str, Any]:
        return {"entries": await console.audit_log(actor(data), env)}

    if cfg.static_dir is not None:
        app.mount("/", StaticFiles(directory=cfg.static_dir, html=True), name="frontend")

    return app
