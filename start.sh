#!/bin/sh
# Load .env if it exists
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

HOST="${SERVER_HOST:-127.0.0.1}"
PORT="${SERVER_PORT:-8000}"

exec uv run uvicorn api.app:app --host "$HOST" --port "$PORT"
