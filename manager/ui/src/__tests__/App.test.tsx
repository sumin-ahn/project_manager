import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { LIGHT_INTERVAL_MS } from "../App";
import type { Bridge, BridgeError, ConfKeysDocument, StatusDocument } from "../api";
import confKeys from "../fixtures/conf-keys.sample.json";
import fixture from "../fixtures/status.sample.json";

const DEMO = "/projects/demo";
const OTHER = "/projects/other";
const document_ = fixture as unknown as StatusDocument;
const oldGeneration: BridgeError = {
  kind: "old-generation",
  message: "이 사본의 엔진은 `status --json` 을 모른다 — 먼저 갱신하라.",
  stderr: "pm-config: error: unrecognized arguments: --json",
  rc: 2,
};

function makeBridge(overrides: Partial<Bridge> = {}): Bridge {
  return {
    list_projects: vi.fn(async () => ({ projects: [{ path: DEMO, name: "demo" }] })),
    add_project: vi.fn(async () => ({ projects: [{ path: DEMO, name: "demo" }] })),
    remove_project: vi.fn(async () => ({ projects: [] })),
    status: vi.fn(async () => document_),
    latest_tags: vi.fn(async () => ({
      projects: { [DEMO]: { upstream: "/projects/upstream.git" } },
      upstreams: { "/projects/upstream.git": { tag: "v2.0.0" } },
    })),
    pick_folder: vi.fn(async () => ({ path: null })),
    conf_keys: vi.fn(async () => confKeys as unknown as ConfKeysDocument),
    conf_values: vi.fn(async () => ({ values: {} })),
    ...overrides,
  } as Bridge;
}

function settingsCalls(bridge: Bridge) {
  return (bridge.conf_keys as ReturnType<typeof vi.fn>).mock.calls.length;
}

/** 화면이 연 알림 스트림 — 테스트 대역(`vitest.setup.ts`)이 모아 둔 마지막 것. */
function openedStream(): EventTarget {
  const opened = (globalThis.EventSource as unknown as { opened: EventTarget[] }).opened;
  return opened[opened.length - 1];
}

function setVisibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
}

/** 가벼운 절 — 보드·원격 대조를 붙이지 않은 조회. */
function lightCalls(bridge: Bridge) {
  return (bridge.status as ReturnType<typeof vi.fn>).mock.calls
    .filter((call) => call[1] === false && call[3] !== true).length;
}

function lightCallsFor(bridge: Bridge, path: string) {
  return (bridge.status as ReturnType<typeof vi.fn>).mock.calls
    .filter((call) => call[0] === path && call[1] === false && call[3] !== true).length;
}

/** 원격 대조(fetch) 조회 — 네 번째 인자가 참인 호출. */
function remoteCalls(bridge: Bridge) {
  return (bridge.status as ReturnType<typeof vi.fn>).mock.calls
    .filter((call) => call[3] === true).length;
}

// 이 화면 회귀는 창 손님을 잰다 — 폴더 고르기가 OS 대화상자로 가는 자리다.
beforeEach(() => { window.history.replaceState({}, "", "/?client=window"); });

afterEach(() => {
  cleanup();
  setVisibility("visible");
  vi.useRealTimers();
});

describe("시작 조회", () => {
  it("목록 1회 · 가벼운 절 전부 · 네트워크 1회를 돌고 배지를 보인다", async () => {
    const bridge = makeBridge();
    render(<App bridge={bridge} />);

    expect(await screen.findByTestId("project-demo")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("v1.7.13").length).toBeGreaterThan(0));
    expect(bridge.list_projects).toHaveBeenCalledTimes(1);
    expect(bridge.latest_tags).toHaveBeenCalledTimes(1);
    // 가벼운 절 1회 + 선택 프로젝트 무거운 절 1회.
    expect(lightCalls(bridge)).toBe(1);
    expect((bridge.status as ReturnType<typeof vi.fn>).mock.calls
      .filter((call) => call[1] === true).length).toBe(1);
    expect(screen.getByText("갱신 있음 ↑")).toBeInTheDocument();
    expect(screen.getByTestId("dot-demo")).toHaveAttribute("data-dot", "orange");
  });

  it("가벼운 절은 보드를 부르지 않는다 (with_board=false)", async () => {
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await screen.findByTestId("project-demo");
    await waitFor(() => expect(bridge.status).toHaveBeenCalledWith(DEMO, false, false));
  });
});

