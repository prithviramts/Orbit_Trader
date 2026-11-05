import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API serves the built dashboard from the same origin (127.0.0.1:8765), so cookies,
// CSRF and Origin checks work without any CORS configuration.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false },
});
