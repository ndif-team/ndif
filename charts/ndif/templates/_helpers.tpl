{{- define "ndif.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ndif.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/* Labels; call with (dict "ctx" $ "component" "api"). */}}
{{- define "ndif.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ndif.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end }}

{{- define "ndif.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .ctx.Chart.Name .ctx.Chart.Version | replace "+" "_" }}
app.kubernetes.io/managed-by: {{ .ctx.Release.Service }}
app.kubernetes.io/version: {{ .ctx.Values.image.tag | default .ctx.Chart.AppVersion | quote }}
{{ include "ndif.selectorLabels" . }}
{{- end }}

{{- define "ndif.image" -}}
{{- $tag := .Values.image.tag | default (printf "%s-%s" .Chart.AppVersion .Values.image.cuda) }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}

{{- define "ndif.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "ndif.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Service hostnames. */}}
{{- define "ndif.rayHead" -}}{{ include "ndif.fullname" . }}-ray-head{{- end }}
{{- define "ndif.apiHost" -}}{{ include "ndif.fullname" . }}-api{{- end }}

{{- define "ndif.redisUrl" -}}
{{- .Values.redis.url | default (printf "redis://%s-redis:6379" (include "ndif.fullname" .)) }}
{{- end }}

{{- define "ndif.objectStoreUrl" -}}
{{- if .Values.objectStore.url }}
{{- .Values.objectStore.url }}
{{- else if .Values.rustfs.enabled }}
{{- printf "http://%s-rustfs:%v" (include "ndif.fullname" .) .Values.rustfs.service.port }}
{{- end }}
{{- end }}

{{- define "ndif.objectStorePublicUrl" -}}
{{- if .Values.objectStore.publicUrl }}
{{- .Values.objectStore.publicUrl }}
{{- else if and .Values.rustfs.enabled .Values.rustfs.ingress.enabled }}
{{- printf "%s://%s" (ternary "https" "http" (ne .Values.rustfs.ingress.tlsSecretName "")) .Values.rustfs.ingress.host }}
{{- end }}
{{- end }}

{{/* Secret holding the object-store keys, or "" for the pod's own identity. */}}
{{- define "ndif.objectStoreSecret" -}}
{{- if .Values.rustfs.enabled }}
{{- .Values.rustfs.existingSecret | default (include "ndif.fullname" .) }}
{{- else if .Values.objectStore.existingSecret }}
{{- .Values.objectStore.existingSecret }}
{{- else if .Values.objectStore.accessKey }}
{{- include "ndif.fullname" . }}
{{- end }}
{{- end }}

{{- define "ndif.secretRef" -}}
valueFrom:
  secretKeyRef:
    name: {{ .name }}
    key: {{ .key }}
{{- end }}

{{/* Env every ndif pod gets: service wiring and the telemetry label. */}}
{{- define "ndif.commonEnv" -}}
- name: NDIF_REDIS_URL
  value: {{ include "ndif.redisUrl" . | quote }}
- name: NDIF_RAY_ADDRESS
  value: {{ printf "ray://%s:10001" (include "ndif.rayHead" .) | quote }}
{{- with .Values.environment }}
- name: NDIF_ENVIRONMENT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{/* Object-store env, for the pods that upload and sign (api, ray). */}}
{{- define "ndif.objectStoreEnv" -}}
- name: NDIF_OBJECT_STORE_URL
  value: {{ include "ndif.objectStoreUrl" . | quote }}
- name: NDIF_OBJECT_STORE_PUBLIC_URL
  value: {{ include "ndif.objectStorePublicUrl" . | quote }}
- name: NDIF_OBJECT_STORE_BUCKET
  value: {{ .Values.objectStore.bucket | quote }}
- name: NDIF_OBJECT_STORE_REGION
  value: {{ .Values.objectStore.region | quote }}
- name: NDIF_OBJECT_STORE_VERIFY
  value: {{ .Values.objectStore.verify | quote }}
{{- $secret := include "ndif.objectStoreSecret" . }}
{{- range $key := list "NDIF_OBJECT_STORE_ACCESS_KEY" "NDIF_OBJECT_STORE_SECRET_KEY" }}
- name: {{ $key }}
{{- if $secret }}
  {{- include "ndif.secretRef" (dict "name" $secret "key" $key) | nindent 2 }}
{{- else }}
  value: ""
{{- end }}
{{- end }}
{{- end }}

{{- define "ndif.hfTokenEnv" -}}
{{- if .Values.hfToken.existingSecret }}
- name: HF_TOKEN
  {{- include "ndif.secretRef" (dict "name" .Values.hfToken.existingSecret "key" "HF_TOKEN") | nindent 2 }}
{{- else if .Values.hfToken.value }}
- name: HF_TOKEN
  {{- include "ndif.secretRef" (dict "name" (include "ndif.fullname" .) "key" "HF_TOKEN") | nindent 2 }}
{{- end }}
{{- end }}

{{/* Restart annotation over the secret values a component reads, so a change
to one (say the dashboard password) does not restart the others. Call with
(dict "ctx" $ "component" "ray"). */}}
{{- define "ndif.secretChecksum" -}}
{{- $v := .ctx.Values }}
{{- $store := list $v.objectStore.accessKey $v.objectStore.secretKey $v.rustfs.accessKey $v.rustfs.secretKey }}
{{- $parts := dict
  "api" (list $store $v.api.postgres)
  "ray" (list $store $v.hfToken)
  "dashboard" (list $v.dashboard.username $v.dashboard.passwordHash $v.dashboard.sessionSecret)
  "rustfs" $store -}}
checksum/secret: {{ get $parts .component | toJson | sha256sum }}
{{- end }}

{{/* Env, volumes and mounts shared by the ray head and workers. */}}
{{- define "ndif.rayEnv" -}}
- name: NDIF_SERVICE
  value: ray
{{- if .Values.hfCache.enabled }}
- name: HF_HOME
  value: {{ .Values.hfCache.mountPath | quote }}
{{- end }}
{{- with include "ndif.hfTokenEnv" . | trim }}
{{ . }}
{{- end }}
{{ include "ndif.commonEnv" . }}
{{ include "ndif.objectStoreEnv" . }}
{{- with .Values.ray.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "ndif.rayVolumeMounts" -}}
- name: dshm
  mountPath: /dev/shm
{{- if .Values.hfCache.enabled }}
- name: hf-cache
  mountPath: {{ .Values.hfCache.mountPath }}
{{- end }}
{{- end }}

{{- define "ndif.rayVolumes" -}}
- name: dshm
  emptyDir:
    medium: Memory
    sizeLimit: {{ .Values.ray.shmSize }}
{{- if .Values.hfCache.enabled }}
- name: hf-cache
  persistentVolumeClaim:
    claimName: {{ .Values.hfCache.existingClaim | default (printf "%s-hf-cache" (include "ndif.fullname" .)) }}
{{- end }}
{{- end }}

{{/* resources plus the GPU limit; call with (dict "resources" ... "gpus" N). */}}
{{- define "ndif.gpuResources" -}}
{{- $res := deepCopy (.resources | default dict) }}
{{- if gt (int .gpus) 0 }}
{{- $limits := $res.limits | default dict }}
{{- $_ := set $limits "nvidia.com/gpu" (int .gpus) }}
{{- $_ := set $res "limits" $limits }}
{{- end }}
{{- toYaml $res }}
{{- end }}
