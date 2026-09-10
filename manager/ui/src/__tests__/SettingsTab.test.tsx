import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Entry, SettingsEntry } from "../App";
import type {
  Bridge, ConfKeyMember, ConfKeysDocument, ConfSection, StatusDocument,
} from "../api";
import confKeys from "../fixtures/conf-keys.sample.json";
import { foldable, ROW_UNIT_FAMILY } from "../lib/settings-rows";
import SettingsTab, {
  ADAPTER_NAV, buildSections, EXTRA_GROUP, FREE_INPUT_FAMILY, UPSTREAM_PATH_KEY,
} from "../tabs/SettingsTab";

/** 키 목록은 실 엔진 산출(`local_conf.py --keys --json`)이고 값·판정만 자리마다 바꾼다. */
const KEYS = confKeys as unknown as ConfKeysDocument;

/**
 * 위임 절이 쓰는 것은 전부 이 픽스처에서 꺼낸다 — 역할·티어·하네스·모델 이름을 테스트도 적지
 * 않는다(적으면 화면 상수 0 을 재는 눈이 같이 멀어진다).
 */
const DELEGATE = KEYS.keys.find((entry) => entry.key === ROW_UNIT_FAMILY)!;
const CELLS = DELEGATE.members as ConfKeyMember[];
const ROW_NAME = CELLS[0].key.slice(0, CELLS[0].key.lastIndexOf("."));
const rowNameOf = (key: string) => key.slice(0, key.lastIndexOf("."));
const IN_ROW = CELLS.filter((cell) => rowNameOf(cell.key) === ROW_NAME);
const HARNESS_CELL = IN_ROW.find((cell) => cell.choices?.depends_on === null)!;
const MODEL_CELL = IN_ROW.find((cell) => cell.choices?.fetch !== null)!;
const REASONING_CELL = IN_ROW.find(
  (cell) => cell.choices?.depends_on !== null && cell.choices?.fetch === null)!;
const HARNESSES = HARNESS_CELL.choices!.values[""];
const REASONING = REASONING_CELL.choices!.values;

const DEMO = "/projects/demo";
const VALUES: Record<string, string> = {
  "runtime.py": "python3",
  "ctx.nudge_pct": "25",
  "delegate_enabled": "true",
  "nope.nope": "1",
  "upstream.path": "/projects/framework",
  [HARNESS_CELL.key]: HARNESSES[0],          // 값이 있는 위임 행 — 접히지 않는다
};
const CONF: ConfSection = {
  missing_required: ["test.cmd"],
  legacy: [{ key: "delegate_enabled", replacement: "delegate.enabled" }],
  unknown: ["nope.nope"],
  nonstandard: [{ key: "ctx.nudge_pct", value: "25", default: "30" }],
  // 값·판정을 늘 나르는 두 조회(가벼운/무거운 절)는 소스를 해소하지 않아 이 축이 null 이다.
  missing_for_latest: null,
};
/** 소스가 해소된 조회(어댑터 절)의 판정 — 다섯 번째 목록이 실린 유일한 자리다. */
const LATEST: ConfSection = { ...CONF, missing_for_latest: ["qa.platforms"] };

/** 사본이 답한 모델 목록 — 이름은 화면이 해석하지 않는다(엔진 문자열 그대로 그린다). */
const MODELS = ["model-a", "model-b"];

function makeBridge(overrides: Partial<Bridge> = {}): Bridge {
  return {
    conf_keys: vi.fn(async () => KEYS),
    conf_values: vi.fn(async () => ({ values: VALUES })),
    conf_save: vi.fn(async () => ({ changed: true, conf: CONF })),
    conf_reset: vi.fn(async () => ({ changed: true, conf: CONF })),
    delegate_models: vi.fn(async (_path: string, harness: string) =>
      ({ harness, kind: "alias", models: MODELS })),
    adapters: vi.fn(async () => ({ source: "/projects/framework", files: [], conf: LATEST })),
    adapter_accept: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })),
    adapter_matrix: vi.fn(async () => ({})),
    upstream_show: vi.fn(async () => ({ rc: 0, stdout: "", stderr: "" })),
    upstream_set: vi.fn(async () => ({ rc: 1, stdout: "", stderr: "[중단] upstream 값 거부" })),
    ...overrides,
  } as unknown as Bridge;
}

/** 가벼운 현황 조회가 나르는 판정 — 설정 탭은 이 `conf` 절을 그대로 쓴다(재조회 0). */
function lightEntry(conf: ConfSection = CONF): Entry {
  return { status: { conf } as StatusDocument };
}

