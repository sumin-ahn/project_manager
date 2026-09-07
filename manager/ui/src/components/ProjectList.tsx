import { Plus, X } from "lucide-react";
import type { Entry } from "../App";
import type { BridgeError, Project, TagsResponse } from "../api";
import { latestTagOf, projectDot, updateAvailable } from "../lib/status";
import StatusDot from "./StatusDot";

/** 왼쪽 2단의 왼칸 — 등록 프로젝트 전부(가벼운 절의 대상). */
export default function ProjectList({
  projects, light, tags, selected, listError, onSelect, onAdd, onRemove,
}: {
  projects: Project[];
  light: Record<string, Entry>;
  tags: TagsResponse | null;
  selected: string | null;
  listError: BridgeError | null;
  onSelect: (path: string) => void;
  onAdd: () => void;
  onRemove: (path: string) => void;
}) {
  return (
    <aside className="flex w-72 shrink-0 flex-col border-r border-slate-200">
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-200">
        <span className="font-medium">Projects</span>
        <button type="button" aria-label="프로젝트 추가" onClick={onAdd}
                className="rounded p-1 hover:bg-slate-100">
          <Plus size={14} />
        </button>
      </div>
      {listError && (
        <p role="alert" className="px-3 py-2 text-red-600">
          등록부를 읽지 못했다 — {listError.message}
        </p>
      )}
      <ul className="flex-1 overflow-y-auto">
        {projects.map((project) => {
          const entry = light[project.path];
          const document_ = entry?.status;
          const rows = [...(document_?.slots ?? []), ...(document_?.tasks ?? [])];
          const latest = latestTagOf(tags, project.path);
          return (
            <li key={project.path}>
              <div
                data-testid={`project-${project.name}`}
                className={`flex cursor-pointer items-start gap-2 px-3 py-1.5 hover:bg-slate-50 ${
                  selected === project.path ? "bg-slate-100" : ""}`}
                onClick={() => onSelect(project.path)}
              >
                <StatusDot
                  dot={document_ ? projectDot(rows) : "gray"}
                  label={document_ ? "" : "현황 없음"}
                  testId={`dot-${project.name}`}
                />
                <div className="min-w-0 flex-1">
                  <div className="truncate">{project.name}</div>
                  <div className="truncate font-mono text-[11px] text-slate-500">
                    {project.path}
                  </div>
                  <div className="flex items-center gap-1">
                    <span className="font-mono text-[11px] text-slate-600">
                      {document_?.engine_rev ?? "알 수 없음"}
                    </span>
                    {updateAvailable(document_?.engine_rev, latest) && (
                      <span className="rounded bg-amber-100 px-1 text-[11px] text-amber-700">
                        갱신 있음 ↑
                      </span>
                    )}
                    {entry?.error && (
                      <span role="alert"
                            title={entry.error.stderr || entry.error.message}
                            className="rounded bg-red-100 px-1 text-[11px] text-red-700">
                        먼저 갱신
                      </span>
                    )}
                  </div>
                </div>
                <button type="button" aria-label={`${project.name} 등록 해제`}
                        onClick={(event) => { event.stopPropagation(); onRemove(project.path); }}
                        className="rounded p-1 text-slate-400 hover:bg-slate-100">
                  <X size={12} />
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
