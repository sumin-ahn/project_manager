#!/usr/bin/env python3
"""worktree 풀 엔진 — 슬롯 리스 alloc/release/reclaim_stale/force_release (ADR-0013).

repo별 git worktree 풀로 *코드*를 격리한다(병렬 브랜치·나중 git merge). 슬롯은
브랜치-무관 재사용 컨테이너(`work/<repo>_<N>`)이고, 브랜치는 슬롯 worktree 의 git HEAD 에서
**live** 로 읽는다(ADR-0013 amend T-0072 — git=단일 진실·장부 저장 폐지·드리프트 불가능·
`current_branch(slot)`). 코드 동시성의 격리 레이어 — 보드(공유 `.project_manager`)
동시성은 board.py 가 따로 책임진다(별 모듈·여기선 import 하지 않는다).

설계 (ADR-0013 / sealed spike §8-2·8-6·8-4(d)):
  - 슬롯 = `work/<repo>_<N>`(repo + 번호·브랜치 무관·전이적 물리자원). 폴더명에 브랜치를
    안 박는다(박으면 stale — 사용자 통찰 §8-6).
  - 브랜치 = 슬롯 worktree 의 git HEAD 에서 live 조회(`current_branch(slot)`·ADR-0013
    amend T-0072 — 장부 저장 폐지·git=진실). 브랜치 변경 = 같은 슬롯 재체크아웃(리스 유지).
  - 리스 = 작업스트림(브랜치) 단위. alloc@bootstrap · release@작업완료(세션종료/회전 ≠
    release). 회전은 리스 유지 + 같은 슬롯 재부착.
  - stale 회수 = pid 생존만(타임아웃/heartbeat 기각·조기회수 위험). dirty 면 stash 보존.
  - git 연동 = DI seam(주입 가능 runner) — `git worktree add/remove`·dirty 검사·stash·
    submodule init 을 seam 통해 호출 → hermetic 테스트(mock 또는 실 임시 repo).

장부 동시쓰기 보호 = **자체 파일락**(stdlib fcntl/msvcrt·둘 다 없으면 단일-머신 폴백).
board.py 의 `board_lock` 과 *같은 패턴*이지만 **독립 구현**이다 — 병렬 작업 충돌 회피 +
worktree 풀이 board 모듈에 의존하지 않게 하기 위함(import 금지·ADR-0013 touches 격리).
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
from collections.abc import Iterator
from pathlib import Path
from typing import Callable

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·pm_*.py 와 동일 앵커 관례(sealed spike §8-4).
# multi-PM 모델에서 이 도구가 어느 worktree cwd 에서 호출돼도 자기 위치(multi-PM 루트 .project_manager)를
# 자동 타깃한다.
REPO = Path(__file__).resolve().parents[2]
LOCAL_DIR = REPO / ".project_manager" / ".local"               # per-clone scratch (git-ignored)
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"               # 리스 장부 (ADR-0013) — leases[] + tasks[](T-0353)
LEASES_LOCK = LOCAL_DIR / "worktree-leases.lock"               # 장부 read-modify-write 직렬화 락
TASKS_DIR = LOCAL_DIR / "tasks"                                # task 서술 공간(.local/tasks/<이름>/·pm_state·메타·⑮·T-0353)
WORK_DIR = REPO / "work"                                        # worktree 풀 루트 (multi-PM 루트 gitignore)
REPOS_DIR = REPO / ".repos"                                     # worktree 의 공유 .git 원 (bare·ADR-0011 §31)
REPO_HOOKS_DIR = LOCAL_DIR / "repo-hooks"                       # per-repo pre-push 보호훅(프레임워크 소유·gitignore·T-0076)

# git subprocess 타임아웃 (초) — captured 러너(_real_git_runner)의 subprocess timeout + worktree
# add console-visible 러너(_real_git_runner_interactive)의 상한. **타임아웃의 실패모드 = 정상 op 도
# 죽임(false-kill·T-0292)** — 대형 repo 의 worktree add(로컬 bare→full checkout·느린 디스크/VPN/
# Windows)가 진행 중인데도 짧은 고정값(옛 120)에 걸려 false-kill 되던 실측 블로커. submodule 선례
# (T-0070·SUBMODULE_TIMEOUT)와 동형으로 env override + 관대한 기본으로 옮긴다.
#
# env override (T-0292·`_resolve_submodule_timeout` 미러): 코드 직수정은 worktree_pool.py=manifest
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

# submodule init 인터랙티브 러너의 timeout (T-0070). 짧은 captured git(status·checkout·dirty·
# stash)은 GIT_TIMEOUT_SECONDS(기본 1800·T-0292 env) 로 충분하지만, submodule clone 은 대형 family
# repo + VPN 에서 600s 도 초과(실 Windows multi-PM 파일럿 "10분 아슬" 실증) → TimeoutExpired
# 로 죽었다. 인터랙티브 러너는 stdio 를 콘솔에 상속(진행상황·credential 프롬프트 작동)하고
# 이 대폭 확대된 timeout(또는 None=무제한)으로 큰 clone 을 끝까지 돌린다. 수동 콘솔 실행과
# 동일 거동. None 으로 두면 timeout 자체를 끈다(완전 무제한·hang 위험은 콘솔에 가시).
#
# env override (T-0070·reviewer): 극단적 대형 repo·느린 VPN 에서 1h 도 모자라면 코드 수정 없이
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

    `git worktree add` 는 fs 행위라 자동으로 안 한다(ADR-0013·사용자 게이트 유지) —
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
    """dirty 또는 활성(leased/creating) 슬롯을 force=False 로 remove_slot 하려 함 (작업 유실·사용중 슬롯 보호·T-0333).

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
    """같은 task 를 살아있는 다른 세션이 열고 있다 — 동시 세션 거부(㉑·T-0353).

    task 활성 pid 생존검사(부트스트랩 진입 시): 기록된 pid 가 **살아있고 내 pid 와 다르면** 이
    예외로 거부한다("다른 창에서 열려 있음"). pid 가 죽었으면(crash) 조용히 회수 후 진입(정상
    재개) — `reclaim_stale` 의 pid-생존 판정(`_pid_alive`)과 **동형 primitive**(신설 개념 0).
    의도적 2창 동시 열람은 막힌다(드묾·사고 재개방보다 낫다·㉑ 트레이드오프 수용).
    """

    def __init__(self, name: str, pid: int):
        self.name = name
        self.pid = pid
        super().__init__(
            f"task {name!r} 이(가) 다른 살아있는 세션(pid {pid})에서 열려 있습니다"
        )


class InvalidTaskName(Exception):
    """task 명이 안전한 단일 path 컴포넌트가 아니거나 예약 패턴이다 — fail-loud (T-0353·must-fix).

    `task_dir(name)=TASKS_DIR/name` 이 무검증이면 `--task ../../evil`·`/tmp/x`·`a/b`·빈 문자열이
    작업트리 밖/임의 경로에 디렉토리를 만들고 장부를 오염시킨다(reviewer 실측). 검증은 **엔진층
    (bind_task 진입점)**에 둔다 — CLI 검증만으론 T-0354~0357 의 bind_task 직접 소비가 우회된다.
    거부: 빈/공백·path separator(`/`·`\\`)·선행 `.`(숨김/상대 traversal)·단일 컴포넌트 아님·(등록
    repo 넘기면) `<repo>_<N>` 슬롯 세션 예약(⑥). `reason` = 위반 사유(진단용).
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"부적합 task 명 {name!r} — {reason}")


class NotTaskOwner(Exception):
    """release `--task <이름>` 이 그 task 명의가 아닌 슬롯을 반납하려 함 — 소유검사 거부 (T-0354·F3).

    슬롯↔task 연결 = lease.session == task 이름 (alloc `--task` 가 session=task 로 리스·⑥).
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
    """슬롯 worktree 의 branch checkout 실패 (ADR-0013).

    fail-soft 로 무시하면 리스 장부의 state/session 이 실제 worktree 상태와 어긋난다
    (부분 leased 전이). alloc 은 checkout 성공 시에만 장부를 갱신하고, 실패하면 이를
    raise 해 기존 리스 상태를 보존한다(부분 갱신 차단). 브랜치 자체는 더는 장부에
    저장하지 않는다(git=진실·ADR-0013 amend T-0072) — checkout 은 git HEAD 를 바꾼다.
    """

    def __init__(self, slot: str, branch: str, output: str):
        self.slot = slot
        self.branch = branch
        self.output = output
        super().__init__(
            f"git checkout {branch!r} failed for slot {slot!r}: {output!r}"
        )


class BareRepoMissing(RuntimeError):
    """worktree 의 공유 .git 원(`.repos/<repo>.git` bare)이 없다 (ADR-0011 §31).

    [[ADR-0011]] §31 = `.repos/<repo>.git` 가 worktree 슬롯의 공유 .git 원(canonical).
    이 bare mirror 가 없으면 `git worktree add` 의 base 가 없다 — `pm-config repo add <repo>` 가
    bare clone 으로 mirror 를 (재)생성해야 한다([[T-0061]]). **repo 가 areas.md 에 이미
    등록됐으면**(하나의 채택 폴더를 여러 사람이 clone 한 2번째 사용자·`.repos/` 는 gitignore·
    per-clone 이라 공유 안 됨) `pm-config repo add <repo>` 를 **`--git` 없이** 실행하면 areas
    등록 URL 로 mirror 를 hydrate 한다([[T-0291]]). 침묵 폴백으로 multi-PM 루트 자신의 worktree 를
    만들면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해 토폴로지가 깨진다([[ADR-0013]]
    fail-soft 규율) → 명시 raise 로 선행 명령을 안내한다.

    **`RuntimeError` 서브클래스**인 이유: 파사드 `pm_config.cmd_worktree_add` 가
    `create_slot` 의 실패를 `except RuntimeError` 로 잡아 사용자 안내 rc 1 로 surface 한다
    ([[T-0061]]). 베이스를 `Exception` 으로 두면 그 가드를 빠져나가 traceback 이 노출된다
    (cross-module 규격 — codex T-0063 게이트 포착).
    """

    def __init__(self, repo: str, bare_path: "Path", *, broken: bool = False):
        self.repo = repo
        self.bare_path = bare_path
        # broken=True (T-0294) = 경로는 있으나 유효 bare 가 아님(부분/깨진 bare) — 부재와 구별해
        # exists-but-broken 진단을 부재 케이스 수준으로 안내한다. broken=False = 종전 경로부재.
        self.broken = broken
        if broken:
            super().__init__(
                f"bare mirror for {repo!r} at {str(bare_path)!r} exists but is not a valid bare "
                f"git repo — 부분/깨진 bare (중단된 `git clone --bare` 잔존 가능성·하네스 타임아웃/"
                f"Ctrl-C·T-0294). `.repos/{repo}.git` 경로는 있으나 `git worktree add` 의 base 로 "
                f"못 써 나중 날 git 에러로 죽는다. 자동 삭제는 하지 않는다(사용자 데이터 오판 위험·"
                f"삭제는 사용자 위임) — `.repos/{repo}.git` 를 수동 삭제 후 "
                f"`pm-config repo add {repo}`(--git 불요·areas 등록 URL 로 재hydrate·미등록이면 "
                f"`--git <url>`)로 재생성하라 (ADR-0011 §31·T-0294)"
            )
        else:
            super().__init__(
                f"bare mirror for {repo!r} not found at {str(bare_path)!r} — "
                f"`.repos/{repo}.git`(worktree 공유 .git 원)가 없다. areas.md 에 이미 등록됐으면 "
                f"(multi-user: `.repos/` 는 gitignore·per-clone 이라 공유 안 됨) "
                f"`pm-config repo add {repo}`(--git 불요)가 areas 등록 URL 로 mirror 를 hydrate 한다; "
                f"미등록 신규 repo 면 `pm-config repo add {repo} --git <url>` (ADR-0011 §31·T-0291)"
            )


class SlotBranchExists(RuntimeError):
    """create_slot 이 파려는 슬롯 전용 브랜치 `<repo>_<N>` 가 이미 존재한다 — 미머지-보존 브랜치 충돌 (T-0335).

    `remove_slot` ④ 가 **미머지 전용 브랜치**(`<repo>_<N>`)를 보존(작업 유실 방지)한 뒤, 같은 번호
    슬롯을 branch-무지정 경로로 재생성하면 `git worktree add` 가 `fatal: a branch named '<repo>_<N>'
    already exists`(rc≠0)로 죽는다. 두 경로 모두 슬롯 전용 브랜치 `<repo>_<N>` 를 판다 — base-경로는
    명시 `--no-track -b <repo>_<N> <path> origin/<base>`, else-경로(base·branch 둘 다 미지정)는 git 이
    슬롯 path basename(=`<repo>_<N>`)으로 브랜치를 자동 생성한다. 슬롯번호는 ledger∪git-worktree
    병합(T-0295)으로 회피하지만, 그 병합은 **worktree 없이 잔존하는 브랜치**(보존 브랜치는 worktree 를
    안 가짐·브랜치 축은 슬롯번호 축과 독립)를 못 본다.

    옛 동작은 이 실패를 create_slot 의 already-exists 진단이 "worktree 경로 이미 등록(orphan)"으로
    **오귀인**했다(T-0333 reviewer 실측 — orphan 정리 안내를 냈지만 지울 orphan worktree 는 없다).
    이 예외가 그 오귀인을 정정한다: **정확한 원인(브랜치 잔존) + 두 갈래 선택**(브랜치 정리 후 새 슬롯
    재생성 / 그 브랜치를 checkout 해 재개)을 fail-loud 로 준다(결정 (b)·데이터 유실 없음 — 현재도 loud 실패였다).

    **왜 (a) 브랜치 재사용-체크아웃이 아니라 (b) fail-loud 인가**: base-경로는 슬롯을 *`base`(origin/
    <base> 최신)에서* 시작하도록 요청한 것이다. 보존 브랜치는 정의상 `base` 에 없는 커밋을 가져
    미머지된 것이라(그래서 remove_slot ④ 가 보존했다) 그 브랜치를 *base-경로에서* 재사용하면 슬롯이
    요청한 base 가 아닌 옛 미머지 작업 위에서 **조용히** 시작한다(base ≠ 브랜치 HEAD·silent 시맨틱
    어긋남). 사용자가 옛 작업 재개를 모른 채 진행할 위험이라 — 재개는 **명시 의사로만**(그 브랜치를
    슬롯에 checkout) 열어 둔다. 이 코드베이스의 fail-loud 규율(`BareRepoMissing`·`RemoveRefused`)과 정합.

    ✅ **재개 경로는 둘 다 리셋 없이 안전 (T-0343 근본 fix)**: 그 브랜치를 checkout 해 미머지 작업을
    이어가려면 (1) 수동 `git worktree add <path> <repo>_<N>` 또는 (2) `create_slot(branch=<repo>_<N>)`
    — 둘 다 **기존 브랜치를 그 tip 에서 checkout**(리셋 없음)한다. create_slot 의 branch-경로가 옛날엔
    `-B`(create-or-reset)라 기존 브랜치를 리셋해 보존 커밋을 잃었으나(T-0335 codex), T-0343 이 존재
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
            f"없이 보존 커밋 tip 에서 checkout·T-0343). (T-0335·데이터 유실 없음)."
        )


# Lease 직렬화 키 분류 (T-0350·additive 스키마 클래스 폐쇄). from_dict 는 canonical 을 각
# 필드로 소비하고, 그 외 미지 키는 `extra` 로 보존해 to_dict 가 재방출한다(구·신 엔진 왕복 무손실).
#   - CANONICAL = 이 엔진 버전이 아는 1급 필드(각자 self.* 로 소비·extra 아님).
#   - DROP = legacy 최상위 `branch`(ADR-0013 amend T-0072 로 권위 폐지) — extra 로도 보존하지
#     않는다. 표시는 git=진실(`current_branch`)이라 장부에 branch 를 되살리면 드리프트 원천이
#     재생긴다(테스트 `test_from_dict_ignores_legacy_branch_key` 가 이 무시를 못박음). git 필드
#     안의 `branch` 서브키(작업 브랜치 스냅)와는 다른 축 — 그건 git blob 의 일부로 보존된다.
_LEASE_CANONICAL_KEYS = frozenset(
    {"slot", "repo", "session", "pid", "started", "state", "test_cmd", "git", "role"}
)
_LEASE_DROP_KEYS = frozenset({"branch"})


class Lease:
    """리스 장부 한 엔트리 (ADR-0013 스키마·sealed spike §3b·amend T-0072·git 필드 amend T-0350/ADR-0060).

    슬롯=브랜치-무관 컨테이너·session/pid=점유 주체·state=leased|idle|creating. `creating` 은
    create_slot 의 provisional 마커(worktree add 전 선기록·확정 시 leased·중단 시 흔적·T-0295).
    **브랜치 *표시*는 권위 필드가 아니다** — git 이 단일 진실(ADR-0013 amend T-0072)이라 장부에
    저장하지 않고 `current_branch(slot)` 로 슬롯 worktree 의 live HEAD 에서 읽는다(드리프트 불가능).

    **`git` 필드 (additive·T-0350·ADR-0060·엔진 전용 write·md 아님)**: 슬롯 git 상태를 *기대*
    (drift 감지 기준) 축으로 기계 기록한다 — `{base:{branch,commit}, branch, head, submodules:
    [{path,pin}], recorded_at}`(스키마 = spike §F9). T-0072 를 뒤집지 않는다: 표시는 여전히 live
    조회(`current_branch`)·기록은 별개의 *기대* 축(submodule pin/drift 모델을 본체로 대칭 확장).
    write 시점 = 부트스트랩 bind/alloc·핸드오프·create(release 시 정리)·compare 시점 = 0단계
    (`compare_slot_git`·T-0351 소비). 미기록(구 슬롯) = drift 감지 비활성(결정 ⑪). raw dict 로
    들고 있어 미지 서브키까지 왕복 보존한다(None=미기록·to_dict 는 None 이면 키 자체를 뺀다).

    **`extra` (미지 키 보존·T-0350)**: 이 엔진 버전이 모르는 최상위 키(`task`[T-035x] 등 향후
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
                 extra: "dict | None" = None):
        self.slot = slot          # "work/<repo>_<N>" (브랜치 무관)
        self.repo = repo          # repo 이름 (per-repo 네임스페이스)
        self.session = session    # 점유 세션 식별자
        self.pid = pid            # 점유 프로세스 pid (stale 회수 판정)
        self.started = started    # 리스 시작 시각 (UTC ISO)
        self.state = state        # "leased" | "idle" | "creating"(provisional·T-0295)
        self.test_cmd = test_cmd  # 슬롯 바인딩 회귀/빌드명령 (T-0066·ADR-0014 amend·None=미지정)
        self.git = git            # 슬롯 git 스냅 dict(base/branch/head/submodules/recorded_at)·None=미기록·T-0350
        # 슬롯 role (additive·T-0358·⑬·spike §F11): "work"(기본·배타 대여 작업 슬롯) | "readonly"
        # (research 전용 공유 슬롯·detached·session/pid 없음·alloc/release/reclaim 대상 제외). role 이
        # 0단계 carve-out(pm_bootstrap `_phase0_is_readonly`)·F6 소유검사 예외(identity_args)·엔진
        # mutation 거부(set-base/rebase/dev/sync)의 canonical 판별 축이다.
        self.role = role
        # 미지 최상위 키 보존(additive 스키마 클래스 폐쇄·T-0350). mutable 기본 회피(None 센티넬).
        self.extra = dict(extra) if extra else {}

    def __repr__(self) -> str:
        return (f"Lease(slot={self.slot!r}, repo={self.repo!r}, "
                f"session={self.session!r}, pid={self.pid!r}, state={self.state!r}, "
                f"test_cmd={self.test_cmd!r}, git={self.git!r}, role={self.role!r})")

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
        # 덧붙이지 않아 왕복이 무손실(하위호환·DoD). test_cmd 는 T-0066 부터 항상 방출(구 거동 유지).
        if self.git is not None:
            d["git"] = self.git
        # role 은 *기본("work")이 아닐 때만* 방출한다 (T-0358·git 필드 조건방출과 동형·하위호환).
        # 구 장부(role 부재=work 슬롯)를 로드·재기록해도 `role: "work"` 를 덧붙이지 않아 왕복이
        # byte-무손실이다(from_dict 가 부재 시 "work" 로 read). readonly 슬롯만 role 을 장부에 남긴다.
        if self.role != "work":
            d["role"] = self.role
        # 미지 키 재방출(구·신 엔진 왕복 무손실). extra 엔 canonical/legacy-branch 가 없다
        # (from_dict 가 배제) — canonical 을 덮을 위험 없음.
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Lease":
        # 하위호환 read: test_cmd 부재(구 장부)는 None·git 부재는 None. 구 장부의 legacy 최상위
        # `branch` 키는 관용적으로 *무시*한다(ADR-0013 amend T-0072 — branch 는 권위 필드가
        # 아니다·표시는 git 에서만 온다). canonical/legacy-branch 를 뺀 나머지 미지 키는 `extra`
        # 로 보존해 to_dict 가 재방출한다(구·신 엔진 왕복 무손실·additive 스키마 클래스 폐쇄·T-0350).
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
            role=d.get("role", "work"),   # 하위호환 read: 구 장부(role 부재) = "work"(작업 슬롯·T-0358).
            extra=extra,
        )


# Task 직렬화 키 분류 (T-0353·Lease 동형 additive 스키마). canonical 을 각 필드로 소비하고 그
# 외 미지 키는 `extra` 로 보존해 왕복 무손실(구·신 엔진 skew·향후 additive task 서브키 대비).
_TASK_CANONICAL_KEYS = frozenset({"name", "prefix", "pid", "started"})