describe("주기 조회", () => {
  it("창이 보이면 5초마다 가벼운 절을 다시 돈다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await waitFor(() => expect(lightCalls(bridge)).toBe(1));

    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS); });
    expect(lightCalls(bridge)).toBe(2);
    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS); });
    expect(lightCalls(bridge)).toBe(3);
  });

  it("창이 보이지 않으면 주기 조회가 0이고, 앞으로 오면 즉시 1회 돈다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await waitFor(() => expect(lightCalls(bridge)).toBe(1));

    setVisibility("hidden");
    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS * 4); });
    expect(lightCalls(bridge)).toBe(1);

    setVisibility("visible");
    await waitFor(() => expect(lightCalls(bridge)).toBe(2));
  });
});

describe("등록", () => {
  const PICKED = "/projects/link-to-fresh/";
  const REGISTERED = "/projects/fresh";

  it("새 행의 조회 키는 폴더 선택 표기가 아니라 등록부가 돌려준 표기다", async () => {
    const bridge = makeBridge({
      pick_folder: vi.fn(async () => ({ path: PICKED })),
      add_project: vi.fn(async () => ({
        projects: [{ path: DEMO, name: "demo" }, { path: REGISTERED, name: "fresh" }],
      })),
    } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await screen.findByTestId("project-demo");

    fireEvent.click(screen.getByLabelText("프로젝트 추가"));

    // 등록 인자는 사람이 고른 표기 그대로다(관리자가 경로를 다시 풀지 않는다).
    await waitFor(() => expect(bridge.add_project).toHaveBeenCalledWith(PICKED));
    // 조회는 목록 행과 같은 키로 나간다 — 두 표기를 들면 그 행이 자기 결과를 못 찾는다.
    await waitFor(() => expect(lightCallsFor(bridge, REGISTERED)).toBe(1));
    expect(lightCallsFor(bridge, PICKED)).toBe(0);
    expect(await screen.findByTestId("project-fresh")).toBeInTheDocument();
  });

  it("이미 등록된 폴더를 다시 고르면 새로 조회할 행이 없다", async () => {
    const bridge = makeBridge({ pick_folder: vi.fn(async () => ({ path: DEMO })) } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await waitFor(() => expect(lightCalls(bridge)).toBe(1));

    fireEvent.click(screen.getByLabelText("프로젝트 추가"));

    await waitFor(() => expect(bridge.add_project).toHaveBeenCalledWith(DEMO));
    expect(lightCalls(bridge)).toBe(1);
  });
});

describe("쓰기 동작 중의 주기 조회", () => {
  it("화면은 쓰기 동작을 세지 않는다 — 주기 조회는 그대로 돌고 제외 판정은 서버가 한다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const bridge = makeBridge({
      list_projects: vi.fn(async () => ({
        projects: [{ path: DEMO, name: "demo" }, { path: OTHER, name: "other" }],
      })),
      update_preview: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })),
      update_apply: vi.fn(() => new Promise<{ rc: number; stdout: string; stderr: string }>(
        () => {})),
    } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await waitFor(() => expect(lightCallsFor(bridge, DEMO)).toBe(1));

    fireEvent.mouseDown(screen.getByRole("tab", { name: "갱신" }));
    fireEvent.click(await screen.findByTestId("update-apply"));
    fireEvent.click(await screen.findByTestId("busy-continue"));   // 진행 중 세션 확인 대화
    await waitFor(() => expect(bridge.update_apply).toHaveBeenCalledWith(DEMO));

    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS * 2); });

    // 손님이 둘이라 화면에서 세면 값이 틀린다 — 두 프로젝트 모두 주기가 그대로 돈다.
    expect(lightCallsFor(bridge, DEMO)).toBeGreaterThanOrEqual(3);
    expect(lightCallsFor(bridge, OTHER)).toBeGreaterThanOrEqual(3);
  });
});

describe("서버 알림", () => {
  it("다른 손님이 저장·적용을 끝내면 그 신호로 다시 읽는다", async () => {
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await waitFor(() => expect(lightCallsFor(bridge, DEMO)).toBe(1));
    const before = settingsCalls(bridge);

    act(() => { openedStream().dispatchEvent(
      new MessageEvent("message", { data: JSON.stringify({ name: "conf_save", path: DEMO }) })); });

    await waitFor(() => expect(lightCallsFor(bridge, DEMO)).toBe(2));
    await waitFor(() => expect(settingsCalls(bridge)).toBe(before + 1));
  });
});

