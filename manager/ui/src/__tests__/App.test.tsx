import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { LIGHT_INTERVAL_MS } from "../App";
import type { Bridge, BridgeError, ConfKeysDocument, CtxCapacity, StatusDocument } from "../api";
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
        return { changed: true, conf: document_.conf, applied: [] };
      }),
      conf_values: vi.fn(async () => ({ values: stored })),
    } as Partial<Bridge>);
    render(<App bridge={bridge} />);
    await waitFor(() => expect(settingsCalls(bridge)).toBe(1));

    fireEvent.mouseDown(screen.getByRole("tab", { name: "설정" }));
    fireEvent.click(await screen.findByTestId("nav-ctx"));
    // 값이 없는 사본이라 칸이 닫혀 있다 — 직접 정함을 켜야 입력 칸이 선다.
    fireEvent.click(screen.getByLabelText("ctx.checkpoint_at_pct 직접 정함"));
    fireEvent.change(screen.getByLabelText("ctx.checkpoint_at_pct"), { target: { value: "76" } });
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { "ctx.checkpoint_at_pct": "76" }));
    await waitFor(() => expect(settingsCalls(bridge)).toBe(2));
    // 저장 직후 판정도 다시 읽는다(가벼운 절) — 배지는 사본이 낸 것이다.
    expect(lightCalls(bridge)).toBeGreaterThan(1);
    await waitFor(() => expect(screen.getByLabelText("ctx.checkpoint_at_pct")).toHaveValue("76"));
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


it("T0938_AT4_user_actions_and_background", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const capacity = vi.fn(async (_path: string, harness: string, _validateOnly: boolean) => ({
    schema: 1 as const, harness, status: "known" as const, reason: null, C: 967000,
    observed_at: new Date().toISOString(), valid_until: null,
  }));
  const bridge = makeBridge({ ctx_capacity: capacity });
  render(<App bridge={bridge} />);
  await waitFor(() => expect(bridge.conf_keys).toHaveBeenCalled());
  expect(capacity.mock.calls.filter((c) => !c[2])).toHaveLength(0);
  fireEvent.mouseDown(screen.getByRole("tab", { name: "설정" }));
  await waitFor(() => expect(capacity.mock.calls.filter((c) => !c[2])).toHaveLength(document_.harnesses.length));
  const initial = capacity.mock.calls.filter((c) => !c[2]).length;
  await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS * 2); });
  setVisibility("hidden"); setVisibility("visible");
  act(() => { openedStream().dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ path: DEMO }) })); });
  await waitFor(() => expect(capacity.mock.calls.some((c) => c[2])).toBe(true));
  expect(capacity.mock.calls.filter((c) => !c[2])).toHaveLength(initial);
  fireEvent.click(screen.getByLabelText("새로고침"));
  await waitFor(() => expect(capacity.mock.calls.filter((c) => !c[2])).toHaveLength(initial * 2));
});

