import { useCallback, useEffect, useMemo, useState } from "react";
import type { Entry, SettingsEntry } from "../App";
import type {
  Bridge, BridgeError, ConfChoices, ConfKeysDocument, ConfSection, DelegateModelsDocument,
} from "../api";
import { isError } from "../api";
import AdapterSection from "../components/AdapterSection";
import QuestionMode from "../components/QuestionMode";
import { foldable, markedKeys, ROW_UNIT_FAMILY, unsetFilled, unsetHint, unsetLabel,
         SET_TOGGLE, UNSET_MARK } from "../lib/settings-rows";

/** 설정 안 함 행을 보일지 — 화면 전체에 하나이고 다음에 열 때도 유지한다. */
const SHOW_UNSET_KEY = "pm.settings.showUnset";

function readShowUnset(): boolean {
  return window.localStorage.getItem(SHOW_UNSET_KEY) === "1";
}

function writeShowUnset(value: boolean): void {
  window.localStorage.setItem(SHOW_UNSET_KEY, value ? "1" : "0");
}

/**
 * 설정 탭 — 왼쪽 목록 + 고른 절의 행(편집형·질문형) + 설정 완전성 다섯 목록 + 어댑터 파일 절.
 *
 * 항목 목록의 정본은 **그 사본의** 키 레지스트리이고 판정은 그 사본의 `status --json` 의 `conf`
 * 절이다. 화면은 둘을 겹쳐 그릴 뿐 값을 스스로 판정하지 않는다 — 선택지·권장값·모델 목록도
 * 전부 엔진 산출이고, 접기 판정은 규칙표(`lib/settings-rows.json`) 하나가 소유한다.
 *
 * 두 조회(`conf_keys`·`conf_values`)는 이 탭이 부르지 않는다. 프로젝트를 고를 때 App 이 미리
 * 받아 프로젝트별로 들고 있고, 이 탭은 그 값을 받아 그린다(탭 진입 호출 0). 다시 읽는 때는
 * 저장 직후·갱신 적용 직후·수동 새로고침 셋뿐이다.
 */

/** 이름을 자유롭게 칠 수 있는 유일한 군 — 축이 그 프로젝트의 `qa.platforms` 값이라서다. */
export const FREE_INPUT_FAMILY = "test.<platform>.cmd";

/**
 * 표에도 질문형에도 행이 없는 키 — 값 자체가 도달성 계약이라 그 자리는 upstream 절
 * (`pm-config upstream set`) 하나다. 이 표의 저장 경로(conf writer)는 그 키를 거부한다.
 */
export const UPSTREAM_PATH_KEY = "upstream.path";

/** 어댑터 카드 렌더에 박히는 키 — 저장해도 카드는 갱신 탭 [적용] 이 다시 낸다. */
const ADAPTER_CARD_KEY = /^(project\.|runtime\.py$|test\.cmd$|delegate\..+\.model$|harness\..+\.pro_model$)/;

const ADAPTER_CARD_NOTE = "어댑터 카드는 이 저장으로 다시 그려지지 않는다 — 갱신 탭의 [적용] 이 다시 낸다.";

/** 왼쪽 목록의 마지막 항목 — 레지스트리 그룹이 아니라 버튼으로만 도는 어댑터 파일 절이다. */
export const ADAPTER_NAV = "어댑터 파일";

export type Row = {
  key: string;
  group: string;
  label: string;
  description: string;
  default: string | null;
  unset: string | null;      // 안 채웠을 때의 상태 (엔진 레지스트리 · 기본값이 있으면 null)
  required: boolean;
  family: string | null;      // 패턴 키군에서 전개된 행이면 그 군 이름
  registered: boolean;        // 레지스트리가 아는 키인가 (아니면 conf 에만 있는 행)
  choices: ConfChoices | null;   // 고를 수 있는 값 (엔진 레지스트리 산출)
  recommended: string | null;    // 권장값 (질문형이 미리 채운다)
};

/** 그룹별 절 하나 — 레지스트리 순서 그대로이고, 마지막 절은 conf 에만 있는 키다. */
export type Section = { group: string; rows: Row[]; freeInputFamily: string | null };

export const EXTRA_GROUP = "그 밖의 키";