describe("배경 조회의 실패", () => {
  it("저장·적용 중 거절돼도 경고를 띄우지 않고 마지막 값이 그대로다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const busy: BridgeError = {
      kind: "not-allowed",
      message: "/projects/demo 에 저장·적용(update_apply)이 도는 중이라 이 요청은 부르지 않았다.",
      stderr: "",
      rc: 2,
    };
    const status = vi.fn()
      .mockResolvedValueOnce(document_)        // 가벼운 절 1회차
      .mockResolvedValueOnce(document_)        // 무거운 절 1회차
      .mockResolvedValue({ error: busy });     // 그 뒤 배경 조회는 서버가 거절한다
    const bridge = makeBridge({ status } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await waitFor(() => expect(screen.getAllByText("v1.7.13").length).toBeGreaterThan(0));

    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS * 2); });

    expect(screen.queryByText("조회 실패 (마지막 값 유지)")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getAllByText("v1.7.13").length).toBeGreaterThan(0);
    expect(screen.getByTestId("dot-demo")).toHaveAttribute("data-dot", "orange");
  });
});

describe("실패 처리", () => {
  it("조회가 실패해도 마지막 값을 지우지 않고 빨간 경고만 붙인다", async () => {
    const status = vi.fn()
      .mockResolvedValueOnce(document_)        // 가벼운 절 1회차
      .mockResolvedValueOnce(document_)        // 무거운 절 1회차
      .mockResolvedValue({ error: oldGeneration });
    const bridge = makeBridge({ status } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await waitFor(() => expect(screen.getAllByText("v1.7.13").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByLabelText("새로고침"));

    expect(await screen.findByText("먼저 갱신")).toBeInTheDocument();
    // 마지막 값 유지 — 버전 배지와 상태 점이 그대로다.
    expect(screen.getAllByText("v1.7.13").length).toBeGreaterThan(0);
    expect(screen.getByTestId("dot-demo")).toHaveAttribute("data-dot", "orange");
    expect(screen.getAllByText("조회 실패 (마지막 값 유지)").length).toBeGreaterThan(0);
  });

  it("한 프로젝트의 실패가 다른 프로젝트 행을 지우지 않는다", async () => {
    const bridge = makeBridge({
      list_projects: vi.fn(async () => ({
        projects: [{ path: DEMO, name: "demo" }, { path: OTHER, name: "other" }],
      })),
      status: vi.fn(async (path: string) =>
        (path === OTHER ? { error: oldGeneration } : document_)),
    } as Partial<Bridge>);
    render(<App bridge={bridge} />);

    await waitFor(() => expect(screen.getByTestId("dot-demo")).toHaveAttribute("data-dot", "orange"));
    expect(screen.getByTestId("project-other")).toHaveTextContent("먼저 갱신");
    expect(screen.getByTestId("project-demo")).not.toHaveTextContent("먼저 갱신");
    expect(screen.getByTestId("project-demo")).toHaveTextContent("v1.7.13");
  });
});

describe("수동 새로고침", () => {
  it("선택 프로젝트의 네 조회를 전부 다시 돈다", async () => {
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await waitFor(() => expect(bridge.latest_tags).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(settingsCalls(bridge)).toBe(1));
    // 원격 대조는 시작 때 1회다(네트워크라 주기 조회에 넣지 않는다).
    await waitFor(() => expect(remoteCalls(bridge)).toBe(1));
    expect(bridge.status).toHaveBeenCalledWith(DEMO, false, false, true);

    fireEvent.click(screen.getByLabelText("새로고침"));

    await waitFor(() => expect(bridge.latest_tags).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(remoteCalls(bridge)).toBe(2));
    expect(lightCalls(bridge)).toBe(2);
    expect((bridge.status as ReturnType<typeof vi.fn>).mock.calls
      .filter((call) => call[1] === true).length).toBe(2);
    await waitFor(() => expect(settingsCalls(bridge)).toBe(2));
  });
});

describe("설정 선읽기", () => {
  it("프로젝트를 고를 때 설정 두 조회를 미리 받고, 탭을 여는 것으로는 부르지 않는다", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const bridge = makeBridge();
    render(<App bridge={bridge} />);

    // 선택 시 1회 — 탭은 아직 열지 않았다.
    await waitFor(() => expect(bridge.conf_keys).toHaveBeenCalledWith(DEMO));
    await waitFor(() => expect(bridge.conf_values).toHaveBeenCalledWith(DEMO));
    expect(settingsCalls(bridge)).toBe(1);

    fireEvent.mouseDown(screen.getByRole("tab", { name: "설정" }));
    await screen.findByTestId("settings-nav");
    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS * 2); });

    // 탭 진입도, 주기 조회도 설정을 다시 부르지 않는다.
    expect(settingsCalls(bridge)).toBe(1);
    expect(lightCalls(bridge)).toBeGreaterThan(1);
  });

  it("저장하면 값과 판정을 다시 읽는다 — 화면 값은 재조회 결과다", async () => {
    let stored: Record<string, string> = {};
    const bridge = makeBridge({
      conf_save: vi.fn(async (_path: string, updates: Record<string, string>) => {
        stored = { ...stored, ...updates };
        return { changed: true, conf: document_.conf };
      }),
      conf_values: vi.fn(async () => ({ values: stored })),
    } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await waitFor(() => expect(settingsCalls(bridge)).toBe(1));

    fireEvent.mouseDown(screen.getByRole("tab", { name: "설정" }));
    fireEvent.click(await screen.findByTestId("nav-ctx"));
    // 값이 없는 사본이라 칸이 닫혀 있다 — 직접 정함을 켜야 입력 칸이 선다.
    fireEvent.click(screen.getByLabelText("ctx.checkpoint_pct 직접 정함"));
    fireEvent.change(screen.getByLabelText("ctx.checkpoint_pct"), { target: { value: "26" } });
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { "ctx.checkpoint_pct": "26" }));
    await waitFor(() => expect(settingsCalls(bridge)).toBe(2));
    // 저장 직후 판정도 다시 읽는다(가벼운 절) — 배지는 사본이 낸 것이다.
    expect(lightCalls(bridge)).toBeGreaterThan(1);
    await waitFor(() => expect(screen.getByLabelText("ctx.checkpoint_pct")).toHaveValue("26"));
  });
});

