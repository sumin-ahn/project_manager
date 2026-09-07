import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, StatusDocument } from "../api";
import fixture from "../fixtures/status.sample.json";
import { ProjectRunContext } from "../lib/run";
import StatusTab from "../tabs/StatusTab";

const document_ = fixture as unknown as StatusDocument;

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
    expect(screen.getByTestId("slot-dot-.")).toHaveAttribute("data-dot", "red");
    expect(screen.getByTestId("slot-.")).toHaveTextContent("다음 bootstrap 이 회수");
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

  it("task 행은 pid·prefix 를 그대로 보인다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getByTestId("task-demo")).toHaveTextContent("pid 없음");
    expect(screen.getByTestId("task-demo")).toHaveTextContent("prefix 없음");
    expect(screen.getByTestId("task-live")).toHaveTextContent("작업 중");
  });

  it("행마다 동작 자리가 하나씩 붙는다", () => {
    renderTab({ light: { status: document_ }, heavy: { status: document_ } });
    expect(screen.getAllByTestId("row-actions").length)
      .toBe(document_.slots.length + document_.tasks.length);
    expect(screen.getByTestId("alloc-work/app_3")).toBeInTheDocument();
    expect(screen.getByTestId("release-work/app_2")).toBeDisabled();
  });
});
