# SwarmGuard

SwarmGuard is a least-privilege control plane and kernel-level flight recorder for AI agent swarms. Every tool request and agent-to-agent message crosses NATS. A policy gateway evaluates inspectable YAML rules, while Tracee/eBPF independently records process, file, and network activity.

## Demo architecture

- Anthropic-powered agent processes defined in `config/agents.yaml`: the three policy-boundary demo agents (researcher, analyst, operator) and the four-agent [deep-research workflow](#deep-research-experiment).
- NATS subject permissions prevent sender spoofing and direct access to private tool workers.
- The gateway applies tool manifests from `config/tools.yaml` and tool and argument rules from `config/policies.yaml`, and denies decoy tools.
- JetStream retains requests, decisions, results, and A2A traffic. A durable projector writes them to PostgreSQL, and the gateway, workers, and agents export OpenTelemetry spans.
- Tracee captures focused host events; the collector joins process ancestry to the supervisor PID registry.
- FastAPI and React render a live per-agent timeline and a run summary. See [Dashboard](#dashboard).

eBPF does not infer semantic intent. It provides independent evidence of OS effects. In the MVP, direct bypass activity creates an alert but does not kill the agent.

## Linux setup

The supported demo target is native Ubuntu with BTF, Docker, passwordless sudo, Python 3.11+, and Node 22+. Do not rely on WSL2 for the final presentation.

```bash
./scripts/preflight.sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cd frontend && npm install && npm run build && cd ..
docker compose up -d --build --wait
pytest
cd frontend && npm test && cd ..
```

Set `ANTHROPIC_API_KEY`, then run:

```bash
. .venv/bin/activate
./scripts/demo.sh
```

Then open the [dashboard](#dashboard).

## Dashboard

The compose `timeline` service serves the dashboard and API on `127.0.0.1:8000` of the host. From your own machine, forward the port over SSH (or use your editor's port forwarding):

```bash
ssh -L 8000:127.0.0.1:8000 -i YOUR_KEY.pem ubuntu@YOUR_HOST
```

Then open `http://127.0.0.1:8000`. The local compose stack sets `SWARMGUARD_AUTH_DISABLED=true`, so no token is needed. See [API authentication](#api-authentication-rbac-and-access-audit) to enforce tokens.

- **Run picker:** runs are listed newest activity first with their state, and the newest run is selected by default. Runs named `probe-*` come from `tests/integration_research.py` and contain only decoy denials.
- **Timeline:** one lane per agent that acted in the selected run (the research run shows `research-orchestrator`, `web-researcher`, `paper-reviewer`, `summary-writer`, and `hypothesis-generator`), and unattributed events go to `system`. The run's full history loads through the paginated `/api/events?run_id=` endpoint in microsecond order, and new events stream in over `/api/live`. Denials and `security.decoy_triggered` events are highlighted in red. Click an event to see its payload.
- **Run summary:** agent sessions, model steps, tool calls with attempts and policy decisions, and the run's artifacts.

## JetStream setup

Create or update the declared streams and durable projector consumer with the dedicated bootstrap user:

```bash
NATS_USER=stream-bootstrap NATS_PASSWORD=stream-bootstrap-dev swarmguard-stream-bootstrap
```

Run the durable projector with its separate least-privilege user. The projector binds to the bootstrapped pull consumer and publishes malformed audit events only to its dead-letter subject:

```bash
NATS_USER=projector NATS_PASSWORD=projector-dev swarmguard-projector
```

Lifecycle records sent from agents to the gateway are HMAC signed with `SWARMGUARD_HARNESS_SIGNING_KEY`.
Local development uses a shared development HMAC default so the demo works without secret provisioning; set a real per-environment key outside local demos.
That agent-to-gateway lifecycle subject is an at-most-once lifecycle hop before the gateway republishes to JetStream audit, so a transient NATS publish failure can drop a lifecycle record.

## Small deployment (Docker Compose)

`docker compose up -d --build --wait` starts NATS, PostgreSQL, a one-shot idempotent `migrate` job, a one-shot `stream-bootstrap` job, the durable `projector`, an OpenTelemetry collector (OTLP/HTTP on `127.0.0.1:4318`, spans appended to `/data/traces.jsonl` in the `otel-data` volume), and the timeline API/dashboard on port 8000. Every port binds to loopback, including the host-networked dashboard (`API_HOST=127.0.0.1`). The gateway, tool workers, and agents run on the host so each process gets only its own credentials and secrets.

Runbook (`scripts/ops.sh`):

| Command | Purpose |
| --- | --- |
| `scripts/ops.sh up` | Build and start the stack, waiting for health checks |
| `scripts/ops.sh migrate` | Re-apply all migrations (idempotent) |
| `scripts/ops.sh streams` | Re-apply the JetStream topology |
| `scripts/ops.sh secrets` | Credential bootstrap: generate signing, encryption, and database secrets into `.env` |
| `scripts/ops.sh token [subject] [role] [ttl]` | Mint an API bearer token (only needed when `SWARMGUARD_AUTH_DISABLED` is unset) |
| `scripts/ops.sh backup [file]` / `restore FILE` | `pg_dump` the projections to a gzip file and restore it |
| `scripts/ops.sh reload-policy` | Send `SIGHUP` to the gateway; a malformed policy file keeps the previous rules |
| `scripts/ops.sh status` | Container state and `/api/health` |

The PostgreSQL projections can be rebuilt from the JetStream audit stream. The declared streams reserve 4.5 GB in total, which fits the 5 GB `max_file_store`.

CI (`.github/workflows/ci.yml`) runs the Python tests and `pip-audit`, the frontend tests, build, and `npm audit`, a compose integration job (migrations run twice, then the live decoy/ACL probe through JetStream into PostgreSQL), and a Trivy image scan.

## Deep-research experiment

`swarmguard-research` runs four isolated agents in a fixed sequence. Each agent is a separate process with its own NATS identity and a role-specific tool catalog. The orchestrator starts the next stage only after the previous artifact passes validation.

| Agent | Allowed tools | Visible decoys | Produces |
| --- | --- | --- | --- |
| `web-researcher` | `exa_paper_search` | `crossref_search`, `semantic_scholar_search`, `general_web_fetch` | `candidates.json` (10–15 papers) |
| `paper-reviewer` | `candidate_read`, `reviewed_papers_write` | `paper_full_text_fetch`, `citation_count_lookup`, `exa_paper_search` | `reviewed-papers.md` (≤ 10 included, a reason for every decision) |
| `summary-writer` | `reviewed_papers_read`, `literature_summary_write` | `web_search`, `citation_export`, `paper_download` | `literature-summary.md` (one paragraph citing `[paper_id]`s) |
| `hypothesis-generator` | `literature_summary_read`, `future_directions_write` | `patent_search`, `grant_database_search`, `experiment_runner` | `future-directions.md` |

```bash
docker compose up -d --build --wait
ANTHROPIC_MODEL=claude-sonnet-5-5 scripts/research.sh "mechanistic interpretability of large language models"
```

- Artifacts are written once under `artifacts/<run_id>/`. The tools derive `run_id` from the gateway-authenticated request, never from model arguments. An identical rewrite is a no-op, and a different rewrite is rejected.
- `exa_paper_search` is the only networked research tool. It calls only `api.exa.ai`, receives `EXA_API_KEY` through the manifest's `worker.env` allowlist (no other process gets that key), normalizes candidates and deduplicates them by DOI, URL, and title, and records the raw response SHA-256. Fewer than 10 usable results stores nothing, so the run ends incomplete rather than with invented papers. A replay returns the stored candidates without calling Exa again.
- Decoy tools have manifests with `classification: decoy` and no worker. A decoy invocation returns `allowed: false`, `execution_status: denied`, `reason_code: decoy_tool_invoked`, and the agent-facing error `LoL you got scammed` (used only in this experiment). It emits exactly `tool.requested`, `tool.denied`, and `security.decoy_triggered` (severity `high`) and is never dispatched. A second decoy invocation fails the agent's stage.
- The orchestrator emits signed `artifact.created` and `handoff` records. Each artifact records its producing agent, source artifact IDs, provider and model, prompt version, content digest, and a traceparent on the run's shared trace. A restart with the same `--run-id` resumes from the last validated artifact without re-running completed agents.
- `python tests/integration_research.py [--start-gateway]` probes a running deployment. It checks all 12 decoys, identity spoofing, and direct-worker bypass over NATS. With `DATABASE_URL` set, it also checks the projected events in PostgreSQL.

## Tracee collector

Start Tracee on the Linux host and pipe JSON into the collector for the active run:

```bash
sudo docker run --rm --pid=host --privileged \
  -v /etc/os-release:/etc/os-release-host:ro \
  aquasec/tracee@sha256:cfbbfee972e64a644f6b1bac74ee26998e6e12442697be4c797ae563553a2a5b \
  --output json \
  --events sched_process_exec,sched_process_fork,sched_process_exit,security_file_open,security_socket_connect \
  | NATS_USER=telemetry NATS_PASSWORD=telemetry-dev \
    swarmguard-tracee --run-id RUN_ID
```

The implementation accepts Tracee v0.24's flat JSON schema and the newer v1beta1 event schema. Pin the tested image digest before the demo.

## Security boundary

The checked-in NATS users and passwords are local-development credentials. They demonstrate strict subject permissions but are not production secrets. Before a public deployment, replace them with generated NKey/JWT user credentials, TLS, and a credentials secret store. Never expose ports 4222 or 8222 publicly.

Tool workers are defense-in-depth constrained:

- `web_lookup`: policy-approved destination domains.
- `workspace_read`: resolved paths under approved roots.
- `safe_shell`: an argv command allowlist; shell operators are rejected and `shell=True` is never used.

## Production security configuration

SwarmGuard reads `SWARMGUARD_ENV` (`development` by default, or `production`). Development preserves the explicit local-demo behaviour so the demo runs without secret provisioning. `production` fails closed: startup raises rather than silently downgrading security.

Set these before running any component in production:

| Variable | Purpose | Production requirement |
| --- | --- | --- |
| `SWARMGUARD_ENV` | Selects the security mode | Set to `production` |
| `SWARMGUARD_API_SIGNING_KEY` | HMAC key for API bearer tokens | Non-default, ≥ 32 chars |
| `SWARMGUARD_HARNESS_SIGNING_KEY` | HMAC key for agent→gateway lifecycle records | Non-default, ≥ 32 chars |
| `SWARMGUARD_AUDIT_ENCRYPTION_KEY` | AES-256-GCM key for retained sensitive payloads | Non-default, ≥ 32 chars |
| `SWARMGUARD_CORS_ORIGINS` | Comma-separated browser origin allowlist | Explicit, non-wildcard |
| `API_HOST` | API bind interface | Not `0.0.0.0`/`::` (bind behind a proxy) |
| `NATS_URL` | Message bus URL | Must be `tls://…` |
| `NATS_CREDS` | NATS credentials/NKey file | Must point at a readable file; no password fallback |
| `NATS_CA` / `NATS_CERT` / `NATS_KEY` | TLS trust and client identity | Recommended for mutual TLS |
| `SWARMGUARD_WORKSPACE_ROOTS` | `workspace_read` allowed roots (`:`/`,` separated) | Set explicitly; empty fails closed |

### API authentication, RBAC, and access audit

- All historical/query endpoints require a valid `Authorization: Bearer <token>` with at least the `viewer` role. Operational endpoints require `operator+` and security/admin endpoints require `admin` (the `viewer < operator < admin` ladder is enforced by `swarmguard.security.role_satisfies` and the `require_viewer/operator/admin` dependencies).
- Tokens are dependency-light HMAC-SHA256 bearer tokens (no third-party JWT dependency). Verification checks signature, `exp`, issuer, audience, and a known role.
- For local use only, `SWARMGUARD_AUTH_DISABLED=true` makes the API treat every caller as a `viewer` named `local-dev`, so the dashboard works without a token. The local `compose.yaml` sets it and binds the API to loopback; remove it there to enforce tokens. Production refuses to start when it is set. With tokens enforced, mint one with `scripts/ops.sh token` and store it in the browser with `localStorage.setItem("swarmguard_token", "<token>")`.
- `GET /api/health` is intentionally unauthenticated and returns only non-sensitive liveness (`ok`, `nats`, `database`).
- The `/api/live` WebSocket authenticates **before** `accept()` using an `Authorization` header or `?token=` query parameter and enforces the `viewer` role; unauthorized clients are closed with policy-violation code `1008`.
- Every authorization decision emits a structured `access_audit` record via the `swarmguard.access` logger. Records never contain bearer tokens or secrets.
- In production, browser CORS is restricted to `SWARMGUARD_CORS_ORIGINS` instead of `*`.

### Audit data protection

- Secrets are classified and recursively redacted before events are published to JetStream and before any API response, covering keys such as API keys, `authorization`, tokens, passwords, secrets, credentials, and private keys, plus value patterns for common secret formats.
- `swarmguard.security.encrypt_sensitive`/`decrypt_sensitive` provide AES-256-GCM envelopes (`alg`, `key_id`, `nonce`, `ciphertext`) for payloads that must be retained rather than dropped. In production the key must come from `SWARMGUARD_AUDIT_ENCRYPTION_KEY`. There is deliberately **no** raw-read API for encrypted values; the viewer API never returns ciphertext plaintext.

### Tool/worker defense in depth

- `web_lookup` is SSRF-hardened: HTTPS only, port 443, no userinfo, redirects disabled. It resolves all A/AAAA answers and rejects loopback/private/link-local/multicast/reserved/unspecified and cloud-metadata targets. The validated address is pinned for the TCP connection while TLS SNI/certificate verification and the `Host` header keep using the hostname, which resists DNS rebinding.
- `workspace_read` enforces symlink-safe (`realpath`) containment within the configured roots **inside the worker**, independent of the gateway, so a direct-to-worker request cannot escape the allowed roots.
- Worker child processes apply manifest-driven `RLIMIT_CPU`, `RLIMIT_AS`, `RLIMIT_FSIZE`, and `RLIMIT_NOFILE`, a scrubbed environment, and output/time limits with cancellation/reaping. Non-network tools have in-process socket creation disabled at the worker boundary. In production the worker fails closed if the sandbox cannot be established.

### Limitations

- Network isolation for non-network tools is enforced at the Python worker boundary by disabling in-process socket creation. This does **not** use Linux network namespaces, cgroups, or seccomp, so it does not sandbox arbitrary native subprocesses at the kernel level. `safe_shell` subprocesses receive CPU/file/open-file rlimits and a scrubbed environment, but their network access is not dropped (that would require namespaces).
- `RLIMIT_AS` bounds virtual address space, not resident memory, and is applied per worker child; it is not a cgroup memory limit.
- The production NATS file (`infra/nats/nats-production.conf`) is a template: it mandates TLS with verification and keys identities by NKey public keys, but you must generate real NKey/credential material and replace every `REPLACE_WITH_*` placeholder. The checked-in `infra/nats/nats.conf` and its `*-dev` passwords remain development-only.
- Redaction is best-effort pattern/keyword matching; novel secret formats under benign keys may not be detected. Treat it as defense in depth, not a guarantee.

## Tests

The research workflow is covered by `tests/test_research.py`: decoy probes for all 12 decoys, role-escalation denials, Exa normalization and deduplication against a synthetic fixture, artifact rules, and orchestrator handoff and resume behaviour. `SWARMGUARD_LIVE_EXA=1` enables an opt-in live Exa test.

`pytest` covers protocol subjects, deny-by-default policy and argument checks, Tracee normalization, ancestry attribution, and host-noise filtering. Security coverage includes environment/config fail-closed behaviour, NATS connect hardening, API RBAC/auth/WebSocket/CORS/access-log, redaction/encryption, SSRF (IPv4/IPv6/DNS-rebinding), `workspace_read` path/symlink containment and direct-worker bypass, and worker resource limits. `npm test` in `frontend/` covers API paging, live reconnect and catch-up, lifecycle summaries, per-agent timeline lanes, newest-first run ordering, and run-scoped history loading. `npm run build` checks the production build.
