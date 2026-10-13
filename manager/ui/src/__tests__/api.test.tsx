/**
 * 화면이 파이썬을 부르는 자리 — HTTP 하나다 (T-0921 AT-003).
 *
 * 창 주입 브리지를 흉내 내는 대역은 없다. 창도 브라우저도 같은 `POST /api/<method>` 을 부르고,
 * 다른 손님의 저장·적용은 `GET /events` 알림으로 온다.
 */
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { Bridge, ChangeEvent, StatusDocument } from "../api";
import { chooseFolder, httpBridge, isWindowClient, subscribeChanges } from "../api";
import fixture from "../fixtures/status.sample.json";

const DEMO = "/projects/demo";

function jsonOnce(payload: unknown) {
  return vi.fn(async () => ({ json: async () => payload })) as unknown as typeof fetch;
}

function openedStream(): EventTarget & { closed: boolean; url: string } {
  const opened = (globalThis.EventSource as unknown as {
    opened: (EventTarget & { closed: boolean; url: string })[] }).opened;
  return opened[opened.length - 1];
}

beforeEach(() => {
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("HTTP 배선", () => {
  it("이름 하나를 POST /api/<method> 로 한 번 부르고 응답 JSON 을 그대로 돌려준다", async () => {
    const saved = { changed: true, conf: { missing_required: [] } };
    globalThis.fetch = jsonOnce(saved);

    const response = await httpBridge().conf_save(DEMO, { "delegate.model": "opus" });

    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toBe("/api/conf_save");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ args: [DEMO, { "delegate.model": "opus" }] });
    expect(response).toEqual(saved);
  });

  it("창 손님은 여는 주소의 질의로 갈리고 그 표식이 요청 머리에 실린다", async () => {
    globalThis.fetch = jsonOnce({ path: null });
    expect(isWindowClient()).toBe(false);
    await httpBridge().status(DEMO, false, false);
    const browserCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0][1];
    expect(browserCall.headers["X-Manager-Client"]).toBe("browser");

    window.history.replaceState({}, "", "/?client=window");
    expect(isWindowClient()).toBe(true);
    await httpBridge().status(DEMO, false, false);
    const windowCall = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[1][1];
    expect(windowCall.headers["X-Manager-Client"]).toBe("window");
  });
});

describe("폴더 고르기", () => {
  it("창이면 OS 대화상자를, 브라우저면 경로 직접 입력을 쓴다", async () => {
    const bridge = { pick_folder: vi.fn(async () => ({ path: DEMO })) } as unknown as Bridge;
    const prompt = vi.spyOn(window, "prompt").mockReturnValue(` ${DEMO} `);

    expect(await chooseFolder(bridge)).toEqual({ path: DEMO });
    expect(prompt).toHaveBeenCalledTimes(1);
    expect(bridge.pick_folder).not.toHaveBeenCalled();

    window.history.replaceState({}, "", "/?client=window");
    expect(await chooseFolder(bridge)).toEqual({ path: DEMO });
    expect(bridge.pick_folder).toHaveBeenCalledTimes(1);
    expect(prompt).toHaveBeenCalledTimes(1);
  });

  it("브라우저에서 취소하면 경로가 없다", async () => {
    vi.spyOn(window, "prompt").mockReturnValue(null);

    expect(await chooseFolder({} as Bridge)).toEqual({ path: null });
  });
});

describe("서버 알림", () => {
  it("스트림 하나를 열고 알림을 그대로 넘기며, 구독을 끝내면 닫는다", () => {
    const seen: ChangeEvent[] = [];
    const stop = subscribeChanges((event) => seen.push(event));

    openedStream().dispatchEvent(new MessageEvent("message", {
      data: JSON.stringify({ name: "conf_save", path: DEMO }),
    }));

    expect(openedStream().url).toBe("/events");
    expect(seen).toEqual([{ name: "conf_save", path: DEMO }]);

    stop();
    expect(openedStream().closed).toBe(true);
  });

  it("알림을 받으면 화면이 다시 읽는다 — 같은 HTTP 자리 하나로", async () => {
    globalThis.fetch = vi.fn(async (url: string) => ({
      json: async () => (url === "/api/list_projects"
        ? { projects: [{ path: DEMO, name: "demo" }] }
        : url === "/api/status" ? (fixture as unknown as StatusDocument)
        : url === "/api/latest_tags" ? { projects: {}, upstreams: {} }
        : url === "/api/conf_values" ? { values: {} }
        : { schema: 3, groups: [], keys: [] }),
    })) as unknown as typeof fetch;
    render(<App bridge={httpBridge()} />);
    const statusCalls = () => (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      .filter((call) => call[0] === "/api/status").length;
    await waitFor(() => expect(statusCalls()).toBeGreaterThan(0));
    const before = statusCalls();

    act(() => { openedStream().dispatchEvent(new MessageEvent("message", {
      data: JSON.stringify({ name: "update_apply", path: DEMO }),
    })); });

    await waitFor(() => expect(statusCalls()).toBeGreaterThan(before));
  });
});
