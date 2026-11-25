import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Capability, CapabilityField } from "../types";

interface Props {
  disabled: boolean;
  onStart: (capability: Capability, details: Record<string, string>) => Promise<void>;
}

function todayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function defaults(cap: Capability): Record<string, string> {
  return Object.fromEntries(cap.fields.map((f) => [f.key, f.default ?? ""]));
}

/** Home screen: pick what NOVA should do, then give it the details it needs. */
export function Launcher({ disabled, onStart }: Props) {
  const [caps, setCaps] = useState<Capability[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Capability | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    api
      .capabilities()
      .then(setCaps)
      .catch((e) => setLoadError((e as Error).message));
  }, []);

  const pick = (cap: Capability) => {
    setSelected(cap);
    setValues(defaults(cap));
  };

  const missing = useMemo(
    () => (selected ? selected.fields.filter((f) => f.required && !values[f.key]?.trim()).map((f) => f.label) : []),
    [selected, values],
  );

  if (loadError) return <section className="panel launcher"><p className="muted">Could not load tasks: {loadError}</p></section>;
  if (!caps) return <section className="panel launcher"><p className="muted">Loading…</p></section>;

  if (!selected) {
    return (
      <section className="panel launcher" aria-label="Choose a task">
        <div className="panel-head">
          <h2>What should NOVA do?</h2>
        </div>
        <p className="muted small launcher-intro">
          Pick a task and tell NOVA the details. It opens a real browser only once it knows exactly what you want.
        </p>
        <div className="cap-grid">
          {caps.map((c) => (
            <button key={c.id} className="cap-card" onClick={() => pick(c)}>
              <span className="cap-icon" aria-hidden>
                {c.icon}
              </span>
              <span className="cap-title">{c.title}</span>
              <span className="cap-desc muted small">{c.description}</span>
            </button>
          ))}
        </div>
        <p className="muted small launcher-intro">Something else? Describe it in the chat and NOVA will ask for anything it needs.</p>
      </section>
    );
  }

  const set = (key: string, value: string) => setValues((v) => ({ ...v, [key]: value }));
  const submit = async () => {
    if (missing.length || disabled || submitting) return;
    setSubmitting(true);
    try {
      const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v.trim() !== ""));
      await onStart(selected, filled);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section className="panel launcher" aria-label={selected.title}>
      <div className="panel-head">
        <h2>
          <span aria-hidden>{selected.icon}</span> {selected.title}
        </h2>
        <button className="btn ghost small" onClick={() => setSelected(null)}>
          ← All tasks
        </button>
      </div>
      <form
        className="cap-form"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {selected.fields.map((f) => (
          <FieldInput key={f.key} field={f} value={values[f.key] ?? ""} onChange={(v) => set(f.key, v)} />
        ))}
        {selected.sensitive_note && <p className="cap-note small">🔒 {selected.sensitive_note}</p>}
        <div className="cap-actions">
          {missing.length > 0 && <span className="muted small">Still needed: {missing.join(", ")}</span>}
          <button className="btn primary" type="submit" disabled={disabled || submitting || missing.length > 0}>
            {submitting ? "Starting…" : "Start task"}
          </button>
        </div>
      </form>
    </section>
  );
}

function FieldInput({ field: f, value, onChange }: { field: CapabilityField; value: string; onChange: (v: string) => void }) {
  const id = `f-${f.key}`;
  const label = (
    <label htmlFor={id}>
      {f.label}
      {f.required && <span className="req" aria-label="required"> *</span>}
    </label>
  );
  let input;
  if (f.type === "select") {
    input = (
      <select id={id} value={value} onChange={(e) => onChange(e.target.value)} required={f.required}>
        {f.options.map((o) => (
          <option key={o} value={o}>
            {o || "Any"}
          </option>
        ))}
      </select>
    );
  } else if (f.type === "textarea") {
    input = (
      <textarea id={id} value={value} rows={3} placeholder={f.placeholder} required={f.required} onChange={(e) => onChange(e.target.value)} />
    );
  } else {
    input = (
      <input
        id={id}
        type={f.type === "number" ? "number" : f.type === "date" ? "date" : f.type === "email" ? "email" : "text"}
        inputMode={f.type === "url" ? "url" : undefined}
        min={f.type === "date" && f.future_date ? todayISO() : f.type === "number" ? 0 : undefined}
        value={value}
        placeholder={f.placeholder}
        required={f.required}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <div className={`cap-field ${f.type === "textarea" ? "wide" : ""}`}>
      {label}
      {input}
      {f.help && <span className="muted small">{f.help}</span>}
    </div>
  );
}
