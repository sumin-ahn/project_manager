import type { Entry } from "../App";
import type { SlotRemote as SlotRemoteValue } from "../api";

/** 슬롯이 상대 브랜치보다 앞/뒤 몇 커밋인지 — 엔진이 센 두 수 그대로다. */
function relation(ahead: number, behind: number): string {
  if (ahead === 0 && behind === 0) return "같음";
  if (ahead === 0) return `${behind} 커밋 뒤`;
  if (behind === 0) return `${ahead} 커밋 앞`;
  return `갈라짐 (앞 ${ahead} · 뒤 ${behind})`;
}

/**
 * 슬롯의 원격 대조 세 줄 — origin 주소 · 추적 원격 브랜치 · 원격 HEAD 브랜치.
 * 값은 엔진(`status --json --with-remote`)이 센 그대로이고, 대조가 아직 없거나 실패하면 그 사실 한 줄이다.
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
    `원격 HEAD origin/${value.head_branch}@${value.head.slice(0, 8)} · ${relation(value.ahead, value.behind)}`,
  ];
}

/** 현황·구성 탭 슬롯 행 아래에 서는 그 슬롯의 원격 대조. */
export default function SlotRemote({ slot, remote }: { slot: string; remote: Entry | undefined }) {
  const value = remote?.status?.slots.find((row) => row.slot === slot)?.remote;
  return (
    <div data-testid={`slot-remote-${slot}`} className="font-mono text-[11px] text-slate-500">
      {remoteLines(value, remote).map((line) => <div key={line}>{line}</div>)}
    </div>
  );
}
