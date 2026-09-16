#!/usr/bin/env bash
#
# Generate the RS256 key pair the platform signs tokens with, and load it into a
# Secret the chart can consume via `config.jwt.existingSecret`.
#
# RS256 rather than the HS256 default, for one concrete reason: with RS256 the
# Grafana handoff verifies a token against the public JWKS endpoint
# (`/.well-known/jwks.json`) and never holds the signing key. With HS256 the
# shared secret has to be copied into Grafana, which turns a read-only dashboard
# into a token-forging capability.
#
# The private key never touches disk here — it goes from openssl straight into
# `kubectl create secret`. A key written to a file is a key that gets committed.
set -euo pipefail

NAMESPACE="${NAMESPACE:-kube-wl-ai}"
SECRET_NAME="${SECRET_NAME:-kube-wl-ai-jwt}"
DRY_RUN="${DRY_RUN:-false}"

for tool in openssl kubectl; do
  if ! command -v "$tool" >/dev/null; then
    echo "缺少 $tool" >&2
    exit 1
  fi
done

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

echo "生成 RSA-4096 密钥对…"
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:4096 -out "$workdir/private.pem" 2>/dev/null
openssl rsa -in "$workdir/private.pem" -pubout -out "$workdir/public.pem" 2>/dev/null
chmod 600 "$workdir/private.pem"

# A sanity check before the key reaches the cluster: a malformed key produces a
# 500 on every login, and the stack trace points at JWT decoding rather than at
# the Secret.
openssl rsa -in "$workdir/private.pem" -check -noout >/dev/null 2>&1 || {
  echo "私钥自检失败" >&2
  exit 1
}

args=(create secret generic "$SECRET_NAME" -n "$NAMESPACE"
      --from-file=jwtPrivateKey="$workdir/private.pem"
      --from-file=jwtPublicKey="$workdir/public.pem")

if [ "$DRY_RUN" = "true" ]; then
  kubectl "${args[@]}" --dry-run=client -o yaml
  exit 0
fi

if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" >/dev/null 2>&1; then
  cat >&2 <<EOF
Secret $NAMESPACE/$SECRET_NAME 已存在，未做改动。

轮换签名密钥会**立即使所有会话失效**：访问令牌、刷新令牌、Grafana 嵌入令牌
全部作废，每个人都必须重新登录。这是轮换的应有之义，但请挑一个合适的时机。

确认要轮换，先删除再重跑：

  kubectl -n $NAMESPACE delete secret $SECRET_NAME
EOF
  exit 1
fi

kubectl "${args[@]}"

cat <<EOF

已创建 Secret $NAMESPACE/$SECRET_NAME。

在 values 中引用：

  config:
    jwt:
      algorithm: RS256
      existingSecret: $SECRET_NAME

公钥指纹（用于比对 Grafana 侧或排障时确认密钥一致）：

$(openssl pkey -pubin -in "$workdir/public.pem" -outform DER | openssl dgst -sha256 -hex | awk '{print $2}')
EOF
