import * as RadixTabs from "@radix-ui/react-tabs";
import { useState } from "react";
import type { Entry } from "../App";
import type { SlotRow, TaskRow } from "../api";
import { rowDot } from "../lib/status";
import SlotRemote from "../components/SlotRemote";
import StatusDot from "../components/StatusDot";
import StatusRowActions, { holderLabel, SlotTableAllocate, TaskSlotAllocate, TaskTodoList }
  from "../components/StatusRowActions";

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

/** 현황 탭의 읽기 부분 — 보드 counts · task 표 · 슬롯 표 · 회귀/게이트 · lint 수. */
export default function StatusTab({ light, heavy, remote }:
  { light: Entry | undefined; heavy: Entry | undefined; remote?: Entry | undefined }) {
  const document_ = light?.status;
  const board = heavy?.status?.board ?? null;
  if (!document_) {
    return (
      <p className="px-4 py-3 text-slate-500">
        {light?.error ? `현황을 읽지 못했습니다 — ${light.error.message}` : "현황 조회 전"}
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
          <p className="text-slate-500">{heavy?.error ? `보드를 읽지 못했습니다 — ${heavy.error.message}` : "선택 시 1회 조회"}</p>
        )}
        {board?.regression != null && (
          <MachineRecord name="regression" label="회귀" record={board.regression} />
        )}
        {board?.livegate != null && (
          <MachineRecord name="livegate" label="게이트" record={board.livegate} />
        )}
      </section>

      <TaskTabs tasks={document_.tasks} slots={document_.slots} remote={remote} />
    </div>
  );
}

const TAB_TRIGGER =
  "px-3 py-1 border-b-2 border-transparent data-[state=active]:border-slate-800";

/** 어느 task 에도 속하지 않는 슬롯이 모이는 자리 — 비어 있어도 대여는 여기 하나다. */
const SHARED = "__shared__";

/** 슬롯 한 줄 — 점·이름·판정·보유자·브랜치·원격 대조·행 동작. */
function SlotRow_({ slot, remote }: { slot: SlotRow; remote?: Entry | undefined }) {
  const verdict = rowDot(slot);
  return (
    <tr data-testid={`slot-${slot.slot}`} className="align-top">
      <td className="w-4 py-0.5">
        <StatusDot dot={verdict.dot} label={verdict.label} testId={`slot-dot-${slot.slot}`} />
      </td>
      <td className="py-0.5 font-mono">{slot.slot}</td>
      <td className="py-0.5 text-slate-500">{verdict.label}</td>
      <td className="py-0.5 font-mono text-[11px] text-slate-500">
        {holderLabel(slot) ?? "비어 있음"}{slot.branch ? ` · ${slot.branch}` : " · 브랜치 없음"}
        {slot.head ? ` @${slot.head.slice(0, 8)}` : ""}
        <SlotRemote slot={slot} remote={remote} />
      </td>
      <td className="py-0.5 text-right"><StatusRowActions row={slot} /></td>
    </tr>
  );
}

/**
 * task 별 탭 — 탭 하나가 task 하나이고, 그 안에 그 task 의 상태·열린 todo·보유 슬롯만 선다.
 *
 * 어느 task 도 쥐지 않은 슬롯(아이들·readonly·세션이 잡은 것)은 마지막 `공용` 탭에 모인다 —
 * task 탭에 남의 슬롯이 섞이면 "이 task 가 무엇을 쥐고 있나" 를 한눈에 못 읽는다. 대여는
 * repo 축이라 공용 탭에 하나만 둔다(누른 자리와 대여되는 자리가 갈리지 않게).
 */
function TaskTabs({ tasks, slots, remote }:
  { tasks: TaskRow[]; slots: SlotRow[]; remote?: Entry | undefined }) {
  const names = [...tasks.map((task) => task.name), SHARED];
  const [picked, setPicked] = useState(names[0]);
  // 프로젝트를 바꾸면 고른 탭이 사라질 수 있다 — 없는 탭을 고른 채로 두지 않는다.
  const active = names.includes(picked) ? picked : names[0];
  const shared = slots.filter((slot) => slot.task == null);

  return (
    <RadixTabs.Root value={active} onValueChange={setPicked}>
      <RadixTabs.List className="flex border-b border-slate-200">
        {tasks.map((task) => (
          <RadixTabs.Trigger key={task.name} value={task.name} className={TAB_TRIGGER}
                             data-testid={`task-tab-${task.name}`}>
            {task.name}
          </RadixTabs.Trigger>
        ))}
        <RadixTabs.Trigger value={SHARED} className={TAB_TRIGGER} data-testid="task-tab-shared">
          공용
        </RadixTabs.Trigger>
      </RadixTabs.List>

      {tasks.map((task) => {
        const verdict = rowDot(task);
        const held = slots.filter((slot) => slot.task === task.name);
        return (
          <RadixTabs.Content key={task.name} value={task.name} className="pt-2">
            <div data-testid={`task-${task.name}`} className="flex items-start justify-between gap-2">
              <span className="flex items-start gap-2">
                <StatusDot dot={verdict.dot} label={verdict.label} />
                <span className="flex flex-col items-start">
                  <span>{task.name} <span className="text-slate-500">{verdict.label}</span></span>
                  <span className="font-mono text-[11px] text-slate-500">
                    {task.alive ? "열림" : "닫힘"}
                    {task.prefix ? ` · prefix ${task.prefix}` : " · prefix 없음"}
                  </span>
                </span>
              </span>
              <StatusRowActions row={task} />
            </div>
            {/* task 아래 두 절은 같은 모양·같은 자리에서 시작한다 — 한쪽만 제목이 있으면
                그 절이 task 와 동급으로 읽힌다. */}
            <div className="mt-4 space-y-4">
              <section className="space-y-1">
                <div className="flex items-center gap-2">
                  {/* task 는 슬롯을 여럿 쥘 수 있다 — 개수를 적고, 더 대여하는 자리를 여기 둔다. */}
                  <h2 className="font-medium" data-testid={`held-count-${task.name}`}>
                    쥔 슬롯 {held.length}개
                  </h2>
                  <TaskSlotAllocate task={task.name} />
                </div>
                {held.length === 0 ? (
                  <p className="text-slate-500">이 task 가 쥔 슬롯이 없습니다 — 위에서 repo 를 골라 대여합니다.</p>
                ) : (
                  <table className="w-full">
                    <tbody>
                      {held.map((slot) => <SlotRow_ key={slot.slot} slot={slot} remote={remote} />)}
                    </tbody>
                  </table>
                )}
              </section>
              <TaskTodoList row={task} />
            </div>
          </RadixTabs.Content>
        );
      })}

      <RadixTabs.Content value={SHARED} className="pt-2">
        <section className="space-y-1">
          <div className="flex items-center gap-2">
            <h2 className="font-medium">어느 task 도 쥐지 않은 슬롯</h2>
            <SlotTableAllocate />
          </div>
          {shared.length === 0 ? (
            <p className="text-slate-500">모든 슬롯을 task 가 쥐고 있습니다.</p>
          ) : (
            <table className="w-full">
              <tbody>
                {shared.map((slot) => <SlotRow_ key={slot.slot} slot={slot} remote={remote} />)}
              </tbody>
            </table>
          )}
        </section>
      </RadixTabs.Content>
    </RadixTabs.Root>
  );
}
