"""`python -m databases_ui [serve | check <catalog> | job]`.

serve  load the config, build the app, serve it with uvicorn (the default)
check  validate a catalog file and print every problem: for the catalog's own CI
job    do one Job's work, as the server wrote it into the Job (runner.py)

TLS ends at the ingress in front of it; this listens on plain http inside the pod.
"""

import json
import logging
import sys
from pathlib import Path

import uvicorn

from .app import create_app
from .catalog import CatalogError, load_catalog
from .config import ConfigError, load_config


def check(path: Path) -> int:
    try:
        catalog = load_catalog(path.read_text(), path.name)
    except OSError as err:
        print(f"cannot read {path}: {err.__class__.__name__}", file=sys.stderr)
        return 1
    except CatalogError as err:
        for problem in err.problems:
            print(problem, file=sys.stderr)
        return 1
    here = sorted(e.name for e in catalog.environments.values() if e.runs_jobs)
    print(f"{len(catalog.environments)} environments, {', '.join(here)} in this cluster: the catalog holds together.")
    return 0


def serve() -> None:
    try:
        cfg = load_config()
    except ConfigError as err:
        print(json.dumps({"msg": "configuration refused", "reason": str(err)}), file=sys.stderr)
        sys.exit(1)
    logging.getLogger("databases_ui").info(
        json.dumps(
            {
                "msg": "starting",
                "version": cfg.version,
                "origin": cfg.origin,
                "tenant": cfg.tenant_id,
                "client": cfg.client_id,
                "credential": "workload identity" if cfg.federated_token_file else "client secret",
                "storage": "sas" if cfg.storage_sas else "entra",
                "environments": sorted(cfg.catalog.environments),
                "storage_client": cfg.storage_client_id,
                "namespace_label": cfg.namespace_label,
                # Who the access reviews ask about, as the API server derives it from a token.
                "kubernetes_user": f"{cfg.username_prefix}<{cfg.username_claim}>",
                "kubernetes_groups": f"{cfg.groups_prefix}<{cfg.groups_claim}>",
                "job_image": cfg.job_image,
            }
        )
    )
    uvicorn.run(
        create_app(cfg),
        # All interfaces: a container port, reached only through the ingress in front of it.
        host="0.0.0.0",  # noqa: S104 # nosec B104
        port=cfg.port,
        # Longer than common ingress idle timeouts, so this side never closes a keep-alive
        # connection the proxy is about to reuse (a 502).
        timeout_keep_alive=95,
        access_log=False,
        server_header=False,
        log_level="info",
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    # httpx logs every request at INFO, and its URLs carry SAS tokens.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "check" and len(sys.argv) == 3:
        sys.exit(check(Path(sys.argv[2])))
    if command == "job":
        from .runner import main as job

        sys.exit(job())
    if command != "serve":
        print("usage: python -m databases_ui [serve | check <catalog> | job]", file=sys.stderr)
        sys.exit(2)
    serve()


if __name__ == "__main__":
    main()
