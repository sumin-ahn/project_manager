"""보드 git 선점과 확인·복구 (층: app)."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from pm_engine.infra import board_git
from pm_engine.domain import board_git_rules
import subprocess
from pm_engine.infra import fs
from pm_engine.infra import git
import sys


def _board_git_claim_prefetch(home, ticket_id: str) -> board_git_rules._ClaimPrefetch:
    """claim STRICT 1단계: **읽기 전용** 선점 감지 + 필요할 때만 통합.

    board 가 별도 git 이 아니면 no-op(`anchor=""` — 검증만 진행·legacy 무변경). 별도 git 이면
    순서가 곧 진단이다:

      1. **detached / mid-rebase 선체크(1순위·양보 불가)** — mid-rebase 에서
         `git commit -- <paths>` 는 rc=0 으로 **detached rebase HEAD 위에** 커밋을 만든다
         (architect 실측). 그래서 어떤 git mutation 보다 앞이고, 둘을 갈라 처방을 정확히 한다
         (rebase=abort/continue · detached=checkout).
      2. **upstream 해소** — 미설정이면 조율할 원격이 없다(offline 과 다른 사유).
      3. **fetch**(읽기 전용) — 실패면 offline. 워킹트리·HEAD 를 건드리지 않으므로 dirty 든
         behind 든 무관하게 시도할 수 있고, 실패해도 로컬 변경이 0 이다.
      4. **원격 트리 직접 조회로 race-lost 판정** — `ls-tree` 로 그 티켓이 원격에서 이미
         claimed/done/blocked 인지 읽는다. **로컬 통합 성공에 의존하지 않는다** — 옛 판정
         (winner 를 pull 로 끌어와야 보임)의 상위집합이다.
      5. **behind 계산 → behind>0 일 때만 `pull --rebase`** — 원격이 안 앞섰으면 통합할 게
         없으므로 dirty 여도 claim 을 막지 않는다. 단일 clone 다중 슬롯은 같은
         로컬 브랜치를 공유해 behind=0 이라 잔여 차단이 구조적으로 발생하지 않는다.
         pull 실패의 사유 분류는 `_classify_pull_failure` 가 **구조 판정만으로** 한다
         (rebase 충돌 / 미커밋 파일 / offline / 원인 미상 — git 메시지 문자열 비의존).
      6. **anchor 확정** — HEAD 를 못 구하면 rollback 복귀 지점이 없다는 뜻이라 안전 실패한다
         (anchor 없는 진행 금지·거짓 소유 방지).

    조율 보장은 그대로다 — 소유 확정 권위는 여전히 **원격 ref FF push(CAS)** 이고, 이 단계는
    "이미 남이 가져갔나" 를 싸게 먼저 확인해 중복작업을 막는 것뿐이다.
    """
    if not board_git._board_git_enabled(home):
        return board_git_rules._ClaimPrefetch(anchor="")  # sync 비활성 — 검증만 진행(legacy).
    # 1. detached / mid-rebase — 어떤 mutation 보다 먼저. rebase 를 detached 보다 먼저 가른다
    #    (mid-rebase 는 detached 의 부분집합인데 처방이 다르다·checkout 안내는 오도).
    if board_git._board_git_rebase_in_progress(home):
        return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_REBASE)
    if board_git._board_git_head_detached(home):
        return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_DETACHED)
    # 2. upstream — 조율 대상 원격 자체가 없으면 offline 이 아니라 설정 문제다.
    upstream = board_git._board_git_upstream(home)
    if upstream is None:
        return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_NO_UPSTREAM)
    # 3. fetch — 읽기 전용(로컬 변경 0). 실패 = 도달 불가.
    if not board_git._board_git_fetch(home, upstream.remote):
        return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_OFFLINE)
    # 4. 원격 선점 판정 — dirty·behind 무관(원격 트리를 직접 읽는다).
    remote_status = board_git._board_git_remote_ticket_status(home, upstream.tracking, ticket_id)
    if remote_status in board_git_rules._CLAIM_RACE_LOST_STATUSES:
        # behind 도 함께 실어 보낸다 — 차단 문구가 "로컬 사본이 왜 뒤처진 판단을 했는지"
        # (list/show 의 freshness 와 같은 판정)를 그 자리에서 설명하게 한다.
        return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_RACE_LOST, detail=remote_status,
                              behind=board_git._board_git_behind(home, upstream.tracking))
    # 5. 원격이 앞섰을 때만 통합. 안 앞섰으면 무관 dirty 가 있어도 claim 은 진행한다.
    behind = board_git._board_git_behind(home, upstream.tracking)
    # behind 판정 불가(None)는 옛 0 폴백과 **같은 방향**으로 흘린다 — 원격이 앞섰다고 볼 근거가
    # 없으면 통합도 차단도 하지 않는다(차단 판단 불변·진단 문구만 정직하게 한다).
    if behind is not None and behind > 0:
        try:
            pull = board_git._board_git_pull_rebase(home)
        except subprocess.TimeoutExpired:  # 결과 미상 timeout 은 offline 취급. 그 밖 예외는 전파.
            return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_OFFLINE, behind=behind,
                                  dirty=board_git._board_git_dirty_entries(home))
        if pull.returncode != 0:
            return board_git._classify_pull_failure(home, upstream, pull, behind)
    # 6. anchor — enabled 인데 HEAD 를 못 구하면 rollback 불가라 안전 실패(로컬 변경 0).
    anchor = board_git._board_git_head(home)
    if not anchor:
        return board_git_rules._ClaimPrefetch(block=board_git_rules._CLAIM_BLOCK_NO_ANCHOR)
    return board_git_rules._ClaimPrefetch(anchor=anchor, behind=behind)


def _claim_block_message(ticket_id: str, prefetch: board_git_rules._ClaimPrefetch) -> str:
    """차단 사유 → 사용자 안내 1건 (**렌더 단일 지점**·오판/이중출력 0).

    한 원인엔 한 안내만 나가야 한다는 규약을 사유 코드 분기 한 곳으로 못박는다 —
    호출부(`cmd_claim`)가 사유별로 문구를 다시 짜면 그 규약이 두 벌이 된다.
    """
    behind = prefetch.behind
    if prefetch.block == board_git_rules._CLAIM_BLOCK_REBASE:
        return (f"board submodule 이 rebase 진행 중(충돌 미해소) — {ticket_id} claim 불가. "
                f"`git -C .project_manager/board rebase --abort` 로 되돌리거나, 충돌을 해소하고 "
                f"`git -C .project_manager/board rebase --continue` 후 재시도 "
                f"(offline 아님·네트워크 정상).")
    if prefetch.block == board_git_rules._CLAIM_BLOCK_DETACHED:
        return (f"board submodule 이 detached HEAD 상태 — {ticket_id} claim 불가(rollback anchor "
                f"없음). `git -C .project_manager/board checkout <branch>`(예: main) 로 브랜치에 "
                f"복귀 후 claim 재시도 (offline 아님·네트워크 정상). detached 에서 이미 쌓인 로컬 "
                f"commit 이 있으면 복귀 후 `git -C .project_manager/board cherry-pick <sha>` 로 이식.")
    if prefetch.block == board_git_rules._CLAIM_BLOCK_NO_UPSTREAM:
        return (f"board submodule 에 추적 브랜치(upstream)가 없다 — {ticket_id} claim 불가. claim 은 "
                f"원격 push 로 소유를 확정하므로 조율할 원격이 필요하다(offline 아님). "
                f"`git -C .project_manager/board push -u origin <branch>` 로 upstream 을 설정하라.")
    if prefetch.block == board_git_rules._CLAIM_BLOCK_RACE_LOST:
        where = prefetch.detail or "claimed"
        # behind 계산 불가(rev-list 실패)면 수치를 지어내지 않는다 — 실측 0 과 판정불가는 다른
        # 사실이고, 사용자가 읽는 숫자는 실측이어야 한다(F-003).
        gap = (f"원격보다 {behind} 커밋 뒤처진 상태에서 조회됐다"
               f"(list/show 의 freshness 와 같은 판정)" if behind is not None else
               "뒤처진 커밋 수는 판정불가다"
               "(rev-list 실패·list/show 의 freshness 와 같은 판정)")
        return (f"claim race lost on {ticket_id} — 원격 board 에서 이미 {where}/ 상태다"
                f"(다른 세션이 먼저 잡았거나 이미 처리됨·로컬 변경 0). 로컬 사본이 stale 했다 "
                f"— {gap}. `board.py list --all` 로 현황을 확인하라.")
    if prefetch.block == board_git_rules._CLAIM_BLOCK_DIRTY:
        return (f"board 가 원격보다 {behind} 커밋 뒤졌는데 미커밋 파일이 통합(pull --rebase)을 "
                f"막는다 — {ticket_id} claim 불가 (offline 아님·네트워크 정상: fetch 성공). "
                f"{board_git._board_git_dirty_hint(prefetch.dirty)}")
    if prefetch.block == board_git_rules._CLAIM_BLOCK_INTEGRATION:
        return (f"board 가 원격보다 {behind} 커밋 뒤졌는데 통합(pull --rebase)이 실패했다 — "
                f"{ticket_id} claim 불가. **네트워크는 정상**(fetch 성공)이고 미커밋 파일도 없다 "
                f"— 원인 미상이므로 `git -C .project_manager/board pull --rebase` 를 직접 돌려 "
                f"출력을 확인하라." + (f" (git: {prefetch.detail})" if prefetch.detail else ""))
    if prefetch.block == board_git_rules._CLAIM_BLOCK_NO_ANCHOR:
        return (f"board git 의 HEAD 를 확인할 수 없다 — {ticket_id} claim 불가(rollback anchor "
                f"없이는 거짓 소유를 되돌릴 수 없다). `git -C .project_manager/board status` 로 "
                f"board git 상태를 확인하라.")
    hint = board_git._board_git_dirty_hint(prefetch.dirty) if prefetch.dirty else ""
    return (f"offline — board 도달 불가, {ticket_id} claim 불가"
            + (f" (원격이 {behind} 커밋 앞섬)" if behind else "")
            + (f" {hint}" if hint else ""))


def _board_git_restore_claim_files(files: board_git_rules._ClaimFiles) -> None:
    """티켓 파일을 claim **직전** 상태로 되돌린다 — 역이동 + 원본 바이트 복원.

    hard reset 은 board 전체를 되감아 **무관한 미커밋
    작업을 파괴**한다(실측). 되돌릴 대상은 이 claim 이 만진 두 경로뿐이므로 파일 수준에서
    정확히 복원한다: `atomic_replace(new, old)`(atomic-rename 모델과 동형 — 중복/소실 창
    0) 후 원본 바이트를 다시 써 frontmatter 갱신까지 되돌린다.
    """
    files.old.parent.mkdir(parents=True, exist_ok=True)
    if files.new.exists():
        fs.atomic_replace(files.new, files.old)
    files.old.write_bytes(files.original)


def _board_git_claim_rollback(home, anchor: str, files: board_git_rules._ClaimFiles,
                              claim_commit: str | None = None,
                              index_snapshot: dict[str, tuple[str, str]] | None = None,
                              root_files_snapshot: board_git_rules._BoardGitRootFilesSnapshot | None = None,
                              ) -> None:
    """claim 을 **그 claim 이 만진 것만** 되돌린다 (모든 단계 시도·실패는 전파).

    시점별로 하는 일이 다른데, 규칙은 하나다 — *내가 만든 것만* 되돌린다:
      - **commit 실패(새 커밋 0)**: 이력 무조작(HEAD==anchor) + 파일 역이동·원본 복원 + index
        스냅샷 복원.
      - **push 실패(non-FF·거부)**: `HEAD == 내 claim 커밋` 일 때만 `reset --soft <anchor>`
        (index 는 남기고 이력만 되감음) + 위와 동일. 그 사이 HEAD 가 다른 커밋이 됐다면
        (직렬화 락이 막지만 방어) 이력을 **건드리지 않는다** — 남의 커밋을 되돌리지 않는다.
      - **push timeout**: 호출부가 원격이 그 커밋을 포함하는지 먼저 재확인해 확정이면 이 함수를
        아예 부르지 않는다.
      - **rebase 충돌**: prefetch 단계라 로컬 mutation 이 0 — 롤백 대상 자체가 없다.

    `index_snapshot` = claim 직전 티켓 + board 루트 파일의 index 항목
    (`_board_git_index_snapshot`). `root_files_snapshot` 은 같은 루트 파일의 워킹트리 상태다.
    둘을 함께 되돌려야 새로 만든 파일이 `A ` 또는 `??` 로, 기존 파일의 append 가 ` M` 으로
    남아 다음 claim 을 막는 영구 잔여가 생기지 않는다. 티켓 경로도 파일 내용뿐 아니라
    **index 상태까지** 되돌려 "미커밋 작업을 상태 그대로 보존" 한다.

    다섯 복원 단계와 마무리 refresh 는 `finally` 사슬이라 한 단계가 throw 해도 뒤 단계를 모두
    시도한다. 실패한 단계의 예외는 사슬 끝에서 전파되고(뒤 단계 실패는 앞 실패를 `__context__`
    로 품는다) claim 은 확정되지 않는다.
    """
    restored_root_files: tuple[str, ...] = ()
    safe_root_indexes: tuple[str, ...] = ()
    try:
        head = board_git._board_git_head(home)
        if claim_commit and head == claim_commit and head != anchor:
            git._board_git(home, ["reset", "--soft", anchor])
    finally:
        try:
            _board_git_restore_claim_files(files)
        finally:
            try:
                restored_root_files = board_git._board_git_restore_root_files(home, root_files_snapshot)
            finally:
                try:
                    safe_root_indexes = board_git._board_git_root_indexes_safe_to_restore(
                        home, index_snapshot, root_files_snapshot, restored_root_files)
                finally:
                    try:
                        _board_git_restore_claim_index(
                            home, (files.old, files.new), index_snapshot, root_files=safe_root_indexes)
                    finally:
                        board_git._board_git_refresh_after_rollback(home)


def _board_git_restore_claim_index(home, paths: Sequence[Path],
                                   snapshot: dict[str, tuple[str, str]] | None, *,
                                   root_files: Sequence[str] = ()) -> None:
    """티켓·루트 파일 index 를 **claim 직전 스냅샷**으로 되돌린다 (실패는 loud).

    스냅샷이 없거나(캡처 실패) 복원이 실패하면 현행 동작(`add -A -- <두 경로>`)으로 폴백해
    index 를 최소한 워킹트리와 일치시키되, **그 사실을 loud 하게 알린다** — claim 대상 파일이
    staged 로 바뀔 수 있다는 뜻이라 사용자가 모르면 안 된다(조용히 다른 상태로 만들지 않는다).
    """
    if snapshot is not None and board_git._board_git_restore_index(
            home, paths, snapshot, root_files=root_files):
        return
    print("  ⚠ board index 상태를 claim 직전으로 정확히 되돌리지 못했다 — 티켓/루트 경로가 "
          "staged 로 남을 수 있다(내용은 보존됨). `git -C .project_manager/board status` 로 "
          "확인하고 필요하면 `git -C .project_manager/board reset -- <경로>` 로 unstage 하라.",
          file=sys.stderr)
    pathspec = board_git._board_git_stageable(home, board_git._board_git_scope_pathspec(home, paths))
    if pathspec:
        git._board_git(home, ["add", "-A", "--", *pathspec])


def _board_git_claim_confirm(home, anchor: str | None, files: board_git_rules._ClaimFiles) -> bool:
    """claim STRICT 3·4단계: 로컬 claim 을 **스코프 커밋** 하고 push 가 성공해야 소유 확정.

    board 가 별도 git 이 아니거나 prefetch 가 sync 를 비활성(`""`)으로 판단했으면 True
    (로컬 atomic-rename 만으로 확정·legacy 무변경). 별도 git 이고 유효 anchor 면:
      1. commit(**그 티켓 두 경로 + 이번에 보강한 board 루트 파일**) — 로컬 claim 박제. **새 commit 을 못
         내면**(identity 부재·hook·nothing-to-commit) push 가 "up-to-date" rc=0 을 내 remote
         미전파인데 확정될 수 있다(거짓 소유) → 즉시 rollback + False.
      2. push — rc=0 이어야 *비로소* 소유 확정(원격 ref FF = CAS·조율 권위).
      3. push 가 **timeout** 이면 원격이 그 커밋을 포함하는지 재확인한다 — 포함이면 원격엔
         이미 반영된 것이므로 **롤백하지 않고 확정**한다.
      4. 그 외 rc 실패는 `_board_git_claim_rollback` 후 False (거짓 소유 0).
    commit 예외는 되돌린 뒤 전파하고, push 의 timeout 밖 예외는 되돌리지 않고 전파한다. 되돌리기
    자체가 실패하면 그 예외가 전파된다.

    **여기가 index 스냅샷 지점이다** — 이 함수 진입 시점의 index 는 아직 claim 이 손대기 전
    상태다(그 앞의 파일 이동·frontmatter 갱신은 워킹트리만 건드린다). 롤백은 그 스냅샷으로
    되돌려 claim 대상 파일의 staged/unstaged/untracked 상태까지 보존한다.
    """
    if not board_git._board_git_enabled(home) or not anchor:
        return True  # sync 비활성(legacy·anchor 없음) — 로컬 rename 만으로 확정(무변경).
    paths = (files.old, files.new)
    index_snapshot = board_git._board_git_index_snapshot(home, paths, root_files=board_git_rules._BOARD_GIT_ROOT_FILES)
    root_files_snapshot = board_git._board_git_root_files_snapshot(home)
    try:
        committed = board_git._board_git_stage_and_commit(
            home, "claim", paths, root_files_snapshot=root_files_snapshot)
    except Exception:  # 되돌린 뒤 재전파 — 원인 모르는 commit 실패를 race-lost 로 내지 않는다.
        _board_git_claim_rollback(
            home, anchor, files, index_snapshot=index_snapshot,
            root_files_snapshot=root_files_snapshot)
        raise
    if not committed:
        # commit 이 새 commit 을 못 냄 → push rc=0(up-to-date)이 거짓 확정을 낼 수 있다.
        _board_git_claim_rollback(
            home, anchor, files, index_snapshot=index_snapshot,
            root_files_snapshot=root_files_snapshot)
        return False
    claim_commit = board_git._board_git_head(home)
    try:
        push = board_git._board_git_push(home)
    except subprocess.TimeoutExpired:  # push timeout 은 *결과 미상* 이지 실패가 아니다.
        upstream = board_git._board_git_upstream(home)
        confirmed = bool(claim_commit) and upstream is not None and \
            board_git._board_git_remote_has_commit(home, upstream, claim_commit)
        if confirmed:
            print("  ⚠ board push 응답이 끊겼으나(timeout) 원격 브랜치가 이 claim 커밋을 이미 "
                  "포함 — 소유 확정(롤백하지 않는다·고아 claim 방지).", file=sys.stderr)
            return True
        _board_git_claim_rollback(
            home, anchor, files, claim_commit, index_snapshot, root_files_snapshot)
        return False
    if push.returncode == 0:
        return True
    _board_git_claim_rollback(
        home, anchor, files, claim_commit, index_snapshot, root_files_snapshot)
    return False
