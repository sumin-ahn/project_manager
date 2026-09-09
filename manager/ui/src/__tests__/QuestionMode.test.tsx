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
