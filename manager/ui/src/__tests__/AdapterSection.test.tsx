import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdapterFile, AdapterResponse, Bridge, ConfSection, StatusDocument } from "../api";
import AdapterMatrix, { cellText } from "../components/AdapterMatrix";
import AdapterSection, { hasChannel } from "../components/AdapterSection";
import status from "../fixtures/status.sample.json";

const document_ = status as unknown as StatusDocument;
/** 소스가 해소된 호출의 판정 — 이 응답만 `missing_for_latest` 를 배열로 싣는다. */
const CONF: ConfSection = { ...document_.conf, missing_for_latest: ["qa.platforms"] };
/** 두 모양은 실 엔진 산출에서 가져온다 — 채널 있는 파일과 인스턴스 소유 문서. */
const SAMPLE: AdapterResponse = { ...document_.adapters!, conf: CONF };
const CHANNEL = SAMPLE.files.find((file) => "mode" in file)!;
const DOCUMENT = SAMPLE.files.find((file) => !("mode" in file))!;

const DEMO = "/projects/demo";
const OTHER = "/projects/other";

const EDITED: AdapterFile = {
  ...CHANNEL,
  status: "edited",
  diff_lines: { added: 1, removed: 15 },
  diff_vs_baseline: "--- .claude/settings.json@6b8d8f74\n+++ .claude/settings.json\n-옛 줄",
  diff_vs_latest: "--- .claude/settings.json\n+++ .claude/settings.json@latest\n+새 줄",
  features: [
    { name: "autoCompactEnabled", project: null, latest: true, verdict: "missing" },
    { name: "permissions.allow", project: 51, latest: 42, verdict: "differs" },
    { name: "hooks.PostToolUse", project: ".claude/run_tests_hook.sh", latest: null,
      verdict: "retired" },
    { name: "statusLine", project: 3, latest: 3, verdict: "same" },
  ],
  distance: 3,
};

const EDITED_DOCUMENT: AdapterResponse = {
  source: "/projects/framework", files: [EDITED, DOCUMENT], conf: CONF,
};

function makeBridge(overrides: Partial<Bridge> = {}): Bridge {
  return {
    adapters: vi.fn(async () => EDITED_DOCUMENT),
    adapter_accept: vi.fn(async () => ({
      rc: 0, stdout: "✓ 어댑터 config 수용: .claude/settings.json (백업 …)", stderr: "" })),
    adapter_matrix: vi.fn(async () => ({
      [DEMO]: SAMPLE,
      [OTHER]: EDITED_DOCUMENT,
    })),
    ...overrides,
  } as unknown as Bridge;
}

afterEach(cleanup);

