import { useCallback, useState } from "react";
import type { AdapterFile, AdapterResponse, Bridge, BridgeError, ConfSection } from "../api";
import { isError } from "../api";

/**
 * 어댑터 파일 절 — 파일별 상태·기준 버전·기능 단위 거리·차이 둘·수용.
 *
 * **[불러오기] 버튼으로만 돈다.** 소스 해소가 URL upstream 이면 엔진이 전체 clone 을 하므로 탭을
 * 여는 것만으로는 네트워크가 나가지 않는다. 수용은 그 사본의 `sync-adapter-config --accept` 이고
 * 관리자가 파일을 복사하지 않는다.
 *
 * 이 호출은 소스가 해소된 유일한 조회라 응답에 설정 완전성 `conf` 절이 함께 온다. 그 절의
 * "갱신하면 필요"(`missing_for_latest`)를 쓰는 곳은 설정 탭 배너·행 배지와 갱신 탭 한 줄이라,
 * 받은 값을 `onConfLoaded` 로 넘긴다(조회를 새로 만들지 않는다).
 */

const STATUS_LABEL: Record<string, string> = {
  "in-sync": "최신",
  unedited: "무편집",
  edited: "채택자 편집",
  unrecorded: "원장 부재",
  unchecked: "검사 없음",
};

const VERDICT_LABEL: Record<string, string> = {
  same: "같음", missing: "없음", differs: "다름", retired: "폐지됨",
};

/** 채널 있는 파일인가 — 인스턴스 소유 문서는 `mode` 키 자체가 없다(null 이 아니다). */
export function hasChannel(file: AdapterFile): boolean {
  return "mode" in file;
}

function label(status: string | null): string {
  return status === null ? "파일 없음" : STATUS_LABEL[status] ?? status;
}

/** 편집 줄 수는 **둘 다 숫자일 때만** 뜻이 있다 — 판정 없는 행은 두 칸이 널이라 그리지 않는다. */
function editedLines(file: AdapterFile): string {
  const { added, removed } = file.diff_lines ?? {};
  return typeof added === "number" && typeof removed === "number"
    ? ` · 편집 +${added}/-${removed}` : "";
}

function shown(value: AdapterFile["distance"]): string {
  return value === undefined ? "" : `최신과 ${value}개 다름`;
}

