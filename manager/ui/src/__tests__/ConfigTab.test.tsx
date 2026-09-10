import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, CommandResult, ErrorResponse, StatusDocument } from "../api";
import fixture from "../fixtures/status.sample.json";
import ConfigTab from "../tabs/ConfigTab";
import { ProjectRunContext } from "../lib/run";

/** 구성 탭 — 명령 인자 형식(위치 인자·플래그)과 확인 대화, 그리고 아이들 규칙의 슬롯 제거. */
const DEMO = "/projects/demo";
const document_ = fixture as unknown as StatusDocument;

function renderTab(result: CommandResult | ErrorResponse = { rc: 0, stdout: "✓ 됐다", stderr: "" }) {
  const run = vi.fn(async (_path: string, _argv: string[]) => result);
  const onChanged = vi.fn();
  render(
    <ProjectRunContext.Provider value={{
      bridge: { run } as unknown as Bridge, path: DEMO, status: document_, onChanged,
    }}>
      <ConfigTab />
    </ProjectRunContext.Provider>);
  return { run, onChanged };
}

afterEach(cleanup);

describe("repo 절", () => {
  it("이름은 위치 인자이고 URL 은 --git 플래그다", async () => {
    const { run, onChanged } = renderTab();

    fireEvent.change(screen.getByLabelText("repo 이름"), { target: { value: "demo" } });
    fireEvent.change(screen.getByLabelText("repo --git"), { target: { value: "/tmp/demo.git" } });
    fireEvent.change(screen.getByLabelText("repo --protected"), { target: { value: "main,dev" } });
    fireEvent.click(screen.getByTestId("repo-add"));

    await waitFor(() => expect(run).toHaveBeenCalledWith(
      DEMO, ["repo", "add", "demo", "--git", "/tmp/demo.git", "--protected", "main,dev"]));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("보호목록은 값 있으면 설정 · 값 없으면 조회 두 자리다", async () => {
    const { run } = renderTab();

    fireEvent.change(screen.getByLabelText("app 보호 브랜치"), { target: { value: "main,release" } });
    fireEvent.click(screen.getByTestId("protected-set-app"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      DEMO, ["repo", "protected", "app", "main,release"]));

    fireEvent.click(screen.getByTestId("protected-show-app"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["repo", "protected", "app"]));
  });

  it("repo 목록은 사본의 표를 그대로 부른다", async () => {
    const { run } = renderTab();
    fireEvent.click(screen.getByTestId("repo-list"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["repo", "list"]));
  });
});

describe("슬롯 절", () => {
  it("슬롯 생성은 확인 대화를 지나야 하고 --user-ack 값이 repo 이름이다", async () => {
    const { run } = renderTab();

    fireEvent.change(screen.getByLabelText("슬롯을 만들 repo"), { target: { value: "app" } });
    fireEvent.click(screen.getByTestId("slot-add"));
    expect(run).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirm-value")).toHaveTextContent("--user-ack app");

    fireEvent.click(screen.getByTestId("confirm-approve"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      DEMO, ["worktree", "add", "app", "--user-ack", "app"]));
  });

  it("readonly 변형에도 승인이 붙는다", async () => {
    const { run } = renderTab();

    fireEvent.change(screen.getByLabelText("슬롯을 만들 repo"), { target: { value: "app" } });
    fireEvent.click(screen.getByTestId("slot-add-readonly"));
    fireEvent.click(screen.getByTestId("confirm-approve"));

    await waitFor(() => expect(run).toHaveBeenCalledWith(
      DEMO, ["worktree", "add", "app", "--readonly", "--user-ack", "app"]));
  });

  it("취소하면 슬롯을 만들지 않는다", () => {
    const { run } = renderTab();

    fireEvent.change(screen.getByLabelText("슬롯을 만들 repo"), { target: { value: "app" } });
    fireEvent.click(screen.getByTestId("slot-add"));
    fireEvent.click(screen.getByTestId("confirm-cancel"));

    expect(run).not.toHaveBeenCalled();
    expect(screen.queryByTestId("confirm-dialog")).toBeNull();
  });

  it("제거는 판정표가 연 행에만 열리고 readonly 행에는 없다", async () => {
    const { run } = renderTab();

    expect(screen.getByTestId("slot-remove-work/app_2")).toBeDisabled();   // 작업 중
    // 열린 기록 행도 같은 한 줄로 닫힌다(그 창이 아직 그 자리를 쥐고 있을 수 있다).
    expect(screen.getByTestId("slot-remove-.")).toBeDisabled();
    expect(screen.queryByTestId("slot-remove-work/app_1")).toBeNull();     // readonly
    fireEvent.click(screen.getByTestId("slot-remove-work/app_3"));         // 아이들 행은 열린다

    await waitFor(() => expect(run).toHaveBeenCalledWith(
      DEMO, ["worktree", "remove", "work/app_3"]));
  });

  it("정리는 인자 없는 한 줄이다", async () => {
    const { run } = renderTab();
    fireEvent.click(screen.getByTestId("slot-prune"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["worktree", "prune-stale"]));
  });
});

describe("하네스·task 절", () => {
  it("설치된 하네스를 보이고 추가는 이름 하나다", async () => {
    const { run } = renderTab();

    expect(screen.getByTestId("harness-list")).toHaveTextContent("claude");
    fireEvent.change(screen.getByLabelText("추가할 하네스"), { target: { value: "codex" } });
    fireEvent.click(screen.getByTestId("harness-add"));

    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["add-harness", "codex"]));
  });

  it("이름만 있는 task 동작 둘이 여기에 있다 (종료한 task 는 현황 행이 없다)", async () => {
    const { run } = renderTab();

    fireEvent.change(screen.getByLabelText("task 이름"), { target: { value: "t1" } });
    fireEvent.click(screen.getByTestId("task-new"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["task", "new", "t1"]));

    fireEvent.click(screen.getByTestId("task-reopen"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(DEMO, ["task", "reopen", "t1"]));
  });
});

describe("산출 표시", () => {
  it("rc≠0 이면 stderr 원문이 빨간 박스로 온다", async () => {
    const refusal = "[중단] worktree add 'app': 물리 슬롯 생성에는 사용자 명시 승인이 필요하다.";
    renderTab({ rc: 1, stdout: "", stderr: refusal });

    fireEvent.click(screen.getByTestId("slot-prune"));

    expect(await screen.findByTestId("command-stderr")).toHaveTextContent(refusal);
  });

  it("허용 목록 밖 거절은 오류 문구로 온다", async () => {
    renderTab({ error: { kind: "not-allowed", message: "화면이 부르는 pm-config 명령이 아니다", stderr: "", rc: 2 } });

    fireEvent.click(screen.getByTestId("slot-prune"));

    expect(await screen.findByTestId("command-error"))
      .toHaveTextContent("화면이 부르는 pm-config 명령이 아니다");
  });

  it("프로젝트를 고르기 전에는 안내만 있다", () => {
    render(<ConfigTab />);
    expect(screen.getByTestId("ConfigTab")).toHaveTextContent("프로젝트를 고르면");
  });
});
