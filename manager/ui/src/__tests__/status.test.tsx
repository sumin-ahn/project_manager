import { describe, expect, it } from "vitest";
import fixture from "../fixtures/status.sample.json";
import { projectDot, rowDot, slotSummary, updateAvailable } from "../lib/status";

const slots = fixture.slots;
const tasks = fixture.tasks;
const slotOf = (name: string) => slots.find((slot) => slot.slot === name)!;

describe("상태 점 — 판정 순서(첫 일치가 이긴다)", () => {
  it("readonly 슬롯은 파랑이다 (leased·pid 0·alive false 와 동시에 참이라도)", () => {
    const readonly = slotOf("work/app_1");
    expect([readonly.state, readonly.pid, readonly.alive, readonly.role])
      .toEqual(["leased", 0, false, "readonly"]);
    expect(rowDot(readonly)).toEqual({ dot: "blue", label: "readonly", actions: "refresh" });
  });

  it("변경 동작 가부가 같은 한 줄에서 나온다 — 아이들만 change", () => {
    expect(rowDot(slotOf("work/app_3")).actions).toBe("change");     // 아이들
    expect(rowDot(slotOf("work/app_2")).actions).toBe("disabled");   // 작업 중
    expect(rowDot(slotOf(".")).actions).toBe("disabled");            // 회수 대기
    expect(rowDot(slotOf("work/app_1")).actions).toBe("refresh");    // readonly
    expect(rowDot(tasks.find((task) => task.name === "demo")!).actions).toBe("change");
  });

  it("idle 슬롯은 회색 · leased+alive 는 주황 · leased+죽은 pid 는 빨강", () => {
    expect(rowDot(slotOf("work/app_3")).dot).toBe("gray");
    expect(rowDot(slotOf("work/app_2")).dot).toBe("orange");
    expect(rowDot(slotOf(".")).dot).toBe("red");
  });

  it("task 행도 같은 규칙이다 — pid 0 회색 · alive 주황", () => {
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

  it("회수 대기만 있으면 빨강 · readonly 는 회수 축이 아니다", () => {
    expect(projectDot([slotOf("."), slotOf("work/app_1")])).toBe("red");
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
    expect(slotSummary(slots)).toBe("1 회수 대기 · 1 readonly · 1 작업 중 · 2 아이들");
  });
});
