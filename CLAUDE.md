# databases-ui

FastAPI backend (`src/databases_ui`) serving a React/TypeScript console (`frontend/`), and the Jobs'
runner (`runner.py`) in the same image, with a Helm chart (`charts/databases-ui`). State lives in
Kubernetes (Database resources, Jobs) and Blob Storage (snapshots, the audit log); the server
stores nothing.

## Things that fail silently

- **The session carries only the groups the catalog named at sign-in.** A team added to the
  catalog reaches its members when they sign in again, not before.
- **The admission policy (`templates/jobpolicy.yaml`) knows the Job's shape.** It checks the
  variables `PGPASSWORD`, `DBUI_STORAGE_SAS`, `PGHOST`, `PGSSLMODE`, `DBUI_BLOB_URL` and
  `DBUI_AUDIT_URL`, the volume kinds and the `job` argument that `jobs.py` writes. Renaming one or
  adding a Secret, a volume or a container in `jobs.py` without changing the policy in the same
  change makes Kubernetes refuse every Job.
- **Job pods carry `app.kubernetes.io/component: job`, never `web`.** `web` is the Service and
  PodDisruptionBudget selector; the chart's database NetworkPolicy admits `job`.
- **A storage lifecycle rule must be scoped to `snapshots/`.** One on the whole container also
  deletes the audit log under `audit/`.
- **Snapshots leave extensions out** (`--exclude-extension=*`): the owner role can't drop one the
  operator created. A copy gets its extensions from its `Database` resource, copied from the
  source's `spec.extensions`; a restore into a database without them fails, and changes nothing.
- **No `from __future__ import annotations` in `app.py`.** The routes use `Annotated` path types;
  postponed annotations turn them into strings and FastAPI answers 422 everywhere.
- **`{env}` in the catalog needs block lists.** YAML reads braces inside `[ ]` as a mapping.
- **The page may hold no inline script or style.** The CSP blocks it and the page renders blank;
  CI greps `frontend/dist/index.html` for it.
- **The image name follows the repository name** (`ghcr.io/magmamoose/databases-ui`). The chart's
  default image is that name, and the Jobs run the same image; a rename means a chart change in the
  same PR.
- **No client or employer names anywhere in this public repository**: code, tests, fixtures,
  docs, commits. Examples use example.com and fictional teams.
