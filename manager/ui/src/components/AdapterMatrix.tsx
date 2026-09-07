import { X } from "lucide-react";
import { useCallback, useState } from "react";
import type { AdapterDocument, AdapterMatrixDocument, Bridge, BridgeError } from "../api";
import { isError } from "../api";

/**
 * 전체 보기 — 파일 × 프로젝트 매트릭스. 칸 값은 기능 단위 거리다.
 *
 * 등록 프로젝트마다 무거운 조회 한 번이라 **버튼으로만** 돌고 주기가 없다. 프로젝트 하나가
 * 실패해도 나머지는 그대로 온다(칸에 "오류").
 */

/** 칸 하나 — 채널 없는 파일은 거리 축이 없다. */
export function cellText(document_: AdapterDocument, file: string): string {
  const row = document_.files.find((entry) => entry.path === file);
  if (row === undefined) return "없음";
  if (!("mode" in row)) return "검사 없음";
  return String(row.distance);
}

function projectName(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export default function AdapterMatrix({ bridge, onClose }:
  { bridge: Bridge; onClose: () => void }) {
  const [matrix, setMatrix] = useState<AdapterMatrixDocument | null>(null);
  const [error, setError] = useState<BridgeError | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    const response = await bridge.adapter_matrix();
    if (isError(response)) setError(response.error);
    else {
      setError(null);
      setMatrix(response);
    }
    setBusy(false);
  }, [bridge]);

  const projects = matrix === null ? [] : Object.keys(matrix);
  const files = [...new Set(projects.flatMap((project) => {
    const entry = matrix![project];
    return isError(entry) ? [] : entry.files.map((file) => file.path);
  }))].sort();

  return (
    <div data-testid="adapter-matrix"
         className="absolute inset-8 z-10 overflow-auto rounded border border-slate-300 bg-white p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="font-medium">어댑터 전체 보기</h2>
        <span className="text-[11px] text-slate-500">칸 = 최신과 다른 기능 수</span>
        <button type="button" data-testid="matrix-load" disabled={busy} onClick={() => void load()}
                className="ml-auto rounded border border-slate-200 px-2 py-0.5">불러오기</button>
        <button type="button" aria-label="전체 보기 닫기" onClick={onClose}
                className="rounded p-1 hover:bg-slate-100"><X size={14} /></button>
      </div>
      {error && <p role="alert" className="text-red-600">등록부를 읽지 못했다 — {error.message}</p>}
      {matrix === null && !error && (
        <p className="text-slate-500">[불러오기] 를 누르면 등록 프로젝트 전부를 한 번 조회한다</p>
      )}
      {matrix !== null && (
        <table className="w-full">
          <thead>
            <tr>
              <th className="py-0.5 text-left font-medium">파일</th>
              {projects.map((project) => (
                <th key={project} className="py-0.5 text-left font-medium">
                  {projectName(project)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file} data-testid={`matrix-row-${file}`}>
                <td className="py-0.5 font-mono text-[11px]">{file}</td>
                {projects.map((project) => {
                  const entry = matrix[project];
                  return (
                    <td key={project} data-testid={`cell-${projectName(project)}-${file}`}
                        className="py-0.5 text-[11px]">
                      {isError(entry) ? (
                        <span title={entry.error.stderr || entry.error.message}
                              className="text-red-600">오류</span>
                      ) : cellText(entry, file)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
