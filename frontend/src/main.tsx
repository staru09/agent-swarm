import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Event = {
  event_id: string;
  run_id: string;
  timestamp: string;
  agent_id: string | null;
  kind: string;
  trace_id: string;
  payload: Record<string, unknown>;
};

type AgentUsage = {
  agent: string;
  total: number;
  tools: { name: string; count: number; allowed: number; denied: number }[];
};

const LANES = ["researcher", "analyst", "operator", "system"];

function App() {
  const [events, setEvents] = useState<Event[]>([]);
  const [connected, setConnected] = useState(false);
  const [selected, setSelected] = useState<Event | null>(null);
  const [filter, setFilter] = useState("");
  const [selectedRun, setSelectedRun] = useState("");
  const [page, setPage] = useState<"timeline" | "summary">("timeline");

  useEffect(() => {
    fetch("/api/events").then((response) => response.json()).then((loaded: Event[]) => {
      setEvents(loaded);
      if (loaded.length) setSelectedRun(loaded[loaded.length - 1].run_id);
    });
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    const socket = new WebSocket(`${protocol}://${location.host}/api/live`);
    socket.onopen = () => {
      setConnected(true);
      socket.send("ready");
    };
    socket.onmessage = (message) => {
      const event = JSON.parse(message.data) as Event;
      setEvents((current) => [...current.slice(-999), event]);
      socket.send("next");
    };
    socket.onclose = () => setConnected(false);
    return () => socket.close();
  }, []);

  const runEvents = useMemo(
    () => events.filter((event) => !selectedRun || event.run_id === selectedRun),
    [events, selectedRun],
  );
  const visible = useMemo(
    () => runEvents.filter(
      (event) =>
        (!filter || event.kind.includes(filter) || event.agent_id?.includes(filter)),
    ),
    [runEvents, filter],
  );
  const runs = useMemo(() => [...new Set(events.map((event) => event.run_id))].reverse(), [events]);
  const usage = useMemo<AgentUsage[]>(() => {
    const agents = new Map<string, Map<string, { count: number; allowed: number; denied: number }>>();
    for (const event of runEvents) {
      if (!event.kind.startsWith("tool.") || !event.agent_id || !event.payload.tool) continue;
      const tool = String(event.payload.tool ?? "unknown");
      const tools = agents.get(event.agent_id) ?? new Map();
      const counts = tools.get(tool) ?? { count: 0, allowed: 0, denied: 0 };
      if (event.kind === "tool.requested") counts.count += 1;
      if (event.kind === "tool.allowed") counts.allowed += 1;
      if (event.kind === "tool.denied") counts.denied += 1;
      tools.set(tool, counts);
      agents.set(event.agent_id, tools);
    }
    return [...agents.entries()].map(([agent, tools]) => ({
      agent,
      total: [...tools.values()].reduce((sum, counts) => sum + counts.count, 0),
      tools: [...tools.entries()].map(([name, counts]) => ({ name, ...counts })),
    }));
  }, [runEvents]);

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
        <Metric label="Denied" value={runEvents.filter((event) => event.kind === "tool.denied").length} danger />
        <Metric label="Kernel alerts" value={runEvents.filter((event) => event.kind === "kernel.alert").length} danger />
        <select value={selectedRun} onChange={(event) => setSelectedRun(event.target.value)}>
          <option value="">All runs</option>
          {runs.map((run) => <option value={run} key={run}>{run}</option>)}
        </select>
        <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder="Filter agent or event…" />
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
                      <small>{String(event.payload.tool ?? event.payload.event ?? "")}</small>
                    </button>
                  ))}
              </div>
            </div>
          ))}
        </section>
      ) : (
        <section className="run-summary">
          <div className="summary-heading">
            <p className="eyebrow">RUN SUMMARY</p>
            <h2>{selectedRun || "All runs"}</h2>
          </div>
          <div className="agent-grid">
            {usage.map((item) => (
              <article className="agent-card" key={item.agent}>
                <span>{item.agent}</span>
                <strong>{item.total}</strong>
                <small>total tool invocations</small>
                <ul>
                  {item.tools.map((tool) => (
                    <li key={tool.name}>
                      <code>{tool.name}</code>
                      <span className="tool-counts">
                        <b>{tool.count} invoked</b>
                        {!!tool.allowed && <em className="allowed">{tool.allowed} allowed</em>}
                        {!!tool.denied && <em className="denied">{tool.denied} denied</em>}
                      </span>
                    </li>
                  ))}
                </ul>
              </article>
            ))}
            {!usage.length && <p className="empty">No tool invocations recorded for this run.</p>}
          </div>
        </section>
      )}

      {selected && (
        <aside>
          <button className="close" onClick={() => setSelected(null)}>×</button>
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

createRoot(document.getElementById("root")!).render(<React.StrictMode><App /></React.StrictMode>);