it.each(["known", "unknown"] as const)("background_capacity_hint stays visible during validation (%s)", async (status) => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const pending: { harness: string; resolve: (reply: CtxCapacity) => void }[] = [];
  const known = (harness: string): CtxCapacity => ({
    schema: 1, harness, status: "known", reason: null, C: 967000,
    observed_at: "2026-10-04T00:00:00Z", valid_until: null,
  });
  const capacity = vi.fn((_path: string, harness: string, validateOnly = false) =>
    validateOnly ? new Promise<CtxCapacity>((resolve) => pending.push({ harness, resolve }))
      : Promise.resolve(known(harness)));
  render(<App bridge={makeBridge({ ctx_capacity: capacity })} />);
  await waitFor(() => expect(screen.getByRole("tab", { name: "설정" })).toBeInTheDocument());
  fireEvent.mouseDown(screen.getByRole("tab", { name: "설정" }));
  fireEvent.click(await screen.findByTestId("nav-harness"));
  await waitFor(() => expect(screen.getByTestId("capacity-claude")).toHaveTextContent("967000"));
  await act(async () => { for (const item of pending.splice(0)) item.resolve(known(item.harness)); });
  const initialQueries = capacity.mock.calls.filter((call) => !call[2]).length;
  const hint = screen.getByTestId("capacity-claude");
  const changedText: string[] = [];
  const observer = new MutationObserver(() => changedText.push(hint.textContent ?? ""));
  observer.observe(hint, { childList: true, subtree: true, characterData: true });
  try {
    await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS); });
    expect(pending).toHaveLength(document_.harnesses.length);
    expect(hint).toHaveTextContent("967000");
    expect(hint).not.toHaveTextContent("확인 중");
    expect(changedText).toEqual([]);
    await act(async () => {
      for (const check of pending.splice(0)) check.resolve(status === "unknown" && check.harness === "claude"
        ? { ...known(check.harness), status: "unknown", C: null, reason: "원천 변경" }
        : known(check.harness));
    });
    if (status === "unknown") expect(hint).toHaveTextContent("967000");
    else {
      expect(hint).toHaveTextContent("967000");
      expect(changedText).toEqual([]);
    }
    expect(capacity.mock.calls.filter((call) => !call[2])).toHaveLength(initialQueries + (status === "unknown" ? 1 : 0));
  } finally { observer.disconnect(); }
});

it("T0941_AT3_singleflight_late_check_project_and_expiry", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const pending: { path: string; harness: string; check: boolean; resolve: (value: any) => void }[] = [];
  const seen = new Set<string>();
  const capacity = vi.fn((path: string, harness: string, check: boolean) => {
    if (check && !seen.has(path)) return Promise.resolve({ schema: 2, harness, status: "unknown", C: null,
      observed_at: null, valid_until: null, reason: "미보유" });
    return new Promise<any>((resolve) => pending.push({ path, harness, check, resolve }));
  });
  const bridge = makeBridge({
    list_projects: vi.fn(async () => ({ projects: [{ path: DEMO, name: "demo" }, { path: OTHER, name: "other" }] })),
    ctx_capacity: capacity,
  });
  render(<App bridge={bridge} />);
  await waitFor(() => expect(pending.filter((p) => !p.check)).toHaveLength(1));
  fireEvent.mouseDown(screen.getByRole("tab", { name: "설정" }));
  fireEvent.click(await screen.findByTestId("nav-harness"));
  fireEvent.click(screen.getByLabelText("새로고침"));
  expect(pending.filter((p) => !p.check)).toHaveLength(1);
  const reply = (item: typeof pending[number], C: number, expiry: string | null = null) => {
    seen.add(item.path);
    item.resolve({ schema: 2, harness: item.harness, status: "known", reason: null, C,
      observed_at: new Date().toISOString(), valid_until: expiry,
      verification: { status: "verified", reason: null, verified_at: new Date().toISOString(), revision: "a".repeat(64), evidence_changed: false },
      budget: { status: "known", reason: null, W: 600000, C, E: Math.min(600000,C), P: Math.floor(Math.min(600000,C)*.94) } });
  };
  await act(async () => { pending.splice(0).forEach((p) => reply(p, 570000)); });
  expect(screen.getByTestId("capacity-claude")).toHaveTextContent("570000");
  await act(async () => { await vi.advanceTimersByTimeAsync(LIGHT_INTERVAL_MS); });
  const oldChecks = pending.splice(0);
  expect(oldChecks.every((p) => p.check)).toBe(true);
  fireEvent.click(screen.getByLabelText("새로고침"));
  await waitFor(() => expect(pending.filter((p) => !p.check)).toHaveLength(1));
  await act(async () => { pending.splice(0).forEach((p) => reply(p, 580000,new Date(Date.now()+1000).toISOString())); });
  await act(async () => { oldChecks.forEach((p) => reply(p,967000)); });
  expect(screen.getByTestId("capacity-claude")).toHaveTextContent("580000");
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  expect(screen.getByTestId("capacity-claude")).toHaveTextContent("유효기간 만료");
  fireEvent.click(screen.getByLabelText("새로고침"));
  await waitFor(() => expect(pending.filter((p) => !p.check)).toHaveLength(1));
  const previous = pending.splice(0);
  fireEvent.click(screen.getByTestId("project-other"));
  await waitFor(() => expect(pending.some((p) => p.path===OTHER && !p.check)).toBe(true));
  await act(async () => { pending.splice(0).forEach((p) => reply(p,258400)); });
  await act(async () => { previous.forEach((p) => reply(p,967000)); });
  fireEvent.click(screen.getByTestId("nav-harness"));
  expect(screen.getByTestId("capacity-claude")).toHaveTextContent("258400");
});

