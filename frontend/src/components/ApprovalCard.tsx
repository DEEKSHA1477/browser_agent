import { useEffect, useState } from "react";
import { api } from "../api";
import type { Approval } from "../types";

export function ApprovalCard({ approval }: { approval: Approval }) {
  const [paymentOk, setPaymentOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPaymentOk(false);
    setError(null);
  }, [approval.id, approval.fingerprint]);

  if (approval.status !== "pending") return null;

  const decide = async (decision: "approve" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      await api.decide(approval.id, decision, approval.fingerprint, paymentOk);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className={`panel approval ${approval.kind === "payment" ? "payment" : ""}`} role="alertdialog" aria-labelledby="approval-title">
      <div className="approval-title" id="approval-title">
        <span aria-hidden>⚠</span> {approval.title}
      </div>
      <p className="approval-desc">{approval.description}</p>
      <dl className="approval-details">
        {approval.details.map((d, i) => (
          <div key={i}>
            <dt>{d.label}</dt>
            <dd>{d.value}</dd>
          </div>
        ))}
      </dl>
      {approval.warnings.length > 0 && (
        <ul className="approval-warnings">
          {approval.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}
      <p className="muted small">
        Flagged by {approval.sources.map((s) => (s === "policy" ? "NOVA's safety policy" : "the agent")).join(" and ")}. The agent is paused and
        will re-check these details on the live page before acting.
      </p>
      {approval.requires_payment_confirmation && (
        <label className="payment-confirm">
          <input type="checkbox" checked={paymentOk} onChange={(e) => setPaymentOk(e.target.checked)} />I explicitly confirm this payment.
        </label>
      )}
      {error && <div className="error-text">{error}</div>}
      <div className="approval-actions">
        <button className="btn danger-outline" onClick={() => decide("reject")} disabled={busy}>
          Reject
        </button>
        <button
          className="btn approve"
          onClick={() => decide("approve")}
          disabled={busy || (approval.requires_payment_confirmation && !paymentOk)}
        >
          Approve
        </button>
      </div>
    </section>
  );
}
