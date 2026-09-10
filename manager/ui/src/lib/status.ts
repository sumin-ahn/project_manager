/** 상태 점·버전 배지 판정 — 값은 전부 `status --json` 에서 오고 여기서 새로 계산하지 않는다. */
import type { TagsResponse } from "../api";
import dotRules from "./status-dots.json";

export type Dot = "blue" | "gray" | "orange";

/** 그 행에서 여는 동작 — 판정표의 셋째 칼럼이고 관리자가 따로 계산하지 않는다. */
export type RowActionMode = "change" | "disabled" | "refresh";

export type StatusRow = {
  state?: string;
  alive?: boolean;
  role?: string;
};

export type DotVerdict = { dot: Dot; label: string; actions: RowActionMode };

/**
 * 슬롯·task 행 하나의 판정 — 규칙표(`status-dots.json`)의 첫 일치가 이긴다.
 *
 * 점·라벨과 **변경 동작 가부가 같은 한 줄**에서 나온다. 아이들 규칙(§4.5)을 두 번째 순회로
 * 다시 재면 readonly 우선 순서가 두 벌이 되고, 한쪽만 고쳐지는 날 공유 슬롯이 닫힘으로
 * 오표시된다.
 */
export function rowDot(row: StatusRow): DotVerdict {
  for (const rule of dotRules.rules) {
    if ((row as Record<string, unknown>)[rule.field] === rule.equals) {
      return { dot: rule.dot as Dot, label: rule.label,
               actions: rule.actions as RowActionMode };
    }
  }
  throw new Error(`상태 점 규칙에 없는 행: ${JSON.stringify(row)}`);
}

/**
 * 프로젝트 목록의 점 — 열린 기록(작업 중) 행이 하나라도 있으면 주황, 아니면 회색.
 * readonly 는 무소유 공유 자산이라 목록 점에 영향을 주지 않는다.
 */
export function projectDot(rows: StatusRow[]): Dot {
  const dots = rows.map((row) => rowDot(row).dot);
  return dots.includes("orange") ? "orange" : "gray";
}

const RELEASE_TAG = /^v(\d+)\.(\d+)\.(\d+)$/;

function versionTuple(value: string | null | undefined): [number, number, number] | null {
  const matched = value ? RELEASE_TAG.exec(value) : null;
  return matched ? [Number(matched[1]), Number(matched[2]), Number(matched[3])] : null;
}

/** "갱신 있음 ↑" — 최신 태그가 이 사본의 엔진 rev 보다 높을 때만. 비교는 숫자 셋이다. */
export function updateAvailable(engineRev: string | null | undefined,
                                latestTag: string | null | undefined): boolean {
  const current = versionTuple(engineRev);
  const latest = versionTuple(latestTag);
  if (current === null || latest === null) return false;
  for (let index = 0; index < 3; index += 1) {
    if (latest[index] !== current[index]) return latest[index] > current[index];
  }
  return false;
}

/** 슬롯 요약 한 줄 — "1 작업중 · 2 아이들 · 1 readonly" 형태의 촘촘한 표기. */
export function slotSummary(rows: StatusRow[]): string {
  const counts = new Map<string, number>();
  for (const row of rows) {
    const { label } = rowDot(row);
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  return [...counts.entries()].map(([label, count]) => `${count} ${label}`).join(" · ");
}

/** 그 프로젝트의 upstream 에 해당하는 최신 태그(미등록·조회 실패면 null). */
export function latestTagOf(tags: TagsResponse | null, path: string): string | null {
  const upstream = tags?.projects[path]?.upstream;
  if (!tags || !upstream) return null;
  const entry = tags.upstreams[upstream];
  return entry && "tag" in entry ? entry.tag ?? null : null;
}
