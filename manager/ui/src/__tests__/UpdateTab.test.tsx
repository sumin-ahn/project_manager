import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Entry } from "../App";
import type { Bridge, ConfSection, SlotRow, StatusDocument } from "../api";
import fixture from "../fixtures/status.sample.json";
import UpdateTab, { busySessionCount } from "../tabs/UpdateTab";

/** 슬롯·conf 는 실 엔진 산출(`status --json`) 픽스처이고 태그·출력만 자리마다 바꾼다. */
const document_ = fixture as unknown as StatusDocument;
const DEMO = "/projects/demo";

/** 실측한 dry-run 출력 — 강조 대상 두 줄이 들어 있다(화면은 이 텍스트를 파싱하지 않는다). */
const DRY_RUN = [
  "[pm_update] 위임 기본값이 허용으로 바뀝니다 — 위임을 원치 않으면 local.conf 에 …",
  "  [update] .project_manager/tools/engine_rev.py",
  "[dry-run] 1 파일 변경 예정 (적용 안 함).",
  "→ 어댑터 config drift(보고 전용) .claude/settings.json — 채택자 133줄 ↔ template 133줄",
  "⚠️  인스턴스 소유 템플릿 변경 — 처방: pm-config sync-adapter-config --accept",
].join("\n");

function entry(overrides: Partial<StatusDocument> = {}): Entry {
  return { status: { ...document_, ...overrides }, at: 1 };
}

function makeBridge(overrides: Partial<Bridge> = {}): Bridge {
  return {
    update_preview: vi.fn(async () => ({ rc: 0, stdout: DRY_RUN, stderr: "" })),
    update_apply: vi.fn(async () => ({ rc: 0, stdout: "✓ 29 파일 동기화\n", stderr: "" })),
    ...overrides,
  } as unknown as Bridge;
}

function mount(bridge: Bridge, latestTag: string | null, light: Entry | undefined,
               onApplied = vi.fn(), adapterConf: ConfSection | null = null) {
  render(<UpdateTab bridge={bridge} path={DEMO} light={light} adapterConf={adapterConf}
                    latestTag={latestTag} onApplied={onApplied} />);
  return onApplied;
}

afterEach(cleanup);

describe("진행 중 세션 수", () => {
  it("리스를 쥔 살아 있는 행만 센다 — readonly(pid 0)와 idle 은 빠진다", () => {
    expect(busySessionCount(document_.slots)).toBe(1);
    expect(busySessionCount(document_.slots.filter((row) => row.role === "readonly"))).toBe(0);
    expect(busySessionCount([])).toBe(0);
  });
});

describe("현재 ↔ 최신 배지", () => {
  it("최신 태그가 높으면 갱신 있음, 같으면 최신", () => {
    mount(makeBridge(), "v2.0.0", entry());
    expect(screen.getByTestId("update-badge")).toHaveTextContent("갱신 있음 ↑");
    cleanup();

    mount(makeBridge(), "v1.7.13", entry());
    expect(screen.getByTestId("update-badge")).toHaveTextContent("최신");
  });

  it("최신 태그를 모르면 배지가 없다 (upstream 미등록·조회 실패)", () => {
    mount(makeBridge(), null, entry());

    expect(screen.queryByTestId("update-badge")).not.toBeInTheDocument();
    expect(screen.getByText("최신 태그 모름")).toBeInTheDocument();
  });

  it("갱신하면 필요한 설정 줄은 어댑터 절이 받아 온 값으로만 뜬다", () => {
    // 이 탭이 늘 보는 가벼운 절은 소스를 해소하지 않아 그 축이 null 이다 — 값이 실린 조회는
    // 설정 탭 [불러오기] 하나이고, 그 값이 여기까지 온다.
    expect(entry().status!.conf.missing_for_latest).toEqual([]);
    mount(makeBridge(), "v2.0.0", entry());
    expect(screen.queryByTestId("missing-for-latest")).not.toBeInTheDocument();
    cleanup();

    const loaded = { ...document_.conf, missing_for_latest: ["qa.platforms"] };
    mount(makeBridge(), "v2.0.0", entry(), vi.fn(), loaded);
    expect(screen.getByTestId("missing-for-latest")).toHaveTextContent("갱신하면 필요한 설정 1");
    cleanup();

    mount(makeBridge(), "v2.0.0", entry(), vi.fn(),
          { ...document_.conf, missing_for_latest: null });
    expect(screen.queryByTestId("missing-for-latest")).not.toBeInTheDocument();
  });
});

