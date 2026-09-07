import { RefreshCw } from "lucide-react";
import type { BridgeError } from "../api";

export type AreaState = { at?: number; error?: BridgeError | null; busy: boolean };

const AREA_LABEL = { light: "현황", heavy: "보드", tags: "최신 태그" } as const;

function clock(at?: number): string {
  return at === undefined ? "조회 전" : new Date(at).toLocaleTimeString("ko-KR", { hour12: false });
}

function Area({ kind, state }: { kind: keyof typeof AREA_LABEL; state: AreaState }) {
  return (
    <span data-testid={`area-${kind}`} data-busy={state.busy ? "true" : "false"}
          className={state.busy ? "opacity-50" : undefined}>
      {AREA_LABEL[kind]} {clock(state.at)}
      {state.error && (
        <span role="alert" title={state.error.stderr || state.error.message}
              className="ml-1 text-red-600">조회 실패 (마지막 값 유지)</span>
      )}
    </span>
  );
}

/** 영역별 마지막 조회 시각 · 조회 중 흐림 · 실패 시 마지막 값 유지 + 빨간 경고 · 수동 새로고침. */
export default function RefreshBar({ light, heavy, tags, onRefresh }:
  { light: AreaState; heavy: AreaState; tags: AreaState; onRefresh: () => void }) {
  return (
    <div className="flex items-center gap-4 border-b border-slate-200 px-4 py-1 text-[11px] text-slate-500">
      <Area kind="light" state={light} />
      <Area kind="heavy" state={heavy} />
      <Area kind="tags" state={tags} />
      <button type="button" aria-label="새로고침" onClick={onRefresh}
              className="ml-auto flex items-center gap-1 rounded px-1 py-0.5 hover:bg-slate-100">
        <RefreshCw size={12} /> 새로고침
      </button>
    </div>
  );
}
