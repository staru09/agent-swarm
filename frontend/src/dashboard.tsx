import React, { useEffect, useMemo, useState } from "react";

export type Page<T> = {
  items: T[];
  next_cursor: string | null;
};

export type EventRecord = {
  event_id: string;
  run_id: string;
  timestamp: string;
  agent_id: string | null;
  agent_instance_id?: string | null;
  step_id?: string | null;
  tool_call_id?: string | null;
  attempt?: number;
  sequence?: number;
  kind: string;
  trace_id: string | null;
  payload: Record<string, unknown>;
};

export type ProjectionRow = Record<string, unknown> & {
  state?: string;
  trace_id?: string;
  latency_ms?: number | null;
};

export type RunRow = ProjectionRow & {
  run_id: string;
};

export type RunDetail = {
  run: RunRow;
  agents: ProjectionRow[];
  model_steps: ProjectionRow[];
  tool_calls: ProjectionRow[];
  tool_attempts: ProjectionRow[];
  policy_decisions: ProjectionRow[];
  artifacts: ProjectionRow[];
};

type Fetcher<T> = (url: string) => Promise<Page<T>>;

// The API now requires a viewer+ bearer token. The token is provisioned out of
// band (e.g. injected into localStorage or window.__SWARMGUARD_TOKEN__ by the
// hosting page/reverse proxy). It is sent as an Authorization header for HTTP
// and as a ?token= query parameter for the WebSocket handshake.
export function authToken(): string | null {
  try {
    if (typeof localStorage !== "undefined") {
      const stored = localStorage.getItem("swarmguard_token");
      if (stored) return stored;
    }
  } catch {
    /* localStorage may be unavailable; fall through */
  }
  if (typeof window !== "undefined") {
    const injected = (window as unknown as { __SWARMGUARD_TOKEN__?: string }).__SWARMGUARD_TOKEN__;
    if (injected) return injected;
  }
  return null;
}

