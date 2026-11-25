import type { StepStatus, TimelineStep } from "../types";

const ICON: Record<StepStatus, string> = {
  pending: "○",
  active: "",
  done: "✓",
  waiting: "⏳",
  error: "✕",
  skipped: "–",
};

export function Timeline({ steps }: { steps: TimelineStep[] }) {
  return (
    <section className="panel timeline" aria-label="Activity timeline">
      <div className="panel-head">
        <h2>Activity timeline</h2>
      </div>
      <ol className="steps">
        {steps.map((s) => (
          <li key={s.id} className={`step ${s.status} ${s.kind}`}>
            <span className="step-icon" aria-label={s.status}>
              {s.status === "active" ? <span className="spinner" /> : ICON[s.status]}
            </span>
            <div className="step-body">
              <div className="step-label">{s.label}</div>
              {s.detail && <div className="step-detail">{s.detail}</div>}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
