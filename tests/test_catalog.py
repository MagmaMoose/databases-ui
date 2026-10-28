from __future__ import annotations

from pathlib import Path

import pytest

from databases_ui.__main__ import check
from databases_ui.catalog import (
    RESTORE,
    RESTORE_PRODUCTION,
    SCRIPT,
    SCRIPT_PRODUCTION,
    CatalogError,
    Check,
    load_catalog,
)

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "catalog.yaml"
EXT = "  - {name: dev, storage: {url: 'https://a.blob.core.windows.net/dev'}, external: [%s]}\n"
DEV = "  - {name: dev, storage: {url: 'https://a.blob.core.windows.net/dev'}}\n"


def test_the_example_holds_together(capsys):
    assert check(EXAMPLE) == 0
    assert "4 environments, dev in this cluster, 1 external database" in capsys.readouterr().out
    catalog = load_catalog(EXAMPLE.read_text())
    assert catalog.environments["prd"].cnpg_cluster == "prod" and catalog.environments["dev"].cnpg_cluster == "dev"
    assert [e.name for e in catalog.environments.values() if e.runs_jobs] == ["dev"]
    assert catalog.cloning_from("acc") == ["dev", "tst"] and catalog.cloning_from("prd") == []
    billing = catalog.environments["dev"].server("billing")
    assert billing is not None and (billing.host, billing.port, billing.tls, billing.ca_secret) == ("billing-db.example.internal", 5432, "verify-full", "billing-db-ca")
    assert catalog.environments["dev"].server("nothing") is None


def test_access_defaults_and_overrides():
    catalog = load_catalog("environments:\n" + DEV + "  - name: prd\n    production: true\n    storage: {url: 'https://a.blob.core.windows.net/prd'}\n    access:\n      read: {verb: get, resource: deployments/scale, group: apps}\n")
    dev, prd = catalog.environments["dev"], catalog.environments["prd"]
    assert dev.check("restore") == RESTORE and prd.check("restore") == RESTORE_PRODUCTION
    assert dev.check("script") == SCRIPT and prd.check("script") == SCRIPT_PRODUCTION
    assert prd.check("read") == Check(verb="get", resource="deployments/scale", group="apps")


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("environments: []\n", "environments"),
        ("- not a mapping\n", "environments"),
        ("environments: [\n", "not YAML"),
        ("environments:\n  - {name: dev, storage: {url: 'ftp://a/b'}}\n", "must be https"),
        ("environments:\n  - {name: dev, storage: {url: 'https://a.blob.core.windows.net'}}\n", "the container's URL"),
        ("environments:\n  - {name: dev, storage: {url: 'http://azurite.storage.svc:10000/acct/dev'}, kubernetes: elsewhere}\n", "kubernetes"),  # DevSkim: ignore DS137138 - a local emulator's address
        ("environments:\n  - {name: Dev, storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "name"),
        ("environments:\n" + DEV + DEV, "'dev' is listed twice"),
        ("environments:\n  - {name: dev, cloneFrom: [acc], storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "unknown environment 'acc'"),
        ("environments:\n  - {name: dev, cloneFrom: [dev], storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "lists itself"),
        ("environments:\n  - {name: dev, kubernetes: none, storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "no environment runs here"),
        ("environments:\n  - {name: dev, storage: {url: 'https://a.blob.core.windows.net/x'}, access: {restore: {verb: 'Get', resource: secrets}}}\n", "verb"),
        ("environments:\n  - {name: dev, storage: {url: 'https://a.blob.core.windows.net/x'}, teams: []}\n", "teams"),
        ("environments:\n" + EXT % "{name: ledger, host: db.example.internal, database: l, user: u, passwordSecret: s, tls: require, caSecret: ca}", "caSecret is for tls: verify-full"),
        ("environments:\n" + EXT % "{name: ledger, host: a.example, database: l, user: u, passwordSecret: s}, {name: ledger, host: b.example, database: l, user: u, passwordSecret: s}", "external database 'ledger' twice"),
        ("environments:\n" + DEV + "  - {name: acc, kubernetes: none, storage: {url: 'https://a.blob.core.windows.net/acc'}, external: [{name: l, host: a.example, database: l, user: u, passwordSecret: s}]}\n", "no Job here reaches"),
        ("environments:\n" + EXT % "{name: ledger, host: 'db.example.internal:5432', database: l, user: u, passwordSecret: s}", "host"),
        ("environments:\n" + EXT % "{name: ledger, host: DB.example.internal, database: l, user: u, passwordSecret: s}", "host"),
        ("environments:\n" + EXT % "{name: ledger, host: db.example.internal, port: 70000, database: l, user: u, passwordSecret: s}", "port"),
        ("environments:\n" + EXT % "{name: ledger, host: db.example.internal, database: l, user: u, passwordSecret: Bad_Secret}", "passwordSecret"),
        ("environments:\n" + EXT % "{name: ledger, host: db.example.internal, database: l, user: u}", "passwordSecret"),
        ("environments:\n" + EXT % "{name: ledger, host: db.example.internal, database: l, user: u, passwordSecret: s, tls: allow}", "tls"),
    ],
)
def test_problems_are_named(text, problem):
    with pytest.raises(CatalogError) as err:
        load_catalog(text)
    assert any(problem in p for p in err.value.problems), err.value.problems


def test_check_reports_every_problem(tmp_path, capsys):
    (tmp_path / "bad.yaml").write_text("environments:\n" + DEV + DEV)
    assert check(tmp_path / "bad.yaml") == 1
    assert "listed twice" in capsys.readouterr().err
    assert check(tmp_path / "missing.yaml") == 1