export function authHeaders(): Record<string, string> {
  const token = authToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export async function apiFetch(url: string): Promise<Response> {
  return fetch(url, { headers: authHeaders() });
}

export async function fetchPaged<T>(
  path: string,
  params: Record<string, string | number | null | undefined> = {},
  fetcher?: Fetcher<T>,
): Promise<Page<T>> {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const url = search.size ? `${path}?${search.toString()}` : path;
  if (fetcher) return fetcher(url);
  const response = await apiFetch(url);
  if (!response.ok) throw new Error(`request failed: ${response.status}`);
  return response.json() as Promise<Page<T>>;
}

export function eventCursor(event: EventRecord): string {
  return `${event.sequence ?? 0}:${event.event_id}`;
}

export function mergeCatchUpEvents(
  current: EventRecord[],
  catchUp: EventRecord[],
  live: EventRecord[],
): EventRecord[] {
  const seen = new Set<string>();
  const merged: EventRecord[] = [];
  for (const item of [...current, ...catchUp, ...live]) {
    if (seen.has(item.event_id)) continue;
    seen.add(item.event_id);
    merged.push(item);
  }
  return merged.sort((left, right) => (left.sequence ?? 0) - (right.sequence ?? 0));
}

export type LiveEventStream = {
  stop: () => void;
};

export function connectLiveEvents({
  urlBase,
  initialCursor,
  onEvents,
  onConnected = () => undefined,
  backoffMs = [500, 1000, 2000, 5000, 10000],
}: {
  urlBase: string;
  initialCursor: string | null;
  onEvents: (events: EventRecord[]) => void;
  onConnected?: (connected: boolean) => void;
  backoffMs?: number[];
}): LiveEventStream {
  let socket: WebSocket | null = null;
  let stopped = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let cursor = initialCursor;
  // Keep dedupe for the stream lifecycle. Bounding it would permit duplicate
  // catch-up delivery after reconnect once an older event aged out.
  const seen = new Set<string>();

  const url = () => {
    const params = new URLSearchParams();
    if (cursor) params.set("cursor", cursor);
    const token = authToken();
    if (token) params.set("token", token);
    const query = params.toString();
    return query ? `${urlBase}?${query}` : urlBase;
  };

  const connect = () => {
    socket = new WebSocket(url());
    socket.onopen = () => {
      attempt = 0;
      onConnected(true);
      socket?.send("ready");
    };
    socket.onmessage = (message) => {
      const item = JSON.parse(message.data) as EventRecord;
      cursor = eventCursor(item);
      if (!seen.has(item.event_id)) {
        seen.add(item.event_id);
        onEvents([item]);
      }
      socket?.send("next");
    };
    socket.onclose = () => {
      onConnected(false);
      if (stopped) return;
      const delay = backoffMs[Math.min(attempt, backoffMs.length - 1)] ?? 10000;
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };
  };

  connect();

  return {
    stop: () => {
      stopped = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      socket?.close();
    },
  };
}

export function summarizeToolLifecycle(events: EventRecord[]) {
  const attempts = new Set<string>();
  const deniedToolCalls = new Set<string>();
  const summary = {
    requested: 0,
    policies: { allowed: 0, denied: 0 },
    completed: 0,
    failed: 0,
    retries: 0,
  };
  for (const item of events) {
    if (!item.kind.startsWith("tool.")) continue;
    if (item.tool_call_id && item.attempt) attempts.add(`${item.tool_call_id}:${item.attempt}`);
    if (item.kind === "tool.requested") summary.requested += 1;
    if (item.kind === "tool.policy_decided") {
      const decision = item.payload.decision === "denied" ? "denied" : "allowed";
      summary.policies[decision] += 1;
      if (decision === "denied" && item.tool_call_id) deniedToolCalls.add(item.tool_call_id);
    }
    if (item.payload.compatibility === "legacy_policy_decision") continue;
    if (item.kind === "tool.completed") summary.completed += 1;
    if (item.kind === "tool.failed") summary.failed += 1;
    if (item.kind === "tool.denied" && (!item.tool_call_id || !deniedToolCalls.has(item.tool_call_id))) {
      summary.policies.denied += 1;
      if (item.tool_call_id) deniedToolCalls.add(item.tool_call_id);
    }
  }
  summary.retries = Math.max(0, attempts.size - new Set(events.map((item) => item.tool_call_id).filter(Boolean)).size);
  return summary;
}

function text(value: unknown, fallback = ""): string {
  return value === undefined || value === null ? fallback : String(value);
}

function toolAttempts(detail: RunDetail, toolCallId: string): ProjectionRow[] {
  return detail.tool_attempts.filter((attempt) => attempt.tool_call_id === toolCallId);
}

function policyFor(detail: RunDetail, toolCallId: string): ProjectionRow | undefined {
  return detail.policy_decisions.find((decision) => decision.tool_call_id === toolCallId);
}

export function RunDetailView({ detail }: { detail: RunDetail }) {
  return (
    <section className="run-summary">
      <div className="summary-heading">
        <p className="eyebrow">RUN DETAIL</p>
        <h2>{detail.run.run_id}</h2>
        <small>{text(detail.run.state, "active")}</small>
      </div>
      <div className="agent-grid">
        {detail.agents.map((agent) => (
          <article className="agent-card" key={text(agent.agent_instance_id)}>
            <span>{text(agent.agent_id, "agent")}</span>
            <strong>{text(agent.state, "active")}</strong>
            <small>{text(agent.agent_instance_id)}</small>
          </article>
        ))}
        {detail.model_steps.map((step) => (
          <article className="agent-card" key={text(step.step_id)}>
            <span>model step</span>
            <strong>{text(step.state, "active")}</strong>
            <small>{text(step.step_id)} {text(step.trace_id)}</small>
          </article>
        ))}
      </div>
      <div className="tool-list">
        {detail.tool_calls.map((call) => {
          const callId = text(call.tool_call_id);
          const policy = policyFor(detail, callId);
          return (
            <article className="tool-card" key={callId}>
              <header>
                <h3>{text(call.tool, "unknown tool")}</h3>
                <span>{text(call.state)} in {text(call.latency_ms, "0")}ms</span>
              </header>
              <p>Trace <code>{text(call.trace_id)}</code></p>
              {policy && (
                <p>
                  <span>policy {text(policy.policy_version, "unknown")} {text(policy.decision)}</span>
                  {policy.reason ? `: ${text(policy.reason)}` : ""}
                </p>
              )}
              <ul>
                {toolAttempts(detail, callId).map((attempt) => (
                  <li key={`${callId}-${text(attempt.attempt)}`}>
                    <span>attempt {text(attempt.attempt)} {text(attempt.state)}</span>
                    {attempt.latency_ms ? ` (${text(attempt.latency_ms)}ms)` : ""}
                  </li>
                ))}
              </ul>
            </article>
          );
        })}
      </div>
      <div className="artifact-list">
        {detail.artifacts.map((artifact) => (
          <article className="tool-card" key={text(artifact.artifact_id)}>
            <h3>{text(artifact.kind, "artifact")}</h3>
            <p>{text(artifact.uri)}</p>
            <p>Kernel evidence <code>{text(artifact.linked_kernel_evidence, "none")}</code></p>
          </article>
        ))}
      </div>
    </section>
  );
}

const LANES = ["researcher", "analyst", "operator", "system"];

export function App() {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [runs, setRuns] = useState<RunRow[]>([]);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<EventRecord | null>(null);
  const [filter, setFilter] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [page, setPage] = useState<"timeline" | "summary">("timeline");

  useEffect(() => {
    let closed = false;
    let stream: LiveEventStream | undefined;
    const load = async () => {
      const [runPage, eventPage] = await Promise.all([
        fetchPaged<RunRow>("/api/runs", { limit: 100 }),
        fetchPaged<EventRecord>("/api/events", { limit: 100 }),
      ]);
      if (closed) return;
      setRuns(runPage.items);
      setEvents(eventPage.items);
      const runId = runPage.items[0]?.run_id ?? eventPage.items.at(-1)?.run_id ?? "";
      setSelectedRun(runId);
      if (runId) setDetail(await apiFetch(`/api/runs/${encodeURIComponent(runId)}`).then((response) => response.json()));
      const last = eventPage.items.at(-1);
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      stream = connectLiveEvents({
        urlBase: `${protocol}://${location.host}/api/live`,
        initialCursor: last ? eventCursor(last) : null,
        onConnected: setConnected,
        onEvents: (items) => setEvents((current) => mergeCatchUpEvents(current, [], items).slice(-1000)),
      });
    };
    void load();
    return () => {
      closed = true;
      stream?.stop();
    };
  }, []);

  useEffect(() => {
    if (!selectedRun) {
      setDetail(null);
      return;
    }
    void apiFetch(`/api/runs/${encodeURIComponent(selectedRun)}`)
      .then((response) => (response.ok ? response.json() : null))
      .then((loaded: RunDetail | null) => setDetail(loaded));
  }, [selectedRun]);

  const runEvents = useMemo(
    () => events.filter((event) => !selectedRun || event.run_id === selectedRun),
    [events, selectedRun],
  );
  const visible = useMemo(
    () =>
      runEvents.filter(
        (event) =>
          !filter ||
          event.kind.includes(filter) ||
          event.agent_id?.includes(filter) ||
          text(event.payload.tool).includes(filter) ||
          event.trace_id?.includes(filter),
      ),
    [runEvents, filter],
  );
  const usage = useMemo(() => summarizeToolLifecycle(runEvents), [runEvents]);

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">AGENT CONTROL PLANE</p>
          <h1>SwarmGuard</h1>
        </div>
        <span className={connected ? "status online" : "status"}>{connected ? "LIVE" : "OFFLINE"}</span>
      </header>

      <section className="summary">
        <Metric label="Events" value={runEvents.length} />
        <Metric label="Denied" value={usage.policies.denied} danger />
        <Metric label="Retries" value={usage.retries} />
        <select value={selectedRun} onChange={(event) => setSelectedRun(event.target.value)}>
          <option value="">All runs</option>
          {runs.map((run) => <option value={run.run_id} key={run.run_id}>{run.run_id}</option>)}
        </select>
        <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter agent, event, trace…" />
      </section>

      <nav className="tabs">
        <button className={page === "timeline" ? "active" : ""} onClick={() => setPage("timeline")}>Timeline</button>
        <button className={page === "summary" ? "active" : ""} onClick={() => setPage("summary")}>Run summary</button>
      </nav>

      {page === "timeline" ? (
        <section className="timeline">
          {LANES.map((lane) => (
            <div className="lane" key={lane}>
              <div className="lane-name">{lane}</div>
              <div className="events">
                {visible
                  .filter((event) => (event.agent_id ?? "system") === lane)
                  .map((event) => (
                    <button
                      key={event.event_id}
                      className={`event ${event.kind.replace(".", "-")}`}
                      title={event.kind}
                      onClick={() => setSelected(event)}
                    >
                      <span>{new Date(event.timestamp).toLocaleTimeString()}</span>
                      <strong>{event.kind}</strong>
                      <small>{text(event.payload.tool ?? event.payload.event ?? event.trace_id)}</small>
                    </button>
                  ))}
              </div>
            </div>
          ))}
        </section>
      ) : detail ? (
        <RunDetailView detail={detail} />
      ) : (
        <section className="run-summary"><p className="empty">No run detail loaded.</p></section>
      )}

      {selected && (
        <aside>
          <button className="close" onClick={() => setSelected(null)}>x</button>
          <p className="eyebrow">{selected.agent_id ?? "system"}</p>
          <h2>{selected.kind}</h2>
          <p>Trace {selected.trace_id}</p>
          <pre>{JSON.stringify(selected.payload, null, 2)}</pre>
        </aside>
      )}
    </main>
  );
}

function Metric({ label, value, danger = false }: { label: string; value: number; danger?: boolean }) {
  return <div className={danger && value ? "metric danger" : "metric"}><span>{label}</span><strong>{value}</strong></div>;
}