describe("미리보기", () => {
  it("엔진 출력을 줄 하나 빼지 않고 그대로 보인다", async () => {
    const bridge = makeBridge();
    mount(bridge, "v2.0.0", entry());

    fireEvent.click(screen.getByTestId("update-preview"));

    await waitFor(() => expect(bridge.update_preview).toHaveBeenCalledWith(DEMO));
    expect(screen.getByTestId("update-output")).toHaveTextContent(
      "[update] .project_manager/tools/engine_rev.py");
    expect(screen.getByTestId("update-output").textContent).toBe(DRY_RUN);
  });

  it("어댑터 drift 줄과 경고 줄만 눈에 띄게 한다 (값 해석 없음)", async () => {
    mount(makeBridge(), "v2.0.0", entry());
    fireEvent.click(screen.getByTestId("update-preview"));
    await screen.findByTestId("update-output");

    const highlighted = [...screen.getByTestId("update-output").querySelectorAll("span")]
      .filter((node) => node.className.includes("text-amber-700"))
      .map((node) => node.textContent);
    expect(highlighted).toEqual([
      "→ 어댑터 config drift(보고 전용) .claude/settings.json — 채택자 133줄 ↔ template 133줄",
      "⚠️  인스턴스 소유 템플릿 변경 — 처방: pm-config sync-adapter-config --accept",
    ]);
  });

  it("실패는 rc 와 원문 그대로다", async () => {
    const bridge = makeBridge({
      update_preview: vi.fn(async () => ({
        rc: 1, stdout: "", stderr: "오류: upstream 경로가 디렉토리가 아니거나 존재하지 않음: /x" })),
    });
    mount(bridge, "v2.0.0", entry());

    fireEvent.click(screen.getByTestId("update-preview"));

    expect(await screen.findByTestId("update-rc")).toHaveTextContent("rc 1");
    expect(screen.getByTestId("update-output")).toHaveTextContent("upstream 경로가 디렉토리가");
  });

  it("브리지 실패는 경고 한 줄이고 화면은 남는다", async () => {
    const bridge = makeBridge({
      update_preview: vi.fn(async () => ({
        error: { kind: "invalid-path" as const, message: "PM 홈이 아니다", stderr: "", rc: null },
      })),
    });
    mount(bridge, "v2.0.0", entry());

    fireEvent.click(screen.getByTestId("update-preview"));

    expect(await screen.findByRole("alert")).toHaveTextContent("PM 홈이 아니다");
    expect(screen.queryByTestId("update-output")).not.toBeInTheDocument();
  });
});

describe("적용", () => {
  it("진행 중인 세션이 없으면 바로 부르고 끝나면 세 조회를 다시 돌린다", async () => {
    const quiet = document_.slots.filter((row) => !row.alive) as SlotRow[];
    const bridge = makeBridge();
    const onApplied = mount(bridge, "v2.0.0", entry({ slots: quiet }));

    fireEvent.click(screen.getByTestId("update-apply"));

    await waitFor(() => expect(bridge.update_apply).toHaveBeenCalledWith(DEMO));
    expect(screen.queryByTestId("busy-confirm")).not.toBeInTheDocument();
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  });

  it("진행 중인 세션이 있으면 한 번 묻는다 — 그리고 [계속] 이 실제로 계속한다", async () => {
    const bridge = makeBridge();
    const onApplied = mount(bridge, "v2.0.0", entry());

    fireEvent.click(screen.getByTestId("update-apply"));

    expect(screen.getByTestId("busy-confirm")).toHaveTextContent("진행 중인 세션 1 — 계속?");
    expect(bridge.update_apply).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("busy-continue"));

    await waitFor(() => expect(bridge.update_apply).toHaveBeenCalledWith(DEMO));
    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
  });

  it("[취소] 는 부르지 않고 대화만 닫는다 (막는 것이 아니라 되묻는 것이다)", async () => {
    const bridge = makeBridge();
    mount(bridge, "v2.0.0", entry());
    fireEvent.click(screen.getByTestId("update-apply"));

    fireEvent.click(screen.getByTestId("busy-cancel"));

    expect(screen.queryByTestId("busy-confirm")).not.toBeInTheDocument();
    expect(bridge.update_apply).not.toHaveBeenCalled();
    expect(screen.getByTestId("update-apply")).not.toBeDisabled();
  });
});

describe("프로젝트 없음", () => {
  it("고른 프로젝트가 없으면 버튼을 두지 않는다", () => {
    render(<UpdateTab bridge={makeBridge()} path={null} light={undefined} adapterConf={null}
                      latestTag={null} onApplied={vi.fn()} />);

    expect(screen.queryByTestId("update-apply")).not.toBeInTheDocument();
  });
});
