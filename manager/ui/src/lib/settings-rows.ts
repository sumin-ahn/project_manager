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

/**
 * 그 칸을 비워 뒀을 때 실제로 쓰이는 값 — 빈 칸이 "아무 일도 안 일어난다" 로 읽히지 않게 한다.
 *
 * 기본값 선언의 주인은 엔진 키 목록(`default` = 미설정 시 엔진이 쓰는 값)이고 문구의 주인은
 * 규칙표다. 화면은 받아 그리기만 한다.
 */
export function unsetHint(row: { default: string | null }): string {
  return row.default === null
    ? rule.unset_hint.none
    : rule.unset_hint.with_default.replace("{default}", row.default);
}
