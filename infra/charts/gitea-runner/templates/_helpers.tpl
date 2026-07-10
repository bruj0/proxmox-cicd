{{/*
Expand the name of the chart.
*/}}
{{- define "gitea-runner.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Create a fully qualified app name.
We truncate at 63 chars because some K8s name fields are limited to this (RFC 1123).
*/}}
{{- define "gitea-runner.fullname" -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels — used by all resources in this chart.
*/}}
{{- define "gitea-runner.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "gitea-runner.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gitea
{{- end -}}

{{/*
Selector labels — used by the Deployment's selector.
*/}}
{{- define "gitea-runner.selectorLabels" -}}
app.kubernetes.io/name: {{ include "gitea-runner.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}