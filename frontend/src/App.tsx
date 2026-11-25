import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import { ActivityLog } from "./components/ActivityLog";
import { ApprovalCard } from "./components/ApprovalCard";
import { BrowserPreview } from "./components/BrowserPreview";
import { ChatPanel } from "./components/ChatPanel";
import { HumanActionBanner } from "./components/HumanActionBanner";
import { ResultsPanel } from "./components/ResultsPanel";
import { StatusBadge } from "./components/StatusBadge";
import { Timeline } from "./components/Timeline";
import { Launcher } from "./components/Launcher";
import { TERMINAL, type Capability, type Health } from "./types";
import { useTaskSocket } from "./useTaskSocket";

interface SessionTask {
  id: string;
  instruction: string;
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [history, setHistory] = useState<SessionTask[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { snapshot, setSnapshot, screenshot, state: socketState } = useTaskSocket(taskId);
  const task = snapshot && snapshot.id === taskId ? snapshot : null;
  const terminal = !task || TERMINAL.includes(task.status);

  useEffect(() => {
    const load = () =>
      api
        .health()
        .then((h) => {
          setHealth(h);
          setHealthError(null);
        })
        .catch((e) => setHealthError(e.message));
    load();
    const t = window.setInterval(load, 15000);
    return () => window.clearInterval(t);
  }, []);

  const run = useCallback(async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  const onSubmit = async (text: string) => {
    if (task?.status === "awaiting_clarification") {
      await run(() => api.clarify(task.id, text));
      return;
    }
    await run(async () => {
      const res = await api.createTask(text);
      setSnapshot(res.snapshot);
      setTaskId(res.task_id);
      setHistory((h) => [{ id: res.task_id, instruction: text }, ...h].slice(0, 20));
    });
  };

  const onStartCapability = async (cap: Capability, details: Record<string, string>) => {
    await run(async () => {
      const res = await api.startCapability(cap.id, details);
      setSnapshot(res.snapshot);
      setTaskId(res.task_id);
      const first = cap.fields.find((f) => f.required);
      const label = `${cap.icon} ${cap.title}${first && details[first.key] ? `: ${details[first.key]}` : ""}`;
      setHistory((h) => [{ id: res.task_id, instruction: label }, ...h].slice(0, 20));
    });
  };

  const onSelectResult = async (itemId: string) => {
    if (!task) return;
    const current = task;
    await run(async () => {
      const res = await api.select(current.id, itemId);
      if (res.task_id !== current.id) {
        setSnapshot(res.snapshot);
        setTaskId(res.task_id);
        setHistory((h) => [{ id: res.task_id, instruction: `▸ ${res.snapshot.details?.Selected ?? "Selected item"}` }, ...h].slice(0, 20));
      }
    });
  };

  const llmReady = !!health?.llm_configured;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <div className="logo" aria-hidden>
            N
          </div>
          <div>
            <h1>NOVA</h1>
            <p className="muted small">AI Browser Agent · humans approve what matters</p>
          </div>
        </div>
        <div className="top-actions">
          <StatusBadge status={task?.status ?? "idle"} />
          {task && !terminal && (
            <>
              {task.paused ? (
                <button className="btn ghost" onClick={() => run(() => api.resume(task.id))} disabled={busy}>
                  ▶ Resume
                </button>
              ) : (
                <button className="btn ghost" onClick={() => run(() => api.pause(task.id))} disabled={busy}>
                  ❚❚ Pause
                </button>
              )}
              <button className="btn danger" onClick={() => run(() => api.stop(task.id))} disabled={busy}>
                ■ Stop
              </button>
            </>
          )}
          {task && terminal && task.can_retry && (
            <button className="btn ghost" onClick={() => run(() => api.retry(task.id))} disabled={busy}>
              ↻ Retry
            </button>
          )}
          {task && terminal && task.browser_open && (
            <button className="btn ghost" onClick={() => run(() => api.closeBrowser(task.id))} disabled={busy}>
              Close browser
            </button>
          )}
          {task && terminal && (
            <button className="btn ghost" onClick={() => setTaskId(null)}>
              + New task
            </button>
          )}
        </div>
      </header>

      {healthError && <div className="banner bad">{healthError}</div>}
      {health && !health.llm_configured && (
        <div className="banner warn">
          <span>
            No LLM API key configured on the backend. Add <code>GEMINI_API_KEY</code> (or set <code>LLM_PROVIDER=openai</code> and{" "}
            <code>OPENAI_API_KEY</code>) to <code>.env</code> and restart the backend. You can still browse the tasks below.
          </span>
        </div>
      )}
      {error && (
        <div className="banner bad" role="alert">
          <span>{error}</span>
          <button className="btn ghost small" onClick={() => setError(null)}>
            Dismiss
          </button>
        </div>
      )}
      {task?.error && task.status === "failed" && <div className="banner bad">{task.error.message}</div>}
      {socketState === "reconnecting" && <div className="banner warn">Live connection lost — reconnecting…</div>}

      <main className="layout">
        <div className="col left">
          <ChatPanel task={task} busy={busy} canStartNew={terminal} llmReady={llmReady} onSubmit={onSubmit} />
          {history.length > 0 && (
            <section className="panel history" aria-label="Session history">
              <div className="panel-head">
                <h2>This session</h2>
              </div>
              <ul>
                {history.map((h) => (
                  <li key={h.id}>
                    <button className={`link ${h.id === taskId ? "active" : ""}`} onClick={() => setTaskId(h.id)}>
                      {h.instruction}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <div className="col center">
          {task?.approval && <ApprovalCard approval={task.approval} />}
          {task && <HumanActionBanner task={task} />}
          {task ? (
            <BrowserPreview task={task} screenshot={screenshot} />
          ) : (
            <Launcher disabled={busy || !llmReady} onStart={onStartCapability} />
          )}
          {task && (
            <ResultsPanel
              results={task.results}
              busy={busy}
              selectMode={
                task.status === "awaiting_clarification" ? "answer" : terminal && llmReady ? "follow" : null
              }
              onSelect={onSelectResult}
            />
          )}
          {task && task.downloads.length > 0 && (
            <section className="panel">
              <div className="panel-head">
                <h2>Downloads</h2>
              </div>
              <ul className="log-list">
                {task.downloads.map((d, i) => (
                  <li key={i} className={d.path ? "log-success" : "log-error"}>
                    <span>{d.path ? `${d.name} → ${d.path}` : `${d.name}: ${d.error}`}</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <div className="col right">
          {task && (
            <section className="panel status-card">
              <div className="panel-head">
                <h2>Current task</h2>
                <StatusBadge status={task.status} />
              </div>
              <p className="goal">{task.goal ?? task.instruction}</p>
              {Object.keys(task.details ?? {}).length > 0 && (
                <dl className="task-details">
                  {Object.entries(task.details).map(([k, v]) => (
                    <div key={k}>
                      <dt>{k}</dt>
                      <dd>{v}</dd>
                    </div>
                  ))}
                </dl>
              )}
              <div className="meta muted small">
                <span>Steps: {task.step_count}</span>
                {task.category && <span>Type: {task.category}</span>}
                {(task.model_in_use || health) && <span>Model: {task.model_in_use ?? health?.llm_model}</span>}
              </div>
              {task.model_is_local && !terminal && (
                <p className="slow-note small">
                  ⏳ Gemini isn't available right now, so NOVA is using the local model on this computer. Each step takes
                  about 1-2 minutes. The task is still working.
                </p>
              )}
            </section>
          )}
          {task ? (
            <>
              <Timeline steps={task.timeline} />
              <ActivityLog logs={task.logs} />
            </>
          ) : (
            <section className="panel">
              <div className="panel-head">
                <h2>How NOVA works</h2>
              </div>
              <ol className="howto">
                <li>Understands your request and plans the steps.</li>
                <li>Opens a visible Chromium browser and reads each page through its DOM and accessibility tree.</li>
                <li>Decides one action at a time: navigate, click, type, scroll, or extract.</li>
                <li>
                  <strong>Pauses for your approval</strong> before payments, orders, bookings, applications, messages, deletions, or account changes.
                </li>
                <li>Checks the result before reporting success.</li>
              </ol>
            </section>
          )}
        </div>
      </main>
    </div>
  );
}
