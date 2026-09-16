{{/*
Refuse to render a production install that is secretly a development one.

`helm install` on a real cluster with the shipped placeholder passwords produces
a system that looks deployed and is not secured. Failing at render time is a bad
afternoon; failing silently is a bad year. `helm template` and `--dry-run` run
this too, so the refusal surfaces before anything reaches the cluster.
*/}}
{{- define "kube-wl-ai.validate" -}}
{{- if eq .Values.config.appEnv "production" -}}
{{- if not .Values.secrets.existingSecret -}}
{{- if not .Values.secrets.secretKey -}}
{{- fail "config.appEnv=production requires secrets.secretKey (or secrets.existingSecret)" -}}
{{- end -}}
{{- if not .Values.secrets.localAdminPassword -}}
{{- fail "config.appEnv=production requires secrets.localAdminPassword (the local provider is the break-glass account)" -}}
{{- end -}}
{{- end -}}
{{- if not .Values.postgresql.enabled -}}
{{- if not .Values.secrets.databaseUrl -}}
{{- fail "postgresql.enabled=false requires secrets.databaseUrl" -}}
{{- end -}}
{{- end -}}
{{- if and .Values.config.requireJwtKeys (not .Values.config.jwt.existingSecret) (not .Values.secrets.jwtPrivateKey) -}}
{{- fail "config.requireJwtKeys=true requires config.jwt.existingSecret or secrets.jwtPrivateKey; generate a pair with scripts/helm-gen-keys.sh" -}}
{{- end -}}
{{- if .Values.ingress.enabled -}}
{{- if not .Values.ingress.host -}}
{{- fail "ingress.enabled=true requires ingress.host" -}}
{{- end -}}
{{- end -}}
{{- if contains "ldap" .Values.config.authProviders -}}
{{- if not .Values.config.ldap.uri -}}
{{- fail "authProviders includes ldap but config.ldap.uri is empty" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
