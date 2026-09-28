# SwarmGuard

SwarmGuard is a least-privilege control plane and kernel-level flight recorder for AI agent swarms. Every tool request and agent-to-agent message crosses NATS. A policy gateway evaluates inspectable YAML rules, while Tracee/eBPF independently records process, file, and network activity.

## Demo architecture

- Three real Anthropic-powered agent processes: researcher, analyst, and operator.
- NATS subject permissions prevent sender spoofing and direct access to private tool workers.
- The gateway applies tool and argument rules from `config/policies.yaml`.
- JetStream retains requests, decisions, results, and A2A traffic.
- Tracee captures focused host events; the collector joins process ancestry to the supervisor PID registry.
- FastAPI and React render a live per-agent timeline.

eBPF does not infer semantic intent. It provides independent evidence of OS effects. In the MVP, direct bypass activity creates an alert but does not kill the agent.

## Linux setup

The supported demo target is native Ubuntu with BTF, Docker, passwordless sudo, Python 3.11+, and Node 22+. Do not rely on WSL2 for the final presentation.

```bash
./scripts/preflight.sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
cd frontend && npm install && npm run build && cd ..
docker compose up -d --build
pytest
```

Set `ANTHROPIC_API_KEY`, then run:

```bash
. .venv/bin/activate
./scripts/demo.sh
```

Open `http://HOST:8000`. The EC2 security group should expose the dashboard only to the presenter’s IP; NATS ports remain bound to loopback.
For a private EC2 session, prefer an SSH tunnel:

```bash
ssh -L 8000:127.0.0.1:8000 -i YOUR_KEY.pem ubuntu@YOUR_HOST
```

Then open `http://127.0.0.1:8000`.

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

## Tests

`pytest` covers protocol subjects, deny-by-default policy and argument checks, Tracee normalization, ancestry attribution, and host-noise filtering. The frontend production build is checked separately with `npm run build`.
