{{/* ---------------------------------------------------------------- names + labels */}}

{{- define "aiops.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "aiops.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "aiops.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
app.kubernetes.io/name: {{ include "aiops.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: ai-incident-agents
{{- end -}}

{{/* Selector labels of one component: include "aiops.selectorLabels" (list $ "api") */}}
{{- define "aiops.selectorLabels" -}}
{{- $ := index . 0 -}}
app.kubernetes.io/name: {{ include "aiops.name" $ }}
app.kubernetes.io/instance: {{ $.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "aiops.componentLabels" -}}
{{ include "aiops.labels" (index . 0) }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "aiops.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "aiops.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* ---------------------------------------------------------------- images */}}

{{/* include "aiops.image" (list $ .Values.api.image) */}}
{{- define "aiops.image" -}}
{{- $ := index . 0 -}}
{{- $img := index . 1 -}}
{{- $tag := $img.tag | default $.Values.global.imageTag | default $.Chart.AppVersion -}}
{{- if $.Values.global.imageRegistry -}}
{{- printf "%s/%s:%s" (trimSuffix "/" $.Values.global.imageRegistry) $img.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" $img.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "aiops.imagePullSecrets" -}}
{{- with .Values.global.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* ---------------------------------------------------------------- security */}}

{{- define "aiops.podSecurityContext" -}}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
{{- end -}}

{{- define "aiops.containerSecurityContext" -}}
securityContext:
  {{- toYaml .Values.containerSecurityContext | nindent 2 }}
{{- end -}}

{{/* ---------------------------------------------------------------- database */}}

{{- define "aiops.postgresql.fullname" -}}
{{- printf "%s-postgresql" (include "aiops.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fails early, with a readable message, when no database is configured. */}}
{{- define "aiops.database.validate" -}}
{{- if .Values.postgresql.enabled -}}
{{- if not .Values.postgresql.auth.existingSecret -}}
{{- fail "postgresql.auth.existingSecret is required: create a Secret with the Postgres password (key `password`) and name it here. Secret values never go in values.yaml." -}}
{{- end -}}
{{- else if not .Values.externalDatabase.existingSecret -}}
{{- fail "postgresql.enabled=false needs externalDatabase.existingSecret: a Secret whose `url` key is postgresql://USER:PASSWORD@HOST:5432/DB" -}}
{{- end -}}
{{- end -}}

{{- define "aiops.postgresql.passwordEnv" -}}
valueFrom:
  secretKeyRef:
    name: {{ .Values.postgresql.auth.existingSecret }}
    key: {{ .Values.postgresql.auth.passwordKey }}
{{- end -}}

{{- define "aiops.externalDatabase.urlEnv" -}}
valueFrom:
  secretKeyRef:
    name: {{ .Values.externalDatabase.existingSecret }}
    key: {{ .Values.externalDatabase.urlKey }}
{{- end -}}

{{/* The evidence store connection of the API and the migration Job. */}}
{{- define "aiops.api.databaseEnv" -}}
{{- if .Values.postgresql.enabled }}
- name: POSTGRES_HOST
  value: {{ include "aiops.postgresql.fullname" . }}
- name: POSTGRES_PORT
  value: "5432"
- name: POSTGRES_USER
  value: {{ .Values.postgresql.auth.username | quote }}
- name: POSTGRES_DB
  value: {{ .Values.postgresql.auth.database | quote }}
- name: POSTGRES_PASSWORD
  {{- include "aiops.postgresql.passwordEnv" . | nindent 2 }}
{{- else }}
- name: AIOPS_DATABASE_URL
  {{- include "aiops.externalDatabase.urlEnv" . | nindent 2 }}
{{- end }}
{{- end -}}

{{/* include "aiops.mcp.databaseEnv" (list $ "tickets"|"libpq") */}}
{{- define "aiops.mcp.databaseEnv" -}}
{{- $ := index . 0 -}}
{{- $kind := index . 1 -}}
{{- if $.Values.postgresql.enabled }}
{{- $prefix := ternary "PG_" "PG" (eq $kind "tickets") }}
- name: {{ $prefix }}HOST
  value: {{ include "aiops.postgresql.fullname" $ }}
- name: {{ $prefix }}PORT
  value: "5432"
- name: {{ $prefix }}USER
  value: {{ $.Values.postgresql.auth.username | quote }}
- name: {{ ternary "PG_DATABASE" "PGDATABASE" (eq $kind "tickets") }}
  value: {{ $.Values.postgresql.auth.database | quote }}
- name: {{ $prefix }}PASSWORD
  {{- include "aiops.postgresql.passwordEnv" $ | nindent 2 }}
{{- else }}
- name: {{ ternary "TICKETS_DATABASE_URL" "KNOWLEDGE_DATABASE_URL" (eq $kind "tickets") }}
  {{- include "aiops.externalDatabase.urlEnv" $ | nindent 2 }}
{{- end }}
{{- end -}}

{{/* ---------------------------------------------------------------- MCP servers */}}

{{/* The DNS name of an MCP server: include "aiops.mcp.fullname" (list $ "mockTickets") */}}
{{- define "aiops.mcp.fullname" -}}
{{- $ := index . 0 -}}
{{- printf "%s-mcp-%s" (include "aiops.fullname" $) (kebabcase (index . 1)) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* ---------------------------------------------------------------- API pod (shared with the migration Job) */}}

{{- define "aiops.profile.configMapName" -}}
{{- if .Values.profile.existingConfigMap -}}
{{- .Values.profile.existingConfigMap -}}
{{- else -}}
{{- printf "%s-profile" (include "aiops.fullname" .) -}}
{{- end -}}
{{- end -}}

{{- define "aiops.profile.mounted" -}}
{{- if or .Values.profile.create .Values.profile.existingConfigMap -}}true{{- end -}}
{{- end -}}

{{- define "aiops.api.env" -}}
- name: AIOPS_PROFILE
  value: {{ .Values.profile.name | quote }}
{{- include "aiops.api.databaseEnv" . }}
{{- range $key, $server := .Values.mcpServers }}
{{- if and $server.enabled $server.urlEnv }}
- name: {{ $server.urlEnv }}
  value: {{ printf "http://%s:%v/mcp" (include "aiops.mcp.fullname" (list $ $key)) $server.port | quote }}
{{- end }}
{{- end }}
- name: AIOPS_REPLAY_TOOL_DELAY_S
  value: {{ .Values.api.replayToolDelaySeconds | quote }}
{{- range $name, $value := .Values.llm.env }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- range $name, $value := .Values.api.env }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{- define "aiops.api.envFrom" -}}
{{- $secrets := list -}}
{{- with .Values.llm.existingSecret }}{{ $secrets = append $secrets . }}{{ end -}}
{{- with .Values.api.auth.existingSecret }}{{ $secrets = append $secrets . }}{{ end -}}
{{- range .Values.api.envFromSecrets }}{{ $secrets = append $secrets . }}{{ end -}}
{{- with $secrets }}
envFrom:
  {{- range (uniq .) }}
  - secretRef:
      name: {{ . }}
  {{- end }}
{{- end }}
{{- end -}}

{{/* Writable paths of the read-only API image: /tmp and /app/.data (file audit fallback). */}}
{{- define "aiops.api.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: data
  mountPath: /app/.data
{{- if include "aiops.profile.mounted" . }}
- name: profile
  mountPath: /app/profiles/{{ .Values.profile.name }}
  readOnly: true
{{- end }}
{{- with .Values.api.extraVolumeMounts }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "aiops.api.volumes" -}}
- name: tmp
  emptyDir: {sizeLimit: 64Mi}
- name: data
  emptyDir: {sizeLimit: 256Mi}
{{- if include "aiops.profile.mounted" . }}
- name: profile
  configMap:
    name: {{ include "aiops.profile.configMapName" . }}
{{- end }}
{{- with .Values.api.extraVolumes }}
{{ toYaml . }}
{{- end }}
{{- end -}}
