import type { TaskStatus } from "../types";

const LABELS: Record<TaskStatus, string> = {
  queued: "Queued",
  understanding: "Understanding",
  awaiting_clarification: "Needs your input",
  running: "Working",
  paused: "Paused",
  awaiting_human_action: "Needs your help",
  awaiting_approval: "Awaiting approval",
  executing: "Executing approved action",
  verifying: "Verifying",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
};

const TONE: Record<TaskStatus, string> = {
  queued: "neutral",
  understanding: "busy",
  awaiting_clarification: "attention",
  running: "busy",
  paused: "neutral",
  awaiting_human_action: "attention",
  awaiting_approval: "attention",
  executing: "busy",
  verifying: "busy",
  completed: "ok",
  failed: "bad",
  cancelled: "neutral",
};

export function StatusBadge({ status }: { status: TaskStatus | "idle" }) {
  if (status === "idle") return <span className="badge tone-neutral">Ready</span>;
  return (
    <span className={`badge tone-${TONE[status]}`}>
      {TONE[status] === "busy" && <span className="pulse" aria-hidden />}
      {LABELS[status]}
    </span>
  );
}
