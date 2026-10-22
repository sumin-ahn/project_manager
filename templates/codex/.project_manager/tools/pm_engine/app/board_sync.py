"""보드 git 변경의 best-effort 공유 (층: app)."""
from __future__ import annotations

from pathlib import Path
from collections.abc import Sequence
from pm_engine.infra import board_git
from pm_engine.domain import board_git_rules
import shlex
import sys


def _warn_board_git_local_commit_failed(message: str,
                                        scope: board_git_rules._BoardGitUncommittedScope,
                                        exc: Exception | None = None) -> None:
    """local commit 미생성의 복구 경로를, 이번 mutation 스코프만으로 loud 출력한다."""
    shown = ", ".join(f"{code} {path}" for code, path in scope.entries[:5])
    more = f" (+{len(scope.entries) - 5}건)" if len(scope.entries) > 5 else ""
    quoted_paths = " ".join(shlex.quote(path) for path in scope.recovery_paths)
    quoted_message = shlex.quote(message)
    detail = f" ({exc})" if exc is not None else ""
    print("  ⚠ board local commit 실패 — 이번 mutation 파일이 uncommitted 상태로 남았다. "
          f"pull/push는 건너뛴다{detail}", file=sys.stderr)
    print(f"    상태: {shown}{more}", file=sys.stderr)
    print("    복구: `git -C .project_manager/board add -A -- "
          f"{quoted_paths}`", file=sys.stderr)
    print("          `git -C .project_manager/board commit -m "
          f"{quoted_message} -- {quoted_paths}`", file=sys.stderr)


def _board_git_sync_best_effort(home, message: str,
                                paths: Sequence[Path] | None = None) -> bool:
    """best-effort local-first sync (new/promote/complete/block/unclaim/unblock/section-add/tier).

    board 가 별도 git 이 아니면 no-op(legacy). 별도 git 이면: 로컬 commit을 항상
    시도하고, **새 commit 사실(HEAD 전진)**을 확인한 뒤 → pull --rebase ; push 를 best-effort 로.
    local commit 실패가 mutation 파일을 uncommitted로 남기면 pull/push를 건너뛰고 False를 반환한다.
    offline/auth/conflict 등
    어떤 실패도 **작업을 차단하지 않는다** — stale 경고만 stderr 로 내고 계속한다. active
    retry 루프는 없다 — 밀린 commit 은 다음 mutation 의 pull-rebase+push 가 catch-up 한다.

    `paths` = 그 mutation 이 만진 경로. claim 뿐 아니라 **ticket mutation 8개
    (new/promote/complete/block/unclaim/unblock/section-add/tier)도 함께** 스코프화해 출하한다 — claim 차단을
    풀면 board 가 상시 dirty 가 되므로, best-effort 가 board 전체를 쓸어담는 채로 남으면 누출
    노출이 오늘보다 커진다(사용자 결정).

    `paths=None` = 레거시 board 전체 스코프(draft 제외). 티켓이 아닌 board 파일을 고치는 외부
    호출부(`pm_config` 의 areas.md 보호목록 갱신)의 현행 동작을 보존하는 자리다 — ticket
    mutation 은 **반드시 경로를 준다**(메타가드 `test_ticket_mutations_pass_scoped_paths` 가
    board.py 호출부를 기계 검사한다·새 호출부가 잊으면 red).

    **단 detached HEAD 는 예외**: commit *전* HEAD 상태를 점검해 detached 면
    commit/pull/push 를 전부 skip 하고 loud 경고만 낸다. detached 위의 commit 은 orphan 으로
    쌓이고 catch-up 이 구조적으로 불가하므로(pull --rebase 계속 실패), 침묵 누적 대신 기록을
    보류하고 복귀를 안내한다(파일 mutation 은 이미 완료라 작업 무차단은 유지).

    git ops 전체를 `board_git_lock` 으로 감싼다 — 같은 clone 의 다른 슬롯이 진행 중인 claim
    트랜잭션(commit→push→rollback) 사이에 끼어들지 않게(락 순서 board_git_lock →
    board_lock: 이 함수는 board_lock 을 놓은 뒤 불린다).
    """
    if not board_git._board_git_enabled(home):
        return True
    with board_git.board_git_lock(home):
        return _board_git_sync_best_effort_locked(home, message, paths)


