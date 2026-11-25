import { useEffect, useRef, useState } from "react";
import type { TaskSnapshot } from "../types";

const EXAMPLES = [
  "Open YouTube and play Arijit Singh songs",
  "Find the cheapest flights from Bengaluru to Delhi this Friday",
  "Search for laptops under ₹60,000 on Amazon and compare their prices",
  "Search for Python developer jobs for freshers in Bengaluru",
  "Find the latest AI research papers on arXiv",
];

interface Props {
  task: TaskSnapshot | null;
  busy: boolean;
  canStartNew: boolean;
  llmReady: boolean;
  onSubmit: (text: string) => Promise<void>;
}

export function ChatPanel({ task, busy, canStartNew, llmReady, onSubmit }: Props) {
  const [text, setText] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const awaitingAnswer = task?.status === "awaiting_clarification";
  const disabled = busy || (!awaitingAnswer && !canStartNew) || (!llmReady && !awaitingAnswer);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" });
  }, [task?.chat.length]);

  const submit = async (value: string) => {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    await onSubmit(trimmed);
    setText("");
  };

  return (
    <section className="panel chat" aria-label="Chat">
      <div className="panel-head">
        <h2>Chat</h2>
      </div>
      <div className="chat-list" ref={listRef}>
        {!task && (
          <div className="chat-empty">
            <p className="muted">Tell NOVA what to do in the browser. It works on its own and asks before anything sensitive.</p>
            <div className="chips">
              {EXAMPLES.map((ex) => (
                <button key={ex} className="chip" onClick={() => submit(ex)} disabled={disabled}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}
        {task?.chat.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            {m.text}
          </div>
        ))}
        {task?.summary && task.status !== "awaiting_clarification" && task.verification && (
          <div className={`verify-note ${task.verification.completed ? "ok" : "warn"}`}>
            <strong>{task.verification.completed ? "Verified" : "Not verified"}:</strong> {task.verification.evidence}
          </div>
        )}
      </div>
      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit(text);
        }}
      >
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit(text);
            }
          }}
          rows={2}
          maxLength={1000}
          placeholder={
            awaitingAnswer
              ? "Type your answer…"
              : !llmReady
                ? "Configure an LLM API key in .env to start"
                : canStartNew
                  ? "What should I do? e.g. “Open YouTube and play lo-fi music”"
                  : "Agent is working… stop it to start a new task"
          }
          aria-label="Instruction"
          disabled={disabled}
        />
        <button className="btn primary" type="submit" disabled={disabled || !text.trim()}>
          {awaitingAnswer ? "Answer" : "Send"}
        </button>
      </form>
    </section>
  );
}
