# databases-ui

FastAPI backend (`src/databases_ui`) serving a React/TypeScript console (`frontend/`), and the Jobs'
runner (`runner.py`) in the same image, with a Helm chart (`charts/databases-ui`). State lives in
Kubernetes (Database resources, Jobs, RBAC) and Blob Storage (snapshots and their shares, scripts,
the audit log); the server stores nothing. External databases (not CloudNativePG) are the
catalog's `external` entries; scripts are `sql.py` (what may run) and `runner.script`.

## Things that fail silently

- **The user and groups the access reviews ask about must be the API server's.** They come from
  `DATABASES_UI_USERNAME_*` and `DATABASES_UI_GROUPS_*` and the ID token, as the session took them
  at sign-in. A claim, a prefix or a lowercased name that differs from the cluster's authentication
  configuration asks about someone RBAC doesn't know, and every database disappears. A change in
  someone's groups reaches the app when they sign in again.
- **A database outside your namespaces answers 404, never 403.** Keep it that way in new routes
  (`Console._database`), or a 403 tells who may not see a database that it exists.
- **The admission policy (`templates/jobpolicy.yaml`) knows the Job's shape.** It checks the
  variables `PGPASSWORD`, `DBUI_STORAGE_SAS`, `PGHOST`, `PGSSLMODE`, `DBUI_BLOB_URL` and
  `DBUI_AUDIT_URL`, the volume kinds and the `job` argument that `jobs.py` writes. Renaming one or
  adding a Secret, a volume or a container in `jobs.py` without changing the policy in the same
  change makes Kubernetes refuse every Job.
- **Job pods carry `app.kubernetes.io/component: job`, never `web`.** `web` is the Service and
  PodDisruptionBudget selector; the chart's database NetworkPolicy admits `job`.
- **COMMIT reaches psql only after pg_restore exited 0** (`runner.apply`). The restore drops what
  the owner owns first, so a COMMIT after a partial script would leave the database half empty;
  any new early exit there must close psql without it.
- **A storage lifecycle rule must be scoped to `snapshots/`.** One on the whole container also
  deletes the audit log under `audit/`.
- **Snapshots leave extensions out** (`--exclude-extension=*`): the owner role can't drop one the
  operator created. A copy gets its extensions from its `Database` resource, copied from the
  source's `spec.extensions`; a restore into a database without them fails, and changes nothing.
- **No `from __future__ import annotations` in `app.py`.** The routes use `Annotated` path types;
  postponed annotations turn them into strings and FastAPI answers 422 everywhere.
- **The page may hold no inline script or style.** The CSP blocks it and the page renders blank;
  CI greps `frontend/dist/index.html` for it.
- **The admission policy pins an external database's host, port, TLS mode and password Secret
  from the rendered catalog** (`templates/jobpolicy.yaml`). A catalog in `existingConfigMap` isn't
  rendered, so its external databases' Jobs are refused; a new Job variable that names another
  host or Secret needs the policy changed in the same change.
- **A script's output reaches the server through the pod's termination message**, which Kubernetes
  caps at 4096 bytes. `runner.termination_message` trims the output, never the JSON; writing it any
  other way truncates mid-string and the server shows the raw text as the failure reason.
- **A script travels in the Job (`DBUI_SCRIPT`), and a single environment variable is capped near
  128 kB by Linux** (`MAX_ARG_STRLEN`), so `sql.MAX_BYTES` stays at 100 kB. Moving the script to a
  volume means the admission policy's volume rule changes too.
- **`sql.problems` is the only thing between a script and psql's backslash commands** (`\!` runs a
  shell in the Job's pod). Keep it a lexer: quoted strings, dollar quotes, comments and BEGIN
  ATOMIC bodies must be skipped, or ordinary function bodies get refused.
- **The image name follows the repository name** (`ghcr.io/magmamoose/databases-ui`). The chart's
  default image is that name, and the Jobs run the same image; a rename means a chart change in the
  same PR.
- **No client or employer names anywhere in this public repository**: code, tests, fixtures,
  docs, commits. Examples use example.com and fictional teams.
