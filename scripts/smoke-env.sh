#!/usr/bin/env bash
# Prepare a throwaway SQLite database and print the environment needed to run
# the API against it. Source it, then start uvicorn:
#
#   source scripts/smoke-env.sh
#   uvicorn app.main:app --port 8000      # from backend/
#   python scripts/smoke_phase1.py        # from the repo root
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SMOKE_DIR="$REPO_ROOT/.smoke"
mkdir -p "$SMOKE_DIR"
rm -f "$SMOKE_DIR/smoke.db"

export APP_ENV=development
export DATABASE_URL="sqlite+aiosqlite:///$SMOKE_DIR/smoke.db"
export SECRET_KEY="smoke-test-secret-key-at-least-32-bytes-long"
export AUTH_PROVIDERS=local
export LOCAL_ADMIN_USERNAME=admin
export LOCAL_ADMIN_PASSWORD=smoke-test-password-123
export CORS_ORIGINS=http://localhost:5173

cd "$REPO_ROOT/backend"
export PATH="$PWD/.venv/bin:$PATH"
alembic upgrade head >/dev/null
python -m app.cli bootstrap-admin >/dev/null

echo "冒烟环境已就绪：$SMOKE_DIR/smoke.db"