it("T0941_AT3_selected_visible_backoff_manual_and_event", async () => {
  vi.useFakeTimers({shouldAdvanceTime:true});
  const started=Date.now();const queries:{path:string;time:number}[]=[];
  const capacity=vi.fn(async(path:string,harness:string,check=false):Promise<CtxCapacity>=>{
    if(!check)queries.push({path,time:Date.now()-started});
    return {schema:2,harness,status:"unknown",reason:"transport unavailable",C:null,observed_at:null,valid_until:null,
      verification:{status:"unknown",reason:"transport unavailable",verified_at:null,revision:null,evidence_changed:false}};
  });
  const bridge=makeBridge({ctx_capacity:capacity,list_projects:vi.fn(async()=>({projects:[{path:DEMO,name:"demo"},{path:OTHER,name:"other"}]}))});
  render(<App bridge={bridge}/>);
  await waitFor(()=>expect(queries).toHaveLength(1));
  await act(async()=>{await vi.advanceTimersByTimeAsync(140000);});
  const differences=queries.slice(1).map((q,i)=>q.time-queries[i].time);
  expect(differences.slice(0,5)).toEqual([5000,10000,20000,40000,60000]);
  expect(queries.every((q)=>q.path===DEMO)).toBe(true);
  const count=queries.length;
  setVisibility("hidden");const calls=capacity.mock.calls.length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(120000);});
  expect(queries).toHaveLength(count);expect(capacity).toHaveBeenCalledTimes(calls);
  setVisibility("visible");
  await waitFor(()=>expect(queries.length).toBeGreaterThan(count));
  const before=queries.length;
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:JSON.stringify({name:"ctx_capacity",path:DEMO,harness:"claude",revision:"a".repeat(64)})})));
  await act(async()=>{await Promise.resolve();});expect(queries).toHaveLength(before);
  fireEvent.click(screen.getByLabelText("새로고침"));
  await waitFor(()=>expect(queries).toHaveLength(before+1));
});

