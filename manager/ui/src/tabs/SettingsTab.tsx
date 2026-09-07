import { useCallback, useEffect, useMemo, useState } from "react";
import type {
  Bridge, BridgeError, ConfKeysDocument, ConfSection, ConfValuesDocument,
} from "../api";
import { isError } from "../api";
import AdapterSection from "../components/AdapterSection";
import QuestionMode from "../components/QuestionMode";

/**
 * 설정 탭 — local.conf 편집형·질문형 + 설정 완전성 다섯 목록 + 어댑터 파일 절.
 *
 * 항목 목록의 정본은 **그 사본의** 키 레지스트리(`conf_keys`)이고, 판정은 그 사본의
 * `status --json` 의 `conf` 절(`conf_values`)이다. 화면은 둘을 겹쳐 그릴 뿐 값을 스스로
 * 판정하지 않는다. 저장 뒤 보이는 값도 **재조회 결과**다(낙관적 갱신 없음).
 */

/** 이름을 자유롭게 칠 수 있는 유일한 군 — 축이 그 프로젝트의 `qa.platforms` 값이라서다. */
export const FREE_INPUT_FAMILY = "test.<platform>.cmd";

/** 어댑터 카드 렌더에 박히는 키 — 저장해도 카드는 갱신 탭 [적용] 이 다시 낸다. */
const ADAPTER_CARD_KEY = /^(project\.|runtime\.py$|test\.cmd$|delegate\..+\.model$|harness\..+\.pro_model$)/;

const ADAPTER_CARD_NOTE = "어댑터 카드는 이 저장으로 다시 그려지지 않는다 — 갱신 탭의 [적용] 이 다시 낸다.";

export type Row = {
  key: string;
  group: string;
  description: string;
  default: string | null;
  required: boolean;
  family: string | null;      // 패턴 키군에서 전개된 행이면 그 군 이름
  registered: boolean;        // 레지스트리가 아는 키인가 (아니면 conf 에만 있는 행)
};

/** 그룹별 절 하나 — 레지스트리 순서 그대로이고, 마지막 절은 conf 에만 있는 키다. */
export type Section = { group: string; rows: Row[]; freeInputFamily: string | null };

export const EXTRA_GROUP = "그 밖의 키";

function registryRows(keys: ConfKeysDocument): Row[] {
  const rows: Row[] = [];
  for (const entry of keys.keys) {
    if (entry.kind === "key") {
      rows.push({ key: entry.key, group: entry.group, description: entry.description,
                  default: entry.default, required: entry.required,
                  family: null, registered: true });
      continue;
    }
    for (const member of entry.members ?? []) {
      rows.push({ key: member.key, group: entry.group, description: entry.description,
                  default: member.default, required: entry.required,
                  family: entry.key, registered: true });
    }
  }
  return rows;
}

/** 레지스트리 행 + conf 에만 있는 행(폐지 키·레지스트리 밖 키·전개되지 않는 군의 실키). */
export function buildSections(keys: ConfKeysDocument, values: Record<string, string>): Section[] {
  const rows = registryRows(keys);
  const known = new Set(rows.map((row) => row.key));
  const families = new Map(keys.keys.filter((entry) => entry.kind === "pattern")
    .map((entry) => [entry.key, entry.group]));
  const sections = keys.groups.map((group) => ({
    group,
    rows: rows.filter((row) => row.group === group),
    freeInputFamily: families.get(FREE_INPUT_FAMILY) === group ? FREE_INPUT_FAMILY : null,
  })).filter((section) => section.rows.length > 0 || section.freeInputFamily !== null);
  const extra = Object.keys(values).filter((key) => !known.has(key)).map((key) => ({
    key, group: EXTRA_GROUP, description: "", default: null, required: false,
    family: null, registered: false,
  }));
  return extra.length > 0
    ? [...sections, { group: EXTRA_GROUP, rows: extra, freeInputFamily: null }]
    : sections;
}

function bannerText(conf: ConfSection): string {
  const forLatest = conf.missing_for_latest === null
    ? "갱신하면 필요 모름" : `갱신하면 필요 ${conf.missing_for_latest.length}`;
  return [`필수 누락 ${conf.missing_required.length}`, `폐지 ${conf.legacy.length}`,
          `표준과 다른 값 ${conf.nonstandard.length}`, forLatest].join(" · ");
}

