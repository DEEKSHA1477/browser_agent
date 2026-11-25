import type { Approval, Capability, Health, TaskSnapshot } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError(0, "Cannot reach the NOVA backend. Is it running on port 8000?");
  }
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = body.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => request<Health>("/api/health"),
  capabilities: () => request<Capability[]>("/api/capabilities"),
  createTask: (instruction: string) => post<{ task_id: string; snapshot: TaskSnapshot }>("/api/tasks", { instruction }),
  startCapability: (capability: string, details: Record<string, string>) =>
    post<{ task_id: string; snapshot: TaskSnapshot }>("/api/tasks", { capability, details }),
  select: (id: string, itemId: string) =>
    post<{ task_id: string; snapshot: TaskSnapshot }>(`/api/tasks/${id}/select`, { item_id: itemId }),
  getTask: (id: string) => request<TaskSnapshot>(`/api/tasks/${id}`),
  clarify: (id: string, answer: string) => post<TaskSnapshot>(`/api/tasks/${id}/clarify`, { answer }),
  pause: (id: string) => post<TaskSnapshot>(`/api/tasks/${id}/pause`),
  resume: (id: string) => post<TaskSnapshot>(`/api/tasks/${id}/resume`),
  stop: (id: string) => post<TaskSnapshot>(`/api/tasks/${id}/stop`),
  retry: (id: string) => post<TaskSnapshot>(`/api/tasks/${id}/retry`),
  humanDone: (id: string) => post<TaskSnapshot>(`/api/tasks/${id}/human-done`),
  closeBrowser: (id: string) => post<TaskSnapshot>(`/api/tasks/${id}/close-browser`),
  decide: (approvalId: string, decision: "approve" | "reject", fingerprint: string, paymentConfirmation = false) =>
    post<Approval>(`/api/approvals/${approvalId}/decision`, {
      decision,
      fingerprint,
      payment_confirmation: paymentConfirmation,
    }),
};
