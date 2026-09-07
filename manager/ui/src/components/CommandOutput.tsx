import type { CommandResult, ErrorResponse } from "../api";
import { isError } from "../api";

/**
 * 사본 명령 하나의 산출 — rc≠0 이면 stderr 원문이 빨간 박스로 그대로 보이고, rc 0 이면
 * stdout 이 접힌 채 붙는다.
 *
 * 줄을 지우거나 요약하지 않는다. 엔진의 거부 문구(승인 토큰·소유검사·dirty)가 화면의 답이라
 * 관리자가 다시 쓸 문장이 없다. 브리지가 부르기를 거절한 자리(허용 목록 밖·`--force`)는
 * `stderr` 가 빈 값인 오류로 온다.
 */
export default function CommandOutput({ outcome }:
  { outcome: CommandResult | ErrorResponse | null }) {
  if (outcome === null) return null;
  if (isError(outcome)) {
    return (
      <div data-testid="command-error" role="alert"
           className="rounded border border-red-300 bg-red-50 p-2 text-red-700">
        <p>{outcome.error.message}</p>
        {outcome.error.stderr && (
          <pre className="whitespace-pre-wrap font-mono text-[11px]">{outcome.error.stderr}</pre>
        )}
      </div>
    );
  }
  if (outcome.rc !== 0) {
    const text = [outcome.stderr, outcome.stdout].filter(Boolean).join("");
    return (
      <div data-testid="command-failed" role="alert"
           className="rounded border border-red-300 bg-red-50 p-2">
        <p className="text-red-700">rc {outcome.rc}</p>
        <pre data-testid="command-stderr"
             className="whitespace-pre-wrap font-mono text-[11px] text-red-700">{text}</pre>
      </div>
    );
  }
  return (
    <details data-testid="command-ok" className="rounded border border-slate-200 p-1">
      <summary className="cursor-pointer text-slate-600">rc 0</summary>
      <pre className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">
        {[outcome.stdout, outcome.stderr].filter(Boolean).join("")}
      </pre>
    </details>
  );
}
