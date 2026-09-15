import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Entry, SettingsEntry } from "../App";
import type {
  Bridge, ConfKeyMember, ConfKeysDocument, ConfSection, StatusDocument,
} from "../api";
import confKeys from "../fixtures/conf-keys.sample.json";
import { foldable, SET_TOGGLE } from "../lib/settings-rows";
import SettingsTab, {
  ADAPTER_NAV, buildSections, EXTRA_GROUP, FREE_INPUT_FAMILY, UPSTREAM_PATH_KEY,
} from "../tabs/SettingsTab";

/** 키 목록은 실 엔진 산출(`local_conf.py --keys --json`)이고 값·판정만 자리마다 바꾼다. */
const KEYS = confKeys as unknown as ConfKeysDocument;

/**
 * 위임 절이 쓰는 것은 전부 이 픽스처에서 꺼낸다 — 역할·티어·하네스·모델 이름을 테스트도 적지
 * 않는다(적으면 화면 상수 0 을 재는 눈이 같이 멀어진다). 한 줄로 묶이는 세트는 엔진이 행마다
 * 준 `unit` 이고, 그 멤버를 가진 군이 위임 군이다.
 */
const DELEGATE = KEYS.keys.find(
  (entry) => (entry.members ?? []).some((member) => member.unit !== null))!;
const CELLS = DELEGATE.members as ConfKeyMember[];
const ROW_NAME = CELLS[0].unit!;
const unitOf = (key: string) => CELLS.find((cell) => cell.key === key)!.unit!;
const IN_ROW = CELLS.filter((cell) => cell.unit === ROW_NAME);
const HARNESS_CELL = IN_ROW.find((cell) => cell.choices?.depends_on === null)!;
const MODEL_CELL = IN_ROW.find((cell) => cell.choices?.fetch !== null)!;
const REASONING_CELL = IN_ROW.find(
  (cell) => cell.choices?.depends_on !== null && cell.choices?.fetch === null)!;
const HARNESSES = HARNESS_CELL.choices!.values[""];
const REASONING = REASONING_CELL.choices!.values;

/** 교차 검토 세트 — 실명 키인데 세트로 묶이는 행. */
const REVIEWER = KEYS.keys.filter((entry) => entry.kind === "key" && entry.unit !== null);
const REVIEWER_UNIT = REVIEWER[0].unit!;
/** 교차 검토 도구가 잠그는 행(켜짐 목록 없음 = 값이 있으면 켜짐) 중 기본값이 있는 것. */
const REVIEWER_GOVERNED = KEYS.keys.find(
  (entry) => entry.kind === "key" && entry.governed_by?.on === null && entry.default !== null)!;
/** 위임 사용 스위치(켜짐 목록이 있는 스위치) 키. */
const DELEGATE_SWITCH = DELEGATE.governed_by!.key;

const toggleOf = (flag: string) => screen.getByLabelText(`${flag} ${SET_TOGGLE}`) as HTMLInputElement;