function settingsEntry(values: Record<string, string> = VALUES): SettingsEntry {
  return { keys: KEYS, values };
}

function mounted(bridge: Bridge, startAsking = false, adapterConf: ConfSection | null = null,
                 settings: SettingsEntry = settingsEntry(), light: Entry | undefined = lightEntry()) {
  const onAdapterConf = vi.fn();
  const onReload = vi.fn();
  const view = render(
    <SettingsTab bridge={bridge} path={DEMO} light={light} settings={settings}
                 onReload={onReload} startAsking={startAsking}
                 adapterConf={adapterConf} onAdapterConf={onAdapterConf} />);
  return { onAdapterConf, onReload, view };
}

/** 왼쪽 목록에서 절 하나를 고른다 — 오른쪽에는 그 절의 행만 있다. */
function openGroup(group: string) {
  fireEvent.click(screen.getByTestId(`nav-${group}`));
}

afterEach(cleanup);
// 보기 선택은 다음 실행까지 남는 값이라 테스트 사이로 새면 안 된다.
afterEach(() => window.localStorage.clear());

describe("항목 목록 구성", () => {
  it("패턴 키군은 전개된 실명으로 펼쳐지고 conf 에만 있는 키는 따로 모인다", () => {
    const sections = buildSections(KEYS, VALUES);
    const keys = sections.flatMap((section) => section.rows.map((row) => row.key));

    expect(keys).toContain("diff_cap.large");             // 전개된 패턴 멤버
    expect(keys).toContain(MODEL_CELL.key);
    expect(keys).not.toContain(FREE_INPUT_FAMILY);        // 군 이름 자체는 행이 아니다
    const extra = sections.find((section) => section.group === EXTRA_GROUP);
    expect(extra?.rows.map((row) => row.key)).toEqual(["delegate_enabled", "nope.nope"]);
  });

  it("upstream.path 는 표에도 질문형에도 행이 없다 — 그 자리는 upstream 절 하나다", () => {
    const keys = buildSections(KEYS, VALUES).flatMap((section) => section.rows)
      .map((row) => row.key);
    const declared = KEYS.keys.find((entry) => entry.key === UPSTREAM_PATH_KEY)!;
    expect(keys).not.toContain(UPSTREAM_PATH_KEY);
    // 레지스트리가 아는 키라 "그 밖의 키" 로도 내려가지 않는다.
    const extra = buildSections(KEYS, VALUES).find((section) => section.group === EXTRA_GROUP);
    expect(extra?.rows.map((row) => row.key)).toEqual(["delegate_enabled", "nope.nope"]);

    mounted(makeBridge());
    openGroup(declared.group);                            // 그 키가 선언된 절이 그 자리다

    expect(screen.queryByTestId(`conf-${UPSTREAM_PATH_KEY}`)).toBeNull();
    expect(screen.queryByLabelText(UPSTREAM_PATH_KEY)).toBeNull();
    // 값을 고치는 자리는 남아 있다 — 도달성 검증을 포함한 사본 명령을 부르는 절이다.
    expect(screen.getByLabelText("upstream 값")).toHaveValue(VALUES[UPSTREAM_PATH_KEY]);
  });

  it("질문형도 같은 목록이라 그 키를 묻지 않는다", () => {
    mounted(makeBridge(), true);

    const asked = buildSections(KEYS, VALUES).flatMap((section) => section.rows);
    expect(asked.map((row) => row.key)).not.toContain(UPSTREAM_PATH_KEY);
    expect(screen.getByTestId("question-key")).toHaveTextContent(asked[0].key);
  });

  it("절 순서는 레지스트리가 준 그룹 순서 그대로다", () => {
    const sections = buildSections(KEYS, {});

    expect(sections.map((section) => section.group))
      .toEqual(KEYS.groups.filter((group) => sections.some((s) => s.group === group)));
    expect(sections[0].group).toBe("project");
  });
});

