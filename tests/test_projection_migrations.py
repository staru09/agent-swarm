from __future__ import annotations

from pathlib import Path


MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "0001_events_and_projections.sql"


def test_projection_migration_creates_append_only_events_and_future_ready_projection_tables() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    for table in [
        "events",
        "runs",
        "agent_instances",
        "model_steps",
        "tool_calls",
        "tool_attempts",
        "policy_decisions",
        "artifacts",
    ]:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql

    assert "event_id TEXT PRIMARY KEY" in sql
    assert "payload JSONB NOT NULL" in sql
    assert "CHECK (attempt >= 1)" in sql
    assert "CHECK (sequence >= 0)" in sql
    assert "UNIQUE (run_id, agent_id, agent_instance_id)" in sql
    assert "UNIQUE (run_id, agent_instance_id, step_id)" in sql
    assert "UNIQUE (run_id, tool_call_id)" in sql
    assert "UNIQUE (run_id, tool_call_id, attempt)" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_events_correlation" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_events_timestamp" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_tool_calls_state" in sql
    assert "CREATE OR REPLACE FUNCTION swarmguard_state_rank(state TEXT)" in sql
    assert "WHEN 'policy_decided' THEN 20" in sql
    assert "WHEN 'completed' THEN 100" in sql
    assert "WHEN 'dead_letter' THEN 90" in sql


def test_projection_migration_keeps_event_log_append_only_with_update_delete_guards() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION reject_events_mutation()" in sql
    assert "BEFORE UPDATE OR DELETE ON events" in sql
    assert "RAISE EXCEPTION 'events is append-only'" in sql
