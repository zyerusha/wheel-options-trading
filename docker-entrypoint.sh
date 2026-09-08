#!/bin/sh
set -eu

# Cloud platforms (Cloud Run, Render, Fly) inject $PORT; fall back to 8765.
: "${PORT:=8765}"

# Containers must bind all interfaces to be reachable from the host / load
# balancer. Local non-container runs keep 127.0.0.1 via the argparse default.
: "${WHEEL_HOST:=0.0.0.0}"

exec python -m wheel.serve --no-browser --host "$WHEEL_HOST" --port "$PORT" "$@"
