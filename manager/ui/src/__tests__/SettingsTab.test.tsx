import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Bridge, ConfKeysDocument, ConfSection, ConfValuesDocument } from "../api";
import confKeys from "../fixtures/conf-keys.sample.json";
import SettingsTab, { buildSections, EXTRA_GROUP, FREE_INPUT_FAMILY } from "../tabs/SettingsTab";

/** 키 목록은 실 엔진 산출(`local_conf.py --keys --json`)이고 값·판정만 자리마다 바꾼다. */
const KEYS = confKeys as unknown as ConfKeysDocument;

const DEMO = "/projects/demo";
const VALUES: Record<string, string> = {
  "runtime.py": "python3",
  "ctx.nudge_pct": "25",
  "delegate_enabled": "true",
  "nope.nope": "1",
  "upstream.path": "/projects/framework",
};
const CONF: ConfSection = {
  missing_required: ["test.cmd"],
  legacy: [{ key: "delegate_enabled", replacement: "delegate.enabled" }],
  unknown: ["nope.nope"],
  nonstandard: [{ key: "ctx.nudge_pct", value: "25", default: "30" }],
  missing_for_latest: ["qa.platforms"],
};

function makeBridge(overrides: Partial<Bridge> = {}): Bridge {
  const values: ConfValuesDocument = { values: VALUES, conf: CONF };
  return {
    conf_keys: vi.fn(async () => KEYS),
    conf_values: vi.fn(async () => values),
    conf_save: vi.fn(async () => ({ changed: true, conf: CONF })),
    conf_reset: vi.fn(async () => ({ changed: true, conf: CONF })),
    adapters: vi.fn(async () => ({ source: "/projects/framework", files: [] })),
    adapter_accept: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })),
    adapter_matrix: vi.fn(async () => ({})),
    upstream_show: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })),
    upstream_set: vi.fn(async () => ({ rc: 1, stdout: "", stderr: "[중단] upstream 값 거부" })),
    ...overrides,
  } as unknown as Bridge;
}

async function mounted(bridge: Bridge) {
  render(<SettingsTab bridge={bridge} path={DEMO} />);
  await screen.findByTestId("settings-banner");
}

afterEach(cleanup);

describe("항목 목록 구성", () => {
  it("패턴 키군은 전개된 실명으로 펼쳐지고 conf 에만 있는 키는 따로 모인다", () => {
    const sections = buildSections(KEYS, VALUES);
    const keys = sections.flatMap((section) => section.rows.map((row) => row.key));

    expect(keys).toContain("diff_cap.large");             // 전개된 패턴 멤버
    expect(keys).toContain("delegate.developer.hard.model");
    expect(keys).not.toContain(FREE_INPUT_FAMILY);        // 군 이름 자체는 행이 아니다
    const extra = sections.find((section) => section.group === EXTRA_GROUP);
    expect(extra?.rows.map((row) => row.key)).toEqual(["delegate_enabled", "nope.nope"]);
  });

  it("절 순서는 레지스트리가 준 그룹 순서 그대로다", () => {
    const sections = buildSections(KEYS, {});

    expect(sections.map((section) => section.group))
      .toEqual(KEYS.groups.filter((group) => sections.some((s) => s.group === group)));
    expect(sections[0].group).toBe("project");
  });
});

describe("다섯 목록의 배너와 행 표시", () => {
  it("배너는 넷을 세고 `갱신하면 필요` 는 모름(null)과 없음([])을 가른다", async () => {
    await mounted(makeBridge());
    expect(screen.getByTestId("settings-banner"))
      .toHaveTextContent("필수 누락 1 · 폐지 1 · 표준과 다른 값 1 · 갱신하면 필요 1");
    cleanup();

    const unknownLatest: ConfSection = { ...CONF, missing_for_latest: null };
    await mounted(makeBridge({
      conf_values: vi.fn(async () => ({ values: VALUES, conf: unknownLatest })),
    }));
    expect(screen.getByTestId("settings-banner")).toHaveTextContent("갱신하면 필요 모름");
  });

  it("행마다 필수 누락·폐지·레지스트리 밖·갱신하면 필요를 표시한다", async () => {
    await mounted(makeBridge());

    expect(screen.getByTestId("conf-test.cmd")).toHaveTextContent("! 필수 누락");
    expect(screen.getByTestId("conf-delegate_enabled"))
      .toHaveTextContent("폐지 · 대체 delegate.enabled");
    expect(screen.getByTestId("conf-nope.nope")).toHaveTextContent("레지스트리 밖");
    expect(screen.getByTestId("conf-qa.platforms")).toHaveTextContent("갱신하면 필요");
  });

  it("[기본값으로] 는 기본값이 선언되고 값이 그와 다른 행에만 있다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);

    fireEvent.click(screen.getByTestId("conf-ctx.nudge_pct").querySelector("button")!);
    await waitFor(() => expect(bridge.conf_reset).toHaveBeenCalledWith(DEMO, "ctx.nudge_pct"));
    // 버튼은 사본이 "표준과 다름" 이라 한 행에만 있다 — 기본값 없는 키도, 판정 밖 키도 없다.
    expect(screen.getByTestId("conf-project.name").querySelector("button")).toBeNull();
    expect(screen.getByTestId("conf-ctx.stop_pct").querySelector("button")).toBeNull();
    expect(screen.getByTestId("conf-ctx.window_tokens")).toHaveTextContent("기본 200000");
  });
});

