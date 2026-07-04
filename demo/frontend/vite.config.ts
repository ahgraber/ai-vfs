import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The build lands in the backend package's `static/` dir so a single FastAPI
// process serves both the API and the SPA (one origin, no CORS). In dev, Vite
// serves the SPA on :5173 and proxies /api to the backend on :7171.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../backend/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:7171",
        changeOrigin: true,
      },
    },
  },
})
