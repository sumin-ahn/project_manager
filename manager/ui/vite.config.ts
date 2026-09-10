import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// 화면은 정적 산출물 하나다 — 같은 프로세스 서버가 `dist` 를 내고 창도 그 손님이다(dev 서버 없음).
export default defineConfig({
  plugins: [react()],
  base: "./",
  // 아이콘 원본 자리는 `manager/assets` 하나다 — 화면이 자기 사본을 들지 않는다.
  publicDir: "../assets",
  build: { outDir: "dist", emptyOutDir: true },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["src/**/*.test.tsx"],
  },
});
