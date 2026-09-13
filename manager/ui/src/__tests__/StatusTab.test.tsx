import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, StatusDocument } from "../api";
import fixture from "../fixtures/status.sample.json";
import { ProjectRunContext } from "../lib/run";
import StatusTab, { recordLine, remoteLine } from "../tabs/StatusTab";

const document_ = fixture as unknown as StatusDocument;

describe("슬롯의 원격 대조 한 줄", () => {
  const value = { url: "git@example.invalid:app.git", head_branch: "main",
                  head: "e4f2436f55d79f70147d19fd65187f88fd3650be", ahead: 0, behind: 0 };

  it("origin 주소 · 원격 HEAD 브랜치@커밋 · 앞뒤 관계를 엔진이 센 그대로 말한다", () => {
    expect(remoteLine(value, undefined))
      .toBe("git@example.invalid:app.git · main@e4f2436f · 원격 HEAD 와 같음");
    expect(remoteLine({ ...value, behind: 3 }, undefined)).toContain("원격 HEAD 보다 3 커밋 뒤");
    expect(remoteLine({ ...value, ahead: 2 }, undefined)).toContain("원격 HEAD 보다 2 커밋 앞");
    expect(remoteLine({ ...value, ahead: 2, behind: 3 }, undefined))
      .toContain("원격 HEAD 와 갈라짐 (앞 2 · 뒤 3)");
  });

  it("대조가 아직 없거나 실패하면 그 사실을 말한다 — 값을 지어내지 않는다", () => {
    expect(remoteLine(null, undefined)).toBe("원격 확인 전");
    expect(remoteLine(null, { error: { kind: "timeout", message: "git fetch origin main 실패",
                                       stderr: "", rc: 1 } }))
      .toBe("원격 확인 실패 — git fetch origin main 실패");
  });

  it("슬롯 행 아래에 그 슬롯의 대조가 선다", () => {
    const slot = document_.slots[0];
    const remote = { status: { ...document_, slots: document_.slots.map((row) => (
      row.slot === slot.slot ? { ...row, remote: { ...value, behind: 1 } } : row)) } };
    renderTab({ light: { status: document_ }, heavy: undefined, remote });

    expect(screen.getByTestId(`slot-remote-${slot.slot}`)).toHaveTextContent("원격 HEAD 보다 1 커밋 뒤");
    cleanup();
  });
});

/**
 * 실측 sidecar 원문 — 이 PM 홈의 `.project_manager/.local/{regression,livegate}.json`(경로만
 * 일반화). 픽스처 PM 홈에는 두 기록이 없어(null) 그 자리를 이 값으로 태운다.
 */
const REGRESSION = {
  head: "e4f2436f55d79f70147d19fd65187f88fd3650be", status: "pass", rc: 0, scope: "full",
  collected: 12882, floor: 7000, conf_anchor: "/projects/framework",
  ts: "2026-09-04T07:34:00+00:00",
  platforms: [{ name: "windows", command: "/projects/ops/win-qa", status: "pass", rc: 0 }],
};
const LIVEGATE = {
  head: "e4f2436f55d79f70147d19fd65187f88fd3650be", status: "pass", n: 22, rc: 0,
  ts: "2026-09-06T10:42:33+00:00", conf_anchor: "/projects/framework",
  platforms: [{ name: "windows", command: "/projects/ops/win-qa", status: "pass", rc: 0 }],
};

/** 기록 둘이 실린 보드 — 무거운 절이 그대로 나르는 값이다(화면이 만들지 않는다). */
function withRecords(): StatusDocument {
  return { ...document_,
           board: { ...document_.board!, regression: REGRESSION, livegate: LIVEGATE } };
}

/** 행 동작이 사본을 부르는 자리 — 읽기 부분을 그리는 데도 컨텍스트가 있어야 한다(T-0913). */
function renderTab(props: Parameters<typeof StatusTab>[0]) {
  return render(
    <ProjectRunContext.Provider value={{
      bridge: { run: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })) } as unknown as Bridge,
      path: "/projects/demo", status: document_, onChanged: vi.fn(),
    }}>
      <StatusTab {...props} />
    </ProjectRunContext.Provider>);
}

afterEach(cleanup);

