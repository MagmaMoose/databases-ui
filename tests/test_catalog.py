from __future__ import annotations

from pathlib import Path

import pytest

from databases_ui.__main__ import check
from databases_ui.catalog import CatalogError, load_catalog

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "catalog.yaml"
GROUP = "6f1c2b7e-3a44-4d0e-9a51-2b8e0c7d9f10"
ENVS = "environments:\n  - {name: dev, storage: {url: 'https://a.blob.core.windows.net/dev'}}\n  - {name: prd, production: true, cluster: prod, storage: {url: 'https://a.blob.core.windows.net/prd'}}\n"


def test_the_example_holds_together(capsys):
    assert check(EXAMPLE) == 0
    assert "4 environments, 2 teams, 8 databases granted" in capsys.readouterr().out
    catalog = load_catalog(EXAMPLE.read_text())
    assert catalog.environments["prd"].cnpg_cluster == "prod" and catalog.environments["dev"].cnpg_cluster == "dev"
    grant = catalog.grant({"payments"}, "acc", "payments_acc")
    assert grant is not None and grant.workloads == (("deployment", "acc-payments", "payments-api"), ("deployment", "acc-payments", "payments-worker"))
    assert sorted(catalog.databases("prd")) == ["keycloak_prd", "payments_prd"]
    assert catalog.grant({"identity"}, "prd", "keycloak") is None


def test_groups_select_teams():
    catalog = load_catalog(ENVS + f"teams:\n  - {{id: a, name: A, groups: ['{GROUP.upper()}']}}\n")
    assert catalog.groups == {GROUP} and [t.id for t in catalog.teams_of(frozenset({GROUP}))] == ["a"]


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
        (ENVS + "  - {name: dev, storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "'dev' is listed twice"),
        ("environments:\n  - {name: dev, cloneFrom: [acc], storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "unknown environment 'acc'"),
        ("environments:\n  - {name: dev, cloneFrom: [dev], storage: {url: 'https://a.blob.core.windows.net/x'}}\n", "lists itself"),
        (ENVS + "teams:\n  - {id: a, name: A, groups: [not-a-guid]}\n", "not a group object id"),
        (ENVS + f"teams:\n  - {{id: a, name: A, groups: ['{GROUP}']}}\n  - {{id: a, name: B, groups: ['{GROUP}']}}\n", "team 'a' is listed twice"),
        (ENVS + f"teams:\n  - {{id: a, name: A, groups: ['{GROUP}'], databases: [{{name: x, environments: [qa]}}]}}\n", "unknown environment 'qa'"),
        (ENVS + f"teams:\n  - {{id: a, name: A, groups: ['{GROUP}'], databases: [{{name: 'Bad Name'}}]}}\n", "not a database name"),
        (ENVS + f"teams:\n  - {{id: a, name: A, groups: ['{GROUP}'], databases: [{{name: x, workloads: [pod/a/b]}}]}}\n", "deployment/<namespace>/<name>"),
        (ENVS + f"teams:\n  - {{id: a, name: A, groups: ['{GROUP}'], extra: 1}}\n", "extra"),
    ],
)
def test_problems_are_named(text, problem):
    with pytest.raises(CatalogError) as err:
        load_catalog(text)
    assert any(problem in p for p in err.value.problems), err.value.problems


def test_another_cluster_is_reached_with_its_own_token():
    catalog = load_catalog("environments:\n  - name: dev\n    storage: {url: 'https://a.blob.core.windows.net/dev'}\n    kubernetes: {server: 'https://dev.example.test:6443', caFile: /ca.crt, tokenFile: /token}\n")
    kube = catalog.environments["dev"].kubernetes
    assert kube.server == "https://dev.example.test:6443" and kube.token_file == "/token" and catalog.environments["dev"].runs_jobs


def test_check_reports_every_problem(tmp_path, capsys):
    (tmp_path / "bad.yaml").write_text(ENVS + "teams:\n  - {id: a, name: A, groups: [x]}\n")
    assert check(tmp_path / "bad.yaml") == 1
    assert "not a group object id" in capsys.readouterr().err
    assert check(tmp_path / "missing.yaml") == 1
