#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUN_ID="${1:-demo-$(date +%s)}"
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set for the live demo}"

sudo mkdir -p /opt/swarmguard/demo-workspace
printf 'SwarmGuard demo brief: agents must follow least-privilege tool policy.\n' \
  | sudo tee /opt/swarmguard/demo-workspace/brief.txt >/dev/null

cleanup() {
  jobs -pr | xargs -r kill
}
trap cleanup EXIT INT TERM

NATS_USER=gateway NATS_PASSWORD=gateway-dev swarmguard-gateway &
NATS_USER=tools NATS_PASSWORD=tools-dev swarmguard-tool web_lookup &
NATS_USER=tools NATS_PASSWORD=tools-dev swarmguard-tool workspace_read &
NATS_USER=tools NATS_PASSWORD=tools-dev swarmguard-tool safe_shell &

sleep 2
ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \
  NATS_DEV_PASSWORDS=true \
  swarmguard-supervisor --run-id "$RUN_ID"

printf 'Run complete: %s\nOpen http://localhost:8000\n' "$RUN_ID"
