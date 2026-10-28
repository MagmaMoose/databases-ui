# databases-ui

Let app teams snapshot, restore and clone their own PostgreSQL databases on
[CloudNativePG](https://cloudnative-pg.io/), and run their migration scripts on them or on any other
PostgreSQL, without asking the platform team. People sign in with Entra ID and see the databases of
the namespaces Kubernetes lets them into; every action runs as a Kubernetes Job next to the
database.

- **Access is Kubernetes RBAC.** A `Database` resource names its app's namespace in a label (an
  external database, in the catalog). Who may see it, snapshot it, restore it or run a script on it
  is what the API server says that person may do in that namespace, asked with a
  SubjectAccessReview: `view` sees, `edit` snapshots, restores and runs scripts, `admin` restores
  and runs scripts in production. There is no second list of who may do what.
- **Snapshots on demand.** `pg_dump` in the custom format, streamed straight into Azure Blob
  Storage with who took it and why. Nothing touches a disk, and nothing leaves the cluster.
- **Restore in place or into a copy, exactly.** In one transaction, everything the database's owner
  owns is dropped and the snapshot restored: the database ends up as the snapshot was, or, if
  anything fails, as it was before. In place, the app's workloads are scaled to 0 first and back
  afterwards; into a copy, a second database `<name>_copy` appears next to it, created and dropped
  by CloudNativePG.
- **Clone across environments, once shared.** Someone who may snapshot a database in acc shares a
  snapshot with a namespace in dev; whoever may restore there can then clone it.
- **Scripts, checked or run.** Paste or open a `.sql` file: it runs as the database's owner in one
  transaction. A check runs it, shows what it said and rolls back; a run commits, after a snapshot
  if asked. One failing statement, or a lock it can't get within 15 seconds, changes nothing.
- **Any PostgreSQL.** A server outside CloudNativePG goes in the catalog with its host, a login and
  that login's password Secret: it is listed, snapshotted and scripted like the others.
- **Production is careful.** A restore or a script in a production environment waits for a second
  person, who reads the script before approving it; snapshots taken there can't be downloaded,
  and a script's output there names its statements but shows no rows.
- **Database users**: the owner roles of the databases you see, and the Secrets with their
  passwords.
- **An audit log** of who asked for what, who approved it and how it ended, per environment.
- **No superuser.** Each Job connects as the database's owner role, with the password from that
  managed role's Secret. The server itself reads no Secret.

## How it works

```mermaid
sequenceDiagram
    participant P as Person
    participant U as databases-ui
    participant K as Kubernetes API
    participant J as Job (in the database namespace)
    participant D as PostgreSQL
    participant B as Blob Storage
    P->>U: sign in (Entra ID: the app the API server trusts)
    P->>U: snapshot payments_dev
    U->>K: Database resource: namespace label, owner, Cluster's managed roles
    U->>K: SubjectAccessReview: may oidc:ada get secrets in dev-payments?
    U->>K: create a Job (owner role, its password Secret, the CA certificate)
    K->>J: start the pod
    J->>D: pg_dump --format=custom
    J->>B: stream the dump into a block blob, commit it with its metadata
    J->>B: append the outcome to the audit log
    P->>U: restore it
    U->>K: create a Job
    J->>K: scale the app's workloads to 0
    J->>B: stream the snapshot through pg_restore
    J->>D: BEGIN; DROP OWNED BY CURRENT_USER; the snapshot; COMMIT
    J->>K: scale the workloads back
```

People sign in to the app registration the cluster's API server trusts, so their ID token carries
the claims the API server builds a user from. The server builds the same user and groups (with the
same claims and prefixes) and asks the API server about them in a SubjectAccessReview; the answers
are kept for a minute. It needs no token of theirs at the API server, and stores none.

The Jobs run this app's own image, which carries the PostgreSQL client, as
`python -m databases_ui job`. A Job's pod reads the owner's password from the kubelet, verifies the
server's certificate against the cluster's CA (`verify-full`), and talks to storage with its own
workload identity.

## The catalog

One instance runs in each cluster. Its catalog lists the environments: the one in its own cluster
runs Jobs, the others are where shared snapshots come from or go to (see
[`examples/catalog.yaml`](examples/catalog.yaml)):

```yaml
environments:
  - name: dev
    # The CloudNativePG Cluster (default: the environment's name) and its namespace (default:
    # database), which holds the Database resources, the role Secrets and the Jobs.
    cluster: dev
    namespace: database
    storage:
      url: https://<account>.blob.core.windows.net/dev-snapshots
    cloneFrom: [acc]       # environments whose shared snapshots may be restored here
  - name: acc
    kubernetes: none       # another cluster's
    storage:
      url: https://<account>.blob.core.windows.net/acc-snapshots
  - name: prd
    kubernetes: none
    production: true       # restores wait for a second person; no downloads
    cluster: prod
    storage:
      url: https://<other account>.blob.core.windows.net/prd-snapshots
```

