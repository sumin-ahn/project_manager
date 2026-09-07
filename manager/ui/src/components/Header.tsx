import type { Entry } from "../App";
import { slotSummary } from "../lib/status";

/** 가운데 칸 머리글 — 선택 프로젝트의 이름·버전·최신·정체성(task/슬롯). */
export default function Header({ name, entry, latestTag }:
  { name: string; entry: Entry | undefined; latestTag: string | null }) {
  const document_ = entry?.status;
  const tasks = document_?.tasks ?? [];
  const slots = document_?.slots ?? [];
  return (
    <header className="flex items-baseline gap-3 border-b border-slate-200 px-4 py-2">
      <h1 className="text-[15px] font-medium">{name}</h1>
      <span className="font-mono text-slate-600">{document_?.engine_rev ?? "알 수 없음"}</span>
      {latestTag && (
        <span className="font-mono text-[11px] text-slate-500">최신 {latestTag}</span>
      )}
      <span className="text-slate-500">
        {tasks.length > 0 ? `task ${tasks.map((task) => task.name).join(" · ")}` : "task 없음"}
      </span>
      {slots.length > 0 && <span className="text-slate-500">{slotSummary(slots)}</span>}
    </header>
  );
}
