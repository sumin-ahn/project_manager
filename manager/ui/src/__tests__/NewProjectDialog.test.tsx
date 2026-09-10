import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ApplyStep, Bridge } from "../api";
import NewProjectDialog, { folderName } from "../components/NewProjectDialog";

const DEST = "/projects/new-thing";
const UPSTREAM = "git@github.com:example/project_manager.git";
const PREFIX = "pay";

/** 실측한 세 단계 로그 — 엔진 문구를 그대로 둔다(화면이 다시 쓰지 않는다). */
const STEPS: ApplyStep[] = [
  { name: "import", rc: 0, stdout: "✓ 116 파일 복사 · 16 파일 placeholder 치환\n", stderr: "" },
  { name: "init", rc: 0, stdout: "─ init 완료 — 이 clone setup 끝 (repo new-thing) ─\n", stderr: "" },
  { name: "register", rc: 0, stdout: `등록부에 추가: ${DEST}`, stderr: "" },
];

const REFUSED: ApplyStep[] = [
  { name: "import", rc: 1, stdout: "",
    stderr: `오류: --new 대상이 비어있지 않습니다: ${DEST}. 기존 파일이 있는 디렉토리에는 `
            + "비파괴 백업이 되는 --into 를 사용하세요." },
];

function makeBridge(overrides: Partial<Bridge> = {}): Bridge {
  return {
    apply_defaults: vi.fn(async () => ({
      harnesses: ["claude", "opencode", "codex"], upstream: UPSTREAM })),
    pick_folder: vi.fn(async () => ({ path: DEST })),
    apply_new: vi.fn(async () => ({
      steps: STEPS, ok: true, path: DEST })),
    ...overrides,
  } as unknown as Bridge;
}

async function mounted(bridge: Bridge, onApplied = vi.fn(), onClose = vi.fn()) {
  render(<NewProjectDialog bridge={bridge} onClose={onClose} onApplied={onApplied} />);
  await screen.findByLabelText("upstream");
  return { onApplied, onClose };
}

afterEach(cleanup);

// 이 화면 회귀는 창 손님을 잰다 — 폴더 고르기가 OS 대화상자로 가는 자리다.
beforeEach(() => { window.history.replaceState({}, "", "/?client=window"); });

describe("대화의 기본값", () => {
  it("하네스 라디오와 upstream 은 번들이 답한 값으로 채워진다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);

    expect(bridge.apply_defaults).toHaveBeenCalledTimes(1);
    expect(screen.getAllByRole("radio").map((node) => (node as HTMLInputElement).value))
      .toEqual(["claude", "opencode", "codex"]);
    expect(screen.getByLabelText("upstream")).toHaveValue(UPSTREAM);
    expect((screen.getAllByRole("radio")[0] as HTMLInputElement).checked).toBe(true);
  });

  it("upstream 은 편집 가능하고 편집한 값이 그대로 브리지로 간다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);

    fireEvent.change(screen.getByLabelText("upstream"),
                     { target: { value: "https://mirror.invalid/pm.git" } });
    fireEvent.click(screen.getByTestId("new-apply"));

    await waitFor(() => expect(bridge.apply_new).toHaveBeenCalledWith(
      DEST, "claude", "new-thing", "", "", "", "https://mirror.invalid/pm.git", ""));
  });

  it("폴더를 고르면 이름 칸이 폴더명으로 찬다", async () => {
    await mounted(makeBridge());

    fireEvent.click(screen.getByText("폴더 선택"));

    expect(await screen.findByText(DEST)).toBeInTheDocument();
    expect(screen.getByLabelText("이름")).toHaveValue("new-thing");
    expect(folderName("C:\\\\work\\\\pay")).toBe("pay");
  });

  it("폴더를 고르기 전에는 적용을 누를 수 없다", async () => {
    await mounted(makeBridge());

    expect(screen.getByTestId("new-apply")).toBeDisabled();
  });
});

