#!/bin/sh
set -eu

# Cloud platforms (Cloud Run, Render, Fly) inject $PORT; fall back to 8765.
: "${PORT:=8765}"

# Containers must bind all interfaces to be reachable from the host / load
# balancer. Local non-container runs keep 127.0.0.1 via the argparse default.
: "${WHEEL_HOST:=0.0.0.0}"

: "${WHEEL_DATA_DIR:=/app/data}"

# The image bakes /app as owned by app:app, but a bind-mounted host dir
# (docker-compose's ./data:/app/data) shadows that entirely with whatever
# ownership the mount shows up with -- on Docker Desktop for Windows/Mac that
# is often root:root regardless of what's on the host side, which then makes
# every write from the non-root `app` user (config.json autosave, uploads,
# caches) fail with a permission error. Fix it up here, as root, before
# dropping to `app` -- chown can fail on some mount types (e.g. NFS with root
# squash), so don't let that abort startup; su-exec below still degrades to
# "reads work, writes don't" instead of a crash-loop.
chown -R app:app "$WHEEL_DATA_DIR" 2>/dev/null || true

exec su-exec app python -m wheel.serve --no-browser --host "$WHEEL_HOST" --port "$PORT" "$@"
