/** 설정 행 접기 판정 — 규칙표(`settings-rows.json`)만 읽고 화면이 새로 계산하지 않는다. */
import type { ConfSection } from "../api";
import rule from "./settings-rows.json";

/** 칸을 한 줄로 묶어 접는 키군 — 위임 절의 역할×티어 한 줄이 이 군에서 나온다. */
export const ROW_UNIT_FAMILY: string = rule.row_unit_family;

/** 접기 판정이 보는 행의 축 셋(넷째 축인 배지는 `markedKeys` 가 잰다). */
export type FoldRow = { key: string; default: string | null; required: boolean };

/**
 * 배지가 붙는 키 전부 — 규칙표가 세는 목록만 훑는다.
 *
 * 목록의 모양이 둘이다: 키 문자열 배열(`missing_required`)과 `{key,…}` 행 배열(`legacy`).
 * 모름(null)인 목록은 배지를 세지 못하므로 건너뛴다 — 조회 전의 `missing_for_latest` 가 그렇다.
 */
export function markedKeys(conf: ConfSection): Set<string> {
  const marked = new Set<string>();
  const lists = conf as unknown as Record<string, unknown>;
  for (const name of rule.fold.marks) {
    const list = lists[name];
    if (!Array.isArray(list)) continue;
    for (const entry of list) {
      marked.add(typeof entry === "string" ? entry : (entry as { key: string }).key);
    }
  }
  return marked;
}

/** 그 행을 접는가 — 네 축이 전부 규칙표의 값과 같을 때만 접는다. */
export function foldable(row: FoldRow, value: string, marked: Set<string>): boolean {
  return value === rule.fold.value
    && row.required === rule.fold.required
    && row.default === rule.fold.default
    && !marked.has(row.key);
}

/** 사용자가 쓴 값이 아니라 기본값이 서 있다는 표식 문구. */
export const UNSET_MARK: string = rule.unset_hint.mark;

/**
 * 그 칸을 비워 뒀을 때 실제로 쓰이는 값 — 빈 칸을 비워 두지 않고 **그 값으로 채운다**.
 *
 * 기본값 선언의 주인은 엔진 키 목록(`default` = 미설정 시 엔진이 쓰는 값)이고 문구의 주인은
 * 규칙표다. 화면은 받아 그리기만 한다. 기본값이 없는 키는 채울 값이 없어 빈 칸 안내가 온다.
 */
export function unsetHint(row: { default: string | null }): string {
  return row.default === null
    ? rule.unset_hint.none
    : rule.unset_hint.with_default.replace("{default}", row.default);
}

/**
 * 그 칸에 기본값이 채워져 서는가 — 사용자가 쓴 값이 없고 채울 기본값이 있을 때.
 *
 * 이 판정도 규칙표 쪽이 소유한다. 화면이 `default === null` 을 다시 적으면 두 벌이 되어
 * 한쪽만 고쳐지는 날 갈린다(접기 판정과 같은 이유).
 */
export function unsetFilled(row: { default: string | null }, value: string): boolean {
  return value === "" && row.default !== null;
}

/** 채울 값이 없는 칸의 빈 칸 안내 — 채울 값이 있으면 안내가 아니라 값이 선다. */
export function unsetPlaceholder(row: { default: string | null }): string | undefined {
  return unsetFilled(row, "") ? undefined : rule.unset_hint.none;
}

/** 목록 칸에서 그 자리를 고를 때 보이는 한 줄 — 채운 값 + 표식이 한 항목에 들어간다. */
export function unsetLabel(row: { default: string | null }): string {
  return row.default === null
    ? rule.unset_hint.none
    : `${unsetHint(row)} (${UNSET_MARK})`;
}
