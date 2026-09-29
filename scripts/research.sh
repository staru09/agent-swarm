#!/usr/bin/env bash
# Run the four-agent deep-research experiment against the compose infrastructure:
#   docker compose up -d --build --wait && scripts/research.sh "mechanistic interpretability"
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TOPIC="${1:?usage: scripts/research.sh \"research topic\" [run-id]}"
RUN_ID="${2:-research-$(date +%s)}"
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY must be set}"
: "${EXA_API_KEY:?EXA_API_KEY must be set}"
export NATS_URL="${NATS_URL:-nats://127.0.0.1:4222}"
export OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://127.0.0.1:4318}"
LOGS="runtime/$RUN_ID"
mkdir -p "$LOGS"

cleanup() {
  jobs -pr | xargs -r kill
}
trap cleanup EXIT INT TERM

# Secrets are scoped per process: only the Exa worker holds EXA_API_KEY and only
# the agents (launched by the orchestrator) hold ANTHROPIC_API_KEY.
no_secrets=(env -u EXA_API_KEY -u ANTHROPIC_API_KEY)
"${no_secrets[@]}" NATS_USER=gateway NATS_PASSWORD=gateway-dev OTEL_SERVICE_NAME=swarmguard-gateway \
  swarmguard-gateway >"$LOGS/gateway.log" 2>&1 &
env -u ANTHROPIC_API_KEY NATS_USER=tools NATS_PASSWORD=tools-dev OTEL_SERVICE_NAME=tool-exa_paper_search \
  swarmguard-tool exa_paper_search >"$LOGS/tool-exa_paper_search.log" 2>&1 &
for tool in candidate_read reviewed_papers_write reviewed_papers_read literature_summary_write \
  literature_summary_read future_directions_write; do
  "${no_secrets[@]}" NATS_USER=tools NATS_PASSWORD=tools-dev OTEL_SERVICE_NAME="tool-$tool" \
    swarmguard-tool "$tool" >"$LOGS/tool-$tool.log" 2>&1 &
done
sleep 2

env -u EXA_API_KEY NATS_USER=research-orchestrator NATS_PASSWORD=research-orchestrator-dev \
  NATS_DEV_PASSWORDS=true OTEL_SERVICE_NAME=swarmguard-research \
  swarmguard-research --topic "$TOPIC" --run-id "$RUN_ID"

printf 'Artifacts: artifacts/%s\nLogs: %s\nDashboard: http://127.0.0.1:8000\n' "$RUN_ID" "$LOGS"
