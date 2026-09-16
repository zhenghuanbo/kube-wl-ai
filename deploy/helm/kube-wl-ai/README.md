# kube-wl-ai Helm chart

Deploys the platform: API, Celery worker and beat, the nginx web tier, PostgreSQL
and Redis, plus the RBAC the platform needs to manage workspace namespaces.

**The monitoring stack is not part of this chart, on purpose.** This cluster
already runs Prometheus and Grafana; a chart that shipped a second copy would
fight the first one for scrape targets and dashboards. The platform *talks* to
them — `config.monitoring.prometheusUrl` and `config.monitoring.grafanaUrl` —
and ships the dashboards separately under `deploy/grafana/`.

## Quick start

```bash
# 1. Signing keys. RS256 so Grafana can verify without holding the key.
./scripts/helm-gen-keys.sh

# 2. Render and read it before applying. Every validation runs here, so a
#    missing secret is caught now rather than as a CrashLoopBackOff.
helm template wl deploy/helm/kube-wl-ai \
  -f deploy/helm/kube-wl-ai/values-prod.example.yaml \
  --kube-version "$(kubectl version -o json | jq -r .serverVersion.gitVersion)" \
  > /tmp/wl.yaml

# 3. Apply.
helm install wl deploy/helm/kube-wl-ai -n kube-wl-ai --create-namespace \
  -f my-values.yaml
```

The initial password is in the release Secret:

```bash
kubectl -n kube-wl-ai get secret wl-kube-wl-ai-secrets \
  -o jsonpath='{.data.localAdminPassword}' | base64 -d
```

## What the chart refuses to do

`templates/_validate.tpl` fails the render — not the install — when
`config.appEnv=production` and any of these is missing: `secretKey`,
`localAdminPassword`, a database URL, JWT keys, an ingress host, or an LDAP URI
while `authProviders` names `ldap`. `helm template` runs the same checks, so the
refusal surfaces before anything reaches the cluster.

This is deliberate. A production install with the shipped placeholder passwords
looks finished and is not, and the failure does not announce itself.

`values-dev.yaml` is the escape hatch for a local install.

## The one trade-off worth reading about

`templates/rbac.yaml` has a long comment and it is the most important thing in
this chart.

The platform creates a Kubernetes namespace per workspace and then acts inside
it. The goal is a ServiceAccount that can do that *and nothing else*. Kubernetes
offers two ways to express it:

- **Subject `namespaceSelector`** (≥ 1.31) — one binding, matched against the
  `wl-managed=true` label. Exactly right.
- **Per-namespace RoleBinding** — created when the platform creates the
  namespace. Works on every version, but means the *application* writes RBAC
  objects, so this chart does not ship it.

On a cluster older than 1.31 the field does not exist and the API server rejects
the binding outright, so the chart requires an explicit
`rbac.allowClusterWideWorkspaceAccess=true` and then grants the workspace role
cluster-wide. The consequence is stated in `NOTES.txt` on every install: the
platform's ServiceAccount can read pods, logs and `exec` in **every** namespace.

Two rules in this chart cannot be scoped by RBAC at all, and both are isolated in
their own ClusterRole so `kubectl get clusterrole` shows them:

| ClusterRole | Why it cannot be scoped | Mitigation |
| --- | --- | --- |
| `-namespace-lifecycle` | `namespaces` is cluster-scoped; a subject selector has nothing to match | ValidatingAdmissionPolicy in `templates/admission-note.yaml` |
| `-workspace-namespaced` (< 1.31) | no selector support in RBAC | upgrade to 1.31, or the per-namespace binding |

`admission-note.yaml` emits the policy as **commented YAML inside a ConfigMap**
rather than as a live object. A ValidatingAdmissionPolicy is cluster-wide in
effect, and a chart install should not silently start rejecting namespace
deletions for every other tenant on the cluster.

## Upgrades and uninstall

Migrations run as a `pre-install,pre-upgrade` Job, not an init container: one
replica applying a migration, and a non-zero exit blocks the release. The Job
also runs `python -m app.cli sync-rbac`, which is not optional — a code change
that adds a permission is a *data* change in the database, and without the sync
the new code checks a permission no role holds.

`helm uninstall` deliberately does not delete data. The Secret (database
password, JWT key) and the PostgreSQL/Redis PVCs carry
`helm.sh/resource-policy: keep`. A mistaken uninstall should not take a term of
usage records with it. Cleanup is explicit:

```bash
kubectl -n kube-wl-ai delete pvc -l app.kubernetes.io/instance=wl
kubectl -n kube-wl-ai delete secret wl-kube-wl-ai-secrets
```

Workspace namespaces (`wl-managed=true`) are never garbage-collected by Helm.

## Values worth knowing

| Value | Default | Why |
| --- | --- | --- |
| `api.replicaCount` | 2 | One uvicorn process per pod; an async app gets concurrency from the loop, and more processes only multiply the connection pool. |
| `beat.replicaCount` | 1 | Two beat schedulers both see the schedule and both fire it. `Recreate` strategy, so a rollout briefly has none rather than two. |
| `postgresql.enabled` | `true` | A convenience, not a production database: single replica, no backups, no failover. Prefer `secrets.databaseUrl`. |
| `networkPolicy.ingressNamespaceSelector` | `{}` | **Empty means the API is unreachable from the ingress**, and the symptom is a 502 with no log line. Set it before enabling `ingress`. |
| `podDisruptionBudget.minAvailable` | 1 | Only on api/web. A PDB on a one-replica worker blocks every node drain forever. |
| `serviceMonitor.enabled` | `false` | Enabling it without knowing the Prometheus label selector produces no scrape target, silently. |
| `rbac.allowClusterWideWorkspaceAccess` | `false` | Required on < 1.31. Read the section above. |

## Rendering checks

```bash
# Lint.
helm lint deploy/helm/kube-wl-ai -f deploy/helm/kube-wl-ai/values-dev.yaml

# Both RBAC paths.
helm template wl deploy/helm/kube-wl-ai -f values-dev.yaml --kube-version 1.28.15 \
  --set rbac.allowClusterWideWorkspaceAccess=true > /tmp/r28.yaml
helm template wl deploy/helm/kube-wl-ai -f values-dev.yaml --kube-version 1.31.0 > /tmp/r31.yaml

# The real API server decides whether the objects are valid. `--dry-run=server`
# catches what client-side rendering cannot: unknown fields, bad enum values,
# immutable-field changes.
kubectl apply --dry-run=server -f /tmp/r28.yaml
```

`--kube-version` matters. Without it Helm uses the version baked into the binary,
not the cluster's, so the version-gated RBAC branch is chosen by accident.
