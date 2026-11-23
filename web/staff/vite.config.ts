import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The UI calls /api/*; Vite proxies it to the gateway, so no CORS is needed.
const api = process.env.API_URL ?? "http://localhost:8200";

export default defineConfig({
  plugins: [react()],
  server: {
    port: Number(process.env.WEB_PORT ?? 5180),
    strictPort: true,
    host: "127.0.0.1",
    proxy: { "/api": { target: api, changeOrigin: true, rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  preview: { port: Number(process.env.WEB_PORT ?? 5180) },
});