What each action takes in the database's namespace, per environment, can be changed with
`access`; these are the defaults:

```yaml
    access:
      read: {verb: list, resource: pods}            # see a database, its snapshots, Jobs, audit
      snapshot: {verb: get, resource: secrets}      # snapshot, download, share
      restore: {verb: get, resource: secrets}       # restore, clone into, drop a copy
      # in a production environment:
      # restore: {verb: create, group: rbac.authorization.k8s.io, resource: rolebindings}
```

A snapshot holds all of a database's data, so taking one takes what reading the namespace's
Secrets takes. Check the file in CI before it reaches the server, which refuses to start with a
catalog that does not hold together:

```bash
docker run --rm -v "$PWD/catalog.yaml:/catalog.yaml:ro" ghcr.io/magmamoose/databases-ui:<tag> check /catalog.yaml
```

## The Database resources

The server finds a database's namespace and workloads on its `Database` resource:

```yaml
apiVersion: postgresql.cnpg.io/v1
kind: Database
metadata:
  name: payments-dev
  namespace: database
  labels:
    databases-ui/namespace: dev-payments          # the label key is config.namespaceLabel
  annotations:
    databases-ui/workloads: deployment/payments-api,statefulset/payments-worker
spec:
  cluster: {name: dev}
  name: payments_dev
  owner: payments_dev                             # a managed role with a passwordSecret
```

Without the label, a database belongs to its own namespace (`database`): only who may act there,
usually the platform team, sees it. Without the annotation, a restore in place scales nothing; the
owner's other sessions are still ended first.

## External databases

A PostgreSQL server that isn't a CloudNativePG `Database` (a VM, a managed service, another
operator's cluster) goes under its environment's `external` in the catalog:

```yaml
environments:
  - name: acc
    storage:
      url: https://<account>.blob.core.windows.net/acc-snapshots
    external:
      - name: billing                   # what this app calls it, unique in the environment
        namespace: acc-billing          # whose people may act on it; empty: the environment's own
        host: billing-db.example.internal
        port: 5432                      # the default
        database: billing               # the database on that server
        user: billing_owner             # the login the Jobs use
        passwordSecret: billing-db      # a Secret in the environment's namespace, key `password`
        caSecret: billing-db-ca         # its `ca.crt` verifies the server
        tls: verify-full                # the default; or require, or disable
```

- **The login is what a script can do.** Give the Jobs the schema's owner, as migration scripts
  need, not a superuser: a superuser's script can do anything to the server.
- **`verify-full` without a `caSecret`** checks the certificate against the image's public roots
  (`sslrootcert=system`), for a server with a public certificate. `require` encrypts without
  checking who answers; `disable` is for a server without TLS on a network you trust.
- **No restores.** A restore drops everything its login owns first, which on a server this app
  doesn't manage could be anything. Its snapshots are ordinary `pg_dump` archives: download one
  outside production, or restore it by hand.
- **The Jobs must reach it.** If the namespace's NetworkPolicies limit egress, admit the Jobs' pods
  (`app.kubernetes.io/component: job`) to the server's address and port. The admission policy
  admits exactly the catalog's host, port, TLS mode and password Secret, so the catalog has to be
  in the chart's values (`catalog.content` or `catalog.text`).

## What a script does

- **One transaction, every statement or none.** `psql` runs `BEGIN`, `SET LOCAL lock_timeout =
  '15s'`, the script with `ON_ERROR_STOP`, then `COMMIT` (run) or `ROLLBACK` (check). At the first
  error psql stops before either and PostgreSQL rolls everything back, so a check shows exactly
  what a run would do.
- **No BEGIN, COMMIT or psql commands.** A script that opens or ends a transaction of its own, or
  holds a backslash command (`\!`, `\connect`, `\o`), is refused before a Job exists, with the
  line. Statements that can't run in a transaction (`CREATE INDEX CONCURRENTLY`, `VACUUM`) fail.
- **A lock it can't get within 15 seconds fails the script**, rather than queueing the app's own
  queries behind an `ALTER TABLE` that waits for a long one.
- **A snapshot first, when asked.** A run can take a snapshot before it starts; if the snapshot
  fails, the script doesn't run.
- **What it said.** The last lines of psql's output (statement tags, notices, errors with the
  script's line numbers, and rows outside production) show under Activity. The script itself is
  kept beside the snapshots (`scripts/<database>/<id>.sql`) and in the Job, with its sha256 in the
  audit log.