it("T0941_AT5_query_generation_discards_old_status_and_keeps_new_unknown", async () => {
  vi.useFakeTimers({shouldAdvanceTime:true});
  let known=false;let hold=false;const old:((value:StatusDocument)=>void)[]=[];
  const budget=(status:"known"|"unknown")=>({status,reason:status==="known"?null:"원천 변경",W:600000,C:status==="known"?967000:null,E:status==="known"?600000:null,P:status==="known"?564000:null});
  const statusDoc=(current:boolean):StatusDocument=>({...document_,conf:{...document_.conf,ctx_budget:{claude:budget(current?"known":"unknown")}}});
  const status=vi.fn(async(_path:string,heavy=false)=>hold&&!heavy?new Promise<StatusDocument>((resolve)=>old.push(resolve)):statusDoc(known));
  const capacity=vi.fn(async(_path:string,harness:string,check=false):Promise<CtxCapacity>=>{
    if(!check)known=true;
    return {schema:2,harness,status:known?"known":"unknown",reason:null,C:known?967000:null,observed_at:null,valid_until:null,
      verification:{status:known?"verified":"unknown",reason:null,verified_at:null,revision:"a".repeat(64),evidence_changed:false},budget:budget(known?"known":"unknown")};
  });
  render(<App bridge={makeBridge({status,ctx_capacity:capacity})}/>);
  await waitFor(()=>expect(capacity.mock.calls.some(c=>!c[2])).toBe(true));
  fireEvent.mouseDown(screen.getByRole("tab",{name:"설정"}));fireEvent.click(await screen.findByTestId("nav-harness"));
  hold=true;await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(old.length).toBeGreaterThan(0);
  hold=false;fireEvent.click(screen.getByLabelText("새로고침"));
  await waitFor(()=>expect(screen.getByTestId("budget-claude")).toHaveTextContent("현재 설정 검증 완료 · 기준 600000"));
  await act(async()=>{old.splice(0).forEach(resolve=>resolve(statusDoc(false)));});
  expect(screen.getByTestId("budget-claude")).not.toHaveTextContent("재확인 필요");
  // A subsequent real core unknown response must be displayed and never hidden by the previous query budget.
  known=false;
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:JSON.stringify({name:"ctx_capacity",path:DEMO})})));
  await waitFor(()=>expect(screen.getByTestId("budget-claude")).toHaveTextContent("현재 예산 확인 불가"));
});

it("T0941_AT3_apply_required_is_stable_until_source_change", async()=>{
  vi.useFakeTimers({shouldAdvanceTime:true});let changed=false;
  const budget={status:"unknown" as const,reason:"현재 검증 없음",W:600000,C:null,E:null,P:null};
  const reply=(harness:string):CtxCapacity=>({schema:2,harness,status:"known",reason:null,C:567000,observed_at:null,valid_until:null,budget,
    verification:{status:"apply-required",reason:"불일치",verified_at:null,revision:"a".repeat(64),evidence_changed:false},
    candidate:{W:600000,C:567000,E:567000,P:532980,native_window:565980,managed:[{path:["autoCompactWindow"],value:565980}]}});
  let queried=false;
  const capacity=vi.fn(async(_path:string,harness:string,check=false):Promise<CtxCapacity>=>{
    if(check&&(!queried||changed))return{...reply(harness),status:"unknown",C:null,verification:{status:"changed",reason:"source changed",verified_at:null,revision:null,evidence_changed:false}};
    if(!check){queried=true;changed=false;}return reply(harness);
  });
  const bridge=makeBridge({ctx_capacity:capacity,status:vi.fn(async()=>({...document_,conf:{...document_.conf,ctx_budget:{claude:budget}}}))});
  render(<App bridge={bridge}/>);await waitFor(()=>expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(1));
  fireEvent.mouseDown(screen.getByRole("tab",{name:"설정"}));fireEvent.click(await screen.findByTestId("nav-harness"));
  await act(async()=>{await Promise.resolve();});
  const count=capacity.mock.calls.filter(c=>!c[2]).length;
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});
  expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(count);expect(screen.getByTestId("budget-claude")).toHaveTextContent("현재 기준 적용");
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});
  expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(count);
  changed=true;await act(async()=>{await vi.advanceTimersByTimeAsync(5000);});
  expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(count+1);
});

