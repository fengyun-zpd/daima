import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时把 /api 与 /internal 代理到本地 API（默认 8099），
// 生产由 nginx 反向代理（docker/web/nginx.conf），前端始终使用相对路径。
const apiTarget = process.env.CODEPILOT_API_URL ?? "http://127.0.0.1:8099";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/internal": { target: apiTarget, changeOrigin: true },
      "/healthz": { target: apiTarget, changeOrigin: true },
      "/readyz": { target: apiTarget, changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