describe("어댑터 절", () => {
  it("탭을 여는 것만으로는 조회하지 않는다 — [불러오기] 가 유일한 방아쇠", async () => {
    const bridge = makeBridge();
    render(<AdapterSection bridge={bridge} path={DEMO} onConfLoaded={vi.fn()} />);

    expect(bridge.adapters).not.toHaveBeenCalled();
    expect(screen.getByText("[불러오기] 를 눌러야 판정이 온다 (소스 해소가 무겁다)"))
      .toBeInTheDocument();

    fireEvent.click(screen.getByTestId("adapter-load"));
    await waitFor(() => expect(bridge.adapters).toHaveBeenCalledTimes(1));
    expect(bridge.adapters).toHaveBeenCalledWith(DEMO);
  });

  it("모양 둘을 가른다 — 인스턴스 소유 문서는 `mode` 키 자체가 없다", async () => {
    expect(hasChannel(CHANNEL)).toBe(true);
    expect(hasChannel(DOCUMENT)).toBe(false);
    render(<AdapterSection bridge={makeBridge()} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));

    const row = await screen.findByTestId(`adapter-${DOCUMENT.path}`);
    expect(row).toHaveTextContent("인스턴스 소유 · 검사 없음");
    expect(row.textContent).not.toContain("최신과");
  });

  it("채널 있는 파일은 상태·기준 버전·거리·편집 줄 수를 한 줄로 보인다", async () => {
    render(<AdapterSection bridge={makeBridge()} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));

    const row = await screen.findByTestId(`adapter-${EDITED.path}`);
    expect(row).toHaveTextContent("claude");
    expect(row).toHaveTextContent("채택자 편집");
    expect(row).toHaveTextContent(EDITED.template_rev!.slice(0, 8));
    expect(row).toHaveTextContent("최신과 3개 다름 · 편집 +1/-15");
  });

  it("[기능 보기] 는 같음/없음/다름/폐지됨 넷을 그대로 보인다", async () => {
    render(<AdapterSection bridge={makeBridge()} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));
    fireEvent.click(await screen.findByText("기능 보기"));

    const table = screen.getByTestId(`features-${EDITED.path}`);
    expect(table).toHaveTextContent("autoCompactEnabled");
    expect(table).toHaveTextContent("없음");
    expect(table).toHaveTextContent("다름");
    expect(table).toHaveTextContent("폐지됨");
    expect(table).toHaveTextContent("같음");
  });

  it("[차이] 는 unified diff 둘(설치 기준↔현재 · 현재↔최신)을 보인다", async () => {
    render(<AdapterSection bridge={makeBridge()} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));
    fireEvent.click(await screen.findByText("차이"));

    const panel = screen.getByTestId(`diff-${EDITED.path}`);
    expect(panel).toHaveTextContent("설치 기준 ↔ 현재");
    expect(panel).toHaveTextContent("현재 ↔ 최신");
    expect(panel.textContent).toContain(EDITED.diff_vs_baseline);
    expect(panel.textContent).toContain(EDITED.diff_vs_latest);
  });

  it("[수용] 은 사본 명령을 부르고 그 산출을 보인 뒤 다시 조회한다", async () => {
    const bridge = makeBridge();
    render(<AdapterSection bridge={bridge} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));
    fireEvent.click(await screen.findByText("수용"));

    await waitFor(() => expect(bridge.adapter_accept)
      .toHaveBeenCalledWith(DEMO, EDITED.path));
    expect(screen.getByTestId("adapter-outcome")).toHaveTextContent("어댑터 config 수용");
    await waitFor(() => expect(bridge.adapters).toHaveBeenCalledTimes(2));
  });

  it("수용 거부는 사본의 문구 그대로 남고 다시 조회하지 않는다", async () => {
    const bridge = makeBridge({
      adapter_accept: vi.fn(async () => ({
        rc: 1, stdout: "", stderr: "[중단] 어댑터 config 수용 거부 (.claude/settings.json)" })),
    });
    render(<AdapterSection bridge={bridge} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));
    fireEvent.click(await screen.findByText("수용"));

    await waitFor(() => expect(screen.getByTestId("adapter-outcome"))
      .toHaveTextContent("[중단] 어댑터 config 수용 거부"));
    expect(bridge.adapters).toHaveBeenCalledTimes(1);
  });

  it("소스를 해소하지 못하면 사유(stderr 원문)를 그대로 보인다", async () => {
    const bridge = makeBridge({
      adapters: vi.fn(async () => ({
        error: { kind: "old-generation" as const, message: "먼저 갱신하라.",
                 stderr: "[중단] 어댑터 config 소스 미해소", rc: 1 },
      })),
    });
    render(<AdapterSection bridge={bridge} path={DEMO} onConfLoaded={vi.fn()} />);
    fireEvent.click(screen.getByTestId("adapter-load"));

    expect(await screen.findByRole("alert")).toHaveTextContent("[중단] 어댑터 config 소스 미해소");
  });
});

describe("전체 보기 매트릭스", () => {
  it("칸 값은 거리이고 채널 없는 파일은 거리 축이 없다", () => {
    expect(cellText(EDITED_DOCUMENT, EDITED.path)).toBe("3");
    expect(cellText(EDITED_DOCUMENT, DOCUMENT.path)).toBe("검사 없음");
    expect(cellText(EDITED_DOCUMENT, ".codex/config.toml")).toBe("없음");
  });

  it("버튼으로만 돌고 파일 × 프로젝트 칸을 채운다", async () => {
    const bridge = makeBridge();
    render(<AdapterMatrix bridge={bridge} onClose={() => undefined} />);
    expect(bridge.adapter_matrix).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId("matrix-load"));

    await waitFor(() => expect(bridge.adapter_matrix).toHaveBeenCalledTimes(1));
    expect(screen.getByTestId(`cell-demo-${EDITED.path}`)).toHaveTextContent("0");
    expect(screen.getByTestId(`cell-other-${EDITED.path}`)).toHaveTextContent("3");
    expect(screen.getByTestId(`cell-demo-${DOCUMENT.path}`)).toHaveTextContent("검사 없음");
  });

  it("프로젝트 하나가 실패해도 나머지 칸은 그대로 온다", async () => {
    const bridge = makeBridge({
      adapter_matrix: vi.fn(async () => ({
        [DEMO]: SAMPLE,
        [OTHER]: { error: { kind: "old-generation" as const, message: "먼저 갱신하라.",
                            stderr: "", rc: 2 } },
      })),
    });
    render(<AdapterMatrix bridge={bridge} onClose={() => undefined} />);
    fireEvent.click(screen.getByTestId("matrix-load"));

    await waitFor(() => expect(screen.getByTestId(`cell-other-${EDITED.path}`))
      .toHaveTextContent("오류"));
    expect(screen.getByTestId(`cell-demo-${EDITED.path}`)).toHaveTextContent("0");
  });
});
