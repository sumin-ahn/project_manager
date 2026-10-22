"""보드 git IO·범위·root 파일 복구 (층: infra)."""
from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path
from collections.abc import Sequence
from pm_engine.domain import board_git_rules
import contextlib
from pm_engine.infra import fs
from pm_engine.infra import git
from pm_engine.domain import git_freshness
from pm_engine.domain import gitattributes_rules
from pm_engine.infra import pm_paths
import re
from pm_engine.domain import round_rules
from pm_engine.infra import round_store
import shutil
import stat
import subprocess
import sys
from pm_engine.domain import ticket_rules
import time


def _board_git_root_file_backfill_target(
        home, name: str, declared: Callable[[str], bool]) -> board_git_rules._BoardGitRootFileBackfillTarget | None:
    """루트 배포 파일 하나의 regular·tracked·clean 상태를 확인한다.

    파일이 **없어도** index/porcelain 을 조회한다. ``ls-files`` 비매치 + status clean 만 진짜
    미배포로 보며, index/worktree 어느 쪽이든 삭제가 기록돼 있으면 사용자 WIP 로 보존한다.
    기존 파일은 regular + tracked + status clean 일 때만 append 가능하다. 반환 ``None`` 은
    이미 선언됨 또는 안전하게 쓸 수 없음이다.
    """
    if name not in board_git_rules._BOARD_GIT_ROOT_FILES:
        raise ValueError(f"board 루트 배포 파일 seam 밖의 이름: {name}")
    path = home.board_root / name
    try:
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None and not stat.S_ISREG(info.st_mode):
            kind = "symlink" if stat.S_ISLNK(info.st_mode) else "비정규 파일"
            print(f"  ⚠ board {name} backfill 거부 — {kind} 이므로 안전하게 append 할 수 없다: "
                  f"{path}", file=sys.stderr)
            return None
        text = fs.read_text_shared(path, encoding="utf-8") if info is not None else ""
        if declared(text):
            return None
    except (OSError, UnicodeError):
        return None

    try:
        tracked = git._board_git(home, ["ls-files", "--error-unmatch", "--", name])
        dirty = git._board_git(home, [
            "status", "--porcelain", "-z", "-uall", "--", name,
        ])
    except Exception as exc:  # noqa: BLE001 — 소유/clean 미확인은 append 거부(안전 방향).
        print(f"  ⚠ board {name} backfill 보류 — git 소유/clean 상태를 확인하지 못했다({exc}). "
              "사용자 편집 보존을 위해 쓰지 않는다.", file=sys.stderr)
        return None

    if tracked.returncode not in (0, 1) or dirty.returncode != 0:
        print(f"  ⚠ board {name} backfill 보류 — git 상태 확인 실패. 사용자 WIP를 mutation "
              "커밋에 쓸어 담지 않도록 쓰지 않는다.", file=sys.stderr)
        return None

    has_status = bool(dirty.stdout)
    if info is None:
        # tracked+부재(status 가 clean 인 skip-worktree 포함) 또는 porcelain 삭제 표식은 사용자가
        # 파일을 없애는 중인 상태다. untracked+부재+status clean 만 신규 배포 대상으로 인정한다.
        if tracked.returncode == 0 or has_status:
            print(f"  ⚠ board {name} backfill 보류 — 추적 파일의 삭제 WIP가 감지됐다. 사용자 "
                  "삭제를 되돌리거나 mutation 커밋에 포함하지 않도록 파일을 만들지 않는다. "
                  f"{name} 변경을 직접 커밋하면 다음 mutation 이 필요한 규칙을 보강한다.",
                  file=sys.stderr)
            return None
    elif tracked.returncode != 0 or has_status:
        state = "git 미추적" if tracked.returncode != 0 else "미커밋 변경 있음"
        print(f"  ⚠ board {name} backfill 보류 — 기존 파일이 {state}. 사용자 WIP를 mutation "
              f"커밋에 쓸어 담지 않도록 쓰지 않는다. {name} 변경을 직접 커밋하면 다음 mutation "
              "이 필요한 규칙을 보강한다.", file=sys.stderr)
        return None
    return board_git_rules._BoardGitRootFileBackfillTarget(path, text)


def _gitattributes_text_eol_attrs(
        text: str, targets: tuple[str, ...]) -> tuple[str | None, str | None]:
    """대상 경로에 적용되는 마지막 ``text``·``eol`` 속성 값을 반환한다."""
    text_attr: str | None = None
    eol_attr: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if any("#" in field for field in fields):
            continue
        pattern = fields[0]
        if pattern.startswith("!"):
            text_attr = None
            eol_attr = None
            continue
        if not gitattributes_rules._gitattributes_pattern_matches(pattern, targets):
            continue
        for attr in fields[1:]:
            if attr == "text":
                text_attr = "set"
            elif attr in ("-text", "!text"):
                text_attr = ""
            elif attr.startswith("text="):
                text_attr = attr[len("text="):]
            elif attr.startswith("eol="):
                eol_attr = attr[len("eol="):]
            elif attr in ("-eol", "!eol"):
                eol_attr = ""
    return text_attr, eol_attr


def _target_lf_declared(text: str, target: str) -> bool:
    """한 경로에 ``text``(set·auto)와 ``eol=lf``가 함께 유효한가."""
    text_attr, eol_attr = _gitattributes_text_eol_attrs(text, (target,))
    return text_attr in board_git_rules._GITATTRIBUTES_TEXT_ENABLED and eol_attr == "lf"


def _board_text_attr_targets() -> tuple[str, ...]:
    """LF 커버리지 판정 대상 — 상태 경로 × 확장자 + board 루트 직계 + **라운드 중첩 경로**.

    라운드 파일은 상태 디렉토리가 아니라 `tickets/<라운드 디렉터리>/<티켓>/` 아래 산다 —
    상태 경로만 재던 판정은 그 자리를 못 본다. 디렉터리 이름과 파일명 문법은 사이드카 seam 이
    소유하므로 여기서 표기를 복제하지 않고 그 표에서 프로브 경로를 만든다.
    """
    probe = f"tickets/{round_store.ROUNDS_DIRNAME}/T-0/{round_rules.round_filename(1, 'developer')}"
    return (*board_git_rules._BOARD_TEXT_ATTR_TARGETS, probe)


def _board_text_lf_declared(text: str) -> bool:
    """모든 상태·라운드 경로 × 엔진-소유 확장자에 ``text``+``eol=lf``가 유효한가."""
    return all(_target_lf_declared(text, target)
               for target in _board_text_attr_targets())


def _board_gitattributes_declared(text: str) -> bool:
    """board git에 엔진 소유 areas union·엔진-소유 텍스트 LF 규칙이 모두 배포됐는가.

    술어는 선언들의 **논리곱**이다 — 하나라도 단독 판정으로 두면 그 선언이 이미 있는 기존
    보드에서 backfill 이 돌지 않아 나머지가 영구히 미배포로 남는다.
    """
    return (
        gitattributes_rules._areas_union_declared(text, gitattributes_rules._BOARD_AREAS_ATTR_TARGETS)
        and _board_text_lf_declared(text)
    )


