export type TaskStatus =
  | "queued"
  | "understanding"
  | "awaiting_clarification"
  | "running"
  | "paused"
  | "awaiting_human_action"
  | "awaiting_approval"
  | "executing"
  | "verifying"
  | "completed"
  | "failed"
  | "cancelled";

export type StepStatus = "pending" | "active" | "done" | "waiting" | "error" | "skipped";

export interface TimelineStep {
  id: string;
  label: string;
  kind: "stage" | "action";
  status: StepStatus;
  detail: string | null;
  started_at: number | null;
  finished_at: number | null;
}

export interface LogEntry {
  ts: number;
  level: "info" | "action" | "warning" | "error" | "success";
  message: string;
}

export interface ChatMessage {
  ts: number;
  role: "user" | "agent" | "system";
  text: string;
}

export interface ResultItem {
  id: string;
  fields: Record<string, string>;
  url: string | null;
  verified: boolean;
  tags: string[];
}

export interface ResultCollection {
  id: string;
  title: string;
  source_url: string | null;
  source_title: string | null;
  columns: string[];
  items: ResultItem[];
  recommendation_id: string | null;
  recommendation_reason: string | null;
  dropped_unverified: number;
  extracted_at: number;
}

export interface ApprovalDetail {
  label: string;
  value: string;
}

export interface Approval {
  id: string;
  task_id: string;
  kind: string;
  title: string;
  description: string;
  details: ApprovalDetail[];
  fingerprint: string;
  status: "pending" | "approved" | "rejected" | "superseded" | "consumed" | "expired";
  warnings: string[];
  sources: string[];
  action_summary: string;
  requires_payment_confirmation: boolean;
}

export interface TaskSnapshot {
  id: string;
  instruction: string;
  capability: string | null;
  details: Record<string, string>;
  created_at: number;
  status: TaskStatus;
  goal: string | null;
  category: string | null;
  plan: string[];
  timeline: TimelineStep[];
  logs: LogEntry[];
  chat: ChatMessage[];
  results: ResultCollection[];
  clarification: { question: string } | null;
  approval: Approval | null;
  human_action: { kind: string; message: string; url: string | null } | null;
  current_url: string | null;
  page_title: string | null;
  current_action: string | null;
  thought: string | null;
  step_count: number;
  downloads: { name: string; path: string | null; url: string; error?: string }[];
  error: { message: string } | null;
  summary: string | null;
  verification: { completed: boolean; evidence: string; missing: string | null } | null;
  can_retry: boolean;
  paused: boolean;
  browser_open: boolean;
  model_in_use: string | null;
  model_is_local: boolean;
}

export interface Screenshot {
  image: string;
  url: string | null;
  title: string | null;
  ts: number;
}

export interface Health {
  status: string;
  llm_configured: boolean;
  llm_provider: string;
  llm_model: string;
  headless: boolean;
  vision_mode: string;
}

export interface CapabilityField {
  key: string;
  label: string;
  type: "text" | "textarea" | "date" | "number" | "email" | "url" | "select";
  required: boolean;
  placeholder: string;
  options: string[];
  default: string;
  help: string;
  future_date: boolean;
}

export interface Capability {
  id: string;
  title: string;
  icon: string;
  description: string;
  fields: CapabilityField[];
  sensitive_note: string;
}

export const TERMINAL: TaskStatus[] = ["completed", "failed", "cancelled"];