// Exact public schema2 facts used by the recurrence and ordering contracts.
function t0942Capacity(harness = "codex", known = true): CtxCapacity {
  return {schema:2,harness,status:"known",reason:null,C:570000,observed_at:null,valid_until:null,
    verification:{status:known?"verified":"changed",reason:known?null:"현재 선택/native/metadata 근거 변경",verified_at:null,revision:"a".repeat(64),evidence_changed:false},
    budget:{status:known?"known":"unknown",reason:known?null:"현재 선택/native/metadata 근거 변경",W:600000,C:known?570000:null,E:known?570000:null,P:known?535800:null},
    applied:{status:"known",reason:null,W:600000,E:570000,P:535800,native_window:535800,applied_at:"2026-10-05T00:00:00Z"}};
}
function t0942Status(known = true): StatusDocument {
  return {...document_,harnesses:["codex"],conf:{...document_.conf,ctx_budget:{codex:t0942Capacity("codex",known).budget!}}};
}
async function t0942Open() {
  fireEvent.mouseDown(screen.getByRole("tab",{name:"설정"}));
  fireEvent.click(await screen.findByTestId("nav-harness"));
}
it("T0942_AT1_source_changed_pending_query_and_repeat_expiry", async()=>{
  vi.useFakeTimers({shouldAdvanceTime:true});
  let changed=false; let pending: ((value:CtxCapacity)=>void)|undefined;
  const capacity=vi.fn((_path:string,harness:string,check=false):Promise<CtxCapacity>=>
    !check&&changed ? new Promise(resolve=>{pending=resolve;}) : Promise.resolve(t0942Capacity(harness,!changed)));
  const values={"harness.codex.ctx_window_tokens":"600000","harness.codex.compaction_pct":"94"};
  const bridge=makeBridge({ctx_capacity:capacity,status:vi.fn(async()=>t0942Status(!changed)),conf_values:vi.fn(async()=>({values}))});
  render(<App bridge={bridge}/>); await waitFor(()=>expect(capacity).toHaveBeenCalled());
  await t0942Open(); await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료"));
  const input=screen.getByLabelText("harness.codex.ctx_window_tokens");
  fireEvent.change(input,{target:{value:"610000"}});
  for(let cycle=0;cycle<2;cycle++) {
    changed=true; await act(async()=>{await vi.advanceTimersByTimeAsync(10000);});
    expect(pending).toBeDefined();
    const hint=screen.getByTestId("budget-codex");
    expect(hint).toHaveTextContent("자동 재검증 중");
    expect(hint).not.toHaveTextContent("재확인 필요");
    expect(hint).not.toHaveTextContent("현재 가용 한도");
    expect(hint).not.toHaveTextContent("현재 설정 검증 완료");
    expect(screen.getByLabelText("harness.codex.ctx_window_tokens")).toBe(input);
    expect(input).toHaveValue("610000");
    expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(cycle+2); // tab query + exactly one automatic query each cycle
    changed=false;
    await act(async()=>{pending!(cycle===1?{...t0942Capacity(),valid_until:new Date(Date.now()+1000).toISOString()}:t0942Capacity());});
    expect(hint).toHaveTextContent("현재 설정 검증 완료");
  }
  await act(async()=>{await vi.advanceTimersByTimeAsync(1000);});
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("자동 재검증 대기");
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("유효기간 만료");
  await act(async()=>{await vi.advanceTimersByTimeAsync(4000);});
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료");
  expect(input).toHaveValue("610000"); expect(bridge.conf_save).toBeUndefined();
});
it("T0942_AT2_same_epoch_unknown_S1_cannot_downgrade_S2_and_new_unknown_admitted",async()=>{
  vi.useFakeTimers({shouldAdvanceTime:true});
  let hold=false; const pending:((value:StatusDocument)=>void)[]=[];
  const status=vi.fn((_path:string,_heavy=false,_board=false,remote=false):Promise<StatusDocument>=>
    hold&&!remote ? new Promise(resolve=>pending.push(resolve)) : Promise.resolve(t0942Status()));
  const capacity=vi.fn(async(_path:string,harness:string)=>t0942Capacity(harness));
  render(<App bridge={makeBridge({status,ctx_capacity:capacity})}/>);
  await waitFor(()=>expect(capacity).toHaveBeenCalled());await t0942Open();
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료"));
  hold=true;
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  const s1=pending.splice(0);expect(s1).toHaveLength(2);
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  const s2=pending.splice(0);expect(s2).toHaveLength(2);
  setVisibility("hidden");
  await act(async()=>{s2.forEach(resolve=>resolve(t0942Status()));});
  await act(async()=>{s1.forEach(resolve=>resolve(t0942Status(false)));});
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료");
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  await act(async()=>{pending.splice(0).forEach(resolve=>resolve(t0942Status(false)));});
  expect(screen.getByTestId("budget-codex")).not.toHaveTextContent("현재 설정 검증 완료");
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 예산 확인 불가");
});

