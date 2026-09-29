import "@testing-library/jest-dom/vitest";
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  RunDetailView,
  connectLiveEvents,
  fetchPaged,
  mergeCatchUpEvents,
  summarizeToolLifecycle,
  type EventRecord,
  type Page,
  type RunDetail,
} from "./dashboard";

function event(overrides: Partial<EventRecord>): EventRecord {
  return {
    event_id: "evt-1",
    run_id: "run-1",
    timestamp: "2026-01-01T00:00:00Z",
    agent_id: "operator",
    agent_instance_id: "agent-instance-1",
    step_id: "step-1",
    tool_call_id: "tool-call-1",
    attempt: 1,
    sequence: 1,
    kind: "tool.requested",
    trace_id: "trace-1",
    payload: { tool: "safe_shell", state: "requested" },
    ...overrides,
  };
}

class FakeSocket {
  static instances: FakeSocket[] = [];
  url: string;
  sent: string[] = [];
  closed = false;
  onopen: (() => void) | null = null;
  onmessage: ((message: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  send(payload: string) {
    this.sent.push(payload);
  }

  close() {
    this.closed = true;
  }

  emit(item: EventRecord) {
    this.onmessage?.({ data: JSON.stringify(item) });
  }
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeSocket.instances = [];
});

describe("fetchPaged", () => {
  it("requests bounded pages with cursor and filters", async () => {
    const calls: string[] = [];
    const fetcher = async (url: string): Promise<Page<{ run_id: string }>> => {
      calls.push(url);
      return { items: [{ run_id: "run-1" }], next_cursor: null };
    };

    const page = await fetchPaged("/api/runs", { limit: 25, cursor: "run-0", state: "completed" }, fetcher);

    expect(page.items).toEqual([{ run_id: "run-1" }]);
    expect(calls).toEqual(["/api/runs?limit=25&cursor=run-0&state=completed"]);
  });
});

describe("mergeCatchUpEvents", () => {
  it("orders catch-up before live events and removes duplicate event IDs", () => {
    const merged = mergeCatchUpEvents(
      [event({ event_id: "old", sequence: 1 })],
      [event({ event_id: "catch-up", sequence: 2 }), event({ event_id: "live-duplicate", sequence: 3 })],
      [event({ event_id: "live-duplicate", sequence: 3 }), event({ event_id: "live-new", sequence: 4 })],
    );

    expect(merged.map((item) => item.event_id)).toEqual(["old", "catch-up", "live-duplicate", "live-new"]);
  });
});

describe("connectLiveEvents", () => {
  it("reconnects with last event cursor and ignores duplicate catch-up events", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
    const received: EventRecord[] = [];

    const stream = connectLiveEvents({
      urlBase: "ws://example.test/api/live",
      initialCursor: "1:old",
      backoffMs: [10, 20],
      onEvents: (items) => {
        received.push(...items);
      },
    });

    expect(FakeSocket.instances[0].url).toBe("ws://example.test/api/live?cursor=1%3Aold");
    FakeSocket.instances[0].emit(event({ event_id: "catch-up", sequence: 2 }));
    FakeSocket.instances[0].onclose?.();
    await vi.advanceTimersByTimeAsync(10);

    expect(FakeSocket.instances[1].url).toBe("ws://example.test/api/live?cursor=2%3Acatch-up");
    FakeSocket.instances[1].emit(event({ event_id: "catch-up", sequence: 2 }));
    FakeSocket.instances[1].emit(event({ event_id: "live-new", sequence: 3 }));

    expect(received.map((item) => item.event_id)).toEqual(["catch-up", "live-new"]);
    stream.stop();
  });

  it("cleans up sockets and pending reconnect timers on stop", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeSocket);
    const stream = connectLiveEvents({
      urlBase: "ws://example.test/api/live",
      initialCursor: null,
      backoffMs: [10],
      onEvents: () => undefined,
    });

    FakeSocket.instances[0].onclose?.();
    stream.stop();
    await vi.advanceTimersByTimeAsync(10);

    expect(FakeSocket.instances[0].closed).toBe(true);
    expect(FakeSocket.instances).toHaveLength(1);
  });
});

