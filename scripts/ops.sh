#!/usr/bin/env bash
# Operational runbook commands for the Docker Compose deployment.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

psql_cmd=(docker compose exec -T postgres psql -v ON_ERROR_STOP=1 -q -U swarmguard -d swarmguard)

case "${1:-help}" in
  up)
    docker compose up -d --build --wait ;;
  migrate)
    docker compose run --rm migrate ;;
  streams)
    docker compose run --rm stream-bootstrap ;;
  secrets)
    # Credential bootstrap: generate per-environment secrets once; compose reads .env.
    if [[ -e .env ]]; then echo ".env already exists; refusing to overwrite" >&2; exit 1; fi
    umask 077
    for key in SWARMGUARD_API_SIGNING_KEY SWARMGUARD_HARNESS_SIGNING_KEY SWARMGUARD_AUDIT_ENCRYPTION_KEY POSTGRES_PASSWORD; do
      printf '%s=%s\n' "$key" "$(openssl rand -hex 32)"
    done >.env
    echo "wrote .env (POSTGRES_PASSWORD only applies to a fresh pg-data volume)" ;;
  token)
    # Mint a dashboard/API bearer token: ops.sh token [subject] [viewer|operator|admin] [ttl-seconds]
    python -c "import sys; from swarmguard.security import mint_token; print(mint_token(sys.argv[1], sys.argv[2], ttl_seconds=int(sys.argv[3])))" \
      "${2:-operator}" "${3:-viewer}" "${4:-43200}" ;;
  backup)
    out="${2:-backups/swarmguard-$(date +%Y%m%d-%H%M%S).sql.gz}"
    mkdir -p "$(dirname "$out")"
    docker compose exec -T postgres pg_dump --clean --if-exists -U swarmguard swarmguard | gzip >"$out"
    echo "$out" ;;
  restore)
    gunzip -c "${2:?usage: ops.sh restore BACKUP.sql.gz}" | "${psql_cmd[@]}" ;;
  reload-policy)
    # The gateway re-reads POLICY_FILE on SIGHUP; a malformed file keeps the previous rules.
    pkill -HUP -f 'swarmguard[.-]gateway' && echo "policy reload signalled" ;;
  status)
    docker compose ps
    curl -fsS http://127.0.0.1:8000/api/health && echo ;;
  *)
    echo "usage: scripts/ops.sh {up|migrate|streams|secrets|token|backup|restore|reload-policy|status}" >&2
    exit 2 ;;
esac