function FeatureTable({ file }: { file: AdapterFile }) {
  return (
    <table data-testid={`features-${file.path}`} className="w-full">
      <tbody>
        {(file.features ?? []).map((feature) => (
          <tr key={feature.name}>
            <td className="w-64 py-0.5 pl-4 text-[11px]">▸ {feature.name}</td>
            <td className="py-0.5 font-mono text-[11px] text-slate-500">
              {String(feature.project ?? "없음")} → {String(feature.latest ?? "없음")}
            </td>
            <td className="w-20 py-0.5 text-[11px]">{VERDICT_LABEL[feature.verdict]}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function AdapterSection({ bridge, path, onConfLoaded }:
  { bridge: Bridge; path: string; onConfLoaded: (conf: ConfSection) => void }) {
  const [section, setSection] = useState<AdapterResponse | null>(null);
  const [error, setError] = useState<BridgeError | null>(null);
  const [busy, setBusy] = useState(false);
  const [opened, setOpened] = useState<Record<string, "features" | "diff" | null>>({});
  const [outcome, setOutcome] = useState<string | null>(null);

  const load = useCallback(async () => {
    setBusy(true);
    const response = await bridge.adapters(path);
    if (isError(response)) setError(response.error);
    else {
      setError(null);
      setSection(response);
      onConfLoaded(response.conf);
    }
    setBusy(false);
  }, [bridge, path, onConfLoaded]);

  const accept = useCallback(async (relpath: string) => {
    setBusy(true);
    const response = await bridge.adapter_accept(path, relpath);
    if (isError(response)) setOutcome(response.error.stderr || response.error.message);
    else {
      setOutcome(response.rc === 0 ? response.stdout : response.stderr);
      if (response.rc === 0) await load();
    }
    setBusy(false);
  }, [bridge, path, load]);

  const toggle = (file: string, panel: "features" | "diff") =>
    setOpened((previous) => ({ ...previous,
      [file]: previous[file] === panel ? null : panel }));

  return (
    <section data-testid="adapter-section" className="border-t border-slate-200 pt-3">
      <div className="mb-1 flex items-center gap-2">
        <h2 className="font-medium">어댑터 파일</h2>
        {section && (
          <span className="font-mono text-[11px] text-slate-500">소스 {section.source}</span>
        )}
        <button type="button" data-testid="adapter-load" disabled={busy} onClick={() => void load()}
                className="ml-auto rounded border border-slate-200 px-2 py-0.5">불러오기</button>
      </div>
      {error && (
        <p role="alert" className="text-red-600">
          어댑터 판정을 읽지 못했습니다. {error.message}
          {error.stderr && <span className="ml-1 font-mono text-[11px]">{error.stderr}</span>}
        </p>
      )}
      {outcome && (
        <pre data-testid="adapter-outcome"
             className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">{outcome}</pre>
      )}
      {!section && !error && (
        <p className="text-slate-500">[불러오기]를 누르면 판정을 가져옵니다. 원본을 찾는 데 시간이 걸립니다.</p>
      )}
      {section && (
        <table className="w-full">
          <tbody>
            {section.files.map((file) => (
              <tr key={file.path} data-testid={`adapter-${file.path}`} className="align-top">
                <td title={file.path}
                    className="w-64 py-0.5 font-mono text-[11px] whitespace-nowrap">{file.path}</td>
                {hasChannel(file) ? (
                  <>
                    <td className="w-24 py-0.5 text-[11px]">{file.harness}</td>
                    <td className="w-24 py-0.5 text-[11px]">
                      {file.mode === "managed" ? "managed" : "인스턴스"}
                    </td>
                    <td className="w-28 py-0.5 text-[11px]">{label(file.status)}</td>
                    <td className="w-28 py-0.5 font-mono text-[11px] text-slate-500">
                      {file.template_rev?.slice(0, 8)}
                    </td>
                    <td className="py-0.5 text-[11px] text-slate-500 whitespace-nowrap">
                      {shown(file.distance)}
                      {editedLines(file)}
                    </td>
                    <td className="w-56 py-0.5 text-right text-[11px] whitespace-nowrap">
                      <button type="button" onClick={() => toggle(file.path, "features")}
                              className="rounded border border-slate-200 px-1">기능 보기</button>
                      <button type="button" onClick={() => toggle(file.path, "diff")}
                              className="ml-1 rounded border border-slate-200 px-1">차이</button>
                      {/* 판정 없는 행(대개 dest 파일 부재)에 수용은 무대상 — 버튼을 닫는다. */}
                      <button type="button" disabled={busy || file.status === null}
                              onClick={() => void accept(file.path)}
                              className="ml-1 rounded border border-slate-200 px-1">수용</button>
                    </td>
                  </>
                ) : (
                  <td className="py-0.5 text-[11px] text-slate-500" colSpan={6}>
                    인스턴스 소유 · {label(file.status)}
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {section?.files.filter((file) => opened[file.path] === "features").map((file) => (
        <FeatureTable key={file.path} file={file} />
      ))}
      {section?.files.filter((file) => opened[file.path] === "diff").map((file) => (
        <div key={file.path} data-testid={`diff-${file.path}`} className="space-y-1">
          <p className="text-[11px] text-slate-500">설치 기준 ↔ 현재</p>
          <pre className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">
            {file.diff_vs_baseline || "차이 없음"}
          </pre>
          <p className="text-[11px] text-slate-500">현재 ↔ 최신</p>
          <pre className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">
            {file.diff_vs_latest || "차이 없음"}
          </pre>
        </div>
      ))}
    </section>
  );
}
