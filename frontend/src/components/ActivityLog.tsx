import { useState } from "react";
import type { LogEntry } from "../types";

export function ActivityLog({ logs }: { logs: LogEntry[] }) {
  const [open, setOpen] = useState(false);
  const shown = open ? logs.slice().reverse() : logs.slice(-6).reverse();
  return (
    <section className="panel log" aria-label="Activity log">
      <div className="panel-head">
        <h2>Activity log</h2>
        {logs.length > 6 && (
          <button className="btn ghost small" onClick={() => setOpen(!open)}>
            {open ? "Show less" : `Show all (${logs.length})`}
          </button>
        )}
      </div>
      <ul className="log-list">
        {shown.map((l, i) => (
          <li key={i} className={`log-${l.level}`}>
            <time>{new Date(l.ts * 1000).toLocaleTimeString()}</time>
            <span>{l.message}</span>
          </li>
        ))}
        {logs.length === 0 && <li className="muted">No activity yet.</li>}
      </ul>
    </section>
  );
}
