import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, CommandResult, ErrorResponse, SlotRow, StatusDocument, TaskRow } from "../api";
import fixture from "../fixtures/status.sample.json";
import StatusRowActions from "../components/StatusRowActions";
import { ProjectRunContext } from "../lib/run";

/**
 * 현황 탭 행 동작 — 아이들 규칙(비활성 + 사유)과 확인 대화의 값 결속.
 *
 * 행은 실 엔진 산출(`status --json`) 픽스처의 네 형태다: 아이들 · 작업 중 · 회수 대기 · readonly.
 */
const DEMO = "/projects/demo";
const document_ = fixture as unknown as StatusDocument;
const slotOf = (name: string) => document_.slots.find((slot) => slot.slot === name)! as SlotRow;
const taskOf = (name: string) => document_.tasks.find((task) => task.name === name)! as TaskRow;

function renderRow(row: SlotRow | TaskRow, result: CommandResult | ErrorResponse =
  { rc: 0, stdout: "✓ 됐다", stderr: "" }) {
  const run = vi.fn(async (_path: string, _argv: string[]) => result);
  const onChanged = vi.fn();
  render(
    <ProjectRunContext.Provider value={{
      bridge: { run } as unknown as Bridge, path: DEMO, status: document_, onChanged,
    }}>
      <StatusRowActions row={row} />
    </ProjectRunContext.Provider>);
  return { run, onChanged };
}

afterEach(cleanup);

describe("아이들 규칙", () => {
  it("아이들 슬롯만 변경 동작이 열린다 — task 를 고른 뒤 alloc 한 줄", async () => {
    const { run, onChanged } = renderRow(slotOf("work/app_3"));

    expect(screen.getByTestId("alloc-work/app_3")).toBeDisabled();   // task 를 고르기 전
    fireEvent.change(screen.getByLabelText("work/app_3 대여 task"), { target: { value: "demo" } });
    fireEvent.click(screen.getByTestId("alloc-work/app_3"));

    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["alloc", "app", "--task", "demo"]));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("작업 중 행은 비활성이고 사유가 세션 이름이다", () => {
    renderRow(slotOf("work/app_2"));

    expect(screen.getByTestId("release-work/app_2")).toBeDisabled();
    expect(screen.getByTestId("row-reason")).toHaveTextContent("세션 proj1_2 이 잡고 있음");
  });

  it("회수 대기 행도 비활성이다 — 회수는 다음 bootstrap 이 한다", () => {
    renderRow(slotOf("."));

    expect(screen.getByTestId("release-.")).toBeDisabled();
    expect(screen.getByTestId("row-reason")).toHaveTextContent("다음 bootstrap 이 회수");
  });

  it("readonly 행은 새로고침만 있고 변경 동작이 없다", () => {
    const { run, onChanged } = renderRow(slotOf("work/app_1"));

    expect(screen.queryByTestId("alloc-work/app_1")).toBeNull();
    expect(screen.queryByTestId("release-work/app_1")).toBeNull();
    fireEvent.click(screen.getByTestId("refresh-work/app_1"));
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(run).not.toHaveBeenCalled();
  });

  it("살아 있는 세션이 잡은 task 행은 prefix·종료가 둘 다 비활성이다", () => {
    renderRow(taskOf("live"));

    expect(screen.getByTestId("prefix-live")).toBeDisabled();
    expect(screen.getByTestId("end-live")).toBeDisabled();
    expect(screen.getByTestId("row-reason")).toHaveTextContent("세션 live 이 잡고 있음");
  });
});

describe("확인 대화와 승인 토큰", () => {
  it("prefix 는 대화를 승인해야 나가고 --user-ack 값이 그 값에 결속된다", async () => {
    const { run } = renderRow(taskOf("demo"));

    fireEvent.change(screen.getByLabelText("demo prefix"), { target: { value: "dm" } });
    fireEvent.click(screen.getByTestId("prefix-demo"));
    expect(run).not.toHaveBeenCalled();                       // 대화만 떴다
    expect(screen.getByTestId("confirm-value")).toHaveTextContent("--user-ack dm");

    fireEvent.click(screen.getByTestId("confirm-approve"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      DEMO, ["task", "prefix", "demo", "dm", "--user-ack", "dm"]));
  });

  it("취소하면 서브프로세스 축으로 아무것도 나가지 않는다", () => {
    const { run } = renderRow(taskOf("demo"));

    fireEvent.change(screen.getByLabelText("demo prefix"), { target: { value: "dm" } });
    fireEvent.click(screen.getByTestId("prefix-demo"));
    fireEvent.click(screen.getByTestId("confirm-cancel"));

    expect(run).not.toHaveBeenCalled();
    expect(screen.queryByTestId("confirm-dialog")).toBeNull();
  });

  it("대화가 없는 동작에는 --user-ack 이 붙지 않는다", async () => {
    const { run } = renderRow(taskOf("demo"));

    fireEvent.click(screen.getByTestId("end-demo"));

    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["task", "end", "demo"]));
    expect(run.mock.calls[0][1]).not.toContain("--user-ack");
  });
});

describe("엔진 산출", () => {
  it("rc≠0 이면 stderr 원문이 빨간 박스로 그대로 보인다", async () => {
    const refusal = "[중단] 슬롯 'work/app_3' 은 task 'other' 소유가 아니다 — 반납 거부";
    const { run } = renderRow(slotOf("work/app_3"), { rc: 1, stdout: "", stderr: refusal });

    fireEvent.change(screen.getByLabelText("work/app_3 대여 task"), { target: { value: "demo" } });
    fireEvent.click(screen.getByTestId("alloc-work/app_3"));

    await screen.findByTestId("command-failed");
    expect(screen.getByTestId("command-stderr")).toHaveTextContent(refusal);
    expect(run).toHaveBeenCalledTimes(1);
  });

  it("브리지가 거절한 자리는 오류 문구가 그대로 온다", async () => {
    const rejected: ErrorResponse = {
      error: { kind: "forbidden-flag", message: "--force 는 화면에 없다", stderr: "", rc: 2 },
    };
    renderRow(taskOf("demo"), rejected);

    fireEvent.click(screen.getByTestId("end-demo"));

    expect(await screen.findByTestId("command-error")).toHaveTextContent("--force 는 화면에 없다");
  });
});