describe("저장", () => {
  it("변경 수를 세고 바뀐 키만 보낸 뒤 **재조회** 값을 보인다", async () => {
    const after: ConfValuesDocument = {
      values: { ...VALUES, "ctx.nudge_pct": "27" },      // 사본이 다시 낸 값
      conf: { ...CONF, nonstandard: [] },
    };
    const conf_values = vi.fn(async () => after);
    const bridge = makeBridge({ conf_values: vi.fn(async () => ({ values: VALUES, conf: CONF })) });
    await mounted(bridge);
    expect(screen.getByTestId("save")).toBeDisabled();

    fireEvent.change(screen.getByLabelText("ctx.nudge_pct"), { target: { value: "26" } });
    expect(screen.getByTestId("save")).toHaveTextContent("저장 (1 변경)");
    (bridge as { conf_values: unknown }).conf_values = conf_values;
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { "ctx.nudge_pct": "26" }));
    // 화면 값은 타이핑한 26 이 아니라 재조회가 준 27 이다(낙관적 갱신 없음).
    await waitFor(() => expect(screen.getByLabelText("ctx.nudge_pct")).toHaveValue("27"));
    expect(conf_values).toHaveBeenCalledTimes(1);
  });

  it("[되돌리기] 는 저장을 부르지 않고 편집을 지운다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);

    fireEvent.change(screen.getByLabelText("ctx.nudge_pct"), { target: { value: "26" } });
    fireEvent.click(screen.getByText("되돌리기"));

    expect(screen.getByLabelText("ctx.nudge_pct")).toHaveValue("25");
    expect(bridge.conf_save).not.toHaveBeenCalled();
  });

  it("어댑터 카드에 박히는 키를 저장하면 갱신 탭 안내 한 줄이 뜬다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);

    fireEvent.change(screen.getByLabelText("runtime.py"), { target: { value: "python3.12" } });
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(screen.getByTestId("settings-note"))
      .toHaveTextContent("갱신 탭의 [적용]"));
  });

  it("자유 입력이 열리는 자리는 `test.<platform>.cmd` 한 군뿐이다", async () => {
    await mounted(makeBridge());

    const inputs = screen.getAllByTestId("new-key-input");
    expect(inputs).toHaveLength(1);
    expect(screen.getByTestId(`new-key-runtime`)).toHaveTextContent(FREE_INPUT_FAMILY);

    fireEvent.change(inputs[0], { target: { value: "windows" } });
    fireEvent.click(screen.getByText("항목 추가"));
    expect(screen.getByTestId("save")).toHaveTextContent("저장 (1 변경)");
    // 레지스트리 밖이라 "그 밖의 키" 절에 선다 — 정오는 저장 뒤 사본의 `unknown` 이 말한다.
    expect(screen.getByLabelText("test.windows.cmd")).toBeInTheDocument();
    expect(screen.getByTestId(`group-${EXTRA_GROUP}`))
      .toContainElement(screen.getByTestId("conf-test.windows.cmd"));
  });
});

describe("질문형", () => {
  it("같은 항목을 순서대로 묻고 바뀐 것만 같은 저장 경로로 보낸다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);
    const order = buildSections(KEYS, VALUES).flatMap((section) => section.rows);

    fireEvent.click(screen.getByText("질문형으로"));

    expect(screen.getByTestId("question-key")).toHaveTextContent(order[0].key);
    fireEvent.change(screen.getByLabelText(`${order[0].key} 값`), { target: { value: "새 이름" } });
    fireEvent.keyDown(screen.getByLabelText(`${order[0].key} 값`), { key: "Enter" });
    expect(screen.getByTestId("question-key")).toHaveTextContent(order[1].key);

    for (let index = 1; index < order.length; index += 1) {
      fireEvent.keyDown(screen.getByLabelText(`${order[index].key} 값`), { key: "Enter" });
    }

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { [order[0].key]: "새 이름" }));
  });
});

describe("upstream", () => {
  it("전환은 사본 명령이고 거부 문구가 원문 그대로 화면에 온다", async () => {
    const bridge = makeBridge();
    await mounted(bridge);

    fireEvent.change(screen.getByLabelText("upstream 값"), { target: { value: "/nowhere" } });
    fireEvent.click(screen.getByText("upstream 설정"));

    await waitFor(() => expect(bridge.upstream_set).toHaveBeenCalledWith(DEMO, "/nowhere"));
    expect(screen.getByTestId("settings-command")).toHaveTextContent("[중단] upstream 값 거부");
    expect(bridge.conf_save).not.toHaveBeenCalled();      // conf writer 를 거치지 않는다
  });
});

describe("실패", () => {
  it("옛 세대 사본이면 값을 지어내지 않고 그 사본의 사유를 보인다", async () => {
    const bridge = makeBridge({
      conf_keys: vi.fn(async () => ({
        error: { kind: "old-generation" as const, message: "먼저 갱신하라.", stderr: "", rc: 2 },
      })),
    });
    render(<SettingsTab bridge={bridge} path={DEMO} />);

    expect(await screen.findByText("설정을 읽지 못했다 — 먼저 갱신하라.")).toBeInTheDocument();
  });
});
