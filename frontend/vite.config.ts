import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 開発中（npm run dev）は、API を動いている Farm Radar（http://localhost:18000）に転送する
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { proxy: { "/api": "http://localhost:18000" } },
  build: { chunkSizeWarningLimit: 900 },   // グラフの部品（Recharts）が大きいため
});
