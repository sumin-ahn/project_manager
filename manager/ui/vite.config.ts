import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 화면은 정적 산출물 하나다 — 같은 프로세스 서버가 `dist` 를 내고 창도 그 손님이다(dev 서버 없음).
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.tsx"],
  },
});
