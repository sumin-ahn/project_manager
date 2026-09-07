import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 화면은 정적 산출물 하나다 — pywebview 가 `dist/index.html` 을 파일로 연다(dev 서버 없음).
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