describe("summarizeToolLifecycle", () => {
  it("counts canonical policy decisions and ignores marked legacy compatibility events", () => {
    const summary = summarizeToolLifecycle([
      event({ event_id: "requested", kind: "tool.requested", sequence: 1 }),
      event({
        event_id: "canonical-policy",
        kind: "tool.policy_decided",
        sequence: 2,
        payload: { tool: "safe_shell", state: "policy_decided", decision: "allowed" },
      }),
      event({
        event_id: "legacy-allowed",
        kind: "tool.allowed",
        sequence: 3,
        payload: { tool: "safe_shell", state: "allowed", compatibility: "legacy_policy_decision" },
      }),
      event({ event_id: "completed", kind: "tool.completed", sequence: 4 }),
    ]);

    expect(summary).toEqual({
      requested: 1,
      policies: { allowed: 1, denied: 0 },
      completed: 1,
      failed: 0,
      retries: 0,
    });
  });

  it("reports retries from distinct tool-call attempts", () => {
    const summary = summarizeToolLifecycle([
      event({ event_id: "attempt-1", attempt: 1, kind: "tool.failed" }),
      event({ event_id: "attempt-2", attempt: 2, kind: "tool.completed" }),
    ]);

    expect(summary.retries).toBe(1);
  });

  it("counts real terminal denied events once and skips marked legacy policy events", () => {
    const summary = summarizeToolLifecycle([
      event({
        event_id: "legacy-denied",
        kind: "tool.denied",
        sequence: 1,
        payload: { tool: "safe_shell", state: "denied", compatibility: "legacy_policy_decision" },
      }),
      event({
        event_id: "terminal-denied",
        kind: "tool.denied",
        sequence: 2,
        payload: { tool: "safe_shell", state: "denied" },
      }),
    ]);

    expect(summary.policies.denied).toBe(1);
  });
});

describe("RunDetailView", () => {
  it("renders lifecycle, attempts, latency, policy version, traces, and linked kernel evidence", () => {
    const detail: RunDetail = {
      run: { run_id: "run-1", state: "completed", trace_id: "trace-run" },
      agents: [{ agent_id: "operator", agent_instance_id: "agent-instance-1", state: "completed" }],
      model_steps: [{ step_id: "step-1", state: "completed", trace_id: "trace-step" }],
      tool_calls: [
        {
          tool_call_id: "tool-call-1",
          tool: "safe_shell",
          state: "completed",
          latency_ms: 30,
          trace_id: "trace-tool",
        },
      ],
      tool_attempts: [
        { tool_call_id: "tool-call-1", attempt: 1, state: "failed", latency_ms: 25 },
        { tool_call_id: "tool-call-1", attempt: 2, state: "completed", latency_ms: 30 },
      ],
      policy_decisions: [
        { tool_call_id: "tool-call-1", decision: "allowed", reason: "ok", policy_version: "v1" },
      ],
      artifacts: [
        {
          artifact_id: "artifact-1",
          kind: "evidence",
          uri: "file:///tmp/evidence.json",
          linked_kernel_evidence: "kernel-1",
        },
      ],
    };

    render(<RunDetailView detail={detail} />);

    expect(screen.getByText("safe_shell")).toBeInTheDocument();
    expect(screen.getByText("completed in 30ms")).toBeInTheDocument();
    expect(screen.getByText("attempt 1 failed")).toBeInTheDocument();
    expect(screen.getByText("attempt 2 completed")).toBeInTheDocument();
    expect(screen.getByText("policy v1 allowed")).toBeInTheDocument();
    expect(screen.getByText("trace-tool")).toBeInTheDocument();
    expect(screen.getByText("kernel-1")).toBeInTheDocument();
  });
});