def _ensure_board_gitattributes(home) -> bool:
    """board git 의 union·엔진-소유 텍스트 LF 규칙을 `.gitattributes` 에 **멱등 보강**.

    board 분리 형상에서 areas.md 는 board submodule 안에 살고 루트 선언이 닿지 않아 union 이
    조용히 사라진다. 신규 board 는 `pm_import.setup_board_submodule` 의 seed 가 이 파일을
    만들지만 **이미 만들어진 board 엔 seed 가 다시 돌지 않으므로** board git
    commit funnel(`_board_git_stage_and_commit`)에서 backfill 한다 — 보강분은 그 commit 에 함께
    실려 pull/push 로 공유 remote·다른 clone 까지 전파된다.

    **호출처는 그 funnel 하나뿐이다**(단일 채널) — write→stage→commit 이 한 호출에 닫혀야
    엔진이 만든 파일이 board 에 미커밋으로 눌러앉지 않는다. write 만 하고 commit 하지 않는
    지점(예: `cmd_init`)에서 부르면 `?? .gitattributes` 가 남는다.
    claim STRICT 의 dirty 가드에 걸려 claim 을 *막기까지* 했다 — 이 그 전면 차단을
    걷어냈으므로 지금 남는 건 "엔진 산출물이 미커밋으로 떠돈다" 는 위생 문제다. 단일 채널
    규칙은 그 이유로 유지된다.) 스코프 커밋의 pathspec 에 `.gitattributes` 를 포함하는 것도
    같은 이유다 — 빠지면 이 backfill 이 영구 미커밋으로 남아 배포가 죽는다.

    **비파괴**: 파일이 있으면 덮어쓰지 않고, union·텍스트 LF 선언 중 하나라도 *없을 때만*
    블록을 append 한다(채택자 규칙 보존). 둘 다 선언돼 있으면 no-write(멱등). 기존 파일에 append
    하려면 regular file 이고 git 추적 상태가 clean 이어야 한다 — symlink/비정규 파일이나
    untracked·staged·unstaged·삭제 WIP 면 loud 하게 알리고 쓰지 않는다. append 는
    `fs.append_atomic`(O_APPEND) — 동시 writer 가 서로를 덮지 않는다.

    fail-soft: board 미분리(legacy)·areas 가 이 git 밖·IO 실패면 아무 것도 하지 않고 False.
    반환 True = 이번 호출이 보강했다.
    """
    root = home.board_root
    if not (root / ".git").exists() or pm_paths.areas_file(home).parent != root:
        return False  # board 미분리(legacy) 또는 areas 가 이 git 밖 — no-op.
    target = _board_git_root_file_backfill_target(
        home, ".gitattributes",
        _board_gitattributes_declared,
    )
    if target is None:
        return False
    try:
        # 기존 내용 뒤에 빈 줄 하나를 띄우고 append (줄바꿈 없이 끝난 파일도 안전하게 이어붙임).
        separator = "" if not target.text else (
            "\n" if target.text.endswith("\n") else "\n\n")
        fs.append_atomic(target.path, separator + board_git_rules._BOARD_GITATTRIBUTES_BLOCK)
    except (OSError, UnicodeError):
        # IO 실패·비-UTF8 `.gitattributes` — 보강을 포기한다. 이 함수는 ticket mutation 의
        # commit funnel 에서 불리므로 어떤 예외도 mutation 을 깨선 안 된다(advisory 가 표면화).
        return False
    return True


def _gitignore_pattern_targets_drafts(pattern: str) -> bool:
    """`.gitignore` 한 줄이 draft 격리 디렉토리를 가리키는가 (부정 `!` 줄 포함).

    선행 `!`(부정)·선행 `**/`·후행 `/`(디렉토리 표기)를 벗겨
    `_BOARD_DRAFT_IGNORE_FORMS` 와 대조한다. 선행 `/` 는 제거 전에 앵커로 기록한다 — 앵커드
    `/.drafts` 는 board 루트의 `.drafts` 만 가리켜 실제 `tickets/.drafts/` 에 닿지 않으므로
    선언으로 세지 않고, `/tickets/.drafts` 만 인정한다. **부정도 '가리킨다' 로 센다** — `!` 는
    채택자가 draft 를 추적하겠다고 명시한 결정이고, gitignore 는 last-match-wins 라 그 뒤에
    우리 줄을 붙이면 그 결정을 실제로 뒤집는다(비파괴 원칙).

    판정 불가/불일치는 항상 False = **한 번 더 append** 하는 쪽으로 기운다(`.gitattributes`
    backfill 과 같은 기울기). 그 방향은 무해하고(같은 디렉토리에 대한 중복 선언·효과 동일),
    반대 방향(거짓 정상)은 오탐이 그대로 남는다. 복합 glob(`tickets/*`·문자 클래스·
    이스케이프)은 인식 대상 밖이다.
    """
    pat = pattern.strip()
    if pat.startswith("!"):
        pat = pat[1:].strip()
    anchored = pat.startswith("/")
    if anchored:
        pat = pat[1:]
    pat = pat.rstrip("/")
    if pat.startswith("**/"):
        pat = pat[len("**/"):]
    forms = board_git_rules._BOARD_DRAFT_IGNORE_FORMS[:1] if anchored else board_git_rules._BOARD_DRAFT_IGNORE_FORMS
    return pat in forms


def _drafts_ignore_declared(text: str) -> bool:
    """이 `.gitignore` 본문이 draft 격리 디렉토리를 이미 다루고 있는가 (내용 판정).

    빈 줄과 `#` 로 시작하는 주석 줄은 건너뛴다 — 우리 블록의 설명 주석이 그 자신을
    "선언됨" 으로 읽게 하지 않는다.
    """
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if _gitignore_pattern_targets_drafts(line):
            return True
    return False


def _board_git_root_file_rule_declared(name: str, text: str) -> bool:
    """루트 배포 파일별 내용 규칙 — `_BOARD_GIT_ROOT_FILES` lockstep dispatcher."""
    if name == ".gitattributes":
        return _board_gitattributes_declared(text)
    if name == ".gitignore":
        return _drafts_ignore_declared(text)
    raise ValueError(f"board 루트 배포 파일 seam 밖의 이름: {name}")


def _board_git_root_files_need_backfill(home) -> bool:
    """draft-only sync funnel 을 열 루트 backfill 필요 여부 (read-only·fail-open 술어).

    둘 다 이미 정합인 regular file 이면 False 이므로 두 번째 draft 는 commit/pull/push 경로에
    들어가지 않는다. 부재·비정규·읽기 실패·규칙 누락은 True 로 두어 실제 ensure 가 공용
    git/WIP 판정을 하고 필요한 write 또는 loud 보류를 수행하게 한다.
    """
    root = home.board_root
    if not (root / ".git").exists():
        return False
    for name in board_git_rules._BOARD_GIT_ROOT_FILES:
        path = root / name
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                return True
            text = fs.read_text_shared(path, encoding="utf-8")
        except (FileNotFoundError, OSError, UnicodeError):
            return True
        if not _board_git_root_file_rule_declared(name, text):
            return True
    return False


def _ensure_board_gitignore(home) -> bool:
    """board git 의 `.gitignore` 에 `tickets/.drafts/` 를 **멱등 보강**.

    `_ensure_board_gitattributes` 와 같은 backfill 패턴이다 — 신규 채택자(board scaffold)든 이미
    만들어진 board(backfill)든 같은 ensure 경로로 수렴하므로 별도 마이그레이션 명령이 없다.
    호출처도 같은 commit funnel(`_board_git_stage_and_commit`) 하나뿐이다: write→stage→commit 이
    한 호출에 닫혀야 엔진이 만든 파일이 board 에 미커밋으로 눌러앉지 않는다(그러면 이 함수가
    없애려는 바로 그 오탐을 스스로 만든다).

    **비파괴**: 파일이 있으면 덮어쓰지 않고, draft 를 다루는 줄이 *없을 때만* 블록을 append
    한다(채택자 규칙·부정 선언 보존). 이미 다뤄지고 있으면 no-write(멱등). 기존 파일에
    append 하려면 regular file 이고 git 추적 상태가 clean 이어야 한다 — symlink/비정규 파일이나
    untracked·staged·unstaged WIP 면 사유를 loud 하게 알리고 쓰지 않는다. append 는
    `fs.append_atomic`(O_APPEND) — 동시 writer 가 서로를 덮지 않는다.

    fail-soft: board 미분리(legacy)·IO 실패면 아무 것도 하지 않고 False.
    반환 True = 이번 호출이 보강했다.
    """
    root = home.board_root
    if not (root / ".git").exists():
        return False  # board 미분리(legacy) — no-op(draft 격리는 board-git 형상 전용).
    target = _board_git_root_file_backfill_target(
        home, ".gitignore", _drafts_ignore_declared)
    if target is None:
        return False
    try:
        # 기존 내용 뒤에 빈 줄 하나를 띄우고 append (줄바꿈 없이 끝난 파일도 안전하게 이어붙임).
        separator = "" if not target.text else (
            "\n" if target.text.endswith("\n") else "\n\n")
        fs.append_atomic(target.path, separator + board_git_rules._BOARD_GITIGNORE_BLOCK)
    except (OSError, UnicodeError):
        # IO 실패·비-UTF8 `.gitignore` — `_ensure_board_gitattributes` 와 같은 fail-soft 규약
        # (이 축의 실패가 ticket mutation 을 깨선 안 된다).
        return False
    return True