const DEMO = "/projects/demo";
const VALUES: Record<string, string> = {
  "runtime.py": "python3",
  "ctx.checkpoint_pct": "25",
  "delegate_enabled": "true",
  "nope.nope": "1",
  "upstream.path": "/projects/framework",
  [HARNESS_CELL.key]: HARNESSES[0],          // 값이 있는 위임 행 — 접히지 않는다
};
const CONF: ConfSection = {
  missing_required: ["test.cmd"],
  legacy: [{ key: "delegate_enabled", replacement: "delegate.enabled" }],
  unknown: ["nope.nope"],
  nonstandard: [{ key: "ctx.checkpoint_pct", value: "25", default: "30" }],
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

const savedUpdates = (bridge: Bridge) =>
  (bridge.conf_save as ReturnType<typeof vi.fn>).mock.calls[0][1] as Record<string, string | null>;

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

  it("upstream.path 는 표에도 질문형에도 행이 없다 — 그 자리는 원본 위치 절 하나다", () => {
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
    // 값을 고치는 자리는 남아 있다 — 받아올 수 있는지 확인하는 사본 명령을 부르는 절이다.
    expect(screen.getByLabelText(declared.label)).toHaveValue(VALUES[UPSTREAM_PATH_KEY]);
  });

  it("질문형도 같은 목록이라 그 키를 묻지 않는다", () => {
    mounted(makeBridge(), true);

    const asked = buildSections(KEYS, VALUES).flatMap((section) => section.rows);
    expect(asked.map((row) => row.key)).not.toContain(UPSTREAM_PATH_KEY);
    expect(screen.getByTestId("question-key")).toHaveAttribute("title", asked[0].key);
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
    const groups = buildSections(KEYS, VALUES)
      .map((section) => KEYS.group_labels[section.group] ?? section.group);
    expect(labels.map((label) => label!.replace(/\d+$/, "")))
      .toEqual([...groups, ADAPTER_NAV]);
    // 배지 수는 그 절에서 손봐야 할 행 수다 — 필수 누락 1(runtime) · 폐지 1 + 모르는 항목 1.
    expect(screen.getByTestId("nav-marks-runtime")).toHaveTextContent("1");
    expect(screen.getByTestId(`nav-marks-${EXTRA_GROUP}`)).toHaveTextContent("2");
    expect(screen.queryByTestId("nav-marks-ctx")).toBeNull();
  });

  it("첫 진입은 배지가 있는 첫 절이고, 고른 절의 행만 오른쪽에 있다", () => {
    mounted(makeBridge());

    expect(screen.getByTestId("group-runtime")).toBeInTheDocument();
    expect(screen.getByTestId("conf-test.cmd")).toBeInTheDocument();
    expect(screen.queryByTestId("conf-ctx.checkpoint_pct")).toBeNull();

    openGroup("ctx");

    expect(screen.queryByTestId("group-runtime")).toBeNull();
    expect(screen.getByTestId("conf-ctx.checkpoint_pct")).toBeInTheDocument();
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

  it("행마다 필수 누락·폐지·모르는 항목·갱신하면 필요를 표시한다", () => {
    mounted(makeBridge(), false, LATEST);

    expect(screen.getByTestId("conf-test.cmd")).toHaveTextContent("! 필수 누락");
    expect(screen.getByTestId("conf-qa.platforms")).toHaveTextContent("갱신하면 필요");

    openGroup(EXTRA_GROUP);

    expect(screen.getByTestId("conf-delegate_enabled"))
      .toHaveTextContent("폐지 · 대체 delegate.enabled");
    expect(screen.getByTestId("conf-nope.nope")).toHaveTextContent("모르는 항목");
  });

  it("[기본값으로] 는 기본값이 선언되고 값이 그와 다른 행에만 있다", async () => {
    const bridge = makeBridge();
    const { onReload } = mounted(bridge);
    openGroup("ctx");

    fireEvent.click(screen.getByText("기본값으로"));
    await waitFor(() => expect(bridge.conf_reset).toHaveBeenCalledWith(DEMO, "ctx.checkpoint_pct"));
    await waitFor(() => expect(onReload).toHaveBeenCalledTimes(1));
    // 버튼은 사본이 "표준과 다름" 이라 한 행에만 있다 — 기본값 없는 키도, 판정 밖 키도 없다.
    expect(screen.getAllByText("기본값으로")).toHaveLength(1);
  });

  it("직접 정하지 않은 칸은 입력이 아니라 안 정했을 때 일어나는 일을 한 줄로 보인다", () => {
    mounted(makeBridge());
    openGroup("ctx");

    // 기본값이 있는 키 — 그 값이 쓰인다고 말한다.
    expect(screen.queryByLabelText("ctx.window_tokens")).toBeNull();
    expect(screen.getByTestId("conf-ctx.window_tokens")).toHaveTextContent("기본값: 200000");
    openGroup("project");
    fireEvent.click(screen.getByTestId("show-unset"));           // 접힘은 숨김이 아니다
    // 기본값이 없는 키 — 엔진이 준 문장이 그대로 선다.
    expect(screen.getByTestId("conf-project.name")).toHaveTextContent(
      `기본 동작: ${KEYS.keys.find((entry) => entry.key === "project.name")!.unset!}`);
  });

  it("군 안에서 갈리는 이름이 있으면 그 이름이 선다 — 같은 이름 세 줄이 서지 않는다", () => {
    const bridge = makeBridge();
    mounted(bridge);
    openGroup("additional_reviewer");
    fireEvent.click(screen.getByTestId("show-unset"));
    const names = ["diff_cap.small", "diff_cap.medium", "diff_cap.large"]
      .map((key) => screen.getByTestId(`conf-${key}`).textContent ?? "");
    expect(new Set(names).size).toBe(3);
    expect(names[0]).toContain(
      KEYS.keys.find((entry) => entry.key.startsWith("diff_cap."))!
        .unit_labels["diff_cap.small"]);
  });

  it("행은 자기 키 문자열을 글자로 세우지 않는다 — 코드 좌표는 툴팁에만 남는다", () => {
    mounted(makeBridge());
    fireEvent.click(screen.getByTestId("show-unset"));

    for (const key of ["test.cmd", "qa.platforms"]) {
      expect(screen.getByTestId(`conf-${key}`)).not.toHaveTextContent(key);
    }
  });
});

describe("저장", () => {
  it("변경 수를 세고 바뀐 키만 보낸 뒤 **재조회** 값을 보인다", async () => {
    const bridge = makeBridge();
    const { onReload, view } = mounted(bridge);
    openGroup("ctx");
    expect(screen.getByTestId("save")).toBeDisabled();

    fireEvent.change(screen.getByLabelText("ctx.checkpoint_pct"), { target: { value: "26" } });
    expect(screen.getByTestId("save")).toHaveTextContent("저장 (1 변경)");
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { "ctx.checkpoint_pct": "26" }));
    await waitFor(() => expect(onReload).toHaveBeenCalledTimes(1));
    expect(bridge.conf_values).not.toHaveBeenCalled();   // 재조회는 이 탭이 부르지 않는다

    // 화면 값은 타이핑한 26 이 아니라 재조회가 준 27 이다(낙관적 갱신 없음).
    view.rerender(
      <SettingsTab bridge={bridge} path={DEMO} light={lightEntry()}
                   settings={settingsEntry({ ...VALUES, "ctx.checkpoint_pct": "27" })}
                   onReload={onReload} startAsking={false}
                   adapterConf={null} onAdapterConf={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("ctx.checkpoint_pct")).toHaveValue("27"));
  });

  it("[되돌리기] 는 저장을 부르지 않고 편집을 지운다", () => {
    const bridge = makeBridge();
    mounted(bridge);
    openGroup("ctx");

    fireEvent.change(screen.getByLabelText("ctx.checkpoint_pct"), { target: { value: "26" } });
    fireEvent.click(screen.getByText("되돌리기"));

    expect(screen.getByLabelText("ctx.checkpoint_pct")).toHaveValue("25");
    expect(bridge.conf_save).not.toHaveBeenCalled();
  });

  it("직접 정함을 끄면 저장이 그 줄을 지운다 — 빈 값을 적지 않는다", async () => {
    const bridge = makeBridge();
    mounted(bridge);
    openGroup("ctx");

    fireEvent.click(toggleOf("ctx.checkpoint_pct"));
    expect(screen.queryByLabelText("ctx.checkpoint_pct")).toBeNull();
    expect(screen.getByTestId("conf-ctx.checkpoint_pct")).toHaveTextContent("기본값: ");
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save)
      .toHaveBeenCalledWith(DEMO, { "ctx.checkpoint_pct": null }));
  });

  it("기본값 없는 키를 켜면 칸만 열리고 아무것도 적지 않는다", () => {
    mounted(makeBridge());
    fireEvent.click(screen.getByTestId("show-unset"));

    fireEvent.click(toggleOf("qa.platforms"));

    expect(screen.getByLabelText("qa.platforms")).toHaveValue("");
    expect(screen.getByTestId("save")).toBeDisabled();
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
    expect(screen.getByTestId("new-key-runtime")).toHaveTextContent(
      KEYS.keys.find((entry) => entry.key === FREE_INPUT_FAMILY)!.label);

    fireEvent.change(inputs[0], { target: { value: "windows" } });
    fireEvent.click(screen.getByText("항목 추가"));
    expect(screen.getByTestId("save")).toHaveTextContent("저장 (1 변경)");
    // 레지스트리 밖이라 "그 밖의 키" 절에 선다 — 정오는 저장 뒤 사본의 `unknown` 이 말한다.
    // 갓 만든 행은 편집 중이라 접히지 않고, 만든 자리에서 바로 값을 칠 수 있다.
    expect(screen.getByLabelText("test.windows.cmd")).toBeInTheDocument();
    expect(screen.getByTestId(`group-${EXTRA_GROUP}`))
      .toContainElement(screen.getByTestId("conf-test.windows.cmd"));
  });
});

