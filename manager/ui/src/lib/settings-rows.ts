/** 설정 행 접기 판정 — 규칙표(`settings-rows.json`)만 읽고 화면이 새로 계산하지 않는다. */
import type { ConfGovernor, ConfSection } from "../api";
import rule from "./settings-rows.json";

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

/** 그 키를 직접 정하겠다는 체크박스의 이름. */
export const SET_TOGGLE: string = rule.unset.toggle;

/** 켠 목록 칸에서 아직 아무것도 고르지 않았을 때의 항목. */
export const PICK: string = rule.unset.pick;

/** 안 채웠을 때 — 엔진 기본값이 있으면 그 값, 없으면 엔진이 준 동작 문장. */
export type UnsetRow = { default: string | null; unset: string | null };

/**
 * 켜지 않은 칸에 서는 한 줄.
 *
 * 주인은 엔진 키 레지스트리다(`default` = 미설정 시 쓰는 값 · `unset` = 그때 일어나는 일).
 * schema 6 은 둘 중 하나가 항상 값이라고 보장한다. 레지스트리 밖 키(conf 에만 있는 행)만 둘 다
 * 없고, 그 행은 애초에 사람이 이미 값을 적은 행이다.
 */
export function unsetHint(row: UnsetRow): string {
  if (row.default !== null) return `${rule.unset.default}: ${row.default}`;
  return row.unset === null ? "" : `${rule.unset.behavior}: ${row.unset}`;
}

/**
 * 그 절을 켜는 스위치가 꺼져 있으면 그 한 줄(엔진이 준 문구), 켜져 있으면 null.
 *
 * 켜짐 판정의 주인은 엔진이다 — `on` 이 null 이면 "값이 있으면 켜짐", 목록이면 그 안의 값만
 * 켜짐이다. 빈 값은 스위치의 기본값으로 읽는다(값을 지운 줄이 스위치를 조용히 끄지 않는다).
 * 대소문자·앞뒤 공백을 무시하는 것은 엔진 판정(`pm_delegate._is_enabled`)과 같다.
 */
export function offReason(governor: ConfGovernor | null, value: string,
                          fallback: string | null): string | null {
  if (governor === null) return null;
  const effective = (value.trim() === "" ? fallback ?? "" : value).trim().toLowerCase();
  const on = governor.on === null ? effective !== "" : governor.on.includes(effective);
  return on ? null : governor.reason;
}