describe("왼쪽 목록", () => {
  it("레지스트리 그룹 순서 + 그 밖의 키 + 어댑터 절이고 항목마다 배지 수가 붙는다", () => {
    mounted(makeBridge());

    const labels = [...screen.getByTestId("settings-nav").querySelectorAll("button")]
      .map((button) => button.textContent);
    const groups = buildSections(KEYS, VALUES).map((section) => section.group);
    expect(labels.map((label) => label!.replace(/\d+$/, "")))
      .toEqual([...groups, ADAPTER_NAV]);
    // 배지 수는 그 절에서 손봐야 할 행 수다 — 필수 누락 1(runtime) · 폐지 1 + 레지스트리 밖 1.
    expect(screen.getByTestId("nav-marks-runtime")).toHaveTextContent("1");
    expect(screen.getByTestId(`nav-marks-${EXTRA_GROUP}`)).toHaveTextContent("2");
    expect(screen.queryByTestId("nav-marks-ctx")).toBeNull();
  });

  it("첫 진입은 배지가 있는 첫 절이고, 고른 절의 행만 오른쪽에 있다", () => {
    mounted(makeBridge());

    expect(screen.getByTestId("group-runtime")).toBeInTheDocument();
    expect(screen.getByTestId("conf-test.cmd")).toBeInTheDocument();
    expect(screen.queryByTestId("conf-ctx.nudge_pct")).toBeNull();

    openGroup("ctx");

    expect(screen.queryByTestId("group-runtime")).toBeNull();
    expect(screen.getByTestId("conf-ctx.nudge_pct")).toBeInTheDocument();
  });

  it("설정 탭을 열고 절을 옮겨도 사본을 부르지 않는다 — 두 조회는 미리 받아 온 것이다", () => {
    const bridge = makeBridge();
    mounted(bridge);

    openGroup("ctx");
    openGroup(EXTRA_GROUP);

    expect(bridge.conf_keys).not.toHaveBeenCalled();
    expect(bridge.conf_values).not.toHaveBeenCalled();
    expect(bridge.delegate_models).not.toHaveBeenCalled();
  });
});

describe("다섯 목록의 배너와 행 표시", () => {
  it("배너는 넷을 세고 `갱신하면 필요` 는 모름(null)과 없음([])을 가른다", () => {
    mounted(makeBridge());
    expect(screen.getByTestId("settings-banner"))
      .toHaveTextContent("필수 누락 1 · 폐지 1 · 표준과 다른 값 1 · 갱신하면 필요 모름");
    cleanup();

    mounted(makeBridge(), false, LATEST);
    expect(screen.getByTestId("settings-banner")).toHaveTextContent("갱신하면 필요 1");
    cleanup();

    mounted(makeBridge(), false, { ...CONF, missing_for_latest: [] });
    expect(screen.getByTestId("settings-banner")).toHaveTextContent("갱신하면 필요 0");
  });

  it("[불러오기] 가 받아 온 판정이 다섯 번째 목록을 채운다 — 새 조회는 0이다", async () => {
    const bridge = makeBridge();
    const { onAdapterConf } = mounted(bridge);
    expect(screen.queryByText("갱신하면 필요")).toBeNull();       // 불러오기 전에는 모름이다

    openGroup(ADAPTER_NAV);
    fireEvent.click(screen.getByTestId("adapter-load"));

    await waitFor(() => expect(onAdapterConf).toHaveBeenCalledWith(LATEST));
    expect(bridge.adapters).toHaveBeenCalledTimes(1);
    expect(bridge.conf_values).not.toHaveBeenCalled();             // 조회 횟수는 그대로다
  });

  it("행마다 필수 누락·폐지·레지스트리 밖·갱신하면 필요를 표시한다", () => {
    mounted(makeBridge(), false, LATEST);

    expect(screen.getByTestId("conf-test.cmd")).toHaveTextContent("! 필수 누락");
    expect(screen.getByTestId("conf-qa.platforms")).toHaveTextContent("갱신하면 필요");

    openGroup(EXTRA_GROUP);

    expect(screen.getByTestId("conf-delegate_enabled"))
      .toHaveTextContent("폐지 · 대체 delegate.enabled");
    expect(screen.getByTestId("conf-nope.nope")).toHaveTextContent("레지스트리 밖");
  });

  it("[기본값으로] 는 기본값이 선언되고 값이 그와 다른 행에만 있다", async () => {
    const bridge = makeBridge();
    const { onReload } = mounted(bridge);
    openGroup("ctx");

    fireEvent.click(screen.getByTestId("conf-ctx.nudge_pct").querySelector("button")!);
    await waitFor(() => expect(bridge.conf_reset).toHaveBeenCalledWith(DEMO, "ctx.nudge_pct"));
    await waitFor(() => expect(onReload).toHaveBeenCalledTimes(1));
    // 버튼은 사본이 "표준과 다름" 이라 한 행에만 있다 — 기본값 없는 키도, 판정 밖 키도 없다.
    expect(screen.getByTestId("conf-ctx.stop_pct").querySelector("button")).toBeNull();
    // 기본값은 따로 선 칸이 아니라 빈 칸 안에 있다 — 안 쓰면 무슨 값이 되는지가 칸에서 읽힌다.
    expect(screen.getByLabelText("ctx.window_tokens"))
      .toHaveAttribute("placeholder", "200000 (안 쓰면 이 값)");
    openGroup("project");
    fireEvent.click(screen.getByTestId("show-unset"));           // 접힘은 숨김이 아니다
    expect(screen.getByLabelText("project.name"))
      .toHaveAttribute("placeholder", "설정 안 함");
    expect(screen.getByTestId("conf-project.name").querySelector("button")).toBeNull();
  });
});