describe("위임 절", () => {
  it("세트 한 줄이고 칸 순서는 엔진이 준 순서다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);

    const row = screen.getByTestId(`unit-${ROW_NAME}`);
    expect(row).toHaveTextContent(KEYS.units[ROW_NAME]);  // 엔진이 준 줄 이름
    expect(row).not.toHaveTextContent(ROW_NAME);          // 키 문자열은 화면에 없다
    const labels = [...row.querySelectorAll("input:not([type=checkbox]),select")]
      .map((field) => field.getAttribute("aria-label"));
    expect(labels).toEqual(IN_ROW.map((cell) => cell.key));
  });

  it("세트는 체크박스가 하나다 — 칸마다 두면 반쪽 세트가 저장된다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);

    expect(screen.getByTestId(`unit-${ROW_NAME}`)
      .querySelectorAll("input[type=checkbox]")).toHaveLength(1);
  });

  it("세트 줄 수는 엔진이 전개한 묶음 수 그대로다 — 화면이 역할·티어를 곱하지 않는다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);
    fireEvent.click(screen.getByTestId("show-unset"));

    const units = new Set(CELLS.map((cell) => cell.unit!));
    expect(screen.getByTestId(`group-${DELEGATE.group}`)
      .querySelectorAll("[data-testid^=unit-]")).toHaveLength(units.size);
    expect(Object.keys(KEYS.units)).toEqual(expect.arrayContaining([...units, REVIEWER_UNIT]));
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

  it("세트를 켜도 도구가 비면 고를 목록이 없고 그 자리에 무엇을 먼저 고를지 나온다", () => {
    mounted(makeBridge(), false, null, settingsEntry({}));   // 값이 하나도 없는 사본
    openGroup(DELEGATE.group);
    fireEvent.click(screen.getByTestId("show-unset"));
    expect(screen.queryByLabelText(REASONING_CELL.key)).toBeNull();   // 끈 세트는 칸이 없다

    fireEvent.click(toggleOf(ROW_NAME));

    const reasoning = screen.getByLabelText(REASONING_CELL.key) as HTMLSelectElement;
    expect([...reasoning.querySelectorAll("option")].filter((option) => option.value !== ""))
      .toHaveLength(0);
    const depends = REASONING_CELL.choices!.depends_on!;
    expect(screen.getAllByText(
      `먼저 ${KEYS.unit_fields[depends.slice(depends.lastIndexOf(".") + 1)]} 를 고르세요`)
      .length).toBeGreaterThan(0);
    expect(screen.getByTestId(`models-${MODEL_CELL.key}`)).toBeDisabled();
    expect(screen.getByTestId("save")).toBeDisabled();       // 켜기만 해서는 적는 것이 없다
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
    // 목록 원천의 종류(엔진 낱말)는 화면에 세우지 않는다 — 고르는 사람에게 쓸모가 없다.
    expect(screen.getByTestId(`conf-${MODEL_CELL.key}`)).not.toHaveTextContent("alias");
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

describe("교차 검토 세트와 절 스위치", () => {
  const TUPLE: Record<string, string> = {
    [REVIEWER.find((entry) => entry.choices?.depends_on === null)!.key]: HARNESSES[0],
    [REVIEWER.find((entry) => entry.choices?.fetch !== null)!.key]: MODELS[0],
  };

  it("세트를 끄면 적혀 있던 줄을 함께 지운다", async () => {
    const bridge = makeBridge();
    mounted(bridge, false, null, settingsEntry({ ...VALUES, ...TUPLE }));
    openGroup(REVIEWER[0].group);

    const row = screen.getByTestId(`unit-${REVIEWER_UNIT}`);
    expect(row.querySelectorAll("input[type=checkbox]")).toHaveLength(1);
    fireEvent.click(toggleOf(REVIEWER_UNIT));
    fireEvent.click(screen.getByTestId("save"));

    await waitFor(() => expect(bridge.conf_save).toHaveBeenCalledTimes(1));
    // 없던 줄(생각 깊이)은 건드리지 않는다 — 있던 두 줄만 지운다.
    expect(savedUpdates(bridge)).toEqual(
      Object.fromEntries(Object.keys(TUPLE).map((key) => [key, null])));
  });

  it("교차 검토 도구가 없으면 그 절의 나머지 칸이 잠기고 까닭 한 줄이 선다", () => {
    mounted(makeBridge());
    openGroup(REVIEWER_GOVERNED.group);

    expect(screen.getByTestId(`switched-off-${REVIEWER_GOVERNED.group}`))
      .toHaveTextContent(REVIEWER_GOVERNED.governed_by!.reason);
    expect(toggleOf(REVIEWER_GOVERNED.key)).toBeDisabled();
    expect(screen.queryByLabelText(REVIEWER_GOVERNED.key)).toBeNull();
    // 세트 자체는 잠기지도 접히지도 않는다 — 그걸 켜야 절이 풀리므로 늘 보인다.
    expect(toggleOf(REVIEWER_UNIT)).not.toBeDisabled();
    cleanup();

    mounted(makeBridge(), false, null, settingsEntry({ ...VALUES, ...TUPLE }));
    openGroup(REVIEWER_GOVERNED.group);
    expect(screen.queryByTestId(`switched-off-${REVIEWER_GOVERNED.group}`)).toBeNull();
    expect(toggleOf(REVIEWER_GOVERNED.key)).not.toBeDisabled();
  });

  it("위임 사용을 끄면 역할 세트가 잠긴다 — 값은 지우지 않고 회색으로 남는다", () => {
    mounted(makeBridge(), false, null, settingsEntry({ ...VALUES, [DELEGATE_SWITCH]: "false" }));
    openGroup(DELEGATE.group);

    expect(screen.getByTestId(`switched-off-${DELEGATE.group}`))
      .toHaveTextContent(DELEGATE.governed_by!.reason);
    expect(toggleOf(ROW_NAME)).toBeDisabled();
    expect(screen.queryByLabelText(HARNESS_CELL.key)).toBeNull();
    expect(screen.getByTestId(`conf-${HARNESS_CELL.key}`)).toHaveTextContent(HARNESSES[0]);
    expect(toggleOf(DELEGATE_SWITCH)).not.toBeDisabled();
  });

  it("위임 사용 줄이 없으면 기본값(켜짐)이라 잠기지 않는다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);

    expect(screen.queryByTestId(`switched-off-${DELEGATE.group}`)).toBeNull();
    expect(toggleOf(ROW_NAME)).not.toBeDisabled();
  });
});

describe("엔진이 적는 키", () => {
  const READONLY = KEYS.keys.filter((entry) => entry.readonly);

  it("체크박스가 없고 값이나 아직 없다는 한 줄만 선다", () => {
    expect(READONLY.length).toBeGreaterThan(0);
    mounted(makeBridge(), false, null, settingsEntry({ ...VALUES, [READONLY[0].key]: "abc123" }));
    openGroup(READONLY[0].group);
    fireEvent.click(screen.getByTestId("show-unset"));

    for (const entry of READONLY) {
      const row = screen.getByTestId(`conf-${entry.key}`);
      expect(row.querySelector("input")).toBeNull();
    }
    expect(screen.getByTestId(`conf-${READONLY[0].key}`)).toHaveTextContent("abc123");
    expect(screen.getByTestId(`conf-${READONLY[1].key}`)).toHaveTextContent(READONLY[1].unset!);
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

    // 값을 넣은 세트는 세 칸 중 하나만 차 있어도 세트로 보인다.
    expect(screen.getByTestId(`unit-${ROW_NAME}`)).toBeInTheDocument();
    const other = CELLS.find((cell) => cell.unit !== ROW_NAME)!;
    expect(screen.queryByTestId(`unit-${unitOf(other.key)}`)).toBeNull();
    expect(screen.getByTestId(`folded-${DELEGATE.group}`)).toHaveTextContent("설정 안 함");
  });

  it("보기를 켜면 접혔던 행이 그대로 나오고 절마다 다시 누르지 않는다", () => {
    mounted(makeBridge());
    openGroup(DELEGATE.group);
    const other = CELLS.find((cell) => cell.unit !== ROW_NAME)!;

    fireEvent.click(screen.getByTestId("show-unset"));

    expect(screen.getByTestId(`unit-${unitOf(other.key)}`)).toBeInTheDocument();
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

    expect(screen.getByTestId("question-key")).toHaveTextContent(order[0].label);
    fireEvent.change(screen.getByLabelText(`${order[0].key} 값`), { target: { value: "새 이름" } });
    fireEvent.keyDown(screen.getByLabelText(`${order[0].key} 값`), { key: "Enter" });
    expect(screen.getByTestId("question-key")).toHaveAttribute("title", order[1].key);

    while (screen.queryByTestId("question-mode")) {
      fireEvent.keyDown(screen.getByTestId("question-mode").querySelector("input")!,
                        { key: "Enter" });
    }

    await waitFor(() => expect(bridge.conf_save).toHaveBeenCalledTimes(1));
    expect(savedUpdates(bridge)[order[0].key]).toBe("새 이름");
  });
});

describe("원본 위치", () => {
  it("전환은 사본 명령이고 거부 문구가 원문 그대로 화면에 온다", async () => {
    const bridge = makeBridge();
    mounted(bridge);
    const declared = KEYS.keys.find((entry) => entry.key === UPSTREAM_PATH_KEY)!;
    openGroup(declared.group);

    fireEvent.change(screen.getByLabelText(declared.label), { target: { value: "/nowhere" } });
    fireEvent.click(screen.getByText("확인하고 저장"));

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

    expect(screen.getByText("설정을 읽지 못했습니다. 먼저 갱신하라.")).toBeInTheDocument();
  });

  it("판정을 나르는 가벼운 절이 없으면 배지를 지어내지 않는다", () => {
    mounted(makeBridge(), false, null, settingsEntry(),
            { error: { kind: "timeout", message: "10초 안에 응답이 없다", stderr: "", rc: null } });

    expect(screen.getByText("설정을 읽지 못했습니다. 10초 안에 응답이 없다")).toBeInTheDocument();
  });
});
