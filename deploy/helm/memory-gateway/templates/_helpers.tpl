{{/*
Names, labels, and the two invariants this chart refuses to render without.
*/}}

{{- define "memory-gateway.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "memory-gateway.fullname" -}}
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

{{- define "memory-gateway.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "memory-gateway.labels" -}}
helm.sh/chart: {{ include "memory-gateway.chart" . }}
{{ include "memory-gateway.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "memory-gateway.selectorLabels" -}}
app.kubernetes.io/name: {{ include "memory-gateway.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "memory-gateway.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "memory-gateway.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
The image reference. A digest wins over a tag: pinning by content is what makes "the
same version everywhere" a fact rather than a claim about a mutable tag.
*/}}
{{- define "memory-gateway.image" -}}
{{- if .Values.image.digest -}}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest -}}
{{- else -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}
{{- end -}}

{{/*
Where every pod's environment comes from: the rendered ConfigMap, the operator's Secret,
and whatever else the deployment adds. `envFrom` rather than a list of `valueFrom`
references so that adding a variable to the Secret needs no chart change — and so that a
credential never appears in a rendered manifest, only the name of the Secret holding it.
*/}}
{{- define "memory-gateway.envFrom" -}}
- configMapRef:
    name: {{ include "memory-gateway.fullname" . }}
{{- if .Values.secrets.existingSecret }}
- secretRef:
    name: {{ .Values.secrets.existingSecret }}
{{- end }}
{{- with .Values.extraEnvFrom }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end -}}

{{/*
The writable mount every pod needs, and the only one. `readOnlyRootFilesystem: true` in
the security context is what makes this necessary: tiktoken caches its vocabulary under
TIKTOKEN_CACHE_DIR, which the image points at /tmp.
*/}}
{{- define "memory-gateway.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: 256Mi
{{- end -}}

{{- define "memory-gateway.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
{{- end -}}

{{/*
Spread pods across nodes. Soft by default: on a small cluster a required rule leaves
replicas Pending, which is a worse availability outcome than two pods on one node.
*/}}
{{- define "memory-gateway.affinity" -}}
{{- $component := .component -}}
{{- $root := .root -}}
{{- $values := .values -}}
{{- if $values.affinity -}}
{{- toYaml $values.affinity -}}
{{- else if eq ($values.antiAffinity | default "soft") "hard" -}}
podAntiAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    - topologyKey: kubernetes.io/hostname
      labelSelector:
        matchLabels:
          {{- include "memory-gateway.selectorLabels" $root | nindent 10 }}
          app.kubernetes.io/component: {{ $component }}
{{- else if eq ($values.antiAffinity | default "soft") "soft" -}}
podAntiAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        topologyKey: kubernetes.io/hostname
        labelSelector:
          matchLabels:
            {{- include "memory-gateway.selectorLabels" $root | nindent 12 }}
            app.kubernetes.io/component: {{ $component }}
{{- end -}}
{{- end -}}

{{/*
The two things this chart will not render without.

The grace period one is the point of the whole exercise. A 60-second completion killed by
a 30-second grace period is a truncated customer response, it happens on every deploy
until the numbers are right, and it is invisible in any test that does not deploy under
load. So it is checked here, where getting it wrong is a failed `helm template` rather
than a support ticket: the pod has to be allowed to finish draining (drainSeconds) and
then to finish the longest request it is still permitted to be making
(routingDeadlineSeconds), with room to spare.
*/}}
{{- define "memory-gateway.validate" -}}
{{- $needed := add (int .Values.api.drainSeconds) (int .Values.config.upstream.routingDeadlineSeconds) -}}
{{- if lt (int .Values.api.terminationGracePeriodSeconds) $needed -}}
{{- fail (printf "api.terminationGracePeriodSeconds (%d) must be at least api.drainSeconds + config.upstream.routingDeadlineSeconds (%d + %d = %d), or a rolling deploy will kill in-flight completions." (int .Values.api.terminationGracePeriodSeconds) (int .Values.api.drainSeconds) (int .Values.config.upstream.routingDeadlineSeconds) $needed) -}}
{{- end -}}
{{- if and (eq .Values.config.environment "prod") (eq .Values.config.embedding.provider "hash") -}}
{{- fail "config.embedding.provider=hash is the local development embedder; the application refuses to start with it in production. Configure a real embedding model." -}}
{{- end -}}
{{- if and (eq .Values.config.environment "prod") (eq .Values.config.upstream.privateAddresses "allow") -}}
{{- fail "config.upstream.privateAddresses=allow turns off the SSRF guard, which makes a tenant-supplied base_url able to reach anything on the cluster network. Use upstream.allowedHosts or allowedCidrs for a legitimate internal endpoint." -}}
{{- end -}}
{{- end -}}
