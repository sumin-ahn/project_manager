import type { SlotRow, TaskRow } from "../api";

/**
 * 현황 탭 행의 동작 버튼(대여·반납·제거·task prefix/종료/되살리기) — T-0913 이 채운다.
 * 지금은 자리만 잡는다: 행 오른쪽 칸이 비어 있어도 표 폭이 흔들리지 않게 한다.
 */
export default function StatusRowActions(_props: { row: SlotRow | TaskRow }) {
  return <span data-testid="row-actions" className="text-slate-300" />;
}
