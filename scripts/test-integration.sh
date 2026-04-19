#!/usr/bin/env bash
# Run the integration test suite against a throwaway mosquitto broker.
#
# Usage: ./scripts/test-integration.sh [<pytest args>]
#
# Env vars:
#   KEEP_BROKER=1  Leave the broker running after tests finish (useful when
#                  iterating — subsequent invocations will reuse it).
#   BROKER_PORT    Host port to bind the broker to. Default: 11883
#                  (non-standard to avoid colliding with a production
#                  mosquitto on the same machine).

set -euo pipefail

BROKER_NAME="ha-skylink-test-mosquitto"
BROKER_IMAGE="eclipse-mosquitto:2"
BROKER_PORT="${BROKER_PORT:-11883}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$SCRIPT_DIR/mosquitto.conf"
VENV_PYTEST="$REPO_ROOT/.venv/bin/pytest"

die() { echo "error: $*" >&2; exit 1; }

[ -x "$VENV_PYTEST" ] || die "venv pytest not found — run 'uv venv && uv pip install -e \".[dev]\"' first"
command -v docker >/dev/null || die "docker not installed"
docker info >/dev/null 2>&1 || die "docker daemon not reachable"

cleanup() {
    if [ "${KEEP_BROKER:-0}" != "1" ]; then
        echo "--- stopping broker"
        docker rm -f "$BROKER_NAME" >/dev/null 2>&1 || true
    else
        echo "--- KEEP_BROKER=1 — broker left running as '$BROKER_NAME' on port $BROKER_PORT"
    fi
}
trap cleanup EXIT

running="$(docker ps --filter "name=^${BROKER_NAME}$" --format '{{.Names}}')"
if [ "$running" = "$BROKER_NAME" ]; then
    echo "--- reusing running broker '$BROKER_NAME'"
else
    # Remove any stale stopped container by the same name
    docker rm -f "$BROKER_NAME" >/dev/null 2>&1 || true
    echo "--- starting broker '$BROKER_NAME' on port $BROKER_PORT"
    docker run -d \
        --name "$BROKER_NAME" \
        -p "${BROKER_PORT}:1883" \
        -v "${CONFIG_FILE}:/mosquitto/config/mosquitto.conf:ro" \
        "$BROKER_IMAGE" >/dev/null

    # Wait for mosquitto to be accepting connections (up to ~10s)
    for _ in $(seq 1 40); do
        if docker logs "$BROKER_NAME" 2>&1 | grep -q "Opening ipv4 listen socket on port 1883"; then
            break
        fi
        sleep 0.25
    done
    docker logs "$BROKER_NAME" 2>&1 | grep -q "Opening ipv4 listen socket on port 1883" \
        || die "broker did not come up within 10s — check 'docker logs $BROKER_NAME'"
fi

export MOSQUITTO_HOST=127.0.0.1
export MOSQUITTO_PORT="$BROKER_PORT"

echo "--- running integration tests"
"$VENV_PYTEST" "$REPO_ROOT/tests/integration" "$@"
