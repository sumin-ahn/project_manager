import { X } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { ApplyDefaults, ApplyStep, Bridge, BridgeError } from "../api";
import { isError } from "../api";

/**
 * 새 프로젝트 적용 — 일곱 값을 받아 `pm_import.py --new` → `pm-config init` → 등록을 부른다.
 *
 * 화면이 스스로 만드는 값은 없다. 하네스 목록과 upstream 기본값은 번들 엔진에서 오고
 * (`apply_defaults`), prefix 승인 토큰은 **확인 대화를 누른 그 값**만 브리지로 간다. 단계마다의
 * rc·출력은 엔진 원문 그대로 쌓이고, 실패해도 그때까지의 로그가 남는다.
 */

/** 폴더명 — 이름 칸의 기본값이다(윈도우 구분자도 같이 자른다). */
export function folderName(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

const FIELD = "w-[30rem] rounded border border-slate-200 px-1 font-mono text-[11px]";

function StepLog({ step }: { step: ApplyStep }) {
  const text = [step.stdout, step.stderr].filter(Boolean).join("");
  return (
    <div data-testid={`step-${step.name}`}>
      <p className={step.rc === 0 ? "text-slate-600" : "text-red-600"}>
        {step.name} — rc {step.rc}
      </p>
      {text && (
        <pre className="whitespace-pre-wrap bg-slate-50 p-2 font-mono text-[11px]">{text}</pre>
      )}
    </div>
  );
}

export default function NewProjectDialog({ bridge, onClose, onApplied }: {
  bridge: Bridge;
  onClose: () => void;
  onApplied: (path: string) => void;
}) {
  const [defaults, setDefaults] = useState<ApplyDefaults | null>(null);
  const [dest, setDest] = useState("");
  const [harness, setHarness] = useState("");
  const [name, setName] = useState("");
  const [prefix, setPrefix] = useState("");
  const [area, setArea] = useState("");
  const [owner, setOwner] = useState("");
  const [upstream, setUpstream] = useState("");
  const [steps, setSteps] = useState<ApplyStep[]>([]);
  const [error, setError] = useState<BridgeError | null>(null);
  const [busy, setBusy] = useState(false);
  const [asking, setAsking] = useState(false);

  // 하네스 목록과 각인 upstream 은 번들이 답한다 — 화면에 박은 상수가 없다.
  useEffect(() => {
    void (async () => {
      const response = await bridge.apply_defaults();
      if (isError(response)) setError(response.error);
      else {
        setDefaults(response);
        setHarness(response.harnesses[0] ?? "");
        setUpstream(response.upstream);
      }
    })();
  }, [bridge]);

  const pick = useCallback(async () => {
    const picked = await bridge.pick_folder();
    if (isError(picked) || picked.path === null) return;
    setDest(picked.path);
    setName(folderName(picked.path));
  }, [bridge]);

  const run = useCallback(async (prefixAck: string) => {
    setBusy(true);
    setAsking(false);
    const response = await bridge.apply_new(
      dest, harness, name, prefix, area, owner, upstream, prefixAck);
    if (isError(response)) {
      setError(response.error);
      setSteps(response.steps ?? []);
    } else {
      setError(null);
      setSteps(response.steps);
      if (response.ok && response.path !== null) onApplied(response.path);
    }
    setBusy(false);
  }, [bridge, dest, harness, name, prefix, area, owner, upstream, onApplied]);

  return (
    <div data-testid="new-project"
         className="absolute inset-8 z-10 overflow-auto rounded border border-slate-300 bg-white p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-2">
        <h2 className="font-medium">새 프로젝트 적용</h2>
        <button type="button" aria-label="새 프로젝트 닫기" onClick={onClose}
                className="ml-auto rounded p-1 hover:bg-slate-100"><X size={14} /></button>
      </div>

      {error && (
        <p role="alert" className="mb-2 text-red-600">
          적용이 거부됐다 — {error.message}
        </p>
      )}

      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <span className="w-20 text-slate-500">폴더</span>
          <span data-testid="new-dest" className={`${FIELD} inline-block truncate`}>
            {dest || "(고르지 않음)"}
          </span>
          <button type="button" onClick={() => void pick()}
                  className="rounded border border-slate-200 px-2 py-0.5">폴더 선택</button>
        </div>

        <div className="flex items-center gap-2">
          <span className="w-20 text-slate-500">하네스</span>
          <span className="flex gap-3">
            {(defaults?.harnesses ?? []).map((value) => (
              <label key={value} className="flex items-center gap-1">
                <input type="radio" name="harness" value={value} checked={harness === value}
                       onChange={() => setHarness(value)} />
                {value}
              </label>
            ))}
          </span>
        </div>

        {([["이름", name, setName], ["prefix", prefix, setPrefix], ["area", area, setArea],
           ["owner", owner, setOwner], ["upstream", upstream, setUpstream]] as const).map(
          ([label, value, set]) => (
            <div key={label} className="flex items-center gap-2">
              <span className="w-20 text-slate-500">{label}</span>
              <input aria-label={label} value={value}
                     onChange={(event) => set(event.target.value)} className={FIELD} />
            </div>
          ))}
        <p className="text-[11px] text-slate-500">
          upstream 은 이 관리자가 만들어진 자리에서 온다 — 사내 미러를 쓰면 여기서 바꾼다.
        </p>
      </div>

      <div className="mt-3 flex items-center gap-2">
        <button type="button" data-testid="new-apply" disabled={busy || dest === ""}
                onClick={() => (prefix.trim() === "" ? void run("") : setAsking(true))}
                className="rounded border border-slate-800 px-2 py-0.5">적용</button>
        {busy && <span className="text-[11px] text-slate-500">적용 중</span>}
      </div>

      {asking && (
        <div data-testid="prefix-confirm" role="alertdialog"
             className="mt-2 rounded border border-amber-300 bg-amber-50 p-2">
          <p>새 카테고리 <span className="font-mono">{prefix}</span> 를 만든다 — 승인?</p>
          <p className="text-[11px] text-slate-600">
            엔진은 새 prefix 신설에 사용자 승인을 요구한다. 승인은 이 값에만 결속된다.
          </p>
          <div className="mt-1 flex gap-2">
            <button type="button" data-testid="prefix-approve" onClick={() => void run(prefix)}
                    className="rounded border border-slate-800 px-2 py-0.5">승인</button>
            <button type="button" data-testid="prefix-cancel" onClick={() => setAsking(false)}
                    className="rounded border border-slate-200 px-2 py-0.5">취소</button>
          </div>
        </div>
      )}

      {steps.length > 0 && (
        <div data-testid="new-log" className="mt-3 space-y-2">
          {steps.map((step) => <StepLog key={step.name} step={step} />)}
        </div>
      )}
    </div>
  );
}
