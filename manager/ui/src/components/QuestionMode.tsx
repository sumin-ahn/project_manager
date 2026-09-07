import { useEffect, useState } from "react";
import type { Row } from "../tabs/SettingsTab";

/**
 * 질문형 — 편집형과 **같은 항목 목록**을 한 번에 하나씩 묻는다(별도 화면이 아니라 표시 모드다).
 * 기본값이 있는 항목은 Enter 로 넘어가고, 끝나면 편집형과 같은 저장 경로를 탄다.
 */
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
    if (row) setDraft(values[row.key] ?? "");
  }, [row, values]);

  if (!row) return <p className="text-slate-500">물어볼 항목이 없다</p>;

  const advance = () => {
    const before = values[row.key] ?? "";
    const next = draft === before ? updates : { ...updates, [row.key]: draft };
    setUpdates(next);
    if (index + 1 < rows.length) setIndex(index + 1);
    else onFinish(next);
  };

  return (
    <section data-testid="question-mode" className="space-y-2">
      <p className="text-[11px] text-slate-500">
        {index + 1} / {rows.length} · Enter 로 다음 항목
      </p>
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
          {row.default === null ? "기본값 없음" : `기본 ${row.default}`}
          {row.required && " · 필수"}
        </span>
        <button type="button" onClick={advance}
                className="rounded border border-slate-200 px-2 py-0.5">다음</button>
      </div>
    </section>
  );
}
