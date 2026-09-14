/**
 * 확인 대화 — 사람이 [승인] 을 누른 **그 값**만 `onConfirm` 으로 돌려준다.
 *
 * 화면에서 `--user-ack <값>` 이 붙는 자리는 이 대화의 반환값 셋뿐이다(`worktree add` 의 repo
 * 이름 · `worktree remove` 의 슬롯 · `task prefix` 의 새 prefix). 대화를 지나지 않고 승인 토큰을
 * 만드는 경로는 없다 — 물리 슬롯 생성·제거와 새 카테고리 신설이 사용자 승인 행위라서다.
 */
export default function ConfirmDialog({ title, note, value, onConfirm, onCancel }: {
  title: string;
  note: string;
  value: string;
  onConfirm: (value: string) => void;
  onCancel: () => void;
}) {
  return (
    <div data-testid="confirm-dialog" role="alertdialog"
         className="rounded border border-amber-300 bg-amber-50 p-2">
      <p>{title}</p>
      <p className="text-[11px] text-slate-600">{note}</p>
      <p className="font-mono text-[11px]" data-testid="confirm-value">--user-ack {value}</p>
      <div className="mt-1 flex gap-2">
        <button type="button" data-testid="confirm-approve" onClick={() => onConfirm(value)}
                className="rounded border border-slate-800 px-2 py-0.5">승인</button>
        <button type="button" data-testid="confirm-cancel" onClick={onCancel}
                className="rounded border border-slate-200 px-2 py-0.5">취소</button>
      </div>
    </div>
  );
}
