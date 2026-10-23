from __future__ import annotations

import pytest

from databases_ui.config import ConfigError, load_config


def test_a_complete_environment_loads(env):
    cfg = load_config(env)
    assert cfg.redirect_uri == "https://databases.example.test/auth/callback" and cfg.secure_cookies
    assert sorted(cfg.catalog.environments) == ["acc", "dev", "prd", "tst"] and cfg.job_service_account == "databases-ui-job"
    assert (cfg.username_claim, cfg.username_prefix, cfg.groups_claim, cfg.groups_prefix) == ("preferred_username", "oidc:", "groups", "oidc:")
    assert cfg.storage_sas == "" and cfg.static_dir is None and cfg.session_seconds == 8 * 3600


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"AZURE_TENANT_ID": "nope"}, "AZURE_TENANT_ID must be a GUID"),
        ({"AZURE_FEDERATED_TOKEN_FILE": ""}, "set AZURE_FEDERATED_TOKEN_FILE"),
        ({"DATABASES_UI_SESSION_KEY": "short"}, "at least 32 characters"),
        ({"DATABASES_UI_PUBLIC_ORIGIN": "http://databases.example.test"}, "must be https"),  # DevSkim: ignore DS137138 - deliberately invalid input; the test asserts config rejects it
        ({"DATABASES_UI_PUBLIC_ORIGIN": "https://databases.example.test/ui"}, "with no path"),
        ({"AZURE_AUTHORITY_HOST": "http://login.example.test"}, "AZURE_AUTHORITY_HOST must be an https host"),  # DevSkim: ignore DS137138 - deliberately invalid input
        ({"DATABASES_UI_CATALOG": ""}, "DATABASES_UI_CATALOG is required"),
        ({"DATABASES_UI_CATALOG": "/nowhere/catalog.yaml"}, "cannot read the catalog"),
        ({"DATABASES_UI_JOB_IMAGE": ""}, "DATABASES_UI_JOB_IMAGE is required"),
        ({"DATABASES_UI_JOB_SERVICE_ACCOUNT": "Not_A_Name"}, "service account name"),
        ({"DATABASES_UI_STORAGE_SAS": "sv=2022-11-02&sig=x"}, "needs both"),
        ({"DATABASES_UI_STORAGE_SAS": "sv=2022-11-02", "DATABASES_UI_STORAGE_SAS_SECRET": "sas/token"}, "a SAS token"),
        ({"DATABASES_UI_STORAGE_SAS": "sv=2022-11-02&sig=x", "DATABASES_UI_STORAGE_SAS_SECRET": "no-key"}, "<secret name>/<key>"),
        ({"AZURE_FEDERATED_TOKEN_FILE": "", "AZURE_CLIENT_SECRET": "s"}, "the Jobs reach storage through workload identity"),
        ({"DATABASES_UI_STORAGE_CLIENT_ID": "not-a-guid"}, "DATABASES_UI_STORAGE_CLIENT_ID must be a GUID"),
        ({"DATABASES_UI_NAMESPACE_LABEL": "not a label"}, "DATABASES_UI_NAMESPACE_LABEL must be a label key"),
        ({"DATABASES_UI_USERNAME_CLAIM": "has spaces"}, "DATABASES_UI_USERNAME_CLAIM must be a claim name"),
        ({"DATABASES_UI_GROUPS_PREFIX": "no spaces"}, "DATABASES_UI_GROUPS_PREFIX must be a prefix"),
        ({"DATABASES_UI_SESSION_HOURS": "48"}, "between 1 and 24"),
        ({"PORT": "eighty"}, "whole numbers"),
        ({"DATABASES_UI_BRAND_ACCENT": "teal"}, "#rrggbb"),
    ],
)
def test_refused(env, change, message):
    with pytest.raises(ConfigError, match=message):
        load_config({**env, **change})


def test_refused_without_an_origin(env):
    del env["DATABASES_UI_PUBLIC_ORIGIN"]
    with pytest.raises(ConfigError, match="DATABASES_UI_PUBLIC_ORIGIN is required"):
        load_config(env)


def test_a_broken_catalog_stops_startup(env, tmp_path):
    (tmp_path / "bad.yaml").write_text("environments: []\n")
    with pytest.raises(ConfigError, match="does not hold together"):
        load_config({**env, "DATABASES_UI_CATALOG": str(tmp_path / "bad.yaml")})


def test_secrets_from_files_and_a_sas_with_a_client_secret(env, tmp_path):
    (tmp_path / "sas").write_text("?sv=2022-11-02&sig=abc\n")
    (tmp_path / "key").write_text("s" * 40)
    cfg = load_config({
        **env, "AZURE_FEDERATED_TOKEN_FILE": "", "AZURE_CLIENT_SECRET": "secret", "DATABASES_UI_SESSION_KEY": "", "DATABASES_UI_SESSION_KEY_FILE": str(tmp_path / "key"),
        "DATABASES_UI_STORAGE_SAS_FILE": str(tmp_path / "sas"), "DATABASES_UI_STORAGE_SAS_SECRET": "databases-ui-storage/sas", "DATABASES_UI_BRAND_ACCENT": "#0B6E4F",
    })  # fmt: skip
    assert cfg.storage_sas == "sv=2022-11-02&sig=abc" and cfg.session_key == b"s" * 40 and cfg.client_secret == "secret"
    assert cfg.branding.accent == "#0b6e4f"
    with pytest.raises(ConfigError, match="cannot read DATABASES_UI_SESSION_KEY_FILE"):
        load_config({**env, "DATABASES_UI_SESSION_KEY": "", "DATABASES_UI_SESSION_KEY_FILE": str(tmp_path / "missing")})


def test_a_storage_identity_and_a_label_of_your_own(env):
    cfg = load_config({**env, "DATABASES_UI_STORAGE_CLIENT_ID": "CCCCCCCC-bbbb-cccc-dddd-eeeeeeeeeeee", "DATABASES_UI_NAMESPACE_LABEL": "example.com/app-namespace"})
    assert cfg.storage_client_id == "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee" and cfg.namespace_label == "example.com/app-namespace"
    assert load_config(env).storage_client_id == cfg.client_id


def test_plain_http_only_on_loopback(env):
    assert load_config({**env, "DATABASES_UI_PUBLIC_ORIGIN": "http://localhost:8080"}).secure_cookies is False  # DevSkim: ignore DS162092 - loopback is allowed for local development