describe("저장", () => {
  it("변경 수를 세고 바뀐 키만 보낸 뒤 **재조회** 값을 보인다", async () => {
    const bridge = makeBridge();
    const { onReload, view } = mounted(bridge);
    openGroup("ctx");
    expect(screen.getByTestId("save")).toBeDisabled();

    fireEvent.change(screen.getByLabelText("ctx.nudge_pct"), { target: { value: "26" } });
    expect(screen.getByTestId("save")).toHaveTextContent("저장 (1 변경)");
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { "ctx.nudge_pct": "26" }));
    await waitFor(() => expect(onReload).toHaveBeenCalledTimes(1));
    expect(bridge.conf_values).not.toHaveBeenCalled();   // 재조회는 이 탭이 부르지 않는다

    // 화면 값은 타이핑한 26 이 아니라 재조회가 준 27 이다(낙관적 갱신 없음).
    view.rerender(
      <SettingsTab bridge={bridge} path={DEMO} light={lightEntry()}
                   settings={settingsEntry({ ...VALUES, "ctx.nudge_pct": "27" })}
                   onReload={onReload} startAsking={false}
                   adapterConf={null} onAdapterConf={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("ctx.nudge_pct")).toHaveValue("27"));
  });

  it("[되돌리기] 는 저장을 부르지 않고 편집을 지운다", () => {
    const bridge = makeBridge();
    mounted(bridge);
    openGroup("ctx");

    fireEvent.change(screen.getByLabelText("ctx.nudge_pct"), { target: { value: "26" } });
    fireEvent.click(screen.getByText("되돌리기"));

    expect(screen.getByLabelText("ctx.nudge_pct")).toHaveValue("25");
    expect(bridge.conf_save).not.toHaveBeenCalled();
  });

  it("어댑터 카드에 박히는 키를 저장하면 갱신 탭 안내 한 줄이 뜬다", async () => {
    const bridge = makeBridge();
    mounted(bridge);

    fireEvent.change(screen.getByLabelText("runtime.py"), { target: { value: "python3.12" } });
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(screen.getByTestId("settings-note"))
      .toHaveTextContent("갱신 탭의 [적용]"));
  });

  it("자유 입력이 열리는 자리는 `test.<platform>.cmd` 한 군뿐이다", () => {
    mounted(makeBridge());

    const inputs = screen.getAllByTestId("new-key-input");
    expect(inputs).toHaveLength(1);
    expect(screen.getByTestId("new-key-runtime")).toHaveTextContent(FREE_INPUT_FAMILY);

    fireEvent.change(inputs[0], { target: { value: "windows" } });
    fireEvent.click(screen.getByText("항목 추가"));
    expect(screen.getByTestId("save")).toHaveTextContent("저장 (1 변경)");
    // 레지스트리 밖이라 "그 밖의 키" 절에 선다 — 정오는 저장 뒤 사본의 `unknown` 이 말한다.
    // 갓 만든 행은 값이 없어 접기 대상이라, 만든 자리에서 그 절을 펼쳐 보여 준다.
    expect(screen.getByLabelText("test.windows.cmd")).toBeInTheDocument();
    expect(screen.getByTestId(`group-${EXTRA_GROUP}`))
      .toContainElement(screen.getByTestId("conf-test.windows.cmd"));
  });
});

