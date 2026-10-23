# databases-ui: the FastAPI backend serving the built React frontend, and the Jobs' runner with the
# PostgreSQL client, as one image.

# The frontend is static files, the same on every architecture, so it builds once on the
# build host rather than under emulation for each target.
FROM --platform=$BUILDPLATFORM node:25-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370 AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS backend
RUN pip install --no-cache-dir uv==0.11.19
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The locked dependencies and the package itself, no dev group, into /app/.venv.
RUN uv sync --locked --no-dev --no-editable

FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

# pg_dump, pg_restore and psql from the PostgreSQL project's repository: pg_dump has to be at
# least the server's version, and Debian's own lags behind. Its signing key is kept here
# (fingerprint B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8), so a rotation is a reviewed change.
COPY docker/apt.postgresql.org.asc /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
# hadolint ignore=DL3008
RUN set -eux; \
    codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends postgresql-client-18; \
    rm -rf /var/lib/apt/lists/*

ARG VERSION=dev
ENV PATH=/app/.venv/bin:/usr/lib/postgresql/18/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_VERSION=${VERSION} \
    PORT=8080

COPY --from=backend /app/.venv /app/.venv
COPY --from=frontend /frontend/dist /app/static

# Numeric and above 10000, so a pod's runAsUser can match the image exactly.
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"]
ENTRYPOINT ["python", "-m", "databases_ui"]