it("T0942_AT2_first_light_survives_newer_heavy_and_preserves_fallback_budget",async()=>{
  const pending:{heavy:boolean;resolve:(value:StatusDocument)=>void}[]=[];
  const status=vi.fn((_path:string,heavy=false,_board=false,remote=false):Promise<StatusDocument>=>remote?Promise.resolve(t0942Status()):new Promise(resolve=>pending.push({heavy,resolve})));
  const capacity=vi.fn(async(_path:string,harness:string):Promise<CtxCapacity>=>{const value=t0942Capacity(harness);delete value.budget;return value;});
  render(<App bridge={makeBridge({status,ctx_capacity:capacity})}/>);
  await waitFor(()=>expect(pending).toHaveLength(2));
  await act(async()=>{pending.find(p=>p.heavy)!.resolve(t0942Status());});
  expect(capacity).not.toHaveBeenCalled();
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  await act(async()=>{pending.filter(p=>p.heavy).at(-1)!.resolve(t0942Status());});
  await act(async()=>{pending.find(p=>!p.heavy)!.resolve(t0942Status(false));});
  await waitFor(()=>expect(capacity).toHaveBeenCalledWith(DEMO,"codex",true));
  await t0942Open(); // existing explicit tab query, whose reply also lacks a budget
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료 · 기준 570000"));
  expect(screen.getByTestId("capacity-codex")).toHaveTextContent("570000");
});
it.each([true,false])("T0942_AT2_newer_check_fences_older_%s_status_and_fallback",async(heavy)=>{
  vi.useFakeTimers({shouldAdvanceTime:true});let hold=false;
  const pending:{heavy:boolean;resolve:(value:StatusDocument)=>void}[]=[];
  const status=vi.fn((_path:string,h=false,_board=false,remote=false):Promise<StatusDocument>=>hold&&!remote?new Promise(resolve=>pending.push({heavy:h,resolve})):Promise.resolve(t0942Status()));
  const capacity=vi.fn(async(_path:string,harness:string,check=false):Promise<CtxCapacity>=>{const value=t0942Capacity(harness);if(check)delete value.budget;return value;});
  render(<App bridge={makeBridge({status,ctx_capacity:capacity})}/>);await waitFor(()=>expect(capacity).toHaveBeenCalled());await t0942Open();
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료"));
  hold=true;act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  const old=pending.splice(0);
  // The other channel admits known, then dispatches a newer pure check with no budget field.
  await act(async()=>{old.find(p=>p.heavy!==heavy)!.resolve(t0942Status());});
  if(heavy) { // light completion already dispatched the check
    expect(capacity.mock.calls.at(-1)![2]).toBe(true);
  } else {
    // Existing visibility return dispatches a light request and check; no new request source.
    setVisibility("hidden");setVisibility("visible");
    await act(async()=>{pending.splice(0).forEach(p=>p.resolve(t0942Status()));});
  }
  setVisibility("hidden");
  await act(async()=>{old.find(p=>p.heavy===heavy)!.resolve(t0942Status(false));});
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료 · 기준 570000");
  expect(screen.getByTestId("budget-codex")).not.toHaveTextContent("현재 예산 확인 불가");
});
it("T0942_AT2_per_harness_watermarks_admit_independent_codex_unknown",async()=>{
  let hold=false;const pending:((value:StatusDocument)=>void)[]=[];
  const doc=(codex=true,claude=true):StatusDocument=>({...t0942Status(),harnesses:["claude","codex","opencode"],conf:{...document_.conf,ctx_budget:{claude:t0942Capacity("claude",claude).budget!,codex:t0942Capacity("codex",codex).budget!,opencode:t0942Capacity("opencode").budget!}}});
  const status=vi.fn((_path:string,_heavy=false,_board=false,remote=false):Promise<StatusDocument>=>hold&&!remote?new Promise(resolve=>pending.push(resolve)):Promise.resolve(doc()));
  const capacity=vi.fn(async(_path:string,harness:string)=>t0942Capacity(harness));
  render(<App bridge={makeBridge({status,ctx_capacity:capacity})}/>);await waitFor(()=>expect(capacity).toHaveBeenCalledTimes(3));await t0942Open();
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료"));
  await act(async()=>{await Promise.resolve();});
  hold=true;setVisibility("hidden");
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));const s1=pending.splice(0);
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));const s2=pending.splice(0);
  const partial={...doc(),conf:{...document_.conf,ctx_applied:{},ctx_budget:{claude:t0942Capacity("claude").budget!}}};
  await act(async()=>{s2.forEach(resolve=>resolve(partial));});
  await act(async()=>{s1.forEach(resolve=>resolve(doc(false,false)));});
  expect(screen.getByTestId("budget-claude")).toHaveTextContent("현재 설정 검증 완료");
  expect(screen.getByTestId("budget-opencode")).toHaveTextContent("현재 설정 검증 완료");
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 예산 확인 불가");
});
it("T0942_AT2_older_known_cannot_restore_newer_unknown_or_invent_verified",async()=>{
  let hold=false;const pending:((value:StatusDocument)=>void)[]=[];
  const status=vi.fn((_path:string,_heavy=false,_board=false,remote=false):Promise<StatusDocument>=>hold&&!remote?new Promise(resolve=>pending.push(resolve)):Promise.resolve(t0942Status()));
  render(<App bridge={makeBridge({status,ctx_capacity:vi.fn(async(_path:string,harness:string)=>t0942Capacity(harness))})}/>);
  await waitFor(()=>expect(screen.getByRole("tab",{name:"설정"})).toBeInTheDocument());await t0942Open();
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료"));
  await act(async()=>{await Promise.resolve();});
  hold=true;setVisibility("hidden");
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));const s1=pending.splice(0);
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));const s2=pending.splice(0);
  await act(async()=>{s2.forEach(resolve=>resolve(t0942Status(false)));});
  await act(async()=>{s1.forEach(resolve=>resolve(t0942Status()));});
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 예산 확인 불가");
  act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  await act(async()=>{pending.splice(0).forEach(resolve=>resolve(t0942Status()));});
  expect(screen.getByTestId("budget-codex")).not.toHaveTextContent("현재 설정 검증 완료"); // status known cannot verify C
});
it("T0942_AT2_query_finally_status_uses_same_admission_as_events",async()=>{
  let hold=false;let query:((value:CtxCapacity)=>void)|undefined;const pending:((value:StatusDocument)=>void)[]=[];
  const status=vi.fn((_path:string,_heavy=false,_board=false,remote=false):Promise<StatusDocument>=>hold&&!remote?new Promise(resolve=>pending.push(resolve)):Promise.resolve(t0942Status()));
  const capacity=vi.fn((_path:string,harness:string,check=false):Promise<CtxCapacity>=>check?Promise.resolve(t0942Capacity(harness)):new Promise(resolve=>{query=resolve;}));
  render(<App bridge={makeBridge({status,ctx_capacity:capacity})}/>);await waitFor(()=>expect(capacity).toHaveBeenCalled());await t0942Open();await waitFor(()=>expect(query).toBeDefined());
  hold=true;await act(async()=>{query!(t0942Capacity());});const finallyStatus=pending.splice(0);expect(finallyStatus).toHaveLength(1);
  setVisibility("hidden");act(()=>openedStream().dispatchEvent(new MessageEvent("message",{data:"{}"})));
  await act(async()=>{pending.splice(0).forEach(resolve=>resolve(t0942Status()));});
  await act(async()=>{finallyStatus.forEach(resolve=>resolve(t0942Status(false)));});
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("현재 설정 검증 완료");
});