describe("위임 절", () => {
  it("역할×티어 한 줄이고 칸 순서는 엔진이 준 순서다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);

    const role = screen.getByTestId(`role-${ROW_NAME}`);
    expect(role).toHaveTextContent(ROW_NAME);              // 행 라벨은 키 문자열 그대로다
    const labels = [...role.querySelectorAll("input,select")]
      .map((field) => field.getAttribute("aria-label"));
    expect(labels).toEqual(IN_ROW.map((cell) => cell.key));
  });

  it("추론 선택지는 그 행의 하네스 값에 따라 바뀐다", () => {
    const other = HARNESSES.find(
      (name) => REASONING[name].join() !== REASONING[HARNESSES[0]].join())!;
    mounted(makeBridge());
    openGroup(DELEGATE.group);

    const options = () => [...(screen.getByLabelText(REASONING_CELL.key) as HTMLSelectElement)
      .querySelectorAll("option")].map((option) => option.value).filter((value) => value !== "");
    expect(options()).toEqual(REASONING[HARNESSES[0]]);

    fireEvent.change(screen.getByLabelText(HARNESS_CELL.key), { target: { value: other } });

    expect(options()).toEqual(REASONING[other]);
  });

  it("하네스가 비면 고를 목록이 없고 그 자리에 무엇을 먼저 고를지 나온다", () => {
    mounted(makeBridge(), false, null, settingsEntry({}));   // 값이 하나도 없는 사본
    openGroup(DELEGATE.group);
    fireEvent.click(screen.getByTestId("show-unset"));

    const reasoning = screen.getByLabelText(REASONING_CELL.key) as HTMLSelectElement;
    expect([...reasoning.querySelectorAll("option")].filter((option) => option.value !== ""))
      .toHaveLength(0);
    const depends = REASONING_CELL.choices!.depends_on!;
    expect(screen.getAllByText(
      `먼저 ${depends.slice(depends.lastIndexOf(".") + 1)} 를 고르세요`).length).toBeGreaterThan(0);
    expect(screen.getByTestId(`models-${MODEL_CELL.key}`)).toBeDisabled();
  });

  it("모델 칸은 누르기 전에는 조회하지 않는다", async () => {
    const bridge = makeBridge();
    mounted(bridge);
    openGroup(DELEGATE.group);
    expect(bridge.delegate_models).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId(`models-${MODEL_CELL.key}`));

    await waitFor(() => expect(bridge.delegate_models)
      .toHaveBeenCalledWith(DEMO, HARNESSES[0]));
    expect(bridge.delegate_models).toHaveBeenCalledTimes(1);
    // 받아 온 목록이 그 칸의 선택지가 되고, 목록 밖 값을 칠 자리도 남는다.
    const select = await screen.findByLabelText(MODEL_CELL.key);
    expect([...(select as HTMLSelectElement).querySelectorAll("option")]
      .map((option) => option.value)).toEqual(["", ...MODELS, "직접 입력"]);
    expect(screen.getByTestId(`conf-${MODEL_CELL.key}`)).toHaveTextContent("alias");
  });

  it("모델 목록 조회 실패는 사본의 사유를 그대로 보인다", async () => {
    const bridge = makeBridge({
      delegate_models: vi.fn(async () => ({
        error: { kind: "old-generation" as const, message: "모델 목록을 내지 못했다",
                 stderr: "[중단] opencode 조회 실패", rc: 3 },
      })),
    });
    mounted(bridge);
    openGroup(DELEGATE.group);

    fireEvent.click(screen.getByTestId(`models-${MODEL_CELL.key}`));

    expect(await screen.findByText(/모델 목록을 내지 못했다/)).toBeInTheDocument();
    expect(screen.getByText(/\[중단\] opencode 조회 실패/)).toBeInTheDocument();
    // 값을 지어내지 않는다 — 그 칸은 직접 칠 수 있는 텍스트 입력으로 남는다.
    expect((screen.getByLabelText(MODEL_CELL.key) as HTMLElement).tagName).toBe("INPUT");
  });
});

