import { useState } from "react";
import type { SlotRow, TaskRow } from "../api";
import { rowDot } from "../lib/status";
import type { DotVerdict } from "../lib/status";
import { useCommand, useProjectRun } from "../lib/run";
import CommandOutput from "./CommandOutput";
import ConfirmDialog from "./ConfirmDialog";

/**
 * 현황 탭 행의 동작 — 슬롯 행([대여]·[반납]·readonly 는 [새로고침]) · task 행([prefix]·[종료]).
 *
 * 아이들 규칙(§4.5): 그 행을 잡은 살아 있는 세션이 있으면 변경 동작이 비활성이고 사유가 붙는다.
 * 가부는 상태 판정표(`status-dots.json`)의 첫 일치가 그대로 낸다 — 관리자 쪽 pid 조회 코드는
 * 없고, `alive` 는 엔진 JSON 값이다.
 *
 * 이름만 있는 동작(`task new`·`task reopen`)은 행이 없어 이 자리에 오지 않는다 — 종료한 task 는
 * `status --json` 의 `tasks` 에서 빠지므로(실측) 구성 탭의 task 절이 그 둘을 맡는다.
 */

const BUTTON = "rounded border border-slate-200 px-2 py-0.5 disabled:opacity-40";
const FIELD = "w-24 rounded border border-slate-200 px-1 font-mono text-[11px]";
/** 회수 대기 행의 사유 — 관리자가 회수하지 않는다(다음 bootstrap 이 한다). */
const RECLAIM_REASON = "다음 bootstrap 이 회수";

/** 비활성 사유 한 줄 — 값은 엔진 JSON 에서 오고 문장만 화면 것이다. */
export function disabledReason(row: SlotRow | TaskRow, verdict: DotVerdict): string | null {
  if (verdict.actions !== "disabled") return null;
  if (verdict.dot === "red") return RECLAIM_REASON;
  const holder = "slot" in row ? row.session ?? row.slot : row.name;
  return `세션 ${holder} 이 잡고 있음`;
}

function Reason({ reason }: { reason: string | null }) {
  if (reason === null) return null;
  return <span data-testid="row-reason" className="text-[11px] text-slate-500">{reason}</span>;
}

/** readonly 행 — 변경 동작 없이 재조회만. 슬롯 내용 갱신(`pm-worktree refresh`)은 스킬이다. */
function RefreshOnly({ slot }: { slot: string }) {
  const { onChanged } = useProjectRun();
  return (
    <button type="button" data-testid={`refresh-${slot}`} onClick={onChanged} className={BUTTON}>
      새로고침
    </button>
  );
}

function SlotActions({ row }: { row: SlotRow }) {
  const verdict = rowDot(row);
  const { status } = useProjectRun();
  const { outcome, busy, call } = useCommand();
  const [task, setTask] = useState("");
  const tasks = status?.tasks ?? [];
  const reason = disabledReason(row, verdict);

  if (verdict.actions === "refresh") return <RefreshOnly slot={row.slot} />;
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className="flex items-center gap-1">
        <Reason reason={reason} />
        {verdict.actions === "change" ? (
          <>
            <select aria-label={`${row.slot} 대여 task`} value={task}
                    onChange={(event) => setTask(event.target.value)}
                    className="rounded border border-slate-200 px-1 text-[11px]">
              <option value="">task 선택</option>
              {tasks.map((row_) => <option key={row_.name} value={row_.name}>{row_.name}</option>)}
            </select>
            {/* `alloc` 은 repo 축이다 — 어느 idle 슬롯이 나갈지는 엔진이 고른다(최소 번호). */}
            <button type="button" data-testid={`alloc-${row.slot}`} disabled={busy || task === ""}
                    onClick={() => void call(["alloc", row.repo, "--task", task])}
                    className={BUTTON}>대여</button>
          </>
        ) : (
          <button type="button" data-testid={`release-${row.slot}`} disabled title={reason ?? ""}
                  className={BUTTON}>반납</button>
        )}
      </span>
      <CommandOutput outcome={outcome} />
    </span>
  );
}

function TaskActions({ row }: { row: TaskRow }) {
  const verdict = rowDot(row);
  const { outcome, busy, call } = useCommand();
  const [prefix, setPrefix] = useState("");
  const [asking, setAsking] = useState(false);
  const reason = disabledReason(row, verdict);
  const frozen = verdict.actions !== "change";
  const value = prefix.trim();

  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className="flex items-center gap-1">
        <Reason reason={reason} />
        <input aria-label={`${row.name} prefix`} value={prefix} disabled={frozen}
               onChange={(event) => setPrefix(event.target.value)} className={FIELD} />
        <button type="button" data-testid={`prefix-${row.name}`} title={reason ?? ""}
                disabled={frozen || busy || value === ""} onClick={() => setAsking(true)}
                className={BUTTON}>prefix</button>
        <button type="button" data-testid={`end-${row.name}`} title={reason ?? ""}
                disabled={frozen || busy} onClick={() => void call(["task", "end", row.name])}
                className={BUTTON}>종료</button>
      </span>
      {asking && (
        <ConfirmDialog
          title={`task ${row.name} 의 prefix 를 ${value} 로 —  승인?`}
          note="엔진은 새 prefix 신설에 사용자 승인을 요구한다. 승인은 이 값에만 결속된다."
          value={value}
          onConfirm={(ack) => {
            setAsking(false);
            // `ack` 는 대화가 돌려준 값이다 — 이 자리에서 만들어 붙이는 문자열이 아니다.
            void call(["task", "prefix", row.name, value, "--user-ack", ack]);
          }}
          onCancel={() => setAsking(false)}
        />
      )}
      <CommandOutput outcome={outcome} />
    </span>
  );
}

export default function StatusRowActions({ row }: { row: SlotRow | TaskRow }) {
  return (
    <span data-testid="row-actions" className="inline-block">
      {"slot" in row ? <SlotActions row={row} /> : <TaskActions row={row} />}
    </span>
  );
}
