#!/usr/bin/env bash
#
# Thin wrapper around Alembic so migrations always run from backend/ with the
# project virtualenv, regardless of the caller's working directory.
#
#   ./scripts/migrate.sh upgrade head
#   ./scripts/migrate.sh revision --autogenerate -m "add workspaces"
#   ./scripts/migrate.sh downgrade -1
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"

ALEMBIC="${ALEMBIC:-.venv/bin/alembic}"

if [[ ! -x "$ALEMBIC" ]]; then
    echo "error: alembic not found at $ALEMBIC" >&2
    echo "hint: python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements-dev.txt" >&2
    exit 1
fi

# Put the virtualenv on PATH so alembic's post_write_hooks (ruff format) resolve.
export PATH="$ROOT/backend/.venv/bin:$PATH"

exec "$ALEMBIC" "$@"
