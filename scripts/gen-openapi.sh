#!/usr/bin/env bash
#
# Dump the FastAPI OpenAPI schema to frontend/openapi/schema.json.
# The frontend generates its TypeScript types from this file, so the API
# contract is checked into the repository and diffed in CI.
#
#   ./scripts/gen-openapi.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
OUT="$ROOT/frontend/openapi/schema.json"

PYTHON="${PYTHON:-$BACKEND/.venv/bin/python}"

if [[ ! -x "$PYTHON" ]]; then
    echo "error: python interpreter not found at $PYTHON" >&2
    echo "hint: python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements-dev.txt" >&2
    exit 1
fi

mkdir -p "$(dirname "$OUT")"

(
    cd "$BACKEND"
    "$PYTHON" -c '
import json
from app.main import app
print(json.dumps(app.openapi(), indent=2, ensure_ascii=False))
'
) > "$OUT"

echo "wrote $OUT"