describe("갱신 탭", () => {
  it("탭을 열면 최신 태그를 다시 조회한다", async () => {
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await waitFor(() => expect(bridge.latest_tags).toHaveBeenCalledTimes(1));

    fireEvent.mouseDown(screen.getByRole("tab", { name: "갱신" }));

    await waitFor(() => expect(bridge.latest_tags).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("UpdateTab")).toBeInTheDocument();
    // 탭 전환은 네트워크 조회만 다시 돈다 — 가벼운/무거운 절은 그대로다.
    expect(lightCalls(bridge)).toBe(1);
  });
});

describe("새 프로젝트 적용", () => {
  const NEW = "/projects/fresh";

  /** 적용 성공 뒤 목록에 그 프로젝트가 등장하는 등록부. */
  function applyBridge(): Bridge {
    let registered = [{ path: DEMO, name: "demo" }];
    return makeBridge({
      list_projects: vi.fn(async () => ({ projects: registered })),
      apply_defaults: vi.fn(async () => ({ harnesses: ["claude"], upstream: "git@x/pm.git" })),
      pick_folder: vi.fn(async () => ({ path: NEW })),
      apply_new: vi.fn(async () => {
        registered = [...registered, { path: NEW, name: "fresh" }];
        return {
          steps: [{ name: "register" as const, rc: 0, stdout: "등록부에 추가", stderr: "" }],
          ok: true, path: NEW,
        };
      }),
    } as Partial<Bridge>);
  }

  it("적용이 끝나면 그 프로젝트가 목록에 서고 설정 탭이 질문형으로 열린다", async () => {
    const bridge = applyBridge();
    render(<App bridge={bridge} />);
    await screen.findByTestId("project-demo");

    fireEvent.click(screen.getByText("새 프로젝트"));
    fireEvent.click(await screen.findByText("폴더 선택"));
    await screen.findByText(NEW);
    fireEvent.click(screen.getByTestId("new-apply"));

    expect(await screen.findByTestId("project-fresh")).toBeInTheDocument();
    expect(screen.queryByTestId("new-project")).not.toBeInTheDocument();
    expect(await screen.findByTestId("question-mode")).toBeInTheDocument();
    expect(bridge.conf_values).toHaveBeenCalledWith(NEW);
  });
});
