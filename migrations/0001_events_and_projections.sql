CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  run_id TEXT NOT NULL,
  agent_id TEXT,
  agent_instance_id TEXT,
  step_id TEXT,
  tool_call_id TEXT,
  attempt INTEGER NOT NULL CHECK (attempt >= 1),
  sequence BIGINT NOT NULL CHECK (sequence >= 0),
  kind TEXT NOT NULL,
  idempotency_key TEXT,
  traceparent TEXT,
  tracestate TEXT,
  trace_id TEXT,
  parent_id TEXT,
  timestamp TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL,
  inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_correlation
  ON events (run_id, agent_id, agent_instance_id, step_id, tool_call_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);
CREATE INDEX IF NOT EXISTS idx_events_idempotency_key ON events (idempotency_key);

CREATE OR REPLACE FUNCTION reject_events_mutation()
RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'events is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS events_append_only ON events;
CREATE TRIGGER events_append_only
  BEFORE UPDATE OR DELETE ON events
  FOR EACH ROW EXECUTE FUNCTION reject_events_mutation();

CREATE OR REPLACE FUNCTION swarmguard_state_rank(state TEXT)
RETURNS INTEGER AS $$
BEGIN
  RETURN CASE state
    WHEN 'active' THEN 0
    WHEN 'requested' THEN 10
    WHEN 'policy_decided' THEN 20
    WHEN 'allowed' THEN 25
    WHEN 'dispatched' THEN 30
    WHEN 'started' THEN 40
    WHEN 'heartbeat' THEN 45
    WHEN 'completed' THEN 100
    WHEN 'failed' THEN 100
    WHEN 'denied' THEN 100
    WHEN 'timed_out' THEN 100
    WHEN 'cancelled' THEN 100
    WHEN 'exhausted' THEN 100
    WHEN 'dead_letter' THEN 90
    ELSE 0
  END;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  state TEXT NOT NULL CHECK (state IN ('active', 'completed', 'failed', 'cancelled', 'exhausted')),
  first_event_id TEXT NOT NULL REFERENCES events(event_id),
  last_event_id TEXT NOT NULL REFERENCES events(event_id),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_instances (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  agent_instance_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active', 'completed', 'failed', 'cancelled', 'exhausted')),
  first_event_id TEXT NOT NULL REFERENCES events(event_id),
  last_event_id TEXT NOT NULL REFERENCES events(event_id),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL,
  UNIQUE (run_id, agent_id, agent_instance_id)
);

CREATE TABLE IF NOT EXISTS model_steps (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL,
  agent_id TEXT,
  agent_instance_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('active', 'completed', 'failed', 'cancelled', 'exhausted')),
  first_event_id TEXT NOT NULL REFERENCES events(event_id),
  last_event_id TEXT NOT NULL REFERENCES events(event_id),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL,
  UNIQUE (run_id, agent_instance_id, step_id)
);

CREATE TABLE IF NOT EXISTS tool_calls (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  agent_id TEXT,
  agent_instance_id TEXT,
  step_id TEXT,
  tool TEXT,
  state TEXT NOT NULL CHECK (
    state IN ('requested', 'policy_decided', 'allowed', 'dispatched', 'started', 'heartbeat',
              'completed', 'failed', 'denied', 'timed_out', 'cancelled', 'dead_letter')
  ),
  first_event_id TEXT NOT NULL REFERENCES events(event_id),
  last_event_id TEXT NOT NULL REFERENCES events(event_id),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL,
  UNIQUE (run_id, tool_call_id)
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_state ON tool_calls (state);

CREATE TABLE IF NOT EXISTS tool_attempts (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  attempt INTEGER NOT NULL CHECK (attempt >= 1),
  state TEXT NOT NULL CHECK (
    state IN ('requested', 'policy_decided', 'allowed', 'dispatched', 'started', 'heartbeat',
              'completed', 'failed', 'denied', 'timed_out', 'cancelled', 'dead_letter')
  ),
  first_event_id TEXT NOT NULL REFERENCES events(event_id),
  last_event_id TEXT NOT NULL REFERENCES events(event_id),
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL,
  UNIQUE (run_id, tool_call_id, attempt)
);

CREATE TABLE IF NOT EXISTS policy_decisions (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL,
  tool_call_id TEXT NOT NULL,
  event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id),
  decision TEXT NOT NULL CHECK (decision IN ('allowed', 'denied')),
  reason TEXT,
  created_at TIMESTAMPTZ NOT NULL,
  payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  agent_id TEXT,
  agent_instance_id TEXT,
  step_id TEXT,
  tool_call_id TEXT,
  kind TEXT NOT NULL,
  uri TEXT,
  digest TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  payload JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_correlation
  ON artifacts (run_id, agent_id, agent_instance_id, step_id, tool_call_id);
