import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import App, { LIGHT_INTERVAL_MS } from "../App";
import type { Bridge, BridgeError, StatusDocument } from "../api";
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
    ...overrides,
  } as Bridge;
}

function setVisibility(value: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => value });
  act(() => { document.dispatchEvent(new Event("visibilitychange")); });
}

function lightCalls(bridge: Bridge) {
  return (bridge.status as ReturnType<typeof vi.fn>).mock.calls
    .filter((call) => call[1] === false).length;
}

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
  it("선택 프로젝트의 세 조회를 전부 다시 돈다", async () => {
    const bridge = makeBridge();
    render(<App bridge={bridge} />);
    await waitFor(() => expect(bridge.latest_tags).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByLabelText("새로고침"));

    await waitFor(() => expect(bridge.latest_tags).toHaveBeenCalledTimes(2));
    expect(lightCalls(bridge)).toBe(2);
    expect((bridge.status as ReturnType<typeof vi.fn>).mock.calls
      .filter((call) => call[1] === true).length).toBe(2);
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