describe("설정 안 함 접기", () => {
  it("네 축이 전부 참인 행만 접힌다", () => {
    const row = { key: "some.key", default: null, required: false };

    expect(foldable(row, "", new Set())).toBe(true);
    expect(foldable(row, "값", new Set())).toBe(false);                    // 값 있음
    expect(foldable({ ...row, required: true }, "", new Set())).toBe(false);   // 필수
    expect(foldable({ ...row, default: "30" }, "", new Set())).toBe(false);    // 기본값
    expect(foldable(row, "", new Set(["some.key"]))).toBe(false);          // 배지
  });

  it("설정 안 함 항목은 그룹 끝 한 줄로 접히고 값이 있는 행은 그대로 있다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);

    // 값을 넣은 행은 세 칸 중 하나만 차 있어도 세트로 보인다.
    expect(screen.getByTestId(`role-${ROW_NAME}`)).toBeInTheDocument();
    const other = CELLS.find((cell) => rowNameOf(cell.key) !== ROW_NAME)!;
    expect(screen.queryByTestId(`role-${rowNameOf(other.key)}`)).toBeNull();
    expect(screen.getByTestId(`folded-${DELEGATE.group}`)).toHaveTextContent("설정 안 함");
  });

  it("보기를 켜면 접혔던 행이 그대로 나오고 절마다 다시 누르지 않는다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);
    const other = CELLS.find((cell) => rowNameOf(cell.key) !== ROW_NAME)!;

    fireEvent.click(screen.getByTestId("show-unset"));

    expect(screen.getByTestId(`role-${rowNameOf(other.key)}`)).toBeInTheDocument();
    expect(screen.getByLabelText(other.key)).toBeInTheDocument();
    expect(screen.queryByTestId(`folded-${DELEGATE.group}`)).toBeNull();

    // 다른 절로 옮겨도 켠 상태가 따라간다 — 같은 조작을 반복하지 않는다.
    openGroup("project");
    expect(screen.queryByTestId("folded-project")).toBeNull();
  });

  it("보기 선택은 다음에 열 때도 유지된다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);
    fireEvent.click(screen.getByTestId("show-unset"));
    cleanup();

    mounted(makeBridge());
    openGroup(DELEGATE.group);
    expect((screen.getByTestId("show-unset") as HTMLInputElement).checked).toBe(true);
    expect(screen.queryByTestId(`folded-${DELEGATE.group}`)).toBeNull();
  });
});

describe("질문형", () => {
  it("같은 항목을 순서대로 묻고 바뀐 것만 같은 저장 경로로 보낸다", async () => {
    const bridge = makeBridge();
    mounted(bridge);
    const order = buildSections(KEYS, VALUES).flatMap((section) => section.rows);

    fireEvent.click(screen.getByText("질문형으로"));

    expect(screen.getByTestId("question-key")).toHaveTextContent(order[0].key);
    fireEvent.change(screen.getByLabelText(`${order[0].key} 값`), { target: { value: "새 이름" } });
    fireEvent.keyDown(screen.getByLabelText(`${order[0].key} 값`), { key: "Enter" });
    expect(screen.getByTestId("question-key")).toHaveTextContent(order[1].key);

    for (let index = 1; index < order.length; index += 1) {
      fireEvent.keyDown(screen.getByLabelText(`${order[index].key} 값`), { key: "Enter" });
    }

    await waitFor(() => expect(bridge.conf_save).toHaveBeenCalledTimes(1));
    const [, updates] = (bridge.conf_save as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(updates[order[0].key]).toBe("새 이름");
  });
});

describe("upstream", () => {
  it("전환은 사본 명령이고 거부 문구가 원문 그대로 화면에 온다", async () => {
    const bridge = makeBridge();
    mounted(bridge);
    openGroup(KEYS.keys.find((entry) => entry.key === UPSTREAM_PATH_KEY)!.group);

    fireEvent.change(screen.getByLabelText("upstream 값"), { target: { value: "/nowhere" } });
    fireEvent.click(screen.getByText("upstream 설정"));

    await waitFor(() => expect(bridge.upstream_set).toHaveBeenCalledWith(DEMO, "/nowhere"));
    expect(screen.getByTestId("settings-command")).toHaveTextContent("[중단] upstream 값 거부");
    expect(bridge.conf_save).not.toHaveBeenCalled();      // conf writer 를 거치지 않는다
  });
});

describe("실패", () => {
  it("옛 세대 사본이면 값을 지어내지 않고 그 사본의 사유를 보인다", () => {
    mounted(makeBridge(), false, null, {
      error: { kind: "old-generation", message: "먼저 갱신하라.", stderr: "", rc: 2 },
    });

    expect(screen.getByText("설정을 읽지 못했다 — 먼저 갱신하라.")).toBeInTheDocument();
  });

  it("판정을 나르는 가벼운 절이 없으면 배지를 지어내지 않는다", () => {
    mounted(makeBridge(), false, null, settingsEntry(),
            { error: { kind: "timeout", message: "10초 안에 응답이 없다", stderr: "", rc: null } });

    expect(screen.getByText("설정을 읽지 못했다 — 10초 안에 응답이 없다")).toBeInTheDocument();
  });
});