it("T0942_AT2_three_query_replies_survive_other_harness_completion_epochs",async()=>{
  const pending:{harness:string;resolve:(value:CtxCapacity)=>void}[]=[];
  const doc={...t0942Status(),harnesses:["claude","codex","opencode"]};
  const capacity=vi.fn((_path:string,harness:string,check=false):Promise<CtxCapacity>=>check?Promise.resolve(t0942Capacity(harness)):new Promise(resolve=>pending.push({harness,resolve})));
  render(<App bridge={makeBridge({status:vi.fn(async()=>doc),ctx_capacity:capacity})}/>);await waitFor(()=>expect(capacity).toHaveBeenCalledTimes(3));await t0942Open();
  await waitFor(()=>expect(pending).toHaveLength(3));
  for(const harness of ["opencode","codex","claude"]) {
    const C=harness==="claude"?967000:harness==="codex"?570000:258400;
    await act(async()=>{pending.find(p=>p.harness===harness)!.resolve({...t0942Capacity(harness),C,budget:{status:"known",reason:null,W:600000,C,E:Math.min(C,600000),P:Math.floor(Math.min(C,600000)*.94)}});});
    expect(screen.getByTestId(`capacity-${harness}`)).toHaveTextContent(`${C}`);
  }
  expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(3);
});
it("T0942_AT1_old_engine_is_an_idle_action_not_an_automatic_retry_loop",async()=>{
  vi.useFakeTimers({shouldAdvanceTime:true});
  const capacity=vi.fn(async(_path:string,_harness:string,_check=false)=>({error:oldGeneration}));
  render(<App bridge={makeBridge({status:vi.fn(async()=>t0942Status()),ctx_capacity:capacity})}/>);
  await waitFor(()=>expect(capacity).toHaveBeenCalled());await t0942Open();
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("엔진 갱신 후 현재 설정 검증 필요"));
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000);});
  expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(1);
  expect(screen.getByTestId("budget-codex")).toHaveTextContent(oldGeneration.message);
  expect(screen.getByTestId("budget-codex")).not.toHaveTextContent("자동 재시도");
});

