#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
export FINDMY_DATA_DIR="${FINDMY_DATA_DIR:-$ROOT/data}"
export FINDMY_PORT="${FINDMY_PORT:-8765}"
exec "$ROOT/.venv/bin/python" -m uvicorn localfindmy.app:app \
  --host "${FINDMY_BIND:-127.0.0.1}" --port "$FINDMY_PORT" \
  --no-access-log --log-level critical
