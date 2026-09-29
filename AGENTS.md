# AGENTS.md

A guide for coding agents working in this repo. Human-facing setup and security docs live in `README.md`; the original design plan is `plan.md`.

SwarmGuard is a least-privilege control plane for AI agent swarms. Every tool call an agent makes crosses NATS to a policy gateway, which authenticates the agent from its NATS subject, applies the tool manifests and YAML policy, denies decoy tools, and dispatches allowed calls to sandboxed tool workers. Every step is published to JetStream, projected into PostgreSQL, traced with OpenTelemetry, and shown in a React dashboard.

## Map

| Path | What lives there |
| --- | --- |
| `src/swarmguard/protocol.py` | Event envelopes, `EventKind`, `ToolRequest`/`ToolResponse`, the tool-call state machine, and the NATS subject builders/parsers. Start here. |
| `src/swarmguard/agent.py` | `AnthropicHarnessAdapter`: the multi-turn Claude tool loop every agent runs. It builds the model-visible catalog and stops after repeated decoy hits. |
| `src/swarmguard/harness.py` | Framework-neutral harness records (run, session, step, handoff, artifact), HMAC-signed and mapped to events. |
| `src/swarmguard/gateway.py` | `ExecutionCoordinator`: the authority for idempotency, decoy denial, policy, retries, deadlines, cancellation, and terminal events. `PolicyGateway` wires it to NATS. |
| `src/swarmguard/policy.py` | `PolicyEngine`: per-agent tool and argument rules from `config/policies.yaml`. |
| `src/swarmguard/registry.py` | Loaders and validators for `config/agents.yaml` and `config/tools.yaml`. |
| `src/swarmguard/tools.py` | `ToolWorker` plus the built-in tools (`web_lookup`, `workspace_read`, `safe_shell`, `echo_metadata`) and the sandbox (rlimits, env scrub, no-network). |
| `src/swarmguard/research.py` | The four-agent deep-research workflow: Exa search, write-once artifact tools, validators, and `ResearchOrchestrator`. |
| `src/swarmguard/security.py` | Dev/production mode, API tokens and RBAC, redaction, AES-GCM encryption, and the local auth switch. |
| `src/swarmguard/bus.py` | NATS connect options (TLS/creds in production), `publish_event` (redacts), and `wait_for_shutdown`. |
| `src/swarmguard/streams.py` / `projector.py` | JetStream topology bootstrap, and the durable consumer that projects events into Postgres. |
| `src/swarmguard/api.py` | FastAPI query API, `/api/live` WebSocket, and static dashboard serving. |
| `src/swarmguard/otel.py` / `telemetry.py` | OpenTelemetry spans / Tracee kernel-event collector. |
| `src/swarmguard/supervisor.py` | Launches the demo agents; `agent_env` gives each agent process its own NATS identity. |
| `config/` | `agents.yaml` (roles, catalogs, decoys), `tools.yaml` (manifests), `policies.yaml` (allowlists). |
| `infra/nats/` | `nats.conf` (dev users and subject ACLs) and `nats-production.conf` (NKey/TLS template). `infra/otel/collector.yaml` is the OTel collector config. |
| `migrations/` | Idempotent SQL applied by the compose `migrate` job. |
| `frontend/src/` | `dashboard.tsx` (all UI logic, exported helpers) and `dashboard.test.tsx`. |
| `scripts/` | `research.sh` (research run), `ops.sh` (runbook), `demo.sh` (policy-boundary demo). |
| `tests/` | `test_*.py` (pytest). `integration_research.py` is a live NATS/Postgres probe; `integration_smoke.py` is stale (predates API auth). |
| `artifacts/`, `runtime/` | Research outputs and run logs (gitignored). |

## How a tool call flows

