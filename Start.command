#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [[ ! -d .venv ]]; then
  echo "⚠️  .venv not found. Create it first with 'python3 -m venv .venv'." >&2
  exit 1
fi

source .venv/bin/activate
exec python -m uvicorn backend.app.main:app --reload --port 8000
