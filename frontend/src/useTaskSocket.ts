import { useEffect, useRef, useState } from "react";
import type { Screenshot, TaskSnapshot } from "./types";

export type SocketState = "idle" | "connecting" | "open" | "reconnecting";

/**
 * Subscribes to a task's live channel. The server sends a full snapshot on
 * every (re)connect, so a dropped connection recovers without losing state.
 */
export function useTaskSocket(taskId: string | null) {
  const [snapshot, setSnapshot] = useState<TaskSnapshot | null>(null);
  const [screenshot, setScreenshot] = useState<Screenshot | null>(null);
  const [state, setState] = useState<SocketState>("idle");
  const retryRef = useRef(0);

  useEffect(() => {
    setScreenshot(null);
    if (!taskId) {
      setState("idle");
      return;
    }
    let ws: WebSocket | null = null;
    let closed = false;
    let pingTimer: number | undefined;
    let reconnectTimer: number | undefined;

    const connect = () => {
      setState(retryRef.current === 0 ? "connecting" : "reconnecting");
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${window.location.host}/ws/tasks/${taskId}`);
      ws.onopen = () => {
        retryRef.current = 0;
        setState("open");
        pingTimer = window.setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 25000);
      };
      ws.onmessage = (event) => {
        if (event.data === "pong") return;
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "snapshot") setSnapshot(msg.data);
          else if (msg.type === "screenshot") setScreenshot(msg.data);
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = (event) => {
        window.clearInterval(pingTimer);
        if (closed || event.code === 4404) return;
        retryRef.current += 1;
        setState("reconnecting");
        const delay = Math.min(10000, 500 * 2 ** Math.min(retryRef.current, 5));
        reconnectTimer = window.setTimeout(connect, delay);
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearInterval(pingTimer);
      window.clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [taskId]);

  return { snapshot, setSnapshot, screenshot, state };
}
