import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` serves the UI on 5173 and hands /api and /auth to the backend on 8080.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
  server: {
    proxy: {
      "/api": "http://localhost:8080", // DevSkim: ignore DS162092 - the dev server's proxy to the local backend
      "/auth": "http://localhost:8080", // DevSkim: ignore DS162092
    },
  },
});