class Task:
    """리스 장부 top-level `tasks` 컬렉션 한 엔트리 — 작업 단위 정체성 (T-0353·spike §3b·ADR-0059).

    task = 슬롯과 **직교**한 작업스트림 정체성(슬롯 0개로도 존재 가능·⑥). 슬롯-키 lease 행과 별개의
    top-level 컬렉션에 산다(같은 파일·같은 `_lease_lock`/atomic replace 직렬화). 필드:
      - `name`   — task 이름(사람이 정하는 자유 포맷·`<등록 repo>_<N>` 예약 제외·유일성=사람 안·⑥).
      - `prefix` — 이 task 세션의 board prefix(기본 None=없음·변경은 `task prefix`·T-0357·①ⓑ).
      - `pid`    — 현재 열려 있는 세션 pid(동시 세션 거부·㉑·`_pid_alive` 생존검사). 0=미점유.
      - `started`— task 레코드 생성 시각(UTC ISO).

    저장 위치 = 리스 장부 파일 top-level `tasks`(promote 확정·PM 73·결정론 ⓐ 출처 1개·⑨ 저장소
    신설 불요·⑳ pm_update 활성 pid 검사 단일 파일 스캔). `.local/tasks/<name>/` 는 **서술
    (pm_state.md)만** — 기계 상태는 장부(⑨ 경계). 미지 최상위 task 키는 `extra` 로 왕복 보존
    (Lease 동형·additive 스키마 클래스 폐쇄). (dataclass 미사용 — Lease 와 같은 forward-ref 회피.)
    """

    def __init__(self, name: str, prefix: "str | None" = None,
                 pid: int = 0, started: str = "", extra: "dict | None" = None):
        self.name = name
        self.prefix = prefix      # board prefix (None=없음·T-0357 이 변경·①ⓑ)
        self.pid = pid            # 현재 열려있는 세션 pid (㉑ 동시세션 생존검사·0=미점유)
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
    """슬롯 worktree 한 submodule 의 상태 — 역할(live HEAD) + pin 비교 판정 (ADR-0051 §Decision 4·T-0276).

    T-0275(`_resync_submodules_selective`)의 *역할 판별*을 표시층으로 재사용한다 — 역할은
    별도 장부 없이 submodule 의 live git HEAD 로 정한다(ADR-0051 §Decision 1·무스키마):

      - `kind="dev-ahead"` — **on-branch**(=dev 역할·`symbolic-ref -q HEAD` rc0). 사용자가 그
        submodule 에서 브랜치를 파 작업 중 → **정보**(경고 아님). detached pin 으로 낚아채지
        않는 selective resync 의 보호 대상(전역 recurse 가 파괴하던 크럭스 A).
      - `kind="drift"` — **detached & pin ≠ working**(`git submodule status` flag `+`/`U`) →
        **경고**. superproject pin 과 어긋난 detached. 재-alloc 시 T-0275 가 재동기하나 dirty 면
        잔존한다(그래서 dirty 도 실어 *왜* 안 풀렸는지 surface).
      - `kind="pinned"` — **detached & pin == working**(flag 공백) → 정상(pinned).
      - `kind="uninitialized"` — submodule 미초기화(flag `-`) → 경고(슬롯 init 비정상).

    `warning` = kind in {"drift", "uninitialized"}(dev-ahead/pinned 은 경고 아님 — 이 구별이
    ADR-0051 §Decision 4 의 핵심). `dirty` = 워킹트리 미커밋 변경(`_submodule_dirty` 재사용).
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
    """슬롯 worktree 한 줄 상태 — branch + upstream + submodule 역할 (ADR-0051 파일럿 T-β·T-0276).

    부트스트랩이 현재 슬롯의 상태를 1회 surface 하는 데 쓰는 구조(표시는 pm_bootstrap 이 담당).

      - `branch` — `current_branch(slot)` live(None=detached/조회불가).
      - `upstream` — `@{upstream}` 해소명(예 `origin/a5`·None=미해소).
      - `upstream_ok` — `@{upstream}` 해소 여부. 미해소=경고(T-0273/0274 로 슬롯 tracking 이
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
    """`git worktree list --porcelain` 한 엔트리 — 실 git worktree 의 경로/브랜치/상태 (T-0295).

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
    """리스 장부 × 실 git worktree 정합 결과 — orphan/stale/incomplete drift (조회 전용·T-0295).

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
    """`remove_slot` 결과 — 무엇을 지웠고 슬롯 전용 브랜치를 어떻게 처리했는지 (보고용·T-0333).

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
    """벽시계(로컬 tz) ISO8601 — git 스냅 `recorded_at` 용 (T-0350·spike §F9 `+09:00` 정합).

    리스 `started` 는 UTC(`_now_utc`)지만 `recorded_at` 은 **로컬 벽시계**로 둔다 — 사람이
    "여기 두고 간다"고 인지하는 스냅 시각이라 벽시계가 자연스럽고, spike §F9 스키마도 `+09:00`
    (로컬 offset)로 예시한다. `.astimezone()` 로 tz-aware(offset 포함)라 UTC 와 무손실 상호변환
    가능 — 둘 다 명시 offset ISO8601 이라 모호성 없음(표시 tz 만 다름·비교엔 무관)."""
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


# ── 자체 파일락 (board.py 와 같은 패턴·독립 구현·import 금지) ───────────────────
# 장부 read-modify-write 를 직렬화한다. POSIX=fcntl.flock·Windows=msvcrt.locking·둘 다
# 없으면 단일-머신 전제의 무락 폴백(락 파일만 생성). 프로세스가 죽으면 OS 가 락을 자동
# 해제(stale-lock 없음). stdlib 만 사용(외부 filelock 의존 금지·런타임 의존은 stdlib+git).


def _flock_acquire(fd: int) -> None:
    """OS 배타락 획득 (블로킹). POSIX=fcntl.flock·Windows=msvcrt.locking·폴백 no-op."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    except ImportError:
        pass
    # 폴백: 락 프리미티브 없음 — 단일-머신 전제로 무락 진행(락 파일만 존재).


def _flock_release(fd: int) -> None:
    """OS 배타락 해제. close 시 OS 가 자동 해제하지만 명시적으로 풀어 둔다."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    except ImportError:
        pass


@contextlib.contextmanager
def _lease_lock() -> Iterator[None]:
    """리스 장부 write 를 직렬화하는 OS 파일락 컨텍스트매니저 (ADR-0013).

    `.project_manager/.local/worktree-leases.lock` 에 배타 OS 락. 프로세스가 죽으면 OS 가
    자동 해제(stale-lock 없음). **재진입 금지** — 같은 프로세스가 이 컨텍스트를 중첩하면
    안 된다(flock 재진입 동작은 OS 별로 다름). 장부의 모든 read-modify-write 가 이 한
    구간 안에서 일어난다.
    """
    LEASES_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LEASES_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _flock_acquire(fd)
        try:
            yield
        finally:
            _flock_release(fd)
    finally:
        os.close(fd)  # close 만으로도 OS 가 락을 해제 (크래시 시 안전망)


# ── 장부 읽기/쓰기 (락 보유 전제) ────────────────────────────────────────────


def _read_ledger_raw() -> dict:
    """장부 파일의 **최상위 dict 원본**을 읽는다. 부재/손상/비-dict → 빈 dict(fail-soft).
    **_lease_lock 보유 전제**.

    `leases`(슬롯 리스)·`tasks`(T-0353)·향후 additive 최상위 컬렉션이 한 파일에 공존하므로,
    특정 컬렉션만 갱신할 때(`_write_ledger`/`_write_tasks`) 나머지 최상위 키를 무손실 보존하는
    read-modify-write 의 read 측이다. Lease-내부 미지 키 보존(T-0350·`extra`)의 **최상위판** —
    구 엔진(신규 최상위 키를 모르는 import 사본 lag)이 아무 op 해도 형제 컬렉션이 안 날아가게
    한다([[robustness-value-connections-before-ship]] silent degrade 방지)."""
    if not LEASES_FILE.exists():
        return {}
    try:
        data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_ledger_raw(data: dict) -> None:
    """최상위 dict 를 atomic replace 로 쓴다 (tmp→os.replace·부분쓰기 방지). **_lease_lock 보유 전제**."""
    LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = LEASES_FILE.with_suffix(".json.tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(str(tmp), str(LEASES_FILE))


def _read_ledger() -> list[Lease]:
    """리스 장부의 `leases` 컬렉션을 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    rows = _read_ledger_raw().get("leases", [])
    if not isinstance(rows, list):
        return []
    return [Lease.from_dict(d) for d in rows]


def _write_ledger(leases: list[Lease]) -> None:
    """`leases` 컬렉션만 교체해 장부를 atomic replace 로 쓴다. **_lease_lock 보유 전제**.

    **형제 최상위 키 보존**(T-0353·top-level round-trip): 현 파일 원본을 읽어(`_read_ledger_raw`)
    `leases` 키만 새 값으로 덮고 나머지(`tasks`·미지 additive 컬렉션)는 그대로 재방출한다 — 옛
    `{"leases": [...]}` 통짜 쓰기는 형제 컬렉션을 조용히 드롭했다(silent drop). read-modify-write
    가 같은 `_lease_lock` 안에서 직렬화되므로 read↔write 사이 파일 변동은 없다.
    """
    data = _read_ledger_raw()
    data["leases"] = [l.to_dict() for l in leases]
    _write_ledger_raw(data)


# ── git DI seam ────────────────────────────────────────────────────────────


def _real_git_runner(cwd: Path) -> GitRunner:
    """실 git 을 `cwd` 컨텍스트로 호출하는 GitRunner 를 만든다 (pm_import._real_git_runner 선례).

    반환 callable: argv(list) → (returncode, stdout+stderr). git 바이너리 부재(shutil.which)
    면 (1, msg)·예외는 (1, str(exc)) 로 감싼다(fail-soft·rc!=0 로 호출부에 위임). `git -C
    <cwd> <argv...>` 형태로 항상 그 work tree/repo 에 묶는다. 인코딩은 엔진 관례대로 UTF-8
    (한글 경로·메시지 안전).

    **stdout+stderr 결합 반환 (T-0070·pm_config._real_clone_runner 정합)**: 옛 코드는
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
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple[int, str]:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        try:
            result = subprocess.run(
                [git_binary, "-C", str(cwd), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                # captured=항상 유한: None(무제한)이면 silent hang → _GIT_TIMEOUT_DEFAULT 로 캡.
                timeout=GIT_TIMEOUT_SECONDS or _GIT_TIMEOUT_DEFAULT,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: 타임아웃/예외 메시지를 surface.
            return 1, str(exc)

    return runner


def _real_git_runner_interactive(
    cwd: Path, *, timeout: "int | None" = SUBMODULE_TIMEOUT,
) -> GitRunner:
    """console-visible 인터랙티브 git runner — stdio 콘솔 상속·튜닝 가능한 timeout (T-0070·T-0292).

    `_real_git_runner` 와 달리 **capture 하지 않는다** — stdout/stderr/stdin 을 부모 콘솔에
    그대로 상속한다(`subprocess.run(..., capture_output 안 줌`). 그래서:
      - 대형 clone/checkout 의 진행상황이 화면에 실시간 표시된다(긴 침묵 대신).
      - git credential/auth 프롬프트가 작동한다(수동 콘솔 실행과 동일).
      - `timeout` 이 관대(또는 None=무제한)라 느린 op 이 짧은 고정값에 false-kill 되지 않는다.

    **두 호출부 (같은 패턴·다른 timeout)**:
      - submodule init (T-0070) — `timeout` 미지정 → 기본 `SUBMODULE_TIMEOUT`(3600s·env
        `PM_SUBMODULE_TIMEOUT`). 600s 초과 대형 clone 이 TimeoutExpired 로 죽던 블로커 해소.
      - worktree add (T-0292) — `timeout=GIT_TIMEOUT_SECONDS`(1800s·env `PM_GIT_TIMEOUT`).
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
    """`git status --porcelain` 출력에서 *실제 status 엔트리* 라인만 추린다 (T-0070).

    porcelain v1 엔트리 형식 = `XY <path>`(X·Y = 2글자 status code·세 번째가 공백). git
    경고(stderr·`warning: ...`)가 stdout 캡처에 섞여도 그 형식이 아니므로 걸러진다 —
    `_real_git_runner` 가 stdout+stderr 를 합치게 바뀐 뒤(T-0070) dirty 오탐을 막는 가드.
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

    ⚠️ stderr 오탐 방어(T-0070): `_real_git_runner` 가 stdout+stderr 를 합쳐 반환하게
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


# ── 슬롯 네이밍 ──────────────────────────────────────────────────────────────


def _slot_for(repo: str, n: int) -> str:
    """슬롯 식별자 `work/<repo>_<N>` (브랜치 무관·ADR-0013·sealed spike §8-6)."""
    return f"work/{repo}_{n}"


def slot_path(slot: str) -> Path:
    """슬롯 식별자(`work/<repo>_<N>`) → 절대 경로 (REPO 기준)."""
    return REPO / slot


def bare_repo_path(repo: str) -> Path:
    """repo 이름 → 그 repo 의 공유 .git 원 경로 `.repos/<repo>.git` (bare·ADR-0011 §31).

    worktree 슬롯이 add/remove 되는 git 컨텍스트. `pm_config.REPOS_DIR / f"{repo}.git"` 와
    같은 관례([[T-0061]]) — worktree 풀이 import 격리(board·pm_config 미import)라 자체 해소한다.
    """
    return REPOS_DIR / f"{repo}.git"


def _is_valid_bare(bare_path: Path, *, runner: GitRunner) -> bool:
    """`.repos/<repo>.git` 가 *worktree add 의 base 로 실제로 쓸 수 있는 bare git repo* 인지 검증 (T-0294).

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
    를 결합 반환(T-0070)하므로, 유효 bare 라도 git 이 stderr 경고 한 줄을 내면 `out.strip()==
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
    """로컬 브랜치 `branch` 가 존재하는지 — **color-safe** machine-readable 출력으로 판정 (T-0343·T-0335 통합).

    create_slot 의 두 곳이 브랜치 존재를 본다: (1) branch 미지정 경로의 슬롯-전용 브랜치 `<repo>_<N>`
    선-검출(→ `SlotBranchExists` fail-loud·T-0335), (2) 명시 `branch=` 경로의 checkout(기존)/`-B`(신규)
    분기(→ 기존 브랜치 리셋-유실 방지·T-0343). 둘 다 이 단일 helper 를 경유한다.

    ⚠️ **평문 `git branch --list <b>` 는 ambient `color.branch=always` 서 ANSI 오염**(codex 실측:
    출력 `'  A_1\\x1b[m\\n'` → `.split()` 토큰 `'A_1\\x1b[m'` 이 브랜치명과 불일치 → 기존 브랜치를
    "없음"으로 오판 → (2)가 checkout 대신 `-B` 로 가서 리셋-유실 재개방). 그래서 **`--format=
    %(refname:short)`**(ref-filter 포맷·색 atom 없음)로 뽑는다 — `color.branch=always` 여도 평문
    (`'A_1\\n'`·실측)이라 오염이 없다. **splitlines 정확-일치**(`line.strip()==branch`)로 판정:
    exact 패턴(`--list <b>`·glob 메타 없음)이라 그 브랜치만 리스트하고, `_real_git_runner` 의
    stdout+stderr 결합(T-0070)에 stderr 경고가 섞여도 그 라인은 브랜치명과 정확-일치하지 않아
    안전(`.split()` 부분매치보다 견고).

    **rc 무시** — `git branch --list` 는 매치 없어도 rc0 이라 rc 기반(show-ref/rev-parse)은 못 쓴다
    (게다가 주입 runner 의 generic 폴백 rc0 이 "존재" 오탐돼 hermetic 테스트를 깬다·T-0335 실측).
    출력 라인 정확-일치만 신뢰한다. 주입 runner(테스트 mock)의 generic 폴백 `(0, "")` 은 빈
    splitlines → **"부재"**(안전 기본 — checkout 대신 `-B` 생성·리셋 대상 없음).
    """
    _rc, out = runner(["branch", "--list", "--format=%(refname:short)", branch])
    return any(line.strip() == branch for line in str(out).splitlines())


# ── 보호 브랜치 pre-push 훅 (T-0076·하드·회사 repo 무영향 / T-0223·라이브 게이트 승격) ────
# 훅 = `.project_manager/.local/repo-hooks/<repo>/pre-push`(프레임워크 소유·gitignore).
# bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리로 set → 슬롯 push 가 이 훅에
# 게이트된다. **회사 repo 서버/사용자 클론 무변경** — client-side·우리 bare 미러 config 1줄만.
#
# 훅은 *generic* 이다 — 보호목록을 sidecar 파일(`protected`·줄당 1브랜치·훅과 같은 디렉토리)
# 에서 읽는다. 설치(install_protected_hook)가 그 sidecar 를 채우므로(목록 변경 = 재설치로
# 갱신), 훅 본문 자체는 repo 무관하게 동일하다. POSIX sh — Windows git 번들 sh 로도 동작.
#
# 로직: stdin 의 `<localref> <localsha> <remoteref> <remotesha>` 줄들(=이 push 의 모든 ref)을 순회한다.
# pre-push 는 push 전체에 한 번 발화하는 all-or-nothing 게이트라 **보호 ref 를 전부 검증**하고 하나라도
# 실패하면 push 전체를 거부한다(예: `git push main release` 서 main 만 green 이어도 release 가 미검증이면
# 편승 차단). remote ref (`refs/heads/<b>`)의 `<b>` 가 sidecar 보호목록에 있으면 (그 ref 의 localsha 로) —
#   - `PM_ALLOW_PROTECTED_PUSH=1` 아니면 하드 차단(T-0076·echo 안내·exit 1).
#   - `PM_ALLOW_PROTECTED_PUSH=1` 이면 **라이브 게이트 승격**(T-0223·ADR-0039 D2/D3): push 되는
#     sha 로 `board.py livegate check --rev <sha>` rc0 을 추가 요구한다(릴리즈 라이브 wave 가
#     그 커밋에서 green 이어야 함). rc≠0 → 거부(2분기 안내). `PM_SKIP_LIVE_GATE=1` 이면 check
#     만 생략(라이브-무관·긴급 변경 한정·승인/protected 시맨틱 불변). python/board.py 를 못
#     돌리면 fail-closed 거부(게이트 무력화 방지). board.py 는 **PM 홈 엔진**을 쓴다 — 슬롯
#     worktree 는 회사/family repo checkout 이라 PM 엔진 파일이 없으므로(T-0076 회사 repo 무영향),
#     설치자가 훅 옆에 쓴 sidecar `engine-root`(PM 홈 REPO 절대경로 1줄)에서 board.py 를 해소한다
#     (livegate.json 도 그 PM 홈 .local 소유라 기록 위치와 정합)·인터프리터는 T-0209 실행검증 폴백.
# feature(비보호) 브랜치·tag push 는 통과(exit 0·PM_ALLOW/라이브 게이트 무관).
#
# **멱등 자가치유 배포**: install_protected_hook 이 매 호출 이 본문을 덮어쓰므로(repo add·
# worktree add), 엔진 update 후 다음 재설치가 이 신 버전을 자동 배포한다.
_PROTECTED_PRE_PUSH_HOOK = """\
#!/bin/sh
# pm 보호 브랜치 pre-push 가드 (T-0076·T-0223) — PM 이 보호 브랜치(main 등)에 자율 push 못 하게 +
# 승인(PM_ALLOW_PROTECTED_PUSH=1)된 protected push 도 릴리즈 라이브 게이트 green 을 추가 요구.
# install_protected_hook() 가 설치. 보호목록 = 같은 디렉토리의 sidecar `protected`(줄당 1브랜치).
hook_dir=$(dirname "$0")
protected_file="$hook_dir/protected"
engine_root_file="$hook_dir/engine-root"
[ -f "$protected_file" ] || exit 0

# stdin(<localref> <localsha> <remoteref> <remotesha> 줄들)의 각 push ref 를 순회한다. pre-push 는
# push 전체에 한 번 발화(all-or-nothing) — 보호 ref 를 *전부* 검증하고, 하나라도 실패하면(하드 차단
# or 라이브 게이트 미green) exit 1 로 push 전체를 거부한다(한 push 에 보호 ref 여러 개면 각 ref 의
# localsha 로 각각 게이트·미검증 ref 가 green ref 에 편승해 올라가는 것 차단). board.py/인터프리터는
# 첫 게이트 검증 때 1회 지연 해소한다. stdin 은 파이프 아닌 fd0 직독이라 루프가 현재 셸에서 돌아
# exit·변수 누적이 훅 전체에 반영된다(서브셸 아님).
board=""
py=""
resolved=0
while read -r _local_ref local_sha remote_ref _remote_sha; do
    case "$remote_ref" in
        refs/heads/*) branch=${remote_ref#refs/heads/} ;;
        *) continue ;;
    esac

    # 이 ref 가 sidecar 보호목록에 있나?
    is_protected=0
    while IFS= read -r protected_branch; do
        [ -n "$protected_branch" ] || continue
        if [ "$branch" = "$protected_branch" ]; then
            is_protected=1
            break
        fi
    done < "$protected_file"
    [ "$is_protected" = "1" ] || continue   # 비보호 ref(feature)·tag → 이 ref 통과·다음 ref.

    # 보호 ref — 승인(PM_ALLOW_PROTECTED_PUSH=1) 없으면 하드 차단 (T-0076·즉시 push 전체 거부).
    if [ "$PM_ALLOW_PROTECTED_PUSH" != "1" ]; then
        echo "[pm 보호 가드] 보호 브랜치 '$branch' 로의 push 거부 (T-0076)." >&2
        echo "  PM 은 보호 브랜치에 자율 commit/push 하지 않는다 — feature 브랜치로 작업하고" >&2
        echo "  main 갱신은 사용자에게 맡긴다(PR/머지). 사용자 명시 OK 면:" >&2
        echo "    PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi

    # 승인됨(PM_ALLOW=1) — 라이브 게이트 승격 (T-0223·ADR-0039 D2/D3). 승인과 검증 스킵은 별개
    # 스위치(감사 가능). PM_SKIP_LIVE_GATE=1 이면 이 ref 의 라이브 check 만 생략(승인·protected 불변).
    [ "$PM_SKIP_LIVE_GATE" = "1" ] && continue

    # 라이브 게이트 검증 자원 1회 지연 해소. board.py 는 **PM 홈 엔진**에 있다 — 슬롯 worktree(회사/
    # family checkout)엔 PM 엔진 파일이 없으므로(T-0076 회사 repo 무영향), 설치자가 훅 옆에 쓴 sidecar
    # `engine-root`(PM 홈 REPO 절대경로 1줄)에서 board.py 를 해소한다. livegate.json 도 그 PM 홈 .local
    # 소유라 기록 위치와 정합. 인터프리터는 실행검증 폴백(python3->python->py·T-0209·WindowsApps 가짜
    # shim 회피). sidecar 부재/경로 무효/인터프리터 부재 = fail-closed 거부(무력화 방지·ADR-0039 D3).
    if [ "$resolved" != "1" ]; then
        engine_root=""
        [ -f "$engine_root_file" ] && IFS= read -r engine_root < "$engine_root_file"
        if [ -n "$engine_root" ] && [ -f "$engine_root/.project_manager/tools/board.py" ]; then
            board="$engine_root/.project_manager/tools/board.py"
        fi
        for _cand in python3 python py; do
            if command -v "$_cand" >/dev/null 2>&1 && "$_cand" --version >/dev/null 2>&1; then
                py="$_cand"
                break
            fi
        done
        if [ -z "$board" ] || [ -z "$py" ]; then
            echo "[pm 라이브 게이트] 게이트 검증 실행 불가 — fail-closed 거부 (T-0223)." >&2
            echo "  보호 브랜치 push 는 라이브 게이트 green 을 요구하는데 PM 엔진 board.py 를 못 찾았다" >&2
            echo "  (engine-root sidecar='${engine_root}', py='${py}'). 게이트를 못 돌리면 무력화 방지로 거부한다." >&2
            echo "  라이브-무관 변경(docs 등)·긴급 hotfix 면 우회:" >&2
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
        # rc!=0 — 이 보호 ref 가 미green → push 전체 거부 + 2분기 안내 (환경 복구 vs 우회·ADR-0039 D3).
        echo "[pm 라이브 게이트] 보호 브랜치 '$branch' push 거부 — 라이브 게이트 미green (T-0223·ADR-0039)." >&2
        echo "  릴리즈(main 머지)는 릴리즈 라이브 wave 가 이 커밋에서 green 이어야 한다(위 사유 참조)." >&2
        echo "  - 환경 문제(오프라인·LLM 서비스 장애/한도·게이트 오작동)면 — 우회 아님·환경 복구 후 재실행:" >&2
        echo "      board.py livegate record" >&2
        echo "  - 라이브-무관 변경(docs 등)·긴급 hotfix 면 — 우회:" >&2
        echo "      PM_SKIP_LIVE_GATE=1 PM_ALLOW_PROTECTED_PUSH=1 git push ..." >&2
        exit 1
    fi
done

# 모든 보호 ref 가 게이트 통과(또는 skip)·또는 보호 ref 없음(비보호/tag) → push 통과.
exit 0
"""


def install_protected_hook(
    repo: str,
    protected: list[str],
    *,
    git_runner: GitRunner | None = None,
) -> bool:
    """보호 브랜치 pre-push 훅 + sidecar 를 (재)설치하고 bare `core.hooksPath` 를 wiring 한다 (T-0076).

    **멱등·자가치유** — `pm-config repo add`·`worktree add` 가 매번 호출(이미 있으면 갱신).
    세 가지를 한다:
      1. 훅 디렉토리 `.project_manager/.local/repo-hooks/<repo>/` 생성(프레임워크 소유·gitignore).
      2. `pre-push` 훅(generic·POSIX sh·LF) + sidecar 2종: `protected`(보호목록·줄당 1브랜치)와
         `engine-root`(PM 홈 REPO 절대경로 1줄·T-0223 라이브 게이트 board.py 해소용). 목록/루트가
         바뀌면 재설치가 sidecar 를 덮어 갱신한다(훅 본문은 불변).
      3. bare(`.repos/<repo>.git`)의 `core.hooksPath` 를 그 디렉토리(절대경로)로 set
         → 슬롯 push 가 이 훅에 게이트된다.

    **bare 부재 = no-op·False**(가드) — bare 가 없으면 게이트할 대상이 없다(repo add 가 아직
    clone 안 함·솔로(단일 repo)). 훅/sidecar 도 쓰지 않고 조용히 False(설치 안 함). bare 존재면 설치
    후 True. **회사 repo 무영향** — 모든 write 는 `.project_manager/.local/` + bare config 1줄
    (client-side)·서버 ref/사용자 클론 무변경.

    `git_runner` 주입 시 `core.hooksPath` config 호출을 그 runner 로(테스트 hermetic·`git -C
    <bare>` 컨텍스트는 `_real_git_runner(bare)` 가 묶는다). LF 줄바꿈 명시(Windows 에서도 sh
    가 읽도록·newline="\\n").
    """
    bare = bare_repo_path(repo)
    if not bare.exists():
        return False  # 게이트할 bare 없음 — no-op(repo add 선행 전·솔로).

    hook_dir = REPO_HOOKS_DIR / repo
    hook_dir.mkdir(parents=True, exist_ok=True)

    # 1) pre-push 훅 (generic·POSIX sh·LF). 멱등 — 매 호출 덮어쓰기(엔진 update 자가치유).
    hook = hook_dir / "pre-push"
    hook.write_text(_PROTECTED_PRE_PUSH_HOOK, encoding="utf-8", newline="\n")
    hook.chmod(0o755)

    # 2) sidecar `protected` — 보호목록(줄당 1브랜치). 목록 변경 시 재설치가 갱신.
    sidecar = hook_dir / "protected"
    sidecar.write_text(
        "".join(f"{b}\n" for b in protected), encoding="utf-8", newline="\n")

    # 2.5) sidecar `engine-root` — PM 홈 REPO 절대경로 1줄 (T-0223 라이브 게이트 board.py 해소용).
    # 설치자는 PM 홈 컨텍스트에서 도므로 REPO(=engine root)를 안다. 훅은 슬롯 worktree(회사/family
    # checkout·PM 엔진 파일 없음)에서 발화하므로 self-locate 불가 — 이 sidecar 로 PM 홈 board.py 를
    # 해소한다(livegate.json 도 PM 홈 .local 소유·기록 위치 정합). protected sidecar 와 동형·멱등.
    (hook_dir / "engine-root").write_text(
        f"{REPO.resolve()}\n", encoding="utf-8", newline="\n")

    # 3) bare core.hooksPath wiring (절대경로) — client-side·우리 미러 config 1줄.
    # **rc 검사(codex T-0076)**: config 실패면 훅이 실제로 wiring 안 됐는데 성공 보고하면 보호
    # 가드가 *침묵 무력화* 된다(하드 차단 보장 위반). rc≠0 → False 반환(호출부가 경고 surface).
    runner = git_runner or _real_git_runner(bare)
    rc, _out = runner(["config", "core.hooksPath", str(hook_dir.resolve())])
    return rc == 0


def _slot_number(slot: str) -> float:
    """슬롯 식별자(`work/<repo>_<N>`)의 번호 N — 정렬 키 (T-0354·최소 번호 대여 결정론 ⓒ).

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


def _pid_alive(pid: int) -> bool:
    """pid 가 살아있는지 (stale 회수 판정·ADR-0013 — 타임아웃/heartbeat 기각·pid 생존만).

    POSIX: `os.kill(pid, 0)` — ESRCH=죽음·EPERM=살아있으나 권한 없음(=살아있음으로 간주).
    Windows: OpenProcess 로 핸들 획득 가능 여부. pid<=0 은 죽음으로 본다.
    """
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover — POSIX 테스트 환경에선 미실행
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 살아있으나 시그널 권한 없음 — 생존으로 간주(보수적·조기회수 방지)


# ── 공개 API ─────────────────────────────────────────────────────────────────


def reclaim_stale(*, git_runner: GitRunner | None = None) -> list[str]:
    """pid 죽은 leased 슬롯을 회수한다. 회수된 슬롯 식별자 리스트 반환 (ADR-0013).

    stale = `state==leased && pid 죽음`. 회수 시 dirty 면 stash 로 보존(작업 유실 방지)하고
    idle 로 전이한다(슬롯=재사용 컨테이너·worktree 폴더는 유지). alloc 진입 시 자동 호출된다.
    타임아웃/heartbeat 회수는 ADR-0013 에서 기각(조용하지만 작업 중 오판) — pid 생존만 본다.
    """
    reclaimed: list[str] = []
    with _lease_lock():
        leases = _read_ledger()
        changed = False
        for lease in leases:
            if lease.state != "leased":
                continue
            # readonly 공유 슬롯(⑬·T-0358) 제외 — session/pid 없는 무소유 공유 자산이라 pid=0(죽음)
            # 으로 보여도 회수 대상이 아니다(회수하면 idle 화돼 alloc 이 잡아채 role 이 유실된다·§F11).
            if lease.role == "readonly":
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
            # ⚠️ **git 은 의도적으로 보존한다 — 여기 `lease.git = None` 을 넣지 마라** (T-0350·crash-resume
            #    계약·load-bearing). release/force_release 는 반대로 git 을 정리(None)하는데 이 **비대칭은
            #    의도적**이다: 그쪽은 *명시적 teardown*(작업완료 반납 → 기대 리셋)이지만, reclaim 은
            #    **crash(pid 죽음) 회수**라 다르다. 죽은 세션이 남긴 스냅(head/base)이 reclaim 을 넘어
            #    살아야, 다음 부트스트랩 0단계 compare(T-0351)가 live 를 "내 crash 커밋의 후손
            #    (descendant=notice·정상 재개)" vs "외부 개입(diverged=FAIL-LOUD)"으로 가른다
            #    (`_head_relation`·㉒). 지우면 그 판정이 unrecorded 로 무력화돼 crash-resume 이 조용히
            #    깨진다. base 도 슬롯 파생 원점(슬롯 속성)이라 같은 워크스트림 재개 시 보존이 옳다.
            #    표면상 모순돼 보이는 release/force_release 의 `git=None` 은 여기 비대칭으로 해소된다 —
            #    향후 '일관성 fix' 로 여기 git 정리를 넣으면 crash-resume 판정이 조용히 사라진다
            #    (`test_reclaim_stale_preserves_git_for_crash_resume` 가 그 회귀를 하드 차단).
            reclaimed.append(lease.slot)
            changed = True
        if changed:
            _write_ledger(leases)
    return reclaimed


# ── task 컬렉션 (top-level `tasks`·슬롯과 직교·⑥·T-0353) ───────────────────────
# 리스 장부 파일의 top-level `tasks` 배열에 산다(leases 와 형제·같은 `_lease_lock`/atomic).
# 슬롯 0개로도 존재 가능(task 는 alloc 전에 먼저 생긴다). 동시 세션 거부(㉑)는 pid 생존검사로
# reclaim_stale 과 동형 판정(`_pid_alive`)한다 — 신설 개념 0.


def _validate_task_name(name: str, registered_repos: "list[str] | set[str] | None" = None) -> None:
    """task 명을 안전한 단일 path 컴포넌트로 검증한다 — 위반 시 `InvalidTaskName`(fail-loud·must-fix).

    `task_dir(name)` 이 무검증 조인이라 traversal/절대경로/빈 문자열이 작업트리 밖에 디렉토리를
    만들고 장부를 오염시킨다(reviewer 실측 `--task ../../evil` → git-tracked `.project_manager/evil`).
    **엔진 진입점(bind_task)**에서 검증해 CLI 우회(T-0354~0357 직접 소비)까지 닫는다. mkdir·장부 write
    **이전**에 raise 한다(부작용 0). `registered_repos` 주면 `<repo>_<N>` 예약(⑥)도 거부(primitive
    자기완결·CLI 의 빠른 거부와 이중화·should-fix). 예약 판정 정규식은 CLI 의
    `identity_args.is_reserved_task_name` 과 동형(모듈 격리라 inline·ADR-0013).

    **문자 도메인 = 하류 구문 표면에 맞춘 협소화**(T-0356 codex 2건의 단일 불변식·per-surface 이스케이프
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
    """task 서술 공간 `.local/tasks/<name>/` 경로 (pm_state.md·메타·⑮). 기계 상태는 장부·서술만 여기(⑨)."""
    return TASKS_DIR / name


def _read_tasks() -> list[Task]:
    """장부의 `tasks` 컬렉션을 읽는다. 부재/손상 → 빈 리스트(fail-soft). **_lease_lock 보유 전제**."""
    rows = _read_ledger_raw().get("tasks", [])
    if not isinstance(rows, list):
        return []
    return [Task.from_dict(d) for d in rows if isinstance(d, dict) and d.get("name")]


def _write_tasks(tasks: list[Task]) -> None:
    """`tasks` 컬렉션만 교체해 장부를 atomic replace 로 쓴다 — 형제 `leases`·미지 키 보존(top-level
    round-trip). **_lease_lock 보유 전제**."""
    data = _read_ledger_raw()
    data["tasks"] = [t.to_dict() for t in tasks]
    _write_ledger_raw(data)


def list_tasks() -> list[Task]:
    """전 task 레코드 (조회 전용·부작용 0). pm_update 활성 pid 스캔 등 단일 파일 스캔 소비(⑳)."""
    with _lease_lock():
        return _read_tasks()


def find_task(name: str) -> "Task | None":
    """이름으로 task 레코드를 찾는다 — 없으면 None."""
    with _lease_lock():
        for t in _read_tasks():
            if t.name == name:
                return t
    return None


def bind_task(name: str, *, pid: "int | None" = None,
              registered_repos: "list[str] | set[str] | None" = None) -> tuple[Task, str, "int | None"]:
    """task 를 신규/resume 바인딩한다 — 명 검증(must-fix) + 동시 세션 거부(㉑) + 서술 디렉토리 신설 (T-0353).

    반환 `(task, action, reclaimed_from_pid)` — action ∈ {"created", "resumed", "reclaimed"}:
      - **created**  — 장부에 없던 task 를 신규 생성(prefix=None·기본 없음·pid=내 pid·슬롯 0개 시작).
      - **resumed**  — 기존 task 를 내 pid(같은 세션·crash 전 나) 로 재개.
      - **reclaimed**— 기존 task 의 pid 가 죽어(또는 미점유) 회수 후 진입. `reclaimed_from_pid` =
        회수한 이전 pid(>0 이면 loud notice 대상·아래 ㉑ 경계 참조). created/resumed 는 None.

    **명 검증(must-fix)**: `_validate_task_name` 을 mkdir·장부 write **이전**에 돌려 traversal/절대경로/
    빈 이름/예약패턴(`registered_repos` 주면)을 fail-loud(`InvalidTaskName`) — 엔진층 배치라 T-0354~
    0357 의 직접 소비도 우회 못 한다.

    **㉑ 동시 세션 거부의 실효 경계 (정직화·must-fix②)**: 기록 pid 가 **살아있고 내 pid 와 다르면**
    `TaskActiveElsewhere`(부트스트랩이 dump 이전에 거부). 그러나 **기록 pid = 부트스트랩 헬퍼
    프로세스**라(dump 후 즉사) alive 거부의 실효 창은 **두 부트스트랩이 동시에 도는 순간뿐**이다 —
    이후 두 번째 창이 같은 task 를 열면 pid 가 죽어 `reclaimed` 로 통과한다. 이는 슬롯 lease pid 와
    **동일 semantics**(ADR-0013 이 heartbeat/타임아웃 회수를 조기회수 위험으로 기각·슬롯의 실 보호는
    session 명[phase0]이지만 task 는 두 창이 같은 이름이라 pid 가 유일 판별자)다. 크로스플랫폼
    프로세스 조상 추적은 과설계라 **비채택** — 대신 `reclaimed_from_pid>0` 이면 호출부(부트스트랩)가
    **loud notice**("다른 창이 아직 작업 중일 수 있다")를 surface 한다(감지=기계·해소=사용자·0단계
    미기록 질의와 동형). 차단 아님(crash 재개가 다수 케이스).

    `_pid_alive` 는 `reclaim_stale` 과 같은 생존 primitive(동형·신설 0). prefix 는 여기서 안 만진다
    — 생성 시 None·재개 시 기존 값 유지(변경 = `task prefix`·T-0357). `.local/tasks/<name>/` 는
    mkdir(exist_ok) — 기계 상태는 장부·서술만 여기(⑨).
    """
    _validate_task_name(name, registered_repos)   # mkdir·장부 write 이전 fail-loud(부작용 0).
    pid = os.getpid() if pid is None else pid
    with _lease_lock():
        tasks = _read_tasks()
        existing = next((t for t in tasks if t.name == name), None)
        if existing is None:
            task = Task(name=name, prefix=None, pid=pid, started=_now_utc())
            tasks.append(task)
            _write_tasks(tasks)
            task_dir(name).mkdir(parents=True, exist_ok=True)
            return task, "created", None
        # 동시 세션 거부 — 기록 pid 가 살아있고 내가 아니면(다른 창) 거부(㉑·위 경계 참조).
        if existing.pid and existing.pid != pid and _pid_alive(existing.pid):
            raise TaskActiveElsewhere(name, existing.pid)
        # dead(crash/미점유 회수) 또는 same pid(내 재개) → 진입. pid 를 내 것으로 갱신.
        if existing.pid == pid:
            action, reclaimed_from = "resumed", None
        else:
            action, reclaimed_from = "reclaimed", existing.pid  # 회수한 이전 pid(>0=loud notice)
        existing.pid = pid
        _write_tasks(tasks)
        task_dir(name).mkdir(parents=True, exist_ok=True)
        return existing, action, reclaimed_from


def set_task_prefix(name: str, prefix: "str | None") -> "Task | None":
    """task 레코드의 board prefix 를 지정/변경/해제한다 — 갱신된 `Task`·task 부재 시 `None` (T-0357·F5).

    `pm-config task prefix <이름> <p|none>` 의 write 백엔드다 — 장부 top-level `tasks` 레코드
    (T-0353)의 `prefix` 필드를 `prefix`(문자열=지정/변경·None=해제) 로 덮는다. **중간 변경 자유**
    (task 종속 없음·①ⓒ) — bind_task 는 prefix 를 안 만지고(생성=None·재개=유지) 변경은 여기 단일
    지점이다. `board.py new --task <이름>` 이 `identity_args.task_prefix` 로 이 값을 read 해 F5 3단
    해소(명시 `--prefix` > task 설정 > 기본 없음)에 쓴다.

    **장부 IO 는 이 모듈이 단일 소유**(직접 JSON read/write 금지·flock/스키마·ADR-0013) — `_lease_lock`
    아래 `_read_tasks`/`_write_tasks`(형제 `leases`·미지 top-level 키 무손실 round-trip)로 atomic 하게
    갱신한다. task 부재면 `None`(호출부가 rc1 안내) — 생성은 F1(bootstrap) 단일 지점이라 여기서
    task 를 만들지 않는다.

    **명 검증(must-fix)**: `_validate_task_name` 을 장부 write **이전**에 돌려 traversal/절대경로/빈
    이름을 fail-loud(`InvalidTaskName`) — write-capable 엔진 진입점이라 CLI 우회(직접 소비)도 닫는다.
    prefix *형식* sanity(`[a-z0-9_]`·`none` 예약·ADR-0042)는 CLI 입력측(pm_config)이 소비 grammar
    단일 진실(`board._validate_prefix`)로 선검증한다 — 여기선 저장만(board.py new 가 신뢰·T-0355)."""
    _validate_task_name(name)   # 장부 write 이전 fail-loud(부작용 0·must-fix).
    with _lease_lock():
        tasks = _read_tasks()
        target = next((t for t in tasks if t.name == name), None)
        if target is None:
            return None
        target.prefix = prefix
        _write_tasks(tasks)
        return target


# 종료된 task 서술 폴더의 아카이브 루트 — `.local/tasks/_ended/`. 선행 `_` 라 `_validate_task_name`
# 이 실 task 명으로는 거부하므로(⑥ path 컴포넌트 규칙) 아카이브 하위와 실 task 가 절대 충돌하지 않는다.
_ENDED_DIR_NAME = "_ended"


def slots_for_task(name: str) -> list[Lease]:
    """이 task(session==name) 명의로 **leased** 인 슬롯 리스트 (조회 전용·부작용 0·T-0354).

    슬롯↔task 연결 = `lease.session == task 이름` — alloc `--task <이름>` 이 session=이름 으로
    슬롯을 리스하므로(⑥ 직교 정체성이 lease 의 session 축에 실린다). release `--task` 소유검사와
    `end_task` 의 dirty 검사·일괄 반납 대상이 이걸 본다. idle/미점유 슬롯은 session 이 비어 제외.
    """
    with _lease_lock():
        return [l for l in _read_ledger() if l.state == "leased" and l.session == name]


class EndTaskResult:
    """`end_task` 결과 — 반납/이동 요약 또는 dirty 거부 (T-0354·F4).

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
    """종료 task 서술 폴더의 이동 목적지 `.local/tasks/_ended/<name>-<UTC날짜>/` (T-0354·②).

    같은 날 같은 이름 재종료 시 충돌하면 `-2`·`-3`… 로 유일화한다(덮어써 기록 유실 방지). 날짜는
    UTC `YYYYMMDD`(`_now_utc` 와 같은 시계·표시만 날짜 단위). 이동이라 삭제-위임 원칙 무저촉이고,
    이름 재사용 시 옛 pm_state 를 resume 처럼 오인하는 조용한 오염을 막는다(②).
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
    """task 를 종료한다 — 보유 슬롯 dirty 검사 → (clean 이면) 일괄 idle 반납 + 장부 제거 + 서술 폴더 아카이브 이동 (T-0354·F4·②).

    **claimed 티켓 소진 게이트(⑲)는 여기서 보지 않는다** — board 스캔은 board 소유(pm_config 가
    board 로드로 선-검사하고, 통과 시에만 이 함수를 부른다·import 격리 ADR-0013). 이 함수는
    worktree/장부/서술 폴더만 다룬다:

      1. `slots_for_task(name)` 보유 슬롯 중 **dirty** 가 하나라도 있으면 → 거부(`EndTaskResult.
         refused`·released/moved 없음·아무 부작용 0). 사용자 정리 후 재시도.
      2. 전부 clean → 보유 슬롯을 일괄 **idle 반납**(worktree 폴더는 유지·삭제 안 함·release 와
         동일 전이: session/pid 비우고 git 스냅 정리) → 장부의 task 레코드 제거 → 서술 폴더
         `.local/tasks/<name>/` 를 `_ended/<name>-<날짜>/` 로 **이동**(삭제 아님·②).

    **명 검증(must-fix ②·T-0353 클래스 재발 차단)**: `_validate_task_name` 을 장부 write·`shutil.move`
    **이전**에 돌려 traversal/절대경로/빈 이름을 fail-loud(`InvalidTaskName`) — `bind_task` 와 동형의
    엔진 진입점 방어다. 무검증이면 `end_task("../evil")` 이 `_archive_dest` 파생 후 `.local/tasks` 밖으로
    `shutil.move` 한다(reviewer/codex 재현·bind_task 만 걸리던 구멍). 예약패턴(`<repo>_<N>`·⑥)은 생성
    시점(bind_task/cmd_alloc)에서 걸리는 **생성 관심사**라 여기선 registered_repos 를 요구하지 않는다
    (종료엔 path-safety 만 필요·session 은 이미 장부에 있음). dirty 검사·반납·장부/task write 는 **한
    `_lease_lock` 안에서** 직렬화한다(release/reclaim 동형·부분상태 차단). 폴더 이동은 락 밖(fs op·장부
    무관). git_runner 주입으로 hermetic.
    """
    _validate_task_name(name)   # 장부 write·shutil.move 이전 fail-loud(부작용 0·must-fix ②)
    with _lease_lock():
        leases = _read_ledger()
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
            released.append(lease.slot)
        if released:
            _write_ledger(leases)

        # 2b) 장부의 task 레코드 제거(같은 락·형제 leases 는 위에서 이미 반영됨).
        tasks = [t for t in _read_tasks() if t.name != name]
        _write_tasks(tasks)

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
    git_runner: GitRunner | None = None,
) -> Lease:
    """repo 슬롯을 리스한다 (ADR-0013·sealed spike §8-6).

    - **idempotent** — 이 세션(session)이 이 repo 에 이미 leased 슬롯을 갖고 있으면 그걸
      반환한다(get-or-create-my-lease). branch 가 주어지고 슬롯의 live HEAD 와 다르면 같은
      슬롯에서 재체크아웃한다(리스 유지·슬롯=브랜치-무관 컨테이너·git=진실·ADR-0013 amend T-0072).
    - **branch/resume 우선 re-alloc** — resume(또는 branch)으로 *이전 작업스트림*의 슬롯을
      찾으면(슬롯 live HEAD 가 그 브랜치) 같은 슬롯을 re-alloc 한다(회전 연속성·dirty 파일 보존 재부착).
    - **idle 슬롯 리스** — 위에 안 걸리면 idle 슬롯을 leased 로 전이(필요 시 branch checkout).
    - **풀 소진 → `NeedsCreate`** — idle 슬롯이 없으면 raise(호출부 bootstrap 사용자 게이트).

    진입 시 `reclaim_stale` 을 먼저 호출해 pid 죽은 슬롯을 회수한다(풀 가용성 회복).
    `branch` 와 `resume` 은 동의어 역할(둘 다 작업스트림 식별) — 명시된 쪽을 쓴다.
    """
    sess = session or _default_session()
    target_branch = branch if branch is not None else resume

    # alloc 진입 시 stale 회수 (풀 가용성 회복·ADR-0013).
    reclaim_stale(git_runner=git_runner)

    with _lease_lock():
        leases = _read_ledger()

        # 1) idempotent — 이 세션의 기존 leased 슬롯 (같은 repo).
        for lease in leases:
            if lease.repo == repo and lease.state == "leased" and lease.session == sess:
                # 슬롯이 이미 target_branch 인가 = 슬롯 worktree 의 live HEAD 로 판정(ADR-0013
                # amend T-0072 — git=진실·저장 복사본 미사용). 아니면 재체크아웃(git 이 권위).
                if (target_branch is not None
                        and current_branch(lease.slot, git_runner=git_runner) != target_branch):
                    # checkout 실패면 raise — git=진실이므로 부분 실패 시 호출부에 위임(ADR-0013).
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                    # 브랜치 재배치가 일어난 경우만 arrival 스냅 갱신 + 장부 write(T-0350·base 보존).
                    # 순수 재진입(브랜치 동일)은 상태 변화가 없어 스냅/write 를 생략한다(재진입 비용 0).
                    _apply_git_snapshot(lease, git_runner=git_runner)
                    _write_ledger(leases)
                return lease

        # 2) resume/branch 우선 re-alloc — 같은 작업스트림(브랜치)의 슬롯 재부착(연속성).
        if target_branch is not None:
            for lease in leases:
                # provisional("creating")은 재부착 대상에서 제외한다 (T-0295·should-fix). worktree add
                # 성공 후~submodule init 전 SIGKILL 로 남은 creating orphan 은 worktree 가 이미 그
                # 브랜치를 체크아웃 중이라 live HEAD 로 매칭되는데, 이를 조용히 leased 로 재부착하면
                # (a) reconcile 의 incomplete surface 를 우회하고 (b) submodule 미초기화 슬롯을
                # leased 로 넘긴다. creating 은 reconcile/prune 경로로만 정리(설계 의도=surface+정리).
                if lease.state == "creating":
                    continue
                # 이 슬롯이 target_branch 를 체크아웃 중인가 = live HEAD 로 매칭(저장 필드 아님·
                # ADR-0013 amend T-0072). 드리프트 불가능 — git 이 단일 진실.
                if (lease.repo == repo
                        and current_branch(lease.slot, git_runner=git_runner) == target_branch):
                    # checkout 을 먼저 — 실패하면 raise 해 in-memory lease·장부 모두 미변경(기존 리스 보존).
                    _checkout_required(lease.slot, target_branch, git_runner=git_runner)
                    lease.state = "leased"
                    lease.session = sess
                    lease.pid = os.getpid()
                    lease.started = _now_utc()
                    _apply_git_snapshot(lease, git_runner=git_runner)  # arrival 스냅(기대 baseline·base 보존·T-0350).
                    _write_ledger(leases)
                    return lease

        # 3) idle 슬롯 리스 — **최소 번호 우선**(결정론 ⓒ·번호 안정·T-0354). 같은 repo 의 idle
        #    후보를 슬롯 번호 오름차순으로 정렬해 최소 가용 번호부터 대여한다(대여 중 불변·반납 후
        #    재사용). 옛 코드는 장부 파일 순서(대개 생성순이나 remove+재생성 후 뒤섞일 수 있음)로
        #    첫 idle 을 골라 번호가 비결정적이었다 — 정렬로 못박아 alloc CLI 의 "최소 번호 대여"를
        #    보장한다. 비-숫자 tail 슬롯(드문 커스텀)은 뒤로 밀어 숫자 슬롯 우선(`_slot_number`).
        idle_for_repo = sorted(
            (l for l in leases if l.repo == repo and l.state == "idle"),
            key=lambda l: _slot_number(l.slot),
        )
        for lease in idle_for_repo:
            # **위험차단 (T-0295·must-fix)**: worktree 물리 부재 슬롯은 리스하지 않는다. stale
            # 엔트리(worktree dir 삭제/prune)가 force_release 등으로 idle 이 되면, 이 재사용
            # 루프가 *없는 worktree* 를 leased 로 넘겨 이후 코드가 깨진다(codex 실측). fs 존재
            # 가드는 **실경로(git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는
            # 슬롯 존재를 모델링하는 권위라 건너뛴다(current_branch/slot_status 동형 규율). 부재
            # 슬롯은 skip → 다음 후보/NeedsCreate(dangling idle 을 leased 로 승격하지 않음).
            if git_runner is None and not slot_path(lease.slot).exists():
                continue
            # 슬롯이 이미 target_branch 가 아니면 재체크아웃(live HEAD 비교·ADR-0013 amend
            # T-0072). git 이 브랜치를 만든다 — 장부엔 branch 를 쓰지 않는다.
            if (target_branch is not None
                    and current_branch(lease.slot, git_runner=git_runner) != target_branch):
                # checkout 을 먼저 — 실패하면 raise(idle 슬롯 상태 보존·부분 leased 전이 차단).
                _checkout_required(lease.slot, target_branch, git_runner=git_runner)
            lease.state = "leased"
            lease.session = sess
            lease.pid = os.getpid()
            lease.started = _now_utc()
            _apply_git_snapshot(lease, git_runner=git_runner)  # arrival 스냅(기대 baseline·base 보존·T-0350).
            _write_ledger(leases)
            return lease

        # 4) 풀 소진 — idle 슬롯 없음. 새 슬롯 생성은 fs 행위라 사용자 게이트(호출부).
        raise NeedsCreate(repo)


def release(
    slot: str,
    *,
    require_clean: bool = True,
    owner_task: str | None = None,
    git_runner: GitRunner | None = None,
) -> Lease:
    """슬롯을 반납한다 — 작업완료 시(ADR-0013). idle 로 전이한 Lease 반환.

    - **dirty + require_clean=True → `ReleaseRefused`** — 수동 정리 요구(작업 유실 방지).
    - **require_clean=False(자동경로) → dirty 면 stash 보존 후 idle 화** — 자동화에서 막힘 방지.
    - **owner_task 주면 소유검사(T-0354·F3)** — 그 슬롯의 leased session 이 owner_task 와 다르면
      `NotTaskOwner`(다른 task 의 슬롯 반납 차단). 검사는 dirty 판정보다 먼저(내 것이 아니면
      dirty 여부를 볼 이유가 없다). `--task` 미지정(owner_task=None·slot-only 백스톱)은 안 탄다.

    슬롯은 idle 로 전이(재사용 컨테이너로 풀에 반납)하고 session/pid 를 비운다 —
    worktree 폴더 자체는 유지(다음 리스가 재사용·remove 는 force_release/수동).
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")

        # readonly 공유 슬롯은 반납(idle 화) 대상이 아니다 (⑬·T-0358·should-fix) — 무소유 공유 자산이라
        # release 하면 idle 이 돼 alloc 이 work 슬롯으로 점유(role 유실). 보유 중인 lease.role 을 직접
        # 검사한다(`_reject_readonly_mutation`/`_slot_role` 은 lock 재취득 → non-reentrant 데드락).
        if target.role == _LEASE_ROLE_READONLY:
            raise ReadonlySlotNotLeasable(slot, "release")

        # task 소유검사 (F3) — dirty/stash 어떤 부작용보다 먼저. 내 task 명의(session)가 아니면
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
        target.git = None    # release 시 정리 — idle 슬롯은 활성 git 기대가 없다(T-0350·다음 alloc 이 재스냅).
        _write_ledger(leases)
        return target


def force_release(slot: str, *, git_runner: GitRunner | None = None) -> Lease | None:
    """수동 백스톱 — dirty/leased 여부 무시하고 슬롯을 강제로 idle 화 (ADR-0013).

    dirty 면 stash 로 보존은 시도하되(작업 유실 최소화) 거부하지 않는다. 장부에 슬롯이
    없으면 None 반환(이미 정리됨·무해). `pm-config release --force` 백스톱의 엔진 진입점.
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        # readonly 공유 슬롯은 강제 반납도 대상이 아니다 (⑬·T-0358·should-fix·release 동형) — 무소유
        # 공유 자산이라 idle 화하면 alloc 이 work 슬롯으로 점유(role 유실). 보유 lease.role 직접 검사.
        if target.role == _LEASE_ROLE_READONLY:
            raise ReadonlySlotNotLeasable(slot, "force-release")
        path = slot_path(slot)
        if path.exists() and _is_dirty(path, git_runner=git_runner):
            _stash(path, git_runner=git_runner)  # 강제라도 작업은 보존 시도.
        target.state = "idle"
        target.session = ""
        target.pid = 0
        target.git = None    # release 시 정리(force 백스톱도 동일·T-0350·다음 alloc 이 재스냅).
        _write_ledger(leases)
        return target


def remove_slot(
    slot: str,
    *,
    force: bool = False,
    git_runner: GitRunner | None = None,
) -> "RemoveResult | None":
    """슬롯을 *통째로* 제거한다 — worktree remove + 전용 브랜치 정리 + 장부 엔트리 삭제 (원자·user-invoked·T-0333).

    `create_slot` 의 역연산 — 슬롯 lifecycle 에서 *제거 본체*가 수동 `git worktree remove`
    위임이던 gap 을 닫는다(PM 69 footgun 체인: 수동 remove → dangling 장부 → `add` 가 번호
    skip → 뒤늦은 prune). 이 한 커맨드가 리스 확인 → dirty 검사 → `git worktree remove`
    (+ `git worktree prune`) → 슬롯 전용 브랜치 정리 → 장부 엔트리 제거를 원자로 한다.

    **삭제-위임 원칙(사용자 명시 호출 전제)** — PM 이 자율 실행하지 않는다(호출부 CLI 가
    사용자 명시 `pm-config worktree remove <slot>`). `prune-stale`(worktree 부재 장부만 정리)과
    달리 실 worktree 를 지운다. orphan worktree(장부 미등록)는 여전히 `git worktree remove`.

    원자 시퀀스:
      ① 리스/장부 확인 — 엔트리 없으면 `None` 반환(무해 종료·이미 정리됨·orphan 은 위 참조).
      ② 활성 리스 확인 — `state != "idle"`(leased/creating·사용 중)이면 `RemoveRefused`
         ("active-lease")·`force=True` 로만 무시(정석은 `release` 먼저·override 시 원래 state 를
         `RemoveResult.forced_state` 에 실어 CLI 가 강제-회수 경고). **dirty 검사** — dirty 면
         `RemoveRefused`("dirty")·`force=True` 면 stash 보존 후 강제. **stash 실패(rc≠0)** 또는
         **stash 후에도 여전히 dirty**(submodule 내부 변경 등 top-level stash 가 못 담는 잔존)면
         제거를 중단(`RuntimeError`·장부/worktree/브랜치 미변경) — 어느 경우도 `worktree remove
         --force` 로 날리는 작업 유실을 막는다(codex must-fix·R2 class-fix 일반 불변식).
      ③ `git worktree remove [--force]`(+ `git worktree prune`) — `.repos/<repo>.git` bare
         컨텍스트(공유 .git 원·ADR-0011 §31). 실패(rc≠0)면 `RuntimeError`(장부/브랜치 미변경·단
         force+dirty 는 stash 가 이미 생성돼 있을 수 있음 — 작업은 보존됨).
      ④ 슬롯 전용 브랜치(`<repo>_<N>`) 정리 — 슬롯이 그 전용 브랜치를 체크아웃 중이면
         `git branch -d`(머지 완료 시에만 삭제·미머지면 rc≠0 로 거부=보존·작업 유실 방지).
         공유/다른 브랜치(main 등)면 삭제 자체를 스킵. detached 면 판별 불가(none).
      ⑤ 장부 엔트리 제거 — `add` 가 빈 번호를 **재사용**(번호 skip footgun 종결·이 티켓 핵심).

    ⚠️ **미머지-보존 브랜치 상호작용** (T-0333 캐비앗 → T-0335 진단 정정): ④ 가 미머지라 전용
    브랜치 `<repo>_<N>` 를 보존하면, 나중에 같은 번호의 슬롯을 branch-무지정 경로(base-경로 `-b
    <repo>_<N>` 또는 else-경로 path-basename 자동 `-b`)로 재생성할 수 없다(그 브랜치가 이미 존재).
    create_slot 은 이제 이를 **선-검출해 fail-loud**(`SlotBranchExists`·오귀인 orphan-worktree 진단
    제거·T-0335) — 정확한 원인(브랜치 잔존) + 두 갈래(그 브랜치 정리[머지/삭제] 후 새 슬롯 재생성 /
    그 브랜치를 checkout 해 재개)를 안내한다. 재개는 수동 `git worktree add <path> <repo>_<N>` 또는
    `create_slot(branch=<repo>_<N>)` — **둘 다 리셋 없이 보존 커밋 tip 에서 checkout**(T-0343 이
    create_slot branch-경로의 `-B` create-or-reset 데이터-유실을 존재 브랜치 checkout 분기로 닫음).
    데이터 유실 없음(현재도 loud 실패였다).

    **fs 존재 가드는 실경로(git_runner 미주입)에서만** 본다 — 주입 runner(hermetic 테스트)는
    슬롯 상태를 모델링하는 권위라 건너뛴다(`alloc` idle-reuse 동형). 슬롯 전용 브랜치명은
    슬롯 식별자 tail(`<repo>_<N>`)로, `git worktree add` 가 슬롯 경로 basename 으로 자동 판
    브랜치와 정합한다(create_slot 결정). `cmd_release`/`cmd_worktree_prune_stale` 패턴대로
    `git_runner` 주입으로 실 git 없이 배선 검증(DI seam).
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        # ① 리스/장부 확인 — 엔트리 없으면 무해 종료(orphan 은 git worktree remove·prune-stale).
        if target is None:
            return None

        # ② 활성 리스 확인 — leased/creating(사용 중·in-flight)은 force 로만(release 먼저가 정석).
        # force override 면 원래 활성 state 를 실어 CLI 가 강제-회수 경고를 낸다(reviewer should-fix).
        forced_state = target.state if target.state != "idle" else None
        if forced_state is not None and not force:
            raise RemoveRefused(slot, "active-lease", state=forced_state)

        path = slot_path(slot)
        # fs 존재 가드는 실경로(git_runner 미주입)에서만 — 주입 runner 는 슬롯 상태를 모델링
        # (alloc idle-reuse 동형). 실경로 worktree 부재(이미 사라짐)면 remove 를 건너뛰고 장부만 정리.
        real_path_missing = git_runner is None and not path.exists()

        # ② dirty 검사 — dirty 면 거부(작업 유실 방지)·force 면 stash 보존 후 강제.
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
            # **stash 성공 후 재검사** (codex R2 must-fix·class-fix 일반 불변식): top-level
            # `git stash push --include-untracked` 는 **submodule 내부 변경을 담지 못한다**. worktree
            # 풀 슬롯은 submodule init 을 하므로(ADR-0013), stash rc0 라도 dirty submodule 작업이
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

        # ③ git worktree remove (+ prune) — bare 컨텍스트(공유 .git 원·ADR-0011 §31·_rollback_worktree
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

        # ④ 슬롯 전용 브랜치 정리 — 전용 브랜치(`<repo>_<N>`)만·머지 완료 시에만 삭제.
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

        # ⑤ 장부 엔트리 제거 — `add` 가 빈 번호를 재사용(번호 skip footgun 종결·T-0333 핵심).
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
    init_submodules: bool = True,
    git_runner: GitRunner | None = None,
    test_cmd: str | None = None,
    readonly: bool = False,
) -> Lease:
    """새 슬롯을 *생성*하고 leased 로 리스한다 — 풀 확장 (NeedsCreate 게이트 통과 후·ADR-0013).

    `test_cmd` 가 주어지면 그 슬롯 리스에 회귀/빌드명령을 바인딩한다(T-0066·ADR-0014
    amend) — 같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 등)을 가질 수 있게.
    board._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다(미지정=None·현행).

    `git worktree add` 는 fs 행위라 사용자 게이트(NeedsCreate) 통과 후에만 불린다 —
    pm-config worktree add / bootstrap 사용자 승인이 호출부. 다음을 한다:
      1. **bare 부재 가드** — `.repos/<repo>.git` 가 없으면 `BareRepoMissing` raise(multi-PM
         worktree 침묵 폴백 금지·ADR-0011 §31·ADR-0013 fail-soft 규율).
      2. 다음 슬롯 번호 결정(`<repo>_<N>`·기존 번호 회피).
      3. `git worktree add [-B <branch>] [-b <slot> <path> <base> | <path>]` —
         **`.repos/<repo>.git` bare 컨텍스트**에서 실행해 슬롯이 그 family repo 의 worktree 가
         되게 한다(ADR-0011 §31). 분기:
           - `branch` 면 그 브랜치를 create-or-reset 으로 체크아웃(`-B <branch> <path>`).
             branch 가 신규든 기존이든 한 호출로 처리(`add <path> <ref>` 는 ref 가 *기존*이어야
             해 신규 작업스트림 브랜치엔 못 씀 → `-B` 로 통일).
           - `base` 면(branch 미지정) 먼저 `git fetch origin`(best-effort·T-0274) 후 슬롯 브랜치
             `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*(`--no-track -b <repo>_<N> <path>
             origin/<base>`). repo 등록 base(areas.md·`pm-config worktree add` 가 전달)에서 일관되게
             따게 한다 — bare HEAD 가 아닌 의도한 base(develop 등). fetch 실패/origin ref 미해소면
             로컬 `<base>`(동결 head) 폴백(T-0152 refspec 은 origin/* 만 갱신·로컬 heads 는 동결·
             fail-soft). `--no-track` = 슬롯 브랜치에 origin/<base> upstream 자동설정 억제(슬롯=작업스트림).
           - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
      4. submodule init — `git worktree add` 는 submodule 자동 init 안 함(ADR-0013·spike
         §8-4(d)) → `git submodule update --init --recursive --force`(슬롯 worktree cwd).
         `--force` 는 worktree+submodule edge(bare 에서 만든 fresh 슬롯)서 plain `--init` 이
         체크아웃 못 하는 상태를 강제 init — fresh 슬롯이라 잃을 로컬 변경 0(T-0067).
      5. 장부에 leased 엔트리 등록.

    **`readonly=True` (⑬·T-0358·spike §F11)**: research 전용 read-only 공유 슬롯을 만든다 —
    슬롯 전용 브랜치를 파지 않고 `git worktree add --detach <path> <base sha>` 로 **detached
    HEAD** 로 만든다(§F11 실측 — `git worktree add <path> main` 은 같은 브랜치를 두 worktree 가
    점유 못 해 `fatal: 'main' is already used by worktree at …` 로 죽는다 → `--detach` 필수).
    `base` = released `main` 의 기준면(문서 검증 기준·released 지점). lease 는 `role="readonly"`·
    **session/pid 없음**(공유 자산·배타 대여 안 함)·alloc/release/reclaim 대상 제외(공유가 정상).
    base 는 스냅(`lease.git.base`)에 기록해 문서 검증 기준면을 남긴다(B축 verified_at 선행 자산).

    git_runner 가 주입되면 그 runner 로 모든 git 호출(테스트 hermetic). 미주입이면
    `.repos/<repo>.git` bare 컨텍스트의 실 git 으로 worktree add 후, 슬롯 경로 컨텍스트로
    submodule init.
    """
    sess = session or _default_session()

    # bare 부재/무효 가드 — worktree 의 공유 .git 원이 없거나 무효면 base 가 없다(ADR-0011 §31).
    # 침묵 폴백(multi-PM 루트 worktree)으로 가면 슬롯이 family repo 가 아닌 multi-PM 루트를 체크아웃해
    # 토폴로지가 깨진다 → 명시 raise 로 `pm-config repo add` 선행 안내(ADR-0013 fail-soft 규율).
    #   1) 경로부재 → BareRepoMissing (종전·hydrate 안내).
    #   2) 경로존재 but 무효(부분/깨진 bare·T-0294) → BareRepoMissing(broken=True). 중단된 clone 이
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
        leases = _read_ledger()
        # 슬롯번호 = **ledger ∪ 실 git worktree** 병합 (T-0295·audit #4). ledger 만 보면 orphan
        # (worktree add 성공 후 lease 기록 전 죽어 disk 엔 있으나 ledger 엔 없는 슬롯·audit #2)
        # 번호를 재사용해 `git worktree add` "already exists" 암호 에러가 난다. git worktree 실측을
        # 합쳐 orphan 번호까지 회피한다(주입 runner 존중·DI seam). git 조회 실패는 fail-soft(빈 집합).
        used = _existing_slot_numbers(repo, leases) | _git_slot_numbers(repo, git_runner=git_runner)
        n = 1
        while n in used:
            n += 1
        slot = _slot_for(repo, n)
        path = slot_path(slot)

        # **provisional lease 선기록 (T-0295·중단-안전)** — worktree add *전에* `state="creating"`
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
        # 실패면 지울 worktree 가 없어 remove 를 안 부른다(기존 T-0070 롤백 범위 유지).
        worktree_created = False
        try:
            # 슬롯 전용 브랜치 `<repo>_<N>` 선-검출 (T-0335·결정 (b)) — branch 미지정(base·else) 경로는
            # 그 브랜치를 판다(base=명시 `-b <repo>_<N>`·else=git 이 슬롯 path basename 으로 자동 `-b`).
            # 미머지-보존 브랜치(remove_slot ④ 가 작업 유실 방지로 보존)가 잔존하면 `worktree add` 가
            # `fatal: a branch named '<repo>_<N>' already exists`(rc≠0)로 죽는다 — 슬롯번호 병합
            # (ledger∪git-worktree·T-0295)은 **worktree 없이 잔존하는 브랜치**를 못 본다(브랜치 축은
            # 슬롯번호 축과 독립). 여기서 선-검출해 정확한 원인+두 갈래를 fail-loud 로 준다(오귀인
            # orphan-worktree 진단 제거). 명시 `branch=` 경로는 이 선-검출을 안 타고 아래서 존재→checkout/
            # 부재→`-B` 로 분기한다(T-0343·기존 브랜치 리셋-유실 방지·명시 의도). try 안에서 raise →
            # 아래 `except` 가 provisional lease 를 롤백한다(worktree_created=False 라 롤백할 worktree 는
            # 없음·중단-안전). 검출은 `_slot_branch_exists`(color-safe `--format=%(refname:short)`·
            # splitlines 정확-일치·rc 무시 — 평문 `branch --list` 는 `color.branch=always` 서 ANSI 오염·
            # rc 기반은 주입 runner generic 폴백 rc0 오탐·T-0335/T-0343).
            if branch is None and not readonly:
                slot_branch = f"{repo}_{n}"
                pre_runner = git_runner or _real_git_runner(bare)
                if _slot_branch_exists(pre_runner, slot_branch):
                    raise SlotBranchExists(slot, slot_branch, base)
            # worktree add 는 `.repos/<repo>.git` bare 컨텍스트에서 — 슬롯이 그 family repo 의
            # worktree 가 되게 한다(ADR-0011 §31). bare repo 도 `git -C <bare> worktree add <abs
            # path>` 가 동작한다(슬롯 path 는 절대).
            #   - branch 면 존재 여부로 분기 (T-0343 근본 fix): **기존 브랜치는 checkout**
            #     (`add <path> <branch>`·그 tip 에서·**리셋 없음**)·**신규 브랜치는 `-B`**(생성).
            #     옛 코드는 신규/기존 모두 `-B`(create-or-**reset**)로 통일했는데, `-B <branch>` 는 기존
            #     브랜치를 start-point(미지정 시 bare HEAD)로 **리셋**해 미머지-보존 브랜치를 `branch=` 로
            #     넘기면 보존 커밋 ref 를 잃는 **데이터-유실 클래스**였다(T-0335 codex 포착). 명시 `branch=`
            #     는 "이 브랜치를 슬롯에" 라는 명시 의도라, 기존 브랜치를 그 tip 에서 그대로 checkout 한다
            #     (base-경로의 silent 재사용=기각한 (a) 와 달리 명시 의도라 놀람 없음). 신규 브랜치는
            #     `add <path> <newbranch>` 가 "invalid reference" 로 죽으므로 `-B`(=생성·리셋 대상 없어
            #     안전)로 판다. 이 fix 로 `SlotBranchExists` 안내의 "그 브랜치로 재개"(create_slot(branch=)
            #     또는 수동 `git worktree add <path> <repo>_<N>`)가 **둘 다 리셋 없는 안전 경로**가 된다.
            #   - base 면(branch 미지정·T-0075) 먼저 `fetch origin`(T-0274) 후 슬롯 브랜치
            #     `<repo>_<N>` 를 *`origin/<base>` 최신에서 파생*(`--no-track -b <slot> <path>
            #     origin/<base>`). 슬롯 브랜치 이름은 슬롯 식별자(`<repo>_<N>`·T-0072 live-branch 정합)
            #     이고 base 만 의도한 분기점(develop 등). `add <path> <ref>` 가 아니라 `-b`(브랜치 생성)인
            #     이유: ref 만 주면 detached 거나 base 브랜치 자체에 붙어 슬롯 작업이 base 를 오염한다 →
            #     슬롯 전용 브랜치를 base 에서 새로 판다. `--no-track` = origin/<base> upstream 자동설정
            #     억제(슬롯=작업스트림). (파생 기준·fetch·--no-track 상세는 아래 base 분기 주석.)
            #   - 둘 다 미지정이면 **현행 보존**(`add <path>` = bare HEAD·회귀 0).
            if readonly:
                # readonly 공유 슬롯(⑬·T-0358·§F11) — 슬롯 전용 브랜치를 파지 않고 **detached HEAD**
                # 로 만든다. 같은 브랜치(main 등)를 두 worktree 가 점유 못 하는 git 제약(§F11 실측)을
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
                            f"  로컬 `{base}`(동결 head)에서 readonly 슬롯을 detach 한다 — 네트워크 복구 후 "
                            "`/pm-worktree refresh` 로 최신 released tip 으로 갱신하라 (T-0358·fail-soft).",
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
                # 기존 브랜치 → checkout(리셋 없음·보존 커밋 유지)·신규 → -B(생성) — T-0343 근본 fix.
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
                # base 파생 — 슬롯을 *origin 최신*에서 시작한다 (T-0274). T-0152 refspec
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
                # 러너로 실행한다(T-0292). 주입된 git_runner(mock)면 그대로(DI seam).
                prep_runner = git_runner or _real_git_runner(bare)
                ref = base
                rc, out = prep_runner(["fetch", "origin"])
                if rc != 0:
                    print(
                        f"[경고] `git -C {bare} fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
                        f"  로컬 `{base}`(동결 head)에서 슬롯을 판다 — 네트워크 복구 후 새 슬롯은 "
                        "origin 최신에서 시작한다 (T-0274·fail-soft).",
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

            # worktree add *자체* 는 **console-visible(인터랙티브) 러너**로 실행한다 (T-0292). 대형 repo 의
            # full checkout(로컬 bare→worktree·느린 디스크/VPN/Windows)이 옛 captured 120s 에 false-kill
            # 되던 블로커 해소 — 진행상황이 콘솔에 실시간 보이고 timeout 이 GIT_TIMEOUT_SECONDS(1800s·env
            # `PM_GIT_TIMEOUT`·`none`=무제한)라 관대하다. submodule 단계와 동일 패턴(_real_git_runner_
            # interactive). **DI seam 보존**: 주입된 git_runner(테스트 mock)가 있으면 그걸 쓴다(현행 테스트
            # 무영향) — 인터랙티브는 `git_runner is None` 실경로만. 인터랙티브는 `(rc, "")` 반환(출력은 콘솔로
            # 직접)이라 실패 메시지는 rc 기반 + 아래 트립 안내로 조정한다(git stderr 는 이미 콘솔에 감).
            add_runner = git_runner or _real_git_runner_interactive(bare, timeout=GIT_TIMEOUT_SECONDS)
            rc, out = add_runner(add_argv)
            if rc != 0:
                # 원인 힌트 (#4-bare·T-0294) — 식별 가능한 원인을 raw git out 앞에 붙인다. 상단 가드가
                # broken bare 를 이미 걸러내지만, upfront rev-parse 는 통과했는데 objects 결손 등으로
                # worktree add 만 죽는 잔여 부분-bare(또는 op 도중 손상)를 여기서 재판정해 안내한다.
                bare_hint = ""
                if not _is_valid_bare(bare, runner=git_runner or _real_git_runner(bare)):
                    bare_hint = (
                        f"`.repos/{repo}.git` 가 유효 bare 가 아니다(부분/깨진 bare 가능성) — 수동 삭제 후 "
                        f"`pm-config repo add {repo}` 로 재hydrate 하라 (T-0294). "
                    )
                # orphan/already-exists 진단 (#4-충돌·T-0295) — 슬롯번호를 git 병합으로 회피하지만,
                # 병합 후 나타난 orphan·수동 add 잔존이 `add` 를 "already exists" 로 죽일 수 있다.
                # out 문자열(captured/injected) 또는 실 git worktree 목록(인터랙티브는 out 이 콘솔 직행이라
                # 실측)에 이 슬롯 경로가 이미 등록됐으면 orphan 정리 경로를 안내한다.
                #
                # ⚠️ **오귀인 정정 (T-0335)**: `git worktree add` 의 "already exists" 는 두 원인이 있다 —
                # (1) worktree **경로** 잔존(orphan·이 블록의 대상), (2) 슬롯 전용 **브랜치**(`<repo>_<N>`)
                # 잔존(`a branch named '<repo>_<N>' already exists`·미머지-보존 브랜치). 옛 코드는 "already
                # exists" 부분매치만 보아 (2)를 (1)로 **오귀인**해 orphan 정리 안내를 냈지만 지울 orphan
                # worktree 는 없었다(T-0333 reviewer 실측). base·else 경로는 위 선-검출(SlotBranchExists)이
                # (2)를 먼저 잡지만, 여기서도 브랜치-존재 에러를 orphan 으로 낚지 않게 원인을 분리 판정한다
                # (클래스-fix·잔여/미래 경로 방어).
                out_l = str(out).lower()
                branch_exists_err = "a branch named" in out_l and "already exists" in out_l
                branch_hint = ""
                orphan_hint = ""
                if branch_exists_err:
                    branch_hint = (
                        f"슬롯 전용 브랜치 `{repo}_{n}` 가 이미 존재한다(미머지-보존 브랜치 잔존 가능성·"
                        f"remove_slot ④·T-0335) — 그 브랜치를 정리(`git branch -d/-D {repo}_{n}`·머지) 후 "
                        f"새 슬롯 재생성하거나, 미머지 작업을 이어가려면 그 브랜치를 checkout 해 재개하라 "
                        f"(수동 `git worktree add {str(path)} {repo}_{n}` 또는 `create_slot(branch={repo}_{n})`·"
                        f"둘 다 리셋 없음·T-0343). "
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
                            f"잔존 가능성·T-0295) — `pm-config status` 로 orphan/stale 을 확인하고 정리"
                            f"(worktree prune 또는 수동 삭제)하라. "
                        )
                # 트립/실패 안내 (T-0292) — 하네스 자동 호출이 timeout/실패로 죽었을 때 사용자에게 다음
                # 행동(터미널 직접 실행·무제한 opt-in)을 준다. rc 기반(out 은 인터랙티브면 빈 문자열).
                timeout_desc = "무제한" if GIT_TIMEOUT_SECONDS is None else f"{GIT_TIMEOUT_SECONDS}초"
                raise RuntimeError(
                    f"git worktree add failed for {slot!r} (rc={rc}, out={out!r}). {bare_hint}{branch_hint}{orphan_hint}"
                    f"매우 느린 op(대형 repo·느린 디스크/VPN)이면 timeout({timeout_desc}·PM_GIT_TIMEOUT)에 "
                    f"걸렸을 수 있다 — 터미널에서 `pm-config worktree add {repo}` 를 직접 실행하면 진행상황이 "
                    f"보이고, `PM_GIT_TIMEOUT=none` 으로 무제한 실행할 수 있다 (T-0292)."
                )
            # add 성공 — fs 에 worktree 존재. 이후 단계(submodule)/interrupt 실패 시 except 가 이 슬롯을
            # 롤백(remove)한다. add *자체* 실패면 worktree_created=False 라 remove 를 안 부른다(범위 유지).
            worktree_created = True

            # submodule init — worktree add 는 submodule 자동 init 안 함(ADR-0013·spike §8-4(d)).
            # `--force`: bare 에서 만든 fresh 슬롯의 worktree+submodule edge 에서 plain `--init` 이
            # 체크아웃 못 하는 상태(`git submodule init failed: ''` — 실 Windows multi-PM 파일럿서 빈
            # 에러로 죽음)를 강제 init 한다(T-0067). create_slot 은 *새 슬롯 생성 때만* 호출되고
            # (기존 슬롯 재사용은 alloc·재init 안 함) fresh worktree 라 잃을 로컬 변경이 없으므로
            # `--force` 안전. 솔로/submodule 없는 repo 는 `--init --recursive --force` 가 no-op rc 0.
            #
            # **인터랙티브 러너 (T-0070)**: 실경로(git_runner 미주입)에선 capture 러너 대신
            # `_real_git_runner_interactive`(stdio 콘솔 상속·SUBMODULE_TIMEOUT 3600s)로 돈다 —
            # 대형 submodule clone 이 600s 초과해 TimeoutExpired→(1,"")로 죽던 블로커 해소(진행
            # 상황 화면 표시·credential 프롬프트·대형 clone 완주). worktree add 도 같은 이유로
            # console-visible(T-0292·GIT_TIMEOUT_SECONDS)이고, 짧은 captured git(status·checkout·
            # dirty·stash)만 capture 러너 그대로. **DI seam 보존**: 주입된 git_runner(테스트 mock)가
            # 있으면 그걸 쓴다(현행 테스트 무영향) — 인터랙티브는 `git_runner is None` 실경로만.
            #
            # **원자적 롤백 (T-0070·T-0295)**: 실패(rc≠0)면 raise 만 하고, 롤백(worktree remove)과
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

            # 성공 — provisional 을 leased 로 **확정**한다(2차 write·branch 는 장부 미저장·ADR-0013
            # amend T-0072·git=진실·조회는 live current_branch). provisional 을 그대로 반환(같은 필드).
            provisional.state = "leased"
            if readonly:
                # readonly 공유 슬롯(⑬·T-0358) — 무소유 확정: session/pid 를 비우고 role 을 박는다.
                # state 는 "leased"(alloc idle-탐색·release 소유탐색이 session 매칭이라 무소유는 자연
                # 제외·reclaim_stale 은 role 가드로 제외). role 이 0단계 carve-out·mutation 거부의 축.
                provisional.session = ""
                provisional.pid = 0
                provisional.role = "readonly"
            # create git 스냅(T-0350·ADR-0060) — base 를 아는 유일 지점이다(base_branch=base·
            # commit=방금 파생된 fresh 슬롯 tip). base 미지정(branch·else 경로)이면 base 는 미기록
            # (drift 감지는 사용자 set-base 후 활성·결정 ⑪). fail-soft — 스냅 실패가 create 를 안 막는다.
            _apply_git_snapshot(provisional, base_branch=base, git_runner=git_runner)
            _write_ledger(leases)
            return provisional
        except BaseException:
            # **중단-안전 청소 (T-0295)** — 실패(rc≠0 raise)·예외·KeyboardInterrupt 는 여기서 단일
            # 경로로 정리한다: (1) worktree add 가 성공했으면(worktree_created) 그 worktree 를
            # `_rollback_worktree`(remove --force·best-effort) 로 지운다(add 자체 실패면 지울 게 없어
            # skip — T-0070 롤백 범위 유지). (2) provisional("creating") 엔트리를 장부에서 제거하고
            # 다시 쓴다(불완전 슬롯 미등록·기존 계약). 롤백은 2차 예외를 삼켜 원래 에러를 가리지 않는다.
            # (SIGKILL 은 여기 못 옴 — provisional 이 disk 에 남아 reconcile 이 incomplete 로 잡는다.)
            if worktree_created:
                _rollback_worktree(repo, path, git_runner=git_runner)
            leases[:] = [l for l in leases if l.slot != slot]
            _write_ledger(leases)
            raise


def bind_slot(slot: str, repo: str, session: str, *, git_runner: GitRunner | None = None) -> Lease:
    """슬롯을 세션에 **직접 바인딩**한다 — 사람 발의 멀티-PM 정체성 선언(T-0074·lean).

    `/pm-bootstrap <repo> --slot <N>` 의 엔진 진입점. 사람이 "내가 슬롯 <N>"을 선언하면
    그 슬롯 리스를 이 세션으로 갱신(있으면) 또는 생성(없으면)한다 — **pool alloc 이 아니다**
    (풀에서 골라잡지 않는다·slot-pinned/supervise 불필요). `alloc` 의 idle-탐색/풀-소진
    `NeedsCreate`/checkout 분기 어느 것도 안 탄다(직접 바인딩).

    **`reclaim_stale` 를 절대 호출하지 않는다** — 사람 경로는 pid-회수를 하지 않는다(R4 근원
    제거·[[ADR-0013]] Amendment(T-0074)). `alloc` 은 진입 시 `reclaim_stale` 로 풀 가용성을
    회복하지만, `bind_slot` 은 슬롯을 직접 지정받으므로 회수가 필요 없다 — pid 는 정보용으로만
    기록(`os.getpid()`)하고 liveness 판정에 쓰지 않는다(명시 `release` 로만 반납).

    ⚠️ **cross-path 한계(reviewer 게이트·[[ADR-0013]] Amendment(T-0074))**: 여기 적는 pid 는
    *ephemeral bootstrap 프로세스* pid 라 bootstrap 종료 후 죽는다. **사람 경로는 회수를 안 하지만
    (위), 자동 relay 경로의 `alloc` 은 진입 시 `reclaim_stale` 를 부른다 — 같은 장부를
    공유하므로, relay 가 가동 중이면 이 bind 엔트리를 `state==leased && pid 죽음` 으로 보고
    idle 화(session 비움)할 수 있다.** 즉 사람 bind 와 자동 alloc 이 *동시 가동*하면 사람 정체성이
    회수될 수 있다("무영향" 아님). 현 사람-only 파일럿엔 relay 미가동이라 **dormant**.
    relay+사람 공존을 실제로 쓸 때 사람 bind 보호(reclaim 제외 마커 등)가 후속 필요하다.

    **branch *표시* 는 건드리지 않는다** — 브랜치는 git=단일 진실이라 슬롯 worktree HEAD 에서
    live 조회(`current_branch(slot)`·ADR-0013 amend T-0072). bind 는 리스 장부의 점유 메타
    (session/state/started/pid)를 갱신하고, **arrival git 스냅**(`lease.git` 기대 baseline·
    branch/head/submodules·기존 `base` 보존)을 additive 로 기록한다(T-0350·ADR-0060). `git_runner`
    는 그 스냅의 DI seam — 미주입 실경로에서 슬롯 worktree 가 없으면 스냅은 fail-soft no-op(기존
    git 유지). 표시(live)와 기대(기록)는 2축이라 branch 표시 단일 진실은 그대로다.

    `_lease_lock` + `_write_ledger`(atomic) — 기존 alloc/release/set_test_cmd 와 동일한
    read-modify-write 직렬화. board.py 를 import 하지 않는다(ADR-0013 isolation·touches 격리).
    갱신/생성된 Lease 를 반환한다.
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        # readonly 공유 슬롯은 바인딩(점유) 대상이 아니다 (⑬·T-0358·should-fix) — bind 는 *점유*고
        # 0단계 carve-out(F6)은 *조회 지칭*만 허용한다(의미 불일치). readonly 를 무조건 leased 로 덮으면
        # role 이 유실되고 무소유 공유 자산이 배타 점유된다. `/pm-bootstrap --slot N` 오지정 방어(엔진
        # 불변식). 보유 lease.role 직접 검사(lock 재취득 데드락 회피). target None(신규)은 work 로 생성.
        if target is not None and target.role == _LEASE_ROLE_READONLY:
            raise ReadonlySlotNotLeasable(slot, "bind")
        if target is None:
            # 없으면 새 Lease append (직접 바인딩 — 풀 탐색/생성 게이트 없음).
            target = Lease(
                slot=slot,
                repo=repo,
                session=session,
                pid=os.getpid(),
                started=_now_utc(),
                state="leased",
            )
            leases.append(target)
        else:
            # 있으면 점유 메타만 갱신 (branch·test_cmd 는 보존). reclaim 안 거침.
            target.repo = repo
            target.session = session
            target.state = "leased"
            target.pid = os.getpid()
            target.started = _now_utc()
        # arrival git 스냅(기대 baseline·기존 base 보존·T-0350·fail-soft — 슬롯 부재면 no-op).
        _apply_git_snapshot(target, git_runner=git_runner)
        _write_ledger(leases)
        return target


def list_leases() -> list[Lease]:
    """현재 리스 장부 전체를 읽어 반환한다 (조회·진단용·pm-config status)."""
    with _lease_lock():
        return _read_ledger()


# ── git worktree × 장부 정합 (reconcile·중단-안전 슬롯번호·T-0295) ────────────────
# 장부(Lease)는 우리 메타(session/pid/test_cmd)이고, 실제 worktree 는 git 이 소유한다
# (`.repos/<repo>.git` bare 의 worktree 목록·ADR-0011 §31). create_slot 이 worktree add 성공
# 후 lease 기록 전에 죽으면 둘이 어긋난다(orphan·audit #2). git worktree 목록을 *실-git 소스*
# 로 삼아 (a) 슬롯번호를 orphan 까지 회피(#4)하고 (b) status 가 drift 를 surface(#3)한다.


def _slot_from_worktree_path(path_str: str) -> "str | None":
    """worktree 절대경로 → 슬롯 식별자 `work/<repo>_<N>` (WORK_DIR 하위 단일 컴포넌트만·T-0295).

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
    """`git worktree list --porcelain` 출력을 GitWorktree 리스트로 파싱한다 (T-0295).

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
    """`.repos/*.git` 에서 (repo, bare 경로) 목록 — reconcile 의 전-repo 열거원 (ADR-0011 §31·T-0295).

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
    """`git worktree list --porcelain` 로 실 git worktree 를 열거한다 — 장부 대조 소스 (T-0295).

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
    """실 git worktree 목록에서 이 repo 의 슬롯 번호 집합 (orphan 포함·슬롯번호 충돌 회피·T-0295).

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
    """리스 장부 × `list_git_worktrees` 대조로 drift 를 판정한다 — 조회 전용·부작용 0 (T-0295).

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
    """worktree 가 **확정 부재**인 dangling 장부 엔트리를 제거한다 — user-invoked 안전 cleanup (T-0295).

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
        leases = _read_ledger()
        kept = [l for l in leases if slot_path(l.slot).exists()]
        pruned = [l.slot for l in leases if not slot_path(l.slot).exists()]
        if pruned:
            _write_ledger(kept)
    return pruned


# `git symbolic-ref HEAD` 의 브랜치 full ref 접두 — `refs/heads/<name>`. 이 접두 **정확히**를
# 제거해야 순수 브랜치명이 된다(모호성 접두가 붙는 `--short` 대신 full ref 를 읽는 이유·T-0377).
_SYMREF_BRANCH_PREFIX = "refs/heads/"


def current_branch(slot: str, *, git_runner: GitRunner | None = None) -> str | None:
    """슬롯 worktree 의 git HEAD 에서 현재 브랜치를 **live** 로 읽는다 (ADR-0013 amend T-0072·T-0377).

    `git symbolic-ref HEAD` → `refs/heads/<name>` 에서 `refs/heads/` 접두를 정확히 제거해 브랜치명.
    브랜치가 git 의 단일 진실 — 장부에 저장된 복사본이 아니라 슬롯 worktree 의 실제 HEAD 를 매번
    조회한다(사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영·드리프트 불가능).

    **`symbolic-ref HEAD`(full ref·`--short` 없이)를 쓰는 이유 (codex T-0072/T-0377 게이트)**:
    `rev-parse --abbrev-ref HEAD` 는 (a) detached 를 `"HEAD"` 문자열로, (b) **unborn 브랜치**(아직
    커밋 0 인 새 브랜치)를 rc≠0 에러로 줘서 — *이름이 있는* unborn 브랜치를 detached/조회불가로
    오판한다(→ "(미지정)"). `symbolic-ref HEAD` 는 unborn 브랜치도 `refs/heads/<name>` 을 rc=0 으로
    주고, detached 일 때만 "ref HEAD is not a symbolic ref" 로 rc≠0 이라 — "현재 브랜치명 or 브랜치
    아님"의 정석 primitive 다(git=진실·ADR-0013 amend 정합). **`--short` 를 뺀 이유(T-0377)**:
    `--short` 는 브랜치명과 같은 이름의 태그가 있으면(릴리즈가 `v1.3.0` 브랜치를 그대로 `v1.3.0`
    태그로 찍은 경우) 모호성 회피로 `heads/v1.3.0` 을 돌려줘 순수 브랜치명이 아니었다 — 장부 기록
    (`v1.3.0`)과 불일치해 부트스트랩 0단계가 가짜 "외부 개입" FAIL-LOUD 로 차단됐다(PM 76 실측).
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
    """슬롯 worktree 의 상태(branch + upstream + submodule 역할)를 live 로 읽는다 (ADR-0051 파일럿 T-β·T-0276).

    부트스트랩이 현재 슬롯 상태를 1회 surface 하는 backbone. **T-0275 의 submodule 역할
    판별을 재사용**한다(중복 구현 금지):
      - `current_branch(slot)` — 브랜치(live·`symbolic-ref HEAD` full ref·ADR-0013 amend T-0072·T-0377).
      - `_upstream_status` — `@{upstream}` 해소 여부(T-0273/0274 로 슬롯 tracking 설정 → 해소).
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
    """기존 슬롯 리스의 test_cmd 를 갱신한다 (T-0069·ADR-0014 amend·idle/leased 무관).

    `pm-config` 콘솔의 `[b]`(슬롯 빌드명령 설정/변경)·worktree add 후의 "나중에 변경"
    경로가 부르는 setter — 슬롯에 바인딩된 회귀/빌드명령(HIL config 등)을 사후에 바꾼다
    (board._test_cmd 가 활성 슬롯의 이 필드를 areas 위 레이어로 읽는다·T-0066). 별도
    CLI 서브커맨드는 만들지 않는다(콘솔 `[b]` + worktree add 프롬프트로 충분·결정 §setter 단순화).

    create_slot 의 lease test_cmd 바인딩과 *같은* flock + atomic write 패턴을 재사용한다
    (`_lease_lock` 으로 read-modify-write 직렬화 → `_write_ledger` atomic replace). 장부에
    슬롯이 없으면 **`KeyError`** raise(침묵 무력화 금지 — 호출부가 명시 안내). 갱신된
    Lease 를 반환한다. `cmd=None` 이면 바인딩 해제(repo areas/local.conf 로 폴백·현행).
    """
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            raise KeyError(f"no lease for slot {slot!r}")
        target.test_cmd = cmd
        _write_ledger(leases)
        return target


# ── 슬롯 git 진실: 스냅 기록(write) + 비교(compare) + is-ancestor 판정 (T-0350·ADR-0060) ───────
# 슬롯 git 상태를 *기대*(drift 감지 기준) 축으로 lease 장부에 기계 기록한다(live 표시는 T-0072
# 그대로·2축). submodule 의 pin/drift 모델(T-0275/0276)을 본체(superproject)로 대칭 확장 — 개념
# 신설 0. write=부트스트랩 bind/alloc·핸드오프·create(release 시 정리)·compare=0단계(T-0351 소비).

# head 비교 결과(㉒ `merge-base --is-ancestor` 완화) — GitCompareResult.head_relation 값.
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
    """`git submodule status` → `[{path, pin}]` (T-0350·기록=기대 축).

    pin = submodule 의 현재 체크아웃 sha(선두 flag 제거) — "여기 두고 간다"의 기준값(재개 시 이
    sha 와 달라지면 drift·`_submodule_pin_drift`). `_submodule_statuses`(T-0276)와 같은 `submodule
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
        parts = line.split()
        if len(parts) < 2:
            continue
        # 라인 형식 `<flag><40-hex-sha> <path>[ (<describe>)]` — flag(공백/+/-/U)는 line[0].
        # 공백 flag 는 split 이 이미 떼어내 parts[0]=sha, 그 외 flag 는 parts[0]=flag+sha.
        raw = parts[0]
        sha = raw[1:] if raw[:1] in ("+", "-", "U") else raw
        pins.append({"path": parts[1], "pin": sha})
    return pins


def _snapshot_slot_git(slot: str, *, git_runner: GitRunner | None = None) -> "dict | None":
    """슬롯의 live git 상태를 스냅한다 — `{branch, head, submodules, recorded_at}` (base 제외·T-0350).

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
    """`lease.git` 을 live 슬롯 스냅으로 갱신한다 — write 프리미티브 본체(in-place·fail-soft·T-0350).

    base 규칙(3표·rebase 로만 변경·결정 ⑨): `base_branch` 주어짐(create·set-base) = base 를
    **새로** 기록 / 미주어짐(alloc/bind arrival) = 기존 `lease.git.base` **보존**. base 를 새로
    기록할 때 commit 은:
      - `base_commit` 명시(set-base — 그 브랜치 tip 또는 사용자 `@<commit>`·T-0352) = 그 값.
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
    except Exception:  # noqa: BLE001 — fail-soft: 스냅 실패가 alloc/bind/create 를 막지 않는다.
        pass


def record_git_snapshot(slot: str, *, base_branch: "str | None" = None,
                        base_commit: "str | None" = None,
                        git_runner: GitRunner | None = None) -> "Lease | None":
    """슬롯 현재 git 상태를 `lease.git` 에 기록하는 독립 write 프리미티브 (핸드오프·set-base·T-0350/T-0352).

    부트스트랩 bind/alloc·create 는 자기 lock 안에서 `_apply_git_snapshot` 을 인라인 호출하지만,
    핸드오프("여기 두고 간다")·set-base(기준점 명시 기록·T-0352)처럼 lifecycle op 밖에서 스냅을
    찍는 호출부(pm_handoff·`set_base`)를 위한 standalone 진입점이다. `base_commit` 은 set-base 가
    base.commit 을 그 브랜치 tip(또는 사용자 `@<commit>`)으로 명시할 때 쓴다(None=create 기존 거동·
    slot HEAD). `_lease_lock`+`_write_ledger`(atomic) — alloc/release 와 동일한 read-modify-write
    직렬화. 장부에 슬롯이 없으면 None(무해). 갱신된 Lease 반환."""
    with _lease_lock():
        leases = _read_ledger()
        target = next((l for l in leases if l.slot == slot), None)
        if target is None:
            return None
        _apply_git_snapshot(target, base_branch=base_branch, base_commit=base_commit,
                            git_runner=git_runner)
        _write_ledger(leases)
        return target


def _is_ancestor(git_runner: GitRunner, ancestor: str, descendant: str) -> bool:
    """`git merge-base --is-ancestor <ancestor> <descendant>` — ancestor 가 descendant 의 조상인가 (㉒·T-0350).

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
    """기록 head vs live head 관계 판정 (㉒ `merge-base --is-ancestor` 완화·T-0350).

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
    """기록 submodule pin ≠ live pin 인 path 목록 (T-0350·T-0275/0276 pin/drift 대칭·경고 축)."""
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
    """기록된 `lease.git`(기대) vs 슬롯 live 상태 비교 결과 — 부트스트랩 0단계 소비 (T-0350·소비처 T-0351).

    - `unrecorded` — git 미기록(구 슬롯·git 필드 부재/슬롯 없음) → drift 감지 **비활성**. 0단계는
      차단이 아니라 loud 표시 + 사용자 질의(`set-base`·결정 ⑪). ok/fail 어느 쪽도 아닌 별도 상태.
    - `recorded`/`live` — 기록 git dict / live 스냅 dict(branch·head·submodules).
    - `branch_match` — 기록 branch == live branch. False = 브랜치 변경(사고·FAIL-LOUD·표 branch 축).
    - `head_relation` — `match`/`descendant`/`diverged`/`unknown`(㉒ is-ancestor 완화·위 상수).
    - `submodule_drift` — 기록 pin ≠ live pin 인 submodule path 목록(경고·T-0275/0276 대칭).
    - ⚠️ **`base` 는 이 티켓(T-0350)에서 recorded-only — compare 가 surface 하지 않는다**(비교축은
      branch+head+submodule 뿐). 인터페이스 산문의 "base=사고 시 FAIL-LOUD"·"base 대비 N behind"
      판정은 0단계([[T-0351]])·F10(rebase·wave-2d)로 이월된다 — 여기선 `recorded["base"]`(inert
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
        fail 아님(사용자 질의 대상·결정 ⑪)."""
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
    """기록된 `lease.git`(기대) vs 슬롯 live 상태를 비교한다 — compare 프리미티브 (T-0350·0단계 소비·T-0351).

    장부에서 슬롯 lease 를 읽어 그 `git` 스냅(기대)을 live 스냅과 대조한다. `_lease_lock` 은 장부
    read 동안만 짧게 잡고 git 조회(subprocess)는 lock 밖에서 한다. 미기록(git 필드 없음/슬롯 없음)
    이면 `unrecorded=True`(drift 감지 비활성·결정 ⑪). head 비교는 `merge-base --is-ancestor` 로
    완화(㉒·crash 후 재개를 경보 소음으로 안 만든다). 판정 정책(FAIL/notice/질의)은 0단계(T-0351)
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


# ── set-base / rebase 기준-gate 계약 / status (기준점 미기록 flow·T-0352·spike §F9/F10) ──
# 기준점(base) 미기록 슬롯(v1.3.0 이전 전부)을 **자동 추론 없이** 다룬다(결정 ⑪): 엔진=상태 surface·
# PM=확인·사용자=결정 (prefix 확인 ①ⓑ 와 동형·[[mechanize-dont-instruct-llm]]). `git merge-base HEAD
# origin/main` 추측은 rebase 이력·다중 후보(main/develop)에서 **조용히 틀리고**, 그 위에서 drift 감지가
# 돌면 무의미해진다 → 추론 금지·사용자 명시 질의. set_base=사용자 지정 base 기록(T-0350 write 소비)·
# resolve_rebase_base=rebase 기준-gate **계약**(본체 wave-2d·⑩)·slot_git_status=조회(미기록 N-behind `-`).


# ── readonly 공유 슬롯 — role 조회 / mutation 거부 / refresh (⑬·T-0358·spike §F11) ──
# readonly 슬롯은 코드를 *읽어* PM 홈 wiki(domain·architecture·status)를 쓰는 research 기준면이다:
# 슬롯 자체는 read-only(detached·배타 대여 없음)이고, mutation op(set-base·rebase·dev·sync)은 **엔진
# 경로에서 거부**한다(fs 레벨 쓰기 차단은 안 함·결정 ④). 갱신은 `refresh`(fetch → detach 이동·dirty=
# 거부+loud)만 허용한다 — read-only 슬롯의 dirty 는 "누군가 여기 썼다"는 신호라 조용히 reset 하지 않는다.

_LEASE_ROLE_READONLY = "readonly"   # role 값(identity_args._LEASE_ROLE_READONLY 정합·모듈 격리 inline).


def _slot_role(slot: str) -> str:
    """슬롯의 role 을 장부에서 읽는다 — 미등록/미기록이면 "work" (T-0358·mutation 거부/status 소비).

    `_read_recorded_base` 와 동형으로 `_lease_lock` 은 장부 read 동안만 짧게 잡는다. 슬롯이 장부에
    없거나 role 필드가 없으면(구 장부) "work"(작업 슬롯·하위호환·fail-soft)."""
    with _lease_lock():
        leases = _read_ledger()
    target = next((l for l in leases if l.slot == slot), None)
    return target.role if target is not None else "work"


class ReadonlySlotMutation(RuntimeError):
    """readonly 공유 슬롯(⑬·role="readonly")에 mutation op(set-base·rebase·dev·sync)을 시도함 — 거부 (T-0358·§F11).

    readonly 슬롯은 문서 검증 기준면(released base)이라 슬롯의 git 상태를 바꾸는 엔진 op 을 거부한다
    (갱신은 `refresh` 만·fetch→detach 이동). fs 레벨 쓰기 차단은 안 한다(결정 ④ — 엔진 경로 한정).
    `op` = 거부된 연산명(진단용). (`RuntimeError` 서브클래스 — `BareRepoMissing` 동형·파사드 rc 1.)"""

    def __init__(self, slot: str, op: str):
        self.slot = slot
        self.op = op
        super().__init__(
            f"슬롯 {slot!r} 은 readonly 공유 슬롯(role=readonly·⑬)이라 `{op}` 를 거부한다 — 문서 검증 "
            f"기준면(released base·detached)이라 git 상태를 바꾸는 op 은 불가하다. 갱신은 "
            f"`/pm-worktree refresh {slot} [--onto <branch>]`(fetch→detach 이동)로만 한다(T-0358·§F11)."
        )


def _reject_readonly_mutation(slot: str, op: str, *, git_runner: GitRunner | None = None) -> None:
    """슬롯이 readonly(⑬)면 `ReadonlySlotMutation` raise — set-base/rebase/dev/sync 진입 가드 (T-0358).

    엔진 경로 한정 거부(결정 ④). 판별 축은 canonical `lease.role`(0단계 carve-out·F6 예외와 동일 축).
    git_runner 는 시그니처 정합용(현 판별은 장부만 읽어 미사용). **`_lease_lock` 을 취득한다**(자체
    `_slot_role`) — 이미 락을 쥔 호출부(release/bind_slot)는 이 헬퍼 대신 보유 중인 `lease.role` 을
    직접 검사한다(non-reentrant flock 재취득 = 데드락)."""
    if _slot_role(slot) == _LEASE_ROLE_READONLY:
        raise ReadonlySlotMutation(slot, op)


class ReadonlySlotNotLeasable(RuntimeError):
    """readonly 공유 슬롯(⑬)에 lease-lifecycle op(release·force_release·bind)을 시도함 — 거부 (T-0358·should-fix).

    readonly 슬롯은 **무소유 공유 자산**(배타 대여 없음·session/pid 없음)이라 대여/반납/바인딩(점유)의
    대상이 아니다. 자동 경로(alloc idle-탐색·reclaim_stale)는 이미 자연/가드로 닫혔으나, **명시 지정**
    (`release <slot>`·`force_release`·`/pm-bootstrap --slot`)이 뚫려 있었다 — idle 화되면 alloc 이 그
    슬롯을 work 슬롯으로 점유해 role 이 유실되는 깨진 상태를 부른다. 0단계 carve-out(F6)이 readonly 를
    *조회 지칭*엔 허용하지만 bind 는 *점유*라 의미가 다르다(이 거부가 그 틈을 닫는다). `op` = 거부된
    연산명(진단용). 제거는 `worktree remove --force`, 갱신은 `refresh`. (`RuntimeError` — 파사드 rc 1.)"""

    def __init__(self, slot: str, op: str):
        self.slot = slot
        self.op = op
        super().__init__(
            f"슬롯 {slot!r} 은 readonly 공유 슬롯(role=readonly·⑬)이라 `{op}`(대여/반납/바인딩) 대상이 "
            f"아니다 — 무소유 공유 자산(배타 대여 없음·session/pid 없음). 제거하려면 "
            f"`worktree remove {slot} --force`, 최신 갱신은 `/pm-worktree refresh {slot}` (T-0358·§F11)."
        )


class RefreshRefused(RuntimeError):
    """readonly 슬롯 `refresh` 거부 — dirty(누군가 씀·신호) / base 미해소 / non-readonly (T-0358·§F11).

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
        super().__init__(msg + tail + f" (T-0358·§F11).")


def refresh(slot: str, *, onto: "str | None" = None,
            git_runner: GitRunner | None = None) -> str:
    """readonly 공유 슬롯을 released 최신으로 갱신한다 — fetch → detached HEAD 이동 (⑬·T-0358·§F11).

    read-only 슬롯(detached·문서 검증 기준면)을 최신 released tip 으로 fast-forward 하는 유일 경로다
    (mutation op 은 거부·`refresh` 만 허용). 순서:
      1. **readonly 확인** — 대상 슬롯이 readonly 가 아니면 `RefreshRefused("not-readonly")`(작업 슬롯을
         detach 이동하면 브랜치 위치 유실).
      2. **기준 해소** — `onto`(명시) > 기록된 `base.branch`. 둘 다 없으면 `RefreshRefused("no-base")`
         (추론 금지·결정 ⑪ 정신).
      3. **dirty 거부 + loud** — 슬롯 worktree 에 미커밋 변경이 있으면 `RefreshRefused("dirty")`. read-only
         슬롯의 dirty 는 "누군가 썼다"는 신호라 조용히 reset 하지 않는다(감지=기계·해소=사용자).
      4. **fetch → detach 이동** — `git fetch origin`(best-effort) 후 기준의 최신 tip(`origin/<branch>`
         해소되면 그 최신·아니면 로컬 `<branch>`)으로 `git checkout --detach <ref>`(detached HEAD 이동).
      4b. **submodule 재동기** — `git submodule update --init --recursive --force`(gitlink 옛 pin 잔존
         →stale+dirty 자가 잠금 방지·must-fix ①·readonly=dev submodule 없어 전체 재동기 안전).
      5. **스냅 갱신** — `record_git_snapshot(base_branch=해소된 branch)` — base.commit 을 새 head 로
         갱신한다(onto 생략[기록된 base 로 refresh]에도 갱신·must-fix ② — HEAD 이동했는데 장부 base.commit
         옛값 잔존 불일치 방지). refresh 는 base 가 정당하게 바뀌는 유일 지점(⑨ "rebase 로만" 의 readonly 예외).
    반환 = detach 이동한 기준 ref(본체 CLI 가 보고). `git_runner` 주입 시 그 runner(테스트 hermetic·
    미주입이면 슬롯 worktree 바인딩 실 runner)."""
    if _slot_role(slot) != _LEASE_ROLE_READONLY:
        raise RefreshRefused(slot, "not-readonly")
    # 기준 해소 — onto 명시 > 기록된 base.branch (추론 금지).
    base_branch = onto
    if base_branch is None:
        recorded = _read_recorded_base(slot)
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

    # fetch → 최신 tip 해소(origin/<branch> 우선·로컬 폴백) → detach 이동.
    ref = base_branch
    rc, out = runner(["fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] 슬롯 {slot} `git fetch origin` 실패 (rc={rc}): {str(out).strip()[:200]}\n"
            f"  로컬 `{base_branch}`(동결 head)로 detach 이동한다 — 네트워크 복구 후 재-refresh 하라 "
            "(T-0358·fail-soft).",
            file=sys.stderr,
        )
    else:
        # base_branch 가 이미 `origin/…` 면 그대로·순수 브랜치명이면 origin/<branch> 해소 시도.
        if not base_branch.startswith("origin/"):
            rc2, _ = runner(["show-ref", "--verify", "--quiet", f"refs/remotes/origin/{base_branch}"])
            if rc2 == 0:
                ref = f"origin/{base_branch}"
    rc, out = runner(["checkout", "--detach", ref])
    if rc != 0:
        raise RefreshRefused(slot, "git-error",
                             detail=f"`git checkout --detach {ref}` 실패 (rc={rc}): {str(out).strip()[:200]}")
    # submodule 재동기 (must-fix ①·codex) — `checkout --detach` 는 superproject HEAD 만 옮기고
    # submodule gitlink 는 **옛 pin 잔존** → readonly 기준면이 stale + `git status` dirty(gitlink 변경)
    # 로 남아 **다음 refresh 가 자기 dirty 거부에 걸리는 자가 잠금**이 된다(create_slot 은 init 하는데
    # refresh 는 안 하던 비대칭). readonly 슬롯은 mutation(dev) 거부라 on-branch(dev) submodule 이
    # 존재할 수 없으니(보호 대상 0), selective 가 아닌 **전체 재동기**(`--init --recursive --force`·
    # create_slot 관례 정합)가 안전하다. rc≠0 은 fail-loud(기준면이 반쯤 갱신된 채 성공 보고 금지).
    rc, out = runner(["submodule", "update", "--init", "--recursive", "--force"])
    if rc != 0:
        raise RefreshRefused(slot, "git-error",
                             detail=f"submodule 재동기 실패 (rc={rc}): {str(out).strip()[:200]}")
    # 스냅 갱신 (must-fix ②·codex) — base 를 **해소된 branch 로 재기록**해 base.commit=새 head 로
    # 갱신한다. onto 생략(기록된 base.branch 로 refresh)에도 base_branch 를 넘겨야, HEAD 는 최신
    # origin/<base> 로 이동했는데 장부 base.commit 은 옛 커밋으로 남아 status "N behind"·기준면 기록이
    # 실제와 어긋나는 불일치를 막는다. refresh(readonly 전용)는 set-base/rebase 외에 base 가 바뀌는
    # *유일한 정당 지점*이다(⑨ "base 는 rebase 로만" 결정의 readonly 예외 — detached 기준면 이동이 곧
    # base 이동). base_commit 미지정 → `_apply_git_snapshot` 이 방금 스냅한 head 를 commit 으로 쓴다.
    record_git_snapshot(slot, base_branch=base_branch, git_runner=git_runner)
    return ref


def _parse_base_ref(base_arg: str) -> "tuple[str, str | None]":
    """`<branch>[@<commit>]` 인자를 (branch, commit|None) 로 분해한다 (set-base·spike §F9·T-0352).

    첫 `@` 에서 가른다(`str.partition`) — `origin/main@df10dc6` → ("origin/main", "df10dc6"),
    `origin/main` → ("origin/main", None). `@` 없으면 commit 미지정(그 브랜치 tip 이 base commit).
    브랜치명에 `@` 가 든 드문 ref(`@{upstream}` 등)는 이 표면에서 지원하지 않는다(문서화·CLI 진입)."""
    branch, sep, commit = base_arg.partition("@")
    return branch, (commit if sep and commit else None)


def _resolve_base_commit(slot: str, ref: str, *, git_runner: GitRunner | None = None) -> "str | None":
    """`ref`(브랜치 또는 커밋)의 커밋 sha 를 슬롯 worktree 에서 해소한다 (set-base commit=branch tip·T-0352).

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
    """set-base 의 base ref(브랜치 또는 `@<commit>`)를 슬롯 worktree 에서 해소할 수 없다 — FAIL-LOUD (T-0352·codex must-fix).

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
            f"않습니다 — 자동 추론/폴백 금지·결정 ⑪·T-0352.)"
        )


def set_base(slot: str, base_ref: str, *, commit: "str | None" = None,
             git_runner: GitRunner | None = None) -> "Lease | None":
    """슬롯 기준점(base)을 **사용자 명시**로 기록한다 — `/pm-worktree set-base` 백본 (결정 ⑪·T-0352).

    미기록 슬롯(v1.3.0 이전)의 base 를 **추론 없이** 사용자가 지정해 기록한다(그때부터 drift 감지
    작동). `base_ref` = 기준 브랜치(예 `origin/main`), `commit` = 명시 `@<commit>`(생략 = 그 브랜치
    tip). base.commit 해소:
      - `commit` 명시 → 그 커밋(rev-parse verify).
      - 생략 → `base_ref` tip(rev-parse verify).
      - **해소 불가 → `BaseRefUnresolvable` FAIL-LOUD**(record 안 함·codex must-fix). slot HEAD 폴백
        금지 — 무관한 커밋으로 조용히 오기록하면 drift 감지가 garbage baseline 위에서 돌아 이 티켓
        계약("조용히 틀린 base 차단")을 스스로 위반한다. (create 경로의 slot-HEAD 폴백은
        `_apply_git_snapshot`/`record_git_snapshot` 레벨에 그대로 — fresh 슬롯 tip==브랜치 tip=정답.)
    T-0350 write 프리미티브(`record_git_snapshot(base_branch=,base_commit=)`)를 소비한다 — 자체 장부
    write 를 재구현하지 않는다(base_commit=검증된 실 sha·None 폴백 경로를 안 탄다). **자동 추론 절대
    금지**(엔진=surface·사용자=결정). 장부에 슬롯이 없으면 None. 갱신된 Lease 반환.

    **readonly 거부(⑬·T-0358)**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise
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
    """장부에서 슬롯의 기록된 base(`lease.git.base`)를 읽는다 — 미기록이면 None (T-0352·rebase gate/status 소비).

    `_lease_lock` 은 장부 read 동안만 짧게 잡는다(git 조회는 lock 밖·`compare_slot_git` 동형)."""
    with _lease_lock():
        leases = _read_ledger()
    target = next((l for l in leases if l.slot == slot), None)
    if target is None or not isinstance(target.git, dict):
        return None
    base = target.git.get("base")
    return base if isinstance(base, dict) else None


class RebaseBaseRequired(RuntimeError):
    """rebase 대상 슬롯에 기준점(base)이 미기록이라 rebase 를 거부한다 (계약·결정 ⑪·T-0352·본체 wave-2d ⑩).

    기준점 없이 rebase 하면 "어디로 rebase" 가 정의되지 않는다(추론 금지·spike §F9). `--onto <branch>`
    를 명시하면 그것을 기준으로 진행하고 그 값을 base 로 기록한다(1회 해소·`resolve_rebase_base(onto=)`).
    이 티켓은 그 **계약**만 정의하고 rebase 엔진 본체는 wave-2d(⑩)가 이 gate 를 소비해 구현한다.

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
    """rebase 기준-gate — 기준 없으면 거부·`--onto` 명시 시 진행(+`record` 시 기록) (결정 ⑪·T-0352·T-0359).

    rebase 엔진 본체(F10·⑩)가 "어느 base 로 rebase" 를 이 gate 로 해소한다:
      - `onto` 명시 + `record=True`(기본·standalone 계약) → 그것을 base 로 **즉시 기록**(`set_base`·
        1회 해소)하고 반환. **base 가 실제로 기록됐을 때만 반환** — `set_base` 가 ref 해소 실패로
        `BaseRefUnresolvable` 을 던지면 자연 전파, 슬롯 장부 미등록으로 `None`(기록 실패)을 반환하면
        `RebaseBaseRequired` 로 명시 실패한다(codex must-fix — silent onto 반환 금지).
      - `onto` 명시 + `record=False`(**rebase 본체 경로·T-0359 must-fix**) → onto 를 **검증만**한다
        (`_resolve_base_commit`·해소 불가면 `BaseRefUnresolvable`). set_base 부작용(즉시 기록) **없음**
        — 장부는 건드리지 않고 브랜치명만 반환한다. 호출부(`_rebase_one`)가 **rebase 성공 시에만**
        base+head+recorded_at 을 원자 기록한다. 이유: onto 를 rebase *이전에* 기록하면 이후 충돌/사용자
        abort 시 장부는 새 base 를 주장하나 tree 는 옛 base 라 "충돌=장부 미갱신" 계약을 위반하고
        status N-behind 를 조용히 오표시한다(no-onto 충돌 경로는 불변인데 onto 만 비대칭이던 갭).
      - `onto` 없음 + 기록된 base 있음 → 기록된 `base.branch` 반환(그 최신으로 rebase·부작용 없음).
      - `onto` 없음 + 미기록 → `RebaseBaseRequired` raise(거부 — 기준 없이 rebase 불가·추론 금지).
    반환값 = rebase 가 향할 base 브랜치명(본체가 소비). 자동 rebase 없음(사용자 명시·spike §F10).

    **readonly 거부(⑬·T-0358)**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
    rebase 는 슬롯 git 을 바꾸는 mutation 이라 read-only 기준면엔 불가(진입 가드·record 무관 선행)."""
    _reject_readonly_mutation(slot, "rebase", git_runner=git_runner)
    if onto is not None:
        if not record:
            # rebase 본체 경로 — 검증만(해소 불가=BaseRefUnresolvable·T-0352 기존 계약 유지)·기록 없음.
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
    """슬롯 HEAD 가 `base_branch` 대비 몇 커밋 behind 인가 — `git rev-list --count HEAD..<base_branch>` (T-0352·spike §F10).

    base 기록의 배당금: "base 대비 N commits behind" = rebase 필요 판단 근거(spike §F9). `base_branch`
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
    """슬롯 git 구성 조회 — base·branch·head·**base 대비 N behind**·submodule pin/drift·dirty (`/pm-worktree status` 백본·T-0352/T-0359·spike §F10).

    미기록(base 없음)이면 `behind=None`·`behind_reason`=이유(계산 불가 → CLI `-` 표기·자동 추론
    금지·결정 ⑪). 기록 있으면 `base_behind_count` 로 N 을 센다. branch/head 는 live 조회
    (`current_branch`/`_slot_head`·표시 축). **submodule pin/drift(`_submodule_statuses`·T-0276
    재사용·역할별 `SubmoduleStatus`)·dirty(`_is_dirty`)는 wave-2d(⑩·T-0359)에서 조회에 합류**한다
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
        reason = "기준점 미기록 — `set-base` 로 지정 필요(자동 추론 금지·결정 ⑪)"
    # submodule pin/drift·dirty — runner 해소 시에만(슬롯 부재/미주입이면 조회 불가 → 빈/False).
    submodules = _submodule_statuses(runner) if runner is not None else []
    dirty = _is_dirty(slot_path(slot), git_runner=runner) if runner is not None else False
    return {"slot": slot, "base": base, "branch": branch, "head": head,
            "behind": behind, "behind_reason": reason,
            "submodules": submodules, "dirty": dirty}


def status(*, task: "str | None" = None, slot: "str | None" = None,
           git_runner: GitRunner | None = None) -> "list[dict]":
    """슬롯 git 구성 일괄 조회 — 단일 슬롯 / `--task` 전 슬롯 / 무인자=내 task 전 슬롯 (§F10·⑩·T-0359).

    대상 슬롯 해소(택일):
      - `slot` 명시 → 그 슬롯 하나(`_normalize_slot` 형식 검증·traversal 차단).
      - `task` 명시 → `slots_for_task(task)`(session==task 이고 leased 인 슬롯·T-0354).
      - 둘 다 생략 → 내 task 전 슬롯(`slots_for_task(_default_session())` — env/local.conf 유입
        세션 정체성이 보유한 leased 슬롯·spike §F10 "무인자=내 task 전체").
    각 슬롯을 `slot_git_status`(base·branch·head·N behind·submodule pin/drift·dirty)로 조회하고
    `role`(work/readonly·⑬·T-0358)을 얹어 슬롯별 dict 리스트로 돌려준다. 손-git 불요·조회 전용
    (부작용 0). 미기록 base 는 `behind=None`(CLI `-`·자동 추론 금지·결정 ⑪).

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


# ── rebase (단일/일괄·선-검사·충돌 그대로+loud·장부 원자 갱신·자동 rebase 없음·⑩·T-0359·§F10) ──
# 슬롯 base 를 사용자 명시로만 옮긴다(자동 rebase 없음·결정 ⑤ 정신). 슬롯마다 독립 처리:
# 선-검사 3종(소유/dirty/rebase 진행중) → 실패면 스킵+loud, 통과면 대상 base 최신 fetch → git rebase.
# 충돌은 **그 상태 그대로 두고 fail-loud**(엔진 임의 abort 금지 — 해소는 사용자 git rebase --continue|
# --abort·다음 부트스트랩 0단계 T-0351 가 "rebase 진행 중" 으로 감지·안내). 장부 갱신은 **성공 시에만**
# 원자적(base.commit=새 base tip·head=새 tip·recorded_at·record_git_snapshot 소비). 기준점 미기록 +
# --onto 없음 = 거부(추론 금지·resolve_rebase_base gate·결정 ⑪).

REBASE_REBASED = "rebased"        # 성공 — 장부 원자 갱신 완료.
REBASE_SKIPPED = "skipped"        # 선-검사/거부 스킵(loud) — reason 참조.
REBASE_CONFLICT = "conflict"      # rebase 가 rc≠0(충돌 등) — 그 상태 그대로·장부 미갱신·loud.


class RebaseSlotResult:
    """rebase 한 슬롯 하나의 결과 — outcome + 진단 (일괄 요약 원료·T-0359·§F10).

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
    """슬롯 worktree 에 rebase 가 진행 중인가 — `.git/rebase-merge` | `rebase-apply` 존재 (T-0359·spike §F10).

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
    """슬롯 하나 rebase — 선-검사 → fetch → git rebase → 성공 시 장부 원자 갱신 (T-0359·§F10).

    선-검사(스킵+loud·순서): readonly(공유 기준면·mutation 불가·⑬) → 소유(내 세션 leased 아님) →
    dirty(clean 전제) → rebase 진행 중. 통과하면 `resolve_rebase_base` gate(미기록+onto 없음=거부·
    onto=진행+기록·결정 ⑪) 로 대상 base 브랜치를 해소하고, `origin/<base>` 최신을 fetch 후 `git
    rebase` 한다. rc≠0(충돌 등) = **그 상태 그대로 두고 conflict**(엔진 임의 abort 금지). 성공 =
    `record_git_snapshot(base_branch, base_commit=새 base tip)` 로 base.commit·head·recorded_at 을
    원자 갱신(§F10). **raise 하지 않는다** — 모든 조건을 RebaseSlotResult 로 돌려 일괄 독립성을
    보장한다(한 슬롯의 예외가 나머지를 막지 않음)."""
    def skip(reason: str, base: "str | None" = None) -> RebaseSlotResult:
        return RebaseSlotResult(slot, REBASE_SKIPPED, reason=reason, base=base)

    # ── 선-검사 (스킵 + loud·독립) ─────────────────────────────────────────
    if _slot_role(slot) == _LEASE_ROLE_READONLY:
        return skip("readonly")   # 공유 기준면 — mutation 불가(⑬·T-0358·refresh 로만 갱신).
    with _lease_lock():
        lease = next((l for l in _read_ledger() if l.slot == slot), None)
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

    # ── base 해소 gate (미기록+onto 없음=거부·onto=검증만·**기록은 성공 시에만**·⑪·T-0352·T-0359 must-fix) ──
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
            f"  로컬 `{base_branch}`(동결)로 rebase 를 시도한다 — 네트워크 복구 후 재시도 권장 "
            "(T-0359·fail-soft).",
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
        # base/head/recorded_at 미갱신·미완·spike §F10 — resolve_rebase_base(record=False)로 onto 를
        # 미리 기록하지 않았기에 가능·T-0359 must-fix) → 다음 부트스트랩 0단계(T-0351)가 "rebase
        # 진행 중" 으로 감지·안내한다.
        return RebaseSlotResult(slot, REBASE_CONFLICT, base=base_branch,
                                reason=str(out).strip()[:200])

    # ── 성공 → 장부 원자 갱신(**유일 기록 지점**·base.commit=새 base tip·head=새 tip·recorded_at·§F10) ──
    # onto 든 no-onto 든 base 는 여기서만 기록된다(성공 원자 갱신) — 충돌 경로는 장부를 안 건드린다.
    base_tip = _resolve_base_commit(slot, target, git_runner=runner)
    record_git_snapshot(slot, base_branch=base_branch, base_commit=base_tip,
                        git_runner=runner)
    return RebaseSlotResult(slot, REBASE_REBASED, base=base_branch,
                            new_head=_slot_head(runner))


def rebase(slots: "list[str]", *, onto: "str | None" = None,
           git_runner: GitRunner | None = None) -> "list[RebaseSlotResult]":
    """슬롯 base 를 사용자 명시로 rebase — 단일/일괄·슬롯 독립·자동 rebase 없음 (⑩·T-0359·spike §F10).

    `slots` = 대상 슬롯 식별자 리스트(단일이면 1개·일괄이면 `slots_for_task` 결과). 각 슬롯을
    `_rebase_one` 로 **독립** 처리한다 — 한 슬롯의 충돌/스킵이 나머지를 막지 않는다(일괄 독립성·
    spike §F10). 소유 판별 축 = 내 세션(`_default_session()` — env/local.conf 유입·`_resolve_current_
    slot` 동형): 그 세션이 leased 로 보유하지 않은 슬롯은 `not-owner` 스킵(loud). `onto` 생략 =
    기록된 base.branch 최신으로 rebase(미기록이면 거부·결정 ⑪). 반환 = 슬롯별 `RebaseSlotResult`
    리스트(호출부가 성공/스킵/충돌 요약)."""
    owner = _default_session()
    return [_rebase_one(s, onto=onto, owner=owner, git_runner=git_runner) for s in slots]


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────


def _rollback_worktree(repo: str, slot_path_: Path, *, git_runner: GitRunner | None = None) -> None:
    """`git worktree add` 성공 후 단계가 실패했을 때 만든 worktree 를 롤백한다 (T-0070).

    bare 컨텍스트(`.repos/<repo>.git`)에서 `git worktree remove <slot_path> --force` 를
    부른다 — `add` 가 거기서 일어났으므로 `remove` 도 같은 컨텍스트라야 한다(공유 .git 원
    = bare·ADR-0011 §31). 실패하면 best-effort 로 `worktree prune` 폴백.

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


def _checkout(slot_path_: Path, branch: str, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 에서 브랜치 체크아웃 (브랜치 변경 = 같은 슬롯 재체크아웃·ADR-0013).

    `git checkout --no-recurse-submodules <branch>`. 브랜치가 없으면 새로 만든다(`-B`) — 풀
    슬롯에 새 작업스트림을 붙이는 정상 경로. (같은 브랜치 동시 2-worktree checkout 은 git 이
    거부 — ADR-0013 §8-6.)

    **`--no-recurse-submodules` (ADR-0051 크럭스 A·codex 게이트)**: 사용자 환경(전역
    `~/.gitconfig` 또는 repo config)에 `submodule.recurse=true` 가 설정돼 있으면 plain
    `git checkout` 이 *selective resync 전에* submodule 을 재귀 갱신해 on-branch(dev) submodule
    을 detached pin 으로 낚아챈다 — ADR-0051 이 selective resync 로 막으려던 바로 그 dev 파괴.
    양 checkout 호출에 `--no-recurse-submodules`(git 2.13+·2.43 확인)를 박아 ambient config 를
    override → checkout 은 submodule 을 절대 안 건드리고 `_resync_submodules_selective` 가
    submodule 상태의 **유일 권위**가 된다(ADR-0051 "전역 recurse 대신 selective" 정신 정합).
    """
    runner = git_runner or _real_git_runner(slot_path_)
    rc, out = runner(["checkout", "--no-recurse-submodules", branch])
    if rc != 0:
        rc, out = runner(["checkout", "--no-recurse-submodules", "-B", branch])
    return rc, out


def _parse_submodule_entries(status_out: str) -> list[tuple[str, str]]:
    """`git submodule status` 출력에서 `(flag, path)` 를 뽑는다 (git 2.43).

    각 라인 형식 = `<flag><40-hex-sha> <path>[ (<describe>)]` — **선두 1글자가 status 플래그**
    (`' '`=index pin 과 일치·`'+'`=working≠pin·`'-'`=미초기화·`'U'`=충돌)다. 플래그는 항상
    라인 첫 글자(`line[0]`)이고(공백 플래그도 포함), **두 번째 whitespace 토큰이 경로**다
    (`line.split()[1]` — 선두 공백은 split 이 무시·플래그 문자는 sha 에 붙어 토큰이 안 쪼개짐).
    `len(parts) >= 2` 를 만족하면 라인은 비어있지 않으므로 `line[0]` 접근이 안전하다. 빈 출력
    (submodule 없음)·토큰 2개 미만 라인은 건너뛴다.

    `flag` 는 pin↔working 판정에 필요하다(T-0276 slot_status — `+`=drift·`' '`=pinned). 경로만
    필요한 호출부(`_resync_submodules_selective`)는 `_parse_submodule_paths` 를 쓴다(경로만 뽑음).
    """
    entries: list[tuple[str, str]] = []
    for line in status_out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            entries.append((line[0], parts[1]))
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
    """슬롯 브랜치의 `@{upstream}` 추적 브랜치명 + 해소 여부 (T-0276·T-0273/0274 로 설정돼야 정상).

    `git rev-parse --abbrev-ref @{upstream}` — 해소되면 rc0 + 추적 브랜치명(예 `origin/a5`),
    미설정이면 rc≠0(`fatal: no upstream configured …`)이다. **rc 를 먼저 본다** — `_real_git_runner`
    가 stdout+stderr 를 합쳐 돌려주므로(T-0070) 미해소 시 out 이 fatal 메시지로 *비어있지 않다*.
    rc≠0 또는 빈 이름이면 `(None, False)`(미해소·부트스트랩이 경고), 해소면 `(name, True)`.
    """
    rc, out = runner(["rev-parse", "--abbrev-ref", "@{upstream}"])
    name = out.strip()
    if rc != 0 or not name:
        return None, False
    return name, True


def _submodule_statuses(runner: GitRunner) -> list[SubmoduleStatus]:
    """각 submodule 을 역할별로 판정한 `SubmoduleStatus` 리스트 (T-0276·T-0275 판별 재사용).

    `_resync_submodules_selective` 와 *같은* primitive 로 역할을 정한다(중복 판별 구현 금지):
    `git submodule status`(`_parse_submodule_entries` — flag+path) + submodule 당
    `git -C <sub> symbolic-ref -q HEAD`(rc0=on-branch/dev·rc≠0=detached) + `_submodule_dirty`.

      - on-branch(dev) → `"dev-ahead"`(정보). 사용자가 그 submodule 에서 브랜치를 파 작업 중.
      - detached & flag `-`(미초기화) → `"uninitialized"`(경고·슬롯 init 비정상).
      - detached & flag 공백(pin==working) → `"pinned"`(정상).
      - detached & 그 외 flag(`+`/`U`·pin≠working) → `"drift"`(경고).

    fail-soft: `git submodule status` rc≠0(조회 불가/submodule 없음)이면 **빈 리스트**
    (부트스트랩이 submodule 줄 생략). dirty 는 *왜* drift 가 안 풀렸는지 surface 용(T-0275 는
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
    """브랜치 전환(`_checkout`) 성공 후 submodule 을 **선택적으로** superproject pin 에 재동기 (ADR-0051).

    worktree 풀 슬롯의 브랜치를 바꾸면 superproject 는 새 브랜치의 submodule pin 을 가리키지만
    submodule 워킹트리는 이전 pin 그대로라 drift 가 생긴다(ADR-0051 §Context). 브랜치 전환
    직후 각 submodule 을 **역할별로** 재동기한다 — 역할은 별도 장부 없이 submodule 의 live git
    HEAD 로 판별한다(ADR-0051 §Decision 1·무스키마·기본 A):

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
        # 역할 = live git HEAD(ADR-0051 §Decision 1·장부 없음). on-branch(dev) → 보호(skip).
        rc_head, _out = runner(["-C", sub, "symbolic-ref", "-q", "HEAD"])
        if rc_head == 0:
            continue
        # detached(consume) → pin 재동기. 단 dirty(미커밋)면 작업 유실 방지 위해 skip + 경고.
        if _submodule_dirty(sub, runner):
            print(
                f"[경고] submodule {sub!r} 이 detached 이나 미커밋 변경으로 dirty — pin "
                f"재동기 skip (작업 보호·ADR-0051). 정리 후 재-alloc 하면 재동기된다.",
                file=sys.stderr,
            )
            continue
        rc_up, out_up = runner(
            ["submodule", "update", "--init", "--recursive", "--force", "--", sub]
        )
        if rc_up != 0:
            print(
                f"[경고] submodule {sub!r} pin 재동기 실패 (rc={rc_up}): "
                f"{str(out_up).strip()[:200]} — drift 잔존 가능(ADR-0051·fail-soft·checkout 은 성공).",
                file=sys.stderr,
            )


def _checkout_required(slot: str, branch: str, *, git_runner: GitRunner | None = None) -> None:
    """`_checkout` 을 부르고 실패(rc≠0)면 `CheckoutFailed` raise (ADR-0013).

    fail-soft 로 무시하면 호출부가 장부 branch/state 를 성공처럼 갱신해 장부↔실제 worktree
    branch 가 어긋난다. 성공해야만 호출부가 장부를 갱신하도록 강제하는 가드.

    체크아웃 성공 직후 `_resync_submodules_selective` 로 submodule 을 새 브랜치 pin 에 selective
    재동기한다(ADR-0051 파일럿 T-α — detached=consume 만 재동기·on-branch=dev skip·dirty
    skip+경고). *브랜치 전환* 경로(alloc 세 checkout 분기)에만 붙는다 — `create_slot` 최초 init
    은 이 함수를 안 타고 자체 `submodule update --init --recursive --force`(fresh=전부 detached)를
    유지한다. 재동기는 fail-soft(raise 안 함)라 checkout 성공을 되돌리지 않는다.
    """
    rc, out = _checkout(slot_path(slot), branch, git_runner=git_runner)
    if rc != 0:
        raise CheckoutFailed(slot, branch, out)
    _resync_submodules_selective(slot_path(slot), git_runner=git_runner)


def _local_conf_session() -> str | None:
    """`.project_manager/local.conf` 의 `session=` (없거나 OSError → None).

    board.py 를 import 하지 않으므로(ADR-0013 isolation·touches 격리·병렬충돌 회피)
    `board.local_config().get("session")` 와 *동일 의미*를 stdlib 로 자체 구현한다 —
    plain `KEY=value`·`#` 주석/빈 줄 무시. 부재/읽기실패는 None(폴백).
    """
    conf_file = REPO / ".project_manager" / "local.conf"
    try:
        text = conf_file.read_text(encoding="utf-8")
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
    """lease 장부 state=="leased" 행들의 session 목록 (count-based 유도·ADR-0040 D1).

    `_default_session` 이 lease 취득 *전*(lock 밖)에 호출하므로 lock 없는 point-read 로 장부
    파일(`LEASES_FILE`)을 직접 읽는다 — `_read_ledger`(lock 보유 전제)와 별도. 리스는
    atomic-replace(`os.replace`)로 쓰므로 lock 없는 read 도 일관 스냅샷을 본다(board.py
    `_leased_sessions` 와 *동형*). 장부 부재/파싱실패/손상은 빈 리스트(fail-soft). session 이
    빈/None 인 행은 제외.
    """
    if not LEASES_FILE.exists():
        return []
    try:
        data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
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
    """세션 식별자 기본값 — board.py `session_name()` 과 *동형* 우선순위 (ADR-0040 D1·T-0073):
    `$PM_SESSION_NAME` env > `$CLAUDE_SESSION_NAME` env(deprecated alias·silent) >
    lease 장부 state=="leased" 행이 정확히 1개면 그 session (단일-lease 유도) >
    (장부 부재·leased 0 = solo) `local.conf session=` > `<host>-<pid>`.

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias
    (둘 다면 PM 승·조용히 동작). **leased ≥2 (모호)면 local.conf 층을 건너뛰고 `<host>-<pid>` 로
    간다**(board.session_name 과 동형 — 저장 쪽지로 남의 세션 행세 차단·ADR-0040).

    board.session_name 과 **tail 만 다르다**: 여기는 lease *취득*의 국소 임시 명명이라 미해소를
    None/fail 로 두지 않고 `<host>-<pid>` 로 폴백한다(ADR-0040 — host-pid 최종 폴백은 세션-귀속
    아닌 국소 용처에만 잔존). board.py 를 import 하지 않으므로([[ADR-0013]] isolation·touches
    격리·병렬충돌 회피) 같은 해소를 자체 구현한다. 저장측(여기)과 매칭측(board.session_name)이
    어긋나면 per-slot test_cmd 가 미스되므로(T-0066 must-fix) 세 모듈을 같은 우선순위로 통일한다.
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


# ── 운영중 관리 backbone: dev/sync + CLI (ADR-0049/0051 파일럿 T-γ·T-0277) ─────────
# worktree/submodule 운영 *중* 관리를 명령어化 하기 위한 두 backbone + argparse 진입점.
# pm-worktree 스킬(어댑터·PM authoring)이 이 커맨드를 얇게 래핑한다 — 백본 로직은 전부 여기.
#   - dev <sub> <branch> : 슬롯 worktree 의 submodule 을 dev 브랜치로 지정(on-branch 화) → 이후
#       selective resync(`_resync_submodules_selective`)가 그 submodule 을 dev 로 판별해 skip
#       (detached pin 으로 낚아채지 않음·ADR-0051 §D1 live-HEAD 모델). "내가 작업 중" 선언.
#   - sync              : 현재 슬롯의 `_resync_submodules_selective` 를 수동 트리거(브랜치 전환
#       없이 명시 재동기·detached=pin 재동기·on-branch=skip·dirty=skip+경고). T-0275 백본 공유.
# 얇게 — 기존 primitive(`_resync_submodules_selective`·submodule 판별)를 재사용한다(중복 금지).


def dev(slot: str, sub: str, branch: str, *, git_runner: GitRunner | None = None) -> tuple[int, str]:
    """슬롯 worktree 의 submodule 을 dev 브랜치로 지정한다 — on-branch 화 (ADR-0051 §D1·T-0277).

    `git -C <sub> checkout -b <branch>`(신규 생성)로 submodule 을 on-branch 로 만든다. 브랜치가
    이미 있으면 `-b` 가 rc≠0 이므로 `git -C <sub> checkout <branch>`(전환)로 폴백한다(`_checkout`
    의 create-or-switch 정신과 정합·단 submodule 컨텍스트). 결과적으로 그 submodule 의 live git
    HEAD 가 symbolic ref(on-branch)가 되어, 이후 `_resync_submodules_selective`(T-0275)가 그
    submodule 을 **dev 역할로 판별해 skip** 한다(detached pin 으로 낚아채지 않음·ADR-0051 크럭스 A)
    — 즉 "이 submodule 은 내가 작업 중이니 pin 재동기로 건드리지 마" 선언(무스키마·역할은 별도
    장부 없이 submodule HEAD 로 정함·ADR-0051 §Decision 1).

    **`sub` 슬롯-경계 검증(codex must-fix·T-0277)**: `dev` 는 실 git side-effect(`checkout`)를
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

    **readonly 거부(⑬·T-0358)**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
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
    """현재 슬롯의 submodule 을 superproject pin 에 selective 재동기(수동 트리거) — (ADR-0051·T-0277).

    T-0275 의 `_resync_submodules_selective` 를 **브랜치 전환 없이** 수동으로 부른다 — 브랜치를
    바꾸지 않고 명시적으로 submodule 상태를 pin 에 맞추고 싶을 때의 진입(부트스트랩/checkout 경로
    밖). 판별·거동은 전부 그 backbone 이 소유한다(중복 구현 금지·얇은 트리거):
      - detached(consume) & clean → `git submodule update --init --recursive --force -- <sub>` 로 pin 재동기.
      - on-branch(dev) → skip(dev 작업 보호·크럭스 A).
      - detached & dirty → skip + 경고(미커밋 작업 유실 방지).
      - submodule 없는 슬롯 → no-op.
    fail-soft(raise 금지·경고는 stderr) — `_resync_submodules_selective` 계약 상속. `git_runner`
    주입 시 그 runner(테스트 mock·DI seam), 미주입이면 슬롯 worktree 바인딩 실 runner.

    **readonly 거부(⑬·T-0358)**: 대상 슬롯이 readonly 공유 슬롯이면 `ReadonlySlotMutation` raise —
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
    """dev 대상 submodule 이 슬롯의 실제 submodule 목록에 없다 — 슬롯 경계 밖 checkout 차단 (must-fix·T-0277).

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
    """`--slot` 값을 슬롯 식별자 정규형(`work/<repo>_<N>`)으로 정규화 + 형식 검증 (must-fix 2·T-0277).

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
    """dev/sync 의 대상 슬롯을 해소한다 — 명시 슬롯 문자열 > cwd > 세션 leased (T-0277·T-0318).

    `slot_arg` 는 CLI `--repo`/`--slot` 로 정체성이 완전 해소된 경우 `main`(`identity.kind ==
    "slot"`)이 조립해 넘기는 `<repo>_<N>` 문자열이다(ADR-0057 §3.1) — 이 함수 자체는 어느
    source 문자열이든 정규화·검증만 한다(`--slot` CLI 인자 자체를 직접 받지 않는다).

    우선순위:
      1. `slot_arg` 명시(빈 문자열 포함) → `_normalize_slot`(main 의 명시 --repo/--slot 조립·
         권장 경로).
      2. cwd 가 슬롯 worktree 안이면 그 슬롯(`_slot_from_cwd`).
      3. 세션(`_default_session`·env/local.conf 유입)이 보유한 leased 슬롯 — 정확히 1개면 그것.

    3에서 매칭 leased 슬롯이 0개(무바인딩)이거나 ≥2(모호)면 `SlotResolutionError` raise — CLI
    main 이 rc 1 + `--repo/--slot` 안내로 surface 한다(침묵 오타깃 금지). `list_leases`(flock
    read)·`_default_session`(board.session_name 동형)을 재사용한다(기존 관례). (인자 전무 시
    이 no-flag 체인은 ADR-0040 불변 — ADR-0057 은 명시 인자 표면만 바꾼다.)

    **형식 검증(must-fix 2·T-0277)**: `slot_arg` 명시는 `_normalize_slot` 이, 그 외 유입도 반환
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
    mine = [l for l in list_leases() if l.state == "leased" and l.session == sess]
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
    """공용 정체성 모듈 `identity_args.py` 를 로드한다 (T-0318·T-0322 채택·ADR-0057 결정 5).

    `__file__` 기준(스크립트-위치 앵커) — `REPO` 전역이 아니라 이 파일 자신의 실제 디스크 경로로
    해석한다. 테스트가 `_load_wp_bound` 로 이 모듈을 로드한 뒤 `REPO`/`LEASES_FILE` 등 전역을
    tmp 경로로 재배선해도(hermetic), `identity_args.py` 는 항상 이 파일과 같은 tools/ 디렉토리에
    물리적으로 있으므로 `__file__` 앵커는 재배선 영향을 받지 않는다(스크립트+테스트 양쪽 동작).

    다른 도구의 sibling 로더(`pm_config._load_module`·`pm_bootstrap._load_worktree_pool`)와
    동형 관용구 — 이 도구가 `identity_args` 를 import 하는 새 coupling 은 ADR-0013 격리 예외로
    T-0322 가 이미 승인(리스 IO 층은 `worktree_pool` 을 되-import 하지 않는 단방향 관계).
    """
    import importlib.util
    path = Path(__file__).resolve().parent / "identity_args.py"
    spec = importlib.util.spec_from_file_location("identity_args", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_actor_slot_for_repo(repo: str) -> str:
    """`--repo` 단독(슬롯 무) actor 해소 — 공용 `identity_args.resolve_actor_slot` 위임 (T-0318·
    ADR-0057 결정 3).

    dev/sync 는 실 git side-effect 를 내는 actor 연산이라 claim/finish 등과 동일 규칙을 따른다:
    그 repo 의 활성(leased) 슬롯이 정확히 1개면 자동 해소, 0개/≥2개는 fail-loud(`--slot <N>`
    명시 안내). 로컬 리스 읽기를 재구현하지 않는다(B-1·T-0322 결정) — `identity_args` 가 리스
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


# ── set-base / status CLI 핸들러 (위치인자 <slot> — pool-management op·명시 슬롯·spike §F9/F10) ──
# dev/sync 의 --repo/--slot identity 와 달리 대상 슬롯을 **위치인자**로 직접 받는다: set-base·status·
# (wave-2d rebase)는 자기 세션 슬롯이 아닌 임의 슬롯도 관리 대상이라(pool 관리·결정 ⑪) 슬롯을 명시
# 지정한다. status 는 위치인자 생략 시 cwd/세션 leased 로 해소(무인자=내 슬롯).


def _cmd_set_base(args) -> int:
    """`set-base <slot> <branch>[@<commit>]` CLI 핸들러 — 기준점 사용자 명시 기록 (결정 ⑪·T-0352).

    자동 추론 없이 사용자가 지정한 base 를 `set_base`(→ T-0350 write)로 기록한다. 슬롯 형식 오류·
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
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부(⑬·T-0358).
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
          "— 이제부터 부트스트랩 0단계 drift 감지가 이 기준으로 작동한다(결정 ⑪).")
    return 0


def _print_status_row(row: dict) -> None:
    """슬롯 git 구성 dict 한 줄 렌더(base·branch·head·N behind·submodule pin/drift·dirty·T-0359).

    미기록 base 는 `-`(계산 불가·이유·추론 금지·⑪). submodule 은 역할별 경고 마크(⚠=drift/
    uninitialized··=pinned/dev-ahead·T-0276)로 표시(빈 목록=submodule 없는 슬롯 → 줄 생략)."""
    slot = row["slot"]
    base = row.get("base")
    base_str = (f"{base.get('branch')}@{(base.get('commit') or '?')[:12]}"
                if base and base.get("branch") else "(미기록)")
    behind = row.get("behind")
    print(f"# 슬롯 {slot} git 구성 (조회 — 손-git 불요·spike §F10)")
    print(f"  role:   {row.get('role') or _slot_role(slot)}")   # work | readonly (⑬·T-0358)
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
    """`status [<slot>] [--task <이름>]` CLI 핸들러 — 슬롯 git 구성 조회 단일/일괄 (T-0352/T-0359·§F10).

    대상: `--task <이름>`(그 task 보유 전 슬롯 일괄) > 위치인자 `<slot>`(단일) > 무인자(내 task 전
    슬롯·`_default_session` 유입). 슬롯별 base·branch·head·base 대비 N behind(미기록=`-`·추론 금지·⑪)
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
            rows = status()   # 무인자 = 내 task 전 슬롯(spike §F10).
    except SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("# 조회 대상 슬롯 없음 — 내 세션이 보유한 leased 슬롯이 없다 "
              "(`--task <이름>` 또는 `<slot>` 으로 대상을 명시하라).")
        return 0
    for i, row in enumerate(rows):
        if i:
            print()   # 슬롯 간 구분 공백(일괄).
        _print_status_row(row)
    return 0


def _rebase_skip_reason(reason: "str | None") -> str:
    """rebase 스킵 사유 코드 → 사람이 읽는 loud 설명 (CLI·T-0359)."""
    if reason and reason.startswith("not-owner:"):
        holder = reason.split(":", 1)[1]
        return f"세션 {holder!r} 이(가) 보유 — 내 슬롯 아님(rebase 차단·소유검사)"
    return {
        "readonly": "readonly 공유 슬롯 — mutation 불가(refresh 로만 갱신·⑬)",
        "not-owner": "내 세션 소유(leased) 슬롯이 아니다 — 남의/미점유 슬롯 rebase 차단",
        "dirty": "미커밋 변경(dirty) — rebase 는 clean 전제(정리 후 재시도)",
        "in-progress": "이미 rebase 진행 중 — `git rebase --continue|--abort` 로 먼저 해소",
        "no-base": "기준점(base) 미기록 + --onto 없음 — `set-base` 지정 또는 `--onto <branch>`(추론 금지·⑪)",
        "unresolvable-onto": "`--onto` ref 해소 실패(오타·미fetch — 실재 브랜치/커밋 지정)",
        "no-worktree": "슬롯 worktree 경로 부재",
    }.get(reason or "", reason or "미상")


def _cmd_rebase(args) -> int:
    """`rebase <slot> [--onto <b>]` (단일) · `rebase --task <이름> [--onto <b>]` (일괄) CLI 핸들러 (⑩·T-0359·§F10).

    슬롯 독립 처리 — 선-검사(소유/dirty/rebase 진행중) 스킵 + 충돌 그대로 fail-loud(엔진 abort 안
    함) + 성공 시 장부 원자 갱신. 끝에 성공/스킵/충돌 요약. 단일은 성공해야 rc 0(스킵/충돌=rc 1),
    일괄은 충돌이 있으면 rc 1(주의 필요·나머지는 독립 진행).

    ⚠️ **선행조건(⑳)**: 활성 백그라운드 위임(dev) 중인 슬롯은 rebase 하지 마라 — 서브에이전트는
    하네스 안 프로세스라 엔진이 못 본다(기계 신호 부재·[[parallel-dev-shared-tree-clobber]] 변형).
    스킬/카드에 명문화·실행 전 사용자 확인."""
    task = args.task
    slot_arg = args.slot
    if task and slot_arg:
        print("[중단] rebase 는 `<slot>`(단일) 또는 `--task <이름>`(일괄) 중 하나만 받는다.",
              file=sys.stderr)
        return 1
    if not task and not slot_arg:
        print("[중단] rebase 대상을 지정하라 — `<slot>`(단일) 또는 `--task <이름>`(일괄).",
              file=sys.stderr)
        return 1
    batch = bool(task)
    if batch:
        slots = [l.slot for l in slots_for_task(task)]
        if not slots:
            print(f"# task {task!r} 이(가) 보유한 leased 슬롯이 없다 — rebase 대상 0.")
            return 0
    else:
        try:
            slots = [_normalize_slot(slot_arg)]
        except SlotResolutionError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1

    results = rebase(slots, onto=args.onto)
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
          "(일괄=슬롯 독립·한 충돌이 나머지를 안 막음·§F10).")
    if batch:
        return 1 if n_conflict else 0
    return 0 if n_ok == 1 else 1   # 단일 — 성공해야 rc 0(스킵/충돌=요청 미수행·rc 1).


def _cmd_refresh(args) -> int:
    """`refresh <slot> [--onto <branch>]` CLI 핸들러 — readonly 슬롯 갱신 (⑬·T-0358·§F11).

    fetch → detached HEAD 를 기준(onto 또는 기록된 base.branch) 최신 tip 으로 이동한다. dirty(누군가
    씀·신호)·미readonly·base 미해소는 rc 1 로 명시 실패(`RefreshRefused`·조용히 reset 안 함)."""
    try:
        slot = _normalize_slot(args.slot)
    except SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        ref = refresh(slot, onto=args.onto)
    except RefreshRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print(f"✓ 슬롯 {slot} refresh: detached HEAD → {ref} 최신 tip 으로 이동(fetch→detach·⑬·T-0358).")
    return 0


def main(argv: "list[str] | None" = None) -> int:
    """argparse 진입점 — pm-worktree 스킬이 래핑할 `dev`/`sync`/`set-base`/`status` 커맨드 (ADR-0049
    파일럿 T-γ·T-0277·ADR-0057 결정 5·T-0318·T-0352).

    라이브러리 모듈에 얇은 CLI 를 얹는다(`if __name__ == "__main__"` 가드로 import 안전 — 다른
    도구의 `spec_from_file_location` import 를 안 깬다). 스킬이
    `python3 .project_manager/tools/worktree_pool.py dev <sub> <branch> [--repo <name> [--slot <N>]]` /
    `... sync [--repo <name> [--slot <N>]]` / `... set-base <slot> <branch>[@<commit>]` /
    `... status [<slot>]` 로 부른다. CLI 는 **실경로 wiring**(git_runner 미주입)이고, 함수 레벨 DI
    seam(`dev`/`sync`/`set_base`/`slot_git_status` 의 git_runner)은 테스트가 쓴다. 사람이 읽는
    stdout(무엇을 했는지)·skip/경고 사유는 backbone 이 stderr·실패는 rc 1 + 메시지.

    **두 인자 표면**: `dev`/`sync` 는 --repo/--slot identity(자기 세션 슬롯 대상)·`set-base`/`status`
    는 위치인자 `<slot>`(임의 슬롯 pool 관리·결정 ⑪). set-base/status 는 identity 파싱 전에 분기
    처리한다(그 args 표면 없음).

    정체성 인자는 공용 `identity_args`(`add_identity_args`·`parse_identity`)로 통일한다(ADR-0057·
    T-0322 채택) — 구 bare `--slot <slot-id>`(전체 슬롯 문자열)는 제거하고 분해형 `--repo <name>
    [--slot <N>]` 만 받는다. `parse_identity` 의 discriminated `kind` 로 해소 경로가 갈린다:
      - `kind="slot"`(`--repo`+`--slot` 모두 명시) → `<repo>_<N>` 조립 후 `_resolve_current_slot`
        (기존 명시-슬롯 정규화/검증 경로 재사용).
      - `kind="repo"`(`--repo` 단독) → actor 해소(`_resolve_actor_slot_for_repo` — 활성 슬롯 1개면
        해소·0개/≥2개 fail-loud). bare `--slot`(`--repo` 없이)은 `parse_identity` 가 `ValueError`
        로 fail-loud(DoD).
      - `kind="none"`(인자 전무) → 기존 no-flag 체인(`_resolve_current_slot(None)`·cwd→세션
        leased·ADR-0040 불변).
    """
    ia = _load_identity_args()
    parser = argparse.ArgumentParser(
        prog="worktree_pool.py",
        description="worktree/submodule 운영중 관리 backbone (dev/sync·ADR-0049/0051 파일럿 T-γ).",
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
        help="슬롯 기준점(base)을 사용자 명시로 기록(추론 금지·결정 ⑪) → 이후 drift 감지 작동.")
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

    # rebase <slot> [--onto <b>] (단일) · rebase --task <이름> [--onto <b>] (일괄) — 위치인자/pool 관리.
    p_rebase = subparsers.add_parser(
        "rebase",
        help="슬롯 base 를 사용자 명시로 rebase(단일/일괄·선-검사·충돌 그대로+loud·장부 원자 갱신·⑩).")
    p_rebase.add_argument("slot", nargs="?", default=None,
                          help="대상 슬롯(단일·`--task` 와 배타).")
    p_rebase.add_argument("--task", default=None,
                          help="그 task 보유 전 슬롯 일괄 rebase(`<slot>` 과 배타).")
    p_rebase.add_argument("--onto", default=None,
                          help="rebase 기준 브랜치(생략 시 기록된 base.branch 최신·미기록이면 거부·⑪).")

    # refresh <slot> [--onto <branch>] — readonly 공유 슬롯 갱신(⑬·T-0358·§F11·위치인자 <slot>).
    p_refresh = subparsers.add_parser(
        "refresh",
        help="readonly 공유 슬롯을 released 최신으로 갱신(fetch→detach 이동·dirty=거부+loud·⑬).")
    p_refresh.add_argument("slot", help="대상 readonly 슬롯(`work/<repo>_<N>` 또는 접두 생략 `<repo>_<N>`).")
    p_refresh.add_argument("--onto", default=None,
                           help="갱신 기준 브랜치(생략 시 기록된 base.branch·둘 다 없으면 거부).")

    args = parser.parse_args(argv)

    # set-base / status / rebase / refresh — 위치인자 <slot> 경로(identity 파싱 미진입·pool 관리·spike §F9/F10/F11).
    if args.command == "set-base":
        return _cmd_set_base(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "rebase":
        return _cmd_rebase(args)
    if args.command == "refresh":
        return _cmd_refresh(args)

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
        else:  # kind == "none" — 인자 전무, 기존 no-flag 체인(ADR-0040 불변).
            slot = _resolve_current_slot(None)
    except SlotResolutionError as exc:
        print(f"[중단] 대상 슬롯 해소 실패 — {exc}", file=sys.stderr)
        return 1

    if args.command == "dev":
        try:
            rc, out = dev(slot, args.submodule, args.branch)
        except ReadonlySlotMutation as exc:
            print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부(⑬·T-0358).
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
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — mutation 거부(⑬·T-0358).
        return 1
    print(f"✓ 슬롯 {slot} 재동기 완료 (skip/경고 사유는 위 stderr 참조).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
