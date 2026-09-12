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

/** 켜지 않은 칸에 붙는 표식 문구 — 그 값이 사용자가 쓴 것이 아님을 말한다. */
export const UNSET_MARK: string = rule.unset.mark;

/** 그 키를 직접 정하겠다는 체크박스의 이름. */
export const SET_TOGGLE: string = rule.unset.toggle;

/** 안 채웠을 때 그 칸에 서는 것 — 엔진 기본값이 있으면 그 값, 없으면 그때의 상태 낱말. */
export type UnsetRow = { default: string | null; unset: string | null };

/**
 * 켜지 않은 칸에 채워 보일 문구.
 *
 * 주인은 엔진 키 레지스트리다(`default` = 미설정 시 쓰는 값 · `unset` = 그때의 상태). schema 5
 * 는 둘 중 하나가 항상 값이라고 보장하므로 화면에 빈 칸이 생기지 않는다. 레지스트리 밖 키
 * (conf 에만 있는 행)만 둘 다 없고, 그 행은 애초에 사람이 이미 값을 적은 행이다.
 */
export function unsetHint(row: UnsetRow): string {
  return row.default ?? row.unset ?? "";
}

/** 그 칸이 "안 쓴 상태"로 서는가 — 켜지 않았고 보일 것이 있을 때. */
export function unsetFilled(row: UnsetRow, on: boolean): boolean {
  return !on && unsetHint(row) !== "";
}

/** 목록 칸에서 그 자리를 고를 때 보이는 한 줄 — 채운 값 + 표식이 한 항목에 들어간다. */
export function unsetLabel(row: UnsetRow): string {
  const hint = unsetHint(row);
  return hint === "" ? UNSET_MARK : `${hint} (${UNSET_MARK})`;
}
