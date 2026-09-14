import type { Entry } from "../App";
import type { SlotRemote as SlotRemoteValue, SlotRow } from "../api";

/** 슬롯이 상대 브랜치보다 앞/뒤 몇 커밋인지 — 엔진이 센 두 수 그대로다. */
function relation(ahead: number, behind: number): string {
  if (ahead === 0 && behind === 0) return "같음";
  if (ahead === 0) return `${behind} 커밋 뒤`;
  if (behind === 0) return `${ahead} 커밋 앞`;
  return `갈라짐 (앞 ${ahead} · 뒤 ${behind})`;
}

/**
 * 부모 브랜치 · 자식 브랜치 · stash — 가벼운 조회(`status --json`)가 실어 온 값 그대로.
 * 브랜치 없이 커밋에 멈춘 작업 슬롯은 부모 줄을 내지 않는다(브랜치 칸이 이미 "브랜치 없음"이다).
 */
export function lineageLines(row: SlotRow): string[] {
  const lines: string[] = [];
  const parent = row.parent;
  if (parent === null) {
    if (row.branch !== null || row.role === "readonly") lines.push("부모 기록 없음");
  } else {
    const named = `부모 ${parent.name}@${parent.fork.slice(0, 8)}`;
    lines.push(parent.ref === null ? `${named} · 부모 브랜치 없음`
      : `${named} · ${relation(parent.ahead!, parent.behind!)}`);
  }
  if (row.children.length > 0) lines.push(`자식 브랜치 ${row.children.join(", ")}`);
  if (row.stashes > 0) lines.push(`stash ${row.stashes}개`);
  return lines;
}

/**
 * 원격 정보 — origin 주소 · 추적 원격 브랜치. 값은 엔진(`status --json --with-remote`)이 센 그대로이고,
 * 조회가 아직 없거나 실패하면 그 사실 한 줄이다.
 */
export function remoteLines(value: SlotRemoteValue | null | undefined, entry: Entry | undefined): string[] {
  if (!value) return [entry?.error ? `원격 확인 실패 — ${entry.error.message}` : "원격 확인 전"];
  const upstream = value.upstream;
  return [
    `origin ${value.url}`,
    upstream === null ? "추적 원격 브랜치 없음"
      : upstream.head === null ? `추적 원격 브랜치 ${upstream.name} · 원격에 없음`
      : `추적 원격 브랜치 ${upstream.name}@${upstream.head.slice(0, 8)} · `
        + relation(upstream.ahead!, upstream.behind!),
  ];
}

/** 현황·구성 탭 슬롯 행 아래에 서는 그 슬롯의 브랜치 관계와 원격 정보. */
export default function SlotRemote({ slot, remote }: { slot: SlotRow; remote: Entry | undefined }) {
  const value = remote?.status?.slots.find((row) => row.slot === slot.slot)?.remote;
  return (
    <div data-testid={`slot-remote-${slot.slot}`} className="font-mono text-[11px] text-slate-500">
      {[...lineageLines(slot), ...remoteLines(value, remote)].map((line) => <div key={line}>{line}</div>)}
    </div>
  );
}
