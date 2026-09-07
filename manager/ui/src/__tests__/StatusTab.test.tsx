import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { StatusDocument } from "../api";
import fixture from "../fixtures/status.sample.json";
import StatusTab from "../tabs/StatusTab";

const document_ = fixture as unknown as StatusDocument;

afterEach(cleanup);

describe("현황 탭 읽기 부분", () => {
  it("슬롯 행마다 엔진 값 그대로의 점을 보인다", () => {
    render(<StatusTab light={{ status: document_ }} heavy={{ status: document_ }} />);

    expect(screen.getByTestId("slot-dot-work/app_1")).toHaveAttribute("data-dot", "blue");
    expect(screen.getByTestId("slot-dot-work/app_2")).toHaveAttribute("data-dot", "orange");
    expect(screen.getByTestId("slot-dot-work/app_3")).toHaveAttribute("data-dot", "gray");
    expect(screen.getByTestId("slot-dot-.")).toHaveAttribute("data-dot", "red");
    expect(screen.getByTestId("slot-.")).toHaveTextContent("다음 bootstrap 이 회수");
  });

  it("보드 counts·lint 수는 무거운 절이 왔을 때만 보인다", () => {
    render(<StatusTab light={{ status: document_ }} heavy={undefined} />);
    expect(screen.queryByTestId("board-counts")).toBeNull();
    expect(screen.getByText("선택 시 1회 조회")).toBeInTheDocument();
    cleanup();

    render(<StatusTab light={{ status: document_ }} heavy={{ status: document_ }} />);
    expect(screen.getByTestId("board-counts")).toHaveTextContent("open 3");
    expect(screen.getByTestId("board-counts")).toHaveTextContent("lint 22");
  });

  it("task 행은 pid·prefix 를 그대로 보인다", () => {
    render(<StatusTab light={{ status: document_ }} heavy={{ status: document_ }} />);
    expect(screen.getByTestId("task-demo")).toHaveTextContent("pid 없음");
    expect(screen.getByTestId("task-demo")).toHaveTextContent("prefix 없음");
    expect(screen.getByTestId("task-live")).toHaveTextContent("작업 중");
  });

  it("행 동작 자리(T-0913)는 표에 자리만 잡는다", () => {
    render(<StatusTab light={{ status: document_ }} heavy={{ status: document_ }} />);
    expect(screen.getAllByTestId("row-actions").length)
      .toBe(document_.slots.length + document_.tasks.length);
  });
});
