import { Fragment, useCallback, useState } from "react";
import type { Entry } from "../App";
import type { Bridge, BridgeError, CommandResult, ConfSection, SlotRow } from "../api";
import { isError } from "../api";
import { updateAvailable } from "../lib/status";

/**
 * 갱신 탭 — 현재 rev ↔ 상류 최신 태그, `pm-config update --dry-run` 미리보기, 적용.
 *
 * 출력은 엔진의 사람용 여러 줄 텍스트다(JSON 없음). 화면은 그것을 **파싱하지 않고** 그대로
 * 띄우며, 어댑터 drift 두 줄만 정규식으로 눈에 띄게 한다. 진행 중인 세션이 있어도 막지 않고
 * 한 번 묻기만 한다 — 엔진은 이 검사를 하지 않고, 판단은 사람이 한다(spike §5.2).
 */

/** 강조할 두 줄 — 어댑터 drift 보고와 인스턴스 소유 템플릿 경고다(값 해석은 하지 않는다). */
export const DRIFT_LINE = /^→ 어댑터 config drift/;
export const WARNING_LINE = /^⚠️/;

/** 진행 중인 세션 수 — 열린 기록을 쥔 행만 센다(readonly 는 세션이 없어 빠진다). */
export function busySessionCount(rows: SlotRow[]): number {
  return rows.filter((row) => row.state === "leased" && row.alive).length;
}

/** 엔진 출력 한 덩이 — 줄을 지우거나 요약하지 않고, 두 줄만 눈에 띄게 한다. */
function EngineOutput({ result }: { result: CommandResult }) {
  const text = [result.stdout, result.stderr].filter(Boolean).join("");
  const lines = text.split("\n");
  return (
    <pre data-testid="update-output"
         className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">
      {lines.map((line, index) => (
        <Fragment key={index}>
          <span className={DRIFT_LINE.test(line) || WARNING_LINE.test(line)
            ? "text-amber-700" : undefined}>{line}</span>
          {index < lines.length - 1 ? "\n" : null}
        </Fragment>
      ))}
    </pre>
  );
}

export default function UpdateTab({ bridge, path, light, adapterConf, latestTag, onApplied }: {
  bridge: Bridge;
  path: string | null;
  light: Entry | undefined;
  /** 설정 탭 [불러오기] 가 받아 온 판정 — 그 조회만 "갱신하면 필요" 를 채운다(전이면 null). */
  adapterConf: ConfSection | null;
  latestTag: string | null;
  onApplied: () => void;
}) {
  const [result, setResult] = useState<CommandResult | null>(null);
  const [error, setError] = useState<BridgeError | null>(null);
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);

  const run = useCallback(async (which: "preview" | "apply") => {
    if (path === null) return;
    setBusy(true);
    setAsking(false);
    const response = which === "preview"
      ? await bridge.update_preview(path)
      : await bridge.update_apply(path);
    if (isError(response)) setError(response.error);
    else {
      setError(null);
      setResult(response);
      if (which === "apply") onApplied();      // 적용 뒤 그 프로젝트의 세 조회를 즉시 다시 돈다
    }
    setBusy(false);
  }, [bridge, path, onApplied]);

  if (path === null) {
    return <section data-testid="UpdateTab" className="px-4 py-3 text-slate-500">
      프로젝트를 고르면 갱신 상태가 온다
    </section>;
  }

  const currentRev = light?.status?.engine_rev ?? null;
  const busySessions = busySessionCount(light?.status?.slots ?? []);
  const pending = updateAvailable(currentRev, latestTag);

  return (
    <section data-testid="UpdateTab" className="space-y-3 px-4 py-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-slate-600">{currentRev ?? "알 수 없음"}</span>
        <span className="text-slate-400">↔</span>
        <span className="font-mono text-slate-600">{latestTag ?? "최신 태그 모름"}</span>
        {pending ? (
          <span data-testid="update-badge"
                className="rounded bg-amber-100 px-1 text-[11px] text-amber-700">갱신 있음 ↑</span>
        ) : latestTag !== null && currentRev !== null && (
          <span data-testid="update-badge"
                className="rounded bg-slate-100 px-1 text-[11px] text-slate-600">최신</span>
        )}
      </div>

      {adapterConf?.missing_for_latest != null && (
        <p data-testid="missing-for-latest" className="text-amber-700">
          갱신하면 필요한 설정 {adapterConf.missing_for_latest.length}
        </p>
      )}

      <div className="flex items-center gap-2">
        <button type="button" data-testid="update-preview" disabled={busy}
                onClick={() => void run("preview")}
                className="rounded border border-slate-200 px-2 py-0.5 hover:bg-slate-50">
          미리보기
        </button>
        <button type="button" data-testid="update-apply" disabled={busy}
                onClick={() => (busySessions > 0 ? setAsking(true) : void run("apply"))}
                className="rounded border border-slate-800 px-2 py-0.5">
          적용
        </button>
        {busySessions > 0 && (
          <span className="text-[11px] text-amber-700">진행 중인 세션 {busySessions}</span>
        )}
      </div>

      {asking && (
        <div data-testid="busy-confirm" role="alertdialog"
             className="rounded border border-amber-300 bg-amber-50 p-2">
          <p>진행 중인 세션 {busySessions} — 계속?</p>
          <p className="text-[11px] text-slate-600">
            엔진은 이 검사를 하지 않는다. 갱신은 막히지 않으며 판단은 사람이 한다.
          </p>
          <div className="mt-1 flex gap-2">
            <button type="button" data-testid="busy-continue" onClick={() => void run("apply")}
                    className="rounded border border-slate-800 px-2 py-0.5">계속</button>
            <button type="button" data-testid="busy-cancel" onClick={() => setAsking(false)}
                    className="rounded border border-slate-200 px-2 py-0.5">취소</button>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="text-red-600">갱신 호출이 실패했다 — {error.message}</p>
      )}
      {result && (
        <div className="space-y-1">
          <p data-testid="update-rc" className={result.rc === 0 ? "text-slate-600" : "text-red-600"}>
            rc {result.rc}
          </p>
          <EngineOutput result={result} />
        </div>
      )}
    </section>
  );
}
