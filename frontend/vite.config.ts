import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dev server proxies REST and WebSocket traffic to the FastAPI backend, so
// the browser only ever talks to one origin and no API keys reach the client.
const backend = process.env.VITE_BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});
