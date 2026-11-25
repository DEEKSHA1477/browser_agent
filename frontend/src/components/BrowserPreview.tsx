import type { Screenshot, TaskSnapshot } from "../types";

export function BrowserPreview({ task, screenshot }: { task: TaskSnapshot | null; screenshot: Screenshot | null }) {
  const url = screenshot?.url ?? task?.current_url ?? "";
  const live = task && !["completed", "failed", "cancelled"].includes(task.status);
  return (
    <section className="panel preview" aria-label="Live browser preview">
      <div className="panel-head">
        <h2>Live browser</h2>
        {live && task?.browser_open && <span className="live-dot">LIVE</span>}
      </div>
      <div className="browser-chrome">
        <div className="dots" aria-hidden>
          <span />
          <span />
          <span />
        </div>
        <div className="urlbar" title={url}>
          {url ? url.replace(/^https?:\/\//, "") : "about:blank"}
        </div>
      </div>
      <div className="viewport">
        {screenshot ? (
          <img src={`data:image/jpeg;base64,${screenshot.image}`} alt={`Browser showing ${screenshot.title ?? url}`} />
        ) : (
          <div className="viewport-empty muted">
            {task ? (task.browser_open ? "Waiting for the first frame…" : "The browser opens once the task is planned.") : "No browser activity yet."}
          </div>
        )}
        {task?.current_action && live && <div className="action-caption">▶ {task.current_action}</div>}
      </div>
      {task?.thought && live && (
        <p className="thought">
          <span className="muted">Agent:</span> {task.thought}
        </p>
      )}
    </section>
  );
}
