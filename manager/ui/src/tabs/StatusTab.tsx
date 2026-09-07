import type { Entry } from "../App";
import { rowDot } from "../lib/status";
import StatusDot from "../components/StatusDot";
import StatusRowActions from "../components/StatusRowActions";

/** 현황 탭의 읽기 부분 — 보드 counts · task 표 · 슬롯 표 · 회귀/게이트 · lint 수. */
export default function StatusTab({ light, heavy }:
  { light: Entry | undefined; heavy: Entry | undefined }) {
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
          <p className="font-mono text-[11px] text-slate-500">회귀 {JSON.stringify(board.regression)}</p>
        )}
        {board?.livegate != null && (
          <p className="font-mono text-[11px] text-slate-500">게이트 {JSON.stringify(board.livegate)}</p>
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
                    {task.pid === 0 ? "pid 없음" : `pid ${task.pid}`}
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
        <h2 className="mb-1 font-medium">슬롯</h2>
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
                    {verdict.dot === "red" && " · 다음 bootstrap 이 회수"}
                  </td>
                  <td className="py-0.5 font-mono text-[11px] text-slate-500">
                    {slot.session ?? "세션 없음"}{slot.branch ? ` · ${slot.branch}` : ""}
                    {slot.head ? ` @${slot.head.slice(0, 8)}` : ""}
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