describe("현황 탭 읽기 부분", () => {
  it("슬롯 행마다 엔진 값 그대로의 점을 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });

    expect(screen.getByTestId("slot-dot-work/app_1")).toHaveAttribute("data-dot", "blue");
    expect(screen.getByTestId("slot-dot-work/app_2")).toHaveAttribute("data-dot", "orange");
    expect(screen.getByTestId("slot-dot-work/app_3")).toHaveAttribute("data-dot", "gray");
    expect(screen.getByTestId("slot-dot-.")).toHaveAttribute("data-dot", "orange");
    expect(screen.getByTestId("slot-.")).toHaveTextContent("작업 중");
  });

  it("보드 counts·lint 수는 무거운 절이 왔을 때만 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: undefined });
    expect(screen.queryByTestId("board-counts")).toBeNull();
    expect(screen.getByText("선택 시 1회 조회")).toBeInTheDocument();
    cleanup();

    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getByTestId("board-counts")).toHaveTextContent("open 3");
    expect(screen.getByTestId("board-counts")).toHaveTextContent("lint 22");
  });

  it("task 행은 열림 표시·prefix 를 그대로 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getByTestId("task-demo")).toHaveTextContent("닫힘");
    expect(screen.getByTestId("task-demo")).toHaveTextContent("prefix 없음");
    expect(screen.getByTestId("task-live")).toHaveTextContent("작업 중");
  });

  it("행마다 동작 자리가 하나씩 붙고 대여는 표 머리 하나다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getAllByTestId("row-actions").length)
      .toBe(document_.slots.length + document_.tasks.length);
    // 아이들이 둘(work/app_3·work/app_4)이라도 [대여] 는 repo 축 하나다.
    expect(screen.getAllByTestId("alloc")).toHaveLength(1);
    expect(screen.getByTestId("release-work/app_2")).toBeDisabled();   // 작업 중
    expect(screen.getByTestId("release-.")).toBeDisabled();            // 열린 기록
  });

  it("회귀·게이트는 사람이 읽는 한 줄이고 원문은 접혀 있다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: withRecords() } });

    const regression = screen.getByTestId("record-regression");
    expect(regression).toHaveTextContent("회귀 pass · 12882개 수집 · 2026-09-04T07:34:00+00:00");
    expect(regression).not.toHaveAttribute("open");                   // 원문은 열어야 나온다
    // 게이트는 하한이 없는 축이라 수집 수를 `n` 에서 고른다(값 이름이 다르고 판정은 그대로다).
    expect(screen.getByTestId("record-livegate"))
      .toHaveTextContent("게이트 pass · 22개 수집 · 2026-09-06T10:42:33+00:00");
    // 접힌 상세 안에는 엔진 원문이 그대로 있다(요약이 지운 축을 잃지 않는다).
    expect(regression).toHaveTextContent("conf_anchor");
  });

  it("기록이 없으면 그 줄 자체가 없다 — 빈 값을 지어내지 않는다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });

    expect(document_.board!.regression).toBeNull();
    expect(screen.queryByTestId("record-regression")).toBeNull();
    expect(screen.queryByTestId("record-livegate")).toBeNull();
  });
});

describe("기계 기록 한 줄", () => {
  it("판정·수집 수·시각을 그 순서로 잇는다 — 넘긴 하한은 적지 않는다", () => {
    expect(recordLine(REGRESSION)).toBe("pass · 12882개 수집 · 2026-09-04T07:34:00+00:00");
    expect(recordLine(LIVEGATE)).toBe("pass · 22개 수집 · 2026-09-06T10:42:33+00:00");
  });

  it("수집 수가 하한에 못 미치면 그 사실을 적는다", () => {
    expect(recordLine({ ...REGRESSION, collected: 6999 }))
      .toBe("pass · 6999개 수집 (하한 7000 미달) · 2026-09-04T07:34:00+00:00");
    expect(recordLine({ ...REGRESSION, collected: 7000 }))
      .toBe("pass · 7000개 수집 · 2026-09-04T07:34:00+00:00");
  });

  it("없는 축은 자리를 비운다 — 하한 없이 남은 옛 회귀 기록", () => {
    expect(recordLine({ head: "abc", status: "pass", rc: 0, scope: "full",
                        ts: "2026-07-22T03:35:31+00:00" }))
      .toBe("pass · 2026-07-22T03:35:31+00:00");
  });
});