def _board_git_sync_best_effort_locked(home, message: str,
                                       paths: Sequence[Path] | None = None) -> bool:
    """`_board_git_sync_best_effort` 의 본체 (board_git_lock 보유 전제·재진입 금지)."""
    # detached HEAD 가드: detached 에선 commit 이 orphan 으로 쌓이고 `pull --rebase`
    # 가 계속 실패해 "다음 mutation 이 catch-up" 약속이 *구조적으로* 성립하지 않는다(attached
    # 브랜치의 일시 offline/conflict 만 상정한 동작). commit/pull/push 를 모두 skip 하고 loud
    # 경고만 내 orphan 무한 누적을 원천 차단한다. 파일 mutation(rename·frontmatter)은 이미
    # 끝난 뒤라 작업은 무차단 — git 기록만 보류한다(best-effort=작업 무차단 원칙 유지). 자동
    # 복구(checkout/cherry-pick)는 PM 편집/브랜치 의도 침해라 하지 않고 안내만 한다.
    if board_git._board_git_head_detached(home):
        print("  ⚠ board sync 보류 — detached HEAD. board git 기록을 건너뛴다(orphan commit "
              "누적 방지). `git -C .project_manager/board checkout <branch>`(예: main) 로 브랜치에 "
              "복귀하면 다음 mutation 이 일괄 commit·catch-up 한다. detached 에서 이미 쌓인 로컬 "
              "commit 이 있으면 복귀 후 `git -C .project_manager/board cherry-pick <sha>` 로 이식.",
              file=sys.stderr)
        return False
    committed = False
    wrote_root_files: list[str] = []
    try:
        committed = board_git._board_git_stage_and_commit(
            home, message, paths, root_files_written=wrote_root_files)
        if not committed:
            # False 자체는 scope가 없거나 변경이 이미 없는 정상 no-op일 수 있다. 하지만 이번
            # mutation 경로가 아직 staged/unstaged/untracked면 HEAD 불변은 local commit 실패다.
            # 이 상태에서 push가 rc=0(up-to-date)을 내도 파일은 remote에 없으므로, "보존·다음
            # mutation catch-up"이라고 말하거나 pull/push를 계속하면 false-success가 된다.
            uncommitted = board_git._board_git_uncommitted_scope(
                home, paths, root_files=wrote_root_files)
            if uncommitted.entries:
                _warn_board_git_local_commit_failed(message, uncommitted)
                return False
        # 통합 시도 여부는 **추적 변경 유무**로 가른다 — `behind` 로 가르면 안 된다(reviewer
        # must-fix): `_board_git_behind` 는 원격-추적 ref 를 읽으므로 **fetch 선행에서만 유효**한데
        # 이 경로엔 fetch 가 없다 → ref 가 영구 stale → behind 항상 0 → pull 을 영영 안 타 push 가
        # 계속 non-FF 로 밀린다("다음 mutation 이 catch-up" 약속이 구조적으로 깨진다·실측).
        # `pull --rebase` 는 자체가 fetch 를 포함하므로 직행이 왕복 수도 옛 코드와 같거나 적다.
        # 추적 dirty 면 pull 이 어차피 rc≠0 이라 건너뛰고 push 만 시도한다(FF 면 성공·아니면 경고).
        pull = None if board_git._board_git_has_tracked_changes(home) else board_git._board_git_pull_rebase(home)
        push = board_git._board_git_push(home) if (pull is None or pull.returncode == 0) else None
    except Exception as exc:  # noqa: BLE001 — fail-soft: best-effort sync 는 절대 작업을 막지 않는다.
        uncommitted = board_git._board_git_uncommitted_scope(
            home, paths, root_files=wrote_root_files)
        if uncommitted.entries:
            _warn_board_git_local_commit_failed(message, uncommitted, exc)
            return False
        if committed:
            print("  ⚠ board sync 보류 — pull/push 예외. 로컬 commit 은 보존되며 다음 mutation 이 "
                  "catch-up 한다.", file=sys.stderr)
            return True
        print(f"  ⚠ board sync 보류(다음 mutation 이 catch-up): {exc}", file=sys.stderr)
        return False
    if pull is not None and pull.returncode != 0:
        print("  ⚠ board sync 보류 — pull --rebase 실패(offline/conflict). 로컬 commit 은 "
              "보존되며 다음 mutation 이 catch-up 한다.", file=sys.stderr)
    elif push is not None and push.returncode != 0:
        print("  ⚠ board sync 보류 — push 실패(offline/auth/non-FF). 로컬 commit 은 보존되며 "
              "다음 mutation 이 catch-up 한다.", file=sys.stderr)
    return True