1. The agent publishes a `ToolRequest` on `swarm.<run_id>.agent.<agent_id>.tool.request`. NATS ACLs let each agent publish only on its own subject, so the subject is the identity.
2. The gateway checks the request against the subject, resolves the manifest, and handles it in this order:
   - Decoys (a manifest with `classification: decoy`, or a tool listed in the agent's `decoy_tools`) are denied with `reason_code: decoy_tool_invoked`.
   - Otherwise policy is evaluated, and an allowed call is dispatched to `private.tool.<tool>.execute`.
3. The worker validates the input schema again, runs the tool in a forked, sandboxed child, validates the output, and replies.
4. The gateway and workers publish lifecycle events to `audit.<run_id>.<kind>`. The `SWARMGUARD_AUDIT` stream feeds the projector (Postgres), the API, and the dashboard.
5. The research orchestrator adds signed `artifact.created` and `handoff` records between stages.

## Commands

```bash
. .venv/bin/activate && pip install -e '.[dev]'                   # setup
pytest -q                                                         # ~300 Python tests, ~25s, no services needed
cd frontend && npm test && npm run build && cd ..                 # frontend tests and type-checked build
docker compose up -d --build --wait                               # NATS, Postgres, migrate, streams, projector, OTel, API on 127.0.0.1:8000
ANTHROPIC_MODEL=claude-sonnet-5-5 scripts/research.sh "topic" [run-id]   # live research run (needs ANTHROPIC_API_KEY, EXA_API_KEY)
DATABASE_URL=postgresql://swarmguard:swarmguard-dev@127.0.0.1:5432/swarmguard \
  python tests/integration_research.py --start-gateway            # live decoy/ACL probe (omit --start-gateway if a gateway runs)
scripts/ops.sh {migrate|streams|token|backup|restore|reload-policy|status}
```

## Common changes

- **Add a tool:**
  1. Add a manifest to `config/tools.yaml`. Secrets go in `worker.env`, which is the only way they reach the sandboxed child.
  2. Implement it in `tools.execute`, or as a `(arguments, run_id)` function in `research.TOOLS`.
  3. Allow it in `config/policies.yaml` and list it under the agent's `tools` in `config/agents.yaml`.
  4. Start its worker with `swarmguard-tool <name>` (and in `scripts/research.sh` if it's a research tool).
- **Add an agent:**
  1. Add it to `config/agents.yaml` and `config/policies.yaml`.
  2. Add a dev user in `infra/nats/nats.conf` and an NKey identity in `infra/nats/nats-production.conf`.
  3. Agents without a `default_task` are skipped by the supervisor and run only through an orchestrator.
- **Add a decoy:** add a manifest with `classification: decoy`, `dispatchable: false`, and no `worker`, then list it in the agent's `decoy_tools`.
- **Add a research stage:** add a `Stage` to `research.STAGES` with its output file, source files, task text, and validator.
- **Add an event kind:** add it to `protocol.EventKind`. If it carries a `tool_call_id`, check the projector's state mapping and `STATE_RANK`, and check how the dashboard renders it.
- **Change the schema:** add a new, idempotent `migrations/000N_*.sql`. Never edit an applied migration.

## Gotchas

- Some tests pin config: `tests/test_registry.py` (agent and tool lists), `tests/test_supervisor.py` (demo agents), `tests/test_nats_config.py` and `tests/test_nats_production_config.py` (ACL strings, README text). Update them when you add agents, tools, or users.
- Worker children scrub their environment after fork. Anything a tool needs must be a manifest `worker.env` key or a module-level value resolved before fork (for example `research.ARTIFACTS_ROOT`).
- `execute(tool, arguments, run_id)` and `execute_async(..., run_id=None)` take a `run_id`, so test fakes must accept it. Research tools take `run_id` from the authenticated request, never from model arguments.
- Research artifacts are write-once: an identical rewrite is a no-op, and a different one raises. Re-running with the same `--run-id` resumes from the last validated artifact without calling Exa again.
- JetStream reserves each stream's `max_bytes` up front. The total must fit `max_file_store` in `nats.conf`, which `test_stream_topology.py` checks.
- The `events` table is append-only (enforced by a DB trigger). Never delete audit rows; the projections can be rebuilt from JetStream.
- Local compose sets `SWARMGUARD_AUTH_DISABLED=true`, and the host-networked API binds `127.0.0.1`. Production refuses to start with that switch, and `tests/test_api_auth.py` clears it.
- `SWARMGUARD_ENV=production` fails closed: TLS NATS, creds files, and strong keys are all required. The `*-dev` passwords in `nats.conf` are local only.
- Dashboard lanes come from the agents present in the selected run. `probe-*` runs come from the integration probe and contain only decoy denials.
- Research stage tasks name their tools explicitly, so models rarely touch decoys. Set `SWARMGUARD_TOOL_CATALOG=all` to show agents every tool.
- Model settings come from the environment: `ANTHROPIC_MODEL` (default `claude-opus-5-5`), `ANTHROPIC_MAX_TOKENS`, and `SWARMGUARD_MAX_MODEL_TURNS`.

## Conventions

- Match the surrounding code. A global ruff config may flag idioms the repo uses on purpose (`typing.Callable`, `timezone.utc`, `asyncio.TimeoutError`); follow the repo, not the linter.
- Keep diffs small and add or adjust a test with every behaviour change. The frontend tests exported pure helpers in `dashboard.tsx`.
- Commit messages use `area: summary` (for example `gateway: …`, `frontend: …`, `docs: …`).
