#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_ID="${1:-live-$(date +%s)}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set}"
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"

cleanup() {
  jobs -pr | xargs -r kill
  docker stop swarmguard-tracee-live >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker run --rm \
  --name swarmguard-tracee-live \
  --pid=host \
  --privileged \
  -v /etc/os-release:/etc/os-release-host:ro \
  aquasec/tracee@sha256:cfbbfee972e64a644f6b1bac74ee26998e6e12442697be4c797ae563553a2a5b \
  --output json \
  --events sched_process_exec,sched_process_fork,sched_process_exit,security_file_open,security_socket_connect \
  | NATS_USER=telemetry NATS_PASSWORD=telemetry-dev \
    swarmguard-tracee --run-id "$RUN_ID" &

./scripts/demo.sh "$RUN_ID"
python -c "import time; time.sleep(2)"