describe("prefix 확인 대화", () => {
  it("prefix 가 있으면 승인 전에는 브리지를 부르지 않는다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);
    fireEvent.change(screen.getByLabelText("prefix"), { target: { value: PREFIX } });

    fireEvent.click(screen.getByTestId("new-apply"));

    expect(screen.getByTestId("prefix-confirm")).toHaveTextContent(`${PREFIX} 를 만든다`);
    expect(bridge.apply_new).not.toHaveBeenCalled();
  });

  it("승인 값은 확인 대화가 돌려준 그 값이다 (관리자가 만들지 않는다)", async () => {
    const bridge = makeBridge();
    await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);
    fireEvent.change(screen.getByLabelText("prefix"), { target: { value: PREFIX } });
    fireEvent.change(screen.getByLabelText("area"), { target: { value: "결제" } });
    fireEvent.click(screen.getByTestId("new-apply"));

    fireEvent.click(screen.getByTestId("prefix-approve"));

    await waitFor(() => expect(bridge.apply_new).toHaveBeenCalledWith(
      DEST, "claude", "new-thing", PREFIX, "결제", "", UPSTREAM, PREFIX));
  });

  it("취소하면 대화만 닫히고 아무것도 부르지 않는다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);
    fireEvent.change(screen.getByLabelText("prefix"), { target: { value: PREFIX } });
    fireEvent.click(screen.getByTestId("new-apply"));

    fireEvent.click(screen.getByTestId("prefix-cancel"));

    expect(screen.queryByTestId("prefix-confirm")).not.toBeInTheDocument();
    expect(bridge.apply_new).not.toHaveBeenCalled();
  });

  it("prefix 가 비면 확인 없이 바로 가고 승인 값도 빈 값이다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);

    fireEvent.click(screen.getByTestId("new-apply"));

    expect(screen.queryByTestId("prefix-confirm")).not.toBeInTheDocument();
    await waitFor(() => expect(bridge.apply_new).toHaveBeenCalledWith(
      DEST, "claude", "new-thing", "", "", "", UPSTREAM, ""));
  });
});

describe("진행 로그", () => {
  it("성공하면 세 단계가 순서대로 남고 질문형으로 넘긴다", async () => {
    const bridge = makeBridge();
    const { onApplied } = await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);

    fireEvent.click(screen.getByTestId("new-apply"));

    await waitFor(() => expect(onApplied).toHaveBeenCalledWith(DEST));
    expect([...screen.getByTestId("new-log").children].map((node) => node.getAttribute("data-testid")))
      .toEqual(["step-import", "step-init", "step-register"]);
    expect(screen.getByTestId("step-init")).toHaveTextContent("init 완료");
  });

  it("실패하면 그때까지의 로그가 엔진 문구 그대로 남고 넘어가지 않는다", async () => {
    const bridge = makeBridge({
      apply_new: vi.fn(async () => ({
        steps: REFUSED, ok: false, path: null })),
    });
    const { onApplied } = await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);

    fireEvent.click(screen.getByTestId("new-apply"));

    expect(await screen.findByTestId("step-import")).toHaveTextContent("비어있지 않습니다");
    expect(screen.getByTestId("step-import")).toHaveTextContent("rc 1");
    expect(screen.queryByTestId("step-init")).not.toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("호출 전 거부(승인 값 불일치·빈 upstream)는 경고 한 줄로 온다", async () => {
    const bridge = makeBridge({
      apply_new: vi.fn(async () => ({
        error: { kind: "invalid-path" as const, message: "upstream 이 비어 있다", stderr: "", rc: 2 },
      })),
    });
    const { onApplied } = await mounted(bridge);
    fireEvent.click(screen.getByText("폴더 선택"));
    await screen.findByText(DEST);

    fireEvent.click(screen.getByTestId("new-apply"));

    expect(await screen.findByRole("alert")).toHaveTextContent("upstream 이 비어 있다");
    expect(screen.queryByTestId("new-log")).not.toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
  });
});