def _board_git_root_files_snapshot(home) -> board_git_rules._BoardGitRootFilesSnapshot:
    """엔진 backfill 전 루트 파일 상태와 이후 실제 작성 목록을 캡처할 그릇을 만든다.

    strict claim 이 실패하면 index 만 되돌려서는 부족하다. 새 파일은 untracked 로, 기존 파일에
    append 한 블록은 unstaged 로 남아 다음 ensure 가 no-write 하는 영구 잔여가 된다. 워킹트리
    바이트도 함께 복원해야 다음 mutation 이 backfill 을 다시 정상 시도할 수 있다. 단, 복원
    소유권은 이 스냅샷 뒤 `_ensure_board_root_files()` 가 실제로 쓴 파일에만 생긴다.
    """
    snapshot: dict[str, board_git_rules._BoardGitRootFileState] = {}
    for name in board_git_rules._BOARD_GIT_ROOT_FILES:
        path = home.board_root / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            snapshot[name] = board_git_rules._BoardGitRootFileState(False, None)
            continue
        except OSError:
            return board_git_rules._BoardGitRootFilesSnapshot(None)
        if not stat.S_ISREG(info.st_mode):
            snapshot[name] = board_git_rules._BoardGitRootFileState(True, None)
            continue
        try:
            snapshot[name] = board_git_rules._BoardGitRootFileState(True, fs.read_bytes_shared(path))
        except OSError:
            return board_git_rules._BoardGitRootFilesSnapshot(None)
    return board_git_rules._BoardGitRootFilesSnapshot(snapshot)


def _board_git_expected_backfill_state(
        name: str, before: board_git_rules._BoardGitRootFileState) -> board_git_rules._BoardGitRootFileState | None:
    """스냅샷 원본에 엔진 블록 하나를 append 한 정확한 워킹트리 상태."""
    if before.existed and before.contents is None:
        return None
    original = before.contents or b""
    separator = b"" if not original else (b"\n" if original.endswith(b"\n") else b"\n\n")
    if name == ".gitattributes":
        block = board_git_rules._BOARD_GITATTRIBUTES_BLOCK
    elif name == ".gitignore":
        block = board_git_rules._BOARD_GITIGNORE_BLOCK
    else:
        return None
    return board_git_rules._BoardGitRootFileState(True, original + separator + block.encode("utf-8"))


def _board_git_restore_root_files(
        home, snapshot: board_git_rules._BoardGitRootFilesSnapshot | None) -> tuple[str, ...]:
    """실제 backfill 파일만 원복하되 제3자 변경은 보존한다 (절대 throw 금지)."""
    if snapshot is None or snapshot.states is None:
        print("  ⚠ board 루트 파일의 claim 직전 상태를 캡처하지 못해 정확히 복원하지 못했다. "
              "`git -C .project_manager/board status` 로 확인하라.", file=sys.stderr)
        return ()
    restored: list[str] = []
    for name in snapshot.backfilled:
        before = snapshot.states.get(name)
        path = home.board_root / name
        expected = _board_git_expected_backfill_state(name, before) if before is not None else None
        if before is None or expected is None:
            print(f"  ⚠ board rollback 이 {name} backfill 원본을 확인하지 못해 덮어쓰지 않았다: "
                  f"{path}", file=sys.stderr)
            continue
        try:
            try:
                info = path.lstat()
            except FileNotFoundError:
                current = board_git_rules._BoardGitRootFileState(False, None)
            else:
                if not stat.S_ISREG(info.st_mode):
                    current = board_git_rules._BoardGitRootFileState(True, None)
                else:
                    current = board_git_rules._BoardGitRootFileState(True, fs.read_bytes_shared(path))

            # 이미 원본으로 돌아갔으면 워킹트리는 no-op. 엔진 산출과 원본 어느 쪽도 아니면
            # hook/동시 편집이 만든 제3의 상태이므로 소유권 밖이다 — bytes를 최우선 보존한다.
            if current == before:
                restored.append(name)
                continue
            if current != expected:
                print(f"  ⚠ board rollback 이 스냅샷 이후 제3자 변경을 보존했다: {path} "
                      "(engine backfill/원본 어느 쪽과도 달라 덮어쓰지 않음).",
                      file=sys.stderr)
                continue

            if before.existed:
                assert before.contents is not None
                path.write_bytes(before.contents)
            else:
                # 맨 `unlink` 는 read-only 속성 파일을 Windows 에서 거부한다 — 공용 seam 이
                #   속성을 풀고 재시도해 backfill 잔재가 워킹트리에 눌러앉지 않게 한다.
                fs.force_unlink(path, missing_ok=True)
            restored.append(name)
        except OSError as exc:
            print(f"  ⚠ board rollback 루트 파일 복원 실패: {path} ({exc})", file=sys.stderr)
    return tuple(restored)


def _ensure_board_root_files(home) -> tuple[str, ...]:
    """board 루트 엔진 파일들을 멱등 보강하고 **이번 호출이 실제로 쓴** 파일명만 돌려준다."""
    written: list[str] = []
    if _ensure_board_gitattributes(home):
        written.append(".gitattributes")
    if _ensure_board_gitignore(home):
        written.append(".gitignore")
    return tuple(written)


def _board_git_enabled(home) -> bool:
    """board 가 별도 git 으로 분리됐고 sync 가능한가 — `Home.board_root/.git` 존재 + git 바이너리.

    True 면 ticket mutation 이 board git 에 commit/pull/push 한다. False 면 sync 전부
    no-op(legacy·git 부재) — `Home.board_root` 가 wiki/ 를 가리키는 legacy 에선
    `wiki/.git` 가 없어 자동으로 False(superproject git 은 REPO 루트에 산다). board/ 분리
    형상에서만 `board/.git`(submodule git 파일/디렉토리)이 존재한다. git 바이너리 부재면
    분리 형상이라도 no-op(fail-soft·sync 불능).
    """
    if shutil.which("git") is None:
        return False
    return (home.board_root / ".git").exists()


def _board_git_lock_file(home) -> Path:
    """board-git mutation 직렬화 락 파일 — `<REPO>/.project_manager/.local/board-git.lock`.

    `BOARD_LOCK` 처럼 import-time 상수로 굳히지 않고 **REPO 추종 lazy 해소**로 둔다(`Home.board_root`
    선례) — hermetic 테스트는 `REPO` 를 monkeypatch 하므로 락 파일이 자동으로 tmp 로 따라간다.
    별도 상수 seam 을 하나 더 두면 그걸 잊은 새 테스트가 실 루트에 락 파일을 쓴다.
    """
    return home.root / ".project_manager" / ".local" / "board-git.lock"


@contextlib.contextmanager
def board_git_lock(home) -> Iterator[None]:
    """board-git mutation(commit→push→rollback) 전체를 직렬화하는 OS 파일락.

    `board_lock` 은 *파일* mutation 임계구역(load→rename)만 감싸고 git ops 는 그 밖에 있어서,
    같은 clone 의 두 슬롯이 commit→push→rollback 을 인터리브할 수 있었다 — 남이 방금 만든
    claim 커밋을 내 rollback 이 되돌리는 창. 이 락이 그 트랜잭션을 통째로 직렬화한다.

    **락 순서 = board_git_lock → board_lock (전역 고정)**. claim 은 이 락을 잡은 채 board_lock
    을 잡으므로, 역순 획득이 어디에도 없어야 데드락이 없다: best-effort sync 7 callsite는
    board_lock을 이미 놓은 뒤 불리고, `_prefix_relabel`도 board_lock **밖**에서 이 락을 먼저 잡는다.

    board-git 비활성(legacy)이면 no-op — 락 파일조차 만들지 않는다(현 동작 무변경).
    **재진입 금지**(flock 관례·board_lock 과 동일) — 이 구간 안에서 이 컨텍스트를 다시 잡는
    헬퍼를 부르지 않는다. 프로세스가 죽으면 OS 가 자동 해제한다(stale-lock 없음).
    """
    if not _board_git_enabled(home):
        yield
        return
    with fs.exclusive_file_lock(_board_git_lock_file(home)):
        yield


