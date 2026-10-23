from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from conftest import CLIENT, make_client, sign_in, write_headers

from databases_ui.app import favicon_svg, release_notes, theme_css
from databases_ui.config import Branding


def test_signed_out_until_signed_in(client):
    assert client.get("/api/me").json() == {"error": "signed_out"}
    assert client.get("/api/environments/dev/databases").status_code == 401


def test_sign_in_names_the_person_as_the_api_server_does(client, world):
    me = sign_in(client, world, "Alice.Smith@Example.com", groups=["k8s-dev-payments-write", "k8s-prd-payments-admin"])
    # The claim as it is, prefixed: no lowercasing, or RBAC would know someone else.
    assert me["kubernetes"] == {"user": "oidc:Alice.Smith@Example.com", "groups": ["oidc:k8s-dev-payments-write", "oidc:k8s-prd-payments-admin"]}
    login = world.entra.token_requests[-1]
    assert login["grant_type"] == ["authorization_code"] and login["client_assertion"] == ["federated-token"] and login["client_id"] == [CLIENT]
    assert login["scope"] == ["openid profile"]
    cookie = next(c for c in client.cookies.jar if c.name.startswith("__Host-databases-ui"))
    assert cookie.secure and cookie.path == "/"


def test_the_claims_and_prefixes_follow_the_cluster(world, env):
    client = make_client(world, {**env, "DATABASES_UI_GROUPS_CLAIM": "roles", "DATABASES_UI_GROUPS_PREFIX": "", "DATABASES_UI_USERNAME_PREFIX": "entra#"})
    world.entra.extra = {"roles": ["k8s-dev-payments-read"]}
    assert sign_in(client, world, groups=[])["kubernetes"] == {"user": "entra#alice@example.com", "groups": ["k8s-dev-payments-read"]}


def test_a_token_without_the_username_claim_is_refused(world, env):
    client = make_client(world, {**env, "DATABASES_UI_USERNAME_CLAIM": "upn"})
    response = client.get("/auth/login")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    world.entra.challenge, world.entra.nonce = query["code_challenge"][0], query["nonce"][0]
    assert "has+no+upn" in client.get(f"/auth/callback?code=good-code&state={query['state'][0]}").headers["location"]


def test_a_sign_in_with_too_many_groups_is_refused(client, world):
    world.entra.overage = True
    response = client.get("/auth/login")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    world.entra.challenge, world.entra.nonce = query["code_challenge"][0], query["nonce"][0]
    done = client.get(f"/auth/callback?code=good-code&state={query['state'][0]}")
    assert "auth_error=" in done.headers["location"] and "do+not+fit+in+the+ID+token" in done.headers["location"]


def test_another_tenant_a_bad_state_and_a_refusal(client, world):
    world.entra.tenant = "99999999-2222-3333-4444-555555555555"
    response = client.get("/auth/login")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    world.entra.challenge, world.entra.nonce = query["code_challenge"][0], query["nonce"][0]
    assert "another+tenant" in client.get(f"/auth/callback?code=good-code&state={query['state'][0]}").headers["location"]
    assert "expired" in client.get("/auth/callback?code=good-code&state=forged").headers["location"]
    assert "AADSTS50105" in client.get("/auth/callback?error=access_denied&error_description=AADSTS50105:+not+assigned").headers["location"]


def test_logout_needs_the_csrf_token(client, world):
    me = sign_in(client, world)
    assert client.post("/auth/logout").status_code == 403
    assert client.post("/auth/logout", headers=write_headers(me)).json() == {"signed_out": True}
    assert client.get("/api/me").status_code == 401


def test_headers_and_theme(world, env):
    client = make_client(world, {**env, "DATABASES_UI_BRAND_ACCENT": "#0b6e4f", "APP_VERSION": "v0.1.0"})
    response = client.get("/healthz")
    assert response.json() == {"status": "ok"}
    assert "default-src 'none'" in response.headers["content-security-policy"] and response.headers["x-frame-options"] == "DENY"
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert "html:root{--accent:#0b6e4f" in client.get("/theme.css").text
    assert 'fill="#0b6e4f"' in client.get("/favicon.svg").text
    assert sign_in(client, world)["release_notes"] == "https://github.com/MagmaMoose/databases-ui/releases/tag/v0.1.0"


def test_release_notes_and_branding_helpers():
    assert release_notes("pr-12") == "https://github.com/MagmaMoose/databases-ui/pull/12" and release_notes("dev") is None
    assert theme_css(Branding()) == "" and "@media (prefers-color-scheme: dark)" in theme_css(Branding(accent="#000000", accent_dark="#ffffff"))
    assert 'fill="#0f766e"' in favicon_svg(Branding())
