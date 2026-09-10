import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, CommandResult, ErrorResponse, SlotRow, StatusDocument, TaskRow } from "../api";
import fixture from "../fixtures/status.sample.json";
import StatusRowActions, { SlotTableAllocate } from "../components/StatusRowActions";
import { ProjectRunContext } from "../lib/run";

/**
 * 현황 탭 동작 — 아이들 규칙(비활성 + 사유)·대여와 반납의 자리·확인 대화의 값 결속.
 *
 * 행은 실 엔진 산출(`status --json`) 픽스처의 세 형태다: 아이들 · 작업 중(열린 기록) · readonly.
 * [대여]는 행이 아니라 슬롯 표 머리에 있다 — `alloc` 이 repo 축이라 어느 idle 슬롯이 나갈지는
 * 엔진이 고르고(최소 번호), 아이들이 둘 이상이면 누른 행과 대여되는 행이 달라진다.
 */
const DEMO = "/projects/demo";
const document_ = fixture as unknown as StatusDocument;
const slotOf = (name: string) => document_.slots.find((slot) => slot.slot === name)! as SlotRow;
const taskOf = (name: string) => document_.tasks.find((task) => task.name === name)! as TaskRow;

function renderInProject(children: ReactNode, result: CommandResult | ErrorResponse =
  { rc: 0, stdout: "✓ 됐다", stderr: "" }) {
  const run = vi.fn(async (_path: string, _argv: string[]) => result);
  const onChanged = vi.fn();
  render(
    <ProjectRunContext.Provider value={{
      bridge: { run } as unknown as Bridge, path: DEMO, status: document_, onChanged,
    }}>
      {children}
    </ProjectRunContext.Provider>);
  return { run, onChanged };
}

function renderRow(row: SlotRow | TaskRow, result?: CommandResult | ErrorResponse) {
  return renderInProject(<StatusRowActions row={row} />, result);
}

afterEach(cleanup);

describe("대여는 repo 단위 한 자리다", () => {
  it("표 머리에서 repo·task 를 고른 뒤 alloc 한 줄을 부른다", async () => {
    const { run, onChanged } = renderInProject(<SlotTableAllocate />);

    expect(screen.getByTestId("alloc")).toBeDisabled();          // repo·task 를 고르기 전
    fireEvent.change(screen.getByLabelText("대여할 repo"), { target: { value: "app" } });
    expect(screen.getByTestId("alloc")).toBeDisabled();          // task 는 아직이다
    fireEvent.change(screen.getByLabelText("대여 task"), { target: { value: "demo" } });
    fireEvent.click(screen.getByTestId("alloc"));

    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["alloc", "app", "--task", "demo"]));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("고를 수 있는 repo 는 그 사본의 registry 행 전부다", () => {
    renderInProject(<SlotTableAllocate />);

    const options = [...screen.getByLabelText("대여할 repo").querySelectorAll("option")]
      .map((option) => option.value);
    expect(options).toEqual(["", ...document_.repos.map((row) => row.repo)]);
  });

  it("아이들 행에는 대여 버튼이 없다 — 누른 행과 대여되는 행이 갈리지 않는다", () => {
    const { run, onChanged } = renderRow(slotOf("work/app_3"));

    expect(screen.queryByTestId("alloc-work/app_3")).toBeNull();
    expect(screen.queryByTestId("release-work/app_3")).toBeNull();
    fireEvent.click(screen.getByTestId("refresh-work/app_3"));
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(run).not.toHaveBeenCalled();
  });
});

describe("아이들 규칙", () => {
  it("열린 기록 행은 반납이 비활성이고 사유가 세션 이름이다 — 닫는 것은 사용자 행위다", async () => {
    const { run } = renderRow(slotOf("."));

    expect(screen.getByTestId("release-.")).toBeDisabled();
    expect(screen.getByTestId("row-reason")).toHaveTextContent("세션 proj1_1 이 잡고 있음");
    fireEvent.click(screen.getByTestId("release-."));

    expect(run).not.toHaveBeenCalled();
  });

  it("작업 중 행은 비활성이고 사유가 세션 이름이다", () => {
    renderRow(slotOf("work/app_2"));

    expect(screen.getByTestId("release-work/app_2")).toBeDisabled();
    expect(screen.queryByLabelText("work/app_2 반납 task")).toBeNull();
    expect(screen.getByTestId("row-reason")).toHaveTextContent("세션 proj1_2 이 잡고 있음");
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
    const refusal = "[중단] task 'demo' 에 claimed 인 티켓이 남아있다 — 종료 거부";
    const { run } = renderRow(taskOf("demo"), { rc: 1, stdout: "", stderr: refusal });

    fireEvent.click(screen.getByTestId("end-demo"));

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