- **In production, a second person reads it first.** The request waits, suspended; whoever approves
  sees the script the Job holds, which can't change after it is created.

## What a restore does

- **One transaction, the snapshot exactly.** `psql` runs `BEGIN`, `DROP OWNED BY CURRENT_USER`, then
  the script `pg_restore --clean --if-exists --no-owner --no-acl` makes of the snapshot, and
  `COMMIT` only once `pg_restore` has finished without an error. Tables created after the snapshot
  go too, so an app's migrations find the state they expect. On any error, or if the download
  breaks off, nothing changes.
- **`DROP OWNED` also revokes privileges granted to the owner role**, on objects in this database
  and on shared objects such as other databases. Owners that own their databases, as CloudNativePG
  sets them up, lose nothing.
- **No owners or grants from the snapshot.** Everything restored belongs to the database's owner
  role, so a snapshot moves between environments whose roles differ.
- **Extensions stay as they are.** Snapshots leave extensions out (`--exclude-extension=*`): the
  owner role can't drop one the operator created. The target database needs the same extensions;
  a copy gets those its `Database` resource lists.
- **In place, the workloads go first.** Each workload in the annotation is scaled to 0, its replica
  count kept in the annotation `databases-ui/replicas-before-restore`, and other sessions of the
  owner role are ended. Afterwards it gets its count back, also when the restore failed; if the Job
  itself dies halfway, the next restore restores the count from that annotation. A GitOps tool
  that sets `replicas` may scale the app back up during the restore; the transaction keeps the
  data right either way.
- **The owner must read everything.** `pg_dump` runs as the owner role, so objects in the database
  that belong to other roles and that it can't read stop the snapshot.

## Setting up

### 1. Storage

A container per environment, for example in a storage account per environment, with:

- a lifecycle rule that deletes blobs under `snapshots/` after as many days as snapshots should
  live; the audit log is under `audit/`, one append blob per month;
- network rules that admit the clusters' egress addresses: every transfer happens inside a
  cluster, the browser's downloads included, which the server streams;
- Storage Blob Data Contributor on it for the storage identity below, and, where an environment
  clones from this one, for that environment's storage identity too: its server lists what was
  shared with it and its Jobs read the snapshots.

### 2. Entra ID

- **Sign-in**: the app registration whose ID tokens the cluster's API server accepts (its client id
  is the API server's audience). Platform **Web**, redirect URI `https://<host>/auth/callback`.
  The sign-in asks for `openid profile`; nothing else. Its credential is a federated credential,
  audience `api://AzureADTokenExchange`, issuer the cluster's service-account issuer, subject
  `system:serviceaccount:<release namespace>:<release name>`; or a client secret.
- **The claims the API server uses** (`config.kubernetesUser`, `config.kubernetesGroups`): the
  username and groups claims and their prefixes, exactly as in its authentication configuration.
  A person in more groups than fit in the token is refused at sign-in, with the reason.
- **Storage** (`azure.storageClientId`, default the sign-in app): a managed identity or app with
  federated credentials for the server and for the Jobs in each namespace of `rbac.namespaces`
  (`system:serviceaccount:<namespace>:databases-ui-job`).

### 3. Kubernetes

The chart installs:

