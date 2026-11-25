import { useState } from "react";
import { api } from "../api";
import type { TaskSnapshot } from "../types";

export function HumanActionBanner({ task }: { task: TaskSnapshot }) {
  const [busy, setBusy] = useState(false);
  if (!task.human_action || task.status !== "awaiting_human_action") return null;
  const title = task.human_action.kind === "captcha" ? "Human verification needed" : task.human_action.kind === "login" ? "Please sign in" : "Your help is needed";
  return (
    <section className="panel human" role="alert">
      <div className="approval-title">
        <span aria-hidden>🖐</span> {title}
      </div>
      <p>{task.human_action.message}</p>
      <p className="muted small">Use the visible Chromium window on your desktop. NOVA never solves or bypasses these checks.</p>
      <button
        className="btn primary"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.humanDone(task.id);
          } finally {
            setBusy(false);
          }
        }}
      >
        I've completed it
      </button>
    </section>
  );
}
