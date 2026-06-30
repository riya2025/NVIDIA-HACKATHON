import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Some filesystems (e.g. under ~/Downloads on macOS, network/synced folders)
    // don't reliably emit fsevents, so HMR can miss edits. Polling guarantees
    // changes are picked up.
    watch: { usePolling: true, interval: 200 },
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
      "/health": "http://127.0.0.1:8000",
    },
  },
});