it("T0942_AT1_backoff_retains_query_error_after_a_changed_pure_check",async()=>{
  vi.useFakeTimers({shouldAdvanceTime:true});let failed=false;
  const capacity=vi.fn(async(_path:string,harness:string,check=false):Promise<CtxCapacity>=>{
    if(check) return t0942Capacity(harness,!failed);
    failed=true;
    return {...t0942Capacity(harness,false),status:"unknown",C:null,reason:"catalog transport failed",verification:{status:"unknown",reason:"catalog transport failed",verified_at:null,revision:null,evidence_changed:false}};
  });
  render(<App bridge={makeBridge({status:vi.fn(async()=>t0942Status(!failed)),ctx_capacity:capacity})}/>);
  await waitFor(()=>expect(capacity).toHaveBeenCalled());await t0942Open();
  await waitFor(()=>expect(screen.getByTestId("budget-codex")).toHaveTextContent("catalog transport failed"));
  await act(async()=>{await vi.advanceTimersByTimeAsync(15000);});
  expect(capacity.mock.calls.filter(c=>!c[2])).toHaveLength(2);
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("자동 재시도 대기");
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("최근 조회 실패 (catalog transport failed)");
  expect(screen.getByTestId("budget-codex")).toHaveTextContent("다음 시도");
  expect(screen.getByTestId("budget-codex")).not.toHaveTextContent("현재 설정 검증 완료");
});