function registryRows(keys: ConfKeysDocument): Row[] {
  const rows: Row[] = [];
  for (const entry of keys.keys) {
    if (entry.kind === "key") {
      rows.push({ key: entry.key, group: entry.group,
                  label: entry.label, description: entry.description,
                  default: entry.default, unset: entry.unset, required: entry.required,
                  family: null, registered: true,
                  choices: entry.choices, recommended: entry.recommended });
      continue;
    }
    for (const member of entry.members ?? []) {
      rows.push({ key: member.key, group: entry.group,
                  label: entry.label, description: entry.description,
                  default: member.default, unset: member.unset, required: entry.required,
                  family: entry.key, registered: true,
                  choices: member.choices, recommended: member.recommended });
    }
  }
  return rows;
}

/** 레지스트리 행 + conf 에만 있는 행(폐지 키·레지스트리 밖 키·전개되지 않는 군의 실키). */
export function buildSections(keys: ConfKeysDocument, values: Record<string, string>): Section[] {
  const registered = registryRows(keys);
  const known = new Set(registered.map((row) => row.key));
  // 레지스트리가 아는 키이므로 "그 밖의 키" 로 내려가지 않고, 행으로만 그리지 않는다.
  const rows = registered.filter((row) => row.key !== UPSTREAM_PATH_KEY);
  const families = new Map(keys.keys.filter((entry) => entry.kind === "pattern")
    .map((entry) => [entry.key, entry.group]));
  const sections = keys.groups.map((group) => ({
    group,
    rows: rows.filter((row) => row.group === group),
    freeInputFamily: families.get(FREE_INPUT_FAMILY) === group ? FREE_INPUT_FAMILY : null,
  })).filter((section) => section.rows.length > 0 || section.freeInputFamily !== null);
  const extra = Object.keys(values).filter((key) => !known.has(key)).map((key) => ({
    key, group: EXTRA_GROUP, label: key, description: "", default: null, required: false,
    family: null, registered: false, choices: null, recommended: null, unset: null,
  }));
  return extra.length > 0
    ? [...sections, { group: EXTRA_GROUP, rows: extra, freeInputFamily: null }]
    : sections;
}

/** 한 줄로 묶어 그리는 키군의 행 — 이름은 키의 마지막 점 앞이고, 칸은 엔진 전개 순서 그대로다. */
export type RoleRow = { row: string; cells: Row[] };
export type GroupItem = { kind: "key"; row: Row } | { kind: "role"; role: RoleRow };

/** 키의 마지막 점 뒤 — 그 칸의 이름(엔진이 전개한 접미사 그대로). */
export function fieldOf(key: string): string {
  return key.slice(key.lastIndexOf(".") + 1);
}

/**
 * 그룹의 행을 그리는 단위로 접는다 — 규칙표의 `row_unit_family` 만 역할×티어 한 줄이 된다.
 *
 * 행 이름과 칸 이름은 **키 문자열의 마지막 점**에서만 갈린다(역할·티어 목록을 화면이 들지 않는다).
 */
