{{/*
Helpers shared by every template.
*/}}

{{- define "kube-wl-ai.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "kube-wl-ai.fullname" -}}
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

{{- define "kube-wl-ai.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
The name of the Secret holding credentials.

Every workload reads the same Secret, so this is resolved in one place and an
`existingSecret` swap is a single value rather than a hunt through templates.
*/}}
{{- define "kube-wl-ai.secretName" -}}
{{- default (printf "%s-secrets" (include "kube-wl-ai.fullname" .)) .Values.secrets.existingSecret -}}
{{- end -}}

{{- define "kube-wl-ai.configMapName" -}}
{{- printf "%s-config" (include "kube-wl-ai.fullname" .) -}}
{{- end -}}

{{- define "kube-wl-ai.serviceAccountName" -}}
{{- if .Values.rbac.create -}}
{{- printf "%s-api" (include "kube-wl-ai.fullname" .) -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "kube-wl-ai.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{- define "kube-wl-ai.webImage" -}}
{{- printf "%s:%s" .Values.web.image.repository (default .Chart.AppVersion .Values.web.image.tag) -}}
{{- end -}}

{{- define "kube-wl-ai.labels" -}}
helm.sh/chart: {{ include "kube-wl-ai.chart" . }}
{{ include "kube-wl-ai.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: kube-wl-ai
{{- with .Values.commonLabels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "kube-wl-ai.selectorLabels" -}}
app.kubernetes.io/name: {{ include "kube-wl-ai.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
The database URL, preferring an explicit value over the bundled PostgreSQL.

Takes a dict: `root` is the chart context, `password` the already-resolved
bundled password. Passing the password in rather than generating it here is the
whole point — `randAlphaNum` re-evaluates on every call, so a helper that
generated its own would produce one password in the URL and a different one in
the Secret.
*/}}
{{- define "kube-wl-ai.databaseUrl" -}}
{{- $root := .root -}}
{{- if $root.Values.secrets.databaseUrl -}}
{{- $root.Values.secrets.databaseUrl -}}
{{- else if $root.Values.postgresql.enabled -}}
{{- printf "postgresql+asyncpg://%s:%s@%s-postgresql:5432/%s" $root.Values.postgresql.auth.username .password $root.Release.Name $root.Values.postgresql.auth.database -}}
{{- else -}}
{{- fail "set secrets.databaseUrl or enable postgresql" -}}
{{- end -}}
{{- end -}}

{{- define "kube-wl-ai.redisUrl" -}}
{{- if .Values.secrets.redisUrl -}}
{{- .Values.secrets.redisUrl -}}
{{- else if .Values.redis.enabled -}}
{{- printf "redis://%s-redis:6379/0" .Release.Name -}}
{{- else -}}
{{- fail "set secrets.redisUrl or enable redis" -}}
{{- end -}}
{{- end -}}

{{/*
Environment shared by the API, the worker and beat.

These three run the same code against the same database, so they must agree on
every one of these; splitting them per workload is how a worker ends up
reconciling against a different setting than the API that created the row.
*/}}
{{- define "kube-wl-ai.commonEnv" -}}
- name: APP_ENV
  value: {{ .Values.config.appEnv | quote }}
- name: LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: LOG_JSON
  value: {{ .Values.config.logJson | quote }}
- name: SECRET_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: secretKey
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: databaseUrl
- name: REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: redisUrl
- name: CELERY_BROKER_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: redisUrl
- name: CELERY_RESULT_BACKEND
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: redisUrl
- name: AUTH_PROVIDERS
  value: {{ .Values.config.authProviders | quote }}
- name: CORS_ORIGINS
  value: {{ .Values.config.corsOrigins | quote }}
- name: ACCESS_TOKEN_EXPIRE_MINUTES
  value: {{ .Values.config.accessTokenExpireMinutes | quote }}
- name: REFRESH_TOKEN_EXPIRE_DAYS
  value: {{ .Values.config.refreshTokenExpireDays | quote }}
- name: LOGIN_RATE_LIMIT_ENABLED
  value: {{ .Values.config.loginRateLimitEnabled | quote }}
- name: LOGIN_RATE_LIMIT_WINDOW_SECONDS
  value: {{ .Values.config.loginRateLimitWindowSeconds | quote }}
- name: LOGIN_RATE_LIMIT_ATTEMPTS_PER_IP
  value: {{ .Values.config.loginRateLimitAttemptsPerIp | quote }}
- name: LOGIN_RATE_LIMIT_ATTEMPTS_PER_USERNAME
  value: {{ .Values.config.loginRateLimitAttemptsPerUsername | quote }}
- name: JWT_ALGORITHM
  value: {{ .Values.config.jwt.algorithm | quote }}
- name: JWT_ISSUER
  value: {{ .Values.config.jwt.issuer | quote }}
- name: JWT_PRIVATE_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: jwtPrivateKey
      optional: true
- name: JWT_PUBLIC_KEY
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: jwtPublicKey
      optional: true
- name: LDAP_URI
  value: {{ .Values.config.ldap.uri | quote }}
- name: LDAP_BASE_DN
  value: {{ .Values.config.ldap.baseDn | quote }}
- name: LDAP_USER_DN_TEMPLATE
  value: {{ .Values.config.ldap.userDnTemplate | quote }}
- name: LDAP_GROUP_BASE_DN
  value: {{ .Values.config.ldap.groupBaseDn | quote }}
- name: LDAP_BIND_DN
  value: {{ .Values.config.ldap.bindDn | quote }}
- name: LDAP_OBSERVER_GROUP
  value: {{ .Values.config.ldap.observerGroup | quote }}
- name: LDAP_BIND_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: ldapBindPassword
      optional: true
- name: K8S_ENABLED
  value: {{ .Values.config.k8s.enabled | quote }}
- name: K8S_IN_CLUSTER
  value: {{ .Values.config.k8s.inCluster | quote }}
- name: HARBOR_URL
  value: {{ .Values.config.harbor.url | quote }}
- name: HARBOR_ROBOT_NAME
  value: {{ .Values.config.harbor.robotName | quote }}
- name: HARBOR_VERIFY_TLS
  value: {{ .Values.config.harbor.verifyTls | quote }}
- name: HARBOR_ROBOT_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "kube-wl-ai.secretName" . }}
      key: harborRobotSecret
      optional: true
- name: PROMETHEUS_URL
  value: {{ .Values.config.monitoring.prometheusUrl | quote }}
- name: GRAFANA_URL
  value: {{ .Values.config.monitoring.grafanaUrl | quote }}
- name: GRAFANA_PUBLIC_URL
  value: {{ .Values.config.monitoring.grafanaPublicUrl | quote }}
- name: GRAFANA_DATASOURCE_UID
  value: {{ .Values.config.monitoring.grafanaDatasourceUid | quote }}
- name: GRAFANA_ORG_ID
  value: {{ .Values.config.monitoring.grafanaOrgId | quote }}
- name: GRAFANA_JWT_AUDIENCE
  value: {{ .Values.config.monitoring.grafanaJwtAudience | quote }}
- name: GRAFANA_JWT_TTL_SECONDS
  value: {{ .Values.config.monitoring.grafanaJwtTtlSeconds | quote }}
- name: SSH_MODE
  value: {{ .Values.config.instance.sshMode | quote }}
- name: SSH_NODEPORT_RANGE
  value: {{ .Values.config.instance.sshNodeportRange | quote }}
- name: SSH_HOST
  value: {{ .Values.config.instance.sshHost | quote }}
- name: INSTANCE_INGRESS_HOST
  value: {{ .Values.config.instance.ingressHost | quote }}
- name: INSTANCE_DEFAULT_STORAGE_GB
  value: {{ .Values.config.instance.defaultStorageGb | quote }}
- name: IDE_TICKET_SECONDS
  value: {{ .Values.config.instance.ideTicketSeconds | quote }}
- name: STORAGE_CLASS_HOME
  value: {{ .Values.config.storageClasses.home | quote }}
- name: STORAGE_CLASS_DATA
  value: {{ .Values.config.storageClasses.data | quote }}
- name: STORAGE_CLASS_SHARE
  value: {{ .Values.config.storageClasses.share | quote }}
{{- with .Values.api.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}
