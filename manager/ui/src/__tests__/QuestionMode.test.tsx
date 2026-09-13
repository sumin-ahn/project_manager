import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Entry, SettingsEntry } from "../App";
import type { Bridge, ConfKeysDocument, ConfSection, StatusDocument } from "../api";
import confKeys from "../fixtures/conf-keys.sample.json";
import QuestionMode from "../components/QuestionMode";
import SettingsTab, { buildSections } from "../tabs/SettingsTab";

/** 키·권장값은 실 엔진 산출(`local_conf.py --keys --json`)이다 — 화면 상수 0. */
const KEYS = confKeys as unknown as ConfKeysDocument;
const DEMO = "/projects/demo";
const CONF: ConfSection = {
  missing_required: [], legacy: [], unknown: [], nonstandard: [], missing_for_latest: null,
};

const ROWS = buildSections(KEYS, {}).flatMap((section) => section.rows);
/** 권장값이 선언된 행 — 질문형이 미리 채우는 자리다. */
const RECOMMENDED = ROWS.filter((row) => row.recommended !== null);

function makeBridge(): Bridge {
  return {
    conf_save: vi.fn(async () => ({ changed: true, conf: CONF })),
    conf_keys: vi.fn(async () => KEYS),
    conf_values: vi.fn(async () => ({ values: {} })),
    adapters: vi.fn(async () => ({ source: "", files: [], conf: CONF })),
  } as unknown as Bridge;
}

function mountTab(bridge: Bridge, values: Record<string, string>) {
  const settings: SettingsEntry = { keys: KEYS, values };
  const light: Entry = { status: { conf: CONF } as StatusDocument };
  render(<SettingsTab bridge={bridge} path={DEMO} light={light} settings={settings}
                      onReload={vi.fn()} startAsking adapterConf={null}
                      onAdapterConf={vi.fn()} />);
}

afterEach(cleanup);

describe("질문형 권장값", () => {
  it("권장값이 있는 항목은 미리 채워진다", () => {
    const row = RECOMMENDED[0];
    render(<QuestionMode rows={[row]} values={{}} onFinish={vi.fn()} />);

    expect(screen.getByLabelText(`${row.key} 값`)).toHaveValue(row.recommended);
  });

  it("이미 값이 있는 항목은 권장값이 덮지 않는다", () => {
    const row = RECOMMENDED[0];
    render(<QuestionMode rows={[row]} values={{ [row.key]: "사람이 정한 값" }}
                         onFinish={vi.fn()} />);

    expect(screen.getByLabelText(`${row.key} 값`)).toHaveValue("사람이 정한 값");
  });

  it("[권장값으로 모두 채우기] 는 같은 저장 경로를 한 번 탄다", async () => {
    const bridge = makeBridge();
    const kept = RECOMMENDED[0];
    mountTab(bridge, { [kept.key]: "사람이 정한 값" });

    fireEvent.click(screen.getByTestId("fill-recommended"));

    await waitFor(() => expect(bridge.conf_save).toHaveBeenCalledTimes(1));
    const [path, updates] = (bridge.conf_save as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(path).toBe(DEMO);
    // 값이 없던 권장 행 전부가 한 번에 실린다 — 사람이 정한 값은 그대로 남는다.
    expect(Object.keys(updates).sort())
      .toEqual(RECOMMENDED.filter((row) => row.key !== kept.key).map((row) => row.key).sort());
    for (const row of RECOMMENDED) {
      if (row.key !== kept.key) expect(updates[row.key]).toBe(row.recommended);
    }
    expect(updates[kept.key]).toBeUndefined();
  });
});

/**
 * Enter 만 눌러 끝까지 넘기며 물은 키(항목 제목의 툴팁)를 순서대로 모은다.
 * 끝은 `onFinish` 가 불린 때다 — 단독 렌더는 끝나도 마지막 항목이 화면에 남는다.
 */
function askAll(finished: () => boolean): string[] {
  const asked: string[] = [];
  for (let step = 0; step <= ROWS.length && !finished(); step += 1) {
    asked.push(screen.getByTestId("question-key").getAttribute("title")!);
    fireEvent.keyDown(screen.getByTestId("question-mode").querySelector("input")!,
                      { key: "Enter" });
  }
  expect(finished()).toBe(true);
  return asked;
}

describe("질문형이 묻지 않는 항목", () => {
  /** 켜짐 목록을 가진 스위치(위임 사용)와 그 스위치가 잠그는 항목 — 이름은 픽스처에서 꺼낸다. */
  const GOVERNED = ROWS.filter((row) => row.governed_by?.on);
  const MASTER = GOVERNED[0].governed_by!.key;

  it("항목 제목은 사람 말 이름이고 키 문자열은 툴팁에만 있다", () => {
    render(<QuestionMode rows={ROWS} values={{}} onFinish={vi.fn()} />);

    expect(screen.getByTestId("question-key")).toHaveTextContent(ROWS[0].label);
    expect(screen.getByTestId("question-key")).not.toHaveTextContent(ROWS[0].key);
  });

  it("스위치를 끄는 답을 하면 그 스위치가 잠그는 항목은 건너뛴다", () => {
    const onFinish = vi.fn();
    render(<QuestionMode rows={ROWS} values={{ [MASTER]: "false" }} onFinish={onFinish} />);

    const asked = askAll(() => onFinish.mock.calls.length > 0);

    expect(asked).toContain(MASTER);
    expect(GOVERNED.filter((row) => asked.includes(row.key))).toEqual([]);
    expect(onFinish).toHaveBeenCalledTimes(1);
  });

  it("스위치가 기본값(켜짐)이면 그 항목들을 묻는다", () => {
    const onFinish = vi.fn();
    render(<QuestionMode rows={ROWS} values={{}} onFinish={onFinish} />);

    expect(askAll(() => onFinish.mock.calls.length > 0))
      .toEqual(expect.arrayContaining(GOVERNED.map((row) => row.key)));
  });

  it("엔진이 적는 항목은 설정 탭의 질문형 목록에 없다", () => {
    mountTab(makeBridge(), {});

    const readonly = ROWS.filter((row) => row.readonly).map((row) => row.key);
    expect(readonly.length).toBeGreaterThan(0);
    const asked = askAll(() => screen.queryByTestId("question-mode") === null);
    expect(asked.filter((key) => readonly.includes(key))).toEqual([]);
  });
});