export function buildDelegateRows(rows: Row[]): GroupItem[] {
  const items: GroupItem[] = [];
  const roles = new Map<string, RoleRow>();
  for (const row of rows) {
    if (row.family !== ROW_UNIT_FAMILY) {
      items.push({ kind: "key", row });
      continue;
    }
    const name = row.key.slice(0, row.key.lastIndexOf("."));
    const existing = roles.get(name);
    if (existing) {
      existing.cells.push(row);
      continue;
    }
    const role: RoleRow = { row: name, cells: [row] };
    roles.set(name, role);
    items.push({ kind: "role", role });
  }
  return items;
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

/** `<select>` 에서 텍스트 입력으로 돌아가는 항목 — 목록 밖 값을 칠 자리다. */
const MANUAL_OPTION = "직접 입력";

const INPUT = "w-full rounded border border-slate-200 px-1 font-mono text-[11px]";
// 기본값이 서 있는 칸 — 사용자가 쓴 값과 같은 굵기면 둘을 눈으로 가를 수 없다.
const INPUT_UNSET = `${INPUT} text-slate-400`;


/**
 * 값 하나를 고치는 칸 — 모양은 그 키의 `choices` 가 정한다(화면 판정 0).
 *
 *   선택지 없음      → 텍스트 입력
 *   목록 조회 칸     → 텍스트 입력 + [목록 불러오기](누르기 전 조회 0 · 실패는 사유 그대로)
 *   그 밖의 선택지   → `<select>`(고른 값이 목록 밖이면 그 값도 항목으로 남긴다)
 *
 * 값을 비워 둔 칸은 비워 두지 않는다 — 안 써도 실제로 적용되는 기본값을 그 자리에 채우고 표식을
 * 붙인다. 채운 값은 `edits` 에 들어가지 않으므로 저장 대상이 아니다(저장은 고친 칸만 쓴다).
 * 채울지 여부와 문구는 규칙표가 소유한다.
 */
function ValueField({ row, value, on, options, dependsValue, fetched, fetchError, manual, busy,
                      onChange, onToggle, onFetch, onManual }: {
  row: Row; value: string; on: boolean; options: string[]; dependsValue: string | null;
  fetched: DelegateModelsDocument | null; fetchError: BridgeError | null; manual: boolean;
  busy: boolean; onChange: (value: string) => void; onToggle: (next: boolean) => void;
  onFetch: () => void; onManual: () => void;
}) {
  const choices = row.choices;
  const filled = unsetFilled(row, on);
  const shown = filled ? unsetHint(row) : value;
  const mark = filled
    ? <span className="whitespace-nowrap text-[11px] text-slate-400">{UNSET_MARK}</span>
    : null;
  // 축 둘을 칸 하나에 겹쳐 두면 "빈 칸"이 미설정인지 빈 값인지 읽히지 않는다 — 체크박스가
  // "직접 정한다"를 들고, 칸은 값만 든다. 끄면 안 썼을 때 적용되는 것이 회색으로 선다.
  const toggle = (
    <input type="checkbox" aria-label={`${row.key} ${SET_TOGGLE}`} checked={on} disabled={busy}
           onChange={(event) => onToggle(event.target.checked)} />
  );
  if (choices === null) {
    return (
      <span className="flex items-center gap-1">
        {toggle}
        <input aria-label={row.key} value={shown} disabled={!on}
               className={filled ? INPUT_UNSET : INPUT}
               onChange={(event) => onChange(event.target.value)} />
        {mark}
      </span>
    );
  }
  // 고른 값이 목록에 없으면(사람이 직접 친 값·다른 세대의 값) 그 값을 지우지 않고 항목으로 남긴다.
  const listed = (items: string[]) => (value === "" || items.includes(value)
    ? items : [value, ...items]);
  const blocked = choices.depends_on !== null && dependsValue === "";
  const hint = blocked ? `먼저 ${fieldOf(choices.depends_on!)} 를 고르세요` : null;

  if (choices.fetch !== null && (fetched === null || manual)) {
    return (
      <span className="flex items-center gap-1">
        {toggle}
        <input aria-label={row.key} value={shown} disabled={!on}
               className={filled ? INPUT_UNSET : INPUT}
               onChange={(event) => onChange(event.target.value)} />
        {mark}
        {/* 안내는 버튼 위에 둔다 — 같은 줄의 선택 칸이 그 문구를 이미 눈에 보이게 낸다. */}
        <button type="button" data-testid={`models-${row.key}`} disabled={blocked || busy}
                title={hint ?? undefined} onClick={onFetch}
                className="whitespace-nowrap rounded border border-slate-200 px-1">
          목록 불러오기
        </button>
        {fetchError && (
          <span title={fetchError.stderr} className="truncate text-[11px] text-red-600">
            {fetchError.message}{fetchError.stderr ? ` — ${fetchError.stderr}` : ""}
          </span>
        )}
      </span>
    );
  }
  const items = fetched === null ? options : fetched.models;
  return (
    <span className="flex items-center gap-1">
      {toggle}
      <select aria-label={row.key} value={value} disabled={!on}
              className={filled ? INPUT_UNSET : INPUT}
              onChange={(event) => {
                if (fetched !== null && event.target.value === MANUAL_OPTION) onManual();
                else onChange(event.target.value);
              }}>
        <option value="">{unsetLabel(row)}</option>
        {listed(items).map((item) => <option key={item} value={item}>{item}</option>)}
        {fetched !== null && <option value={MANUAL_OPTION}>{MANUAL_OPTION}</option>}
      </select>
      {fetched !== null && (
        <span className="whitespace-nowrap text-[11px] text-slate-500">{fetched.kind}</span>
      )}
      {hint && <span className="truncate text-[11px] text-slate-500">{hint}</span>}
    </span>
  );
}

export default function SettingsTab({ bridge, path, light, settings, onReload, startAsking,
                                     adapterConf, onAdapterConf }:
  { bridge: Bridge; path: string | null; light: Entry | undefined;
    settings: SettingsEntry | undefined; onReload: () => void; startAsking: boolean;
    adapterConf: ConfSection | null; onAdapterConf: (conf: ConfSection) => void }) {
  const [error, setError] = useState<BridgeError | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [newKey, setNewKey] = useState("");
  const [upstreamDraft, setUpstreamDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [command, setCommand] = useState<string | null>(null);
  const [asking, setAsking] = useState(startAsking);
  // [목록 불러오기] 가 받아 온 것 — 하네스별 최근 응답 하나이고 주기 조회로는 다시 부르지 않는다.
  const [models, setModels] = useState<Record<string, DelegateModelsDocument>>({});
  // "직접 정함" 을 이 화면에서 껐다 켠 행 — 판정의 기본은 conf 에 값이 있느냐이고, 이 표는
  // 그 위의 명시 조작이다. 값으로만 판정하면 기본값 없는 키를 켜는 순간 빈 값이라 다시
  // 꺼진 것으로 읽혀 칸이 잠긴다.
  const [setKeys, setSetKeys] = useState<Record<string, boolean>>({});
  const [modelErrors, setModelErrors] = useState<Record<string, BridgeError>>({});
  const [manual, setManual] = useState<Record<string, boolean>>({});
  // 설정 안 함 행을 보일지는 화면 전체에 하나다 — 절마다 다시 누르지 않는다.
  const [showUnset, setShowUnset] = useState<boolean>(() => readShowUnset());
  const [selectedNav, setSelectedNav] = useState<string | null>(null);

  // 갓 적용한 프로젝트는 질문형으로 열린다 — 그 프로젝트를 고른 순간의 표시 모드다.
  useEffect(() => { setAsking(startAsking); }, [path, startAsking]);

  // 프로젝트를 바꾸면 이 화면이 들고 있던 것만 지운다(값·키는 App 이 프로젝트별로 보관한다).
  useEffect(() => {
    setError(null);
    setEdits({});
    setNote(null);
    setCommand(null);
    setUpstreamDraft(null);
    setModels({});
    setModelErrors({});
    setSetKeys({});
    setManual({});
    setSelectedNav(null);
  }, [path]);

  const keys = settings?.keys;
  const values = settings?.values;

  // 아직 저장하지 않은 새 키(platform 명령)도 행으로 보여야 값을 칠 수 있다 — 레지스트리 밖이라
  // 그 자리는 "그 밖의 키" 절이고, 정오는 저장 뒤 사본의 `unknown` 판정이 말한다.
  const sections = useMemo(
    () => (keys && values ? buildSections(keys, { ...values, ...edits }) : []),
    [keys, values, edits]);
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
      onReload();                // 화면 값은 저장 직후 **재조회** 결과다
    }
    setBusy(false);
  }, [bridge, path, onReload]);

  const reset = useCallback(async (key: string) => {
    if (path === null) return;
    setBusy(true);
    const response = await bridge.conf_reset(path, key);
    if (isError(response)) setError(response.error);
    else {
      setError(null);
      onReload();
    }
    setBusy(false);
  }, [bridge, path, onReload]);

  const setUpstream = useCallback(async (value: string) => {
    if (path === null) return;
    setBusy(true);
    const response = await bridge.upstream_set(path, value);
    setCommand(isError(response)
      ? response.error.stderr || response.error.message
      : (response.rc === 0 ? response.stdout : response.stderr));
    if (!isError(response) && response.rc === 0) onReload();
    setBusy(false);
  }, [bridge, path, onReload]);

  const loadModels = useCallback(async (harness: string) => {
    if (path === null) return;
    setBusy(true);
    const response = await bridge.delegate_models(path, harness);
    if (isError(response)) {
      setModelErrors((previous) => ({ ...previous, [harness]: response.error }));
    } else {
      setModelErrors((previous) => {
        const next = { ...previous };
        delete next[harness];
        return next;
      });
      setModels((previous) => ({ ...previous, [harness]: response }));
    }
    setBusy(false);
  }, [bridge, path]);

  if (path === null) return <p className="px-4 py-3 text-slate-500">프로젝트를 고르면 설정이 온다</p>;
  // 다섯 번째 목록(`missing_for_latest`)만 어댑터 절이 받아 온 값으로 채운다 — 나머지 넷은
  // 가벼운 현황 조회의 판정이 더 새 것이고, 그 조회는 소스를 해소하지 않아 이 목록이 늘
  // null(모름)이다. 어댑터 절을 불러오기 전에는 그 null 이 그대로 화면에 남는다.
  const verdict = light?.status?.conf ?? null;
  if (!keys || !values || !verdict) {
    const failed = settings?.error?.message ?? light?.error?.message ?? null;
    return (
      <p className="px-4 py-3 text-slate-500">
        {failed ? `설정을 읽지 못했다 — ${failed}` : "설정 조회 중"}
      </p>
    );
  }
  const conf: ConfSection = adapterConf === null ? verdict
    : { ...verdict, missing_for_latest: adapterConf.missing_for_latest };
  const rowsInOrder = sections.flatMap((section) => section.rows);
  const unitLabels: Record<string, string> = Object.assign(
    {}, ...keys.keys.map((entry) => entry.unit_labels ?? {}));
  const valueOf = (key: string) => edits[key] ?? values[key] ?? "";
  const isOn = (key: string) => setKeys[key] ?? valueOf(key) !== "";
  const marked = markedKeys(conf);
  const folds = (row: Row) => foldable(row, valueOf(row.key), marked);
  const foldedItem = (item: GroupItem) => (item.kind === "key"
    ? folds(item.row) : item.role.cells.every(folds));
  const marksOf = (section: Section) =>
    section.rows.filter((row) => marked.has(row.key)).length;

  // upstream 절이 서는 자리는 그 키가 속한 그룹이다 — 절 이름을 화면이 새로 짓지 않는다.
  const upstreamGroup = keys.keys.find((entry) => entry.key === UPSTREAM_PATH_KEY)?.group ?? null;
  const navItems = [
    ...sections.map((section) => ({ id: section.group, marks: marksOf(section) })),
    { id: ADAPTER_NAV, marks: 0 },
  ];
  // 첫 진입은 손볼 것이 있는 첫 절이다(없으면 첫 절).
  const current = selectedNav
    ?? navItems.find((item) => item.marks > 0)?.id ?? navItems[0]?.id ?? null;
  const section = sections.find((entry) => entry.group === current) ?? null;
  const items = section === null ? [] : buildDelegateRows(section.rows);
  const folded = items.filter(foldedItem);
  const shown = showUnset ? items : items.filter((item) => !foldedItem(item));

  const field = (row: Row) => {
    const choices = row.choices;
    const dependsValue = choices?.depends_on ? valueOf(choices.depends_on) : null;
    const bucket = choices === null ? []
      : choices.values[choices.depends_on === null ? "" : (dependsValue ?? "")] ?? [];
    const harness = dependsValue ?? "";
    return (
      <ValueField
        row={row} value={valueOf(row.key)} options={bucket} dependsValue={dependsValue}
        fetched={choices?.fetch && harness !== "" ? models[harness] ?? null : null}
        fetchError={choices?.fetch && harness !== "" ? modelErrors[harness] ?? null : null}
        manual={manual[row.key] === true} busy={busy} on={isOn(row.key)}
        onChange={(next) => setEdits((previous) => ({ ...previous, [row.key]: next }))}
        onToggle={(next) => {
          // 켜면 기본값을 넣어 두고(없으면 빈 칸이라 입력을 요구), 끄면 값을 비운다.
          // 끈다고 local.conf 줄을 지우지는 않는다 — 저장이 그 칸을 빈 값으로 쓸 뿐이다.
          setSetKeys((previous) => ({ ...previous, [row.key]: next }));
          setEdits((previous) => ({ ...previous, [row.key]: next ? row.default ?? "" : "" }));
        }}
        onFetch={() => {
          setManual((previous) => {
            const next = { ...previous };
            delete next[row.key];
            return next;
          });
          void loadModels(harness);
        }}
        onManual={() => setManual((previous) => ({ ...previous, [row.key]: true }))}
      />
    );
  };

  // 이름을 크게, 키 문자열과 설명은 그 아래 작은 글씨 한 줄 — 옆에 붙이면 잘려서 안 읽힌다
  // 문구의 단일 소유는 엔진 키 목록이고 화면은 받아 그리기만 한다.
  const nameCell = (label: string, key: string, description: string) => (
    <td className="w-72 py-1 pr-3 align-top">
      <div className="truncate" title={label}>{label}</div>
      <div className="truncate text-[11px] text-slate-500" title={`${key} · ${description}`}>
        <span className="font-mono">{key}</span>
        {description && <span> · {description}</span>}
      </div>
    </td>
  );

  // 군 이름 하나가 멤버 전부에 붙으면 같은 이름이 여러 줄에 선다 — 갈리는 이름이 있으면 그것을
  // 쓴다. 갈리는지 아닌지의 주인은 엔진 키 목록(`unit_labels`)이고 화면은 있으면 쓸 뿐이다.
  const keyRow = (row: Row) => (
    <tr key={row.key} data-testid={`conf-${row.key}`}>
      {nameCell(unitLabels[row.key] ?? row.label, row.key, row.description)}
      <td className="py-0.5 pr-2">{field(row)}</td>
      <td className="w-32 py-0.5 pr-2 text-[11px]">
        <span className="flex gap-1 overflow-hidden whitespace-nowrap">
          <RowMarks row={row} conf={conf} />
          {conf.nonstandard.some((entry) => entry.key === row.key) && (
            <button type="button" disabled={busy} onClick={() => void reset(row.key)}
                    className="rounded border border-slate-200 px-1">기본값으로</button>
          )}
        </span>
      </td>
    </tr>
  );

  const roleRow = (role: RoleRow) => (
    <tr key={role.row} data-testid={`role-${role.row}`}>
      {nameCell(unitLabels[role.row] ?? role.row, role.row, "")}
      {role.cells.map((cell) => (
        <td key={cell.key} data-testid={`conf-${cell.key}`} className="py-0.5 pr-2">
          <span className="flex items-center gap-1">
            <span className="min-w-0 flex-1">{field(cell)}</span>
            <RowMarks row={cell} conf={conf} />
          </span>
        </td>
      ))}
    </tr>
  );

  // 같은 모양의 행이 이어지는 만큼씩 표 하나로 낸다 — 레지스트리 순서를 지키면서 칸 폭이
  // 행마다 흔들리지 않는다(위임 절은 칸 이름을 표 머리에 한 번만 적는다).
  const runs: GroupItem[][] = [];
  for (const item of shown) {
    const last = runs[runs.length - 1];
    if (last && last[0].kind === item.kind) last.push(item);
    else runs.push([item]);
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-3 border-b border-slate-200 px-4 py-2">
        <span data-testid="settings-banner" className="font-medium">{bannerText(conf)}</span>
        {!asking && (
          <label className="flex items-center gap-1 text-slate-600">
            <input type="checkbox" data-testid="show-unset" checked={showUnset}
                   onChange={(event) => {
                     setShowUnset(event.target.checked);
                     writeShowUnset(event.target.checked);
                   }} />
            설정 안 함 항목 보기
          </label>
        )}
        <button type="button" onClick={() => setAsking((value) => !value)}
                className="rounded border border-slate-200 px-2 py-0.5">
          {asking ? "편집형으로" : "질문형으로"}
        </button>
        {!asking && (
          <>
            <button type="button" disabled={changedCount === 0 || busy}
                    onClick={() => setEdits({})}
                    className="rounded border border-slate-200 px-2 py-0.5">되돌리기</button>
            <button type="button" data-testid="save" disabled={changedCount === 0 || busy}
                    onClick={() => void save(edits)}
                    className="rounded border border-slate-800 px-2 py-0.5">
              저장 ({changedCount} 변경)
            </button>
          </>
        )}
      </div>
      {error && (
        <p role="alert" className="px-4 py-1 text-red-600">설정 호출이 실패했다 — {error.message}</p>
      )}
      {note && <p data-testid="settings-note" className="px-4 py-1 text-amber-700">{note}</p>}
      {command && (
        <pre data-testid="settings-command"
             className="mx-4 my-1 whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">
          {command}
        </pre>
      )}

      {asking ? (
        <div className="px-4 py-3">
          <QuestionMode rows={rowsInOrder} values={values}
                        onFinish={(updates) => { setAsking(false); void save(updates); }} />
        </div>
      ) : (
        <div className="flex min-h-0 flex-1">
          <nav data-testid="settings-nav" className="w-44 shrink-0 overflow-y-auto border-r border-slate-200 py-2">
            {navItems.map((item) => (
              <button key={item.id} type="button" data-testid={`nav-${item.id}`}
                      onClick={() => setSelectedNav(item.id)}
                      className={`flex w-full items-center justify-between px-3 py-1 text-left ${
                        item.id === current ? "bg-slate-100 font-medium" : "hover:bg-slate-50"}`}>
                <span className="truncate">{item.id}</span>
                {item.marks > 0 && (
                  <span data-testid={`nav-marks-${item.id}`}
                        className="rounded bg-amber-100 px-1 text-[11px] text-amber-700">
                    {item.marks}
                  </span>
                )}
              </button>
            ))}
          </nav>

          <div className="min-w-0 flex-1 overflow-y-auto px-4 py-2">
            {current === ADAPTER_NAV ? (
              <AdapterSection bridge={bridge} path={path} onConfLoaded={onAdapterConf} />
            ) : section === null ? null : (
              <section data-testid={`group-${section.group}`}>
                {runs.map((run) => (run[0].kind === "key" ? (
                  <table key={run[0].kind + run[0].row.key} className="w-full table-fixed">
                    <tbody>
                      {run.map((item) => keyRow((item as { row: Row }).row))}
                    </tbody>
                  </table>
                ) : (
                  <table key={run[0].kind + run[0].role.row} className="w-full table-fixed">
                    <thead>
                      <tr className="text-left text-[11px] text-slate-500">
                        <th title={ROW_UNIT_FAMILY}
                            className="w-52 truncate font-normal">{ROW_UNIT_FAMILY}</th>
                        {(run[0] as { role: RoleRow }).role.cells.map((cell) => (
                          <th key={cell.key} className="font-normal">{fieldOf(cell.key)}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {run.map((item) => roleRow((item as { role: RoleRow }).role))}
                    </tbody>
                  </table>
                )))}

                {folded.length > 0 && !showUnset && (
                  <p data-testid={`folded-${section.group}`}
                     className="mt-1 text-[11px] text-slate-400">
                    설정 안 함 {folded.length}개 숨김
                  </p>
                )}

                {section.freeInputFamily && (
                  <div data-testid={`new-key-${section.group}`} className="mt-2 flex items-center gap-1">
                    <span className="font-mono text-[11px] text-slate-500">
                      {section.freeInputFamily}
                    </span>
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
                              // 갓 만든 행은 아직 값이 없어 접기 규칙에 걸린다 — 그 행이 서는
                              // 절로 옮기고 펼쳐, 만든 자리에서 바로 값을 칠 수 있게 한다.
                              setSelectedNav(EXTRA_GROUP);
                              setShowUnset(true);
                              writeShowUnset(true);
                            }}
                            className="rounded border border-slate-200 px-1">
                      항목 추가
                    </button>
                  </div>
                )}

                {section.group === upstreamGroup && (
                  <div className="mt-3 border-t border-slate-200 pt-2">
                    <p className="text-[11px] text-slate-500">
                      {UPSTREAM_PATH_KEY} 는 값 자체가 도달성 계약이라 사본의 `upstream set` 이
                      검증하고 기록한다.
                    </p>
                    <div className="flex items-center gap-1">
                      <input aria-label="upstream 값"
                             value={upstreamDraft ?? values[UPSTREAM_PATH_KEY] ?? ""}
                             onChange={(event) => setUpstreamDraft(event.target.value)}
                             className="w-[36rem] rounded border border-slate-200 px-1 font-mono text-[11px]" />
                      <button type="button" disabled={busy}
                              onClick={() => void setUpstream(
                                upstreamDraft ?? values[UPSTREAM_PATH_KEY] ?? "")}
                              className="rounded border border-slate-200 px-2">
                        upstream 설정
                      </button>
                    </div>
                  </div>
                )}
              </section>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
