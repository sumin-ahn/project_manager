import "@testing-library/jest-dom/vitest";

/**
 * jsdom 에는 `EventSource` 가 없다 — 서버 알림 스트림을 재려면 그 자리가 있어야 한다.
 * 열린 스트림을 모아 두어 테스트가 `dispatchEvent` 로 알림을 흘려보낸다(브라우저 API 대역).
 */
class TestEventSource extends EventTarget {
  static opened: TestEventSource[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    super();
    TestEventSource.opened.push(this);
    this.addEventListener("message", (event) => this.onmessage?.(event as MessageEvent));
  }

  close() {
    this.closed = true;
  }
}

Object.defineProperty(globalThis, "EventSource", {
  configurable: true, writable: true, value: TestEventSource,
});
