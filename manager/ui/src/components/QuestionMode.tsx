import { useEffect, useState } from "react";
import type { Row } from "../tabs/SettingsTab";
import { unsetHint } from "../lib/settings-rows";

/**
 * 질문형 — 편집형과 **같은 항목 목록**을 한 번에 하나씩 묻는다(별도 화면이 아니라 표시 모드다).
 * 기본값이 있는 항목은 Enter 로 넘어가고, 끝나면 편집형과 같은 저장 경로를 탄다.
 *
 * 값이 없는 항목은 엔진이 준 권장값(`recommended`)으로 미리 채운다 — 사람이 이미 정한 값은
 * 덮지 않는다. [권장값으로 모두 채우기] 는 그 미리 채운 값들을 한꺼번에 같은 저장 경로로 보낸다
 * (새 저장 경로 0 · `conf_save` 1회).
 */

/** 그 행에 미리 채울 값 — 값이 없을 때만 권장값이 들어간다. */
export function prefill(row: Row, values: Record<string, string>): string {
  const stored = values[row.key] ?? "";
  return stored === "" ? row.recommended ?? "" : stored;
}

/** 값이 없어 권장값이 들어간 행 전부 — [권장값으로 모두 채우기] 가 보내는 그대로다. */
export function recommendedUpdates(rows: Row[], values: Record<string, string>):
  Record<string, string> {
  const updates: Record<string, string> = {};
  for (const row of rows) {
    const filled = prefill(row, values);
    if (filled !== (values[row.key] ?? "")) updates[row.key] = filled;
  }
  return updates;
}

export default function QuestionMode({ rows, values, onFinish }: {
  rows: Row[];
  values: Record<string, string>;
  onFinish: (updates: Record<string, string>) => void;
}) {
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState("");
  const [updates, setUpdates] = useState<Record<string, string>>({});
  const row = rows[index];

  useEffect(() => {
    if (row) setDraft(prefill(row, values));
  }, [row, values]);

  if (!row) return <p className="text-slate-500">물어볼 항목이 없다</p>;

  const advance = () => {
    const before = values[row.key] ?? "";
    const next = draft === before ? updates : { ...updates, [row.key]: draft };
    setUpdates(next);
    if (index + 1 < rows.length) setIndex(index + 1);
    else onFinish(next);
  };

  const filled = recommendedUpdates(rows, values);

  return (
    <section data-testid="question-mode" className="space-y-2">
      <p className="text-[11px] text-slate-500">
        {index + 1} / {rows.length} · Enter 로 다음 항목
      </p>
      {index === 0 && Object.keys(filled).length > 0 && (
        <button type="button" data-testid="fill-recommended"
                onClick={() => onFinish(filled)}
                className="rounded border border-slate-200 px-2 py-0.5">
          권장값으로 모두 채우기 ({Object.keys(filled).length}개)
        </button>
      )}
      <p data-testid="question-key" className="font-mono">{row.key}</p>
      <p className="text-[11px] text-slate-500">{row.description}</p>
      <div className="flex items-center gap-2">
        <input
          autoFocus
          aria-label={`${row.key} 값`}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter") advance(); }}
          className="w-[36rem] rounded border border-slate-200 px-1"
        />
        <span className="text-[11px] text-slate-500">
          {`안 쓰면 ${unsetHint(row)}`}
          {row.required && " · 필수"}
          {row.recommended !== null && ` · 권장 ${row.recommended}`}
        </span>
        <button type="button" onClick={advance}
                className="rounded border border-slate-200 px-2 py-0.5">다음</button>
      </div>
    </section>
  );
}
