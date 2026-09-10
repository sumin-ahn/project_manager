import type { Dot } from "../lib/status";

const COLOR: Record<Dot, string> = {
  blue: "bg-sky-500",
  gray: "bg-slate-300",
  orange: "bg-amber-500",
};

/** 색 점 하나 — 문장으로 풀지 않고 점 + 짧은 배지로만 표현한다(§6.4). */
export default function StatusDot({ dot, label, testId }:
  { dot: Dot; label: string; testId?: string }) {
  return (
    <span
      data-testid={testId}
      data-dot={dot}
      title={label}
      aria-label={label}
      className={`inline-block h-2 w-2 shrink-0 rounded-full ${COLOR[dot]}`}
    />
  );
}
