#!/usr/bin/env python3
"""worktree 풀 엔진 — 슬롯 리스 alloc/release/reclaim_stale/force_release.

repo별 git worktree 풀로 *코드*를 격리한다(병렬 브랜치·나중 git merge). 슬롯은
브랜치-무관 재사용 컨테이너(`work/<repo>_<N>`)이고, 브랜치는 슬롯 worktree 의 git HEAD 에서
**live** 로 읽는다(git=단일 진실·장부 저장 폐지·드리프트 불가능·
`current_branch(slot)`). 코드 동시성의 격리 레이어 — 보드(공유 `.project_manager`)
동시성은 board.py 가 따로 책임진다(별 모듈·여기선 import 하지 않는다).

설계:
  - 슬롯 = `work/<repo>_<N>`(repo + 번호·브랜치 무관·전이적 물리자원). 폴더명에 브랜치를
    안 박는다(박으면 stale).
  - 브랜치 = 슬롯 worktree 의 git HEAD 에서 live 조회(`current_branch(slot)`
    장부 저장 폐지·git=진실). 브랜치 변경 = 같은 슬롯 재체크아웃(리스 유지).
  - 리스 = 작업스트림(브랜치) 단위. alloc@bootstrap · release@작업완료(세션종료/회전 ≠
    release). 회전은 리스 유지 + 같은 슬롯 재부착.
  - stale 회수 = pid 생존만(타임아웃/heartbeat 기각·조기회수 위험). dirty 면 stash 보존.
  - git 연동 = DI seam(주입 가능 runner) — `git worktree add/remove`·dirty 검사·stash·
    submodule init 을 seam 통해 호출 → hermetic 테스트(mock 또는 실 임시 repo).

장부 동시쓰기 보호 = **배타 파일락**(공용 `file_lock` seam·경로 규약
`.local/worktree-leases.lock` 만 이 도구 소유). board.py 의 `board_lock` 과 *같은
프리미티브*를 쓰지만 board 모듈에는 여전히 의존하지 않는다(top-level import 금지 — seam 은
형제를 로드하지 않는 leaf).
(예외 — `switch` 의 보호목록 조회만 board 를 **동적 sibling 로드**한다: `_load_board`·
`_resolve_protected`. `identity_args`와 동형의 단방향 로더로 top-level import
는 여전히 없고, 로드 실패는 default 보호목록 폴백이다.)
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Callable, NamedTuple

_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise



# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.7.7"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제 모듈의 baked ENGINE_REV 를 이 사본의 것과 대조한다 (fail-loud·skew→명시 에러).

    불일치/부재(구형 형제는 리터럴 부재=None)면 사본 skew → 명시 에러(어느 파일이 어떤 rev 인지
    지목 + pm-update 안내). self-contained(engine_rev.py 런타임 의존 0)라 부분복사도 정확 검출한다.
    """
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True  # fail-soft 로더가 재-raise 식별
        raise err


def _is_engine_rev_skew(exc):
    """fail-soft 소비 지점에서 rev skew만 재-raise하기 위한 구조화 판정."""
    return getattr(exc, "_engine_rev_skew", False)


def _require_engine_sibling(path: Path, filename: str) -> None:
    """load-bearing 형제 모듈의 **부재**를 stale 사본과 같은 진단으로 번역한다 (fail-loud).

    부재는 raw `FileNotFoundError` 로 터져 복구 방법(pm-update 재동기)을 알려주지 않는다 —
    원인이 부분/수동 복사라는 점은 stale 사본과 같으므로 같은 marked skew 로 표출한다
    (board.py `_require_engine_sibling` 동형·self-contained 복제). *옵션* 형제(`_load_board`
    처럼 부재가 정상 폴백인 곳)에는 쓰지 않는다."""
    if path.exists():
        return
    err = RuntimeError(
        f"엔진 사본 불완전 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 형제 "
        f"{filename} 을(를) 찾지 못했다: {path} (부분/수동 복사). `pm-update`(또는 "
        "pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
    )
    err._engine_rev_skew = True
    raise err


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·pm_*.py 와 동일 앵커 관례.
# multi-PM 모델에서 이 도구가 어느 worktree cwd 에서 호출돼도 자기 위치(multi-PM 루트 .project_manager)를
# 자동 타깃한다.
REPO = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPO / ".project_manager" / ".local"               # per-clone scratch (git-ignored)
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"               # 리스 장부 — leases[] + tasks[]
LEASES_LOCK = LOCAL_DIR / "worktree-leases.lock"               # 장부 read-modify-write 직렬화 락
TASKS_DIR = LOCAL_DIR / "tasks"                                # task 서술 공간(.local/tasks/<이름>/·pm_state·메타)
WORK_DIR = REPO / "work"                                        # worktree 풀 루트 (multi-PM 루트 gitignore)
REPOS_DIR = REPO / ".repos"                                     # worktree 의 공유 .git 원 (bare)
REPO_HOOKS_DIR = LOCAL_DIR / "repo-hooks"                       # per-repo pre-push 보호훅(프레임워크 소유·gitignore)
# git subprocess 타임아웃 (초) — captured 러너(_real_git_runner)의 subprocess timeout + worktree
# add console-visible 러너(_real_git_runner_interactive)의 상한. **타임아웃의 실패모드 = 정상 op 도
# 죽임(false-kill)** — 대형 repo 의 worktree add(로컬 bare→full checkout·느린 디스크/VPN/
# Windows)가 진행 중인데도 짧은 고정값(옛 120)에 걸려 false-kill 되던 실측 블로커. submodule 선례
# (SUBMODULE_TIMEOUT)와 동형으로 env override + 관대한 기본으로 옮긴다.
#
# env override (`_resolve_submodule_timeout` 미러): 코드 직수정은 worktree_pool.py=manifest
#   엔진이라 다음 pm_update 가 원복 → **env `PM_GIT_TIMEOUT`(pm_update overwrite 를 넘어 persist)** 로
#   튜닝한다. `0`/`none`/`unlimited`/빈값 → None(무제한 — 진행이 콘솔에 보이는 worktree add 에 안전·
#   hang 은 콘솔 가시·Ctrl-C), 양의 정수 → 그 초, 미설정/비정상 → 기본 1800(30분·유한 관대). 무제한을
#   기본으로 안 두는 건 captured 계열 fast op(status/checkout 등)의 silent-hang 방지(submodule 3600
#   유한 기본과 동일 철학·무제한은 env opt-in).
#
# ⚠️ **무제한은 console-visible 러너에만** (codex 게이트·설계 모순 fix): `None`(무제한)은 진행이 콘솔에
#   보이는 worktree-add 인터랙티브 러너에만 적용한다. **captured 러너(`_real_git_runner`)는 절대 무제한이
#   되지 않는다** — silent(진행 안 보임)라 무제한이면 base 파생 `fetch origin` 네트워크 stall 시 silent hang
#   한다. 그래서 captured 러너는 GIT_TIMEOUT_SECONDS 가 None 일 때 `_GIT_TIMEOUT_DEFAULT`(유한)로 폴백-캡한다
#   (아래 `_real_git_runner`). 즉 captured=항상 유한·visible add=none 가능.
_GIT_TIMEOUT_DEFAULT = 1800   # 기본 유한 상한 (초·30분) — 미설정/비정상 기본 + captured 러너 무제한 폴백 cap.


def _resolve_git_timeout() -> "int | None":
    raw = os.environ.get("PM_GIT_TIMEOUT")
    if raw is None:
        return _GIT_TIMEOUT_DEFAULT
    raw = raw.strip().lower()
    if raw in ("0", "none", "unlimited", ""):
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except ValueError:
        return _GIT_TIMEOUT_DEFAULT


GIT_TIMEOUT_SECONDS = _resolve_git_timeout()

# submodule init 인터랙티브 러너의 timeout. 짧은 captured git(status·checkout·dirty·
# stash)은 GIT_TIMEOUT_SECONDS(기본 1800) 로 충분하지만, submodule clone 은 대형 family
# repo + VPN 에서 600s 도 초과(실 Windows multi-PM 파일럿 "10분 아슬" 실증) → TimeoutExpired
# 로 죽었다. 인터랙티브 러너는 stdio 를 콘솔에 상속(진행상황·credential 프롬프트 작동)하고
# 이 대폭 확대된 timeout(또는 None=무제한)으로 큰 clone 을 끝까지 돌린다. 수동 콘솔 실행과
# 동일 거동. None 으로 두면 timeout 자체를 끈다(완전 무제한·hang 위험은 콘솔에 가시).
#
# env override (reviewer): 극단적 대형 repo·느린 VPN 에서 1h 도 모자라면 코드 수정 없이
#   `PM_SUBMODULE_TIMEOUT` 로 재조정한다 — `0`/`none`/`unlimited` → None(무제한), 양의 정수 → 그 초.
#   미설정/비정상 → 기본 3600.
def _resolve_submodule_timeout() -> "int | None":
    raw = os.environ.get("PM_SUBMODULE_TIMEOUT")
    if raw is None:
        return 3600
    raw = raw.strip().lower()
    if raw in ("0", "none", "unlimited", ""):
        return None
    try:
        val = int(raw)
        return val if val > 0 else None
    except ValueError:
        return 3600


SUBMODULE_TIMEOUT = _resolve_submodule_timeout()

# git CLI argv → (returncode, stdout). DI seam 의 타입(pm_import.GitRunner 선례).
GitRunner = Callable[[list], "tuple[int, str]"]


# ── 예외 / 데이터 ─────────────────────────────────────────────────────────


class NeedsCreate(Exception):
    """풀 소진 — idle 슬롯이 없어 새 슬롯 생성이 필요하다(호출부 = bootstrap 사용자 게이트).

    `git worktree add` 는 fs 행위라 자동으로 안 한다(사용자 게이트 유지) —
    alloc 호출부(pm-bootstrap)가 이 신호를 받아 사용자에게 슬롯 생성을 묻는다.
    """

    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"worktree pool exhausted for repo {repo!r} — needs `git worktree add`")


class ReleaseRefused(Exception):
    """dirty worktree 를 require_clean=True 로 release 하려 함 (수동 정리 또는 자동경로 stash 필요)."""

    def __init__(self, slot: str):
        self.slot = slot
        super().__init__(f"refusing to release dirty slot {slot!r} (require_clean=True)")


class RemoveRefused(Exception):
    """dirty 또는 활성(leased/creating) 슬롯을 force=False 로 remove_slot 하려 함 (작업 유실·사용중 슬롯 보호).

    `release` 의 dirty 거부 철학과 동형이되, remove 는 슬롯을 *통째로* 없애므로 활성 리스
    (다른 세션 사용 중·in-flight 생성)까지 거부한다 — force=True 로만 무시(dirty 는 stash
    보존 후 강제 제거). 정석 흐름은 `release`(→idle) 후 `remove`(idle 슬롯). `reason` ∈
    {"dirty", "active-lease"}·`state` = active-lease 시 슬롯 상태(leased/creating·진단용).
    """

    def __init__(self, slot: str, reason: str, *, state: "str | None" = None):
        self.slot = slot
        self.reason = reason      # "dirty" | "active-lease"
        self.state = state        # active-lease 시 슬롯 state (leased/creating)
        detail = f" (state={state})" if state else ""
        super().__init__(
            f"refusing to remove slot {slot!r} — {reason}{detail} (force=True 로 무시)"
        )


class TaskActiveElsewhere(Exception):
    """같은 task 를 살아있는 다른 세션이 열고 있다 — 동시 세션 거부.

    task 활성 pid 생존검사(부트스트랩 진입 시): 기록된 pid 가 **살아있고 내 pid 와 다르면** 이
    예외로 거부한다("다른 창에서 열려 있음"). pid 가 죽었으면(crash) 조용히 회수 후 진입(정상
    재개) — `reclaim_stale` 의 pid-생존 판정(`_pid_alive`)과 **동형 primitive**(신설 개념 0).
    의도적 2창 동시 열람은 막힌다(드묾·사고 재개방보다 낫다).
    """

    def __init__(self, name: str, pid: int):
        self.name = name
        self.pid = pid
        super().__init__(
            f"task {name!r} 이(가) 다른 살아있는 세션(pid {pid})에서 열려 있습니다"
        )


class InvalidTaskName(Exception):
    """task 명이 안전한 단일 path 컴포넌트가 아니거나 예약 패턴이다 — fail-loud (must-fix).

    `task_dir(name)=TASKS_DIR/name` 이 무검증이면 `--task ../../evil`·`/tmp/x`·`a/b`·빈 문자열이
    작업트리 밖/임의 경로에 디렉토리를 만들고 장부를 오염시킨다(reviewer 실측). 검증은 **엔진층
    (bind_task 진입점)**에 둔다 — CLI 검증만으론 bind_task 직접 소비가 우회된다.
    거부: 빈/공백·path separator(`/`·`\\`)·선행 `.`(숨김/상대 traversal)·단일 컴포넌트 아님·(등록
    repo 넘기면) `<repo>_<N>` 슬롯 세션 예약. `reason` = 위반 사유(진단용).
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"부적합 task 명 {name!r} — {reason}")


class NotTaskOwner(Exception):
    """release `--task <이름>` 이 그 task 명의가 아닌 슬롯을 반납하려 함 — 소유검사 거부.

    슬롯↔task 연결 = lease.session == task 이름 (alloc `--task` 가 session=task 로 리스).
    `--task` 를 준 release 는 이 소유검사를 통과해야 반납한다 — 다른 task/세션이 쓰는 슬롯을
    실수로 idle 화(작업 뺏기)하지 않게. `holder` = 실제 그 슬롯을 leased 중인 session(진단용·
    빈 문자열이면 idle/미점유). slot-only 반납(`--task` 미지정·백스톱)은 이 검사를 안 탄다.
    """

    def __init__(self, slot: str, task: str, holder: str):
        self.slot = slot
        self.task = task
        self.holder = holder
        held = f"session {holder!r}" if holder else "미점유(idle)"
        super().__init__(
            f"슬롯 {slot!r} 은 task {task!r} 소유가 아님 — {held} 이(가) 보유 중"
        )


class CheckoutFailed(Exception):
    """슬롯 worktree 의 branch checkout 실패.

    fail-soft 로 무시하면 리스 장부의 state/session 이 실제 worktree 상태와 어긋난다
    (부분 leased 전이). alloc 은 checkout 성공 시에만 장부를 갱신하고, 실패하면 이를
    raise 해 기존 리스 상태를 보존한다(부분 갱신 차단). 브랜치 자체는 더는 장부에
    저장하지 않는다(git=진실) — checkout 은 git HEAD 를 바꾼다.
    """

    def __init__(self, slot: str, branch: str, output: str):
        self.slot = slot
        self.branch = branch
        self.output = output
        super().__init__(
            f"git checkout {branch!r} failed for slot {slot!r}: {output!r}"
        )


class BareRepoMissing(RuntimeError):
    """worktree 의 공유 .git 원(`.repos/<repo>.git` bare)이 없다.

    `.repos/<repo>.git` 가 worktree 슬롯의 공유 .git 원(canonical).
    이 bare mirror 가 없으면 `git worktree add` 의 base 가 없다 — `pm-config repo add <repo>` 가
    bare clone 으로 mirror 를 (재)생성해야 한다. **repo 가 areas.md 에 이미
    등록됐으면**(하나의 채택 폴더를 여러 사람이 clone 한 2번째 사용자·`.repos/` 는 gitignore·
    per-clone 이라 공유 안 됨) `pm-config repo add <repo>` 를 **`--git` 없이** 실행하면 areas
    등록 URL 로 mirror 를 hydrate 한다. 침묵 폴백으로 multi-PM 루트 자신의 worktree 를
    만들면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해 토폴로지가 깨진다
    fail-soft 규율) → 명시 raise 로 선행 명령을 안내한다.

    **`RuntimeError` 서브클래스**인 이유: 파사드 `pm_config.cmd_worktree_add` 가
    `create_slot` 의 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로 surface 한다
    베이스를 `Exception` 으로 두면 그 가드를 빠져나가 traceback 이 노출된다
    (cross-module 규격).
    """

    def __init__(self, repo: str, bare_path: "Path", *, broken: bool = False):
        self.repo = repo
        self.bare_path = bare_path
        # broken=True = 경로는 있으나 유효 bare 가 아님(부분/깨진 bare) — 부재와 구별해
        # exists-but-broken 진단을 부재 케이스 수준으로 안내한다. broken=False = 종전 경로부재.
        self.broken = broken
        if broken:
            super().__init__(
                f"bare mirror for {repo!r} at {str(bare_path)!r} exists but is not a valid bare "
                f"git repo — 부분/깨진 bare (중단된 `git clone --bare` 잔존 가능성·하네스 타임아웃/"
                f"Ctrl-C). `.repos/{repo}.git` 경로는 있으나 `git worktree add` 의 base 로 "
                f"못 써 나중 날 git 에러로 죽는다. 자동 삭제는 하지 않는다(사용자 데이터 오판 위험·"
                f"삭제는 사용자 위임) — `.repos/{repo}.git` 를 수동 삭제 후 "
                f"`pm-config repo add {repo}`(--git 불요·areas 등록 URL 로 재hydrate·미등록이면 "
                f"`--git <url>`)로 재생성하라 "
            )
        else:
            super().__init__(
                f"bare mirror for {repo!r} not found at {str(bare_path)!r} — "
                f"`.repos/{repo}.git`(worktree 공유 .git 원)가 없다. areas.md 에 이미 등록됐으면 "
                f"(multi-user: `.repos/` 는 gitignore·per-clone 이라 공유 안 됨) "
                f"`pm-config repo add {repo}`(--git 불요)가 areas 등록 URL 로 mirror 를 hydrate 한다; "
                f"미등록 신규 repo 면 `pm-config repo add {repo} --git <url>` "
            )


class SlotBranchExists(RuntimeError):
    """create_slot 이 파려는 슬롯 전용 브랜치 `<repo>_<N>` 가 이미 존재한다 — 미머지-보존 브랜치 충돌.

    `remove_slot` 가 **미머지 전용 브랜치**(`<repo>_<N>`)를 보존(작업 유실 방지)한 뒤, 같은 번호
    슬롯을 branch-무지정 경로로 재생성하면 `git worktree add` 가 `fatal: a branch named '<repo>_<N>'
    already exists`(rc≠0)로 죽는다. 두 경로 모두 슬롯 전용 브랜치 `<repo>_<N>` 를 판다 — base-경로는
    명시 `--no-track -b <repo>_<N> <path> origin/<base>`, else-경로(base·branch 둘 다 미지정)는 git 이
    슬롯 path basename(=`<repo>_<N>`)으로 브랜치를 자동 생성한다. 슬롯번호는 ledger∪git-worktree
    병합으로 회피하지만, 그 병합은 **worktree 없이 잔존하는 브랜치**(보존 브랜치는 worktree 를
    안 가짐·브랜치 축은 슬롯번호 축과 독립)를 못 본다.

    옛 동작은 이 실패를 create_slot 의 already-exists 진단이 "worktree 경로 이미 등록(orphan)"으로
    **오귀인**했다(reviewer 실측 — orphan 정리 안내를 냈지만 지울 orphan worktree 는 없다).
    이 예외가 그 오귀인을 정정한다: **정확한 원인(브랜치 잔존) + 두 갈래 선택**(브랜치 정리 후 새 슬롯
    재생성 / 그 브랜치를 checkout 해 재개)을 fail-loud 로 준다(결정 (b)·데이터 유실 없음 — 현재도 loud 실패였다).

    **왜 (a) 브랜치 재사용-체크아웃이 아니라 (b) fail-loud 인가**: base-경로는 슬롯을 *`base`(origin/
    <base> 최신)에서* 시작하도록 요청한 것이다. 보존 브랜치는 정의상 `base` 에 없는 커밋을 가져
    미머지된 것이라(그래서 remove_slot 가 보존했다) 그 브랜치를 *base-경로에서* 재사용하면 슬롯이
    요청한 base 가 아닌 옛 미머지 작업 위에서 **조용히** 시작한다(base ≠ 브랜치 HEAD·silent 시맨틱
    어긋남). 사용자가 옛 작업 재개를 모른 채 진행할 위험이라 — 재개는 **명시 의사로만**(그 브랜치를
    슬롯에 checkout) 열어 둔다. 이 코드베이스의 fail-loud 규율(`BareRepoMissing`·`RemoveRefused`)과 정합.

    ✅ **재개 경로는 둘 다 리셋 없이 안전**: 그 브랜치를 checkout 해 미머지 작업을
    이어가려면 (1) 수동 `git worktree add <path> <repo>_<N>` 또는 (2) `create_slot(branch=<repo>_<N>)`
    — 둘 다 **기존 브랜치를 그 tip 에서 checkout**(리셋 없음)한다. create_slot 의 branch-경로가 옛날엔
    `-B`(create-or-reset)라 기존 브랜치를 리셋해 보존 커밋을 잃었으나 이 존재
    브랜치 checkout 분기로 그 데이터-유실 클래스를 API 에서 닫았다.

    **`RuntimeError` 서브클래스**인 이유: `BareRepoMissing` 과 동형 — 파사드 `pm_config.
    cmd_worktree_add` 가 `create_slot` 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로
    surface 한다. `base` = 요청 base(없으면 None·else-경로).
    """

    def __init__(self, slot: str, branch: str, base: "str | None"):
        self.slot = slot
        self.branch = branch
        self.base = base
        base_desc = f"base {base!r} 에서 " if base is not None else ""
        super().__init__(
            f"슬롯 전용 브랜치 {branch!r} 가 이미 존재한다 — {base_desc}슬롯 {slot!r} 을 재생성할 수 "
            f"없다(`git worktree add` 가 'a branch named {branch!r} already exists' 로 죽는다). 미머지-"
            f"보존 브랜치(remove_slot 이 작업 유실 방지로 보존) 잔존 가능성 — 다음 중 택1: (1) 그 "
            f"브랜치를 정리(`git branch -d {branch}` 머지 완료 시·미머지면 `git branch -D {branch}` 로 "
            f"폐기) 후 새 슬롯 재생성, 또는 (2) 그 브랜치의 미머지 작업을 그 브랜치로 **checkout 해 재개** "
            f"— 수동 `git worktree add {slot} {branch}` 또는 `create_slot(branch={branch})`(둘 다 리셋 "
            f"없이 보존 커밋 tip 에서 checkout). (데이터 유실 없음)."
        )


# Lease 직렬화 키 분류 (additive 스키마 클래스 폐쇄). from_dict 는 canonical 을 각
# 필드로 소비하고, 그 외 미지 키는 `extra` 로 보존해 to_dict 가 재방출한다(구·신 엔진 왕복 무손실).
#   - CANONICAL = 이 엔진 버전이 아는 1급 필드(각자 self.* 로 소비·extra 아님).
#   - DROP = legacy 최상위 `branch` — extra 로도 보존하지
#     않는다. 표시는 git=진실(`current_branch`)이라 장부에 branch 를 되살리면 드리프트 원천이
#     재생긴다(테스트 `test_from_dict_ignores_legacy_branch_key` 가 이 무시를 못박음). git 필드
#     안의 `branch` 서브키(작업 브랜치 스냅)와는 다른 축 — 그건 git blob 의 일부로 보존된다.
_LEASE_CANONICAL_KEYS = frozenset(
    {"slot", "repo", "session", "pid", "started", "state", "test_cmd", "git", "role", "bound"}
)
_LEASE_DROP_KEYS = frozenset({"branch"})


class Lease:
    """리스 장부 한 엔트리.

    슬롯=브랜치-무관 컨테이너·session/pid=점유 주체·state=leased|idle|creating. `creating` 은
    create_slot 의 provisional 마커(worktree add 전 선기록·확정 시 leased·중단 시 흔적).
    **브랜치 *표시*는 권위 필드가 아니다** — git 이 단일 진실이라 장부에
    저장하지 않고 `current_branch(slot)` 로 슬롯 worktree 의 live HEAD 에서 읽는다(드리프트 불가능).

    **`git` 필드 (additive·엔진 전용 write·md 아님)**: 슬롯 git 상태를 *기대*
    (drift 감지 기준) 축으로 기계 기록한다 — `{base:{branch,commit}, branch, head, submodules:
    [{path,pin}], recorded_at}`. 뒤집지 않는다: 표시는 여전히 live
    조회(`current_branch`)·기록은 별개의 *기대* 축(submodule pin/drift 모델을 본체로 대칭 확장).
    write 시점 = 부트스트랩 bind/alloc·핸드오프·create(release 시 정리)·compare 시점 = 0단계
    (`compare_slot_git`). 미기록(구 슬롯) = drift 감지 비활성. raw dict 로
    들고 있어 미지 서브키까지 왕복 보존한다(None=미기록·to_dict 는 None 이면 키 자체를 뺀다).

    **`extra` (미지 키 보존)**: 이 엔진 버전이 모르는 최상위 키(`task`[T-035x] 등 향후
    additive 키)를 `from_dict` 가 `extra` dict 로 보존하고 `to_dict` 가 재방출한다. 장부 지속은
    `_read_ledger`→`_write_ledger`(from_dict→to_dict) 왕복 하나로 수렴하는데, **신규 키를 모르는
    엔진(adopter#0 import 사본 lag 로 버전 skew 실재)이 아무 op 라도 하면 그 read-modify-write
    왕복에서 *모든 슬롯*의 신규 키가 소실**된다 — base/head 기록이 조용히 날아가면 drift 감지가
    가짜 기준 위에서 돌아 무의미해진다([[robustness-value-connections-before-ship]] silent
    degrade). extra 보존이 그 왕복을 무손실로 만들어 additive 전략 전체를 안전하게 닫는다.

    (dataclass 미사용 — 엔진 도구는 `spec_from_file_location` 으로 로드되는데 sys.modules
    미등록 시 dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, slot: str, repo: str, session: str,
                 pid: int, started: str, state: str, test_cmd: str | None = None,
                 git: "dict | None" = None, role: str = "work",
                 bound: bool = False, extra: "dict | None" = None):
        self.slot = slot          # "work/<repo>_<N>" (브랜치 무관)
        self.repo = repo          # repo 이름 (per-repo 네임스페이스)
        self.session = session    # 점유 세션 식별자
        self.pid = pid            # 점유 프로세스 pid (stale 회수 판정)
        self.started = started    # 리스 시작 시각 (UTC ISO)
        self.state = state        # "leased" | "idle" | "creating"(provisional)
        self.test_cmd = test_cmd  # 슬롯 바인딩 회귀/빌드명령 (None=미지정)
        self.git = git            # 슬롯 git 스냅 dict(base/branch/head/submodules/recorded_at)·None=미기록
        # 슬롯 role (additive): "work"(기본·배타 대여 작업 슬롯) | "readonly"
        # (research 전용 공유 슬롯·detached·session/pid 없음·alloc/release/reclaim 대상 제외). role 이
        # 0단계 carve-out(pm_bootstrap `_phase0_is_readonly`) 소유검사 예외(identity_args)·엔진
        # mutation 거부(set-base/rebase/dev/sync)의 canonical 판별 축이다.
        self.role = role
        # bind-origin 마커 (additive 동형 조건방출): True = 사람 발의 `bind_slot`(직접 슬롯
        # 바인딩)이 점유한 슬롯. `bind_slot` 이 적는 pid 는 *ephemeral bootstrap subprocess* pid 라
        # 즉사하는데, 사람 경로는 명시 `release` 로만 반납한다(pid 는 정보용) — 그런데 타 세션 `alloc`
        # (진입 시 `reclaim_stale` 호출)이 `state==leased && pid 죽음` 으로 이 bind lease 를 stale 오판해
        # 회수(idle 화·session 비움)하면 사람 정체성이 유실된다(타 창 세션 bind slot 2 가
        # alloc 에 회수됨). `reclaim_stale` 과 alloc 의 branch/resume 재부착 경로가 `bound` lease 를 제외해
        # 닫는다("reclaim 제외 마커"). pool 경로(alloc idle-리스·
        # create_slot·release/force_release teardown)는 이 마커를 기록하지 않거나 해제해 현행 pid-회수
        # 거동을 유지한다. 구 장부(키 부재)=False 동치·마이그레이션 0.
        #   **task-명의 lease 의 reclaim 보호는 이 마커가 아니라 tasks 장부 조인이 담당한다**:
        #   `reclaim_stale`/재부착이 `session ∈ tasks 장부` 인 lease 를 제외한다 — task 소유의 단일
        #   진실 = tasks 장부라, 구버전 alloc 이 만든 bound-부재 task lease 도 마이그레이션 0 으로
        #   자동 보호된다(bound 는 사람-bind 축 그대로). 상세는 `reclaim_stale`/`alloc` 참조.
        self.bound = bound
        # 미지 최상위 키 보존(additive 스키마 클래스 폐쇄). mutable 기본 회피(None 센티넬).
        self.extra = dict(extra) if extra else {}

    def __repr__(self) -> str:
        return (f"Lease(slot={self.slot!r}, repo={self.repo!r}, "
                f"session={self.session!r}, pid={self.pid!r}, state={self.state!r}, "
                f"test_cmd={self.test_cmd!r}, git={self.git!r}, role={self.role!r}, "
                f"bound={self.bound!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Lease):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict:
        d = {
            "slot": self.slot,
            "repo": self.repo,
            "session": self.session,
            "pid": self.pid,
            "started": self.started,
            "state": self.state,
            "test_cmd": self.test_cmd,
        }
        # git 은 *있을 때만* 방출한다 — 구 장부(git 필드 부재)를 로드·재기록해도 `git: null` 을
        # 덧붙이지 않아 왕복이 무손실(하위호환·DoD). test_cmd 는 항상 방출.
        if self.git is not None:
            d["git"] = self.git
        # role 은 *기본("work")이 아닐 때만* 방출한다 (git 필드 조건방출과 동형·하위호환).
        # 구 장부(role 부재=work 슬롯)를 로드·재기록해도 `role: "work"` 를 덧붙이지 않아 왕복이
        # byte-무손실이다(from_dict 가 부재 시 "work" 로 read). readonly 슬롯만 role 을 장부에 남긴다.
        if self.role != "work":
            d["role"] = self.role
        # bound 는 *True 일 때만* 방출한다 (git/role 조건방출과 동형·하위호환). 구 장부(bound
        # 부재=False·pool 슬롯)를 로드·재기록해도 `bound: false` 를 덧붙이지 않아 왕복이 byte-무손실
        # 이다(from_dict 가 부재 시 False 로 read). 사람 bind 슬롯만 bound 를 장부에 남긴다.
        if self.bound:
            d["bound"] = True
        # 미지 키 재방출(구·신 엔진 왕복 무손실). extra 엔 canonical/legacy-branch 가 없다
        # (from_dict 가 배제) — canonical 을 덮을 위험 없음.
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        # 하위호환 read: test_cmd 부재(구 장부)는 None·git 부재는 None. 구 장부의 legacy 최상위
        # `branch` 키는 관용적으로 *무시*한다(branch 는 권위 필드가
        # 아니다·표시는 git 에서만 온다). canonical/legacy-branch 를 뺀 나머지 미지 키는 `extra`
        # 로 보존해 to_dict 가 재방출한다(구·신 엔진 왕복 무손실·additive 스키마 클래스 폐쇄).
        extra = {
            k: v for k, v in d.items()
            if k not in _LEASE_CANONICAL_KEYS and k not in _LEASE_DROP_KEYS
        }
        return cls(
            slot=d["slot"],
            repo=d["repo"],
            session=d.get("session", ""),
            pid=int(d.get("pid", 0)),
            started=d.get("started", ""),
            state=d.get("state", "leased"),
            test_cmd=d.get("test_cmd"),
            git=d.get("git"),
            role=d.get("role", "work"),   # 하위호환 read: 구 장부(role 부재) = "work"(작업 슬롯).
            bound=bool(d.get("bound", False)),  # 하위호환 read: 구 장부(bound 부재) = False(pool 슬롯).
            extra=extra,
        )


# Task 직렬화 키 분류 (Lease 동형 additive 스키마). canonical 을 각 필드로 소비하고 그
# 외 미지 키는 `extra` 로 보존해 왕복 무손실(구·신 엔진 skew·향후 additive task 서브키 대비).
_TASK_CANONICAL_KEYS = frozenset({"name", "prefix", "pid", "started"})


class Task:
    """리스 장부 top-level `tasks` 컬렉션 한 엔트리 — 작업 단위 정체성.

    task = 슬롯과 **직교**한 작업스트림 정체성(슬롯 0개로도 존재 가능). 슬롯-키 lease 행과 별개의
    top-level 컬렉션에 산다(같은 파일·같은 `_lease_lock`/atomic replace 직렬화). 필드:
      - `name`   — task 이름(사람이 정하는 자유 포맷·`<등록 repo>_<N>` 예약 제외·유일성=사람 안).
      - `prefix` — 이 task 세션의 board prefix(기본 None=없음·변경은 `task prefix`).
      - `pid`    — 현재 열려 있는 세션 pid(동시 세션 거부·`_pid_alive` 생존검사). 0=미점유.
      - `started`— task 레코드 생성 시각(UTC ISO).

    저장 위치 = 리스 장부 파일 top-level `tasks`(출처 1개·저장소
    신설 불요·pm_update 활성 pid 검사 단일 파일 스캔). `.local/tasks/<name>/` 는 **서술
    (pm_state.md)만** — 기계 상태는 장부. 미지 최상위 task 키는 `extra` 로 왕복 보존
    (Lease 동형·additive 스키마 클래스 폐쇄). (dataclass 미사용 — Lease 와 같은 forward-ref 회피.)
    """

    def __init__(self, name: str, prefix: "str | None" = None,
                 pid: int = 0, started: str = "", extra: "dict | None" = None):
        self.name = name
        self.prefix = prefix      # board prefix (None=없음)
        self.pid = pid            # 현재 열려있는 세션 pid (동시세션 생존검사·0=미점유)
        self.started = started    # 레코드 생성 시각 (UTC ISO)
        self.extra = dict(extra) if extra else {}

    def __repr__(self) -> str:
        return (f"Task(name={self.name!r}, prefix={self.prefix!r}, "
                f"pid={self.pid!r}, started={self.started!r})")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Task):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def to_dict(self) -> dict:
        d = {
            "name": self.name,
            "prefix": self.prefix,
            "pid": self.pid,
            "started": self.started,
        }
        d.update(self.extra)      # 미지 키 재방출(왕복 무손실·canonical 을 덮을 위험 없음)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        extra = {k: v for k, v in d.items() if k not in _TASK_CANONICAL_KEYS}
        return cls(
            name=d["name"],
            prefix=d.get("prefix"),
            pid=int(d.get("pid", 0)),
            started=d.get("started", ""),
            extra=extra,
        )


class SubmoduleStatus:
    """슬롯 worktree 한 submodule 의 상태 — 역할(live HEAD) + pin 비교 판정.

    (`_resync_submodules_selective`)의 *역할 판별*을 표시층으로 재사용한다 — 역할은
    별도 장부 없이 submodule 의 live git HEAD 로 정한다(무스키마):

      - `kind="dev-ahead"` — **on-branch**(=dev 역할·`symbolic-ref -q HEAD` rc0). 사용자가 그
        submodule 에서 브랜치를 파 작업 중 → **정보**(경고 아님). detached pin 으로 낚아채지
        않는 selective resync 의 보호 대상(전역 recurse 가 파괴하던 크럭스 A).
      - `kind="drift"` — **detached & pin ≠ working**(`git submodule status` flag `+`/`U`) →
        **경고**. superproject pin 과 어긋난 detached. 재-alloc 시 재동기하나 dirty 면
        잔존한다(그래서 dirty 도 실어 *왜* 안 풀렸는지 surface).
      - `kind="pinned"` — **detached & pin == working**(flag 공백) → 정상(pinned).
      - `kind="uninitialized"` — submodule 미초기화(flag `-`) → 경고(슬롯 init 비정상).

    `warning` = kind in {"drift", "uninitialized"}(dev-ahead/pinned 은 경고 아님 — 이 구별이
    `dirty` = 워킹트리 미커밋 변경(`_submodule_dirty` 재사용).
    (dataclass 미사용 — Lease 와 같은 이유: `spec_from_file_location` 로드 시 dataclass
    forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, path: str, kind: str, *, warning: bool, dirty: bool):
        self.path = path
        self.kind = kind
        self.warning = warning
        self.dirty = dirty

    def __repr__(self) -> str:
        return (f"SubmoduleStatus(path={self.path!r}, kind={self.kind!r}, "
                f"warning={self.warning!r}, dirty={self.dirty!r})")


class SlotStatus:
    """슬롯 worktree 한 줄 상태 — branch + upstream + submodule 역할.

    부트스트랩이 현재 슬롯의 상태를 1회 surface 하는 데 쓰는 구조(표시는 pm_bootstrap 이 담당).

      - `branch` — `current_branch(slot)` live(None=detached/조회불가).
      - `upstream` — `@{upstream}` 해소명(예 `origin/a5`·None=미해소).
      - `upstream_ok` — `@{upstream}` 해소 여부. 미해소=경고(슬롯 tracking 이
        설정돼야 정상 — 미해소면 origin-freshness 판정 불가).
      - `submodules` — 각 submodule `SubmoduleStatus` 리스트. **빈 리스트 = submodule 없는
        슬롯**(부트스트랩이 submodule 줄을 생략).

    (dataclass 미사용 — Lease/SubmoduleStatus 와 동일 이유.)
    """

    def __init__(self, slot: str, *, branch: str | None, upstream: str | None,
                 upstream_ok: bool, submodules: "list[SubmoduleStatus]"):
        self.slot = slot
        self.branch = branch
        self.upstream = upstream
        self.upstream_ok = upstream_ok
        self.submodules = submodules

    def __repr__(self) -> str:
        return (f"SlotStatus(slot={self.slot!r}, branch={self.branch!r}, "
                f"upstream={self.upstream!r}, upstream_ok={self.upstream_ok!r}, "
                f"submodules={self.submodules!r})")


class GitWorktree:
    """`git worktree list --porcelain` 한 엔트리 — 실 git worktree 의 경로/브랜치/상태.

    리스 장부(Lease)와 대조할 *실-git* 소스(`list_git_worktrees`). `slot` = 경로가 WORK_DIR
    (=REPO/work) 바로 아래의 단일 디렉토리면 슬롯 식별자(`work/<repo>_<N>`), 아니면 None
    (bare 원·multi-PM 루트·외부 worktree). `bare` = bare 원 엔트리(슬롯 아님·reconcile/슬롯번호
    에서 제외). (dataclass 미사용 — Lease/SlotStatus 와 동일 이유: `spec_from_file_location`
    로드 시 dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, path: str, slot: "str | None", branch: "str | None",
                 detached: bool, bare: bool):
        self.path = path          # `git worktree list` 가 준 절대경로
        self.slot = slot          # "work/<repo>_<N>" (WORK_DIR 하위 단일 컴포넌트) 또는 None
        self.branch = branch      # 체크아웃 브랜치명 (detached/bare 면 None)
        self.detached = detached  # detached HEAD 여부
        self.bare = bare          # bare 원 엔트리 여부(슬롯 아님)

    def __repr__(self) -> str:
        return (f"GitWorktree(path={self.path!r}, slot={self.slot!r}, "
                f"branch={self.branch!r}, detached={self.detached!r}, bare={self.bare!r})")


class ReconcileResult:
    """리스 장부 × 실 git worktree 정합 결과 — orphan/stale/incomplete drift (조회 전용).

    - **orphans** = git worktree(슬롯 경로·non-bare)인데 장부에 없음(`GitWorktree` 리스트).
      중단된 create/수동 add 잔존(audit #2) — 다음 create_slot 번호 충돌(#4)·status blind(#3).
    - **stale** = 장부 확정 리스(leased/idle)인데 대응 git worktree 없음(`Lease` 리스트).
      worktree dir 삭제/prune 됨(audit #3).
    - **incomplete** = provisional("creating") 리스(`Lease` 리스트). worktree add 후 확정 전에
      중단된 create 의 흔적(SIGKILL 도 커버) — 정리 대상.

    (dataclass 미사용 — 위 클래스들과 동일 이유.)
    """

    def __init__(self, orphans: "list[GitWorktree]", stale: "list[Lease]",
                 incomplete: "list[Lease]"):
        self.orphans = orphans
        self.stale = stale
        self.incomplete = incomplete

    def __repr__(self) -> str:
        return (f"ReconcileResult(orphans={self.orphans!r}, stale={self.stale!r}, "
                f"incomplete={self.incomplete!r})")


class RemoveResult:
    """`remove_slot` 결과 — 무엇을 지웠고 슬롯 전용 브랜치를 어떻게 처리했는지 (보고용).

    - `slot` — 제거한 슬롯 식별자(`work/<repo>_<N>`).
    - `branch` — 제거 당시 슬롯 worktree 의 live 브랜치(`current_branch`·None=detached/조회불가).
    - `branch_action` — 슬롯 전용 브랜치(`<repo>_<N>`·`git worktree add` 가 슬롯명으로 판 브랜치)
      처리 결과:
        - "deleted" — 전용 브랜치가 main(bare HEAD)에 머지돼 삭제(`git branch -d` rc0).
        - "preserved-unmerged" — 전용 브랜치가 미머지라 보존(`git branch -d` rc≠0·작업 유실 방지).
        - "skipped-shared" — 슬롯이 전용 브랜치(`<repo>_<N>`)가 아닌 공유/다른 브랜치(main 등)를
          체크아웃 중이라 브랜치 삭제 자체를 스킵(공유 브랜치 보호).
        - "none" — detached/조회불가라 지울 브랜치 판별 불가.
    - `stashed` — force + dirty 라 제거 전 변경을 stash 로 보존 시도했는지(stash 성공 시에만 True·
      실패면 remove_slot 이 RuntimeError 로 중단하므로 여기 도달 안 함).
    - `forced_state` — `--force` 로 **활성 리스**(leased/creating·사용 중)를 override 하고 제거했을 때
      그 원래 state(예 "leased"·CLI 가 강제 회수 경고 surface). idle 슬롯이었으면 None(정상 제거·경고 불요).

    (dataclass 미사용 — Lease/ReconcileResult 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다. 평범한 클래스로 그 결합을 피한다.)
    """

    def __init__(self, slot: str, *, branch: "str | None", branch_action: str,
                 stashed: bool, forced_state: "str | None" = None):
        self.slot = slot
        self.branch = branch
        self.branch_action = branch_action
        self.stashed = stashed
        self.forced_state = forced_state   # force 로 override 한 활성 state(leased/creating)·None=idle 정상.

    def __repr__(self) -> str:
        return (f"RemoveResult(slot={self.slot!r}, branch={self.branch!r}, "
                f"branch_action={self.branch_action!r}, stashed={self.stashed!r}, "
                f"forced_state={self.forced_state!r})")


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _now_local() -> str:
    """벽시계(로컬 tz) ISO8601 — git 스냅 `recorded_at` 용 (`+09:00` 정합).

    리스 `started` 는 UTC(`_now_utc`)지만 `recorded_at` 은 **로컬 벽시계**로 둔다 — 사람이
    "여기 두고 간다"고 인지하는 스냅 시각이라 벽시계가 자연스럽고, `+09:00`
    (로컬 offset)로 예시한다. `.astimezone()` 로 tz-aware(offset 포함)라 UTC 와 무손실 상호변환
    가능 — 둘 다 명시 offset ISO8601 이라 모호성 없음(표시 tz 만 다름·비교엔 무관)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ── 리스 장부 파일락 (경로 규약만 소유·플랫폼 분기는 공용 `file_lock` seam) ──────────
# 장부 read-modify-write 를 직렬화한다. 프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock
# 없음). 플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 seam 이 소유하고
# 이 도구는 *어느 파일에 거는지*(`.local/worktree-leases.lock`)만 정한다.
# board 를 top-level import 하지 않는다는 원칙은 그대로다 — seam 은 형제를 로드하지 않는
# leaf 라 그 원칙과 무관하게 공유할 수 있다.


def _load_file_lock():
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    `_load_identity_args` 동형 — 경로-앵커 로더다. **import 시점** 바인딩으로 둔다(`file_lock`
    전역·identity_args 와 같은 블록·board.py 동형): 장부의 *모든* op 가 이 락을 지나므로, 락을
    잡을 때마다 형제를 로드하면 worktree_pool 을 fail-soft 로 소비하는 호출층(pm_bootstrap·
    pm_handoff·pm_config)이 사본 skew 를 조용히 삼키는 경로가 장부 op 수만큼 늘어난다 —
    import 경계 단일 fail-loud 로 그 확산을 막는다.
    """
    lock_path = Path(__file__).resolve().with_name("file_lock.py")
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev,
    )


def _load_relay():
    """공용 PID 생존 조회 seam을 소유한 ``pm_relay.py``를 형제 경로에서 로드한다."""
    relay_path = Path(__file__).resolve().with_name("pm_relay.py")
    _require_engine_sibling(relay_path, "pm_relay.py")
    return _load_module_from_path(
        relay_path, "pm_relay.py", verifier=_verify_engine_rev, cache=True,
    )


@contextlib.contextmanager
def _lease_lock() -> Iterator[None]:
    """리스 장부 write 를 직렬화하는 OS 파일락 컨텍스트매니저.

    `.project_manager/.local/worktree-leases.lock` 에 배타 OS 락. 프로세스가 죽으면 OS 가
    자동 해제(stale-lock 없음). **재진입 금지** — 같은 프로세스가 이 컨텍스트를 중첩하면
    안 된다(flock 재진입 동작은 OS 별로 다름). 장부의 모든 read-modify-write 가 이 한
    구간 안에서 일어난다.
    """
    with file_lock.exclusive_file_lock(LEASES_LOCK):
        yield


# ── 장부 읽기/쓰기 (락 보유 전제) ────────────────────────────────────────────


def _read_ledger_raw() -> dict:
    """장부 파일의 **최상위 dict 원본**을 읽는다. 부재/손상/비-dict → 빈 dict(fail-soft).
    **_lease_lock 보유 전제**.

    `leases`(슬롯 리스)·`tasks`·향후 additive 최상위 컬렉션이 한 파일에 공존하므로,
    특정 컬렉션만 갱신할 때(`_write_ledger`/`_write_tasks`) 나머지 최상위 키를 무손실 보존하는
    read-modify-write 의 read 측이다. Lease-내부 미지 키 보존(`extra`)의 **최상위판** —
    구 엔진(신규 최상위 키를 모르는 import 사본 lag)이 아무 op 해도 형제 컬렉션이 안 날아가게
    한다([[robustness-value-connections-before-ship]] silent degrade 방지)."""
    if not LEASES_FILE.exists():
        return {}
    try:
        data = json.loads(file_lock.read_text_shared(LEASES_FILE, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_ledger_raw_strict() -> dict:
    """장부 최상위 dict를 읽되 **부재만** 빈 dict로 본다. **_lease_lock 보유 전제**.

    bootstrap/handoff처럼 ``0슬롯``과 ``장부를 읽을 수 없음``을 구분해야 하는 안전 게이트용
    파서다. JSON 손상·읽기 오류는 원 예외를 전파하고, JSON 최상위가 dict가 아니면
    ``ValueError``를 낸다. 기존 ``_read_ledger_raw``과 그 fail-soft 소비자는 그대로 둔다.
    """
    if not LEASES_FILE.exists():
        return {}
    data = json.loads(file_lock.read_text_shared(LEASES_FILE, encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("worktree lease 장부 최상위 값이 object가 아님")
    return data


def _write_ledger_raw(data: dict) -> None:
    """최상위 dict 를 atomic replace 로 쓴다 (tmp→원자 교체·부분쓰기 방지). **_lease_lock 보유 전제**."""
    LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = LEASES_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8", newline="\n")
    file_lock.atomic_replace(tmp, LEASES_FILE)


def _read_ledger() -> list[Lease]:
    """리스 장부의 `leases` 컬렉션을 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    rows = _read_ledger_raw().get("leases", [])
    if not isinstance(rows, list):
        return []
    return [Lease.from_dict(d) for d in rows]


def _read_ledger_strict() -> list[Lease]:
    """리스 컬렉션 strict 조회 — 손상/읽기 오류/잘못된 컬렉션·행을 예외로 전파한다."""
    rows = _read_ledger_raw_strict().get("leases", [])
    if not isinstance(rows, list):
        raise ValueError("worktree lease 장부의 'leases' 값이 list가 아님")
    leases: list[Lease] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"worktree lease 장부의 leases[{index}] 값이 object가 아님")
        leases.append(Lease.from_dict(row))
    return leases


def _write_ledger(leases: list[Lease]) -> None:
    """`leases` 컬렉션만 교체해 장부를 atomic replace 로 쓴다. **_lease_lock 보유 전제**.

    **형제 최상위 키 보존**(top-level round-trip): 현 파일 원본을 읽어(`_read_ledger_raw`)
    `leases` 키만 새 값으로 덮고 나머지(`tasks`·미지 additive 컬렉션)는 그대로 재방출한다 — 옛
    `{"leases": [...]}` 통짜 쓰기는 형제 컬렉션을 조용히 드롭했다(silent drop). read-modify-write
    가 같은 `_lease_lock` 안에서 직렬화되므로 read↔write 사이 파일 변동은 없다.
    """
    # write-capable 최종 경계도 strict 원본을 재확인한다. 호출부가 실수로 fail-soft read 결과를
    # 넘기거나 read 이후 장부가 손상돼도 빈 dict로 축약해 형제 컬렉션을 덮어쓰지 않는다.
    data = _read_ledger_raw_strict()
    data["leases"] = [l.to_dict() for l in leases]
    _write_ledger_raw(data)


# ── git DI seam ────────────────────────────────────────────────────────────


def _load_repo_owned_files():
    """공용 captured git runner seam을 rev 검증 후 로드한다."""
    path = Path(__file__).resolve().with_name("repo_owned_files.py")
    if not path.exists():
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
    )
    try:
        helper_path = Path(__file__).resolve().with_name("engine_rev.py")
        helper = _load_module_from_path(
            helper_path, "engine_rev.py", allow_unverified=True,
        )
        return helper.load_repo_owned_files(path, verifier=_verify_engine_rev)
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
        ) from exc


def _real_git_runner(cwd: Path) -> GitRunner:
    """실 git 을 `cwd` 컨텍스트로 호출하는 GitRunner 를 만든다 (pm_import._real_git_runner 선례).

    반환 callable: argv(list) → (returncode, stdout+stderr). git 바이너리 부재(shutil.which)
    면 (1, msg)·예외는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부에 위임). `git -C
    <cwd> <argv...>` 형태로 항상 그 work tree/repo 에 묶는다. 인코딩은 엔진 관례대로 UTF-8
    (한글 경로·메시지 안전).

    **stdout+stderr 결합 반환 (pm_config._real_clone_runner 정합)**: 옛 코드는
    `result.stdout` 만 돌려 stderr 를 버려 — TimeoutExpired/실패 시 out='' 가 돼 진단이
    불가능했다(`git submodule init failed: ''`). stderr 를 합쳐 에러를 가시화한다.
    ⚠️ `_is_dirty` 는 결합된 출력을 `_porcelain_status_lines`(porcelain 형식 라인만 추림·아래)로
    필터하므로 stderr 경고가 섞여도 dirty 오탐이 없다 — 이 결합은 진단용이고 dirty 판정은
    porcelain 라인만 보는 경로로 분리돼 있다.

    ⚠️ **captured 러너는 절대 무제한이 되지 않는다 (codex 게이트·설계 모순 fix)**: subprocess
    timeout 은 `GIT_TIMEOUT_SECONDS or _GIT_TIMEOUT_DEFAULT` — GIT_TIMEOUT_SECONDS 가
    None(PM_GIT_TIMEOUT=none)이어도 유한 fallback(1800s)으로 캡한다. captured 는 진행이 콘솔에
    안 보여(silent) 무제한이면 network stall(base 파생 `fetch origin`) 시 silent hang 하기 때문.
    무제한(None)은 진행이 콘솔에 보이는 worktree-add 인터랙티브 러너에만 허용한다.
    """
    repo_files = _load_repo_owned_files()
    return repo_files.real_git_runner(
        cwd,
        missing_binary_rc=1,
        # captured=항상 유한: None(무제한)이면 silent hang → 기본값으로 캡.
        timeout=GIT_TIMEOUT_SECONDS or _GIT_TIMEOUT_DEFAULT,
        output_mode="stdout_stderr",
        which=shutil.which,
        run=subprocess.run,
    )


def _real_git_runner_interactive(
    cwd: Path, *, timeout: "int | None" = SUBMODULE_TIMEOUT,
) -> GitRunner:
    """console-visible 인터랙티브 git runner — stdio 콘솔 상속·튜닝 가능한 timeout.

    `_real_git_runner` 와 달리 **capture 하지 않는다** — stdout/stderr/stdin 을 부모 콘솔에
    그대로 상속한다(`subprocess.run(..., capture_output 안 줌`). 그래서:
      - 대형 clone/checkout 의 진행상황이 화면에 실시간 표시된다(긴 침묵 대신).
      - git credential/auth 프롬프트가 작동한다(수동 콘솔 실행과 동일).
      - `timeout` 이 관대(또는 None=무제한)라 느린 op 이 짧은 고정값에 false-kill 되지 않는다.

    **두 호출부 (같은 패턴·다른 timeout)**:
      - submodule init — `timeout` 미지정 → 기본 `SUBMODULE_TIMEOUT`(3600s·env
        `PM_SUBMODULE_TIMEOUT`). 600s 초과 대형 clone 이 TimeoutExpired 로 죽던 블로커 해소.
      - worktree add — `timeout=GIT_TIMEOUT_SECONDS`(1800s·env `PM_GIT_TIMEOUT`).
        대형 repo 의 full checkout 이 옛 captured 120s 에 false-kill 되던 블로커 해소.

    반환 `(rc, "")` — 출력은 콘솔로 직접 갔으므로 캡처 문자열은 없다(rc 로만 성공/실패 판정).
    git 부재/예외는 `(1, str(exc))`(또는 부재 메시지) — `_real_git_runner` 와 같은 fail-soft.
    `create_slot` 의 submodule/worktree-add 단계가 `git_runner is None` 인 실경로에서만 이걸
    쓴다 — 주입된 git_runner(테스트 mock)가 있으면 그대로(DI seam 보존·인터랙티브 안 탐).

    ⚠️ 비-tty(CI/pytest)서도 안전: 테스트는 git_runner 를 주입하므로 이 실 인터랙티브
    경로를 타지 않는다. 이 함수 자체의 단위테스트는 짧은 비-네트워크 git 명령(stdin 블록
    없음)으로만 호출한다(submodule clone 은 실행하지 않음).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            # capture_output 미지정 = stdout/stderr/stdin 부모 콘솔 상속(인터랙티브).
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                timeout=timeout,
            )
            return result.returncode, ""
        except Exception as exc:  # noqa: BLE001 — fail-soft: 타임아웃/예외 메시지 surface.
            return 1, str(exc)

    return runner


def _porcelain_status_lines(out: str) -> list[str]:
    """`git status --porcelain` 출력에서 *실제 status 엔트리* 라인만 추린다.

    porcelain v1 엔트리 형식 = `XY <path>`(X·Y = 2글자 status code·세 번째가 공백). git
    경고(stderr·`warning: ...`)가 stdout 캡처에 섞여도 그 형식이 아니므로 걸러진다 —
    `_real_git_runner` 가 stdout+stderr 를 합치게 바뀐 뒤dirty 오탐을 막는 가드.
    빈 줄 무시. 형식이 맞는 라인만 dirty 신호로 본다(보수성은 호출부 rc 가드가 유지).
    """
    lines: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        # porcelain v1 엔트리: 2글자 status code + 공백 구분자 (예 " M file", "?? new").
        if len(line) >= 3 and line[2] == " ":
            lines.append(line)
    return lines


def _is_dirty(slot_path: Path, *, git_runner: GitRunner | None = None) -> bool:
    """슬롯 worktree 에 미커밋 변경(untracked 포함)이 있는지. git 오류 → 보수적으로 dirty.

    `git status --porcelain` 의 porcelain 엔트리 라인이 하나라도 있으면 dirty. git 호출
    실패(rc!=0)는 상태를 모르므로 **보수적으로 dirty 로 본다** — clean 으로 오판해 stash
    없이 날리는 것보다 안전.

    ⚠️ stderr 오탐 방어: `_real_git_runner` 가 stdout+stderr 를 합쳐 반환하게
    바뀌어, status 출력에 stderr 경고(`warning: ...`)가 섞일 수 있다. `out.strip()!=""`
    로 보면 그 경고만 있어도 dirty 오탐이 난다 → porcelain 엔트리 형식 라인만 보아
    경고에 안 흔들리게 한다(`_porcelain_status_lines`).
    """
    runner = git_runner or _real_git_runner(slot_path)
    rc, out = runner(["status", "--porcelain"])
    if rc != 0:
        return True
    return len(_porcelain_status_lines(out)) > 0


def _stash(slot_path: Path, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 의 dirty 변경을 stash 보존(untracked 포함). (rc, stdout) 반환."""
    runner = git_runner or _real_git_runner(slot_path)
    return runner(["stash", "push", "--include-untracked",
                   "-m", f"worktree_pool auto-stash {_now_utc()}"])


# `fetch origin` 실패 시 폴백 대상 ref 의 정체 — 경고 문구 단일 소스. remote-tracking ref
# (`origin/<branch>`)가 기준이면 폴백 대상은 로컬 브랜치가 아니라 **직전 fetch 시점에 멈춘
# remote-tracking ref** 다. 둘을 "로컬 …(동결 head)" 로 뭉뚱그리면 사용자가 로컬 브랜치를
# 갱신해 해소하려 하지만(그 ref 는 안 움직인다) 실제 해소는 fetch 복구뿐이다.
_REMOTE_TRACKING_PREFIX = "origin/"


def _frozen_fallback_label(ref: str) -> str:
    """fetch 실패 폴백 대상 표기 — 로컬 브랜치 / stale remote-tracking ref 를 구분한다."""
    if ref.startswith(_REMOTE_TRACKING_PREFIX):
        return f"stale remote-tracking `{ref}`(직전 fetch 시점 동결)"
    return f"로컬 `{ref}`(동결 head)"


# ── 슬롯 네이밍 ──────────────────────────────────────────────────────────────


def _slot_for(repo: str, n: int) -> str:
    """슬롯 식별자 `work/<repo>_<N>`."""
    return f"work/{repo}_{n}"


def slot_path(slot: str) -> Path:
    """슬롯 식별자(`work/<repo>_<N>`) → 절대 경로 (REPO 기준)."""
    return REPO / slot


def bare_repo_path(repo: str) -> Path:
    """repo 이름 → 그 repo 의 공유 .git 원 경로 `.repos/<repo>.git` (bare).

    worktree 슬롯이 add/remove 되는 git 컨텍스트. `pm_config.REPOS_DIR / f"{repo}.git"` 와
    같은 관례 — worktree 풀이 import 격리(board·pm_config 미import)라 자체 해소한다.
    """
    return REPOS_DIR / f"{repo}.git"


def _is_valid_bare(bare_path: Path, *, runner: GitRunner) -> bool:
    """`.repos/<repo>.git` 가 *worktree add 의 base 로 실제로 쓸 수 있는 bare git repo* 인지 검증.

    `Path.exists()` 는 경로 존재만 본다 — 중단된 `git clone --bare`(하네스 타임아웃·Ctrl-C·
    #5→#1 cascade)가 남긴 **부분·빈·깨진** `.repos/<repo>.git` 도 exists()=True 다. 그런 무효
    bare 를 "존재 → 재사용"으로 통과시키면 repo add 는 rc0 success 인데 invariant(쓸 수 있는
    bare)는 깨진 채, 나중 `git worktree add` 의 base 가 없어 날 git 에러로 죽는다(원인 안
    드러남·audit #1→#4).

    **두 조건 AND** (codex 게이트·실측 확정):
      1. **bare 형식** — `git -C <bare> rev-parse --is-bare-repository` rc0 && stdout 에 `true`.
      2. **HEAD 해소** — `git -C <bare> rev-parse --verify HEAD` **rc0**. is-bare(=`core.bare=true`)
         만으론 부족하다: `git init --bare`(또는 objects fetch 전 죽은 clone)가 남긴 **빈/부분
         bare** 는 core.bare=true 지만 HEAD/objects 가 없다(실측: is-bare "true"·`rev-parse
         --verify HEAD` rc128·`rev-list --all --count` 0). `worktree add <path>` 는 HEAD 를
         체크아웃하므로 **HEAD 해소 = base 가용성의 정확한 precondition** — rc 기반 검사(문자열
         파싱 아님·견고)로 이 빈/부분 bare 를 broken 으로 잡는다.

    부분 bare 는 discovery 가 상위 워킹트리로 올라가 is-bare `false`(rc0·`.repos/` 는 워킹트리
    안)거나 상위도 repo 아니면 rc≠0(fatal) — 어느 쪽도 조건1 False. **fail-soft**: git 바이너리
    부재·예외는 runner 가 (1, msg) 로 감싸므로 자연히 False(크래시 0·rc≠0 로 호출부 위임).

    is-bare 파싱은 `"true" in out.split()`(reviewer sug) — `_real_git_runner` 가 stdout+stderr
    를 결합 반환하므로, 유효 bare 라도 git 이 stderr 경고 한 줄을 내면 `out.strip()==
    "true"` 는 false-negative(유효를 broken 오판)다. 공백 토큰에 `true` 존재로 그 경고에 안 흔들린다.

    `runner` 는 argv 앞에 `-C <bare>` 를 받는 clone-runner 계열 — pm_config 의 sibling 헬퍼
    (`_set_bare_fetch_refspec`·`_ensure_bare_branch_tracking`·`_resolve_base`)가 주입 runner 를
    `-C <bare>` 로 재사용하는 관례와 동일(별도 DI seam 안 만듦·주입 mock 은 이 argv 로 유효/무효
    bare 를 모델링). 절대경로 `-C` 는 중첩돼도(worktree_pool 실경로 `_real_git_runner(bare)` 와
    이중 `-C`) idempotent(뒤 절대경로가 앞을 리셋)라 안전.
    """
    rc, out = runner(["-C", str(bare_path), "rev-parse", "--is-bare-repository"])
    if rc != 0 or "true" not in out.split():
        return False
    # HEAD 해소 검증 — is-bare(형식)만으론 빈/부분 bare 를 통과시킨다(위 조건2). worktree add 의
    # 체크아웃 대상 HEAD 가 해소돼야 base 로 쓸 수 있다. rc 기반(견고·문자열 무관).
    head_rc, _ = runner(["-C", str(bare_path), "rev-parse", "--verify", "HEAD"])
    return head_rc == 0


def _slot_branch_exists(runner: GitRunner, branch: str) -> bool:
    """로컬 브랜치 `branch` 가 존재하는지 — **color-safe** machine-readable 출력으로 판정.

    create_slot 의 두 곳이 브랜치 존재를 본다: (1) branch 미지정 경로의 슬롯-전용 브랜치 `<repo>_<N>`
    선-검출(→ `SlotBranchExists` fail-loud), (2) 명시 `branch=` 경로의 checkout(기존)/`-B`(신규)
    분기(→ 기존 브랜치 리셋-유실 방지). 둘 다 이 단일 helper 를 경유한다.

    ⚠️ **평문 `git branch --list <b>` 는 ambient `color.branch=always` 서 ANSI 오염**(codex 실측:
    출력 `'  A_1\\x1b[m\\n'` → `.split()` 토큰 `'A_1\\x1b[m'` 이 브랜치명과 불일치 → 기존 브랜치를
    "없음"으로 오판 → (2)가 checkout 대신 `-B` 로 가서 리셋-유실 재개방). 그래서 **`--format=
    %(refname:short)`**(ref-filter 포맷·색 atom 없음)로 뽑는다 — `color.branch=always` 여도 평문
    (`'A_1\\n'`·실측)이라 오염이 없다. **splitlines 정확-일치**(`line.strip()==branch`)로 판정:
    exact 패턴(`--list <b>`·glob 메타 없음)이라 그 브랜치만 리스트하고, `_real_git_runner` 의
    stdout+stderr 결합에 stderr 경고가 섞여도 그 라인은 브랜치명과 정확-일치하지 않아
    안전(`.split()` 부분매치보다 견고).

    **rc 무시** — `git branch --list` 는 매치 없어도 rc0 이라 rc 기반(show-ref/rev-parse)은 못 쓴다
    (게다가 주입 runner 의 generic 폴백 rc0 이 "존재" 오탐돼 hermetic 테스트를 깬다).
    출력 라인 정확-일치만 신뢰한다. 주입 runner(테스트 mock)의 generic 폴백 `(0, "")` 은 빈
    splitlines → **"부재"**(안전 기본 — checkout 대신 `-B` 생성·리셋 대상 없음).
    """
    _rc, out = runner(["branch", "--list", "--format=%(refname:short)", branch])
    return any(line.strip() == branch for line in str(out).splitlines())


# ── 보호 브랜치 pre-push 훅 (하드·회사 repo 무영향 / 라이브 게이트 승격) ────
# 훅 = `.project_manager/.local/repo-hooks/<repo>/pre-push`(프레임워크 소유·gitignore).
# bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리로 set → 슬롯 push 가 이 훅에
# 게이트된다. **회사 repo 서버/사용자 클론 무변경** — client-side·우리 bare 미러 config 1줄만.
#
# 훅은 *generic* 이다 — 보호목록은 `protected`, repo 형상별 증거 계약은
# `gate-contract` sidecar에서 읽는다. 계약 파일의 1행은 mode(release/self-test),
# 2행은 self-test 명령이며 둘은 하나의 원자 교체로 갈린다. 따라서
# 재설치 중 push가 겹쳐도 서로 다른 세대의 mode/command가 결합하지 않는다.
# 설치(install_protected_hook)가 sidecar를 채우며, 훅 본문 자체는 repo 무관하게
# 동일하다. POSIX sh — Windows git 번들 sh 로도 동작.
#
# 로직: stdin 의 `<localref> <localsha> <remoteref> <remotesha>` 줄들(=이 push 의 모든 ref)을 순회한다.
# pre-push 는 push 전체에 한 번 발화하는 all-or-nothing 게이트라 **보호 ref 를 전부 검증**하고 하나라도
# 실패하면 push 전체를 거부한다(예: `git push main release` 서 main 만 green 이어도 release 가 미검증이면
# 편승 차단). remote ref (`refs/heads/<b>`)의 `<b>` 가 sidecar 보호목록에 있으면 (그 ref 의 localsha 로) —
#   - `PM_ALLOW_PROTECTED_PUSH=1` 아니면 하드 차단(echo 안내·exit 1).
#   - `release`: push SHA의 `board.py livegate check --rev <sha>` rc0을 추가 요구.
#     `PM_SKIP_LIVE_GATE=1`은 라이브-무관·긴급 변경 한정 우회다. 단 **미해소 must-fix 잔여**는
#     그 우회의 대상이 아니다. 훅은 장부 writer가 현행화한 `release-must-fix` 한 줄 표식만 읽고,
#     `clear`가 명확할 때만 우회한다. 표식 부재·손상·읽기 실패는 잔여 미상이라 fail-closed다.
#   - `self-test`: 현재 checkout HEAD=push SHA·clean을 고정한 뒤 계약의 repo 테스트
#     명령을 실행한다. `PM_SKIP_SELF_TEST=1` + 빈 값이 아닌
#     `PM_SELF_TEST_BYPASS_REASON` 조합만 감사 로그를 남기고 우회한다.
# 계약/mode 미해소·필요 자원 부재는 fail-closed다. 보호목록을 정상 해소한 뒤의
# feature(비보호) 브랜치와 목록이 필요 없는 tag push는 통과(exit 0·PM_ALLOW/증거 게이트 무관).
#
# 외부 명령 실패 방향: pre-push의 `cat`(보호목록 I/O 건전성), 비우회 self-test의 `git`·`sh`,
# 우회 감사의 `tr`·`date`, release의 Python launcher·board.py는 모두 실패 시 거부한다.
# 우회 중 `git status`만 검증 결과가 아닌 보조 dirty telemetry라 실패를 `unknown`으로 명시 기록한다.
# pre-commit의 `git` 판정 실패도 거부한다. 경로 계산은 셸 확장만 써 dirname/basename 의존이 없다.
#
# **멱등 자가치유 배포**: install_protected_hook 이 매 호출 이 본문을 덮어쓰므로(repo add·
# worktree add), 엔진 update 후 다음 재설치가 이 신 버전을 자동 배포한다.
_PROTECTED_PRE_PUSH_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-push 가드 — PM 이 보호 브랜치(main 등)에 자율 push 못 하게 +
# 승인(PM_ALLOW_PROTECTED_PUSH=1)된 protected push 도 릴리즈 라이브 게이트 green 을 추가 요구.
# install_protected_hook() 가 설치. 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치).
case "$0" in
    */*) hook_dir=${0%/*} ;;
    *) hook_dir=. ;;
esac
protected_file="$hook_dir/protected"
engine_root_file="$hook_dir/engine-root"
gate_contract_file="$hook_dir/gate-contract"
protected_loaded=0
protected_branches="
"

# tag/non-branch ref는 이름만으로 비보호임이 확정된다. 보호목록은 첫 branch ref에서만 읽어,
# 판정에 필요 없는 sidecar 손상이 tag-only push 계약을 막지 않게 한다. branch ref는 목록을
# 해소하지 못하면 비보호로 추측하지 않고 fail-closed한다.
pm_load_protected_branches() {
    if [ ! -f "$protected_file" ]; then
        echo "[pm 보호 가드] 보호 브랜치 목록을 찾을 수 없어 push 거부: $protected_file" >&2
        echo "  sidecar를 직접 편집하지 말고 보호목록 설정 후 훅을 재설치하라:" >&2
        echo "    pm-config repo protected ${hook_dir##*/} <목록>" >&2
        echo "    pm-update" >&2
        return 1
    fi
    if ! command -v cat >/dev/null 2>&1; then
        echo "[pm 보호 가드] 보호 브랜치 목록을 읽을 cat 명령을 찾을 수 없어 push 거부." >&2
        echo "  실행 환경의 PATH를 복구한 뒤 다시 push하라." >&2
        return 1
    fi

    # 파일은 cat 한 번으로만 읽고, 종료코드와 그때 얻은 문자열을 같은 스냅샷의 두 축으로 쓴다.
    # 명령 치환이 후행 개행을 지우므로 셸 builtin printf가 종단 바이트 하나를 붙여 보존한 뒤
    # 위치로 제거한다. 이 바이트는 성공 표식이 아니다(cat rc는 $?가 단독 담당) — 파일 내용이
    # 같은 바이트를 포함해도 마지막에 훅이 붙인 한 바이트만 제거하므로 위조 채널이 없다.
    protected_snapshot=$(
        cat "$protected_file"
        protected_read_rc=$?
        printf .
        exit "$protected_read_rc"
    )
    protected_read_rc=$?
    protected_snapshot=${protected_snapshot%.}

    protected_entries=0
    protected_malformed=0
    # 정상 sidecar는 각 행(마지막 행 포함)이 개행으로 끝난다. 위 프레이밍 덕분에 명령 치환
    # 뒤에도 원본의 마지막 개행 유무가 남아 있어, 여러 완성 행 뒤의 마지막 부분행도 잡는다.
    case "$protected_snapshot" in
        *"
") ;;
        *) protected_malformed=1 ;;
    esac
    while IFS= read -r protected_branch; do
        [ -n "$protected_branch" ] || continue
        case "$protected_branch" in
            *[[:space:]]*) protected_malformed=1 ;;
        esac
        protected_entries=$((protected_entries + 1))
        protected_branches="${protected_branches}${protected_branch}
"
    done <<PM_PROTECTED_SNAPSHOT
$protected_snapshot
PM_PROTECTED_SNAPSHOT

    protected_invalid=0
    [ "$protected_read_rc" -eq 0 ] || protected_invalid=1
    [ "$protected_entries" -gt 0 ] || protected_invalid=1
    [ "$protected_malformed" -eq 0 ] || protected_invalid=1
    if [ "$protected_invalid" -ne 0 ]; then
        echo "[pm 보호 가드] 보호 브랜치 목록을 확인할 수 없어 push 거부 — 손상됐거나 훅 재설치 중일 수 있음: $protected_file" >&2
        echo "  sidecar를 직접 편집하지 말고 보호목록 설정 후 훅을 재설치하라:" >&2
        echo "    pm-config repo protected ${hook_dir##*/} <목록>" >&2
        echo "    pm-update" >&2
        return 1
    fi
    protected_loaded=1
    unset protected_branch protected_entries protected_malformed
    unset protected_snapshot protected_read_rc protected_invalid
    return 0
}

# stdin(<localref> <localsha> <remoteref> <remotesha> 줄들)의 각 push ref 를 순회한다. pre-push 는
# push 전체에 한 번 발화(all-or-nothing) — 보호 ref 를 *전부* 검증하고, 하나라도 실패하면(하드 차단
# or 라이브 게이트 미green) exit 1 로 push 전체를 거부한다(한 push 에 보호 ref 여러 개면 각 ref 의
# localsha 로 각각 게이트·미검증 ref 가 green ref 에 편승해 올라가는 것 차단). board.py/인터프리터는
# 첫 게이트 검증 때 1회 지연 해소한다. stdin 은 파이프 아닌 fd0 직독이라 루프가 현재 셸에서 돌아
# exit·변수 누적이 훅 전체에 반영된다(서브셸 아님).
board=""
py=""
python_floor=""
found_versions=""
resolved=0
self_tested_shas=""

# 채택자 명령을 실행할 때 제거할 repository-local Git 환경은 현재 Git 자체에서 유도한다.
# rev-parse가 고장난/구형 Git에서도 훅 자체가 죽지 않도록, 폴백은 이 기능 도입 당시 훅이
# 명시적으로 지우던 보수적 7개 목록을 유지한다. 정상 경로 목록을 코드에 복제하지 않는다.
pm_unset_git_local_env() {
    _pm_git_local_env=$(git rev-parse --local-env-vars 2>/dev/null)
    _pm_git_local_env_rc=$?
    if [ "$_pm_git_local_env_rc" -ne 0 ] || [ -z "$_pm_git_local_env" ]; then
        _pm_git_local_env="GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES"
    fi
    unset $_pm_git_local_env
    unset _pm_git_local_env _pm_git_local_env_rc
}

while read -r _local_ref local_sha remote_ref _remote_sha; do
    case "$remote_ref" in
        refs/heads/*) branch=${remote_ref#refs/heads/} ;;
        *) continue ;;
    esac

    if [ "$protected_loaded" != "1" ]; then
        pm_load_protected_branches || exit 1
    fi

    # 이 ref 가 첫 branch ref 시점에 읽어 둔 sidecar 보호목록에 있나?
    is_protected=0
    case "$protected_branches" in
        *"
$branch
"*) is_protected=1 ;;
    esac
    [ "$is_protected" = "1" ] || continue   # 비보호 ref(feature)·tag → 이 ref 통과·다음 ref.

    # 보호 ref — 승인(PM_ALLOW_PROTECTED_PUSH=1) 없으면 하드 차단 (즉시 push 전체 거부).
    if [ "$PM_ALLOW_PROTECTED_PUSH" != "1" ]; then
        echo "[pm 보호 가드] 보호 브랜치 '$branch' 로의 push 거부." >&2
        echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — feature 브랜치로 작업하고" >&2
        echo "  main 갱신은 사용자에게 맡긴다(PR/머지). 사용자 명시 OK 면:" >&2
        echo "    PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi

    # 승인됨(PM_ALLOW=1) — repo 형상별 증거 계약. mode sidecar 부재/미해소는 추측 없이 거부한다.
    gate_mode=""
    self_test_cmd=""
    if [ -f "$gate_contract_file" ]; then
        {
            IFS= read -r gate_mode
            IFS= read -r self_test_cmd
        } < "$gate_contract_file"
    fi
    case "$gate_mode" in
        release)
            # PM_SKIP_LIVE_GATE는 framework release livegate만 생략한다. adopter self-test에는
            # 적용하지 않아 skip 상시화가 자기 검증까지 무력화하지 않게 한다.
            if [ "$PM_SKIP_LIVE_GATE" = "1" ]; then
                # 우회 사유는 **라이브 축**(오프라인·라이브 무관 변경·긴급 hotfix)이다. 미해소
                # must-fix 는 다른 축이라 이 우회의 대상이 아니다. livegate 마지막 reason 은 green
                # 뒤 새 반려를 못 보므로, 장부 writer가 매 변경에 원자 갱신하는 현행 잔여 표식만
                # 읽는다. Python 해소는 불요지만 `clear` 정확 한 줄 외에는 전부 fail-closed다.
                mf_engine_root=""
                [ -f "$engine_root_file" ] && IFS= read -r mf_engine_root < "$engine_root_file"
                mf_flag="$mf_engine_root/.project_manager/.local/release-must-fix"
                mf_state=""
                mf_extra=""
                mf_read_rc=1
                mf_extra_rc=1
                if [ -n "$mf_engine_root" ] && [ -f "$mf_flag" ]; then
                    {
                        IFS= read -r mf_state
                        mf_read_rc=$?
                        IFS= read -r mf_extra
                        mf_extra_rc=$?
                    } < "$mf_flag"
                fi
                if [ "$mf_read_rc" -ne 0 ] || [ "$mf_extra_rc" -eq 0 ] || [ -n "$mf_extra" ]; then
                    echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — must-fix 잔여 판정 표식을 읽을 수 없음." >&2
                    echo "  PM_SKIP_LIVE_GATE 는 잔여가 없다는 현행 표식(clear)이 명확할 때만 허용된다 — 부재·손상·판독 실패는 fail-closed." >&2
                    echo "  장부/환경을 복구하고 표식을 다시 생성하라:" >&2
                    echo "    board.py livegate record" >&2
                    exit 1
                fi
                case "$mf_state" in
                    clear) continue ;;
                    blocked)
                        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — 미해소 must-fix 잔여." >&2
                        echo "  PM_SKIP_LIVE_GATE 는 라이브 축 우회이고 이 차단은 리뷰 잔여 축이다 — 우회 대상이 아니다." >&2
                        echo "  게이트마다 처분을 선언한 뒤 다시 기록하라:" >&2
                        echo "    external_review.py --resolve-gate <게이트> --into <T-NNNN> | --fixed <근거 게이트>" >&2
                        echo "    board.py livegate record" >&2
                        exit 1
                        ;;
                    *)
                        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — must-fix 잔여 판정 표식 형식 오류('$mf_state')." >&2
                        echo "  허용 형식은 writer가 원자 기록한 clear 또는 blocked 정확 한 줄뿐이다. board.py livegate record를 다시 실행하라." >&2
                        exit 1
                        ;;
                esac
            fi
            ;;
        self-test)
            # 채택자 전용 한정 우회는 증거 생성(명령 해소·HEAD pin·clean 검사)보다 앞선다.
            # dirty는 거부 조건이 아니라 감사 메타데이터다. status 자체가 실패해도 우회는
            # 사용할 수 있어야 하므로 unknown을 기록한다. 반면 시각·사유·append는 누가 왜 언제
            # 우회했는지 남기는 감사 증거 자체라 하나라도 만들지 못하면 fail-closed한다.
            if [ "$PM_SKIP_SELF_TEST" = "1" ]; then
                if ! command -v tr >/dev/null 2>&1 || ! command -v date >/dev/null 2>&1; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사에 필요한 tr/date 명령을 찾을 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                bypass_reason=$(printf '%s' "$PM_SELF_TEST_BYPASS_REASON" | tr '\\r\\n\\t' '   ')
                bypass_reason_rc=$?
                if [ "$bypass_reason_rc" -ne 0 ]; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 사유를 안전하게 정규화할 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                case "$bypass_reason" in
                    *[![:space:]]*) ;;
                    *)
                        echo "[pm 보호 가드] 채택자 자기 검증 우회 거부 — PM_SELF_TEST_BYPASS_REASON 사유가 필요하다." >&2
                        exit 1
                        ;;
                esac
                bypass_status=$(
                    pm_unset_git_local_env
                    git status --porcelain --untracked-files=normal 2>/dev/null
                )
                bypass_status_rc=$?
                if [ "$bypass_status_rc" -ne 0 ]; then
                    bypass_dirty=unknown
                elif [ -n "$bypass_status" ]; then
                    bypass_dirty=true
                else
                    bypass_dirty=false
                fi
                bypass_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ' </dev/null 2>/dev/null)
                bypass_at_rc=$?
                if [ "$bypass_at_rc" -ne 0 ] || [ -z "$bypass_at" ]; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 시각을 기록할 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                bypass_log="$hook_dir/self-test-bypass.log"
                if [ -L "$bypass_log" ] || { [ -e "$bypass_log" ] && [ ! -f "$bypass_log" ]; }; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사 로그가 일반 파일이 아님 — 보호 push 거부." >&2
                    exit 1
                fi
                repo_name=${hook_dir##*/}
                if [ -z "$repo_name" ]; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사 repo 이름을 해소할 수 없어 보호 push 거부." >&2
                    exit 1
                fi
                if ! printf '%s\\trepo=%s\\tbranch=%s\\tsha=%s\\tdirty=%s\\treason=%s\\n' \
                        "$bypass_at" "$repo_name" "$branch" "$local_sha" \
                        "$bypass_dirty" "$bypass_reason" >> "$bypass_log"; then
                    echo "[pm 보호 가드] 채택자 자기 검증 우회 감사 로그 기록 실패 — 보호 push 거부." >&2
                    exit 1
                fi
                echo "[pm 보호 가드·감사] 채택자 자기 검증 우회 사용: branch=$branch sha=$local_sha dirty=$bypass_dirty" >&2
                echo "  사유: $bypass_reason" >&2
                echo "  기록: $bypass_log" >&2
                continue
            fi

            if ! command -v git >/dev/null 2>&1; then
                echo "[pm 보호 가드] 채택자 자기 검증에 필요한 git 명령을 찾을 수 없어 보호 push 거부." >&2
                exit 1
            fi
            if [ -z "$self_test_cmd" ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 명령 미해소 — fail-closed 거부." >&2
                echo "  areas.md test_cmd 또는 PM 홈 local.conf test_cmd를 설정하고 훅을 재설치하라." >&2
                exit 1
            fi
            if ! command -v sh >/dev/null 2>&1; then
                echo "[pm 보호 가드] 채택자 자기 검증 명령을 실행할 sh를 찾을 수 없어 보호 push 거부." >&2
                exit 1
            fi

            # 한 push에 같은 SHA의 보호 ref가 여러 개여도 clean checkout에서 같은
            # repo 계약을 다시 돌릴 필요가 없다. SHA는 git hex라 공백 구분이 안전하다.
            case " $self_tested_shas " in
                *" $local_sha "*) continue ;;
            esac
            repo_root=$(git rev-parse --show-toplevel 2>/dev/null)
            current_head=""
            [ -n "$repo_root" ] && current_head=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null)
            if [ -z "$repo_root" ] || [ -z "$current_head" ] || [ "$current_head" != "$local_sha" ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 기준을 push SHA에 고정할 수 없어 거부." >&2
                echo "  push 대상 브랜치를 checkout한 뒤 같은 HEAD에서 다시 push하라." >&2
                echo "  checkout HEAD=${current_head:-미해소} · push=${local_sha}" >&2
                exit 1
            fi

            # pre-push는 git이 GIT_DIR/GIT_WORK_TREE 등을 훅 환경에 주입할 수 있다. 그 상태로
            # 채택자 테스트의 git fixture를 실행하면 임시 repo 대신 사용자 슬롯 index/gitdir을
            # 읽거나 변경한다. repo_root/HEAD 핀은 위에서 먼저 해소하고, clean 판정과 실제 테스트는
            # git 저장소 지시 환경을 제거한 별도 subshell에서 실행한다.
            worktree_status=$(
                pm_unset_git_local_env
                git -C "$repo_root" status --porcelain --untracked-files=normal
            )
            status_rc=$?
            if [ "$status_rc" -ne 0 ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 전 워킹트리 clean 상태를 확인할 수 없어 거부." >&2
                echo "  push 대상 checkout의 git status를 복구한 뒤 다시 push하라." >&2
                exit 1
            fi
            if [ -n "$worktree_status" ]; then
                echo "[pm 보호 가드] push SHA와 다른 dirty 워킹트리에서는 자기 검증할 수 없어 거부." >&2
                echo "  tracked/staged/untracked 변경을 commit·stash·정리한 clean checkout에서 다시 push하라." >&2
                exit 1
            fi

            echo "[pm 보호 가드] 채택자 자기 검증: $self_test_cmd" >&2
            (
                pm_unset_git_local_env
                cd "$repo_root" && sh -c "$self_test_cmd"
            ) </dev/null
            self_test_rc=$?
            if [ "$self_test_rc" -ne 0 ]; then
                echo "[pm 보호 가드] 채택자 자기 검증 RED(rc=$self_test_rc) — 보호 push 거부." >&2
                echo "  areas.md test_cmd 또는 PM 홈 local.conf test_cmd를 현재 repo 명령으로 설정하라." >&2
                echo "  긴급 우회(감사 기록 필수): PM_SKIP_SELF_TEST=1 PM_SELF_TEST_BYPASS_REASON='사유' PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
                exit 1
            fi
            self_tested_shas="$self_tested_shas $local_sha"
            continue
            ;;
        *)
            echo "[pm 보호 가드] repo 게이트 스코프 미해소('$gate_mode') — fail-closed 거부." >&2
            echo "  PM 홈 local.conf upstream을 확인하고 pm-update 또는 pm-config repo add로 훅을 재설치하라." >&2
            exit 1
            ;;
    esac

    # framework release 라이브 게이트 검증 자원 1회 지연 해소. board.py 는 **PM 홈 엔진**에 있다 — 슬롯 worktree(회사/
    # family checkout)엔 PM 엔진 파일이 없으므로(회사 repo 무영향), 설치자가 훅 옆에 쓴 sidecar
    # `engine-root`(PM 홈 REPO 절대경로 1줄)에서 board.py 를 해소한다. livegate.json 도 그 PM 홈 .local
    # 소유라 기록 위치와 정합. 인터프리터는 실행검증 폴백(python3->python->py·WindowsApps 가짜
    # shim 회피) + Python 3.11 하한 검증(engine_rev.MIN_PYTHON 미러·테스트 skew 가드).
    # sidecar 부재/경로 무효/인터프리터 부재 = fail-closed 거부(무력화 방지).
    if [ "$resolved" != "1" ]; then
        engine_root=""
        [ -f "$engine_root_file" ] && IFS= read -r engine_root < "$engine_root_file"
        if [ -n "$engine_root" ] && [ -f "$engine_root/.project_manager/tools/board.py" ]; then
            board="$engine_root/.project_manager/tools/board.py"
        fi
        if [ -n "$engine_root" ] && [ -f "$engine_root/.project_manager/tools/python_floor.py" ]; then
            python_floor="$engine_root/.project_manager/tools/python_floor.py"
        fi
        for _cand in python3 python py; do
            if command -v "$_cand" >/dev/null 2>&1 &&
                    "$_cand" --version >/dev/null 2>&1 &&
                    [ -n "$python_floor" ]; then
                probe_output=$("$_cand" "$python_floor" 2>&1)
                probe_rc=$?
                case "$probe_output" in
                    Python*) probe_version=${probe_output#Python } ;;
                    *) probe_version="확인 실패" ;;
                esac
                [ -z "$found_versions" ] || found_versions="$found_versions, "
                found_versions="${found_versions}${_cand}=${probe_version}"
                if [ "$probe_rc" -eq 0 ]; then
                    py="$_cand"
                    break
                fi
            fi
        done
        if [ -z "$board" ] || [ -z "$py" ]; then
            echo "[pm 라이브 게이트] 게이트 검증 실행 불가 — fail-closed 거부." >&2
            if [ -z "$board" ]; then
                echo "  PM 엔진 board.py 를 못 찾았다 (engine-root sidecar='${engine_root}')." >&2
            elif [ -z "$python_floor" ]; then
                echo "  Python 하한 probe를 못 찾았다 (engine-root sidecar='${engine_root}')." >&2
            else
                echo "  Python 3.11+ 필요, 발견: ${found_versions:-없음}." >&2
            fi
            echo "  보호 브랜치 push 는 라이브 게이트 green 을 요구한다. 게이트를 못 돌리면 무력화 방지로 거부한다." >&2
            echo "  라이브-무관 변경(docs 등)·긴급 hotfix 면 우회(현행 must-fix 잔여-무 표식 clear 가 있어야 함 — 부재면 record 1회 선행):" >&2
            echo "    PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
            exit 1
        fi
        resolved=1
    fi

    # 이 보호 ref 의 push sha 로 라이브 게이트 green 검증 (기록 존재·pass·HEAD 일치=rc0·board.py 사유 surface).
    # </dev/null — 다중 ref 루프가 stdin(fd0) 직독이라 board.py 가 stdin 을 소비하면
    # all-or-nothing 순회가 깨진다(현재 안 읽지만 향후 변경 방어).
    "$py" "$board" livegate check --rev "$local_sha" </dev/null
    gate_rc=$?
    if [ "$gate_rc" -ne 0 ]; then
        # rc!=0 — 이 보호 ref 가 미green → push 전체 거부 + 2분기 안내 (환경 복구 vs 우회).
        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — 라이브 게이트 미green." >&2
        echo "  릴리즈(main 머지)는 릴리즈 라이브 wave 가 이 커밋에서 green 이어야 한다(위 사유 참조)." >&2
        echo "  - 환경 문제(오프라인·LLM 서비스 장애/한도·게이트 오작동)면 — 우회 아님·환경 복구 후 재실행:" >&2
        echo "      board.py livegate record" >&2
        echo "  - 라이브-무관 변경(docs 등)·긴급 hotfix 면 — 우회(현행 must-fix 잔여-무 표식 clear 필요·부재면 record 1회 선행):" >&2
        echo "      PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi
done

# 모든 보호 ref 가 게이트 통과(또는 skip)·또는 보호 ref 없음(비보호/tag) → push 통과.
exit 0
"""


# ── 보호 브랜치 pre-commit 훅 (commit-time 강제) ──────────────
# pre-push 훅과 **같은 배선**을 탄다 — 같은 훅 디렉토리(`.local/repo-hooks/<repo>/`)·같은
# sidecar `protected`(줄당 1브랜치)·같은 bare `core.hooksPath`(신규 seam 0). 파일 하나가
# 더 놓일 뿐이라 슬롯 worktree 는 재설치 즉시 이 훅을 탄다. 단, sidecar 부재 처리는 의도적으로
# 비대칭이다: pre-commit은 전 commit 차단의 blast radius를 피하려 통과시키고, 하드 백스톱인
# pre-push만 보호목록 미해소를 fail-closed로 거부한다.
#
# 왜 pre-push 만으론 부족한가: pm_role §보호 브랜치 가드의 "보호 브랜치에 자율 commit 하지
# 않는다" 는 강제 수단이 push 단계뿐이었고, 부트스트랩 0단계 main-참조 검사는 세션 *진입
# 시점*만 본다 — 세션 중 보호 브랜치로 checkout 해서 커밋하는 것을 아무 기계도 안 봤다
# (실측: v1.4.0 wave 11커밋이 전부 `main` 에서).
#
# 로직: sidecar 보호목록에 `git symbolic-ref --short HEAD`(= 현재 브랜치)가 있으면
# `PM_ALLOW_PROTECTED_COMMIT=1` 이 아닌 한 거부(exit 1). **detached HEAD 는 통과** —
# symbolic-ref 가 rc≠0 이라 브랜치가 없다(readonly 공유 슬롯 시맨틱·`_phase0_protected_reject`
# 의 readonly carve-out 과 일치). 비보호 브랜치·sidecar 부재도 통과.
#
# **비커버(정직한 한계)**: `git commit --no-verify`(실측 통과)·merge 커밋
# (`pre-merge-commit` 소관·실측 미발화)·rebase/cherry-pick/revert(sequencer 클래스·실측 통과).
# 이건 우발 방지 가드지 적대적 통제가 아니고, 하드 백스톱은 pre-push(라이브 게이트 포함)가
# 그대로 맡는다. 또 훅은 bare `core.hooksPath` 를 공유하는 **풀 슬롯 worktree** 에만 걸린다
# (PM 홈 clone 자신은 `.git/hooks` 라 가드 밖). merge 미발화는
# 설계상 이득 — "release 브랜치에서 커밋 → main 으로 merge → push" 라는 정합 릴리즈 flow 가
# escape 없이 통과한다(pm_role §릴리즈 절차).
_PROTECTED_PRE_COMMIT_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-commit 가드 — PM 이 보호 브랜치(main 등)에서 자율로
# commit 하지 못하게 한다. install_protected_hook() 가 pre-push 훅과 같은 디렉토리에 설치하고,
# 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치·pre-push 와 공유).
case "$0" in
    */*) hook_dir=${0%/*} ;;
    *) hook_dir=. ;;
esac
protected_file="$hook_dir/protected"
# 의도적 비대칭: 이 commit-time 우발 방지 가드는 sidecar 부재 시 전 commit 차단의 blast radius를
# 피하려 통과한다. 보호목록 미해소의 하드 fail-closed 백스톱은 pre-push가 담당한다.
[ -f "$protected_file" ] || exit 0

# 현재 브랜치. **detached HEAD 는 통과** — symbolic-ref 가 rc≠0(브랜치 없음)이고, readonly
# 공유 슬롯 시맨틱(0단계 readonly carve-out)과 일치시킨다.
if ! command -v git >/dev/null 2>&1; then
    echo "[pm 보호 가드] 현재 브랜치 판정에 필요한 git 명령을 찾을 수 없어 commit 거부." >&2
    exit 1
fi
branch=$(git symbolic-ref -q --short HEAD 2>/dev/null)
branch_rc=$?
if [ "$branch_rc" -eq 1 ]; then
    # symbolic-ref rc1은 정상 detached HEAD일 때만 통과한다. 저장소/HEAD까지 읽을 수 없는
    # 손상을 detached로 오인하지 않도록 commit 객체 해소를 추가 확인한다.
    git rev-parse --verify 'HEAD^{commit}' >/dev/null 2>&1 || {
        echo "[pm 보호 가드] detached HEAD 여부를 확인할 수 없어 commit 거부." >&2
        exit 1
    }
    exit 0
fi
if [ "$branch_rc" -ne 0 ] || [ -z "$branch" ]; then
    echo "[pm 보호 가드] 현재 브랜치를 판정할 수 없어 commit 거부." >&2
    exit 1
fi

# 이 브랜치가 sidecar 보호목록에 있나? (pre-commit이 직접 순회해 정확히 일치하는 행을 찾는다)
is_protected=0
while IFS= read -r protected_branch; do
    [ -n "$protected_branch" ] || continue
    if [ "$branch" = "$protected_branch" ]; then
        is_protected=1
        break
    fi
done < "$protected_file"
[ "$is_protected" = "1" ] || exit 0   # 비보호 브랜치(feature) → 통과.

# 보호 브랜치 — 승인(PM_ALLOW_PROTECTED_COMMIT=1) 없으면 거부.
if [ "$PM_ALLOW_PROTECTED_COMMIT" != "1" ]; then
    echo "[pm 보호 가드] 보호 브랜치 '$branch' 에서의 commit 거부." >&2
    echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — 작업 브랜치로 옮겨 커밋하라:" >&2
    echo "    git switch -c <작업-브랜치>      # 새로 파거나" >&2
    echo "    git switch <기존-작업-브랜치>    # 이미 있으면 그리로" >&2
    echo "  릴리즈도 같다 — 릴리즈 커밋은 release 브랜치에서 하고 '$branch' 은 merge 로 받는다" >&2
    echo "  (merge 커밋은 이 훅이 안 보므로 escape 불요)." >&2
    echo "  사용자 명시 OK 면:" >&2
    echo "    PM_ALLOW_PROTECTED_COMMIT=1 git commit ..." >&2
    exit 1
fi

exit 0
"""


# ── 설치 산출물 명세 — install ↔ 정합 판정의 단일 진실 ────────────────
# **`install_protected_hook` 이 *쓰는 것*이 곧 drift 축이다.** 설치와 판정(pm_update 의
# `_protected_hook_in_sync`)이 각자 목록을 들고 있으면 한쪽만 자라 조용히 갈라진다 — 실제로 그
# 클래스가 연달아 났다(읽기 실패 축 누락 → 다음 라운드 실행권한 축 누락). 그래서 산출물 전수를
# 이 명세 하나가 소유하고, 설치는 이걸 **써서** 쓰고 판정은 이걸 **읽어서** 본다(축을 열거하지
# 않고 유도). 새 산출물을 설치에 추가하려면 이 목록에 넣어야 하고(설치 루프가 순회한다), 그러면
# 판정도 자동으로 따라간다.
#
# 실행권한: 훅 파일은 0755 여야 한다 — **없으면 git 이 훅을 조용히 건너뛴다**(보호 침묵 비활성).
# sidecar(`protected`·`engine-root`·`gate-contract`)는 훅이 `read` 로만 읽는 데이터라 실행권한 요구가 없다
# (설치는 atomic replace 전에 0644 설정) → `executable=False`. 나중에 실행 파일 sidecar가
# 생기면 그 플래그만 True로 바꾸면 판정이 따라온다.
_PROTECTED_HOOK_EXECUTABLE_MODE = 0o755
_PROTECTED_SIDECAR_MODE = 0o644
_PROTECTED_GATE_MODES = frozenset({"release", "self-test"})
PROTECTED_GATE_CONTRACT_NAME = "gate-contract"


class ProtectedHookArtifact(NamedTuple):
    """보호 훅 설치 산출물 1개 — 경로·기대 내용·실행권한 필요 여부."""

    path: Path
    content: str
    executable: bool


def _atomic_write_protected_artifact(artifact: ProtectedHookArtifact) -> None:
    """훅/sidecar 하나를 같은 디렉터리 임시파일에서 완성한 뒤 원자 교체한다.

    재설치와 push가 겹쳐도 독자는 이전 완성본 또는 새 완성본만 본다. 특히 빈/부분
    ``protected``를 잠깐 노출해 보호 ref를 비보호로 통과시키는 제자리 truncate 창을 없앤다.
    실행권한도 교체 전에 임시파일에 설정하므로 새 훅이 보이는데 아직 실행 불가인 창이 없다.
    """
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{artifact.path.name}.", suffix=".tmp", dir=artifact.path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(artifact.content)
        tmp.chmod(
            _PROTECTED_HOOK_EXECUTABLE_MODE
            if artifact.executable else _PROTECTED_SIDECAR_MODE)
        file_lock.atomic_replace(tmp, artifact.path)
    except BaseException:
        # fdopen 전/중 실패도 descriptor와 임시파일을 남기지 않는다. 원래 산출물은 replace
        # 전까지 건드리지 않았으므로 그대로 유효하다.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def protected_hook_artifacts(
    repo: str, protected: list[str], *, gate_mode: str = "release",
    test_cmd: str = "pytest -q",
) -> list[ProtectedHookArtifact]:
    """`install_protected_hook` 이 쓰는 **파일 전수** + 각 파일의 기대 내용/실행권한.

    설치·정합 판정 공용 단일 진실(위 주석). 반환 순서 = 설치 순서:
    데이터 sidecar를 먼저 완성하고 generic 훅 본문을 나중에 노출한다. 배선은 이 목록
    순회 *뒤에* 수행되므로 초기 설치의 미완성 본문이 발화하지 않는다.

    ⚠ bare `core.hooksPath` **배선**은 파일이 아니라 git config 라 이 목록 밖이다 — 판정은
    `pm_config.protected_hook_wired()`(공용 헬퍼)로 그 축을 따로 본다."""
    if gate_mode not in _PROTECTED_GATE_MODES:
        raise ValueError(f"알 수 없는 보호 push gate mode: {gate_mode!r}")
    if not test_cmd.strip() or "\n" in test_cmd or "\r" in test_cmd:
        raise ValueError("보호 push test_cmd는 비어 있지 않은 한 줄이어야 한다")
    if not protected or any(
            not isinstance(branch, str) or not branch or
            any(char.isspace() for char in branch)
            for branch in protected):
        raise ValueError("보호 브랜치 목록은 비어 있지 않은 공백 없는 이름이어야 한다")
    hook_dir = REPO_HOOKS_DIR / repo
    gate_contract = f"{gate_mode}\n{test_cmd}\n"
    return [
        ProtectedHookArtifact(
            hook_dir / "protected", "".join(f"{b}\n" for b in protected), False),
        ProtectedHookArtifact(hook_dir / "engine-root", f"{REPO.resolve()}\n", False),
        ProtectedHookArtifact(
            hook_dir / PROTECTED_GATE_CONTRACT_NAME, gate_contract, False),
        ProtectedHookArtifact(hook_dir / "pre-push", _PROTECTED_PRE_PUSH_HOOK, True),
        ProtectedHookArtifact(hook_dir / "pre-commit", _PROTECTED_PRE_COMMIT_HOOK, True),
    ]


def install_protected_hook(
    repo: str,
    protected: list[str],
    *,
    gate_mode: str,
    test_cmd: str,
    git_runner: GitRunner | None = None,
) -> bool:
    """보호 브랜치 pre-push + pre-commit 훅 + sidecar 를 (재)설치하고 bare `core.hooksPath` 를 wiring 한다.

    **멱등·자가치유** — `pm-config repo add`·`worktree add`·`pm_update` sync 가 매번 호출(이미
    있으면 갱신). 세 가지를 한다:
      1. 훅 디렉토리 `.project_manager/.local/repo-hooks/<repo>/` 생성(프레임워크 소유·gitignore).
      2. sidecar 3종 + `pre-push`·`pre-commit`훅(generic·POSIX sh·LF):
         `protected`(보호목록·줄당 1브랜치·**두 훅 공용**)와 `engine-root`(PM 홈 REPO 절대경로
         1줄·라이브 게이트 board.py 해소용), `gate-contract`(1행 mode,
         2행 adopter 자기 검증 명령). mode/command는 **한 파일·한 번의 원자 교체**로
         교체되어 재설치 중에도 서로 다른 세대가 결합하지 않는다.
      3. bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리(절대경로)로 set
         → 슬롯 push/commit 이 이 훅들에 게이트된다.

    **bare 부재 = no-op·False**(가드) — bare 가 없으면 게이트할 대상이 없다(repo add 가 아직
    clone 안 함·솔로(단일 repo)). 훅/sidecar 도 쓰지 않고 조용히 False(설치 안 함). bare 존재면 설치
    후 True. **회사 repo 무영향** — 모든 write 는 `.project_manager/.local/` + bare config 1줄
    (client-side)·서버 ref/사용자 클론 무변경.

    `git_runner` 주입 시 `core.hooksPath` config 호출을 그 runner 로(테스트 hermetic·`git -C
    <bare>` 컨텍스트는 `_real_git_runner(bare)` 가 묶는다). LF 줄바꿈 명시(Windows 에서도 sh
    가 읽도록·newline="\\n").

    `gate_mode`/`test_cmd`는 의도적으로 기본값 없는 keyword-only다. 설치 경로가 repo별 계약
    해소를 잊으면 즉시 TypeError로 실패해야 하며, framework `release/pytest`로 조용히 되돌아가면
    안 된다. 프로덕션 호출은 `pm_config._install_protected_hook` 한 깔때기를 탄다.
    """
    bare = bare_repo_path(repo)
    if not bare.exists():
        return False  # 게이트할 bare 없음 — no-op(repo add 선행 전·솔로).

    hook_dir = REPO_HOOKS_DIR / repo
    hook_dir.mkdir(parents=True, exist_ok=True)

    # 1~2) 훅·sidecar **전수** write(+실행권한). 멱등 — 매 호출 덮어쓰기(엔진 update 자가치유).
    # 산출물 목록·내용·실행권한은 `protected_hook_artifacts` 단일 진실이다 — 여기서 개별 파일을
    # 직접 쓰지 않는다(설치와 정합 판정이 갈라지는 클래스 폐쇄·위 명세 주석). 순회 순서 = 명세
    # 순서(protected → engine-root → gate-contract → pre-push → pre-commit)이고,
    # 배선(3)은 그 뒤다. 데이터를 본문보다 먼저 완성해 초기 설치 중
    # "신 본문 + mode 부재" 하드 차단 창도 만들지 않는다.
    #   - `pre-push`·`pre-commit`: generic 훅 본문·POSIX sh·LF·0755.
    #   - sidecar `protected`: 보호목록(줄당 1브랜치). 목록 변경 시 재설치가 갱신.
    #   - sidecar `engine-root`: PM 홈 REPO 절대경로 1줄(라이브 게이트 board.py 해소용).
    #   - sidecar `gate-contract`: repo 형상별 mode + adopter 자기 검증 명령의
    #     분할 불가 원자 계약.
    #     설치자는 PM 홈 컨텍스트에서 도므로 REPO 를 안다 — 훅은 슬롯 worktree(회사/family
    #     checkout·PM 엔진 파일 없음)에서 발화해 self-locate 가 불가하다.
    for artifact in protected_hook_artifacts(
            repo, protected, gate_mode=gate_mode, test_cmd=test_cmd):
        _atomic_write_protected_artifact(artifact)

    # 3) bare core.hooksPath wiring (절대경로) — client-side·우리 미러 config 1줄.
    # **rc 검사**: config 실패면 훅이 실제로 wiring 안 됐는데 성공 보고하면 보호
    # 가드가 *침묵 무력화* 된다(하드 차단 보장 위반). rc≠0 → False 반환(호출부가 경고 surface).
    runner = git_runner or _real_git_runner(bare)
    rc, _out = runner(["config", "core.hooksPath", str(hook_dir.resolve())])
    return rc == 0


def _slot_number(slot: str) -> float:
    """슬롯 식별자(`work/<repo>_<N>`)의 번호 N — 정렬 키 (최소 번호 대여 결정론 ⓒ).

    마지막 `_` 뒤 tail 이 숫자면 그 int, 아니면(비-숫자 커스텀 슬롯) `inf` 로 밀어 숫자 슬롯을
    앞세운다(결정론적 정렬·부재/이상 슬롯이 최소 자리를 차지하지 않게). repo 명에 `_` 가 들어가도
    (`project_manager`) 마지막 `_` 분리라 정확하다(슬롯 번호는 항상 최후 마디)."""
    tail = slot.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else float("inf")


def _existing_slot_numbers(repo: str, leases: list[Lease]) -> set[int]:
    """장부에 이미 있는 이 repo 의 슬롯 번호 집합."""
    nums: set[int] = set()
    prefix = f"work/{repo}_"
    for lease in leases:
        if lease.slot.startswith(prefix):
            tail = lease.slot[len(prefix):]
            if tail.isdigit():
                nums.add(int(tail))
    return nums


# ── pid 생존 판정 (stale 회수) ───────────────────────────────────────────────


def _pid_alive(pid: object) -> bool:
    """공용 ``pm_relay.pid_is_alive`` seam으로 stale/활성 PID를 판정한다."""
    return _load_relay().pid_is_alive(pid)


# ── 공개 API ─────────────────────────────────────────────────────────────────


def reclaim_stale(*, git_runner: GitRunner | None = None) -> list[str]:
    """pid 죽은 leased 슬롯을 회수한다. 회수된 슬롯 식별자 리스트 반환.

    stale = `state==leased && pid 죽음`. 회수 시 dirty 면 stash 로 보존(작업 유실 방지)하고
    idle 로 전이한다(슬롯=재사용 컨테이너·worktree 폴더는 유지). alloc 진입 시 자동 호출된다.
    (조용하지만 작업 중 오판) — pid 생존만 본다.
    """
    reclaimed: list[str] = []
    with _lease_lock():
        leases = _read_ledger_strict()
        # task 소유 슬롯 회수 제외 근거 — **task 소유의 단일 진실 = tasks 장부**.
        # `session` 이 명명 task 이름과 일치하는 leased 슬롯은 pid(즉사 CLI/bootstrap subprocess)가
        # 죽어도 회수하지 않는다(반납은 명시 `release --task`/`task end` 만). bound 마커와 별개의
        # 백스톱이자 상위 진실 — **구버전 alloc 이 만든 기존 task lease 는 `bound` 키 부재(=False 로드)**
        # 라 마커만으론 안 걸리는데, tasks 장부 조인은 마이그레이션 0 으로 구·신 lease 를 모두 보호한다.
        # (런타임 fallback 누적이 아니라 "task 소유=tasks 장부" 라는 단일 진실을 reclaim 이 읽는 것 —
        # [[prefer-data-migration-over-fallback]] 정합: 데이터 진실을 원천에서 읽지 별도 축 누적 안 함.)
        task_names = {t.name for t in _read_tasks_strict()}
        changed = False
        for lease in leases:
            if lease.state != "leased":
                continue
            # readonly 공유 슬롯 제외 — session/pid 없는 무소유 공유 자산이라 pid=0(죽음)
            # 으로 보여도 회수 대상이 아니다(회수하면 idle 화돼 alloc 이 잡아채 role 이 유실된다).
            if lease.role == "readonly":
                continue
            # 사람 bind 슬롯(bound) 제외 — `bind_slot` 이 적는 pid 는 ephemeral bootstrap
            # subprocess pid 라 즉사하는데, 사람 경로는 명시 `release` 로만 반납한다(pid=정보용). 타 세션
            # alloc 의 `reclaim_stale` 진입이 이 bind lease 를 `leased && pid 죽음` = stale 오판해 회수하면
            # 사람 정체성이 유실된다. bound 를 회수 대상에서 제외해 닫는다
            # Amendment가 처방한 "reclaim 제외 마커"). crash 후 자동 회수는 없다 — 0단계 세션명
            # 점유검사 + 재bind(직접 지정)로 자연 회복(열린 질문 수용).
            if lease.bound:
                continue
            # task 소유 슬롯(session ∈ tasks 장부) 제외 — 위 근거. 구·신 task lease 통합 보호.
            if lease.session in task_names:
                continue
            if _pid_alive(lease.pid):
                continue
            # stale — pid 죽음. dirty 면 stash 로 보존하고 idle 화.
            path = slot_path(lease.slot)
            if path.exists() and _is_dirty(path, git_runner=git_runner):
                _stash(path, git_runner=git_runner)
            lease.state = "idle"
            lease.session = ""
            lease.pid = 0
            # ⚠️ **git 은 의도적으로 보존한다 — 여기 `lease.git = None` 을 넣지 마라** (crash-resume
            #    계약·load-bearing). release/force_release 는 반대로 git 을 정리(None)하는데 이 **비대칭은
            #    의도적**이다: 그쪽은 *명시적 teardown*(작업완료 반납 → 기대 리셋)이지만, reclaim 은
            #    **crash(pid 죽음) 회수**라 다르다. 죽은 세션이 남긴 스냅(head/base)이 reclaim 을 넘어
            #    살아야, 다음 부트스트랩 0단계 compare가 live 를 "내 crash 커밋의 후손
            #    (descendant=notice·정상 재개)" vs "외부 개입(diverged=FAIL-LOUD)"으로 가른다
            #    (`_head_relation`). 지우면 그 판정이 unrecorded 로 무력화돼 crash-resume 이 조용히
            #    깨진다. base 도 슬롯 파생 원점(슬롯 속성)이라 같은 워크스트림 재개 시 보존이 옳다.
            #    표면상 모순돼 보이는 release/force_release 의 `git=None` 은 여기 비대칭으로 해소된다 —
            #    향후 '일관성 fix' 로 여기 git 정리를 넣으면 crash-resume 판정이 조용히 사라진다
            #    (`test_reclaim_stale_preserves_git_for_crash_resume` 가 그 회귀를 하드 차단).
            reclaimed.append(lease.slot)
            changed = True
        if changed:
            _write_ledger(leases)
    return reclaimed


# ── task 컬렉션 (top-level `tasks`·슬롯과 직교) ───────────────────────
# 리스 장부 파일의 top-level `tasks` 배열에 산다(leases 와 형제·같은 `_lease_lock`/atomic).
# 슬롯 0개로도 존재 가능(task 는 alloc 전에 먼저 생긴다). 동시 세션 거부는 pid 생존검사로
# reclaim_stale 과 동형 판정(`_pid_alive`)한다 — 신설 개념 0.


def _validate_task_name(name: str, registered_repos: "list[str] | set[str] | None" = None) -> None:
    """task 명을 안전한 단일 path 컴포넌트로 검증한다 — 위반 시 `InvalidTaskName`(fail-loud·must-fix).

    `task_dir(name)` 이 무검증 조인이라 traversal/절대경로/빈 문자열이 작업트리 밖에 디렉토리를
    만들고 장부를 오염시킨다(reviewer 실측 `--task ../../evil` → git-tracked `.project_manager/evil`).
    **엔진 진입점(bind_task)**에서 검증해 CLI 우회(직접 소비)까지 닫는다. mkdir·장부 write
    **이전**에 raise 한다(부작용 0). `registered_repos` 주면 `<repo>_<N>` 예약도 거부(primitive
    자기완결·CLI 의 빠른 거부와 이중화·should-fix). 예약 판정 정규식은 CLI 의
    `identity_args.is_reserved_task_name` 과 동형(모듈 격리라 inline).

    **문자 도메인 = 하류 구문 표면에 맞춘 협소화**(단일 불변식·per-surface 이스케이프
    회피). 거부 순서와 표면 근거:
      - 빈/공백-only → 빈 이름.
      - **모든 whitespace**(스페이스·탭 등·선행/후행/내부 전부) → task 명은 하류 *인자 경계*다: CLI
        `--task <이름>`(shell word)·relay 재진입 프롬프트 `/pm-bootstrap --task <이름>`(slash 인자
        경계)이 공백에서 쪼개져 정체성이 파손된다(codex 실증 `my task` → 인자 경계 깨짐).
      - **괄호 `(`·`)`** → log 헤더 태그 `(task:<이름>)`(pm_handoff `_TASK_TAG_PREFIX`)의 delimiter 다:
        `)` 포함 명은 태그를 조기 종료시켜 파서(`task:[^)]+`)의 bound_session 매칭이 어긋나 task 연속성
        추론/본문 추출이 파손된다(codex 실증 `foo)bar` → 태그 `(task:foo)bar)` 조기 매칭).
      - path separator(`/`·`\\`) → 선행 `.`(숨김/`.`/`..` 상대) → 단일 컴포넌트 아님(traversal).
    한글·하이픈·언더스코어·숫자는 유지 — 어느 표면(CLI·log 태그·path)과도 무충돌."""
    if not name or not name.strip():
        raise InvalidTaskName(name, "빈 이름(공백 포함)")
    if any(ch.isspace() for ch in name):
        raise InvalidTaskName(
            name, "공백·탭 등 whitespace 불가 (CLI/relay `--task <이름>` slash 인자 경계 파손 방지)"
        )
    if "(" in name or ")" in name:
        raise InvalidTaskName(
            name, "괄호 `(`·`)` 불가 (log 헤더 태그 `(task:<이름>)` delimiter 파손 방지)"
        )
    if "/" in name or "\\" in name:
        raise InvalidTaskName(name, "path separator(`/`·`\\`) 불가 — 단일 이름이어야")
    if name.startswith("."):
        raise InvalidTaskName(name, "선행 `.` 불가(숨김/`.`/`..` 상대경로 traversal 방지)")
    if Path(name).name != name:
        raise InvalidTaskName(name, "단일 path 컴포넌트가 아님")
    if registered_repos:
        for repo in registered_repos:
            if re.match(rf"^{re.escape(repo)}_\d+$", name):
                raise InvalidTaskName(name, f"슬롯 세션 예약 패턴(<{repo}>_<N>·⑥)")


def task_dir(name: str) -> Path:
    """task 서술 공간 `.local/tasks/<name>/` 경로 (pm_state.md·메타). 기계 상태는 장부·서술만 여기."""
    return TASKS_DIR / name


def task_pm_state_file(name: str) -> Path:
    """task 연속성 단일 앵커 `.local/tasks/<name>/pm_state.md` 경로."""
    return task_dir(name) / "pm_state.md"


def _task_pm_state_template_file() -> Path:
    """task state seed 원천 — 호출 시점 REPO를 따라 hermetic 재배선과 실제 실행을 함께 지원."""
    return REPO / ".project_manager" / "wiki" / "pm_state.template.md"


def _render_initial_task_pm_state(template_text: str, date_str: str) -> str:
    """공용 pm_state template을 task 첫 세션용 state로 렌더한다.

    공용 template의 예시 `1차`는 완료된 세션 기록이 아니다. task 생성 시 그대로 복사하면 첫
    bootstrap이 2차로 오인하므로, 세션 entry·이전 차 포인터를 비우고 명시 marker를 둔다.
    `pm_handoff.update_session_window`가 첫 핸드오프 때 이 marker를 실제 1차 entry로 교체한다.
    """
    rendered = template_text.replace("{{DATE}}", date_str)
    rendered = rendered.replace(
        "`pm_state.template.md` 에서 `pm-init` 이 생성.",
        "`pm_state.template.md` 에서 task 생성/재개 시 자동 생성.",
        1,
    )
    anchor = "## 세션 식별 (현재까지 사용된 이름)"
    start = rendered.find(anchor)
    if start < 0:
        raise ValueError(f"pm_state template에 세션 식별 앵커가 없다: {anchor}")
    next_header = re.search(r"^##(?!#) ", rendered[start + len(anchor):], re.MULTILINE)
    end = (
        len(rendered)
        if next_header is None
        else start + len(anchor) + next_header.start()
    )
    section = rendered[start:end]
    first_entry = re.search(r"^  - \*\*\d+차\*\*.*(?:\n|$)", section, re.MULTILINE)
    if first_entry is not None:
        section = (
            section[:first_entry.start()]
            + TASK_PM_STATE_EMPTY_MARKER
            + "\n"
            + section[first_entry.end():]
        )
    else:
        session_list = "최근 N 차 (sliding window, 기본 3 차):"
        marker_at = section.find(session_list)
        if marker_at < 0:
            raise ValueError(f"pm_state template에 세션 목록 앵커가 없다: {session_list}")
        insert_at = marker_at + len(session_list)
        section = (
            section[:insert_at]
            + "\n"
            + TASK_PM_STATE_EMPTY_MARKER
            + section[insert_at:]
        )
    section = re.sub(
        r"^  - \*\*\d+차\*\*.*(?:\n|$)",
        "",
        section,
        flags=re.MULTILINE,
    )
    section = re.sub(
        r"^  - 이전 차 .*?(?:\n|$)",
        "",
        section,
        flags=re.MULTILINE,
    )
    return rendered[:start] + section + rendered[end:]


def ensure_task_pm_state(name: str) -> Path:
    """task pm_state를 즉시 보장한다 — 신규 생성·구 task 재개 모두 같은 불변식.

    task는 슬롯 0개여도 독립 정체성이므로 state 생성은 slot alloc/bind나 첫 handoff까지 미루지
    않는다. 이미 있으면 byte 불변 no-op. 없으면 tracked template을 task 첫 세션 형태로 렌더해
    원자 교체한다. template 부재/손상은 task 장부만 생기는 반쪽 상태를 허용하지 않고 fail-loud.
    """
    _validate_task_name(name)
    target = task_pm_state_file(name)
    if target.exists():
        return target
    template = _task_pm_state_template_file()
    if not template.exists():
        raise FileNotFoundError(
            f"task pm_state template 부재: {template} — task state를 만들 수 없다"
        )
    rendered = _render_initial_task_pm_state(
        file_lock.read_text_shared(template, encoding="utf-8"),
        datetime.date.today().isoformat(),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8", newline="\n")
    file_lock.atomic_replace(tmp, target)
    return target


def slot_pm_state_file(slot: str) -> Path:
    """slot 연속성 앵커 `.local/slots/<repo>_<N>/pm_state.md` 경로.

    lease 표준형(`work/<repo>_<N>`)과 handoff 표준형(`<repo>_<N>`)을 같은 canonical
    디렉토리로 모은다. `pm_handoff._slots_root() / slot / "pm_state.md"`와 동형이다.
    """
    normalized = _normalize_slot(slot)
    return LOCAL_DIR / "slots" / Path(normalized).name / "pm_state.md"


def _load_pm_handoff_for_slot_state():
    """slot state 경로·마이그레이션의 단일 진실인 ``pm_handoff``를 fail-loud 로 로드한다."""
    path = Path(__file__).resolve().with_name("pm_handoff.py")
    if not path.exists():
        raise RuntimeError(
            "pm_handoff.py 부재 — slot pm_state 마이그레이션 계약을 해소할 수 없다"
        )
    # 로드/검증 실패는 그대로 fail-loud. 복구 경계가 아니므로 broad catch로 감싸 엔진
    # fail-soft 경계를 늘리지 않는다.
    module = _load_module_from_path(
        path,
        "pm_handoff.py",
        verifier=_verify_engine_rev,
    )
    # hermetic 호출부가 worktree_pool.REPO를 재배선해도 handoff의 호출시점 경로 함수들이
    # 같은 프로젝트를 보게 한다. explicit slot을 넘기므로 lease/areas 자동해소는 타지 않는다.
    module.REPO = REPO
    return module


def _ensure_slot_pm_state_locked(
    slot: str,
    lease: "Lease | None",
    task_names: set[str],
) -> Path:
    """락 안의 lease 스냅샷으로 slot-mode pm_state를 보장한다."""
    normalized = _normalize_slot(slot)
    target = slot_pm_state_file(normalized)
    if target.exists():
        return target

    if (
        lease is None
        or lease.slot != normalized
        or lease.state != "leased"
        or lease.role != "work"
        or not lease.session
        or lease.session in task_names
    ):
        return target

    handoff = _load_pm_handoff_for_slot_state()
    migrated = Path(handoff._migrate_legacy_pm_state(normalized))
    if migrated != target:
        raise RuntimeError(
            "slot pm_state 경로 drift — "
            f"worktree_pool={target}, pm_handoff={migrated}"
        )
    if target.exists():
        return target

    template = _task_pm_state_template_file()
    if not template.exists():
        raise FileNotFoundError(
            f"slot pm_state template 부재: {template} — slot state를 만들 수 없다"
        )
    rendered = _render_initial_task_pm_state(
        file_lock.read_text_shared(template, encoding="utf-8"),
        datetime.date.today().isoformat(),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8", newline="\n")
    file_lock.atomic_replace(tmp, target)
    return target


def ensure_slot_pm_state(slot: str) -> Path:
    """slot-mode lease의 pm_state를 즉시 보장한다.

    발동 술어는 session 문자열의 모양이 아니라 장부 정체성이다. ``leased`` 상태의 일반 작업
    슬롯 중 session이 있고, 그 session이 task 장부 이름이 아닌 lease만 slot-mode다. 따라서
    ``_default_session()``이 env/기존 lease/local.conf/host-pid 중 무엇을 내도 slot state를 만들고,
    task 소유·readonly(무소유) 슬롯은 거짓 앵커를 만들지 않는다.

    canonical 파일이 없으면 템플릿보다 먼저 ``pm_handoff._migrate_legacy_pm_state``를 호출해
    handoff와 똑같은 bare 슬롯 dir backfill → legacy 이동 체인을 탄다. 그래도 없을 때만 tracked
    template을 첫 세션 형태로 렌더해 원자 교체한다. 이미 있으면 byte 불변 no-op이고 template
    부재/손상은 fail-loud 한다.
    """
    normalized = _normalize_slot(slot)
    with _lease_lock():
        lease = next(
            (item for item in _read_ledger_strict() if item.slot == normalized),
            None,
        )
        task_names = {task.name for task in _read_tasks_strict()}
        return _ensure_slot_pm_state_locked(normalized, lease, task_names)


def _read_tasks() -> list[Task]:
    """장부의 `tasks` 컬렉션을 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    rows = _read_ledger_raw().get("tasks", [])
    if not isinstance(rows, list):
        return []
    return [Task.from_dict(d) for d in rows if isinstance(d, dict) and d.get("name")]


def _read_tasks_strict() -> list[Task]:
    """task 컬렉션 strict 조회 — 장부 손상/읽기 오류/잘못된 행을 예외로 전파한다."""
    rows = _read_ledger_raw_strict().get("tasks", [])
    if not isinstance(rows, list):
        raise ValueError("worktree lease 장부의 'tasks' 값이 list가 아님")
    tasks: list[Task] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not row.get("name"):
            raise ValueError(f"worktree lease 장부의 tasks[{index}] 값이 유효한 task object가 아님")
        tasks.append(Task.from_dict(row))
    return tasks


def _write_tasks(tasks: list[Task]) -> None:
    """`tasks` 컬렉션만 교체해 장부를 atomic replace 로 쓴다 — 형제 `leases`·미지 키 보존(top-level
    round-trip). **_lease_lock 보유 전제**."""
    # `_write_ledger`와 같은 최종 방어: 손상 원본을 빈 dict로 축약한 뒤 tasks만 쓰는
    # destructive rewrite를 허용하지 않는다.
    data = _read_ledger_raw_strict()
    data["tasks"] = [t.to_dict() for t in tasks]
    _write_ledger_raw(data)


def list_tasks() -> list[Task]:
    """전 task 레코드 (조회 전용·부작용 0). pm_update 활성 pid 스캔 등 단일 파일 스캔 소비."""
    with _lease_lock():
        return _read_tasks()


def find_task(name: str) -> "Task | None":
    """이름으로 task 레코드를 찾는다 — 없으면 None."""
    with _lease_lock():
        for t in _read_tasks():
            if t.name == name:
                return t
    return None


def find_task_strict(name: str) -> "Task | None":
    """이름으로 task를 strict 조회한다 — 장부 손상/읽기 오류는 예외로 전파한다."""
    with _lease_lock():
        for task in _read_tasks_strict():
            if task.name == name:
                return task
    return None


def bind_task(name: str, *, pid: "int | None" = None,
              registered_repos: "list[str] | set[str] | None" = None) -> tuple[Task, str, "int | None"]:
    """task 를 신규/resume 바인딩한다 — 명 검증(must-fix) + 동시 세션 거부 + 서술 디렉토리 신설.

    반환 `(task, action, reclaimed_from_pid)` — action ∈ {"created", "resumed", "reclaimed"}:
      - **created**  — 장부에 없던 task 를 신규 생성(prefix=None·기본 없음·pid=내 pid·슬롯 0개 시작).
      - **resumed**  — 기존 task 를 재개(경고 없음·clean): 내 pid(같은 세션·crash 전 나) **또는**
        미점유(pid 0/None — 핸드오프가 정상-종료로 pid 를 비워 둠·`release_task_pid`).
      - **reclaimed**— 기존 task 의 pid 가 **죽은 채 잔존**(핸드오프 없이 crash·pid>0)해 회수 후 진입.
        `reclaimed_from_pid` = 회수한 이전 pid(>0 이면 loud notice 대상·아래 경계 참조).
        created/resumed 는 None. (정상 인계=미점유는 resumed 로 분류돼 crash 경고를 받지 않는다.)

    **명 검증(must-fix)**: `_validate_task_name` 을 mkdir·장부 write **이전**에 돌려 traversal/절대경로/
    빈 이름/예약패턴(`registered_repos` 주면)을 fail-loud(`InvalidTaskName`) — 엔진층 배치
    0357 의 직접 소비도 우회 못 한다.

    **동시 세션 거부의 실효 경계**: 기록 pid 가 **살아있고 내 pid 와 다르면**
    `TaskActiveElsewhere`(부트스트랩이 dump 이전에 거부). 그러나 **기록 pid = 부트스트랩 헬퍼
    프로세스**라(dump 후 즉사) alive 거부의 실효 창은 **두 부트스트랩이 동시에 도는 순간뿐**이다 —
    이후 두 번째 창이 같은 task 를 열면 pid 가 죽어 `reclaimed` 로 통과한다. 이는 슬롯 lease pid 와
    **동일 semantics**(슬롯의 실 보호는
    session 명[phase0]이지만 task 는 두 창이 같은 이름이라 pid 가 유일 판별자)다. 크로스플랫폼
    프로세스 조상 추적은 과설계라 **비채택** — 대신 `reclaimed_from_pid>0` 이면 호출부(부트스트랩)가
    **loud notice**("다른 창이 아직 작업 중일 수 있다")를 surface 한다(감지=기계·해소=사용자·0단계
    미기록 질의와 동형). 차단 아님(crash 재개가 다수 케이스).

    `_pid_alive` 는 `reclaim_stale` 과 같은 생존 primitive(동형·신설 0). prefix 는 여기서 안 만진다
    — 생성 시 None·재개 시 기존 값 유지(변경 = `task prefix`). 신규/재개 모두
    `ensure_task_pm_state`로 `.local/tasks/<name>/pm_state.md`를 즉시 보장한다. task state는
    slot 유무와 무관한 연속성 앵커이며 첫 handoff까지 생성을 미루지 않는다.
    """
    _validate_task_name(name, registered_repos)   # mkdir·장부 write 이전 fail-loud(부작용 0).
    pid = os.getpid() if pid is None else pid
    with _lease_lock():
        tasks = _read_tasks_strict()
        existing = next((t for t in tasks if t.name == name), None)
        if existing is None:
            task = Task(name=name, prefix=None, pid=pid, started=_now_utc())
            ensure_task_pm_state(name)
            tasks.append(task)
            _write_tasks(tasks)
            return task, "created", None
        # 동시 세션 거부 — 기록 pid 가 살아있고 내가 아니면(다른 창) 거부.
        if existing.pid and existing.pid != pid and _pid_alive(existing.pid):
            raise TaskActiveElsewhere(name, existing.pid)
        # 구 엔진에서 만들어져 task 디렉토리만 있고 state가 없는 레코드도 재개 시 즉시 backfill.
        # state 보장 실패 시 pid/장부를 갱신하지 않아 반쪽 resume을 만들지 않는다.
        ensure_task_pm_state(name)
        # 진입 분류(pid 를 내 것으로 갱신):
        #   - same pid(내 재개) 또는 **미점유(pid 0/None=정상 인계·핸드오프가 두고 감)**
        #     → resumed(경고 없음·clean). 정상 종료 후 재개는 dead-pid 회수가 아니라 clean resume 이다.
        #   - 죽은 pid(>0 잔존=핸드오프 없이 crash) → reclaimed + reclaimed_from(loud notice).
        # 미점유를 resumed 로 재분류하는 게 핵심 — 정상 인계 상시 crash 경고를 없애 진짜
        # 경보(pid>0 잔존)만 남긴다(구분이 목적·산 pid 거부·죽은 pid 회수 거동은 불변).
        if existing.pid == pid or not existing.pid:
            action, reclaimed_from = "resumed", None
        else:
            action, reclaimed_from = "reclaimed", existing.pid  # 죽은 이전 pid(>0=loud notice·crash)
        existing.pid = pid
        _write_tasks(tasks)
        return existing, action, reclaimed_from


def set_task_prefix(name: str, prefix: "str | None") -> "Task | None":
    """task 레코드의 board prefix 를 지정/변경/해제한다 — 갱신된 `Task`·task 부재 시 `None`.

    `pm-config task prefix <이름> <p|none>` 의 write 백엔드다 — 장부 top-level `tasks` 레코드
    의 `prefix` 필드를 `prefix`(문자열=지정/변경·None=해제) 로 덮는다. **중간 변경 자유**
    (task 종속 없음) — bind_task 는 prefix 를 안 만지고(생성=None·재개=유지) 변경은 여기 단일
    지점이다. `board.py new --task <이름>` 이 `identity_args.task_prefix` 로 이 값을 read 해
    해소(명시 `--prefix` > task 설정 > 기본 없음)에 쓴다.

    **장부 IO 는 이 모듈이 단일 소유**(직접 JSON read/write 금지·flock/스키마) — `_lease_lock`
    아래 `_read_tasks`/`_write_tasks`(형제 `leases`·미지 top-level 키 무손실 round-trip)로 atomic 하게
    갱신한다. task 부재면 `None`(호출부가 rc1 안내) — 생성은 단일 지점이라 여기서
    task 를 만들지 않는다.

    **명 검증(must-fix)**: `_validate_task_name` 을 장부 write **이전**에 돌려 traversal/절대경로/빈
    이름을 fail-loud(`InvalidTaskName`) — write-capable 엔진 진입점이라 CLI 우회(직접 소비)도 닫는다.
    prefix *형식* sanity(`[a-z0-9_]`·`none` 예약)는 CLI 입력측(pm_config)이 소비 grammar
    단일 진실(`board._validate_prefix`)로 선검증한다 — 여기선 저장만(board.py new 가 신뢰)."""
    _validate_task_name(name)   # 장부 write 이전 fail-loud(부작용 0·must-fix).
    with _lease_lock():
        tasks = _read_tasks_strict()
        target = next((t for t in tasks if t.name == name), None)
        if target is None:
            return None
        target.prefix = prefix
        _write_tasks(tasks)
        return target


def release_task_pid(name: str) -> "Task | None":
    """task handoff intent — 장부 task 레코드 `pid=0`(미점유) 세팅.

    pm_handoff task 모드가 log 쓰기 전 load-bearing released/occupied 경계로 호출한다.
    task 장부 pid 는 dump 후 즉사하는 bootstrap subprocess pid라, 핸드오프가 intent를 기록하지
    않으면 **정상 인계 후 재개도** dead-pid →
    `bind_task` 가 `reclaimed`(crash 회수 loud notice)로 상시 오탐한다. 종료 시 pid 를
    0(미점유)으로 비워 두면 다음 부트스트랩이 clean `resumed`(경고 없음)로 재개한다 — 진짜 crash
    (핸드오프 없이 죽어 pid>0 잔존)만 회수 경고를 받게 구분한다. (슬롯 lease 재스냅·session
    end="두고 간다")의 task 축 짝. task end(`end_task`)와는 별개 — end 는 레코드 제거·아카이브,
    이건 세션 인계라 레코드는 유지하고 pid 만 비운다.

    반환 = 갱신된 `Task`. task 부재면 primitive는 `None`을 반환하지만, load-bearing
    pm_handoff 호출부는 이를 log 무접촉 fail-loud로 올린다. 신규 상태/필드 없이 기존
    "미점유" 값(pid=0)을 재사용하는 최소 변경.

    **장부 IO 는 이 모듈이 단일 소유**(직접 JSON 금지·flock/스키마) — `_lease_lock` 아래
    `_read_tasks`/`_write_tasks`(형제 `leases`·미지 top-level 키 무손실 round-trip)로 atomic 갱신한다.

    **명 검증**: `_validate_task_name` 을 장부 write **이전**에 돌려 traversal/
    절대경로/빈 이름을 fail-loud(`InvalidTaskName`) — 형제 write 진입점(`set_task_prefix`·`end_task`)과
    동형 방어. 예약패턴(`<repo>_<N>`)은 생성 관심사라 여기선 registered_repos 미요구(end_task 동형·
    종료엔 path-safety 만 필요·session 은 이미 장부에 있음)."""
    _validate_task_name(name)   # 장부 write 이전 fail-loud(부작용 0·must-fix).
    with _lease_lock():
        tasks = _read_tasks_strict()
        target = next((t for t in tasks if t.name == name), None)
        if target is None:
            return None
        target.pid = 0             # 미점유 표기 = 정상-종료(다음 재개=clean resumed·bind_task 재분류).
        _write_tasks(tasks)
        return target


# 종료된 task 서술 폴더의 아카이브 루트 — `.local/tasks/_ended/`. 선행 `_` 라 `_validate_task_name`
# 이 실 task 명으로는 거부하므로(path 컴포넌트 규칙) 아카이브 하위와 실 task 가 절대 충돌하지 않는다.
_ENDED_DIR_NAME = "_ended"


def slots_for_task(name: str) -> list[Lease]:
    """이 task(session==name) 명의로 **leased** 인 슬롯 리스트 (조회 전용·부작용 0).

    슬롯↔task 연결 = `lease.session == task 이름` — alloc `--task <이름>` 이 session=이름 으로
    슬롯을 리스하므로(직교 정체성이 lease 의 session 축에 실린다). release `--task` 소유검사와
    `end_task` 의 dirty 검사·일괄 반납 대상이 이걸 본다. idle/미점유 슬롯은 session 이 비어 제외.
    """
    with _lease_lock():
        return [l for l in _read_ledger() if l.state == "leased" and l.session == name]


def slots_for_task_strict(name: str) -> list[Lease]:
    """``slots_for_task``의 안전 게이트용 strict 변형.

    실제 빈 장부/보유 0개는 ``[]``이지만 JSON 손상·읽기 오류·스키마 손상은 예외로 전파한다.
    일반 조회 소비자는 기존 fail-soft ``slots_for_task``를 계속 사용한다.
    """
    with _lease_lock():
        leases = [
            lease
            for lease in _read_ledger_strict()
            if lease.state == "leased" and lease.session == name
        ]
    # 이 값은 bootstrap에서 cwd로 직접 이어져 git/pytest를 실행한다. 장부 유입도 caller 입력과
    # 같은 canonical 경계를 통과시켜 절대경로/traversal/repo 불일치가 REPO 밖 cwd가 되는 것을 막는다.
    for lease in leases:
        slot = lease.slot
        repo = lease.repo
        if not isinstance(slot, str) or _SLOT_ID_RE.fullmatch(slot) is None:
            raise ValueError(
                f"task {name!r} 보유 슬롯 식별자 {slot!r} 형식 오류 — "
                "`work/<repo>_<N>` 상대 형식만 허용한다"
            )
        slot_repo, separator, slot_number = slot[len("work/"):].rpartition("_")
        if (
            not isinstance(repo, str)
            or not separator
            or not slot_number.isdigit()
            or slot_repo != repo
        ):
            raise ValueError(
                f"task {name!r} 보유 슬롯 {slot!r}의 repo {repo!r}가 "
                "슬롯 식별자의 repo와 일치하지 않음"
            )
    return leases


class EndTaskResult:
    """`end_task` 결과 — 반납/이동 요약 또는 dirty 거부.

    `dirty` 가 비어있지 않으면 **거부**(아무것도 반납/이동하지 않음) — 호출부가 목록을 보이고
    사용자 정리를 요구한다. 비어있으면 `released`(idle 반납한 슬롯)·`moved_to`(아카이브 목적지·
    서술 폴더 부재 시 None)가 결과다. dataclass 미사용 — Lease 등과 동일(forward-ref 회피).
    """

    def __init__(self, name: str, *, released: list[str], dirty: list[str],
                 moved_from: "Path | None", moved_to: "Path | None"):
        self.name = name
        self.released = released      # idle 로 반납한 슬롯 식별자(정렬)
        self.dirty = dirty            # dirty 라 거부 사유가 된 슬롯 식별자(정렬·비어있으면 성공)
        self.moved_from = moved_from  # 이동 전 서술 폴더(.local/tasks/<name>/·부재면 None)
        self.moved_to = moved_to      # 아카이브 목적지(.local/tasks/_ended/<name>-<날짜>/·부재면 None)

    @property
    def refused(self) -> bool:
        return bool(self.dirty)

    def __repr__(self) -> str:
        return (f"EndTaskResult(name={self.name!r}, released={self.released!r}, "
                f"dirty={self.dirty!r}, moved_to={self.moved_to!r})")


def _archive_dest(name: str) -> Path:
    """종료 task 서술 폴더의 이동 목적지 `.local/tasks/_ended/<name>-<UTC날짜>/`.

    같은 날 같은 이름 재종료 시 충돌하면 `-2`·`-3`… 로 유일화한다(덮어써 기록 유실 방지). 날짜는
    UTC `YYYYMMDD`(`_now_utc` 와 같은 시계·표시만 날짜 단위). 이동이라 삭제-위임 원칙 무저촉이고,
    이름 재사용 시 옛 pm_state 를 resume 처럼 오인하는 조용한 오염을 막는다.
    """
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    ended_root = TASKS_DIR / _ENDED_DIR_NAME
    dest = ended_root / f"{name}-{date}"
    n = 2
    while dest.exists():
        dest = ended_root / f"{name}-{date}-{n}"
        n += 1
    return dest


def end_task(name: str, *, git_runner: GitRunner | None = None) -> EndTaskResult:
    """task 를 종료한다 — 보유 슬롯 dirty 검사 → (clean 이면) 일괄 idle 반납 + 장부 제거 + 서술 폴더 아카이브 이동.

    **claimed 티켓 소진 게이트는 여기서 보지 않는다** — board 스캔은 board 소유(pm_config 가
    board 로드로 선-검사하고, 통과 시에만 이 함수를 부른다·import 격리). 이 함수는
    worktree/장부/서술 폴더만 다룬다:

      1. `slots_for_task(name)` 보유 슬롯 중 **dirty** 가 하나라도 있으면 → 거부(`EndTaskResult.
         refused`·released/moved 없음·아무 부작용 0). 사용자 정리 후 재시도.
      2. 전부 clean → 보유 슬롯을 일괄 **idle 반납**(worktree 폴더는 유지·삭제 안 함·release 와
         동일 전이: session/pid 비우고 git 스냅 정리) → 장부의 task 레코드 제거 → 서술 폴더
         `.local/tasks/<name>/` 를 `_ended/<name>-<날짜>/` 로 **이동**(삭제 아님).

    **명 검증**: `_validate_task_name` 을 장부 write·`shutil.move`
    **이전**에 돌려 traversal/절대경로/빈 이름을 fail-loud(`InvalidTaskName`) — `bind_task` 와 동형의
    엔진 진입점 방어다. 무검증이면 `end_task("../evil")` 이 `_archive_dest` 파생 후 `.local/tasks` 밖으로
    `shutil.move` 한다. 예약패턴(`<repo>_<N>`)은 생성
    시점(bind_task/cmd_alloc)에서 걸리는 **생성 관심사**라 여기선 registered_repos 를 요구하지 않는다
    (종료엔 path-safety 만 필요·session 은 이미 장부에 있음). dirty 검사·반납·장부/task write 는 **한
    `_lease_lock` 안에서** 직렬화한다(release/reclaim 동형·부분상태 차단). 폴더 이동은 락 밖(fs op·장부
    무관). git_runner 주입으로 hermetic.
    """
    _validate_task_name(name)   # 장부 write·shutil.move 이전 fail-loud
    with _lease_lock():
        leases = _read_ledger_strict()
        # task 컬렉션도 어떤 lease/fs 변경보다 먼저 strict로 스냅샷한다. 뒤늦게 손상을 발견하면
        # 슬롯은 이미 idle로 썼는데 task 레코드는 남는 반쪽 end가 되므로 선검증이 필수다.
        tasks = _read_tasks_strict()
        owned = [l for l in leases if l.state == "leased" and l.session == name]

        # 1) dirty 검사 — 하나라도 dirty 면 거부(부작용 0·장부 미변경).
        dirty: list[str] = []
        for lease in owned:
            path = slot_path(lease.slot)
            if path.exists() and _is_dirty(path, git_runner=git_runner):
                dirty.append(lease.slot)
        if dirty:
            return EndTaskResult(name, released=[], dirty=sorted(dirty),
                                 moved_from=None, moved_to=None)

        # 2a) 전부 clean → 보유 슬롯 일괄 idle 반납(worktree 유지·release 동형 전이).
        released: list[str] = []
        for lease in owned:
            lease.state = "idle"
            lease.session = ""
            lease.pid = 0
            lease.git = None    # release 와 동일 — idle 슬롯은 활성 git 기대 없음(다음 alloc 재스냅).
            lease.bound = False  # task 종료 = bind 점유 종료 — 사람 bind 마커 해제(release 동형 lifecycle).
            released.append(lease.slot)
        if released:
            _write_ledger(leases)

        # 2b) 장부의 task 레코드 제거(같은 락·형제 leases 는 위에서 이미 반영됨).
        _write_tasks([task for task in tasks if task.name != name])

    # 2c) 서술 폴더 이동(락 밖·fs op) — 부재면 이동 없음(장부만 정리된 task·graceful).
    src = task_dir(name)
    dest: "Path | None" = None
    if src.exists():
        dest = _archive_dest(name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
    return EndTaskResult(name, released=sorted(released), dirty=[],
                         moved_from=src if dest is not None else None, moved_to=dest)


def alloc(
    repo: str,
    *,
    branch: str | None = None,
    resume: str | None = None,
    session: str | None = None,
    owner_task: str | None = None,
    git_runner: GitRunner | None = None,
) -> Lease:
    """repo 슬롯을 리스하고 slot-mode 정체성의 pm_state를 보장한다.

    - **idempotent** — 이 세션(session)이 이 repo 에 이미 leased 슬롯을 갖고 있으면 그걸
      반환한다(get-or-create-my-lease). branch 가 주어지고 슬롯의 live HEAD 와 다르면 같은
      슬롯에서 재체크아웃한다(리스 유지·슬롯=브랜치-무관 컨테이너·git=진실).
    - **branch/resume 우선 re-alloc** — resume(또는 branch)으로 *이전 작업스트림*의 슬롯을
      찾으면(슬롯 live HEAD 가 그 브랜치) 같은 슬롯을 re-alloc 한다(회전 연속성·dirty 파일 보존 재부착).
    - **idle 슬롯 리스** — 위에 안 걸리면 idle 슬롯을 leased 로 전이(필요 시 branch checkout).
    - **풀 소진 → `NeedsCreate`** — idle 슬롯이 없으면 raise(호출부 bootstrap 사용자 게이트).

    **task-명의 alloc (`owner_task` 주어짐)**: 항상 **신규 idle 슬롯을 대여**한다 —
    idempotent 1경로를 건너뛴다. 같은 task 가 같은 repo 슬롯을 이미 보유해도 조용히
    그 슬롯을 재반환(silent aliasing)하지 않고 다른 idle 슬롯(min-number)을 대여해 **같은 repo
    복수 보유**(병렬 dev 격리 등)를 자연 지원한다. lease.session 은 owner_task 로 기록된다(직교
    정체성) — 그 task-명의 lease 의 reclaim/재부착 보호는 `bound` 마커가 아니라 **tasks 장부 조인**이
    담당한다(session ∈ tasks 장부 → 회수/재부착 제외
    레이션 0). 슬롯-세션 도착 alloc(부트스트랩·`owner_task=None`)은 현행 idempotent 유지.

    진입 시 `reclaim_stale` 을 먼저 호출해 pid 죽은 슬롯을 회수한다(풀 가용성 회복·task 소유 슬롯은
    tasks 장부 조인으로 제외). `branch` 와 `resume` 은 동의어 역할(둘 다 작업스트림 식별) — 명시된 쪽을 쓴다.
    """
    # task-명의 alloc이면 session 은 task 이름이다(항상 신규 idle 대여).
    task_alloc = owner_task is not None
    sess = owner_task if task_alloc else (session or _default_session())
    target_branch = branch if branch is not None else resume

    # alloc 진입 시 stale 회수 (풀 가용성 회복).
    reclaim_stale(git_runner=git_runner)

    with _lease_lock():
        leases = _read_ledger_strict()
        # task 소유 슬롯 집합 — session ∈ tasks 장부인 leased 슬롯은 branch/
        # resume 재부착 대상에서도 제외한다(reclaim_stale 과 동형·task 소유 단일 진실=tasks 장부).
        # 슬롯-세션 alloc(owner_task=None)이 branch 매칭으로 타 task 소유 슬롯을 탈취하는 것을 막는다.
        task_names = {t.name for t in _read_tasks_strict()}

        def _finalize(lease: Lease, *, write_ledger: bool) -> Lease:
            if not task_alloc:
                # (a) pm_state를 lease 장부 최종 확정 전에 만들어 template 실패 시 장부를 호출 전 상태로 둔다.
                _ensure_slot_pm_state_locked(lease.slot, lease, task_names)
            if write_ledger:
                _write_ledger(leases)
            return lease

        # 1) idempotent — 이 세션의 기존 leased 슬롯 (같은 repo). **task-명의 alloc 은 이 경로를
        #    건너뛴다**(항상 신규 대여) — 멱등이 기존 슬롯을 신규처럼 반환하는 silent
        #    같은 repo 복수 보유를 지원한다. 슬롯-세션 도착 alloc 만 idempotent.
        for lease in leases:
            if task_alloc:
                break
            if lease.repo == repo and lease.state == "leased" and lease.session == sess:
                # 슬롯이 이미 target_branch 인가 = 슬롯 worktree 의 live HEAD 로 판정(
                # git=진실·저장 복사본 미사용). 아니면 재체크아웃(git 이 권위).
                ledger_changed = False
                if (target_branch is not None
                        and current_branch(lease.slot, git_runner=git_runner) != target_branch):
                    # checkout 실패면 raise — git=진실이므로 부분 실패 시 호출부에 위임.
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                    # 브랜치 재배치가 일어난 경우만 arrival 스냅 갱신 + 장부 write(base 보존).
                    # 순수 재진입(브랜치 동일)은 상태 변화가 없어 스냅/write 를 생략한다(재진입 비용 0).
                    _apply_git_snapshot(lease, git_runner=git_runner)
                    ledger_changed = True
                return _finalize(lease, write_ledger=ledger_changed)

        # 2) resume/branch 우선 re-alloc — 같은 작업스트림(브랜치)의 슬롯 재부착(연속성).
        #    **task-명의 alloc 은 이 경로도 건너뛴다**(항상 신규 idle 대여) — owner_task 에
        #    branch/resume 이 함께 와도(API 상 가능) 기존 leased 슬롯의 session/pid 를 덮어 재부착하면
        #    "항상 신규 idle 대여"가 깨지고 타 작업스트림 lease 를 탈취한다. idle
        #    경로(3)만 태워 신규 슬롯을 대여하거나 소진 시 NeedsCreate 한다.
        if target_branch is not None and not task_alloc:
            for lease in leases:
                # provisional("creating")은 재부착 대상에서 제외한다 (should-fix). worktree add
                # 성공 후~submodule init 전 SIGKILL 로 남은 creating orphan 은 worktree 가 이미 그
                # 브랜치를 체크아웃 중이라 live HEAD 로 매칭되는데, 이를 조용히 leased 로 재부착하면
                # (a) reconcile 의 incomplete surface 를 우회하고 (b) submodule 미초기화 슬롯을
                # leased 로 넘긴다. creating 은 reconcile/prune 경로로만 정리(설계 의도=surface+정리).
                if lease.state == "creating":
                    continue
                # 사람 bind 슬롯(bound)은 branch-매칭 재부착 대상에서 제외한다. `alloc(repo,
                # branch=X)`/`resume=X` 가 그 브랜치를 체크아웃 중인 *타 세션 bound lease* 를 만나면
                # session/pid/bound 를 덮어 사람 bind 정체성을 탈취한다(핵심 목표가 이 경로에서 깨짐).
                # 같은 세션 재진입은 1경로 idempotent 가 이미 처리하므로 여기서 bound=skip 이 안전
                # (bound 슬롯은 명시 release/재bind 로만 소유가 바뀐다·codex must-fix). **task 소유 슬롯
                # (session ∈ tasks 장부)도 동일 근거로 제외**한다 — 슬롯-세션 alloc(branch=X)이 그 브랜치를
                # 체크아웃 중인 task-명의 lease 를 탈취하는 것을 막는다
                # task lease 통합·bound 부재 구장부도 자동 보호).
                if lease.bound or lease.session in task_names:
                    continue
                # 이 슬롯이 target_branch 를 체크아웃 중인가 = live HEAD 로 매칭(저장 필드 아님·
                # 드리프트 불가능 — git 이 단일 진실.
                if (lease.repo == repo
                        and current_branch(lease.slot, git_runner=git_runner) == target_branch):
                    # checkout 을 먼저 — 실패하면 raise 해 in-memory lease·장부 모두 미변경(기존 리스 보존).
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                    lease.state = "leased"
                    lease.session = sess
                    lease.pid = os.getpid()
                    lease.started = _now_utc()
                    lease.bound = False  # pool 재부착 — 사람 bind 마커 해제(task 소유는 tasks 장부가 진실).
                    _apply_git_snapshot(lease, git_runner=git_runner)  # arrival 스냅(기대 baseline·base 보존).
                    return _finalize(lease, write_ledger=True)

        # 3) idle 슬롯 리스 — **최소 번호 우선**(결정론 ⓒ·번호 안정). 같은 repo 의 idle
        #    후보를 슬롯 번호 오름차순으로 정렬해 최소 가용 번호부터 대여한다(대여 중 불변·반납 후
        #    재사용). 옛 코드는 장부 파일 순서(대개 생성순이나 remove+재생성 후 뒤섞일 수 있음)로
        #    첫 idle 을 골라 번호가 비결정적이었다 — 정렬로 못박아 alloc CLI 의 "최소 번호 대여"를
        #    보장한다. 비-숫자 tail 슬롯(드문 커스텀)은 뒤로 밀어 숫자 슬롯 우선(`_slot_number`).
        idle_for_repo = sorted(
            (l for l in leases if l.repo == repo and l.state == "idle"),
            key=lambda l: _slot_number(l.slot),
        )
        for lease in idle_for_repo:
            # **위험차단 (must-fix)**: worktree 물리 부재 슬롯은 리스하지 않는다. stale
            # 엔트리(worktree dir 삭제/prune)가 force_release 등으로 idle 이 되면, 이 재사용
            # 루프가 *없는 worktree* 를 leased 로 넘겨 이후 코드가 깨진다(codex 실측). fs 존재
            # 가드는 **실경로(git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는
            # 슬롯 존재를 모델링하는 권위라 건너뛴다(current_branch/slot_status 동형 규율). 부재
            # 슬롯은 skip → 다음 후보/NeedsCreate(dangling idle 을 leased 로 승격하지 않음).
            if git_runner is None and not slot_path(lease.slot).exists():
                continue
            # 슬롯이 이미 target_branch 가 아니면 재체크아웃(live HEAD 비교
            # ). git 이 브랜치를 만든다 — 장부엔 branch 를 쓰지 않는다.
            if (target_branch is not None
                    and current_branch(lease.slot, git_runner=git_runner) != target_branch):
                # checkout 을 먼저 — 실패하면 raise(idle 슬롯 상태 보존·부분 leased 전이 차단).
                _checkout_required(lease.slot, target_branch, git_runner=git_runner)
            lease.state = "leased"
            lease.session = sess
            lease.pid = os.getpid()
            lease.started = _now_utc()
            # pool 대여 — 사람 bind 마커 해제. task-명의(sess=task 이름)의 reclaim/재부착 보호는 bound 가
            # 아니라 tasks 장부 조인이 담당한다.
            lease.bound = False
            _apply_git_snapshot(lease, git_runner=git_runner)  # arrival 스냅(기대 baseline·base 보존).
            return _finalize(lease, write_ledger=True)

        # 4) 풀 소진 — idle 슬롯 없음. 새 슬롯 생성은 fs 행위라 사용자 게이트(호출부).
        raise NeedsCreate(repo)


def release(
    slot: str,
    *,
    require_clean: bool = True,
    owner_task: str | None = None,
    git_runner: GitRunner | None = None,
) -> Lease:
    """슬롯을 반납한다 — 작업완료 시. idle 로 전이한 Lease 반환.

    - **dirty + require_clean=True → `ReleaseRefused`** — 수동 정리 요구(작업 유실 방지).
    - **require_clean=False(자동경로) → dirty 면 stash 보존 후 idle 화** — 자동화에서 막힘 방지.
    - **owner_task 주면 소유검사** — 그 슬롯의 leased session 이 owner_task 와 다르면
      `NotTaskOwner`(다른 task 의 슬롯 반납 차단). 검사는 dirty 판정보다 먼저(내 것이 아니면
      dirty 여부를 볼 이유가 없다). `--task` 미지정(owner_task=None·slot-only 백스톱)은 안 탄다.

    슬롯은 idle 로 전이(재사용 컨테이너로 풀에 반납)하고 session/pid 를 비운다 —
    worktree 폴더 자체는 유지(다음 리스가 재사용·remove 는 force_release/수동).
    """
    with _lease_lock():
        leases = _read_ledger_strict()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")

        # readonly 공유 슬롯은 반납(idle 화) 대상이 아니다 — 무소유 공유 자산이라
        # release 하면 idle 이 돼 alloc 이 work 슬롯으로 점유(role 유실). 보유 중인 lease.role 을 직접
        # 검사한다(`_reject_readonly_mutation`/`_slot_role` 은 lock 재취득 → non-reentrant 데드락).
        if target.role == _LEASE_ROLE_READONLY:
            raise ReadonlySlotNotLeasable(slot, "release")

        # task 소유검사 — dirty/stash 어떤 부작용보다 먼저. 내 task 명의(session)가 아니면
        # 아무것도 바꾸지 않고 raise(다른 세션 작업을 실수로 idle 화하지 않음).
        if owner_task is not None and target.session != owner_task:
            raise NotTaskOwner(slot, owner_task, target.session)

        path = slot_path(slot)
        if path.exists() and _is_dirty(path, git_runner=git_runner):
            if require_clean:
                raise ReleaseRefused(slot)
            _stash(path, git_runner=git_runner)  # 자동경로 — dirty 를 stash 보존.

        target.state = "idle"
        target.session = ""
        target.pid = 0
        target.git = None    # release 시 정리 — idle 슬롯은 활성 git 기대가 없다(다음 alloc 이 재스냅).
        target.bound = False  # 명시 반납 = bind 점유 종료 — 사람 bind 마커 해제(git=None 동형 teardown).
        _write_ledger(leases)
        return target


def force_release(slot: str, *, git_runner: GitRunner | None = None) -> Lease | None:
    """수동 백스톱 — dirty/leased 여부 무시하고 슬롯을 강제로 idle 화.

    dirty 면 stash 로 보존은 시도하되(작업 유실 최소화) 거부하지 않는다. 장부에 슬롯이
    없으면 None 반환(이미 정리됨·무해). `pm-config release --force` 백스톱의 엔진 진입점.
    """
    with _lease_lock():
        leases = _read_ledger_strict()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        # readonly 공유 슬롯은 강제 반납도 대상이 아니다 — 무소유
        # 공유 자산이라 idle 화하면 alloc 이 work 슬롯으로 점유(role 유실). 보유 lease.role 직접 검사.
        if target.role == _LEASE_ROLE_READONLY:
            raise ReadonlySlotNotLeasable(slot, "force-release")
        path = slot_path(slot)
        if path.exists() and _is_dirty(path, git_runner=git_runner):
            _stash(path, git_runner=git_runner)  # 강제라도 작업은 보존 시도.
        target.state = "idle"
        target.session = ""
        target.pid = 0
        target.git = None    # release 시 정리(force 백스톱도 동일·다음 alloc 이 재스냅).
        target.bound = False  # 강제 반납도 bind 점유 종료 — 사람 bind 마커 해제(release 동형).
        _write_ledger(leases)
        return target


def remove_slot(
    slot: str,
    *,
    force: bool = False,
    git_runner: GitRunner | None = None,
) -> "RemoveResult | None":
    """슬롯을 *통째로* 제거한다 — worktree remove + 전용 브랜치 정리 + 장부 엔트리 삭제 (원자·user-invoked).

    `create_slot` 의 역연산 — 슬롯 lifecycle 에서 *제거 본체*가 수동 `git worktree remove`
    위임이던 gap 을 닫는다(PM 69 footgun 체인: 수동 remove → dangling 장부 → `add` 가 번호
    skip → 뒤늦은 prune). 이 한 커맨드가 리스 확인 → dirty 검사 → `git worktree remove`
    (+ `git worktree prune`) → 슬롯 전용 브랜치 정리 → 장부 엔트리 제거를 원자로 한다.

    **삭제-위임 원칙(사용자 명시 호출 전제)** — PM 이 자율 실행하지 않는다(호출부 CLI 가
    사용자 명시 `pm-config worktree remove <slot>`). `prune-stale`(worktree 부재 장부만 정리)과
    달리 실 worktree 를 지운다. orphan worktree(장부 미등록)는 여전히 `git worktree remove`.

    원자 시퀀스:
      리스/장부 확인 — 엔트리 없으면 `None` 반환(무해 종료·이미 정리됨·orphan 은 위 참조).
      활성 리스 확인 — `state != "idle"`(leased/creating·사용 중)이면 `RemoveRefused`
         ("active-lease")·`force=True` 로만 무시(정석은 `release` 먼저·override 시 원래 state 를
         `RemoveResult.forced_state` 에 실어 CLI 가 강제-회수 경고). **dirty 검사** — dirty 면
         `RemoveRefused`("dirty")·`force=True` 면 stash 보존 후 강제. **stash 실패(rc≠0)** 또는
         **stash 후에도 여전히 dirty**(submodule 내부 변경 등 top-level stash 가 못 담는 잔존)면
         제거를 중단(`RuntimeError`·장부/worktree/브랜치 미변경) — 어느 경우도 `worktree remove
         --force` 로 날리는 작업 유실을 막는다.
      `git worktree remove [--force]`(+ `git worktree prune`) — `.repos/<repo>.git` bare
         컨텍스트(공유 .git 원). 실패(rc≠0)면 `RuntimeError`(장부/브랜치 미변경·단
         force+dirty 는 stash 가 이미 생성돼 있을 수 있음 — 작업은 보존됨).
      슬롯 전용 브랜치(`<repo>_<N>`) 정리 — 슬롯이 그 전용 브랜치를 체크아웃 중이면
         `git branch -d`(머지 완료 시에만 삭제·미머지면 rc≠0 로 거부=보존·작업 유실 방지).
         공유/다른 브랜치(main 등)면 삭제 자체를 스킵. detached 면 판별 불가(none).
      장부 엔트리 제거 — `add` 가 빈 번호를 **재사용**(번호 skip footgun 종결·이 티켓 핵심).

    ⚠️ **미머지-보존 브랜치 상호작용**: 미머지라 전용
    브랜치 `<repo>_<N>` 를 보존하면, 나중에 같은 번호의 슬롯을 branch-무지정 경로(base-경로 `-b
    <repo>_<N>` 또는 else-경로 path-basename 자동 `-b`)로 재생성할 수 없다(그 브랜치가 이미 존재).
    create_slot 은 이제 이를 **선-검출해 fail-loud**(`SlotBranchExists`·오귀인 orphan-worktree 진단
    제거) — 정확한 원인(브랜치 잔존) + 두 갈래(그 브랜치 정리[머지/삭제] 후 새 슬롯 재생성 /
    그 브랜치를 checkout 해 재개)를 안내한다. 재개는 수동 `git worktree add <path> <repo>_<N>` 또는
    `create_slot(branch=<repo>_<N>)` — **둘 다 리셋 없이 보존 커밋 tip 에서 checkout**
    create_slot branch-경로의 `-B` create-or-reset 데이터-유실을 존재 브랜치 checkout 분기로 닫음).
    데이터 유실 없음(현재도 loud 실패였다).

    **fs 존재 가드는 실경로(git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는
    슬롯 상태를 모델링하는 권위라 건너뛴다(`alloc` idle-reuse 동형). 슬롯 전용 브랜치명은
    슬롯 식별자 tail(`<repo>_<N>`)로, `git worktree add` 가 슬롯 경로 basename 으로 자동 판
    브랜치와 정합한다(create_slot 결정). `cmd_release`/`cmd_worktree_prune_stale` 패턴대로
    `git_runner` 주입으로 실 git 없이 배선 검증(DI seam).
    """
    with _lease_lock():
        leases = _read_ledger_strict()
        target = next((l for l in leases if l.slot == slot), None)
        # 리스/장부 확인 — 엔트리 없으면 무해 종료(orphan 은 git worktree remove·prune-stale).
        if target is None:
            return None

        # 활성 리스 확인 — leased/creating(사용 중·in-flight)은 force 로만(release 먼저가 정석).
        # force override 면 원래 활성 state 를 실어 CLI 가 강제-회수 경고를 낸다(reviewer should-fix).
        forced_state = target.state if target.state != "idle" else None
        if forced_state is not None and not force:
            raise RemoveRefused(slot, "active-lease", state=forced_state)

        path = slot_path(slot)
        # fs 존재 가드는 실경로(git_runner 미주입)에서만 — 주입 runner 는 슬롯 상태를 모델링
        # (alloc idle-reuse 동형). 실경로 worktree 부재(이미 사라짐)면 remove 를 건너뛰고 장부만 정리.
        real_path_missing = git_runner is None and not path.exists()

        # dirty 검사 — dirty 면 거부(작업 유실 방지)·force 면 stash 보존 후 강제.
        stashed = False
        if not real_path_missing and _is_dirty(path, git_runner=git_runner):
            if not force:
                raise RemoveRefused(slot, "dirty")
            # force — dirty 를 stash 보존. **stash 실패(rc≠0)면 제거를 중단**(작업 유실 방지·codex
            # must-fix): 아직 아무것도 안 지웠으므로 장부/worktree/브랜치 미변경으로 raise 한다.
            # 이 가드가 없으면 stash 못 뜬 dirty 변경을 `worktree remove --force` 가 날린다.
            rc_s, out_s = _stash(path, git_runner=git_runner)
            if rc_s != 0:
                raise RuntimeError(
                    f"git stash failed for {slot!r} (rc={rc_s}, out={str(out_s).strip()[:200]!r}) — "
                    "dirty 변경을 보존하지 못해 제거를 중단한다(작업 유실 방지·장부/worktree/브랜치 "
                    "미변경). 수동으로 정리/커밋 후 재시도하라."
                )
            stashed = True
            # **stash 성공 후 재검사**: top-level
            # `git stash push --include-untracked` 는 **submodule 내부 변경을 담지 못한다**. worktree
            # 풀 슬롯은 submodule init 을 하므로, stash rc0 라도 dirty submodule 작업이
            # stash 를 빠져나가 `worktree remove --force` 로 유실될 수 있다. submodule 전용 감지 대신
            # *일반 불변식* 으로 — stash 후에도 여전히 dirty 면(=stash 가 못 담는 변경 잔존) 제거를
            # 중단한다(장부/worktree/브랜치 미변경·stash 는 이미 생성됐을 수 있음).
            if _is_dirty(path, git_runner=git_runner):
                raise RuntimeError(
                    f"slot {slot!r} 이 stash 후에도 dirty — stash 로 보존 못 하는 변경 잔존"
                    "(submodule 내부 변경 가능). 제거를 중단한다(작업 유실 방지·장부/worktree/브랜치 "
                    "미변경·stash 는 이미 생성됐을 수 있음). 해당 worktree 에서 수동 정리(커밋/push) "
                    "후 재시도하라."
                )

        # 슬롯 전용 브랜치 판별은 worktree remove *전에* — 제거 후엔 슬롯 HEAD 조회 불가.
        branch = current_branch(slot, git_runner=git_runner)

        bare = bare_repo_path(target.repo)
        bare_runner = git_runner or _real_git_runner(bare)

        # git worktree remove (+ prune) — bare 컨텍스트(공유 .git 원·_rollback_worktree
        # 동형). force 면 --force(dirty stash 후·submodule/locked 강제). 실패는 raise — 장부/브랜치를
        # 손대기 전이라 미변경(단 force+dirty 는 위 stash 가 이미 생성돼 있을 수 있음·작업은 보존됨).
        # worktree 부재(real_path_missing)면 remove 스킵·prune 만.
        if not real_path_missing:
            remove_argv = ["worktree", "remove", str(path)] + (["--force"] if force else [])
            rc, out = bare_runner(remove_argv)
            if rc != 0:
                raise RuntimeError(
                    f"git worktree remove failed for {slot!r} (rc={rc}, out={out!r}) — "
                    "장부/브랜치 미변경(원자). dirty/submodule/locked 면 `--force` 로 재시도하라."
                )
        bare_runner(["worktree", "prune"])  # best-effort 등록 정리(remove 후·부재 슬롯 dangling admin).

        # 슬롯 전용 브랜치 정리 — 전용 브랜치(`<repo>_<N>`)만·머지 완료 시에만 삭제.
        dedicated = slot[len("work/"):]   # "<repo>_<N>" = 슬롯 식별자 = create_slot 전용 브랜치명.
        if branch is None:
            branch_action = "none"          # detached/조회불가 — 지울 브랜치 판별 불가.
        elif branch != dedicated:
            # 슬롯이 전용 브랜치가 아닌 공유/다른 브랜치(main 등)를 체크아웃 중 → 삭제 스킵(공유 보호).
            branch_action = "skipped-shared"
        else:
            # 전용 브랜치 — `git branch -d`(머지 완료 시에만 삭제·미머지면 rc≠0 로 거부=보존).
            # bare HEAD(=main·기본 브랜치)에 머지됐는지를 git 이 판정한다("머지 완료(main 에 포함)").
            rc_b, _out_b = bare_runner(["branch", "-d", branch])
            branch_action = "deleted" if rc_b == 0 else "preserved-unmerged"

        # 장부 엔트리 제거 — `add` 가 빈 번호를 재사용.
        leases[:] = [l for l in leases if l.slot != slot]
        _write_ledger(leases)
        return RemoveResult(
            slot=slot, branch=branch, branch_action=branch_action,
            stashed=stashed, forced_state=forced_state,
        )


def create_slot(
    repo: str,
    *,
    branch: str | None = None,
    base: str | None = None,
    session: str | None = None,
    owner_task: str | None = None,
    init_submodules: bool = True,
    git_runner: GitRunner | None = None,
    test_cmd: str | None = None,
    readonly: bool = False,
) -> Lease:
    """새 슬롯을 *생성*하고 leased 로 리스한다 — 풀 확장 (NeedsCreate 게이트 통과 후).

    **task-명의 생성 (`owner_task` 주어짐)**: 풀 소진 시 `worktree add <repo> --task
    <이름>` 이 새 슬롯을 만들고 **생성 직후 그 슬롯을** task 명의로 대여한다(min-idle 재탐색의
    오슬롯 리스크 없이 생성분 직결). lease.session=owner_task — 그 슬롯의 reclaim/재부착 보호는
    `bound` 이 아니라 **tasks 장부 조인**이 담당한다
    lease 통합·마이그레이션 0). owner_task 는 기바인딩 task 요구(cmd 층 `find_task`)라 항상 tasks
    장부에 있다. readonly 와는 상호배타(readonly=무소유 공유 슬롯). owner_task 미지정은 현행(session=
    도착 세션·pool 슬롯).

    `test_cmd` 가 주어지면 그 슬롯 리스에 회귀/빌드명령을 바인딩한다(
    amend) — 같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 등)을 가질 수 있게.
    board._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다(미지정=None·현행).

    `git worktree add` 는 fs 행위라 사용자 게이트(NeedsCreate) 통과 후에만 불린다 —
    pm-config worktree add / bootstrap 사용자 승인이 호출부. 다음을 한다:
      1. **bare 부재 가드** — `.repos/<repo>.git` 가 없으면 `BareRepoMissing` raise(multi-PM
         worktree 침묵 폴백 금지).
      2. 다음 슬롯 번호 결정(`<repo>_<N>`·기존 번호 회피).
      3. `git worktree add [-B <branch>] [-b <slot> <path> <base> | <path>]` —
         **`.repos/<repo>.git` bare 컨텍스트**에서 실행해 슬롯이 그 family repo 의 worktree 가
         되게 한다. 분기:
           - `branch` 면 그 브랜치를 create-or-reset 으로 체크아웃(`-B <branch> <path>`).
             branch 가 신규든 기존이든 한 호출로 처리(`add <path> <ref>` 는 ref 가 *기존*이어야
             해 신규 작업스트림 브랜치엔 못 씀 → `-B` 로 통일).
           - `base` 면(branch 미지정) 먼저 `git fetch origin`(best-effort) 후 슬롯 브랜치
             `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*(`--no-track -b <repo>_<N> <path>
             origin/<base>`). repo 등록 base(areas.md·`pm-config worktree add` 가 전달)에서 일관되게
             따게 한다 — bare HEAD 가 아닌 의도한 base(develop 등). fetch 실패/origin ref 미해소면
             로컬 `<base>`(동결 head) 폴백(refspec 은 origin/* 만 갱신·로컬 heads 는 동결·
             fail-soft). `--no-track` = 슬롯 브랜치에 origin/<base> upstream 자동설정 억제(슬롯=작업스트림).
           - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
      4. submodule init — `git worktree add` 는 submodule 자동 init 안 함(spike
         §8-4(d)) → `git submodule update --init --recursive --force`(슬롯 worktree cwd).
         `--force` 는 worktree+submodule edge(bare 에서 만든 fresh 슬롯)서 plain `--init` 이
         체크아웃 못 하는 상태를 강제 init — fresh 슬롯이라 잃을 로컬 변경 0.
      5. 장부에 leased 엔트리 등록.

    **`readonly=True`**: research 전용 read-only 공유 슬롯을 만든다 —
    슬롯 전용 브랜치를 파지 않고 `git worktree add --detach <path> <base sha>` 로 **detached
    HEAD** 로 만든다(`git worktree add <path> main` 은 같은 브랜치를 두 worktree 가
    점유 못 해 `fatal: 'main' is already used by worktree at …` 로 죽는다 → `--detach` 필수).
    `base` = released `main` 의 기준면(문서 검증 기준·released 지점). lease 는 `role="readonly"`·
    **session/pid 없음**(공유 자산·배타 대여 안 함)·alloc/release/reclaim 대상 제외(공유가 정상).
    base 는 스냅(`lease.git.base`)에 기록해 문서 검증 기준면을 남긴다(B축 verified_at 선행 자산).

    git_runner 가 주입되면 그 runner 로 모든 git 호출(테스트 hermetic). 미주입이면
    `.repos/<repo>.git` bare 컨텍스트의 실 git 으로 worktree add 후, 슬롯 경로 컨텍스트로
    submodule init.
    """
    # task-명의 생성이면 session=task 이름(reclaim/재부착 보호는 tasks 장부 조인이 담당·
    # bound 아님). owner_task 미지정은 현행(session=도착 세션).
    task_created = owner_task is not None
    # owner_task + readonly 는 모순(무소유 공유 자산 vs task 명의 배타 대여) — CLI 가드만 믿지 않고
    # 엔진 자체에서 fail-loud(codex suggestion·타 호출부/미래 경로 방어). 부작용(worktree add) 이전.
    if task_created and readonly:
        raise ValueError(
            "create_slot: owner_task 와 readonly 는 상호배타다 — readonly 공유 슬롯은 무소유"
            "(session/pid 없음·배타 대여 없음)라 task 명의 대여 대상이 아니다."
        )
    sess = owner_task if task_created else (session or _default_session())

    # bare 부재/무효 가드 — worktree 의 공유 .git 원이 없거나 무효면 base 가 없다.
    # 침묵 폴백(multi-PM 루트 worktree)으로 가면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해
    # 토폴로지가 깨진다 → 명시 raise 로 `pm-config repo add` 선행 안내(fail-soft 규율).
    #   1) 경로부재 → BareRepoMissing (종전·hydrate 안내).
    #   2) 경로존재 but 무효(부분/깨진 bare) → BareRepoMissing(broken=True). 중단된 clone 이
    #      남긴 부분 bare 는 exists()=True 지만 worktree add 의 base 로 못 써 날 git 에러로 죽는다
    #      (audit #1→#4). `_is_valid_bare`(rev-parse)로 실 bare 를 판정해 그 조용한 통과를 fail-loud
    #      진단으로 닫는다. 주입된 git_runner(테스트 mock)도 같은 가드를 거친다 — mock 이 rev-parse
    #      로 유효/무효 bare 를 모델링(DI seam 보존·실 git 안 탐).
    bare = bare_repo_path(repo)
    if not bare.exists():
        raise BareRepoMissing(repo, bare)
    if not _is_valid_bare(bare, runner=git_runner or _real_git_runner(bare)):
        raise BareRepoMissing(repo, bare, broken=True)

    with _lease_lock():
        leases = _read_ledger_strict()
        # 슬롯번호 = **ledger ∪ 실 git worktree** 병합 (audit #4). ledger 만 보면 orphan
        # (worktree add 성공 후 lease 기록 전 죽어 disk 엔 있으나 ledger 엔 없는 슬롯·audit #2)
        # 번호를 재사용해 `git worktree add` "already exists" 암호 에러가 난다. git worktree 실측을
        # 합쳐 orphan 번호까지 회피한다(주입 runner 존중·DI seam). git 조회 실패는 fail-soft(빈 집합).
        used = _existing_slot_numbers(repo, leases) | _git_slot_numbers(repo, git_runner=git_runner)
        n = 1
        while n in used:
            n += 1
        slot = _slot_for(repo, n)
        path = slot_path(slot)

        # **provisional lease 선기록 (중단-안전)** — worktree add *전에* `state="creating"`
        # lease 를 장부에 넣어 disk 에 흔적을 남긴다. 성공 시 `leased` 로 확정하고, 실패/예외/
        # KeyboardInterrupt 는 아래 try/except 가 fs 롤백 + provisional 제거로 청소한다. **SIGKILL
        # (kill -9)** 은 except/finally 가 안 돌지만 이 provisional 이 disk 에 남아 다음
        # status/reconcile 이 incomplete 로 surface(→ 사용자 정리) — finally-rollback 만으론 못
        # 잡던 SIGKILL 을 provisional 이 커버한다(결정 §provisional 우선). 슬롯번호도 이 엔트리로
        # 예약된다(`_existing_slot_numbers` 가 세므로 후속 create_slot 이 재사용 안 함).
        provisional = Lease(
            slot=slot, repo=repo, session=sess, pid=os.getpid(),
            started=_now_utc(), state="creating", test_cmd=test_cmd,
        )
        leases.append(provisional)
        _write_ledger(leases)

        # add 성공(=fs 에 worktree 생성) 여부 — except 청소가 롤백(remove) 대상을 판정한다. add 자체
        # 실패면 지울 worktree 가 없어 remove 를 안 부른다.
        worktree_created = False
        try:
            # 슬롯 전용 브랜치 `<repo>_<N>` 선-검출 (결정 (b)) — branch 미지정(base·else) 경로는
            # 그 브랜치를 판다(base=명시 `-b <repo>_<N>`·else=git 이 슬롯 path basename 으로 자동 `-b`).
            # 미머지-보존 브랜치(작업 유실 방지로 보존)가 잔존하면 `worktree add` 가
            # `fatal: a branch named '<repo>_<N>' already exists`(rc≠0)로 죽는다 — 슬롯번호 병합
            # (ledger∪git-worktree)은 **worktree 없이 잔존하는 브랜치**를 못 본다(브랜치 축은
            # 슬롯번호 축과 독립). 여기서 선-검출해 정확한 원인+두 갈래를 fail-loud 로 준다(오귀인
            # orphan-worktree 진단 제거). 명시 `branch=` 경로는 이 선-검출을 안 타고 아래서 존재→checkout/
            # 부재→`-B` 로 분기한다(기존 브랜치 리셋-유실 방지·명시 의도). try 안에서 raise →
            # 아래 `except` 가 provisional lease 를 롤백한다(worktree_created=False 라 롤백할 worktree 는
            # 없음·중단-안전). 검출은 `_slot_branch_exists`(color-safe `--format=%(refname:short)`·
            # splitlines 정확-일치·rc 무시 — 평문 `branch --list` 는 `color.branch=always` 서 ANSI 오염·
            # rc 기반은 주입 runner generic 폴백 rc0 오탐).
            if branch is None and not readonly:
                slot_branch = f"{repo}_{n}"
                pre_runner = git_runner or _real_git_runner(bare)
                if _slot_branch_exists(pre_runner, slot_branch):
                    raise SlotBranchExists(slot, slot_branch, base)
            # worktree add 는 `.repos/<repo>.git` bare 컨텍스트에서 — 슬롯이 그 family repo 의
            # worktree 가 되게 한다. bare repo 도 `git -C <bare> worktree add <abs
            # path>` 가 동작한다(슬롯 path 는 절대).
            #   - branch 면 존재 여부로 분기: **기존 브랜치는 checkout**
            #     (`add <path> <branch>`·그 tip 에서·**리셋 없음**)·**신규 브랜치는 `-B`**(생성).
            #     옛 코드는 신규/기존 모두 `-B`(create-or-**reset**)로 통일했는데, `-B <branch>` 는 기존
            #     브랜치를 start-point(미지정 시 bare HEAD)로 **리셋**해 미머지-보존 브랜치를 `branch=` 로
            #     명시 `branch=`
            #     는 "이 브랜치를 슬롯에" 라는 명시 의도라, 기존 브랜치를 그 tip 에서 그대로 checkout 한다
            #     (base-경로의 silent 재사용=기각한 (a) 와 달리 명시 의도라 놀람 없음). 신규 브랜치는
            #     `add <path> <newbranch>` 가 "invalid reference" 로 죽으므로 `-B`(=생성·리셋 대상 없어
            #     안전)로 판다. 이 fix 로 `SlotBranchExists` 안내의 "그 브랜치로 재개"(create_slot(branch=)
            #     또는 수동 `git worktree add <path> <repo>_<N>`)가 **둘 다 리셋 없는 안전 경로**가 된다.
            #   - base 면(branch 미지정) 먼저 `fetch origin`후 슬롯 브랜치
            #     `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*(`--no-track -b <slot> <path>
            #     origin/<base>`). 슬롯 브랜치 이름은 슬롯 식별자(`<repo>_<N>`·live-branch 정합)
            #     이고 base 만 의도한 분기점(develop 등). `add <path> <ref>` 가 아니라 `-b`(브랜치 생성)인
            #     이유: ref 만 주면 detached 거나 base 브랜치 자체에 붙어 슬롯 작업이 base 를 오염한다 →
            #     슬롯 전용 브랜치를 base 에서 새로 판다. `--no-track` = origin/<base> upstream 자동설정
            #     억제(슬롯=작업스트림). (파생 기준·fetch·--no-track 상세는 아래 base 분기 주석.)
            #   - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
            if readonly:
                # readonly 공유 슬롯 — 슬롯 전용 브랜치를 파지 않고 **detached HEAD**
                # 로 만든다. 같은 브랜치(main 등)를 두 worktree 가 점유 못 하는 git 제약을
                # detached 로 우회하고, 부수효과로 브랜치 미점유·실수 커밋이 브랜치를 안 움직여 피해가
                # 국소화된다(submodule pin=detached 모델 동형). base 를 released 기준면에서 detach:
                #   - base 미지정 → bare HEAD 에서 detach(`add --detach <path>`).
                #   - base 지정 → fetch origin(best-effort) 후 origin/<base> 해소되면 그 최신, 아니면
                #     로컬 <base>(폴백)에서 detach. base 파생 작업 슬롯의 fetch/폴백 규율과 동형.
                ref = base
                if base is not None:
                    prep_runner = git_runner or _real_git_runner(bare)
                    rc, out = prep_runner(["fetch", "origin"])
                    if rc != 0:
                        print(
                            f"[경고] `git -C {bare} fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
                            f"  {_frozen_fallback_label(base)}에서 readonly 슬롯을 detach 한다 — 네트워크 복구 후 "
                            f"`{_runtime_skill_entry('pm-worktree')} refresh` 로 최신 released tip "
                            "으로 갱신하라 (fail-soft).",
                            file=sys.stderr,
                        )
                    else:
                        rc, _ = prep_runner(
                            ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base}"]
                        )
                        if rc == 0:
                            ref = f"origin/{base}"
                add_argv = ["worktree", "add", "--detach", str(path)]
                if ref is not None:
                    add_argv.append(ref)
            elif branch is not None:
                # 기존 브랜치 → checkout(리셋 없음·보존 커밋 유지)·신규 → -B(생성).
                # `_slot_branch_exists`(color-safe `--format=%(refname:short)`·splitlines 정확-일치·
                # 위 slot-branch 선-검출과 동일 helper)로 존재를 본다: 존재하면 `add <path> <branch>`
                # (그 브랜치 tip 에서 checkout·리셋 없음)·부재하면 `-B <branch>`(신규 생성·리셋 대상 없어
                # 안전). base 파생 prep 과 별개의 capture 러너 1회(실경로 capture-프로파일 반영·회귀 갱신).
                branch_runner = git_runner or _real_git_runner(bare)
                if _slot_branch_exists(branch_runner, branch):
                    add_argv = ["worktree", "add", str(path), branch]
                else:
                    add_argv = ["worktree", "add", "-B", branch, str(path)]
            elif base is not None:
                # base 파생 — 슬롯을 *origin 최신*에서 시작한다. refspec
                # (`+refs/heads/*:refs/remotes/origin/*`)은 origin/* 만 갱신하고 로컬 `refs/heads/<base>`
                # 는 clone 시점 동결이라, 로컬 base 에서 파면 슬롯이 origin 보다 stale 하게 시작한다.
                # worktree add 전에 origin 을 fetch 하고(best-effort) `origin/<base>`(존재 시)에서 판다.
                #   - fetch 실패(오프라인)면 경고 후 로컬 `<base>` 폴백 — 슬롯 생성은 막지 않는다
                #     (worktree add 핵심 부작용 보존·`_set_bare_fetch_refspec` fail-soft 규율 정합).
                #   - 파생 기준 ref = fetch 성공 + `refs/remotes/origin/<base>` resolvable → `origin/<base>`
                #     (fetch 로 갱신된 최신), 아니면 로컬 `<base>`(폴백). 로컬 `refs/heads/<base>` 동결은
                #     손대지 않는다(pool 슬롯은 자기 슬롯 브랜치를 파므로 로컬 base 직접 체크아웃 안 함·
                #     fast-forward 는 범위 밖·리스크 회피).
                #   - **`--no-track` 필수**: 슬롯 브랜치 `<repo>_<N>` 는 새 작업 브랜치(슬롯=작업스트림)라
                #     origin/<base> tracking 을 걸지 않는다. 그런데 remote-tracking ref(origin/<base>)에서
                #     `-b` 로 브랜치를 파면 git 기본 `branch.autoSetupMerge=true` 가 그 슬롯 브랜치에
                #     upstream 을 *자동* 설정한다(A_1@{upstream}=origin/<base>·`git status`/무인자 `git pull`
                #     이 슬롯 작업을 base 에 묶음). `--no-track` 으로 그 자동설정을 억제한다(로컬 <base>
                #     폴백 경로에서도 안전 — no-op·codex 게이트 포착 결함).
                # prep(fetch/show-ref)은 **capture 러너** — fetch out 을 경고 detail 로 쓰고 rc 로 origin
                # 해소를 판정한다(짧은 로컬 op·인터랙티브 아님). worktree add *자체* 만 아래서 console-visible
                # 러너로 실행한다. 주입된 git_runner(mock)면 그대로(DI seam).
                prep_runner = git_runner or _real_git_runner(bare)
                ref = base
                rc, out = prep_runner(["fetch", "origin"])
                if rc != 0:
                    print(
                        f"[경고] `git -C {bare} fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
                        f"  {_frozen_fallback_label(base)}에서 슬롯을 판다 — 네트워크 복구 후 새 슬롯은 "
                        "origin 최신에서 시작한다 (fail-soft).",
                        file=sys.stderr,
                    )
                else:
                    rc, _ = prep_runner(
                        ["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base}"]
                    )
                    if rc == 0:
                        ref = f"origin/{base}"
                add_argv = ["worktree", "add", "--no-track", "-b", f"{repo}_{n}", str(path), ref]
            else:
                add_argv = ["worktree", "add", str(path)]

            # worktree add *자체* 는 **console-visible(인터랙티브) 러너**로 실행한다. 대형 repo 의
            # full checkout(로컬 bare→worktree·느린 디스크/VPN/Windows)이 옛 captured 120s 에 false-kill
            # 되던 블로커 해소 — 진행상황이 콘솔에 실시간 보이고 timeout 이 GIT_TIMEOUT_SECONDS(1800s·env
            # `PM_GIT_TIMEOUT`·`none`=무제한)라 관대하다. submodule 단계와 동일 패턴(_real_git_runner_
            # interactive). **DI seam 보존**: 주입된 git_runner(테스트 mock)가 있으면 그걸 쓴다(현행 테스트
            # 무영향) — 인터랙티브는 `git_runner is None` 실경로만. 인터랙티브는 `(rc, "")` 반환(출력은 콘솔로
            # 직접)이라 실패 메시지는 rc 기반 + 아래 트립 안내로 조정한다(git stderr 는 이미 콘솔에 감).
            add_runner = git_runner or _real_git_runner_interactive(bare, timeout=GIT_TIMEOUT_SECONDS)
            rc, out = add_runner(add_argv)
            if rc != 0:
                # 원인 힌트 (#4-bare) — 식별 가능한 원인을 raw git out 앞에 붙인다. 상단 가드가
                # broken bare 를 이미 걸러내지만, upfront rev-parse 는 통과했는데 objects 결손 등으로
                # worktree add 만 죽는 잔여 부분-bare(또는 op 도중 손상)를 여기서 재판정해 안내한다.
                bare_hint = ""
                if not _is_valid_bare(bare, runner=git_runner or _real_git_runner(bare)):
                    bare_hint = (
                        f"`.repos/{repo}.git` 가 유효 bare 가 아니다(부분/깨진 bare 가능성) — 수동 삭제 후 "
                        f"`pm-config repo add {repo}` 로 재hydrate 하라. "
                    )
                # orphan/already-exists 진단 (#4-충돌) — 슬롯번호를 git 병합으로 회피하지만,
                # 병합 후 나타난 orphan·수동 add 잔존이 `add` 를 "already exists" 로 죽일 수 있다.
                # out 문자열(captured/injected) 또는 실 git worktree 목록(인터랙티브는 out 이 콘솔 직행이라
                # 실측)에 이 슬롯 경로가 이미 등록됐으면 orphan 정리 경로를 안내한다.
                #
                # ⚠️ **오귀인 정정**: `git worktree add` 의 "already exists" 는 두 원인이 있다 —
                # (1) worktree **경로** 잔존(orphan·이 블록의 대상), (2) 슬롯 전용 **브랜치**(`<repo>_<N>`)
                # 잔존(`a branch named '<repo>_<N>' already exists`·미머지-보존 브랜치). 옛 코드는 "already
                # exists" 부분매치만 보아 (2)를 (1)로 **오귀인**해 orphan 정리 안내를 냈지만 지울 orphan
                # base·else 경로는 위 선-검출(SlotBranchExists)이
                # (2)를 먼저 잡지만, 여기서도 브랜치-존재 에러를 orphan 으로 낚지 않게 원인을 분리 판정한다
                # (클래스-fix·잔여/미래 경로 방어).
                out_l = str(out).lower()
                branch_exists_err = "a branch named" in out_l and "already exists" in out_l
                branch_hint = ""
                orphan_hint = ""
                if branch_exists_err:
                    branch_hint = (
                        f"슬롯 전용 브랜치 `{repo}_{n}` 가 이미 존재한다(미머지-보존 브랜치 잔존 가능성·"
                        f"remove_slot) — 그 브랜치를 정리(`git branch -d/-D {repo}_{n}`·머지) 후 "
                        f"새 슬롯 재생성하거나, 미머지 작업을 이어가려면 그 브랜치를 checkout 해 재개하라 "
                        f"(수동 `git worktree add {str(path)} {repo}_{n}` 또는 `create_slot(branch={repo}_{n})`·"
                        f"둘 다 리셋 없음). "
                    )
                else:
                    # worktree-path orphan — (브랜치 아닌) "already exists" 또는 실 git worktree 목록에 슬롯 등록.
                    already = "already exists" in out_l
                    if not already:
                        already = any(
                            w.slot == slot for w in list_git_worktrees(repo, git_runner=git_runner)
                        )
                    if already:
                        orphan_hint = (
                            f"경로 `{path}` 에 worktree 가 이미 등록돼 있다(orphan·중단된 create/수동 add "
                            f"잔존 가능성) — `pm-config status` 로 orphan/stale 을 확인하고 정리"
                            f"(worktree prune 또는 수동 삭제)하라. "
                        )
                # 트립/실패 안내 — 하네스 자동 호출이 timeout/실패로 죽었을 때 사용자에게 다음
                # 행동(터미널 직접 실행·무제한 opt-in)을 준다. rc 기반(out 은 인터랙티브면 빈 문자열).
                timeout_desc = "무제한" if GIT_TIMEOUT_SECONDS is None else f"{GIT_TIMEOUT_SECONDS}초"
                raise RuntimeError(
                    f"git worktree add failed for {slot!r} (rc={rc}, out={out!r}). {bare_hint}{branch_hint}{orphan_hint}"
                    f"매우 느린 op(대형 repo·느린 디스크/VPN)이면 timeout({timeout_desc}·PM_GIT_TIMEOUT)에 "
                    f"걸렸을 수 있다 — 먼저 사용자에게 repo {repo!r} 슬롯 생성 승인을 "
                    f"요청하라. 승인한 사용자만 터미널에서 `pm-config worktree add {repo} "
                    f"--user-ack {repo}`를 대상값에 결속해 직접 실행하면 진행상황이 보이고, "
                    f"`PM_GIT_TIMEOUT=none` 으로 무제한 실행할 수 있다(세션 자동 부착 금지)."
                )
            # add 성공 — fs 에 worktree 존재. 이후 단계(submodule)/interrupt 실패 시 except 가 이 슬롯을
            # 롤백(remove)한다. add *자체* 실패면 worktree_created=False 라 remove 를 안 부른다(범위 유지).
            worktree_created = True

            # submodule init — worktree add 는 submodule 자동 init 안 함.
            # `--force`: bare 에서 만든 fresh 슬롯의 worktree+submodule edge 에서 plain `--init` 이
            # 체크아웃 못 하는 상태(`git submodule init failed: ''` — 실 Windows multi-PM 파일럿서 빈
            # 에러로 죽음)를 강제 init 한다. create_slot 은 *새 슬롯 생성 때만* 호출되고
            # (기존 슬롯 재사용은 alloc·재init 안 함) fresh worktree 라 잃을 로컬 변경이 없으므로
            # `--force` 안전. 솔로/submodule 없는 repo 는 `--init --recursive --force` 가 no-op rc 0.
            #
            # **인터랙티브 러너**: 실경로(git_runner 미주입)에선 capture 러너 대신
            # `_real_git_runner_interactive`(stdio 콘솔 상속·SUBMODULE_TIMEOUT 3600s)로 돈다 —
            # 대형 submodule clone 이 600s 초과해 TimeoutExpired→(1,"")로 죽던 블로커 해소(진행
            # 상황 화면 표시·credential 프롬프트·대형 clone 완주). worktree add 도 같은 이유로
            # console-visible(GIT_TIMEOUT_SECONDS)이고, 짧은 captured git(status·checkout·
            # dirty·stash)만 capture 러너 그대로. **DI seam 보존**: 주입된 git_runner(테스트 mock)가
            # 있으면 그걸 쓴다(현행 테스트 무영향) — 인터랙티브는 `git_runner is None` 실경로만.
            #
            # **원자적 롤백**: 실패(rc≠0)면 raise 만 하고, 롤백(worktree remove)과
            # provisional 제거는 아래 `except` 가 단일 경로로 처리한다 — worktree add 는 *이미 성공*
            # 했으므로 그 worktree 를 지워야 댕글링("슬롯 없음"+재시도 "이미 존재")이 안 남는다. 빈 out
            # (Windows 인코딩 캡처 유실·인터랙티브는 항상 빈 out)에도 막히지 않게 rc + argv surface.
            if init_submodules:
                sub_runner = git_runner or _real_git_runner_interactive(path)
                sub_argv = ["submodule", "update", "--init", "--recursive", "--force"]
                rc, out = sub_runner(sub_argv)
                if rc != 0:
                    raise RuntimeError(
                        f"git submodule init failed for {slot!r}: "
                        f"rc={rc}, argv={sub_argv!r}, out={out!r}"
                    )

            # 성공 — provisional 을 leased 로 **확정**한다(2차 write·branch 는 장부 미저장
            # git=진실·조회는 live current_branch). provisional 을 그대로 반환(같은 필드).
            provisional.state = "leased"
            if readonly:
                # readonly 공유 슬롯 — 무소유 확정: session/pid 를 비우고 role 을 박는다.
                # state 는 "leased"(alloc idle-탐색·release 소유탐색이 session 매칭이라 무소유는 자연
                # 제외·reclaim_stale 은 role 가드로 제외). role 이 0단계 carve-out·mutation 거부의 축.
                provisional.session = ""
                provisional.pid = 0
                provisional.role = "readonly"
            # create git 스냅— base 를 아는 유일 지점이다(base_branch=base·
            # commit=방금 파생된 fresh 슬롯 tip). base 미지정(branch·else 경로)이면 base 는 미기록
            # (drift 감지는 사용자 set-base 후 활성). fail-soft — 스냅 실패가 create 를 안 막는다.
            _apply_git_snapshot(provisional, base_branch=base, git_runner=git_runner)
            _write_ledger(leases)
            return provisional
        except BaseException:
            # **중단-안전 청소** — 실패(rc≠0 raise)·예외·KeyboardInterrupt 는 여기서 단일
            # 경로로 정리한다: (1) worktree add 가 성공했으면(worktree_created) 그 worktree 를
            # `_rollback_worktree`(remove --force·best-effort) 로 지운다(add 자체 실패면 지울 게 없어
            # (2) provisional("creating") 엔트리를 장부에서 제거하고
            # 다시 쓴다(불완전 슬롯 미등록·기존 계약). 롤백은 2차 예외를 삼켜 원래 에러를 가리지 않는다.
            # (SIGKILL 은 여기 못 옴 — provisional 이 disk 에 남아 reconcile 이 incomplete 로 잡는다.)
            if worktree_created:
                _rollback_worktree(repo, path, git_runner=git_runner)
            leases[:] = [l for l in leases if l.slot != slot]
            _write_ledger(leases)
            raise


def bind_slot(slot: str, repo: str, session: str, *, git_runner: GitRunner | None = None) -> Lease:
    """슬롯을 세션에 **직접 바인딩**한다 — 사람 발의 멀티-PM 정체성 선언(lean).

    `/pm-bootstrap <repo> --slot <N>` 의 엔진 진입점. 사람이 "내가 슬롯 <N>"을 선언하면
    그 슬롯 리스를 이 세션으로 갱신(있으면) 또는 생성(없으면)한다 — **pool alloc 이 아니다**
    (풀에서 골라잡지 않는다·slot-pinned/supervise 불필요). `alloc` 의 idle-탐색/풀-소진
    `NeedsCreate`/checkout 분기 어느 것도 안 탄다(직접 바인딩).

    **`reclaim_stale` 를 절대 호출하지 않는다** — 사람 경로는 pid-회수를 하지 않는다(근원
    제거). `alloc` 은 진입 시 `reclaim_stale` 로 풀 가용성을
    회복하지만, `bind_slot` 은 슬롯을 직접 지정받으므로 회수가 필요 없다 — pid 는 정보용으로만
    기록(`os.getpid()`)하고 liveness 판정에 쓰지 않는다(명시 `release` 로만 반납).

    ⚠️ **cross-path 보호(bound 마커)**: 여기 적는 pid 는
    *ephemeral bootstrap 프로세스* pid 라 bootstrap 종료 후 죽는다. **사람 경로는 회수를 안 하지만
    (위), 타 세션 `alloc`(task alloc·relay 등)은 진입 시 `reclaim_stale` 를 부른다 — 같은
    장부를 공유하므로, 방치하면 이 bind 엔트리를 `state==leased && pid 죽음` = stale 로 오판해
    idle 화(session 비움)한다** (task `alloc` 이 타 창 세션 bind slot 을 회수). 이를
    막기 위해 bind 는 lease 에 **`bound=True` 마커**(additive·조건방출·구 장부 부재=False)를 박고
    `reclaim_stale` 은 `bound` lease 를 회수 대상에서 제외한다 — 사람 bind 정체성이 타 alloc 의
    reclaim 에 유실되지 않는다. 반납은 명시 경로(`release`/`force_release`/`pm_handoff --done`)로만
    이뤄지고 그때 마커가 해제된다(pool 재대여 slot 은 alloc 이 마커 clear·현행 pid-회수 복귀).
    한계: bound lease 는 crash(bootstrap 후 세션 crash) 후 자동 회수가 없다 — slot bind 는 0단계
    세션명 점유검사 + 재bind(직접 지정)로 자연 회복되므로 수용.

    **branch *표시* 는 건드리지 않는다** — 브랜치는 git=단일 진실이라 슬롯 worktree HEAD 에서
    live 조회(`current_branch(slot)`). bind 는 리스 장부의 점유 메타
    (session/state/started/pid)를 갱신하고, **arrival git 스냅**(`lease.git` 기대 baseline·
    branch/head/submodules·기존 `base` 보존)을 additive 로 기록한다. `git_runner`
    는 그 스냅의 DI seam — 미주입 실경로에서 슬롯 worktree 가 없으면 스냅은 fail-soft no-op(기존
    git 유지). 표시(live)와 기대(기록)는 2축이라 branch 표시 단일 진실은 그대로다.

    `_lease_lock` + `_write_ledger`(atomic) — 기존 alloc/release/set_test_cmd 와 동일한
    read-modify-write 직렬화. board.py 를 import 하지 않는다(isolation·touches 격리).
    갱신/생성된 Lease 를 반환한다.
    """
    with _lease_lock():
        leases = _read_ledger_strict()
        task_names = {task.name for task in _read_tasks_strict()}
        target = next((l for l in leases if l.slot == slot), None)
        # readonly 공유 슬롯은 바인딩(점유) 대상이 아니다 — bind 는 *점유*고
        # 0단계 carve-out은 *조회 지칭*만 허용한다(의미 불일치). readonly 를 무조건 leased 로 덮으면
        # role 이 유실되고 무소유 공유 자산이 배타 점유된다. `/pm-bootstrap --slot N` 오지정 방어(엔진
        # 불변식). 보유 lease.role 직접 검사(lock 재취득 데드락 회피). target None(신규)은 work 로 생성.
        if target is not None and target.role == _LEASE_ROLE_READONLY:
            raise ReadonlySlotNotLeasable(slot, "bind")
        if target is None:
            # 없으면 새 Lease append (직접 바인딩 — 풀 탐색/생성 게이트 없음). bound=True 로 사람
            # bind 마커를 박아 이후 alloc 의 reclaim_stale 이 즉사 pid 로 오판·회수하지 못하게 한다.
            target = Lease(
                slot=slot,
                repo=repo,
                session=session,
                pid=os.getpid(),
                started=_now_utc(),
                state="leased",
                bound=True,
            )
            leases.append(target)
        else:
            # 있으면 점유 메타만 갱신 (branch·test_cmd 는 보존). reclaim 안 거침. bound 마커도 박는다
            # (기존 슬롯 재bind — pool 대여였던 슬롯을 사람이 직접 점유로 승격).
            target.repo = repo
            target.session = session
            target.state = "leased"
            target.pid = os.getpid()
            target.started = _now_utc()
            target.bound = True
        # arrival git 스냅(기대 baseline·기존 base 보존·fail-soft — 슬롯 부재면 no-op).
        _apply_git_snapshot(target, git_runner=git_runner)
        # (a) pm_state를 lease 장부 최종 확정 전에 만들어 template 실패 시 bound/session까지 미변경으로 둔다.
        _ensure_slot_pm_state_locked(target.slot, target, task_names)
        _write_ledger(leases)
        return target


def list_leases() -> list[Lease]:
    """현재 리스 장부 전체를 읽어 반환한다 (조회·진단용·pm-config status)."""
    with _lease_lock():
        return _read_ledger()


# ── git worktree × 장부 정합 (reconcile·중단-안전 슬롯번호) ────────────────
# 장부(Lease)는 우리 메타(session/pid/test_cmd)이고, 실제 worktree 는 git 이 소유한다
# (`.repos/<repo>.git` bare 의 worktree 목록). create_slot 이 worktree add 성공
# 후 lease 기록 전에 죽으면 둘이 어긋난다(orphan·audit #2). git worktree 목록을 *실-git 소스*
# 로 삼아 (a) 슬롯번호를 orphan 까지 회피(#4)하고 (b) status 가 drift 를 surface(#3)한다.


def _slot_from_worktree_path(path_str: str) -> "str | None":
    """worktree 절대경로 → 슬롯 식별자 `work/<repo>_<N>` (WORK_DIR 하위 단일 컴포넌트만).

    슬롯은 WORK_DIR(=REPO/work) 바로 아래의 단일 디렉토리(`work/<repo>_<N>`)다. bare 원·multi-PM
    루트·중첩 경로는 슬롯이 아니므로 None(슬롯번호/reconcile 에서 제외). 심링크(/tmp 등)는 양쪽을
    `resolve()` 로 정규화해 장부 `slot_path`(REPO/slot) 파생과 일관 매칭한다.
    """
    try:
        p = Path(path_str).resolve()
        rel = p.relative_to(WORK_DIR.resolve())
    except (ValueError, OSError):
        return None
    if len(rel.parts) != 1:
        return None
    return f"work/{rel.parts[0]}"


def _parse_worktree_porcelain(out: str) -> list[GitWorktree]:
    """`git worktree list --porcelain` 출력을 GitWorktree 리스트로 파싱한다.

    porcelain 포맷 = 엔트리별 속성 라인(각 줄 `worktree <path>`·`HEAD <sha>`·`branch
    refs/heads/<name>`·`detached`·`bare`·`locked`/`prunable` 등) + 빈 줄 구분. `worktree` 라인이
    새 엔트리 시작이다. `branch` 는 `refs/heads/` 프리픽스를 벗겨 브랜치명만. 인식 못 하는 속성
    라인은 무시(견고). 빈 출력/`worktree` 라인 전 잡음은 무시.
    """
    entries: list[GitWorktree] = []
    cur: "dict | None" = None

    def _flush() -> None:
        if cur is None:
            return
        entries.append(GitWorktree(
            path=cur["path"],
            slot=_slot_from_worktree_path(cur["path"]),
            branch=cur["branch"],
            detached=cur["detached"],
            bare=cur["bare"],
        ))

    for raw in out.splitlines():
        line = raw.rstrip()
        if line.startswith("worktree "):
            _flush()
            cur = {"path": line[len("worktree "):].strip(),
                   "branch": None, "detached": False, "bare": False}
        elif cur is None:
            continue
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        # HEAD/locked/prunable/기타 속성은 무시(reconcile 판정에 불요).
    _flush()
    return entries


def _discover_bares() -> "list[tuple[str, Path]]":
    """`.repos/*.git` 에서 (repo, bare 경로) 목록 — reconcile 의 전-repo 열거원.

    REPOS_DIR 부재면 빈 리스트(fail-soft). repo 이름 = `<name>.git` 의 stem(`.git` 제거).
    """
    if not REPOS_DIR.exists():
        return []
    out: "list[tuple[str, Path]]" = []
    for p in sorted(REPOS_DIR.glob("*.git")):
        out.append((p.name[:-len(".git")], p))
    return out


def list_git_worktrees(
    repo: "str | None" = None, *, git_runner: GitRunner | None = None,
) -> list[GitWorktree]:
    """`git worktree list --porcelain` 로 실 git worktree 를 열거한다 — 장부 대조 소스.

    `repo` 지정 → 그 repo 의 bare(`.repos/<repo>.git`) 컨텍스트 1개만. `repo=None` → `.repos/` 의
    모든 bare 를 순회(전-repo·reconcile 용). 각 bare 에서 `git -C <bare> worktree list --porcelain`
    을 **capture 러너**(`_real_git_runner`)로 실행한다 — 출력을 파싱해야 하므로 인터랙티브(출력이
    콘솔 직행·빈 out)를 쓰지 않는다. 짧은 로컬 read-only op 라 유한 timeout 으로 충분.

    **fail-soft**: git 부재·rc≠0·예외는 그 bare 를 건너뛴다(빈 기여·크래시 0·rc≠0 로 위임). **DI
    seam 보존**: 주입 runner(테스트 mock)는 모든 bare 에 재사용(hermetic — porcelain 을 모델링·실
    git 안 탐). 반환은 GitWorktree 리스트(bare 엔트리 포함·slot=None) — 호출부(reconcile/슬롯번호)가
    non-bare 슬롯만 필터한다.
    """
    if repo is not None:
        bares = [(repo, bare_repo_path(repo))]
    else:
        bares = _discover_bares()
    result: list[GitWorktree] = []
    for _r, bare in bares:
        runner = git_runner or _real_git_runner(bare)
        try:
            rc, out = runner(["worktree", "list", "--porcelain"])
        except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 흡수(그 bare 건너뜀).
            continue
        if rc != 0:
            continue
        result.extend(_parse_worktree_porcelain(out))
    return result


def _git_slot_numbers(repo: str, *, git_runner: GitRunner | None = None) -> set[int]:
    """실 git worktree 목록에서 이 repo 의 슬롯 번호 집합 (orphan 포함·슬롯번호 충돌 회피).

    `_existing_slot_numbers`(ledger 기준)와 **병합**해 create_slot 이 orphan(ledger 미등록·중단된
    create/수동 add 잔존) worktree 번호와 충돌하지 않게 한다 — `git worktree add` "already exists"
    암호 에러(audit #4) 근절.
    """
    nums: set[int] = set()
    prefix = f"work/{repo}_"
    for w in list_git_worktrees(repo, git_runner=git_runner):
        if w.bare or not w.slot or not w.slot.startswith(prefix):
            continue
        tail = w.slot[len(prefix):]
        if tail.isdigit():
            nums.add(int(tail))
    return nums


def reconcile_worktrees(*, git_runner: GitRunner | None = None) -> ReconcileResult:
    """리스 장부 × `list_git_worktrees` 대조로 drift 를 판정한다 — 조회 전용·부작용 0.

    - **orphan** = git worktree(슬롯 경로·non-bare)인데 장부에 없음 — 중단된 create/수동 add 잔존
      (audit #2). 다음 create_slot 번호 충돌(#4)·status blind(#3)의 근원.
    - **stale** = 장부 확정 리스(leased/idle)인데 대응 git worktree 없음 — worktree dir 삭제/prune
      (audit #3).
    - **incomplete** = provisional("creating") 리스 — worktree add 후 확정 전 중단된 create 흔적
      (SIGKILL 커버). 별도 카테고리라 stale 과 이중 계상하지 않는다.

    **부작용 없음**(삭제/이동/prune 안 함) — surface + 복구 안내만(자동삭제는 사용자 위임·파일
    삭제 원칙). 실 git 실패는 `list_git_worktrees` 가 fail-soft(빈 리스트)라 그 repo 확정 리스가
    보수적으로 stale 로 degrade 할 수 있으나 advisory·조회 전용이라 무해.
    """
    leases = list_leases()
    git_wts = list_git_worktrees(git_runner=git_runner)
    slot_wts = [w for w in git_wts if w.slot and not w.bare]
    lease_slots = {l.slot for l in leases}
    git_slots = {w.slot for w in slot_wts}
    orphans = [w for w in slot_wts if w.slot not in lease_slots]
    incomplete = [l for l in leases if l.state == "creating"]
    stale = [l for l in leases if l.state != "creating" and l.slot not in git_slots]
    return ReconcileResult(orphans=orphans, stale=stale, incomplete=incomplete)


def prune_stale_leases() -> list[str]:
    """worktree 가 **확정 부재**인 dangling 장부 엔트리를 제거한다 — user-invoked 안전 cleanup.

    reconcile 이 surface 한 stale/incomplete 중 **worktree dir 이 물리적으로 사라진**(`slot_path
    (slot).exists()` False) 엔트리를 장부에서 삭제한다. 제거된 슬롯 식별자 리스트를 반환한다.

    **왜 안전한가 (삭제-위임 원칙 위반 아님)**: 이건 *사용자 데이터/worktree 삭제*가 아니라 이미
    사라진 worktree 의 **dangling 부기 정리**다 — 지울 파일이 없다(dir 부재가 전제). 그래서:
      - orphan **worktree**(git 측·disk 에 존재·작업이 있을 수 있음)는 **손대지 않는다** — 그건
        `git worktree remove <path>` 로 사용자가 판단해 지운다(status reconcile 이 안내).
      - worktree 가 **존재**하는 leased/idle/creating 엔트리도 손대지 않는다(dir 존재 → prune 안 함).
        (leased 재사용·incomplete 정리는 release/사용자 몫.)

    조회-전용 reconcile 과 분리한 **명시 user-invoked 프리미티브**(`pm-config worktree prune-stale`)라
    status 는 여전히 부작용 0 다. `_lease_lock` + `_write_ledger`(atomic·다른 장부 op 와 동일 직렬화).
    fs 존재 판정뿐이라 git 불요(hermetic).
    """
    pruned: list[str] = []
    with _lease_lock():
        leases = _read_ledger_strict()
        kept = [l for l in leases if slot_path(l.slot).exists()]
        pruned = [l.slot for l in leases if not slot_path(l.slot).exists()]
        if pruned:
            _write_ledger(kept)
    return pruned


# `git symbolic-ref HEAD` 의 브랜치 full ref 접두 — `refs/heads/<name>`. 이 접두 **정확히**를
# 제거해야 순수 브랜치명이 된다(모호성 접두가 붙는 `--short` 대신 full ref 를 읽는 이유).
_SYMREF_BRANCH_PREFIX = "refs/heads/"


def current_branch(slot: str, *, git_runner: GitRunner | None = None) -> str | None:
    """슬롯 worktree 의 git HEAD 에서 현재 브랜치를 **live** 로 읽는다.

    `git symbolic-ref HEAD` → `refs/heads/<name>` 에서 `refs/heads/` 접두를 정확히 제거해 브랜치명.
    브랜치가 git 의 단일 진실 — 장부에 저장된 복사본이 아니라 슬롯 worktree 의 실제 HEAD 를 매번
    조회한다(사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영·드리프트 불가능).

    **`symbolic-ref HEAD`(full ref·`--short` 없이)를 쓰는 이유**:
    `rev-parse --abbrev-ref HEAD` 는 (a) detached 를 `"HEAD"` 문자열로, (b) **unborn 브랜치**(아직
    커밋 0 인 새 브랜치)를 rc≠0 에러로 줘서 — *이름이 있는* unborn 브랜치를 detached/조회불가로
    오판한다(→ "(미지정)"). `symbolic-ref HEAD` 는 unborn 브랜치도 `refs/heads/<name>` 을 rc=0 으로
    주고, detached 일 때만 "ref HEAD is not a symbolic ref" 로 rc≠0 이라 — "현재 브랜치명 or 브랜치
    아님"의 정석 primitive 다(git=진실). **`--short` 를 뺀 이유**:
    `--short` 는 브랜치명과 같은 이름의 태그가 있으면(릴리즈가 `v1.3.0` 브랜치를 그대로 `v1.3.0`
    태그로 찍은 경우) 모호성 회피로 `heads/v1.3.0` 을 돌려줘 순수 브랜치명이 아니었다 — 장부 기록
    (`v1.3.0`)과 불일치해 부트스트랩 0단계가 가짜 "외부 개입" FAIL-LOUD 로 차단됐다.
    full ref 는 태그 존재와 무관하게 항상 `refs/heads/<정확한 브랜치명>` 이라 모호성 자체가 없고,
    `refs/heads/` 접두만 정확히 벗기면 진짜 이름이 `heads/x` 인 브랜치(합법·`heads/x` → 그대로 보존)
    도 오인하지 않는다(`--short` 후 `heads/` strip 은 이 브랜치를 `x` 로 오인한다·codex must-fix).

    `None` 반환(전부 fail-soft·예외 raise 금지·표시층이 "(detached/조회불가)" 등으로 변환):
      - detached HEAD — `symbolic-ref` 가 rc≠0(symbolic ref 아님).
      - git 호출 실패 (rc≠0) — 손상/락/git 부재 등.
      - `refs/heads/` 로 시작 안 하는 이상 출력 — 보수적으로 None(브랜치 아님).
      - 슬롯 경로 부재 — worktree 폴더가 아직 없거나 지워짐.

    `git_runner` 미주입 시 실경로는 `_real_git_runner(slot_path(slot))` 로 해소한다
    (기존 DI seam 패턴 — 테스트는 mock runner 주입으로 hermetic·실 git 불요). **슬롯 경로
    부재 가드는 실경로(미주입)에서만 본다** — git_runner 주입 시엔 그 runner 가 존재/rc/HEAD
    를 전부 모델링하는 권위이므로 fs 가드를 건너뛴다(hermetic 테스트가 슬롯 폴더 없이 동작).
    주입 runner 가 예외를 던져도 None 으로 흡수한다(docstring 의 "raise 금지" 규칙을 DI seam
    까지 보장 — 실 `_real_git_runner` 는 이미 예외를 (1, str) 로 감싸므로 실경로는 영향 없음).
    """
    runner = git_runner
    if runner is None:
        path = slot_path(slot)
        if not path.exists():
            return None
        runner = _real_git_runner(path)
    try:
        rc, out = runner(["symbolic-ref", "HEAD"])
    except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 None(규칙: raise 금지).
        return None
    if rc != 0:  # detached(symbolic ref 아님)·git 부재/실패 → 브랜치 없음.
        return None
    ref = out.strip()
    if not ref.startswith(_SYMREF_BRANCH_PREFIX):  # 이상 출력(브랜치 아님) → 보수적 None.
        return None
    return ref[len(_SYMREF_BRANCH_PREFIX):] or None


def slot_status(slot: str, *, git_runner: GitRunner | None = None) -> SlotStatus:
    """슬롯 worktree 의 상태(branch + upstream + submodule 역할)를 live 로 읽는다.

    부트스트랩이 현재 슬롯 상태를 1회 surface 하는 backbone.
    판별을 재사용**한다(중복 구현 금지):
      - `current_branch(slot)` — 브랜치(live·`symbolic-ref HEAD` full ref).
      - `_upstream_status` — `@{upstream}` 해소 여부.
      - `_submodule_statuses` — `git submodule status`(`_parse_submodule_entries`) + submodule 당
        `git -C <sub> symbolic-ref -q HEAD`(on-branch/detached) + `_submodule_dirty` 로 역할 판정
        (`_resync_submodules_selective` 와 *같은* primitive). detached & pin≠working=drift(경고)·
        on-branch=dev-ahead(정보)·detached & pin==working=pinned.

    **전부 fail-soft — 예외 raise 안 함**(형제 backbone `current_branch`·`_resync_submodules_
    selective`·`_rollback_worktree` 와 같은 규율·미래 호출부[on-demand 전체-풀 status·sync]가
    이 계약에 기댄다): `current_branch` 는 자체 흡수. upstream/submodule 은 rc≠0 이면 미해소/
    빈목록이고, `git_runner` 자체가 **예외를 던져도** 본문 try/except 로 흡수한다(upstream 미해소·
    submodule 빈목록으로 보수적 degrade — 각각 독립 흡수라 한쪽 실패가 다른쪽을 안 가린다).
    실 `_real_git_runner` 는 이미 예외를 (1, str) 로 감싸 rc≠0 경로로 오지만, 주입 runner
    (DI seam·테스트/미래 호출부)가 raise 해도 이 계약이 지켜진다. `git_runner` 미주입 시 슬롯
    worktree 바인딩 `_real_git_runner(slot_path(slot))` 로 해소(hermetic 테스트는 mock 주입).
    슬롯 경로 부재 가드는 **실경로(미주입)에서만** 본다 — 주입 runner 는 존재/rc/HEAD 를
    전부 모델링하는 권위이므로 fs 가드를 건너뛴다(`current_branch` 동형).
    """
    runner = git_runner
    if runner is None:
        path = slot_path(slot)
        if not path.exists():
            return SlotStatus(slot, branch=None, upstream=None, upstream_ok=False, submodules=[])
        runner = _real_git_runner(path)
    branch = current_branch(slot, git_runner=runner)
    # upstream/submodule 조회는 runner 예외까지 흡수한다(docstring "예외 raise 안 함" 계약).
    # 각각 독립 try/except — 한쪽 raise 가 다른쪽 정보를 가리지 않게(부분 degrade).
    try:
        upstream, upstream_ok = _upstream_status(runner)
    except Exception:  # noqa: BLE001 — fail-soft: runner raise → 미해소로 흡수(경고 surface).
        upstream, upstream_ok = None, False
    try:
        submodules = _submodule_statuses(runner)
    except Exception:  # noqa: BLE001 — fail-soft: runner raise → 빈목록(부트스트랩 줄 생략).
        submodules = []
    return SlotStatus(
        slot, branch=branch, upstream=upstream, upstream_ok=upstream_ok, submodules=submodules
    )


def set_test_cmd(slot: str, cmd: str | None) -> Lease:
    """기존 슬롯 리스의 test_cmd 를 갱신한다 (idle/leased 무관).

    `pm-config` 콘솔의 `[b]`(슬롯 빌드명령 설정/변경)·worktree add 후의 "나중에 변경"
    경로가 부르는 setter — 슬롯에 바인딩된 회귀/빌드명령(HIL config 등)을 사후에 바꾼다
    (board._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다). 별도
    CLI 서브커맨드는 만들지 않는다(콘솔 `[b]` + worktree add 프롬프트로 충분·결정 §setter 단순화).

    create_slot 의 lease test_cmd 바인딩과 *같은* flock + atomic write 패턴을 재사용한다
    (`_lease_lock` 으로 read-modify-write 직렬화 → `_write_ledger` atomic replace). 장부에
    슬롯이 없으면 **`KeyError`** raise(침묵 무력화 금지 — 호출부가 명시 안내). 갱신된
    Lease 를 반환한다. `cmd=None` 이면 바인딩 해제(repo areas/local.conf 로 폴백·현행).
    """
    with _lease_lock():
        leases = _read_ledger_strict()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")
        target.test_cmd = cmd
        _write_ledger(leases)
        return target


# ── 슬롯 git 진실: 스냅 기록(write) + 비교(compare) + is-ancestor 판정 ───────
# 슬롯 git 상태를 *기대*(drift 감지 기준) 축으로 lease 장부에 기계 기록한다(live 표시는 
# 그대로·2축). submodule 의 pin/drift 모델을 본체(superproject)로 대칭 확장 — 개념
# write=부트스트랩 bind/alloc·핸드오프·create(release 시 정리)·compare=0단계.

# head 비교 결과(`merge-base --is-ancestor` 완화) — GitCompareResult.head_relation 값.
HEAD_MATCH = "match"            # 기록 head == live head (같은 branch) → 통과.
HEAD_DESCENDANT = "descendant"  # live 가 기록 head 의 후손 + 같은 branch → crash 후 재개(notice·통과).
HEAD_DIVERGED = "diverged"      # 브랜치 변경 또는 비후손(리셋·되감기) → FAIL-LOUD.
HEAD_UNKNOWN = "unknown"        # 기록/live head 미상 → 판정 불가(통과 취급).


def _slot_head(git_runner: GitRunner) -> "str | None":
    """슬롯 worktree HEAD 커밋 sha (live·`git rev-parse HEAD`·fail-soft None·`current_branch` 동형 규율)."""
    try:
        rc, out = git_runner(["rev-parse", "HEAD"])
    except Exception:  # noqa: BLE001 — fail-soft: 주입 runner raise 도 None(raise 금지 규칙).
        return None
    if rc != 0:
        return None
    return out.strip() or None


def _snapshot_submodule_pins(git_runner: GitRunner) -> "list[dict]":
    """`git submodule status` → `[{path, pin}]` (기록=기대 축).

    pin = submodule 의 현재 체크아웃 sha(선두 flag 제거) — "여기 두고 간다"의 기준값(재개 시 이
    sha 와 달라지면 drift·`_submodule_pin_drift`). `_submodule_statuses`와 같은 `submodule
    status` primitive 를 읽되 sha 만 뽑는다. rc≠0(조회 불가/submodule 없음)·예외 → 빈 목록(fail-soft).
    """
    try:
        rc, out = git_runner(["submodule", "status"])
    except Exception:  # noqa: BLE001 — fail-soft: 빈 목록(스냅에 submodule 생략).
        return []
    if rc != 0:
        return []
    pins: list[dict] = []
    for line in out.splitlines():
        # 경로는 공용 파서 단일 진실(`_parse_submodule_entries`) — 공백 경로·describe 를
        # 손실 없이 처리한다. sha 는 첫 토큰(flag+sha)에서 뽑는다(경로 공백과 무관).
        entries = _parse_submodule_entries(line)
        if not entries:
            continue
        raw = line.split(maxsplit=1)[0]
        sha = raw[1:] if raw[:1] in ("+", "-", "U") else raw
        _flag, path = entries[0]
        pins.append({"path": path, "pin": sha})
    return pins


def _snapshot_slot_git(slot: str, *, git_runner: GitRunner | None = None) -> "dict | None":
    """슬롯의 live git 상태를 스냅한다 — `{branch, head, submodules, recorded_at}` (base 제외).

    write 프리미티브의 원료(base 는 호출부가 결정: create=새 기록·alloc/bind=기존 보존). 전부
    fail-soft(`current_branch`/`slot_status` 동형·raise 금지). 슬롯 경로 부재 가드는 **실경로
    (git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는 존재/HEAD 를 모델링하는
    권위라 건너뛴다. 슬롯 부재(실경로)면 None(스냅 불가 → 호출부가 기존 git 을 유지)."""
    runner = git_runner
    if runner is None:
        path = slot_path(slot)
        if not path.exists():
            return None
        runner = _real_git_runner(path)
    return {
        "branch": current_branch(slot, git_runner=runner),
        "head": _slot_head(runner),
        "submodules": _snapshot_submodule_pins(runner),
        "recorded_at": _now_local(),
    }


def _apply_git_snapshot(lease: "Lease", *, base_branch: "str | None" = None,
                        base_commit: "str | None" = None,
                        git_runner: GitRunner | None = None) -> None:
    """`lease.git` 을 live 슬롯 스냅으로 갱신한다 — write 프리미티브 본체(in-place·fail-soft).

    base 규칙(3표·rebase 로만 변경): `base_branch` 주어짐(create·set-base) = base 를
    **새로** 기록 / 미주어짐(alloc/bind arrival) = 기존 `lease.git.base` **보존**. base 를 새로
    기록할 때 commit 은:
      - `base_commit` 명시(set-base — 그 브랜치 tip 또는 사용자 `@<commit>`) = 그 값.
      - `base_commit` None(create — fresh 슬롯 tip 이 파생 base·기존 거동) = 방금 스냅한 head.
    스냅 불가(슬롯 부재 등 None)면 기존 git 을 clobber 하지 않고 그대로 둔다(silent 손실 방지).
    예외 흡수 — 스냅 실패가 리스 lifecycle op 을 막지 않는다.
    """
    try:
        snap = _snapshot_slot_git(lease.slot, git_runner=git_runner)
        if snap is None:
            return  # 슬롯 부재/스냅 불가 → 기존 git 유지(clobber 안 함).
        if base_branch is not None:
            commit = base_commit if base_commit is not None else snap.get("head")
            base = {"branch": base_branch, "commit": commit}
        elif isinstance(lease.git, dict):
            base = lease.git.get("base")
        else:
            base = None
        if base is not None:
            snap = {"base": base, **snap}   # base 를 앞에(스키마 순서·cosmetic).
        lease.git = snap
    except Exception as exc:  # noqa: BLE001 — fail-soft: 스냅 실패가 alloc/bind/create 를 막지 않는다.
        if _is_engine_rev_skew(exc):
            raise


def record_git_snapshot(slot: str, *, base_branch: "str | None" = None,
                        base_commit: "str | None" = None,
                        git_runner: GitRunner | None = None) -> "Lease | None":
    """슬롯 현재 git 상태를 `lease.git` 에 기록하는 독립 write 프리미티브 (핸드오프·set-base).

    부트스트랩 bind/alloc·create 는 자기 lock 안에서 `_apply_git_snapshot` 을 인라인 호출하지만,
    핸드오프("여기 두고 간다")·set-base(기준점 명시 기록)처럼 lifecycle op 밖에서 스냅을
    찍는 호출부(pm_handoff·`set_base`)를 위한 standalone 진입점이다. `base_commit` 은 set-base 가
    base.commit 을 그 브랜치 tip(또는 사용자 `@<commit>`)으로 명시할 때 쓴다(None=create 기존 거동·
    slot HEAD). `_lease_lock`+`_write_ledger`(atomic) — alloc/release 와 동일한 read-modify-write
    직렬화. 장부에 슬롯이 없으면 None(무해). 갱신된 Lease 반환."""
    with _lease_lock():
        leases = _read_ledger_strict()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        _apply_git_snapshot(target, base_branch=base_branch, base_commit=base_commit,
                            git_runner=git_runner)
        _write_ledger(leases)
        return target


def read_lease(slot: str) -> "Lease | None":
    """슬롯 lease 를 장부에서 읽어 반환한다 — read-only 조회 프리미티브 (`record_git_snapshot` 짝).

    `record_git_snapshot` 이 갱신하는 `lease.git`(도착 기대 스냅)을 *갱신 전에* 조회하려는
    호출부(pm_handoff `_record_slot_snapshot` 의 실갱신/무변경 판별)를 위한 얇은 단일-lease
    read. `_read_ledger` 를 짧게 lock 안에서 훑어 slot 일치 lease 를 반환한다(없으면 None).
    부작용/git 조회 없음(순수 장부 read)."""
    with _lease_lock():
        leases = _read_ledger()
        return next((l for l in leases if l.slot == slot), None)


def read_lease_strict(slot: str) -> "Lease | None":
    """mutation 안전 게이트용 단일 lease strict 조회 — 손상/읽기 오류를 예외로 전파한다."""
    with _lease_lock():
        leases = _read_ledger_strict()
        return next((lease for lease in leases if lease.slot == slot), None)


def lease_owned_by_task_strict(slot: str, task: str) -> bool:
    """슬롯이 현재도 ``task``의 leased 소유인지 락 안에서 strict 확인한다."""
    with _lease_lock():
        lease = next((item for item in _read_ledger_strict() if item.slot == slot), None)
        return lease is not None and lease.state == "leased" and lease.session == task


def _is_ancestor(git_runner: GitRunner, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor <ancestor> <descendant>` — ancestor 가 descendant 의 조상인가.

    rc0=조상(True)·rc1=아님(False)·rc>1 또는 예외=False(보수적 — 판정 불가는 "후손 아님"으로 →
    FAIL-LOUD 쪽으로 기운다·경보를 놓치기보다 잘못 울리는 게 낫다). git 결정론 판정(추측 아님)."""
    try:
        rc, _out = git_runner(["merge-base", "--is-ancestor", ancestor, descendant])
    except Exception:  # noqa: BLE001 — fail-soft: 판정 불가는 후손 아님(보수적).
        return False
    return rc == 0


def _head_relation(recorded_branch: "str | None", recorded_head: "str | None",
                   live_branch: "str | None", live_head: "str | None",
                   *, git_runner: GitRunner | None) -> str:
    """기록 head vs live head 관계 판정 (`merge-base --is-ancestor` 완화).

    - head 한쪽이라도 미상 → `unknown`(판정 불가·통과 취급).
    - 브랜치 변경 → `diverged`(사고·FAIL-LOUD).
    - 같은 branch·head 동일 → `match`(통과).
    - 같은 branch·head 다름 & live 가 기록 head 의 **후손** → `descendant`(crash 후 재개·내 커밋·
      notice·통과). crash 후 재개(핸드오프 못 하고 죽음)를 경보 소음으로 만들지 않는다.
    - 같은 branch·head 다름 & **비후손**(리셋·되감기·divergent) → `diverged`(FAIL-LOUD)."""
    if not recorded_head or not live_head:
        return HEAD_UNKNOWN
    if recorded_branch != live_branch:
        return HEAD_DIVERGED
    if recorded_head == live_head:
        return HEAD_MATCH
    if git_runner is not None and _is_ancestor(git_runner, recorded_head, live_head):
        return HEAD_DESCENDANT
    return HEAD_DIVERGED


def _submodule_pin_drift(recorded_subs: "list", live_subs: "list") -> "list[str]":
    """기록 submodule pin ≠ live pin 인 path 목록 (pin/drift 대칭·경고 축)."""
    live_map = {
        s["path"]: s.get("pin") for s in live_subs
        if isinstance(s, dict) and "path" in s
    }
    drift: list[str] = []
    for s in recorded_subs or []:
        if not isinstance(s, dict) or "path" not in s:
            continue
        if live_map.get(s["path"]) != s.get("pin"):
            drift.append(s["path"])
    return drift


class GitCompareResult:
    """기록된 `lease.git`(기대) vs 슬롯 live 상태 비교 결과 — 부트스트랩 0단계 소비.

    - `unrecorded` — git 미기록(구 슬롯·git 필드 부재/슬롯 없음) → drift 감지 **비활성**. 0단계는
      차단이 아니라 loud 표시 + 사용자 질의(`set-base`). ok/fail 어느 쪽도 아닌 별도 상태.
    - `recorded`/`live` — 기록 git dict / live 스냅 dict(branch·head·submodules).
    - `branch_match` — 기록 branch == live branch. False = 브랜치 변경(사고·FAIL-LOUD·표 branch 축).
    - `head_relation` — `match`/`descendant`/`diverged`/`unknown`(is-ancestor 완화·위 상수).
    - `submodule_drift` — 기록 pin ≠ live pin 인 submodule path 목록(경고).
    - ⚠️ **`base` 는 이 티켓에서 recorded-only — compare 가 surface 하지 않는다**(비교축은
      branch+head+submodule 뿐). 인터페이스 산문의 "base=사고 시 FAIL-LOUD"·"base 대비 N behind"
      판정은 0단계·(rebase·wave-2d)로 이월된다 — 여기선 `recorded["base"]`(inert
      breadcrumb)로 *전달만* 하고 비교/계산을 붙이지 않는다. 소비처가 base 를 비교축으로 오해하지
      않도록 명시(delivered compare 는 base 를 판정에 안 쓴다).

    (dataclass 미사용 — Lease/SlotStatus 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다.)
    """

    def __init__(self, slot: str, *, unrecorded: bool, recorded: "dict | None",
                 live: dict, branch_match: bool, head_relation: str,
                 submodule_drift: "list[str]"):
        self.slot = slot
        self.unrecorded = unrecorded
        self.recorded = recorded
        self.live = live
        self.branch_match = branch_match
        self.head_relation = head_relation
        self.submodule_drift = submodule_drift

    @property
    def fail_loud(self) -> bool:
        """명시적 FAIL-LOUD — branch 변경(사고) 또는 head diverged(리셋·비후손). unrecorded 는
        fail 아님(사용자 질의 대상)."""
        if self.unrecorded:
            return False
        return (not self.branch_match) or self.head_relation == HEAD_DIVERGED

    @property
    def ok(self) -> bool:
        """0단계 통과 여부 — 기록 있고 FAIL-LOUD 아님. unrecorded 는 ok 아님(질의 필요·별도 처리)."""
        return (not self.unrecorded) and (not self.fail_loud)

    def __repr__(self) -> str:
        return (f"GitCompareResult(slot={self.slot!r}, unrecorded={self.unrecorded!r}, "
                f"branch_match={self.branch_match!r}, head_relation={self.head_relation!r}, "
                f"submodule_drift={self.submodule_drift!r})")


def compare_slot_git(slot: str, *, git_runner: GitRunner | None = None) -> GitCompareResult:
    """기록된 `lease.git`(기대) vs 슬롯 live 상태를 비교한다 — compare 프리미티브 (0단계 소비).

    장부에서 슬롯 lease 를 읽어 그 `git` 스냅(기대)을 live 스냅과 대조한다. `_lease_lock` 은 장부
    read 동안만 짧게 잡고 git 조회(subprocess)는 lock 밖에서 한다. 미기록(git 필드 없음/슬롯 없음)
    이면 `unrecorded=True`(drift 감지 비활성). head 비교는 `merge-base --is-ancestor` 로
    완화(crash 후 재개를 경보 소음으로 안 만든다). 판정 정책(FAIL/notice/질의)은 0단계
    가 `ok`/`fail_loud`/`unrecorded`/`head_relation` 을 읽어 결정한다(엔진=surface·PM=확인)."""
    with _lease_lock():
        leases = _read_ledger()
    target = next((l for l in leases if l.slot == slot), None)
    recorded = target.git if (target is not None and isinstance(target.git, dict)) else None
    live = _snapshot_slot_git(slot, git_runner=git_runner) or {}
    if recorded is None:
        return GitCompareResult(
            slot, unrecorded=True, recorded=None, live=live,
            branch_match=False, head_relation=HEAD_UNKNOWN, submodule_drift=[],
        )
    # head 비교용 runner — 주입 우선, 아니면 슬롯 실경로(부재면 None → is-ancestor 스킵·unknown).
    runner = git_runner
    if runner is None and slot_path(slot).exists():
        runner = _real_git_runner(slot_path(slot))
    return GitCompareResult(
        slot,
        unrecorded=False,
        recorded=recorded,
        live=live,
        branch_match=recorded.get("branch") == live.get("branch"),
        head_relation=_head_relation(
            recorded.get("branch"), recorded.get("head"),
            live.get("branch"), live.get("head"), git_runner=runner,
        ),
        submodule_drift=_submodule_pin_drift(
            recorded.get("submodules") or [], live.get("submodules") or []
        ),
    )


# ── set-base / rebase 기준-gate 계약 / status (기준점 미기록 flow) ──
# 기준점(base) 미기록 슬롯(v1.3.0 이전 전부)을 **자동 추론 없이** 다룬다: 엔진=상태 surface·
# 사용자=결정 `git merge-base HEAD
# origin/main` 추측은 rebase 이력·다중 후보(main/develop)에서 **조용히 틀리고**, 그 위에서 drift 감지가
# 돌면 무의미해진다 → 추론 금지·사용자 명시 질의. set_base=사용자 지정 base 기록·
# resolve_rebase_base=rebase 기준-gate **계약**·slot_git_status=조회(미기록 N-behind `-`).


# ── readonly 공유 슬롯 — role 조회 / mutation 거부 / refresh ──
# readonly 슬롯은 코드를 *읽어* PM 홈 wiki(domain·architecture·status)를 쓰는 research 기준면이다:
# 슬롯 자체는 read-only(detached·배타 대여 없음)이고, mutation op(set-base·rebase·dev·sync)은 **엔진
# 경로에서 거부**한다(fs 레벨 쓰기 차단은 안 함). 갱신은 `refresh`(fetch → detach 이동·dirty=
# 거부+loud)만 허용한다 — read-only 슬롯의 dirty 는 "누군가 여기 썼다"는 신호라 조용히 reset 하지 않는다.

_LEASE_ROLE_READONLY = "readonly"   # role 값(identity_args._LEASE_ROLE_READONLY 정합·모듈 격리 inline).


def _slot_role(slot: str) -> str:
    """슬롯의 role 을 장부에서 읽는다 — 미등록/미기록이면 "work" (mutation 거부/status 소비).

    `_read_recorded_base` 와 동형으로 `_lease_lock` 은 장부 read 동안만 짧게 잡는다. 슬롯이 장부에
    없거나 role 필드가 없으면(구 장부) "work"(작업 슬롯·하위호환·fail-soft)."""
    with _lease_lock():
        leases = _read_ledger()
    target = next((l for l in leases if l.slot == slot), None)
    return target.role if target is not None else "work"


class ReadonlySlotMutation(RuntimeError):
    """readonly 공유 슬롯(role="readonly")에 mutation op(set-base·rebase·dev·sync)을 시도함 — 거부.

    readonly 슬롯은 문서 검증 기준면(released base)이라 슬롯의 git 상태를 바꾸는 엔진 op 을 거부한다
    (갱신은 `refresh` 만·fetch→detach 이동). fs 레벨 쓰기 차단은 안 한다(엔진 경로 한정).
    `op` = 거부된 연산명(진단용). (`RuntimeError` 서브클래스 — `BareRepoMissing` 동형·파사드 rc 1.)"""

    def __init__(self, slot: str, op: str):
        self.slot = slot
        self.op = op
        super().__init__(
            f"슬롯 {slot!r} 은 readonly 공유 슬롯(role=readonly·⑬)이라 `{op}` 를 거부한다 — 문서 검증 "
            f"기준면(released base·detached)이라 git 상태를 바꾸는 op 은 불가하다. 갱신은 "
            f"`{_runtime_skill_entry('pm-worktree')} refresh {slot} "
            f"[--onto <branch>]`(fetch→detach 이동)로만 한다."
        )


def _reject_readonly_mutation(slot: str, op: str, *, git_runner: GitRunner | None = None) -> None:
    """슬롯이 readonly면 `ReadonlySlotMutation` raise — set-base/rebase/dev/sync 진입 가드.

    엔진 경로 한정 거부. 판별 축은 canonical `lease.role`(0단계 carve-out·F6 예외와 동일 축).
    git_runner 는 시그니처 정합용(현 판별은 장부만 읽어 미사용). mutation 선행 판정이므로
    `read_lease_strict`를 써 손상 장부를 기본 work role로 축약한 뒤 Git 변경을 시작하지 않는다.
    이미 락을 쥔 호출부(release/bind_slot)는 이 헬퍼 대신 보유 중인 `lease.role` 을 직접 검사한다
    (non-reentrant flock 재취득 = 데드락)."""
    lease = read_lease_strict(slot)
    if lease is not None and lease.role == _LEASE_ROLE_READONLY:
        raise ReadonlySlotMutation(slot, op)


class ReadonlySlotNotLeasable(RuntimeError):
    """readonly 공유 슬롯에 lease-lifecycle op(release·force_release·bind)을 시도함 — 거부.

    readonly 슬롯은 **무소유 공유 자산**(배타 대여 없음·session/pid 없음)이라 대여/반납/바인딩(점유)의
    대상이 아니다. 자동 경로(alloc idle-탐색·reclaim_stale)는 이미 자연/가드로 닫혔으나, **명시 지정**
    (`release <slot>`·`force_release`·`/pm-bootstrap --slot`)이 뚫려 있었다 — idle 화되면 alloc 이 그
    슬롯을 work 슬롯으로 점유해 role 이 유실되는 깨진 상태를 부른다. 0단계 carve-out()이 readonly 를
    *조회 지칭*엔 허용하지만 bind 는 *점유*라 의미가 다르다(이 거부가 그 틈을 닫는다). `op` = 거부된
    연산명(진단용). 제거는 `worktree remove --force`, 갱신은 `refresh`. (`RuntimeError` — 파사드 rc 1.)"""

    def __init__(self, slot: str, op: str):
        self.slot = slot
        self.op = op
        super().__init__(
            f"슬롯 {slot!r} 은 readonly 공유 슬롯(role=readonly·⑬)이라 `{op}`(대여/반납/바인딩) 대상이 "
            f"아니다 — 무소유 공유 자산(배타 대여 없음·session/pid 없음). 제거하려면 "
            f"`worktree remove {slot} --force`, 최신 갱신은 "
            f"`{_runtime_skill_entry('pm-worktree')} refresh {slot}`."
        )


class RefreshRefused(RuntimeError):
    """readonly 슬롯 `refresh` 거부 — dirty(누군가 씀·신호) / base 미해소 / non-readonly ().

    `reason` ∈ {"dirty", "no-base", "not-readonly"}:
      - **dirty** — read-only 슬롯에 미커밋 변경이 있다 = "누군가 여기 썼다"는 신호. 조용히 reset 하면
        신호가 사라진다 → 거부 + loud(감지=기계·해소=사용자·submodule drift 동형·결정). 사용자가 보고
        판단한다(수동 정리/조사 후 재시도).
      - **no-base** — `--onto` 미지정 + 기록된 base.branch 도 없어 어디로 갱신할지 불명(추론 금지).
      - **not-readonly** — 대상 슬롯이 readonly 가 아니다(refresh 는 detached 공유 슬롯 전용 — 작업
        슬롯을 detach 로 이동하면 브랜치 위치를 잃는다). (`RuntimeError` — 파사드 rc 1.)"""

    def __init__(self, slot: str, reason: str, *, detail: str = ""):
        self.slot = slot
        self.reason = reason
        tail = f" — {detail}" if detail else ""
        msg = {
            "dirty": (f"슬롯 {slot!r} 에 미커밋 변경이 있어 refresh 를 거부한다 — read-only 슬롯의 dirty 는 "
                      f"'누군가 여기 썼다'는 신호다(조용히 reset 안 함). 변경을 확인·정리한 뒤 재시도하라"),
            "no-base": (f"슬롯 {slot!r} 의 기준점(base)이 미기록이고 `--onto` 도 없어 어디로 refresh 할지 "
                        f"불명이다(추론 금지) — `--onto <branch>` 로 갱신 기준을 명시하라"),
            "not-readonly": (f"슬롯 {slot!r} 은 readonly 공유 슬롯이 아니다 — refresh 는 detached read-only "
                             f"슬롯 전용이다(작업 슬롯을 detach 이동하면 브랜치 위치를 잃는다)"),
            "git-error": (f"슬롯 {slot!r} refresh 중 git 오류"),
        }.get(reason, f"슬롯 {slot!r} refresh 거부({reason})")
        super().__init__(msg + tail + f"")


def refresh(slot: str, *, onto: "str | None" = None,
            git_runner: GitRunner | None = None) -> "tuple[str, str]":
    """readonly 공유 슬롯을 released 최신으로 갱신한다 — fetch → detached HEAD 이동 ().

    read-only 슬롯(detached·문서 검증 기준면)을 최신 released tip 으로 fast-forward 하는 유일 경로다
    (mutation op 은 거부·`refresh` 만 허용). 순서:
      1. **readonly 확인** — 대상 슬롯이 readonly 가 아니면 `RefreshRefused("not-readonly")`(작업 슬롯을
         detach 이동하면 브랜치 위치 유실).
      2. **기준 해소** — `onto`(명시) > 기록된 `base.branch`. 둘 다 없으면 `RefreshRefused("no-base")`
         (추론 금지).
      3. **dirty 거부 + loud** — 슬롯 worktree 에 미커밋 변경이 있으면 `RefreshRefused("dirty")`. read-only
         슬롯의 dirty 는 "누군가 썼다"는 신호라 조용히 reset 하지 않는다(감지=기계·해소=사용자).
      4. **fetch → detach 이동** — `git fetch origin`(best-effort) 후 기준 ref 로 `git checkout
         --detach <ref>`(detached HEAD 이동). **기준 ref 해소는 두 경로가 다르다**:
           - `onto` **명시** = 준 ref 를 **그대로** 쓴다(로컬 브랜치명이면 로컬 tip·자동 대체 없음).
             명시 인자를 `origin/<branch>` 로 조용히 바꾸면 미push 로컬 tip 으로 갱신할 길이 없고,
             사용자가 지정한 기준과 실제 이동 지점이 어긋난 채 성공 보고된다. 원격 기준을 원하면
             `origin/<branch>` 로 적는다. 해소 실패는 대체 없이 fail-loud(`RefreshRefused`).
           - `onto` **미지정**(기록된 base.branch) = `origin/<branch>` 우선·미해소면 로컬 `<branch>`
             폴백(readonly 기준면 = 공개된 released 상태라는 기본 경로 설계 의도 유지).
      4b. **submodule 재동기** — `git submodule update --init --recursive --force`(gitlink 옛 pin 잔존
         →stale+dirty 자가 잠금 방지·readonly=dev submodule 없어 전체 재동기 안전).
      5. **스냅 갱신** — `record_git_snapshot(base_branch=기준 branch)` — 기준 branch 는 onto(명시 시)
         또는 기록된 base.branch 이며(무인자 경로에서 ref 가 origin/<branch> 로 해소돼도 장부에는 논리
         branch 가 남는다), base.commit 을 새 head 로 갱신한다(onto 생략에도 갱신 — HEAD 이동했는데
         장부 base.commit 옛값 잔존 불일치 방지). refresh 는 base 가 정당하게 바뀌는 유일 지점
         ("rebase 로만" 의 readonly 예외). 주의: 명시 `--onto` 는 비-고착 — 장부에 남는 건 그 branch 명이라
         다음 무인자 refresh 는 기본 규칙(origin 우선)대로 origin/<branch> 로 되돌아간다(성공 메시지의
         실측 ref/sha 로 확인).
    반환 = `(이동한 기준 ref, 이동 후 HEAD 커밋 sha)` — 본체 CLI 가 이 실측값을 그대로 보고한다(조용한
    ref 대체 제거의 짝: 결과 메시지도 추정이 아니라 실제 해소값). sha 조회 불가는 빈 문자열(fail-soft·
    이동 자체는 성공). `git_runner` 주입 시 그 runner(테스트 hermetic·미주입이면 슬롯 worktree 바인딩
    실 runner)."""
    lease = read_lease_strict(slot)
    if lease is None or lease.role != _LEASE_ROLE_READONLY:
        raise RefreshRefused(slot, "not-readonly")
    # 기준 해소 — onto 명시 > 기록된 base.branch (추론 금지).
    base_branch = onto
    if base_branch is None:
        recorded = lease.git.get("base") if isinstance(lease.git, dict) else None
        if not isinstance(recorded, dict):
            recorded = None
        if recorded and recorded.get("branch"):
            base_branch = recorded["branch"]
    if not base_branch:
        raise RefreshRefused(slot, "no-base")

    runner = git_runner
    if runner is None:
        p = slot_path(slot)
        if not p.exists():
            raise RefreshRefused(slot, "git-error",
                                 detail=f"슬롯 worktree 경로 {p} 가 없다")
        runner = _real_git_runner(p)

    # dirty = 거부 + loud (조용히 reset 금지·신호 보존).
    if _is_dirty(slot_path(slot), git_runner=runner):
        raise RefreshRefused(slot, "dirty")

    # fetch → 기준 ref 해소 → detach 이동 (해소 규칙은 docstring 4 — 명시 인자는 그대로·기본 경로만
    # origin 우선). 명시 인자를 조용히 다른 ref 로 바꾸면 사용자가 지정한 기준과 실제 이동 지점이
    # 어긋난 채 성공 보고된다(미push 로컬 tip 갱신 불가). 대체가 필요한 상황이면 loud 실패로 남긴다.
    ref = base_branch
    rc, out = runner(["fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] 슬롯 {slot} `git fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
            f"  {_frozen_fallback_label(base_branch)}로 detach 이동한다 — 네트워크 복구 후 재-refresh 하라 "
            "(fail-soft).",
            file=sys.stderr,
        )
    elif onto is None and not base_branch.startswith("origin/"):
        # `--onto` 미지정 기본 경로 한정 — 기록된 base.branch 는 순수 브랜치명이면 origin/<branch> 로
        # 해소 시도(공개 released 상태 = readonly 기준면), 미해소면 로컬 <branch> 폴백.
        rc2, _ = runner(["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}"])
        if rc2 == 0:
            ref = f"origin/{base_branch}"
    rc, out = runner(["checkout", "--detach", ref])
    if rc != 0:
        # 명시 인자 해소 실패는 자동 대체 없이 여기서 끝난다 — 대안(원격 기준)은 사용자가 명시한다.
        hint = ("" if onto is None else
                " — `--onto` 는 준 ref 를 그대로 해소한다(자동 대체 없음). 원격 기준을 원하면 "
                "`origin/<branch>` 로 명시하라")
        raise RefreshRefused(slot, "git-error",
                             detail=f"`git checkout --detach {ref}` 실패 (rc={rc}): "
                                    f"{str(out).strip()[:200]}{hint}")
    # submodule 재동기 () — `checkout --detach` 는 superproject HEAD 만 옮기고
    # submodule gitlink 는 **옛 pin 잔존** → readonly 기준면이 stale + `git status` dirty(gitlink 변경)
    # 로 남아 **다음 refresh 가 자기 dirty 거부에 걸리는 자가 잠금**이 된다(create_slot 은 init 하는데
    # refresh 는 안 하던 비대칭). readonly 슬롯은 mutation(dev) 거부라 on-branch(dev) submodule 이
    # 존재할 수 없으니(보호 대상 0), selective 가 아닌 **전체 재동기**(`--init --recursive --force`·
    # create_slot 관례 정합)가 안전하다. rc≠0 은 fail-loud(기준면이 반쯤 갱신된 채 성공 보고 금지).
    rc, out = runner(["submodule", "update", "--init", "--recursive", "--force"])
    if rc != 0:
        raise RefreshRefused(slot, "git-error",
                             detail=f"submodule 재동기 실패 (rc={rc}): {str(out).strip()[:200]}")
    # 스냅 갱신 () — base 를 **기준 branch(onto 또는 기록된 base.branch)로 재기록**해 base.commit=새 head 로
    # 갱신한다. onto 생략(기록된 base.branch 로 refresh)에도 base_branch 를 넘겨야, HEAD 는 최신
    # origin/<base> 로 이동했는데 장부 base.commit 은 옛 커밋으로 남아 status "N behind"·기준면 기록이
    # 실제와 어긋나는 불일치를 막는다. refresh(readonly 전용)는 set-base/rebase 외에 base 가 바뀌는
    # *유일한 정당 지점*이다( "base 는 rebase 로만" 결정의 readonly 예외 — detached 기준면 이동이 곧
    # base 이동). base_commit 미지정 → `_apply_git_snapshot` 이 방금 스냅한 head 를 commit 으로 쓴다.
    record_git_snapshot(slot, base_branch=base_branch, git_runner=git_runner)
    # 이동 후 실 HEAD sha — 결과 메시지가 "어느 ref 의 어느 커밋으로 갔는지"를 실측으로 보고하게 한다
    # (해소 규칙을 사용자 명시 존중으로 바꾼 것의 짝). 조회 불가는 "" fail-soft(이동 자체는 성공).
    return ref, (_slot_head(runner) or "")


def _parse_base_ref(base_arg: str) -> "tuple[str, str | None]":
    """`<branch>[@<commit>]` 인자를 (branch, commit|None) 로 분해한다 (set-base).

    첫 `@` 에서 가른다(`str.partition`) — `origin/main@df10dc6` → ("origin/main", "df10dc6"),
    `origin/main` → ("origin/main", None). `@` 없으면 commit 미지정(그 브랜치 tip 이 base commit).
    브랜치명에 `@` 가 든 드문 ref(`@{upstream}` 등)는 이 표면에서 지원하지 않는다(문서화·CLI 진입)."""
    branch, sep, commit = base_arg.partition("@")
    return branch, (commit if sep and commit else None)


def _resolve_base_commit(slot: str, ref: str, *, git_runner: GitRunner | None = None) -> "str | None":
    """`ref`(브랜치 또는 커밋)의 커밋 sha 를 슬롯 worktree 에서 해소한다 (set-base commit=branch tip).

    `git rev-parse --verify <ref>^{commit}` — 브랜치면 tip, 커밋이면 그 커밋. 해소 불가(rc≠0)·슬롯
    부재·예외 → None(fail-soft·`_apply_git_snapshot` 이 slot HEAD 로 폴백·degraded 기록). 실경로
    (runner 미주입)는 슬롯 worktree 바인딩 runner, 슬롯 부재면 None(`_slot_head` 동형 규율)."""
    runner = git_runner
    if runner is None:
        p = slot_path(slot)
        if not p.exists():
            return None
        runner = _real_git_runner(p)
    try:
        rc, out = runner(["rev-parse", "--verify", f"{ref}^{{commit}}"])
    except Exception:  # noqa: BLE001 — fail-soft: 해소 불가는 None.
        return None
    if rc != 0:
        return None
    return (out or "").strip() or None


class BaseRefUnresolvable(RuntimeError):
    """set-base 의 base ref(브랜치 또는 `@<commit>`)를 슬롯 worktree 에서 해소할 수 없다 — FAIL-LOUD (codex must-fix).

    이 티켓의 중심 계약 = **조용히 틀린 base 차단**. 옛 코드는 해소 불가 ref 를 slot HEAD 로 폴백해
    `base=origin/typo@<슬롯HEAD>` 로 조용히 오기록했고(오타·미fetch), drift 감지가 garbage baseline
    위에서 돌았다. create 경로(fresh 슬롯 tip==브랜치 tip=정답)의 slot-HEAD 폴백과 달리 set-base 의
    잘못된-ref 경로는 slot HEAD 가 **무관한 커밋**이라 silent 오기록이다. 그래서 set_base 는 ref 해소
    실패 시 record 이전에 이 예외로 거부한다(사용자가 실재 브랜치/커밋을 주거나 fetch 하도록).

    (`RuntimeError` 서브클래스 — `RebaseBaseRequired`/`BareRepoMissing` 동형·CLI 파사드가 rc 1 로 surface.)"""

    def __init__(self, slot: str, ref: str):
        self.slot = slot
        self.ref = ref
        super().__init__(
            f"base ref {ref!r} 를 슬롯 {slot!r} 에서 해소할 수 없습니다 — 실재하는 브랜치/커밋을 "
            f"지정하거나(오타 확인), 원격 ref 면 먼저 fetch 하세요. (조용히 틀린 base 로 기록하지 "
            f"않습니다 — 자동 추론/폴백 금지.)"
        )


def set_base(slot: str, base_ref: str, *, commit: "str | None" = None,
             git_runner: GitRunner | None = None) -> "Lease | None":
    """슬롯 기준점(base)을 **사용자 명시**로 기록한다 — `/pm-worktree set-base` 백본 ().

    미기록 슬롯(v1.3.0 이전)의 base 를 **추론 없이** 사용자가 지정해 기록한다(그때부터 drift 감지
    작동). `base_ref` = 기준 브랜치(예 `origin/main`), `commit` = 명시 `@<commit>`(생략 = 그 브랜치
    tip). base.commit 해소:
      - `commit` 명시 → 그 커밋(rev-parse verify).
      - 생략 → `base_ref` tip(rev-parse verify).
      - **해소 불가 → `BaseRefUnresolvable` FAIL-LOUD**(record 안 함·codex must-fix). slot HEAD 폴백
        금지 — 무관한 커밋으로 조용히 오기록하면 drift 감지가 garbage baseline 위에서 돌아 이 티켓
        계약("조용히 틀린 base 차단")을 스스로 위반한다. (create 경로의 slot-HEAD 폴백은
        `_apply_git_snapshot`/`record_git_snapshot` 레벨에 그대로 — fresh 슬롯 tip==브랜치 tip=정답.)
    write 프리미티브(`record_git_snapshot(base_branch=,base_commit=)`)를 소비한다 — 자체 장부
    write 를 재구현하지 않는다(base_commit=검증된 실 sha·None 폴백 경로를 안 탄다). **자동 추론 절대
    금지**(엔진=surface·사용자=결정). 장부에 슬롯이 없으면 None. 갱신된 Lease 반환.

    **readonly 거부()**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise
    (base 는 released 기준면·mutation 불가·갱신은 refresh 만). ref 해소/장부 write 이전 가드."""
    _reject_readonly_mutation(slot, "set-base", git_runner=git_runner)
    resolved = _resolve_base_commit(
        slot, commit if commit is not None else base_ref, git_runner=git_runner
    )
    if resolved is None:
        # ref 해소 실패(오타·미fetch·슬롯 worktree 부재) → record 이전에 거부(silent 오기록 차단).
        raise BaseRefUnresolvable(slot, commit if commit is not None else base_ref)
    return record_git_snapshot(slot, base_branch=base_ref, base_commit=resolved,
                               git_runner=git_runner)


def _read_recorded_base(slot: str) -> "dict | None":
    """장부에서 슬롯의 기록된 base(`lease.git.base`)를 읽는다 — 미기록이면 None (rebase gate/status 소비).

    `_lease_lock` 은 장부 read 동안만 짧게 잡는다(git 조회는 lock 밖·`compare_slot_git` 동형)."""
    with _lease_lock():
        leases = _read_ledger()
    target = next((l for l in leases if l.slot == slot), None)
    if target is None or not isinstance(target.git, dict):
        return None
    base = target.git.get("base")
    return base if isinstance(base, dict) else None


class RebaseBaseRequired(RuntimeError):
    """rebase 대상 슬롯에 기준점(base)이 미기록이라 rebase 를 거부한다 ().

    기준점 없이 rebase 하면 "어디로 rebase" 가 정의되지 않는다(추론 금지). `--onto <branch>`
    를 명시하면 그것을 기준으로 진행하고 그 값을 base 로 기록한다(1회 해소·`resolve_rebase_base(onto=)`).
    그 **계약**만 정의하고 rebase 엔진 본체는 ()가 이 gate 를 소비해 구현한다.

    (`RuntimeError` 서브클래스 — `BareRepoMissing`/`SlotBranchExists` 동형·파사드가 rc 1 로 surface.)"""

    def __init__(self, slot: str):
        self.slot = slot
        super().__init__(
            f"슬롯 {slot!r} 의 기준점(base)이 미기록이라 rebase 를 거부한다 — 기준 없이 rebase 불가"
            f"(추론 금지·결정 ⑪). `--onto <branch>` 로 기준을 명시하면 진행 + 그 값을 base 로 기록"
            f"(1회 해소), 또는 먼저 `set-base {slot} <branch>[@<commit>]` 로 기준점을 지정하라."
        )


def resolve_rebase_base(slot: str, *, onto: "str | None" = None, record: bool = True,
                        git_runner: GitRunner | None = None) -> str:
    """rebase 기준-gate — 기준 없으면 거부·`--onto` 명시 시 진행(+`record` 시 기록).

    rebase 엔진 본체()가 "어느 base 로 rebase" 를 이 gate 로 해소한다:
      - `onto` 명시 + `record=True`(기본·standalone 계약) → 그것을 base 로 **즉시 기록**(`set_base`·
        1회 해소)하고 반환. **base 가 실제로 기록됐을 때만 반환** — `set_base` 가 ref 해소 실패로
        `BaseRefUnresolvable` 을 던지면 자연 전파, 슬롯 장부 미등록으로 `None`(기록 실패)을 반환하면
        `RebaseBaseRequired` 로 명시 실패한다(codex must-fix — silent onto 반환 금지).
      - `onto` 명시 + `record=False`(**rebase 본체 경로**) → onto 를 **검증만**한다
        (`_resolve_base_commit`·해소 불가면 `BaseRefUnresolvable`). set_base 부작용(즉시 기록) **없음**
        — 장부는 건드리지 않고 브랜치명만 반환한다. 호출부(`_rebase_one`)가 **rebase 성공 시에만**
        base+head+recorded_at 을 원자 기록한다. 이유: onto 를 rebase *이전에* 기록하면 이후 충돌/사용자
        abort 시 장부는 새 base 를 주장하나 tree 는 옛 base 라 "충돌=장부 미갱신" 계약을 위반하고
        status N-behind 를 조용히 오표시한다(no-onto 충돌 경로는 불변인데 onto 만 비대칭이던 갭).
      - `onto` 없음 + 기록된 base 있음 → 기록된 `base.branch` 반환(그 최신으로 rebase·부작용 없음).
      - `onto` 없음 + 미기록 → `RebaseBaseRequired` raise(거부 — 기준 없이 rebase 불가·추론 금지).
    반환값 = rebase 가 향할 base 브랜치명(본체가 소비). 자동 rebase 없음(사용자 명시).

    **readonly 거부()**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
    rebase 는 슬롯 git 을 바꾸는 mutation 이라 read-only 기준면엔 불가(진입 가드·record 무관 선행)."""
    _reject_readonly_mutation(slot, "rebase", git_runner=git_runner)
    if onto is not None:
        if not record:
            # rebase 본체 경로 — 검증만(해소 불가=BaseRefUnresolvable)·기록 없음.
            if _resolve_base_commit(slot, onto, git_runner=git_runner) is None:
                raise BaseRefUnresolvable(slot, onto)
            return onto
        lease = set_base(slot, onto, git_runner=git_runner)   # 1회 해소·해소 실패는 BaseRefUnresolvable 전파.
        if lease is None:
            # 슬롯 장부 미등록 → base 기록 실패. onto 를 조용히 반환하면 "진행+기록" 계약 위반.
            raise RebaseBaseRequired(slot)
        return onto
    base = _read_recorded_base(slot)
    if base and base.get("branch"):
        return base["branch"]
    raise RebaseBaseRequired(slot)


def base_behind_count(slot: str, base_branch: str, *,
                      git_runner: GitRunner | None = None) -> "int | None":
    """슬롯 HEAD 가 `base_branch` 대비 몇 커밋 behind 인가 — `git rev-list --count HEAD..<base_branch>` ().

    base 기록의 배당금: "base 대비 N commits behind" = rebase 필요 판단 근거(). `base_branch`
    (예 `origin/main`)의 live tip 대비 HEAD 가 뒤진 커밋 수. **fetch 안 함**(조회 — 현재 remote-tracking
    ref 기준·era-warning `_slot_era_info` 동형). rev-list 실패(rc≠0)·미해소·슬롯 부재·파싱 실패 →
    None(계산 불가 → 상위에서 `-` 표기)."""
    runner = git_runner
    if runner is None:
        p = slot_path(slot)
        if not p.exists():
            return None
        runner = _real_git_runner(p)
    try:
        rc, out = runner(["rev-list", "--count", f"HEAD..{base_branch}"])
    except Exception:  # noqa: BLE001 — fail-soft: 계산 불가는 None.
        return None
    if rc != 0:
        return None
    try:
        return int((out or "").strip())
    except (ValueError, TypeError):
        return None


def slot_git_status(slot: str, *, git_runner: GitRunner | None = None) -> dict:
    """슬롯 git 구성 조회 — base·branch·head·**base 대비 N behind**·submodule pin/drift·dirty (`/pm-worktree status` 백본).

    미기록(base 없음)이면 `behind=None`·`behind_reason`=이유(계산 불가 → CLI `-` 표기·자동 추론
    금지). 기록 있으면 `base_behind_count` 로 N 을 센다. branch/head 는 live 조회
    (`current_branch`/`_slot_head`·표시 축). **submodule pin/drift(`_submodule_statuses`
    재사용·역할별 `SubmoduleStatus`)·dirty(`_is_dirty`)는 ()조회에 합류**한다
    (rebase 선-검사가 보는 것과 같은 primitive). 전부 fail-soft. 반환 dict: `slot`·`base`
    ({branch,commit}|None)·`branch`·`head`·`behind`(int|None)·`behind_reason`(str|None)·
    `submodules`(list[SubmoduleStatus]·runner 미해소면 [])·`dirty`(bool·runner 미해소면 False)."""
    base = _read_recorded_base(slot)
    runner = git_runner
    if runner is None and slot_path(slot).exists():
        runner = _real_git_runner(slot_path(slot))
    branch = current_branch(slot, git_runner=runner) if runner is not None else None
    head = _slot_head(runner) if runner is not None else None
    if base and base.get("branch"):
        behind = base_behind_count(slot, base["branch"], git_runner=runner)
        reason = None if behind is not None else "base.branch 해소 실패(ref 부재/fetch 필요)"
    else:
        behind = None
        reason = "기준점 미기록 — `set-base` 로 지정 필요(자동 추론 금지)"
    # submodule pin/drift·dirty — runner 해소 시에만(슬롯 부재/미주입이면 조회 불가 → 빈/False).
    submodules = _submodule_statuses(runner) if runner is not None else []
    dirty = _is_dirty(slot_path(slot), git_runner=runner) if runner is not None else False
    return {"slot": slot, "base": base, "branch": branch, "head": head,
            "behind": behind, "behind_reason": reason,
            "submodules": submodules, "dirty": dirty}


def status(*, task: "str | None" = None, slot: "str | None" = None,
           git_runner: GitRunner | None = None) -> "list[dict]":
    """슬롯 git 구성 일괄 조회 — 단일 슬롯 / `--task` 전 슬롯 / 무인자=내 task 전 슬롯 ().

    대상 슬롯 해소(택일):
      - `slot` 명시 → 그 슬롯 하나(`_normalize_slot` 형식 검증·traversal 차단).
      - `task` 명시 → `slots_for_task(task)`(session==task 이고 leased 인 슬롯).
      - 둘 다 생략 → 내 task 전 슬롯(`slots_for_task(_default_session())` — env/local.conf 유입
        세션 정체성이 보유한 leased 슬롯· "무인자=내 task 전체").
    각 슬롯을 `slot_git_status`(base·branch·head·N behind·submodule pin/drift·dirty)로 조회하고
    `role`(work/readonly·)을 얹어 슬롯별 dict 리스트로 돌려준다. 손-git 불요·조회 전용
    (부작용 0). 미기록 base 는 `behind=None`(CLI `-`·자동 추론 금지).

    `git_runner` 주입 시 그 runner(테스트 hermetic — 모든 슬롯 공용)·미주입이면 슬롯별 실 runner
    (`slot_git_status` 가 슬롯 worktree 바인딩)."""
    if slot is not None:
        slots = [_normalize_slot(slot)]
    elif task is not None:
        slots = [l.slot for l in slots_for_task(task)]
    else:
        slots = [l.slot for l in slots_for_task(_default_session())]
    rows: list[dict] = []
    for s in slots:
        row = slot_git_status(s, git_runner=git_runner)
        row["role"] = _slot_role(s)
        rows.append(row)
    return rows


# ── rebase (단일/일괄·선-검사·충돌 그대로+loud·장부 원자 갱신·자동 rebase 없음) ──
# 슬롯 base 를 사용자 명시로만 옮긴다(자동 rebase 없음). 슬롯마다 독립 처리:
# 선-검사 3종(소유/dirty/rebase 진행중) → 실패면 스킵+loud, 통과면 대상 base 최신 fetch → git rebase.
# 충돌은 **그 상태 그대로 두고 fail-loud**(엔진 임의 abort 금지 — 해소는 사용자 git rebase --continue|
# --abort·다음 부트스트랩 0단계가 "rebase 진행 중" 으로 감지·안내). 장부 갱신은 **성공 시에만**
# 원자적(base.commit=새 base tip·head=새 tip·recorded_at·record_git_snapshot 소비). 기준점 미기록 +
# --onto 없음 = 거부(추론 금지·resolve_rebase_base gate).

REBASE_REBASED = "rebased"        # 성공 — 장부 원자 갱신 완료.
REBASE_SKIPPED = "skipped"        # 선-검사/거부 스킵(loud) — reason 참조.
REBASE_CONFLICT = "conflict"      # rebase 가 rc≠0(충돌 등) — 그 상태 그대로·장부 미갱신·loud.


class RebaseSlotResult:
    """rebase 한 슬롯 하나의 결과 — outcome + 진단 (일괄 요약 원료).

    - `outcome` ∈ {`rebased`(성공·장부 원자 갱신), `skipped`(선-검사/거부 스킵+loud),
      `conflict`(rebase rc≠0·그 상태 그대로 + fail-loud·장부 미갱신)}.
    - `reason` — skipped/conflict 사유(진단·CLI loud surface). skipped: `readonly`/`not-owner`/
      `dirty`/`in-progress`/`no-base`/`unresolvable-onto`/`unregistered`. conflict: git 출력 요약.
    - `base` — rebase 가 향한(또는 향할) base 브랜치명(해소된 경우·None=해소 전 스킵).
    - `new_head` — 성공 시 rebase 후 슬롯 HEAD sha(진단·None=미성공).

    (dataclass 미사용 — Lease/RemoveResult 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다.)"""

    def __init__(self, slot: str, outcome: str, *, reason: "str | None" = None,
                 base: "str | None" = None, new_head: "str | None" = None):
        self.slot = slot
        self.outcome = outcome
        self.reason = reason
        self.base = base
        self.new_head = new_head

    @property
    def ok(self) -> bool:
        return self.outcome == REBASE_REBASED

    def __repr__(self) -> str:
        return (f"RebaseSlotResult(slot={self.slot!r}, outcome={self.outcome!r}, "
                f"reason={self.reason!r}, base={self.base!r}, new_head={self.new_head!r})")


def _rebase_in_progress(slot: str, *, git_runner: GitRunner | None = None) -> bool:
    """슬롯 worktree 에 rebase 가 진행 중인가 — `.git/rebase-merge` | `rebase-apply` 존재 ().

    worktree 슬롯의 per-worktree git 디렉토리 위치를 `git rev-parse --git-path <name>` 로 해소하고
    (worktree 는 `.git` 이 gitdir 를 가리키는 파일이라 직접 경로 조합 불가) 그 경로 실재를 본다. 둘
    중 하나라도 있으면 rebase 진행 중(선-검사 스킵 사유). git 조회 실패·예외 → False(fail-soft·
    진행중 미검출 시 rebase 시도가 git 자체 'already a rebase in progress' 로 rc≠0 → conflict 로
    수렴·이중 안전). `git_runner` 미주입이면 슬롯 worktree 바인딩 실 runner(부재면 False)."""
    runner = git_runner
    if runner is None:
        p = slot_path(slot)
        if not p.exists():
            return False
        runner = _real_git_runner(p)
    for name in ("rebase-merge", "rebase-apply"):
        try:
            rc, out = runner(["rev-parse", "--git-path", name])
        except Exception:  # noqa: BLE001 — fail-soft: 판정 불가는 진행중 아님(rebase 가 이중 안전).
            continue
        if rc != 0:
            continue
        raw = (out or "").strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = slot_path(slot) / raw   # rev-parse --git-path 는 cwd(슬롯) 상대일 수 있다.
        if path.exists():
            return True
    return False


def _rebase_one(slot: str, *, onto: "str | None", owner: str,
                git_runner: GitRunner | None = None) -> RebaseSlotResult:
    """슬롯 하나 rebase — 선-검사 → fetch → git rebase → 성공 시 장부 원자 갱신 ().

    선-검사(스킵+loud·순서): readonly(공유 기준면·mutation 불가) → 소유(`owner` 명의 leased
    아님 — `owner` 는 세션 또는 task 명의·해소는 `rebase`) → dirty(clean 전제) → rebase
    진행 중. 통과하면 `resolve_rebase_base` gate(미기록+onto 없음=거부·
    onto=진행+기록) 로 대상 base 브랜치를 해소하고, `origin/<base>` 최신을 fetch 후 `git
    rebase` 한다. rc≠0(충돌 등) = **그 상태 그대로 두고 conflict**(엔진 임의 abort 금지). 성공 =
    `record_git_snapshot(base_branch, base_commit=새 base tip)` 로 base.commit·head·recorded_at 을
    원자 갱신(). **raise 하지 않는다** — 모든 조건을 RebaseSlotResult 로 돌려 일괄 독립성을
    보장한다(한 슬롯의 예외가 나머지를 막지 않음)."""
    def skip(reason: str, base: "str | None" = None) -> RebaseSlotResult:
        return RebaseSlotResult(slot, REBASE_SKIPPED, reason=reason, base=base)

    # ── 선-검사 (스킵 + loud·독립) ─────────────────────────────────────────
    lease = read_lease_strict(slot)
    if lease is not None and lease.role == _LEASE_ROLE_READONLY:
        return skip("readonly")   # 공유 기준면 — mutation 불가(·refresh 로만 갱신).
    # 소유 판정 = leased lease.session == 내 명의(`owner`) — `release`(`target.session !=
    # owner_task`)와 **같은 장부·같은 비교**다. 명의가 세션이냐 task 냐는 `rebase` 가
    # 해소하고(단일 지점), 여기 판정은 축과 무관하게 하나다.
    if lease is None or lease.state != "leased" or lease.session != owner:
        holder = lease.session if (lease is not None and lease.state == "leased") else ""
        return skip("not-owner" if not holder else f"not-owner:{holder}")
    runner = git_runner
    if runner is None:
        p = slot_path(slot)
        if not p.exists():
            return skip("no-worktree")
        runner = _real_git_runner(p)
    if _is_dirty(slot_path(slot), git_runner=runner):
        return skip("dirty")      # rebase 는 clean 전제(미커밋 변경 유실 방지).
    if _rebase_in_progress(slot, git_runner=runner):
        return skip("in-progress")  # 이미 진행 중 — 사용자 해소(continue/abort) 먼저.

    # ── base 해소 gate (미기록+onto 없음=거부·onto=검증만·**기록은 성공 시에만**) ──
    # record=False: onto 를 rebase *이전에* 기록하지 않는다(해소만 검증) — 이후 충돌/abort 시 장부가
    # 새 base 를 거짓 주장하는 것을 막는다(no-onto 충돌 경로와 대칭·장부는 성공 시에만 원자 갱신).
    try:
        base_branch = resolve_rebase_base(slot, onto=onto, record=False, git_runner=runner)
    except RebaseBaseRequired:
        return skip("no-base")            # 기준점 미기록 + --onto 없음 → 거부(추론 금지).
    except BaseRefUnresolvable:
        return skip("unresolvable-onto")  # --onto ref 해소 실패(오타·미fetch).
    except ReadonlySlotMutation:
        return skip("readonly")           # 방어적(위 role 가드와 이중) — mutation 거부.

    # ── 대상 base 최신 fetch → git rebase ──────────────────────────────────
    target = base_branch
    rc, out = runner(["fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] 슬롯 {slot} `git fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
            f"  {_frozen_fallback_label(base_branch)}로 rebase 를 시도한다 — 네트워크 복구 후 재시도 권장 "
            "(fail-soft).",
            file=sys.stderr,
        )
    else:
        if not base_branch.startswith("origin/"):
            rc2, _ = runner(["show-ref", "--verify", "--quiet",
                             f"refs/remotes/origin/{base_branch}"])
            if rc2 == 0:
                target = f"origin/{base_branch}"
    rc, out = runner(["rebase", target])
    if rc != 0:
        # 충돌(또는 실패) — **그 상태 그대로 두고 fail-loud**. 엔진이 임의 abort 하지 않는다
        # (해소는 사용자 git rebase --continue|--abort). 장부는 **완전 불변**(onto 든 no-onto 든
        # base/head/recorded_at 미갱신·미완 — resolve_rebase_base(record=False)로 onto 를
        # 미리 기록하지 않았기에 가능) → 다음 부트스트랩 0단계가 "rebase
        # 진행 중" 으로 감지·안내한다.
        return RebaseSlotResult(slot, REBASE_CONFLICT, base=base_branch,
                                reason=str(out).strip()[:200])

    # ── 성공 → 장부 원자 갱신(**유일 기록 지점**·base.commit=새 base tip·head=새 tip·recorded_at) ──
    # onto 든 no-onto 든 base 는 여기서만 기록된다(성공 원자 갱신) — 충돌 경로는 장부를 안 건드린다.
    base_tip = _resolve_base_commit(slot, target, git_runner=runner)
    record_git_snapshot(slot, base_branch=base_branch, base_commit=base_tip,
                        git_runner=runner)
    return RebaseSlotResult(slot, REBASE_REBASED, base=base_branch,
                            new_head=_slot_head(runner))


def rebase(slots: "list[str]", *, onto: "str | None" = None,
           owner_task: "str | None" = None,
           git_runner: GitRunner | None = None) -> "list[RebaseSlotResult]":
    """슬롯 base 를 사용자 명시로 rebase — 단일/일괄·슬롯 독립·자동 rebase 없음 ().

    `slots` = 대상 슬롯 식별자 리스트(단일이면 1개·일괄이면 `slots_for_task` 결과). 각 슬롯을
    `_rebase_one` 로 **독립** 처리한다 — 한 슬롯의 충돌/스킵이 나머지를 막지 않는다(일괄 독립성·
    ). `onto` 생략 = 기록된 base.branch 최신으로 rebase(미기록이면 거부). 반환 =
    슬롯별 `RebaseSlotResult` 리스트(호출부가 성공/스킵/충돌 요약).

    **소유 판별 축** = `owner_task`(명시 task 명의) > `_default_session()`(세션 명의):
      - `owner_task` 주어짐(CLI `--task <이름>`·단일/일괄 공통) → **그 task 명의**가 '내 것'의
        정의다. `release(owner_task=)` 의 소유검사와 **동형**(같은 장부·같은 의미: 그 슬롯의
        leased `lease.session` 이 그 명의인가) — 같은 소유를 두 도구가 다르게 판정하던 게 결함의
        본질이었다(이 task 를 1급 축으로 올렸는데 rebase 의 소유 축만 슬롯 세션에 머묾).
      - 미지정 → 종전대로 세션 명의(`_default_session()` — env/local.conf 유입·`_resolve_current_
        slot` 동형)로 판정(슬롯-세션 모드 거동 불변).
    어느 축이든 **검사 자체는 그대로**다 — 그 명의가 leased 로 보유하지 않은 슬롯(다른 task·다른
    슬롯 세션·미점유)은 여전히 `not-owner` 스킵(loud). 축 확장이지 검사 제거가 아니다."""
    owner = owner_task or _default_session()
    return [_rebase_one(s, onto=onto, owner=owner, git_runner=git_runner) for s in slots]


# ── switch (브랜치 전환 + 장부 스냅 재기록 **원자**·0단계 main-참조 remedy) ──
# 엔진이 매개하는 브랜치/head 전환은 전부 자기가 스냅을 재기록한다(bind·create·rebase·refresh·
# set-base·handoff) — 그런데 0단계 main-참조 fault 의 **remedy 만 엔진 밖 raw git**(`git switch -c`)
# 이었다. 그래서 사용자가 안내대로 해소하면 장부 스냅은 옛 브랜치를 가리킨 채라 **곧바로 0단계
# '기록↔live diverged'(
# 차단 → record → 겨우 진입·왕복 2회 강제). 이 커맨드는 전환과 스냅 재기록을 **한 호출**에 담아
# 그 상태전이(remedy-유발 상태전이)를 구조적으로 없앤다.
#   - diverged 검사 자체는 **무변경** — 사람이 raw `git switch` 를 직접 하면 여전히 FAIL-LOUD 로
#     잡힌다(외부 개입 탐지력 손실 0). 엔진 경로만 스냅을 동반한다.
#   - 보호목록 브랜치로의 전환은 **거부** — 이 커맨드의 목적(main-참조 해소)과 정반대다.

# board.DEFAULT_PROTECTED 와 *동형* 폴백 (pm_config._DEFAULT_PROTECTED 동형). board 부재/
# 헬퍼 부재/파싱 실패로 areas 를 못 읽어도 보호는 안전 기본값이 있어야 한다(미해소여도 main 류 차단).
_DEFAULT_PROTECTED = ("main", "master", "develop")


def _load_board():
    """board.py 를 형제 모듈로 동적 로드한다 — **보호목록 조회 전용** (fail-soft None).

    이 모듈은 board 를 import 하지 않는다 — `_load_identity_args`
    `pm_config._load_module`·`pm_bootstrap._load_board` 와 동형의 sibling 동적 로더로,
    단방향(board 는 worktree_pool 을 로드하지 않는다)이라 순환이 없다. `__file__` 앵커(REPO 전역이
    아님) — 테스트가 경로 전역을 tmp 로 재배선해도 형제 파일 위치는 이 파일 옆으로 고정된다.

    부재/로드 실패는 None(호출부 `_resolve_protected` 가 default 폴백) — 보호목록 조회는 *추가
    권위*(areas override)이지 이 커맨드의 필수 전제가 아니다. 단 **형제 rev skew 는 fail-loud**
    (`_verify_engine_rev`)로 재-raise 한다(사본 skew 를 조용한 default 폴백으로 감추지 않음).
    """
    path = Path(__file__).resolve().parent / "board.py"
    if not path.exists():
        return None
    try:
        mod = _load_module_from_path(
            path, "board.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — 로드 실패는 default 폴백(단 skew 는 재-raise).
        if _is_engine_rev_skew(exc):
            raise  # 중첩 로드 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _resolve_protected(repo: str, *, board=None) -> "list[str]":
    """그 repo 의 보호 브랜치 목록 (`pm_config._resolve_repo_protected` 동형).

    areas.md `protected` 칼럼(`board._repo_protected`)이 권위이고, board/헬퍼 부재·파싱 실패만
    `_DEFAULT_PROTECTED` 로 강등한다(크래시 0·보호 기본값 보장). `board` 주입 = 테스트 hermetic
    seam(실 areas.md 를 읽지 않는다)."""
    board_mod = board if board is not None else _load_board()
    repo_protected = getattr(board_mod, "_repo_protected", None) if board_mod else None
    if repo_protected is None:
        return list(_DEFAULT_PROTECTED)
    try:
        return list(repo_protected(repo))
    except Exception as exc:  # noqa: BLE001 — 일반 areas 실패는 default 폴백(단 skew 재전파).
        if _is_engine_rev_skew(exc):
            raise
        return list(_DEFAULT_PROTECTED)


def _normalize_branch_name(branch: str, *, git_runner: GitRunner) -> "str | None":
    """`branch` 를 **git 이 실제로 해석할 브랜치명**으로 정규화한다 — `check-ref-format --branch` 출력.

    task/세션 명이 그대로 브랜치명으로 흐르므로(`fix^2`·`a..b`·공백 등) **git 자신의 판정**으로
    거른다(자체 정규식 재구현 금지·규칙 drift 0). 단 이 커맨드는 rc 만 쓰면 안 된다 — `--branch` 는
    유효성 판정 + **revspec 확장**을 함께 한다(`@{-1}` → 이전 브랜치의 실명·rc 0·실측):

        $ git check-ref-format --branch '@{-1}'   →  main   (rc 0)
        $ git switch '@{-1}'                      →  실제로 main 으로 전환

    rc 만 보고 **원문 문자열**로 보호목록을 비교하면 `@{-1}` ≠ `main` 이라 통과하고, 그 결과 이
    커맨드가 스스로 슬롯을 보호 브랜치에 앉힌다(codex 게이트 must-fix — 이 티켓이 막으려던 상태를
    remedy 가 만든다). 그래서 **출력(정규화된 이름)을 단일 기준**으로 삼고 이후 전 단계(보호목록
    비교·존재 판정·D/F 검사·`git switch` 인자·장부 스냅 기록)에 일관 적용한다. 거부 목록(`@{` 금지)
    방식은 다른 revspec 문법에서 채택하지 않는다(정규화가 클래스를 닫는다).

    반환 = 정규화된 브랜치명. 아래는 전부 None(**fail-closed** 거부 — 정규화 불가 = 안전검사 불가라
    통과시키지 않는다. 옛 `_branch_ref_name_ok` 의 "판정 불가는 통과" fail-soft 를 뒤집는다):
      - rc≠0(ref 규칙 위반) · 호출/예외 실패.
      - 출력이 비었거나 여러 줄·공백 포함(예상 밖 형태 방어).
      - **고정점 아님** — 정규화 결과를 한 번 더 통과시켰을 때 자기 자신이 아니면 거부한다
        (`--branch` rc 0 이 곧 안전은 아니라는 가정).

    ⚠ **이 함수가 브랜치명 수용 규칙의 단일 진실**이다 — 0단계 remedy 의 *후보 생성* 쪽
    (`pm_bootstrap._remedy_branch_ref_ok` → `_remedy_branch_name`)도 판정을 따로 구현하지 않고 이
    함수를 호출한다(제안 쪽과 실행 쪽이 갈리면 안내가 곧바로 실행 불가·codex 게이트 must-fix)."""
    def resolve(name: str) -> "str | None":
        try:
            rc, out = git_runner(["check-ref-format", "--branch", name])
        except Exception:  # noqa: BLE001 — fail-closed: 판정 불가는 거부(안전검사 불가).
            return None
        if rc != 0:
            return None
        text = (out or "").strip()
        if not text or len(text.splitlines()) != 1 or any(ch.isspace() for ch in text):
            return None
        return text

    first = resolve(branch)
    if first is None:
        return None
    return first if resolve(first) == first else None   # 고정점 확인().


def _local_branch_exists(branch: str, *, git_runner: GitRunner) -> bool:
    """슬롯 worktree 에 로컬 브랜치 `refs/heads/<branch>` 가 있는가 — 전환 형태(`-c` 유무) 판정.

    `git show-ref --verify --quiet refs/heads/<branch>` rc 0 = 존재(→ `git switch <b>`), 아니면
    미존재(→ `git switch -c <b>`). 호출/예외 실패는 미존재(fail-soft — `-c` 시도 실패는 git 이
    loud 하게 surface)."""
    try:
        rc, _out = git_runner(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    except Exception:  # noqa: BLE001 — fail-soft: 판정 불가는 미존재(git 이 최종 판정).
        return False
    return rc == 0


def _branch_protected_upstream(branch: str, protected: "list[str]", *,
                               git_runner: GitRunner) -> "str | None":
    """기존 브랜치 `branch` 의 `@{upstream}` 이 **보호 브랜치 원격**이면 그 upstream 명, 아니면 None.

    0단계 main-참조 판정의 **축 2**(`pm_bootstrap._phase0_protected_upstream`·)와 같은 규칙이다:
    upstream `<remote>/<branch>` 의 branch 부분(첫 `/` 이후·`feature/x` 도 보존)이 보호목록이면
    main-참조. 자기 feature 브랜치 추적(`origin/a5`)은 정상 작업 슬롯이라 통과시킨다(오탐 0).

    **기존 브랜치 전환 경로 전용** — `switch -c` 로 새로 파는 브랜치는 upstream 이 없다(실측). 이
    검사가 없으면 `switch <slot> <origin/main 추적 브랜치>` 가 통과하고 **다음 0단계가 다시 main-참조로
    막는다** = 이 티켓이 닫으려는 remedy-유발 상태전이의 다른 축(내부 리뷰 should-fix).

    `git rev-parse --abbrev-ref <branch>@{upstream}`(`_upstream_status` 의 branch-지정 변형·rc 우선
    판정 동형 — `_real_git_runner` 는 stdout+stderr 를 합쳐 주므로 미해소 시 out 이 비어있지 않다).
    미해소(rc≠0·빈 이름)·`/` 없는 이름·호출 실패는 None(fail-soft — 판정 불가는 통과·오탐 0)."""
    try:
        rc, out = git_runner(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"])
    except Exception:  # noqa: BLE001 — fail-soft: 조회 실패는 판정 생략(오탐 0).
        return None
    name = (out or "").strip()
    if rc != 0 or not name or "/" not in name:
        return None
    tracked = name.split("/", 1)[1]     # origin/main → main · origin/feature/x → feature/x
    return name if tracked in protected else None


def _non_branch_ref_kind(name: str, *, git_runner: GitRunner) -> "str | None":
    """`name` 이 **로컬 브랜치가 아닌 ref**(remote-tracking·태그)로도 해소되는가 — 종류 or None.

    `switch` 의 *생성* 경로(로컬 브랜치 미존재)에서 인자가 `origin/main`·태그명이면 "새 작업 브랜치
    이름" 으로 부적절하고, 옛 plain-checkout 폴백에선 **detached HEAD 이동**으로 빠졌다(실측:
    `git checkout origin/main` → `## HEAD (브랜치 없음)`). 게다가 `origin/main` 은 보호목록 `main`
    과 **문자열이 달라 보호검사를 통과**한다 — 그 창을 인자 단계에서 닫는다(codex 게이트 must-fix).

    축 2(`_branch_protected_upstream`)와 **다른 축**이다: 축 2 는 *기존 로컬 브랜치의 upstream*
    을 보고, 이건 *인자 자체가 로컬 브랜치가 아닌 것*을 가리키는 경우다(둘 다 필요).

    `refs/remotes/<name>` → "remote-tracking" · `refs/tags/<name>` → "태그" · 둘 다 아니면 None.
    호출 실패/예외는 None(fail-soft — 판정 불가는 통과·`-b` 생성이 최종 방어)."""
    for ref_prefix, kind in (("refs/remotes/", "remote-tracking ref"), ("refs/tags/", "태그")):
        try:
            rc, _out = git_runner(["show-ref", "--verify", "--quiet", f"{ref_prefix}{name}"])
        except Exception:  # noqa: BLE001 — fail-soft: 판정 불가는 통과.
            continue
        if rc == 0:
            return kind
    return None


def _branch_df_conflict(branch: str, *, git_runner: GitRunner) -> "str | None":
    """`refs/heads/<branch>` 생성이 **접두 부모 ref** 와 D/F 충돌하는가 — 충돌 ref 명 or None.

    브랜치 `task` 가 존재하면 `refs/heads/task/main` 은 `show-ref` 상 **미존재**(→ 생성 전환 경로)
    지만 `git switch -c` 는 `cannot lock ref … 'refs/heads/task' exists` 로 실패한다(ref 는 파일
    시스템 디렉토리/파일 구조라 같은 이름이 파일이면서 디렉토리일 수 없다). 그래서 선-검사에서
    접두 부모(`a/b/c` → `a`, `a/b`)의 실재를 함께 본다.

    ⚠️ 역방향(디렉토리가 이미 있는데 그 이름의 파일 ref 를 만드는 경우 — `task/main` 존재 시
    `task` 생성)은 여기서 안 본다: 그 경우는 `git switch -c` 의 loud 실패(→ `git-error`)로 수렴하고
    사유가 git 메시지에 그대로 실린다(선-검사 범위는 remedy 안내가 실제로 부딪히는 클래스에 한정)."""
    parts = branch.split("/")
    for i in range(1, len(parts)):
        parent = "/".join(parts[:i])
        if _local_branch_exists(parent, git_runner=git_runner):
            return parent
    return None


class SwitchRefused(RuntimeError):
    """`switch <slot> <branch>` 거부 — rc 1 + 사유 ().

    ⚠ **부작용 범위는 사유마다 다르다**(내부 리뷰 should-fix — docstring 이 자기 사유와 모순이면
    안 된다):
      - **선-검사 사유**(unregistered·protected·protected-upstream·no-worktree·dirty·
        rebase-in-progress·invalid-ref·ambiguous-ref·df-conflict) — 전환도 기록도 하지 않는다(부작용 0).
      - **git-error** — `_checkout_required`(checkout+resync)를 *시도한 뒤* 실패(트리 상태는 git 이
        남긴 그대로·장부 불변·`CheckoutFailed` 를 이 사유로 감싼다).
      - **record-failed** — 전환은 **성공**했고 장부 스냅 재기록만 실패(원자성 파손 → 아래 안내).

    `reason` ∈ {"unregistered", "protected", "protected-upstream", "no-worktree", "dirty",
    "invalid-ref", "ambiguous-ref", "df-conflict", "git-error", "record-failed"}:
      - **unregistered** — 장부에 없는 슬롯(스냅 기록 대상이 아니다·`record` 동형 메시지).
      - **protected** — 대상 브랜치가 그 repo 보호목록. 보호브랜치로 *들어가는* 전환은 이
        커맨드의 목적(main-참조 해소)과 정반대다.
      - **protected-upstream** — 대상(기존) 브랜치가 **보호브랜치 원격을 origin-추적**한다().
        전환은 되지만 다음 0단계가 다시 main-참조로 막으므로 여기서 거부한다(remedy-유발 상태전이).
      - **no-worktree** — 슬롯 worktree 경로 부재(실경로·runner 미주입).
      - **dirty** — 미커밋 변경이 있다. 전환이 WIP 를 흔든다(rebase 선-검사 동형).
      - **rebase-in-progress** — 그 슬롯에 rebase 가 진행 중(`.git/rebase-merge|rebase-apply`).
        해소(continue/abort)가 먼저다 — 중간에 브랜치를 옮기면 진행 중 rebase 가 꼬인다
        (`_rebase_one` 선-검사와 동형·같은 프리미티브 `_rebase_in_progress`).
      - **invalid-ref** — git 브랜치명으로 부적합하거나 **정규화 불가**(`check-ref-format --branch`
        rc≠0·빈/이상 출력·비고정점·호출 실패). 정규화가 안 되면 보호목록 등 안전검사를 그 이름
        기준으로 할 수 없으므로 fail-closed 로 거부한다.
      - **ambiguous-ref** — (생성 경로) 인자가 **로컬 브랜치가 아닌 ref**(remote-tracking·태그)를
        가리킨다. `origin/main` 류는 새 작업 브랜치 이름으로 부적절하고, 보호목록(`main`)과 문자열이
        달라 검사를 우회하는 창이었다(옛 plain-checkout 폴백에선 detached 이동).
      - **df-conflict** — 접두 부모 ref 존재로 생성 불가(`refs/heads/task` 있는데 `task/main`).
      - **git-error** — 브랜치 전환(`_checkout_required`) 자체가 실패.
      - **record-failed** — 전환은 됐으나 장부 스냅 재기록이 실패했다(원자성 깨짐 → **loud**:
        조용히 성공 보고하면 다음 0단계가 diverged 로 막힌다 → `record` 안내).
    (`RuntimeError` — `RefreshRefused`/`RebaseBaseRequired` 동형·CLI 파사드가 rc 1 로 surface.)"""

    def __init__(self, slot: str, reason: str, *, detail: str = ""):
        self.slot = slot
        self.reason = reason
        tail = f" — {detail}" if detail else ""
        msg = {
            "unregistered": (f"슬롯 {slot!r} 이 리스 장부에 없다 — switch 는 등록된 슬롯에만 "
                             f"(전환+스냅 재기록을) 수행한다"),
            "protected": (f"슬롯 {slot!r} 을 보호 브랜치로 전환하는 것은 거부한다 — 이 커맨드는 "
                          f"main-참조 상태를 *벗어나는* 전환용이다(보호목록=areas.md `protected`)"),
            "protected-upstream": (f"슬롯 {slot!r} 을 **보호브랜치 원격을 추적하는** 브랜치로 전환하는 것은 "
                                   f"거부한다 — 전환은 되지만 다음 부트스트랩 0단계가 다시 main-참조"
                                   f"(origin-추적)로 막는다. upstream 이 없는 새 작업 브랜치로 "
                                   f"전환하라(`switch {slot} <새-브랜치명>`)"),
            "no-worktree": f"슬롯 {slot!r} 의 worktree 경로가 없다 — 전환할 트리가 없다",
            "dirty": (f"슬롯 {slot!r} 에 미커밋 변경이 있어 switch 를 거부한다 — 브랜치 전환이 WIP 를 "
                      f"흔든다(커밋/stash 후 재시도)"),
            "rebase-in-progress": (f"슬롯 {slot!r} 에 rebase 가 진행 중이라 switch 를 거부한다 — 먼저 "
                                   f"슬롯에서 `git rebase --continue`(충돌 해결 후) 또는 "
                                   f"`git rebase --abort`(취소)로 해소하라(rebase 선-검사 동형)"),
            "invalid-ref": (f"슬롯 {slot!r} 에 지정한 브랜치명이 git ref 규칙에 어긋나거나 실 브랜치명으로 "
                            f"정규화할 수 없다(`git check-ref-format --branch`) — 다른 이름을 지정하라"),
            "ambiguous-ref": (f"슬롯 {slot!r} 에 지정한 이름이 로컬 브랜치가 아니라 다른 ref 를 가리킨다 "
                              f"— 이 커맨드는 그 ref 로 detached 이동하지 않는다. **새 로컬 브랜치명**을 "
                              f"지정하라(예 `switch {slot} task/<작업이름>`). 원격 브랜치를 따라가려면 "
                              f"먼저 로컬 브랜치를 만들고(`git -C {slot} switch -c <이름> <원격ref>`) "
                              f"`record {slot}` 로 스냅을 맞춰라"),
            "df-conflict": (f"슬롯 {slot!r} 에 지정한 브랜치명이 기존 브랜치와 D/F 충돌한다 "
                            f"(접두 부모 ref 가 이미 브랜치로 존재 → `switch -c` 가 `cannot lock ref` "
                            f"로 실패) — 다른 이름을 지정하라"),
            "git-error": f"슬롯 {slot!r} 브랜치 전환 중 git 오류",
            "record-failed": (f"슬롯 {slot!r} 브랜치 전환은 됐으나 장부 스냅 재기록에 실패했다 — 이대로면 "
                              f"다음 0단계가 '기록↔live diverged' 로 막는다. "
                              f"`worktree_pool.py record {slot}` 로 스냅을 재기록하라"),
        }.get(reason, f"슬롯 {slot!r} switch 거부({reason})")
        super().__init__(msg + tail + "")


class SwitchResult:
    """`switch` 성공 결과 — 어느 브랜치로·생성 전환이었는지·재기록된 head (CLI 보고 원료).

    (dataclass 미사용 — Lease/RebaseSlotResult 등과 동일 이유: `spec_from_file_location` 로드 시
    dataclass 의 forward-ref 해소가 깨진다.)"""

    def __init__(self, slot: str, branch: str, *, created: bool, head: "str | None",
                 requested: "str | None" = None):
        self.slot = slot
        self.branch = branch        # **정규화된** 실 브랜치명(검사·전환·장부 기록의 단일 기준).
        self.created = created      # True = 미존재라 `switch -c` 로 생성 전환.
        self.head = head            # 재기록된 장부 스냅의 head(sha).
        self.requested = requested if requested is not None else branch  # 사용자 원문(revspec 가능).

    @property
    def expanded(self) -> bool:
        """원문이 revspec 이라 다른 이름으로 해소됐는가(`@{-1}`→`main`) — 호출부 loud 보고용."""
        return self.requested != self.branch

    def __repr__(self) -> str:
        return (f"SwitchResult(slot={self.slot!r}, branch={self.branch!r}, "
                f"created={self.created!r}, head={self.head!r}, requested={self.requested!r})")


def switch(slot: str, branch: str, *, protected: "list[str] | None" = None,
           git_runner: GitRunner | None = None) -> SwitchResult:
    """슬롯 브랜치를 전환하고 **같은 호출 안에서** 장부 스냅을 재기록한다 — 원자 ().

    0단계 main-참조 fault 의 remedy 를 엔진-매개 단일 커맨드로 만든다. 순서(선-검사는 전부 부작용
    0 — 하나라도 걸리면 전환/기록 어느 것도 하지 않는다):
      1. **readonly 거부**(·`_reject_readonly_mutation`) — 공유 기준면은 mutation 불가.
      2. **장부 등록 확인** — 미등록이면 `SwitchRefused("unregistered")`(스냅 기록 대상 아님).
      3. **worktree/runner 해소** — 실경로(runner 미주입)에서 슬롯 부재면 `no-worktree`.
      4. **dirty 거부 + rebase 진행중 거부** — 전환이 WIP 를 흔든다·rebase 중 전환은 꼬인다
         (`_rebase_one` 선-검사와 같은 프리미티브 `_is_dirty`/`_rebase_in_progress` 재사용).
         ⚠ **소유(내 세션 leased) 검사는 없다** — `record`/`set-base`/`refresh` 와 같은 위치인자
         pool 관리 표면이고, bind 경로 remedy 는 **바인딩 이전**에 실행되므로 소유를 요구하면
         이 커맨드의 주 용처가 막힌다(rebase 의 not-owner 스킵은 task 일괄 표면 특성).
      5. **브랜치명 정규화**(`_normalize_branch_name` — `check-ref-format --branch` 출력) —
         부적합 이름 거부 + **revspec 확장 해소**(`@{-1}`→실명). 이후 **모든 단계가 이 정규화
         이름 기준**이다(codex must-fix — 원문 기준이면 `@{-1}` 이 보호목록 비교를 우회해 이
         커맨드가 스스로 슬롯을 보호브랜치에 앉힌다). 원문과 다르면 stderr 로 **loud 고지**
         (조용히 다른 브랜치로 가지 않는다).
      6. **보호목록 거부** — 정규화된 브랜치가 그 repo 보호목록이면 거부().
         `protected` 주입 시 그 목록(테스트 hermetic), 아니면 `_resolve_protected(repo)`.
      7. **존재 판정**(`refs/heads/<정규화>`) → **기존 브랜치면 `@{upstream}` 보호 추적 거부**
         (·`protected-upstream`) · **미존재(=생성 의도)면 모호 인자 거부**(remote-tracking·
         태그로 해소되면 `ambiguous-ref` — 축 2 와 다른 축) + **D/F 충돌** 선-검사.
      8. **전환** — 기존 프리미티브 `_checkout_required(slot, <정규화>)` **조합**(raw git 금지):
         `_checkout`(`checkout --no-recurse-submodules <b>` → 실패 시 `-B <b>` 생성
         ambient `submodule.recurse=true` override) + 성공 직후 `_resync_submodules_selective`
         (detached=pin 재동기·**on-branch(dev)=skip**·dirty=skip+경고·fail-soft). alloc 의 브랜치
         전환 3분기가 쓰는 그 경로다 — 새 프리미티브를 쓰면 이 submodule 보호가 통째로 빠진다
         (codex 게이트 must-fix). 실패는 `CheckoutFailed` → `git-error`.
      9. **스냅 재기록** — 전환·resync **후에** `record_git_snapshot(slot)`(base 미전달 = 기존 base **보존**·`record`
         서브커맨드와 동형). 실패는 전부 `record-failed` 로 **loud** 수렴한다 — **예외**(장부 IO·
         권한·락)까지 포함(내부 리뷰 must-fix: 옛 코드는 불일치 dict 반환만 잡아 가장 흔한 실패인
         예외가 원시 traceback 으로 샜다) + 판정도 `_cmd_record` 와 **동형**(무변경·branch 불일치·
         None 전부 실패). 원자성이 깨진 채 성공 보고하면 다음 0단계가 diverged 로 막는다.
    반환 = `SwitchResult`(branch=**정규화 이름**·requested=원문·created·head). **diverged 검사는
    무변경** — 사람이 raw `git switch` 를 직접 하면 여전히 FAIL-LOUD 로 잡힌다(탐지력 손실 0)."""
    _reject_readonly_mutation(slot, "switch", git_runner=git_runner)
    lease = read_lease_strict(slot)
    if lease is None:
        raise SwitchRefused(slot, "unregistered")
    repo = getattr(lease, "repo", None) or slot.rpartition("/")[2].rsplit("_", 1)[0]

    runner = git_runner
    if runner is None:
        p = slot_path(slot)
        if not p.exists():
            raise SwitchRefused(slot, "no-worktree", detail=str(p))
        runner = _real_git_runner(p)

    if _is_dirty(slot_path(slot), git_runner=runner):
        raise SwitchRefused(slot, "dirty")
    if _rebase_in_progress(slot, git_runner=runner):
        raise SwitchRefused(slot, "rebase-in-progress")   # `_rebase_one` 선-검사와 동형 프리미티브.

    # 브랜치명 정규화 — **이 아래 전 단계의 단일 기준**(원문 아님·revspec 확장 해소·codex must-fix).
    target = _normalize_branch_name(branch, git_runner=runner)
    if target is None:
        raise SwitchRefused(slot, "invalid-ref", detail=f"`{branch}`")
    expansion = "" if target == branch else f"입력 `{branch}` → `{target}` 로 해소됨(revspec 확장)"
    if expansion:
        # 조용한 오전환 방지 — 사용자가 친 문자열과 실제 대상이 다르면 즉시 고지(거부 경로 포함).
        print(f"[알림] 슬롯 {slot} switch: {expansion} — 보호목록 검사·전환·장부 기록은 전부 "
              f"`{target}` 기준으로 진행한다.", file=sys.stderr)

    protected_list = protected if protected is not None else _resolve_protected(repo)
    if target in protected_list:
        detail = f"`{target}`" + (f" ({expansion})" if expansion else "")
        raise SwitchRefused(slot, "protected", detail=detail)

    exists = _local_branch_exists(target, git_runner=runner)
    if exists:
        # ()보호브랜치 원격을 추적하는 기존 브랜치로 가면 다음 0단계가 다시 main-참조로
        # 막는다(remedy 가 다른 축의 fault 를 만든다). 새 브랜치(`-c`)는 upstream 이 없어 무관.
        tracked = _branch_protected_upstream(target, protected_list, git_runner=runner)
        if tracked is not None:
            raise SwitchRefused(slot, "protected-upstream",
                                detail=f"`{target}` → `{tracked}` 추적")
    else:
        # 생성 의도인데 인자가 remote-tracking ref·태그를 가리킨다 = "새 브랜치 이름" 으로 부적절
        # (옛 plain-checkout 폴백에선 detached 이동 + 보호목록 문자열 우회 창이었다).
        kind = _non_branch_ref_kind(target, git_runner=runner)
        if kind is not None:
            raise SwitchRefused(slot, "ambiguous-ref", detail=f"`{target}` = {kind}")
        parent = _branch_df_conflict(target, git_runner=runner)
        if parent is not None:
            raise SwitchRefused(slot, "df-conflict",
                                detail=f"`{target}` vs 기존 브랜치 `{parent}`")

    # ── 전환 = 기존 프리미티브 조합(raw git 금지) ────────────────────────────
    # `_checkout_required` = `_checkout`(--no-recurse-submodules·미존재면 `-B` 생성) +
    # `_resync_submodules_selective`(on-branch dev 보호·detached 재동기·dirty skip+경고·fail-soft).
    # ambient `submodule.recurse=true` 환경에서 raw 전환은 작업 중 submodule 을
    # detached pin 으로 파괴한다 — 그 보호는 이 쌍에만 있다(alloc 전환 3분기와 동일 경로).
    # `create_only=not exists` — 생성 의도면 **비파괴 `-b` 만**(plain 폴백 금지: DWIM 자동 tracking·
    # detached 이동 회피 / `-B` 금지: 판정↔실행 경합에도 기존 브랜치 리셋 0).
    try:
        _checkout_required(slot, target, create_only=not exists, git_runner=runner)
    except CheckoutFailed as exc:
        raise SwitchRefused(slot, "git-error",
                            detail=f"브랜치 전환 실패: {str(exc.output).strip()[:200]}") from exc

    # ── 스냅 재기록(원자 짝) — base 미전달 = 기존 base 보존(`record` 동형·arrival 규칙) ──
    # 여기서부터는 **전환이 이미 일어난 뒤**다. 어떤 실패든 조용히 통과시키면 장부가 stale 인 채
    # 성공 보고가 나가고, 사용자는 다음 0단계에서 diverged 차단을 만난다 → 전부 `record-failed`
    # 로 수렴해 해소 커맨드(`record <slot>`)를 안내한다.
    before_git = lease.git if isinstance(getattr(lease, "git", None), dict) else None
    try:
        updated = record_git_snapshot(slot, git_runner=git_runner)
    except Exception as exc:  # noqa: BLE001 — must-fix: 장부 IO/권한/락 예외도 안내 채널로 수렴.
        converted = SwitchRefused(slot, "record-failed", detail=f"장부 기록 예외: {exc}")
        if _is_engine_rev_skew(exc):
            converted._engine_rev_skew = True
        raise converted from exc
    recorded = updated.git if (updated is not None and isinstance(updated.git, dict)) else None
    # 판정은 `_cmd_record` 와 동형 — 무변경(스냅 불가로 기존 git 보존)도 실패로 본다. branch 만
    # 보면 "기록=X·live=main diverged 에서 switch X" 처럼 **이미 X 로 기록된** 장부가 stale head 를
    # 지닌 채 성공으로 통과한다(내부 리뷰 should-fix). 이미 그 브랜치·완전 동일 스냅이면 보수적
    # loud(=record 동형·재실행 안내)로 기운다 — 조용한 stale 보다 낫다.
    if recorded is None or recorded == before_git or recorded.get("branch") != target:
        raise SwitchRefused(slot, "record-failed",
                            detail=f"기록된 branch=`{(recorded or {}).get('branch')}`"
                                   f"{'·무변경(스냅 불가)' if recorded is not None and recorded == before_git else ''}")
    return SwitchResult(slot, target, created=not exists, head=recorded.get("head"),
                        requested=branch)


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────


def _rollback_worktree(repo: str, slot_path_: Path, *, git_runner: GitRunner | None = None) -> None:
    """`git worktree add` 성공 후 단계가 실패했을 때 만든 worktree 를 롤백한다.

    bare 컨텍스트(`.repos/<repo>.git`)에서 `git worktree remove <slot_path> --force` 를
    부른다 — `add` 가 거기서 일어났으므로 `remove` 도 같은 컨텍스트라야 한다(공유 .git 원
    = bare). 실패하면 best-effort 로 `worktree prune` 폴백.

    **best-effort·2차 예외 삼킴 금지**: 이 함수는 절대 raise 하지 않는다(롤백 자체가 실패해도
    원래 에러로 raise 되도록·호출부가 finally/except 에서 부른다). 댕글링 worktree("슬롯
    없음"+재시도 "이미 존재")가 안 생기게 fs 를 정리하는 게 목적이고, 정리 실패는 원래
    에러를 가린다(2차 예외)는 더 나쁜 결과를 부르므로 조용히 best-effort 한다.
    """
    runner = git_runner or _real_git_runner(bare_repo_path(repo))
    try:
        rc, _out = runner(["worktree", "remove", str(slot_path_), "--force"])
        if rc != 0:
            runner(["worktree", "prune"])  # 폴백 — 등록 메타만이라도 정리.
    except Exception:  # noqa: BLE001 — best-effort: 롤백 실패가 원래 에러를 가리면 안 됨.
        pass


def _checkout(slot_path_: Path, branch: str, *, create_only: bool = False,
              git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 에서 브랜치 체크아웃 (브랜치 변경 = 같은 슬롯 재체크아웃).

    `git checkout --no-recurse-submodules <branch>`. 브랜치가 없으면 새로 만든다(`-B`) — 풀
    슬롯에 새 작업스트림을 붙이는 정상 경로. (같은 브랜치 동시 2-worktree checkout 은 git 이
    거부)

    **`create_only=True` (`switch` 생성 경로 전용·기본값은 현행 동작 불변)**: plain checkout
    폴백 없이 **비파괴 생성**(`-b <branch>`)만 시도한다. 기본 경로의 "plain → 실패하면 `-B`" 는
    *전환 의도*엔 맞지만 **생성 의도엔 위험**하다 — 로컬 브랜치가 없어도 plain checkout 이 성공해
    버리는 두 경우(실측):
      - `origin/<b>` 가 있으면 **DWIM 자동 tracking 브랜치 생성**(의도한 "새 작업 브랜치"가 아니다).
      - 인자가 remote-tracking ref·태그를 가리키면 **detached HEAD** 로 이동(`git checkout
        origin/main` → `## HEAD (브랜치 없음)`).
    `-b`(create-or-fail) 는 `-B`(create-or-**reset**)와 달리 기존 브랜치를 리셋하지 않는다 —
    존재 판정과 실행 사이의 경합/판정 오차에도 **데이터 유실 0**(`worktree add -B` 의
    보존-브랜치 리셋 유실을 닫은 것과 같은 결론·거기선 존재 선-판정으로 갈랐고 여기선 실행 자체를
    비파괴로 고정해 한 단계 더 잠근다). 이미 있으면 rc≠0 로 **loud 실패**(조용한 리셋보다 낫다).

    **`--no-recurse-submodules`**: 사용자 환경(전역
    `~/.gitconfig` 또는 repo config)에 `submodule.recurse=true` 가 설정돼 있으면 plain
    `git checkout` 이 *selective resync 전에* submodule 을 재귀 갱신해 on-branch(dev) submodule
    을 detached pin 으로 낚아챈다 — dev 파괴.
    양 checkout 호출에 `--no-recurse-submodules`(git 2.13+·2.43 확인)를 박아 ambient config 를
    override → checkout 은 submodule 을 절대 안 건드리고 `_resync_submodules_selective` 가
    submodule 상태의 **유일 권위**가 된다.
    """
    runner = git_runner or _real_git_runner(slot_path_)
    if create_only:
        return runner(["checkout", "--no-recurse-submodules", "-b", branch])
    rc, out = runner(["checkout", "--no-recurse-submodules", branch])
    if rc != 0:
        rc, out = runner(["checkout", "--no-recurse-submodules", "-B", branch])
    return rc, out


def _parse_submodule_entries(status_out: str) -> list[tuple[str, str]]:
    """`git submodule status` 출력에서 `(flag, path)` 를 뽑는다 (git 2.43).

    각 라인 형식 = `<flag><40-hex-sha> <path>[ (<describe>)]` — **선두 1글자가 status 플래그**
    (`' '`=index pin 과 일치·`'+'`=working≠pin·`'-'`=미초기화·`'U'`=충돌)다. 플래그는 항상
    라인 첫 글자(`line[0]`)이고(공백 플래그도 포함), sha 뒤 첫 whitespace 이후부터 선택적인
    마지막 ` (<describe>)` 직전까지가 경로다. 경로 자체에는 공백이 올 수 있으므로 전체
    whitespace 토큰 분리는 쓰지 않는다. describe 는 git 2.43 구현상 `' '`/`'+'` 상태에만
    출력되므로 그 두 flag 에서만 걷는다 — `-`(미초기화)·`U`(충돌) 행의 괄호로 끝나는
    경로는 그대로 보존한다. 빈 출력(submodule 없음)·sha/경로 경계가 없는 행은
    건너뛴다.

    `flag` 는 pin↔working 판정에 필요하다(`+`=drift·`' '`=pinned). 경로만
    필요한 호출부(`_resync_submodules_selective`)는 `_parse_submodule_paths` 를 쓴다(경로만 뽑음).
    """
    entries: list[tuple[str, str]] = []
    for line in status_out.splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) >= 2:
            flag = line[0]
            path = parts[1]
            if flag in (" ", "+") and path.endswith(")"):
                candidate, separator, describe = path.rpartition(" (")
                if separator and describe[:-1] and not any(
                    char.isspace() for char in describe[:-1]
                ):
                    path = candidate
            entries.append((flag, path))
    return entries


def _parse_submodule_paths(status_out: str) -> list[str]:
    """`git submodule status` 출력에서 submodule 경로(superproject-상대)만 뽑는다 (git 2.43).

    `_parse_submodule_entries` 의 경로만 반환하는 얇은 래퍼 — 플래그가 불필요한 호출부
    (`_resync_submodules_selective`)의 기존 인터페이스를 그대로 보존한다(행동 불변).
    """
    return [path for _flag, path in _parse_submodule_entries(status_out)]


def _submodule_dirty(sub: str, runner: GitRunner) -> bool:
    """submodule 워킹트리에 미커밋 변경(untracked 포함)이 있는지 — `_is_dirty` 동형(sub 컨텍스트).

    superproject-바인딩 runner 로 `git -C <sub> status --porcelain` 을 돌려 그 submodule
    워킹트리를 본다(다중 `-C` = superproject cwd 기준 sub 로 재진입). porcelain 엔트리 라인이
    하나라도 있으면 dirty. rc≠0(상태 조회 실패)은 **보수적으로 dirty** 로 본다 — `_is_dirty`
    와 같은 규율(모르면 안 날린다·미커밋 작업 유실 방지). stderr 경고 오탐은
    `_porcelain_status_lines` 로 걸러진다(`_is_dirty` 와 동일 가드).
    """
    rc, out = runner(["-C", sub, "status", "--porcelain"])
    if rc != 0:
        return True
    return len(_porcelain_status_lines(out)) > 0


def _upstream_status(runner: GitRunner) -> tuple[str | None, bool]:
    """슬롯 브랜치의 `@{upstream}` 추적 브랜치명 + 해소 여부.

    `git rev-parse --abbrev-ref @{upstream}` — 해소되면 rc0 + 추적 브랜치명(예 `origin/a5`),
    미설정이면 rc≠0(`fatal: no upstream configured …`)이다. **rc 를 먼저 본다** — `_real_git_runner`
    가 stdout+stderr 를 합쳐 돌려주므로미해소 시 out 이 fatal 메시지로 *비어있지 않다*.
    rc≠0 또는 빈 이름이면 `(None, False)`(미해소·부트스트랩이 경고), 해소면 `(name, True)`.
    """
    rc, out = runner(["rev-parse", "--abbrev-ref", "@{upstream}"])
    name = out.strip()
    if rc != 0 or not name:
        return None, False
    return name, True


def _submodule_statuses(runner: GitRunner) -> list[SubmoduleStatus]:
    """각 submodule 을 역할별로 판정한 `SubmoduleStatus` 리스트.

    `_resync_submodules_selective` 와 *같은* primitive 로 역할을 정한다(중복 판별 구현 금지):
    `git submodule status`(`_parse_submodule_entries` — flag+path) + submodule 당
    `git -C <sub> symbolic-ref -q HEAD`(rc0=on-branch/dev·rc≠0=detached) + `_submodule_dirty`.

      - on-branch(dev) → `"dev-ahead"`(정보). 사용자가 그 submodule 에서 브랜치를 파 작업 중.
      - detached & flag `-`(미초기화) → `"uninitialized"`(경고·슬롯 init 비정상).
      - detached & flag 공백(pin==working) → `"pinned"`(정상).
      - detached & 그 외 flag(`+`/`U`·pin≠working) → `"drift"`(경고).

    fail-soft: `git submodule status` rc≠0(조회 불가/submodule 없음)이면 **빈 리스트**
    (부트스트랩이 submodule 줄 생략). dirty 는 *왜* drift 가 안 풀렸는지 surface 용
    dirty detached 를 재동기 skip → drift 잔존).
    """
    rc, out = runner(["submodule", "status"])
    if rc != 0:
        return []  # 조회 불가(대개 submodule 없음/손상) → 빈 목록(부트스트랩 줄 생략).
    statuses: list[SubmoduleStatus] = []
    for flag, sub in _parse_submodule_entries(out):
        rc_head, _out = runner(["-C", sub, "symbolic-ref", "-q", "HEAD"])
        if rc_head == 0:
            kind = "dev-ahead"       # on-branch = dev 역할(정보·경고 아님).
        elif flag == "-":
            kind = "uninitialized"   # 미초기화(경고).
        elif flag == " ":
            kind = "pinned"          # detached & pin == working(정상).
        else:
            kind = "drift"           # detached & pin ≠ working('+'/'U' → 경고).
        # dirty 는 미초기화 submodule 엔 무의미하다(워킹트리 부재 → `status --porcelain` rc≠0 을
        # `_submodule_dirty` 가 보수적 True 로 → `⚠ uninitialized ·dirty` 잉여 렌더). uninitialized
        # 는 skip(False)·나머지만 `_submodule_dirty` 재사용(불필요한 `-C status` 호출도 절약·reviewer).
        dirty = False if kind == "uninitialized" else _submodule_dirty(sub, runner)
        statuses.append(
            SubmoduleStatus(sub, kind, warning=kind in ("drift", "uninitialized"), dirty=dirty)
        )
    return statuses


def _resync_submodules_selective(slot_path_: Path, *, git_runner: GitRunner | None = None) -> None:
    """브랜치 전환(`_checkout`) 성공 후 submodule 을 **선택적으로** superproject pin 에 재동기.

    worktree 풀 슬롯의 브랜치를 바꾸면 superproject 는 새 브랜치의 submodule pin 을 가리키지만
    submodule 워킹트리는 이전 pin 그대로라 drift 가 생긴다. 브랜치 전환
    직후 각 submodule 을 **역할별로** 재동기한다 — 역할은 별도 장부 없이 submodule 의 live git
    HEAD 로 판별한다(무스키마·기본 A):

      - **on-branch submodule (= dev 역할·`symbolic-ref -q HEAD` rc0)**: **skip**. 사용자가 그
        submodule 에서 브랜치를 파 작업 중이므로 detached pin 으로 낚아채지 않는다(전역
        `submodule.recurse=true` 가 dev 브랜치를 파괴하던 크럭스 A → *selective* 인 이유).
      - **detached submodule (= consume 역할·`symbolic-ref -q HEAD` rc≠0)**: superproject pin
        으로 `git submodule update --init --recursive --force -- <sub>` 재동기. 단 워킹트리가
        **dirty(미커밋 변경)면 skip + 경고** — 재동기가 미커밋 작업을 날리지 않게 보호.
      - **submodule 없는 repo**: `git submodule status` 가 rc0·빈 출력 → no-op.

    fail-soft(브랜치 전환은 이미 성공했고 submodule 재동기 실패가 checkout 을 되돌리지 않는다·
    raise 금지): `submodule status` rc≠0(조회 불가)면 no-op 반환. update 실패는 경고만 하고
    drift 잔존을 surface 한다(침묵 무력화 금지). `git_runner` 미주입 시 슬롯 worktree 바인딩
    `_real_git_runner(slot_path_)` 로 해소(기존 DI seam 패턴·`_is_dirty` 동형) — 모든 submodule
    git 호출을 이 superproject-바인딩 runner + `-C <sub>` 로 돌려 테스트 mock 가능(hermetic).
    """
    runner = git_runner or _real_git_runner(slot_path_)
    rc, out = runner(["submodule", "status"])
    if rc != 0:
        return  # 조회 불가(대개 submodule 없음/손상) → no-op fail-soft(checkout 은 이미 성공).
    for sub in _parse_submodule_paths(out):
        # 역할 = live git HEAD(장부 없음). on-branch(dev) → 보호(skip).
        rc_head, _out = runner(["-C", sub, "symbolic-ref", "-q", "HEAD"])
        if rc_head == 0:
            continue
        # detached(consume) → pin 재동기. 단 dirty(미커밋)면 작업 유실 방지 위해 skip + 경고.
        if _submodule_dirty(sub, runner):
            print(
                f"[경고] submodule {sub!r} 이 detached 이나 미커밋 변경으로 dirty — pin "
                f"재동기 skip (작업 보호). 정리 후 재-alloc 하면 재동기된다.",
                file=sys.stderr,
            )
            continue
        rc_up, out_up = runner(
            ["submodule", "update", "--init", "--recursive", "--force", "--", sub]
        )
        if rc_up != 0:
            print(
                f"[경고] submodule {sub!r} pin 재동기 실패 (rc={rc_up}): "
                f"{str(out_up).strip()[:200]} — drift 잔존 가능(fail-soft·checkout 은 성공).",
                file=sys.stderr,
            )


def _checkout_required(slot: str, branch: str, *, create_only: bool = False,
                       git_runner: GitRunner | None = None) -> None:
    """`_checkout` 을 부르고 실패(rc≠0)면 `CheckoutFailed` raise.

    fail-soft 로 무시하면 호출부가 장부 branch/state 를 성공처럼 갱신해 장부↔실제 worktree
    branch 가 어긋난다. 성공해야만 호출부가 장부를 갱신하도록 강제하는 가드.

    체크아웃 성공 직후 `_resync_submodules_selective` 로 submodule 을 새 브랜치 pin 에 selective
    재동기한다(detached=consume 만 재동기·on-branch=dev skip·dirty
    skip+경고). *브랜치 전환* 경로(alloc 세 checkout 분기)에만 붙는다 — `create_slot` 최초 init
    은 이 함수를 안 타고 자체 `submodule update --init --recursive --force`(fresh=전부 detached)를
    유지한다. 재동기는 fail-soft(raise 안 함)라 checkout 성공을 되돌리지 않는다.

    `create_only` 는 `_checkout` 으로 그대로 전달한다(`switch` 생성 경로 — 비파괴 `-b` 만
    시도·DWIM tracking/detached 회피). **기본값 False = 현행 동작 불변**(alloc 전환 3분기 무영향).
    selective 재동기는 두 경로에서 동일하게 붙는다.
    """
    rc, out = _checkout(slot_path(slot), branch, create_only=create_only, git_runner=git_runner)
    if rc != 0:
        raise CheckoutFailed(slot, branch, out)
    _resync_submodules_selective(slot_path(slot), git_runner=git_runner)


def _local_conf_session() -> str | None:
    """`.project_manager/local.conf` 의 `session=` (없거나 OSError → None).

    board.py 를 import 하지 않으므로(touches 격리·병렬충돌 회피)
    `board.local_config().get("session")` 와 *동일 의미*를 stdlib 로 자체 구현한다 —
    plain `KEY=value`·`#` 주석/빈 줄 무시. 부재/읽기실패는 None(폴백).
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = file_lock.read_text_shared(conf_file, encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == "session":
            return val.strip() or None
    return None


def _leased_sessions() -> list[str]:
    """lease 장부 state=="leased" 행들의 session 목록 (count-based 유도).

    `_default_session` 이 lease 취득 *전*(lock 밖)에 호출하므로 lock 없는 point-read 로 장부
    파일(`LEASES_FILE`)을 직접 읽는다 — `_read_ledger`(lock 보유 전제)와 별도. 리스는
    원자 교체(`file_lock.atomic_replace`)로 쓰므로 lock 없는 read 도 일관 스냅샷을 본다(board.py
    `_leased_sessions` 와 *동형*). 장부 부재/파싱실패/손상은 빈 리스트(fail-soft). session 이
    빈/None 인 행은 제외.
    """
    if not LEASES_FILE.exists():
        return []
    try:
        data = json.loads(file_lock.read_text_shared(LEASES_FILE, encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    rows = data.get("leases", [])
    if not isinstance(rows, list):
        return []
    sessions: list[str] = []
    for row in rows:
        if isinstance(row, dict) and row.get("state") == "leased":
            sess = row.get("session")
            if sess:
                sessions.append(sess)
    return sessions


def _default_session() -> str:
    """세션 식별자 기본값 — board.py `session_name()` 과 *동형* 우선순위:
    `$PM_SESSION_NAME` env > `$CLAUDE_SESSION_NAME` env(deprecated alias·silent) >
    lease 장부 state=="leased" 행이 정확히 1개면 그 session (단일-lease 유도) >
    (장부 부재·leased 0 = solo) `local.conf session=` > `<host>-<pid>`.

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias
    (둘 다면 PM 승·조용히 동작). **leased ≥2 (모호)면 local.conf 층을 건너뛰고 `<host>-<pid>` 로
    간다**(board.session_name 과 동형 — 저장 쪽지로 남의 세션 행세 차단).

    board.session_name 과 **tail 만 다르다**: 여기는 lease *취득*의 국소 임시 명명이라 미해소를
    None/fail 로 두지 않고 `<host>-<pid>` 로 폴백한다(host-pid 최종 폴백은 세션-귀속
    아닌 국소 용처에만 잔존). board.py 를 import 하지 않으므로(touches
    격리·병렬충돌 회피) 같은 해소를 자체 구현한다. 저장측(여기)과 매칭측(board.session_name)이
    어긋나면 per-slot test_cmd 가 미스되므로 세 모듈을 같은 우선순위로 통일한다.
    """
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    leased = _leased_sessions()
    if len(leased) == 1:
        return leased[0]
    if not leased:
        # 장부 부재·leased 0 = solo → legacy local.conf 폴백 (후방호환).
        conf_sess = _local_conf_session()
        if conf_sess:
            return conf_sess
    # leased ≥2 (모호) 또는 solo 무바인딩 → 국소 임시 명명 host-pid (lease 취득 전용 잔존).
    import socket
    return f"{socket.gethostname()}-{os.getpid()}"


# ── 운영중 관리 backbone: dev/sync + CLI ─────────
# worktree/submodule 운영 *중* 관리를 명령어化 하기 위한 두 backbone + argparse 진입점.
# pm-worktree 스킬(어댑터·PM authoring)이 이 커맨드를 얇게 래핑한다 — 백본 로직은 전부 여기.
#   - dev <sub> <branch> : 슬롯 worktree 의 submodule 을 dev 브랜치로 지정(on-branch 화) → 이후
#       selective resync(`_resync_submodules_selective`)가 그 submodule 을 dev 로 판별해 skip
#       (detached pin 으로 낚아채지 않음·live-HEAD 모델). "내가 작업 중" 선언.
#   - sync              : 현재 슬롯의 `_resync_submodules_selective` 를 수동 트리거(브랜치 전환
#       없이 명시 재동기·detached=pin 재동기·on-branch=skip·dirty=skip+경고).
# 얇게 — 기존 primitive(`_resync_submodules_selective`·submodule 판별)를 재사용한다(중복 금지).


def dev(slot: str, sub: str, branch: str, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 의 submodule 을 dev 브랜치로 지정한다 — on-branch 화.

    `git -C <sub> checkout -b <branch>`(신규 생성)로 submodule 을 on-branch 로 만든다. 브랜치가
    이미 있으면 `-b` 가 rc≠0 이므로 `git -C <sub> checkout <branch>`(전환)로 폴백한다(`_checkout`
    의 create-or-switch 정신과 정합·단 submodule 컨텍스트). 결과적으로 그 submodule 의 live git
    HEAD 가 symbolic ref(on-branch)가 되어, 이후 `_resync_submodules_selective`가 그
    submodule 을 **dev 역할로 판별해 skip** 한다(detached pin 으로 낚아채지 않음)
    — 즉 "이 submodule 은 내가 작업 중이니 pin 재동기로 건드리지 마" 선언(무스키마·역할은 별도
    장부 없이 submodule HEAD 로 정함).

    **`sub` 슬롯-경계 검증(codex must-fix)**: `dev` 는 실 git side-effect(`checkout`)를
    caller-공급 경로에 낸다. 이 도구는 LLM-구동 pm-internal 이라 `sub` 가 자연어에서 구성된다
    (hallucination/오타) — 절대경로(`/etc/...`)·`..` traversal·목록 밖 경로를 그대로 `git -C <sub>
    checkout` 에 넘기면 **슬롯 밖 다른 git repo 에 checkout** 을 실행해 "대상 슬롯 운영" 경계를
    깬다. 그래서 checkout 전에 `sub` 를 그 슬롯의 실제 submodule 목록(`git submodule status` →
    `_parse_submodule_paths`)과 **대조(allowlist)** 하고, 목록 밖이면 `SubmoduleNotInSlot` raise
    (fail-closed — status 조회 실패/submodule 없음도 빈 목록 → 거부). allowlist 라 절대경로·
    traversal 은 자동 거부(목록의 값은 항상 슬롯-상대 submodule 경로).

    `sub` 는 **슬롯 worktree 상대 submodule 경로**(`git submodule status` 가 나열하는 경로·예
    `vendor/sub`). runner 는 슬롯 worktree 바인딩(`_real_git_runner(slot_path(slot))`) + 다중 `-C`
    (`["-C", sub, ...]`)로 superproject cwd 기준 submodule 에 재진입한다(`_submodule_dirty`·
    `_resync_submodules_selective` 와 동형 배선). `(rc, out)` 반환 — 호출부(CLI)가 rc≠0 를 명시
    에러로 surface 한다. `git_runner` 주입 시 그 runner(테스트 mock·DI seam 보존).

    **readonly 거부()**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
    dev 는 submodule 을 on-branch 로 checkout 하는 mutation 이라 read-only 기준면엔 불가(진입 가드).
    """
    _reject_readonly_mutation(slot, "dev", git_runner=git_runner)
    runner = git_runner or _real_git_runner(slot_path(slot))
    # 슬롯 경계 검증(must-fix 1) — sub 를 슬롯 실제 submodule 목록과 대조(allowlist·fail-closed).
    rc_st, out_st = runner(["submodule", "status"])
    known = _parse_submodule_paths(out_st) if rc_st == 0 else []
    if sub not in known:
        raise SubmoduleNotInSlot(slot, sub, known)
    rc, out = runner(["-C", sub, "checkout", "-b", branch])
    if rc != 0:
        # `-b` 실패 원인 판별(codex bundle) — 브랜치가 *이미 존재*할 때만 전환 폴백한다. 그 외
        # (충돌/lock/잘못된 브랜치명)의 `-b` 실패까지 폴백하면 진단이 흐려지므로, `show-ref
        # --verify --quiet refs/heads/<branch>` rc0(존재)일 때만 plain `checkout <branch>` 로
        # 전환하고, 미존재(rc≠0)면 원 `-b` 실패(rc/out)를 그대로 전파한다(원인 명확).
        rc_ref, _out_ref = runner(
            ["-C", sub, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        )
        if rc_ref == 0:
            rc, out = runner(["-C", sub, "checkout", branch])
    return rc, out


def sync(slot: str, *, git_runner: GitRunner | None = None) -> None:
    """현재 슬롯의 submodule 을 superproject pin 에 selective 재동기(수동 트리거) —.

    `_resync_submodules_selective` 를 **브랜치 전환 없이** 수동으로 부른다 — 브랜치를
    바꾸지 않고 명시적으로 submodule 상태를 pin 에 맞추고 싶을 때의 진입(부트스트랩/checkout 경로
    밖). 판별·거동은 전부 그 backbone 이 소유한다(중복 구현 금지·얇은 트리거):
      - detached(consume) & clean → `git submodule update --init --recursive --force -- <sub>` 로 pin 재동기.
      - on-branch(dev) → skip(dev 작업 보호·크럭스 A).
      - detached & dirty → skip + 경고(미커밋 작업 유실 방지).
      - submodule 없는 슬롯 → no-op.
    fail-soft(raise 금지·경고는 stderr) — `_resync_submodules_selective` 계약 상속. `git_runner`
    주입 시 그 runner(테스트 mock·DI seam), 미주입이면 슬롯 worktree 바인딩 실 runner.

    **readonly 거부()**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
    sync 는 submodule 을 pin 으로 재동기(checkout)하는 mutation 이라 read-only 기준면엔 불가(진입
    가드·fail-soft 계약보다 우선 — 이건 op 전면 거부지 조회 실패가 아니다).
    """
    _reject_readonly_mutation(slot, "sync", git_runner=git_runner)
    _resync_submodules_selective(slot_path(slot), git_runner=git_runner)


class SlotResolutionError(RuntimeError):
    """CLI dev/sync 의 현재-슬롯 자동해소 실패 — 매칭 leased 슬롯 0개/≥2(모호) 또는 슬롯 형식 오류.

    메시지는 세션명 + 후보 슬롯(또는 형식 위반) + `--slot` 안내를 담는다(호출부 main 이 그대로
    stderr surface). (`RuntimeError` 서브클래스 — CLI main 이 `except SlotResolutionError` 로 rc 1.)
    """


class SubmoduleNotInSlot(ValueError):
    """dev 대상 submodule 이 슬롯의 실제 submodule 목록에 없다 — 슬롯 경계 밖 checkout 차단 (must-fix).

    LLM-구동 pm-internal 도구가 실 checkout side-effect 를 caller-공급 경로에 내므로, 목록 밖
    경로(절대경로·`..` traversal·오타)를 fail-closed 로 거부한다. `known` = 검증에 쓴 슬롯의
    실제 submodule 경로 목록(진단용·CLI main 이 메시지에 surface).
    """

    def __init__(self, slot: str, sub: str, known: "list[str]"):
        self.slot = slot
        self.sub = sub
        self.known = known
        super().__init__(
            f"submodule {sub!r} 이 슬롯 {slot} 의 submodule 목록에 없다 "
            f"(등록: {known if known else '(없음/조회 실패)'}) — 슬롯 밖 경로·오타 거부(경계 보호)."
        )


# 슬롯 식별자 형식 — `work/<repo>_<N>`(<repo>=선두 alnum 슬러그·<N>=숫자). 앵커 + `/` 불허라
# `slot_path` 결합이 슬롯 루트를 벗어날 수 없다(traversal·빈값·`work/` 단독 거부·must-fix 2).
_SLOT_ID_RE = re.compile(r"^work/[A-Za-z0-9][A-Za-z0-9_.-]*_\d+$")


def _normalize_slot(slot_arg: str) -> str:
    """`--slot` 값을 슬롯 식별자 정규형(`work/<repo>_<N>`)으로 정규화 + 형식 검증 (must-fix 2).

    스킬/사용자가 `work/A_1`(정규형) 또는 `A_1`(접두 생략) 어느 쪽으로 줘도 받는다 —
    `slot_path`/장부의 슬롯 식별자 관례(`work/<repo>_<N>`·`_slot_for`)와 정합. **`slot_path` 로
    `REPO / slot` 결합하기 전에** `_SLOT_ID_RE` 로 형식을 검증한다 — caller-공급 문자열이라
    `../x`·`work/../x`·빈값·`work/` 단독 같은 traversal/부적격 값이 슬롯 루트 밖을 가리키면
    `SlotResolutionError` 로 중단한다(side-effect 를 슬롯 경계 안에 가둠).
    """
    s = slot_arg.strip()
    slot = s if s.startswith("work/") else f"work/{s}"
    if not _SLOT_ID_RE.match(slot):
        raise SlotResolutionError(
            f"슬롯 식별자 {slot_arg!r} 형식 오류 — `work/<repo>_<N>`(또는 접두 생략 `<repo>_<N>`) "
            "형식만 허용한다(traversal·빈값·`work/` 단독·경로구분자 거부·슬롯 경계 보호)."
        )
    return slot


def _slot_from_cwd() -> str | None:
    """cwd 가 `<WORK_DIR>/<repo>_<N>[/...]` 안이면 그 슬롯 식별자(`work/<repo>_<N>`), 아니면 None.

    슬롯 worktree(또는 그 하위 submodule) cwd 에서 커맨드를 부르면 정체성 인자 없이도 그 슬롯을
    타깃하게 한다(pm_bootstrap `_worktree_cwd`/`_auto_slot` 의 cwd-유입 정신과 정합). WORK_DIR 밖
    (예: multi-PM 루트)이면 None(→ session 해소로 폴백). 경로 해석 실패는 None(fail-soft).
    """
    try:
        rel = Path(os.getcwd()).resolve().relative_to(WORK_DIR.resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    return f"work/{parts[0]}" if parts else None


def _resolve_current_slot(slot_arg: str | None) -> str:
    """dev/sync 의 대상 슬롯을 해소한다 — 명시 슬롯 문자열 > cwd > 세션 leased.

    `slot_arg` 는 CLI `--repo`/`--slot` 로 정체성이 완전 해소된 경우 `main`(`identity.kind ==
    "slot"`)이 조립해 넘기는 `<repo>_<N>` 문자열이다 — 이 함수 자체는 어느
    source 문자열이든 정규화·검증만 한다(`--slot` CLI 인자 자체를 직접 받지 않는다).

    우선순위:
      1. `slot_arg` 명시(빈 문자열 포함) → `_normalize_slot`(main 의 명시 --repo/--slot 조립·
         권장 경로).
      2. cwd 가 슬롯 worktree 안이면 그 슬롯(`_slot_from_cwd`).
      3. 세션(`_default_session`·env/local.conf 유입)이 보유한 leased 슬롯 — 정확히 1개면 그것.

    3에서 매칭 leased 슬롯이 0개(무바인딩)이거나 ≥2(모호)면 `SlotResolutionError` raise — CLI
    main 이 rc 1 + `--repo/--slot` 안내로 surface 한다(침묵 오타깃 금지). 이 해소는 dev/sync
    mutation의 대상 선택이므로 장부를 strict로 읽고, `_default_session`(board.session_name 동형)을
    재사용한다(기존 관례). (인자 전무 시
    이 no-flag 체인은 불변.)

    **형식 검증(must-fix 2)**: `slot_arg` 명시는 `_normalize_slot` 이, 그 외 유입도 반환
    전 `_SLOT_ID_RE`(`_normalize_slot` 재적용)로 최종 슬롯이 `work/<repo>_<N>` 형식임을 강제한다
    — 어느 source(명시/cwd/session)든 부적격/traversal 값이 `slot_path` 결합으로 슬롯 경계를
    벗어나지 못하게 하는 단일 불변식. `slot_arg` 는 **빈 문자열도 명시로 취급**(`is not None`)해
    형식 검증에 태운다(빈값 거부).
    """
    if slot_arg is not None:
        return _normalize_slot(slot_arg)  # 명시(빈값 포함) → 정규화 + 형식 검증.
    cwd_slot = _slot_from_cwd()
    if cwd_slot:
        return _normalize_slot(cwd_slot)  # cwd 유입도 최종 형식 검증(단일 불변식).
    sess = _default_session()
    with _lease_lock():
        mine = [
            lease
            for lease in _read_ledger_strict()
            if lease.state == "leased" and lease.session == sess
        ]
    if len(mine) == 1:
        return _normalize_slot(mine[0].slot)  # 장부 유입도 최종 형식 검증(단일 불변식).
    if not mine:
        raise SlotResolutionError(
            f"세션 {sess!r} 의 leased 슬롯이 없다 — `--repo <name> --slot <N>` 으로 대상 슬롯을 명시하라."
        )
    raise SlotResolutionError(
        f"세션 {sess!r} 의 leased 슬롯이 {len(mine)}개"
        f"({', '.join(l.slot for l in mine)}) — `--repo <name> --slot <N>` 으로 어느 슬롯인지 명시하라."
    )


def _load_identity_args():
    """공용 정체성 모듈 `identity_args.py` 를 로드한다.

    `__file__` 기준(스크립트-위치 앵커) — `REPO` 전역이 아니라 이 파일 자신의 실제 디스크 경로로
    해석한다. 테스트가 `_load_wp_bound` 로 이 모듈을 로드한 뒤 `REPO`/`LEASES_FILE` 등 전역을
    tmp 경로로 재배선해도(hermetic), `identity_args.py` 는 항상 이 파일과 같은 tools/ 디렉토리에
    물리적으로 있으므로 `__file__` 앵커는 재배선 영향을 받지 않는다(스크립트+테스트 양쪽 동작).

    다른 도구의 sibling 로더(`pm_config._load_module`·`pm_bootstrap._load_worktree_pool`)와
    동형 관용구
    (리스 IO 층은 `worktree_pool` 을 되-import 하지 않는 단방향 관계).
    """
    path = Path(__file__).resolve().parent / "identity_args.py"
    return _load_module_from_path(
        path, "identity_args.py", verifier=_verify_engine_rev,
    )


# state 생성과 handoff 갱신이 같은 marker를 쓰도록 literal은 identity_args 한 곳만 소유한다.
# 앞서 정의된 함수들은 이 전역을 호출 시점에 조회하므로 module import 계약은 그대로다
# (`file_lock` 도 같은 이유로 여기서 묶어 바인딩한다 — `_lease_lock` 은 위에 정의돼 있지만
#  호출 시점에 이 전역을 본다·board.py 의 identity_args+file_lock 동시 바인딩 동형).
try:
    _identity_args = _load_identity_args()
    file_lock = _load_file_lock()
except Exception as exc:  # noqa: BLE001 — 직접 CLI는 import 단계 skew도 복구 안내로 번역.
    if _is_engine_rev_skew(exc):
        if __name__ == "__main__":
            print(
                f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
                "동기화한 뒤 다시 실행하세요.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        raise
    raise
TASK_PM_STATE_EMPTY_MARKER = _identity_args.TASK_PM_STATE_EMPTY_MARKER
_runtime_skill_entry = _identity_args._runtime_skill_entry


def _resolve_actor_slot_for_repo(repo: str) -> str:
    """`--repo` 단독(슬롯 무) actor 해소 — 공용 `identity_args.resolve_actor_slot` 위임 (
    

    dev/sync 는 실 git side-effect 를 내는 actor 연산이라 claim/finish 등과 동일 규칙을 따른다:
    그 repo 의 활성(leased) 슬롯이 정확히 1개면 자동 해소, 0개/≥2개는 fail-loud(`--slot <N>`
    명시 안내). 로컬 리스 읽기를 재구현하지 않는다 — `identity_args` 가 리스
    장부를 직접 point-read 한다. `identity_args.SlotResolutionError`(모호·다른 모듈의 클래스)는
    이 모듈의 `SlotResolutionError` 로 번역해 전파한다 — CLI `main` 의 단일 except 로 수렴시키기
    위함(호출부가 두 예외 타입을 각각 알 필요 없음).
    """
    ia = _load_identity_args()
    try:
        session = ia.resolve_actor_slot(repo, LEASES_FILE)
    except ia.SlotResolutionError as exc:
        raise SlotResolutionError(str(exc)) from exc
    if session is None:
        raise SlotResolutionError(
            f"repo {repo!r} 의 활성(leased) 슬롯이 없다 — `--slot <N>` 으로 대상 슬롯을 명시하라."
        )
    return _normalize_slot(session)  # session="<repo>_<N>" → 최종 형식 검증(단일 불변식).


# ── set-base / status CLI 핸들러 (위치인자 <slot> — pool-management op·명시 슬롯) ──
# dev/sync 의 --repo/--slot identity 와 달리 대상 슬롯을 **위치인자**로 직접 받는다: set-base·status·
# ()는 자기 세션 슬롯이 아닌 임의 슬롯도 관리 대상이라(pool 관리) 슬롯을 명시
# 지정한다. status 는 위치인자 생략 시 cwd/세션 leased 로 해소(무인자=내 슬롯).


def _cmd_set_base(args) -> int:
    """`set-base <slot> <branch>[@<commit>]` CLI 핸들러 — 기준점 사용자 명시 기록 ().

    자동 추론 없이 사용자가 지정한 base 를 `set_base`로 기록한다. 슬롯 형식 오류·
    ref 해소 실패(오타·미fetch·슬롯 worktree 부재 → `BaseRefUnresolvable`)·장부 미등록은 rc 1 로 명시
    실패(silent 오기록 방지·codex must-fix)."""
    try:
        slot = _normalize_slot(args.slot)
    except SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    base_ref, commit = _parse_base_ref(args.base)
    if not base_ref:
        print(f"[중단] set-base 기준 인자 {args.base!r} 가 비었다 — `<branch>[@<commit>]` 형식으로 주라.",
              file=sys.stderr)
        return 1
    try:
        lease = set_base(slot, base_ref, commit=commit)
    except ReadonlySlotMutation as exc:
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부().
        return 1
    except BaseRefUnresolvable as exc:
        print(f"[중단] {exc}", file=sys.stderr)   # ref 해소 실패 → 조용히 오기록하지 않고 fail-loud.
        return 1
    if lease is None:
        print(f"[중단] 슬롯 {slot} 이 리스 장부에 없다 — set-base 는 등록된 슬롯에만 기준점을 기록한다.",
              file=sys.stderr)
        return 1
    recorded = lease.git.get("base") if isinstance(lease.git, dict) else None
    if not recorded or recorded.get("branch") != base_ref:
        # 방어적 — 여기 오면 ref 는 해소됐으나 스냅이 실패한 극히 드문 race(경로 삭제 등).
        print(f"[중단] 슬롯 {slot} 기준점 기록 실패 — 슬롯 worktree 를 스냅할 수 없다(경로 부재/git 오류). "
              "슬롯이 실재하는지 확인하라.", file=sys.stderr)
        return 1
    print(f"✓ 슬롯 {slot} 기준점 기록: base = {recorded.get('branch')}@{(recorded.get('commit') or '?')[:12]} "
          "— 이제부터 부트스트랩 0단계 drift 감지가 이 기준으로 작동한다().")
    return 0


def _print_status_row(row: dict) -> None:
    """슬롯 git 구성 dict 한 줄 렌더(base·branch·head·N behind·submodule pin/drift·dirty).

    미기록 base 는 `-`(계산 불가·이유·추론 금지·). submodule 은 역할별 경고 마크(⚠=drift/
    uninitialized··=pinned/dev-ahead)로 표시(빈 목록=submodule 없는 슬롯 → 줄 생략)."""
    slot = row["slot"]
    base = row.get("base")
    base_str = (f"{base.get('branch')}@{(base.get('commit') or '?')[:12]}"
                if base and base.get("branch") else "(미기록)")
    behind = row.get("behind")
    print(f"# 슬롯 {slot} git 구성 (조회 — 손-git 불요)")
    print(f"  role:   {row.get('role') or _slot_role(slot)}")   # work | readonly ()
    print(f"  base:   {base_str}")
    print(f"  branch: {row.get('branch') or '(detached/미상)'}")
    print(f"  head:   {(row.get('head') or '(미상)')[:12]}")
    if behind is None:
        print(f"  base 대비 behind: -  ({row.get('behind_reason')})")
    else:
        print(f"  base 대비 behind: {behind} 커밋")
    print(f"  dirty:  {'예 (미커밋 변경 있음)' if row.get('dirty') else '아니오 (clean)'}")
    for s in (row.get("submodules") or []):
        mark = "⚠" if getattr(s, "warning", False) else "·"
        dirty_tag = " ·dirty" if getattr(s, "dirty", False) else ""
        print(f"  submodule {mark} {s.path}: {s.kind}{dirty_tag}")


def _cmd_status(args) -> int:
    """`status [<slot>] [--task <이름>]` CLI 핸들러 — 슬롯 git 구성 조회 단일/일괄.

    대상: `--task <이름>`(그 task 보유 전 슬롯 일괄) > 위치인자 `<slot>`(단일) > 무인자(내 task 전
    슬롯·`_default_session` 유입). 슬롯별 base·branch·head·base 대비 N behind(미기록=`-`·추론 금지·)
    ·submodule pin/drift·dirty·role 을 조회한다(`status` 백본)."""
    task = getattr(args, "task", None)
    slot_arg = getattr(args, "slot", None)
    if task and slot_arg:
        print("[중단] status 는 `<slot>`(단일) 또는 `--task <이름>`(일괄) 중 하나만 받는다.",
              file=sys.stderr)
        return 1
    try:
        if task:
            rows = status(task=task)
        elif slot_arg:
            rows = status(slot=slot_arg)
        else:
            rows = status()   # 무인자 = 내 task 전 슬롯().
    except SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("# 조회 대상 슬롯 없음 — 내 세션 명의로 보유한 leased 슬롯이 없다 "
              "(task 모드면 `--task <이름>`, 아니면 `<slot>` 으로 대상을 명시하라).")
        return 0
    for i, row in enumerate(rows):
        if i:
            print()   # 슬롯 간 구분 공백(일괄).
        _print_status_row(row)
    return 0


def _is_registered_task(name: str) -> bool:
    """`name` 이 tasks 장부에 등록된 task 인가 — 스킵 안내 전용 조회 (부작용 0).

    ⚠ **`_lease_lock` 밖에서만 호출**한다 — 내부의 `find_task` 가 락을 잡는데 `_lease_lock` 은
    비재진입이라 락 보유 중 호출하면 데드락이다(현 호출부 = CLI 요약 루프·락 밖·내부 리뷰 지적).

    not-owner 스킵의 보유자가 **task 명의**면 해소는 `--task <이름>`(소유 축을 밝히는 것)이다 —
    사용자가 그걸 추측하지 않게 CLI 안내에 실값을 싣기 위한 판정. 장부 IO/파싱 실패는 False
    (fail-soft — 안내 문구가 없을 뿐 스킵 자체는 그대로 loud). 안내용 조회가 CLI 를 죽이지
    않는다(`pm_config.cmd_status` 의 task 축 fail-soft 와 동형)."""
    try:
        return find_task(name) is not None
    except Exception:  # noqa: BLE001 — 안내 전용 조회·실패는 문구 생략(스킵 판정엔 무영향).
        return False


def _rebase_skip_reason(reason: "str | None") -> str:
    """rebase 스킵 사유 코드 → 사람이 읽는 loud 설명 (CLI)."""
    if reason and reason.startswith("not-owner:"):
        holder = reason.split(":", 1)[1]
        # 보유자 명의는 세션일 수도 task 일 수도 있다 — "세션" 으로 단정하면 task 모드에서
        # 안내가 틀린 축을 가리킨다(codex 게이트 suggestion).
        msg = f"명의 {holder!r} 이(가) 보유 — 내 슬롯 아님(rebase 차단·소유검사)"
        if _is_registered_task(holder):
            # 보유자가 등록 task = 소유 축이 task 다— 해소 커맨드를 실값으로 안내.
            msg += (f" · 이 슬롯이 내 task 명의면 `--task {holder}` 를 붙여 그 명의로 rebase 하라"
                    " (소유검사)")
        return msg
    return {
        "readonly": "readonly 공유 슬롯 — mutation 불가(refresh 로만 갱신·)",
        "not-owner": "내 명의(세션/task) 소유(leased) 슬롯이 아니다 — 남의/미점유 슬롯 rebase 차단",
        "dirty": "미커밋 변경(dirty) — rebase 는 clean 전제(정리 후 재시도)",
        "in-progress": "이미 rebase 진행 중 — `git rebase --continue|--abort` 로 먼저 해소",
        "no-base": "기준점(base) 미기록 + --onto 없음 — `set-base` 지정 또는 `--onto <branch>`(추론 금지·)",
        "unresolvable-onto": "`--onto` ref 해소 실패(오타·미fetch — 실재 브랜치/커밋 지정)",
        "no-worktree": "슬롯 worktree 경로 부재",
    }.get(reason or "", reason or "미상")


def _cmd_rebase(args) -> int:
    """`rebase <slot> [--task <이름>] [--onto <b>]` (단일) · `rebase --task <이름> [--onto <b>]` (일괄)
    CLI 핸들러 ().

    슬롯 독립 처리 — 선-검사(소유/dirty/rebase 진행중) 스킵 + 충돌 그대로 fail-loud(엔진 abort 안
    함) + 성공 시 장부 원자 갱신. 끝에 성공/스킵/충돌 요약. 단일은 성공해야 rc 0(스킵/충돌=rc 1),
    일괄은 충돌이 있으면 rc 1(주의 필요·나머지는 독립 진행).

    **`--task` 는 두 역할이고 위치인자와 배타가 아니다**: 대상 *선택*(위치인자 없으면
    그 task 보유 전 슬롯 일괄) 소유 *명의*(`rebase(owner_task=)` — release F3 동형). 그래서
    `rebase <slot> --task <이름>` = 그 task 명의로 **단일** 슬롯 rebase 다 — 종전엔 둘이 배타라
    task 명의 슬롯을 단일 지정으로 rebase 할 방법이 아예 없었다. 위치인자만이면 종전대로 세션
    명의로 판정한다(슬롯-세션 모드 불변).

    ⚠️ **선행조건()**: 활성 백그라운드 위임(dev) 중인 슬롯은 rebase 하지 마라 — 서브에이전트는
    하네스 안 프로세스라 엔진이 못 본다(기계 신호 부재·[[parallel-dev-shared-tree-clobber]] 변형).
    스킬/카드에 명문화·실행 전 사용자 확인."""
    task = args.task
    slot_arg = args.slot
    if not task and not slot_arg:
        print("[중단] rebase 대상을 지정하라 — `<slot>`(단일) 또는 `--task <이름>`(일괄).",
              file=sys.stderr)
        return 1
    batch = bool(task) and not slot_arg   # `--task` 단독일 때만 일괄(위치인자 동반 = 단일 지정).
    if batch:
        try:
            slots = [lease.slot for lease in slots_for_task_strict(task)]
        except Exception as exc:  # noqa: BLE001 — mutation 대상 해소 실패는 0개 성공이 아님.
            print(
                f"[중단] task {task!r} 보유 슬롯 장부 조회 실패 — {exc}. "
                "rebase 대상을 0개로 간주하지 않는다.",
                file=sys.stderr,
            )
            return 1
        if not slots:
            print(f"# task {task!r} 이(가) 보유한 leased 슬롯이 없다 — rebase 대상 0.")
            return 0
    else:
        try:
            slots = [_normalize_slot(slot_arg)]
        except SlotResolutionError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1

    # 소유 명의 = `--task` 주어지면 그 task(단일·일괄 공통), 아니면 세션(`rebase` 내부 해소).
    results = rebase(slots, onto=args.onto, owner_task=task)
    n_ok = n_skip = n_conflict = 0
    for r in results:
        if r.outcome == REBASE_REBASED:
            n_ok += 1
            print(f"✓ 슬롯 {r.slot} rebase 성공 — base {r.base} 최신으로 이동 "
                  f"(head {(r.new_head or '?')[:12]} · 장부 base.commit/head/recorded_at 원자 갱신).")
        elif r.outcome == REBASE_CONFLICT:
            n_conflict += 1
            print(f"✗ 슬롯 {r.slot} rebase 충돌 — **그 상태 그대로** 두었다(엔진 임의 abort 안 함). "
                  f"해소는 사용자: 슬롯에서 `git rebase --continue`(충돌 해결 후) 또는 `git rebase "
                  f"--abort`(취소). 장부 base 미갱신 → 다음 부트스트랩 0단계가 '진행 중' 감지. "
                  f"git: {r.reason}", file=sys.stderr)
        else:
            n_skip += 1
            print(f"— 슬롯 {r.slot} rebase 스킵 ({_rebase_skip_reason(r.reason)}).",
                  file=sys.stderr)
    print(f"\n# rebase 요약: 성공 {n_ok} · 스킵 {n_skip} · 충돌 {n_conflict} "
          "(일괄=슬롯 독립·한 충돌이 나머지를 안 막음).")
    if batch:
        return 1 if n_conflict else 0
    return 0 if n_ok == 1 else 1   # 단일 — 성공해야 rc 0(스킵/충돌=요청 미수행·rc 1).


def _cmd_refresh(args) -> int:
    """`refresh <slot> [--onto <branch>]` CLI 핸들러 — readonly 슬롯 갱신 ().

    fetch → detached HEAD 를 기준(onto 또는 기록된 base.branch) 최신 tip 으로 이동한다. `--onto` 는 준
    ref 를 그대로 해소하고(자동 대체 없음·원격 기준은 `origin/<branch>` 로 명시), 생략 시에만 기록된
    base.branch 를 origin 우선으로 해소한다. 성공 메시지는 백본이 돌려준 **실제 이동 ref + HEAD sha**
    를 그대로 찍는다. dirty(누군가 씀·신호)·미readonly·base 미해소·ref 해소 실패는 rc 1 로 명시
    실패(`RefreshRefused`·조용히 reset 안 함)."""
    try:
        slot = _normalize_slot(args.slot)
    except SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        ref, head = refresh(slot, onto=args.onto)
    except RefreshRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print(f"✓ 슬롯 {slot} refresh: detached HEAD → {ref} @ {(head or '?')[:12]} "
          "로 이동(fetch→detach).")
    return 0


def _cmd_record(args) -> int:
    """`record <slot>` CLI 핸들러 — 도착 스냅(lease.git)을 live 로 재기록 (base 보존).

    0단계 record-vs-live diverged FAIL-LOUD 를 사용자가 정당(의도한 브랜치 전환·릴리즈 등)이라
    판단했을 때의 명시 재동기 진입 — `record_git_snapshot(slot)`(base 미전달=기존 base 보존·
    arrival 동형) 프리미티브를 CLI 로 노출한다(감지=기계·해소=사용자·자동 실행 없음). 슬롯 형식
    오류·장부 미등록·스냅 불가는 rc 1 로 명시 실패(silent 무변경 방지)."""
    try:
        slot = _normalize_slot(args.slot)
    except SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    before = read_lease_strict(slot)
    before_git = before.git if (before is not None and isinstance(before.git, dict)) else None
    lease = record_git_snapshot(slot)
    if lease is None:
        print(f"[중단] 슬롯 {slot} 이 리스 장부에 없다 — record 는 등록된 슬롯에만 스냅을 기록한다.",
              file=sys.stderr)
        return 1
    after_git = lease.git if isinstance(lease.git, dict) else None
    if after_git is None or after_git == before_git:
        # 스냅 불가(슬롯 worktree 경로 부재 등 → `_apply_git_snapshot` 이 기존 git 보존)·무변경.
        print(f"[중단] 슬롯 {slot} git 스냅 재기록 실패 — 슬롯 worktree 를 스냅할 수 없다(경로 부재/git 오류). "
              "슬롯이 실재하는지 확인하라.", file=sys.stderr)
        return 1
    print(f"✓ 슬롯 {slot} 도착 스냅 재기록: branch=`{after_git.get('branch')}` "
          f"head=`{(after_git.get('head') or '?')[:12]}` (base 보존·이후 0단계 정합이 이 스냅 기준).")
    return 0


def _cmd_switch(args) -> int:
    """`switch <slot> <branch>` CLI 핸들러 — 브랜치 전환 + 스냅 재기록 원자.

    0단계 main-참조 fault 의 remedy 진입점. 선-검사 거부(`SwitchRefused`)·readonly
    (`ReadonlySlotMutation`)·슬롯 형식 오류는 rc 1 + 사유(loud). 성공 시 무엇을 했는지(전환 형태·
    재기록 head)를 stdout 으로 surface 한다 — 이 한 커맨드로 0단계 재진입이 열린다(2차 diverged
    차단 없음)."""
    try:
        slot = _normalize_slot(args.slot)
    except SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        result = switch(slot, args.branch)
    except (SwitchRefused, ReadonlySlotMutation) as exc:
        if _is_engine_rev_skew(exc):
            raise
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    kind = "비파괴 생성 전환(`-b`)" if result.created else "기존 브랜치 전환"
    # 원문이 revspec 이라 다른 이름으로 해소됐으면 성공 보고에도 실값을 싣는다(조용한 오전환 방지).
    asked = f" (입력 `{result.requested}` → 해소)" if result.expanded else ""
    print(f"✓ 슬롯 {slot} → 브랜치 `{result.branch}`{asked} {kind} + 도착 스냅 재기록 "
          f"(head=`{(result.head or '?')[:12]}`·base 보존·원자) — 0단계 재진입이 "
          f"'기록↔live diverged' 로 막히지 않는다.")
    return 0


def _main(argv: "list[str] | None" = None) -> int:
    """argparse 진입점 — pm-worktree 스킬이 래핑할 `dev`/`sync`/`set-base`/`status` 커맨드
    

    라이브러리 모듈에 얇은 CLI 를 얹는다(`if __name__ == "__main__"` 가드로 import 안전 — 다른
    도구의 `spec_from_file_location` import 를 안 깬다). 스킬이
    `python3 .project_manager/tools/worktree_pool.py dev <sub> <branch> [--repo <name> [--slot <N>]]` /
    `... sync [--repo <name> [--slot <N>]]` / `... set-base <slot> <branch>[@<commit>]` /
    `... status [<slot>]` 로 부른다. CLI 는 **실경로 wiring**(git_runner 미주입)이고, 함수 레벨 DI
    seam(`dev`/`sync`/`set_base`/`slot_git_status` 의 git_runner)은 테스트가 쓴다. 사람이 읽는
    stdout(무엇을 했는지)·skip/경고 사유는 backbone 이 stderr·실패는 rc 1 + 메시지.

    **두 인자 표면**: `dev`/`sync` 는 --repo/--slot identity(자기 세션 슬롯 대상)·`set-base`/`status`
    는 위치인자 `<slot>`(임의 슬롯 pool 관리). set-base/status 는 identity 파싱 전에 분기
    처리한다(그 args 표면 없음).

    정체성 인자는 공용 `identity_args`(`add_identity_args`·`parse_identity`)로 통일한다(
    분해형 `--repo <name>
    [--slot <N>]` 만 받는다. `parse_identity` 의 discriminated `kind` 로 해소 경로가 갈린다:
      - `kind="slot"`(`--repo`+`--slot` 모두 명시) → `<repo>_<N>` 조립 후 `_resolve_current_slot`
        (기존 명시-슬롯 정규화/검증 경로 재사용).
      - `kind="repo"`(`--repo` 단독) → actor 해소(`_resolve_actor_slot_for_repo` — 활성 슬롯 1개면
        해소·0개/≥2개 fail-loud). bare `--slot`(`--repo` 없이)은 `parse_identity` 가 `ValueError`
        로 fail-loud(DoD).
      - `kind="none"`(인자 전무) → 기존 no-flag 체인(`_resolve_current_slot(None)`·cwd→세션
        leased).
    """
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    ia = _load_identity_args()
    parser = argparse.ArgumentParser(
        prog="worktree_pool.py",
        description="worktree/submodule 운영중 관리 backbone (dev/sync).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_dev = subparsers.add_parser(
        "dev", help="submodule 을 dev 브랜치로 지정(on-branch 화 → selective resync 가 skip).")
    p_dev.add_argument("submodule", help="슬롯 worktree 상대 submodule 경로(예 vendor/sub).")
    p_dev.add_argument("branch", help="지정할 dev 브랜치명(없으면 생성·있으면 전환).")
    ia.add_identity_args(p_dev)

    p_sync = subparsers.add_parser(
        "sync", help="현재 슬롯 submodule 을 pin 에 selective 재동기(수동 트리거).")
    ia.add_identity_args(p_sync)

    # set-base / status — 위치인자 <slot>(dev/sync 의 --repo/--slot identity 와 다른 표면·pool 관리).
    p_set_base = subparsers.add_parser(
        "set-base",
        help="슬롯 기준점(base)을 사용자 명시로 기록(추론 금지) → 이후 drift 감지 작동.")
    p_set_base.add_argument("slot", help="대상 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")
    p_set_base.add_argument(
        "base", metavar="branch[@commit]",
        help="기준 브랜치[@커밋] — 커밋 생략 시 그 브랜치 tip(예 origin/main·origin/main@df10dc6).")

    p_status = subparsers.add_parser(
        "status",
        help="슬롯 git 구성 조회(role·base·branch·head·N behind·submodule pin/drift·dirty·미기록 시 `-`·단일/일괄).")
    p_status.add_argument("slot", nargs="?", default=None,
                          help="대상 슬롯(단일·생략 시 내 task 전 슬롯 일괄).")
    p_status.add_argument("--task", default=None,
                          help="그 task 보유 전 슬롯 일괄 조회(`<slot>` 과 배타).")

    # rebase <slot> [--task <이름>] [--onto <b>] (단일) · rebase --task <이름> [--onto <b>] (일괄)
    # — 위치인자/pool 관리. `--task` 는 선택 축이자 **소유 명의** 축이라 위치인자와 배타가 아니다.
    p_rebase = subparsers.add_parser(
        "rebase",
        help="슬롯 base 를 사용자 명시로 rebase(단일/일괄·선-검사·충돌 그대로+loud·장부 원자 갱신).")
    p_rebase.add_argument("slot", nargs="?", default=None,
                          help="대상 슬롯(단일 지정·`--task` 와 함께 주면 그 task 명의로 이 슬롯만).")
    p_rebase.add_argument("--task", default=None,
                          help="task 명의(소유검사 축). `<slot>` 없이 주면 "
                               "그 task 보유 전 슬롯 일괄 rebase.")
    p_rebase.add_argument("--onto", default=None,
                          help="rebase 기준 브랜치(생략 시 기록된 base.branch 최신·미기록이면 거부).")

    # refresh <slot> [--onto <branch>] — readonly 공유 슬롯 갱신(위치인자 <slot>).
    p_refresh = subparsers.add_parser(
        "refresh",
        help="readonly 공유 슬롯을 released 최신으로 갱신(fetch→detach 이동·dirty=거부+loud).")
    p_refresh.add_argument("slot", help="대상 readonly 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")
    p_refresh.add_argument("--onto", default=None,
                           help="갱신 기준 ref — 준 값 그대로 해소한다(로컬 브랜치명=로컬 tip·원격 기준은 "
                                "`origin/<branch>` 로 명시·자동 대체 없음). 생략 시 기록된 "
                                "base.branch 를 origin 우선으로 해소하고, 둘 다 없으면 거부.")

    # record <slot> — 도착 기대 스냅(lease.git)을 live 로 재기록(base 보존·위치인자 <slot>).
    # 0단계 record-vs-live diverged FAIL-LOUD 를 사용자가 정당(의도한 브랜치 전환 등)이라 판단했을
    # 때의 명시 재동기 진입 — `record_git_snapshot` 프리미티브의 CLI 노출(감지=기계·해소=사용자).
    p_record = subparsers.add_parser(
        "record",
        help="슬롯 도착 스냅(lease.git·branch/head)을 live 로 재기록(base 보존) — 0단계 diverged 정당 판단 시 명시 재동기.")
    p_record.add_argument("slot", help="대상 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")

    # switch <slot> <branch> — 브랜치 전환 + 장부 스냅 재기록을 **원자**로(위치인자 <slot>).
    # 0단계 main-참조 fault 의 해소 단일 커맨드 — raw `git switch` 는 스냅을 안 남겨 곧바로 2차
    # 차단(기록↔live diverged)을 부른다(remedy-유발 상태전이). 보호목록 브랜치로의 전환은 거부.
    p_switch = subparsers.add_parser(
        "switch",
        help="슬롯 브랜치 전환 + 도착 스냅 재기록(원자·base 보존·보호브랜치 거부) — 0단계 main-참조 해소 단일 커맨드.")
    p_switch.add_argument("slot", help="대상 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")
    p_switch.add_argument("branch", help="전환할 브랜치(기존이면 그대로 전환·미존재면 생성 전환).")

    args = parser.parse_args(argv)

    # set-base / status / rebase / refresh / record / switch — 위치인자 <slot> 경로(identity 파싱 미진입·pool 관리).
    if args.command == "set-base":
        return _cmd_set_base(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "rebase":
        return _cmd_rebase(args)
    if args.command == "refresh":
        return _cmd_refresh(args)
    if args.command == "record":
        return _cmd_record(args)
    if args.command == "switch":
        return _cmd_switch(args)

    try:
        identity = ia.parse_identity(args)
    except ValueError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1

    try:
        if identity.kind == "slot":
            slot = _resolve_current_slot(f"{identity.repo}_{identity.slot}")
        elif identity.kind == "repo":
            slot = _resolve_actor_slot_for_repo(identity.repo)
        else:  # kind == "none" — 인자 전무, 기존 no-flag 체인.
            slot = _resolve_current_slot(None)
    except SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1

    if args.command == "dev":
        try:
            rc, out = dev(slot, args.submodule, args.branch)
        except ReadonlySlotMutation as exc:
            print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부.
            return 1
        except SubmoduleNotInSlot as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        if rc != 0:
            print(
                f"[중단] 슬롯 {slot} · submodule {args.submodule!r} 을 dev 브랜치 "
                f"{args.branch!r} 로 지정 실패 (rc={rc}): {str(out).strip()[:200]}",
                file=sys.stderr,
            )
            return 1
        print(
            f"✓ 슬롯 {slot} · submodule {args.submodule} → dev 브랜치 {args.branch!r} "
            "(on-branch·이후 selective resync 가 이 submodule 을 skip)."
        )
        return 0

    # args.command == "sync"
    print(
        f"# 슬롯 {slot} submodule selective 재동기 "
        "(detached=pin 재동기·on-branch=skip·dirty=skip+경고)"
    )
    try:
        sync(slot)
    except ReadonlySlotMutation as exc:
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부.
        return 1
    print(f"✓ 슬롯 {slot} 재동기 완료 (skip/경고 사유는 위 stderr 참조).")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        _console_encoding = _load_module_from_path(
            Path(__file__).resolve().with_name("console_encoding.py"),
            "console_encoding.py",
            verifier=_verify_engine_rev,
        )
        _console_encoding.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