def _board_git_head(home) -> str | None:
    """board git 의 현재 HEAD SHA (없으면 None) — claim rollback 의 복귀 지점 기록용."""
    r = git._board_git(home, ["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def _board_git_head_detached(home) -> bool:
    """board git 의 HEAD 가 detached 인가.

    `git symbolic-ref -q HEAD` 는 HEAD 가 브랜치를 가리키면(attached) 그 ref 를 출력하며
    rc=0, HEAD 가 커밋을 직접 가리키면(detached) **rc=1** 이다(`-q` 로 에러 메시지 억제·locale
    무관). detached HEAD 는 dirty 도 offline 도 아닌 제3의 상태로, prefetch 오진과
    best-effort orphan 누적의 공통 근원이라 두 경로가 이 판정을 공유한다.

    **rc=1 만 detached** (codex must-fix): gitdir 손상/조회 불능 같은 fatal 오류는 rc=128 로
    떨어지는데 이를 detached 로 취급하면 "offline 아님·detached" 오진으로 claim 을 잘못
    차단하고 best-effort commit 도 잘못 skip 한다 — fatal 은 False 로 흘려 기존 실패
    경로(dirty/offline/정상 분기)가 처리하게 한다.

    **fail-soft**: 예외(timeout·git 소실)·rc==0(attached)·rc==128(fatal) 모두 False — 판정
    실패는 현행 경로(detached 가드 미발동)로 흘려보낸다. 거짓 detached 판정이 정상
    sync/claim 을 막는 것보다, 판정 불능 시 현행 동작 유지가 보수적이다(dirty 선체크의
    fail-soft clean 취급과 동형).
    """
    try:
        r = git._board_git(home, ["symbolic-ref", "-q", "HEAD"])
    except (OSError, subprocess.SubprocessError):  # fail-soft: 판정 예외(timeout 등)는 현행 경로(False).
        return False
    return r.returncode == 1


def _board_git_status_porcelain(home) -> str:
    """board git working tree 의 uncommitted 변경 요약 (`status --porcelain`·locale 무관).

    `--porcelain` 은 번역되지 않는 안정 포맷이라(cp949·한글 git 메시지 무관) dirty 판정에
    robust 하다 — non-empty 면 staged/unstaged/untracked 변경이 있다는 뜻. rc≠0(이상)이나
    예외는 빈 문자열로 fail-soft 처리해 호출부가 dirty 아님(=clean)으로 보게 한다(보수적).

    **dirty 판정의 단일 원천**이다 — 목록/추적여부 분해(`_board_git_dirty_entries`·
    `_board_git_has_tracked_changes`)는 이 출력을 파싱만 한다. dirty 는 claim 을
    막는 조건이 아니라 (a) 원격이 앞섰을 때 통합 가능성 판정과 (b) 잔여 차단 진단의 재료다.

    `_BOARD_GIT_DRAFT_PATHSPEC` 으로 `tickets/.drafts/`(미충전 draft)를 제외한다 —
    draft 는 board-git 관점에서 아예 존재하지 않는 것처럼 보여야 한다(진단 목록에도 안 나온다).
    """
    try:
        r = git._board_git(home, ["status", "--porcelain", "--", *board_git_rules._BOARD_GIT_DRAFT_PATHSPEC])
    except (OSError, subprocess.SubprocessError):  # fail-soft: status 예외(timeout 등)는 clean 취급(보수적).
        return ""
    return r.stdout if r.returncode == 0 else ""


def _board_git_dirty_entries(home) -> tuple[tuple[str, str], ...]:
    """board 의 미커밋 변경 목록 — `(상태코드, 경로)` 튜플 (draft 제외·판정 단일 지점).

    dirty 의 **원천 판정은 `_board_git_status_porcelain()` 하나**다 — 이 함수는 그 출력을 파싱만
    한다(두 벌 판정 금지). porcelain 포맷은 `XY <path>`(코드 2칸 + 공백 + 경로)이고 rename 은
    `R  old -> new` 라 화살표 뒤(현재 경로)를 취한다. 공백/한글 경로는 git 이 따옴표로 감싸므로
    양끝 `"` 만 벗긴다(진단 출력용이라 8진 이스케이프까지 복원하지 않는다).
    """
    entries: list[tuple[str, str]] = []
    for line in _board_git_status_porcelain(home).splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            entries.append((code, path))
    return tuple(entries)


def _dirty_has_tracked(entries: Sequence[tuple[str, str]]) -> bool:
    """이 변경 목록에 **추적 파일** 변경이 있는가 (untracked-only 는 False).

    `pull --rebase` 는 **untracked-only dirty 에선 rc=0 으로 성공**한다(architect 실측) — 통합을
    막는 건 dirty 일반이 아니라 *추적* 변경뿐이다. 옛 prefetch 가 이 구분 없이 board 전체
    **판정은 여기 한 곳뿐**이다 — 이미 목록을
    들고 있는 호출부(prefetch 의 pull 실패 분류)는 이 함수를, 목록이 없는 호출부는 아래
    `_board_git_has_tracked_changes()` 를 쓴다(같은 규칙·git 호출만 다름).
    """
    return any(code != "??" for code, _ in entries)


def _board_git_has_tracked_changes(home) -> bool:
    """board 워킹트리에 추적 파일 변경이 있는가 — 목록을 새로 떠서 `_dirty_has_tracked` 로 판정."""
    return _dirty_has_tracked(_board_git_dirty_entries(home))


def _board_git_rebase_in_progress(home) -> bool:
    """board git 이 rebase 진행 중(충돌 미해소)인가 — `.git/rebase-merge`·`rebase-apply` 존재.

    mid-rebase 는 detached 의 부분집합이지만 **처방이 다르다**(`rebase --abort/--continue` vs
    `checkout <branch>`)
    먼저 갈라야 한다. 그리고 이 선체크가 **어떤 board-git mutation 보다도 앞**이어야 한다:
    mid-rebase 에서 `git commit -- <paths>` 는 rc=0 으로 **detached rebase HEAD 위에** 커밋을
    만들어 버린다(architect 실측). fail-soft — 판정 예외는 False(현행 경로로 흘림).
    """
    for name in ("rebase-merge", "rebase-apply"):
        try:
            r = git._board_git(home, ["rev-parse", "--git-path", name])
        except (OSError, subprocess.SubprocessError):  # fail-soft: 판정 예외는 현행 경로(False).
            return False
        if r.returncode != 0:
            continue
        raw = r.stdout.strip()
        # `--git-path` 는 -C(board_root) 기준 상대경로를 낸다 — 절대경로면 `/` 연산이 그대로 채택.
        if raw and (home.board_root / raw).exists():
            return True
    return False


def _board_git_upstream(home) -> board_git_rules._BoardGitUpstream | None:
    """현재 브랜치의 upstream 설정 (미설정·판정 불가면 None) — **네트워크 접촉 0**.

    `branch.<b>.remote`/`branch.<b>.merge` 를 직접 읽는다. `@{upstream}` 해소는 원격-추적 ref 가
    아직 없으면(첫 fetch 전) 실패해서 "upstream 미설정" 과 구별되지 않는데, 그 둘은 claim 진단의
    서로 다른 사유다(설정 없음 ≠ offline) — 그래서 설정값 자체를 본다.
    """
    try:
        head = git._board_git(home, ["rev-parse", "--abbrev-ref", "HEAD"])
        if head.returncode != 0:
            return None
        branch = head.stdout.strip()
        if not branch or branch == "HEAD":  # detached — 선체크가 이미 걸렀다(방어적).
            return None
        remote = git._board_git(home, ["config", "--get", f"branch.{branch}.remote"])
        merge = git._board_git(home, ["config", "--get", f"branch.{branch}.merge"])
    except (OSError, subprocess.SubprocessError):  # fail-soft: 판정 예외는 upstream 미상(None).
        return None
    if remote.returncode != 0 or merge.returncode != 0:
        return None
    remote_name, merge_ref = remote.stdout.strip(), merge.stdout.strip()
    if not remote_name or not merge_ref:
        return None
    return board_git_rules._BoardGitUpstream(remote_name, branch, merge_ref)


def _board_git_fetch(home, remote: str) -> bool:
    """원격 상태를 추적 ref 로 당긴다 — **읽기 전용**(워킹트리·HEAD 미접촉). 성공 True.

    선점 감지를 `pull --rebase` 대신 fetch+ls-tree 로 하는 근거가 이것이다 — dirty 든 behind 든
    로컬 상태와 무관하게 원격을 볼 수 있고, 실패해도 로컬 변경이 0 이다.
    """
    try:
        return git._board_git(home, ["fetch", remote]).returncode == 0
    except (OSError, subprocess.SubprocessError):  # fail-soft: fetch 예외(timeout)는 도달 불가로 본다.
        return False


def _board_git_remote_ticket_status(home, tracking: str, ticket_id: str) -> str | None:
    """원격 트리에서 그 티켓이 놓인 status 디렉토리 (없으면 None) — 읽기 전용 선점 감지.

    `git ls-tree -r --name-only <tracking> -- tickets/` 로 **원격 스냅샷을 직접** 읽는다. 로컬
    통합(pull) 성공에 의존하지 않으므로 dirty·behind 와 무관하게 판정된다 — 옛 판정(winner 를
    로컬로 끌어와야 보임)보다 **등가 이상**이다. `-z` 로 NUL 구분 raw 경로를 받는다
    (한글 파일명이 core.quotepath 8진 이스케이프로 나와 매칭을 놓치는 것을 막는다).
    """
    try:
        r = git._board_git(home, ["ls-tree", "-r", "-z", "--name-only", tracking, "--", "tickets/"])
    except (OSError, subprocess.SubprocessError):  # fail-soft: 조회 예외는 판정 불가(None·차단하지 않음).
        return None
    if r.returncode != 0:
        return None
    pattern = re.compile(
        rf"^tickets/({'|'.join(ticket_rules.STATUS_DIRS)})/{re.escape(ticket_id)}(-.*)?\.md$")
    for entry in r.stdout.split("\0"):
        m = pattern.match(entry.strip())
        if m:
            return m.group(1)
    return None


def _board_git_behind(home, tracking: str) -> int | None:
    """원격이 로컬보다 앞선 커밋 수 (`HEAD..<tracking>`) — **계산 불가면 None**(판정불가).

    옛 구현은 예외·rc≠0·비정수를 전부 0 으로 접었다. **차단 판단**에는 그 폴백이 보수적이지만
    (behind 를 모르면 "원격이 안 앞섬" 으로 보고 차단하지 않는다 — 소유 확정 권위는 어차피
    push CAS 다), **진단 문구**에서는 계산 불능과 실제 0 이 구별되지 않아 "원격보다 0 커밋
    뒤처진 상태" 라는 거짓 실측 진술이 나왔다. 그래서 값은 known/unknown 을 보존하고, 차단
    방향의 보수적 해석은 호출부가 `None = 앞서지 않음` 으로 명시한다 — 판단은 불변이고 문구만
    정직해진다.
    """
    try:
        r = git._board_git(home, ["rev-list", "--count", f"HEAD..{tracking}"])
    except (OSError, subprocess.SubprocessError):  # fail-soft: 조회 예외는 판정 불가(None).
        return None
    if r.returncode != 0:
        return None
    try:
        return int(r.stdout.strip())
    except ValueError:
        return None


def _board_git_remote_tip(home, upstream: board_git_rules._BoardGitUpstream) -> str | None:
    """원격 브랜치 tip SHA (`git ls-remote`) — 네트워크 1왕복·로컬 ref 미의존.

    `_board_git_remote_has_commit` 의 폴백 경로다(fetch 불가 시). tip 동일성만으로는 "내 커밋이
    원격에 있는가" 를 절반만 답한다 — 그래서 기본 술어가 아니라 폴백이다.
    """
    try:
        r = git._board_git(home, ["ls-remote", upstream.remote, upstream.merge_ref])
    except (OSError, subprocess.SubprocessError):  # fail-soft: 재확인 실패는 "미확정"(None) 으로 흘린다.
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0].strip()


def _board_git_remote_has_commit(home, upstream: board_git_rules._BoardGitUpstream, commit: str) -> bool:
    """원격 브랜치가 이 커밋을 **포함**하는가 — push 예외 후 성공 여부 재확인.

    push 가 timeout 예외로 끝나도 원격엔 이미 반영됐을 수 있다. 그때 롤백하면 원격 claimed·
    로컬 open 인 **고아 claim** 이 된다. 술어는 tip 동일성이 아니라 **조상 관계** 여야 한다
    (reviewer): 내 push 가 수락된 직후 다른 clone 이 무관 커밋을 얹으면 tip 은 이미 내 커밋이
    아니지만 소유는 확정된 상태다 — 동일성으로 보면 그 경우를 롤백해 고아를 다시 만든다.

    `fetch`(읽기 전용) 후 `merge-base --is-ancestor` 로 판정하고, fetch 가 불가하면
    `ls-remote` tip 동일성으로 **폴백**한다(정확도는 낮지만 "확정된 것을 롤백" 은 막는다).
    판정 불가는 False = 롤백(거짓 소유보다 안전한 방향).
    """
    try:
        if _board_git_fetch(home, upstream.remote):
            r = git._board_git(home, ["merge-base", "--is-ancestor", commit, upstream.tracking])
            return r.returncode == 0
        return _board_git_remote_tip(home, upstream) == commit
    except (OSError, subprocess.SubprocessError):  # fail-soft: 재확인 실패는 미확정(False·롤백).
        return False


def _board_git_rc_out(home, args: list[str]) -> tuple[int, str]:
    """`_board_git` 를 스코프 프리미티브 러너 계약((rc, stdout))으로 어댑트한다.

    모듈 속성 `_board_git` 를 **호출 시점에** 조회하므로 기존 테스트의 monkeypatch(가짜
    board-git)가 그대로 프리미티브 안까지 적용된다.
    """
    r = git._board_git(home, args)
    return r.returncode, r.stdout


def _board_git_relpath(home, path: Path) -> str | None:
    """절대 경로 → board git 루트 기준 상대 pathspec (board git 밖이면 None)."""
    return git.git_scope_relpath(home.board_root, path)


def _board_git_scope_pathspec(home, paths: Sequence[Path], *,
                              root_files: Sequence[str] = ()) -> tuple[str, ...]:
    """이 mutation 이 만진 경로 → board git pathspec (`git_scope_pathspec` 의 board 바인딩).

    스코프 규칙(repo 밖 제외·금지 구역 제외·중복 제거)은 repo-중립 프리미티브
    `git_scope_pathspec` 이 단일 구현으로 갖고 있고, 여기선 board 고유분만 얹는다:
      - `Home.board_root` 를 repo 로, `tickets/.drafts/`(`_BOARD_GIT_SCOPE_EXCLUDE`)를 금지 구역으로.
      - `root_files` 로 준 board 루트 파일(`_BOARD_GIT_ROOT_FILES` 부분집합)만 덧붙인다 —
        **이번 호출의 backfill 이 실제로 쓴 것**(`_ensure_board_root_files()` 반환)만 온다.
        무조건 붙이면 사용자가 편집 중인 `.gitattributes`/`.gitignore` 를 아무 티켓 mutation 이
        대신 커밋해 스코프 커밋이 닫으려는 누출과 **동형** 이 된다(reviewer). 반대로 backfill
        분을 빼면 그 파일이 영구 미커밋으로 남아 배포(areas union·draft ignore)가 무력화되므로,
        "썼으면 싣는다" 가 정확한 조건이다. 파일 부재 시엔 붙이지 않는다(`add` rc=128 fatal 방지).
    반환이 비면 커밋할 스코프가 없다는 뜻(호출부가 no-op 으로 처리).
    """
    rels: list[str] = list(git.git_scope_pathspec(
        home.board_root, paths, exclude_prefixes=board_git_rules._BOARD_GIT_SCOPE_EXCLUDE))
    for name in root_files:
        rel = _board_git_relpath(home, home.board_root / name)
        if rel and rel not in rels and (home.board_root / name).is_file():
            rels.append(rel)
    return tuple(rels)


def _board_git_stageable(home, pathspec: Sequence[str]) -> tuple[str, ...]:
    """`add`/`commit` 이 매치할 수 있는 경로만 남긴다 (`git_scope_stageable` 의 board 바인딩).

    판정 본체(존재/추적 필터·fail-soft)는 repo-중립 프리미티브에 있다 — 여기선 `Home.board_root`
    와 board-git 러너(`_board_git_rc_out`)를 물린다. 미매치 pathspec 이 `add` 를 rc=128 fatal
    로 죽이는 것을 막는 필터라, 디자인 git 쪽 새 스코프도 같은 구현을 쓴다.
    """
    return git.git_scope_stageable(home.board_root, pathspec, run_git=lambda argv: _board_git_rc_out(home, argv))


def _board_git_stage_and_commit(
        home, message: str, paths: Sequence[Path] | None = None, *,
        root_files_written: list[str] | None = None,
        root_files_snapshot: board_git_rules._BoardGitRootFilesSnapshot | None = None,
) -> bool:
    """이 mutation 이 만진 **경로만** stage+commit 한다.

    `paths` = 그 mutation 이 만들거나 옮긴 파일들(절대 경로). `git add -A -- <경로>` +
    `git commit -m … -- <경로>`(부분 커밋)로 닫아, 공유 board 워킹트리에 있는 **무관한 미커밋
    작업**(다른 슬롯 WIP·남의 staged 편집)이 이 커밋에 실려 push 되는 것을 원천 차단한다(D2 —
    claim 커밋에 타 슬롯 WIP 가 함께 push 된 것이 실증됐다). `add` 선행은 **필수**다:
    `git commit --only -- <새 경로>` 단독은 그 경로가 untracked 라 `pathspec did not match` 로
    실패한다(architect 실측).

    `paths=None` = **레거시 광역 스코프**(board 전체·draft 제외). 엔진 호출부는 더 이상 이
    형태를 쓰지 않는다 — 마지막 소비자였던 `_prefix_relabel` 도 rewrite 가 실제로 쓴 파일 +
    rename 경로로 스코프됐다. 하위호환(모듈 밖 호출부·테스트)으로만 남긴다.
    **호출부는 반드시 경로를 준다.**

    새 커밋 생성 판정은 rc 가 아니라 **`HEAD != anchor`** 다 — 부분 커밋은 pathspec 이 아무것도
    못 잡으면 rc 가 애매한데, claim 은 "커밋을 못 내면 거짓 소유"라 커밋 *사실* 을
    확인해야 한다. 반환 True = 새 commit 생성.

    stage 직전 board 루트 배포 파일(`areas.md merge=union` 선언 · draft 격리 `.gitignore`)을 멱등
    보강한다. **이번 호출이 실제로 보강한 파일만** pathspec 에 넣는다 — 무조건 넣으면 사용자가
    편집 중인 그 파일을 이 mutation 이 대신 커밋한다(스코프 커밋이 닫는 누출과 동형).
    """
    wrote_root_files = _ensure_board_root_files(home)
    if root_files_written is not None:
        root_files_written.extend(wrote_root_files)
    if root_files_snapshot is not None:
        root_files_snapshot.backfilled.extend(wrote_root_files)

    def _capture_staged_root_index() -> None:
        if root_files_snapshot is not None:
            root_files_snapshot.staged_index = _board_git_index_snapshot(
                home, (), root_files=wrote_root_files)

    before = _board_git_head(home)
    if paths is None:
        git._board_git(home, ["add", "-A", "--", *board_git_rules._BOARD_GIT_DRAFT_PATHSPEC])
        _capture_staged_root_index()
        git._board_git(home, ["commit", "-m", message])
    else:
        pathspec = _board_git_stageable(
            home, _board_git_scope_pathspec(home, paths, root_files=wrote_root_files))
        if not pathspec:
            return False  # 커밋할 스코프 없음(전부 board 밖·draft·미존재) — no-op.
        git.git_scope_stage_and_commit(home.board_root, message, pathspec,
                                   run_git=lambda argv: _board_git_rc_out(home, argv),
                                   after_stage=_capture_staged_root_index)
    return _board_git_head(home) != before


def _board_git_uncommitted_scope(
        home, paths: Sequence[Path] | None, *,
        root_files: Sequence[str] = (),
) -> board_git_rules._BoardGitUncommittedScope:
    """이번 mutation 경로에 남은 board-git 변경만 반환한다.

    best-effort가 local commit을 못 만든 경우, board 전체 dirty를 진단에 쓰면 다른 ticket의
    WIP를 이번 mutation 실패로 오인하고 그 경로까지 복구 대상으로 노출한다. 따라서 호출자가
    선언한 경로만 porcelain -z로 조회한다. ``paths=None``인 레거시 호출부에는 소유 경계가
    없으므로 false-success 판정을 하지 않는다.
    """
    if paths is None:
        return board_git_rules._BoardGitUncommittedScope()
    pathspec = _board_git_scope_pathspec(home, paths, root_files=root_files)
    if not pathspec:
        return board_git_rules._BoardGitUncommittedScope()
    try:
        result = git._board_git(home, [
            "status", "--porcelain", "-z", "--untracked-files=all", "--", *pathspec,
        ])
    except (OSError, subprocess.SubprocessError):  # 진단 실패가 best-effort mutation을 막으면 안 된다.
        return board_git_rules._BoardGitUncommittedScope()
    if result.returncode != 0:
        return board_git_rules._BoardGitUncommittedScope()
    entries = git.git_parse_porcelain_z(result.stdout)
    # pathspec 자체를 보존해야 porcelain rename의 새 경로만으로 old 삭제를 잃지 않는다.
    # root_files 는 이번 commit funnel 이 실제 쓴 이름만 오므로 빈 ticket pathspec인 첫 draft도
    # 정확히 진단하면서, 이미 정합인 루트 파일/사용자 WIP는 이번 실패 범위에 끌어오지 않는다.
    return board_git_rules._BoardGitUncommittedScope(entries, tuple(pathspec))


def _board_git_pull_rebase(home) -> subprocess.CompletedProcess:
    """board git 을 remote 최신화 (`pull --rebase`) — 선점/원격 변경을 로컬에 반영."""
    return git._board_git(home, ["pull", "--rebase"])


def _board_git_push(home) -> subprocess.CompletedProcess:
    """board git 을 remote 로 push — claim 소유 확정(strict)·best-effort 동기(나머지)."""
    return git._board_git(home, ["push"])


def _board_git_mutation_state_suffix(local_commit_ready: bool) -> str:
    """best-effort caller의 성공 문구가 uncommitted 상태를 성공처럼 숨기지 않게 한다."""
    return "" if local_commit_ready else " (board-git 기록 보류: local-only/uncommitted)"


def _classify_pull_failure(home, upstream: board_git_rules._BoardGitUpstream,
                           pull: subprocess.CompletedProcess,
                           behind: int) -> board_git_rules._ClaimPrefetch:
    """`pull --rebase` 실패의 원인 분류 — **구조 판정만** 쓴다 (문자열 매칭 0·codex must-fix).

    옛 분류는 "추적 변경이 있나" 만 보고 나머지를 전부 offline 으로 흘렸다. 그런데 git 은
    **원격에서 새로 들어오는 파일과 같은 경로에 로컬 untracked 파일이 있으면** pull 을 거부한다
    ("untracked working tree files would be overwritten") — 네트워크가 아니라 **로컬 파일 충돌**
    인데 "offline · 도달 불가" 로 진단하고 네트워크를 확인하라는 틀린 안내를 냈다(D5 잔여).

    판정 축을 git 메시지가 아니라 **관측 가능한 상태** 로 잡는다(로케일·git 버전 무관):
      1. rebase 진행 중인가(`.git/rebase-merge`) → 충돌 → `--abort/--continue`.
      2. **원격에 여전히 닿는가**(fetch 재시도·읽기 전용) → 안 닿으면 그때만 offline 이다.
         이 프로브가 오분류 방향을 안전한 쪽으로 기울인다 — 진짜 offline 은 fetch 도 실패하므로
         정확히 offline 이 되고, 네트워크가 살아 있는데 pull 만 실패한 경우는 **결코 offline 이라
         부르지 않는다**(회색지대 = pull 순간만 끊겼다 살아난 경우인데, 그때 안내는 "정리 후
         재시도" 라 재시도하면 성공한다).
      3. 닿는데 미커밋 파일이 있다 → **dirty 로 흡수**한다. untracked 충돌과 추적 dirty 는 사용자
         액션이 같기 때문이다 — *그 파일을 커밋하거나 치워라*(사용자에게 필요한 건 분류 개수가
         아니라 할 일). 안내는 `??` 항목이 있으면 "옮기거나 지워라" 를 함께 낸다.
      4. 닿고 미커밋도 없다 → 원인 미상. 이건 위 셋 중 어디에도 정직하게 못 넣으므로(액션이
         "직접 돌려 원인을 보라" 로 다르다) 별도 사유로 두고 git 출력 마지막 줄을 함께 낸다.
    """
    dirty = _board_git_dirty_entries(home)
    detail = ""
    for line in reversed((pull.stderr or "").strip().splitlines()):
        if line.strip():
            detail = line.strip()[:board_git_rules._BOARD_GIT_DETAIL_MAX]
            break
    if _board_git_rebase_in_progress(home):
        block = board_git_rules._CLAIM_BLOCK_REBASE
    elif not _board_git_fetch(home, upstream.remote):
        block = board_git_rules._CLAIM_BLOCK_OFFLINE
    elif dirty:
        block = board_git_rules._CLAIM_BLOCK_DIRTY
    else:
        block = board_git_rules._CLAIM_BLOCK_INTEGRATION
    return board_git_rules._ClaimPrefetch(block=block, behind=behind, dirty=dirty, detail=detail)


def _board_git_dirty_hint(dirty: Sequence[tuple[str, str]]) -> str:
    """더러운 파일 표본 + 총계 + **경로 스코프** 커밋 안내 (진단 문구 단일 지점).

    (`add -A -- . ':!tickets/.drafts'`) 공유 board 에서 그 커맨드는 **남의
    미완성 편집을 내가 커밋**하게 만든다.
    대신 실제로 막고 있는 파일을 이름으로 보여주고, 자기 것만 골라 커밋하도록 안내한다.
    """
    if not dirty:
        return ""
    sample = [path for _, path in dirty[:board_git_rules._BOARD_GIT_DIRTY_SAMPLE_MAX]]
    listed = " ".join(f"'{path}'" for path in sample)
    more = (f" (외 {len(dirty) - len(sample)}건)" if len(dirty) > len(sample) else "")
    untracked = [path for code, path in dirty if code == "??"]
    # 미추적(`??`)은 커밋 외에 **치우기**도 답이다 — 원격에서 같은 경로의 파일이 들어오면 git 이
    # "untracked working tree files would be overwritten" 로 통합을 거부한다. 이 문장은 `??`
    # 항목 유무로만 결정한다(git 메시지 문자열에 의존하지 않음·로케일 무관).
    untracked_note = (
        f" 미추적 {len(untracked)}건({', '.join(untracked[:board_git_rules._BOARD_GIT_DIRTY_SAMPLE_MAX])})은 "
        f"커밋하거나 board 밖으로 옮겨라 — 원격에서 같은 경로가 들어오면 git 이 덮어쓰기를 "
        f"거부한다." if untracked else "")
    return (f"미커밋 {len(dirty)}건: {', '.join(sample)}{more}. "
            f"**자기 변경만** 골라 커밋하라 — "
            f"`git -C .project_manager/board add -- {listed} && "
            f"git -C .project_manager/board commit`. "
            f"(`add -A` 로 쓸어담지 마라 — 남의 미완성 편집까지 커밋된다.)"
            f"{untracked_note}")


def _board_git_index_pathspec(home, paths: Sequence[Path], *,
                              root_files: Sequence[str] = ()) -> tuple[str, ...]:
    """index snapshot/restore pathspec — 부재한 루트 파일도 이름으로 포함한다.

    일반 commit pathspec 은 실제로 쓴 루트 파일만 받지만, rollback 사전 스냅샷은 아직 생성되지
    않은 파일의 **부재**도 기억해야 한다. 그래서 존재 여부로 거르는
    `_board_git_scope_pathspec(..., root_files=...)` 와 별도인 이 작은 경계를 둔다.
    """
    rels = list(_board_git_scope_pathspec(home, paths))
    for name in root_files:
        if name not in board_git_rules._BOARD_GIT_ROOT_FILES:
            continue
        rel = _board_git_relpath(home, home.board_root / name)
        if rel and rel not in rels:
            rels.append(rel)
    return tuple(rels)


def _board_git_index_snapshot(
        home, paths: Sequence[Path], *, root_files: Sequence[str] = (),
) -> dict[str, tuple[str, str]] | None:
    """대상 경로들의 **index 항목** 을 그대로 캡처 — `{경로: (mode, blob sha)}`.

    롤백이 "미커밋 작업을 상태·내용 그대로 보존" 하려면 무관 파일뿐 아니라 **claim 대상 파일
    자신** 도 보존해야 한다. 옛 롤백은 복원 후 무조건 `add -A` 를 해서, claim 전에 unstaged
    편집이었거나 untracked 였던 티켓을 **staged 로 바꿔** 놓았다(codex must-fix).

    케이스 분기("unstaged 였으면 stage 하지 마라")로 막지 않는다 — `MM`(staged+unstaged 혼합)·
    mode 변경 같은 조합에서 또 샌다. 대신 index 항목을 **그대로 떠서 그대로 되돌린다**: 상태
    조합이 늘어도 로직은 안 는다. index 에 없는 경로(untracked)는 맵에서 빠지고, 그 사실 자체가
    "index 에서 제거" 라는 복원 지시가 된다.

    **캡처 실패는 None** — 파싱 불가·unmerged(stage≠0) 항목처럼 정확 복원을 보장할 수 없으면
    조용히 다른 상태로 만들지 말고 호출부가 보수적 현행 동작(+loud 경고)으로 가게 한다.
    """
    pathspec = _board_git_index_pathspec(home, paths, root_files=root_files)
    if not pathspec:
        return {}
    try:
        r = git._board_git(home, ["ls-files", "--stage", "-z", "--", *pathspec])
    except (OSError, subprocess.SubprocessError):  # 캡처 실패는 None(보수적 폴백).
        return None
    if r.returncode != 0:
        return None
    snapshot: dict[str, tuple[str, str]] = {}
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        meta, tab, path = entry.partition("\t")
        fields = meta.split()
        if not tab or not path or len(fields) != 3:
            return None                      # 예상 밖 포맷 — 정확 복원 보장 불가.
        mode, blob, stage = fields
        if stage != "0":
            return None                      # unmerged 항목 — 3-way index 는 그대로 못 되돌린다.
        snapshot[path] = (mode, blob)
    return snapshot


def _board_git_restore_index(home, paths: Sequence[Path],
                             snapshot: dict[str, tuple[str, str]], *,
                             root_files: Sequence[str] = ()) -> bool:
    """캡처한 index 상태로 되돌린다 (`update-index`) — 전부 성공해야 True.

    항목이 있던 경로는 `--cacheinfo <mode>,<sha>,<path>` 로 **그 blob 그대로** 되꽂고(그래서
    `MM` 처럼 index 와 워킹트리가 다른 상태도 정확히 복원된다), 없던 경로는 `--force-remove` 로
    index 에서 뺀다(untracked 복원). 워킹트리 내용 복원은 `_board_git_restore_claim_files` 몫이라
    둘을 합치면 `git status` 출력이 claim 직전과 같아진다.
    """
    ok = True
    for rel in _board_git_index_pathspec(home, paths, root_files=root_files):
        entry = snapshot.get(rel)
        if entry is not None:
            mode, blob = entry
            r = git._board_git(home, ["update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}"])
        else:
            r = git._board_git(home, ["update-index", "--force-remove", "--", rel])
        ok = ok and r.returncode == 0
    return ok


def _board_git_refresh_after_rollback(home) -> None:
    """롤백 마무리 — winner 를 로컬에 반영하되, 못 하면 **조용히 넘어가지 않는다**.

    `pull --rebase` 는 추적 파일이 dirty 면 실패한다. 공유 워킹트리에서 그 dirty 는 *남의*
    작업일 수 있으므로 stash/reset 으로 치우지 않는다(auto-stash 기각) — 대신 로컬
    board 뷰가 stale 이라는 사실을 loud 하게 알린다. 옛 코드는 이 실패를 suppress 해 winner
    미반영 사실이 침묵으로 묻혔다.

    **fetch 가 이 판정의 전제다**(reviewer must-fix): 롤백은 push 가 거부된 직후에 불리는데,
    그 시점의 원격-추적 ref 는 *정의상* stale 이라(우리가 아직 winner 를 안 당겼다) fetch 없이
    `behind` 를 읽으면 항상 0 → 두 분기(winner 반영·stale 경고) 모두 도달 불가가 된다.

    **생략만이 아니라 실패도 loud** 다(codex must-fix): 사전 감지는 *추적* 변경만 보므로,
    untracked 경로 충돌처럼 그 관문을 통과하고도 pull 이 rc≠0 인 갈래가 있다. rc 를 확인하고
    사유는 `_classify_pull_failure`(claim prefetch 와 동일 판정기)로 붙여 안내한다.
    """
    with contextlib.suppress(Exception):
        upstream = _board_git_upstream(home)
        if upstream is None:
            return
        _board_git_fetch(home, upstream.remote)   # 추적 ref 갱신 — 아래 behind 판정의 전제.
        behind = _board_git_behind(home, upstream.tracking)
        if behind is None or behind <= 0:
            return
        if _board_git_has_tracked_changes(home):
            print(f"  ⚠ 로컬 board 뷰 stale — 원격이 {behind} 커밋 앞서지만 미커밋 변경이 있어 "
                  f"pull --rebase 를 생략했다(남의 작업을 치우지 않는다). 정리 후 "
                  f"`git -C .project_manager/board pull --rebase` 로 최신화하라.",
                  file=sys.stderr)
            return
        pull = _board_git_pull_rebase(home)
        if pull.returncode == 0:
            return
        # **실패도 생략과 똑같이 loud** 해야 약속이 반쪽이 아니다 — untracked 경로 충돌처럼
        # 사전 감지(추적 변경 유무)를 통과하고도 rc≠0 인 갈래가 있다. 사유 판정은 claim prefetch
        # 와 **같은 판정기**를 재사용한다(새 로직 0·두 벌 금지).
        failure = _classify_pull_failure(home, upstream, pull, behind)
        label = board_git_rules._BOARD_GIT_BLOCK_LABELS.get(failure.block or "", failure.block or "원인 미상")
        hint = _board_git_dirty_hint(failure.dirty)
        detail = f" (git: {failure.detail})" if failure.detail and not hint else ""
        parts = [f"  ⚠ 로컬 board 뷰 stale — 원격이 {behind} 커밋 앞서는데 "
                 f"pull --rebase 가 실패했다({label}){detail}."]
        if hint:
            parts.append(hint)
        parts.append("해소 후 `git -C .project_manager/board pull --rebase` 로 최신화하라.")
        print(" ".join(parts), file=sys.stderr)


def _board_git_root_indexes_safe_to_restore(
        home, before_index: dict[str, tuple[str, str]] | None,
        root_snapshot: board_git_rules._BoardGitRootFilesSnapshot | None,
        root_files: Sequence[str],
) -> tuple[str, ...]:
    """현재 index가 원본/engine-stage 중 하나인 실제 backfill 파일만 복원 허용한다."""
    if not root_files:
        return ()
    if before_index is None or root_snapshot is None or root_snapshot.staged_index is None:
        print("  ⚠ board rollback 이 루트 파일 index의 제3자 변경 여부를 확인하지 못해 "
              "덮어쓰지 않았다. `git -C .project_manager/board status` 로 확인하라.",
              file=sys.stderr)
        return ()
    current = _board_git_index_snapshot(home, (), root_files=root_files)
    if current is None:
        print("  ⚠ board rollback 이 현재 루트 파일 index를 읽지 못해 덮어쓰지 않았다. "
              "`git -C .project_manager/board status` 로 확인하라.", file=sys.stderr)
        return ()
    safe: list[str] = []
    for name in root_files:
        rel = _board_git_relpath(home, home.board_root / name)
        if rel is None:
            continue
        now = current.get(rel)
        before = before_index.get(rel)
        staged = root_snapshot.staged_index.get(rel)
        if now == before or now == staged:
            safe.append(name)
            continue
        print(f"  ⚠ board rollback 이 스냅샷 이후 제3자 index 변경을 보존했다: {rel} "
              "(engine stage/원본 어느 쪽과도 달라 덮어쓰지 않음).", file=sys.stderr)
    return tuple(safe)


def _board_fetch_head_fresh(home, ttl: float) -> bool:
    """board-git FETCH_HEAD mtime 이 `ttl` 초 이내면 True — 직전 fetch 재사용(fetch 생략).

    별도 캐시 파일/상태 신설 없이 git 자신의 FETCH_HEAD mtime 만으로 **cross-process** TTL 판정.
    경로는 `git rev-parse --git-path FETCH_HEAD`(submodule/worktree gitdir 레이아웃 무관 해소).
    FETCH_HEAD 부재(한 번도 fetch 안 함)·경로 조회 실패·stat 실패면 False(fetch 진행·보수적).
    """
    try:
        r = git._board_git(home, ["rev-parse", "--git-path", "FETCH_HEAD"])
    except (OSError, subprocess.SubprocessError):  # 경로 조회 예외는 stale 취급(fetch 진행).
        return False
    if r.returncode != 0 or not r.stdout.strip():
        return False
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = home.board_root / p   # rev-parse 는 -C home.board_root 기준 상대 경로를 낼 수 있다.
    try:
        return (time.time() - p.stat().st_mtime) < ttl
    except OSError:
        return False           # FETCH_HEAD 부재(한 번도 fetch 안 함) → fetch 진행.


def _board_git_freshness(home) -> board_git_rules._BoardFreshness:
    """board submodule freshness 를 부트스트랩과 **같은 판정**으로 1줄 만든다 (없으면 line=None).

    board 비-git(legacy·`_board_git_enabled()` False)이면
    `line=None`·`verified=False`(표면화 생략·오탐 0). 그 외엔 board-git 을 fetch(원격 실측·offline 이면 fail-soft) 후
    detached/dirty/ahead·behind 를 board-git 공급자로 수집해, git_freshness의
    `_format_freshness`로 포맷한다 — **판정 단일화**(중복 0). list
    는 read-only 라 pull 하지 않는다(부트스트랩의 자동 ff-pull 과 달리 표면화만·never-block).
    offline(fetch 실패)이면 remote-tracking 스냅샷을 "최신"으로 주장하지 않고 "판정불가 —
    스냅샷일 수 있음"으로 fail-soft.
    """
    # 비-git 홈에서는 원격 조회를 생략한다.
    if not _board_git_enabled(home):
        return board_git_rules._BoardFreshness(None, False)
    # fetch 전 TTL 가드: 직전 fetch 가 TTL 이내면 재사용(원격 실측된 스냅샷이라
    # fetched=True)·fetch 생략. 아니면 advisory 짧은 timeout(5s)으로 fetch(offline 이면 fail-soft).
    if _board_fetch_head_fresh(home, board_git_rules._FRESHNESS_FETCH_TTL_SECONDS):
        fetched = True
    else:
        try:
            fetched = git._board_git(
                home, ["fetch", "origin"],
                timeout=board_git_rules._FRESHNESS_FETCH_TIMEOUT_SECONDS).returncode == 0
        except (OSError, subprocess.SubprocessError):  # fetch 예외(timeout·offline)는 fail-soft(판정불가).
            fetched = False
    detached = _board_git_head_detached(home)
    ahead = behind = None
    try:
        ab = git._board_git(home, ["rev-list", "--left-right", "--count", "HEAD...@{u}"])
        if ab.returncode == 0:
            parsed = git_freshness.parse_git_ahead_behind(ab.stdout)
            if parsed is not None:
                ahead, behind = parsed
    except (OSError, subprocess.SubprocessError):  # rev-list 예외는 upstream 미상(behind None) 취급.
        pass
    scope = {"fetched": fetched, "detached": detached, "dirty": None,
             "ahead": ahead, "behind": behind, "note": None}
    if behind and behind > 0:
        # behind>0 에서만 dirty 를 조회(로컬 git status·common 경로 무오버헤드)해 경고 사유를
        # 정확히 채운다(`_behind_warning` = ⚠ behind N — 수동 동기 필요 (fetch/dirty/diverged)).
        scope["dirty"] = bool(_board_git_status_porcelain(home).strip())
        scope["note"] = git_freshness._behind_warning(scope)
    return board_git_rules._BoardFreshness(f"board-git: {git_freshness._format_freshness(scope)}", fetched)
