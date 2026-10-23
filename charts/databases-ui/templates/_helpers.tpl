{{- define "databases-ui.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "databases-ui.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (include "databases-ui.name" .) .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "databases-ui.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- /* The web pods only: the Jobs' pods carry app.kubernetes.io/component: job. */}}
{{- define "databases-ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "databases-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: web
{{- end }}

{{- define "databases-ui.labels" -}}
app.kubernetes.io/name: {{ include "databases-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "databases-ui.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "databases-ui.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- required "serviceAccount.name is required when serviceAccount.create is false" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "databases-ui.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- if .Values.image.digest }}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}
{{- end }}

{{- define "databases-ui.sessionSecret" -}}
{{- default (printf "%s-session" (include "databases-ui.fullname" .)) .Values.session.existingSecret }}
{{- end }}

{{- define "databases-ui.catalogConfigMap" -}}
{{- default (printf "%s-catalog" (include "databases-ui.fullname" .)) .Values.catalog.existingConfigMap }}
{{- end }}

{{- /* The catalog as YAML text: catalog.content is a map in values, or a string from --set-file. */}}
{{- define "databases-ui.catalogContent" -}}
{{- if kindIs "string" .Values.catalog.content }}
{{- .Values.catalog.content }}
{{- else }}
{{- toYaml .Values.catalog.content }}
{{- end }}
{{- end }}

{{- define "databases-ui.host" -}}
{{- default (regexReplaceAll "^https?://([^/:]+).*$" .Values.config.publicOrigin "${1}") .Values.ingress.host }}
{{- end }}

{{- /* Fails the render on a missing required value, instead of installing something half-configured. */}}
{{- define "databases-ui.validate" -}}
{{- $_ := required "config.publicOrigin is required, such as https://databases.example.com" .Values.config.publicOrigin }}
{{- $_ := required "azure.tenantId is required" .Values.azure.tenantId }}
{{- $_ := required "azure.clientId is required" .Values.azure.clientId }}
{{- if and (not .Values.catalog.existingConfigMap) (not .Values.catalog.content) }}
{{- fail "set catalog.content (environments and teams) or catalog.existingConfigMap" }}
{{- end }}
{{- if and (not .Values.azure.workloadIdentity.enabled) (not .Values.azure.clientSecret.existingSecret) }}
{{- fail "set azure.workloadIdentity.enabled, or azure.clientSecret.existingSecret" }}
{{- end }}
{{- if and (not .Values.azure.workloadIdentity.enabled) (not .Values.storage.sas.existingSecret) }}
{{- fail "without workload identity the Jobs reach storage through storage.sas.existingSecret" }}
{{- end }}
{{- end }}

{{- define "databases-ui.env" -}}
- name: AZURE_TENANT_ID
  value: {{ .Values.azure.tenantId | quote }}
- name: AZURE_CLIENT_ID
  value: {{ .Values.azure.clientId | quote }}
{{- with .Values.azure.authorityHost }}
- name: AZURE_AUTHORITY_HOST
  value: {{ . | quote }}
{{- end }}
{{- if .Values.azure.workloadIdentity.enabled }}
- name: AZURE_FEDERATED_TOKEN_FILE
  value: /var/run/secrets/azure/tokens/azure-identity-token
{{- else }}
# Mounted files rather than env vars: an environment variable is readable in /proc/<pid>/environ
# and inherited by child processes.
- name: AZURE_CLIENT_SECRET_FILE
  value: /etc/databases-ui/client/{{ .Values.azure.clientSecret.key }}
{{- end }}
# The tag deployed, not the one baked in: a release promotes its pull request's image as it is,
# so the image itself still says pr-<N>.
- name: APP_VERSION
  value: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
- name: DATABASES_UI_PUBLIC_ORIGIN
  value: {{ .Values.config.publicOrigin | quote }}
- name: DATABASES_UI_SESSION_HOURS
  value: {{ .Values.config.sessionHours | quote }}
- name: DATABASES_UI_SESSION_KEY_FILE
  value: /etc/databases-ui/session/{{ .Values.session.key }}
- name: DATABASES_UI_CATALOG
  value: /etc/databases-ui/catalog/{{ .Values.catalog.key }}
- name: DATABASES_UI_JOB_IMAGE
  value: {{ include "databases-ui.image" . | quote }}
- name: DATABASES_UI_JOB_SERVICE_ACCOUNT
  value: {{ .Values.jobs.serviceAccountName | quote }}
{{- if .Values.storage.sas.existingSecret }}
- name: DATABASES_UI_STORAGE_SAS_FILE
  value: /etc/databases-ui/storage/{{ .Values.storage.sas.key }}
- name: DATABASES_UI_STORAGE_SAS_SECRET
  value: {{ printf "%s/%s" .Values.storage.sas.existingSecret .Values.storage.sas.key | quote }}
{{- end }}
- name: DATABASES_UI_ADMIN_ROLE
  value: {{ .Values.config.adminRole | quote }}
- name: DATABASES_UI_AUDITOR_ROLE
  value: {{ .Values.config.auditorRole | quote }}
{{- range $key, $env := dict "accent" "ACCENT" "accentText" "ACCENT_TEXT" "accentDark" "ACCENT_DARK" "accentTextDark" "ACCENT_TEXT_DARK" }}
{{- with index $.Values.branding $key }}
- name: DATABASES_UI_BRAND_{{ $env }}
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "databases-ui.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "databases-ui.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
