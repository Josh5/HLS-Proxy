#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROXY_URL="${HLS_PROXY_BASE_URL:-http://localhost:9987}"

cd "${ROOT_DIR}"

echo "[INFO] Starting proxy with docker compose..."
docker compose up -d

echo "[INFO] Waiting for ${PROXY_URL} to be ready..."
for _ in $(seq 1 30); do
  if curl -fsS "${PROXY_URL}" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "${PROXY_URL}" >/dev/null 2>&1; then
  echo "[ERROR] Proxy did not become ready at ${PROXY_URL}"
  exit 1
fi

echo "[INFO] Running integration tests..."
python tests/integration_proxy_tests.py

echo "[INFO] Integration tests completed."