- a ClusterRole that lets the server create SubjectAccessReviews;
- in each namespace of `rbac.namespaces` (where the environment's CloudNativePG Cluster is): a Role
  for the server (Database resources: get, list, create and delete, for copies; Clusters: get;
  Jobs: get, list, create, patch, delete; pods: list; no Secrets), the Jobs' service account
  `databases-ui-job`, and with `networkPolicy.databases.enabled` a NetworkPolicy that lets the Jobs
  reach PostgreSQL on the CloudNativePG pods;
- in each namespace of `jobs.scaleNamespaces`: the right for the Jobs to get and patch Deployments
  and StatefulSets and list pods, for restores in place;
- a ValidatingAdmissionPolicy (`jobPolicy`, Kubernetes 1.30+) that holds the Jobs the server creates
  to what it builds: this image, run as `job`; one owner's password and never a `*-superuser`
  Secret; the CA certificate and no other Secret file; `verify-full` TLS to a `-rw` service in the
  Job's own namespace; and only the catalog's storage. Creating Jobs in a namespace would otherwise
  let a client mount any Secret there.

### 4. Install

```bash
helm install databases-ui oci://ghcr.io/magmamoose/charts/databases-ui \
  --namespace databases-ui --create-namespace \
  --set azure.tenantId=<tenant> --set azure.clientId=<the API server's app> \
  --set azure.storageClientId=<storage identity> \
  --set config.publicOrigin=https://databases.example.com \
  --set-file catalog.text=catalog.yaml \
  --set 'jobs.scaleNamespaces={dev-payments}' \
  --set ingress.enabled=true
```

Every value is described in [`charts/databases-ui/values.yaml`](charts/databases-ui/values.yaml).
The catalog can also be a map in `catalog.content` or your own ConfigMap
(`catalog.existingConfigMap`); the server reads it at startup, and the chart rolls the pods when
its own copy changes.

## Configuration

| Variable | |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID` | the sign-in app registration |
| `AZURE_FEDERATED_TOKEN_FILE` or `AZURE_CLIENT_SECRET[_FILE]` | its credential |
| `AZURE_AUTHORITY_HOST` | a sovereign cloud's login host; default `https://login.microsoftonline.com` |
| `DATABASES_UI_STORAGE_CLIENT_ID` | the storage identity; default the sign-in app |
| `DATABASES_UI_PUBLIC_ORIGIN` | where people open it, `https://host` |
| `DATABASES_UI_SESSION_KEY[_FILE]` | at least 32 characters, the same on every replica |
| `DATABASES_UI_SESSION_HOURS` | 1 to 24, default 8 |
| `DATABASES_UI_CATALOG` | the catalog file |
| `DATABASES_UI_NAMESPACE_LABEL` | the label that names a database's namespace; default `databases-ui/namespace` |
| `DATABASES_UI_USERNAME_CLAIM`, `DATABASES_UI_USERNAME_PREFIX` | as the API server builds the user; default `preferred_username`, `oidc:` |
| `DATABASES_UI_GROUPS_CLAIM`, `DATABASES_UI_GROUPS_PREFIX` | as it builds the groups; default `groups`, `oidc:` |
| `DATABASES_UI_JOB_IMAGE` | the image the Jobs run: this app's own |
| `DATABASES_UI_JOB_SERVICE_ACCOUNT` | the Jobs' service account; default `databases-ui-job` |
| `DATABASES_UI_STORAGE_SAS[_FILE]` and `DATABASES_UI_STORAGE_SAS_SECRET` | instead of Entra ID: a SAS for the server, and `<secret>/<key>` of the same SAS in each Job namespace (for Azurite and tests) |
| `DATABASES_UI_BRAND_ACCENT`, `_ACCENT_TEXT`, `_ACCENT_DARK`, `_ACCENT_TEXT_DARK` | your colours as `#rrggbb` |

`python -m databases_ui [serve | check <catalog> | job]`: the server (default), the catalog check,
and a Job's work.

## Security

- **Backend for frontend.** The browser holds no token it can read: the session is an AES-GCM
  sealed, `__Host-`, `HttpOnly`, `Secure`, `SameSite=Strict` cookie, and every write also needs a
  CSRF token and this origin. No inline script or style, under a strict Content Security Policy.
- **Kubernetes decides.** Every call asks the API server whether the person may do what it takes in
  the database's namespace. Outside the namespaces someone may see, a database does not exist to
  them: not in a list, not in an answer. If the claims or prefixes differ from the API server's,
  the reviews ask about nobody and refuse everything.
- **No passwords in the server.** It reads no Secret; a Job's pod gets the owner's password from
  the kubelet, and the admission policy keeps the server's Jobs to what it builds.
- **A production restore or script takes two people.** It is created suspended and starts only when
  someone else who may restore (or run scripts) there approves it; a Job's pod template can't
  change after creation, so what runs is what was asked for. Unapproved requests expire after a
  day.
- **A script runs as the database's owner, never as a superuser the app chose.** For an external
  database the catalog names the login, and the admission policy holds a script Job to that host,
  port, TLS mode and password Secret.
- **Data leaves an environment only when someone who may snapshot it says so**: a share names one
  namespace in one environment, and is in the audit log. Downloads, outside production only,
  stream through the server and are recorded too.

## Development

```bash
uv sync
uv run pytest
uv run python -m databases_ui check examples/catalog.yaml
cd frontend && npm install && npm run dev   # the UI on 5173, the API proxied to 8080
```

[`tests/e2e/run.py`](tests/e2e/run.py) runs every action against a real cluster with CloudNativePG, Azurite and
RBAC like an OIDC-backed API server's; CI runs it on kind:

```bash
k3d cluster create dbui
helm install cnpg cloudnative-pg --repo https://cloudnative-pg.github.io/charts --version 0.29.1 \
  --namespace cnpg-system --create-namespace --wait
docker buildx bake app-local && k3d image import databases-ui:dev -c dbui
uv run python tests/e2e/run.py
```

## Licence

Apache-2.0, see [LICENSE](LICENSE).
