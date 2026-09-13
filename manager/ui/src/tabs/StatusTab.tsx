import type { Entry } from "../App";
import type { SlotRemote } from "../api";
import { rowDot } from "../lib/status";
import StatusDot from "../components/StatusDot";
import StatusRowActions, { holderLabel, SlotTableAllocate } from "../components/StatusRowActions";

/**
 * 기계 기록 한 줄 — 판정(`status`) · 수집 수 · 시각. 값은 엔진 sidecar 그대로 고르고 여기서
 * 다시 계산하지 않는다. 회귀는 `collected`, 게이트는 `n` 이라 이름만 다르다.
 *
 * 하한(`floor`)은 **수집 수가 그에 못 미칠 때만** 붙는다 — 넘긴 하한은 사람이 읽을 것이 없고,
 * 미달은 그 자체가 봐야 할 사실이다. 수집 수가 없는 옛 기록은 그 자리를 비운 채 나머지를 낸다.
 */
export function recordLine(record: Record<string, unknown>): string {
  const collected = record.collected ?? record.n;
  const short = record.floor != null && collected != null
    && Number(collected) < Number(record.floor);
  const counted = collected == null ? null
    : short ? `${collected}개 수집 (하한 ${record.floor} 미달)` : `${collected}개 수집`;
  return [record.status, counted, record.ts]
    .filter((part) => part != null).map(String).join(" · ");
}

/** 그 기록 한 줄 + 접힌 원문 — 원문은 열어야 나오고, 접힌 채로는 한 줄만 자리를 쓴다. */
function MachineRecord({ name, label, record }:
  { name: string; label: string; record: Record<string, unknown> }) {
  return (
    <details data-testid={`record-${name}`}>
      <summary className="cursor-default text-slate-600">
        {label} {recordLine(record)}
      </summary>
      <pre className="mt-1 whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px] text-slate-500">
        {JSON.stringify(record, null, 2)}
      </pre>
    </details>
  );
}

/**
 * 슬롯의 원격 대조 한 줄 — origin 주소 · 원격 HEAD 브랜치@커밋 · 슬롯이 그보다 앞/뒤 몇 커밋인지.
 * 값은 엔진(`status --json --with-remote`)이 센 그대로이고, 대조가 아직 없거나 실패하면 그 사실을 말한다.
 */
export function remoteLine(value: SlotRemote | null | undefined, entry: Entry | undefined): string {
  if (value) {
    const relation = value.ahead === 0 && value.behind === 0 ? "원격 HEAD 와 같음"
      : value.ahead === 0 ? `원격 HEAD 보다 ${value.behind} 커밋 뒤`
      : value.behind === 0 ? `원격 HEAD 보다 ${value.ahead} 커밋 앞`
      : `원격 HEAD 와 갈라짐 (앞 ${value.ahead} · 뒤 ${value.behind})`;
    return `${value.url} · ${value.head_branch}@${value.head.slice(0, 8)} · ${relation}`;
  }
  return entry?.error ? `원격 확인 실패 — ${entry.error.message}` : "원격 확인 전";
}

/** 현황 탭의 읽기 부분 — 보드 counts · task 표 · 슬롯 표 · 회귀/게이트 · lint 수. */
export default function StatusTab({ light, heavy, remote }:
  { light: Entry | undefined; heavy: Entry | undefined; remote?: Entry | undefined }) {
  const remoteOf = (slot: string) =>
    remote?.status?.slots.find((row) => row.slot === slot)?.remote;
  const document_ = light?.status;
  const board = heavy?.status?.board ?? null;
  if (!document_) {
    return (
      <p className="px-4 py-3 text-slate-500">
        {light?.error ? `현황을 읽지 못했다 — ${light.error.message}` : "현황 조회 전"}
      </p>
    );
  }
  return (
    <div className="space-y-4 px-4 py-3">
      <section>
        <h2 className="mb-1 font-medium">보드</h2>
        {board ? (
          <p data-testid="board-counts" className="text-slate-600">
            {Object.entries(board.counts).map(([name, count]) => `${name} ${count}`).join(" · ")}
            {" · lint "}{board.lint.length}
          </p>
        ) : (
          <p className="text-slate-500">{heavy?.error ? `보드를 읽지 못했다 — ${heavy.error.message}` : "선택 시 1회 조회"}</p>
        )}
        {board?.regression != null && (
          <MachineRecord name="regression" label="회귀" record={board.regression} />
        )}
        {board?.livegate != null && (
          <MachineRecord name="livegate" label="게이트" record={board.livegate} />
        )}
      </section>

      <section>
        <h2 className="mb-1 font-medium">task</h2>
        <table className="w-full">
          <tbody>
            {document_.tasks.map((task) => {
              const verdict = rowDot(task);
              return (
                <tr key={task.name} data-testid={`task-${task.name}`} className="align-top">
                  <td className="w-4 py-0.5"><StatusDot dot={verdict.dot} label={verdict.label} /></td>
                  <td className="py-0.5">{task.name}</td>
                  <td className="py-0.5 text-slate-500">{verdict.label}</td>
                  <td className="py-0.5 font-mono text-[11px] text-slate-500">
                    {task.alive ? "열림" : "닫힘"}
                    {task.prefix ? ` · prefix ${task.prefix}` : " · prefix 없음"}
                  </td>
                  <td className="py-0.5 text-right"><StatusRowActions row={task} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>

      <section>
        <div className="mb-1 flex items-center gap-2">
          <h2 className="font-medium">슬롯</h2>
          <SlotTableAllocate />
        </div>
        <table className="w-full">
          <tbody>
            {document_.slots.map((slot) => {
              const verdict = rowDot(slot);
              return (
                <tr key={slot.slot} data-testid={`slot-${slot.slot}`} className="align-top">
                  <td className="w-4 py-0.5">
                    <StatusDot dot={verdict.dot} label={verdict.label} testId={`slot-dot-${slot.slot}`} />
                  </td>
                  <td className="py-0.5 font-mono">{slot.slot}</td>
                  <td className="py-0.5 text-slate-500">
                    {verdict.label}
                  </td>
                  <td className="py-0.5 font-mono text-[11px] text-slate-500">
                    {holderLabel(slot) ?? "비어 있음"}{slot.branch ? ` · ${slot.branch}` : " · 브랜치 없음"}
                    {slot.head ? ` @${slot.head.slice(0, 8)}` : ""}
                    <div data-testid={`slot-remote-${slot.slot}`}>
                      {remoteLine(remoteOf(slot.slot), remote)}
                    </div>
                  </td>
                  <td className="py-0.5 text-right"><StatusRowActions row={slot} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </section>
    </div>
  );
}
