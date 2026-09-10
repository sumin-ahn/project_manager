import { useState } from "react";
import type { SlotRow, TaskRow } from "../api";
import { rowDot } from "../lib/status";
import type { DotVerdict } from "../lib/status";
import { useCommand, useProjectRun } from "../lib/run";
import CommandOutput from "./CommandOutput";
import ConfirmDialog from "./ConfirmDialog";

/**
 * 현황 탭의 동작 — 슬롯 표 머리의 [대여](repo 단위) · 슬롯 행의 [반납]·[새로고침] ·
 * task 행의 [prefix]·[종료].
 *
 * 아이들 규칙(§4.5): 그 행을 잡은 살아 있는 세션이 있으면 변경 동작이 비활성이고 사유가 붙는다.
 * 가부는 상태 판정표(`status-dots.json`)의 첫 일치가 그대로 낸다 — 관리자 쪽 생존 조회 코드는
 * 없고, 판정 필드는 엔진 JSON 값이다.
 *
 * [대여]가 표 머리에 있는 이유: `alloc` 은 repo 축이고 어느 idle 슬롯이 나갈지는 엔진이
 * 고른다(최소 번호). 행마다 버튼을 두면 아이들이 둘 이상일 때 누른 행과 대여되는 행이 다르다.
 *
 * 이름만 있는 동작(`task new`·`task reopen`)은 행이 없어 이 자리에 오지 않는다 — 종료한 task 는
 * `status --json` 의 `tasks` 에서 빠지므로(실측) 구성 탭의 task 절이 그 둘을 맡는다.
 */

const BUTTON = "rounded border border-slate-200 px-2 py-0.5 disabled:opacity-40";
const FIELD = "w-24 rounded border border-slate-200 px-1 font-mono text-[11px]";
const SELECT = "rounded border border-slate-200 px-1 text-[11px]";
/** 비활성 사유 한 줄 — 값은 엔진 JSON 에서 오고 문장만 화면 것이다. */
export function disabledReason(row: SlotRow | TaskRow, verdict: DotVerdict): string | null {
  if (verdict.actions !== "disabled") return null;
  const holder = "slot" in row ? row.session ?? row.slot : row.name;
  return `세션 ${holder} 이 잡고 있음`;
}

function Reason({ reason }: { reason: string | null }) {
  if (reason === null) return null;
  return <span data-testid="row-reason" className="text-[11px] text-slate-500">{reason}</span>;
}

/** 대여·반납이 함께 쓰는 task 고르기 — 목록은 그 사본의 `status --json` 값이다. */
function TaskSelect({ label, value, onChange }:
  { label: string; value: string; onChange: (value: string) => void }) {
  const { status } = useProjectRun();
  return (
    <select aria-label={label} value={value} className={SELECT}
            onChange={(event) => onChange(event.target.value)}>
      <option value="">task 선택</option>
      {(status?.tasks ?? []).map((task) => (
        <option key={task.name} value={task.name}>{task.name}</option>
      ))}
    </select>
  );
}

/**
 * 강제 닫기 확인 문구 — `release <slot> --force` 가 실제로 하는 일을 그대로 적는다.
 *
 * 그 강제는 dirty 슬롯의 커밋하지 않은 변경을 `git stash push --include-untracked` 로 밀어낸다
 * (엔진 `worktree_pool.force_release`). 그래서 "변경이 그대로 남는다" 고 적으면 사용자는
 * 관측하지 않은 사실을 읽고 승인하게 된다. 값(`dirty`)은 엔진 JSON 의 그 행 값이다.
 */
export function forceCloseNote(row: SlotRow): string {
  const stash = row.dirty
    ? "이 슬롯에는 커밋하지 않은 변경이 있다 — 그 변경은 stash 로 옮겨진다(작업 폴더에는 남지 않는다)."
    : "커밋하지 않은 변경이 생긴 뒤라면 그 변경은 stash 로 옮겨진다.";
  return `그 세션이 인계를 남기지 못하고 끝났을 때 쓴다. 아직 그 창이 살아 있으면 그 세션의 기록이 사라진다. 작업 폴더 자체는 남는다. ${stash}`;
}

/** 변경 동작이 없는 행 — 재조회만. 슬롯 내용 갱신(`pm-worktree refresh`)은 스킬이다. */
function RefreshOnly({ slot }: { slot: string }) {
  const { onChanged } = useProjectRun();
  return (
    <button type="button" data-testid={`refresh-${slot}`} onClick={onChanged} className={BUTTON}>
      새로고침
    </button>
  );
}

/** 슬롯 표 머리의 [대여] — repo 하나를 골라 `alloc <repo> --task <task>` 한 줄을 부른다. */
export function SlotTableAllocate() {
  const { status } = useProjectRun();
  const { outcome, busy, call } = useCommand();
  const [repo, setRepo] = useState("");
  const [task, setTask] = useState("");

  return (
    <span className="inline-flex flex-col gap-1">
      <span className="flex items-center gap-1">
        <select aria-label="대여할 repo" value={repo} className={SELECT}
                onChange={(event) => setRepo(event.target.value)}>
          <option value="">repo 선택</option>
          {(status?.repos ?? []).map((row) => (
            <option key={row.repo} value={row.repo}>{row.repo}</option>
          ))}
        </select>
        <TaskSelect label="대여 task" value={task} onChange={setTask} />
        <button type="button" data-testid="alloc" disabled={busy || repo === "" || task === ""}
                onClick={() => void call(["alloc", repo, "--task", task])}
                className={BUTTON}>대여</button>
      </span>
      <CommandOutput outcome={outcome} />
    </span>
  );
}

function SlotActions({ row }: { row: SlotRow }) {
  const verdict = rowDot(row);
  const { outcome, busy, call } = useCommand();
  const [closing, setClosing] = useState(false);
  const reason = disabledReason(row, verdict);

  // 가부는 판정표가 낸다 — 화면이 다시 재지 않는다. `change` 인 행(아이들 · 닫힘=종료 기록
  // 있음)은 잡고 있는 주체가 없어 [반납]이 열리고, `disabled`(열린 기록) 행은 사유와 함께
  // 비활성이다. 그 행만 [강제 닫기]를 갖는다 — 세션이 인계를 남기지 못하고 끝나는 일이 있어
  // (창이 그냥 닫히는 경우) 사람이 확인 한 번으로 `release <slot> --force` 를 부른다.
  // `refresh`(readonly)는 되돌릴 것이 없어 재조회만 남는다.
  if (verdict.actions === "refresh") {
    return <RefreshOnly slot={row.slot} />;
  }
  const open = verdict.actions === "change";
  return (
    <span className="inline-flex flex-col items-end gap-1">
      <span className="flex items-center gap-1">
        <Reason reason={reason} />
        <button type="button" data-testid={`release-${row.slot}`} title={reason ?? ""}
                disabled={!open || busy}
                onClick={() => void call(["release", row.slot])}
                className={BUTTON}>반납</button>
        {!open && (
          <button type="button" data-testid={`force-close-${row.slot}`} disabled={busy}
                  onClick={() => setClosing(true)} className={BUTTON}>강제 닫기</button>
        )}
      </span>
      {closing && (
        <ConfirmDialog
          title={`${row.slot} 의 열린 기록을 닫는다 — 계속?`}
          note={forceCloseNote(row)}
          value={row.slot}
          onConfirm={() => {
            setClosing(false);
            void call(["release", row.slot, "--force"]);
          }}
          onCancel={() => setClosing(false)}
        />
      )}
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
