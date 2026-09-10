import { describe, expect, it } from "vitest";
import fixture from "../fixtures/status.sample.json";
import { projectDot, rowDot, slotSummary, updateAvailable } from "../lib/status";

const slots = fixture.slots;
const tasks = fixture.tasks;
const slotOf = (name: string) => slots.find((slot) => slot.slot === name)!;

describe("상태 점 — 판정 순서(첫 일치가 이긴다)", () => {
  it("readonly 슬롯은 파랑이다 (leased·세션 없음·alive false 와 동시에 참이라도)", () => {
    const readonly = slotOf("work/app_1");
    expect([readonly.state, readonly.session, readonly.alive, readonly.role])
      .toEqual(["leased", null, false, "readonly"]);
    expect(rowDot(readonly)).toEqual({ dot: "blue", label: "readonly", actions: "refresh" });
  });

  it("변경 동작 가부가 같은 한 줄에서 나온다 — 열린 기록만 닫힌다", () => {
    expect(rowDot(slotOf("work/app_3")).actions).toBe("change");     // 아이들
    expect(rowDot(slotOf("work/app_2")).actions).toBe("disabled");   // 작업 중
    expect(rowDot(slotOf(".")).actions).toBe("disabled");            // 열린 기록
    expect(rowDot(slotOf("work/app_1")).actions).toBe("refresh");    // readonly
    expect(rowDot(tasks.find((task) => task.name === "demo")!).actions).toBe("change");
  });

  it("idle 슬롯은 회색 · 열린 기록은 주황 (빨강 축은 없다)", () => {
    expect(rowDot(slotOf("work/app_3")).dot).toBe("gray");
    expect(rowDot(slotOf("work/app_2")).dot).toBe("orange");
    expect(rowDot(slotOf(".")).dot).toBe("orange");
  });

  it("task 행도 같은 규칙이다 — 닫힘 회색 · 열림 주황", () => {
    expect(rowDot(tasks.find((task) => task.name === "demo")!).dot).toBe("gray");
    expect(rowDot(tasks.find((task) => task.name === "live")!).dot).toBe("orange");
  });

  it("규칙에 없는 행은 조용히 회색이 되지 않고 터진다", () => {
    expect(() => rowDot({})).toThrow(/상태 점 규칙/);
  });
});

describe("프로젝트 목록 점", () => {
  it("작업 중 행이 있으면 주황", () => {
    expect(projectDot(slots)).toBe("orange");
  });

  it("열린 기록이 없으면 회색 · readonly 는 목록 점 축이 아니다", () => {
    expect(projectDot([slotOf("."), slotOf("work/app_1")])).toBe("orange");
    expect(projectDot([slotOf("work/app_1"), slotOf("work/app_3")])).toBe("gray");
  });
});

describe("버전 비교", () => {
  it("숫자 셋으로 비교한다 — v1.7.9 는 v1.7.13 보다 낮다", () => {
    expect(updateAvailable("v1.7.9", "v1.7.13")).toBe(true);
    expect(updateAvailable("v1.7.13", "v1.7.9")).toBe(false);
    expect(updateAvailable("v1.7.13", "v1.7.13")).toBe(false);
  });

  it("버전을 모르면 갱신 표시를 하지 않는다", () => {
    expect(updateAvailable(undefined, "v2.0.0")).toBe(false);
    expect(updateAvailable("v1.7.13", null)).toBe(false);
  });
});

describe("슬롯 요약", () => {
  it("상태별 개수를 촘촘한 한 줄로 낸다", () => {
    expect(slotSummary(slots)).toBe("2 작업 중 · 1 readonly · 2 아이들 · 1 닫힘");
  });
});