function RowMarks({ row, conf }: { row: Row; conf: ConfSection }) {
  const legacy = conf.legacy.find((entry) => entry.key === row.key);
  return (
    <>
      {conf.missing_required.includes(row.key) && (
        <span className="text-red-600">! 필수 누락</span>
      )}
      {legacy && (
        <span className="text-amber-700">
          폐지 · {legacy.replacement ? `대체 ${legacy.replacement}` : "대체 키 없음"}
        </span>
      )}
      {conf.unknown.includes(row.key) && <span className="text-amber-700">레지스트리 밖</span>}
      {conf.missing_for_latest?.includes(row.key) && (
        <span className="rounded bg-amber-100 px-1 text-amber-700">갱신하면 필요</span>
      )}
    </>
  );
}

export default function SettingsTab({ bridge, path, startAsking }:
  { bridge: Bridge; path: string | null; startAsking: boolean }) {
  const [keys, setKeys] = useState<ConfKeysDocument | null>(null);
  const [current, setCurrent] = useState<ConfValuesDocument | null>(null);
  const [error, setError] = useState<BridgeError | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [newKey, setNewKey] = useState("");
  const [upstreamDraft, setUpstreamDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [command, setCommand] = useState<string | null>(null);
  const [asking, setAsking] = useState(startAsking);

  const loadValues = useCallback(async (target: string) => {
    const response = await bridge.conf_values(target);
    if (isError(response)) {
      setError(response.error);
      return;
    }
    setError(null);
    setCurrent(response);
  }, [bridge]);

  // 갓 적용한 프로젝트는 질문형으로 열린다 — 그 프로젝트를 고른 순간의 표시 모드다.
  useEffect(() => { setAsking(startAsking); }, [path, startAsking]);

  useEffect(() => {
    if (path === null) return;
    setKeys(null);
    setCurrent(null);
    setEdits({});
    setNote(null);
    setCommand(null);
    setUpstreamDraft(null);
    void (async () => {
      const registry = await bridge.conf_keys(path);
      if (isError(registry)) setError(registry.error);
      else {
        setError(null);
        setKeys(registry);
      }
      await loadValues(path);
    })();
  }, [bridge, path, loadValues]);

  // 아직 저장하지 않은 새 키(platform 명령)도 행으로 보여야 값을 칠 수 있다 — 레지스트리 밖이라
  // 그 자리는 "그 밖의 키" 절이고, 정오는 저장 뒤 사본의 `unknown` 판정이 말한다.
  const sections = useMemo(
    () => (keys && current ? buildSections(keys, { ...current.values, ...edits }) : []),
    [keys, current, edits]);
  const changedCount = Object.keys(edits).length;

  const save = useCallback(async (updates: Record<string, string>) => {
    if (path === null || Object.keys(updates).length === 0) return;
    setBusy(true);
    const response = await bridge.conf_save(path, updates);
    if (isError(response)) setError(response.error);
    else {
      setError(null);
      setEdits({});
      setNote(Object.keys(updates).some((key) => ADAPTER_CARD_KEY.test(key))
        ? ADAPTER_CARD_NOTE : null);
      await loadValues(path);      // 화면 값은 저장 직후 **재조회** 결과다
    }
    setBusy(false);
  }, [bridge, path, loadValues]);

  const reset = useCallback(async (key: string) => {
    if (path === null) return;
    setBusy(true);
    const response = await bridge.conf_reset(path, key);
    if (isError(response)) setError(response.error);
    else {
      setError(null);
      await loadValues(path);
    }
    setBusy(false);
  }, [bridge, path, loadValues]);

  const setUpstream = useCallback(async (value: string) => {
    if (path === null) return;
    setBusy(true);
    const response = await bridge.upstream_set(path, value);
    setCommand(isError(response)
      ? response.error.stderr || response.error.message
      : (response.rc === 0 ? response.stdout : response.stderr));
    if (!isError(response) && response.rc === 0) await loadValues(path);
    setBusy(false);
  }, [bridge, path, loadValues]);

  if (path === null) return <p className="px-4 py-3 text-slate-500">프로젝트를 고르면 설정이 온다</p>;
  if (!keys || !current) {
    return (
      <p className="px-4 py-3 text-slate-500">
        {error ? `설정을 읽지 못했다 — ${error.message}` : "설정 조회 중"}
      </p>
    );
  }

  const conf = current.conf;
  const rowsInOrder = sections.flatMap((section) => section.rows);
  const valueOf = (row: Row) => edits[row.key] ?? current.values[row.key] ?? "";

  return (
    <div className="space-y-4 px-4 py-3">
      <div className="flex items-center gap-3">
        <span data-testid="settings-banner" className="font-medium">{bannerText(conf)}</span>
        <button type="button" onClick={() => setAsking((value) => !value)}
                className="rounded border border-slate-200 px-2 py-0.5 hover:bg-slate-50">
          {asking ? "편집형으로" : "질문형으로"}
        </button>
      </div>
      {error && (
        <p role="alert" className="text-red-600">설정 호출이 실패했다 — {error.message}</p>
      )}
      {note && <p data-testid="settings-note" className="text-amber-700">{note}</p>}
      {command && (
        <pre data-testid="settings-command" className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">
          {command}
        </pre>
      )}

      {asking ? (
        <QuestionMode rows={rowsInOrder} values={current.values}
                      onFinish={(updates) => { setAsking(false); void save(updates); }} />
      ) : (
        <>
          {sections.map((section) => (
            <section key={section.group} data-testid={`group-${section.group}`}>
              <h2 className="mb-1 font-medium">{section.group}</h2>
              <table className="w-full table-fixed">
                <tbody>
                  {section.rows.map((row) => (
                    <tr key={row.key} data-testid={`conf-${row.key}`}>
                      <td title={row.key}
                          className="w-56 truncate py-0.5 pr-2 font-mono text-[11px]">{row.key}</td>
                      <td className="py-0.5 pr-2">
                        <input
                          aria-label={row.key}
                          value={valueOf(row)}
                          onChange={(event) => setEdits((previous) => ({
                            ...previous, [row.key]: event.target.value }))}
                          className="w-full rounded border border-slate-200 px-1 font-mono text-[11px]"
                        />
                      </td>
                      <td className="w-20 truncate py-0.5 pr-2 text-[11px] text-slate-500">
                        {row.default !== null && <span>기본 {row.default}</span>}
                      </td>
                      <td className="w-36 py-0.5 pr-2 text-[11px]">
                        <span className="flex gap-1 overflow-hidden whitespace-nowrap">
                          <RowMarks row={row} conf={conf} />
                          {conf.nonstandard.some((entry) => entry.key === row.key) && (
                            <button type="button" disabled={busy}
                                    onClick={() => void reset(row.key)}
                                    className="rounded border border-slate-200 px-1">
                              기본값으로
                            </button>
                          )}
                        </span>
                      </td>
                      <td title={row.description}
                          className="w-56 truncate py-0.5 text-[11px] text-slate-500">
                        {row.description}
                      </td>
                    </tr>
                  ))}
                  {section.freeInputFamily && (
                    <tr data-testid={`new-key-${section.group}`}>
                      <td className="py-0.5 font-mono text-[11px] text-slate-500">
                        {section.freeInputFamily}
                      </td>
                      <td className="py-0.5" colSpan={4}>
                        <input
                          data-testid="new-key-input"
                          aria-label={`${section.freeInputFamily} 이름`}
                          placeholder="platform 이름 (예: windows)"
                          value={newKey}
                          onChange={(event) => setNewKey(event.target.value)}
                          className="rounded border border-slate-200 px-1"
                        />
                        <button type="button" disabled={newKey.trim() === ""}
                                onClick={() => {
                                  setEdits((previous) => ({
                                    ...previous, [`test.${newKey.trim()}.cmd`]: "" }));
                                  setNewKey("");
                                }}
                                className="ml-1 rounded border border-slate-200 px-1">
                          항목 추가
                        </button>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </section>
          ))}

          <section>
            <h2 className="mb-1 font-medium">upstream</h2>
            <p className="text-[11px] text-slate-500">
              값 자체가 도달성 계약이라 사본의 `upstream set` 이 검증하고 기록한다.
            </p>
            <div className="flex items-center gap-1">
              <input aria-label="upstream 값"
                     value={upstreamDraft ?? current.values["upstream.path"] ?? ""}
                     onChange={(event) => setUpstreamDraft(event.target.value)}
                     className="w-[36rem] rounded border border-slate-200 px-1 font-mono text-[11px]" />
              <button type="button" disabled={busy}
                      onClick={() => void setUpstream(
                        upstreamDraft ?? current.values["upstream.path"] ?? "")}
                      className="rounded border border-slate-200 px-2">
                upstream 설정
              </button>
            </div>
          </section>

          <div className="flex items-center gap-2">
            <button type="button" disabled={changedCount === 0 || busy}
                    onClick={() => setEdits({})}
                    className="rounded border border-slate-200 px-2 py-0.5">되돌리기</button>
            <button type="button" data-testid="save" disabled={changedCount === 0 || busy}
                    onClick={() => void save(edits)}
                    className="rounded border border-slate-800 px-2 py-0.5">
              저장 ({changedCount} 변경)
            </button>
          </div>
        </>
      )}

      <AdapterSection bridge={bridge} path={path} />
    </div>
  );
}
