#!/usr/bin/env python3
"""Ticket board CLI — multi-session development coordination.

Atomic claim via POSIX rename(2). Tickets live as markdown files in
`.project_manager/wiki/tickets/{open,claimed,blocked,done}/`. Each command
updates `.project_manager/wiki/board.md` automatically.

`board.py idea …` manages pre-ADR ideas under
`.project_manager/wiki/ideas/{open,promoted,killed}/` with the same
atomic-rename + frontmatter-sync mechanics (see the `idea` subcommand group).

See `.project_manager/wiki/tickets/README.md` and
`.project_manager/wiki/ideas/README.md` for the workflows.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import functools
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

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
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
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

REPO = Path(__file__).resolve().parents[2]
IDEAS_DIR = REPO / ".project_manager" / "wiki" / "ideas"
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"
SPECS_DIR = REPO / ".project_manager" / "wiki" / "specs"
ARCHITECTURE_FILE = REPO / ".project_manager" / "wiki" / "architecture.md"  # 현재-아키텍처 단일 진실 (freshness lint 비교 대상)
HOOKS_DIR = REPO / ".project_manager" / "hooks"  # instance-owned lint hooks
BOARD_FILE = REPO / ".project_manager" / "wiki" / "board.md"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
STATUS_FILE = REPO / ".project_manager" / "wiki" / "status.md"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored): py·test_cmd·ctx_* + solo-legacy prefix/session (multi 홈은 유도)


# ── board root (graceful 탐지) ───────────────────────────────
# board(tickets+areas)는 `.project_manager/board/`(submodule)로 분리될 수 있다 — 그러면
# git 형상이 design(superproject)/board(submodule) 둘로 갈려 PM 운영 commit 이 코드 git 을
# 오염하지 않는다. 분리되지 않은 legacy(솔로·미마이그 adopter)에선 board 가
# wiki/ 안에 그대로 산다. 아래 board_root() 가 *실측*으로 둘을 가른다 — board/tickets 가
# 실제 dir 이면 board/ 루트, 아니면 wiki/ 루트(legacy). install_pre_push_hook 의 git-path
# 탐지 패턴 동형(존재할 때만 새 경로·없으면 현 위치).
#
# board-관련 경로(tickets·_template·areas)는 *상수가 아니라 함수*로 lazy 해소한다 — board/
# 존재 여부가 런타임(설치/마이그레이션)에 바뀔 수 있고, hermetic 테스트가 REPO 를 monkeypatch
# 한 뒤 board_root 가 그 tmp REPO 를 따라야 하기 때문(import-time 상수면 실 REPO 에 굳음).
# 나머지 wiki 잔류 경로(ideas/board.md/decisions/specs/architecture/log/status)는 board 와
# 물리 위치는 다르지만 같은 PM 홈 소유 입력이다. 등록 worktree read 폴백에서는 아래
# `_read_pm_inputs_at()`가 board와 함께 이 경로들을 한 홈으로 재앵커한다.

# read dispatch 안에서만 설정되는 PM 입력 override. 등록 worktree의 조회가 소유 PM 홈
# board·wiki·hooks·로컬 PM 설정을 읽을 때 사용하며, main()이 context manager로 즉시
# 되돌린다. mutation/sidecar와 direct import 호출은 항상 None이라 기존 자기 REPO 해소를
# 유지한다.
_READ_BOARD_ROOT_OVERRIDE: Path | None = None
_READ_PM_HOME_OVERRIDE: Path | None = None


def _board_root_at(repo: Path) -> Path:
    """`repo`가 직접 소유한 board 루트를 graceful 규칙 하나로 해소한다."""
    base = repo / ".project_manager"
    if (base / "board" / "tickets").is_dir():
        return base / "board"
    return base / "wiki"


def board_root() -> Path:
    """board(tickets+areas) 루트 — board/ 분리 시 `<REPO>/.project_manager/board`, 아니면
    legacy `<REPO>/.project_manager/wiki`.

    등록 worktree에서 실행된 read dispatch가 단일 소유 PM 홈을 장부로 확정한 동안에는
    그 홈의 board 루트를 반환한다. override는 read 명령 실행 구간에만 유효하므로 mutation/
    sidecar 게이트를 우회하지 않는다.

    `.project_manager/board/tickets` 가 실 디렉토리면 board 가 submodule 로 분리된
    형상 → board/ 루트. 아니면 board 가 아직 wiki/ 안에 있는 legacy 형상 →
    wiki/ 루트(현 위치·무변경). install_pre_push_hook 의 디렉토리-탐지와 동형 — *존재할
    때만* 새 경로로 갈리고, 없으면 현재 위치로 100% 폴백한다(솔로·미마이그 무영향).
    """
    if _READ_BOARD_ROOT_OVERRIDE is not None:
        return _READ_BOARD_ROOT_OVERRIDE
    return _board_root_at(REPO)


def tickets_dir() -> Path:
    """ticket 디렉토리 — board_root()/tickets (board/ 분리 추종·legacy=wiki/tickets)."""
    return board_root() / "tickets"


def template_file() -> Path:
    """ticket 본문 템플릿 — tickets_dir()/_template.md (board_root 추종)."""
    return tickets_dir() / "_template.md"


def drafts_dir() -> Path:
    """미충전 draft 격리 디렉토리 — tickets_dir()/.drafts (board_root 추종).

    `board.py new` 가 board-git 활성(공유) 상태에서 placeholder/thin 본문을 감지하면
    티켓을 `tickets/open/` 이 아니라 이 디렉토리에 둔다 — STATUS_DIRS(`open/claimed/
    blocked/done`) *밖*이라 STATUS_DIRS 를 순회하는 어떤 mutation(`_board_git_stage_and_commit`
    의 `git add -A`·`_board_git_status_porcelain` 의 `git status --porcelain`·list·lint 등)도
    draft 를 보지 못한다 — 다음 mutation 이 무관 draft 를 실수로 board-git 에 커밋하는 leak
    을 원천 차단한다.
    `_BOARD_GIT_DRAFT_PATHSPEC` 로 board-git 호출에서도 이중으로 명시 제외한다(방어적 이중화).
    본문을 채운 뒤 `board.py promote <id>` 가 `open/` 으로 이동 + board-git 커밋한다.
    """
    return tickets_dir() / ".drafts"


def areas_file() -> Path:
    """areas 레지스트리 경로 (board_root 추종·*조건분기*).

    areas.md 는 legacy 에서 `.project_manager/areas.md`(wiki *밖*·committed shared registry)에
    산다 — tickets 처럼 wiki/ 안이 아니다. board/ 분리 시엔 board submodule *안*으로 옮겨야
    PM 운영(repo add 가 append)이 코드 git 을 오염하지 않는다. 그래서:
      - board/ 존재 → `board_root()/areas.md` (= board/areas.md·submodule 안)
      - legacy     → `<REPO>/.project_manager/areas.md` (현 위치·wiki 밖·무변경)
    """
    root = board_root()
    if root.name == "board" and (root / "tickets").is_dir():
        return root / "areas.md"
    return root.parent / "areas.md"


# board-관련 경로의 module-level 별칭 — 위 함수가 *실제* 해소 경로다(board_root 추종). 이
# 상수들은 (1) hermetic 테스트의 monkeypatch seam(`setattr(board, "TICKETS_DIR", …)` 가
# AttributeError 없이 동작)과 (2) 외부 import 안전(import-time 평가)을 위해 legacy 기본값으로
# 유지한다. 내부 코드는 *함수*를 부르므로(board_root 추종·아래), 이 상수가 가리키는 legacy
# 경로는 board/ 미분리 시점에 함수 결과와 동일하다 — board/ 분리 후에도 함수가 우선이라
# 회귀 없음. (테스트가 이들을 patch 할 땐 REPO 도 함께 patch 하고 그 값이 legacy 와 일치 →
# 함수가 같은 경로를 낸다.)
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"
TEMPLATE_FILE = TICKETS_DIR / "_template.md"
AREAS_FILE = REPO / ".project_manager" / "areas.md"    # shared registry (committed, merge=union)
PM_STATE_FILE = REPO / ".project_manager" / "wiki" / "pm_state.md"          # per-clone (git-ignored)
PM_STATE_TEMPLATE = REPO / ".project_manager" / "wiki" / "pm_state.template.md"  # tracked skeleton
LOCAL_DIR = REPO / ".project_manager" / ".local"            # per-clone scratch (git-ignored)
REGRESSION_FLAG = LOCAL_DIR / "regression.json"             # last regression result, keyed by HEAD
LIVEGATE_FLAG = LOCAL_DIR / "livegate.json"                 # last release live-gate result, keyed by worktree HEAD (per-clone)
BOARD_LOCK = LOCAL_DIR / "board.lock"                       # OS file lock — board write serialization
# worktree_pool 의 LEASES_FILE 와 *같은 위치*(그 관례 — `.local/worktree-leases.json`). board 는
# worktree_pool 을 import 하지 않으므로 경로를 자체 해소해 파일을
# 직접 read 한다(슬롯 test_cmd 레이어·아래 _active_slot_test_cmd). areas.md 읽듯 데이터-결합만.
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"            # worktree 리스 장부 (read-only here)
DOMAIN_PY = REPO / ".project_manager" / "tools" / "domain.py"  # domain lint deep-import (순환 회피·아래 lint_domain)
PM_DELEGATE_PY = REPO / ".project_manager" / "tools" / "pm_delegate.py"  # delegate lint deep-import (아래 lint_delegate)
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")
# Ideas have a simpler lifecycle than tickets — no claim/complete middle
# states, just `open → promoted|killed`.
IDEA_STATUS_DIRS: tuple[str, ...] = ("open", "promoted", "killed")


# ── PM-홈 worktree 오실행 가드 (mutation dispatch 게이트) ───────────────
# board 계열 도구를 PM 홈의 등록 worktree(`work/<repo>_<N>`) cwd 에서 실행하면 도구가 cwd
# 기준 자기-앵커(REPO)로 그 worktree 트리에 조용히 착지해 stray 산출을 낸다 — `board.py new`
# 가 잘못된 ID 네임스페이스의 `T-0001` 을, `ticket_finish` 가 stray `wiki/log/current.md` 를
# 만든다. worktree는 코드 전용이고 board 는 PM 홈이 소유하기
# 때문이다. 이 silent-misanchor 클래스를 **fail-loud** 로 폐쇄한다 — mutation 명령이
# 착지 *전에* 실제 PM 홈 경로를 안내하며 중단한다. 자동 재앵커(silent redirect)는 택하지
# 않았다: 오탐 시 *다른* board 에 조용히 쓰는 더 나쁜 silent 결과가 되고, 두-git 경계를
# 가로지르는 board-git sync/counter 재해소가 취약하다. fail-loud 는 최소·안전하며 
# (silent 자동화보다 명시)·livegate seam의 동형 규율이다.
#
# **게이트 = mutation subcommand 전수·단일 dispatch 지점**(main()). 개별 cmd 배선 대신 아래
# 분류 상수(`_MUTATION_SUBCOMMANDS`)로 dispatch 에서 한 번 판정한다 — 신규 mutation subcommand
# 추가 시 누락되는 클래스는 메타 가드 테스트(`test_board_worktree_misanchor_guard`·분류 상수 vs
# 실 등록 subcommand 전수 대조)가 잡는다. 읽기-경로·anchor-keyed sidecar(regression·livegate —
# worktree 에서 도는 게 설계 의도·`.local` sidecar 만 씀)·솔로/standalone(worktree 형상 아님)은
# 무영향(감지 실패 시 현행 동작·오탐 0·fail-soft). `_worktree_cwd`/`_auto_slot`(②에서 ①찾기·
# 회귀 cwd 해소)의 *역방향*: 여기선 ①에서 실행된 걸 잡는다.
#
# 분류 원칙: **PM 홈 소유 상태를 쓰는 명령**(티켓 상태전이·생성·ID/prefix relabel·idea·wiki
# family-scope retag·clone init·파생 board.md 재생성)은 게이트. **조회(read)** 와 **anchor-keyed
# sidecar**(regression/livegate·설계상 worktree 실행)는 비게이트. 각 항목 근거는 아래 상수 주석.

# board 상태(티켓/idea/prefix/id/areas/wiki-scope/파생 board.md)를 쓰거나 clone 을 init 하는
# mutation subcommand — dispatch 게이트가 이 집합에만 worktree 오실행 가드를 적용한다.
# (idea/prefix 서브그룹은 `<group> <sub>` 점표기.) reid=ID 재부여·promote-scope=wiki family_scope
# retag·refresh=파생 board.md 재생성(worktree refresh 는 정당 사용 없음·stray dashboard 방지)·
# verified-at-backfill/repin=PM 홈 현재-진실 문서(architecture/status/domain) frontmatter 쓰기(
# worktree 엔 그 문서가 없어 no-op 이나 PM 홈 실행이 설계 의도라 오실행 가드).
_MUTATION_SUBCOMMANDS: frozenset[str] = frozenset({
    "new", "promote", "claim", "complete", "block", "unclaim", "unblock",
    "init", "migrate-identity", "promote-scope", "reid", "refresh",
    "verified-at-backfill", "verified-at-repin",
    "idea new", "idea promote", "idea kill",
    "prefix rename", "prefix strip", "prefix merge", "prefix delete",
})
# 조회(read-only·board 상태 미변경) — 게이트 없음.
_READ_SUBCOMMANDS: frozenset[str] = frozenset({
    "list", "show", "lint", "idea list", "prefix list",
})
# read leaf별 PM-owned 입력 전수. dispatch 첫 줄이 이 값을 그대로 표시하므로, worktree에서
# 어느 홈의 무엇을 읽는지 조용히 숨지 않는다. 제품 코드(REPO 루트 문서·adapter/template 및
# git freshness 대상)는 현재 worktree를 계속 읽는다. PM 운영 상태만 단일 소유 홈으로 묶는다.
_READ_PM_INPUTS_BY_SUBCOMMAND: dict[str, str] = {
    "list": "board·areas·local.conf·lease",
    "show": "board",
    "lint": "board·areas·wiki(decisions·ideas·specs·domain·current-truth)·hooks·local.conf",
    "idea list": "ideas",
    "prefix list": "board",
}
# anchor-keyed sidecar — board 상태 아님. regression=`.local/regression.json`, livegate=
# 공유 engine-root `livegate.json`(둘 다 anchor HEAD 로 키·**worktree cwd 실행이 설계 의도**).
# 게이트하면 릴리즈/회귀 흐름이 깨진다— 비게이트.
_SIDECAR_SUBCOMMANDS: frozenset[str] = frozenset({
    "regression", "livegate", "git-anchor",
})

# raw git mutation 가드도 board dispatch의 mutation 분류와 같은 원칙(한 표 → 모든 소비처)을 쓴다.
# 하네스별 훅은 이 표를 복제하지 않고 ``judge_git_anchor``/``judge_git_anchor_command``만 호출한다.
_GIT_MUTATION_SUBCOMMANDS: frozenset[str] = frozenset({
    "add", "stage", "commit", "push", "checkout", "restore", "reset",
    "rm", "mv", "clean", "apply", "stash",
})
_GIT_ANCHOR_DENY_ROOTS: frozenset[str] = frozenset({"tests", "templates"})
_GIT_COMMAND_PREFILTER = re.compile(r"(?<![A-Za-z0-9_.-])['\"]?git['\"]?(?=\s|$|[<>])")


class _GitInvocation(NamedTuple):
    """정규화한 git 호출. ``cwd``는 global ``-C``를 적용한 실제 명령 앵커다."""

    cwd: Path
    subcommand: str
    args: tuple[str, ...]
    anchor_certain: bool


def _git_invocation(cwd: str | os.PathLike[str], argv: Sequence[str]) -> _GitInvocation | None:
    """git argv에서 global option/``-C``를 걷고 실제 subcommand를 해소한다.

    모르는 global option은 보수적으로 한 토큰 option으로 취급한다. 판정불가 호출을 deny로
    올리지 않는 것이 계약이라, 애매하면 뒤의 첫 non-option을 subcommand로 삼고 결국 warn/ok로
    흐른다. ``git -C``는 cwd 오인 판정의 일부라 반복 지정도 순서대로 적용한다.
    """
    tokens = [str(value) for value in argv]
    if not tokens:
        return None
    try:
        start = next(i for i, token in enumerate(tokens) if Path(token).name == "git")
    except StopIteration:
        return None
    anchor = Path(cwd).expanduser()
    anchor_certain = not any(
        token.startswith(("GIT_WORK_TREE=", "GIT_DIR=")) for token in tokens[:start]
    )
    i = start + 1
    value_options = {"-c", "--config-env", "--git-dir", "--work-tree", "--namespace"}
    while i < len(tokens):
        token = tokens[i]
        if token == "-C":
            if i + 1 >= len(tokens):
                return _GitInvocation(anchor, "", tuple(), anchor_certain)
            target = Path(tokens[i + 1]).expanduser()
            anchor = target if target.is_absolute() else anchor / target
            i += 2
            continue
        if token in value_options:
            if token in {"--git-dir", "--work-tree"}:
                anchor_certain = False
            i += 2
            continue
        if token.startswith("--git-dir=") or token.startswith("--work-tree=") or token.startswith("--namespace="):
            if token.startswith(("--git-dir=", "--work-tree=")):
                anchor_certain = False
            i += 1
            continue
        if token == "--":
            i += 1
            break
        if token.startswith("-"):
            i += 1
            continue
        break
    if i >= len(tokens):
        return _GitInvocation(anchor.resolve(), "", tuple(), anchor_certain)
    return _GitInvocation(anchor.resolve(), tokens[i], tuple(tokens[i + 1:]), anchor_certain)


def _git_operand_pathspecs(
    args: Sequence[str], *, value_options: frozenset[str],
    short_value_options: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """option 값을 건너뛰고 명시 operand만 반환한다.

    ``--pathspec-from-file``은 파일 *내용*이 pathspec이고 옵션 값은 그 파일 경로일 뿐이다. 훅이
    파일 내용을 읽어 shell 실행 시점 의미를 재현하는 것은 TOCTOU라, 이 축은 통째로 fail-open한다.
    """
    values = list(args)
    if any(
        value in {"--pathspec-from-file", "--pathspec-file-nul"}
        or value.startswith("--pathspec-from-file=")
        for value in values
    ):
        return ()
    operands: list[str] = []
    i = 0
    while i < len(values):
        value = values[i]
        if value == "--":
            operands.extend(values[i + 1:])
            break
        if value in value_options or value in short_value_options:
            i += 2
            continue
        if any(value.startswith(option + "=") for option in value_options if option.startswith("--")):
            i += 1
            continue
        if value.startswith("-") and not value.startswith("--"):
            # Git은 short option을 묶을 수 있다(``git commit -am msg``). 묶음 안에 값 소비
            # option이 있고 그 option 뒤 suffix가 없으면 다음 토큰이 option 값이다.
            consuming = {option[1:] for option in short_value_options if len(option) == 2}
            cluster = value[1:]
            consume_next = any(
                char in consuming and position == len(cluster) - 1
                for position, char in enumerate(cluster)
            )
            i += 2 if consume_next else 1
            continue
        if any(value.startswith(option) and value != option for option in short_value_options):
            i += 1
            continue
        if value.startswith("-"):
            i += 1
            continue
        operands.append(value)
        i += 1
    return tuple(operands)


def _git_pathspecs(subcommand: str, args: Sequence[str]) -> tuple[str, ...]:
    """deny 판정에 쓸 명시 pathspec만 추출한다.

    checkout/reset은 ref와 path 문법이 겹쳐 ``--`` 뒤만 신뢰한다. add/stage·restore·commit은
    Git 문법상 non-option operand가 pathspec이므로 option arity를 걷어 ``--`` 없는 명시 경로도 받는다.
    """
    values = list(args)
    if subcommand == "apply":
        # operand는 target pathspec이 아니라 읽을 patch 파일이다.
        return ()
    if "--" in values:
        return tuple(values[values.index("--") + 1:])
    if subcommand in {"checkout", "reset", "push"}:
        return ()
    if subcommand in {"add", "stage"}:
        return _git_operand_pathspecs(
            values,
            value_options=frozenset({"--chmod", "--pathspec-from-file"}),
        )
    if subcommand in {"rm", "mv", "clean"}:
        return _git_operand_pathspecs(
            values,
            value_options=frozenset(
                {"--pathspec-from-file", "--exclude"} if subcommand == "clean"
                else {"--pathspec-from-file"}
            ),
            short_value_options=frozenset({"-e"}) if subcommand == "clean" else frozenset(),
        )
    if subcommand == "restore":
        return _git_operand_pathspecs(
            values,
            value_options=frozenset({"--source", "--pathspec-from-file"}),
            short_value_options=frozenset({"-s"}),
        )
    if subcommand == "commit":
        return _git_operand_pathspecs(
            values,
            value_options=frozenset({
                "--author", "--date", "--file", "--fixup", "--gpg-sign", "--message",
                "--pathspec-from-file", "--reedit-message", "--reuse-message", "--template",
                "--trailer",
            }),
            short_value_options=frozenset({"-C", "-F", "-S", "-c", "-m", "-t"}),
        )
    return ()


def _git_repo_root(anchor: Path, *, runner: Any = subprocess.run) -> Path | None:
    root = _git_rev_parse(anchor, "--show-toplevel", runner=runner)
    return Path(root).resolve() if root else None


def _registered_slot_paths(
    pm_home: Path, *, runner: Any = subprocess.run
) -> tuple[Path, ...]:
    """공용 slot naming + 기존 linked-worktree/misanchor seam을 모두 통과한 lease 경로만 반환."""
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ()
    rows = data.get("leases") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return ()
    repos = {
        row.get("repo") for row in rows
        if isinstance(row, dict) and isinstance(row.get("repo"), str) and row.get("repo")
    }
    work_root = (pm_home / "work").resolve()
    lexical_work_root = pm_home / "work"
    paths: list[Path] = []
    for repo in sorted(repos):
        # identity_args가 소유하는 `work/<repo>_<N>` + leased-state 판정을 재사용한다.
        numbers = identity_args.repo_slot_numbers(repo, ledger)
        for number in numbers or []:
            lexical = lexical_work_root / f"{repo}_{number}"
            # resolve 전에 lexical identity를 검증한다. 내부 alias도 resolve 후에는 정상 slot과
            # 같은 inode/path가 되어 장부 repo/name 위조를 숨기므로 work 조상·leaf symlink는 거부.
            if lexical_work_root.is_symlink() or lexical.is_symlink():
                continue
            path = lexical.resolve()
            try:
                relative = path.relative_to(work_root)
            except ValueError:
                continue
            # symlink가 work/ 밖을 가리키거나 중첩 경로가 되면 slot이 아니다.
            if len(relative.parts) != 1 or not path.is_dir():
                continue
            if not _is_linked_worktree(path, runner=runner):
                continue
            if _pm_home_worktree_misanchor(path, runner=runner) != pm_home:
                continue
            registered, _error = _ledger_registration(pm_home, path)
            if not registered:
                continue
            if path not in paths:
                paths.append(path)
    return tuple(paths)


def _git_anchor_identity(
    anchor: Path, *, runner: Any = subprocess.run
) -> tuple[str, Path | None, Path | None]:
    """(public identity, git root, owning PM home)를 기존 misanchor seam으로 해소한다."""
    root = _git_repo_root(anchor, runner=runner)
    if root is None:
        return "non-repo", None, None
    if _has_real_board(root / ".project_manager"):
        return "pm-home", root, root
    pm_home = _pm_home_worktree_misanchor(root, runner=runner)
    if pm_home is not None:
        registered_slots = _registered_slot_paths(pm_home, runner=runner)
        return ("slot" if root in registered_slots else "worktree"), root, pm_home
    return "foreign", root, None


def _dangerous_cross_repo_pathspecs(
    pm_home: Path, repo_root: Path, path_base: Path, pathspecs: Sequence[str],
    *, runner: Any = subprocess.run,
) -> tuple[str, ...]:
    """PM 홈과 등록 slot 양쪽에 실재하는 좁은 코드-root pathspec만 반환한다.

    존재하지 않는 path는 git 자체가 fail-loud하므로 deny하지 않는다. ``tests``/``templates``만
    후보인 이유는 PM 홈 부기 commit의 정상 pathspec(``.project_manager/wiki/...`` 등)을 막지 않고,
    실제 add-harness 오염 모드만 고정하기 위해서다.
    """
    slots = _registered_slot_paths(pm_home, runner=runner)
    if not slots:
        return ()
    dangerous: list[str] = []
    for raw in pathspecs:
        if not raw or any(ch in raw for ch in "*?["):
            continue
        literal = raw
        top_relative = False
        if literal.startswith(":(top)"):
            literal = literal[len(":(top)"):]
            top_relative = True
        elif literal.startswith(":/"):
            literal = literal[2:]
            top_relative = True
        elif literal.startswith(":("):
            # glob/exclude/attr 등 magic은 해석하지 않아 false-deny를 만들지 않는다.
            continue
        rel = Path(literal)
        if rel.is_absolute():
            try:
                rel = rel.resolve().relative_to(repo_root)
            except (OSError, ValueError):
                continue
        local = ((repo_root if top_relative else path_base) / rel).resolve()
        try:
            normalized = local.relative_to(repo_root)
        except ValueError:
            continue
        parts = normalized.parts
        if not parts or parts[0] not in _GIT_ANCHOR_DENY_ROOTS:
            continue
        if not local.exists():
            continue
        if any((slot / normalized).exists() for slot in slots):
            dangerous.append(normalized.as_posix())
    return tuple(dict.fromkeys(dangerous))


def judge_git_anchor(
    cwd: str, argv: list[str], *, runner: Any = subprocess.run
) -> dict[str, str]:
    """raw git 호출의 cwd 안전성을 판정하는 엔진·하네스 중립 seam.

    기본은 warn(정체 주입)이며 deny는 PM 홈에서 등록 slot과 교차 실재하는 코드-root pathspec을
    mutation으로 건드리는 실사고 패턴 하나뿐이다. 비-repo/foreign/판정불가는 정상 작업을 막지 않는다.
    """
    invocation = _git_invocation(cwd, argv)
    if invocation is None:
        return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "git 호출이 아님"}
    identity, repo_root, pm_home = _git_anchor_identity(invocation.cwd, runner=runner)
    subcommand = invocation.subcommand
    if subcommand not in _GIT_MUTATION_SUBCOMMANDS:
        return {
            "verdict": "ok", "cwd_identity": identity,
            "reason": f"git {subcommand or '<missing>'}는 mutation 가드 대상이 아님",
        }
    if not invocation.anchor_certain:
        return {
            "verdict": "warn", "cwd_identity": identity,
            "reason": "GIT_DIR/work-tree override로 실제 mutation anchor를 단일 증명할 수 없음",
        }
    if identity == "slot":
        return {
            "verdict": "ok", "cwd_identity": identity,
            "reason": f"등록된 활성 linked worktree 앵커={repo_root} — git {subcommand} 허용",
        }
    if identity == "pm-home" and repo_root is not None and pm_home is not None:
        dangerous = _dangerous_cross_repo_pathspecs(
            pm_home, repo_root, invocation.cwd,
            _git_pathspecs(subcommand, invocation.args), runner=runner,
        )
        if dangerous:
            joined = ", ".join(dangerous)
            return {
                "verdict": "deny", "cwd_identity": identity,
                "reason": (
                    f"PM 홈에서 등록 worktree와 양쪽에 실재하는 코드 경로({joined})를 "
                    f"git {subcommand} 하려 함 — 대상 worktree cwd에서 다시 실행하라"
                ),
            }
    shown = str(repo_root or invocation.cwd)
    return {
        "verdict": "warn", "cwd_identity": identity,
        "reason": f"git {subcommand} 실행 앵커={shown} (정체={identity}) — 의도한 repo인지 확인",
    }


class _ShellGitInvocation(NamedTuple):
    cwd: str
    argv: tuple[str, ...]
    certain: bool


class _ShellParseResult(NamedTuple):
    invocations: tuple[_ShellGitInvocation, ...]
    uncertain: bool
    reason: str


class _ShellGitCommand(NamedTuple):
    """wrapper를 걷어낸 git command word와 그 해석 확실성."""

    index: int
    certain: bool
    anchor_env_override: bool


def _shell_git_command(segment: Sequence[str]) -> _ShellGitCommand | None:
    """simple-command의 command position을 해소한다.

    quote fragment는 앞선 POSIX shlex가 이미 한 word로 결합한다. ``env``/``command``의
    지원 grammar는 정확히 소비하고, 미지원 option 뒤의 git 후보는 실행 여부가 불명확하므로
    ``certain=False``로 surface한다. 임의 일반 argv 안의 git은 여전히 데이터다.
    """
    values = list(segment)

    def _assignment(value: str) -> bool:
        return re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*=.*", value, flags=re.DOTALL,
        ) is not None

    def _is_git(value: str) -> bool:
        return Path(value).name == "git"

    def _uncertain_git(start: int, override: bool) -> _ShellGitCommand | None:
        for candidate in range(start, len(values)):
            if _is_git(values[candidate]):
                return _ShellGitCommand(candidate, False, override)
        return None

    index = 0
    anchor_env_override = False
    # POSIX assignment word는 command name 앞에 올 수 있다. 이후 임의 argv의 ``git``은
    # echo/printf 데이터일 뿐이므로 절대 실행으로 승격하지 않는다.
    while index < len(values) and _assignment(values[index]):
        anchor_env_override = anchor_env_override or values[index].startswith(
            ("GIT_WORK_TREE=", "GIT_DIR=")
        )
        index += 1

    # 실행 wrapper는 중첩 가능하다(``exec env -i command git …``). 각 grammar가 다음
    # command word까지 index를 전진시키고 같은 loop에서 다시 해소한다.
    while index < len(values):
        if _is_git(values[index]):
            return _ShellGitCommand(index, True, anchor_env_override)
        wrapper = Path(values[index]).name

        if wrapper == "env":
            index += 1
            option_phase = True
            while index < len(values):
                token = values[index]
                if option_phase and token == "--":
                    option_phase = False
                    index += 1
                    continue
                if _assignment(token):
                    anchor_env_override = anchor_env_override or token.startswith(
                        ("GIT_WORK_TREE=", "GIT_DIR=")
                    )
                    index += 1
                    continue
                if option_phase and token in {
                    "-i", "--ignore-environment", "-0", "--null", "--debug",
                }:
                    index += 1
                    continue
                if option_phase and token in {
                    "-u", "--unset", "-C", "--chdir", "-a", "--argv0",
                }:
                    if index + 1 >= len(values):
                        return None
                    # child cwd 변경은 현재 anchor와 같다고 증명할 수 없다.
                    if token in {"-C", "--chdir"}:
                        return _uncertain_git(index + 2, anchor_env_override)
                    index += 2
                    continue
                if option_phase and token.startswith(("--unset=", "--chdir=", "--argv0=")):
                    if token.startswith("--chdir="):
                        return _uncertain_git(index + 1, anchor_env_override)
                    index += 1
                    continue
                if option_phase and token.startswith("-"):
                    return _uncertain_git(index + 1, anchor_env_override)
                break
            continue

        if wrapper == "command":
            index += 1
            while index < len(values):
                token = values[index]
                if token in {"--", "-p"}:
                    index += 1
                    continue
                if token in {"-v", "-V"}:
                    return None
                if token.startswith("-"):
                    return _uncertain_git(index + 1, anchor_env_override)
                break
            continue

        if wrapper == "exec":
            index += 1
            while index < len(values):
                token = values[index]
                if token in {"--", "-c", "-l"}:
                    index += 1
                    continue
                if token == "-a":
                    if index + 1 >= len(values):
                        return None
                    index += 2
                    continue
                if token.startswith("-"):
                    return _uncertain_git(index + 1, anchor_env_override)
                break
            continue

        if wrapper == "nohup":
            index += 1
            if index < len(values) and values[index] == "--":
                index += 1
            elif index < len(values) and values[index].startswith("-"):
                return _uncertain_git(index + 1, anchor_env_override)
            continue

        if wrapper == "nice":
            index += 1
            while index < len(values):
                token = values[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-n", "--adjustment"}:
                    if index + 1 >= len(values):
                        return None
                    index += 2
                    continue
                if token.startswith("--adjustment=") or re.fullmatch(r"-\d+", token):
                    index += 1
                    continue
                if token.startswith("-"):
                    return _uncertain_git(index + 1, anchor_env_override)
                break
            continue

        if wrapper == "timeout":
            index += 1
            while index < len(values):
                token = values[index]
                if token == "--":
                    index += 1
                    break
                if token in {"-k", "--kill-after", "-s", "--signal"}:
                    if index + 1 >= len(values):
                        return None
                    index += 2
                    continue
                if token in {"--foreground", "--preserve-status", "--verbose"}:
                    index += 1
                    continue
                if token.startswith(("--kill-after=", "--signal=")):
                    index += 1
                    continue
                if token.startswith("-"):
                    return _uncertain_git(index + 1, anchor_env_override)
                break
            if index >= len(values):
                return None
            index += 1  # DURATION
            continue

        if wrapper == "sudo":
            index += 1
            if index < len(values) and values[index] == "--":
                index += 1
            elif index < len(values) and values[index].startswith("-"):
                # sudo option grammar는 플랫폼별로 넓고 option value가 command처럼 보일 수 있다.
                return _uncertain_git(index + 1, anchor_env_override)
            while index < len(values) and _assignment(values[index]):
                anchor_env_override = anchor_env_override or values[index].startswith(
                    ("GIT_WORK_TREE=", "GIT_DIR=")
                )
                index += 1
            continue

        if any(marker in values[index] for marker in ("$", "`")):
            # command word 자체가 parameter/command expansion이면 런타임에 exec류 wrapper가
            # 될 수 있다. 뒤의 git은 argv 데이터일 수도 있어 deny할 수 없지만, mutation 후보를
            # ok로 숨기지도 않고 uncertainty로 surface한다. echo/printf 등 명백한 command의
            # 일반 argv는 이 분기에 오지 않는다.
            return _uncertain_git(index + 1, anchor_env_override)

        return None
    return None


def _normalize_shell_line_continuations(command: str) -> str:
    """quote/heredoc data 밖의 backslash-newline만 POSIX continuation으로 접는다.

    heredoc body는 caller가 먼저 제거한다. quote 내부 bytes는 delimiter/data 의미를 보존하기
    위해 그대로 두며, command-word quote fragment 결합은 후속 shlex에 맡긴다.
    """
    output: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote is None and char == "\\":
            if index + 1 < len(command) and command[index + 1] == "\n":
                index += 2
                continue
            if command[index + 1:index + 3] == "\r\n":
                index += 3
                continue
            # quote 밖 escaped quote는 literal word byte다. 다음 loop에서 quote state를
            # 열면 뒤의 실제 continuation/git command를 quote data로 오인한다.
            output.append(char)
            if index + 1 < len(command):
                index += 1
                output.append(command[index])
            index += 1
            continue
        output.append(char)
        if quote is None and char in {"'", '"'}:
            quote = char
        elif quote == char:
            quote = None
        elif quote == '"' and char == "\\" and index + 1 < len(command):
            index += 1
            output.append(command[index])
        index += 1
    return "".join(output)


def _git_prefilter_text(command: str) -> str:
    """성능 선필터용 완화 text. quote fragment false-negative만 제거한다.

    실제 실행 여부는 반드시 shell parser가 판정한다. 따라서 quote 제거로 데이터가 후보가
    되어도 차단으로 승격되지 않는다.
    """
    return command.replace("'", "").replace('"', "")


def _strip_shell_comments(command: str) -> str:
    """quote/escape 밖 command-word 경계의 ``#`` comment를 newline은 보존해 제거한다."""
    output: list[str] = []
    quote: str | None = None
    escaped = False
    word_start = True
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            output.append(char)
            escaped = False
            word_start = False
            index += 1
            continue
        if quote is not None:
            output.append(char)
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                escaped = True
            index += 1
            continue
        if char == "\\":
            output.append(char)
            escaped = True
            index += 1
            continue
        if char in {"'", '"'}:
            output.append(char)
            quote = char
            word_start = False
            index += 1
            continue
        if char == "#" and word_start:
            while index < len(command) and command[index] != "\n":
                index += 1
            if index < len(command):
                output.append("\n")
                index += 1
            word_start = True
            continue
        output.append(char)
        word_start = char.isspace() or char in ";&|()"
        index += 1
    return "".join(output)


def _heredoc_declarations(line: str) -> tuple[list[tuple[str, bool]], bool]:
    """한 command line의 quote/comment 밖 실제 heredoc operator를 token 단위로 읽는다."""
    try:
        lexer = shlex.shlex(_strip_shell_comments(line), posix=True, punctuation_chars="<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except (TypeError, ValueError):
        return [], True
    found: list[tuple[str, bool]] = []
    unknown = False
    for index, token in enumerate(tokens):
        if token != "<<":
            continue
        if index + 1 >= len(tokens):
            unknown = True
            continue
        word = tokens[index + 1]
        strip_tabs = word.startswith("-")
        word = word[1:] if strip_tabs else word
        found.append((word, strip_tabs))
    return found, unknown


def _strip_shell_heredoc_bodies(command: str) -> tuple[str, bool]:
    """인식 가능한 heredoc body를 newline 자리만 남기고 제거한다.

    body의 ``git``은 데이터이지 실행 command가 아니다. delimiter를 끝까지 소비하지 못하거나
    ``<<``가 있는데 선언을 인식하지 못하면 uncertainty를 반환해 caller가 warn으로 surface한다.
    """
    lines = command.splitlines(keepends=True)
    output: list[str] = []
    pending: list[tuple[str, bool]] = []
    uncertain = False
    for line in lines:
        if pending:
            word, strip_tabs = pending[0]
            candidate = line.rstrip("\r\n")
            if strip_tabs:
                candidate = candidate.lstrip("\t")
            output.append("\n" if line.endswith(("\n", "\r")) else "")
            if candidate == word:
                pending.pop(0)
            continue
        declarations, unknown = _heredoc_declarations(line)
        uncertain = uncertain or unknown
        output.append(line)
        pending.extend(declarations)
    if pending:
        uncertain = True
    return "".join(output), uncertain


def _shell_segments(command: str) -> tuple[list[list[str]], list[str]] | None:
    """shell 문자열을 simple-command segment와 그 사이 operator로 나눈다."""
    try:
        lexer = shlex.shlex(
            _strip_shell_comments(command), posix=True, punctuation_chars=";&|\n<>",
        )
        lexer.whitespace_split = True
        # unquoted newline은 ``;``와 같은 순차 command 경계다. 기본 shlex whitespace로
        # 버리면 두 git 호출이 한 argv로 합쳐져 뒤 호출을 놓친다. quote 안 newline은 token에 남는다.
        lexer.whitespace = " \t\r"
        lexer.commenters = ""
        tokens = list(lexer)
    except (TypeError, ValueError):
        return None
    segments: list[list[str]] = []
    operators: list[str] = []
    current: list[str] = []
    for token in tokens:
        if token and set(token) == {"\n"}:
            token = ";"
        if token in {"&&", "||", ";", "|"}:
            segments.append(current)
            operators.append(token)
            current = []
        elif token and all(ch in ";&|\n" for ch in token):
            # case/and-or 확장 연산자 등 미지원 shell 문법은 해석하지 않는다(never-block).
            return None
        else:
            current.append(token)
    segments.append(current)
    return segments, operators


_SHELL_REDIRECTION_OPERATORS = frozenset({
    "<", ">", "<<", ">>", "<<<", "<>", ">|", "<&", ">&",
})


def _shell_without_redirections(segment: Sequence[str]) -> tuple[list[str], bool]:
    """simple-command argv에서 redirection operator와 target을 제거한다.

    redirection target은 command argv/pathspec이 아니다. fd prefix(``2>``)는 shlex에서 별도
    숫자 token으로 갈라지므로 operator 바로 앞 숫자도 제거한다. 복잡/불완전 redirection도
    target을 git pathspec으로 승격하지 않는 쪽으로 fail-open한다.
    """
    cleaned: list[str] = []
    redirected = False
    index = 0
    values = list(segment)
    while index < len(values):
        token = values[index]
        if token in _SHELL_REDIRECTION_OPERATORS:
            redirected = True
            if cleaned and cleaned[-1].isdigit():
                cleaned.pop()
            index += 2  # operator + redirection target
            continue
        cleaned.append(token)
        index += 1
    return cleaned, redirected


def _shell_gate(operator: str | None, status: bool | None) -> bool | None:
    if operator in {None, ";"}:
        return True
    if operator == "&&":
        return status
    if operator == "||":
        return None if status is None else not status
    return True


def _shell_simple_command(
    anchors: set[Path], cwd_certain: bool, segment: Sequence[str], execute: bool | None,
    *, globally_certain: bool,
) -> tuple[set[Path], bool, bool | None, list[_ShellGitInvocation]]:
    """simple command 하나의 cwd/status를 추상 실행한다. ``execute=None``은 조건부 실행이다."""
    if execute is False or not segment:
        return set(anchors), cwd_certain, None, []
    command, redirected = _shell_without_redirections(segment)
    if not command:
        return set(anchors), cwd_certain, None, []
    git_command = _shell_git_command(command)
    if git_command is not None:
        # 조건부 실행 여부와 cwd 확정성은 별개다. ``git status && git add …``에서
        # 후자는 실행될 수도 있지만 실행되는 경우의 cwd는 하나로 확정되므로 위험 pathspec을
        # 놓치지 않는다. false gate는 위에서 호출 자체를 제거한다.
        certain = (
            globally_certain and cwd_certain and len(anchors) == 1
            and git_command.certain and not git_command.anchor_env_override
        )
        calls = [
            _ShellGitInvocation(str(anchor), tuple(command[git_command.index:]), certain)
            for anchor in sorted(anchors, key=str)
        ]
        return set(anchors), cwd_certain, None, calls

    name = command[0]
    if name == "false":
        return set(anchors), cwd_certain, (False if execute is True else None), []
    if name == "true":
        return set(anchors), cwd_certain, (True if execute is True else None), []
    if name in {"echo", "printf", ":"}:
        # 셸 builtin의 정상 simple-command 형태는 status 0이다. 이 증명이 있어야
        # ``echo … && git …``의 실제 실행 경로를 불필요하게 uncertain으로 낮추지 않는다.
        return set(anchors), cwd_certain, (True if execute is True else None), []
    if name in {"pushd", "popd"}:
        # directory stack을 모델링하지 않는다. 실행 가능성이 있으면 이후 cwd는 unknown이며,
        # false gate로 실행되지 않은 경우만 함수 상단에서 certainty를 보존한다.
        return set(anchors), False, None, []
    if name != "cd":
        return set(anchors), cwd_certain, None, []

    operands = [value for value in command[1:] if value != "--"]
    if (redirected or len(operands) != 1 or operands[0].startswith("-")
            or any(ch in operands[0] for ch in "$`")):
        # cd가 실행될 수 있으나 지원 문법으로 target을 단일 증명하지 못했다.
        return set(anchors), False, None, []
    target_arg = Path(operands[0]).expanduser()
    successes: set[Path] = set()
    failures: set[Path] = set()
    for anchor in anchors:
        target = (target_arg if target_arg.is_absolute() else anchor / target_arg).resolve()
        (successes if target.is_dir() else failures).add(target if target.is_dir() else anchor)
    if execute is None:
        return set(anchors) | successes | failures, False, None, []
    if successes and not failures:
        return successes, cwd_certain, True, []
    if failures and not successes:
        return failures, cwd_certain, False, []
    return successes | failures, False, None, []


def _git_invocations_from_shell(
    cwd: str, command: str
) -> _ShellParseResult:
    """shell 제어연산과 ``cd``를 보수적으로 추상 실행해 git 호출 좌표를 반환한다.

    ``&&``/``||``는 직전 status, ``;``는 무조건 순차, ``|``는 각 pipeline command가 원래 cwd의
    subshell에서 실행되는 의미를 구분한다. ``true``/``false``/실재 여부가 확정된 ``cd``만 status를
    증명한다. 조건·cwd가 여러 가능성이면 call을 ``certain=False``로 표시해 후단 deny를 warn으로
    강등한다. ``if …; then …; fi``의 단순형은 condition cwd를 body로 전달한다.
    """
    if not isinstance(command, str):
        return _ShellParseResult((), False, "")
    executable, heredoc_uncertain = _strip_shell_heredoc_bodies(command)
    command = _normalize_shell_line_continuations(executable)
    if not _GIT_COMMAND_PREFILTER.search(_git_prefilter_text(command)):
        reason = "shell heredoc을 정적으로 완전히 소비할 수 없음" if heredoc_uncertain else ""
        return _ShellParseResult((), heredoc_uncertain, reason)
    parsed = _shell_segments(command)
    if parsed is None:
        return _ShellParseResult((), True, "shell 구문을 정적으로 해석할 수 없음")
    segments, operators = parsed
    anchors = {Path(cwd).expanduser().resolve()}
    cwd_certain = True
    found: list[_ShellGitInvocation] = []
    last_status: bool | None = None
    in_if_condition = False
    if_status: bool | None = None
    if_body_gate: bool | None = True
    shell_words = [word for segment in segments for word in segment]
    unsupported_control = any(
        segment and segment[0] in {
            "while", "until", "for", "select", "case", "do", "done", "{", "}",
        }
        for segment in segments
    )
    has_grouping = any(
        (word.startswith("(") or word.endswith(")")) and not word.startswith(":(")
        for word in shell_words
    )
    globally_certain = (
        "$" not in command and "`" not in command
        and not has_grouping and not unsupported_control
    )

    i = 0
    while i < len(segments):
        # Pipeline 전체는 같은 outer cwd에서 병렬 subshell 실행되고 cd가 밖으로 전파되지 않는다.
        end = i
        while end < len(operators) and operators[end] == "|":
            end += 1
        previous_operator = operators[i - 1] if i > 0 else None
        gate = _shell_gate(previous_operator, last_status)
        pipeline = end > i
        base_anchors = set(anchors)
        base_cwd_certain = cwd_certain
        group_status: bool | None = None

        for position in range(i, end + 1):
            segment = list(segments[position])
            # loop/case 전체 실행 의미는 지원하지 않는다. 다만 condition/body command 위치의
            # git은 놓치지 않고 uncertain call로 surface해 deny 대신 ambiguity warn을 만든다.
            if unsupported_control and segment and segment[0] in {"while", "until", "do", "{"}:
                segment = segment[1:]
            starts_if = bool(segment and segment[0] == "if")
            starts_then = bool(segment and segment[0] == "then")
            starts_else = bool(segment and segment[0] == "else")
            starts_fi = bool(segment and segment[0] == "fi")
            if starts_if:
                in_if_condition = True
                segment = segment[1:]
            if starts_then:
                in_if_condition = False
                if_body_gate = if_status
                segment = segment[1:]
                gate = if_body_gate
            if starts_else:
                in_if_condition = False
                if_body_gate = None if if_status is None else not if_status
                segment = segment[1:]
                gate = if_body_gate
            if starts_fi:
                in_if_condition = False
                if_body_gate = True
                segment = segment[1:]
            if (not in_if_condition and not starts_then and not starts_else
                    and if_body_gate is not True):
                if gate is True:
                    gate = if_body_gate

            command_anchors = base_anchors if pipeline else anchors
            command_cwd_certain = base_cwd_certain if pipeline else cwd_certain
            new_anchors, new_cwd_certain, status, calls = _shell_simple_command(
                command_anchors, command_cwd_certain, segment, gate,
                globally_certain=globally_certain,
            )
            found.extend(calls)
            group_status = status
            if not pipeline and gate is not False:
                anchors = new_anchors
                cwd_certain = new_cwd_certain
            if in_if_condition:
                if_status = status

        if pipeline:
            anchors = base_anchors
            cwd_certain = base_cwd_certain
        if gate is not False:
            last_status = group_status
        i = end + 1
    uncertain = heredoc_uncertain or unsupported_control
    reason = "shell 구문/cwd를 정적으로 단일 증명할 수 없음" if uncertain else ""
    return _ShellParseResult(tuple(found), uncertain, reason)


def judge_git_anchor_command(
    cwd: str, command: str, *, runner: Any = subprocess.run
) -> dict[str, str]:
    """셸 command의 모든 git 호출을 중앙 파싱하고 가장 강한 판정을 반환한다."""
    parsed = _git_invocations_from_shell(cwd, command)
    invocations = parsed.invocations
    if not invocations and parsed.uncertain:
        return {
            "verdict": "warn", "cwd_identity": "non-repo",
            "reason": f"{parsed.reason} — 차단하지 않음; cwd를 직접 확인",
        }
    if not invocations:
        return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "git mutation 없음"}
    judgments: list[dict[str, str]] = []
    for call in invocations:
        judgment = judge_git_anchor(call.cwd, list(call.argv), runner=runner)
        if not call.certain and judgment["verdict"] != "warn":
            judgment = {
                **judgment,
                "verdict": "warn",
                "reason": f"조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음; {judgment['reason']}",
            }
        judgments.append(judgment)
    if parsed.uncertain:
        judgments.append({
            "verdict": "warn", "cwd_identity": "non-repo",
            "reason": f"{parsed.reason} — 차단하지 않음; cwd를 직접 확인",
        })
    rank = {"ok": 0, "warn": 1, "deny": 2}
    strongest = max(judgments, key=lambda item: rank[item["verdict"]])
    if len(judgments) == 1:
        return strongest
    reasons = " | ".join(
        f"호출 {index} [{item['cwd_identity']}/{item['verdict']}]: {item['reason']}"
        for index, item in enumerate(judgments, 1)
    )
    return {**strongest, "reason": reasons}


def _resolved_subcommand(args: argparse.Namespace) -> str:
    """argparse Namespace 에서 실행된 subcommand 를 점표기로 해소한다(idea/prefix 서브그룹 포함).

    top-level dest=`cmd`, 서브그룹 dest=`idea_cmd`/`prefix_cmd` — dispatch 게이트가 mutation
    분류를 조회할 키를 만든다. (미지정/불명 → "".)
    """
    cmd = getattr(args, "cmd", None) or ""
    if cmd == "idea":
        return f"idea {getattr(args, 'idea_cmd', '') or ''}".strip()
    if cmd == "prefix":
        return f"prefix {getattr(args, 'prefix_cmd', '') or ''}".strip()
    return cmd


def _print_read_anchor(
    *, subcommand: str, pm_home: Path | None = None, pm_inputs_missing: bool = False
) -> None:
    """읽기 조회가 실제로 측정하는 repo 앵커와 역할을 stdout 첫 줄에 표시한다.

    앵커는 도구가 이미 사용하는 ``REPO`` 그대로다. 역할은 mutation misanchor
    가드가 등록 worktree라고 확인한 경우만 ``worktree``로, 실 board 소유가 확인된
    경우만 ``PM 홈``으로 표기한다. 어느 양성 증거도 없으면 역할을 단언하지 않는다.
    PM 홈 폴백이면 같은 첫 줄에 실제 PM 입력 앵커와 read leaf별 입력 목록을 함께
    표시한다. PM 입력 홈을 확정하지 못해 조회를 중단하는 경우에도 첫 줄에서 그 사실을
    숨기지 않는다.
    """
    anchor = REPO
    if _pm_home_worktree_misanchor(anchor) is not None:
        role = "worktree"
    elif _has_real_board(anchor / ".project_manager"):
        role = "PM 홈"
    else:
        role = "역할 미상"
    # flush 필수 — stdout 은 파이프/리다이렉션에서 블록 버퍼링되는데 stderr 는 unbuffered 라,
    # flush 없이는 `board.py <read> 2>&1 | …` 에서 stderr 가 앵커보다 먼저 나와 "첫 줄" 계약이
    # 깨진다. in-process capsys 테스트는
    # 버퍼링을 거치지 않아 이 클래스를 구조적으로 못 본다 — subprocess 순서 테스트가 짝이다.
    line = f"repo 앵커: {anchor} ({role})"
    if pm_home is not None:
        inputs = _READ_PM_INPUTS_BY_SUBCOMMAND[subcommand]
        line += f" → PM 입력 앵커: {pm_home} (PM 홈 폴백: {inputs})"
    elif pm_inputs_missing:
        line += " → PM 입력 앵커: 없음"
    print(line, flush=True)


def _git_rev_parse(anchor: Path, *args: str, runner: Any = subprocess.run) -> str | None:
    """`git -C <anchor> rev-parse <args>` 결과(strip)를 반환. git 아님/오류/빈 값이면 None
    (fail-soft — 솔로/standalone·비-git 트리 무영향). `runner` 는 hermetic 테스트 주입 seam."""
    try:
        r = runner(["git", "-C", str(anchor), "rev-parse", *args],
                   capture_output=True, text=True, encoding="utf-8",
                   errors="replace", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    out = (r.stdout or "").strip()
    return out or None


def _is_linked_worktree(anchor: Path, *, runner: Any = subprocess.run) -> bool:
    """`anchor` 가 linked git worktree(main checkout 아님)인가.

    linked worktree 는 `git rev-parse --git-dir` 가 `<common>/worktrees/<name>` 을 가리켜
    `--git-common-dir`(=`<common>`)와 **다르다**. 일반 checkout·PM 홈은 둘 다 `.git` 로 동일
    (→ False). git 아님/오류면 False(fail-soft — 솔로/standalone 무영향).
    """
    git_dir = _git_rev_parse(anchor, "--git-dir", runner=runner)
    common_dir = _git_rev_parse(anchor, "--git-common-dir", runner=runner)
    if git_dir is None or common_dir is None:
        return False

    def _abs(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else (anchor / pp).resolve()

    return _abs(git_dir) != _abs(common_dir)


def _has_real_board(pm_dir: Path) -> bool:
    """`.project_manager` 디렉토리(`pm_dir`)가 *실제* board 를 소유하는가.

    board/ 분리(submodule)면 `board/tickets`, legacy 면 `wiki/tickets` 의 상태
    디렉토리에 실 티켓(`T-*.md`)이 하나라도 있으면 True. 빈 scaffold(README/_template/
    `.gitkeep` 만 — worktree 출하 형상)는 False: worktree 자신의 scaffold 를 '실 board' 로
    오인해 가드를 무력화하지 않게 한다.
    """
    for base in (pm_dir / "board" / "tickets", pm_dir / "wiki" / "tickets"):
        if not base.is_dir():
            continue
        for status in STATUS_DIRS:
            status_dir = base / status
            if status_dir.is_dir() and any(status_dir.glob("T-*.md")):
                return True
    return False


def _registers_worktree(pm_home: Path, anchor: Path, *, runner: Any = subprocess.run) -> bool:
    """`pm_home` 이 `anchor` 를 **자기 worktree 로 등록**하는가 — git worktree 메타/경로 관례로 확인.

    조상 스캔이 '실 board 를 가진 최근접 디렉토리'를 찾아도, 그게 *실제로* 이 anchor 를 자기
    worktree 로 두는 PM 홈인지 미검증이면, 무관한 프레임워크 PM 홈 하위에 우연히 중첩된 linked
    worktree 를 엉뚱한 pm_home 으로 오귀속한다(reviewer should-fix). 아래 둘 중 하나면 등록으로 인정:
      (a) anchor 경로가 `<pm_home>/work/<name>` 형태 — 프레임워크 worktree 등록 관례(leases slot·
          `_regression_cwd` 가 `repo_root / "work/<repo>_<N>"` 조립·동형), 또는
      (b) anchor 의 `git rev-parse --git-common-dir`(공유 git 저장소)이 `pm_home` 하위 —
          `<pm_home>/.repos/<repo>.git`(두-git) 또는 `<pm_home>/.git`(단일 git worktree).
    둘 다 아니면 False → 호출부가 None(기존 fail-soft·오탐 0).
    """
    # (a) work/<name> 관례 — git 불요(경로만).
    if anchor.parent.name == "work" and anchor.parent.parent == pm_home:
        return True
    # (b) 공유 git-common-dir 이 pm_home 하위.
    common = _git_rev_parse(anchor, "--git-common-dir", runner=runner)
    if common is not None:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (anchor / common_path).resolve()
        try:
            common_path.relative_to(pm_home)
            return True
        except ValueError:
            pass
    return False


def _pm_home_worktree_misanchor(anchor: Path, *, runner: Any = subprocess.run) -> Path | None:
    """`anchor`(도구 자기-앵커=REPO)가 **다른 PM 홈의 등록 worktree** 안이면 그 PM 홈 경로를,
    아니면 None(fail-soft·솔로/standalone 무영향·오탐 0).

    3중 conjunction (오탐 0 지향·ticket §인터페이스 recipe):
      1. anchor 자신이 *실 board* 를 소유하지 않음 — 소유하면 정당(자체 board 사용이 있는
         가상 채택자도 무영향). (공개 제품 worktree)은 코드 전용·board 미소유라 항상 통과
         
      2. anchor 가 linked git worktree — 솔로/standalone(일반 checkout·PM 홈)은 여기서 탈락.
      3. 상위 PM 홈 식별 + **등록 확인** — anchor 조상 중 *실 board* 를 가진 최근접 디렉토리를
         찾고, 그 홈이 `_registers_worktree`(work/<name> 관례 또는 git-common-dir 하위)로 anchor 를
         실제 자기 worktree 로 두는지 확인. 등록 안 하면 None(무관 중첩 오탐 방지). 못 찾아도 None.
    """
    if _has_real_board(anchor / ".project_manager"):
        return None
    if not _is_linked_worktree(anchor, runner=runner):
        return None
    for parent in anchor.parents:
        if _has_real_board(parent / ".project_manager"):
            # 최근접 board-owner 가 anchor 를 자기 worktree 로 등록해야 그 홈을 안내한다
            # (무관 프레임워크 홈 하위 우연 중첩이면 등록 실패 → None·fail-soft).
            return parent if _registers_worktree(parent, anchor, runner=runner) else None
    return None


class _ReadBoardResolution(NamedTuple):
    """read dispatch의 board 해소 결과. error가 있으면 root/home은 None이다."""

    root: Path | None
    home: Path | None
    error: str | None


# 등록 worktree read 폴백에서 같은 PM 홈으로 옮겨야 하는 import-time 경로 상수 전수.
# board_root/tickets/areas는 lazy override가 담당하고, 이 표는 wiki 잔류 상태·instance hook·
# PM 로컬 설정·그 상태를 읽는 deep-import 모듈을 담당한다. 상대경로가 단일 진실이라 새
# PM-owned 입력을 추가할 때 이 표와 `_READ_PM_INPUTS_BY_SUBCOMMAND`를 함께 갱신하게 된다.
_READ_PM_PATH_RELS: dict[str, str] = {
    "IDEAS_DIR": "wiki/ideas",
    "DECISIONS_DIR": "wiki/decisions",
    "SPECS_DIR": "wiki/specs",
    "ARCHITECTURE_FILE": "wiki/architecture.md",
    "HOOKS_DIR": "hooks",
    "BOARD_FILE": "wiki/board.md",
    "LOG_FILE": "wiki/log/current.md",
    "STATUS_FILE": "wiki/status.md",
    "LOCAL_CONF": "local.conf",
    "TICKETS_DIR": "wiki/tickets",
    "TEMPLATE_FILE": "wiki/tickets/_template.md",
    "AREAS_FILE": "areas.md",
    "PM_STATE_FILE": "wiki/pm_state.md",
    "PM_STATE_TEMPLATE": "wiki/pm_state.template.md",
    "LOCAL_DIR": ".local",
    "REGRESSION_FLAG": ".local/regression.json",
    "LIVEGATE_FLAG": ".local/livegate.json",
    "BOARD_LOCK": ".local/board.lock",
    "LEASES_FILE": ".local/worktree-leases.json",
    "DOMAIN_PY": "tools/domain.py",
    "PM_DELEGATE_PY": "tools/pm_delegate.py",
}


@contextlib.contextmanager
def _read_pm_inputs_at(pm_home: Path, board_root_path: Path) -> Iterator[None]:
    """read dispatch 동안 PM-owned 입력 전부를 `pm_home` 하나로 재앵커한다.

    module 상수는 import 시 REPO에 굳으므로 board_root override만으로는 decisions/ideas 등이
    worktree에 남는다. 이 scope는 그 상수와 파생 tuple을 함께 바꾸고 finally에서 원복한다.
    mutation/sidecar는 이 context에 진입하지 않아 쓰기 게이트 의미론은 바뀌지 않는다.
    """
    global _READ_BOARD_ROOT_OVERRIDE, _READ_PM_HOME_OVERRIDE, _SCOPE_AWARE_DIRS
    saved = {name: globals()[name] for name in _READ_PM_PATH_RELS}
    saved_scope_dirs = _SCOPE_AWARE_DIRS
    saved_board_override = _READ_BOARD_ROOT_OVERRIDE
    saved_home_override = _READ_PM_HOME_OVERRIDE
    pm_dir = pm_home / ".project_manager"
    try:
        for name, rel in _READ_PM_PATH_RELS.items():
            globals()[name] = pm_dir / rel
        _SCOPE_AWARE_DIRS = (DECISIONS_DIR, SPECS_DIR)
        _READ_BOARD_ROOT_OVERRIDE = board_root_path
        _READ_PM_HOME_OVERRIDE = pm_home
        yield
    finally:
        for name, value in saved.items():
            globals()[name] = value
        _SCOPE_AWARE_DIRS = saved_scope_dirs
        _READ_BOARD_ROOT_OVERRIDE = saved_board_override
        _READ_PM_HOME_OVERRIDE = saved_home_override


def _candidate_board_homes(anchor: Path, *, runner: Any = subprocess.run) -> list[Path]:
    """worktree 소유 PM 홈 후보를 경로 조상과 git common-dir 조상에서 찾는다.

    worktree 경로를 둔 홈과 bare git을 둔 홈이 다를 수 있으므로 두 증거축을 합친다.
    실제 티켓을 가진 board 소유 홈만 후보이며, 최종 선택은 이 함수가 아니라 각 홈의
    worktree lease 장부가 한다.
    """
    search: list[Path] = list(anchor.parents)
    common = _git_rev_parse(anchor, "--git-common-dir", runner=runner)
    if common is not None:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (anchor / common_path).resolve()
        search.extend((common_path, *common_path.parents))
    homes: list[Path] = []
    seen: set[Path] = set()
    for path in search:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if _has_real_board(resolved / ".project_manager"):
            homes.append(resolved)
    return homes


def _ledger_registration(
    pm_home: Path, anchor: Path
) -> tuple[bool, str | None]:
    """PM 홈 장부가 `anchor` 슬롯을 등록하는지 strict point-read 한다.

    반환은 (일치, 오류). 부재·손상·스키마 오류는 load-bearing 소유 판정에서 빈 장부로
    강등하지 않고 오류 문자열로 돌려 호출부가 fail-loud 하게 한다.
    """
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    if not ledger.is_file():
        return False, f"{pm_home}: worktree lease 장부 없음"
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError) as exc:
        return False, f"{pm_home}: worktree lease 장부를 읽을 수 없음 ({exc})"
    if not isinstance(data, dict):
        return False, f"{pm_home}: worktree lease 장부 최상위 값이 object가 아님"
    rows = data.get("leases")
    if not isinstance(rows, list):
        return False, f"{pm_home}: worktree lease 장부의 leases 값이 list가 아님"
    target = anchor.resolve()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return False, f"{pm_home}: worktree lease 장부 leases[{index}]가 object가 아님"
        slot = row.get("slot")
        if not isinstance(slot, str) or not slot:
            return False, f"{pm_home}: worktree lease 장부 leases[{index}]의 slot이 유효하지 않음"
        if (pm_home / slot).resolve() == target:
            return True, None
    return False, None


def _resolve_read_board(
    anchor: Path, *, runner: Any = subprocess.run
) -> _ReadBoardResolution:
    """read-only board를 uniform 규칙으로 해소한다.

    자기 앵커가 실제 board를 가지면 그대로 쓴다. linked worktree가 아니면 legacy/solo
    graceful 경로를 그대로 보존한다. board 없는 linked worktree만 PM 홈 후보의 strict
    lease 장부를 조회하며, 정확히 한 홈이 슬롯을 등록할 때만 그 board를 연다.
    """
    if _has_real_board(anchor / ".project_manager"):
        return _ReadBoardResolution(_board_root_at(anchor), anchor, None)
    if not _is_linked_worktree(anchor, runner=runner):
        return _ReadBoardResolution(_board_root_at(anchor), None, None)

    matches: list[Path] = []
    errors: list[str] = []
    for home in _candidate_board_homes(anchor, runner=runner):
        matched, error = _ledger_registration(home, anchor)
        if matched:
            matches.append(home)
        elif error is not None and _registers_worktree(home, anchor, runner=runner):
            # 무관한 상위 board 홈의 장부 부재는 오류가 아니다. 경로/git 증거상 이 슬롯을
            # 등록한 홈만 load-bearing 장부 부재·손상을 fail-loud 사유로 삼는다.
            errors.append(error)

    if errors:
        detail = "; ".join(errors)
        return _ReadBoardResolution(
            None,
            None,
            "이 앵커에는 board가 없고 소유 PM 홈의 worktree lease 장부를 확정할 수 "
            f"없습니다: {detail}. PM 홈에서 조회하세요.",
        )
    unique = sorted(set(matches))
    if len(unique) == 1:
        home = unique[0]
        return _ReadBoardResolution(_board_root_at(home), home, None)
    if len(unique) > 1:
        homes = ", ".join(str(home) for home in unique)
        return _ReadBoardResolution(
            None,
            None,
            "이 앵커에는 board가 없고 슬롯이 여러 PM 홈의 worktree lease 장부에 "
            f"등록되어 board 소유자가 모호합니다: {homes}. 장부를 정리한 뒤 다시 조회하세요.",
        )
    return _ReadBoardResolution(
        None,
        None,
        "이 앵커에는 board가 없고 worktree lease 장부에서 소유 PM 홈을 찾지 "
        "못했습니다. PM 홈에서 조회하세요.",
    )


def _guard_worktree_misanchor(action: str, *, runner: Any = subprocess.run) -> bool:
    """쓰기-경로 진입 가드 — `anchor`(호출 시점 module-global `REPO`·hermetic monkeypatch
    추종)가 PM 홈 등록 worktree 면 fail-loud 후 True(차단), 아니면 False(통과).

    `REPO` 를 default 인자로 굳히지 않고 *호출 시점* 에 읽는다 — import 시점 상수로 캡처하면
    테스트의 `monkeypatch.setattr(board, "REPO", tmp)` 가 무시되고 실 worktree 앵커가 굳는다
    (pm_handoff LEASES_FILE 재해소와 동형 규율).
    """
    anchor = REPO
    pm_home = _pm_home_worktree_misanchor(anchor, runner=runner)
    if pm_home is None:
        return False
    print(
        f"[중단] `{action}` 을(를) worktree(코드 전용) 트리에서 실행했습니다 — board 상태는 "
        f"PM 홈이 소유합니다. 이대로면 이 worktree 에 stray 티켓/log 를 잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {anchor})",
        file=sys.stderr,
    )
    return True


# ── utilities ──────────────────────────────────────────────────────────

def local_config(repo: Path | None = None) -> dict[str, str]:
    """Per-clone local config (`.project_manager/local.conf`, git-ignored).

    `repo` 를 주면 **그 트리의** conf 를 읽는다(`<repo>/.project_manager/local.conf`) —
    이 board.py 사본의 `REPO` 가 아니라 판정 대상 트리가 앵커여야 하는 소비처(회귀 수집 하한 —
    스위트는 실행 cwd 트리의 것이다)를 위한 seam. 미지정은 현행 `LOCAL_CONF`(무변경).
    `external_review.local_config(repo)` 와 동형 시그니처.

    Plain `KEY=value` lines; `#` comments and blank lines ignored. Missing → {}.
    Holds per-clone settings that must NOT be shared via git (py·test_cmd·ctx_*·
    upstream 등). Written by `pm-init`. `session=`/`prefix=` 는 **solo 형상 전용 legacy**
    leased ≥2 인 multi 홈에서는 이 키가 있어도 무시되고 세션/prefix 는
    lease 장부에서 유도된다(session_name·id_prefix). solo 홈만 이 키로 폴백.
    """
    conf: dict[str, str] = {}
    path = (repo / ".project_manager" / "local.conf") if repo is not None else LOCAL_CONF
    if not path.exists():
        return conf
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def _ensure_trailing_newline(path: Path) -> None:
    """파일 끝 개행 보장 — append 전 가드. 마지막 개행 없는 local.conf(`…upstream_rev=abc`)에 바로
    append 하면 append 텍스트가 그 값에 이어붙어 기존 키가 손상된다(`abc# cross…`). 파일이 비어있지
    않고 개행으로 끝나지 않으면 `"\\n"` 하나를 덧붙인다(부재/읽기 실패는 무해 no-op)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return
    if text and not text.endswith("\n"):
        with path.open("a", encoding="utf-8") as f:
            f.write("\n")


def _set_conf_keys(text: str, updates: dict[str, str]) -> str:
    """local.conf 텍스트에서 지정 키만 set-or-replace. 나머지 줄·주석은 보존.

    있으면 그 자리에서 `key=value` 로 교체(첫 등장만), 없으면 끝에 추가. stdlib only.
    pm_import._set_conf_keys 와 동형 — board 는 pm_import 를 import 하지 않으므로
    (의존 방향: pm_import 가 board init 을 subprocess 로 부르는 상위) board-local 사본을
    둔다(중복 최소·순수 프리미티브). cmd_init 의 비파괴 병합에 쓴다.
    """
    remaining = dict(updates)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                newline = "\n" if line.endswith("\n") else ""
                out.append(f"{key}={remaining.pop(key)}{newline}")
                continue
        out.append(line)
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        for key, value in updates.items():
            if key in remaining:
                out.append(f"{key}={value}\n")
    return "".join(out)


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.6.2"


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


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew(EngineRevSkew·불완전 복사) 유래인지.

    fail-soft sibling 로더의 `except Exception` 은 로드 실패/부재만 None 으로 흡수하고, 이
    판정이 True 인 예외(중첩 로드에서 검출된 형제 skew)는 재-raise 해 fail-loud 를 보존한다."""
    return getattr(exc, "_engine_rev_skew", False)


def _require_engine_sibling(path: Path, filename: str) -> None:
    """load-bearing 형제 모듈의 **부재**를 stale 사본과 같은 진단으로 번역한다 (fail-loud).

    부재는 `spec_from_file_location`+exec 단계에서 raw `FileNotFoundError` 로 터져 복구 방법
    (pm-update 재동기)을 알려주지 않는다 — 부분/수동 복사라는 원인은 stale 사본과 같으므로
    같은 marked skew 로 표출한다(`_engine_rev_skew`: fail-soft 로더가 조용히 None 으로
    삼키지 않고 재-raise 한다).
    """
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


def _load_identity_args():
    """공용 정체성 인자 모듈(`identity_args.py`)을 같은 tools/ 디렉토리에서
    경로 로드한다 (`_load_pm_update_module`/`_load_domain_module` 동형 — 스크립트-위치 앵커·
    sys.path 무오염).

    board.py 는 스크립트 실행(`python3 tools/board.py`)과 테스트(`spec_from_file_location`)
    양쪽으로 로드되는데, 어느 쪽이든 `Path(__file__).resolve().parent` 는 정확히 tools/ 를
    가리키므로 이 경로-앵커 로더가 양쪽에서 동일하게 동작한다 — 평범한 `import identity_args`
    는 스크립트 실행에서만(sys.path[0] 에 우연히 tools/ 가 실림) 동작하고, 테스트 로드에선
    sys.path 에 tools/ 가 없어 실패한다.

    identity_args 는 board.py 의 전 서브(claim·init·list·migrate-identity·regression·livegate·
    reid) 정체성 파싱에 **load-bearing**이다 — 로드 실패는 엔진 자체 손상이므로(lint 계열의
    advisory fail-soft 관용구와 달리) 조용히 흡수하지 않고 그대로 예외를 낸다(fail-loud).
    """
    ia_path = Path(__file__).resolve().parent / "identity_args.py"
    return _load_module_from_path(
        ia_path, "identity_args.py", verifier=_verify_engine_rev,
    )


def _load_file_lock():
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    `_load_identity_args` 동형 — 경로-앵커 로더다. board 의 *모든* 변경 경로가 이 락을 지나므로
    identity_args 와 같은 **import 시점** 바인딩으로 둔다(아래 `file_lock = ...`): 락을 잡을 때마다
    형제를 로드하면 board 를 fail-soft 로 소비하는 호출층(pm_config 등)이 사본 skew 를 조용히
    삼키는 새 경로가 생긴다 — import 경계에서 한 번 fail-loud 로 세워 그 확산을 막는다.
    """
    lock_path = Path(__file__).resolve().parent / "file_lock.py"
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev,
    )


try:
    identity_args = _load_identity_args()
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


def session_name(override: str | None = None, *, required: bool = False) -> str | None:
    """세션 식별자 해소 — count-based 유도:

        override(`--repo`/`--slot` 해소값 — 액터 서브가 `_actor_session_override` 로
                 미리 계산해 넘긴다)
          > $PM_SESSION_NAME (env·harness 무관 엔진 식별자)
          > $CLAUDE_SESSION_NAME (env·deprecated alias·silent back-compat)
          > lease 장부 state=="leased" 행이 정확히 1개면 그 session   (단일-lease 유도)
          > (장부 부재·leased 0 = solo 홈) local.conf `session=`        (legacy 폴백)
          > None

    `PM_SESSION_NAME` 이 정식 이름(엔진 변수·하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias
    (둘 다면 PM 승·조용히 동작·안내는 문서만·`--json` 출력 오염 방지).

    **leased ≥2 (모호)면 local.conf 층을 건너뛴다** — per-clone 저장값(`session=`)으로 남의
    세션을 silent 오귀속하던 클래스를 원천 차단한다(Windows 4슬롯 홈 리포트). 단일-lease
    값과 local.conf 값이 다르면 유도값(lease) 승 — 저장 쪽지보다 슬롯 파생 진실.

    `required=True`(귀속 쓰기: claim·migrate·init owner 기본값)에서 미해소(None)면 fail-loud —
    `--repo <repo> --slot <N>` 명시를 안내하고 `sys.exit` 한다(silent 오귀속 금지). `required=False`
    (surface: whoami/status/list --mine)면 None 을 반환한다(호출부가 "(비바인딩)" 표시). 구
    `<host>-<pid>` 최종 폴백은 세션-귀속 아닌 국소 용처(worktree_pool `_default_session` 의
    lease 취득 임시 명명)에만 잔존한다 — 여기(귀속 해소)선 제거.

    저장측(worktree_pool._default_session)과 매칭측(여기)이 어긋나면 per-slot test_cmd·claim
    소유권이 미스되므로 세 모듈(board.session_name·worktree_pool._default_
    session·pm_config._default_session)을 같은 우선순위로 통일한다(tail 만 용처별 상이).
    """
    if override:
        return override
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    leased = identity_args.leased_sessions(LEASES_FILE)
    if len(leased) == 1:
        return leased[0]
    if not leased:
        # 장부 부재·leased 0 = solo 홈 → legacy local.conf 폴백 (후방호환).
        sess = local_config().get("session")
        if sess:
            return sess
    # leased ≥2 (모호) 또는 solo 무바인딩 → 미해소.
    if required:
        sys.exit(
            "[중단] 세션 미해소 — 활성 슬롯이 여럿이거나 바인딩이 없다. 귀속 조작은 "
            "`--repo <repo> --slot <N>` 로 세션을 명시하라 (예: `--repo project_manager --slot 1`)."
        )
    return None


# user identity 해소 git 폴백 timeout — `git config user.email` 은 로컬 config 읽기라
# 즉답이지만(네트워크 0) 환경 이상에 대비해 짧은 상한을 둔다(엔진 subprocess 관례·_interp_runs 동류).
_GIT_USER_TIMEOUT_SECONDS = 5


def _git_config_email() -> str | None:
    """`git config user.email` 을 읽어 반환 — 미설정/git 부재/실패 → None (fail-soft).

    user identity 해소(`user_name`)의 폴백 레이어다 — `local.conf user=` 가 없을 때
    git 의 commit author email 을 user 식별자로 쓴다. subprocess 는
    엔진 관례대로 UTF-8 고정(한글 이름·메시지 안전)·짧은 timeout. git 바이너리 부재
    (`shutil.which` None)·rc≠0(미설정)·예외는 모두 None 으로 강등한다(크래시 0).
    """
    git_binary = shutil.which("git")
    if git_binary is None:
        return None
    try:
        r = subprocess.run(
            [git_binary, "-C", str(REPO), "config", "user.email"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_GIT_USER_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 — fail-soft: git 호출 실패는 None(미상)으로 강등.
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def user_name(override: str | None = None) -> str | None:
    """user 식별자 해소 — `session_name()` 과 *동형* 우선순위:

        override > local.conf `user=` > `git config user.email` > None

    `pm`(슬롯)이 *어느 PM 컨텍스트*인지(=`session_name()`)와 직교하는 **누가**(사람) 차원이다
    multi-user 보드 공유에서 `created_by`(provenance)·`claimed_by`(assignee)·
    areas `area_owner` 의 user 토큰을 푼다. solo(N=1·M=1)는 보통 `local.conf user=` 미설정 →
    `git config user.email` 로 폴백(commit author 와 동일 식별자)·그마저 없으면 None(graceful —
    user 미상 허용·fail-soft·기존 슬롯-only 동작 무변경).
    """
    if override:
        return override
    conf_user = local_config().get("user")
    if conf_user:
        return conf_user
    return _git_config_email()


def identity_tag(session_override: str | None = None,
                 user_override: str | None = None) -> str | None:
    """현재 identity 를 `<user>/<pm-slot>` 토큰으로 합성한다.

    `created_by`(provenance)·`claimed_by`(assignee) frontmatter 에 박는 값이다. user 가
    해소되면 `<user>/<pm>`, 미상(None)이면 슬롯만(`<pm>`) — **기존 슬롯-only 값과 형태가
    같다**(graceful·하위호환). 읽기측은 `/` 로 split 해 user/slot 을 분리하되, `/` 없는 값
    (구 ticket·user 미상)은 slot-only 로 읽어야 한다(fail-soft).

    세션 미바인딩(`session_name` None·surface·required=False)이면 슬롯 토큰이 없으므로 user
    만(있으면) 반환하고 둘 다 없으면 None 이다(graceful). 귀속 쓰기 호출부(claim)는
    이 함수 전에 `session_name(required=True)` 로 세션을 확정하므로 pm 이 None 으로 새지 않는다.
    """
    pm = session_name(session_override)
    user = user_name(user_override)
    if pm is None:
        return user
    return f"{user}/{pm}" if user else pm


def _reject_task_slot_identity_mix(args: argparse.Namespace) -> None:
    """task 소비 연산에서 `--task`와 `--repo/--slot` 혼합을 부작용 전에 거부한다.

    task는 귀속/실행 위치 모두 보유 집합을 자동 해소하는 독립 축이다. repo/slot을 함께 받으면
    task 조용 우선이나 특정 슬롯 선택으로 의미가 갈렸던 과거 경로를 없애고 fail-loud 한다.
    `--slot` 단독 혼합도 parse_identity의 slot-mode 처방보다 이 계약이 먼저 surface 된다.
    자원 연산(alloc/release·rebase 소유검사)은 이 깔때기를 소비하지 않아 불변이다.
    """
    if getattr(args, "task", None) is not None and (
        getattr(args, "repo", None) is not None
        or getattr(args, "slot", None) is not None
    ):
        sys.exit(
            "[중단] --task 는 독립 정체성이다 — --repo/--slot 과 함께 쓸 수 없다 "
            "(task 보유 작업공간은 장부에서 자동 해소)."
        )


def _actor_session_override(args: argparse.Namespace, *, soft: bool = False) -> str | None:
    """`--repo`/`--slot`에서 actor 연산(claim·init·migrate-identity·regression·
    livegate record·reid)의 세션 override 문자열을 해소한다 — 구 `args.session` 을 대체하는
    단일 seam(전 actor 서브 공유·중복 0).

    **`soft=True`**(명시 `--cwd` 핀 호출부 전용) — `--repo` 단독 해소가 모호(활성 ≥2·
    `SlotResolutionError`)하거나 미해소(활성 0)여도 `sys.exit` 대신 `None` 을 돌린다. 실행 위치가
    `--cwd` 로 핀된 regression run 은 session 이 cwd/디스패치에 불요하고 슬롯 test_cmd 유도에만
    남으므로(cwd override 최우선), 모호/미해소를 하드 실패시키지 않고 test_cmd 폴백(repo/local)에
    맡긴다 — livegate cf14d9b(`--cwd` 시 eager 해소 생략)의 동형 처방. `--slot` 명시(kind="slot")·
    task 는 애초에 raise 하지 않으므로 soft 무영향(그 슬롯 test_cmd 를 그대로 유지). 사용 오류
    (ValueError — `--slot` 단독·`--slot < 1`)는 soft 여도 하드 실패(모호 아님·usage error).

    `identity_args.parse_identity` 로 kind 를 가른다:
      - kind="slot"(`--repo X --slot N`) — 리스 조회 없이 즉시 `"<repo>_<N>"`(완전 해소·정체성
        문자열 그대로 — 옛 `--session <repo>_<N>` 과 byte-identical).
      - kind="repo"(`--repo X` 단독) — `identity_args.resolve_actor_slot`(repo X 의 활성 리스가
        정확히 1개면 그 세션·≥2개는 `SlotResolutionError`·**0개는 fail-loud**). 명시 repo 는
        해소되거나 명시적으로 실패한다 — 0개를 None 폴백하면 kind=none 과 구분 못 해 다른 세션
        silent 오귀속(codex r2). **kind=none(인자 전무)만** None → 기존 no-flag 체인.
      - kind="none"(인자 전무) — None → 기존 체인 그대로(무변경).
    `--slot` 단독(`--repo` 없음)·`--slot < 1` 은 `parse_identity` 가 `ValueError` 로 거부한다 —
    여기서 잡아 board.py 관례(`[중단]` 접두)로 fail-loud 한다.
    """
    _reject_task_slot_identity_mix(args)
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as e:
        sys.exit(f"[중단] {e}")
    if identity.task:
        # task-mode 귀속 — claimed_by/created_by 는 `<user>/<task>`. 작업공간
        # 이 아니라 **정체성 축**이라 task 이름을 그대로 세션 override 로 돌린다.
        #
        # **깔때기 1회 검증**: 이 분기를 claim/new/migrate/reid 가 지나고,
        # 실행-위치 도구(regression/livegate/ticket_finish)는 `_resolve_task_workspace_cwd` 가
        # 같은 `_validate_actor_task_or_exit` 를 부른다 — 무검증 task 명이 created_by/claimed_by/
        # lease-session 으로 영속되는 클래스를 소비 지점 전체에서 한 번에 닫는다(point-patch 아님).
        _validate_actor_task_or_exit(identity.task)
        return identity.task
    if identity.kind == "slot":
        return identity.session
    if identity.kind == "repo":
        # `--repo X` 명시 = actor 정체성을 X 로 하겠다는 의도. 활성 1슬롯이면 해소, ≥2면 fail-loud.
        # **0개(미해소)도 fail-loud** — None 반환하면 호출부가 "인자 전무(kind=none)"와 구분 못 해
        # env/단일-lease/local.conf 로 폴백→다른 세션으로 **silent 오귀속**(예 `--repo typo` 가 단일
        # lease 세션으로 claim). 명시-정체성 계약·오귀속 방지 목적이라 explicit-unresolved
        # 는 폴백 아니라 명시적 실패로 닫는다(codex r2·kind=none 만 폴백·[[answer-feasibility-dont-decide]] 정신).
        try:
            session = identity_args.resolve_actor_slot(identity.repo, LEASES_FILE)
        except identity_args.SlotResolutionError as e:
            if soft:
                return None   # --cwd 핀— 모호를 cwd 실행이 존중, test_cmd 폴백에 맡김.
            sys.exit(f"[중단] {e}")
        if session is None:
            if soft:
                return None   # --cwd 핀— 미해소도 cwd 실행이 존중(soft).
            sys.exit(
                f"[중단] repo '{identity.repo}' 의 활성 슬롯을 해소할 수 없다(활성 리스 0개). "
                f"`--repo {identity.repo} --slot <N>` 으로 슬롯을 명시하거나, 인자 없이(자동 해소) 실행하라."
            )
        return session
    return None


def _validate_actor_task_or_exit(task: str) -> None:
    """task 명 공유 validator 소비 — 불법이면 fail-loud (깔때기 단일 지점).

    정체성 깔때기(`_actor_session_override`·귀속)와 실행-위치 해소(`_resolve_task_workspace_cwd`)가
    **같은 한 지점**을 소비해, 무검증 task 명이 created_by/claimed_by/lease-session 으로 영속되거나
    실행-위치로 쓰이기 **이전** 부작용 0 로 거부한다. 공유 validator = `identity_args.validate_task_name`
    (worktree_pool 엔진 validator 와 동형·로직 중복 0). 예약패턴(`<repo>_<N>`) 판정용 registered_repos
    는 areas 에서 fail-soft 해소(파싱 실패 시 char/traversal 검증만).
    """
    try:
        registered = registered_repos()
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화(char/traversal 유지).
        registered = None
    try:
        identity_args.validate_task_name(task, registered)
    except identity_args.InvalidTaskName as e:
        sys.exit(
            f"[중단] {e} — `--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴"
            f"(`<repo>_<N>`·⑥)은 쓸 수 없다."
        )


def _resolve_task_workspace_cwd(args: argparse.Namespace) -> tuple[str, str | None] | None:
    """task-mode(`--task`) 실행 위치를 해소해 `(절대경로, 슬롯 test_cmd)` 를 반환한다.

    `--task` 미지정이면 `None`(task 아님·호출부는 기존 slot-mode 경로 유지). task 지정이면
    `identity_args.resolve_task_workspace`(모호=에러)로 슬롯을 특정하고 그 worktree
    **절대경로**(`REPO / ws.slot`)를 돌린다 — cwd 는 해소에 참여하지 않고 순전히 장부+
    명시 인자로만 판정한다. 모호/미보유는 fail-loud(`[중단]` 접두·board 관례). 슬롯 test_cmd 는
    바인딩된 회귀명령(None=미바인딩·호출부 폴백).
    """
    if not getattr(args, "task", None):
        return None
    if getattr(args, "cwd", None) is not None:
        sys.exit(
            "[중단] --task 모드에서는 --cwd를 명시할 수 없다 — task 작업공간은 장부에서 "
            "자동 해소하며 외부 경로 override를 허용하지 않는다."
        )
    _reject_task_slot_identity_mix(args)
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as e:
        sys.exit(f"[중단] {e}")
    # 해소 이전 깔때기 검증(must-fix) — 불법 task 명이 실행-위치로 쓰이기 전 fail-loud(귀속 경로와
    # 동일 validator·일관 메시지). "보유 작업공간 없음" 보다 앞서 정확한 사유를 준다.
    _validate_actor_task_or_exit(identity.task)
    try:
        ws = identity_args.resolve_task_workspace(identity, LEASES_FILE)
    except identity_args.WorkspaceResolutionError as e:
        sys.exit(f"[중단] {e}")
    return str(REPO / ws.slot), ws.test_cmd


def _repo_from_session(session: str) -> str | None:
    """세션명 `<repo>_<N>` 에서 repo 를 추출한다.

    슬롯 세션명은 `{repo}_{N}`(N=숫자 슬롯·worktree_pool `_slot_for`·pm_bootstrap lean
    끝의 `_<숫자>` 마디를 슬롯 번호로 떼고 나머지를 repo 로
    잡는다(`rpartition('_')` — 마지막 `_` 만 분리) — repo 명이 `_` 를 포함해도(예
    `project_manager_1` → repo `project_manager`·`a_2_3` → `a_2`) 정확히 갈린다. 끝
    마디가 숫자가 아니거나(솔로 커스텀 세션명 `pm`·`my-session`·`foo_bar`) repo 부분이
    비면(`_1`) `<repo>_<N>` 형태가 아니므로 None (유도 skip → id_prefix 가 다음 층으로).
    """
    head, sep, tail = session.rpartition("_")
    if not sep or not head or not tail.isdigit():
        return None
    return head


def _prefix_from_session(session: str | None = None) -> str | None:
    """바인딩된 세션의 repo → areas.md 그 repo 행의 prefix.

    `session_name(session)`(count-based·surface·required=False)이 세션을 해소하면 세션명을
    `<repo>_<N>` 로 파싱해(`_repo_from_session`) repo 를 얻고, areas.md 에서 그 repo 행의
    prefix 를 돌려준다 — per-repo prefix 의 단일 진실은 areas.md 칼럼이다. 다음은
    모두 None → id_prefix 가 다음 층(count-based)으로 폴백:
      - 세션 미해소(None·모호 M>1·비바인딩) — `session_name()` 이 None(required=False라 fail-loud 아님).
      - 세션명이 `<repo>_<N>` 형태 아님(솔로 커스텀 세션명) — `_repo_from_session` None.
      - 그 repo 가 areas.md 에 미등록(또는 prefix 칼럼 빈 값·areas.md 부재).

    `session` 명시는 M>1 슬롯 순회(`_regression_run_slot`→`_test_cmd`)가 슬롯별로
    prefix 를 뽑을 때 쓴다 — `session_name(session)` 이 그 override 를 즉시 반환하므로 슬롯 lease
    test_cmd 가 빈 areas 폴백이 *그 슬롯의* repo prefix 로 정확 해소된다. session 을 안 넘기면
    (`None`) 전역 `session_name()` 재해소로 떨어져, M>1 순회에서 전 슬롯이 None(모호)·env 오귀속
    prefix 로 같은 areas test_cmd 를 돌리는 false-green 이 생긴다(push 게이트·codex must-fix).
    """
    resolved = session_name(session)   # override(session) 우선·미지정 시 전역 해소(required=False → None 가능)
    if not resolved:
        return None
    repo = _repo_from_session(resolved)
    if not repo:
        return None
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("repo") == repo:
            return row.get("prefix") or None
    return None


def id_prefix(override: str | None = None, *, session: str | None = None) -> str | None:
    """Resolve ticket-ID namespace prefix (multi-repo areas·N×M).

    prefix 는 M>1 repo 의 ID 네임스페이스(협업용 아님)다. 해소 체인(count-based
    유도):

        override(--prefix)
          > 세션 유도: session_name(session) → 세션명 `<repo>_<N>` → repo → areas.md 행 prefix
          > 등록 repo(prefix) 가 정확히 1개면 그 prefix               (count-based)
          > (solo·areas 부재 = 등록 0) local.conf `prefix=`           (legacy 폴백)
          > None

    None → legacy `T-NNNN`(graceful·후방호환). Non-None → `T-<PREFIX>-NNN` 네임스페이스.

    `session`(키워드 전용) 은 세션 유도층에만 쓰이는 override 다 — M>1 슬롯 순회(`_test_cmd`)가
    슬롯별 prefix 해소를 위해 그 슬롯 세션명을 넘긴다. 미지정(`None`)이면 세션 유도가 전역
    `session_name()` 을 해소한다(현행·cmd_new/status/init 무변경). count-based·local.conf 층은
    session 과 무관하다(전역 registry/conf).

    **solo(areas.md·lease 장부 부재) 경로는 무변경** — 세션 유도(장부 부재 → session_name 이
    local.conf session 폴백이나 그 세션명이 areas 미등록이라 None)·count-based(등록 0 → skip)를
    거쳐 legacy local.conf `prefix=` 폴백에 그대로 도달한다. **local.conf `prefix=` 는 solo
    전용으로 강등**— 등록 repo 가 있으면(≥1) per-repo prefix 는 areas.md(세션/
    count 유도)가 단일 진실이고 clone 전역 키는 무시한다(남의 prefix 로 silent 오네임스페이스
    하던 클래스 차단). multi-repo(등록 repo ≥2) 홈에서 세션 유도·count-based 가 둘 다 실패
    (None)하면 `cmd_new` 가 fail-loud(오네임스페이스 방지).
    """
    if override:
        # override 를 등록된 canonical case 로 해소 (prefix 동일성=case-insensitive fold):
        # 입력 `aaa` 가 등록 `AAA` 로 fold-매치되면 등록 case `AAA` 로 발행(네임스페이스 분할 방지).
        # 미등록이면 입력 그대로 — 최초 사용이 canonical case 를 확립하고, 발행 시 `_next_id` 가
        # 기존 티켓 시리즈 case 로 정련한다(기존 `T-AAA-*` 를 `--prefix aaa` 가 이어감).
        return _fold_lookup(override, registered_prefixes()) or override
    # 2. 세션 유도 — 바인딩된 세션명 `<repo>_<N>` → repo → areas.md prefix (단일 진실).
    #    session override 를 thread — M>1 슬롯 순회가 슬롯별 정확 해소(전역 재해소 false-green 차단).
    derived = _prefix_from_session(session)
    if derived:
        return derived
    # 3. count-based — 등록 repo(prefix) 가 정확히 1개면 그것(단일 self-host·모호성 0).
    #    cmd_new 의 ≥2 fail-loud 가드와 같은 `registered_prefixes()` 를 세어 lockstep 유지.
    registered = registered_prefixes()
    if len(registered) == 1:
        return next(iter(registered))
    # 4. (solo·areas 부재 = 등록 0) local.conf `prefix=` legacy 폴백. 등록 repo 가 있으면
    #    (≥2·모호) 여기서 local.conf 를 쓰지 않는다 → None(cmd_new fail-loud·오귀속 차단).
    if not registered:
        return local_config().get("prefix") or None
    # 5. 등록 repo ≥2 인데 세션 유도 실패 → None (cmd_new fail-loud).
    return None


# areas.md 신/구 스키마.
#   - 구 스키마: `| prefix | area | owner |`                      (멀티-CLONE)
#   - per-repo: `| repo | prefix | git | test_cmd | owner |`      (per-repo 레지스트리)
#   - base 스키마: `| repo | prefix | git | test_cmd | owner | base |`  (base 브랜치)
#   - protected 스키마: `| repo | prefix | git | test_cmd | owner | base | protected |`  (보호브랜치)
#   - 신 스키마: `| … | protected | area_owner |`                 (user 소유)
#     area_owner = `--mine` 기본 풀 입력의 *user* 소유. 기존 `owner`
#     (per-repo registry registrant)를 overload 하지 않는 *별도* 칼럼이다(codex sug — 의미 충돌 회피).
# 파싱은 **헤더 행을 읽어 칼럼명→인덱스**로 매핑한다(위치-비의존) — 모든 스키마를 같은
# 코드로 읽고, 누락 칼럼은 빈 값으로 떨어뜨려 하위호환을 보장한다(`base`/`protected` 칼럼
# 없는 구 레지스트리 → 행 dict 에 그 키 없음 → `_repo_base`/`_repo_protected` 가 폴백).
_AREAS_SEP_RE = re.compile(r"^\|[\s:|-]+\|?$")  # markdown 구분선 `|---|---|`

# 보호 브랜치 default (엔진 상수) — areas.md `protected` 칼럼이 미지정/미등록일 때
# 폴백. PM 이 자율로 commit/push 못 하는 브랜치(pre-push 훅·bootstrap 경고가 이걸 본다).
# per-repo override 는 areas.md `protected` 칼럼(쉼표분리·예 `main,develop`).
DEFAULT_PROTECTED = ("main", "master", "develop")


def _split_areas_row(line: str) -> list[str] | None:
    """`| a | b | c |` 한 줄을 셀 리스트로. table row 가 아니면 None.

    구분선(`|---|---|`)·빈 줄·비-`|` 줄은 None. 앞뒤 파이프를 벗기고 셀을 strip.
    """
    s = line.strip()
    if not s.startswith("|") or _AREAS_SEP_RE.match(s):
        return None
    # 앞뒤 경계 파이프 제거 후 분할 (내부 셀 사이 파이프로 split).
    inner = s.strip("|")
    return [c.strip() for c in inner.split("|")]


# areas.md canonical 칼럼 순서 (신 스키마). 구 헤더(`base`/
# `protected`/`area_owner` 없음)는 이 순서의 *prefix* 다(`repo|prefix|git|test_cmd|owner` 또는
# …`|base`/…`|protected`). 그래서 헤더보다 셀이 많은 행(구 헤더에 신 칼럼 row 가 append 된
# *업그레이드* 프로젝트 — `repo add` 가 완전 canonical row 를 더 짧은 헤더에 붙인 경우)을 이
# canonical 순서로 이어 매핑해 `base`/`protected`/`area_owner` 유실을 막는다(게이트가
# base 에 대해 건 가드를 protected[7칸]→area_owner[8칸]까지 확장).
_AREAS_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base", "protected",
                  "area_owner")


def _areas_header_line() -> str:
    """canonical areas.md 헤더 행 (`| repo | prefix | … | area_owner |`·줄바꿈 없음).

    `_AREAS_COLUMNS`(단일 진실)에서 파생한다 — `areas_append` 의 신규 파일 헤더와
    `_migrate_areas_text` 의 구-헤더 업그레이드가 같은 8칼럼 헤더를 쓰도록 한 곳에서 만든다.
    """
    return "| " + " | ".join(_AREAS_COLUMNS) + " |"


def _areas_separator_line() -> str:
    """canonical areas.md 구분선 (`|---|---|…|`·칼럼 수만큼 `---`·줄바꿈 없음)."""
    return "|" + "|".join("---" for _ in _AREAS_COLUMNS) + "|"


# ── areas.md 칼럼 인덱스 해소 (공용 헬퍼) ───────────────────────────
# `_parse_areas`(읽기)·`_migrate_areas_text`(area_owner backfill)·`areas_set_cell`(범용 셀 변경)
# 세 경로가 **같은** 인덱스 규칙을 써야 한다. 두 벌로 갈라지면 칼럼 오매핑
# 행 dict 매핑
# (`_areas_row_dict`) 헤더 인덱스 + 구-헤더 업그레이드 계획(`_areas_column_index`) 데이터
# 행의 wider-row 보정(`_areas_row_cell_index`) 을 여기 한 곳에 모은다.


def _areas_row_dict(header_cells: list[str], cells: list[str]) -> dict[str, str]:
    """데이터 행 셀 → `{칼럼명: 값}` (헤더 매핑 · wider row 는 canonical 순서).

    셀 수가 헤더보다 **많으면**(더 넓은 신 스키마 row 를 더 좁은 구 헤더에 append 한 업그레이드
    프로젝트) 헤더를 무시하고 `_AREAS_COLUMNS` 순서로 매핑한다(base/protected/area_owner 유실
    차단·폭 초과는 `col{i}` 폴백). 그 외는 자기 헤더로 매핑(모자란 셀은 빈 문자열).
    """
    if len(cells) > len(header_cells):
        return {
            (_AREAS_COLUMNS[i] if i < len(_AREAS_COLUMNS) else f"col{i}"): cells[i]
            for i in range(len(cells))
        }
    return {header_cells[i]: (cells[i] if i < len(cells) else "")
            for i in range(len(header_cells))}


def _areas_column_index(header_cells: list[str],
                        column: str) -> tuple[int, str | None, int]:
    """헤더에서 `column` 셀 인덱스를 해소한다 — 부재면 구-헤더 업그레이드 계획을 함께 낸다.

    반환 `(idx, new_header_line, sep_cols)`:
      - 헤더에 그 칼럼이 있으면 `(header_cells.index(column), None, 0)` — 업그레이드 없음(헤더
        행 verbatim 보존).
      - 없으면(구 헤더) **업그레이드**한다. 헤더가 canonical prefix(`_AREAS_COLUMNS` 앞 N개와
        일치하는 per-repo 레지스트리 계열 5/6/7칼럼)면 canonical 8칼럼 헤더(`_areas_header_line`)
        로 교체하고 idx=canonical 인덱스. 비호환 구 헤더(멀티-clone `prefix|area|owner`[3] 등 —
        칼럼 의미가 canonical 과 어긋남)면 정렬을 깨지 않게 기존 칼럼 **뒤에 그 칼럼만 append**
        하고 idx=len(header_cells). `sep_cols` = 업그레이드 후 구분선이 가질 칼럼 수.

    `_migrate_areas_text`(area_owner backfill)와 `areas_set_cell`(범용 셀 변경)이 **공유**한다.
    """
    if column in header_cells:
        return header_cells.index(column), None, 0
    if tuple(header_cells) == _AREAS_COLUMNS[:len(header_cells)]:
        return (_AREAS_COLUMNS.index(column), _areas_header_line(),
                len(_AREAS_COLUMNS))
    return (len(header_cells),
            "| " + " | ".join(header_cells + [column]) + " |",
            len(header_cells) + 1)


def _areas_row_cell_index(header_cells: list[str], cells: list[str],
                          header_idx: int, column: str) -> int:
    """데이터 행에서 `column` 셀의 인덱스 — wider row 는 canonical 인덱스로 보정한다.

    셀 수가 헤더보다 많은 행은 `_areas_row_dict`(=`_parse_areas`)가 헤더를 무시하고 canonical
    순서로 매핑하므로, 쓰기측도 **정확히 같은** 인덱스를 잡아야 한다. 그 외는
    `_areas_column_index` 가 준 헤더 인덱스 그대로.
    """
    if len(cells) > len(header_cells) and column in _AREAS_COLUMNS:
        return _AREAS_COLUMNS.index(column)
    return header_idx


def _parse_areas() -> tuple[list[str], list[dict[str, str]]]:
    """areas.md 를 (header 칼럼명 리스트, 데이터 행 dict 리스트) 로 파싱한다.

    헤더-인식: 첫 table row 를 칼럼명(소문자)으로 보고, 이후 데이터 행을
    `{칼럼명: 셀값}` 으로 매핑한다. 누락 칼럼은 빈 문자열. 신/구 스키마 공용.

    **신 스키마 행 관용(하위호환)**: `areas_append` 는 *항상* 그 시점의 완전한
    canonical per-repo row 를 쓴다(6칸 base·7칸 protected). 구 헤더(6칸 base / 5칸
    per-repo `repo|…|owner` / 3칸 멀티-clone `prefix|area|owner`)에 그 *더 넓은* row 가 append 된
    업그레이드 프로젝트에서, 헤더 길이만큼만 매핑하면 `protected`/`base`(또는 `repo` 등)가
    유실/오매핑된다 → **셀 수가 헤더보다 많으면**(=더 넓은 신 스키마 row 를 더 좁은 구 헤더에 붙임)
    헤더와 무관하게 `_AREAS_COLUMNS`(canonical) 순서로 매핑한다(append-only 보존·파일 미수정).
    `== canonical폭` 만 보면 *직전 버전*(6칸)이 append 한 row 가 _AREAS_COLUMNS 가 7칸으로 자란 뒤
    헤더 매핑으로 떨어져 `base` 유실 → `> len(header)` 가 6칸·7칸 신 row 둘 다 보존.
    셀 수가 헤더 이하인 행(구 6/5/3칸 데이터 row)은 자기 헤더로 매핑(현행). areas.md 부재 → ([], []).
    """
    af = areas_file()
    if not af.exists():
        return [], []
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in af.read_text(encoding="utf-8").splitlines():
        cells = _split_areas_row(line)
        if cells is None:
            continue
        if not header:
            header = [c.lower() for c in cells]
            continue
        # 헤더보다 넓은 행 = 신(더 넓은) 스키마 canonical row 를 더 좁은 구 헤더에 append 한
        # 업그레이드 케이스(6칸·7칸 row 를 5/3칸 헤더 아래) → canonical 순서로
        # 매핑해 base/protected 유실 차단. 매핑 규칙은 `_areas_row_dict` 단일 진실(쓰기측
        # `areas_set_cell`·`_migrate_areas_text` 와 공유).
        rows.append(_areas_row_dict(header, cells))
    return header, rows


def _areas_row_for_prefix(prefix: str) -> dict[str, str] | None:
    """활성 prefix 의 areas.md 데이터 행(dict). 미등록/부재 → None."""
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("prefix") == prefix:
            return row
    return None


def _repo_base(repo: str) -> str | None:
    """그 repo 의 areas.md `base` 브랜치. 미지정/미등록/구 스키마 → None.

    `pm-config repo add --base`(또는 clone-time bare HEAD 해소)가 areas.md `base`
    칼럼에 기록한 값을 읽어, worktree 슬롯 브랜치가 *그 base 에서 파생*되게 한다
    (`pm-config worktree add` → `create_slot(base=)`). repo 명은 areas.md `repo` 칼럼과
    매칭한다(repo add 가 `repo=name·prefix=name` 으로 등록하므로 repo==prefix 가 보통).

    None 폴백(worktree add 가 현행 bare HEAD 동작·회귀 0):
      - areas.md 부재(솔로) — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `base` 칼럼 자체가 없는 구 레지스트리(헤더에 base 없음 → 행 dict 에 base 키 없음).
      - `base` 칼럼이 빈 값(부분 등록).
    """
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("repo") == repo:
            return row.get("base") or None
    return None


def _areas_git_url(repo: str) -> str | None:
    """그 repo 의 areas.md `git` 칼럼 URL (bare mirror clone 원). 미등록/부재/빈 값 → None.

    multi-user 공유 채택 폴더(= 하나의 git repo 를 여러 사람이 clone)에서 2번째 사용자가
    `.repos/<repo>.git` bare mirror 를 hydrate 할 때(`pm-config repo add <repo>` — `--git`
    불요) clone 원 URL 을 여기서 해소한다. areas.md(git-tracked·공유)엔 URL 이 있으나
    `.repos/`(gitignore·per-clone)엔 mirror 가 없는 상황 — 등록된 URL 을 재제공 없이 재사용한다.

    repo 명은 areas.md `repo` 칼럼과 매칭한다(prefix 칼럼은 비므로 **repo 명 키**).
    중복 행이면 first-match(`_repo_base`·`_areas_row_for_prefix` 동형·같은 repo=같은 URL 이라
    결정론적).

    None 폴백(호출자 cmd_repo_add 가 fail-loud 또는 `--git` 명시 요구로 전환):
      - areas.md 부재(솔로) — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `git` 칼럼 자체가 없는 구 레지스트리(헤더에 git 없음 → 행 dict 에 git 키 없음).
      - `git` 칼럼이 빈 값(부분 등록).
    """
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("repo") == repo:
            return row.get("git") or None
    return None


def _repo_protected(repo: str) -> list[str]:
    """그 repo 의 보호 브랜치 목록. 미지정/미등록/구 스키마 → `DEFAULT_PROTECTED`.

    areas.md `protected` 칼럼(쉼표분리·예 `main,develop`)을 읽는다. pre-push 훅 설치(sidecar
    채움)·bootstrap 보호 경고가 이 목록으로 PM 의 보호 브랜치 commit/push 를 막는다.

    **default 폴백 = `DEFAULT_PROTECTED`(main/master/develop)** (`_repo_base` 의 None 폴백과
    다름 — 보호는 *안전 기본값이 있어야* 한다·미지정 repo 도 main 을 막는다). 다음 모두
    default 로 떨어진다:
      - areas.md 부재(솔로) — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `protected` 칼럼 자체가 없는 구 레지스트리(헤더에 protected 없음 → 행 dict 에 키 없음).
      - `protected` 칼럼이 빈 값(부분 등록).
    명시 지정이면 쉼표분리·strip·빈 토큰 제거 후 그 목록(전부 빈 토큰이면 default 폴백).
    """
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("repo") == repo:
            raw = row.get("protected") or ""
            branches = [b.strip() for b in raw.split(",") if b.strip()]
            return branches if branches else list(DEFAULT_PROTECTED)
    return list(DEFAULT_PROTECTED)


def _repo_area_owner(repo: str) -> str | None:
    """그 repo 의 areas.md `area_owner`(user 소유) — 미지정/미등록/구 스키마 → None.

    `--mine` 기본 풀(내 area 의 open 티켓) 판정의 입력이다.
    기존 `owner`(per-repo registry registrant)와 의미가 다른 *별도* 칼럼 — overload 금지(codex sug).
    단일 user 토큰이다(목록/구분자 아님). repo 명은 areas.md `repo` 칼럼과 매칭한다
    (repo add 가 `repo=name` 으로 등록).

    None 폴백(현행 동작·회귀 0 — `--mine` 풀 판정이 그 area 를 비소유로 처리):
      - areas.md 부재(솔로) — `_parse_areas()` 가 ([],[]).
      - 그 repo 행이 없음(미등록).
      - `area_owner` 칼럼 자체가 없는 구 레지스트리(헤더에 area_owner 없음 → 행 dict 에 키 없음).
      - `area_owner` 칼럼이 빈 값(부분 등록).
    """
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("repo") == repo:
            return row.get("area_owner") or None
    return None


# ── `--mine` 뷰 필터 ─────────
# 단일 공유 보드 위의 *렌즈*(별도 저장 아님). `board list --mine` 은 두 풀의 합집합:
#   (a) 내 area 의 open — status=open ∧ 그 티켓 area 의 `area_owner` == 내 user.
#   (b) 내 claim — `claimed_by` 의 *user* == 내 user (새 형태) ∨ `claimed_by` == 내 슬롯
#       (legacy 슬롯-only·user 차원 없는 claim). 상태 무관(연속성).
# graceful degrade(핵심): user 미해소(None)이거나 보드에 area_owner 가 운영 중이지
# 않으면(미마이그레이션 채택자·솔로) (a)=전체 open 으로 떨어진다(빈 보드 금지·plain list 처럼).
# `cmd_list` 가 `_area_owner_in_use()`(areas.md 전역 1회 스캔)로 (a) 범위를 정한다 — per-user
# 2축 분기(`_owns_any_area`+`area_filter`)를 전역 플래그 1개로 단순화(동반·사용자 결정
# 2026-06-26: 데이터 정합은 `board migrate-identity` 가 책임·런타임 폴백은 최소).

# 티켓 ID prefix 추출 — `_next_id` ID 발행 규칙의 *정확한 역*:
#   prefixed = `T-{prefix}-{NNN}` (`_next_id` line 1011·prefix 는 리터럴 삽입·끝 -NNN 은 숫자),
#   legacy   = `T-{NNNN}`        (`_next_id` line 1013·하이픈 1개·prefix 없음).
# prefix 문법은 **등록/검증측(`pm_config._REPO_NAME_RE`·`^[A-Za-z0-9][A-Za-z0-9_-]*$`)과 정합**한다
# — repo add·init `--prefix` 가 그 패턴으로 prefix 를 검증·등록하므로 소비측도 같은 grammar 여야
# 한다(소비 grammar 가 등록 grammar 보다 좁으면 `123` 같은 순수-숫자
# prefix 가 등록은 되는데 소비측에서 legacy 로 오인돼 `_ticket_prefix`/wikilink/bootstrap 이 prefix
# 로 인식 못 함). 영숫자로 시작(leading `-` 배제)·이후 영숫자/`_`/`-` — 그래서 `P0`(숫자 포함)·
# `service-a`(family-scope·하이픈)·`123`(순수 숫자) prefix 모두 발행·해소된다(`T-P0-001`·
# `T-service-a-001`·`T-123-001`). 역파서는 끝의 `-NNN`(숫자) 한 마디만 떼고 나머지를 prefix 로 잡는다.
# legacy 와의 구분은 **구조적**으로 유지된다(prefix grammar 가 순수 숫자를 포함해도): full-ID
# regex `^T-(prefix)-\d+$` 가 *내부 하이픈*(prefix-NNN 2세그먼트)을 요구하므로 `T-0164`(하이픈
# 1개)는 매칭 안 됨 → None(legacy), `T-123-001`(하이픈 2개)는 prefix `123` 으로 갈린다. 발행측이
# legacy 를 prefix 없는 `T-NNNN` 단일 하이픈으로만 내므로 이 하이픈-수 경계가 둘을 정확히 가른다.
# ID grammar 의 단일 진실 — prefix 마디 본체. `_TICKET_PREFIX_RE` + prefixed-ID 를 매칭하는 다른
# 파서(`_ticket_id_from_filename`·wikilink lint·bootstrap `_TICKET_ID`)가 *전부* 이 한 조각(또는
# 동형 grammar)을 쓴다 — grammar drift 방지(round-3 클래스: 한 곳 고치면 같은 가정의 다른
# 파서가 어긋남). `P0`(숫자)·`service-a`(하이픈)·`x_y`(언더스코어)·`123`(순수 숫자) prefix 포섭.
_TICKET_PREFIX_BODY = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_TICKET_PREFIX_RE = re.compile(
    rf"^T-(?P<prefix>{_TICKET_PREFIX_BODY})-\d+$")
# prefixed | legacy 둘 다 매칭하는 ID 본체 (anchor 없음 — 호출측이 ^…$·\b 등으로 감싼다).
# 파일명/wikilink 파서가 공유한다(자체 `[A-Za-z]+` regex 두지 말 것 — `P0`/`service-a` 누락).
_TICKET_ID_BODY = rf"T-(?:{_TICKET_PREFIX_BODY}-\d+|\d+)"
# 명시 NEW-ID(`reid`) 형식 sanity 용 full-match 앵커 — 발행 문법(`_TICKET_ID_BODY`)을 그대로
# 감싸 `T-NNNN`·`T-<pfx>-NNN` 둘 다 받고 그 외(빈 문자열·`foo`·`T-`·`X-1`)는 거른다. prefix 마디는
# 소비측 grammar(`_TICKET_PREFIX_BODY`·대문자/하이픈 legacy 포함)라 **자유 입력**이다 — 기존
# 네임스페이스로의 relabel 을 위해 좁은 `_validate_prefix`(소문자 카테고리)를 적용하지 않는다.
# 앵커는 `\A…\Z` — `$` 는 Python 에서 trailing newline 앞에서도 매치해 `T-0250\n`
# 같은 개행-포함 ID 를 통과시킨다(파일명/frontmatter/참조에 깨진 ID 기록 위험). `\Z` 는 문자열 끝에서만.
_FULL_TICKET_ID_RE = re.compile(rf"\A{_TICKET_ID_BODY}\Z")


def _is_valid_ticket_id(tid: str) -> bool:
    """`tid` 가 발행 규칙 ID 문법(`T-NNNN`·`T-<pfx>-NNN`)에 완전 일치하는지 (reid NEW-ID sanity)."""
    return bool(_FULL_TICKET_ID_RE.match(tid))


def _ticket_prefix(tid: str) -> str | None:
    """티켓 ID 에서 네임스페이스 prefix 추출. legacy `T-NNNN`(prefix 없음) → None.

    `_next_id` 의 ID 발행 규칙의 역이다: prefixed = `T-<PREFIX>-NNN`, legacy = `T-NNNN`.
    PREFIX 문법은 등록/검증측(`pm_config._REPO_NAME_RE`·`[A-Za-z0-9][A-Za-z0-9_-]*`)과 정합이라
    숫자(`P0`)·하이픈(`service-a`)·순수 숫자(`123`)를 포함할 수 있고 그런 ID(`T-P0-001`·
    `T-service-a-001`·`T-123-001`)도 해소된다. legacy 4자리 숫자 ID는 full-ID regex
    가 *내부 하이픈*(prefix-NNN)을 요구하는데 하이픈이 1개뿐이라 매칭 안 됨 → None(구조적 구분).
    """
    if not tid:
        return None
    m = _TICKET_PREFIX_RE.match(tid)
    return m.group("prefix") if m else None


# legacy `T-NNNN` 의 순번 추출 — prefixed(`T-<p>-NNN`)는 마지막 하이픈 뒤 숫자로 뗀다.
_LEGACY_TICKET_ID_RE = re.compile(r"^T-(\d+)$")


def _ticket_id_number(tid: str) -> int | None:
    """티켓 ID 의 순번(정수). `T-NNNN`→NNNN / `T-<p>-NNN`→NNN. 미매치→None (prefix list 범위용)."""
    if _TICKET_PREFIX_RE.match(tid):        # prefixed — 순번 = 마지막 `-` 뒤 숫자 마디.
        return int(tid.rsplit("-", 1)[1])
    m = _LEGACY_TICKET_ID_RE.match(tid)
    return int(m.group(1)) if m else None


# 명시 `--prefix` 입력측 sanity — 소비측 grammar(`_TICKET_PREFIX_BODY`·
# 대문자/하이픈 포함·legacy ID 역파싱용)와 **의도적으로 다르다**. 소비측은 기존 발행분(대문자·
# 하이픈 acronym)을 *해소*해야 하므로 넓지만, *새* 카테고리 prefix 입력은 좁게 권장형식으로 못박아
# (작업 카테고리 = 짧은 소문자·언더스코어). 유도/등록된 legacy prefix 는 검증
# 안 한다(cmd_new 는 명시 override 만·cmd_init 은 명시 --prefix 만 검사).
_PREFIX_RESERVED: frozenset[str] = frozenset({"none"})   # `none`=무prefix(T-NNNN) 1급 인자·실 prefix 예약 (case-insensitive)
_PREFIX_FORMAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*$")    # 형식 sanity (영숫자 시작·이후 영숫자/`_`·대소문자 허용)


def _validate_prefix(prefix: str) -> str | None:
    """명시 `--prefix` 형식 sanity — 위반 사유 메시지 반환, 정상이면 None.

    - 예약어(`none`·대소문자 무관): 무prefix(`T-NNNN`) 네임스페이스의 1급 인자로 예약(rename/
      merge 의 from/to/into)이라 실 prefix 로 등록/사용 금지. prefix 동일성이
      case-insensitive fold 이므로 `NONE`/`None` 도 같은 예약어로 fold 돼 거부된다.
    - 형식 `[A-Za-z0-9][A-Za-z0-9_]*`(첫 글자 영숫자·이후 영숫자/`_`): **대소문자 모두 허용**
      (prefix 동일성은 case-insensitive fold 이되 canonical case 는 보존). 하이픈·
      공백·특수문자·빈 문자열은 fail-loud(하이픈은 ID 구분자와 충돌 — 이번 변경 범위 아님).
    """
    if prefix.lower() in _PREFIX_RESERVED:
        return (f"prefix {prefix!r} 은 예약어 — 무prefix(T-NNNN) 네임스페이스의 1급 인자다"
                "(rename/merge·`none` 은 case-insensitive). 실 prefix 로 쓸 수 없다.")
    if not _PREFIX_FORMAT_RE.match(prefix):
        return (f"prefix {prefix!r} 형식 위반 — 작업 카테고리는 "
                "`[A-Za-z0-9][A-Za-z0-9_]*`(첫 글자 영숫자·이후 영숫자/`_`·대소문자 허용·"
                "). 하이픈·특수문자·공백·빈 문자열은 금지"
                "(하이픈은 `T-{prefix}-NNN` ID 구분자와 충돌).")
    return None


def _fold_lookup(prefix: str, pool: set[str]) -> str | None:
    """`pool` 에서 `prefix` 와 case-insensitive fold 로 같은 항목을 찾아 반환·없으면 None.

    prefix 동일성은 소문자 fold 비교다 — 등록/발행된 canonical case 를 대소문자 무관하게 되찾는다
    (`aaa` 입력 → 등록 `AAA` 반환). 첫 매치를 돌려준다(레지스트리는 fold-유일하므로 매치는 최대 1개).
    """
    fold = prefix.lower()
    return next((p for p in pool if p.lower() == fold), None)


def _case_only_conflict(prefix: str, existing: set[str]) -> str | None:
    """`prefix` 가 기존 항목에 *대소문자만 다르게* fold-매치되면 그 기존 항목, 아니면 None.

    정확-case 일치는 None(멱등·같은 항목) — case 만 다른 근접 중복(`aaa` vs 기존 `AAA`)만 잡아
    네임스페이스 분할을 fail-loud 로 막는 데 쓴다(등록·rename/merge dst·repo add). **모든 fold
    매치를 훑는다**(codex must-fix): 이미 오염된 pool 에 exact `aaa` 와 case-only `AAA` 가 함께
    있으면 `_fold_lookup` 의 첫-매치(set 순회·비결정)가 exact 를 먼저 만나 split 를 놓칠 수 있으므로,
    case 가 다른 매치가 하나라도 있으면 결정적으로(정렬 첫) 그것을 충돌로 반환한다.
    """
    fold = prefix.lower()
    conflicts = sorted(p for p in existing if p.lower() == fold and p != prefix)
    return conflicts[0] if conflicts else None


def _fold_key(prefix: str | None) -> str | None:
    """prefix 의 네임스페이스 동일성 키 — 문자열은 소문자 fold, None(무prefix)은 그대로.

    prefix 동일성은 case-insensitive fold 이므로 `AAA`≡`aaa` 는 같은 키(`aaa`)로, legacy 무prefix
    (None)는 None 과만 같은 키다. rename/merge/delete 의 **source-측 매칭·collision 정규화**가 이
    키로 비교해 `T-AAA-*` 를 `aaa` source 로 잡고, `T-AAA-001`/`T-aaa-001` 오염 공존을 collision 으로
    잡는다(canonical case 보존과 별개 — 여긴 *비교* 층이라 소문자 fold 만).
    """
    return prefix.lower() if prefix is not None else None


# ── 티켓 ID 참조 rewriter 코어 ───────────────────
# prefix rename/merge가 소비하는 순수·hermetic 프리미티브. old→new ID 맵을 받아
# 대상 파일(board tickets 본문·wiki/·log/)의 참조를 **토큰단위 정확치환**한다 — frontmatter
# `depends_on`·`[[wikilink]]`·bare inline·산문 임베드(`**A·B(...)**`)가 전부
# 리터럴 ID 토큰이라 한 rewriter 로 전부 커버된다.
#
# 경계 규칙(부분매치 방지): 매치된 old ID 양옆이 식별자 문자면 치환하지
# 않는다 — (1) 뒤 char ∈ `[A-Za-z0-9_-]` → `T-0063` 이 `T-00631`(뒤 숫자)·`T-0063-2`(뒤 하이픈)·
# `T-0063_legacy`(뒤 언더스코어)·`T-0063abc`(뒤 알파벳)에 안 걸림, (2) 앞 char ∈ `[A-Za-z0-9_-]`(lookbehind) →
# `fooT-0063`·`NOT-0063`(앞 식별자 문자)에 안 걸림. old ID 가 리터럴 `T-` 로 시작하는 것만으론
# 왼쪽 부분매치(`fooT-0063`)를 못 막으므로 lookbehind 로 왼쪽 경계를 명시한다(codex must-fix).
# 한글·괄호·공백 등 비-식별자 인접(`[[T-0063]]다음`·`(T-0063)`·`depends_on: T-0063`)은 양옆이
# 경계라 정상 치환된다. 무prefix `T-NNNN`·prefix `T-<p>-NNN` 두 표기형 모두 map 키를 리터럴
# escape 해 자연 포섭한다.
_REWRITE_LEADING_BOUNDARY = r"(?<![A-Za-z0-9_-])"   # old ID 앞 char ∉ [A-Za-z0-9_-] (왼쪽 부분매치 방지)
_REWRITE_TRAILING_BOUNDARY = r"(?![A-Za-z0-9_-])"   # old ID 뒤 char ∉ [A-Za-z0-9_-] (오른쪽 부분매치 방지·`T-0063abc` 차단)


def _rewrite_text_counted(text: str, id_map: dict[str, str]) -> tuple[str, dict[str, int]]:
    """`rewrite_text_token_aware` 의 per-ID 카운트 판 (rewrite_refs 의 N 집계용·내부).

    반환 `(new_text, {old_id: 치환건수})`. 빈 맵/무매치면 `(text, {})`. old ID 를 **길이
    내림차순**으로 alternation 에 넣어 짧은 ID 가 더 긴 ID 를 가리는 것을 막는다(같은 앵커에서
    최장매치 선호). 단일 pass 치환이라 new 값 안에 다른 old 키가 있어도 재치환되지 않는다.
    """
    counts: dict[str, int] = {}
    if not id_map:
        return text, counts
    olds = sorted(id_map, key=len, reverse=True)
    pattern = re.compile(
        _REWRITE_LEADING_BOUNDARY
        + "(?:" + "|".join(re.escape(o) for o in olds) + ")"
        + _REWRITE_TRAILING_BOUNDARY)

    def _sub(m: re.Match[str]) -> str:
        old = m.group(0)
        counts[old] = counts.get(old, 0) + 1
        return id_map[old]

    return pattern.sub(_sub, text), counts


def rewrite_text_token_aware(text: str, id_map: dict[str, str]) -> tuple[str, int]:
    """한 문자열 내 모든 old ID 를 new 로 토큰단위 정확치환·(new_text, 총 치환건수) 반환.

    경계 규칙은 위 섹션 헤더 참조(앞 char ∉ `[A-Za-z0-9_-]`·뒤 char ∉ `[0-9_-]`). `T-0063`→X 는
    `T-00631`·`T-0063-2`·`T-0063_legacy`(오른쪽)·`fooT-0063`·`NOT-0063`(왼쪽)을 건드리지 않는다.
    frontmatter·`[[wikilink]]`·bare·산문 임베드 전 표기형(무prefix `T-NNNN`·prefix `T-<p>-NNN`)을
    한 번에 커버한다.
    """
    new_text, counts = _rewrite_text_counted(text, id_map)
    return new_text, sum(counts.values())


def collect_rewrite_targets(root: str | Path) -> list[Path]:
    """rewrite 대상 `.md` 파일 집합 — `board/tickets/`·`wiki/`·`log/`(root 하위·순서보존 dedup).

    `root` 는 `board/`·`wiki/`·`log/` 를 담는 디렉토리(관례상 `<REPO>/.project_manager`)를
    호출측이 주입한다 — 전역 상태 무의존·테스트가 tmp 트리를 그대로 넘긴다. 각 하위는 있을
    때만 스캔한다(legacy 형상은 tickets/log 가 `wiki/` 안이라 `wiki.rglob` 가 이미 포섭·
    board/ 분리 형상은 `board/tickets` 가 별도 → union). frontmatter·h1 포함 텍스트 전체가
    스캔 대상(depends_on 등 frontmatter 참조도 치환). 파일명 slug rename 은 범위 밖.
    """
    root = Path(root)
    files: list[Path] = []
    for sub in (root / "board" / "tickets", root / "wiki", root / "log"):
        if sub.is_dir():
            files.extend(sub.rglob("*.md"))
    return list(dict.fromkeys(files))  # 순서보존 dedup (겹치는 서브트리 방어적 중복 제거)


def rewrite_refs(root: str | Path, id_map: dict[str, str], *, dry_run: bool,
                 changed_paths: list[Path] | None = None) -> dict[str, int]:
    """대상 파일 전부에 토큰단위 rewrite 적용·규모 집계 반환 (rename/merge 공용 코어).

    반환 `{"ids": N, "refs": M, "files": K}` — N=id_map 중 *실제 참조된* old ID 수, M=총
    치환 refs, K=치환이 일어난 파일 수. `dry_run=True` 면 파일을 기록하지 않고 카운트만 낸다
    (`board.py prefix … --dry-run` 의 "N ID·M refs" preview 원천).

    `changed_paths` 를 주면 **치환이 일어난 파일 경로**를 순서대로 append 한다(반환 dict 는
    불변 — 기존 소비자 무영향). 호출부가 "이 mutation 이 실제로 만진 경로" 를 알아야 스코프
    커밋이 가능한데, 그 집합은 여기서만 알 수 있다. dry_run 에서도 *바뀔* 파일을
    담는다(preview 와 동일 집합).
    """
    referenced: set[str] = set()
    total_refs = 0
    files_changed = 0
    for path in collect_rewrite_targets(root):
        # `newline=""` (universal-newline OFF): 원본 개행을 그대로 읽어 재쓰기 때 보존한다 —
        # CRLF 채택자(Windows 회사 실측)의 파일을 rewrite 가 LF 로 무단 정규화하지 않게 한다.
        # 읽기 실패는 OSError(권한·소실)뿐 아니라 UnicodeDecodeError(비-UTF-8 바이너리·깨진
        # 인코딩)도 포함해 넓게 잡되, **silent 누락 금지** — skip 한 파일을 stderr 로 1줄 경고해
        # 참조가 조용히 남는 것을 표면화한다.
        try:
            with path.open("r", encoding="utf-8", newline="") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  ⚠ rewrite skip {path.name} — 읽기 실패({exc.__class__.__name__}): 참조가 "
                  "남을 수 있으니 수동 확인.", file=sys.stderr)
            continue
        new_text, counts = _rewrite_text_counted(text, id_map)
        if not counts:
            continue
        referenced.update(counts)
        total_refs += sum(counts.values())
        files_changed += 1
        if changed_paths is not None:
            changed_paths.append(path)
        if not dry_run:
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
    return {"ids": len(referenced), "refs": total_refs, "files": files_changed}


def _ticket_area_owner(tid: str) -> str | None:
    """티켓의 area `area_owner`(user 소유) 해소 — 미상이면 None (`--mine` (a) 입력).

    매핑 경로: ID prefix(`_ticket_prefix`) → areas.md 의 그 prefix 행(`_areas_row_for_prefix`)에서
    `area_owner` 를 *직접* 읽는다(`_active_test_cmd`/line 737 의 prefix-행 직접-읽기와 동형).
    미등록 prefix·area_owner 빈값은 None(area 비소유 처리).

    prefix 행에서 직접 읽는 이유(repo 칼럼 경유 재스캔 금지): areas registry 는 prefix-unique 만
    보장하고 repo-unique 는 아니다 — 두 prefix 가 같은 `repo` 칼럼값을 공유하면 `repo` 로 재스캔할
    경우 *그 repo 의 첫 행* area_owner 를 돌려줘 잘못된 소유자가 나온다. prefix 로 이미 정확한 행을
    잡았으니 그 행에서 바로 읽는다(이중 스캔도 제거).

    **no-prefix(솔로 self-host) 폴백 (sole-area)**: 솔로 self-host(
    prefix-불요)는 티켓이 `T-NNNN`(prefix 없음)이라 `_ticket_prefix` None 이다. no-prefix 티켓 ⟹
    솔로 단일-repo(id_prefix None) ⟹ areas registry 의 *단일 area* 가 그 티켓의 area 다 — prefix
    매핑은 multi-repo 메커니즘이므로 솔로엔 sole-area 폴백이 맞다. areas 에 area 가 **정확히 1개**면
    그 단일 area 의 area_owner 를 돌려준다(migration 이 area_owner 를 채운 솔로 보드에서 `--mine`
    (a) 가 no-prefix open 티켓을 잡게). area 가 여러 개면(multi-repo 인데 no-prefix 티켓 = 모순적/
    희귀) 모호하므로 None 유지(기존 동작). prefix 가 *있는* 티켓은 이 폴백을 안 타고 기존 prefix
    경로 그대로(multi-repo 정합·무회귀).
    """
    prefix = _ticket_prefix(tid)
    if not prefix:
        _header, rows = _parse_areas()
        if len(rows) == 1:
            return rows[0].get("area_owner") or None
        return None
    row = _areas_row_for_prefix(prefix)
    if not row:
        return None
    return row.get("area_owner") or None


def _area_owner_in_use() -> bool:
    """areas.md 에 non-empty `area_owner` 행이 **하나라도** 있는가.

    `--mine` (a) 풀(내 area 의 open)을 area_owner 로 좁히는 건 *소유권 데이터가 보드에 실제로
    구성돼 있을 때만* 의미가 있다. 이건 **전역**(per-user 아님) 1회 판정이다 — areas.md 전체를
    한 번 스캔해 `area_owner` 칼럼이 어디든 채워져 있으면 True. 채워져 있으면 area_owner 파티션이
    운영 중(마이그레이션됨·multi-user)이라 (a) 를 area_owner==me 로 좁히고, 비어 있으면(미마이그레이션
    채택자·솔로) (a) 를 전체 open 으로 degrade 한다(빈 보드 금지·plain list 처럼).

    이전 per-user `_owns_any_area(my_user)`(내 소유 area ≥1 인가)를 대체한다 — 데이터 정합은
    마이그레이션 도구(`board migrate-identity`)가 책임지고, 런타임 폴백은 **전역 플래그
    하나**로 최소화한다. area_owner 가 운영 중인데 *내* area 가 0개면
    (a) 는 자연히 빈다 — 그건 회귀가 아니라 '내 area 의 open 이 없음'이라는 올바른 결과다.

    areas.md 부재(솔로)·모든 area_owner 빈 값이면 False. ≥1 채워짐이면 True.
    """
    _header, rows = _parse_areas()
    return any((row.get("area_owner") or "").strip() for row in rows)


def _distinct_area_owners() -> int:
    """areas.md 의 non-empty `area_owner` 칼럼에서 해소되는 *distinct user* 수.

    `_area_owner_in_use`(채워졌나 bool)와 달리 **다중성**을 센다 — `multi_user` 신호의 두 번째
    축이다. `_distinct_ticket_users`(티켓 귀속만 셈)는 다중-owner 보드라도 claim 이 전부 legacy
    슬롯-only(user 토큰 0)면 ≤1 로 떨어져 그 보드를 solo 로 오판한다(→ legacy 슬롯-only 포함 경로가
    발동해 `--slot N` 이 타 area 의 legacy `<repo>_N` 을 suffix 매칭으로 끌어오는 누출).
    areas 에 area_owner 가 2명 이상이면 티켓 user 토큰이 비어도 multi-user 보드다 — 그
    다중성을 여기서 세어 `multi_user = distinct ticket-user >1 OR distinct area_owner >1` 로 solo
    정의를 완결한다. areas.md 부재/전부 빈 값이면 0(솔로 신호 보존·회귀 0).
    """
    _header, rows = _parse_areas()
    return len({owner for row in rows if (owner := (row.get("area_owner") or "").strip())})


def _claimed_by_user(claimed_by: str | None) -> str | None:
    """`claimed_by`(`<user>/<pm-slot>`)에서 *user* 토큰 추출 — 슬롯-only/빈값은 None (codex sug).

    `claimed_by` 는 이제 `<user>/<slot>` 또는 구 슬롯-only(`<slot>`)다.
    user 추출은 **마지막 `/` 분리** 규칙(`rsplit('/', 1)[0]`) — slot 이 마지막 토큰이므로 user 에
    `/` 가 들어가도(이메일은 보통 없지만 안전) 정확히 분리한다. `/` 가 없으면(구 슬롯-only·user
    미상) None 을 반환해 (b) 매칭에서 graceful 제외한다.
    """
    if not claimed_by or "/" not in claimed_by:
        return None
    return claimed_by.rsplit("/", 1)[0] or None


def _created_by_user(created_by: str | None) -> str | None:
    """`created_by`(`<user>/<pm-slot>`·`<user>`·슬롯-only)에서 *user* 토큰 추출.

    `created_by` 는 `identity_tag` 산출(`<user>/<slot>`·session 미바인딩 시 user-only·user 미상
    시 슬롯-only) 또는 `migrate-identity` backfill(부재 → *순수 user*·line `_migrate_ticket_fm`)이다.
    `_ticket_owner` 의 2차 폴백 소유자(area_owner 미해소 시 항상-존재 소유)로 쓴다.

    `_claimed_by_user`(슬롯-only=None)와 갈리는 지점: **`/` 없는 값을 user 로 본다** — backfill 이
    부재 created_by 를 슬롯 없는 순수 user 로 채우므로(다중사용자 보드 migrate-identity 후 흔함) 그
    항상-존재 소유자를 살려야 유출을 없앤다.
    `<user>/<slot>` 은 마지막 `/` 분리로 user 를 뽑고, 빈/None 은 None(미상). 드물게 `/` 없는 슬롯-only
    created_by(user 미상 생성)를 user 로 오인할 수 있으나 — 그런 보드는 다중사용자 신호가 안 서면
    solo degrade 라 무해하고, 서면 strict-exclude 라 안전하다(회귀 0).
    """
    if created_by is None:
        return None
    cb = str(created_by).strip()
    if not cb:
        return None
    if "/" in cb:
        return cb.rsplit("/", 1)[0] or None
    return cb


def _created_by_session(created_by: str | None) -> str | None:
    """`created_by`(`<user>/<session>`·`<user>`·슬롯-only)에서 *session* 토큰 추출.

    session = `created_by` 의 `/` 뒤 마지막 토큰(`<repo>_<N>` 슬롯 세션 또는 task 이름). 세션 기본
    뷰(무인자 `list`·명시 세션 뷰)의 "내 스트림 open" 판정에 쓴다 — 현 세션(`session_name`)과
    이 값이 일치하는 open 이 내 스트림이다. 이전 task-prefix 판정을 대체한다(prefix 는 ID 라벨일
    뿐 스트림 판정 아님).

    **`/` 없는 값의 모호성**: `identity_tag` 는 (a) 세션 미바인딩 발행 시 순수 **user**(user 해소·슬롯
    부재)·(b) **user 미해소** 발행 시 순수 **session**(슬롯/task-only)을 둘 다 slash 없이 낸다 — 두
    경우가 겹친다. 슬롯 세션 예약 패턴(`<repo>_<N>`·task 명은 이 패턴 금지)으로 기계 판별한다:
    slash 없는 값이 그 패턴에 부합하면(`_repo_from_session` non-None) **session 토큰**으로 취급해
    user-미해소로 슬롯-발행한 open 이 자기 세션 뷰에서 소실되는 것을 막는다. 부합하지 않으면
    (순수 user·`<repo>_<N>` 아닌 task-only) None — 세션 미상이라 어느 세션 스트림에도 안 든다
    ([[prefer-data-migration-over-fallback]]로 backfill 대상).

    `_created_by_user`(user 토큰)와의 대칭이 깨지는 지점: 저쪽은 slash 없는 값을 전부 *user* 로
    보지만, 이쪽은 slot-session 패턴에 부합하면 *session* 으로 본다 — user 식별자가 우연히
    `<repo>_<N>` 형태면 양쪽이 겹쳐 오분류할 수 있으나, 그건 예약 체계가 이미 수용하는 동일
    클래스의 모호성(슬롯 세션 이름공간 예약)이다.
    """
    if created_by is None:
        return None
    cb = str(created_by).strip()
    if not cb:
        return None
    if "/" in cb:
        return cb.rsplit("/", 1)[-1] or None
    # slash 없음 — user-only(세션 미바인딩)와 slot-only(user 미해소)가 겹치는 모호 값. 슬롯 세션
    # 예약 패턴(`<repo>_<N>`)에 부합하면 session 토큰으로 판별(user-미해소 슬롯 발행 open 소실 방지).
    return cb if _repo_from_session(cb) is not None else None


def _slot_matches(claimed_by: str, my_slot: str, *, mode: str = "exact") -> bool:
    """`claimed_by`(`<user>/<pm-slot>` 또는 legacy 슬롯-only)의 slot 토큰이 `my_slot` 인가.

    `mode="exact"`(기본·`--mine`/`--repo X --slot N`): 완전 일치 — slot 식별자 전체를 안다.
    `mode="repo"`(`--repo X` 단독·view/actor repo-scope): slot 규칙
    (`work/<repo>_<N>` → 세션 이름 `<repo>_<N>`)상 `my_slot` 이 repo 이름이므로, slot 토큰의
    repo(`_repo_from_session`·repo 명 `_` 안전·`--repo project` 가 `project_manager_1` 오매칭 안 함)가
    `my_slot`(그 repo 의 어느 슬롯이든) 이거나 토큰 자체가 `my_slot`(드문 비-숫자
    커스텀 슬롯)이면 매칭한다 — "그 repo 의 내 슬롯 전체".

    (구 `suffix=True` — 숫자 N 만으로 repo 불문 cross-repo 매칭하던 bare `--slot N` 뷰는
    `--slot` 은 `--repo` 없이는 fail-loud
    """
    if not claimed_by:
        return False
    slot_token = claimed_by.rsplit("/", 1)[-1]
    if mode == "repo":
        return slot_token == my_slot or _repo_from_session(slot_token) == my_slot
    return slot_token == my_slot


def _ticket_owner(fm: dict, area_owner_in_use: bool) -> str | None:
    """open 티켓의 소유 user — `area_owner`(1차) ?? `created_by.user`(2차·항상-존재).

    세션 뷰 (a) "내 소유 open" 판정의 소유자를 완전-데이터로 해소한다:
      - area_owner 파티션이 운영 중(`area_owner_in_use`)이면 그 티켓 area 의 `area_owner`
        (`_ticket_area_owner`)를 1차로 쓰고, 미운영/미해소(경계-교차·미등록 area·미마이그 채택자)면
      - `created_by` 의 user(`_created_by_user`·항상-존재 폴백)로 떨어진다.
    둘 다 미상이면 None — 호출부(`_ticket_is_mine`)가 solo=degrade / multi=strict 로 가른다.
    미해소 read 유출을 없앤다([[prefer-data-migration-over-fallback]]).
    """
    tid = fm.get("id") or ""
    area_owner = _ticket_area_owner(tid) if area_owner_in_use else None
    return area_owner or _created_by_user(fm.get("created_by"))


def _distinct_ticket_users() -> int:
    """보드 전체 티켓의 `created_by`/`claimed_by` 에서 해소되는 *distinct user* 수.

    **데이터-유도 다중사용자 신호**다(config 플래그 아님) — 세션 뷰 격리(`_ticket_is_mine`)가 소유
    미해소 open 을 solo(≤1)면 all-open degrade(회귀 0), 다중(≥2)이면 strict-exclude 로 가르는 게이트다.
    전 status 디렉토리를 1회 스캔해 `created_by`(→`_created_by_user`)·`claimed_by`(→`_claimed_by_user`)의
    user 토큰을 집합에 모아 크기를 센다 — 슬롯-only claimed_by·미상은 집합에 안 든다(graceful). 깨진
    티켓은 신호 산정에서 skip(fail-soft·크래시 0). areas 의 area_owner 가 아니라 *티켓* 귀속만 세는 건
    정의(소유 데이터가 티켓에 실려야 다중사용자로 본다)다.
    """
    users: set[str] = set()
    for status in STATUS_DIRS:
        for p in (tickets_dir() / status).glob("T-*.md"):
            try:
                fm, _ = load_ticket(p)
            except (OSError, ValueError, yaml.YAMLError):
                continue
            cb_user = _created_by_user(fm.get("created_by"))
            if cb_user:
                users.add(cb_user)
            claim_user = _claimed_by_user(fm.get("claimed_by"))
            if claim_user:
                users.add(claim_user)
    return len(users)


def scan_task_tickets(user: str | None, task: str,
                      prefix: str | None = None) -> dict[str, list[dict]]:
    """task end 소진 게이트용 티켓 스캔 (자체 스캔·조회 전용·부작용 0).

    `board list --task` 렌즈(wave 3)가 아직 없으므로 `pm-config task end` 가 이걸로
    자체 스캔한다. 두 축을 한 번의 status 디렉토리 순회로 모은다:

      - **claimed** — **진행 중(open/claimed/blocked)** 티켓 중 `claimed_by` 의 slot 토큰(마지막 `/`
        뒤·`_slot_matches`)이 `task` 와 일치하는 것. `task` 세션의 claim 형태 = `<user>/<task>`
        (identity_tag slot 값 `<repo>_<N>` 예약과 기계 판별·task 명은 자유 포맷). `user` 가 주어지고
        claimed_by 에 user 토큰이 있으면 그 user 도 일치해야 한다(교차사용자 동명 task 오귀속 방지).
        slot-only claim(user 토큰 없음)은 graceful 포함. **terminal status(`done`)는 제외한다(must-fix
        ①)** — `cmd_complete` 는 done 이동 시 `claimed_by` 를 지우지 않으므로(status/completed_at 만),
        `<user>/<task>` 로 claim→complete 한 티켓이 done/ 에 claimed_by 를 남긴다. done 을 담으면 그
        티켓은 `unclaim`(status=="claimed" 요구)도 불가라 **해소 수단이 없어 task end 가 영구 차단**된다
        ("해소=complete 또는 unclaim"). 완료된 작업은 소진 게이트 대상이 아니다. 이게
        비어야 task end 가 반납/이동으로 진행한다(거부 게이트).
      - **prefix_open** — `prefix` 가 주어지면 그 prefix(`_ticket_prefix`)의 `open` 티켓. **정보
        표시만**(차단 안 함·prefix≠경계) — task 지정 prefix 의 backlog 를 참고로 보여줄 뿐.

    각 row = {"id", "title", "status"}. 깨진 티켓은 skip(fail-soft). board 를 import 하지 않는
    pm_config 가 `_load_module` 로 로드해 소비한다(isolation·ticket-스캔 단일 진실=board).
    """
    claimed: list[dict] = []
    prefix_open: list[dict] = []
    for status in STATUS_DIRS:
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            try:
                fm, _ = load_ticket(p)
            except (OSError, ValueError, yaml.YAMLError):
                continue
            tid = fm.get("id") or p.stem
            row = {"id": tid, "title": fm.get("title") or "", "status": status}
            cb = fm.get("claimed_by") or ""
            # terminal status(done)는 claimed 축에서 제외 — done 은 claimed_by 잔존이라도 완료됨이고
            # 해소 수단(unclaim=claimed 전용)이 없어 담으면 task end 영구 차단.
            if status != "done" and cb and _slot_matches(cb, task):
                cb_user = _claimed_by_user(cb)
                if user is None or cb_user is None or cb_user == user:
                    claimed.append(row)
            if prefix and status == "open" and _ticket_prefix(tid) == prefix:
                prefix_open.append(row)
    return {"claimed": claimed, "prefix_open": prefix_open}


def _ticket_is_mine(status: str, fm: dict, my_user: str | None,
                    my_slot: str, area_owner_in_use: bool, multi_user: bool,
                    *, slot_mode: str = "exact", slot_scoped: bool = False) -> bool:
    """이 티켓이 `--mine`/`--repo`/`--slot` 뷰에 들어가는지 — (b) 내 claim ∨ (a) 내 소유 open.

    **단일 불변식**(전 surface 수렴·point-patch 금지): 필터 뷰 멤버십 = (내
    claim) ∪ (내 소유 open). 타 사용자의 claim·미claim open 은 **어떤 필터 뷰에도 안 나온다** —
    전체는 `list --all` 전용(기존 무필터 무인자 `list` 의 이관). querying identity(`my_user`)는 항상 **현재 사용자**(cmd_list 가
    `user_name()` 해소)다. degrade("전체 open=mine")는
    **solo(distinct user ≤1·`multi_user` False)에서만** 허용한다.

    (b) 내 claim — 상태 무관 연속성 (user-first). `claimed_by` 의 user 토큰 유무로 가른다:
      - **user-qualified**(`_claimed_by_user(cb)` non-None·`<user>/<slot>`): 내 것 iff `cb_user ==
        my_user`. **slot-scoped 뷰**(`--repo`/`--slot`)면 `_slot_matches` AND 로 *내 것
        ∩ 그 슬롯(또는 그 repo 의 내 슬롯 전체)*(타 슬롯의 내 claim 은 slot 뷰서 제외·`--mine`
        엔 나옴). **비제약 뷰**(`--mine`)면 user 만(전 슬롯). 타 사용자 claim(user 불일치)·my_user
        미해소(귀속 불가)는 제외 — 남의 user-claim 을 slot 번호로 끌어오는 누출 0.
      - **legacy 슬롯-only**(`cb_user is None`·user 토큰 없음): **진짜 solo(distinct user ≤1·
        `not multi_user`)에서만** `_slot_matches`(내 슬롯)로 포함한다. 게이트가 `my_user is None` 이
        아니라 `not multi_user` 인 건 `user_name()` 이 git email 폴백으로 solo 도 my_user 를 해소할
        수 있어(흔함) — my_user proxy 면 그 solo 의 자기 슬롯 legacy claim 을 잘못 숨긴다.
        multi_user 면 legacy 는 ambiguous → strict-exclude(migrate-identity backfill).
    (a) 내 소유 open — status==open 한정. `owner = _ticket_owner(fm, area_owner_in_use)`
        (area_owner ?? created_by.user):
      - my_user·owner 둘 다 해소 → strict `owner == my_user`(유출 0·유일 포함 규칙).
      - 미해소(my_user None ∨ owner None) + `multi_user` → `return False`(strict-exclude).
      - 미해소 + solo(¬multi_user) → `return True`(all-open degrade 보존·빈 보드 금지·회귀 0).
    open 은 미claim 이라 슬롯이 없다 — slot-scoped 뷰에서도 (a) 는 슬롯 무관 backlog(`--mine` 과
    동일 풀)로 두고 슬롯으로 좁히지 않는다.
    """
    cb = fm.get("claimed_by") or ""
    # (b) 내 claim — user-first. user 토큰 유무로 가른다.
    if cb:
        cb_user = _claimed_by_user(cb)
        if cb_user is not None:
            # user-qualified claim(`<user>/<slot>`) — 내 것 iff user 일치(+ slot-scoped 면 그 슬롯).
            # my_user 미해소(None)면 귀속 불가 → 미포함(남의 user-claim 을 slot 번호로 끌어오는 누출 0).
            if my_user is not None and cb_user == my_user and (
                    not slot_scoped or _slot_matches(cb, my_slot, mode=slot_mode)):
                return True
        elif not multi_user and _slot_matches(cb, my_slot, mode=slot_mode):
            # legacy 슬롯-only claim(user 토큰 없음) — **진짜 solo(distinct user ≤1·not multi_user)**
            # 에서만 slot 매칭으로 포함. 게이트가 `my_user is None` 이 아니라 `not multi_user` 인 건
            # `user_name()` 이 git email 폴백으로 solo 도 my_user 를 해소할 수 있어(흔함) my_user
            # proxy 면 그 solo 의 자기 슬롯 legacy claim 을 잘못 숨기기 때문이다. multi_user 면
            # legacy 는 ambiguous → strict-exclude(migrate-identity backfill).
            return True
    # (a) 내 소유의 open.
    if status == "open":
        owner = _ticket_owner(fm, area_owner_in_use)
        if my_user and owner:
            return owner == my_user       # 소유 해소 → strict(유출 0)
        if multi_user:
            return False                  # 다중사용자 + 미해소 → strict-exclude
        return True                       # solo → all-open degrade 보존(회귀 0)
    return False


def _in_default_view(status: str, fm: dict, my_user: str | None,
                     my_session: str | None, area_owner_in_use: bool,
                     multi_user: bool) -> bool:
    """세션 기본 뷰(무인자 `list`·명시 세션 뷰) 멤버십 — **내 user ∧ session 스트림**.

    세션의 기본 화면은 그 세션이 **생성한 open + 그 세션 claim** 만 출력한다 — 단,
    user-qualified 귀속은 **현재 user 도 일치**해야 한다. 동명 task(`alice/main` 대 `bob/main`)와
    슬롯 재대여 뒤 이전 보유자 티켓이 같은 session 라벨만으로 섞이지 않게 open/claim 양쪽을
    user ∧ session 복합축으로 판정한다(전체는 명시 `--all`).

      - **바인딩 세션**(`my_session` 해소): 먼저 open 은 `_created_by_session`, claim(비-open)은
        `_slot_matches` exact 로 session 을 맞춘다. user-qualified 값은 각각 `_created_by_user` /
        `_claimed_by_user` 로 user 를 해소해 `my_user` 와도 strict 일치해야 한다.
      - **user 미해소 또는 legacy user 토큰 부재**: 다중사용자(`multi_user`)면 모호한 귀속을
        strict-exclude하고, solo 면 session-only 로 degrade한다. claim 분기는 의도적으로
        `_ticket_is_mine` 보다 관대하다. solo에서 `my_user` 만 미해소된 user-qualified claim은
        기본 뷰에 보이지만 `--mine`에서는 제외된다. 조회 정체성을 잠시 못 구했다는 이유로 자기
        세션 티켓을 숨기지 않기 위한 보호이며, 다중사용자에서는 이 완화를 허용하지 않는다.
      - **무바인딩/솔로**(`my_session` None): user-단위 폴백 = `--mine`(내 소유 open + 내 claim·
        전 슬롯). solo=subset·특례 아님 — N=1 이면 user 스트림=세션 스트림이라 등가([[solo-is-
        subset-of-multipm]]). `_ticket_is_mine`(slot_scoped=False)를 그대로 상속한다.
    """
    if not my_session:
        # 무바인딩/솔로 — user-단위 폴백(--mine 동형·전 슬롯). open 소유·claim 판정은 단일
        # predicate 재사용(point-patch 금지). my_slot="" 은 slot 매칭 미발동(무바인딩=슬롯 없음).
        return _ticket_is_mine(status, fm, my_user, "", area_owner_in_use,
                               multi_user, slot_mode="exact", slot_scoped=False)
    if status == "open":
        created_by = str(fm.get("created_by") or "").strip()
        if _created_by_session(created_by) != my_session:
            return False
        # 바인딩 세션 스트림은 생성자 축으로 판정한다. area_owner 는 backlog 소유 축이라
        # 의도적으로 참여시키지 않는다.
        # `_created_by_user` 는 slash 없는 migrate backfill 값을 user 로도 해석하므로, `/` 유무는
        # user-qualified 형상인지(legacy session-only인지)만 가른다. 토큰 파싱은 기존 헬퍼가 맡는다.
        created_user = _created_by_user(created_by) if "/" in created_by else None
        if my_user is not None and created_user is not None:
            return created_user == my_user
        return not multi_user
    # 비-open(claimed/blocked/done) — claim session AND user. `_claimed_by_user` 가 None 인
    # legacy 슬롯-only와 my_user 미해소는 multi-user strict-exclude / solo session-only degrade.
    claimed_by = fm.get("claimed_by") or ""
    if not _slot_matches(claimed_by, my_session, mode="exact"):
        return False
    claimed_user = _claimed_by_user(claimed_by)
    if my_user is not None and claimed_user is not None:
        return claimed_user == my_user
    return not multi_user


def registered_prefixes() -> set[str]:
    """Prefixes registered in areas.md (shared registry). Empty set if no registry.

    The registry's *existence* is the multi-repo (N×M·prefix 네임스페이스) mode
    signal — when present, `board.py new` requires a registered prefix (see
    cmd_new guard). solo(N=1·M=1)는 레지스트리 부재 → 가드 off.

    헤더-인식 파서(`_parse_areas`)로 `prefix` 칼럼을 읽는다 — 구 스키마
    (`| prefix | … |`)와 신 스키마(`| repo | prefix | … |`) 모두에서
    prefix 칼럼 위치에 상관없이 동작한다.
    """
    _header, rows = _parse_areas()
    return {p for row in rows if (p := row.get("prefix"))}


def registered_repos() -> set[str]:
    """Repo names registered in areas.md (per-repo registry `repo` 칼럼). 부재→빈 set.

    `registered_prefixes` 의 repo-명 짝. `repo add`(pm_config)가 멱등 재등록을 판별할 때
    쓴다 — repo명 자동시드 폐지후 prefix 칼럼이 비므로 repo 존재 여부를 prefix
    로 셀 수 없다(빈 prefix 는 `registered_prefixes` 에 안 잡힘). repo 칼럼으로 직접 세어
    중복 등록(같은 repo 두 번 append)을 막는다.
    """
    _header, rows = _parse_areas()
    return {r for row in rows if (r := row.get("repo"))}


def areas_append(prefix: str, area: str, owner: str,
                 *, repo: str | None = None, git: str | None = None,
                 test_cmd: str | None = None, base: str | None = None,
                 protected: str | None = None, area_owner: str | None = None) -> None:
    """Register a prefix in areas.md (append-only; create with header if missing).

    Append-only + `merge=union` (.gitattributes) → concurrent registrations from
    different clones never conflict.

    헤더 최초 생성(if-absent) + row append 를 **하나의 `board_lock()`** 구간으로
    원자화한다. 락이 없으면 동시 최초 등록 2개가 둘 다 "not exists" 를
    보고 → 둘 다 헤더를 write_text 해 한쪽이 다른쪽 append row 를 클로버한다(row 만
    O_APPEND 라도 헤더 race 가 남음). 락으로 감싸면 동시 최초 등록에도 헤더 1회·모든
    row 보존.

    스키마: per-repo 레지스트리
    `| repo | prefix | git | test_cmd | owner | base | protected | area_owner |`.
    `owner` = **등록 식별자(registrant)** — 협업 소유자(다중-사람)가 아니라 single user
    의 등록 출처 표식이다. 기본 = 현 세션. 컬럼/형식은 보존
    (test_path 바인딩·regression 게이트가 의존) — 의미만 재정의.
    `repo`/`git`/`test_cmd`/`base`/`protected`/`area_owner` 미지정 시 빈 칼럼으로 채운다
    (부분 등록 허용·하위호환). `base`는 worktree 슬롯 브랜치가 파생될 base 브랜치
    — 빈 값/누락이면 `_repo_base` 가 None 폴백(worktree add 가 현행 bare HEAD 동작).
    `protected`는 PM 이 자율 commit/push 못 하는 보호 브랜치(쉼표분리) — 빈 값/
    누락이면 `_repo_protected` 가 `DEFAULT_PROTECTED`(main/master/develop) 폴백.
    `area_owner`는 그 area 의 *user* 소유(`--mine` 풀 입력) — `owner`
    (registrant)와 별개 칼럼(overload 금지). 빈 값/누락이면 `_repo_area_owner` None 폴백.
    `area`(구 스키마 칼럼)는 신 스키마에 칼럼이 없어 무시한다 — 호출 시그니처는
    하위호환을 위해 유지(기존 `cmd_init`·테스트가 positional 로 area 를 넘김).

    **재진입 금지**(board_lock docstring) — board_lock 보유 중에는 부르지 않는다.
    유일 호출자 `cmd_init` 은 락 밖에서 부른다.
    """
    _repo = repo if repo is not None else prefix  # repo 미지정 시 prefix 를 repo 명으로
    _git = git or ""
    _test = test_cmd or ""
    _base = base or ""
    _protected = protected or ""
    _area_owner = area_owner or ""
    af = areas_file()
    with board_lock():
        if not af.exists():
            af.write_text(
                "# Area Registry\n\n"
                "> per-repo 레지스트리: repo → prefix → git → "
                "test_cmd → owner → base → protected → area_owner. 멀티-PM ID 네임스페이스 + "
                "per-repo 테스트 경로 + worktree base 브랜치 + 보호 브랜치 + user 소유의 단일 진실. "
                "append-only (`merge=union`).\n"
                "> `board.py init` / `pm-config repo add` 가 등록. "
                "prefix 유일성 = race-free ID 의 전제.\n\n"
                + _areas_header_line() + "\n"
                + _areas_separator_line() + "\n",
                encoding="utf-8")
        # O_APPEND atomic append — areas 는 append-only 레지스트리이므로
        # read-modify-write 가 아니라 OS 가 보장하는 원자 추가로 동시 등록 충돌을 없앤다.
        file_lock.append_atomic(
            af,
            f"| {_repo} | {prefix} | {_git} | {_test} | {owner} | {_base} "
            f"| {_protected} | {_area_owner} |\n")


# ── areas.md 셀 변경 ──────────────────────────────────────────────
# "append-only" 는 **등록(행 추가)** 의 동시성 불변식이지 불변 파일 규약이 아니다 — *기존 셀
# 변경* 은 `board_lock()` 하 비파괴 in-place 재기록으로 한다(`_migrate_areas_text` 의 선례와
# 같은 관용구: 줄 종결자·주석·타 행 보존 + 구 헤더 canonical 업그레이드 + wider-row canonical
# 인덱스). 중복 repo 행은 조용한 first-match 고착을 낳으므로 **fail-loud**(부작용 0)로 거부하고
# `board lint` 의 `areas-duplicate-repo` advisory 가 가시화한다.


class AreasRepoNotFound(Exception):
    """areas.md 에 그 repo 행이 없다 — 셀 변경 대상 부재 (부작용 0).

    등록되지 않은 repo(또는 areas.md 부재)의 칼럼을 바꾸려 한 경우다. 등록은 `pm-config repo
    add`(append) 소관이고 setter 는 *기존 셀만* 고친다 — 없는 행을 만들어내지 않는다.
    """

    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(f"areas.md 에 repo {repo!r} 행이 없다")


class AreasDuplicateRepo(Exception):
    """areas.md 에 같은 repo 행이 2개 이상 — 셀 변경 fail-loud (부작용 0).

    first-match 리졸버(`_repo_protected`·`_repo_base`·`_areas_git_url`·`_repo_area_owner`)가
    첫 행에서 return 하므로 어느 행을 고쳐야 하는지 기계가 정할 수 없다. 추측해서 한쪽만 고치면
    사용자가 바꿨다고 믿는 값과 리졸버가 읽는 값이 갈린다 → **아무것도 쓰지 않고** 수동 정리를
    안내한다(자동 병합 안 함·사람 판정). `count` = 중복 행 수(진단용).
    """

    def __init__(self, repo: str, count: int):
        self.repo = repo
        self.count = count
        super().__init__(f"areas.md 에 repo {repo!r} 행이 {count}개 있다(중복)")


def _areas_set_cell_text(text: str, repo: str, column: str,
                         value: str) -> tuple[str, str]:
    """areas.md 텍스트에서 `repo` 행의 `column` 셀을 `value` 로 바꾼 (새 텍스트, 옛 값) 반환.

    순수 변환(IO 없음·`_migrate_areas_text` 동형 관용구):
      - 줄 종결자(LF/CRLF)·주석·빈 줄·표 밖 텍스트·**다른 행**은 verbatim 보존.
      - 헤더에 그 칼럼이 없으면(구 레지스트리) `_areas_column_index` 계획대로 헤더 + 헤더 직후
        구분선 1개를 업그레이드한다(canonical 8칼럼 또는 기존 헤더 끝 append·비파괴).
      - 행 매칭은 `_areas_row_dict`(= `_parse_areas` 와 같은 매핑)의 `repo` 값으로 한다 —
        읽기측이 보는 행과 쓰기측이 고치는 행이 **항상 같다**.
      - 대상 셀 인덱스는 `_areas_row_cell_index`(wider-row canonical 보정)로 잡는다.
      - 셀이 모자라면 빈 칸으로 패딩해 인덱스를 확보한다(비파괴 append).

    매칭 행이 0개면 `AreasRepoNotFound`, 2개 이상이면 `AreasDuplicateRepo` — **호출자가 쓰기
    전에** 터지므로 부작용 0이다.
    """
    lines = text.splitlines(keepends=True)
    header_cells: list[str] | None = None
    col_idx: int | None = None
    upgrade = False
    sep_cols = 0
    sep_replaced = False
    matches = 0
    old_value = ""
    out: list[str] = []
    for line in lines:
        nl = ""
        body = line
        if line.endswith("\r\n"):
            nl, body = "\r\n", line[:-2]
        elif line.endswith("\n"):
            nl, body = "\n", line[:-1]
        cells = _split_areas_row(body)
        if cells is None:
            if upgrade and not sep_replaced and _AREAS_SEP_RE.match(body.strip()):
                out.append("|" + "|".join("---" for _ in range(sep_cols)) + "|" + nl)
                sep_replaced = True
            else:
                out.append(line)
            continue
        if header_cells is None:
            header_cells = [c.lower() for c in cells]
            col_idx, new_header, sep_cols = _areas_column_index(header_cells, column)
            if new_header is None:
                out.append(line)
            else:
                upgrade = True
                out.append(new_header + nl)
            continue
        if _areas_row_dict(header_cells, cells).get("repo") != repo:
            out.append(line)
            continue
        matches += 1
        idx = _areas_row_cell_index(header_cells, cells, col_idx or 0, column)
        if matches == 1:
            old_value = cells[idx] if idx < len(cells) else ""
        while len(cells) <= idx:
            cells.append("")
        cells[idx] = value
        out.append("| " + " | ".join(cells) + " |" + nl)
    if matches == 0:
        raise AreasRepoNotFound(repo)
    if matches > 1:
        raise AreasDuplicateRepo(repo, matches)
    return "".join(out), old_value


def areas_set_cell(repo: str, column: str, value: str) -> tuple[str, str]:
    """areas.md 의 `repo` 행 `column` 셀을 `value` 로 in-place 교체한다 — (옛 값, 새 값) 반환.

    `board_lock()` 하 read→transform→atomic write(temp + `os.replace`) 이다 — `areas_append`
    (O_APPEND 등록)와 같은 락으로 직렬화해 동시 등록의 lost-update 를 막는다(
    `_migrate_areas_apply` 동형). 변환이 no-op(값 동일·`_areas_set_cell_text` 결과가 원문과
    같음)이면 **쓰지 않는다**(멱등).

    `column` 은 `_AREAS_COLUMNS` 의 칼럼명이어야 하고, `value` 는 표를 깨는 `|`·개행을 담을 수
    없다(둘 다 `ValueError`). 대상 행 부재 → `AreasRepoNotFound`, 중복 행 → `AreasDuplicateRepo`
    (둘 다 쓰기 이전·부작용 0).

    `value` 는 **strip 해서** 기록한다 — 범용 백엔드라 호출자가 정규화했다고 가정할 수 없고,
    `"  main , release  "` 를 verbatim 쓰면 셀 앞뒤 공백이 표에 남는다(`_parse_areas` 는 셀을
    strip 해 읽으므로 읽기값은 같지만 파일이 지저분해진다). 반환 `(old, new)` 의 new 도 그 값이다.

    **재진입 금지**(board_lock docstring) — board_lock 보유 중에는 부르지 않는다.
    """
    if column not in _AREAS_COLUMNS:
        raise ValueError(
            f"알 수 없는 areas.md 칼럼 {column!r} — 허용: {', '.join(_AREAS_COLUMNS)}")
    if "|" in value or "\n" in value or "\r" in value:
        raise ValueError(
            f"areas.md 셀 값에 `|`/개행을 쓸 수 없다(표 corruption): {value!r}")
    value = value.strip()
    af = areas_file()
    with board_lock():
        # `newline=""`(universal-newline OFF) 로 읽고 쓴다 — CRLF 채택자(Windows)의 파일을
        # 셀 하나 바꾸려다 LF 로 무단 정규화하지 않게(`rewrite_refs` 와 같은 관용구).
        text = ""
        if af.exists():
            with af.open("r", encoding="utf-8", newline="") as fh:
                text = fh.read()
        new_text, old_value = _areas_set_cell_text(text, repo, column, value)
        if new_text != text:
            # atomic write — 같은 디렉토리 temp + os.replace(부분기록/crash 잔재 방지).
            # write 실패(디스크풀·권한)로 replace 에 못 가면 `.tmp` 가 남으므로 finally 로 청소한다
            # (다음 실행이 stale temp 를 만나지 않게·잔재 0).
            tmp = af.with_suffix(af.suffix + ".tmp")
            try:
                with tmp.open("w", encoding="utf-8", newline="") as fh:
                    fh.write(new_text)
                os.replace(str(tmp), str(af))
            finally:
                # 성공 시 os.replace 로 tmp 는 이미 사라졌다(missing_ok 로 no-op).
                tmp.unlink(missing_ok=True)
    return old_value, value


# ── 보드 동시성 ────────────────────────────────────────────────
# 단일 루트 동시 세션이 공유 `.project_manager` 파일을 안전하게 쓰게 한다.
#   - board_lock: OS 파일락 — ID 발행(new)·공유 단일파일 write(board.md) 직렬화.
#     프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock 없음).
#   - file_lock.append_atomic: O_APPEND — log/areas 같은 append-only 파일의 원자 추가.
#   - claim(`cmd_claim` 의 load→rename 임계구역)도 board_lock 으로 직렬화한다 — POSIX rename 은
#     원자적이나 Windows os.rename 은 동시 프로세스에 배타적이지 않아
#     락으로 배타성을 복원한다(패배자는 깨끗한 `claim race lost`). complete/block 같은 비경합
#     전이(단일 소유 ticket)는 race 가 없어 락 없이 rename 만 쓴다.
#
# 크로스플랫폼(stdlib-only — 런타임 의존은 PyYAML 뿐): POSIX=fcntl.flock,
# Windows=msvcrt.locking. 둘 다 없으면 단일-머신 전제의 무락 폴백(락 파일만 생성).
# 그 플랫폼 분기도, O_APPEND 원자 추가도 공용 `file_lock` seam 이 소유한다 —
# 엔진 도구가 같은 구현을 쓰고(board 는 모듈 상단 `file_lock` 바인딩), board 는 *어느 파일에
# 언제 거는지·무엇을 붙이는지*(경로 규약·락 순서·행 포맷)만 정한다.


@contextlib.contextmanager
def board_lock() -> Iterator[None]:
    """공유 보드 write 를 직렬화하는 OS 파일락 컨텍스트매니저.

    `.project_manager/.local/board.lock` 에 배타 OS 락을 건다. **프로세스가 죽으면
    OS 가 락을 자동 해제**하므로 stale-lock 이 없다(worktree 리스의 pid-회수와 수명이
    다른 이유). 읽기(list/show)는 락을 잡지 않는다 — *변경* 경로만 직렬화한다.

    **재진입 금지** — 같은 프로세스가 이 컨텍스트를 중첩하면 안 된다(flock 의 재진입
    동작은 OS 별로 다름). `cmd_new` 의 ID 발행 트랜잭션과 `refresh_board` 의 board.md
    write 는 *각자 독립* 락 구간으로 분리한다(중첩 아님).
    """
    with file_lock.exclusive_file_lock(BOARD_LOCK):
        yield


# ── 회귀 게이트 ──────────────────────────────────────────────────────
# 회귀 단위 ≡ push 단위 · green 인 것만 push. `regression run` 이 측정·기록(per-clone
# 로컬 플래그), pre-push 훅이 `regression check` 로 HEAD green 을 검증. 비차단 pre-warm 은
# PM 이 `run_in_background` 로 `regression run` 을 돌리는 워크플로(하니스 background).

def _git_head_at(cwd: str) -> str:
    """주어진 작업 디렉토리의 git HEAD sha 를 반환한다 (실패/비-repo 면 '').

    `_git_head` 는 board 프로세스의 `REPO` 기준이지만, livegate 기록은 테스트가 실제로
    돈 **활성 slot worktree**(=`_regression_cwd`)의 HEAD 를 키로 삼아야 한다 — 보호훅이
    push 하는 sha(=worktree HEAD)와 대조되기 때문. 이 함수가 그 cwd-매개 HEAD 를 낸다.
    """
    r = subprocess.run(["git", "-C", cwd, "rev-parse", "HEAD"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


def _git_head() -> str:
    return _git_head_at(str(REPO))


def _hooks_dir() -> Path | None:
    # encoding 명시 — git path 출력(Korean 경로 가능)을 cp949 로 디코딩하지 않도록 utf-8 고정.
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-path", "hooks"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    d = Path(r.stdout.strip())
    return d if d.is_absolute() else REPO / d


def install_pre_push_hook() -> bool:
    """Install the pre-push gate (회귀 + lint). Idempotent. False if not a git repo.

    두 단계를 AND 로 묶는다:
      1. 회귀 게이트 — green 회귀만 push (`regression check` 실패 시 `regression run`).
      2. lint 게이트 — `lint --gate` 차단 카테고리(dangling/unstable-ref/
         dependency/thin) 발견 시 push 실패. status drift 자문성은 차단 안 함.
    `board.py init` 가 (재)설치하므로 멱등·재설치 안전.
    """
    hooks = _hooks_dir()
    if hooks is None:
        return False
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    py = _detect_py()
    hook.write_text(
        "#!/bin/sh\n"
        "# pm pre-push gate — green 회귀 AND lint 게이트만 push. board.py init 이 설치.\n"
        f"{py} .project_manager/tools/board.py regression check || \\\n"
        f"  {py} .project_manager/tools/board.py regression run || exit 1\n"
        f"{py} .project_manager/tools/board.py lint --gate || exit 1\n",
        encoding="utf-8")
    hook.chmod(0o755)
    return True


def _configure_board_submodule() -> bool:
    """board submodule 의 `ignore = all` 을 자동 설정 (누출 0). 멱등·fail-soft.

    board 가 submodule 로 분리(`.project_manager/board/.git` 존재)된 형상에서만 동작한다 —
    superproject(design·코드 git)에서 `submodule.<path>.ignore all` 을 켜면, board(submodule)가
    PM 운영 commit 으로 전진해도 design 의 `git status`/`git diff` 가 그 gitlink drift 를 숨겨
    routine `git add -A` 가 board 포인터 bump 를 *우발 stage* 하지 않는다(board↔design 누출 0).

    fail-soft: git 바이너리 부재·git repo 아님·submodule 미분리(`.../board/.git` 없음·솔로/
    legacy)면 아무 것도 하지 않고 False 반환(솔로·미마이그 adopter 100% 무영향). 멱등:
    `git config` 는 같은 키를 덮어쓰므로 재실행 안전. 반환 True = 설정 적용.

    config 키 = `submodule.<.gitmodules-path>.ignore`. board
    의 `.gitmodules` 서브섹션 *이름*을 권위로 읽어(표준 `git submodule add` 는 name==path) 키를
    구성한다 — 이름이 path 와 달라도 정확한 키로 set.
    """
    board_git = REPO / ".project_manager" / "board" / ".git"
    if not board_git.exists():
        return False  # submodule 미분리(솔로/legacy) — no-op
    # `.gitmodules` 에서 이 board path 에 대응하는 submodule 서브섹션 *이름*을 찾는다.
    # 출력 예: `submodule.<name>.path .project_manager/board` — 표준은 name == path.
    name = _board_submodule_name()
    if name is None:
        return False  # .gitmodules 부재/미등록·git 부재 — fail-soft
    r = subprocess.run(
        ["git", "-C", str(REPO), "config", f"submodule.{name}.ignore", "all"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


def _board_submodule_name() -> str | None:
    """`.gitmodules` 에서 `.project_manager/board` path 의 submodule 서브섹션 이름 (없으면 None).

    `git config -f .gitmodules --get-regexp '^submodule\\..*\\.path$'` 행을 파싱해 값이
    `.project_manager/board` 인 항목의 키 `submodule.<name>.path` 에서 `<name>` 을 추출한다.
    git 부재·.gitmodules 부재·매칭 없음 → None (fail-soft·_configure_board_submodule 가 no-op).
    """
    gitmodules = REPO / ".gitmodules"
    if not gitmodules.exists():
        return None
    r = subprocess.run(
        ["git", "-C", str(REPO), "config", "-f", str(gitmodules),
         "--get-regexp", r"^submodule\..*\.path$"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return None
    want = ".project_manager/board"
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if value.strip() != want:
            continue
        # key = `submodule.<name>.path` → 가운데 <name>(점 포함 가능) 추출.
        if key.startswith("submodule.") and key.endswith(".path"):
            return key[len("submodule."):-len(".path")]
    return None


# ── areas.md union merge 배포 ───────────────────
# areas.md 의 동시 등록 안전(merge 시 **양쪽 행을 모두 보존**)은 git 내장 `merge=union` 드라이버에
# 기댄다. 그 선언은 **areas.md 를 담은 git** 의 `.gitattributes` 에 있어야 유효하다:
#   - inline(legacy) : areas.md = `<REPO>/.project_manager/areas.md` → 루트 `.gitattributes` 의
#     `.project_manager/areas.md merge=union` 이 그대로 유효(engine.manifest 로 채택자에도 배포).
#   - board 분리     : areas.md 는 board git(submodule) *안* 이라 루트 선언이 **다른 git** 이라 닿지
#     않는다 — `git -C .project_manager/board check-attr merge -- areas.md` = unspecified.
#     그래서 board git 자체에 `.gitattributes` 를 둔다: 신규는 pm_import seed, 기존 board 는
#     `_ensure_board_gitattributes` backfill. 두 형상 다 union 이 성립한다(루트 선언은 유지).
# 판정은 전부 **파일 내용**으로 한다 — 런타임 `git check-attr` 호출을 늘리지 않는다(비용·이식성).
_AREAS_UNION_DRIVER = "union"
# 형상별 areas.md 경로 표기(그 git 의 `.gitattributes` 기준 상대). 선언 줄의 패턴을 이 표기들에
# 맞춰본다 — 리터럴 일치 + 단순 glob(`_gitattributes_pattern_matches`).
_BOARD_AREAS_ATTR_TARGETS: tuple[str, ...] = ("areas.md", "/areas.md")
_INLINE_AREAS_ATTR_TARGETS: tuple[str, ...] = (
    ".project_manager/areas.md", "/.project_manager/areas.md")
_BOARD_GITATTRIBUTES_BLOCK = (
    "# areas.md = 멀티-PM prefix 레지스트리 — 동시 등록(행 append)이 merge 에서 충돌하지 않도록\n"
    "# git 내장 union merge 드라이버로 양쪽 행을 모두 보존한다.\n"
    "# board 는 별도 git 이라 superproject 루트의 같은 선언이 닿지 않는다 — 여기가 그 배포처다.\n"
    f"areas.md merge={_AREAS_UNION_DRIVER}\n"
)


def _gitattributes_pattern_matches(pattern: str, targets: tuple[str, ...]) -> bool:
    """`.gitattributes` 선언 줄의 패턴이 areas.md 경로(targets)에 걸리는가.

    리터럴 일치 + **단순 glob**(`fnmatch`) — `*.md`·`*`·`areas.*` 처럼 실제 `.gitattributes` 에
    흔한 형태를 판정한다. 선행 `/`(그 `.gitattributes` 디렉토리 기준 앵커)는 양쪽에서 벗기고,
    슬래시 없는 패턴은 git 처럼 **basename** 에도 맞춰본다(`areas.*` 가 하위 경로의 areas.md 에
    걸리는 규칙). 판정 불가/불일치는 항상 False = *거짓 경보* 방향으로 기운다(advisory 1줄 +
    중복 선언 append 는 무해하고, 거짓 정상은 union 보호 상실이라 훨씬 비싸다).
    """
    pat = pattern.lstrip("/")
    for target in targets:
        candidate = target.lstrip("/")
        if candidate == pat:
            return True
        if fnmatch.fnmatchcase(candidate, pat):
            return True
        if "/" not in pat and fnmatch.fnmatchcase(candidate.rsplit("/", 1)[-1], pat):
            return True
    return False


def _gitattributes_merge_attr(text: str, targets: tuple[str, ...]) -> str | None:
    """`.gitattributes` 본문에서 areas.md 경로에 걸린 **마지막** merge 속성 값 (없으면 None).

    git 은 뒤 줄이 앞 줄을 덮으므로(last-match-wins) 전 줄을 훑어 마지막 선언을 취한다 —
    `areas.md merge=union` 뒤에 `areas.md -merge` 가 오면 union 은 취소된 것으로 읽는다.
    빈 줄과 `#` 로 *시작하는* 주석 줄은 건너뛴다. `#` 은 줄 끝 주석이 **아니므로**(git 은
    `areas.md merge=union # 설명` 을 잘못된 속성 이름으로 보고 줄 전체를 무시한다·실측)
    필드에 `#` 이 섞인 줄은 git 과 동일하게 **무효** 취급한다 — 그렇지 않으면 실제로는
    unspecified 인 파일을 "선언됨" 으로 읽어 union 상실이 조용히 굳는다(거짓 정상).

    패턴 매칭은 **단순 glob 까지 처리**한다(`_gitattributes_pattern_matches`) — 리터럴 경로,
    `*.md`·`*`·`areas.*` 형태가 대상이다. 그래서 union 선언 **뒤에 오는 glob unset**
    (`areas.md merge=union` 다음 줄 `*.md -merge` → git 은 unset·실측)도 잡는다.

    **남는 한계(git 패턴 규칙과 완전 일치를 주장하지 않는다)**: `**/areas.md` 같은 `**` 형태·
    디렉토리 한정·이스케이프 등 복합 패턴은 판정하지 못한다. 어긋나는 방향은 둘인데 무게가
    다르다 —
      - 못 알아본 것이 *선언*(`**/areas.md merge=union`)이면 **거짓 경보**: advisory 1줄 +
        backfill 이 리터럴 줄을 한 번 더 append 할 뿐이라 무해하다(안전 방향).
      - 못 알아본 것이 *unset*(`**/areas.md -merge`)이면 **거짓 정상**이 남는다 — 이 잔여만
        진짜 위험이지만, 완전 해소는 git 패턴 엔진 재구현 또는 금지된 런타임 `check-attr`
        호출을 요구하므로 범위를 명시하고 둔다(채택자가 손으로 얹은 복합 unset 한정).
    반환: `"union"`(우리 선언) · 다른 드라이버명 · `""`(unset/`-merge`) · None(선언 없음).
    """
    found: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if any("#" in field for field in fields):
            continue  # git 이 줄 전체를 무시하는 형태 — 우리도 무효로 본다(거짓 정상 방지).
        if not _gitattributes_pattern_matches(fields[0], targets):
            continue
        for attr in fields[1:]:
            if attr.startswith("merge="):
                found = attr[len("merge="):]
            elif attr in ("merge", "-merge", "!merge"):
                found = ""  # 드라이버 미지정 set / unset / unspecified — union 아님.
    return found


def _areas_union_declared(text: str, targets: tuple[str, ...]) -> bool:
    """이 `.gitattributes` 본문이 areas.md 에 union merge 를 부여하는가 (내용 판정)."""
    return _gitattributes_merge_attr(text, targets) == _AREAS_UNION_DRIVER


def _ensure_board_gitattributes() -> bool:
    """board git 의 `.gitattributes` 에 `areas.md merge=union` 을 **멱등 보강**.

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

    **비파괴**: 파일이 있으면 덮어쓰지 않고, union 선언이 *없을 때만* 블록을 append 한다(채택자
    가 자기 규칙을 가진 경우 보존). 이미 선언돼 있으면 no-write(멱등). append 는
    `file_lock.append_atomic`(O_APPEND) — 동시 writer 가 서로를 덮지 않는다.

    fail-soft: board 미분리(legacy·솔로)·areas 가 이 git 밖·IO 실패면 아무 것도 하지 않고 False.
    반환 True = 이번 호출이 보강했다.
    """
    root = board_root()
    if not (root / ".git").exists() or areas_file().parent != root:
        return False  # board 미분리(legacy·솔로) 또는 areas 가 이 git 밖 — no-op.
    attrs = root / ".gitattributes"
    try:
        text = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
        if _areas_union_declared(text, _BOARD_AREAS_ATTR_TARGETS):
            return False
        # 기존 내용 뒤에 빈 줄 하나를 띄우고 append (줄바꿈 없이 끝난 파일도 안전하게 이어붙임).
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        file_lock.append_atomic(attrs, separator + _BOARD_GITATTRIBUTES_BLOCK)
    except (OSError, UnicodeError):
        # IO 실패·비-UTF8 `.gitattributes` — 보강을 포기한다. 이 함수는 ticket mutation 의
        # commit funnel 에서 불리므로 어떤 예외도 mutation 을 깨선 안 된다(advisory 가 표면화).
        return False
    return True


# ── board git 즉시 sync ────────
# board(tickets+areas)가 별도 git(submodule·standalone)으로 분리된 형상에서, ticket
# mutation 마다 board git 에 자동 commit + (필요 시) pull --rebase + push 한다. mutation 별
# sync 강도가 다르다:
#
#   - **claim = STRICT(원자·조율 primitive)**: 선점 감지(읽기 전용 fetch + 원격 트리 직접
#     조회)로 이미 남이 claim 했으면 작업 시작을 차단(race-lost·로컬 변경 0) → 로컬 claim
#     commit → push 가 성공해야 *비로소* 소유 확정. non-FF/거부면 로컬 claim 을 rollback
#     (티켓 open 복귀) + 명시 실패. best-effort 로 "내가 claim" 을 남기면 둘이 같은 일 =
#     중복작업 방지가 깨지므로 claim 만 strict 다.
#   - **new/promote/complete/block/unclaim/unblock = best-effort local-first**: 로컬 commit 은
#     항상 성공(로컬) → (추적 변경이 없으면) pull --rebase ; push 는 best-effort → 실패 시
#     stale 경고 + 무차단 계속. active retry 루프는 두지 않는다 — 다음 mutation 의
#     pull-rebase+push 가 밀린 commit 을 자연 catch-up 한다("retry" 의 해석).
#     이 catch-up 은 **pull 이 매번 fetch 를 포함**해야 성립한다 — 원격-추적 ref 를 읽는
#     `behind` 로 pull 여부를 가르면 fetch 가 없어 ref 가 영구 stale → 영영 안 따라잡는다.
#
# **공통 불변식 = 스코프**: 어떤 mutation 도 *자기가 만진 경로* 밖을 커밋하지 않고,
# 롤백도 자기가 만든 것만 되돌린다. 공유 board 워킹트리엔 항상 남의 미커밋 작업이 있을 수
# 있기 때문이다(claim 이 dirty 를 더는 막지 않으므로 더 그렇다). 조율 권위는 로컬 tree 의
# clean 여부가 아니라 **원격 ref 의 FF push(CAS)** 다.
#
# **활성 게이트 = board 가 별도 git 일 때만**(`board_root()/.git` 존재). legacy(board 가
# wiki/ 안·별도 git 아님)면 sync 는 전부 no-op(git 호출 0·현 동작 byte-identical) —
# board_root() graceful 탐지와 동형이고, 기존 회귀가 green 으로 남는 핵심이다. 모든 git
# 호출은 fail-soft subprocess(엔진 관례·UTF-8 고정·짧은 timeout) — 거짓 원자성/락 보장을
# 만들지 않는다(best-effort 는 정직하게 경고, claim 만 명시 실패).

# board git 호출 timeout — pull/push 는 네트워크 왕복이라 user-email 폴백(5s)보다 길게
# 둔다. 환경 이상(hang·offline DNS)에서 무한 대기를 막는 상한(엔진 subprocess 관례).
_BOARD_GIT_TIMEOUT_SECONDS = 30

# claim 차단 사유 코드 — prefetch 가 claim 을 막을 때의 *원인* 이다. 옛
# sentinel 문자열(`\0dirty`·`\0detached`)을 대체한다: 사유가 넷(dirty/rebase 충돌/offline/
# upstream 없음)으로 갈리고 진단에 behind·더러운 파일 목록이 함께 필요해, 반환을 구조체
# (`_ClaimPrefetch`)로 바꾸면서 사유도 코드로 명시했다. 하나의 원인엔 하나의 안내만 나가야
# 한다.
_CLAIM_BLOCK_DETACHED = "detached"          # board HEAD 가 브랜치를 안 가리킴 → checkout 안내
_CLAIM_BLOCK_REBASE = "rebase"              # rebase 진행 중(충돌 미해소) → abort/continue 안내
_CLAIM_BLOCK_RACE_LOST = "race-lost"        # 원격에서 이미 claimed/done/blocked → 중복작업 방지
_CLAIM_BLOCK_DIRTY = "dirty"                # 원격 앞섬 + 미커밋 파일이 통합을 막음(offline 아님)
_CLAIM_BLOCK_INTEGRATION = "integration"    # 네트워크 정상·미커밋 0 인데 통합 실패(원인 미상)
_CLAIM_BLOCK_OFFLINE = "offline"            # fetch 도달 불가(네트워크·auth)
_CLAIM_BLOCK_NO_UPSTREAM = "no-upstream"    # 추적 브랜치 미설정 → 조율할 원격이 없음
_CLAIM_BLOCK_NO_ANCHOR = "no-anchor"        # HEAD 조회 불가 → rollback anchor 없이 진행 금지

# 잔여 차단 진단에 나열할 더러운 파일 표본 상한 — 전부 쏟으면(수십 건) 안내가 묻히고, 0 이면
# "무엇 때문에 막혔나" 를 사용자가 다시 찾아야 한다. 총계는 항상 함께 낸다.
_BOARD_GIT_DIRTY_SAMPLE_MAX = 5

# 진단에 인용할 git 출력 길이 상한 — 원인 미상 통합 실패에서 마지막 stderr 줄을 그대로 보여준다
# (분류에는 안 쓴다·로케일 무관 — 사람이 읽을 단서로만).
_BOARD_GIT_DETAIL_MAX = 200

# 사유 코드 → **짧은 라벨**. "무엇 때문인지" 한 마디만 끼워 넣는 자리(롤백 후 stale 경고)용이다
# — 사유별 *처방* 문장은 `_claim_block_message` 가 소유한다(claim 차단이라는 다른 맥락). 판정
# 자체는 두 곳 모두 `_classify_pull_failure` 하나를 쓴다.
_BOARD_GIT_BLOCK_LABELS: dict[str, str] = {
    _CLAIM_BLOCK_REBASE: "rebase 진행 중·충돌 미해소",
    _CLAIM_BLOCK_DIRTY: "미커밋 파일이 통합을 막음",
    _CLAIM_BLOCK_OFFLINE: "원격 도달 불가",
    _CLAIM_BLOCK_INTEGRATION: "원인 미상·네트워크는 정상",
}

# board-git pathspec — `tickets/.drafts/`(drafts_dir()) 를 `git add`/`git status` 에서 명시
# 제외한다. draft 는 STATUS_DIRS 순회 밖이라 이미 안 보이지만, 이 pathspec 은 방어적
# 이중화다 — draft 경로가 board_root 바로 아래(add -A 의 스캔 범위)에 있는 한, 어떤 향후
# 변경으로 draft 가 STATUS_DIRS 스캔 함수에 실수로 노출되더라도 git 수준에서 한 번 더 막는다.
# pathspec 은 board_root() 기준 상대경로 리터럴이라 위치 이동(legacy↔board 분리) 무관 동작한다.
_BOARD_GIT_DRAFT_PATHSPEC: tuple[str, ...] = (".", ":!tickets/.drafts")


def _board_git_enabled() -> bool:
    """board 가 별도 git 으로 분리됐고 sync 가능한가 — `board_root()/.git` 존재 + git 바이너리.

    True 면 ticket mutation 이 board git 에 commit/pull/push 한다. False 면 sync 전부
    no-op(legacy·솔로·git 부재) — `board_root()` 가 wiki/ 를 가리키는 legacy 에선
    `wiki/.git` 가 없어 자동으로 False(superproject git 은 REPO 루트에 산다). board/ 분리
    형상에서만 `board/.git`(submodule git 파일/디렉토리)이 존재한다. git 바이너리 부재면
    분리 형상이라도 no-op(fail-soft·sync 불능).
    """
    if shutil.which("git") is None:
        return False
    return (board_root() / ".git").exists()


def _board_git(args: list[str], *, check: bool = False,
               timeout: float | None = None) -> subprocess.CompletedProcess:
    """board git working dir(`board_root()`)에서 git 명령을 실행한다 (UTF-8·timeout 고정).

    엔진 subprocess 관례: UTF-8 디코딩(한글 ticket/경로 안전)·짧은 timeout·`errors=replace`.
    `-C board_root()` 로 작업 디렉토리를 board git 으로 고정한다(cwd 의존 0). `check=False`
    가 기본 — 호출부가 returncode 로 분기하며, 예외(timeout·바이너리 이상)는 호출부가
    fail-soft 로 처리한다. `timeout` 미지정은 `_BOARD_GIT_TIMEOUT_SECONDS`(30s) — advisory
    freshness fetch 처럼 대화형 hang 을 짧게 끊어야 하는 호출부만 override 한다(기존
    경로 무변경).
    """
    return subprocess.run(
        ["git", "-C", str(board_root()), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_BOARD_GIT_TIMEOUT_SECONDS if timeout is None else timeout, check=check)


def _board_git_lock_file() -> Path:
    """board-git mutation 직렬화 락 파일 — `<REPO>/.project_manager/.local/board-git.lock`.

    `BOARD_LOCK` 처럼 import-time 상수로 굳히지 않고 **REPO 추종 lazy 해소**로 둔다(board_root()
    선례) — hermetic 테스트는 `REPO` 를 monkeypatch 하므로 락 파일이 자동으로 tmp 로 따라간다.
    별도 상수 seam 을 하나 더 두면 그걸 잊은 새 테스트가 실 루트에 락 파일을 쓴다.
    """
    return REPO / ".project_manager" / ".local" / "board-git.lock"


@contextlib.contextmanager
def board_git_lock() -> Iterator[None]:
    """board-git mutation(commit→push→rollback) 전체를 직렬화하는 OS 파일락.

    `board_lock` 은 *파일* mutation 임계구역(load→rename)만 감싸고 git ops 는 그 밖에 있어서,
    같은 clone 의 두 슬롯이 commit→push→rollback 을 인터리브할 수 있었다 — 남이 방금 만든
    claim 커밋을 내 rollback 이 되돌리는 창. 이 락이 그 트랜잭션을 통째로 직렬화한다.

    **락 순서 = board_git_lock → board_lock (전역 고정)**. claim 은 이 락을 잡은 채 board_lock
    을 잡으므로, 역순 획득이 어디에도 없어야 데드락이 없다: best-effort sync 6곳은 board_lock
    을 이미 놓은 뒤 불리고, `_prefix_relabel` 도 board_lock **밖**에서 이 락을 먼저 잡는다.

    board-git 비활성(legacy·솔로)이면 no-op — 락 파일조차 만들지 않는다(현 동작 무변경).
    **재진입 금지**(flock 관례·board_lock 과 동일) — 이 구간 안에서 이 컨텍스트를 다시 잡는
    헬퍼를 부르지 않는다. 프로세스가 죽으면 OS 가 자동 해제한다(stale-lock 없음).
    """
    if not _board_git_enabled():
        yield
        return
    with file_lock.exclusive_file_lock(_board_git_lock_file()):
        yield


def _board_git_head() -> str | None:
    """board git 의 현재 HEAD SHA (없으면 None) — claim rollback 의 복귀 지점 기록용."""
    r = _board_git(["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def _board_git_head_detached() -> bool:
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
        r = _board_git(["symbolic-ref", "-q", "HEAD"])
    except Exception:  # noqa: BLE001 — fail-soft: 판정 예외(timeout 등)는 현행 경로(False).
        return False
    return r.returncode == 1


def _board_git_status_porcelain() -> str:
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
        r = _board_git(["status", "--porcelain", "--", *_BOARD_GIT_DRAFT_PATHSPEC])
    except Exception:  # noqa: BLE001 — fail-soft: status 예외(timeout 등)는 clean 취급(보수적).
        return ""
    return r.stdout if r.returncode == 0 else ""


def _board_git_dirty_entries() -> tuple[tuple[str, str], ...]:
    """board 의 미커밋 변경 목록 — `(상태코드, 경로)` 튜플 (draft 제외·판정 단일 지점).

    dirty 의 **원천 판정은 `_board_git_status_porcelain()` 하나**다 — 이 함수는 그 출력을 파싱만
    한다(두 벌 판정 금지). porcelain 포맷은 `XY <path>`(코드 2칸 + 공백 + 경로)이고 rename 은
    `R  old -> new` 라 화살표 뒤(현재 경로)를 취한다. 공백/한글 경로는 git 이 따옴표로 감싸므로
    양끝 `"` 만 벗긴다(진단 출력용이라 8진 이스케이프까지 복원하지 않는다).
    """
    entries: list[tuple[str, str]] = []
    for line in _board_git_status_porcelain().splitlines():
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


def _board_git_has_tracked_changes() -> bool:
    """board 워킹트리에 추적 파일 변경이 있는가 — 목록을 새로 떠서 `_dirty_has_tracked` 로 판정."""
    return _dirty_has_tracked(_board_git_dirty_entries())


def _board_git_rebase_in_progress() -> bool:
    """board git 이 rebase 진행 중(충돌 미해소)인가 — `.git/rebase-merge`·`rebase-apply` 존재.

    mid-rebase 는 detached 의 부분집합이지만 **처방이 다르다**(`rebase --abort/--continue` vs
    `checkout <branch>`)
    먼저 갈라야 한다. 그리고 이 선체크가 **어떤 board-git mutation 보다도 앞**이어야 한다:
    mid-rebase 에서 `git commit -- <paths>` 는 rc=0 으로 **detached rebase HEAD 위에** 커밋을
    만들어 버린다(architect 실측). fail-soft — 판정 예외는 False(현행 경로로 흘림).
    """
    for name in ("rebase-merge", "rebase-apply"):
        try:
            r = _board_git(["rev-parse", "--git-path", name])
        except Exception:  # noqa: BLE001 — fail-soft: 판정 예외는 현행 경로(False).
            return False
        if r.returncode != 0:
            continue
        raw = r.stdout.strip()
        # `--git-path` 는 -C(board_root) 기준 상대경로를 낸다 — 절대경로면 `/` 연산이 그대로 채택.
        if raw and (board_root() / raw).exists():
            return True
    return False


class _BoardGitUpstream(NamedTuple):
    """board git 이 조율하는 원격 — remote 이름 · 로컬 브랜치 · 원격 브랜치 ref."""

    remote: str        # 예: origin
    branch: str        # 로컬 브랜치명
    merge_ref: str     # 예: refs/heads/main (`ls-remote` 조회 키)

    @property
    def tracking(self) -> str:
        """원격-추적 ref (`refs/remotes/<remote>/<branch>`) — ls-tree/rev-list 의 revision."""
        short = self.merge_ref
        if short.startswith("refs/heads/"):
            short = short[len("refs/heads/"):]
        return f"refs/remotes/{self.remote}/{short}"


def _board_git_upstream() -> _BoardGitUpstream | None:
    """현재 브랜치의 upstream 설정 (미설정·판정 불가면 None) — **네트워크 접촉 0**.

    `branch.<b>.remote`/`branch.<b>.merge` 를 직접 읽는다. `@{upstream}` 해소는 원격-추적 ref 가
    아직 없으면(첫 fetch 전) 실패해서 "upstream 미설정" 과 구별되지 않는데, 그 둘은 claim 진단의
    서로 다른 사유다(설정 없음 ≠ offline) — 그래서 설정값 자체를 본다.
    """
    try:
        head = _board_git(["rev-parse", "--abbrev-ref", "HEAD"])
        if head.returncode != 0:
            return None
        branch = head.stdout.strip()
        if not branch or branch == "HEAD":  # detached — 선체크가 이미 걸렀다(방어적).
            return None
        remote = _board_git(["config", "--get", f"branch.{branch}.remote"])
        merge = _board_git(["config", "--get", f"branch.{branch}.merge"])
    except Exception:  # noqa: BLE001 — fail-soft: 판정 예외는 upstream 미상(None).
        return None
    if remote.returncode != 0 or merge.returncode != 0:
        return None
    remote_name, merge_ref = remote.stdout.strip(), merge.stdout.strip()
    if not remote_name or not merge_ref:
        return None
    return _BoardGitUpstream(remote_name, branch, merge_ref)


def _board_git_fetch(remote: str) -> bool:
    """원격 상태를 추적 ref 로 당긴다 — **읽기 전용**(워킹트리·HEAD 미접촉). 성공 True.

    선점 감지를 `pull --rebase` 대신 fetch+ls-tree 로 하는 근거가 이것이다 — dirty 든 behind 든
    로컬 상태와 무관하게 원격을 볼 수 있고, 실패해도 로컬 변경이 0 이다.
    """
    try:
        return _board_git(["fetch", remote]).returncode == 0
    except Exception:  # noqa: BLE001 — fail-soft: fetch 예외(timeout)는 도달 불가로 본다.
        return False


def _board_git_remote_ticket_status(tracking: str, ticket_id: str) -> str | None:
    """원격 트리에서 그 티켓이 놓인 status 디렉토리 (없으면 None) — 읽기 전용 선점 감지.

    `git ls-tree -r --name-only <tracking> -- tickets/` 로 **원격 스냅샷을 직접** 읽는다. 로컬
    통합(pull) 성공에 의존하지 않으므로 dirty·behind 와 무관하게 판정된다 — 옛 판정(winner 를
    로컬로 끌어와야 보임)보다 **등가 이상**이다. `-z` 로 NUL 구분 raw 경로를 받는다
    (한글 파일명이 core.quotepath 8진 이스케이프로 나와 매칭을 놓치는 것을 막는다).
    """
    try:
        r = _board_git(["ls-tree", "-r", "-z", "--name-only", tracking, "--", "tickets/"])
    except Exception:  # noqa: BLE001 — fail-soft: 조회 예외는 판정 불가(None·차단하지 않음).
        return None
    if r.returncode != 0:
        return None
    pattern = re.compile(
        rf"^tickets/({'|'.join(STATUS_DIRS)})/{re.escape(ticket_id)}(-.*)?\.md$")
    for entry in r.stdout.split("\0"):
        m = pattern.match(entry.strip())
        if m:
            return m.group(1)
    return None


def _board_git_behind(tracking: str) -> int:
    """원격이 로컬보다 앞선 커밋 수 (`HEAD..<tracking>`) — 판정 불가면 0.

    0 폴백은 보수적 방향이다: behind 를 모르면 "원격이 안 앞섬" 으로 보고 **차단하지 않는다**
    (소유 확정 권위는 어차피 push CAS 라, 여기서 막을 이유가 없다).
    """
    try:
        r = _board_git(["rev-list", "--count", f"HEAD..{tracking}"])
    except Exception:  # noqa: BLE001 — fail-soft: 조회 예외는 0(차단하지 않음).
        return 0
    if r.returncode != 0:
        return 0
    try:
        return int(r.stdout.strip())
    except ValueError:
        return 0


def _board_git_remote_tip(upstream: _BoardGitUpstream) -> str | None:
    """원격 브랜치 tip SHA (`git ls-remote`) — 네트워크 1왕복·로컬 ref 미의존.

    `_board_git_remote_has_commit` 의 폴백 경로다(fetch 불가 시). tip 동일성만으로는 "내 커밋이
    원격에 있는가" 를 절반만 답한다 — 그래서 기본 술어가 아니라 폴백이다.
    """
    try:
        r = _board_git(["ls-remote", upstream.remote, upstream.merge_ref])
    except Exception:  # noqa: BLE001 — fail-soft: 재확인 실패는 "미확정"(None) 으로 흘린다.
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return r.stdout.split()[0].strip()


def _board_git_remote_has_commit(upstream: _BoardGitUpstream, commit: str) -> bool:
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
        if _board_git_fetch(upstream.remote):
            r = _board_git(["merge-base", "--is-ancestor", commit, upstream.tracking])
            return r.returncode == 0
        return _board_git_remote_tip(upstream) == commit
    except Exception:  # noqa: BLE001 — fail-soft: 재확인 실패는 미확정(False·롤백).
        return False


# ── repo-중립 스코프 프리미티브 ─────────────────────────────────
# **공유 워킹트리 mutation 은 선언된 경로만 건드린다**
# 스코프 원칙을 repo-중립으로 올린 것이다. 아래 함수들이 그 판정의 **단일 구현**이고,
# `_board_git_*` 는 `board_root()` 를 물린 얇은 wrapper, 디자인 git(PM 홈·wiki)은
# `ticket_finish` 가 `REPO` 를 물려 같은 함수를 부른다. 판정을 두 벌로 복제하지 않는다 —
# 복제하면 다음 사람이 한쪽만 고쳐 반대쪽이 조용히 샌다(v1.4.1 실측).
#
# `run_git` 은 `(argv) -> (rc, stdout)` 러너 seam 이다 — 호출 repo 마다 git 실행 관용구가
# 다르기 때문(board-git 은 `-C board_root()` 인 `_board_git`, ticket_finish 는 cwd=REPO 인
# 주입형 `_run_git_fn`). 미지정이면 `-C repo` 기본 러너를 쓴다.

GitRunner = Callable[[list[str]], tuple[int, str]]


def _git_scope_run(repo: Path, args: list[str]) -> tuple[int, str]:
    """`git -C <repo> …` 기본 러너 → (rc, stdout) — 엔진 subprocess 관례(UTF-8·timeout 고정)."""
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_BOARD_GIT_TIMEOUT_SECONDS, check=False)
    return r.returncode, r.stdout


def git_scope_relpath(repo: Path, path: Path) -> str | None:
    """절대 경로 → `<repo>` 루트 기준 상대 pathspec (그 repo 밖이면 None)."""
    try:
        return Path(path).resolve().relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        return None


def git_scope_pathspec(repo: Path, paths: Sequence[Path], *,
                       exclude_prefixes: Sequence[str] = ()) -> tuple[str, ...]:
    """이 mutation 이 만진 경로 → `<repo>` pathspec (**스코프 산출 단일 지점**).

    규칙 셋을 한 곳에서만 적용한다(경로별로 다시 판정하는 두 번째 구현을 만들지 않는다):
      1. `repo` **밖** 경로는 제외 — 그 git 이 모르는 파일(예: 두-git 형상에서 코드 worktree
         의 `touches`, board-git 기준의 파생물 `wiki/board.md`).
      2. `exclude_prefixes` 로 시작하는 경로는 제외 — 호출부가 그 repo 고유의 금지 구역을
         준다(board-git 의 `tickets/.drafts/` = 미충전 draft).
      3. 중복 제거(순서 보존).
    반환이 비면 스코프가 없다는 뜻(호출부가 no-op 으로 처리).
    """
    rels: list[str] = []
    for path in paths:
        rel = git_scope_relpath(repo, path)
        if rel is None or rel in rels:
            continue
        if any(rel.startswith(prefix) for prefix in exclude_prefixes):
            continue
        rels.append(rel)
    return tuple(rels)


def _git_scope_nested_git(repo: Path, rel: str) -> bool:
    """`rel` 이 **중첩 git(서브모듈) 안쪽** 경로인가 — 상위 repo 의 `add` 를 fatal 로 죽이는 갈래.

    `git add -A -- .project_manager/board/tickets/…` 는 그 경로가 서브모듈 *내부* 면
    `fatal: Pathspec '…' is in submodule` 로 rc=128 이 되고 **pathspec 전체가 stage 되지
    않는다**(board 분리 형상에서 `touches` 에 board 경로를 적으면 finish 가 통째로 중단됐다).
    조상 디렉토리 중 `.git` 을 가진 것이 있으면(=별도 git) 그 아래는 상위 repo 의 pathspec 이
    아니므로 미리 거른다. 경로 자신은 제외(repo 루트/대상 자체가 git 인 건 정상).
    """
    parent = Path(rel).parent
    while parent != Path("."):
        if (Path(repo) / parent / ".git").exists():
            return True
        parent = parent.parent
    return False


def _git_scope_is_directory(repo: Path, rel: str) -> bool:
    """`rel` 이 디렉토리(또는 디렉토리 표기)인가 — 최종 관문의 판정.

    worktree 실측(`is_dir()`)에 더해 **끝 슬래시 표기**(`newdir/`)도 디렉토리로 본다: git 의
    porcelain 이 접어서 낸 untracked 디렉토리가 그 모양이라, 실측만 하면 이미 지워진 경로가
    빠져나간다. 판정이 애매할 때는 *제외* 가 안전한 방향이다 — 빠지면 잔여 loud 보고에 뜨지만,
    통과하면 남의 파일이 조용히 실린다.
    """
    return rel.endswith("/") or (Path(repo) / rel).is_dir()


def _git_scope_ignored(repo: Path, pathspec: Sequence[str],
                       run: GitRunner) -> set[str]:
    """`.gitignore` 에 걸리는 경로들 (fail-soft — 판정 불가면 빈 집합).

    **`git add` 는 명시 pathspec 이 ignored 면 rc=1 에러다.** 광역 `add -A` 가 ignored 를 조용히
    건너뛰는 것과 다르고, `--ignore-errors` 로도 안 없어진다(reviewer 실측). 그래서 `touches` 에
    gitignored 경로가 하나만 있어도 stage 가 통째로 죽고, 하필 그 순간 잔여 loud 보고까지 함께
    사라진다(부기는 이미 절반 진행된 상태). 실형상 도달 가능 — 이 보드의 done 티켓 여러 건이
    `touches: .project_manager/local.conf`(gitignored)를 선언한다.

    판정은 `git check-ignore` 로 한다:
      - 먼저 **전체를 한 번** 물어(`check-ignore -- <paths>`) rc=1(매치 0)이면 즉시 종료 —
        평시 비용은 subprocess 1회다.
      - 매치가 있을 때만 경로별 `-q` 로 어느 것인지 가른다(`-q` 는 경로 1개만 받고 `-z` 는
        `--stdin` 전용이라, 인용/이스케이프 없는 **rc 판정** 만 쓴다 — 출력 파싱 0).
    **추적 파일은 check-ignore 가 보고하지 않는다**(실측) — 이미 추적 중인 파일은 ignore 규칙과
    무관하게 stage 되어야 하므로 이 동작이 정확히 맞다. rc=128(비-git·판정 불가)은 fail-soft.
    """
    if not pathspec:
        return set()
    try:
        rc, _out = run(["check-ignore", "--", *pathspec])
    except Exception:  # noqa: BLE001 — fail-soft: 판정 실패면 제외 0(현행 동작).
        return set()
    if rc != 0:            # 1 = 매치 0(정상) · 128 = 비-git/판정 불가 → 제외 0.
        return set()
    ignored: set[str] = set()
    for path in pathspec:
        try:
            rc_one, _ = run(["check-ignore", "-q", "--", path])
        except Exception:  # noqa: BLE001 — 개별 판정 실패는 그 경로만 통과(보수적).
            continue
        if rc_one == 0:
            ignored.add(path)
    return ignored


def git_parse_porcelain_z(out: str) -> tuple[tuple[str, str], ...]:
    """`git status --porcelain -z` 출력 → `((XY, 경로), …)` — **NUL 파싱 단일 지점**.

    `-z` 가 **판정용으로 필수**다: `-z` 없이는 git 이 비-ASCII 경로를 8진 이스케이프 + 인용으로
    낸다(`"\\353\\251\\200…"`). 그 문자열을 스코프 비교에 넣으면 *자기가 방금 stage 한 파일* 을
    "스코프 밖" 으로 오판해 "빼라" 고 지시한다(reviewer 실측 — PM 홈에 실제로 한글 티켓/아이디어
    경로가 있다). `core.quotepath=false` 도 공백 경로 인용(`"a b.md"`)이 남아 불충분하다.
    `-z` 는 인용 자체를 하지 않으므로 원문 경로가 그대로 나온다.

    rename/copy(`R`/`C`) 항목은 `<코드> <신규>\\0<원본>\\0` **2토큰**이라 원본 토큰을 소비하고
    신규 경로를 취한다. 스코프 산출(디렉토리 전개)과 잔여 보고가 **이 함수 하나**를 공유한다.
    """
    tokens = out.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in ("R", "C"):
            index += 1          # 원본 경로 토큰 소비 (신규 경로만 취한다)
        if path:
            entries.append((code, path))
    return tuple(entries)


def _git_scope_expand_dir(repo: Path, rel: str, run: GitRunner) -> list[str]:
    """디렉토리 선언 → 그 아래 **변경된 파일** 경로들 (`status --porcelain -z -- <dir>`).

    디렉토리가 **이미 삭제된** 경우도 같은 조회로 덮인다 — git 은 index 를 함께 보므로 삭제된
    파일이 ` D <경로>` 로 나온다(실측). 그래서 "지워진 디렉토리 선언" 을 위한 두 번째 경로를
    만들지 않는다.

    디렉토리를 그대로 pathspec 에 넘기면 `git add -A -- <dir>` 가 그 아래 **남의 미완성
    편집까지 통째로** stage 한다 — 좁힌 척하고 안 좁힌 상태다. 그래서
    스코프에서 디렉토리는 **살아남지 못한다**: 여기서 변경 파일 목록으로 펼쳐, 호출부가
    무엇이 실리는지 **파일 단위로 출력·검증**할 수 있게 한다(변경이 없으면 빈 목록 → 소멸).

    `--untracked-files=all` 이 **load-bearing** 이다: 기본값(`normal`)은 새 untracked 디렉토리를
    `?? newdir/` **한 항목으로 접어서** 낸다 — 그러면 전개 결과가 파일이 아니라 *디렉토리* 라
    아래 필터를 그대로 통과해 `git add -A -- newdir/` 가 되고, 막으려던 뭉뚱그리기가 이 경로로
    되살아난다(PM 실 git 재현). `all` 이면 `?? newdir/c.md` 로 파일까지 펼친다.

    `-z` + NUL 파싱은 `git_parse_porcelain_z` 한 곳이 담당한다(비-ASCII/공백 경로 인용 문제·
    rename 2토큰 처리를 두 벌로 만들지 않는다 — 잔여 보고도 같은 함수를 쓴다).
    """
    try:
        rc, out = run(["status", "--porcelain", "-z", "--untracked-files=all", "--", rel])
    except Exception:  # noqa: BLE001 — fail-soft: 조회 실패면 확장 0(디렉토리는 소멸).
        return []
    if rc != 0:
        return []
    return [path for _code, path in git_parse_porcelain_z(out)]


def git_scope_stageable(repo: Path, pathspec: Sequence[str], *,
                        run_git: GitRunner | None = None) -> tuple[str, ...]:
    """`add`/`commit` 이 매치할 수 있는 **파일** 경로만 남긴다 — 미매치/서브모듈 fatal 방지 +
    디렉토리 소멸.

    다섯 가지를 한 곳에서 처리한다(호출부가 다시 판정하지 않는다):
      1. **디렉토리는 파일로 펼친다** — 변경 파일 목록으로(`_git_scope_expand_dir`). 디렉토리
         stage 는 그 아래 남의 WIP 를 함께 싣는 누출 채널이라 구조적으로 막는다. **이미 삭제된
         디렉토리도 전개한다**(선언한 삭제가 빠지면 안 된다 — 아래 루프 주석).
      2. **중첩 git(서브모듈) 내부 경로 제거** — 상위 repo 의 `add` 가 rc=128 fatal 로 죽는다.
      3. **미존재·미추적 경로 제거** — `git add -A -- <경로>` 는 index·worktree 어디에도 없는
         경로를 주면 rc=128 fatal 로 죽고 **아무것도 stage 되지 않는다**(→ commit 실패 →
         claim 롤백). 로컬에서 만들어진 뒤 한 번도 커밋되지 않은 티켓이 이동하면 *옛* 경로가
         정확히 그 상태고(detached 구간의 보류 commit), 디자인 git 에선 `touches` 가 이 repo 에
         없는 경로(두-git 형상의 코드 worktree 경로·오타)를 가리키는 경우가 그렇다. 추적 중인
         *삭제* 경로는 남긴다(삭제도 커밋돼야 이동이 완성된다).
      4. **gitignored 경로 제거**(`_git_scope_ignored`) — 명시 pathspec 이 ignored 면 `add` 가
         **rc=1 에러**다(광역 `add -A` 와 다르다). 사람이 손으로 적는 `touches` 에 gitignored
         경로가 섞여도 stage 가 통째로 죽으면 안 된다(3과 같은 fail-soft 클래스).
      5. **최종 관문: 디렉토리는 하나도 반환하지 않는다** — 1의 전개가 *조용히 실패* 해도
         불변식이 유지되게 마지막에 한 번 더 거른다. 실제로 그런 실패가 있었다: `status
         --porcelain` 기본값(`untracked-files=normal`)이 새 untracked 디렉토리를 `?? newdir/`
         한 항목으로 접어, 전개 결과가 디렉토리인 채 3의 `exists()` 를 통과했다(PM 실 git
         재현). **불변식은 전개의 성공에 의존하면 안 된다.**
    """
    run = run_git or (lambda args: _git_scope_run(repo, args))
    expanded: list[str] = []
    for p in pathspec:
        # 전개는 **디렉토리이거나 worktree 에 없는** 경로에 시도한다. 후자가 load-bearing 이다:
        # 선언한 디렉토리가 *통째로 삭제* 되면 `is_dir()` 이 false 라 옛 게이트는 전개를 건너뛰고,
        # 이어지는 추적 판정도 `ls-files -- src` 가 `src/a.py` 를 내놔 `src` 와 매칭되지 않아
        # 경로가 통째로 탈락했다 — **선언한 삭제가 커밋에서 조용히 빠졌다**(reviewer 실측).
        # 존재하는 *파일* 은 전개 결과가 자기 자신이라 시도할 이유가 없다(subprocess 절약).
        candidate_path = Path(repo) / p
        expand = candidate_path.is_dir() or not candidate_path.exists()
        candidates = (_git_scope_expand_dir(repo, p, run) or [p]) if expand else [p]
        for candidate in candidates:
            if candidate and candidate not in expanded:
                expanded.append(candidate)
    expanded = [p for p in expanded if not _git_scope_nested_git(repo, p)]
    on_disk = {p for p in expanded if (Path(repo) / p).exists()}
    unknown = [p for p in expanded if p not in on_disk]
    tracked: set[str] = set()
    if unknown:
        try:
            rc, out = run(["ls-files", "-z", "--", *unknown])
            if rc == 0:
                tracked = {line for line in out.split("\0") if line}
        except Exception:  # noqa: BLE001 — fail-soft: 조회 실패면 존재하는 경로만 남긴다.
            tracked = set()
    stageable = [p for p in expanded if p in on_disk or p in tracked]
    ignored = _git_scope_ignored(repo, stageable, run)
    return tuple(p for p in stageable
                 if p not in ignored and not _git_scope_is_directory(repo, p))


def git_scope_stage_pathspec(repo: Path, paths: Sequence[Path], *,
                             exclude_prefixes: Sequence[str] = (),
                             run_git: GitRunner | None = None) -> tuple[str, ...]:
    """선언 경로 → 그 repo 에서 실제 stage 가능한 pathspec (스코프+동형 필터 단일 진입).

    `git_scope_pathspec`(스코프) → `git_scope_stageable`(미존재/미추적 제거) 순서 고정 —
    두 단계를 따로 부르는 호출부가 순서를 뒤바꾸거나 한쪽을 빼먹지 않게 한 진입점으로 묶는다.
    """
    return git_scope_stageable(
        repo, git_scope_pathspec(repo, paths, exclude_prefixes=exclude_prefixes),
        run_git=run_git)


def git_scope_stage_and_commit(repo: Path, message: str, pathspec: Sequence[str], *,
                               run_git: GitRunner | None = None) -> None:
    """선언된 pathspec **만** stage+commit 한다 (`add -A --` + `commit -m … --` 쌍).

    `add` 선행은 **필수**다 — `git commit --only -- <새 경로>` 단독은 그 경로가 untracked 라
    `pathspec did not match` 로 실패한다(architect 실측). 커밋이 실제로 생겼는지 판정(HEAD
    전진)은 호출부 몫이다 — repo 마다 HEAD 조회 seam 이 다르다.
    """
    run = run_git or (lambda args: _git_scope_run(repo, args))
    run(["add", "-A", "--", *pathspec])
    run(["commit", "-m", message, "--", *pathspec])


# 잔여 dirty 보고(under-stage loud)는 **의도적으로 여기 없다** — `ticket_finish` 가
# 자체 파서로 낸다. 보고 채널이 board 모듈 로드에 의존하면, 로드가 실패한 형상(예: 실행
# 인터프리터에 PyYAML 부재·엔진 사본 손상)에서 stage 가 0인 채 "잔여 없음"이라는 **거짓 안심**이
# 나온다(reviewer 실측). 보고는 어떤 경우에도 살아 있어야 하는 마지막 안전망이다.


# board-git 고유 금지 구역 — `tickets/.drafts/`(미충전 draft)는 어떤 mutation 도
# 커밋하지 않는다. promote 의 *옛* 경로가 실제로 draft 라 이 규칙이 매번 발동한다.
_BOARD_GIT_SCOPE_EXCLUDE: tuple[str, ...] = ("tickets/.drafts/",)


def _board_git_rc_out(args: list[str]) -> tuple[int, str]:
    """`_board_git` 를 스코프 프리미티브 러너 계약((rc, stdout))으로 어댑트한다.

    모듈 속성 `_board_git` 를 **호출 시점에** 조회하므로 기존 테스트의 monkeypatch(가짜
    board-git)가 그대로 프리미티브 안까지 적용된다.
    """
    r = _board_git(args)
    return r.returncode, r.stdout


def _board_git_relpath(path: Path) -> str | None:
    """절대 경로 → board git 루트 기준 상대 pathspec (board git 밖이면 None)."""
    return git_scope_relpath(board_root(), path)


def _board_git_scope_pathspec(paths: Sequence[Path], *,
                              gitattributes: bool = False) -> tuple[str, ...]:
    """이 mutation 이 만진 경로 → board git pathspec (`git_scope_pathspec` 의 board 바인딩).

    스코프 규칙(repo 밖 제외·금지 구역 제외·중복 제거)은 repo-중립 프리미티브
    `git_scope_pathspec` 이 단일 구현으로 갖고 있고, 여기선 board 고유분만 얹는다:
      - `board_root()` 를 repo 로, `tickets/.drafts/`(`_BOARD_GIT_SCOPE_EXCLUDE`)를 금지 구역으로.
      - `gitattributes=True` 일 때만 `.gitattributes` 를 덧붙인다 — **이번 호출의 backfill 이
        실제로 썼을 때**(`_ensure_board_gitattributes()` 반환 True)만 참이다. 무조건 붙이면
        사용자가 편집 중인 `.gitattributes` 를 아무 티켓 mutation 이 대신 커밋해 이 티켓이
        닫으려는 누출과 **동형** 이 된다(reviewer). 반대로 backfill 분을 빼면 그 파일이 영구
        미커밋으로 남아 (areas union 배포)이 무력화되므로, "썼으면 싣는다" 가 정확한
        조건이다. 파일 부재 시엔 붙이지 않는다(`add` rc=128 fatal 방지).
    반환이 비면 커밋할 스코프가 없다는 뜻(호출부가 no-op 으로 처리).
    """
    rels: list[str] = list(git_scope_pathspec(
        board_root(), paths, exclude_prefixes=_BOARD_GIT_SCOPE_EXCLUDE))
    if gitattributes:
        attrs = _board_git_relpath(board_root() / ".gitattributes")
        if attrs and attrs not in rels and (board_root() / ".gitattributes").is_file():
            rels.append(attrs)
    return tuple(rels)


def _board_git_stageable(pathspec: Sequence[str]) -> tuple[str, ...]:
    """`add`/`commit` 이 매치할 수 있는 경로만 남긴다 (`git_scope_stageable` 의 board 바인딩).

    판정 본체(존재/추적 필터·fail-soft)는 repo-중립 프리미티브에 있다 — 여기선 board_root()
    와 board-git 러너(`_board_git_rc_out`)를 물린다. 미매치 pathspec 이 `add` 를 rc=128 fatal
    로 죽이는 것을 막는 필터라, 디자인 git 쪽 새 스코프도 같은 구현을 쓴다.
    """
    return git_scope_stageable(board_root(), pathspec, run_git=_board_git_rc_out)


def _board_git_stage_and_commit(message: str,
                                paths: Sequence[Path] | None = None) -> bool:
    """이 mutation 이 만진 **경로만** stage+commit 한다.

    `paths` = 그 mutation 이 만들거나 옮긴 파일들(절대 경로). `git add -A -- <경로>` +
    `git commit -m … -- <경로>`(부분 커밋)로 닫아, 공유 board 워킹트리에 있는 **무관한 미커밋
    작업**(다른 슬롯 WIP·남의 staged 편집)이 이 커밋에 실려 push 되는 것을 원천 차단한다(D2 —
    claim 커밋에 타 슬롯 WIP 가 함께 push 된 것이 실증됐다). `add` 선행은 **필수**다:
    `git commit --only -- <새 경로>` 단독은 그 경로가 untracked 라 `pathspec did not match` 로
    실패한다(architect 실측).

    `paths=None` = **레거시 광역 스코프**(board 전체·draft 제외). 엔진 호출부는 더 이상 이
    형태를 쓰지 않는다 — 마지막 소비자였던 `_prefix_relabel` 도 rewrite 가 실제로 쓴 파일 +
    rename 경로로 스코프됐다. 하위호환(외부 호출·테스트)으로만 남긴다.
    **호출부는 반드시 경로를 준다.**

    새 커밋 생성 판정은 rc 가 아니라 **`HEAD != anchor`** 다 — 부분 커밋은 pathspec 이 아무것도
    못 잡으면 rc 가 애매한데, claim 은 "커밋을 못 내면 거짓 소유"라 커밋 *사실* 을
    확인해야 한다. 반환 True = 새 commit 생성.

    stage 직전 `areas.md merge=union` 배포를 멱등 보강한다. **이번 호출이 실제로
    보강했을 때만** `.gitattributes` 를 pathspec 에 넣는다 — 무조건 넣으면 사용자가 편집 중인
    `.gitattributes` 를 이 mutation 이 대신 커밋한다(이 티켓이 닫는 누출과 동형).
    """
    wrote_gitattributes = _ensure_board_gitattributes()
    before = _board_git_head()
    if paths is None:
        _board_git(["add", "-A", "--", *_BOARD_GIT_DRAFT_PATHSPEC])
        _board_git(["commit", "-m", message])
    else:
        pathspec = _board_git_stageable(
            _board_git_scope_pathspec(paths, gitattributes=wrote_gitattributes))
        if not pathspec:
            return False  # 커밋할 스코프 없음(전부 board 밖·draft·미존재) — no-op.
        git_scope_stage_and_commit(board_root(), message, pathspec,
                                   run_git=_board_git_rc_out)
    return _board_git_head() != before


class _BoardGitUncommittedScope(NamedTuple):
    """local commit 실패 진단에 필요한 실제 상태와 복구 pathspec."""
    entries: tuple[tuple[str, str], ...] = ()
    recovery_paths: tuple[str, ...] = ()


def _board_git_uncommitted_scope(paths: Sequence[Path] | None) -> _BoardGitUncommittedScope:
    """이번 mutation 경로에 남은 board-git 변경만 반환한다.

    best-effort가 local commit을 못 만든 경우, board 전체 dirty를 진단에 쓰면 다른 ticket의
    WIP를 이번 mutation 실패로 오인하고 그 경로까지 복구 대상으로 노출한다. 따라서 호출자가
    선언한 경로만 porcelain -z로 조회한다. ``paths=None``인 레거시 외부 호출에는 소유 경계가
    없으므로 false-success 판정을 하지 않는다.
    """
    if paths is None:
        return _BoardGitUncommittedScope()
    pathspec = _board_git_scope_pathspec(paths)
    if not pathspec:
        return _BoardGitUncommittedScope()
    # `.gitattributes`는 이 호출이 `_ensure_board_gitattributes()`로 새로 만들면 실제 stage
    # 대상이 된다. status에는 함께 넣되, staged 상태일 때만 recovery pathspec에도 보탠다.
    status_paths = [*pathspec]
    if ".gitattributes" not in status_paths:
        status_paths.append(".gitattributes")
    try:
        result = _board_git([
            "status", "--porcelain", "-z", "--untracked-files=all", "--", *status_paths,
        ])
    except Exception:  # noqa: BLE001 — 진단 실패가 best-effort mutation을 막으면 안 된다.
        return _BoardGitUncommittedScope()
    if result.returncode != 0:
        return _BoardGitUncommittedScope()
    entries = git_parse_porcelain_z(result.stdout)
    recovery = list(pathspec)  # porcelain rename의 새 경로만으로 old 삭제를 잃지 않는다.
    if any(path == ".gitattributes" and code[0] not in (" ", "?")
           for code, path in entries):
        recovery.append(".gitattributes")
    return _BoardGitUncommittedScope(entries, tuple(recovery))


def _warn_board_git_local_commit_failed(message: str,
                                        scope: _BoardGitUncommittedScope,
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


def _board_git_pull_rebase() -> subprocess.CompletedProcess:
    """board git 을 remote 최신화 (`pull --rebase`) — 선점/원격 변경을 로컬에 반영."""
    return _board_git(["pull", "--rebase"])


def _board_git_push() -> subprocess.CompletedProcess:
    """board git 을 remote 로 push — claim 소유 확정(strict)·best-effort 동기(나머지)."""
    return _board_git(["push"])


def _board_git_sync_best_effort(message: str,
                                paths: Sequence[Path] | None = None) -> bool:
    """best-effort local-first sync (new/complete/block/unclaim/unblock/promote).

    board 가 별도 git 이 아니면 no-op(legacy·솔로). 별도 git 이면: 로컬 commit을 항상
    시도하고, **새 commit 사실(HEAD 전진)**을 확인한 뒤 → pull --rebase ; push 를 best-effort 로.
    local commit 실패가 mutation 파일을 uncommitted로 남기면 pull/push를 건너뛰고 False를 반환한다.
    offline/auth/conflict 등
    어떤 실패도 **작업을 차단하지 않는다** — stale 경고만 stderr 로 내고 계속한다. active
    retry 루프는 없다 — 밀린 commit 은 다음 mutation 의 pull-rebase+push 가 catch-up 한다.

    `paths` = 그 mutation 이 만진 경로. claim 뿐 아니라 **ticket mutation 6곳
    (new/promote/complete/block/unclaim/unblock)도 함께** 스코프화해 출하한다 — claim 차단을
    풀면 board 가 상시 dirty 가 되므로, best-effort 가 board 전체를 쓸어담는 채로 남으면 누출
    노출이 오늘보다 커진다(사용자 결정).

    `paths=None` = 레거시 board 전체 스코프(draft 제외). 티켓이 아닌 board 파일을 고치는 외부
    호출부(`pm_config` 의 areas.md 보호목록 갱신)의 현행 동작을 보존하는 자리다 — ticket
    mutation 은 **반드시 경로를 준다**(메타가드 `test_ticket_mutations_pass_scoped_paths` 가
    board.py 호출부를 기계 검사한다·새 호출부가 잊으면 red).

    **단 detached HEAD 는 예외**: commit *전* HEAD 상태를 점검해 detached 면
    commit/pull/push 를 전부 skip 하고 loud 경고만 낸다. detached 위의 commit 은 orphan 으로
    쌓이고 catch-up 이 구조적으로 불가하므로(pull --rebase 계속 실패), 침묵 누적 대신 부기를
    보류하고 복귀를 안내한다(파일 mutation 은 이미 완료라 작업 무차단은 유지).

    git ops 전체를 `board_git_lock` 으로 감싼다 — 같은 clone 의 다른 슬롯이 진행 중인 claim
    트랜잭션(commit→push→rollback) 사이에 끼어들지 않게(락 순서 board_git_lock →
    board_lock: 이 함수는 board_lock 을 놓은 뒤 불린다).
    """
    if not _board_git_enabled():
        return True
    with board_git_lock():
        return _board_git_sync_best_effort_locked(message, paths)


def _board_git_sync_best_effort_locked(message: str,
                                       paths: Sequence[Path] | None = None) -> bool:
    """`_board_git_sync_best_effort` 의 본체 (board_git_lock 보유 전제·재진입 금지)."""
    # detached HEAD 가드: detached 에선 commit 이 orphan 으로 쌓이고 `pull --rebase`
    # 가 계속 실패해 "다음 mutation 이 catch-up" 약속이 *구조적으로* 성립하지 않는다(attached
    # 브랜치의 일시 offline/conflict 만 상정한 동작). commit/pull/push 를 모두 skip 하고 loud
    # 경고만 내 orphan 무한 누적을 원천 차단한다. 파일 mutation(rename·frontmatter)은 이미
    # 끝난 뒤라 작업은 무차단 — git 부기만 보류한다(best-effort=작업 무차단 원칙 유지). 자동
    # 복구(checkout/cherry-pick)는 PM 편집/브랜치 의도 침해라 하지 않고 안내만 한다.
    if _board_git_head_detached():
        print("  ⚠ board sync 보류 — detached HEAD. board git 부기를 건너뛴다(orphan commit "
              "누적 방지). `git -C .project_manager/board checkout <branch>`(예: main) 로 브랜치에 "
              "복귀하면 다음 mutation 이 일괄 commit·catch-up 한다. detached 에서 이미 쌓인 로컬 "
              "commit 이 있으면 복귀 후 `git -C .project_manager/board cherry-pick <sha>` 로 이식.",
              file=sys.stderr)
        return False
    committed = False
    try:
        committed = _board_git_stage_and_commit(message, paths)
        if not committed:
            # False 자체는 scope가 없거나 변경이 이미 없는 정상 no-op일 수 있다. 하지만 이번
            # mutation 경로가 아직 staged/unstaged/untracked면 HEAD 불변은 local commit 실패다.
            # 이 상태에서 push가 rc=0(up-to-date)을 내도 파일은 remote에 없으므로, "보존·다음
            # mutation catch-up"이라고 말하거나 pull/push를 계속하면 false-success가 된다.
            uncommitted = _board_git_uncommitted_scope(paths)
            if uncommitted.entries:
                _warn_board_git_local_commit_failed(message, uncommitted)
                return False
        # 통합 시도 여부는 **추적 변경 유무**로 가른다 — `behind` 로 가르면 안 된다(reviewer
        # must-fix): `_board_git_behind` 는 원격-추적 ref 를 읽으므로 **fetch 선행에서만 유효**한데
        # 이 경로엔 fetch 가 없다 → ref 가 영구 stale → behind 항상 0 → pull 을 영영 안 타 push 가
        # 계속 non-FF 로 밀린다("다음 mutation 이 catch-up" 약속이 구조적으로 깨진다·실측).
        # `pull --rebase` 는 자체가 fetch 를 포함하므로 직행이 왕복 수도 옛 코드와 같거나 적다.
        # 추적 dirty 면 pull 이 어차피 rc≠0 이라 건너뛰고 push 만 시도한다(FF 면 성공·아니면 경고).
        pull = None if _board_git_has_tracked_changes() else _board_git_pull_rebase()
        push = _board_git_push() if (pull is None or pull.returncode == 0) else None
    except Exception as exc:  # noqa: BLE001 — fail-soft: best-effort sync 는 절대 작업을 막지 않는다.
        uncommitted = _board_git_uncommitted_scope(paths)
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


def _board_git_mutation_state_suffix(local_commit_ready: bool) -> str:
    """best-effort caller의 성공 문구가 uncommitted 상태를 성공처럼 숨기지 않게 한다."""
    return "" if local_commit_ready else " (board-git 부기 보류: local-only/uncommitted)"


class _ClaimPrefetch(NamedTuple):
    """claim 선점 감지 결과 — anchor · 차단 사유 · 진단 재료.

    옛 sentinel 문자열 반환(5분)을 대체한다. 차단 사유가 넷으로 갈리고(dirty / rebase 충돌 /
    offline / upstream 없음) 진단에 `behind` 와 더러운 파일 목록이 함께 필요해, 값 하나로는
    표현이 안 된다 — 사유와 재료를 **한 구조체로 묶어** 호출부가 메시지를 한 곳에서 렌더한다.
    """

    anchor: str | None = None
    """`<sha>` = 정상 진행(rollback 복귀 지점) · `""` = board-git 비활성(legacy) · None = 차단."""
    block: str | None = None
    """`_CLAIM_BLOCK_*` 사유 코드 (None = 진행 가능)."""
    behind: int = 0
    """원격이 로컬보다 앞선 커밋 수 — 잔여 차단 진단의 핵심 수치."""
    dirty: tuple[tuple[str, str], ...] = ()
    """차단 시점의 미커밋 변경 `(코드, 경로)` — 안내에 표본 + 총계로 낸다."""
    detail: str = ""
    """사유별 부가 정보 (race-lost 면 원격에서 그 티켓이 놓인 status 등)."""


def _board_git_claim_prefetch(ticket_id: str) -> _ClaimPrefetch:
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
    if not _board_git_enabled():
        return _ClaimPrefetch(anchor="")  # sync 비활성 — 검증만 진행(legacy·솔로).
    # 1. detached / mid-rebase — 어떤 mutation 보다 먼저. rebase 를 detached 보다 먼저 가른다
    #    (mid-rebase 는 detached 의 부분집합인데 처방이 다르다·checkout 안내는 오도).
    if _board_git_rebase_in_progress():
        return _ClaimPrefetch(block=_CLAIM_BLOCK_REBASE)
    if _board_git_head_detached():
        return _ClaimPrefetch(block=_CLAIM_BLOCK_DETACHED)
    # 2. upstream — 조율 대상 원격 자체가 없으면 offline 이 아니라 설정 문제다.
    upstream = _board_git_upstream()
    if upstream is None:
        return _ClaimPrefetch(block=_CLAIM_BLOCK_NO_UPSTREAM)
    # 3. fetch — 읽기 전용(로컬 변경 0). 실패 = 도달 불가.
    if not _board_git_fetch(upstream.remote):
        return _ClaimPrefetch(block=_CLAIM_BLOCK_OFFLINE)
    # 4. 원격 선점 판정 — dirty·behind 무관(원격 트리를 직접 읽는다).
    remote_status = _board_git_remote_ticket_status(upstream.tracking, ticket_id)
    if remote_status in ("claimed", "done", "blocked"):
        return _ClaimPrefetch(block=_CLAIM_BLOCK_RACE_LOST, detail=remote_status)
    # 5. 원격이 앞섰을 때만 통합. 안 앞섰으면 무관 dirty 가 있어도 claim 은 진행한다.
    behind = _board_git_behind(upstream.tracking)
    if behind > 0:
        try:
            pull = _board_git_pull_rebase()
        except Exception:  # noqa: BLE001 — fail-soft: pull 예외(timeout 등)는 offline 취급.
            return _ClaimPrefetch(block=_CLAIM_BLOCK_OFFLINE, behind=behind,
                                  dirty=_board_git_dirty_entries())
        if pull.returncode != 0:
            return _classify_pull_failure(upstream, pull, behind)
    # 6. anchor — enabled 인데 HEAD 를 못 구하면 rollback 불가라 안전 실패(로컬 변경 0).
    anchor = _board_git_head()
    if not anchor:
        return _ClaimPrefetch(block=_CLAIM_BLOCK_NO_ANCHOR)
    return _ClaimPrefetch(anchor=anchor, behind=behind)


def _classify_pull_failure(upstream: _BoardGitUpstream,
                           pull: subprocess.CompletedProcess,
                           behind: int) -> _ClaimPrefetch:
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
    dirty = _board_git_dirty_entries()
    detail = ""
    for line in reversed((pull.stderr or "").strip().splitlines()):
        if line.strip():
            detail = line.strip()[:_BOARD_GIT_DETAIL_MAX]
            break
    if _board_git_rebase_in_progress():
        block = _CLAIM_BLOCK_REBASE
    elif not _board_git_fetch(upstream.remote):
        block = _CLAIM_BLOCK_OFFLINE
    elif dirty:
        block = _CLAIM_BLOCK_DIRTY
    else:
        block = _CLAIM_BLOCK_INTEGRATION
    return _ClaimPrefetch(block=block, behind=behind, dirty=dirty, detail=detail)


def _board_git_dirty_hint(dirty: Sequence[tuple[str, str]]) -> str:
    """더러운 파일 표본 + 총계 + **경로 스코프** 커밋 안내 (진단 문구 단일 지점).

    (`add -A -- . ':!tickets/.drafts'`) 공유 board 에서 그 커맨드는 **남의
    미완성 편집을 내가 커밋**하게 만든다.
    대신 실제로 막고 있는 파일을 이름으로 보여주고, 자기 것만 골라 커밋하도록 안내한다.
    """
    if not dirty:
        return ""
    sample = [path for _, path in dirty[:_BOARD_GIT_DIRTY_SAMPLE_MAX]]
    listed = " ".join(f"'{path}'" for path in sample)
    more = (f" (외 {len(dirty) - len(sample)}건)" if len(dirty) > len(sample) else "")
    untracked = [path for code, path in dirty if code == "??"]
    # 미추적(`??`)은 커밋 외에 **치우기**도 답이다 — 원격에서 같은 경로의 파일이 들어오면 git 이
    # "untracked working tree files would be overwritten" 로 통합을 거부한다. 이 문장은 `??`
    # 항목 유무로만 결정한다(git 메시지 문자열에 의존하지 않음·로케일 무관).
    untracked_note = (
        f" 미추적 {len(untracked)}건({', '.join(untracked[:_BOARD_GIT_DIRTY_SAMPLE_MAX])})은 "
        f"커밋하거나 board 밖으로 옮겨라 — 원격에서 같은 경로가 들어오면 git 이 덮어쓰기를 "
        f"거부한다." if untracked else "")
    return (f"미커밋 {len(dirty)}건: {', '.join(sample)}{more}. "
            f"**자기 변경만** 골라 커밋하라 — "
            f"`git -C .project_manager/board add -- {listed} && "
            f"git -C .project_manager/board commit`. "
            f"(`add -A` 로 쓸어담지 마라 — 남의 미완성 편집까지 커밋된다.)"
            f"{untracked_note}")


def _claim_block_message(ticket_id: str, prefetch: _ClaimPrefetch) -> str:
    """차단 사유 → 사용자 안내 1건 (**렌더 단일 지점**·오판/이중출력 0).

    한 원인엔 한 안내만 나가야 한다는 규약을 사유 코드 분기 한 곳으로 못박는다 —
    호출부(`cmd_claim`)가 사유별로 문구를 다시 짜면 그 규약이 두 벌이 된다.
    """
    behind = prefetch.behind
    if prefetch.block == _CLAIM_BLOCK_REBASE:
        return (f"board submodule 이 rebase 진행 중(충돌 미해소) — {ticket_id} claim 불가. "
                f"`git -C .project_manager/board rebase --abort` 로 되돌리거나, 충돌을 해소하고 "
                f"`git -C .project_manager/board rebase --continue` 후 재시도 "
                f"(offline 아님·네트워크 정상).")
    if prefetch.block == _CLAIM_BLOCK_DETACHED:
        return (f"board submodule 이 detached HEAD 상태 — {ticket_id} claim 불가(rollback anchor "
                f"없음). `git -C .project_manager/board checkout <branch>`(예: main) 로 브랜치에 "
                f"복귀 후 claim 재시도 (offline 아님·네트워크 정상). detached 에서 이미 쌓인 로컬 "
                f"commit 이 있으면 복귀 후 `git -C .project_manager/board cherry-pick <sha>` 로 이식.")
    if prefetch.block == _CLAIM_BLOCK_NO_UPSTREAM:
        return (f"board submodule 에 추적 브랜치(upstream)가 없다 — {ticket_id} claim 불가. claim 은 "
                f"원격 push 로 소유를 확정하므로 조율할 원격이 필요하다(offline 아님). "
                f"`git -C .project_manager/board push -u origin <branch>` 로 upstream 을 설정하라.")
    if prefetch.block == _CLAIM_BLOCK_RACE_LOST:
        where = prefetch.detail or "claimed"
        return (f"claim race lost on {ticket_id} — 원격 board 에서 이미 {where}/ 상태다"
                f"(다른 세션이 먼저 잡았거나 이미 처리됨·로컬 변경 0). "
                f"`board.py list --all` 로 현황을 확인하라.")
    if prefetch.block == _CLAIM_BLOCK_DIRTY:
        return (f"board 가 원격보다 {behind} 커밋 뒤졌는데 미커밋 파일이 통합(pull --rebase)을 "
                f"막는다 — {ticket_id} claim 불가 (offline 아님·네트워크 정상: fetch 성공). "
                f"{_board_git_dirty_hint(prefetch.dirty)}")
    if prefetch.block == _CLAIM_BLOCK_INTEGRATION:
        return (f"board 가 원격보다 {behind} 커밋 뒤졌는데 통합(pull --rebase)이 실패했다 — "
                f"{ticket_id} claim 불가. **네트워크는 정상**(fetch 성공)이고 미커밋 파일도 없다 "
                f"— 원인 미상이므로 `git -C .project_manager/board pull --rebase` 를 직접 돌려 "
                f"출력을 확인하라." + (f" (git: {prefetch.detail})" if prefetch.detail else ""))
    if prefetch.block == _CLAIM_BLOCK_NO_ANCHOR:
        return (f"board git 의 HEAD 를 확인할 수 없다 — {ticket_id} claim 불가(rollback anchor "
                f"없이는 거짓 소유를 되돌릴 수 없다). `git -C .project_manager/board status` 로 "
                f"board git 상태를 확인하라.")
    hint = _board_git_dirty_hint(prefetch.dirty) if prefetch.dirty else ""
    return (f"offline — board 도달 불가, {ticket_id} claim 불가"
            + (f" (원격이 {behind} 커밋 앞섬)" if behind else "")
            + (f" {hint}" if hint else ""))


class _ClaimFiles(NamedTuple):
    """claim 이 만진 두 경로 + 이동 *전* 원본 바이트 — 스코프 커밋·역이동 복원의 입력."""

    old: Path     # open/ 의 원래 경로 (롤백 복귀 지점)
    new: Path     # claimed/ 로 옮겨진 경로
    original: bytes  # 이동 전 파일 바이트(claimed_by/claimed_at 갱신 전)


def _board_git_index_snapshot(paths: Sequence[Path]) -> dict[str, tuple[str, str]] | None:
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
    pathspec = _board_git_scope_pathspec(paths)
    if not pathspec:
        return {}
    try:
        r = _board_git(["ls-files", "--stage", "-z", "--", *pathspec])
    except Exception:  # noqa: BLE001 — 캡처 실패는 None(보수적 폴백).
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


def _board_git_restore_index(paths: Sequence[Path],
                             snapshot: dict[str, tuple[str, str]]) -> bool:
    """캡처한 index 상태로 되돌린다 (`update-index`) — 전부 성공해야 True.

    항목이 있던 경로는 `--cacheinfo <mode>,<sha>,<path>` 로 **그 blob 그대로** 되꽂고(그래서
    `MM` 처럼 index 와 워킹트리가 다른 상태도 정확히 복원된다), 없던 경로는 `--force-remove` 로
    index 에서 뺀다(untracked 복원). 워킹트리 내용 복원은 `_board_git_restore_claim_files` 몫이라
    둘을 합치면 `git status` 출력이 claim 직전과 같아진다.
    """
    ok = True
    for rel in _board_git_scope_pathspec(paths):
        entry = snapshot.get(rel)
        if entry is not None:
            mode, blob = entry
            r = _board_git(["update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}"])
        else:
            r = _board_git(["update-index", "--force-remove", "--", rel])
        ok = ok and r.returncode == 0
    return ok


def _board_git_restore_claim_files(files: _ClaimFiles) -> None:
    """티켓 파일을 claim **직전** 상태로 되돌린다 — 역이동 + 원본 바이트 복원.

    hard reset 은 board 전체를 되감아 **무관한 미커밋
    작업을 파괴**한다(실측). 되돌릴 대상은 이 claim 이 만진 두 경로뿐이므로 파일 수준에서
    정확히 복원한다: `os.replace(new, old)`(atomic-rename 모델과 동형 — 중복/소실 창
    0) 후 원본 바이트를 다시 써 frontmatter 갱신까지 되돌린다.
    """
    files.old.parent.mkdir(parents=True, exist_ok=True)
    if files.new.exists():
        os.replace(files.new, files.old)
    files.old.write_bytes(files.original)


def _board_git_refresh_after_rollback() -> None:
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
        upstream = _board_git_upstream()
        if upstream is None:
            return
        _board_git_fetch(upstream.remote)   # 추적 ref 갱신 — 아래 behind 판정의 전제.
        behind = _board_git_behind(upstream.tracking)
        if behind <= 0:
            return
        if _board_git_has_tracked_changes():
            print(f"  ⚠ 로컬 board 뷰 stale — 원격이 {behind} 커밋 앞서지만 미커밋 변경이 있어 "
                  f"pull --rebase 를 생략했다(남의 작업을 치우지 않는다). 정리 후 "
                  f"`git -C .project_manager/board pull --rebase` 로 최신화하라.",
                  file=sys.stderr)
            return
        pull = _board_git_pull_rebase()
        if pull.returncode == 0:
            return
        # **실패도 생략과 똑같이 loud** 해야 약속이 반쪽이 아니다 — untracked 경로 충돌처럼
        # 사전 감지(추적 변경 유무)를 통과하고도 rc≠0 인 갈래가 있다. 사유 판정은 claim prefetch
        # 와 **같은 판정기**를 재사용한다(새 로직 0·두 벌 금지).
        failure = _classify_pull_failure(upstream, pull, behind)
        label = _BOARD_GIT_BLOCK_LABELS.get(failure.block or "", failure.block or "원인 미상")
        hint = _board_git_dirty_hint(failure.dirty)
        detail = f" (git: {failure.detail})" if failure.detail and not hint else ""
        parts = [f"  ⚠ 로컬 board 뷰 stale — 원격이 {behind} 커밋 앞서는데 "
                 f"pull --rebase 가 실패했다({label}){detail}."]
        if hint:
            parts.append(hint)
        parts.append("해소 후 `git -C .project_manager/board pull --rebase` 로 최신화하라.")
        print(" ".join(parts), file=sys.stderr)


def _board_git_claim_rollback(anchor: str, files: _ClaimFiles,
                              claim_commit: str | None = None,
                              index_snapshot: dict[str, tuple[str, str]] | None = None) -> None:
    """claim 을 **그 claim 이 만진 것만** 되돌린다 (절대 throw 금지).

    시점별로 하는 일이 다른데, 규칙은 하나다 — *내가 만든 것만* 되돌린다:
      - **commit 실패(새 커밋 0)**: 이력 무조작(HEAD==anchor) + 파일 역이동·원본 복원 + index
        스냅샷 복원.
      - **push 실패(non-FF·거부)**: `HEAD == 내 claim 커밋` 일 때만 `reset --soft <anchor>`
        (index 는 남기고 이력만 되감음) + 위와 동일. 그 사이 HEAD 가 다른 커밋이 됐다면
        (직렬화 락이 막지만 방어) 이력을 **건드리지 않는다** — 남의 커밋을 되돌리지 않는다.
      - **push 예외**: 호출부가 원격이 그 커밋을 포함하는지 먼저 재확인해 확정이면 이 함수를
        아예 부르지 않는다.
      - **rebase 충돌**: prefetch 단계라 로컬 mutation 이 0 — 롤백 대상 자체가 없다.

    `index_snapshot` = claim 직전 대상 경로들의 index 항목(`_board_git_index_snapshot`). 파일
    내용뿐 아니라 **index 상태까지** 되돌려야 "미커밋 작업을 상태 그대로 보존" 이 claim 대상
    파일 자신에도 성립한다 — 옛 코드는 무조건 `add -A` 라 unstaged/untracked 였던 티켓을
    staged 로 바꿨다(codex must-fix).

    **어떤 git/IO 호출이 throw 해도 예외를 삼킨다** — confirm 이 "loser 는 깨끗한
    race-lost rc=1·never traceback" 을 어기지 않도록. 복원이 불완전해도 claim 을 *확정하지
    않는다*(False)는 사실과 독립이다.
    """
    with contextlib.suppress(Exception):
        head = _board_git_head()
        if claim_commit and head == claim_commit and head != anchor:
            _board_git(["reset", "--soft", anchor])
    with contextlib.suppress(Exception):
        _board_git_restore_claim_files(files)
    with contextlib.suppress(Exception):
        _board_git_restore_claim_index((files.old, files.new), index_snapshot)
    _board_git_refresh_after_rollback()


def _board_git_restore_claim_index(paths: Sequence[Path],
                                   snapshot: dict[str, tuple[str, str]] | None) -> None:
    """대상 두 경로의 index 를 **claim 직전 스냅샷**으로 되돌린다 (실패는 loud·조용한 변조 금지).

    스냅샷이 없거나(캡처 실패) 복원이 실패하면 현행 동작(`add -A -- <두 경로>`)으로 폴백해
    index 를 최소한 워킹트리와 일치시키되, **그 사실을 loud 하게 알린다** — claim 대상 파일이
    staged 로 바뀔 수 있다는 뜻이라 사용자가 모르면 안 된다(조용히 다른 상태로 만들지 않는다).
    """
    if snapshot is not None and _board_git_restore_index(paths, snapshot):
        return
    print("  ⚠ board index 상태를 claim 직전으로 정확히 되돌리지 못했다 — 그 티켓 경로가 "
          "staged 로 남을 수 있다(내용은 보존됨). `git -C .project_manager/board status` 로 "
          "확인하고 필요하면 `git -C .project_manager/board reset -- <경로>` 로 unstage 하라.",
          file=sys.stderr)
    pathspec = _board_git_stageable(_board_git_scope_pathspec(paths))
    if pathspec:
        _board_git(["add", "-A", "--", *pathspec])


def _board_git_claim_confirm(anchor: str | None, files: _ClaimFiles) -> bool:
    """claim STRICT 3·4단계: 로컬 claim 을 **스코프 커밋** 하고 push 가 성공해야 소유 확정.

    board 가 별도 git 이 아니거나 prefetch 가 sync 를 비활성(`""`)으로 판단했으면 True
    (로컬 atomic-rename 만으로 확정·legacy 무변경). 별도 git 이고 유효 anchor 면:
      1. commit(**그 티켓 두 경로 + `.gitattributes`**) — 로컬 claim 박제. **새 commit 을 못
         내면**(identity 부재·hook·nothing-to-commit) push 가 "up-to-date" rc=0 을 내 remote
         미전파인데 확정될 수 있다(거짓 소유) → 즉시 rollback + False.
      2. push — rc=0 이어야 *비로소* 소유 확정(원격 ref FF = CAS·조율 권위).
      3. push 가 **예외**(timeout)면 원격이 그 커밋을 포함하는지 재확인한다 — 포함이면 원격엔
         이미 반영된 것이므로 **롤백하지 않고 확정**한다.
      4. 그 외 실패는 `_board_git_claim_rollback` 후 False (거짓 소유 0).
    **어떤 경로에서도 bool 만 반환**한다(rollback 은 절대 throw 안 함) — cmd_claim 이 깨끗한
    race-lost rc=1 을 내도록(never traceback).

    **여기가 index 스냅샷 지점이다** — 이 함수 진입 시점의 index 는 아직 claim 이 손대기 전
    상태다(그 앞의 파일 이동·frontmatter 갱신은 워킹트리만 건드린다). 롤백은 그 스냅샷으로
    되돌려 claim 대상 파일의 staged/unstaged/untracked 상태까지 보존한다.
    """
    if not _board_git_enabled() or not anchor:
        return True  # sync 비활성(legacy·anchor 없음) — 로컬 rename 만으로 확정(무변경).
    paths = (files.old, files.new)
    index_snapshot = _board_git_index_snapshot(paths)
    try:
        committed = _board_git_stage_and_commit("claim", paths)
    except Exception:  # noqa: BLE001 — fail-soft: 어떤 sync 예외도 거짓 확정을 만들지 않는다.
        _board_git_claim_rollback(anchor, files, index_snapshot=index_snapshot)
        return False
    if not committed:
        # commit 이 새 commit 을 못 냄 → push rc=0(up-to-date)이 거짓 확정을 낼 수 있다.
        _board_git_claim_rollback(anchor, files, index_snapshot=index_snapshot)
        return False
    claim_commit = _board_git_head()
    try:
        push = _board_git_push()
    except Exception:  # noqa: BLE001 — push 예외(timeout)는 *결과 미상* 이지 실패가 아니다.
        upstream = _board_git_upstream()
        confirmed = bool(claim_commit) and upstream is not None and \
            _board_git_remote_has_commit(upstream, claim_commit)
        if confirmed:
            print("  ⚠ board push 응답이 끊겼으나(timeout) 원격 브랜치가 이 claim 커밋을 이미 "
                  "포함 — 소유 확정(롤백하지 않는다·고아 claim 방지).", file=sys.stderr)
            return True
        _board_git_claim_rollback(anchor, files, claim_commit, index_snapshot)
        return False
    if push.returncode == 0:
        return True
    _board_git_claim_rollback(anchor, files, claim_commit, index_snapshot)
    return False


def _active_slot_test_cmd(session: str | None = None) -> str | None:
    """활성 worktree 슬롯(lease)에 바인딩된 test_cmd (없으면 None).

    같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 1/2/3·full vs a-only 등)을
    지속적으로 가질 수 있게 — `_test_cmd` 가 이를 repo areas *위* 레이어로 끼운다.

    **board 는 worktree_pool 을 import 하지 않는다**.
    대신 리스 장부 *파일*(`LEASES_FILE` = `.local/worktree-leases.json`·worktree_pool 과
    같은 위치)을 stdlib json 으로 직접 read 한다 — areas.md 를 읽듯 데이터-결합만(모듈 결합
    아님). 리스는 worktree_pool 이 atomic-replace(`os.replace`)로 쓰므로 **락 없는
    point-read 가 일관 스냅샷**을 본다(쓰기 경합과 분리 — 부분쓰기 장부를 못 본다).

    활성 슬롯 = (`session` 인자 또는 `session_name()`) == lease.session && state=="leased" 인
    첫 행 — `session` 명시는 M>1 슬롯 순회가 슬롯별 test_cmd
    를 뽑을 때 쓴다(무명시는 현행대로 `session_name()` 해소). 그 행의 test_cmd 가 비어 있지 않으면
    반환. 장부 부재/파싱실패/매칭없음/빈 test_cmd → None
    (침묵 폴백 — 슬롯 레이어는 *추가 우선*이지 강제 아님·호출부가 다음 레이어로 폴백).
    파싱 실패를 에러로 죽이지 않는다(fail-soft — 장부 손상이 회귀해소를 깨면 안 된다).
    """
    if not LEASES_FILE.exists():
        return None
    try:
        data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 장부 손상 = fail-soft (위 docstring): 유효 JSON 이라도 dict/list 가 아니면
    # `.get`/순회가 크래시 — 회귀해소를 깨지 않게 None 폴백(다음 레이어).
    if not isinstance(data, dict):
        return None
    leases = data.get("leases", [])
    if not isinstance(leases, list):
        return None
    sess = session if session is not None else session_name()
    for row in leases:
        if not isinstance(row, dict):
            continue
        # `and sess` 방어가드: sess 가 None(모호 M>1·미바인딩)일 때 손상 행(`session: null`·
        # `state: "leased"`)에 None==None 으로 false-match 하지 않게 한다.
        if sess and row.get("session") == sess and row.get("state") == "leased":
            cmd = row.get("test_cmd")
            return cmd or None   # 빈/None → None (이 활성 슬롯엔 바인딩 없음·다음 레이어로)
    return None


def _active_slot_path(session: str | None = None) -> str | None:
    """활성 worktree 슬롯(lease)의 절대 경로 (없으면 None).

    분리된 PM 홈(코드 없음)+worktree 모델에서 회귀는 활성 repo 의
    worktree cwd 에서 돌아야 한다 — 이 함수가 그 경로를 lease 장부에서 해소한다.

    `_active_slot_test_cmd` 와 *동형* 데이터-결합: **worktree_pool 을 import 하지 않고**
    리스 장부 파일(`LEASES_FILE`)을 stdlib json 으로 직접 read 한다.
    slot 식별자는 `work/` 접두를 이미 포함(`work/<repo>_<N>`)하므로 worktree_pool 의
    `slot_path()`(= `REPO / slot`)와 동일하게 board 가 import 없이 `REPO / lease["slot"]` 로 직접 구성한다.
    리스는 worktree_pool 이 atomic-replace 로 쓰므로 락 없는 point-read 가 일관 스냅샷을 본다.

    활성 슬롯 = (`session` 인자 또는 `session_name()`) == lease.session && state=="leased" 인
    첫 행 — `session` 명시는 M>1 슬롯 순회가 슬롯별 cwd 를 뽑을 때 쓴다. 그 행의
    `slot` 을 `REPO / slot` 절대경로로 반환. 장부 부재/파싱실패/매칭없음/빈 slot → None
    (fail-soft — 호출부가 다음 레이어[REPO]로 폴백·솔로 무변경).
    """
    if not LEASES_FILE.exists():
        return None
    try:
        data = json.loads(LEASES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # 장부 손상 = fail-soft (_active_slot_test_cmd 와 동일 가드): 유효 JSON 이라도
    # dict/list 가 아니면 None 폴백(회귀해소를 깨지 않게).
    if not isinstance(data, dict):
        return None
    leases = data.get("leases", [])
    if not isinstance(leases, list):
        return None
    sess = session if session is not None else session_name()
    for row in leases:
        if not isinstance(row, dict):
            continue
        # `and sess` 방어가드 — sess None 시 손상 행 false-match 방지.
        if sess and row.get("session") == sess and row.get("state") == "leased":
            slot = row.get("slot")
            if not slot:
                return None  # 빈/None slot → None (다음 레이어[REPO]로)
            return str(REPO / slot)
    return None


def _test_cmd(override: str | None, session: str | None = None) -> str:
    """회귀에 쓸 테스트 명령을 해소한다 (per-repo + per-slot).

    우선순위:
      1. `override` (CLI `--cmd`).
      2. **활성 슬롯(lease)의 test_cmd** — worktree 리스 장부에서 이 세션의 leased 슬롯에
         바인딩된 명령(`_active_slot_test_cmd(session)`). 같은 repo 슬롯별 빌드변형을 수용한다
         `session` 명시는 M>1 슬롯 순회가 슬롯별로
         호출할 때 쓴다(무명시는 `session_name()` 해소). 장부 부재/매칭없음/빈 값이면 다음
         레이어로 폴백.
      3. **활성 repo 의 areas.md test_cmd** — 멀티-PM 모드(areas.md 존재)에서
         활성 prefix(`id_prefix(None, session=session)`)의 레지스트리 행에 비어 있지 않은
         `test_cmd` 가 있으면 그것. per-repo 스택(pytest/go test…)을 수용한다. **`session` 을
         id_prefix 에도 thread** — M>1 슬롯 순회에서 슬롯 lease test_cmd 가 비면 prefix 유도가
         *그 슬롯의* repo 로 해소돼야 한다(전역 재해소 시 모호 None·env 오귀속으로 전 슬롯이
         같은 test_cmd 를 돌리는 false-green·codex must-fix).
      4. **솔로 폴백** — 위 전부 미스면 현 단일 `local.conf test_cmd`
         (없으면 `pytest -q`). 100% 하위호환(장부 없는 솔로/multi-PM-미배선 무영향).
    """
    if override:
        return override
    slot_cmd = _active_slot_test_cmd(session)
    if slot_cmd:
        return slot_cmd
    prefix = id_prefix(None, session=session)
    if prefix:
        row = _areas_row_for_prefix(prefix)
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    return local_config().get("test_cmd") or "pytest -q"


def _regression_cwd(override: str | None = None, session: str | None = None) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다.

    multi-PM 모델에선 코드가 활성 repo 의 **worktree** 에 있고 multi-PM 루트(`REPO`)엔 코드/테스트가
    없다 — 회귀는 worktree cwd 에서 돌아야 한다.
    이 함수는 그 cwd 를 주입 가능한 seam 으로 노출한다.

    해소 순서:
      - `override`(CLI `--cwd`·미래 호출자가 worktree 경로를 넘김) 가 있으면 그것,
      - 없으면 **활성 슬롯 경로**(`_active_slot_path(session)` — lease 장부에서 이 세션의 leased
        슬롯 worktree 경로·worktree_pool 미import·`session` 명시는 M>1 슬롯 순회용),
      - 활성 슬롯이 미해소인데 **leased ≥2·세션/cwd 미지정**(진짜 모호)이면 `REPO` 침묵 폴백
        대신 **fail-loud**(`sys.exit`) — `--repo <repo> --slot <N>`/`--cwd <path>` 명시를 안내한다.
        REPO(PM 홈·`tests/` 없음)로 조용히 폴백하면 livegate/회귀가 broken slot 을 수집해
        false fail 을 내던 것(livegate `--cwd` 우회의 근원·PM 61+62 이월)을 근절한다 —
        session_name 의 귀속-쓰기 fail-loud(bare slot 입구 거부)(rc5 vacuous-pass
        근절)과 같은 "모호는 시끄럽게" 철학. **`session` 명시(비-None)·leased <2(솔로/단일)는
        무변경** — 아래 `REPO` 폴백을 그대로 탄다(additive·솔로 100% 보존).
      - 그것도 없으면 **현 `REPO` 기본** (솔로/multi-PM-미배선 — additive·솔로 무변경).
    """
    if override:
        return override
    slot = _active_slot_path(session)
    if slot:
        return slot
    # 활성 슬롯 미해소 + leased ≥2 + 세션/cwd 미지정 = genuine ambiguity → fail-loud.
    # (session 명시면 `session is None` False → REPO 폴백·무변경 / leased <2 도 REPO 폴백·무변경.)
    if session is None and len(identity_args.leased_sessions(LEASES_FILE)) >= 2:
        sys.exit(
            "[중단] 회귀/livegate cwd 미해소 — 활성 슬롯이 여럿(leased ≥2)인데 세션/cwd "
            "미지정으로 모호하다. REPO(PM 홈·tests 없음)로 침묵 폴백하면 broken slot 을 수집해 "
            "false fail 이 되므로 거부한다. `--repo <repo> --slot <N>` 로 슬롯을 명시하거나 "
            "`--cwd <worktree 절대경로>` 로 직접 지정하라 (예: `--repo project_manager --slot 1`)."
        )
    return str(REPO)


def _minimum_python() -> tuple[int, int]:
    """engine_rev.py 에 선언된 엔진 Python 하한을 경로 기반으로 읽는다.

    board.py 는 패키지 import 와 직접 실행 양쪽을 지원하므로 평범한 sibling import 대신
    기존 엔진 로더 관례대로 파일 위치를 앵커로 삼는다. 탐지 때만 지연 로드해 board 의
    다른 명령과 최소 격리 테스트에는 새 선행 의존을 만들지 않는다.
    """
    # os.name 은 _detect_py Windows 분기 테스트/실행에서 바뀔 수 있으므로, 그 값에 따라
    # WindowsPath/PosixPath 구현을 고르는 pathlib 대신 import 시 고정된 os.path 를 쓴다.
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "engine_rev.py")
    mod = _load_module_from_path(
        type(REPO)(path), "engine_rev.py", allow_unverified=True,
    )
    return tuple(mod.MIN_PYTHON)


def _python_floor_probe_path() -> str:
    """엔진 진입점과 같은 shebang을 가진 2.7-파싱 안전 하한 probe 경로."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "python_floor.py"
    )


def _interp_runs(cmd: str) -> bool:
    """후보 인터프리터가 실행되고 엔진 Python 하한을 충족하는지 검증한다.

    존재하지만 죽은 shim (Windows 의 비기능 `python3` WindowsApps 별칭 등) 을
    `--version` rc 로 먼저 거른 뒤, 엔진과 같은 shebang의 2.7-파싱 안전 probe를
    ``cmd <probe.py>``로 실행한다. 특히 Windows ``py``의 ``-c`` 기본 버전과 실제
    스크립트 shebang 디스패치가 갈리는 창을 닫는다. 단, 부분/수동 복사본에 probe가
    없으면 ``-c`` 인라인 검사로 degrade해 하한 자체는 계속 강제한다
    시나리오). 짧은 timeout·예외 전부 흡수해 탐지가 절대 실패하지 않게 한다(fail-soft).
    """
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5)
        if r.returncode != 0:
            return False
        probe = _python_floor_probe_path()
        if os.path.isfile(probe):
            argv = [cmd, probe]
        else:
            major, minor = _minimum_python()
            argv = [
                cmd,
                "-c",
                "import sys; sys.exit(0 if sys.version_info >= "
                f"({major}, {minor}) else 1)",
            ]
        r = subprocess.run(
            argv,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
        )
        return r.returncode == 0
    except Exception:
        return False


def _interp_version_label(cmd: str) -> str | None:
    """실 스크립트 디스패치 버전을 loud 진단용 ``cmd=X.Y`` 형태로 읽는다."""
    try:
        probe = _python_floor_probe_path()
        # probe가 없는 부분 사본은 _interp_runs와 같은 -c 디스패치에서 버전을 읽는다.
        argv = [cmd, probe] if os.path.isfile(probe) else [
            cmd, "-c", "import sys; print('Python %d.%d' % sys.version_info[:2])",
        ]
        r = subprocess.run(argv, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5)
        output = f"{r.stdout}\n{r.stderr}"
        match = re.search(r"\bPython\s+(\d+)\.(\d+)", output)
        if match:
            return f"{cmd}={match.group(1)}.{match.group(2)}"
    except Exception:
        pass
    return None


@functools.lru_cache(maxsize=1)
def _detect_py() -> str:
    """init 의 local.conf py= 기본값으로 쓸 bare 인터프리터 명령을 탐지한다.

    Windows(`os.name == "nt"`) 는 `python` 을 1순위로 둔다 — **직접 인터프리터라 스크립트
    shebang 을 무시**한다. 반면 `py` 런처는 `py board.py` 에서 `#!/usr/bin/env python3`
    shebang 을 읽어 *엉뚱한* 버전으로 디스패치하므로(다중 버전 머신에서 deps 없는 Store
    Python 으로 빠질 수 있음) `py -m pytest`(기본 버전)와 `py board.py`(shebang)가 갈린다.
    `python <script>`·`python -m pytest` 는 같은 인터프리터로 일관된다. `py` 는 차선(런처),
    `python3` 은 최후(흔히 비기능 WindowsApps 별칭·sh 훅서 Permission denied). POSIX 는
    현행대로 `python3` 우선.

    후보 순서: Windows = (python, py, python3), POSIX = (python3, python). 각 후보는
    `shutil.which` 존재 **및** `_interp_runs` 실행검증을 모두 통과해야 채택된다 —
    존재하지만 죽은 shim·Python 하한 미달을 건너뛴다. 아무 것도 통과 못 하면 loud 진단을
    stderr 에 한 줄 남기고 `"python3"` 리터럴로 폴백한다(기존 fail-soft 계약 유지).
    **bare 명령**을 반환한다(which 의 절대경로가 아니라) —
    subprocess 가 PATH 해석하고, CLAUDE.md `{{PY}}` 표시에도 가독하다.
    탐지는 후보마다 subprocess를 최대 세 번 실행하므로 프로세스 수명 동안 결과를 캐시한다.
    """
    candidates = ("python", "py", "python3") if os.name == "nt" else ("python3", "python")
    probe_missing = not os.path.isfile(_python_floor_probe_path())
    found: list[str] = []
    for cand in candidates:
        if not shutil.which(cand):
            continue
        if _interp_runs(cand):
            return cand
        label = _interp_version_label(cand)
        if label:
            found.append(label)
    discovered = ", ".join(found) if found else "없음"
    try:
        major, minor = _minimum_python()
        if probe_missing:
            message = (
                f"Python 하한 probe 부재 — 인라인 검사상 {major}.{minor}+ 필요, "
                f"발견: {discovered}"
            )
        else:
            message = f"Python {major}.{minor}+ 필요, 발견: {discovered}"
    except Exception:
        # 불완전/구형 사본에서 engine_rev 자체를 못 읽어도 탐지는 절대 크래시하지 않는다.
        # 정상 배포에서는 단일 진실의 정확한 하한을 위 분기로 표시한다.
        message = f"Python 지원 하한 확인 불가(engine_rev.py), 발견: {discovered}"
    print(message, file=sys.stderr)
    return "python3"


# ── ctx 임계 (context 정지-핸드오프) ──────────────────────────────
# 어댑터 훅(opencode·claude)이 컨텍스트 잔여 비율로 nudge/stop 을 판정할 기본값.
# local.conf `ctx_nudge_pct`·`ctx_stop_pct` 로 per-clone 조정 가능 (board.py init 기록).
# 잔여 10% 정지는 rich 핸드오프 돌릴 컨텍스트가 아슬.
# 어댑터 사본(claude ctx_guard.py·opencode ctx-guard.js)과 미러(test_ctx_default_mirror 가드).
CTX_NUDGE_PCT_DEFAULT = 30  # 잔여 ≤ 이 % → "곧 정지" nudge (아직 일은 계속).
CTX_STOP_PCT_DEFAULT = 20   # 잔여 ≤ 이 % → 정지·핸드오프 트리거 임계.
# 핸드오프 토큰 예산(위 nudge/stop %의 기준). 어댑터 ctx_guard.CTX_WINDOW_TOKENS_DEFAULT
# 와 값을 동기 — board 는 ctx_guard 를 import 하지 않고(touches 격리) 리터럴을 보유한다
# (nudge/stop pct 도 동형으로 board 자체 상수). 큰 물리 window(1M) 모델이라도 낮게 두면
# 이른 핸드오프 = 토큰 경제이므로 기본은 200K 유지(auto-detect 안 함). init 이 local.conf surface.
CTX_WINDOW_TOKENS_DEFAULT = 200000


# cross-harness 역할 위임(pm_delegate) local.conf 시드 — init 이 쓰는 **주석 스키마**
# 블록(전 4역할·3키 예시). 전부 독립 주석 라인이다(값 뒤 inline `#` 금지 — local.conf 파서는 값 안의
# `#` 을 제거하지 않아 inline 주석이 값에 섞인다). 각 예시 key 라인은 주석 해제 시 그대로 유효한
# KEY=value 가 되도록 trailing 주석/화살표를 붙이지 않는다(설명은 별도 주석 라인). 기본 OFF 는
# `delegate_enabled=false` 로 표기(부재=false 이나 false 가 기본임을 스키마로 명시). **실키 결정**은
# TTY 면 init/pm_update 가 1회 물어 기록(prompt_delegate_optin)·비대화형이면 이 주석 기본 OFF 유지.
# 모델 실값은 어댑터/과금 특수라 엔진 기본값 0 — 아래는 주석 예시일 뿐 활성 key 가 아니다.
_DELEGATE_SEED_MARKER = "cross-harness 역할 위임"  # 멱등 append 판정 마커(스키마 블록 존재 여부)
_DELEGATE_CONF_SEED = (
    "# ── cross-harness 역할 위임 (pm_delegate·기본 OFF) ─────────────\n"
    "# delegate_enabled 는 기본 OFF. false 가 기본값이며, TTY init/pm_update 는 1회 물어 이 실키를\n"
    "# 기록한다(y=true). 비대화형(CI)은 아래 기본 OFF 주석을 유지한다. 켜기 = true(외부 송신·과금\n"
    "# 수용 opt-in 계약 상속 — 켜면 위임 프롬프트/코드가 외부 하네스로 전송된다):\n"
    "# delegate_enabled=false\n"
    "# 역할→(하네스·모델·reasoning) 매핑 — 4역할·3키 예시(독립 주석 라인만·값 뒤 inline # 금지):\n"
    "# 평시(normal) developer:\n"
    "# delegate.developer.harness=codex\n"
    "# delegate.developer.model=gpt-5.6-terra\n"
    "# delegate.developer.reasoning=medium\n"
    "# 난제(hard) 티어 developer — 세트를 통째로 해소한다(normal 과 혼합 상속 금지):\n"
    "# delegate.developer.hard.harness=codex\n"
    "# delegate.developer.hard.model=gpt-5.6-sol\n"
    "# delegate.developer.hard.reasoning=high\n"
    "# researcher (순수 읽기·조사):\n"
    "# delegate.researcher.harness=codex\n"
    "# delegate.researcher.model=gpt-5.6-terra\n"
    "# delegate.researcher.reasoning=medium\n"
    "# architect (설계 초안·발행은 게이트):\n"
    "# delegate.architect.harness=codex\n"
    "# delegate.architect.model=gpt-5.6-sol\n"
    "# delegate.architect.reasoning=high\n"
    "# code-reviewer — generate≠evaluate 침식을 피하려면 dev 와 다른 모델 권장(하네스 무관 비교):\n"
    "# delegate.code-reviewer.harness=codex\n"
    "# delegate.code-reviewer.model=gpt-5.6-luna\n"
    "# delegate.code-reviewer.reasoning=high\n"
    "# (cross-harness 도 지원·권장 — 예 harness=claude·model=opus. 단 claude/opencode .reasoning 은\n"
    "#  실측 후 적용되며 그 전 지정 시 fail-loud·codex 는 low/medium/high/xhigh):\n"
    "# loud 폴백(선택·엔진 기본값 없음) — 주 하네스가 **인프라 실패**(스폰 실패·타임아웃·한도/인증)일\n"
    "# 때만 1회 대체 실행한다. 정상 완료 판정(반려·must-fix)은 폴백 대상이 아니고, 미설정이면 기존\n"
    "# fail-loud 그대로다. 역할/티어별 완전 세트로 쓴다(예 developer → claude/opus):\n"
    "# delegate.developer.fallback.harness=claude\n"
    "# delegate.developer.fallback.model=opus\n"
    "# delegate.developer.hard.fallback.harness=claude\n"
    "# delegate.developer.hard.fallback.model=opus\n"
)

# 하네스별 시간 예산 local.conf 시드 — **별도 마커/블록**인 이유: 위 위임 스키마를 이미 받은 기존
# 채택자도 재실행 시 이 블록을 append 로 받아 키의 존재를 알게 된다(같은 블록에 끼워 넣으면 마커가
# 이미 있어 영영 도달하지 않는다). 값은 엔진 기본으로 충분하고, 이 시드는 **노브의 존재를 알리는
# 문서**다 — 전부 주석(활성 key 0)이라 미설정 채택자 동작은 불변.
_HARNESS_BUDGET_SEED_MARKER = "하네스별 시간 예산"
_HARNESS_BUDGET_CONF_SEED = (
    "# ── 하네스별 시간 예산 (무진행 판정 + 벽시계 백스톱·전부 선택) ─────────\n"
    "# 외부 하네스 실행(위임·외부 리뷰)의 중단 판정은 **무진행**(마지막 진행 출력 이후 침묵)이 주\n"
    "# 판정이고 벽시계는 백스톱이다. 엔진 기본값은 축별로 다르다 — 클라우드 축(codex·claude)은\n"
    "# 실측 기반으로 타이트하고, 로컬 GPU 축(opencode)은 긴 침묵 + 장시간 완주를 견딘다.\n"
    "# **미설정이어도 안전**하다(설정 없이 정상 작업이 죽지 않게 잡혀 있다). 아래는 배포 환경이\n"
    "# 다를 때만 조인다 — 예: GPU 가 넉넉해 로컬 추론이 빠르면 opencode 값을 낮춘다.\n"
    "# 단위는 초. 하네스별 키가 표면-flat 키(delegate_timeout·external_review_timeout)를 이긴다.\n"
    "# harness.codex.idle_timeout=900\n"
    "# harness.codex.wall_timeout=3600\n"
    "# harness.claude.idle_timeout=900\n"
    "# harness.claude.wall_timeout=3600\n"
    "# harness.opencode.idle_timeout=5400\n"
    "# harness.opencode.wall_timeout=14400\n"
    "# 추가 리뷰어(additional reviewer·external_review)도 같은 키를 읽는다 — 하네스는\n"
    "# additional_reviewer.harness(기본 codex·아래 opt-in 블록)에서, 그게 없는 레거시 채택자는\n"
    "# reviewer_cmd 의 첫 토큰에서 온다. 둘 다 codex 면 harness.codex.* 가 적용된다.\n"
)


def _ctx_pct(key: str, default: int) -> int:
    """local.conf 의 ctx 임계값을 정수로 읽는다. 없거나 비정수면 default."""
    raw = local_config().get(key)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (ValueError, AttributeError):
        return default


def ctx_thresholds() -> dict[str, int]:
    """ctx 정지-핸드오프 임계값을 dict 로 반환 (어댑터 훅이 읽어 판정).

    반환: {"nudge_pct": N, "stop_pct": M}. local.conf 우선·없으면 엔진 기본(30/20).
    """
    return {
        "nudge_pct": _ctx_pct("ctx_nudge_pct", CTX_NUDGE_PCT_DEFAULT),
        "stop_pct": _ctx_pct("ctx_stop_pct", CTX_STOP_PCT_DEFAULT),
    }


def _ticket_touches(tid: str) -> list[str]:
    try:
        _status, path = find_ticket(tid)
    except FileNotFoundError:
        return []
    fm, _ = load_ticket(path)
    return list(fm.get("touches") or [])


def _scope_args(touches: list[str]) -> str:
    """touches → pytest -k 선택식 (파일 stem 기반). 비면 '' (스코프 없음 = full)."""
    stems = sorted({Path(t).stem for t in touches
                    if t.strip() and Path(t).stem not in ("", "__init__")})
    return f'-k "{" or ".join(stems)}"' if stems else ""


def _quarantine_args() -> str:
    """quarantine.txt(있으면)의 test node id 를 --deselect 로. flaky 격리 (full 게이트 보호)."""
    q = REPO / ".project_manager" / "quarantine.txt"
    if not q.exists():
        return ""
    ids = [ln.strip() for ln in q.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.startswith("#")]
    return " ".join(f"--deselect {i}" for i in ids)


# ── FULL 게이트 수집 하한 (부분수집 false-green 차단) ─────────────────────
# rc5(수집 0)만 결함 신호로 보면 **부분 수집**(rc0 인데 스위트 일부만 돎 — cwd/pythonpath 파손)이
# pass 로 기록된다. 하한은 스위트 규모의 함수라 엔진이 보편값을 정할 수 없으므로 채택자 opt-in
# (local.conf `regression_min_collected`·기본 0 = 가드 off)으로 선언받고, FULL 게이트 결과가 rc0
# 인데 실행 수가 하한 미만이면 fail 로 강등한다. 강등 rc 는 전용 라벨이라 실 red(rc≠0)·
# rc5(수집 0)와 사유가 구분된다(check/훅 메시지 진단 가능).
REGRESSION_MIN_COLLECTED_KEY = "regression_min_collected"
REGRESSION_RC_PARTIAL_COLLECTION = "partial-collection"    # 수집 < 하한 (비-0·전용 라벨)
REGRESSION_RC_UNVERIFIED_COLLECTION = "unverified-collection"  # 하한 활성인데 검증 불가


# ── pytest 요약행 파서 (엔진 공용 seam) ────────────────────────────────────
# 요약행은 **끝에서부터** 줄 단위로 찾는다. 캡처된 pytest 출력에는 테스트 자신이 찍은
# `3 passed` 류 로그가 섞이고, 출력 전체를 `re.search` 하면 그 로그를 요약으로 오판한다.
# 도구마다 복제돼 있던 첫-매칭 파서를 여기로 승격했다 — 수집 하한 가드·릴리즈 라이브 게이트
# (이 파일)·부트스트랩 회귀 dump·ticket 마감 회귀 판정·핸드오프 회귀 1줄이 모두 이 seam 을
# 쓴다. 다른 도구는 각자 이미 가진 board 형제 로더로 읽는다 — **로컬 사본 금지**(사본은
# 첫-매칭 오판을 그대로 되살린다).
#
# 오염은 **양방향**이다: 요약 *앞* 로그는 끝에서-탐색이, 요약 *뒤* 로그(소비처 넷은 stdout+
# stderr 를 병합해 먹인다 — 자식 하네스의 stderr 한 줄이 꼬리에 붙는다)는 요약행 문법 완전
# 일치가 막는다. 두 축 모두 이 seam 안에 있어 소비처가 자동 상속한다.

# pytest 요약행 outcome 종류 — 실행 수 = 이 값들의 합. `deselected`(quarantine `--deselect`·
# 마커 필터)는 *돌지 않은* 수라 세지 않는다. `xfailed`/`xpassed` 는 패턴이 `<수> <종류>` 라
# `failed`/`passed` 에 겹쳐 잡히지 않는다(각자 독립 카운트).
_PYTEST_OUTCOME_KINDS = ("passed", "failed", "errors?", "skipped", "xfailed", "xpassed")


def _pytest_outcome_match(line: str, kind: str) -> re.Match[str] | None:
    """요약행에서 outcome 한 종류(`N passed`)의 정규식 매치 — 없으면 None.

    `kind` 는 pytest 표기 그대로의 패턴 조각이라 단/복수 겸용(`errors?`)도 그대로 받는다.
    이 한 지점이 outcome 표기의 단일 정의다(카운트·요약 문자열 절단이 함께 쓴다).
    """
    return re.search(rf"(\d+) {kind}\b", line or "")


def _pytest_outcome_count(line: str, kind: str) -> int | None:
    """요약행에서 outcome 한 종류의 수 — 그 종류가 없으면 None(수 0 과 구분)."""
    match = _pytest_outcome_match(line, kind)
    return int(match.group(1)) if match else None


def _pytest_outcome_counts(line: str,
                           kinds: tuple[str, ...] = _PYTEST_OUTCOME_KINDS) -> list[int]:
    """한 줄에서 pytest outcome 카운트를 뽑는다 (`7577 passed, 12 skipped …` → [7577, 12]).

    `kinds` 로 셀 종류를 좁힌다 — 소비처마다 *무엇을 실행으로 보는지* 가 다르다(수집 하한은
    6종 전부·릴리즈 라이브 게이트는 마커 안에서 돈 3종).
    """
    counts: list[int] = []
    for kind in kinds:
        count = _pytest_outcome_count(line, kind)
        if count is not None:
            counts.append(count)
    return counts


# 요약행 **전체 문법** — outcome 목록 + `in <소요시간>` 종결. 끝에서-탐색만으로는 부족하다:
# 실제 요약 *뒤에* wrapper/자식 하네스가 `child harness: 5 passed in 1.00s` 류를 찍으면(소비처는
# stdout+stderr 병합 출력을 먹인다) 그게 마지막 카운트 줄이라 요약으로 뽑혀 하한·릴리즈 pin 이
# 우회된다. 실측된 그 오염 줄은 `in X.XXs` 종결까지 갖췄으므로 **줄머리 앵커가 필수**다 — 판정을
# "카운트를 포함한다"가 아니라 "요약행 문법에 완전히 일치한다"로 세운다. 허용 형태: 구분선
# 장식(`==== … ====`)·긴 실행의 `(0:06:28)` 꼬리·구 pytest 의 `in 0.30 seconds`.
_PYTEST_SUMMARY_ITEM = r"\d+ [a-z]+"
_PYTEST_SUMMARY_RE = re.compile(
    r"^=*\s*"                                                       # 선택적 구분선
    rf"{_PYTEST_SUMMARY_ITEM}(?:\s*,\s*{_PYTEST_SUMMARY_ITEM})*"    # `N kind`(, `N kind`)*
    r"\s+in\s+\d+(?:\.\d+)?\s*(?:s|seconds?)"                       # 소요시간 종결 앵커
    r"(?:\s*\(\d+:\d{2}:\d{2}\))?"                                  # 긴 실행 `(0:06:28)`
    r"\s*=*\s*$"
)
# 색 출력(`--color=yes`·`PY_COLORS=1`)은 요약행에 ANSI 를 섞는다 — 문법 판정 전에 걷어낸다
# (안 걷으면 정상 요약이 문법 불일치로 떨어져 `unverified-collection` false-RED 가 된다).
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _is_pytest_summary_line(line: str) -> bool:
    """그 줄이 pytest 요약행 문법에 **완전히** 일치하나 (카운트 포함 여부가 아니다)."""
    return bool(_PYTEST_SUMMARY_RE.match(_ANSI_ESCAPE_RE.sub("", line or "").strip()))


def _pytest_summary_line(output: str) -> str | None:
    """pytest 출력의 요약행 — **끝에서부터** 첫 *요약행 문법* 줄 (없으면 None).

    두 겹으로 거른다:
      1. **끝에서-탐색** — 캡처 출력에는 테스트/자식 러너가 찍은 `3 passed` 류 로그가 섞이고,
         앞에서부터 찾으면 그 로그를 요약으로 오판한다.
      2. **문법 완전 일치**(`_is_pytest_summary_line`) — 실제 요약 *뒤에* 오는 wrapper 로그
         (`[post] 8000 passed in 900s`)까지 끝에서-탐색이 집어삼키는 구멍을 닫는다. 카운트
         나열 + `in <초>s` 종결이라는 요약행 고유 형태를 만족해야 한다.
    """
    for line in reversed((output or "").splitlines()):
        if _is_pytest_summary_line(line) and _pytest_outcome_counts(line):
            return line.strip()
    return None


def _collected_count(output: str) -> int | None:
    """pytest 요약행에서 *실제 실행된* 테스트 수를 센다 (파싱 실패 시 None).

    N = passed + failed + error(s) + skipped + xfailed + xpassed. 요약행이 없으면
    (수집 0 "no tests ran"·비-pytest test_cmd) None — 하한 가드는 그때 판정을 건너뛴다
    (fail-soft·현행 rc 신호에 맡김).
    """
    line = _pytest_summary_line(output)
    if line is None:
        return None
    return sum(_pytest_outcome_counts(line))


def _pytest_summary_tail(output: str, kind: str = "passed") -> str | None:
    """요약행의 `N <kind>` 부터 줄 끝까지 (요약행/그 종류가 없으면 None).

    사람이 읽는 회귀 1줄 표기(인계 프롬프트·log entry)가 소비한다 — 줄 전체가 아니라 카운트
    지점부터 잘라 쓰던 표기를 보존하되, 자를 대상 줄은 끝에서-탐색으로 고른다.
    """
    line = _pytest_summary_line(output)
    if line is None:
        return None
    match = _pytest_outcome_match(line, kind)
    return line[match.start():].strip() if match else None


def _regression_min_collected(cwd: str | None = None) -> int:
    """FULL 게이트 수집 하한 — **회귀를 도는 트리**(run cwd)의 local.conf 에서 해소한다.

    앵커가 run cwd 인 이유: 하한은 *그 트리 스위트* 규모의 함수다. 호출된 board.py 사본의
    `REPO`(두-git 형상에선 tests/ 가 없는 PM 홈·multi-repo 홈에선 남의 repo)에서 읽으면 다른
    트리의 선언이 새어 들어와 엉뚱한 스위트를 강등한다 — 실제로 개발 머신 local.conf 가 tmp
    스텁 회귀(수집 1)를 강등시킨 누출이 있었다. cwd 미지정은 현행 `REPO` 앵커(솔로 동일).

    엔진 기본값은 0(off)이고 채택자가 자기 수집수 기준으로 선언한다. 비정수/음수는 0(off)로
    폴백하되 **경고 1줄**을 낸다 — 오타로 게이트가 조용히 무력화되면 이 가드가 막으려던
    false-green 이 그대로 돌아온다.
    """
    conf = local_config(Path(cwd)) if cwd else local_config()
    raw = conf.get(REGRESSION_MIN_COLLECTED_KEY)
    if raw is None:
        return 0
    try:
        value: int | None = int(str(raw).strip())
    except ValueError:
        value = None
    if value is None or value < 0:
        print(f"regression: 경고 — local.conf `{REGRESSION_MIN_COLLECTED_KEY}={raw}` 가 "
              "비정수/음수 — 수집 하한 가드 off.", file=sys.stderr)
        return 0
    return value


def _collection_shortfall_text(collected: int | None, floor: int | None) -> str:
    """부분수집 사유의 공용 표기 — `수집 N<하한 M` (run·check 세 surface 가 공유).

    두 값 모두 **플래그에 기록된 실행 당시 값**을 받는다 — check 시점에 conf 를 다시 읽으면
    그 사이 바뀐 하한으로 과거 기록을 설명하게 된다. 미기록(옛 플래그)은 `?`.
    """
    shown = "?" if collected is None else collected
    limit = "?" if floor is None else floor
    return f"수집 {shown}<하한 {limit}"


def _flag_conf_anchor(data: dict, fallback: str | None) -> str | None:
    """플래그에 기록된 **conf 앵커**(실행 당시 cwd) — 미기록/손상이면 `fallback`.

    하한은 *실행 트리* 의 선언이다. `run --cwd <tree>` 로 핀해 돈 결과를 pre-push 훅이
    `check`(훅은 `--cwd` 를 못 넘긴다)로 검증할 때, check 가 **자기** cwd 로 하한을 재해소하면
    실행 때와 다른 local.conf 를 읽어 실행 트리의 하한 상향을 놓치거나(false-green) 무관한
    하한으로 막는다(false-RED). 그래서 run 이 앵커를 기록하고 check 는 그 앵커로 해소한다.
    앵커 트리가 사라졌으면 conf 부재 → 하한 0 으로 흐른다(fail-soft·HEAD 대조가 별도로 stale
    을 잡는다). 미기록(옛 플래그)은 호출부 해소값으로 폴백해 후방호환.
    """
    anchor = data.get("conf_anchor")
    return anchor if isinstance(anchor, str) and anchor else fallback


def _flag_collected(data: dict) -> int | None:
    """플래그의 `collected` 를 정수로 정규화 — 손상값(문자열·리스트·bool)은 None.

    None 은 "검증 불가" 로 흘러 하한 활성 시 stale 강등이 된다(재실행 유도). 정규화 없이
    손상값이 `_green_floor_stale` 의 `<` 비교에 들어가면 TypeError 로 **게이트 자체가 죽는다** —
    장부 손상이 회귀 해소를 깨면 안 된다는 기존 fail-soft 규율과 같은 축이다.
    """
    value = data.get("collected")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag_blocks_push(data: dict, anchor: str | None) -> bool:
    """이 기록이 push 를 막는 상태인가 — fail 기록 또는 하한 미달 green(재실행 요구).

    `check` 의 두 차단 갈래(RED / 하한 신규·상향 stale)와 **같은 판정 프리미티브**를 쓴다
    (판정 두 벌 금지). HEAD 불일치는 여기 없다 — 그건 커밋마다 생기는 정상 재실행 사유다.
    """
    if data.get("status") != "pass":
        return True
    return _green_floor_stale(_flag_collected(data), _regression_min_collected(anchor))


def _inherit_flag_anchor(default_cwd: str) -> str:
    """차단 기록이 있으면 그 기록의 conf 앵커를 이어받는다 — 없으면 `default_cwd`.

    pre-push 훅은 `check || run` 이고 `--cwd` 를 못 넘긴다. 앵커 A(핀된 트리)의 기록으로
    check 가 RED/stale 을 냈는데 이어지는 재실행이 기본 트리 B 에서 돌면, **B 의 green 이 A 의
    차단 기록을 덮어** 그대로 push 된다(게이트 우회). 그래서 차단 기록의 재실행은 그 기록이
    가리키는 트리에서 돈다.

    **HEAD-stale 은 이어받지 않는다** — 커밋마다 생기는 정상 재실행이라 여기서 이어받으면
    릴리즈 때 한 번 쓴 `--cwd <readonly>` 핀이 이후 모든 훅 재실행에 눌러붙는다(그 트리는
    현 작업 커밋이 아니다). 앵커 디렉토리가 사라졌으면(worktree 정리) 기본 해소로 돌아간다.
    """
    if not REGRESSION_FLAG.exists():
        return default_cwd
    try:
        data = json.loads(REGRESSION_FLAG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_cwd
    if not isinstance(data, dict):
        return default_cwd
    anchor = _flag_conf_anchor(data, None)
    if not anchor or os.path.abspath(anchor) == default_cwd:
        return default_cwd
    if not _flag_blocks_push(data, anchor):
        return default_cwd
    if not Path(anchor).is_dir():
        print(f"regression: 직전 차단 기록의 앵커({anchor})가 없어 기본 트리에서 재실행 — "
              "그 기록은 이 실행 결과로 덮인다.", file=sys.stderr)
        return default_cwd
    print(f"regression: 직전 차단 기록의 앵커를 이어받아 실행 → {anchor} "
          "(다른 트리 green 으로 덮이는 게이트 우회 차단)")
    return os.path.abspath(anchor)


def _green_floor_stale(collected: int | None, floor_now: int) -> bool:
    """기록된 green 이 **현재** 하한을 못 만족하나 — green 재사용(HEAD+status)의 산술 보강.

    HEAD·status 만 보면 하한을 켜거나 올린 뒤에도 옛 green 이 그대로 통과하고(check) M>1 run
    조차 그 슬롯을 skip 해 새 하한이 영영 적용되지 않는다. 기록 증거가 현재 하한을 만족하지
    못하면 **stale**(재실행 유도)로 강등한다:
      - 수집수 < 하한 → stale. 하한을 *낮춘* 경우엔 증거가 이미 충족하므로 green 유지
        (불필요한 full 재실행 0 — 판정이 산술로 닫힌다).
      - 수집수 미기록(옛 플래그·하한 0 시절 기록) + 하한 활성 → 검증 불가라 stale.
    무한 재실행은 없다: 재실행 시 파싱이 또 실패하면 그때는 `fail`(unverified)로 기록돼
    다음 check 가 stale 이 아니라 RED 로 끝난다.
    """
    if floor_now <= 0:
        return False
    return collected is None or collected < floor_now


def _apply_collection_floor(rc: int, status: str, collected: int | None,
                            floor: int) -> tuple[str, int | str, str]:
    """FULL 게이트 수집 하한 판정 — `(status, 기록 rc, 진단 note)` 로 강등을 반영한다.

    rc0(green) 인데 실행 수가 하한 미만이면 부분 수집(cwd/pythonpath 파손으로 스위트 일부만
    돎)을 의심해 status 를 `fail`, 기록 rc 를 전용 라벨 `partial-collection` 으로 강등한다.
    하한 0(기본·미설정)이면 무조건 현행 동작 그대로다(no-op — 파싱 실패도 조용히 skip).

    **하한이 활성인데 수집수를 못 읽으면(파싱 실패) `fail`(`unverified-collection`)이다** —
    경고만 내고 pass 로 기록하면 "설정된 하한을 검증하지 못한 결과"가 push 를 통과해 이 가드가
    막으려던 false-green 이 그대로 재도입된다. 비-pytest `test_cmd`(요약행 규약 밖)로 하한을
    쓰려면 하한을 해제하거나 요약행 호환 러너를 써야 한다(remedy 를 메시지에 싣는다).

    `floor` 는 호출부가 **run cwd 앵커**로 해소해 넘긴다(`_regression_min_collected(cwd)`) —
    이 판정 안에서 conf 를 다시 읽지 않는다(앵커 이원화 금지). run 두 경로(단일-슬롯·M>1
    `_regression_run_slot`)가 공유하는 단일 판정 지점이다.
    """
    if rc != 0 or floor <= 0:
        return status, rc, ""
    if collected is None:
        return ("fail", REGRESSION_RC_UNVERIFIED_COLLECTION,
                f" · 수집수 파싱 실패 — 하한 {floor} 검증 불가"
                "(요약행 없는 test_cmd 면 하한 해제 필요)")
    if collected >= floor:
        return status, rc, ""
    return ("fail", REGRESSION_RC_PARTIAL_COLLECTION,
            f" · {_collection_shortfall_text(collected, floor)} — 부분 수집 의심"
            "(테스트 루트/cwd 확인)")


def _tee_stream(stream, echo: Callable[[str], None]) -> str:
    """자식 스트림을 줄 단위로 **실시간 echo 하면서 동시에** 전체를 모아 돌려준다.

    수집 하한 가드는 pytest 요약행을 읽어야 해서 출력을 버퍼에 담아야 하는데, 캡처만 하면
    `git push` 회귀(pre-push 훅)가 수 분 동안 **완전 무출력**으로 멈춰 있는 것처럼 보인다
    (실측 367s). tee 로 진행을 그대로 흘리면서 파싱용 버퍼를 유지해 둘 다 만족한다.
    스트림 미개방(None)은 빈 문자열.
    """
    if stream is None:
        return ""
    chunks: list[str] = []
    for line in stream:
        chunks.append(line)
        echo(line)
    return "".join(chunks)


def _run_regression_cmd(cmd: str, cwd: str,
                        env: dict[str, str]) -> tuple[int, str, str]:
    """회귀 test_cmd 를 실행하고 `(rc, stdout, stderr)` 를 돌려준다 — run 두 경로 공용 단일 지점.

    출력을 버퍼에 담는 이유는 수집 하한 가드가 pytest 요약행을 읽어야 하기 때문이다(별도
    `--co -q` 재실행 대신 *이미 돈* 실행의 요약을 쓴다·자식 1회). 동시에 `_tee_stream` 으로
    실시간 echo 해 캡처 이전의 가시성을 그대로 유지한다(무출력 대기 근절).

    **두 스트림을 분리해 돌려준다** — 요약행 파싱은 **stdout 단독**이어야 한다. pytest 요약은
    stdout 에만 나오므로, stderr 에 섞인 로그(`3 passed …`)를 합치면 그게 "마지막 요약행"으로
    뽑혀 수집수가 오염된다(false-green/false-RED 양방향). 재출력·진단은 두 스트림 모두 쓴다.
    스트림 읽기는 스레드 2개로 동시에 — 한쪽만 읽으면 파이프가 차서 자식이 멈춘다(교착).
    """
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    captured: dict[str, str] = {}

    def _pump(name: str, stream, echo: Callable[[str], None]) -> None:
        captured[name] = _tee_stream(stream, echo)

    pumps = [
        threading.Thread(target=_pump, daemon=True, args=(
            "stdout", proc.stdout, lambda line: print(line, end="", flush=True))),
        threading.Thread(target=_pump, daemon=True, args=(
            "stderr", proc.stderr, lambda line: print(line, end="", flush=True,
                                                      file=sys.stderr))),
    ]
    for pump in pumps:
        pump.start()
    rc = proc.wait()
    for pump in pumps:
        pump.join()
    return rc, captured.get("stdout", ""), captured.get("stderr", "")


def _regression_rc5_note(rc: int, cwd: str, override: str | None) -> str:
    """rc5(pytest 수집 0 · "no tests ran") 진단 힌트를 만든다 (rc≠5 면 '').

    "no tests ran"(exit 5)은 pass 로 기록하지 않는다— 수집 0 은 테스트 루트/cwd 가
    어긋났다는 신호지 green 이 아니다
    채널로 확장). 나아가 lease/세션 미매칭으로 cwd 가 REPO 로 폴백했고 그 REPO 에 `tests/` 가
    없으면 세션 해소 경로를 시끄럽게
    표면화한다 — 훅 env 에 세션 정체성이 없어 상시 vacuous green 을 만들던 침묵 폴백
    `override`(명시 `--cwd`)면 폴백이 아니므로 힌트를 붙이지 않는다.
    """
    if rc != 5:
        return ""
    note = " · 수집 0 — 테스트 루트/cwd 확인"
    fell_back_to_repo = not override and cwd == str(REPO)
    if fell_back_to_repo and not (Path(cwd) / "tests").is_dir():
        note += (f" · 활성 slot lease 미매칭(session=`{session_name() or '(비바인딩)'}`) — "
                 "`PM_SESSION_NAME` 또는 local.conf `session=` 확인")
    return note


# ── M>1 회귀 슬롯 해소 ────────────────────────────────────
# 훅은 `--repo/--slot` 을 못 넘긴다(pre-push 훅 스크립트 무변경·check||run 체인). 명시/env/단일-lease
# 는 그 슬롯(현행 결과 동일)이지만, 활성 슬롯이 여럿(leased ≥2·무명시)이면 **어느 세션이 push 하든**
# 전 leased 슬롯이 green 이어야 한다 — check-first(저비용·기록 baseline)로 이미 green 인 슬롯의
# pytest 재실행을 억제하고 stale/red 만 run, 하나라도 red 면 push 차단
# nothing 철학과 동형). 슬롯별 회귀 플래그를 분리해(같은 `.local/` 공유) 결과가 서로 덮이지 않게 한다.

def _regression_flag_for(session: str | None) -> Path:
    """세션(슬롯)별 회귀 플래그 경로 — M>1 all-or-nothing 순회용.

    `session` None(솔로·단일-lease·현행 단일-슬롯) → 공유 `REGRESSION_FLAG`(무변경·후방호환).
    지정(M>1 슬롯 순회) → `regression-<slug>.json` 로 슬롯별 분리 — 여러 slot 이 같은 `.local/`
    을 공유하므로 세션명 슬러그를 파일명에 담아 슬롯 결과가 서로 덮이지 않게 한다.
    """
    if not session:
        return REGRESSION_FLAG
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", session)
    return LOCAL_DIR / f"regression-{slug}.json"


class _SlotRegressionState(NamedTuple):
    """슬롯 회귀 상태 판정 결과 — 상태 + 진단에 쓰는 기록값(rc·수집수·실행 당시 하한)."""
    state: str                    # green | stale | red | missing
    rc: int | str | None
    collected: int | None
    floor: int | None


def _regression_slot_state(session: str, cwd: str) -> _SlotRegressionState:
    """슬롯별 회귀 플래그를 읽어 상태를 판정 — (`'green'|'stale'|'red'|'missing'`, rc, 수집수, 하한).

    all-or-nothing check-first 의 저비용 판정 (pytest 미실행): per-slot 플래그
    (`_regression_flag_for(session)`)를 읽고 그 슬롯 worktree HEAD(`_git_head_at(cwd)`·각 슬롯은
    독립 worktree·독립 commit)와 대조한다. green(HEAD 일치·pass)이면 재실행 skip, 그 외
    (stale=HEAD 불일치·red=fail·missing=기록없음/손상)는 run 대상. 손상 플래그는 missing 강등
    (fail-soft — 장부 손상이 회귀해소를 깨면 안 된다·`_regression_slot_state` 는 재실행을 유도).

    rc 는 기록값 그대로다 — 강등이면 전용 라벨(`partial-collection`/`unverified-collection`·
    정수 아님)이라 기록된 수집수·하한과 함께 라벨에 사유를 실을 수 있다.

    **green 은 HEAD·status 만으로 재사용하지 않는다**: 하한을 켜거나 올린 뒤에도 옛 green 이
    통과하면(그리고 M>1 run 이 그 슬롯을 skip 하면) 새 하한이 영영 적용되지 않는다 —
    `_green_floor_stale` 로 기록 증거를 **그 슬롯 cwd 앵커의 현재 하한**과 대조해 stale 강등한다.
    """
    flag = _regression_flag_for(session)
    if not flag.exists():
        return _SlotRegressionState("missing", None, None, None)
    try:
        data = json.loads(flag.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _SlotRegressionState("missing", None, None, None)
    if not isinstance(data, dict):
        return _SlotRegressionState("missing", None, None, None)
    collected, floor = _flag_collected(data), data.get("floor")
    if data.get("head") != _git_head_at(cwd):
        return _SlotRegressionState("stale", data.get("rc"), collected, floor)
    if data.get("status") != "pass":
        return _SlotRegressionState("red", data.get("rc"), collected, floor)
    # 현재 하한은 **기록된 conf 앵커**(그 실행이 돈 트리)로 해소한다 — 슬롯 cwd 와 다를 수 있다
    # (`--cwd` 핀 실행). 미기록 플래그는 슬롯 cwd 폴백(후방호환).
    if _green_floor_stale(collected,
                          _regression_min_collected(_flag_conf_anchor(data, cwd))):
        # 기록은 green 이지만 현재 하한을 못 만족 → 재실행 대상(stale). rc/수집수는 기록값 유지.
        return _SlotRegressionState("stale", data.get("rc"), collected, floor)
    return _SlotRegressionState("green", data.get("rc"), collected, floor)


def _regression_run_slot(args: argparse.Namespace, session: str, cwd: str) -> int:
    """한 슬롯의 회귀를 pytest 로 측정·기록(슬롯별 플래그) — rc 반환.

    단일-슬롯 run 본체와 동형(같은 env·shell·인코딩·rc0 만 pass·rc5 vacuous 근절·수집 하한
    가드)이되, cwd/test_cmd/플래그/HEAD 를 슬롯별로 해소한다. 스코프(touches)는 훅 M>1 경로엔
    없으므로 full 만(플래그는 push 게이트 대상). 플래그 키는 그 슬롯 worktree HEAD 다
    (`_git_head_at`).

    conf 앵커 이어받기(`_inherit_flag_anchor`)는 여기 없다 — 슬롯 실행 위치는 리스 장부에서
    구조적으로 나오고(플래그도 그 세션 키), 장부가 슬롯을 옮겼다면 새 위치가 맞다.
    """
    cwd = os.path.abspath(cwd)   # 기록·conf 해소가 프로세스 cwd 에 안 흔들리게(단일-슬롯 동형).
    cmd = " ".join(p for p in (_test_cmd(args.cmd, session=session),
                               _quarantine_args()) if p)
    print(f"regression[{session}]: $ {cmd}  (cwd={cwd})")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    rc, out, _err = _run_regression_cmd(cmd, cwd, env)
    status = "pass" if rc == 0 else "fail"
    collected = _collected_count(out)        # 파싱은 stdout 단독(stderr 로그 오염 차단).
    floor = _regression_min_collected(cwd)   # 앵커 = 이 슬롯이 회귀를 도는 트리.
    status, recorded_rc, floor_note = _apply_collection_floor(rc, status, collected, floor)
    head = _git_head_at(cwd)
    note = (" · 수집 0 — 테스트 루트/cwd 확인" if rc == 5 else "") + floor_note
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_regression_flag_for(session),
                       {"head": head, "status": status, "rc": recorded_rc, "scope": "full",
                        "collected": collected, "floor": floor, "conf_anchor": cwd,
                        "session": session, "ts": now_utc()})
    print(f"regression[{session}]: {status} (rc={recorded_rc}{note}) @ {head[:8] or '?'}")
    # 반환은 게이트 판정 — 부분수집 강등(pytest rc0)도 비-0 이어야 M>1 순회가 red 로 센다.
    return 0 if status == "pass" else (rc or 1)


def _slot_state_label(session: str, state: str, rc: int | str | None,
                      collected: int | None = None, floor: int | None = None) -> str:
    """미검증 슬롯 라벨 — `<session>=<state>(rc=N·수집0)` (rc None 이면 상태만·codex sug).

    check 실패 메시지에 슬롯별 rc 를 실어 단일-슬롯 진단과 균질화한다 — 특히 rc5(수집 0)는
    테스트 루트/cwd 결함 신호이므로 힌트를 붙인다(`_regression_slot_state` 가 이미 rc 반환·저비용).
    수집 하한 강등(`partial-collection`/`unverified-collection`)도 같은 자리에 기록값 기반 사유를
    붙인다(rc5 힌트와 동형).
    """
    if rc is None:
        return f"{session}={state}"
    if rc == REGRESSION_RC_PARTIAL_COLLECTION:
        hint = "·" + _collection_shortfall_text(collected, floor)
    elif rc == REGRESSION_RC_UNVERIFIED_COLLECTION:
        hint = f"·수집수 미확인(하한 {'?' if floor is None else floor})"
    else:
        hint = "·수집0" if rc == 5 else ""
    return f"{session}={state}(rc={rc}{hint})"


def _regression_multi_check(sessions: list[str]) -> int:
    """M>1 check — 전 leased 슬롯이 green(HEAD 일치·pass)이어야 rc0 (all-or-nothing).

    미검증(stale/red/missing) 슬롯이 하나라도 있으면 rc1 로 push 를 차단하고 *어느 슬롯이
    어떤 상태(+rc)*인지 명시한다(디버깅 동선). pytest 는 안 돌린다(check = 저비용 baseline 검증).
    """
    not_green: list[str] = []
    for session in sessions:
        cwd = _active_slot_path(session) or str(REPO)
        slot_state = _regression_slot_state(session, cwd)
        if slot_state.state != "green":
            not_green.append(_slot_state_label(session, slot_state.state, slot_state.rc,
                                               slot_state.collected, slot_state.floor))
    if not_green:
        print(f"regression(M={len(sessions)}): 미검증 슬롯 [{', '.join(not_green)}] "
              "— `regression run` 필요 (push 차단).", file=sys.stderr)
        return 1
    print(f"regression(M={len(sessions)}): 전 슬롯 green ✓ [{', '.join(sessions)}]")
    return 0


def _regression_multi_run(args: argparse.Namespace, sessions: list[str]) -> int:
    """M>1 run — 슬롯별 check-first(green skip)·stale/red 만 pytest·하나라도 red 면 rc1.

    all-or-nothing: 어느 세션이 push 하든 전
    leased 슬롯이 green 이어야 통과. check-first 로 이미 green(기록 baseline·HEAD 일치)인 슬롯의
    pytest 재실행을 억제(비용 억제)하고, 실패 슬롯(red)을 종합 메시지에 명시한다(디버깅 동선).
    """
    skipped: list[str] = []
    ran: list[str] = []
    red: list[str] = []
    for session in sessions:
        cwd = _active_slot_path(session) or str(REPO)
        if _regression_slot_state(session, cwd).state == "green":
            skipped.append(session)
            continue
        # stale/red/missing → 이 슬롯 pytest 실행·슬롯별 플래그 갱신.
        ran.append(session)
        if _regression_run_slot(args, session, cwd) != 0:
            red.append(session)
    summary = (f"regression(M={len(sessions)}): "
               f"skip(green) {len(skipped)} · run {len(ran)}")
    if red:
        print(f"{summary} · RED [{', '.join(red)}] — push 차단.", file=sys.stderr)
        return 1
    print(f"{summary} · 전 슬롯 green ✓")
    return 0


def cmd_regression(args: argparse.Namespace) -> int:
    """run = 측정+기록(HEAD 키), check = HEAD 가 green 인지 (pre-push 훅이 호출).

    M>1(leased ≥2) 홈은 전 leased 슬롯 all-or-nothing 으로 해소한다: 훅은
    --repo/--slot 을 못 넘기므로 활성 슬롯이 여럿이면 슬롯별 check-first(저비용) 후 stale/red 만
    run 하며 하나라도 red 면 push 를 차단한다.

    **보호 게이트는 ambient env(PM_SESSION_NAME/CLAUDE_SESSION_NAME)로 좁혀지면 안 된다**
    (codex): 훅 프로세스가 env 세션을 상속하면 "어느 세션이 push 하든 전 leased 슬롯 green"이
    조용히 자기 슬롯 단일 경로로 우회된다. 그래서 M>1 디스패치 판정은 **CLI `--repo`/`--slot` 명시**
    (문서화된 의도적 조작)만 단일-슬롯으로 좁히고 env 는 이 판정에서 제외한다 — 단일-lease/
    솔로/명시는 현행 결과 동일. (env 는 단일-슬롯 threading 등 다른 해소엔 그대로 유효.)
    """
    # task-mode(`--task`) 실행 위치 — 특정 슬롯 worktree 절대경로를 cwd
    # 로 고정하고 슬롯 test_cmd 를 실어 잘못된 형제-슬롯 유도를 피한다. 절대경로를
    # surface 해 dev/git 짐작 여지를 없앤다(cwd 비참여). run 만 실행 위치가 필요하다.
    if args.action == "run" and getattr(args, "task", None):
        task_cwd, task_test_cmd = _resolve_task_workspace_cwd(args)
        if getattr(args, "cwd", None) is None:
            args.cwd = task_cwd
        if getattr(args, "cmd", None) is None:
            # 슬롯을 **확정**했으므로 그 슬롯 test_cmd 를 직접 싣는다 — session 유도
            # (`_active_slot_test_cmd(<task>)`)는 같은 task 의 **형제 슬롯 첫-매칭**을 반환해 오매칭
            # 여지가 있다(reviewer). 미바인딩(None)이면 슬롯 레이어를 건너뛰고 repo/local 폴백
            # (`session=None`)으로 — 형제 슬롯 test_cmd 를 타지 않는다(결정론).
            args.cmd = task_test_cmd or _test_cmd(None, session=None)
        print(f"regression: 작업공간(task {args.task}) → {task_cwd}")
    # 디스패치 판정은 CLI --repo/--slot(명시) 만 본다 — env 세션은 M>1 게이트를 조용히 좁히므로
    # 여기선 제외(위 docstring·codex). explicit 없고 leased ≥2 면 env 유무 무관 전-슬롯 순회.
    #
    # **명시 `--cwd`(run) 대칭**:
    # `--cwd` 가 명시되면 실행 위치가 그 경로로 핀돼(단일 위치) session 이 cwd/디스패치에 불요하다.
    # 그런데 eager `_actor_session_override` 가 `--repo` 단독+활성 ≥2(또는 0)에서 SlotResolutionError
    # 를 오발화해 readonly-핀 처방(`regression run --repo <r> --cwd <readonly>`)을 막던
    # 것 — livegate 와 같은 노출이다. `--cwd` 핀이면 actor 해소를 **soft**(모호/미해소 → None·raise
    # 아님)로 낮춰 그 cwd 실행을 존중하고 M>1 순회도 건너뛴다(명시 cwd=단일 위치). session 은 슬롯
    # test_cmd 유도에만 남으므로(cwd override 최우선) `--repo`/`--slot` 명시면 그 슬롯 test_cmd 를
    # 그대로 유지하고(soft 는 kind=slot 을 안 건드림·pickable 할 때만), 모호(→None)면 `--cmd`/repo/
    # local 폴백을 탄다(결정 절 (a)).
    _run_cwd_pinned = args.action == "run" and getattr(args, "cwd", None)
    explicit_override = _actor_session_override(args, soft=bool(_run_cwd_pinned))
    if not _run_cwd_pinned and explicit_override is None:
        leased = identity_args.leased_sessions(LEASES_FILE)
        if len(leased) >= 2:
            slots = sorted(set(leased))
            return (_regression_multi_run(args, slots) if args.action == "run"
                    else _regression_multi_check(slots))
    # 단일-슬롯 (명시 --repo/--slot·단일-lease·솔로·명시 --cwd 핀) — 현행 경로. sess 는 슬롯
    # test_cmd/cwd threading 용(env 유효·위에서 M>1 만 걸러냄·--cwd 핀은 soft 해소).
    sess = session_name(explicit_override)
    if args.action == "run":
        touches = (_ticket_touches(args.ticket) if getattr(args, "ticket", None)
                   else (args.touches.split(",") if getattr(args, "touches", None) else []))
        scoped = bool(touches)
        parts = [_test_cmd(args.cmd, session=sess)]
        if scoped:
            parts.append(_scope_args(touches))
        parts.append(_quarantine_args())
        cmd = " ".join(p for p in parts if p)
        print(f"regression: $ {cmd}")
        # shell=True 로 띄운 pytest 자식은 별도 프로세스 — 부모 콘솔 reconfigure 보호를
        # 못 받는다. 자식의 인코딩을 도구가 코드로 명시(env 워크어라운드 아님): 한국어
        # Windows(cp949 콘솔)에서도 자식 stdout/stderr·파일 IO 를 UTF-8 로 강제.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        # cwd seam — multi-PM 모델은 활성 repo 의 worktree 에서 돌아야 한다.
        # `--cwd` 주입 시 그 경로, 미주입(솔로/multi-PM-미배선)은 REPO. **절대경로로 정규화**해
        # 실행·기록·이후 conf 해소가 프로세스 cwd 에 흔들리지 않게 한다(상대 `--cwd` 방어).
        explicit_cwd = getattr(args, "cwd", None)
        cwd = os.path.abspath(_regression_cwd(explicit_cwd, session=sess))
        if not scoped and not explicit_cwd:
            # 훅 `check || run` 재실행 — 차단 기록이 있으면 그 앵커에서 돈다(위 함수 참조).
            cwd = _inherit_flag_anchor(cwd)
        rc, out, _err = _run_regression_cmd(cmd, cwd, env)
        # pass = rc0 한정. pytest rc5(수집 0·"no tests ran")는 fail — 수집 0 은 green 이
        # 아니라 테스트 루트/cwd 결함이다.
        # 미해소 시 상시 vacuous green 이었다.
        status = "pass" if rc == 0 else "fail"
        collected = _collected_count(out)     # 파싱은 stdout 단독(stderr 로그 오염 차단).
        rc5_note = _regression_rc5_note(rc, cwd, getattr(args, "cwd", None))
        if scoped:
            # 스코프 실행 = dev 빠른 피드백 (advisory). full 만 push 게이트 → 게이트 플래그 안 씀.
            # 수집 하한도 FULL 게이트 전용이다 — 스코프는 touches 매칭분만 도는 게 정상이라
            # 하한을 대면 상시 false-RED 가 된다.
            print(f"regression(scoped, {len(touches)} touches): {status} (rc={rc}{rc5_note}) "
                  "— dev 피드백 · push 게이트 아님")
            return 0 if status == "pass" else 1
        # FULL 게이트 = push 게이트. rc0 라도 실행 수가 하한 미만이면 부분 수집으로 강등한다.
        # 하한 앵커는 **회귀를 도는 트리**(cwd) — 호출 사본의 REPO 가 아니다.
        floor = _regression_min_collected(cwd)
        status, recorded_rc, floor_note = _apply_collection_floor(rc, status, collected, floor)
        detail = f"{status} (rc={recorded_rc}{rc5_note}{floor_note})"
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        REGRESSION_FLAG.write_text(json.dumps(
            {"head": _git_head(), "status": status, "rc": recorded_rc, "scope": "full",
             "collected": collected, "floor": floor, "conf_anchor": cwd,
             "ts": now_utc()}), encoding="utf-8")
        print(f"regression: {detail} @ {_git_head()[:8] or '?'}")
        return 0 if status == "pass" else 1
    # action == "check" — pre-push 게이트
    if not REGRESSION_FLAG.exists():
        print("regression: 기록 없음 — `board.py regression run` 필요 (push 차단).",
              file=sys.stderr)
        return 1
    data = json.loads(REGRESSION_FLAG.read_text(encoding="utf-8"))
    head = _git_head()
    if data.get("head") != head:
        print(f"regression: stale (기록 {str(data.get('head'))[:8]} ≠ HEAD {head[:8]}) "
              "— 재실행 필요.", file=sys.stderr)
        return 1
    if data.get("status") != "pass":
        rc = data.get("rc")
        # rc5(수집 0)는 fail 로 기록된다— RED 사유를 push 게이트에서 드러내
        # "테스트가 안 돌았는데 green" 이던 침묵 폴백을 진단 가능하게 한다(run/check 일관).
        # 수집 하한 강등도 같은 자리에 기록값 기반 사유를 실어 실 red 와 구분한다.
        if rc == REGRESSION_RC_PARTIAL_COLLECTION:
            extra = (f" · {_collection_shortfall_text(_flag_collected(data), data.get('floor'))} "
                     "— 부분 수집 의심(테스트 루트/cwd 확인)")
        elif rc == REGRESSION_RC_UNVERIFIED_COLLECTION:
            extra = (f" · 수집수 파싱 실패 — 하한 {data.get('floor')} 검증 불가"
                     "(요약행 없는 test_cmd 면 하한 해제 필요)")
        else:
            extra = " · 수집 0 — 테스트 루트/cwd 확인" if rc == 5 else ""
        print(f"regression: RED @ {head[:8]} (rc={rc}){extra} — push 차단.",
              file=sys.stderr)
        return 1
    # green 기록이라도 **현재** 하한을 못 만족하면 재사용하지 않는다 — 하한을 켜거나 올린 뒤
    # 옛 green 이 그대로 통과하면 새 하한이 영영 미적용이다(run 쪽 강등과 산술 대칭).
    # 하한 해소 앵커 = **그 실행이 기록한 conf 앵커**. 훅은 `--cwd` 를 못 넘기므로 여기서
    # 재해소하면 `run --cwd <tree>` 와 다른 local.conf 를 읽는다(false-green/false-RED).
    recorded_collected = _flag_collected(data)
    floor_now = _regression_min_collected(_flag_conf_anchor(
        data, _regression_cwd(getattr(args, "cwd", None), session=sess)))
    if _green_floor_stale(recorded_collected, floor_now):
        print(f"regression: stale (기록 수집 {recorded_collected} < 현재 하한 {floor_now} "
              "— 하한 신규/상향) — 재실행 필요.", file=sys.stderr)
        return 1
    print(f"regression: green @ {head[:8]} ✓")
    return 0


# ── 릴리즈 라이브 게이트 ────────────────────────────────────────
# 라이브 LLM 검증(실 하네스 smoke)을 릴리즈(main 머지) 단일 지점으로 모은 게이트.
# `livegate record` 가 `pytest -m release` 를 회귀와 동일한 cwd 해소로 실행·측정하고
# (실행=기록·손기록 없음), 보호훅이 `livegate check --rev <sha>` 로 push HEAD 가 green 인지
# 소비한다. false-green 방어를 위해 rc0 만으로는 부족하고 수집 N==pin 을 함께 요구한다
# (수집 pin·rc5 vacuous-pass 근절의 원칙을 라이브 채널로 확장).
LIVEGATE_RELEASE_PIN = 18  # `pytest -m release` 로 돌아야 하는 라이브/사이클 케이스 수.
                           # tests/test_release_wave.py `_EXPECTED_RELEASE_TESTS` 와 값 공유.
LIVEGATE_TEST_CMD = "pytest -m release -q"   # 라이브 릴리즈 wave selection.


# 라이브 게이트의 "실행 수" 로 세는 outcome 종류 — 마커 안에서 *돈* 케이스만.
# `deselected`(release 마커 밖)·`skipped` 는 실행분이 아니라 제외한다.
_LIVEGATE_RAN_KINDS = ("passed", "failed", "errors?")


def _livegate_ran_count(output: str) -> int:
    """`pytest -m release -q` 요약행에서 *실제 실행된* release 테스트 수(수집 N)를 센다.

    N = passed + failed + error(s). deselected 는 release 마커 밖이라 세지 않는다. "no tests
    ran"(exit5)처럼 요약에 카운트가 없으면 0. 이 수집 N 을 pin 과 대조해 마커 소실·wrong-cwd
    로 인한 false-green(수집 위장)을 차단한다 — rc0 만으로는 "0개 수집됐지만 red 아님"을
    green 으로 삼킬 수 있다. 대상 줄은 공용 seam 이 **끝에서** 찾는다(라이브 wave 가 캡처
    출력에 찍는 `N passed` 로그를 요약으로 오판하면 pin 대조가 통째로 어긋난다).
    """
    line = _pytest_summary_line(output)
    if line is None:
        return 0
    return sum(_pytest_outcome_counts(line, _LIVEGATE_RAN_KINDS))


def _write_json_atomic(path: Path, data: dict) -> None:
    """dict → JSON 을 temp + `os.replace` 로 원자 교체한다 (crash 시 잔재/부분기록 방지).

    `dump_ticket_atomic` 과 동형 — 같은 디렉토리 안 tmp 에 전체를 쓰고 atomic rename.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(str(tmp), str(path))


# ── livegate 기록 위치 = 훅 read 위치 정렬 (two-git 단일 소스) ─────────────
# `livegate record` 는 push 보호훅이 읽는 **바로 그** livegate.json 에 기록해야 한다. 훅은 repo 의
# git `core.hooksPath` 에 설치돼(worktree_pool.install_protected_hook), 같은 디렉토리의 sidecar
# `engine-root`(PM 홈 REPO 절대경로 1줄)로 `<engine-root>/.project_manager/tools/board.py` 를 해소하고
# 그 사본의 `.local/livegate.json` 을 `livegate check` 로 읽는다(worktree_pool `_PROTECTED_PRE_PUSH_HOOK`).
# two-git 토폴로지(PM 홈+worktree)에서 record 를 호출된 사본의 `REPO/.local` 에 그냥 쓰면,
# worktree board.py 로 record 할 때 훅이 안 읽는 worktree `.local` 에 조용히 기록→pass 위장→push
# 순간에야 불일치로 드러난다. 그래서 record 도 훅과 **동일한 engine-root
# sidecar 해소**를 공유해 같은 파일에 기록한다(단일 소스). 단일-repo/솔로(livegate 훅 없음)면 현행
# `REPO/.local` 폴백이라 채택자 무변경.
_LG_ENGINE_ROOT = "engine-root"  # 프레임워크 push 보호훅 활성 → PM 홈 .local (단일 소스).
_LG_SOLO = "solo"                # livegate 훅 없음 → 호출된 사본 REPO/.local (단일-repo/솔로 무변경).
_LG_BROKEN = "broken"            # 훅 sidecar 존재하나 engine-root 무효 → fail-loud(false-green 차단).


def _git_config_get(cwd: str, key: str) -> str | None:
    """`git -C <cwd> config --get <key>` 값 (미설정/비-repo/빈값이면 None).

    인코딩 명시(git path 출력이 cp949 로 디코딩되지 않도록 utf-8·`_hooks_dir` 동형).
    """
    r = subprocess.run(["git", "-C", cwd, "config", "--get", key],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    val = r.stdout.strip() if r.returncode == 0 else ""
    return val or None


def _resolve_livegate_flag(cwd: str) -> tuple[Path, str]:
    """record 가 기록할 livegate.json 경로 = **push 보호훅이 읽는 파일**로 정렬한다.

    보호훅(`_PROTECTED_PRE_PUSH_HOOK`)과 동일 해소를 공유한다 — repo(`cwd`)의 git
    `core.hooksPath` 옆 sidecar `engine-root`(PM 홈 REPO 절대경로 1줄)를 읽어, 그 root 에
    board.py 가 실재하면(훅의 `[ -f "$engine_root/.../board.py" ]` fail-closed 게이트와 동일
    검증) `<engine-root>/.project_manager/.local/livegate.json` 을 돌려준다. 반환:
      - (PM 홈 .local livegate.json, `_LG_ENGINE_ROOT`) — 훅 활성·단일 소스.
      - (`LIVEGATE_FLAG`, `_LG_SOLO`) — livegate 훅 없음(단일-repo/솔로·현행 폴백 무변경).
      - (`LIVEGATE_FLAG`, `_LG_BROKEN`) — 훅 sidecar 는 있으나 engine-root 무효(빈값/board.py
        부재). 이땐 훅 read 위치와 기록 위치가 갈릴 수 있어 호출부가 fail-loud 로 거부한다.
    """
    hooks_path = _git_config_get(cwd, "core.hooksPath")
    if not hooks_path:
        return LIVEGATE_FLAG, _LG_SOLO
    # 상대 `core.hooksPath` 는 git 이 worktree root(=`cwd`) 기준으로 해소한다(프로세스 cwd 아님).
    # 프레임워크 설치는 절대경로라 평시 무영향이나, 수동/상대 설정 방어로 git 시맨틱을 미러.
    hp = Path(hooks_path)
    if not hp.is_absolute():
        hp = Path(cwd) / hp
    sidecar = hp / "engine-root"
    if not sidecar.is_file():
        # `core.hooksPath` 는 있으나 engine-root sidecar 부재 = livegate 보호훅 아님(예: PM 홈
        # 자신의 회귀 훅·채택자 custom 훅) → 솔로 폴백(오탐 fail-loud 방지).
        return LIVEGATE_FLAG, _LG_SOLO
    lines = sidecar.read_text(encoding="utf-8").splitlines()
    engine_root = lines[0].strip() if lines else ""
    if not engine_root:
        return LIVEGATE_FLAG, _LG_BROKEN
    root = Path(engine_root)
    if not (root / ".project_manager" / "tools" / "board.py").is_file():
        return LIVEGATE_FLAG, _LG_BROKEN
    return root / ".project_manager" / ".local" / "livegate.json", _LG_ENGINE_ROOT


def _livegate_record(args: argparse.Namespace) -> int:
    """`pytest -m release` 를 실행·측정하고 결과를 livegate.json 에 기록한다 (실행=기록).

    회귀와 동일한 cwd 해소(`_regression_cwd` — 활성 slot worktree)로 subprocess 실행 →
    **rc0 그리고 수집 N==pin** 일 때만 `pass @ <worktree HEAD>` 기록. rc!=0 또는 N!=pin 이면
    `fail` 기록 + rc1(사유 표면화). 손기록 경로는 없다(위조/착오 차단). 기록 위치는
    push 보호훅 read 위치(`_resolve_livegate_flag` — engine-root sidecar)와 정렬해 단일 소스다
    (worktree/PM 홈 어느 board.py 로 돌려도 훅이 읽는 한 파일).

    `--repo`/`--slot`은 regression/handoff 과 동형으로 `session_name` 해소를 거쳐
    `_regression_cwd` 에 thread 한다 (M>1 홈에서 슬롯 cwd 를 명시 — `--cwd` 절대경로 핀 우회
    불요). 무명시 + leased ≥2 는 seam 이 fail-loud(모호는 시끄럽게) 하며, 그 메시지가
    안내하는 `--repo <repo> --slot <N>` 이 실제로 이 subparser 에서 수용돼 dead-end 가 아니다
    (remedy 정직·anti-pattern 회피).
    """
    # task-mode(`--task`) 실행 위치 — 특정 슬롯 worktree 절대경로를 cwd 로
    # 고정하고 surface 한다(cwd 비참여·livegate 는 고정 release cmd 라 test_cmd 는 불요).
    if getattr(args, "task", None):
        task_cwd, _ = _resolve_task_workspace_cwd(args)
        if getattr(args, "cwd", None) is None:
            args.cwd = task_cwd
        print(f"livegate: 작업공간(task {args.task}) → {task_cwd}")
    # `--cwd` 명시(릴리즈 readonly 슬롯 핀)면 session 해소 자체를 생략한다 —
    # `_regression_cwd` 는 override 최우선이라 session 이 불요한데, eager 해소가 `--repo` 단독
    # actor 특정(resolve_actor_slot)을 타서 readonly(leased) 슬롯 추가로 활성 ≥2 가 되는 순간
    # 모호 fail-loud 를 오발화시켰다.
    _explicit_cwd = getattr(args, "cwd", None)
    cwd = _regression_cwd(_explicit_cwd,
                          session=(None if _explicit_cwd
                                   else session_name(_actor_session_override(args))))
    # 기록 위치를 push 보호훅 read 위치와 정렬(단일 소스) — **실행 전에** 해소한다. 훅과 같은
    # engine-root sidecar 해소를 공유해, worktree board.py·PM 홈 board.py 어느 쪽으로 돌려도 훅이
    # 읽는 한 파일에 기록. engine-root 무효(BROKEN)는 실행 전에 알 수 있으니, 값비싼 `pytest -m
    # release`(라이브 wave)를 헛돌리기 전에 fail-loud 로 조기 거부한다.
    flag, mode = _resolve_livegate_flag(cwd)
    if mode == _LG_BROKEN:
        # 보호훅은 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소)
        # → 기록 위치와 훅 read 위치가 갈릴 수 있어 pass 위장을 찍지 않고 fail-loud 거부한다
        # (false-green 백스톱). engine-root sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단). engine-root sidecar 수리 또는 "
              "`pm-config repo add` 재실행. 릴리즈 차단.", file=sys.stderr)
        return 1
    print(f"livegate: $ {LIVEGATE_TEST_CMD}  (cwd={cwd})")
    # 자식 pytest 인코딩을 코드로 명시(env 워크어라운드 아님) — cp949 콘솔에서도 UTF-8.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(LIVEGATE_TEST_CMD, shell=True, cwd=cwd, env=env,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    rc = result.returncode
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        print(output if output.endswith("\n") else output + "\n", end="")
    n = _livegate_ran_count(output)
    head = _git_head_at(cwd)
    passed = rc == 0 and n == LIVEGATE_RELEASE_PIN
    status = "pass" if passed else "fail"
    flag.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(flag,
                       {"head": head, "status": status, "n": n, "rc": rc, "ts": now_utc()})
    if passed:
        print(f"livegate: pass @ {head[:8] or '?'} "
              f"(release {n}/{LIVEGATE_RELEASE_PIN} green) ✓")
        return 0
    if rc != 0:
        reason = f"release red (rc={rc})"
    else:
        reason = (f"수집 {n} ≠ pin {LIVEGATE_RELEASE_PIN} — 마커 소실/wrong-cwd 의심"
                  " (수집 위장 차단)")
    print(f"livegate: fail @ {head[:8] or '?'} — {reason}. 릴리즈 차단.", file=sys.stderr)
    return 1


def _livegate_check(args: argparse.Namespace) -> int:
    """기록된 라이브 게이트가 `--rev <sha>` 에 대해 green 인지 검증한다 (보호훅 소비 채널).

    기록 존재 ∧ status==pass ∧ head==rev → rc0. 아니면 rc1 + 사유 3분화(기록 부재 / red /
    rev 불일치 — 각각 다른 메시지로 훅이 그대로 surface). fail-soft 아님(명확 rc).

    읽을 livegate.json 은 record 와 **동일 해소**(`_resolve_livegate_flag` — engine-root sidecar)
    를 거친다(단일 소스). 모듈상수 `LIVEGATE_FLAG` 직독이 아니라, 어느 board.py 사본/cwd 로
    check 하든 push 보호훅이 기록한 **바로 그** 파일을 읽어 stale/wrong-copy 오독(false-green/
    false-red)을 원천 차단한다 — record(`_livegate_record`)의 기록-위치 정렬과 대칭이다.
    `cwd` = `--cwd`(record 와 대칭·미지정 시 이 board.py 사본의 `REPO` — 그 repo 의 `core.hooksPath`
    로 engine-root 해소). engine-root 무효(`_LG_BROKEN`)는 record 와 **동형** fail-loud(조용한 통과 금지).
    """
    rev = getattr(args, "rev", None)
    if not rev:
        print("livegate check: --rev <sha> 필요 (push 대상 커밋).", file=sys.stderr)
        return 1
    # 읽을 위치를 push 보호훅 read 위치와 정렬(record 와 대칭·단일 소스). 훅과 같은
    # engine-root sidecar 해소를 공유해, worktree board.py·PM 홈 board.py 어느 사본으로 check 해도
    # 훅이 기록한 한 파일을 읽는다. hooksPath 미설정/솔로면 현행 LIVEGATE_FLAG(REPO/.local) 폴백 무변경.
    cwd = getattr(args, "cwd", None) or str(REPO)
    flag, mode = _resolve_livegate_flag(cwd)
    if mode == _LG_BROKEN:
        # 보호훅 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소) →
        # 기록 위치와 훅 read 위치가 갈릴 수 있어 pass/fail 판정을 신뢰 못 한다. 조용한 통과 대신
        # record 와 동형 fail-loud 거부(false-green/false-red 백스톱). engine-root
        # sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단). engine-root sidecar 수리 또는 "
              "`pm-config repo add` 재실행. 릴리즈 차단.", file=sys.stderr)
        return 1
    if not flag.exists():
        print("livegate: 기록 없음 — `board.py livegate record` 필요 (릴리즈 차단).",
              file=sys.stderr)
        return 1
    data = json.loads(flag.read_text(encoding="utf-8"))
    if data.get("status") != "pass":
        n, rc = data.get("n"), data.get("rc")
        print(f"livegate: RED (status={data.get('status')}, 수집 {n}, rc={rc}) "
              "— 릴리즈 차단.", file=sys.stderr)
        return 1
    head = data.get("head")
    if head != rev:
        print(f"livegate: rev 불일치 (기록 {str(head)[:8]} ≠ push {str(rev)[:8]}) "
              "— `livegate record` 재실행 필요 (릴리즈 차단).", file=sys.stderr)
        return 1
    print(f"livegate: green @ {str(rev)[:8]} ✓")
    return 0


def cmd_livegate(args: argparse.Namespace) -> int:
    """record = `pytest -m release` 실행+기록 / check = push HEAD green 검증 (보호훅용)."""
    if args.action == "record":
        return _livegate_record(args)
    return _livegate_check(args)


def now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def find_item(base_dir: Path, statuses: tuple[str, ...], item_id: str,
              kind: str = "item") -> tuple[str, Path]:
    """Return (status_dir, path) for an `{item_id}-*.md` file under base_dir.

    Generic over tickets and ideas — the lookup is identical, only the
    directory layout and ID shape differ. Raises FileNotFoundError if missing.
    """
    for status in statuses:
        for p in (base_dir / status).glob(f"{item_id}-*.md"):
            return status, p
    raise FileNotFoundError(f"{kind} not found: {item_id}")


def find_ticket(tid: str) -> tuple[str, Path]:
    """Return (status_dir, path). Raises FileNotFoundError if missing.

    STATUS_DIRS(open/claimed/blocked/done)에서 못 찾으면 draft 격리 디렉토리
    (`drafts_dir()`)를 폴백으로 스캔해 pseudo-status `"draft"` 로 반환한다 — `show`/
    `promote`가 draft 를 조회할 수 있게 한다. 다른 mutation(claim/block/complete 등)이 이
    경로로 draft 를 찾아도 각자의 status 검사(`!= "open"` 등)가 즉시 거부하므로 안전하다
    (예: "cannot claim T-x: currently in draft/" — 오히려 명확한 신호).
    """
    try:
        return find_item(tickets_dir(), STATUS_DIRS, tid, "ticket")
    except FileNotFoundError:
        for p in drafts_dir().glob(f"{tid}-*.md"):
            return "draft", p
        raise


def find_idea(iid: str) -> tuple[str, Path]:
    """Return (status_dir, path) for idea `iid`. Raises FileNotFoundError."""
    return find_item(IDEAS_DIR, IDEA_STATUS_DIRS, iid, "idea")


def _parse_ticket_text(text: str, source: Path | str) -> tuple[dict[str, Any], str]:
    """`load_ticket`의 실제 frontmatter 문법으로 문자열을 파싱한다.

    파일 I/O와 파서를 분리해, 쓰기 전 변환 결과도 런타임 소비자와 완전히 같은 문법으로
    검증할 수 있게 한다. `source`는 오류 메시지의 파일 식별자일 뿐 파싱에는 관여하지 않는다.
    """
    if not text.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {source}")
    # Split on the FIRST closing '---' after the opener
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        raise ValueError(f"unterminated frontmatter: {source}")
    fm_text = after_open[:end]
    body = after_open[end + 5:]
    fm = yaml.safe_load(fm_text) or {}
    return fm, body


def load_ticket(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body string)."""
    return _parse_ticket_text(path.read_text(encoding="utf-8"), path)


def dump_ticket(path: Path, fm: dict[str, Any], body: str) -> None:
    # A partial fresh-adopter scaffold must not turn a valid lifecycle write
    # into FileNotFoundError.  The destination directory is part of the write
    # contract, including `new`'s initial open/ ticket.
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm_text}\n---\n{body}", encoding="utf-8")


def dump_ticket_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    """`dump_ticket` 과 같은 바이트를 쓰되 temp 파일 + `os.replace` 로 원자 교체한다.

    부분쓰기로 티켓 frontmatter 가 깨지는 것을 막는다(worktree_pool `_write_ledger`
    동형 — tmp 에 전체를 쓰고 같은 디렉토리 안에서 atomic rename). backfill 처럼
    *기존* 티켓을 제자리 갱신할 때 쓴다 — 같은 status 디렉토리 안 rename 이라 원자적이다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"---\n{fm_text}\n---\n{body}", encoding="utf-8")
    os.replace(str(tmp), str(path))


def move_item(base_dir: Path, src: Path, dst_status: str) -> Path:
    """Atomic mv of an item file into a sibling status directory.

    On POSIX rename(2) is atomic and a lost race surfaces as FileNotFoundError
    (the source is already gone). On Windows os.rename is NOT exclusive across
    concurrent processes, so a caller that needs
    mutual exclusion for a *contended* transition must serialize it itself —
    `cmd_claim` wraps its load→rename in `board_lock` for exactly this reason.
    Uncontended transitions (complete/block on a ticket a single session already
    owns) never race, so this primitive stays lock-free. Generic over tickets
    and ideas.
    """
    dst_dir = base_dir / dst_status
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    os.rename(src, dst)
    return dst


def move_ticket(src: Path, dst_status: str) -> Path:
    """Atomic mv into a ticket status directory."""
    return move_item(tickets_dir(), src, dst_status)


def move_idea(src: Path, dst_status: str) -> Path:
    """Atomic mv into an idea status directory."""
    return move_item(IDEAS_DIR, src, dst_status)


def next_numeric_id(base_dir: Path, statuses: tuple[str, ...],
                    glob_pat: str, id_re: str) -> int:
    """Return the next free integer ID across every status directory.

    `glob_pat` selects candidate files; `id_re` extracts the integer from a
    filename (its first group). Generic over tickets (`T-NNNN`) and ideas
    (`NNNN`).
    """
    max_id = 0
    pattern = re.compile(id_re)
    for d in statuses:
        for p in (base_dir / d).glob(glob_pat):
            m = pattern.match(p.name)
            if m:
                max_id = max(max_id, int(m.group(1)))
    return max_id + 1


# ID 스캔용 status 튜플 — STATUS_DIRS + draft 격리 디렉토리(`.drafts`). draft 도 이미
# 발행된 ID(파일명에 박제)이므로 스캔에서 빠지면 promote 전 다음 `new` 가 같은 번호를 재사용해
# 충돌한다. `next_numeric_id` 는 `base_dir / d` 로 순회하므로 디렉토리명 그대로 넣으면 된다.
_ID_SCAN_STATUSES: tuple[str, ...] = (*STATUS_DIRS, ".drafts")


def _next_prefixed_id(prefix: str) -> str:
    """Next `T-<canonical>-NNN` for `prefix`, matched **case-insensitively**.

    prefix 동일성은 case-insensitive fold 이되 발행 ID 는 *기존 시리즈* case(canonical)를 이어쓴다
    — `--prefix aaa` 가 case-분할 `T-aaa-*` 를 새로 파지 않고 기존 `T-AAA-*` 를 잇게 한다. 파일명을
    LITERAL prefix + `re.IGNORECASE` 로 매치(하이픈 포함 prefix 도 번호 경계 모호성 0·현행
    `T-{prefix}-(\\d+)-` 규칙과 동형)해 prefix 가 fold-일치하는 모든 case 변종의 최대 번호+1 을
    센다(case-insensitive 카운트). canonical case = 최저 번호 티켓의 실제 case(결정적). fold-일치
    티켓이 없으면 입력 case 그대로(최초 사용이 canonical case 확립)·번호 1.
    """
    fold_re = re.compile(rf"^T-({re.escape(prefix)})-(\d+)-", re.IGNORECASE)
    max_num = 0
    canonical = prefix                 # 최초 사용: 입력 case 가 canonical.
    anchor_num: int | None = None      # 최저 번호(그 티켓 case 를 canonical 로·결정적).
    for d in _ID_SCAN_STATUSES:
        for p in (tickets_dir() / d).glob("T-*.md"):
            m = fold_re.match(p.name)
            if not m:
                continue
            num = int(m.group(2))
            max_num = max(max_num, num)
            if anchor_num is None or num < anchor_num:
                anchor_num = num
                canonical = m.group(1)   # 파일명에 쓰인 실제 case (예 `AAA`).
    return f"T-{canonical}-{max_num + 1:03d}"


def _next_id(prefix: str | None = None) -> str:
    """Next ticket ID. Namespaced per prefix so concurrent areas never collide.

    prefix=None → legacy `T-NNNN` (4-digit). prefix="PAY" → `T-PAY-NNN` (3-digit),
    counted independently. The legacy regex `T-(\\d+)-` never matches a prefixed
    file, so the two namespaces stay disjoint. prefix 동일성은 case-insensitive fold —
    `--prefix aaa` 는 기존 `T-AAA-*` 시리즈를 이어간다(`_next_prefixed_id`).
    legacy 경로(prefix=None)는 무변경(`T-NNNN` 회귀 0).
    """
    if prefix:
        return _next_prefixed_id(prefix)
    n = next_numeric_id(tickets_dir(), _ID_SCAN_STATUSES, "T-*.md", r"T-(\d+)-")
    return f"T-{n:04d}"


def _next_idea_id() -> str:
    n = next_numeric_id(IDEAS_DIR, IDEA_STATUS_DIRS, "[0-9]*.md", r"(\d+)-")
    return f"{n:04d}"


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9가-힣-]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "ticket"


# ── commands ───────────────────────────────────────────────────────────

def cmd_claim(args: argparse.Namespace) -> int:
    _reject_task_slot_identity_mix(args)
    # claim = 귀속 쓰기 — 세션 미해소면 fail-loud
    # 명시 --repo/--slot> env > 단일-lease 유도 >
    # (solo) local.conf.
    override = _actor_session_override(args)
    sess = session_name(override, required=True)
    # claimed_by 는 `<user>/<slot>` — user 미상이면 슬롯만
    # (graceful·기존 슬롯-only 값과 형태 동일). 진행메시지/board surface 는 슬롯(sess)을 쓴다.
    assignee = identity_tag(session_override=override,
                            user_override=getattr(args, "user", None))

    # board-git 트랜잭션(prefetch → commit → push → rollback)을 통째로 직렬화한다 —
    # 같은 clone 의 다른 슬롯이 그 사이에 끼어들어 서로의 커밋을 되돌리는 창을 없앤다. 락 순서는
    # **board_git_lock → board_lock** 고정(아래 임계구역이 안쪽) — 역순 획득은 어디에도 없다.
    with board_git_lock():
        return _cmd_claim_locked(args, assignee)


def _cmd_claim_locked(args: argparse.Namespace, assignee: str) -> int:
    """`cmd_claim` 본체 — board_git_lock 보유 전제 (재진입 금지)."""
    # claim STRICT 1단계: 선점 감지는 **읽기 전용**(fetch + 원격 트리
    # 직접 조회)이고, `pull --rebase` 는 원격이 앞섰을 때만 시도한다. 차단은 "원격이 앞섰고
    # 통합 불가" 로만 남고, 사유(dirty/rebase 충돌/offline/upstream 없음)와 진단 재료(behind·
    # 더러운 파일)를 구조체로 받아 **한 곳에서** 안내를 렌더한다(오판·이중출력 0).
    prefetch = _board_git_claim_prefetch(args.id)
    if prefetch.block is not None:
        print(_claim_block_message(args.id, prefetch), file=sys.stderr)
        return 1
    orig_head = prefetch.anchor

    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot claim {args.id}: currently in {status}/", file=sys.stderr)
        return 1

    # The claim critical section (load → dependency check → open→claimed rename)
    # runs under board_lock so the transition is *serialized* across sessions.
    # On POSIX the atomic rename(2) alone was exclusive; on Windows os.rename is
    # NOT exclusive across concurrent processes, so without serialization every
    # concurrent claimer could "win" and duplicate the claim
    # the "rename is the lock" premise is Windows-invalid.
    # Holding board_lock makes the rename effectively exclusive everywhere: once
    # the winner moves the ticket out of open/, every serialized loser's
    # load_ticket/move_ticket hits the now-missing path → FileNotFoundError. The
    # two race windows (the loser's `load_ticket(path)` read or its
    # `move_ticket(path)` rename) both mean the same thing — we lost the claim
    # race — so both surface as one clean "claim race lost" (rc=1), never an
    # unhandled traceback. board_lock is OS-flock
    # based, so a crash mid critical-section auto-releases the lock (no stale
    # lock). Note: a dependency's own FileNotFoundError is caught below and is a
    # *normal* rejection ("dependency not found"), distinct from a claim race.
    try:
        with board_lock():
            fm, body = load_ticket(path)
            # Check dependencies
            for dep in fm.get("depends_on") or []:
                try:
                    dep_status, _ = find_ticket(dep)
                except FileNotFoundError:
                    print(f"dependency {dep} not found", file=sys.stderr)
                    return 1
                if dep_status != "done":
                    print(f"dependency {dep} is {dep_status}/, not done",
                          file=sys.stderr)
                    return 1

            # 롤백 복원 기준 = 이동 *전* 원본 바이트
            # frontmatter 갱신(claimed_by/claimed_at)을 되돌릴 원본을 우리가 들고 있어야 한다.
            original_bytes = path.read_bytes()
            # board_lock (not the bare os.rename) is now the exclusive gate.
            new_path = move_ticket(path, "claimed")
    except FileNotFoundError:
        print(f"claim race lost on {args.id}", file=sys.stderr)
        return 1

    fm["status"] = "claimed"
    fm["claimed_by"] = assignee
    fm["claimed_at"] = now_utc()
    dump_ticket(new_path, fm, body)

    # claim STRICT 3·4단계: 로컬 claim 을 **그 티켓 경로만** commit 하고
    # push 가 성공해야 *비로소* 소유 확정. push 실패(non-FF/거부)면 `reset --soft <anchor>` +
    # 티켓 파일 역이동·원본 복원으로 **이 claim 이 만진 것만** 되돌리고(공유 트리의 무관한
    # 미커밋 작업은 불변) race-lost 로 명시 실패한다 — 거짓 소유를 남기지 않는다. board 가 별도
    # git 이 아니면 confirm 은 True(로컬 rename 만으로 확정·legacy 무변경).
    claim_files = _ClaimFiles(old=path, new=new_path, original=original_bytes)
    if not _board_git_claim_confirm(orig_head, claim_files):
        print(f"claim race lost on {args.id} (board push 충돌·소유 미확정·롤백됨)",
              file=sys.stderr)
        refresh_board()
        return 1
    print(f"claimed {args.id} as {assignee}")
    refresh_board()
    return 0


def _complete_gate(tid: str, args: argparse.Namespace) -> list[str]:
    """Verify completion housekeeping before a ticket may move to done/.

    Returns a list of *blocking* problems (empty = gate passes). Non-blocking
    concerns are printed to stderr as warnings from here.

    The regression check trusts the caller's `--tests-pass` assertion rather
    than re-running the (slow) suite.
    """
    problems: list[str] = []
    id_re = re.compile(rf"\b{re.escape(tid)}\b")

    # 1. log/current.md must contain an entry for this ticket.
    if not args.allow_missing_log:
        log_text = LOG_FILE.read_text(encoding="utf-8") if LOG_FILE.exists() else ""
        if not id_re.search(log_text):
            problems.append(
                f"no log/current.md entry mentions {tid} — append one to "
                f"{_rel_to_repo(LOG_FILE)} (or pass --allow-missing-log)")

    # 2. regression must be confirmed by the implementing session.
    if not (args.tests_pass or args.allow_untested):
        problems.append(
            "regression not confirmed — run `pytest tests/ -q`, then pass "
            "--tests-pass (or --allow-untested for a regression-irrelevant "
            "ticket)")

    return problems


def cmd_complete(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "claimed":
        print(f"cannot complete {args.id}: in {status}/, must be claimed",
              file=sys.stderr)
        return 1

    # Sync gate — refuse to mark done until housekeeping is verified.
    problems = _complete_gate(args.id, args)
    if problems:
        print(f"cannot complete {args.id}: sync gate failed —", file=sys.stderr)
        for msg in problems:
            print(f"  ✗ {msg}", file=sys.stderr)
        return 1

    fm, body = load_ticket(path)
    new_path = move_ticket(path, "done")
    fm["status"] = "done"
    fm["completed_at"] = now_utc()
    dump_ticket(new_path, fm, body)
    refresh_board()
    # 스코프 = 이 mutation 이 만진 두 경로만 — 공유 board 의 무관한 미커밋 작업이
    # 이 커밋에 실려 push 되지 않는다. 이하 best-effort 5곳 동일.
    ready = _board_git_sync_best_effort(f"complete {args.id}", (path, new_path))
    print(f"completed {args.id}{_board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status not in ("open", "claimed"):
        print(f"cannot block from {status}/", file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_ticket(path, "blocked")
    fm["status"] = "blocked"
    note = f"\n## Blocked\n{args.reason} — {datetime.date.today().isoformat()}\n"
    dump_ticket(new_path, fm, body + note)
    refresh_board()
    ready = _board_git_sync_best_effort(f"block {args.id}", (path, new_path))
    print(f"blocked {args.id}: {args.reason}{_board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_unclaim(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "claimed":
        print(f"cannot unclaim {args.id}: in {status}/", file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_ticket(path, "open")
    fm["status"] = "open"
    fm["claimed_by"] = None
    fm["claimed_at"] = None
    dump_ticket(new_path, fm, body)
    refresh_board()
    ready = _board_git_sync_best_effort(f"unclaim {args.id}", (path, new_path))
    print(f"unclaimed {args.id}{_board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "blocked":
        print(f"cannot unblock {args.id}: in {status}/, must be blocked",
              file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_ticket(path, "open")
    fm["status"] = "open"
    dump_ticket(new_path, fm, body)
    refresh_board()
    ready = _board_git_sync_best_effort(f"unblock {args.id}", (path, new_path))
    print(f"unblocked {args.id}{_board_git_mutation_state_suffix(ready)}")
    return 0


INIT_GUIDE = """\
─ init 완료 — 이 clone setup 끝 ({mode}) ─
  3계층: 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf · git-ignored)
  규칙: 내구 진실은 공유 채널에만 · pm_state 는 버려도 되는 로컬 · 공유 파일 직접 난편집 금지
  ID:   `board.py new` 로 {idfmt} 발행
"""

# 추가 리뷰어(additional reviewer) 첫 opt-in 이 원자적으로 심는 기본 프로필.
#   사람이 부르는 역할 이름은 **추가 리뷰어**이고, `external_*` 은 기계 식별자와 외부 전송·격리·
#   과금 축에만 남긴다(기존 키/파일명은 그대로 — 이름만 사람 표면에서 바뀐다).
#   `reviewer_cmd` 는 **신규 온보딩에서 만들지 않는다** — 레거시 키는 이미 쓰는 채택자에게만
#   남고, 새 채택자는 구조적 튜플 하나로 통일한다.
#   같은 값을 pm_update.ADDITIONAL_REVIEWER_DEFAULTS 도 심는다(두 온보딩 진입·동일 프로필).
#   실행 해소(하네스/모델/추론 강도 → 실 명령)는 external_review 가 하고, 여기서는 값만
#   시드한다 — 무거운 실행 코어를 board 로 끌어오지 않는다. 드리프트는 테스트가 잡는다.
ADDITIONAL_REVIEWER_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("external_review_enabled", "true"),
    ("additional_reviewer.harness", "codex"),
    ("additional_reviewer.model", "gpt-5.6-sol"),
    ("additional_reviewer.reasoning", "max"),
)

# opt-in "예" 가 append 하는 블록. external_review_enabled=true 는 *설정된* 외부 전송과 통상
#   과금에 대한 **지속 동의**라, 이후 리뷰마다·라운드 상한마다 비용을 다시 묻지 않는다.
ADDITIONAL_REVIEWER_OPTIN_BLOCK = (
    "# 추가 리뷰어(additional reviewer) — ON.\n"
    "# external_review_enabled=true 는 설정된 외부 전송과 통상 과금에 대한 지속 동의다\n"
    "# (리뷰마다·라운드 상한마다 비용을 다시 묻지 않는다). 프로필은 아래 3키로 교체한다.\n"
    + "".join(f"{key}={value}\n" for key, value in ADDITIONAL_REVIEWER_DEFAULTS)
)

# 나중에 켜는 법 — 비대화형/거절 경로가 같은 문장을 쓴다(안내 단일 진실).
ADDITIONAL_REVIEWER_ENABLE_HINT = (
    "local.conf 에 external_review_enabled=true + "
    "additional_reviewer.harness/model/reasoning"
)


def _is_noninteractive() -> bool:
    """`PM_NONINTERACTIVE` env 가 truthy 면 True — 비대화 결정 신호.

    Windows 서 DEVNULL stdin 의 `isatty()` 가 신뢰불가라
    pm_import 가 board init 을 비대화로 부를 때 env 로 결정적 신호를 준다. truthy 판정은
    `"1"`/`"true"`/`"yes"`/`"on"`(대소문자 무관) — 빈/`"0"`/`"false"` 등은 미설정 취급(폴백).
    """
    return os.environ.get("PM_NONINTERACTIVE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def prompt_external_review_optin() -> None:
    """추가 리뷰어(additional reviewer) opt-in 프롬프트 → local.conf 에 기록.

    코드 diff 가 외부로 *전송*되므로 기본 거부. **첫 1회만** 묻는다 — 이미 결정돼 있거나
    (true/false 무관) 비대화형(파이프·CI)이면 묻지 않고 안전쪽(OFF 유지). 선택은 어느 쪽이든
    기록해 다음 init/update 때 다시 묻지 않는다.

    "예" 는 ADDITIONAL_REVIEWER_DEFAULTS 4키를 **원자적으로** 심는다(활성 플래그 + 하네스·
    모델·추론 강도). 레거시 `reviewer_cmd` 는 만들지 않는다. 이미 결정된 conf 는 구조적 튜플이든
    레거시 `reviewer_cmd` 든 값을 건드리지 않는다(자동 마이그레이션 없음).
    """
    if "external_review_enabled" in local_config():
        return  # 이미 결정됨 (true/false 무관·기존 프로필/레거시 키 불변)
    # 명시적 비대화 신호 우선: Windows DEVNULL 의 isatty() 신뢰불가 함정 회피.
    # PM_NONINTERACTIVE truthy 면 묻지 않고 안전쪽(OFF 유지). isatty 는 보조 폴백(env 없을 때).
    if _is_noninteractive() or not sys.stdin.isatty():
        print(f"  (비대화형 — 추가 리뷰어 OFF 유지. 켜려면 {ADDITIONAL_REVIEWER_ENABLE_HINT})")
        return
    print("\n추가 리뷰어(additional reviewer·external_review)를 켤까요? 코드 diff 가 설정된 "
          "리뷰 하네스로 *전송*되고 그 하네스에 *과금*됩니다 — 내부 code-reviewer 와 상보적입니다.")
    print("  예 = 기본 프로필(codex · gpt-5.6-sol · reasoning max)을 한 번에 기록합니다 "
          "— 이후 리뷰마다 비용을 다시 묻지 않습니다(local.conf 에서 교체 가능).")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        # stdin 이 EOF (비대화·파이프 종료) — 비대화 가드와 동일 동작: 결정 미기록,
        # 아무것도 쓰지 않고 반환. 기존 local.conf 의 결정을 덮어쓰지 않는다(preservation).
        return
    _ensure_trailing_newline(LOCAL_CONF)  # 개행 없는 conf 에 바로 append 시 기존 값 손상 방지
    with LOCAL_CONF.open("a", encoding="utf-8") as f:
        if answer in ("y", "yes"):
            f.write(ADDITIONAL_REVIEWER_OPTIN_BLOCK)
            print("  ✓ 추가 리뷰어 ON (codex · gpt-5.6-sol · reasoning max — "
                  "local.conf additional_reviewer.* 로 교체 가능)")
        else:
            f.write("# 추가 리뷰어 — 기본 OFF. 켜려면 true 로.\n"
                    "external_review_enabled=false\n")
            print(f"  → 추가 리뷰어 OFF (나중에 {ADDITIONAL_REVIEWER_ENABLE_HINT} 로 켤 수 있음).")


def prompt_delegate_optin() -> None:
    """cross-harness 위임(pm_delegate) opt-in 프롬프트 → local.conf 에 delegate_enabled **실키** 기록
    (external_review opt-in 동형).

    위임 프롬프트/코드가 외부 하네스로 *전송*되고 그 하네스에 *과금*되므로 기본 OFF. **실키**
    delegate_enabled(주석 예시가 아니라 local_config 가 파싱하는 활성 키)가 이미 있으면 결정됨 →
    no-op. 비대화형(파이프·CI)이면 묻지 않고 기본 OFF 유지(주석 스키마 시드가 도입을 안내). TTY 면
    1회 질문 — y=true·그 외/무입력=false 실키를 기록해 다음 init/update 때 다시 묻지 않는다(멱등).
    주석 예시는 local_config 파싱에서 제외되므로 주석만 있고 실키가 없으면 '미결정' 으로 본다."""
    if "delegate_enabled" in local_config():
        return  # 실키로 이미 결정됨(주석 예시는 미포함 — 미결정 취급)
    # 명시적 비대화 신호 우선(Windows DEVNULL isatty 함정 회피·external_review optin 동형).
    if _is_noninteractive() or not sys.stdin.isatty():
        print("  (비대화형 — cross-harness 위임 OFF 유지. 켜려면 local.conf delegate_enabled=true)")
        return
    print("\ncross-harness 위임(pm_delegate)을 켤까요? 켜면 위임 프롬프트/코드가 외부 하네스로 "
          "*전송*되고 그 하네스에 *과금*됩니다 (역할 노동을 다른 하네스 CLI 로 위임).")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        # stdin EOF(Ctrl-D) = 기본 거절 → false 실키를 **기록**(매번 재질문 방지·opt-in 결정 박제).
        answer = ""
    _ensure_trailing_newline(LOCAL_CONF)  # 개행 없는 conf 에 바로 append 시 기존 값 손상 방지
    with LOCAL_CONF.open("a", encoding="utf-8") as f:
        if answer in ("y", "yes"):
            f.write("# cross-harness 위임 — ON.\ndelegate_enabled=true\n")
            print("  ✓ cross-harness 위임 ON (delegate_enabled=true·외부 송신·과금 수용). "
                  "역할 매핑은 local.conf delegate.<role>.* 주석 예시 참조.")
        else:
            f.write("# cross-harness 위임 — 기본 OFF. 켜려면 true 로.\n"
                    "delegate_enabled=false\n")
            print("  → cross-harness 위임 OFF (나중에 local.conf delegate_enabled=true 로 켤 수 있음).")


def cmd_init(args: argparse.Namespace) -> int:
    """clone 당 1회 setup. --prefix 있으면 multi-repo 네임스페이스, 없으면 solo (N=1·M=1).

    multi-PM = N 세션 × M repo 한 개념— *수가 1이냐 더냐*의 문제다.
    `--prefix` 는 협업(다중-사람)용이 아니라 **M>1 repo 의 ID 네임스페이스** — 같은
    single user 가 여러 repo 를 동시에 도는 multi-PM 셋업에서 ID 충돌을 막는다.

    공통: local.conf + pm_state(template) + pre-push 회귀 훅.
    namespaced(--prefix): areas.md 레지스트리 등록 + prefix(→ T-PREFIX-NNN·multi-repo 가드 활성).
    solo (N=1·M=1): areas.md 안 만듦 → 가드 off → legacy T-NNNN (오버헤드 0).
    """
    _reject_task_slot_identity_mix(args)
    prefix = args.prefix
    # 명시 --prefix sanity (예약어 `none`·형식 [a-z0-9_]+) — 위반이면 areas 등록·
    # local.conf write 어떤 부작용보다 앞에서 부작용 0 으로 거부(cmd_init 첫 문장 뒤).
    if prefix:
        reason = _validate_prefix(prefix)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
    namespaced = bool(prefix)  # prefix 있음 = multi-repo 네임스페이스 모드(협업 아님)
    if namespaced:
        registered = registered_prefixes()
        # case-only 근접중복 검출은 등록 ∪ **티켓** prefix (`_validate_dst_prefix` 와 대칭) —
        # 미등록 `T-aaa-*` 티켓이 있는데 `init --prefix AAA` 하는 case-불일치도 fail-loud 로 안내한다.
        existing = registered | _existing_ticket_prefixes()
        if prefix in registered:
            print(f"prefix {prefix!r} 이미 등록됨 (areas.md) — local.conf 만 갱신.")
        elif (conflict := _case_only_conflict(prefix, existing)) is not None:
            # case-only 중복 거부 — 이미 있는 `AAA` 와 대소문자만 다른 `aaa` 등록은
            # 레지스트리/티켓과 fold-충돌한다(네임스페이스 분할). 기존 canonical case 로 안내.
            print(f"[중단] prefix {prefix!r} 은 기존 {conflict!r} 과 대소문자만 다르다 "
                  f"(prefix 동일성은 case-insensitive). 기존 case {conflict!r} 를 "
                  "그대로 쓰라 (areas 미변경·부작용 0).", file=sys.stderr)
            return 1
        else:
            if not args.area:
                print(f"새 prefix {prefix!r} 등록엔 --area <설명> 필요.", file=sys.stderr)
                return 1
            # owner = areas.md 등록 식별자(registrant) — 협업 소유자(다중-사람)가 아니라
            # single user 의 등록 출처 표식이다. 기본 = 현 세션.
            # 등록 owner 기본값 = 귀속 쓰기 — 세션 미해소면 fail-loud
            # (`--owner`/`--repo`/`--slot` 명시 유도). --owner 명시면 session_name 미호출(short-circuit).
            owner = args.owner or _actor_session_override(args) or session_name(required=True)
            # area_owner = 그 area 의 *user* 소유(`--mine` 풀 입력) —
            # registrant `owner`(슬롯/세션)와 별개 칼럼(overload 금지).
            # cmd_repo_add 와 동형 해소: `--user` 명시 > local.conf user= > git config
            # user.email > None(빈 칼럼·_repo_area_owner None 폴백·현행 `--mine` 미포함).
            area_owner = user_name(getattr(args, "user", None))
            areas_append(prefix, args.area, owner, area_owner=area_owner)
            ao_surface = area_owner if area_owner else "(미상 — local.conf user= / git user.email 미설정)"
            print(f"✓ areas.md 등록: {prefix} | {args.area} | owner={owner} | area_owner={ao_surface}")
    # session=/prefix= write 는 **solo 형상 전용 legacy** — leased ≥2 인
    # multi 홈은 이 키를 무시하고 세션/prefix 를 lease 장부에서 유도한다(session_name·
    # id_prefix). solo 채택자 폴백(후방호환)을 위해 write 는 유지하되, multi 홈은 흡수 후
    # 이 키를 제거해도 동작 동일(위생).
    # --repo/--slot로 명시하면 "<repo>_<N>" 으로 완전 해소(리스 조회 불요·kind=slot).
    # --repo 단독이면 그 repo 의 활성 리스가 1개일 때만 해소(0개/무인자 → None → 아래 default).
    override = _actor_session_override(args)
    sess = override or (f"{prefix.lower()}-pm" if namespaced else "pm")
    if not LOCAL_CONF.exists():
        # 부재 시(첫 생성) — 현행 그대로 전체 default conf write. 회귀 0.
        conf = "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
        if namespaced:
            conf += f"prefix={prefix}\n"  # solo-legacy — multi 홈은 areas.md 유도
        conf += (f"session={sess}\n"  # solo-legacy — multi 홈은 lease 유도
                 "# 엔진 문서 operational placeholder 해소값 ({{PY}}·{{TEST_CMD}}·{{PROJECT_NAME}}):\n"
                 f"py={_detect_py()}\ntest_cmd=pytest -q\nproject_name=\n"
                 "# ctx 정지-핸드오프 임계 (어댑터 훅이 잔여 컨텍스트 %로 판정):\n"
                 f"ctx_nudge_pct={CTX_NUDGE_PCT_DEFAULT}\nctx_stop_pct={CTX_STOP_PCT_DEFAULT}\n"
                 "# ctx_window_tokens: 핸드오프 토큰 예산(위 nudge/stop %의 기준). 큰 window(1M)\n"
                 "# 모델이라도 낮게 두면 이른 핸드오프 = 토큰 경제(큰 컨텍스트가 매 턴 소모 가속).\n"
                 "# 올리면 세션당 더 길게. 물리 window 아님 — 사용자 비용/맥락 선택.\n"
                 f"ctx_window_tokens={CTX_WINDOW_TOKENS_DEFAULT}\n"
                 "# 하네스별 오버라이드(옵션·주석 해제 시 활성): 한 repo 를 claude·opencode 동시\n"
                 "# 운용 시 하네스별 예산 분리(ctx_window_tokens_<harness> > generic > 200K·\n"
                 "# claude/opencode 독립). 미설정 시 위 generic 값이 분모.\n"
                 "# ctx_window_tokens_claude=500000\n"
                 "# ctx_window_tokens_opencode=200000\n"
                 "# ctx_window_tokens_codex=200000\n"
                 + _DELEGATE_CONF_SEED + _HARNESS_BUDGET_CONF_SEED)
        LOCAL_CONF.write_text(conf, encoding="utf-8")
        surface_sess = sess
    else:
        # 존재 시 — 비파괴 병합. init 이 안 쓰는 사용자/operational 키
        # (external_review_enabled·additional_reviewer.*·레거시 reviewer_cmd·upstream·
        # upstream_rev·opencode_pro_model·status_total_style·user 등)를 절대 삭제/변경하지
        # 않는다. 통째 write 금지.
        text = LOCAL_CONF.read_text(encoding="utf-8")
        existing = local_config()
        updates: dict[str, str] = {}
        # init 기본키는 *없을 때만* 추가 — 기존 값(커스텀 ctx_window_tokens 등)은 보존.
        defaults = {
            "py": _detect_py(),
            "test_cmd": "pytest -q",
            "project_name": "",
            "ctx_nudge_pct": str(CTX_NUDGE_PCT_DEFAULT),
            "ctx_stop_pct": str(CTX_STOP_PCT_DEFAULT),
            "ctx_window_tokens": str(CTX_WINDOW_TOKENS_DEFAULT),
        }
        for key, value in defaults.items():
            if key not in existing:
                updates[key] = value
        # session·prefix 는 명시 인자일 때만 set-or-replace(재등록 UX 보존). 인자 없으면
        # 기존 session 보존 — 없으면 default(`pm`/`<prefix>-pm`)로 표면화만. (둘 다 solo-legacy·
        # multi 홈은 유도로 무시.
        if override:
            updates["session"] = override
        if namespaced:
            updates["prefix"] = prefix
        merged = _set_conf_keys(text, updates)
        # trailing newline 보장 — updates 가 비어(default 키 전부 존재) `_set_conf_keys` 가
        # 원문을 그대로 반환하고 그 원문이 개행 없이 끝나면, 뒤이은 prompt_external_review_optin()
        # 의 append 가 마지막 키에 그대로 붙어 기존 키를 변질시킨다(codex must-fix·병합 경로 회귀).
        if merged and not merged.endswith("\n"):
            merged += "\n"
        # 기존 adopter 보완: delegate 스키마 시드는 fresh 생성 branch 에만 있으므로
        # this-change 이전 local.conf 를 가진 채택자는 재실행에도 스키마를 못 받는다. **스키마 블록
        # 마커**가 없으면 시드를 파일 끝에 append 한다 — 기존 byte 는 위에서 보존(비파괴 병합). 마커로
        # 판정하므로 실키 결정(delegate_enabled)과 독립이다(스키마 문서 = 별개 축). 이미 있으면 no-op
        # → 재실행 멱등(fresh conf 는 이미 포함하므로 자연 no-op). 실키 opt-in 은 prompt_delegate_optin.
        if _DELEGATE_SEED_MARKER not in merged:
            merged += _DELEGATE_CONF_SEED
        # 하네스별 시간 예산 스키마도 같은 규칙으로 append(별도 마커 — 위 블록을 이미 받은
        # 기존 채택자에게도 도달한다). 전부 주석이라 기존 동작/값은 불변.
        if _HARNESS_BUDGET_SEED_MARKER not in merged:
            merged += _HARNESS_BUDGET_CONF_SEED
        LOCAL_CONF.write_text(merged, encoding="utf-8")
        surface_sess = existing.get("session") or sess
        if override:
            surface_sess = override
    print(f"✓ local.conf: {('prefix=' + prefix + ' · ') if namespaced else ''}session={surface_sess}")
    if not PM_STATE_FILE.exists() and PM_STATE_TEMPLATE.exists():
        # `{{DATE}}` 의 소유자는 **생성 시점**이다 — 템플릿은 채택자 디스크에 토큰-form
        #   으로 남아야 pm_update 의 manifest byte-copy 와 진동하지 않는다. 그러니 그 템플릿으로
        #   산출물을 만드는 이 지점이 오늘 날짜를 채운다(worktree_pool.ensure_task_pm_state 의
        #   task pm_state 렌더와 같은 규칙·소유 선언은 pm_import.CONSUMPTION_TIME_TOKENS).
        seed = PM_STATE_TEMPLATE.read_text(encoding="utf-8").replace(
            "{{DATE}}", datetime.date.today().isoformat())
        PM_STATE_FILE.write_text(seed, encoding="utf-8")
        print(f"✓ pm_state.md 생성 ({_rel_to_repo(PM_STATE_TEMPLATE)} 에서)")
    if install_pre_push_hook():
        print("✓ pre-push 회귀 게이트 훅 설치 (green 회귀만 push)")
    # board submodule 이 분리된 형상이면 ignore=all 자동 설정 — design(코드) git 이
    # board PM-commit 으로 오염되지 않게(누출 0). 솔로/미분리/git 부재면 no-op(fail-soft·무영향).
    if _configure_board_submodule():
        print("✓ board submodule ignore=all 설정 (코드 git 누출 0)")
    # areas.md `merge=union` 배포는 여기서 하지 않는다 — init 은 board git 에 commit 하지
    # 않으므로 파일만 쓰면 board 가 dirty(`?? .gitattributes`)로 남아 다음 `claim` 이 STRICT dirty
    # 가드에 막힌다(엔진이 만든 파일을 사용자 편집으로 오인·clone→init→claim 온보딩 직격). 배포는
    # `_board_git_stage_and_commit`(write→stage→commit 이 한 호출에 닫힘) **단일 채널**로 한다.
    prompt_external_review_optin()
    prompt_delegate_optin()  # cross-harness 위임 opt-in(TTY 1회 질문·실키 기록)
    mode = f"multi-repo · {prefix}" if namespaced else "solo (N=1·M=1)"
    idfmt = f"T-{prefix}-NNN" if namespaced else "T-NNNN (legacy)"
    print(INIT_GUIDE.format(mode=mode, idfmt=idfmt))
    return 0


# ── identity backfill 마이그레이션 ────────
# 이전 데이터(areas `area_owner` 부재·ticket `created_by` 부재·`claimed_by` 슬롯-only)를
# *일회성* backfill 해 `--mine`·provenance 가 기존 보드에서 동작하게 한다. graceful-null 우회를
# 정합 데이터로 대체하는 정식 업그레이드 경로.
#   - 멱등: 빈/부재 필드만 채운다. 기존 non-empty 값은 절대 덮어쓰지 않는다(재실행 no-op).
#   - 비파괴: frontmatter 키 순서·body 보존(dump_ticket 가 sort_keys=False)·areas.md 표/주석 보존.
#   - 대상: areas 빈 area_owner → user · 티켓 부재 created_by → user · 슬롯-only claimed_by
#     (`/` 없음·non-empty) → `<user>/<slot>`(기존 슬롯값을 slot 으로 보존·user 차원만 prepend).


def _migrate_areas_text(text: str, user: str) -> tuple[str, list[str]]:
    """areas.md 텍스트에서 빈 `area_owner` 셀을 user 로 채운 (새 텍스트, per-row 요약) 반환.

    표/주석/빈 줄은 verbatim 보존하고 *데이터 행의 area_owner 셀만* 채운다(비파괴·멱등).
    헤더(첫 table row)에서 `area_owner` 칼럼 인덱스를 찾는다 — 이미 채워진 행은 건드리지 않는다.

    **구-헤더 업그레이드**: areas.md 는 `area_owner` 칼럼
    자체가 없는 구 스키마다(`repo|prefix|git|test_cmd|owner`[5]·`…|base`[6]·`…|protected`[7] 또는
    멀티-clone `prefix|area|owner`[3] variant). 헤더에 area_owner 칼럼이 없으면 단순히 채울 자리가
    없어 no-op 이 되어버리면 migrate 의 본래 목적(구 데이터를 `--mine` 가능하게)이 가장 구형
    스키마에서 작동하지 않는다. 그래서 area_owner 칼럼이 없는 구 헤더를 만나면 **canonical 8칼럼
    헤더(`_areas_header_line`·`_AREAS_COLUMNS` 단일 진실)로 업그레이드**한다 — 헤더 행 교체 +
    바로 뒤 구분선(`|---|`) 행을 canonical 폭으로 교체 + 각 데이터 행에 area_owner 칼럼(빈값 →
    user) 추가. 기존 칼럼·값·정렬·표 밖 텍스트(주석)는 보존하고 area_owner 만 *append* 한다
    (비파괴). 멱등: 이미 area_owner 칼럼이 있으면 헤더 업그레이드 없이 빈 셀만 채운다.

    셀 수가 헤더보다 많은 wider row(비-canonical 구 헤더 아래에 canonical 8칼럼 row 가 append 된
    경우 포함)는 `upgrade` 여부와 무관하게 `_AREAS_COLUMNS` 의 area_owner 인덱스(7)로 area_owner
    셀을 찾는다 — `_parse_areas` 가 wider row 를 헤더 무시하고 canonical 순서로 매핑하는 것과
    정확히 동형(must-fix). 그렇지 않으면 헤더 폭으로 읽다 index 3(`test_cmd`)을 area_owner 로
    오인해 backfill 을 놓친다.

    인덱스 해소·구-헤더 업그레이드 계획·wider-row 보정은 **공용 헬퍼**(`_areas_column_index`·
    `_areas_row_cell_index`)에 있고 `areas_set_cell`(범용 셀 변경)과 공유한다 — 두 벌로 갈라지면
    칼럼 오매핑.
    """
    lines = text.splitlines(keepends=True)
    header_cells: list[str] | None = None
    ao_idx: int | None = None
    # 구-헤더 업그레이드 모드: 헤더에 area_owner 칼럼이 없을 때 켜진다. 켜지면 헤더 행을
    # 갈아끼우고, 바로 뒤 구분선 1개를 같은 폭으로 교체하고, 데이터 행마다 area_owner 칼럼을
    # append 한다. ao_idx 는 새 area_owner 칼럼 위치로 고정된다.
    upgrade = False
    sep_cols = 0     # 업그레이드 후 구분선이 가질 칼럼 수(canonical 8 또는 구 헤더+1).
    sep_replaced = False  # 헤더 직후 구분선 1회만 교체(이후 구분선은 verbatim).
    canonical_ao = _AREAS_COLUMNS.index("area_owner")
    changes: list[str] = []
    out: list[str] = []
    for line in lines:
        # 줄바꿈을 떼어 셀을 검사하고, 재조립 시 원래 종결자를 복원한다(비파괴).
        nl = ""
        body = line
        if line.endswith("\r\n"):
            nl, body = "\r\n", line[:-2]
        elif line.endswith("\n"):
            nl, body = "\n", line[:-1]
        cells = _split_areas_row(body)
        if cells is None:
            # 비-table 줄(주석·빈 줄·구분선)은 기본 verbatim. 단 업그레이드 모드에서 헤더
            # 직후 첫 구분선(`|---|`)은 새 칼럼 수에 맞춰 교체한다(헤더 폭과 정합).
            if upgrade and not sep_replaced and _AREAS_SEP_RE.match(body.strip()):
                out.append("|" + "|".join("---" for _ in range(sep_cols)) + "|" + nl)
                sep_replaced = True
            else:
                out.append(line)
            continue
        if header_cells is None:
            header_cells = [c.lower() for c in cells]
            # 공용 헬퍼 — 칼럼이 있으면 그 인덱스(헤더 verbatim), 없으면 구-헤더 업그레이드
            # 계획(canonical 8칼럼 교체 또는 기존 헤더 끝 append)을 함께 돌려준다.
            ao_idx, new_header, sep_cols = _areas_column_index(header_cells, "area_owner")
            if new_header is None:
                out.append(line)  # 이미 신 스키마 헤더 — verbatim.
            else:
                upgrade = True
                out.append(new_header + nl)
            continue
        # 헤더보다 넓은 row 는 `upgrade` 여부와 무관하게 canonical area_owner 인덱스(7)로
        # 매핑한다(`_areas_row_cell_index` 공용 보정) — `_parse_areas` 가 wider row 를 헤더
        # 무시하고 `_AREAS_COLUMNS` 순서로 매핑하는 것과 정확히 동형이다. 비-canonical 구
        # 헤더(예 멀티-clone `prefix|area|owner`[3]) 아래에 canonical 8칼럼 row 가 append 된
        # 케이스에서 `ao_idx`(=헤더 폭) 로 읽으면 index 3(`test_cmd`)을 area_owner 로 오인해
        # backfill 못 한다(must-fix·_parse_areas 정합).
        idx = _areas_row_cell_index(
            header_cells, cells,
            ao_idx if ao_idx is not None else canonical_ao, "area_owner")
        prefix = cells[1] if len(cells) > 1 else "?"
        cur = cells[idx] if idx < len(cells) else ""
        if cur.strip():
            out.append(line)  # 이미 채워짐 — 멱등(보존).
            continue
        # 빈 셀 채움. 셀이 모자라면 빈 칸으로 패딩해 인덱스를 확보(비파괴 append).
        while len(cells) <= idx:
            cells.append("")
        cells[idx] = user
        out.append("| " + " | ".join(cells) + " |" + nl)
        changes.append(f"area {prefix}: area_owner → {user}")
    return "".join(out), changes


def _migrate_ticket_fm(fm: dict, user: str, slot: str) -> list[str]:
    """티켓 frontmatter 를 *제자리* backfill 하고 per-field 변경 요약을 반환(빈 = no-op).

    멱등·비파괴: 부재/빈 `created_by` 만 user 로, 슬롯-only(`/` 없음·non-empty) `claimed_by`
    만 `<user>/<slot>` 로 채운다. 기존 non-empty 값(이미 `<user>/<slot>` 형태 포함)은 불변.
    키 순서는 dict 제자리 수정이라 보존(없던 created_by 추가는 끝에 붙음 → dump 순서 유지).
    """
    changes: list[str] = []
    created_by = fm.get("created_by")
    # 부재(키 없음·None)거나 빈/공백 문자열이면 backfill. 기존 non-empty 값은 불변(멱등).
    if not (str(created_by).strip() if created_by is not None else ""):
        fm["created_by"] = user
        changes.append(f"created_by → {user}")
    cb = fm.get("claimed_by")
    if isinstance(cb, str) and cb.strip() and "/" not in cb:
        # 슬롯-only(구 형태·user 차원 없음) → user 차원 prepend(슬롯값 보존).
        fm["claimed_by"] = f"{user}/{cb}"
        changes.append(f"claimed_by {cb} → {user}/{cb}")
    return changes


def _migrate_identity_preview(
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """--dry-run 경로: read-only 스캔 + per-file 미리보기. 락·쓰기 0.

    어떤 파일도 옮기거나 쓰지 않으므로 board_lock 을 *전혀* 잡지 않는다(read-only 보장).
    반환 `(total, wrote)` 에서 wrote 는 항상 False(쓰기 없음 → refresh_board 미호출).
    """
    total = 0
    # areas.md 미리보기(읽기 전용).
    af = areas_file()
    if af.exists():
        text = af.read_text(encoding="utf-8")
        _, area_changes = _migrate_areas_text(text, user)
        for c in area_changes:
            print(f"  areas.md: {c}")
        total += len(area_changes)
    # 티켓 미리보기 — glob 스캔 후 변경 산출만(쓰기 없음).
    for status in statuses:
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _body = load_ticket(p)
            changes = _migrate_ticket_fm(fm, user, slot)
            if not changes:
                continue
            tid = fm.get("id") or p.stem
            total += len(changes)
            for c in changes:
                print(f"  {tid} ({status}/): {c}")
    return total, False


def _migrate_areas_apply(user: str) -> tuple[int, bool]:
    """areas.md 의 빈 area_owner backfill (read→transform→write)을 board_lock 으로 보호.

    areas.md 는 `areas_append`가 *진짜* 공유 mutation 으로
    board_lock 을 잡고 쓰는 단일 파일이라, 본 RMW 의 write 도 같은 락으로 직렬화해야
    동시 repo-add 의 lost-update(전체 write_text 가 append 된 row 를 클로버)를 막는다.
    이 락은 areas 구간 *한정* — 티켓 backfill 은 별개(아래 best-effort).

    **재진입 금지**: board_lock 은 OS flock(non-reentrant). 락 안에서 부르는 IO
    (`_migrate_areas_text`·AREAS_FILE read/write)는 락을 다시 잡지 않는다. 반환 `(total, wrote)`.
    """
    af = areas_file()
    if not af.exists():
        return 0, False
    total = 0
    wrote = False
    with board_lock():
        text = af.read_text(encoding="utf-8")
        new_text, area_changes = _migrate_areas_text(text, user)
        for c in area_changes:
            print(f"  areas.md: {c}")
        if area_changes:
            total += len(area_changes)
            if new_text != text:
                af.write_text(new_text, encoding="utf-8")
                wrote = True
    return total, wrote


def _migrate_tickets_apply(
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """티켓 backfill — **best-effort**(하드 보장 아님). 글로벌 board_lock 을 잡지 않는다.

    티켓 이동(`cmd_claim`·`cmd_complete`·`cmd_block`·`cmd_unclaim`)은 *설계상* board_lock
    을 안 타고 lock-free atomic-rename(`move_ticket`)만 쓴다. 따라서 migration 이
    board_lock 을 쥐어도 티켓 이동을 막지 못한다 — 락은 거짓 안전(차단만 유발)이라 안 잡는다.
    일회성 backfill 도구를 위해 claim/complete 같은 코어 hot-path 를 락-직렬화로 재설계하는
    것은 과설계다(PM 결정). 대신 정직한 best-effort 로 착지한다:

      1. glob 으로 후보 ID 를 스캔한다(스냅샷·경로는 stale 될 수 있다).
      2. 각 티켓을 *쓰기 직전* ID 로 현재 경로를 **재조회**(`find_ticket`)한다. 사라졌거나
         스캔 경로와 다르면(다른 세션이 claim/complete 로 이동) **skip + stderr 경고** —
         이동/완료된 티켓에 stale 쓰기를 하지 않는다.
      3. 살아 있으면 현재 경로에 **atomic write**(temp + `os.replace`)로 backfill 한다
         (부분쓰기 0).

    재조회↔replace 사이의 미세 TOCTOU 는 *하드 보장하지 않는다* — migrate-identity 는
    단일-세션 업그레이드 op(조용한 창에서 1회 실행) 전제로 이 잔여 창을 수용한다. 원자성·
    이동-차단을 *주장하지 않는다*. 반환 `(total, wrote)`.
    """
    total = 0
    wrote = False
    for status in statuses:
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            # 스캔 시점 frontmatter 로 변경 산출(읽기). ID 는 frontmatter 에서 얻는다.
            try:
                fm, body = load_ticket(p)
            except FileNotFoundError:
                # 스캔↔load 사이에 이동/완료됨 — best-effort skip.
                print(f"  skip {p.name}: 스캔 후 사라짐(다른 세션이 이동) — backfill 안 함",
                      file=sys.stderr)
                continue
            changes = _migrate_ticket_fm(fm, user, slot)
            if not changes:
                continue
            tid = fm.get("id") or p.stem
            # 쓰기 *직전* ID 로 현재 경로 재조회 — 스캔 경로와 다르거나 사라졌으면 stale
            # 쓰기를 막는다(이동/완료된 티켓에 안 씀). 살아 있으면 현재 경로에 atomic write.
            try:
                cur_status, cur_path = find_ticket(tid)
            except FileNotFoundError:
                print(f"  skip {tid}: 재조회 시 없음(다른 세션이 완료/삭제) — backfill 안 함",
                      file=sys.stderr)
                continue
            if cur_path != p:
                print(f"  skip {tid}: {status}/ → {cur_status}/ 이동됨(쓰기 직전) — "
                      f"stale 쓰기 안 함", file=sys.stderr)
                continue
            total += len(changes)
            for c in changes:
                print(f"  {tid} ({status}/): {c}")
            dump_ticket_atomic(cur_path, fm, body)
            wrote = True
    return total, wrote


def _migrate_identity_apply(
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """비-dry-run 경로: areas(락 보호) + 티켓(best-effort) backfill 을 차례로 수행.

    - **areas.md**: `_migrate_areas_apply` 가 board_lock 으로 RMW 를 보호한다(`areas_append`
      와의 lost-update 방지·진짜 공유 mutation).
    - **티켓**: `_migrate_tickets_apply` 가 **best-effort** 로 backfill 한다(글로벌락 없음 —
      티켓 이동이 락-free atomic-rename 이라 락이 이동을 못 막으므로 거짓 안전을 두지 않는다).
      각 티켓은 쓰기 직전 재조회로 이동/완료 시 skip 한다.

    **재진입 금지**: areas 락 안에서 board_lock 을 다시 잡는 헬퍼는 부르지 않는다.
    board.md 재생성(`refresh_board` — 자체 board_lock)은 **호출자(`cmd_migrate_identity`)가
    락 밖에서 1회** 한다(데드락 방지). 반환 `(total, wrote)`.
    """
    area_total, area_wrote = _migrate_areas_apply(user)
    ticket_total, ticket_wrote = _migrate_tickets_apply(user, slot, statuses)
    return area_total + ticket_total, area_wrote or ticket_wrote


def cmd_migrate_identity(args: argparse.Namespace) -> int:
    """backfill — areas area_owner·ticket created_by/claimed_by.

    `--user` override > `user_name()`(local.conf user= / git config user.email). 미해소(None)면
    abort(식별자 없이는 backfill 불가). `--dry-run` 은 쓰기 0·per-file 미리보기. `--scope`
    active(open+claimed) | all(기본·done 포함). 멱등(빈 필드만)·비파괴(순서/body/표 보존).

    `--repo`/`--slot`은 출력·기본 identity 표시용이며 **backfill 대상 슬롯을 바꾸지
    않는다**. 슬롯-only `claimed_by`(`pm-1` 같은 `/` 없는 값)는 user 차원만 prepend 하고 *기존
    슬롯 토큰을 보존*한다(`pm-1` → `<user>/pm-1`). `--repo`/`--slot` 은 부재 created_by 의
    표시값과 로그 표기에만 쓰이고, 이미 기록된 슬롯 토큰을 자신의 값으로 덮어쓰지 않는다(비파괴).

    **단일-세션 업그레이드 op (동시성 모델)**: migrate-identity 는 *단일-세션* 업그레이드
    op 다. 다른 세션이 claim/complete 로 보드를 변경하는 중엔 실행하지 말 것 — 조용한 창에서
    1회 돌린다. 보드의 티켓 이동(claim/complete/block/unclaim)은 *설계상* board_lock 을 안
    타고 lock-free atomic-rename 만 쓰므로, migration 이 락을 쥐어도 티켓 이동을
    막지 못한다. 따라서:
      - **areas write** 는 board_lock 으로 보호한다(`areas_append` 와의 lost-update 방지 —
        areas 는 진짜 락-보호 공유 mutation).
      - **티켓 backfill** 은 best-effort 다 — 각 티켓을 쓰기 직전 재조회해, 동시에 이동/완료
        됐으면 해당 티켓을 skip(경고)하고 살아 있으면 atomic write 한다. 재조회↔쓰기 사이의
        미세 TOCTOU 는 *하드 보장하지 않는다*(단일-세션 전제로 수용). 원자성·이동-차단을
        주장하지 않는다.
    board.md 재생성은 데드락 방지를 위해 (areas) 락 밖에서 1회 한다.
    """
    _reject_task_slot_identity_mix(args)
    user = user_name(getattr(args, "user", None))
    if not user:
        print("[중단] user 식별자 미해소 — `--user <id>` 를 주거나 local.conf user= / "
              "git config user.email 를 설정하라(식별자 없이는 backfill 불가).",
              file=sys.stderr)
        return 1
    # backfill slot = 귀속 쓰기(claimed_by 에 박음·required=True) — 세션 미해소면
    # fail-loud(None 슬롯으로 backfill 하면 오귀속). 명시 --repo/--slot> env >
    # 단일-lease 유도.
    slot = session_name(_actor_session_override(args), required=True)
    dry_run = bool(getattr(args, "dry_run", False))
    scope = getattr(args, "scope", "all") or "all"
    statuses = ("open", "claimed") if scope == "active" else STATUS_DIRS

    tag = "[dry-run] " if dry_run else ""
    # scope 문구 정합: migrate 는 소유 무관 *전 티켓*을 스캔·backfill 한다
    # (all=done 포함 전체·active=open+claimed). `list --mine`/`--repo`/`--slot`(=현재-사용자 ∩
    # 슬롯 뷰)와 스캔 대상이 다르므로 "전체 스캔" 을 명시해 두 기준이 어긋나 보이는 오독을 없앤다.
    scope_note = "전체 스캔·소유 무관" if scope == "all" else "open+claimed 스캔·소유 무관"
    print(f"{tag}migrate-identity — user={user} · slot={slot} · scope={scope} ({scope_note})")

    if dry_run:
        total, wrote = _migrate_identity_preview(user, slot, statuses)
    else:
        total, wrote = _migrate_identity_apply(user, slot, statuses)

    if total == 0:
        print("  (변경 없음 — 이미 마이그레이션됨이거나 backfill 대상 없음)")
    else:
        verb = "변경 예정" if dry_run else "변경 완료"
        print(f"{tag}{total}건 {verb}.")
    if dry_run:
        print("[dry-run] 쓰기 0 — 적용하려면 --dry-run 없이 재실행.")
    # 파생 board.md 갱신("board.py 변경 명령마다 파생 보드 갱신" 보장·codex sug). migrate 가
    # claimed_by 를 바꾸면 board.md claimed 표시도 달라진다 — 실제 쓰기가 있었고 dry-run 이
    # 아닐 때만 1회 재생성한다(dry-run 은 파생물도 안 건드림·읽기-only 미리보기 보장).
    if wrote:
        refresh_board()
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    _reject_task_slot_identity_mix(args)
    # 명시 --prefix sanity (예약어 `none`·형식 [a-z0-9_]+) — 위반이면 ID 스캔·파일
    # 발행 전에 부작용 0 으로 즉시 거부. 유도/count-based 로 해소된 legacy prefix 는 검증하지
    # 않는다(기존 발행분 존중) — 사용자가 *명시*한 override 만 입력측 sanity 대상이다.
    # task-mode(`--task`)는 정체성 깔때기(`_actor_session_override`)를 지나 **task 명 검증**(무검증
    # created_by 영속 차단)을 받고 세션 override(=task 이름·F5b)를 해소한다 —
    # cmd_new 는 구조상 이 깔때기를 안 지나므로 여기서 명시 소비해 소비 지점 폐쇄에 합류한다. 무-task 는
    # None(기존 created_by 체인 무변경). 부작용(ID 발행·파일 write) 이전이라 불법 task 는 여기서 fail-loud.
    # `--task` → task 명(F5b) · `--repo/--slot`→ `<repo>_<N>` 슬롯 세션 · 무명시 → None.
    # 하나의 세션 override 문자열이지만 축이 둘(task vs slot)이라 아래 분기가 축을 명시 구분한다.
    actor_session = _actor_session_override(args)
    override = getattr(args, "prefix", None)
    if override:
        reason = _validate_prefix(override)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
    elif getattr(args, "task", None):
        # 우선순위: --prefix 명시 > task 설정 prefix > 유도 체인. **`--task` 일 때만** task 설정
        # prefix(`task prefix`·검증·기록)를 조회한다 — slot 세션명(`<repo>_<N>`)을 task_prefix
        # 에 넣던 혼선 제거. 기본 None=무prefix.
        task_pfx = identity_args.task_prefix(actor_session, LEASES_FILE)
        if task_pfx:
            override = task_pfx
    # 해소한 세션(task/slot)을 id_prefix 유도에 thread — 안 넘기면 등록 repo ≥2 환경에서 `new "x"
    # --repo alpha --slot 1` 이 전역 재해소(모호 None)로 prefix 유도 실패해 "prefix 필요" 로 거부된다
    # (codex must-fix·`_test_cmd` L2207 의 `id_prefix(None, session=)` 동형). None(무명시)이면
    # session=None → 기존 전역 session_name() 해소(거동 무변경).
    prefix = id_prefix(override, session=actor_session)
    # multi-repo 네임스페이스 가드는 **레지스트리 *존재*가 아니라 등록 repo *개수*** 기준이다.
    # 등록 prefix 가 ≥2 면 진짜 ID 충돌 가능성이 있으니 prefix 필수(namespace 강제). 등록이
    # ≤1(0=레지스트리 부재/빈·1=단일 self-host) 이면 충돌이 없으므로 solo legacy `T-NNNN` 을
    # 허용한다(prefix optional) — 단일 self-host 가 areas.md 1행만으로 multi-PM 마찰을 떠안지
    # 않게(단일 등록 repo 케이스). 명시 prefix 가 *주어지면* 그건 그대로
    # 존중해 prefixed ID 를 발행한다 — ≤1 라도 사용자가 골랐으면 따른다.
    #
    # **명시 prefix 의 "등록돼 있어야 한다" 가드는 없다** (자유 입력·"등록 제약
    # 없음"). prefix 는 이제 repo 네임스페이스가 아니라 작업 카테고리 — 새 카테고리를 즉석에서
    # 붙일 수 있어야 한다. 입력측 sanity 는 위 `_validate_prefix`(예약어+형식)만으로 끝난다.
    # 아래 ≥2 가드는 별개 — 등록 repo 가 ≥2 인데 prefix 를 *안* 준 implicit 모호성 방지다
    # (유도 체인·미해소 fail-loud).
    registered = registered_prefixes()
    if len(registered) >= 2:
        if not prefix:
            print("multi-repo 네임스페이스 모드(등록 repo ≥2) — prefix 필요(미해소). "
                  "`--prefix <PFX>` 로 명시하거나 세션을 바인딩하라"
                  "(`PM_SESSION_NAME=<repo>_<N>` env 또는 단일 활성 슬롯 lease → areas.md "
                  "repo→prefix 유도). 미등록이면 먼저 `board.py init --prefix <PFX> --area <name>`.",
                  file=sys.stderr)
            return 1

    tmpl_fm, tmpl_body = load_ticket(template_file())

    # 발행 규율 게이트: board-git 이 공유(별도 git·submodule) 상태일 때만
    # 의미가 있다 — 미충전 stub 이 board-git 에 커밋돼 다른 slot 의 handoff/bootstrap 을
    # 오염시키는 게 문제이므로, board 가 별도 git 이 아니면(legacy·솔로)
    # 게이트 없이 기존처럼 즉시 open/ 에 발행한다. 별도 git 이면 본문을 *쓰기 전에* 미리
    # 검사(`_body_lint_issues` — `lint_bodies` 와 동일 로직)해 placeholder/thin 이 남아있으면
    # `open/` 이 아니라 `drafts_dir()`(STATUS_DIRS 밖)에 쓴다 — draft 가 STATUS_DIRS
    # 순회 대상이 되는 창(open/ 에 잠깐이라도 존재)이 아예 없어야, 이후의 **어떤** mutation
    # (자기 자신의 board-git sync 뿐 아니라 무관한 claim/complete/promote 등)도 draft 를
    # board-git 에 잘못 쓸어담을 수 없다
    # board-git 에 아예 안 보이는 게 핵심).
    # `list`/`show`/`promote` 는 `find_ticket`/`drafts_dir()` 로 draft 를 계속 인지한다.
    #
    # ID 발행(`_next_id` = max+1·동시 발행 race)과 파일 생성을 단일 락으로 직렬화한다
    # 락 안에서 ID 를 *읽고* 곧바로 파일을 만들어, 다른 세션이 같은 ID 를
    # 발행할 틈을 없앤다. board.md 재생성은 락 밖(별도 트랜잭션 — 파생물).
    with board_lock():
        tid = _next_id(prefix)
        slug = _slugify(args.title)
        filename = f"{tid}-{slug}.md"

        # Replace placeholder tokens in body
        body = tmpl_body.replace("T-NNNN", tid).replace("<제목>", args.title)
        # lint 판정은 `tid` 치환과 무관(placeholder/section 검사가 `T-NNNN` 자체를 안 봄) —
        # 발행 전에 판정해 쓰기 경로(open/ vs drafts_dir())를 정한다(open/ 창 노출 0).
        is_draft = _board_git_enabled() and bool(
            _body_lint_issues(tid, body, strict_sections=True))

        fm: dict[str, Any] = dict(tmpl_fm)
        fm["id"] = tid
        fm["title"] = args.title
        fm["status"] = "draft" if is_draft else "open"
        fm["created"] = datetime.date.today().isoformat()
        # created_by = `<user>/<pm-slot>` (provenance·불변·생성 시 set).
        # "누가 추가했나" = 중복-작업 방지의 출처 표식. user 미상이면 슬롯만(graceful).
        fm["created_by"] = identity_tag(
            # task-mode(`--task`)면 created_by = <user>/<task>(F5b 귀속 축). `--repo --slot`이면
            # <user>/<repo>_<N>(생성-세션 기록·세션 기본 뷰 스트림 판정 입력) — 둘 다 `_actor_session_override`
            # 가 해소해 `actor_session` 에 담는다. 무명시는 session_override=None → identity_tag 내부 체인 해소.
            session_override=(actor_session or None),
            user_override=getattr(args, "user", None))
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        fm["completed_at"] = None
        fm["touches"] = (args.touches.split(",") if args.touches else [])
        fm["depends_on"] = (args.depends.split(",") if args.depends else [])
        fm["blocks"] = []
        fm["tags"] = (args.tag.split(",") if args.tag else [])
        fm["estimate"] = args.estimate

        if is_draft:
            drafts_dir().mkdir(parents=True, exist_ok=True)
            path = drafts_dir() / filename
        else:
            path = tickets_dir() / "open" / filename
        dump_ticket(path, fm, body)

    print(f"created {tid} ({_rel_to_repo(path)})")
    print("  → fill in 목표 / 완료 조건 / 참고, then `board.py lint` "
          "(placeholders left in the body fail lint)")

    if is_draft:
        # draft 는 STATUS_DIRS 밖(drafts_dir())에 있어 board.md(STATUS_DIRS 스캔)에도, 다른
        # slot 의 board-git pull/handoff 에도 나타나지 않는다 — 로컬 `board.py show <id>` 로만
        # 조회 가능(`find_ticket` 이 drafts_dir() 폴백으로 찾는다). board-git sync 자체를
        # 부르지 않는다(draft 는 STATUS_DIRS 밖이라 다른 mutation 의 `git add -A` pathspec
        # exclude 와 별개로 이미 board_root 스캔에 걸리지 않지만, 명시적으로 skip).
        print(f"  ⚠ draft — board-git 미커밋(공유 board 오염 방지): "
              f"미충전(placeholder/thin) 본문. 본문을 채운 뒤 "
              f"`board.py promote {tid}` 로 승격(open/ 이동 + board-git 커밋)하라.",
              file=sys.stderr)
        return 0
    refresh_board()
    ready = _board_git_sync_best_effort(f"new {tid}", (path,))
    if not ready:
        print("  ⚠ board-git 부기 보류: local-only/uncommitted", file=sys.stderr)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """draft(drafts_dir() 격리·board-git 미커밋) 티켓을 승격 — 재검사 후 open/ 이동 + board-git sync.

    `board.py new` 가 생성 시점 게이트로 `drafts_dir()`(STATUS_DIRS 밖)에 남긴
    draft 를, 본문을 채운 뒤 이 명령으로 승격한다. 여전히 placeholder/thin 이 남아있으면
    거부(rc=1)하고 남은 이슈를 안내한다(파일은 drafts_dir() 에 그대로 — 재수정 후 재시도).
    통과해야 `open/` 으로 옮겨져 STATUS_DIRS 스캔 대상이 되고 board-git 에 커밋돼 공유 board
    에 존재하게 된다(생성~claim 사이 handoff 창에 미충전 stub 이 인계되는 실패를 원천 차단).
    board 가 별도 git 이 아니면(legacy·솔로) 게이트 자체가 무의미하므로 항상 통과(sync 는
    기존처럼 no-op) — 이 경로에선 draft 개념 자체가 없으므로(`cmd_new` 가 legacy 에선 항상
    `open/` 에 발행) status 는 정상적으로 "open" 이다.
    """
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status not in ("open", "draft"):
        print(f"cannot promote {args.id}: currently in {status}/ (promote 는 open/draft 만)",
              file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    if _board_git_enabled():
        remaining = _body_lint_issues(args.id, body, strict_sections=True)
        if remaining:
            print(f"promote 거부 — {args.id} 에 아직 미충전 {len(remaining)}건:",
                  file=sys.stderr)
            for tid, kind, detail in remaining:
                print(f"  ✗ [{kind}] {detail}", file=sys.stderr)
            return 1
    # 스코프= 이 promote 가 만진 경로. draft 쪽 옛 경로는 `_board_git_scope_pathspec`
    # 이 걸러낸다.
    touched: list[Path] = [path]
    if status == "draft":
        # drafts_dir() → open/ 이동 — 이제서야 STATUS_DIRS 스캔·board-git 대상이 된다.
        fm["status"] = "open"
        dump_ticket(path, fm, body)  # status 갱신을 먼저 디스크에 반영한 뒤 이동.
        new_path = move_ticket(path, "open")
        touched.append(new_path)
        refresh_board()
    ready = _board_git_sync_best_effort(f"promote {args.id}", touched)
    if ready:
        print(f"promoted {args.id} (board-git 승격 완료)")
    else:
        print(f"promoted {args.id} (board-git 부기 보류: local-only/uncommitted)")
    return 0


# `list` 기본뷰(무-status)의 활성 상태 — done 을 접어 범람을 해소한다. `--status all`
# 이 STATUS_DIRS 전체(done 포함)를 연다.
_LIST_ACTIVE_STATUSES: tuple[str, ...] = ("open", "claimed", "blocked")


def _tag_values(fm: dict[str, Any]) -> list[str]:
    """frontmatter 의 tags 를 전부 str 로 캐스팅한 리스트.

    YAML 은 `tags: [2026, cleanup]` 의 `2026` 을 int 로 로드한다 — 이를 그대로
    `str.join` 하면 TypeError 로 크래시하고(cmd_list·마크다운 렌더), 문자열 `--tag`
    와 `in` 비교하면 조용히 매치 실패한다(필터). tags 를 표시·필터하는 전 호출부가
    이 str 캐스팅을 단일 지점에서 거치게 해 두 결함을 함께 없앤다. 문자열 태그는
    str→str 로 무변경(형식·매치 회귀 없음).
    """
    return [str(t) for t in (fm.get("tags") or [])]


# ── board-git freshness 표면화 (advisory·never-block) ──
# 세션 중간의 board 읽기(`list`)에도 board submodule 최신도를 1줄 표면화한다 — 부트스트랩
# 이후 시점의 stale 오독의 잔여
# 갈래를 닫는다. 판정은 **신규 구현 0** — 부트스트랩이 소비하는 pm_bootstrap 의 순수
# 판정(`_format_freshness`·`parse_git_ahead_behind`·`_behind_warning`)을 그대로 재사용해 두
# 소비처(부트스트랩·list)가 같은 하나를 본다(중복 판정 금지). board 비-git(솔로·legacy)은
# freshness 개념이 없어 조용히 생략(오탐 0). list 는 read-only 조회라 pull 하지 않고 표면화만.


# advisory freshness fetch 조율 — `list` 는 대화형으로 세션당 수십 회 도는
# 로컬-only 명령이라, 매 호출 원격 fetch(30s timeout)는 offline/VPN-hang 시 호출당 최대 30s 블록·
# online 도 매번 round-trip 이 advisory 1줄 대비 과하다. fetch 전 FETCH_HEAD mtime TTL 가드로
# 직전 fetch 를 cross-process 재사용(별도 캐시 파일 없음)·advisory fetch timeout 을 5s 로 단축.
_FRESHNESS_FETCH_TTL_SECONDS = 60      # 이 창 안이면 직전 fetch 결과 재사용(fetch 생략).
_FRESHNESS_FETCH_TIMEOUT_SECONDS = 5   # advisory fetch 상한(대화형 hang 완화·기존 30s 경로 불변).


def _board_fetch_head_fresh(ttl: float) -> bool:
    """board-git FETCH_HEAD mtime 이 `ttl` 초 이내면 True — 직전 fetch 재사용(fetch 생략).

    별도 캐시 파일/상태 신설 없이 git 자신의 FETCH_HEAD mtime 만으로 **cross-process** TTL 판정.
    경로는 `git rev-parse --git-path FETCH_HEAD`(submodule/worktree gitdir 레이아웃 무관 해소).
    FETCH_HEAD 부재(한 번도 fetch 안 함)·경로 조회 실패·stat 실패면 False(fetch 진행·보수적).
    """
    try:
        r = _board_git(["rev-parse", "--git-path", "FETCH_HEAD"])
    except Exception:  # noqa: BLE001 — 경로 조회 예외는 stale 취급(fetch 진행).
        return False
    if r.returncode != 0 or not r.stdout.strip():
        return False
    p = Path(r.stdout.strip())
    if not p.is_absolute():
        p = board_root() / p   # rev-parse 는 -C board_root() 기준 상대 경로를 낼 수 있다.
    try:
        return (time.time() - p.stat().st_mtime) < ttl
    except OSError:
        return False           # FETCH_HEAD 부재(한 번도 fetch 안 함) → fetch 진행.


def _load_pm_bootstrap_module():
    """pm_bootstrap 모듈을 같은 tools/ 에서 로드 (freshness 순수 판정 재사용).

    `_load_pm_update_module` 동형 seam — board.py 가 형제 모듈의 순수 함수를 재사용할 때 쓴다.
    실패 시 None → 호출부가 freshness 표면화를 조용히 생략(fail-soft·advisory 라 무발화)."""
    pmb_py = Path(__file__).resolve().parent / "pm_bootstrap.py"
    try:
        mod = _load_module_from_path(
            pmb_py, "pm_bootstrap.py", verifier=_verify_engine_rev,
        )
        return mod
    except Exception as exc:  # noqa: BLE001 — 로드 실패는 무발화(freshness 생략·advisory).
        if _is_engine_rev_skew(exc):
            raise  # pm_bootstrap 사본 skew 는 fail-loud(삼키지 않는다).
        return None


def _board_git_freshness_line() -> str | None:
    """board submodule freshness 를 부트스트랩과 **같은 판정**으로 1줄 만든다 (없으면 None).

    board 비-git(솔로·legacy·`_board_git_enabled()` False)이거나 pm_bootstrap 로드 실패면 None
    (표면화 생략·오탐 0). 그 외엔 board-git 을 fetch(원격 실측·offline 이면 fail-soft) 후
    detached/dirty/ahead·behind 를 board.py 자체 board-git 함수로 수집해, pm_bootstrap 의
    `_format_freshness`로 포맷한다 — **판정 단일화**(중복 0). list
    는 read-only 라 pull 하지 않는다(부트스트랩의 자동 ff-pull 과 달리 표면화만·never-block).
    offline(fetch 실패)이면 remote-tracking 스냅샷을 "최신"으로 주장하지 않고 "판정불가 —
    스냅샷일 수 있음"으로 fail-soft.
    """
    # board-git 존재 확인을 **먼저** — 비-git 솔로는 pm_bootstrap(4천줄) 로드도 fetch 도 안 한다.
    if not _board_git_enabled():
        return None
    pmb = _load_pm_bootstrap_module()
    if pmb is None:
        return None
    # fetch 전 TTL 가드: 직전 fetch 가 TTL 이내면 재사용(원격 실측된 스냅샷이라
    # fetched=True)·fetch 생략. 아니면 advisory 짧은 timeout(5s)으로 fetch(offline 이면 fail-soft).
    if _board_fetch_head_fresh(_FRESHNESS_FETCH_TTL_SECONDS):
        fetched = True
    else:
        try:
            fetched = _board_git(
                ["fetch", "origin"],
                timeout=_FRESHNESS_FETCH_TIMEOUT_SECONDS).returncode == 0
        except Exception:  # noqa: BLE001 — fetch 예외(timeout·offline)는 fail-soft(판정불가).
            fetched = False
    detached = _board_git_head_detached()
    ahead = behind = None
    try:
        ab = _board_git(["rev-list", "--left-right", "--count", "HEAD...@{u}"])
        if ab.returncode == 0:
            parsed = pmb.parse_git_ahead_behind(ab.stdout)
            if parsed is not None:
                ahead, behind = parsed
    except Exception:  # noqa: BLE001 — rev-list 예외는 upstream 미상(behind None) 취급.
        pass
    scope = {"fetched": fetched, "detached": detached, "dirty": None,
             "ahead": ahead, "behind": behind, "note": None}
    if behind and behind > 0:
        # behind>0 에서만 dirty 를 조회(로컬 git status·common 경로 무오버헤드)해 경고 사유를
        # 정확히 채운다(`_behind_warning` = ⚠ behind N — 수동 동기 필요 (fetch/dirty/diverged)).
        scope["dirty"] = bool(_board_git_status_porcelain().strip())
        scope["note"] = pmb._behind_warning(scope)
    return f"board-git: {pmb._format_freshness(scope)}"


def _print_board_freshness() -> None:
    """board freshness 1줄을 **stderr** 로 표면화한다 (advisory·stdout 목록 포맷 무오염).

    stdout 이 아니라 stderr 인 이유: `board list` stdout 를 파싱하는 소비처(회귀 파서·
    pm_bootstrap counts·아래 anti-degrade warn 과 동형)를 오염시키지 않는다. board 비-git/
    로드 실패면 None → 무출력(조용히 생략·오탐 0)."""
    line = _board_git_freshness_line()
    if line is not None:
        print(line, file=sys.stderr)


def cmd_list(args: argparse.Namespace) -> int:
    # `--mine` / `--repo`·`--slot`
    # 뷰: 단일 공유 보드의 렌즈 — **현재 사용자**의 area open + claim.
    # identity 입력을 한 번 해소해 행마다 재계산 안 함. **무인자 list 는 세션 기본 뷰**
    # (내 세션 스트림만 = 생성 open + 내 claim·타 세션분 완전 비노출·아래 default_view)이고,
    # 필터 없는 전체(모든 세션·타 사용자)는 `--all` 이다(status 셀렉터는 어느 뷰에도 적용).
    #
    # user-first: 필터 뷰의 "me" 는 **항상 현재 사용자**(`user_name()` =
    # local.conf user= > git config user.email)다. `--repo`/`--slot` 의 my_user 를
    # area_owner-derived(`_area_owner_from_session`/`_area_owner_for_single_area`)로
    # area_owner 미설정(흔함)이면 my_user=None 이 돼 slot-only 매칭·
    # all-open degrade 로 타 슬롯 claim·타 사용자 티켓이 유출되던 근본을 없앤다. area_owner 는
    # 이제 open-티켓 *소유* 정의(`_ticket_owner`)로만 남고, "누가 조회하는가" 와 무관하다.
    #
    # 뷰 스코프:
    #   - `--mine` = 내 것(내 claim ∪ 내 open), **전 슬롯**(slot_scoped=False).
    #   - `--repo X --slot N`(kind=slot) = 내 것 **∩ 그 슬롯**(slot_scoped=True·완전 일치) — 옛
    #     `--session <repo>_<N>` 과 동형(`_slot_matches` mode="exact").
    #   - `--repo X`(kind=repo·슬롯 무) = 내 것 **∩ 그 repo 의 내 슬롯 전체**(slot_scoped=True·
    #     `_slot_matches` mode="repo"·prefix 매칭) — 신규 repo-scope 뷰. 옛 bare `--slot N`
    #     (repo 불문 cross-repo suffix 매칭)은 제거됐다 — `--slot` 단독은 이제 fail-loud.
    #   - `--task <이름>` = 내 것 **∩ 그 task**(slot_scoped=True·
    #     완전 일치) — 멤버십 = claimed_by 바인딩이 그 task(`<user>/<task>`)인
    #     claim + 내 소유 open backlog. task 는 slot 축과 직교지만 조회 렌즈로선 slot 축을 task
    #     이름으로 재사용한다 — `_slot_matches` exact 가 claimed_by 의 `/` 뒤 토큰을 task 와 대조하고
    #     예약(task 명 ≠ `<repo>_<N>`) 덕에 slot 세션값 `<user>/<repo>_<N>` 과 **기계 판별**된다
    #     (추가 필드 0). `_ticket_is_mine` 단일 predicate 를 그대로 상속(point-patch 금지).
    #   `--mine`·`--repo`(+`--slot`)·`--task` 는 상호 배타(뷰 스코프는 하나만) — 타 사용자는 어느
    #   필터 뷰에도 안 나온다(`list --all` 전용).
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as e:
        sys.exit(f"[중단] {e}")
    explicit_mine = bool(getattr(args, "mine", False))
    # task 렌즈(`--task <이름>`) — slot 축과 직교지만 조회 뷰로선 별개 필터 스코프라 --mine/
    # --repo/--slot 과 상호 배타(어느 축으로 좁힐지 모호 방지·뷰 스코프는 하나). None=미지정.
    task_lens = identity.task
    if task_lens is not None:
        # read 경로도 깔때기 소비 — 미검증이면 `--task alpha_1`(슬롯
        # 예약 패턴) 입력이 `_slot_matches` exact 에서 slot claim 을 task 처럼 매칭해 기계 판별이
        # 깨진다. 부작용 0 조회지만 소비 지점 폐쇄(전 surface 단일 규칙)에 합류한다.
        _validate_actor_task_or_exit(task_lens)
    if task_lens is not None and (explicit_mine or identity.kind != "none"):
        sys.exit("[중단] --task 는 --mine/--repo/--slot 과 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    if explicit_mine and identity.kind != "none":
        sys.exit("[중단] --mine 은 --repo/--slot 과 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    mine = explicit_mine or identity.kind != "none" or task_lens is not None
    # `--all`= 기존 무인자 전체 뷰(모든 세션·타 사용자 포함)의 이관. 무인자
    # 기본은 이제 **세션 기본 뷰**(default_view — 내 세션 스트림만·타 세션분 완전 비노출)다. `--all` 은
    # 렌즈(`--mine`/`--repo`/`--slot`/`--task`)와 상호 배타(뷰 스코프는 하나만). default_view 는
    # 어떤 렌즈도 `--all` 도 없을 때(순수 무인자) 발동한다.
    all_flag = bool(getattr(args, "all", False))
    if all_flag and mine:
        sys.exit("[중단] --all 은 --mine/--repo/--slot/--task 와 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    default_view = not all_flag and not mine
    # 세션 뷰— **무인자 기본 뷰** 또는 **명시 세션 뷰**(`--repo X --slot N`·kind="slot").
    # 둘 다 "그 세션 스트림"(생성 open + 그 세션 claim)만 출력한다(`_in_default_view`). `--repo X`
    # 단독(repo-scope)·`--mine`(user-wide)·`--task`(task 렌즈)는 세션 뷰가 아니라 기존 `_ticket_is_mine`
    # 렌즈(의미론 불변) — 단일 세션이 아니거나(repo/task) user-단위(--mine)라 생성-세션 스트림이 부적.
    session_view = default_view or identity.kind == "slot"
    # slot-scoped 뷰(--repo/--slot·--task)는 claim 을 그 슬롯/repo/task 로 교집합한다(--mine 은
    # 전 슬롯·전 task). task 렌즈는 slot 축을 task 이름으로 재사용(claimed_by 재사용
    # ) — slot_mode="exact"(task 토큰 완전 일치)라 예약으로 slot 세션과 기계 판별.
    slot_scoped = identity.kind != "none" or task_lens is not None
    slot_mode = "repo" if identity.kind == "repo" else "exact"
    if task_lens is not None:
        my_user = user_name()
        my_slot = task_lens
    elif identity.kind == "slot":
        my_user = user_name()
        my_slot = identity.session
    elif identity.kind == "repo":
        my_user = user_name()
        my_slot = identity.repo
    else:
        # 무렌즈 경로 — `--mine`(user-wide) 또는 기본 뷰(default_view·세션 스코프). 둘 다
        # 현재 사용자/세션을 해소한다(전체 뷰 `--all` 만 정체성 무해소·additive). 기본 뷰의 my_slot
        # (=현 세션)은 내 세션 claim 매칭과 생성-세션 스트림 판별(created_by 세션 일치)의 입력이다.
        resolve_identity = mine or default_view
        my_user = user_name() if resolve_identity else None
        my_slot = session_name() if resolve_identity else ""
        if mine and my_slot is None:
            # 세션 미바인딩(surface·required=False) — slot-claim 필터를 못 좁힌다.
            # 안내는 stderr 로 내 stdout 티켓 목록 포맷을 오염시키지 않는다(소유 open 은 계속 표시).
            print("(비바인딩 — 세션 미해소·claim 필터 비활성; `--repo <repo> --slot <N>` 로 지정)",
                  file=sys.stderr)
    # graceful degrade: (a) 풀(내 소유 open) 필터는 보드에 area_owner 가 *운영
    # 중일 때만* 그 파티션을 1차 소유로 쓴다. areas.md 에 area_owner 가 하나도 안 채워졌으면
    # (미마이그레이션 채택자·솔로) area_owner_in_use=False → 소유는 created_by.user 2차 폴백으로
    # 해소한다(`_ticket_owner`).
    #
    # 다중사용자 판정(`multi_user`·solo 정의 완결): **티켓 user 토큰이든 area_owner 든
    # 둘 중 하나라도 distinct ≥2 면 multi-user**. `_distinct_ticket_users`(티켓 귀속만 셈) 단독이면
    # 다중-owner 보드라도 claim 이 전부 legacy 슬롯-only(user 토큰 0)일 때 ≤1 로 떨어져 solo 로
    # 오판 → legacy 슬롯-only 포함 경로가 발동해 (당시) bare `--slot N`(repo 불문 cross-repo
    # suffix 매칭은 제거됨)이 타 area 의 legacy `<repo>_N` 을 끌어오는 누출이 났다.
    # `_distinct_area_owners`(areas 소유 다중성)를 OR 로 더해 그 클래스를 닫는다.
    # solo(둘 다 ≤1)면 미해소 open all-open degrade + legacy 슬롯-only 포함 보존, 다중이면
    # strict-exclude한다.
    area_owner_in_use = (mine or default_view) and _area_owner_in_use()
    multi_user = (mine or default_view) and (
        _distinct_ticket_users() > 1 or _distinct_area_owners() > 1)
    # status 뷰 셀렉터: 기본(무-status)=활성만(done 접기) · `--status all`=전체(done 포함)
    # · `--status <특정>`=그 status 만(기존 동작 무변경).
    status_arg = getattr(args, "status", None)
    if status_arg == "all":
        allowed_statuses = STATUS_DIRS
    elif status_arg:
        allowed_statuses = (status_arg,)
    else:
        allowed_statuses = _LIST_ACTIVE_STATUSES
    rows: list[tuple[str, dict]] = []
    # 세션격리 strict-exclude 신호: 다중사용자 판정 때문에 귀속 미해소/불일치 티켓을 이 뷰에서
    # 조용히 드롭했는지 잡는다. solo면 재평가 자체를 생략해 오버헤드와 오탐을 피한다.
    strict_exclude_fired = False
    for status in STATUS_DIRS:
        if status not in allowed_statuses:
            continue
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in _tag_values(fm):
                continue
            if session_view:
                # 세션 뷰(무인자 기본 또는 `--repo X --slot N`) — 그 세션 스트림(생성 open +
                # 그 세션 claim)만. 타 세션분은 카운트 줄 포함 완전 비노출(전체는 `--all`).
                in_view = _in_default_view(
                    status, fm, my_user, my_slot, area_owner_in_use, multi_user)
                if in_view:
                    rows.append((status, fm))
                elif multi_user and not strict_exclude_fired:
                    # 같은 predicate 를 solo로 재평가해 다중사용자 strict-exclude 때문에 숨긴
                    # 티켓인지 판정한다. 해소된 사용자 값이 과거 스탬프와 어긋난 경우도 같은
                    # 세션 후보인지 확인하도록 조회 user만 미해소 상태로 완화한다. 세션 축은
                    # 그대로이므로 타 세션의 정상 제외는 경고 신호가 되지 않는다.
                    strict_exclude_fired = (
                        _in_default_view(
                            status, fm, my_user, my_slot, area_owner_in_use, False)
                        or _in_default_view(
                            status, fm, None, my_slot, area_owner_in_use, False)
                    )
                continue
            if mine and not _ticket_is_mine(status, fm, my_user, my_slot,
                                            area_owner_in_use, multi_user,
                                            slot_mode=slot_mode,
                                            slot_scoped=slot_scoped):
                # 이 제외가 *strict-exclude* 였는지 판정: 같은 predicate 를 multi_user=False 로
                # 재평가해 solo(all-open degrade)라면 포함됐을 open 이면 = 다중사용자라서 드롭한
                # 것(`_ticket_is_mine` 미해소 분기). 판정을 복제하지 않고 단일 predicate 를 재사용
                # 해 실 드롭만 신호로 잡는다. 이미 발동했으면 재평가를 생략한다. 소유 해소된 타
                # 사용자 티켓 제외는 solo 에서도 제외라 무신호다.
                if multi_user and not strict_exclude_fired and _ticket_is_mine(
                        status, fm, my_user, my_slot,
                        area_owner_in_use, False, slot_mode=slot_mode,
                        slot_scoped=slot_scoped):
                    strict_exclude_fired = True
                continue
            rows.append((status, fm))
    # anti-degrade loud-warn: 다중사용자 격리가 조용히 티켓을 드롭했거나 정체성이 미해소면 목록
    # 출력 전에 stderr로 한 줄 경고한다. stdout 목록 포맷은 그대로이며 solo는 경고하지 않는다.
    if (mine or session_view) and multi_user and (
            strict_exclude_fired or my_user is None):
        print(
            "⚠ 세션격리(strict-exclude): 다중사용자 보드에서 귀속이 미해소되거나 현재 사용자와 "
            "어긋난 티켓을 이 뷰에서 제외했다. solo 인데 email(git config user.email)을 바꿨다면 "
            "옛 티켓 귀속(old→new)이 어긋나 2인으로 오판된 것 — created_by/claimed_by 를 backfill "
            "로 정합: `python3 .project_manager/tools/board.py migrate-identity --dry-run` "
            "(단일-세션 op·다른 세션 claim 중 실행 금지). 진짜 다중사용자면 정체성 설정 "
            "`board init --owner <you>`.",
            file=sys.stderr,
        )
    # board-git freshness 표면화 (advisory·stderr) — 각 list 변형 공통·양 return 경로
    # ("(no tickets)"·행 있음)를 커버하도록 여기서 한 번 소환. board 비-git 이면 무출력.
    _print_board_freshness()
    if not rows:
        print("(no tickets)")
        return 0
    for status, fm in rows:
        tags = ",".join(_tag_values(fm))
        claimed = fm.get("claimed_by") or ""
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:7s}] {fm['id']}  {title:60s}  {claimed:18s}  {tags}")
    return 0


# 무prefix(legacy `T-NNNN`) 티켓의 prefix-list 라벨. `none` 은 무prefix 네임스페이스의 1급
# 인자이므로 그 라벨을 그대로 쓴다(예약어와 표기 일치).
_NO_PREFIX_LABEL = "none"


def cmd_prefix_list(args: argparse.Namespace) -> int:
    """prefix별 티켓 수·번호범위 현황을 출력한다 (read-only·비파괴).

    STATUS_DIRS(open/claimed/blocked/done)의 전 티켓 ID 를 파싱해 `T-<p>-NNN` → 그 prefix,
    `T-NNNN` → `none`(무prefix)로 버킷팅한다. mess(카테고리 난립·번호 재시작)를 표면화하는
    도구 — board mutation·rewrite 는 하지 않는다.
    """
    # prefix(또는 None=무prefix) → [(순번, 실 ID), …]. 실 ID 를 들고 있어 min/max 를 재구성
    # 없이 그대로 범위로 쓴다(zero-pad 재구성 mismatch 회피).
    buckets: dict[str | None, list[tuple[int, str]]] = {}
    for status in STATUS_DIRS:
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            tid = fm.get("id") or _ticket_id_from_filename(p.name)
            if not tid:
                continue
            num = _ticket_id_number(tid)
            if num is None:
                continue
            buckets.setdefault(_ticket_prefix(tid), []).append((num, tid))
    if not buckets:
        print("(no tickets)")
        return 0
    # none(무prefix) 먼저, 그다음 prefix 알파벳순 — 무prefix 기준선을 맨 위에 고정.
    print(f"  {'prefix':16s}{'count':>6s}  범위(min ~ max)")
    for pfx in sorted(buckets, key=lambda k: (k is not None, k or "")):
        entries = sorted(buckets[pfx])
        lo_id, hi_id = entries[0][1], entries[-1][1]
        label = pfx if pfx is not None else _NO_PREFIX_LABEL
        rng = lo_id if lo_id == hi_id else f"{lo_id} ~ {hi_id}"
        print(f"  {label:16s}{len(entries):>6d}  {rng}")
    return 0


# ── prefix rename/strip/merge/delete 코어  ─────────
# 카테고리 개명/통합 동사. rewriter(`rewrite_refs`)를 소비해 참조까지 무손실 relabel
# 한다. 공통 파이프라인(`_prefix_relabel`): old→new 맵 → collision abort → 본문 토큰치환 +
# slug 파일명 rename → 홈 git clean 가드 → board-git 백업 commit. 티켓 물리삭제 없음.

def _parse_prefix_arg(raw: str) -> str | None:
    """CLI prefix 인자 → 실 prefix(str) 또는 None(무prefix). 예약어 `none`(대소문자 무관) → None.

    `none` 은 이름 없는(`T-NNNN`) 네임스페이스의 1급 인자라 from/to/into 어디서든
    `None`(무prefix) 로 해소된다. prefix 동일성이 case-insensitive fold 이므로 `NONE`/`None` 도
    같은 예약어로 fold 된다. `none` 이 실 prefix 로 등록될 수 없게 예약돼 있어
    (`_validate_prefix`) 실 카테고리와 충돌하지 않는다.
    """
    return None if raw.lower() in _PREFIX_RESERVED else raw


def _format_ticket_id(prefix: str | None, num: int) -> str:
    """prefix(또는 None=무prefix)와 순번으로 canonical ID 생성 (`_next_id` 발행 규칙과 정합).

    None → `T-NNNN`(최소 4자리) · prefix → `T-<p>-NNN`(최소 3자리). zero-pad 는 *최소* 폭이라
    번호가 커지면 자연 확장된다(`_next_id` 와 동형).
    """
    if prefix:
        return f"T-{prefix}-{num:03d}"
    return f"T-{num:04d}"


def _scan_prefix_tickets() -> list[dict[str, Any]]:
    """STATUS_DIRS 전 티켓을 relabel 용 레코드로 수집한다 (id·num·prefix·status·path·created).

    `created` 는 merge 시간순 정렬 키(문자열 ISO 로 정규화 — yaml date/str 양쪽 흡수). ID 미해소
    (파일명·frontmatter 둘 다 실패)나 순번 미상 파일은 건너뛴다(`cmd_prefix_list` 버킷팅과 동형).
    """
    out: list[dict[str, Any]] = []
    # .drafts 포함 — draft 도 이미 발행된 ID(find_ticket/_next_id 인지).
    # relabel 이 draft 를 놓치면 old-prefix draft 가 잔존, promote 시 혼재가 보드로 재유입된다.
    for status in (*STATUS_DIRS, ".drafts"):
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            tid = fm.get("id") or _ticket_id_from_filename(p.name)
            if not tid:
                continue
            num = _ticket_id_number(tid)
            if num is None:
                continue
            out.append({
                "id": tid, "num": num, "prefix": _ticket_prefix(tid),
                "status": status, "path": p,
                "created": str(fm.get("created") or ""),
            })
    return out


def _existing_ticket_prefixes() -> set[str]:
    """티켓 ID 에 실제로 쓰인 prefix 집합 (canonical case·fold 네임스페이스 판정용).

    `_scan_prefix_tickets` 의 canonical ID 해소(frontmatter id 우선·파일명 폴백)를 재사용해
    숫자-slug 모호성 없이 실 prefix 만 모은다. rename/merge dst 의 case-only 중복 검출이 이
    집합에 fold-비교한다(기존 `T-AAA-*` 가 있으면 dst `aaa` 를 fail-loud).
    """
    return {p for t in _scan_prefix_tickets() if (p := t["prefix"])}


def _rename_map(src: str | None, dst: str | None,
                tickets: list[dict[str, Any]]) -> dict[str, str]:
    """rename 무충돌 맵 — src 네임스페이스 티켓의 prefix 만 dst 로 교체(번호 유지).

    src 매칭은 **case-insensitive fold**(`_fold_key`) — `rename aaa bbb` 가 기존
    `T-AAA-*`(prefix `AAA`)를 잡아 relabel 한다(case 만 다르면 silent no-op 하던 갭 봉합). dst 는
    `_validate_dst_prefix` 가 exact canonical case 를 강제하므로 그대로 발행 case 다.
    """
    src_key = _fold_key(src)
    id_map: dict[str, str] = {}
    for t in tickets:
        if _fold_key(t["prefix"]) != src_key:
            continue
        new_id = _format_ticket_id(dst, t["num"])
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _merge_append_map(sources: list[str | None], into: str | None,
                      tickets: list[dict[str, Any]]) -> dict[str, str]:
    """merge 기본(append) 맵 — source 티켓을 created 순으로 대상 max 번호 *뒤에* 재부여.

    대상(into) 티켓 번호는 무변경(저위험). tiebreak = (source 목록 순서, 기존 번호) 로 같은
    created 안에서 기존 상대순서를 보존한다.
    created 부재(빈 문자열) 티켓은 정렬에서 맨 앞(최고령)으로 간다 — created 없는 티켓은 대개
    구세대(초기 도입 전) 산출물이라 최고령 배치가 자연스럽다(suggestion 채택·현행 유지).

    source membership 은 **case-insensitive fold**(`_fold_key`) — `merge aaa --into bbb`
    가 기존 `T-AAA-*` source 를 모은다. into(대상) max 계산은 exact(`== into`) — `_validate_dst_prefix`
    가 into 를 exact canonical case 로 강제하므로 case-혼동이 없다(source 측만 fold).
    """
    src_index = {_fold_key(p): i for i, p in enumerate(sources)}
    src_tickets = [t for t in tickets if _fold_key(t["prefix"]) in src_index]
    start = max((t["num"] for t in tickets if t["prefix"] == into), default=0)
    ordered = sorted(
        src_tickets, key=lambda t: (t["created"], src_index[_fold_key(t["prefix"])], t["num"]))
    id_map: dict[str, str] = {}
    for i, t in enumerate(ordered, start=start + 1):
        new_id = _format_ticket_id(into, i)
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _merge_reorder_map(sources: list[str | None], into: str | None,
                       tickets: list[dict[str, Any]]) -> dict[str, str]:
    """merge --reorder-chronological 맵 — 대상+source 전체를 created 순으로 1..N 재번호(opt-in).

    전체 interleave 라 대상 티켓 ID 도 바뀌어 전 참조 rewrite(17k refs 고위험)라 opt-in 이다.
    tiebreak: 대상 그룹 먼저(-1), 그다음 source 목록 순서, 그다음 기존 번호.

    source membership 은 case-insensitive fold(`_fold_key`)·into 그룹은 exact(`== into`·
    `_validate_dst_prefix` 가 canonical case 강제) — `_merge_append_map` 과 동일 규율.
    """
    src_index = {_fold_key(p): i for i, p in enumerate(sources)}
    involved = [t for t in tickets
                if t["prefix"] == into or _fold_key(t["prefix"]) in src_index]

    def _key(t: dict[str, Any]) -> tuple[str, int, int]:
        group = -1 if t["prefix"] == into else src_index[_fold_key(t["prefix"])]
        return (t["created"], group, t["num"])

    id_map: dict[str, str] = {}
    for i, t in enumerate(sorted(involved, key=_key), start=1):
        new_id = _format_ticket_id(into, i)
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _collision_key(tid: str) -> tuple[str | None, int] | str:
    """collision 판정용 정규화 키 — `(prefix, 논리번호)`. 파싱 불가면 문자열 그대로(방어적).

    문자열 비교로는 `T-001` 과 `T-0001` 이 서로 다른 최종 ID 로 보여 zero-pad 폭만 다른 *같은
    논리 티켓번호*의 공존을 놓친다(내부 reviewer should-fix). `(prefix, int)` 로 정규화해 폭
    불일치도 같은 키로 묶어 abort 로 잡는다. prefix 는 **case-insensitive fold**(`_fold_key`·
    이미 오염된 `T-AAA-001`/`T-aaa-001` 공존 보드에서 rename/merge 가 그 case-split 를
    collision 으로 잡는다(무손실 abort). 순번 미파싱(malformed)은 리터럴 문자열로 폴백해 자기끼리만
    충돌 판정(오검출 없음).
    """
    num = _ticket_id_number(tid)
    if num is None:
        return tid
    return (_fold_key(_ticket_prefix(tid)), num)


def _detect_collisions(id_map: dict[str, str], all_ids: set[str]) -> list[str]:
    """relabel 후 new ID 유일성 검사 — 중복(같은 논리 ID 로 ≥2 티켓)을 정렬 목록으로 반환.

    최종 ID = `id_map.get(cur, cur)`(맵에 없으면 자기 ID 유지). 유일성은 문자열이 아니라
    `(prefix, 논리번호)`(`_collision_key`)로 판정한다 — `T-001` 과 `T-0001` 처럼 zero-pad 폭만
    다른 같은 논리번호도 충돌로 잡는다. 두 티켓이 같은 논리 ID 로 떨어지면 relabel 이 티켓을
    잃으므로 abort 해야 한다(무손실 원칙).
    """
    seen: dict[tuple[str | None, int] | str, list[str]] = {}
    for cur in all_ids:
        final = id_map.get(cur, cur)
        seen.setdefault(_collision_key(final), []).append(final)
    dup: list[str] = []
    for finals in seen.values():
        if len(finals) > 1:
            dup.extend(finals)
    return sorted(set(dup))


def _is_rewritable(path: Path) -> bool:
    """`rewrite_refs` 가 본문을 읽어 치환할 수 있는 파일인지 — 읽기 실패(비-UTF-8·권한)면 False.

    `rewrite_refs` 의 read 와 *같은 방식*(`encoding="utf-8"` strict·`newline=""`)으로 프로브해,
    거기서 skip 될 파일을 `_plan_file_renames` 도 정확히 같은 판정으로 걸러내게 한다(둘의 skip
    결정이 어긋나면 파일명↔content 불일치가 생긴다).
    """
    try:
        with path.open("r", encoding="utf-8", newline="") as fh:
            fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _canonical_ticket_id(path: Path, fm: dict[str, Any] | None = None) -> str | None:
    """티켓의 canonical ID — frontmatter `id:` 1차 진실·파일명 파싱은 폴백 (숫자-slug 모호성 해소).

    파일명 파서(`_ticket_id_from_filename`)는 legacy `T-NNNN` + **숫자로 끝나는 slug**
    (`T-0036-fix-123.md`)를 prefixed ID `T-0036-fix-123` 으로 오인한다(하이픈-마디 grammar 모호성·
    `dump_ticket`/`_next_id` 이 발행 시 확정해 쓰는 frontmatter `id:` 가
    권위 진실이므로 이를 우선하고, 부재/읽기 실패(비-UTF-8·frontmatter 깨짐) 시에만 파일명 파싱으로
    폴백한다(fail-soft). `_scan_prefix_tickets` 의 `fm.get("id") or …` 와 동형 semantics — 스캔측과
    rename-planning 측이 같은 canonical 해소를 쓰게 해 파일명↔content 불일치를 원천 차단한다.

    `fm` 이 주어지면(호출부가 이미 로드) 재-read 를 피한다. 미지정이면 여기서 fail-soft 로 로드한다.
    """
    if fm is None:
        try:
            fm, _ = load_ticket(path)
        except Exception:  # noqa: BLE001 — 읽기/파싱 실패는 파일명 폴백(fail-soft·비-UTF-8 등).
            fm = {}
    fid = fm.get("id")
    if isinstance(fid, str) and fid:
        return fid
    return _ticket_id_from_filename(path.name)


def _plan_file_renames(id_map: dict[str, str]) -> list[tuple[Path, Path]]:
    """slug 파일명 rename 계획 — `T-old-slug.md` → `T-new-slug.md` (상태 디렉토리 보존).

    canonical ID 는 **frontmatter `id:` 1차 진실**(`_canonical_ticket_id`)로 잡고, 파일명은 그 ID 로
    시작하므로 prefix 구간만 new ID 로 바꾸고 나머지(`-slug.md`)는 보존한다(slug 안에 old ID 가 또
    있어도 오치환 없음). 파일명-only 파싱은 숫자로 끝나는 legacy slug(`T-0036-fix-123.md`)를
    prefixed ID 로 오인해 rename 을 누락시키므로, 스캔측(`_scan_prefix_tickets`)과
    같은 fm-우선 해소로 통일한다. canonical 이 파일명 접두가 아니면(코너/corrupt) slug 슬라이스가
    어긋나므로 파일명 파싱값으로 폴백해 접두 일치를 보장한다(비손실·기존 동작 보존).

    **본문 rewrite 가 스킵될 티켓 파일(비-UTF-8·읽기 실패)은 rename 도 제외**한다(suggestion 채택·
    `rewrite_refs` 가 그 파일의 content id 를 못 바꿔 남겨두는데 파일명만 new ID
    로 바꾸면 파일명↔content id 가 어긋난다. 같은 read 프로브(`_is_rewritable`)로 걸러 파일명을
    유지하고 stderr 경고로 수동 확인을 유도한다(silent 누락 금지·`rewrite_refs` 의 skip 경고와 짝).
    """
    renames: list[tuple[Path, Path]] = []
    for status in (*STATUS_DIRS, ".drafts"):   # .drafts 포함 — _scan_prefix_tickets 와 lockstep.
        for p in (tickets_dir() / status).glob("T-*.md"):
            tid = _canonical_ticket_id(p)       # frontmatter id 1차 진실 (숫자-slug 모호성 해소).
            if not (tid and p.name.startswith(tid)):
                tid = _ticket_id_from_filename(p.name)   # 접두 불일치(corrupt) → 파일명 폴백.
            if not (tid and tid in id_map):
                continue
            if not _is_rewritable(p):
                print(f"  ⚠ rename skip {p.name} — 본문 rewrite 불가(비-UTF-8/읽기 실패): "
                      "파일명↔content id 불일치 방지 위해 파일명 유지. 수동 확인.", file=sys.stderr)
                continue
            new_name = id_map[tid] + p.name[len(tid):]
            renames.append((p, p.with_name(new_name)))
    return renames


def _apply_file_renames(renames: list[tuple[Path, Path]]) -> None:
    """파일명 rename 을 2단계(src→tmp→dst)로 적용 — 번호 swap(reorder) 시 clobber 방지.

    reorder 는 두 티켓이 번호를 맞바꿀 수 있어(T-P-001↔T-P-002) 직접 rename 하면 중간에 대상을
    덮어쓴다. 먼저 전부 유니크 tmp(`.relabel-N.tmp`·dot-prefix 라 STATUS glob 밖)로 옮긴 뒤
    tmp→최종으로 옮겨 사이클/swap 에서도 무손실이다.
    """
    staged: list[tuple[Path, Path]] = []
    for i, (src, dst) in enumerate(renames):
        tmp = src.with_name(f".relabel-{i}.tmp")
        os.rename(src, tmp)
        staged.append((tmp, dst))
    for tmp, dst in staged:
        os.rename(tmp, dst)


def _home_git_status_porcelain() -> str | None:
    """홈(superproject) git working tree 의 uncommitted 변경 (`status --porcelain`).

    None = git 부재/repo 아님(보고 skip·솔로 안전) · `""` = clean · non-empty = dirty. prefix
    rewrite 는 wiki/log(홈 git)를 건드리므로 relabel diff 가 남의 WIP 와 섞여 보일 수 있다 —
    그래서 이 값을 **안내로** 낸다.
    남의 dirty 로 내 작업이 막히는 과차단이었고, mutation 자체가 만진 경로만 커밋하므로
    격리는 스코프가 보장한다. fail-soft: 예외는 None.
    """
    if shutil.which("git") is None:
        return None
    try:
        r = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_BOARD_GIT_TIMEOUT_SECONDS, check=False)
    except Exception:  # noqa: BLE001 — fail-soft: 판정 예외는 가드 skip(None).
        return None
    if r.returncode != 0:
        return None  # not a git repo → 가드 skip
    return r.stdout


def _prefix_relabel(build_map, *, verb: str, label: str,
                    dry_run: bool, noun: str = "prefix") -> int:
    """검증된 old→new 맵을 적용하는 공통 파이프라인 (rename/merge/reid 공용).

    `noun` = 출력·commit 메시지의 op 접두어(카테고리 동사는 "prefix", 단건 재부여는 "reid" —
    `op = f"{noun} {verb}".strip()`·verb 빈 문자열이면 noun 만). 기본값은 prefix 동사 하위호환.

    dry-run: 규모 preview("N ID·M refs·K 파일")만·쓰기 0(read-only 라 락 불요). 적용: 홈 git
    dirty 안내(**차단 아님**) → **단일 `board_lock()` 구간에서** 본문 토큰
    rewrite(`rewrite_refs`) + slug 파일명 rename → board.md 재생성 → **만진 경로만** board-git
    백업 commit(분리 형상)·legacy skip 안내. 티켓 물리삭제 없음.

    **락 직렬화(codex must-fix)**: rewrite→rename→refresh→board-git 백업 전체를 `cmd_new` 의 ID
    발행·`cmd_claim` 이 쓰는 그 board_lock 으로 감싼다— 동시 new/claim 과 직렬화해
    relabel 이 그들 사이에 끼어 참조를 절반만 고치는 것을 막는다. **재진입 정리**: 이 구간 안에서
    부르는 board.md 재생성은 `refresh_board`(자체 board_lock·재진입 데드락)가 아니라 락-보유 전제
    변형 `_refresh_board_locked` 를 직접 부른다. board-git 백업(`_board_git_*`)은 board_lock 이
    아니라 별도 git repo(subprocess)를 만지므로 재획득이 없다(구간 안이어도 데드락 없음).
    complete/block/unclaim 은 설계상 board_lock 을 안 잡는 lock-free rename 이라 이 락이 못 막는다
    (migrate-identity 와 같은 정직한 한계 — 단일-세션 admin op 전제).

    **TOCTOU 봉합**: `build_map` 은 (id_map|None, rc) 를 반환하는 클로저다 —
    스캔→맵→collision 검사를 *락 안 fresh snapshot* 에서 수행해, 검사와 적용 사이에 `cmd_new` 가
    같은 번호를 발행해 stale 검사를 통과하는 창을 없앤다. 추가 belt: stage1 전에 계획된 dst 가
    (계획 밖 파일로) 이미 존재하면 **아무것도 쓰기 전에 abort** 한다(덮어쓰기 원천 차단).
    dry-run 은 read-only preview 라 락 없이 빌드한다(정확성보다 규모 감이 목적).
    """
    root = REPO / ".project_manager"
    op = f"{noun} {verb}".strip()   # "prefix rename" / "prefix merge" / "reid" (verb 빈 문자열이면 noun만)

    def _scale_line(n_ids: int, scale: dict[str, int], n_renames: int) -> str:
        return (f"{n_ids} ID 변경 · {scale['refs']} refs · "
                f"{scale['files']} 파일(본문 rewrite) · {n_renames} 파일명 rename")

    if dry_run:
        # read-only preview — 어떤 파일도 안 쓰므로 board_lock 을 잡지 않는다(스냅샷 오차 허용).
        id_map, rc = build_map()
        if not id_map:
            return rc
        scale = rewrite_refs(root, id_map, dry_run=True)
        print(f"[dry-run] {op} {label} — "
              f"{_scale_line(len(id_map), scale, len(_plan_file_renames(id_map)))}")
        print("[dry-run] 쓰기 0 — 적용하려면 --dry-run 없이 재실행.")
        return 0

    # 홈 git dirty 는 **차단하지 않고 보고**한다. 전면 abort 는 "공유
    # 트리를 나 혼자 쓴다"는 가정에 서 있어, 멀티-PM 에서 *남의* WIP 하나로 내 relabel 이 막혔다
    # 아래 mutation 은 자기가 만진 경로만
    # 커밋하므로 남의 dirty 와 무관하고, 리뷰용 격리는 이 안내 + board-git 백업 rev 로 갈음한다.
    dirty = _home_git_status_porcelain()
    if dirty:
        n_dirty = len([ln for ln in dirty.splitlines() if ln.strip()])
        print(f"  ⓘ 홈 git 에 무관한 미커밋 변경 {n_dirty}건 — relabel 이 만진 파일과 뒤섞여 보이니 "
              "리뷰 시 유의하라(차단하지 않는다). 이 명령은 자기가 만진 경로만 커밋한다.")

    # board-git 백업 rev — 분리 형상에서 relabel *직전* HEAD(되돌아갈 지점). rewrite 뒤 새 commit
    # 이 relabel 을 board-git 에 기록하므로 이 rev 로 `reset --hard` 하면 원복된다.
    backup_rev = _board_git_head() if _board_git_enabled() else None

    # 전체 mutation 을 단일 board_lock 으로 직렬화 (동시 new/claim 과 상호배제). 파일명
    # rename 계획은 락 보유 하에 fresh scan 으로 세운다(rewrite 와 같은 스냅샷). refresh 는 락-보유
    # 변형(`_refresh_board_locked`)을 직접 불러 board_lock 재진입(데드락)을 피한다.
    # 백업 commit 이 이 구간 안에 있으므로 **board_git_lock 을 board_lock 보다 먼저** 잡는다
    # (락 순서 고정 — 역순 획득이 하나라도 있으면 claim 과 데드락). board-git 비활성이면
    # board_git_lock 은 no-op 이라 legacy 경로는 무변경.
    with board_git_lock(), board_lock():
        # 맵 생성+collision 검사도 락 안 fresh snapshot 으로 — 검사↔적용 사이 cmd_new 창 봉합.
        id_map, rc = build_map()
        if not id_map:
            return rc
        renames = _plan_file_renames(id_map)
        # dst 선검증(belt·아무것도 쓰기 전): 계획 밖 파일이 dst 를 점유하면 abort — 덮어쓰기 차단.
        planned_srcs = {src for src, _ in renames}
        occupied = [dst for _, dst in renames if dst.exists() and dst not in planned_srcs]
        if occupied:
            sample = ", ".join(x.name for x in occupied[:5])
            print(f"[중단] rename 대상 경로가 이미 존재({sample}) — 덮어쓰기 방지 위해 적용 전 "
                  "abort(쓰기 0). 보드 상태 재확인 후 재시도.", file=sys.stderr)
            return 1
        # 이 relabel 이 실제로 만진 경로만 모아 스코프 커밋한다 — rewrite 가 쓴
        # 파일 + rename 의 옛/새 경로. 옛 경로는 더 이상 존재하지 않지만 index 에는 있어
        # `git_scope_stageable` 의 추적 판정이 살려낸다(삭제가 커밋에 실려야 rename 이 완성).
        touched: list[Path] = []
        scale = rewrite_refs(root, id_map, dry_run=False, changed_paths=touched)
        _apply_file_renames(renames)
        for src, dst in renames:
            touched.extend((src, dst))
        _refresh_board_locked()
        if _board_git_enabled():
            _board_git_stage_and_commit(f"{op} {label}", touched)

    if _board_git_enabled():
        if backup_rev:
            print(f"  board-git 백업 rev(되돌리려면 "
                  f"`git -C .project_manager/board reset --hard {backup_rev[:12]}`)")
    else:
        print("  legacy(board-git 미분리) — board 변경은 홈 git 에 함께 있다. "
              "되돌리려면 홈 git 으로 revert.")
    print(f"{op} {label} — {_scale_line(len(id_map), scale, len(renames))} 적용 완료.")
    return 0


def _validate_dst_prefix(raw: str, parsed: str | None) -> str | None:
    """대상 카테고리(dst/into) 형식 sanity + case-only 중복 거부 — 위반 메시지·정상이면 None.

    `none`(parsed=None)은 항상 허용(이름 지우기·무prefix). 실 prefix 는 새/재사용 카테고리
    이름이므로 `_validate_prefix`(예약어+`[A-Za-z0-9][A-Za-z0-9_]*`·대소문자 허용)로
    못박아 malformed ID 발행을 막는다. source prefix 는 *기존* 발행분이라 검증하지 않는다(하이픈
    legacy 존중). 형식 통과 후 dst 가 기존 prefix(등록 ∪ 티켓)에 대소문자만 다르게 fold-매치되면
    fail-loud — `_detect_collisions` 는 case-민감이라 `T-aaa-005` 와 `T-AAA-005` 를 다른 ID 로 봐
    case-분할을 못 잡으므로, 여기서 기존 canonical case 로 안내해 분할을 원천 차단한다.
    """
    if parsed is None:
        return None
    reason = _validate_prefix(raw)
    if reason:
        return reason
    conflict = _case_only_conflict(raw, registered_prefixes() | _existing_ticket_prefixes())
    if conflict is not None:
        return (f"대상 prefix {raw!r} 은 기존 {conflict!r} 과 대소문자만 다르다 "
                f"(prefix 동일성은 case-insensitive·네임스페이스 분할 방지). "
                f"기존 case {conflict!r} 로 지정하라.")
    return None


def cmd_prefix_rename(args: argparse.Namespace) -> int:
    """`prefix rename <A|none> <B|none>` — 무충돌=번호유지 교체·충돌=merge 안내."""
    src = _parse_prefix_arg(args.src)
    dst = _parse_prefix_arg(args.dst)
    if src == dst:
        print(f"[중단] src 와 dst 가 같다({args.src} → {args.dst}) — 변경 없음.",
              file=sys.stderr)
        return 1
    reason = _validate_dst_prefix(args.dst, dst)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot 에서 스캔→맵→collision (TOCTOU 봉합·dry-run 은 락 밖 호출).
        tickets = _scan_prefix_tickets()
        id_map = _rename_map(src, dst, tickets)
        if not id_map:
            print(f"prefix {args.src} 에 해당하는 티켓이 없다 — 변경 없음.")
            return None, 0
        collisions = _detect_collisions(id_map, {t["id"] for t in tickets})
        if collisions:
            sample = ", ".join(collisions[:5]) + (" …" if len(collisions) > 5 else "")
            print(f"[중단] rename 충돌 — {args.dst} 네임스페이스에 번호가 겹친다({sample}). 번호 유지 "
                  f"rename 불가. `board.py prefix merge {args.src} --into {args.dst}` 로 created 순 "
                  f"재번호 통합하라(append·무손실).", file=sys.stderr)
            return None, 1
        return id_map, 0

    return _prefix_relabel(build_map, verb="rename", label=f"{args.src} → {args.dst}",
                           dry_run=bool(getattr(args, "dry_run", False)))


def cmd_prefix_strip(args: argparse.Namespace) -> int:
    """`prefix strip <A>` — `rename <A> none` 의 별칭(별도 로직 없음)."""
    return cmd_prefix_rename(argparse.Namespace(
        src=args.prefix, dst="none", dry_run=getattr(args, "dry_run", False)))


def cmd_prefix_merge(args: argparse.Namespace) -> int:
    """`prefix merge <A> [B...] --into <T|none>` — created 순 통합(기본 append)."""
    sources = [_parse_prefix_arg(s) for s in args.sources]
    into = _parse_prefix_arg(args.into)
    reason = _validate_dst_prefix(args.into, into)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    if _fold_key(into) in {_fold_key(s) for s in sources}:
        # 자기-merge 가드도 case-insensitive — `merge aaa --into AAA` 는 fold-동일
        # 네임스페이스라 자기 자신에 merge 다(source fold-매칭이 노출한 클래스).
        print(f"[중단] --into 대상({args.into})이 source 목록에 있다 — 자기 자신에 merge 불가"
              "(대소문자 무관).", file=sys.stderr)
        return 1
    reorder = bool(getattr(args, "reorder_chronological", False))

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot — merge 의 append start=max(...) 도 stale 이면 clobber.
        tickets = _scan_prefix_tickets()
        id_map = (_merge_reorder_map if reorder else _merge_append_map)(sources, into, tickets)
        if not id_map:
            print("통합할 source 티켓이 없다 — 변경 없음.")
            return None, 0
        collisions = _detect_collisions(id_map, {t["id"] for t in tickets})
        if collisions:
            sample = ", ".join(collisions[:5]) + (" …" if len(collisions) > 5 else "")
            print(f"[중단] merge 충돌(new ID 유일성 위배): {sample}. 적용하지 않음.",
                  file=sys.stderr)
            return None, 1
        return id_map, 0

    mode = "reorder" if reorder else "append"
    label = f"{'+'.join(args.sources)} → {args.into} [{mode}]"
    return _prefix_relabel(build_map, verb="merge", label=label,
                           dry_run=bool(getattr(args, "dry_run", False)))


def _areas_clear_prefix_cell(prefix: str) -> int:
    """areas.md 에서 prefix 셀이 `prefix` 인 행의 그 셀을 빈 값으로 in-place 편집한다(행 보존).

    repo 등록 행 자체는 남기고 prefix 칼럼만 비운다
    동형·무손실. areas write 는 진짜 공유 mutation 이라 board_lock 으로 동시 `areas_append` 와의
    lost-update 를 막는다(`_migrate_areas_apply` 동형). **재진입 금지**: 락 안에서
    board_lock 을 다시 잡는 헬퍼는 부르지 않는다(순수 텍스트 RMW 만). 반환 = 비운 셀(행) 수.

    prefix 칼럼 index 는 헤더에서 얻는다(canonical per-repo 레지스트리). 헤더에 prefix 칼럼이
    없거나 areas.md 부재면 0(무변경). 변경된 행만 canonical `| … |`(areas_append 와 동형)로 재조립
    하고 나머지 줄(헤더·구분선·무관 행·산문)은 원문 그대로 보존한다.
    """
    af = areas_file()
    with board_lock():
        if not af.exists():
            return 0
        text = af.read_text(encoding="utf-8")
        ends_nl = text.endswith("\n")
        header_seen = False
        pidx: int | None = None
        cleared = 0
        out: list[str] = []
        for line in text.splitlines():
            cells = _split_areas_row(line)
            if cells is None:                       # 구분선·빈 줄·비-표 산문
                out.append(line)
                continue
            if not header_seen:                     # 첫 table row = 헤더
                header_seen = True
                lower = [c.lower() for c in cells]
                pidx = lower.index("prefix") if "prefix" in lower else None
                out.append(line)
                continue
            if pidx is not None and pidx < len(cells) and cells[pidx] == prefix:
                cells[pidx] = ""
                cleared += 1
                out.append("| " + " | ".join(cells) + " |")
            else:
                out.append(line)
        if cleared:
            af.write_text("\n".join(out) + ("\n" if ends_nl else ""), encoding="utf-8")
        return cleared


def cmd_prefix_delete(args: argparse.Namespace) -> int:
    """`prefix delete <A>` — 빈(0티켓) prefix 의 areas 등록을 지운다·티켓 있으면 fail-loud.

    prefix 는 티켓으로만 존재하는 작업 카테고리(자동시드 폐지)다. 0 티켓이면:
      - areas.md 에 A 가 등록돼 있으면 → 그 행의 **prefix 셀을 실제로 비운다**(행·repo 등록 보존·
        무손실·② 수동 조치와 동형). promise=do — 메시지가 실제 셀 편집과 일치한다.
      - 미등록이면 → 지울 등록이 없으므로 "확인만"(변경 0)으로 정직하게 보고한다.
    티켓이 있으면 물리삭제 없이 rename/merge 로 안내한다(fail-loud). `--dry-run` 은 쓰기 0·규모
    preview(다른 동사와 공통 표기).
    """
    target = _parse_prefix_arg(args.prefix)
    if target is None:
        print("[중단] none(무prefix) 네임스페이스는 delete 불가 — 기준 네임스페이스다.",
              file=sys.stderr)
        return 1
    dry_run = bool(getattr(args, "dry_run", False))
    # 티켓 존재 카운트는 **case-insensitive fold**(`_fold_key`) — `delete AAA` 가 case-변종
    # `T-aaa-*` 도 세어, fold-비지 않은 네임스페이스를 "빈 것"으로 오판해 등록만 지우는 것을 막는다.
    target_key = _fold_key(target)
    count = sum(1 for t in _scan_prefix_tickets() if _fold_key(t["prefix"]) == target_key)
    if count > 0:
        print(f"[중단] prefix {args.prefix} 에 티켓 {count}개 — delete 는 빈(0티켓) prefix 전용"
              f"(무손실·물리삭제 없음). 개명은 `board.py prefix rename {args.prefix} <B|none>`, "
              f"통합은 `board.py prefix merge {args.prefix} --into <T|none>` 로.",
              file=sys.stderr)
        return 1
    registered = _areas_row_for_prefix(target) is not None
    if not registered:
        print(f"prefix {args.prefix} — 0 티켓·areas 등록 없음. 확인만(변경 없음).")
        return 0
    if dry_run:
        print(f"[dry-run] prefix delete {args.prefix} — 0 티켓·areas 등록 셀 비움 예정"
              f"(행·repo 등록 보존). 쓰기 0 — 적용하려면 --dry-run 없이 재실행.")
        return 0
    cleared = _areas_clear_prefix_cell(target)
    print(f"prefix {args.prefix} — 0 티켓·areas 등록 셀 {cleared}행 비움(행·repo 등록 보존·무손실).")
    return 0


# ── reid — 단일 티켓 ID 재부여  ──────────
# 카테고리 일괄(`prefix` 네임스페이스)과 달리 **티켓 1장**의 오발행 ID(번호·prefix)를 고친다.
# *같은 파이프라인*(`_prefix_relabel`)을 재사용한다 — old→new 맵을
# `{OLD: NEW}` 단일 항으로 세워 토큰단위 rewriter·slug 파일명 rename·홈 git clean 가드·단일
# board_lock·board-git 백업·dry-run 규모 preview 를 그대로 상속한다(새 rewrite 엔진 없음).

def cmd_reid(args: argparse.Namespace) -> int:
    """`reid <OLD-ID> <NEW-ID> [--dry-run]` — 단일 티켓 ID 를 무손실 재부여한다.

    잘못 발행된 티켓 1장의 ID(`T-0036`→`T-0250`·`T-0036`→`T-finance-036`·역방향)를 파일명·
    frontmatter·**전 참조**(board 내 depends_on/blocks·타 티켓 본문·wiki/log wikilink)까지 한 번에
    고친다. 카테고리 일괄이 아니라 단건이므로 top-level 서브커맨드다(`prefix` 네임스페이스는 카테고리
    전용 유지).

    가드(값싼 정적 → 상태 의존 순): NEW-ID 형식 sanity(발행 문법·prefix 자유 입력) → src≠dst → (락
    안 fresh snapshot) OLD 실재 → 타 세션 claim abort(단일세션 op) → NEW collision(전 상태
    디렉토리+.drafts 에 이미 존재하면 abort). 번호 자동발급 카운터는 `_next_id` 가 max 기반이라 어느
    번호로 옮겨도 무충돌이다 — 다음 발급이 최대치를 자연히 이으므로 별도 조정 없이 확인만 한다(결정).
    """
    _reject_task_slot_identity_mix(args)
    old_id, new_id = args.old_id, args.new_id
    dry_run = bool(getattr(args, "dry_run", False))

    # 정적 sanity(락·재조회 불요) — 값싼 거부 먼저. OLD/NEW 형식·자기 자신.
    # OLD-ID 도 형식 선검증: find_ticket 은 glob 기반이라 메타문자(`*`·`?`·`[]`)
    # 든 OLD 가 임의 티켓에 매치된 뒤 rewrite 는 리터럴 키라 no-op 인데 성공처럼 끝날 수 있다.
    if not _is_valid_ticket_id(old_id):
        print(f"[중단] OLD-ID {old_id!r} 형식 위반 — `T-NNNN`(무prefix) 또는 `T-<prefix>-NNN`"
              "(발행 문법) 이어야 한다.", file=sys.stderr)
        return 1
    if not _is_valid_ticket_id(new_id):
        print(f"[중단] NEW-ID {new_id!r} 형식 위반 — `T-NNNN`(무prefix) 또는 `T-<prefix>-NNN`"
              "(발행 문법·prefix 자유 입력) 이어야 한다.", file=sys.stderr)
        return 1
    if old_id == new_id:
        print(f"[중단] OLD 와 NEW 가 같다({old_id}) — 변경 없음.", file=sys.stderr)
        return 1

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 상태 의존 검사는 (적용 경로에선) `_prefix_relabel` 이 잡은 board_lock 안 fresh snapshot 에서
        # 수행한다 — 검사↔적용 사이 cmd_new/claim 창을 봉합한다(prefix rename build_map 동형).
        # OLD 티켓을 canonical ID 로 정확 선택한다. `find_ticket` 은 `{old_id}-*.md` glob 의 *첫*
        # legacy `T-0036` 부재 시 숫자-prefix `T-0036-001-*.md`
        # 가 glob 에 걸려 silent-noop / `T-0036-slug.md` 와 `T-0036-001-slug.md` 공존 시 디렉토리
        # 순서에 따라 후자가 먼저 잡혀 canonical mismatch 로 실재하는 OLD 를 놓침(false-negative).
        # `_scan_prefix_tickets`(fm.get("id") or 파일명 폴백·전 상태 디렉토리+.drafts)로 전수 스캔해
        # canonical ID 가 정확히 old_id 인 레코드만 고른다.
        matches = [t for t in _scan_prefix_tickets() if t["id"] == old_id]
        if not matches:
            print(f"[중단] OLD 티켓 {old_id} 을 찾을 수 없다 — 재부여할 대상이 없다.",
                  file=sys.stderr)
            return None, 2
        if len(matches) > 1:
            # 같은 canonical ID 가 둘 이상 = 이미 오염된 보드 — 임의 선택은 그 자체로 silent-wrong-target.
            dup = ", ".join(t["path"].name for t in matches[:5])
            print(f"[중단] OLD 티켓 {old_id} 가 여러 파일에 존재한다({dup}) — 먼저 중복을 해소하라.",
                  file=sys.stderr)
            return None, 1
        path = matches[0]["path"]
        fm, _ = load_ticket(path)
        # 타 세션 claim 가드 — 단일세션 op(migrate-identity·prefix rename 동류). claim 중인
        # 티켓은 그 소유 세션만 reid 할 수 있다(다른 세션이 작업 중인 ID 를 바꿔 참조를 흔들지 않게).
        # claim/complete/block 이 board_lock-free 라 미세 TOCTOU 는 하드 보장하지 않는다(정직한 한계).
        claimed_by = fm.get("claimed_by")
        if claimed_by:
            claimed_slot = str(claimed_by).rsplit("/", 1)[-1]   # `<user>/<slot>` → slot (또는 slot-only)
            current = session_name(_actor_session_override(args))
            if current is None or claimed_slot != current:
                # remedy 는 canonical `--repo/--slot` 로 안내 — `claimed_slot` 이
                # `<repo>_<N>` 형태면 분해해 그대로 보여주고(정직한 remedy), 아니면(솔로 커스텀
                # 세션명) 소유 세션명만 병기한다(그 형태는 --repo/--slot 로 재현 불가).
                remedy_repo = _repo_from_session(claimed_slot)
                if remedy_repo is not None:
                    remedy_num = claimed_slot.rsplit("_", 1)[-1]
                    remedy = f"--repo {remedy_repo} --slot {remedy_num}"
                else:
                    remedy = f"--repo <repo> --slot <N>(소유 세션 `{claimed_slot}`)"
                print(f"[중단] {old_id} 은 `{claimed_by}` 가 claim 중 — reid 는 단일세션 op 다. 소유 "
                      f"세션에서 `{remedy}` 로 재실행하거나 먼저 unclaim 하라.",
                      file=sys.stderr)
                return None, 1
        # NEW collision — `_detect_collisions` 재사용(zero-pad 폭 정규화 포함). {OLD:NEW} 를 전 티켓
        # ID 집합에 적용해 두 티켓이 같은 논리 ID(전 상태 디렉토리+.drafts)로 떨어지면 잡는다.
        id_map = {old_id: new_id}
        collisions = _detect_collisions(id_map, {t["id"] for t in _scan_prefix_tickets()})
        if collisions:
            sample = ", ".join(collisions[:5])
            print(f"[중단] NEW-ID {new_id} collision — 이미 존재하는 티켓과 번호가 겹친다({sample}). "
                  "미사용 ID 로 재부여하라(`board.py prefix list` 로 현황 확인).", file=sys.stderr)
            return None, 1
        return id_map, 0

    return _prefix_relabel(build_map, verb="", label=f"{old_id} → {new_id}",
                           dry_run=dry_run, noun="reid")


def cmd_show(args: argparse.Namespace) -> int:
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"-- {args.id} ({status}/) --\n")
    print(path.read_text(encoding="utf-8"))
    return 0


# ── idea commands ──────────────────────────────────────────────────────
#
# Ideas are pre-ADR candidates living under ideas/{open,promoted,killed}/.
# They reuse the ticket frontmatter/body helpers (load_ticket / dump_ticket)
# and the generic find_item / move_item / next_numeric_id helpers — only the
# lifecycle differs (no claim/complete; just open → promoted|killed).

# Body skeleton for `idea new`. Mirrors the 권장 섹션 list in ideas/README.md.
_IDEA_BODY_TEMPLATE = """# Idea-{iid} — {title}

## 한 줄 요약

<무엇을 / 왜 끌리는가 1~2 문장>

## 동기

- <왜 이 idea 에 끌리는가>

## 가능한 구현 형태 (high-level)

- <high-level 구현 방향 — 어느 모듈/계층에, 어떤 형태로>

## 위험 / 고민거리

- <검토할 위험>

## 열린 질문

- [ ] <답이 필요한 질문>

## 다음 행동

- promote 기준 / kill 기준 / 어떻게 익힐지

## 관련 링크

- [[xxxxx]]
"""


def cmd_idea_list(args: argparse.Namespace) -> int:
    rows: list[tuple[str, dict]] = []
    for status in IDEA_STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted((IDEAS_DIR / status).glob("[0-9]*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in _tag_values(fm):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no ideas)")
        return 0
    for status, fm in rows:
        tags = ",".join(_tag_values(fm))
        iid = str(fm.get("id") or "")
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:8s}] {iid:6s} {title:60s}  {tags}")
    return 0


def cmd_idea_new(args: argparse.Namespace) -> int:
    iid = _next_idea_id()
    slug = _slugify(args.title)
    filename = f"{iid}-{slug}.md"

    today = datetime.date.today().isoformat()
    fm: dict[str, Any] = {
        "id": iid,
        "title": args.title,
        "created": today,
        "updated": today,
        "type": "idea",
        "status": "open",
        "tags": (args.tag.split(",") if args.tag else []),
    }
    body = "\n" + _IDEA_BODY_TEMPLATE.format(iid=iid, title=args.title)

    path = IDEAS_DIR / "open" / filename
    dump_ticket(path, fm, body)
    print(f"created idea {iid} ({_rel_to_repo(path)})")
    print("  → fill in 한 줄 요약 / 동기 / 위험 / 다음 행동")
    return 0


# Maps an idea's destination status to the imperative verb used in messages.
_IDEA_TRANSITION_VERB = {"promoted": "promote", "killed": "kill"}


def _transition_idea(iid: str, dst_status: str) -> int:
    """Atomic mv open/ → dst_status/ + frontmatter status sync.

    Shared by `idea promote` and `idea kill` — the only transitions ideas
    support. Both move out of `open/` only.
    """
    verb = _IDEA_TRANSITION_VERB[dst_status]
    try:
        status, path = find_idea(iid)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot {verb} idea {iid}: currently in {status}/",
              file=sys.stderr)
        return 1
    fm, body = load_ticket(path)
    new_path = move_idea(path, dst_status)
    fm["status"] = dst_status
    fm["updated"] = datetime.date.today().isoformat()
    dump_ticket(new_path, fm, body)
    print(f"{dst_status} idea {iid} ({_rel_to_repo(new_path)})")
    return 0


def cmd_idea_promote(args: argparse.Namespace) -> int:
    return _transition_idea(args.id, "promoted")


def cmd_idea_kill(args: argparse.Namespace) -> int:
    return _transition_idea(args.id, "killed")


def cmd_refresh(_args: argparse.Namespace) -> int:
    refresh_board()
    print(f"board refreshed: {_rel_to_repo(BOARD_FILE)}")
    issues = lint_tickets()
    if issues:
        print(f"⚠️  {len(issues)} lint issue(s) — run `board.py lint` for detail",
              file=sys.stderr)
    return 0


def cmd_git_anchor(args: argparse.Namespace) -> int:
    """하네스 훅용 JSON 판정 표면. 외부 상태 mutation 없이 한 줄 JSON만 출력한다."""
    print(json.dumps(judge_git_anchor_command(args.cwd, args.command), ensure_ascii=False))
    return 0


def _run_lint_hooks() -> list[tuple[str, str]]:
    """Discover & run instance lint hooks — .project_manager/hooks/lint_*.py.

    각 훅 모듈은 `check() -> list[str]`(이슈 detail 문자열)을 노출한다. fail-soft:
    로드/실행 실패·규격 불충족은 stderr 경고로 보고하고 계속한다(부분 실패가 lint 전체를
    막지 않음). 인스턴스가 엔진 board.py 를 안 건드리고 자기 검사를 더하는 seam — 프레임워크
    공통 검사(wikilink 등)는 엔진 내장(lint_wikilinks), 프로젝트 고유 검사는 여기로.
    """
    if not HOOKS_DIR.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for hook in sorted(HOOKS_DIR.glob("lint_*.py")):
        try:
            mod = _load_module_from_path(
                hook, hook.name, allow_unverified=True,
            )
            check = getattr(mod, "check", None)
            if not callable(check):
                print(f"⚠️  lint hook {hook.name}: check() 미정의 — 건너뜀", file=sys.stderr)
                continue
            for detail in (check() or []):
                out.append((hook.stem, str(detail)))
        except Exception as exc:  # noqa: BLE001 — fail-soft: 한 훅 실패가 lint 를 막지 않음
            print(f"⚠️  lint hook {hook.name} 로드/실행 실패: {exc}", file=sys.stderr)
    return out


def cmd_lint(args: argparse.Namespace) -> int:
    """전체 lint 보고 (무인자) 또는 push 게이트 (`--gate`).

    무인자: 모든 issue 를 보고하고 issue 가 하나라도 있으면 종료코드 1 (현행 동작).
    `--gate`: 종료코드를 *차단 카테고리*에만 1 로 둔다 — status drift 같은 자문성
    (lint_status 의 "never blocks" 보장) 은 보고는 하되 종료코드에 반영하지 않는다.
    즉 `--gate` 는 pre-push 게이트용 엄격 부분집합이다.
    """
    gate = getattr(args, "gate", False)
    issues = lint_tickets()
    hook_issues = _run_lint_hooks()
    total = len(issues) + len(hook_issues)
    if total == 0:
        print("✓ no lint issues")
        return 0
    # 차단 카테고리 = 자문성(status drift) 제외 전부 + 모든 instance 훅 issue.
    blocking = [i for i in issues if i[1] not in _ADVISORY_LINT_KINDS]
    block_count = len(blocking) + len(hook_issues)
    label = "blocking " if gate else ""
    print(f"⚠️  {total} lint issue(s) ({block_count} {label}차단):"
          if gate else f"⚠️  {total} lint issue(s):")
    for ticket_id, kind, detail in issues:
        mark = " " if (kind in _ADVISORY_LINT_KINDS) else "✗"
        prefix = f"  {mark} " if gate else "  "
        print(f"{prefix}[{kind}] {ticket_id}: {detail}")
    for hook_name, detail in hook_issues:
        prefix = "  ✗ " if gate else "  "
        print(f"{prefix}[{hook_name}] {detail}")
    if gate:
        return 1 if block_count > 0 else 0
    return 1


def _rel_to_repo(path: Path) -> str:
    """Best-effort pretty path. Falls back to absolute when path is outside REPO
    (e.g. in unit tests using tmp_path)."""
    try:
        return str(path.relative_to(REPO))
    except ValueError:
        return str(path)


# ── lint ───────────────────────────────────────────────────────────────

def _all_tickets() -> list[tuple[str, dict]]:
    """[(status, frontmatter), ...] for every ticket regardless of dir."""
    out: list[tuple[str, dict]] = []
    for status in STATUS_DIRS:
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            out.append((status, fm))
    return out


def _find_cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    """Return circular paths in a directed graph.

    Each cycle is a node list closed on itself, e.g. ['A', 'B', 'A'].
    Cycles sharing the same node set are reported once.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in graph}
    stack: list[str] = []
    cycles: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for nxt in graph.get(node, []):
            if color.get(nxt, WHITE) == GRAY:          # back edge → cycle
                cyc = stack[stack.index(nxt):] + [nxt]
                key = frozenset(cyc)
                if key not in seen:
                    seen.add(key)
                    cycles.append(cyc)
            elif color.get(nxt, WHITE) == WHITE:
                dfs(nxt)
        stack.pop()
        color[node] = BLACK

    for n in graph:
        if color[n] == WHITE:
            dfs(n)
    return cycles


def lint_dependencies() -> list[tuple[str, str, str]]:
    """Return list of (ticket_id, issue_kind, detail).

    Checks:
      - unknown:        depends_on / blocks references a non-existent ticket
      - self-reference: ticket lists its own ID in depends_on or blocks
      - asymmetric:     A.blocks contains B but B.depends_on does not contain A
      - cycle:          depends_on graph contains a circular path
    """
    tickets = {fm["id"]: (status, fm) for status, fm in _all_tickets()}
    issues: list[tuple[str, str, str]] = []

    for tid, (_status, fm) in tickets.items():
        deps = list(fm.get("depends_on") or [])
        blocks = list(fm.get("blocks") or [])

        # self-reference
        if tid in deps:
            issues.append((tid, "self-reference",
                           "depends_on contains itself"))
        if tid in blocks:
            issues.append((tid, "self-reference",
                           "blocks contains itself"))

        # unknown reference
        for ref in deps:
            if ref != tid and ref not in tickets:
                issues.append((tid, "unknown",
                               f"depends_on references missing {ref}"))
        for ref in blocks:
            if ref != tid and ref not in tickets:
                issues.append((tid, "unknown",
                               f"blocks references missing {ref}"))

        # asymmetric blocks ↔ depends_on
        for ref in blocks:
            if ref == tid or ref not in tickets:
                continue
            other_fm = tickets[ref][1]
            other_deps = list(other_fm.get("depends_on") or [])
            if tid not in other_deps:
                issues.append((tid, "asymmetric",
                               f"blocks {ref} but {ref}.depends_on lacks {tid}"))

    # circular depends_on — self-references are handled above and excluded here
    graph = {
        tid: [d for d in (fm.get("depends_on") or [])
              if d in tickets and d != tid]
        for tid, (_status, fm) in tickets.items()
    }
    for cycle in _find_cycles(graph):
        issues.append((cycle[0], "cycle",
                       f"circular depends_on: {' → '.join(cycle)}"))

    return issues


# Unfilled `_template.md` text — its presence means the ticket is still a stub.
# The `## 메모` placeholder is intentionally NOT listed: that section is a work
# journal filled at completion time, so an empty 메모 is normal for a complete,
# claimable ticket and must not count as "thin".
#
# 각 항목은 `_template.md` 의 절별 뼈대 문장 리터럴이라 *채우면 사라진다* — 실 본문엔 안 나타나
# 오탐 0(placeholder 잔존만 잡는다). promote 게이트가
# 이 집합을 재사용(`_body_lint_issues` 단일 깔때기)하므로, 뼈대만 채운 절(인터페이스·결정·참고
# pattern-reference)까지 걸러야 "자족성 = placeholder 0" 이 성립한다. 목표/DoD 뿐 아니라
# 인터페이스·결정·참고 절의 미충전도 여기서 promote-차단된다.
_PLACEHOLDERS: tuple[str, ...] = (
    "무엇을 만들 / 바꿀 / 검증할지",      # ## 목표 절 미충전
    "이 ticket 이 만들거나 바꾸는",        # ## 인터페이스 절 미충전
    "구현 방향에 대한 확정 사항",           # ## 결정 절 미충전
    "핵심 산출물 (파일, 동작)",            # ## 완료 조건(DoD) 미충전
    "[[architecture]] 관련 절",            # ## 참고 절 미충전
    "[[xxxxx]]",                           # ## 참고 ADR/spec 미충전
    "T-XXXX",                              # ## 참고 pattern-reference 미충전
    "<제목>",                              # 제목 미치환
)
_REQUIRED_SECTIONS: tuple[str, ...] = ("## 목표", "## 완료 조건", "## 참고")
# authoring 게이트(`cmd_new` 발행·`cmd_promote` 승격) 전용 strict 절 집합 — 자족성이
# 요구하는 5절(목표/인터페이스/결정/DoD/참고) 전부의 *존재*를 강제한다. placeholder 검사는 절을
# **통째로 삭제한** 회피(뼈대 문장이 없으니 잔존 토큰도 없음)를 못 잡으므로,
# 절 자체의 부재를 thin 으로 세운다. 전역 lint(`lint_bodies`)는 레거시 blast-radius(인터페이스/결정
# 절 없는 기존 open/claimed 티켓이 blocking 화) 때문에 3절 불변(`_REQUIRED_SECTIONS`)을 유지하고,
# 이 strict 집합은 authoring **두 소비 지점**만 `strict_sections=True` 로 opt-in 한다(단일 깔때기·
# 소비측 파라미터 — 판정 로직은 한 함수).
_STRICT_REQUIRED_SECTIONS: tuple[str, ...] = (
    "## 목표", "## 인터페이스", "## 결정", "## 완료 조건", "## 참고")


def _body_lint_issues(tid: str, body: str, *,
                      strict_sections: bool = False) -> list[tuple[str, str, str]]:
    """단일 티켓 본문의 self-containment issue — `lint_bodies` 와 authoring 게이트가 공유.

    `lint_bodies` 의 검사 로직(placeholder·thin)을 단일-티켓 단위로 추출한 것 — `board.py new`
    발행 게이트·`board.py promote` 승격 게이트가 방금/승격 대상 티켓 하나를 즉석 검사할 때 재사용한다.

    `strict_sections`: authoring 게이트(발행·승격)만 True — 5절(목표/인터페이스/결정/DoD/참고) 전부의
    존재를 강제한다(절 삭제 회피 차단). False(기본·전역 `lint_bodies` 경로)는 3절 불변
    (`_REQUIRED_SECTIONS`)만 요구 — 인터페이스/결정 절 없는 레거시 티켓의 blast-radius 회피.
    placeholder 검사는 두 모드 공통(단일 규칙).
    """
    issues: list[tuple[str, str, str]] = []
    prose = _strip_code(body)
    for placeholder in _PLACEHOLDERS:
        if placeholder in prose:
            issues.append((tid, "placeholder",
                           f"unfilled template text: {placeholder!r}"))
    required = _STRICT_REQUIRED_SECTIONS if strict_sections else _REQUIRED_SECTIONS
    for section in required:
        if section not in body:
            issues.append((tid, "thin",
                           f"missing standard section: {section}"))
    return issues


def lint_bodies() -> list[tuple[str, str, str]]:
    """Lint open/claimed ticket bodies for self-containment.

    Checks:
      - placeholder: unfilled `_template.md` text still present as prose
      - thin:        a standard section (목표 / 완료 조건 / 참고) is missing

    done/blocked tickets are skipped — only live, claimable work is gated.
    """
    issues: list[tuple[str, str, str]] = []
    for status in ("open", "claimed"):
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, body = load_ticket(p)
            tid = fm.get("id") or p.name
            issues.extend(_body_lint_issues(tid, body))
    return issues


def lint_ideas() -> list[tuple[str, str, str]]:
    """Lint ideas for frontmatter `status` ↔ directory agreement.

    The directory is the source of truth; a mismatched frontmatter `status`
    means a manual `mv` bypassed board.py (drift — see ideas/README.md).
    """
    issues: list[tuple[str, str, str]] = []
    for status in IDEA_STATUS_DIRS:
        for p in sorted((IDEAS_DIR / status).glob("[0-9]*.md")):
            fm, _ = load_ticket(p)
            iid = fm.get("id") or p.name
            fm_status = fm.get("status")
            if fm_status != status:
                issues.append((iid, "idea-status",
                               f"in {status}/ but frontmatter status={fm_status!r}"))
    return issues


# status.md ✅ 완성 행 누적 임계값 (warn-only — 차단 아님·archive 권고). 활성 매트릭스 = 진행 중만.
# 남은 가드는 ✅ 누적뿐.
STATUS_DONE_ROW_WARN = 30

# 모듈 매트릭스 행 중 상태 셀이 ✅ 인 행 (범례 "- ✅ ..." 는 `|` 시작 아니라 제외).
_STATUS_DONE_ROW_RE = re.compile(r"^\|.*\| ✅ \|", re.MULTILINE)

def lint_status() -> list[tuple[str, str, str]]:
    """status.md 의 ✅ 완성 행 누적을 경고한다 (warn-only·judgment-only status).

    Checks:
      - status-done-accum: 활성 매트릭스에 ✅ 완성 행이 누적 — status_done.md 로 archive 권고.

    (status.md 헤더 scalar·테스트 수·합계·소계·회귀 실측은 derivable 이라
    제거됐다 — 따라서 `status-header-bloat` 가드와 ticket_finish 스칼라 앵커 무결성 검사
    `lint_status_anchors` 도 같이 제거. status.md = judgment-only.)

    status.md 없으면 빈 리스트. (board.py refresh/lint 끝에서 호출.)
    """
    issues: list[tuple[str, str, str]] = []
    if not STATUS_FILE.exists():
        return issues
    text = STATUS_FILE.read_text(encoding="utf-8")

    done_rows = len(_STATUS_DONE_ROW_RE.findall(text))
    if done_rows > STATUS_DONE_ROW_WARN:
        issues.append((
            "status.md", "status-done-accum",
            f"활성 매트릭스 ✅ 완성 행 {done_rows}개 > {STATUS_DONE_ROW_WARN} — "
            f"status_done.md 로 archive 권고"))

    return issues


# ── family wiki scope 태그 + 승격 ─────────────────────────────
# multi-PM wiki 하나 + repo 전용 문서를 `family_scope:` frontmatter 태그로 구분한다.
#   - 값 = `shared`(기본) / repo 명(areas.md 의 등록 prefix). 부재 → shared 로 간주.
#   - "완료 시 공유" = 물리 머지 아니라 scope 승격(`repoA → shared` retag·idea-promote 동형).
#   - `board.py lint` 가 family_scope 를 *인지*(파싱·기본 shared)하되 차단은 최소 —
#     알 수 없는 형식만 자문성 권고(`scope-advice`·never-blocks). scope 자체로 hard-fail 안 함.
#
# 키 선택(`family_scope:` ≠ `scope:`): 기존 ADR frontmatter 의 `scope:` 는 이미 문서 전략
# 분류(`mission`·`internal-process`)로 점유돼 있어, 같은 키에 repo 네임스페이스를 얹으면 기존
# 의미를 깨고 오탐을 부른다. family wiki scope 는 전용 키 `family_scope:` 로 박제해 두 의미체계를
# 분리한다 — 솔로(키 부재) 회귀 0.

FAMILY_SCOPE_DEFAULT = "shared"  # family_scope 부재/빈값 → shared 로 간주.
# family_scope 가 인지되는 wiki 디렉토리 — ADR(decisions/)·spec(specs/).
_SCOPE_AWARE_DIRS: tuple[Path, ...] = (DECISIONS_DIR, SPECS_DIR)
# 유효 family_scope 값 형식 — `shared` 또는 prefix 형(영숫자·`-`·`_`, 등록 prefix 와 동형).
# 형식만 검사(등록 여부는 advisory 메시지로) — areas.md 부재인 솔로에서도 동작.
_FAMILY_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _in_scope_aware_dir(path: Path) -> bool:
    """path 가 scope-aware 디렉토리(decisions/·specs/) 안에 있는가.

    promote-scope 가 ADR/spec 문서로만 retag 를 제한하기 위한 가드— 임의
    frontmatter 문서 retag 를 거부한다. 경로 비교 전 resolve 된 절대경로를 받는다.
    """
    resolved = path if path.is_absolute() else (REPO / path).resolve()
    for base in _SCOPE_AWARE_DIRS:
        try:
            base_resolved = base.resolve()
        except OSError:
            continue
        if resolved == base_resolved or base_resolved in resolved.parents:
            return True
    return False


def family_scope(fm: dict[str, Any]) -> str:
    """frontmatter dict 의 family wiki scope. 부재/빈값 → shared.

    `family_scope:` 값을 strip 해 반환한다. 없거나 빈 문자열이면 `shared` 기본
    비-문자열(잘못 적힌 list 등)도 shared 로
    안전 폴백 — 파싱이 절대 예외를 던지지 않게 한다(lint fail-soft).
    """
    raw = fm.get("family_scope")
    if not isinstance(raw, str):
        return FAMILY_SCOPE_DEFAULT
    val = raw.strip()
    return val or FAMILY_SCOPE_DEFAULT


def lint_scopes() -> list[tuple[str, str, str]]:
    """family_scope 태그를 파싱·인지한다 (kind=`scope-advice`·자문성).

    decisions/·specs/ 문서의 `family_scope:` 를 읽어 *인지*한다(부재 → shared 기본).
    차단은 최소 — 다음만 자문성 권고(never-blocks·`_ADVISORY_LINT_KINDS`):
      - 비문자열 family_scope (list/dict/number 등 — frontmatter 형식 오류).
      - 형식이 깨진 scope (공백/특수문자 등 `_FAMILY_SCOPE_RE` 불일치).
      - shared 도 아니고 areas.md 의 등록 prefix 도 아닌 미지의 repo scope (오타 신호).
        단 areas.md 부재(솔로)면 등록 대조를 건너뛴다 — 솔로에서 repo scope 는 미래값일 뿐.
    scope 자체로 hard-fail 을 만들지 않는다. 솔로
    (family_scope 부재) 에선 항상 빈 리스트 — 회귀 0.

    *원본 값* 을 검사한다(파싱 헬퍼 `family_scope()` 의 fail-soft 폴백과 분리) — 헬퍼는
    비문자열을 shared 로 안전 폴백하지만, lint 는 그 형식 오류를 조용히 삼키지 않고
    `scope-advice` 로 권고해야 한다.
    """
    issues: list[tuple[str, str, str]] = []
    known = registered_prefixes()  # areas.md 부재면 빈 set → 등록 대조 생략.
    for base in _SCOPE_AWARE_DIRS:
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.md")):
            try:
                fm, _body = load_ticket(p)
            except (ValueError, OSError, yaml.YAMLError):
                # frontmatter 없음(README) 또는 placeholder({{DATE}} 등 비-YAML) — scope 인지
                # 대상 아님(fail-soft). 한 문서 파싱 실패가 lint 전체를 막지 않게 흡수한다.
                continue
            if not isinstance(fm, dict):
                continue  # frontmatter 가 스칼라/리스트 — scope 인지 대상 아님.
            if "family_scope" not in fm:
                continue  # 부재 = shared 기본 — 정상, 보고 없음.
            raw = fm.get("family_scope")
            src = _rel_to_repo(p)
            if not isinstance(raw, str):
                # list/dict/number 등 — frontmatter 형식 오류. 헬퍼는 shared 폴백하지만
                # lint 는 조용히 삼키지 않고 권고한다.
                issues.append((src, "scope-advice",
                               f"family_scope 가 비문자열({type(raw).__name__}) — "
                               f"`shared` 또는 repo prefix 문자열이어야 함"))
                continue
            if not raw.strip():
                continue  # 빈값 = shared 기본 — 정상, 보고 없음.
            scope = raw.strip()
            if not _FAMILY_SCOPE_RE.match(scope):
                issues.append((src, "scope-advice",
                               f"family_scope={scope!r} 형식이 깨짐 — `shared` 또는 "
                               f"repo prefix 여야 함"))
            elif scope != FAMILY_SCOPE_DEFAULT and known and scope not in known:
                issues.append((src, "scope-advice",
                               f"family_scope={scope!r} 가 등록된 repo prefix 아님 "
                               f"(areas.md: {sorted(known)}) — 오타 또는 승격 누락 가능 "
                               f""))
    return issues


# 승격 destination 으로 허용할 약식 (board.py promote-scope <file> --to <scope>).
# 임의 repo prefix 도 허용하되, 형식 검증(`_FAMILY_SCOPE_RE`)은 통과해야 한다.
def cmd_promote_scope(args: argparse.Namespace) -> int:
    """family_scope retag — `repoA → shared` 등 scope 값을 교체한다 (idea-promote 동형).

    "완료 시 공유" = 물리 머지 아니라 scope 승격. 대상 문서(decisions/·specs/ 의
    .md)의 frontmatter `family_scope:` 를 `--to` 값으로 교체(부재면 신규 기록)한다. 단순·최소 —
    파일 한 개 retag. `--to` 형식은 `_FAMILY_SCOPE_RE` 로 검증한다(깨진 값 차단). 대상은
    scope-aware 디렉토리(decisions/·specs/) 안이어야 한다
    승격 명령이므로 임의 frontmatter 문서 retag 는 거부한다.
    """
    target = args.file
    new_scope = args.to.strip()
    if not _FAMILY_SCOPE_RE.match(new_scope):
        print(f"invalid --to scope {new_scope!r}: `shared` 또는 repo prefix "
              "(영숫자·-·_) 여야 함.", file=sys.stderr)
        return 1
    path = Path(target)
    if not path.is_absolute():
        path = (REPO / target).resolve()
    else:
        path = path.resolve()
    if not _in_scope_aware_dir(path):
        print(f"refusing to retag {_rel_to_repo(path)}: scope 승격은 decisions/·specs/ "
              "문서만 대상 (ADR/spec scope 승격 명령).", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"file not found: {_rel_to_repo(path)}", file=sys.stderr)
        return 2
    try:
        fm, body = load_ticket(path)
    except ValueError as exc:
        print(f"cannot retag {_rel_to_repo(path)}: {exc}", file=sys.stderr)
        return 1
    old_scope = family_scope(fm)
    if old_scope == new_scope and isinstance(fm.get("family_scope"), str):
        print(f"family_scope already {new_scope!r} — no change ({_rel_to_repo(path)})")
        return 0
    fm["family_scope"] = new_scope
    dump_ticket(path, fm, body)
    print(f"promoted scope {old_scope!r} → {new_scope!r} ({_rel_to_repo(path)})")
    return 0


# ── wikilink lint ───────────────────────────────────────────
# 엔진은 *구조적으로 해석 가능한* 참조만 검증한다: [[ADR-NNNN]]·[[T-NNNN]]/[[T-PFX-NNN]]·
# [[idea-NNNN]] 가 실제 파일로 resolve 되는지. 자유어휘([[some-memory-slug]] 등)는 프로젝트마다
# 화이트리스트가 달라 엔진이 판정할 수 없으므로 건드리지 않는다(오탐 0) — 프로젝트 고유 링크 검사는
# lint 훅(.project_manager/hooks/lint_*.py)으로 분리. placeholder([[T-NNNN]]·[[xxxxx]])는
# 숫자 패턴이 아니라 자연히 제외된다.

# [[name]] 또는 alias [[name|display]] — name 만 캡처. backtick 안도 포함.
_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_\s.\-]+?)(?:\|[^\]]+)?\]\]")

# 어댑터 scaffold 경로 — fresh adopter 에 출하되는 harness 어댑터(.claude/.opencode).
# 채택자(특히 framework ADR 0001~ 이 없는 다운스트림 앱)는 자기 repo 의 scaffold 에서
# framework ADR/idea 를 [[bracket]] 참조하면 *영구 dangling* 이 된다 — 이는 정상이며
# push 를 막아선 안 된다("차단은 최소·advisory 우선"). `_collect_wikilink_files`
# 의 scaffold rel 목록과 동일 — POSIX 경계로 비교(_rel_to_repo 는 `/` 정규화).
# `.opencode/command/` **legacy-compat 로 유지** — 은퇴 전 import 한
# 채택자 트리엔 command 파일이 잔존하고(pm_update 는 복사만·은퇴 경로 삭제 안 함), 여기서 빼면 그
# 파일들의 framework wikilink 가 scaffold-advisory 대신 blocking dangling 으로 오분류된다.
_SCAFFOLD_PATH_PREFIXES: tuple[str, ...] = (
    ".claude/agents/", ".claude/skills/", ".opencode/agents/", ".opencode/command/")


def _is_scaffold_src(src: str) -> bool:
    """src(`_rel_to_repo` 결과)가 어댑터 scaffold 경로 하위인지 — `\\`→`/` 정규화 후 prefix 매칭."""
    norm = src.replace("\\", "/")
    return norm.startswith(_SCAFFOLD_PATH_PREFIXES)


def _collect_wikilink_files() -> list[Path]:
    """wikilink 검사 대상 .md — wiki/ 전체 + 레포 루트 CLAUDE.md·README.md + 어댑터 scaffold.

    어댑터 scaffold(`.claude/{agents,skills}`·`.opencode/{agents,command}`)도 스캔한다 —
    fresh adopter 엔 framework ADR/ticket 이 없으므로, 출하 scaffold 의 `[[ADR-NNNN]]` 같은
    구조참조 wikilink 가 그대로 새 나가면 fresh-clone 에서 dangling 이 된다. 가드가 wiki/ 만
    보던 동안 이 scaffold dangling 은 *구조적으로* 안 잡혔다(scaffold ref 를 늘림).
    각 dir 은 harness 별로 존재 여부가 다르므로(claude 채택자엔 `.opencode` 부재·역도 마찬가지)
    `.is_dir()` 가드로 있을 때만 추가한다.
    """
    # PM 운영 wiki는 등록 worktree read 폴백에서 소유 PM 홈을 따른다. 루트 README와
    # adapter scaffold는 아래에서 계속 REPO(현재 제품 worktree)를 읽어 코드 lint 의미를 보존한다.
    pm_repo = _READ_PM_HOME_OVERRIDE or REPO
    wiki = pm_repo / ".project_manager" / "wiki"
    files: list[Path] = list(wiki.rglob("*.md")) if wiki.is_dir() else []
    # board/ 분리 시 ticket 본문이 wiki/ 밖(board/tickets)으로 빠진다 — 그러면
    # ticket 의 `[[ADR-NNNN]]` 구조참조가 wiki-only 스캔에선 안 보여 dangling 이 *미검출*된다.
    # tickets_dir() 를 union 해 두 루트를 모두 본다. legacy(board_root==wiki)면 이 경로는 이미
    # wiki.rglob 에 포함되므로 set dedup 으로 no-op(중복 0). board/areas.md 등 비-md 는 제외.
    tk = tickets_dir()
    if tk.is_dir():
        files.extend(tk.rglob("*.md"))
    files = list(dict.fromkeys(files))  # 순서보존 dedup (legacy 중복 제거 + board union 합집합)
    for name in ("CLAUDE.md", "README.md"):
        p = REPO / name
        if p.exists():
            files.append(p)
    # `.opencode/command` = 은퇴 경로·legacy-compat 스캔 유지(_SCAFFOLD_PATH_PREFIXES 주석).
    for rel in (".claude/agents", ".claude/skills", ".opencode/agents", ".opencode/command"):
        d = REPO / rel
        if d.is_dir():
            files.extend(d.rglob("*.md"))
    return files


def _leading_num(filename: str) -> str | None:
    """파일명 선두 숫자를 0-strip 정규화해 반환 ('0028-foo.md' → '28'). 없으면 None."""
    m = re.match(r"(\d+)", filename)
    return (m.group(1).lstrip("0") or "0") if m else None


def _resolve_wikilink_targets() -> tuple[set[str], set[str], set[str]]:
    """(ticket_ids, adr_nums, idea_nums) — 실재하는 구조 참조 대상 집합."""
    ticket_ids = {fm.get("id") for _s, fm in _all_tickets() if fm.get("id")}
    adr_nums: set[str] = set()
    if DECISIONS_DIR.is_dir():
        for p in DECISIONS_DIR.glob("[0-9]*.md"):
            n = _leading_num(p.name)
            if n is not None:
                adr_nums.add(n)
    idea_nums: set[str] = set()
    for status in IDEA_STATUS_DIRS:
        for p in (IDEAS_DIR / status).glob("[0-9]*.md"):
            n = _leading_num(p.name)
            if n is not None:
                idea_nums.add(n)
    return ticket_ids, adr_nums, idea_nums


def lint_wikilinks() -> list[tuple[str, str, str]]:
    """Return dangling [[name]] for *structural* refs (ADR/ticket/idea) only.

    name 으로 dedupe 하고 사용처를 detail 에 모은다. 자유어휘는 검사하지 않는다.
    코드 span/fence 안의 *예시* wikilink(관례 문서가 backtick 으로 보여주는
    `[[ADR-NNNN]]`)는 실 참조가 아니므로 `_strip_code` 로 제거 후 스캔한다 —
    `lint_unstable_refs` 와 동일한 처리(오탐 0).

    kind 분류:
      - `dangling-wikilink`          = wiki/·root-doc(CLAUDE.md·README) 의 framework ADR/idea
        dangling, 그리고 **모든 ticket(`[[T-...]]`) dangling** — `lint --gate` 차단(blocking).
      - `dangling-wikilink-scaffold` = framework ADR/idea dangling 이 *오직* 어댑터 scaffold
        경로(`.claude/{agents,skills}`·`.opencode/{agents,command}`)에서만 등장 — advisory
        (`_ADVISORY_LINT_KINDS` 등재·`--gate` 미차단). 채택자(framework ADR 부재 다운스트림)의
        scaffold bracket-ref 는 영구 dangling 이 정상이라 push 를 막으면 안 된다.
    같은 ref 가 scaffold + wiki/root-doc 양쪽에서 dangle 하면 blocking 유지(프레임워크 자기
    문서는 dangle 하면 안 됨). per-occurrence source 경로를 추적해 분기한다(name 별로 ADR/idea
    여부 + 사용처 전부가 scaffold 인지).
    """
    ticket_ids, adr_nums, idea_nums = _resolve_wikilink_targets()
    # name → (is_ticket, [source rel paths]). is_ticket=True 면 항상 blocking,
    # False(ADR/idea)면 사용처가 전부 scaffold 일 때만 advisory 강등.
    dangling: dict[str, tuple[bool, list[str]]] = {}

    for path in _collect_wikilink_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        src = _rel_to_repo(path)
        text = _strip_code(text)
        for raw in _WIKILINK_RE.findall(text):
            name = raw.strip()
            m_adr = re.fullmatch(r"ADR-(\d+)", name)
            m_idea = re.fullmatch(r"idea-(\d+)", name)
            is_ticket = False
            if m_adr:
                ok = (m_adr.group(1).lstrip("0") or "0") in adr_nums
            elif re.fullmatch(_TICKET_ID_BODY, name):
                # prefixed(`T-PAY-001`)·legacy(`T-0164`) wikilink 둘 다 ticket 참조로 본다
                # (multi-repo 보드). grammar 는 `_TICKET_ID_BODY` 공유(자체 regex 금지).
                ok = name in ticket_ids
                is_ticket = True
            elif m_idea:
                ok = (m_idea.group(1).lstrip("0") or "0") in idea_nums
            else:
                continue  # 자유어휘 — 엔진 판정 안 함
            if ok:
                continue
            _t, srcs = dangling.setdefault(name, (is_ticket, []))
            if src not in srcs:
                srcs.append(src)

    issues: list[tuple[str, str, str]] = []
    for name in sorted(dangling):
        is_ticket, srcs = dangling[name]
        shown = ", ".join(srcs[:3]) + (f" (외 {len(srcs) - 3}개)" if len(srcs) > 3 else "")
        # ticket dangling 은 항상 blocking. ADR/idea 는 사용처가 *전부* scaffold 일 때만
        # advisory 강등 — 하나라도 wiki/root-doc 이면 framework 자기 문서 dangle 이라 blocking.
        scaffold_only = (not is_ticket) and all(_is_scaffold_src(s) for s in srcs)
        if scaffold_only:
            issues.append((name, "dangling-wikilink-scaffold",
                           f"[[{name}]] 대상 파일 없음 (어댑터 scaffold 참조 · 채택자 "
                           f"decisions/ 에 framework ADR 부재 = 정상) · 사용처: {shown}"))
        else:
            issues.append((name, "dangling-wikilink",
                           f"[[{name}]] 대상 파일 없음 · 사용처: {shown}"))
    return issues


# ── render-leak (리터럴 `{{...}}` 누출 차단) ──────────────────
# @render 어댑터 파일 = render_adapter 산출물(operational 토큰 치환). half-rendered
# 토큰(`{{...}}` 잔존)이 *출하 산출물* 에 새 나가면 harness-load 에이전트 지시가 무음 열화하므로
# 실결함 — blocking(경고 아님).
#
# 스캔 대상 = **@render manifest path 의 산출물**
#    .opencode/agents·command). 토큰은 @render 산출물 path 에서만 leak 으로 간주된다 —
#    pm_render(post-render assertion) + 이 lint(상시 backstop)가 함께 자족성을 보증한다.

# leak 스캔 토큰 — 대문자/언더스코어 placeholder (`{{PROJECT_NAME}}`·`{{PROTECTED_PATHS}}` 등).
# pm_render._ANY_TOKEN_RE 와 동형(소문자/공백 토큰은 산문이라 제외·오탐 0).
_RENDER_TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _render_managed_relpaths() -> set[str]:
    """engine.manifest 에서 `@render` 태그가 붙은 path 들(repo 기준 relpath·POSIX) — 검사 대상.

    pm_update.read_manifest 를 재사용해 `.render` 플래그가 True 인 항목만 모은다. manifest
    부재·로드 실패는 빈 set(검사 대상 0·무발화). manifest 의 @render path 가 디렉토리면 그
    하위 출하 어댑터가 전부 산출물이므로 prefix 매칭에 쓴다.

    **트리 성격 게이트 (local.conf 의미론)**: render-leak 은 *렌더
    산출물*(operational 토큰이 concrete 로 치환된 어댑터)의 미해소 토큰을 잡는 가드다. 그런데
    토큰-form *소스 트리*(canonical worktree)는 산출물이 아니라 출하 전 원본이라 토큰이 정상
    이다. local.conf 부재 ⟺ 소스 트리(채택/init 전), 존재 ⟺ 채택 인스턴스(render 산출물 보유)
    이므로, local.conf 가 파일로 없으면 검사 대상 0(무발화)으로 잘라낸다 — `.opencode`(templates
    =소스)가 스캔에서 빠지는 것의 *트리-단위 일반화*. 이로써 루트 manifest 가 `.claude/* @render`
    여도 worktree(local.conf 부재)에선 토큰-form 어댑터를 산출물로 오인하지 않는다.
    ⚠️ 단 도그푸딩 worktree(adopter#0)는 local.conf 를 **보유**해 이 트리 게이트만으론 부족하다 —
    그 트리의 토큰-form 출하 원본 면제는 `_template_mirror_state` 가 파일 단위로 보완한다.
    """
    if not (REPO / ".project_manager" / "local.conf").is_file():
        return set()  # 토큰-form 소스 트리(local.conf 부재) — render 산출물 아님.
    return _manifest_render_relpaths()


def _manifest_render_relpaths() -> set[str]:
    """engine.manifest 의 `@render` dest 경로 집합 (repo 기준 relpath·POSIX·local.conf 게이트 무관).

    순수 manifest 파생 — render-leak(`_render_managed_relpaths` 가 트리성격 게이트 후 호출)과
    un-migrated-overlay(`_collect_overlay_adapter_files` 가 게이트 없이 어댑터 본문 스코프로 호출)의
    **공유 소스**다. 두 소비처가 같은 출하 인벤토리(engine.manifest `@render` = 프레임워크가 렌더-
    관리하는 어댑터 본문 트리)에서 스코프를 파생하므로, 새 하네스·새 어댑터 항목이 manifest 에
    등재되면 양쪽에 자동 편입된다(손-열거 0). config·hooks·엔진 코드·`node_modules` 등 런타임/설정
    파일은 `@render` 가 아니라 `@source`(byte-copy)/미등재라 이 집합에 애초에 안 든다 — 스코프가
    구조적으로 어댑터 본문으로 좁혀진다. pm_update.read_manifest 재사용(`_load_pm_update_module`
    seam). manifest 부재·로드 실패는 빈 set(검사 대상 0·무발화)."""
    pm_update = _load_pm_update_module()
    if pm_update is None:
        return set()
    managed: set[str] = set()
    for manifest_path in _engine_manifest_paths():
        try:
            for entry in pm_update.read_manifest(manifest_path):
                if getattr(entry, "render", False):
                    managed.add(str(entry).replace("\\", "/"))
        except Exception:  # noqa: BLE001 — 깨진/부재 manifest 는 검사 대상 0(무발화).
            continue
    return managed


def _engine_manifest_paths() -> list[Path]:
    """render-leak 검사 대상 engine.manifest — **루트 manifest 만** (렌더 산출물 트리).

    render-leak 은 *렌더 산출물*(operational 토큰이 concrete 로 치환된 어댑터 .md)에서 미해소
    토큰을 잡는 가드다. 그 산출물 트리는 **루트 트리**다 — 채택자는 루트 manifest 가 @render 면
    루트 `.claude/`·`.opencode/` 가 렌더된 산출물이다. 도그푸딩 모노레포(이 repo·canonical)는
    루트 manifest 가 `.claude/* @render` 여도 토큰-form 소스라 산출물이 아니다 — 그 트리-성격
    판별은 `_render_managed_relpaths` 의 local.conf 게이트가 한다(부재=소스 트리→검사 0
    render-overlay 의미론). 따라서 이 함수는 manifest *위치*만 정하고, 토큰-form 소스의 무발화는
    local.conf 게이트가 보장한다 — 다만 local.conf 를 보유한 도그푸딩 worktree 에선 그 게이트가
    부족해 파일 단위 보완(`_template_mirror_state` 의 출하-템플릿 mirror 면제)이 붙는다.

    ⚠️ `templates/<harness>/` 는 **스캔하지 않는다**: 출하 템플릿은 *token-form 소스*다(`--target`
    이 copy2 로 토큰을 보존). 그 manifest 가 `.claude/agents @render` 여도 그건 *채택자가 import/
    update 할 때 렌더하라*는 표식이지 템플릿 자신이 렌더 산출물이란 뜻이 아니다 — 템플릿은 늘 토큰을
    가지므로 스캔하면 영구 오탐
    "활성화 시 템플릿이 렌더된다"는 오해에 기반했다(템플릿은 렌더되지 않음).

    `.is_file()` 가드로 존재할 때만."""
    out: list[Path] = []
    root_manifest = REPO / ".project_manager" / "engine.manifest"
    if root_manifest.is_file():
        out.append(root_manifest)
    return out


def _load_pm_update_module():
    """pm_update 모듈을 같은 tools/ 디렉토리에서 로드 (read_manifest @render 파싱 재사용).

    board.py 가 _detected_py 류 seam 으로 형제 모듈을 로드하는 패턴과 동형. 실패 시 None →
    호출부가 검사 대상 0(무발화)으로 흡수한다.

    ⚠️ **memoize 금지**: `_shipping_template_render_scopes` 가 반환 모듈의 `REPO` 를 board.REPO 로
    덮어쓴다 — 호출마다 새 exec 라 그 대입이 갇히는데, 캐시를 도입하면 전역 오염이 된다."""
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    try:
        return _load_module_from_path(
            pm_update_py, "pm_update.py", allow_unverified=True,
        )
    except Exception:  # noqa: BLE001 — 로드 실패는 무발화(검사 대상 0).
        return None


def _is_render_managed(rel_posix: str, managed: set[str]) -> bool:
    """rel_posix 가 @render manifest path(파일 정확일치 OR 디렉토리 prefix) 하위인지."""
    for m in managed:
        if rel_posix == m or rel_posix.startswith(m.rstrip("/") + "/"):
            return True
    return False


# 출하 템플릿 mirror 판정 상태 (`_template_mirror_state` 반환값) — 면제/힌트 분기의 단일 판정원.
_TEMPLATE_MIRROR_IDENTICAL = "identical"  # 템플릿 사본과 byte-identical → 토큰-form 출하 원본(면제)
_TEMPLATE_MIRROR_DIFFERS = "differs"      # 같은 상대경로가 있으나 내용 다름 → 전파 누락 의심(면제 없음)
_TEMPLATE_MIRROR_ABSENT = "absent"        # 대응 사본 없음 → 일반 render 산출물(면제 없음)


def _shipping_template_render_scopes() -> list[tuple[Path, set[str]]]:
    """면제 후보 = `(출하 템플릿 루트, 그 manifest 의 @render 경로 집합)` 목록.

    세 겹 파생 — 손-열거 0 + 성격 게이트 + 범위 게이트:
      이름 축 = `pm_update.discover_target_names()` 재사용(`_load_pm_update_module` seam·
         `--all-targets` 가 쓰는 그 규칙). 판정 사본을 새로 만들지 않는다(인접 규범 동일).
         *앵커 정렬*: 그 함수는 pm_update 자신의 `REPO` 를 본다 — 프로덕션에선 board.REPO 와 같은
         파일-앵커지만(형제 tools/), render-leak 판정 축은 board.REPO 이므로 갓 exec 한 모듈 사본의
         `REPO` 를 board.REPO 로 맞춰 호출한다(`_load_pm_update_module` 은 호출마다 새 모듈을
         exec 한다 — 캐시 없음 → 이 대입은 이 호출에만 갇힌다).
      경로 축 = `pm_update.resolve_target_root(name)` 재사용 — 단일 path segment 검증 + resolve()
         후 parent 가 `templates/` 실경로임을 이중 확인한다(symlink 탈출 거부·거기 이미 있는 검증).
         `templates/alias -> ..` 같은 링크를 막지 않으면 candidate 가 루트 파일 *자기 자신*으로
         해소돼 byte-identical 이 자명 성립 → 전 파일 면제라는 백스톱 붕괴가 난다.
      성격·범위 축 = `templates/<name>/.project_manager/engine.manifest` 의 **`@render` 선언**.
         manifest 존재만으로는 부족하다 — 빈/무관 manifest 를 둔 트리에 같은 바이트 파일을 놓으면
         실 leak 이 면제된다. 그 템플릿이 *렌더-관리한다고 선언한* 경로(`_is_render_managed` 로
         파일 정확일치/디렉토리 prefix 판정) 하위일 때만 면제 근거가 된다. @render 0 개인 트리는
         후보에서 아예 빠진다(디렉토리 이름·엔진 사본만 흉내낸 트리 봉쇄).

    pm_update 로드/열거·manifest 파싱 실패 시 그 후보 제외(전부 실패면 빈 목록) — **면제는 특권이지
    기본값이 아니다**(판정원이 없으면 면제하지 않고 leak 을 보고하는 쪽이 보수적)."""
    pm_update = _load_pm_update_module()
    if pm_update is None:
        return []
    try:
        pm_update.REPO = REPO
        names = pm_update.discover_target_names()
    except Exception:  # noqa: BLE001 — 열거 실패는 면제 없음(보수 방향).
        return []
    scopes: list[tuple[Path, set[str]]] = []
    for name in names:
        try:
            # 탈출 검증 포함(ValueError) + 디렉토리 부재(FileNotFoundError) → 후보 제외.
            root = pm_update.resolve_target_root(name)
        except Exception:  # noqa: BLE001 — 검증 실패는 면제 없음(보수 방향).
            continue
        manifest = root / ".project_manager" / "engine.manifest"
        if not manifest.is_file():
            continue
        try:
            render_managed = {
                str(entry).replace("\\", "/")
                for entry in pm_update.read_manifest(manifest)
                # `@source=<path>` remap 항목은 제외한다 — 아래 후보 조립(`template_root / rel_posix`)
                # 이 "사본은 dest 와 같은 상대경로"를 전제하는데, source-remap 은 그 전제를 깬다
                # (`ManifestEntry.source_rel`). 전제가 안 서는 항목은 면제 판정 범위 밖으로
                # 두는 쪽이 보수적이다(오면제 대신 leak 보고). 완전 해소(remap 경로 추적)는 후속.
                if getattr(entry, "render", False) and not getattr(entry, "source_rel", None)
            }
        except Exception:  # noqa: BLE001 — 깨진 manifest 는 면제 없음(보수 방향).
            continue
        if render_managed:
            scopes.append((root, render_managed))
    return scopes


def _template_mirror_report(path: Path, rel_posix: str,
                            template_scopes: list[tuple[Path, set[str]]] | None = None
                            ) -> tuple[str, list[str]]:
    """루트 @render 파일과 출하 템플릿의 같은 상대경로 사본의 관계를 판정한다.

    engine.manifest 의 ``@render`` 는 *채택 인스턴스의 dest* 에서 render 하라는 선언이다. 그러나
    프레임워크 공개 루트는 그 dest 를 동시에 canonical source 로 보관한다: 출하 템플릿 트리
    (`_shipping_template_render_scopes`)가 **@render 로 선언한 경로** 아래 같은 상대경로와
    byte-identical 이면 아직 render 산출물이 아니라 출하할 token-form 원본이다
    (`_TEMPLATE_MIRROR_IDENTICAL` → 면제). 도그푸딩 worktree 는 local.conf 를 보유하므로 트리
    게이트만으론 이 성격을 가를 수 없다 — 이 파일 단위 판정이 그 보완이다.

    경로를 손열거하지 않고 템플릿 트리와 상대경로의 대응으로 성격을 파생한다. 한 바이트라도 다르면
    면제하지 않는다(`_TEMPLATE_MIRROR_DIFFERS`) — 템플릿을 가진 트리에서 생긴 실제 half-rendered
    leak 은 계속 lint 하고, 그 상태는 "루트 어댑터만 고치고 전파를 안 했다"는 별도 힌트를 낳는다.

    **다중 템플릿은 first-match 가 아니라 전수 집계**다: 한 상대경로를 여러 타깃이 @render 로 선언할
    수 있고(실재 — `.claude/skills` 는 claude_code·opencode 양쪽 범위), 첫 일치에서 조기 반환하면
    *다른* 타깃의 미전파 drift 가 면제 뒤에 숨는다(`--all-targets` 누락을 놓친다). 적용 후보를 전부
    보고 **하나라도 다르면 DIFFERS 우선**(면제 없음·전파 힌트 finding), 전부 같을 때만 면제한다.
    **선언 범위 안의 사본 부재·확인 실패도 drift** 다 — 신규 파일이 한 타깃에만 전파된 상태에서
    "없으니 비참여"로 넘기면 그 누락이 그대로 숨는다.

    적용 후보가 하나도 없으면(모든 타깃의 @render 범위 *밖*이거나 후보 트리 0) `_TEMPLATE_MIRROR_ABSENT`
    (일반 렌더 산출물·면제 없음). candidate 가 symlink 로 템플릿 트리를 벗어나는 경우는 둘로 가른다 —
    **루트 산출물 자기 자신**으로 해소되면 byte 비교가 자명 성립하므로 비참여(면제도 drift 도 아님),
    그 밖의 트리 밖 링크는 전파 상태를 신뢰할 수 없으므로 drift 로 집계한다. 트리 루트 탈출은
    `_shipping_template_render_scopes` 가 이미 막고, 여기선 파일 단위 containment 를 본다.

    반환은 `(state, drifted_targets)` — drift 후보의 타깃 이름(정렬)을 함께 돌려 finding 문구가 *어느*
    타깃이 어긋났는지 지목하게 한다. `template_scopes` 는 호출부가 루프 밖에서 한 번 구한 결과를
    재사용하기 위한 주입 지점(파일마다 pm_update 재로드 방지)."""
    try:
        source_bytes = path.read_bytes()
    except OSError:
        return _TEMPLATE_MIRROR_ABSENT, []
    try:
        source_resolved = path.resolve()
    except OSError:
        source_resolved = path
    scopes = (_shipping_template_render_scopes() if template_scopes is None else template_scopes)
    # 조건부 표기 렌더가 켜진 @render source는 타깃 사본과 의도적으로 byte-different다. 비교 전에
    # pm_update의 실제 최소-render seam으로 source를 같은 타깃 산출값으로 바꿔야 진짜 drift만 남는다.
    pm_update = _load_pm_update_module()
    matched = 0                     # 판정 참여 후보 수(@render 선언 + 비-자기참조).
    drifted_targets: list[str] = []  # 그중 drift(내용 불일치·사본 부재·확인 실패·트리 밖 링크) 타깃.
    for template_root, render_managed in scopes:
        # 그 템플릿이 @render 로 선언한 경로 하위가 아니면 면제 근거가 없다(범위 게이트·비참여).
        if not _is_render_managed(rel_posix, render_managed):
            continue
        candidate = template_root / rel_posix
        try:
            exists = candidate.is_file()
            resolved = candidate.resolve() if exists else None
            if resolved is not None and not resolved.is_relative_to(template_root):
                # 링크가 루트 산출물 *자기 자신*을 가리키면 비교가 자명 성립 → 판정 비참여.
                if resolved == source_resolved:
                    continue
                # 그 밖의 트리 밖 링크는 사본이 실제로 전파됐는지 알 수 없다 → drift.
                drifted = True
            else:
                # 범위 안인데 사본이 **아예 없으면** 신규 파일 미전파 = drift 다(면제 근거 아님).
                if pm_update is None or not hasattr(
                    pm_update, "render_skill_entry_notation",
                ):
                    drifted = True
                else:
                    expected = pm_update.render_skill_entry_notation(
                        source_bytes.decode("utf-8"),
                        template_root.name,
                        source=str(path),
                    ).encode("utf-8")
                    drifted = (not exists) or candidate.read_bytes() != expected
        except (OSError, UnicodeError, RuntimeError, AttributeError):
            drifted = True  # 사본 상태를 확인 못 했다 — 확인 못 한 것을 면제하지 않는다(보수).
        matched += 1
        if drifted:
            drifted_targets.append(template_root.name)
    if drifted_targets:
        return _TEMPLATE_MIRROR_DIFFERS, sorted(drifted_targets)
    return (_TEMPLATE_MIRROR_IDENTICAL if matched else _TEMPLATE_MIRROR_ABSENT), []


def _template_mirror_state(path: Path, rel_posix: str,
                           template_scopes: list[tuple[Path, set[str]]] | None = None) -> str:
    """`_template_mirror_report` 의 상태만 — 타깃 이름이 필요 없는 호출부/가드 테스트용."""
    return _template_mirror_report(path, rel_posix, template_scopes)[0]


def _is_token_form_template_mirror(path: Path, rel_posix: str) -> bool:
    """루트 @render 파일이 출하 템플릿의 byte-identical token-form 원본인지(=면제 대상인지).

    `_template_mirror_state` 의 얇은 술어 — 면제 여부만 묻는 호출부/가드 테스트용."""
    return _template_mirror_state(path, rel_posix) == _TEMPLATE_MIRROR_IDENTICAL


def lint_render_leak() -> list[tuple[str, str, str]]:
    """render 산출물에 리터럴 `{{...}}` 누출 차단 (kind=`render-leak`·blocking).

    `_ADVISORY_LINT_KINDS` 밖 → `lint --gate` 차단 → pre-push exit 1(dangling-wikilink 미러).
    half-rendered 토큰은 harness-load 에이전트 지시의 무음 열화라 실결함(경고 아님).

    **트리 성격 무발화 경계**: 검사 대상 = engine.manifest 에서 `@render` 태그가 붙은 path 의
    산출물뿐(`_render_managed_relpaths`). 그 헬퍼는 local.conf 부재 트리(토큰-form 소스
    canonical)를 검사 0 으로 잘라낸다(local.conf=트리 성격 판별)
    — 루트 manifest 가 `.claude/* @render` 여도 소스 트리에선 무발화, 채택 인스턴스(local.conf
    보유·render 산출물)에선 미해소 토큰을 잡는다. **단 도그푸딩 worktree 는 local.conf 를 보유해
    트리 게이트가 부족하다** — 출하 템플릿과 byte-identical 인 토큰-form 원본은 파일 단위로
    면제한다(`_template_mirror_state`). pm_render 의 post-render assertion 과 2중
    backstop — pm_update 가 마지막 도구였는지 무관한 상시 가드.

    fail-soft: manifest 부재·로드 실패·파일 read 오류 → 그 부분 skip(검사 대상 0·솔로/신규 무영향).
    """
    managed = _render_managed_relpaths()
    if not managed:
        return []  # @render path 0 → 검사 대상 0 (활성화 전 무발화).
    # 텍스트 판정은 pm_update._is_text_source 를 **공유**한다: render-leak 은
    # `.md` 뿐 아니라 @render 산출물 하위 *모든 텍스트 파일*(.toml·.json·.yaml·확장자 없는 텍스트
    # 등)의 미해소 토큰을 잡아야 한다 — `.md` 만 스캔하면 새 하니스 형식을 조용히 놓치는 클래스
    # (codex `.codex/agents/*.toml`)가 이 blocking 백스톱을 통과한다. 확장자 열거·판정 사본을 새로
    # 만들지 않고 render 채널(pm_update.plan)이 쓰는 그 함수에 위임한다(네 번째 판정 지점 방지).
    # managed 가 비어있지 않으면 _render_managed_relpaths 안에서 pm_update 로드가 이미 성립했다 —
    # 방어적 None 이면 아래 read 의 넓힌 except 가 바이너리를 흡수한다(graceful degrade).
    pm_update = _load_pm_update_module()
    # 출하 템플릿 후보는 파일마다 다시 구하지 않는다(pm_update 재로드·manifest 재파싱 방지).
    template_scopes = _shipping_template_render_scopes()
    issues: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for managed_rel in sorted(managed):
        target = REPO / managed_rel
        files: list[Path] = []
        if target.is_dir():
            files = sorted(p for p in target.rglob("*") if p.is_file())
        elif target.is_file():
            files = [target]
        for p in files:
            rel_posix = _rel_to_repo(p).replace("\\", "/")
            if rel_posix in seen:
                continue
            seen.add(rel_posix)
            # 바이너리 리소스(폰트·이미지 등)는 스캔 대상 아님 — 텍스트 판정 공유(위 주석).
            if pm_update is not None and not pm_update._is_text_source(p):
                continue
            try:
                # rglob("*") 로 넓히면 바이너리가 섞여 UnicodeDecodeError 로 죽을 수 있어
                # OSError 와 함께 잡는다 (_is_text_source 통과 후 TOCTOU·pm_update None 폴백 안전판).
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            leaked = sorted(set(_RENDER_TOKEN_RE.findall(text)))
            if leaked:
                # 공개 루트의 byte-identical template 원본은 token-form 이 정상이다. 반대로 내용이
                # 달라진 렌더 산출물은 아래 finding 으로 떨어진다(_template_mirror_state 참조).
                mirror_state, drifted_targets = _template_mirror_report(
                    p, rel_posix, template_scopes)
                if mirror_state == _TEMPLATE_MIRROR_IDENTICAL:
                    continue
                if mirror_state == _TEMPLATE_MIRROR_DIFFERS:
                    # 전파 대상인데 사본이 어긋났다(내용 불일치·부재) = 루트만 고치고 전파를 안 했을
                    # 가능성이 먼저다 — 채널 오진단(overlay/local.conf 만 뒤지기)을 막는 힌트 +
                    # 어느 타깃이 어긋났는지 지목(바로 그 타깃만 재전파하면 된다).
                    cause = (f"@render 관리 path — 출하 템플릿 사본과 불일치"
                             f"({', '.join(drifted_targets)}): 루트 어댑터 수정 후 "
                             "`pm_update --all-targets` 미전파 가능성 (또는 overlay/local.conf 채널 "
                             "누락·미배선 토큰)")
                else:
                    cause = "@render 관리 path — overlay/local.conf 채널 누락 또는 미배선 토큰"
                issues.append((
                    rel_posix, "render-leak",
                    f"render 산출물에 미해소 토큰 잔존: {', '.join(leaked)} ({cause})"))
    return issues


# ── un-migrated overlay 검출 (advisory) ─────────────
# free-form(채택자 손편집 산문)의 canonical home 은 root doc(§프로젝트 고유 제약)·
# `pm_role.local.md`(§보호 영역)이고, pm_import 의 FILL 채널이 거기서 전담한다. 따라서
# 어댑터 .md 는 free-form-free 여야 한다(토큰 0). 채택자가 *아직* 마이그레이션을 안 했으면 어댑터
# .md 에 리터럴 `{{PROTECTED_PATHS}}` 류가 잔존한다 — 이 lint 가 그 신호를 표면화한다(
# "un-migrated 검출"). render-leak(blocking·@render 산출물 한정)과 별개·상보: render-leak 은
# *활성화된* render path 의 미해소 토큰을, 이 lint 는 어댑터 본문의 미마이그레이션 토큰 잔존을 본다.
#
# **advisory only** — 마이그레이션 누락은 push 결함이 아니라 채택자 운영 ritual 신호(
# "push-block 아님·advisory")라 `_ADVISORY_LINT_KINDS` 에 등재(`--gate` 미차단). free-form 3종
# (로컬 `_UNMIGRATED_FREEFORM_KEYS` 디커플)만 본다 — operational 토큰(`{{PROJECT_NAME}}`
# 등)은 import sed/local.conf 채널이라 별개. graceful: 어댑터 파일/디렉토리 부재 시 finding 0.

# 어댑터 스캔 축은 **손-열거하지 않고 출하 인벤토리에서 파생**한다.
#   스코프 축 = engine.manifest 의 `@render` dest 경로 (`_manifest_render_relpaths` — render-leak
#      과 공유하는 출하 인벤토리): `.claude/agents`·`.claude/skills`·`.opencode/agents`·`.codex/
#      agents`·`.agents/skills`(codex dual) 등 *프레임워크가 렌더-관리하는 어댑터 본문*
#      트리. 옛 하드코딩(`.claude`/`.opencode` 두 하네스 × `*.md`)은 세 번째 하네스(codex)의 `.codex/
#      agents/*.toml` 을 구조적으로 못 봤다 — manifest 파생이라 새 `@render` 항목이 자동 편입된다.
#      **런타임/설정 파일 제외가 구조적**: `.opencode/node_modules`(플러그인 deps)·adopter-owned
#      `.codex/config.toml`·`.codex/hooks.json`·`.claude/settings.json`·엔진 코드는 `@render` 가 아닌
#      `@source`(byte-copy)/미등재라 스코프에 애초에 안 든다 — 무관 파일 이중 read·토큰 오탐 봉쇄.
#      스코프 = 인스턴스 `@render`(engine.manifest) **∪ 은퇴 채널**(`_RETIRED_OVERLAY_GLOBS` — 현행
#      manifest 엔 없지만 구 채택자 잔존분). **add-harness guest 어댑터도 인스턴스 manifest 에
#      `@render` 로 등재**(add_harness)되므로 이 인스턴스-manifest 파생이 guest 를 자연 커버
#      한다(templates/ 불요) — flavor-manifest 보강(`_all_harness_body_relpaths`)은 출하
#      인스턴스에 templates/ 가 없어 항상 ∅였다(false-green). manifest 등재로 대체·판정원 단일화(제거).
#   파일 필터 축 = 텍스트 판정 (`pm_update._is_text_source` seam
#      `_load_pm_update_module` 재사용): 옛 확장자 열거(`*.md`·`SKILL.md`)는 codex `.toml` 같은
#      새 형식을 놓쳤다. 확장자 대신 "텍스트로 읽히는가"만 본다 — render-leak(blocking)이 이미 쓰는
#      그 판정을 공유(네 번째 판정 지점 방지).
# root 문서(CLAUDE.md·AGENTS.md 등)는 *제외*: 채택자가 통째로 손편집하는 instance-owned
# scaffold 라 `@render` 가 아니다(free-form 의 canonical home) — manifest 미등재라 스코프 밖. 거기의
# raw 토큰은 "미마이그레이션"이 아니라 "채택자가 아직 안 채움"이라 이 lint 의 오분류 대상이 아니다.

# free-form 3종 토큰 — un-migrated-overlay lint 가 어댑터 본문에서 스캔하는 리터럴 토큰 집합.
# pm_render 의 free-form value-fill 기계(FREEFORM_KEYS·overlay)는  로 제거됐으므로,
# 이 lint 는 그 심볼에 의존하지 않고 자체 로컬 튜플로 검출 대상을 정의한다(디커플·단일 책임).
# pm_import.FREE_FORM_TOKENS(FILL 채널·canonical home 전담)와 동일 집합을 bare key 로 본다.
_UNMIGRATED_FREEFORM_KEYS: tuple[str, ...] = (
    "PROJECT_CONSTRAINTS",
    "PROTECTED_PATHS",
    "USER_GATE_ITEMS",
)

# 은퇴 어댑터 채널 (닫힌 역사적 집합) — manifest `@render` 파생 스코프의 **보완**.
# @render 파생은 *현행* manifest 만 본다.
# manifest 에 미등재지만, pm_update 는 채택자의 기존 파일을 **삭제하지 않는다**(이 프로젝트는 채택자
# 파일 삭제를 자동화하지 않는다 — 삭제는 사용자 위임·[[deletion-delegate-to-user]]). 그래서 구 채택자
# 트리엔 `.opencode/command/*.md` 가 잔존할 수 있고, 그 안의 un-migrated free-form 토큰이 @render-only
# 스코프에선 조용히 누락된다.
#
# ⚠️ 이 목록의 손-열거는 **decay 클래스가 아니다**: 그 클래스는
# "새 값(새 하네스·새 확장자)을 열거가 못 따라온다" 였는데, 은퇴 채널은 정의상 **닫힌 과거 집합**이라
# 새 하네스 축과 무관하게 **늘어나지 않는다**. 새 채널은 manifest(파생)로, 은퇴 채널만 여기로 — 둘은
# 직교한다. glob 형태는 은퇴 당시 형상 그대로 박제(`.opencode/command/*.md`·비재귀·직속 `.md`).
_RETIRED_OVERLAY_GLOBS: tuple[tuple[str, str], ...] = (
    (".opencode/command", "*.md"),
)


def _collect_overlay_adapter_files() -> list[Path]:
    """un-migrated 검사 대상 어댑터 본문 — engine.manifest `@render` 경로 ∪ 은퇴 채널 하위 텍스트 파일.

    **스코프·파일필터 두 축을 출하 인벤토리에서 파생**한다(손-열거 아님):
      - 스코프 축 = `_manifest_render_relpaths`(**인스턴스 engine.manifest** `@render` dest 경로·
        render-leak 공유) **∪ `_RETIRED_OVERLAY_GLOBS`**(은퇴 채널·닫힌 과거 집합·구 채택자 잔존분·
        codex R3). manifest 항목이 디렉토리면 rglob, 파일이면 그 파일(render-leak 의 dir/file 처리
        동형); 은퇴 채널은 은퇴 당시 glob(`.opencode/command/*.md`·직속). **add-harness guest 어댑터도
        인스턴스 manifest 에 `@render` 로 등재**(add_harness)되므로 이 host-manifest 파생이
        guest 를 자연 커버한다 — templates/ 불요(옛 flavor-manifest 보강 `_all_harness_body_relpaths`
        는 인스턴스에 templates/ 가 없어 무력이라 제거·판정원 단일화). 런타임/설정(`node_modules`·
        `config.toml`·`settings.json`·lib·plugins·엔진 코드)은 `@render` 도 은퇴 채널도 아니라 스코프에
        애초에 안 든다 — 네임스페이스-전체 rglob 의 무관 파일 read·오탐 봉쇄.
      - 파일 필터 축 = `pm_update._is_text_source`(확장자 열거 대신 텍스트 판정) — codex `.codex/
        agents/*.toml` 같은 새 형식도 편입. 로드 실패 시 필터 생략 → 호출부 read 의 넓힌 except 가
        바이너리를 흡수(graceful degrade·render-leak 동형).
    root 문서(CLAUDE.md·AGENTS.md)는 `@render`·은퇴 채널 모두 미등재(instance-owned scaffold)라 스코프
    밖. manifest 부재·스코프 0(솔로/신규)이면 finding 0."""
    pm_update = _load_pm_update_module()
    candidates: list[Path] = []
    # 인스턴스 engine.manifest @render (host 어댑터 + add-harness 가 등재한 guest·
    #    render-leak 공유·손-열거 0) — @render 파생이라 런타임/설정 제외 불변식 유지.
    for managed_rel in sorted(_manifest_render_relpaths()):
        target = REPO / managed_rel
        if target.is_dir():
            candidates.extend(sorted(p for p in target.rglob("*") if p.is_file()))
        elif target.is_file():
            candidates.append(target)
    # 은퇴 채널 = 닫힌 과거 집합 (manifest 미등재·구 채택자 잔존 가능·직교·안 늘어남)
    for rel, pattern in _RETIRED_OVERLAY_GLOBS:
        d = REPO / rel
        if d.is_dir():
            candidates.extend(sorted(p for p in d.glob(pattern) if p.is_file()))
    # 텍스트 필터 + dedupe (두 스코프 잠재 중복·재열거 방지·바이너리는 텍스트 판정 공유로 제외)
    files: list[Path] = []
    seen: set[Path] = set()
    for p in candidates:
        if p in seen:
            continue
        seen.add(p)
        if pm_update is not None and not pm_update._is_text_source(p):
            continue
        files.append(p)
    return files


def lint_unmigrated_overlay() -> list[tuple[str, str, str]]:
    """어댑터 본문에 리터럴 free-form 토큰이 잔존하면 un-migrated 신호 (kind=`un-migrated-overlay`).

    `_ADVISORY_LINT_KINDS` 등재 → `lint --gate` 미차단(advisory "push-block 아님"). 마이그레이션
    누락은 채택자 운영 ritual 신호이지 출하 결함이 아니므로 visibility 만 제공한다.

    검사 (정적·shipped tree 스캔):
      - 어댑터 본문(`_collect_overlay_adapter_files` — engine.manifest `@render` 경로 하위 텍스트
        파일·확장자 무관·codex `.toml` 포함)에 리터럴 free-form 토큰
        (`{{PROJECT_CONSTRAINTS}}`/`{{PROTECTED_PATHS}}`/`{{USER_GATE_ITEMS}}`)이 잔존 → 파일·토큰별
        finding 1건. 마이그레이션 후엔 어댑터 본문이 free-form-free(토큰 0)다.

    디커플: render-overlay free-form value-fill 기계(`FREEFORM_KEYS`·overlay.local.yaml)
    는 제거됐으므로 이 lint 는 그 심볼에 의존하지 않고 자체 로컬 튜플(`_UNMIGRATED_FREEFORM_KEYS`)
    로 검출 대상을 정의한다. free-form 은 pm_import FILL 채널이 canonical home 에서 전담하므로
    overlay 파일 부재 조건은 더 이상 의미가 없다 — 리터럴 토큰 잔존만으로 advisory 를 낸다.

    오탐 0 경계:
      - operational 토큰(`{{PROJECT_NAME}}` 등)은 *검사 대상 아님* — import sed/local.conf 채널이라
        별개. free-form 3종만 매칭(채택자 손편집 산문).
      - 코드 span/fence 안의 *예시* 토큰은 `_strip_code` 로 제거 후 스캔(문서가 토큰을 예시로
        보여줘도 오탐 안 됨).
      - graceful: 어댑터 파일/디렉토리 부재(솔로·non-adopter) → finding 0. 파일 read 오류는 skip.
    """
    token_re = re.compile(
        r"\{\{(" + "|".join(re.escape(k) for k in _UNMIGRATED_FREEFORM_KEYS) + r")\}\}")

    issues: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for p in _collect_overlay_adapter_files():
        rel_posix = _rel_to_repo(p).replace("\\", "/")
        if rel_posix in seen:
            continue
        seen.add(rel_posix)
        try:
            # 텍스트 판정(_is_text_source) 통과 후에도 pm_update None 폴백 시 바이너리가 새거나
            # TOCTOU 로 디코드가 깨질 수 있어 UnicodeDecodeError 를 함께 흡수(render-leak 동형).
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # 코드 span/fence 예시 토큰은 실 placeholder 가 아니므로 제거 후 스캔(오탐 0).
        leaked = sorted(set(token_re.findall(_strip_code(text))))
        if leaked:
            toks = ", ".join("{{" + k + "}}" for k in leaked)
            issues.append((
                rel_posix, "un-migrated-overlay",
                f"리터럴 free-form 토큰 잔존: {toks} — 어댑터가 아직 canonical home(root doc·"
                f"pm_role.local.md)으로 마이그레이션되지 않았다(§3.6·free-form-free)."))
    return issues


# ── 파일명-무관 참조 강제  ──────────
# 엔진은 [[ADR-NNNN]] 를 *번호*로 resolve 하므로 슬러그는 무관하다. 그러나 LLM 이
# 구조화 디렉토리(decisions/·tickets/·ideas/)를 **생파일명·슬러그**로 가리키면 — markdown 경로
# 링크 `](…/decisions/<slug>.md)` 나 숫자선두 자유어휘 wikilink `[[NNNN-slug]]` — 슬러그가 바뀌면
# 부정확 참조가 된다. 이 둘은 *번호로 resolve 가능*
# resolve 실패 = dangling(차단), 실재하지만 슬러그 의존 = 권고(canonical ID-wikilink 로 전환).
# 자유어휘 일반([[some-memory]])·산문 언급은 *건드리지 않는다*.

# 구조화 디렉토리를 가리키는 markdown 링크. 견고성을 위해 2단계로 본다
# suggestion — 정규식만으론 link-form edge 가 새므로):
#   (a) `_MD_LINK_TARGET_RE` 로 링크 target 을 추출 — 선택적 `<…>` 꺾쇠·트레일링 `"title"` 허용.
#   (b) target 에서 fragment(`#…`)·query(`?…`)를 떼고, 외부 URL(`scheme://`)이면 건너뛴 뒤
#       `_STRUCT_PATH_RE` 로 구조화 경로(decisions/·tickets/<state>/·ideas/<state>/<file>.md)를 매칭.
# 이렇게 `.md)`·`.md#sec)`·`.md "title")`·앞 경로 유무를 다 흡수하고 외부 URL 오탐(오차단)을 막는다.
# `(?:^|/)` 로 segment 경계를 요구해 `mydecisions/` 류 비-경계 매치를 배제. 매핑: decisions→ADR,
# tickets/<state>→ticket, ideas/<state>→idea (tickets/ideas 는 상태 디렉토리 필수 — README·_template 제외).
# title 은 CommonMark 3형 모두 흡수 — `"…"`·`'…'`·`(…)`.
_MD_LINK_TARGET_RE = re.compile(
    r"\]\(\s*<?([^)>\s]+)>?(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")
_STRUCT_PATH_RE = re.compile(
    r"(?:^|/)(?:"
    r"decisions/([^/]+\.md)"
    r"|tickets/(?:open|claimed|blocked|done)/([^/]+\.md)"
    r"|ideas/(?:open|promoted|killed)/([^/]+\.md)"
    r")$")
# 숫자선두 자유어휘 wikilink — `[[NNNN-slug]]`·`[[NNNN]]`·alias `[[NNNN-slug|표시명]]`
# (ADR/idea 를 ID 아닌 형으로 적은 것). slug 부는 `[^\]|]+`(비-ASCII 포함) — `_slugify` 가 한글
# slug 를 허용하므로 `[[0001-한글아이디어]]` 도 포착.
# alias `|표시명` 은 `_WIKILINK_RE` 동작과 동일하게 흡수(codex suggestion).
_NUM_LEAD_WIKILINK_RE = re.compile(r"\[\[(\d+)(?:-[^\]|]+)?(?:\|[^\]]+)?\]\]")

# 코드 span/fence 안의 *예시* 링크·wikilink 는 실제 참조가 아니므로 스캔 전 제거(오탐 0).
# 문서가 "나쁜 예시"로 `[x](decisions/9999-ghost.md)` 를 코드로 보여줘도 게이트를 막지 않게 한다.
# fenced(``` … ``` · ~~~ … ~~~) 를 먼저(여러 줄·비-greedy), 그 다음 inline(`…`·한 줄) 을 지운다.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """markdown 코드 span/fence 를 공백으로 치환 — 그 안의 예시 링크가 lint 에 안 걸리게."""
    return _INLINE_CODE_RE.sub(" ", _FENCED_CODE_RE.sub(" ", text))


def lint_unstable_refs() -> list[tuple[str, str, str]]:
    """파일명/슬러그-의존 구조 참조를 포착한다 (kind=`unstable-ref`/`unstable-ref-advice`).

    두 형태를 본다 (둘 다 *구조화 디렉토리*를 가리킬 때만):
      - markdown 경로 링크 `](…/decisions/<slug>.md)`·`](…/tickets/<state>/<slug>.md)`·
        `](…/ideas/<state>/<slug>.md)` → 대상 파일이 실재 안 하면 dangling(차단), 실재하면 권고.
        (명시적 구조 경로라 의도가 분명 → 차단 가능.)
      - 숫자선두 wikilink `[[NNNN-slug]]`·`[[NNNN]]` → 번호가 ADR/idea 로 **resolve 될 때만**
        canonical `[[ADR-NNNN]]`/`[[idea-NNNN]]` 권고. resolve 안 되면 자유어휘(`[[2026-roadmap]]`
        등)와 구분 불가 → **불검사**(차단 안 함 · 오탐 0).

    kind 분류:
      - `unstable-ref`        = markdown 경로 링크가 실재 안 함 (환각) — `lint --gate` 차단.
      - `unstable-ref-advice` = 실재 파일을 슬러그로 가리킴 / 숫자선두 슬러그형 — 작동은 함,
        ID-wikilink 권고만(자문성·차단 안 함).

    자유어휘 일반(`[[some-memory]]`)·산문 언급은 건드리지 않는다 (오탐 0). detail 메시지에
    권장 교체형(`→ [[ADR-NNNN]]`)을 싣는다. (name, dangling) 으로 dedupe 하고 사용처를 모은다.
    """
    _ticket_ids, adr_nums, idea_nums = _resolve_wikilink_targets()
    # (name, dangling) → (detail, [source rel paths]) — name+상태가 dedupe 키.
    found: dict[tuple[str, bool], tuple[str, list[str]]] = {}

    def _record(name: str, dangling: bool, detail: str, src: str) -> None:
        # raw/ 스냅샷(sealed 면 immutable)은 슬러그-경로 *권고*(never-blocks)를 면제한다:
        # 봉인된 스냅샷의 링크는 고칠 수 없고(immutable) 역사적 인용이라 ID-wikilink 권고가 비실행적이다.
        # dangling(환각·차단)은 유지 — 깨진 구조 링크는 raw 에서도 surface 한다.
        if not dangling and "/raw/" in ("/" + src.replace("\\", "/")):
            return
        detail0, bucket = found.setdefault((name, dangling), (detail, []))
        del detail0  # 첫 detail 보존 (같은 name 의 메시지는 동일).
        if src not in bucket:
            bucket.append(src)

    for path in _collect_wikilink_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        src = _rel_to_repo(path)
        # 코드 span/fence 안의 예시 링크·wikilink 는 실제 참조 아님 → 제거 후 스캔(오탐 0).
        text = _strip_code(text)

        # (1) markdown 링크 — target 추출 → fragment/query 제거·외부 URL 제외 → 구조화 경로 분류.
        for raw_target in _MD_LINK_TARGET_RE.findall(text):
            # 외부 URL 제외(오탐·오차단 방지) — scheme `http://` 형 + protocol-relative `//host/…`
            # Windows `C:/` 는 `://`·`//`
            # 둘 다 아니라 영향 없음(이식성 고려해 urlsplit scheme-오인 회피).
            if "://" in raw_target or raw_target.startswith("//"):
                continue
            clean = raw_target.split("#", 1)[0].split("?", 1)[0]
            m = _STRUCT_PATH_RE.search(clean)
            if not m:
                continue
            dec_f, tic_f, idea_f = m.group(1), m.group(2), m.group(3)
            if dec_f:
                num = _leading_num(dec_f)
                if num is None:
                    continue  # 숫자선두 ADR 파일만 (template 등 비번호 제외).
                if not (DECISIONS_DIR / dec_f).exists():
                    _record(dec_f, True,
                            f"decisions/{dec_f} 실재 안 함 (슬러그 링크 dangling) "
                            f"→ [[ADR-{num.zfill(4)}]] 로 교체", src)
                else:
                    _record(dec_f, False,
                            f"decisions/{dec_f} 슬러그 경로 링크 (환각 취약) "
                            f"→ [[ADR-{num.zfill(4)}]] 권고", src)
            elif tic_f:
                target = _find_ticket_file(tic_f)
                tid = _ticket_id_from_filename(tic_f)
                if target is None:
                    _record(tic_f, True,
                            f"tickets/.../{tic_f} 실재 안 함 (슬러그 링크 dangling)"
                            + (f" → [[{tid}]] 로 교체" if tid else ""), src)
                else:
                    _record(tic_f, False,
                            f"tickets/.../{tic_f} 슬러그 경로 링크 (환각 취약)"
                            + (f" → [[{tid}]] 권고" if tid else ""), src)
            elif idea_f:
                num = _leading_num(idea_f)
                if num is None:
                    continue
                target = _find_idea_file(idea_f)
                if target is None:
                    _record(idea_f, True,
                            f"ideas/.../{idea_f} 실재 안 함 (슬러그 링크 dangling) "
                            f"→ [[idea-{num.zfill(4)}]] 로 교체", src)
                else:
                    _record(idea_f, False,
                            f"ideas/.../{idea_f} 슬러그 경로 링크 (환각 취약) "
                            f"→ [[idea-{num.zfill(4)}]] 권고", src)

        # (2) 숫자선두 wikilink — [[NNNN-slug]]/[[NNNN]]. ADR/idea 번호로 *resolve 될 때만* 권고
        #     (canonical ID-wikilink 로 전환·자문성). resolve 안 되면 자유어휘(`[[2026-roadmap]]`·
        #     `[[1234-experiment]]` 등 숫자선두 메모리 링크)와 구분 불가 → **건드리지 않는다**
        #     ("자유어휘 불검사·오탐 0"; 미resolve hard-block 은 자유어휘를
        #     거짓 차단하는 회귀였음). 차단(dangling)은 명시적 구조 경로를 가진 markdown 링크만(위 (1)).
        for m in _NUM_LEAD_WIKILINK_RE.finditer(text):
            # alias(`|표시명`) 제거 후를 dedupe 키·메시지로 — 같은 대상이 alias 만 달라도 1 issue.
            name = m.group(0)[2:-2].split("|", 1)[0]   # 대괄호·alias 벗긴 raw (NNNN-slug).
            num = m.group(1).lstrip("0") or "0"
            if num in adr_nums:
                _record(name, False,
                        f"[[{name}]] 숫자선두 슬러그형 → canonical [[ADR-{num.zfill(4)}]] 권고", src)
            elif num in idea_nums:
                _record(name, False,
                        f"[[{name}]] 숫자선두 슬러그형 → canonical [[idea-{num.zfill(4)}]] 권고", src)
            # else: ADR/idea 로 resolve 안 됨 → 자유어휘로 간주·불검사(오탐 0).

    # dangling = hard block (resolve 실패 = 환각), 실재 슬러그 = 권고(자문성).
    # kind 로 구분: `unstable-ref`(차단) vs `unstable-ref-advice`(never-blocks · 권장형).
    issues: list[tuple[str, str, str]] = []
    for (name, dangling) in sorted(found, key=lambda k: (k[0], k[1])):
        detail, srcs = found[(name, dangling)]
        if not srcs:
            continue
        shown = ", ".join(srcs[:3]) + (f" (외 {len(srcs) - 3}개)" if len(srcs) > 3 else "")
        kind = "unstable-ref" if dangling else "unstable-ref-advice"
        issues.append((name, kind, f"{detail} · 사용처: {shown}"))
    return issues


def _find_ticket_file(filename: str) -> Path | None:
    """tickets/<state>/<filename> 중 실재하는 첫 경로 (상태 무관 — mv 로 이동했을 수 있음)."""
    for status in STATUS_DIRS:
        p = tickets_dir() / status / filename
        if p.exists():
            return p
    return None


def _find_idea_file(filename: str) -> Path | None:
    """ideas/<state>/<filename> 중 실재하는 첫 경로 (상태 무관)."""
    for status in IDEA_STATUS_DIRS:
        p = IDEAS_DIR / status / filename
        if p.exists():
            return p
    return None


def _ticket_id_from_filename(filename: str) -> str | None:
    """ticket 파일명에서 canonical ID 추출. 없으면 None.

    prefixed(`T-PAY-001-foo.md` → `T-PAY-001`·`T-service-a-001-…`)도 추출 — 발행측
    `_next_id` 가 prefixed 파일을 만드므로. grammar 는 `_TICKET_ID_BODY` 공유.
    """
    m = re.match(rf"({_TICKET_ID_BODY})", filename)
    return m.group(1) if m else None


# 차단되지 않는 자문성 lint kind — push 를 막지 않는 권고/드리프트 카테고리.
# `lint --gate` 는 이 카테고리를 종료코드에서 제외한다 (push 게이트용 엄격 부분집합):
#   - status-done-accum : status.md ✅ 완성 행 누적 archive 권고 (lint_status "never blocks" 보장).
#     (status-header-bloat·scalar-anchor-broken 은 status judgment-only 화로 제거.)
#   - unstable-ref-advice : 실재 파일을 슬러그로 가리키는 링크 — 작동은 함, ID-wikilink 권고만
#     resolve 실패는 kind=`unstable-ref` 로 차단됨.
#   - scope-advice : family_scope 형식/등록 권고 — scope 자체로 hard-fail 안 함
#     "차단은 최소·advisory 우선").
#   - stale·orphan·oversized·history : domain freshness finding (lint_domain·history=현재-진실
#     페이지에 쌓인 변경 이력 항목). enforcement 아닌 visibility — push 를 절대 막지 않는다.
#   - dangling-wikilink-scaffold : 어댑터 scaffold(.claude/.opencode) 에서만 등장하는 framework
#     ADR/idea dangling. 채택자(framework ADR 부재 다운스트림)의 scaffold bracket-ref 는
#     영구 dangling 이 정상 — visibility 만, push 미차단. ticket dangling·wiki/root-doc dangling 은
#     여전히 `dangling-wikilink`(blocking).
#   - un-migrated-overlay : 어댑터 .md 에 리터럴 free-form 토큰 잔존.
#     canonical home(root doc·pm_role.local.md) 마이그레이션 누락 신호 — 채택자 운영 ritual 이지
#     출하 결함 아니므로 visibility 만, push 미차단. render-leak(@render 산출물 한정·blocking)과 별개.
#   - adapter-drift : 채택자의 adapter-layer(facade·진입문서·settings) 가 baseline(마지막 동기) 이후
#     전파 채널 없는 manifest-제외 잔여라 *전파 대신*
#     PM 에게 경고만 — `pm-update` 안내(visibility>enforcement). B 전파는 채택자 customization clobber(비파괴
#     위배)라 의도적 비-전파. instance-state(status·architecture·tickets·log·decisions·README·lite)는 채택자
#     소유·diverge 정상이라 scope 제외. push 미차단(never-block).
#   - adr-author : ADR frontmatter `author: <user>/<pm-slot>` provenance 권고.
#     "누가 결정했나"(provenance·연속성 아님)를 박는 발행측 규칙 — board.py 는 ADR 을 발행하지 않으므로
#     부재/형식어긋남을 권고만 한다. solo·구 ADR(author 부재)은 정상이라 push 미차단(never-block).
#   - architecture-stale·status-stale·domain-stale : 현재-진실 문서 freshness — 문서
#     frontmatter `verified_at: <sha>` 이후 그 문서 매핑 경로에 커밋이 있으면 "재검증 필요"
#     권고(date 비교 대체). architect 재검증·PM 점검 대상이지 push 결함이 아니므로
#     visibility 만·never-block.
#   - areas-duplicate-repo : areas.md 에 같은 repo 행이 2개 이상. first-match 리졸버
#     4종이 첫 행에서 return 하므로 조용히 굳는 걸 보이게만 한다 — 자동 병합은 사람 판정 영역이고
#     레지스트리 정리가 push 결함도 아니라 never-block(`areas_set_cell` 의 fail-loud 와 짝).
#   - areas-merge-union : areas.md 를 담은 git 에 `merge=union` 이 선언되지 않음. 동시 등록
#     안전(양쪽 행 보존)이 사라진 상태를 보이게만 한다 — 엔진이 backfill 하는 채널이 이미 있고
#     (`_ensure_board_gitattributes`) 채택자가 자기 `.gitattributes` 를 가질 수 있어 never-block.
_ADVISORY_LINT_KINDS: frozenset[str] = frozenset(
    {"status-done-accum", "unstable-ref-advice", "scope-advice", "coverage",
     "stale", "orphan", "oversized", "history", "adr-lifecycle", "architecture-stale",
     "status-stale", "domain-stale", "domain-unverifiable",
     "architecture-unverifiable", "status-unverifiable",
     "dangling-wikilink-scaffold", "un-migrated-overlay", "adapter-drift",
     "adr-author", "areas-duplicate-repo", "areas-merge-union",
     "delegate-same-model"})


def _adr_id_from_path(p: Path) -> str:
    """decisions/ 파일명(`NNNN-slug.md`) → `ADR-NNNN`. `.stem` 으로 확장자 제거(dashless 방어)."""
    return f"ADR-{p.stem.split('-', 1)[0]}"


def _as_id_list(val) -> list[str]:
    """frontmatter 값(None/str/list)을 ID 문자열 리스트로 정규화한다.

    `amends: [ADR-0002, ADR-0011]`(yaml list) · `amends: ADR-0001`(scalar) ·
    `refines: ADR-0006, ADR-0008`(comma scalar) 모두 수용 — 쉼표/공백 분리.
    """
    if val is None:
        return []
    items = val if isinstance(val, list) else re.split(r"[,\s]+", str(val))
    return [str(s).strip() for s in items if str(s).strip()]


def lint_adr_lifecycle() -> list[tuple[str, str, str]]:
    """ADR lifecycle 정합 advisory (never-block).

    `amends:[Y]`/`supersedes:Y` 인 ADR-X 에 대해 대상 Y(ADR)가 **back-ref**(amended_by/
    superseded_by 에 X)와 **status**(amended/superseded)를 갖는지 검사한다. + 자가일관:
    status=amended 면 amended_by 가, superseded 면 superseded_by 가 있어야 한다. 어긋나면
    권고(kind=`adr-lifecycle`·`_ADVISORY_LINT_KINDS` 등재로 `--gate` 종료코드 비기여).
    `refines`(추가·대상 불변)는 검사 안 한다. ticket back-ref(`amended_by:[T-NNNN]`)는
    forward edge 가 없어 cross-check 대상 아님(자가일관만). decisions/ 부재·깨진 frontmatter
    → graceful skip(솔로/신규 clone·ADR 0개 무영향)."""
    findings: list[tuple[str, str, str]] = []
    if not DECISIONS_DIR.is_dir():
        return findings
    adrs: dict[str, dict] = {}
    for p in sorted(DECISIONS_DIR.glob("[0-9]*.md")):
        try:
            fm, _ = load_ticket(p)
        except Exception:  # noqa: BLE001 — 깨진/frontmatter 없는 파일은 skip(비차단).
            continue
        adrs[_adr_id_from_path(p)] = fm or {}

    for adr_id, fm in adrs.items():
        status = str(fm.get("status") or "").strip()
        for verb, want_status, back_field in (
            ("amends", "amended", "amended_by"),
            ("supersedes", "superseded", "superseded_by"),
        ):
            for tgt in _as_id_list(fm.get(verb)):
                if not tgt.startswith("ADR-"):
                    continue  # ADR↔ADR 만 cross-check (ticket 등 비-ADR 대상 제외).
                tfm = adrs.get(tgt)
                if tfm is None:
                    findings.append((adr_id, "adr-lifecycle",
                                     f"{verb}: {tgt} 인데 그 ADR 파일이 없음"))
                    continue
                if adr_id not in _as_id_list(tfm.get(back_field)):
                    findings.append((tgt, "adr-lifecycle",
                                     f"{adr_id} 이 {verb} 하는데 {back_field} 에 {adr_id} 누락"))
                tgt_status = str(tfm.get("status") or "").strip()
                # supersession은 amendment보다 강한 종결 상태다. 이미 superseded 된 ADR에
                # 후속 ADR이 amendment 역사를 더해도 상태를 amended로 되돌리지 않는다.
                status_satisfies = (
                    tgt_status == want_status
                    or (verb == "amends" and tgt_status == "superseded")
                )
                if not status_satisfies:
                    findings.append((tgt, "adr-lifecycle",
                                     f"{adr_id} 이 {verb} 하는데 status={tgt_status or '없음'} (기대 {want_status})"))
        # 자가일관 — status 가 amended/superseded 면 back-ref 가 있어야.
        if status == "amended" and not _as_id_list(fm.get("amended_by")):
            findings.append((adr_id, "adr-lifecycle", "status: amended 인데 amended_by 없음"))
        if status == "superseded" and not _as_id_list(fm.get("superseded_by")):
            findings.append((adr_id, "adr-lifecycle", "status: superseded 인데 superseded_by 없음"))
    return findings


def _parse_adr_author(val) -> tuple[str, str] | None:
    """ADR frontmatter `author` 를 `(user, slot)` 으로 파싱한다.

    형식 = `<user>/<pm-slot>` — `created_by`/`claimed_by` identity 토큰과 동일 형태(`identity_tag`).
    *마지막* `/` 로 분리(`rsplit('/', 1)`)해 slot 을 마지막 토큰으로 잡는다 — user 에 `/` 가
    있어도(이메일 등엔 없지만 방어) slot 이 흔들리지 않는다. 두 토큰이 모두 non-empty 여야
    유효(`<user>/<pm-slot>`); `/` 없음·한쪽 빈값(`/slot`·`user/`)은 None(형식 어긋남).
    빈값/None 은 None(부재) — 호출측이 부재와 형식 어긋남을 구분한다.
    """
    s = str(val or "").strip()
    if "/" not in s:
        return None
    user, slot = s.rsplit("/", 1)
    user, slot = user.strip(), slot.strip()
    return (user, slot) if user and slot else None


def lint_adr_author() -> list[tuple[str, str, str]]:
    """ADR `author` provenance 권고 advisory.

    각 ADR frontmatter 에 `author: <user>/<pm-slot>`(누가 결정했나·provenance·연속성 아님)가
    박혀 있는지 권고한다 — board.py 가 ADR 을 *발행*하지 않으므로 발행측 규칙을 강제하는 대신
    부재/형식어긋남을 visibility 로만 표면화한다. `author` 부재 → "author 권고"; 있으나
    `<user>/<pm-slot>` 형식이 아니면 → 형식 권고. kind=`adr-author`(`_ADVISORY_LINT_KINDS`
    등재로 `--gate` 종료코드 비기여). decisions/ 부재·깨진 frontmatter → graceful skip
    (솔로/신규 clone·구 ADR author 부재 정상 무영향)."""
    findings: list[tuple[str, str, str]] = []
    if not DECISIONS_DIR.is_dir():
        return findings
    for p in sorted(DECISIONS_DIR.glob("[0-9]*.md")):
        try:
            fm, _ = load_ticket(p)
        except Exception:  # noqa: BLE001 — 깨진/frontmatter 없는 파일은 skip(비차단).
            continue
        fm = fm or {}
        adr_id = _adr_id_from_path(p)
        raw = fm.get("author")
        if not str(raw or "").strip():
            findings.append((adr_id, "adr-author",
                             "author 권고 — `author: <user>/<pm-slot>` (누가 결정했나·provenance)"))
        elif _parse_adr_author(raw) is None:
            findings.append((adr_id, "adr-author",
                             f"author 형식 권고 — `{raw}` 이 `<user>/<pm-slot>` 아님"))
    return findings


# ── 현재-진실 문서 freshness = verified_at sha 판정 (date 비교 대체) ──────────
# architecture.md·status.md·domain 페이지의 freshness 를 frontmatter `verified_at: <sha>`
# 기준으로 판정한다: "그 sha 이후 문서 매핑 경로에 커밋이 있나"의 이진 기계 판정
# Decision 2). date 비교("최신이겠지" 해석 구멍·결정 ⑭ 위반)를 대체한다. architecture.md·
# status.md 는 엔진 코드 트리를, domain 페이지는 covers→pathspec(domain.py 재사용)을 매핑
# 경로로 쓴다. 판정은 advisory(`_ADVISORY_LINT_KINDS`·never-block·architect 재검증·PM 점검·
# 소유 불변).

# architecture.md·status.md 의 verified_at 매핑 경로 = 엔진 코드 트리. 이 문서들이 판정
# 대상으로 삼는 코드다. 디렉토리 전체를 pathspec 으로 잡아 over-approx(과경고 < 미경고·
# 리터럴 디렉토리 prefix) — 그 트리 어느 파일이 sha 이후 커밋돼도 "재검증해" 신호.
_CURRENT_TRUTH_ENGINE_PATHS: tuple[str, ...] = (".project_manager/tools",)


def _git_commits_between(sha: str, pathspecs: list[str], *,
                         runner=None, repo: Path | None = None) -> bool | None:
    """`<sha>..HEAD` 에 `pathspecs` 경로 커밋이 있으면 True·없으면 False·판정불가 None.

    verified_at 판정의 핵심 — `git log --oneline <sha>..HEAD -- <pathspecs>` 출력이 비지
    않으면 그 sha 이후 매핑 경로가 바뀐 것(stale). fail-soft(모두 None):
      - sha 빈값·pathspec 전부 빈 → None (판정 대상 없음).
      - git 바이너리 부재·미지 sha(rc≠0·`bad revision`)·타임아웃/예외 → None (skip).
    None 은 "판정불가(unknown)"라 호출부가 finding 을 내지 않는다(현행 freshness fail-soft
    성격·솔로/신규 clone 무탈). `runner` 는 테스트 hermetic seam(argv→(rc, stdout))·미주입
    시 실 subprocess(`git -C repo`)를 쓴다. `repo` 부재는 REPO(기존 호출·solo 자연 퇴화)."""
    sha = (sha or "").strip()
    pathspecs = [p for p in pathspecs if p]
    if not sha or not pathspecs:
        return None
    if runner is None:
        if shutil.which("git") is None:
            return None
        repo = Path(repo) if repo is not None else REPO

        def runner(argv: list[str]) -> tuple[int, str]:
            try:
                r = subprocess.run(
                    ["git", "-C", str(repo), *argv],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=_BOARD_GIT_TIMEOUT_SECONDS)
                return r.returncode, r.stdout or ""
            except Exception:  # noqa: BLE001 — fail-soft: 타임아웃/예외를 rc≠0 로.
                return 1, ""

    try:
        rc, out = runner(["log", "--oneline", f"{sha}..HEAD", "--", *pathspecs])
    except Exception:  # noqa: BLE001 — 주입 runner raise 도 unknown(None).
        return None
    if rc != 0:
        return None
    return bool(out.strip())


# 고정 16진 commit sha 형식 — abbrev 7자+ ~ full 40(SHA-1)/64(SHA-256). `HEAD`·브랜치·태그
# 처럼 **움직이는 ref** 를 freshness anchor 로 오인하지 않기 위한 게이트(codex MF2). 이들은
# rev-parse 를 통과(rc0)하지만 `<ref>..HEAD` 가 (거의) 항상 비어 영구 false-green 이다.
_HEX_SHA_RE = re.compile(r"[0-9a-fA-F]{7,64}")


def _is_hex_sha(value: str) -> bool:
    """`value` 가 고정 16진 commit sha 형식인가 (freshness anchor 자격·`_sha_anchor_status`/
    `_canonical_commit_oid` 공유 형식 규칙·사본 금지). `HEAD`·브랜치명·태그·너무 짧은 값은 False —
    움직이는 ref 는 안정적 검증 기준이 못 된다(codex MF2)."""
    return bool(_HEX_SHA_RE.fullmatch((value or "").strip()))


# freshness anchor 판정 verdict. 사유 문구까지 한 곳에서 구분한다.
_ANCHOR_OK = "ok"                      # 고정 hex·해소·HEAD 선조 → 유효 backward anchor.
_ANCHOR_NON_SHA = "non-sha"            # 형식 아님(HEAD/브랜치/태그·움직이는 ref·MF2).
_ANCHOR_UNRESOLVED = "unresolved"      # hex 이나 이 저장소 commit 아님(rev-parse rc1·타-git SHA·MF1).
_ANCHOR_NON_ANCESTOR = "non-ancestor"  # 해소되나 HEAD 선조 아님(merge-base rc1·descendant/딴 브랜치).
_ANCHOR_AMBIGUOUS = "ambiguous"        # 축약 sha 가 다중 매칭(repo 성장) — pin 속성·unverifiable.
_ANCHOR_UNKNOWN = "unknown"            # 환경적 판정불가(git 부재·rc≥2 fatal) → silent skip.

# ambiguous 축약 sha 의 rev-parse stderr 신호 (실 git 실측·git 2.43: "error: short object ID <p> is
# ambiguous"). `--quiet` 는 이 stderr 를 억제하므로(실측) 진단은 non-quiet 재질의로 본다. 소문자
# 부분일치(버전 문구 변동 여유).
_AMBIGUOUS_STDERR_SIGNALS: tuple[str, ...] = ("ambiguous", "short object id")


def _git_run(argv: list[str], *, runner=None,
             repo: Path | None = None) -> tuple[int, str] | None:
    """git 명령의 `(rc, stdout)` 반환·git 부재/예외 → None. runner seam(hermetic 테스트)·미주입
    시 실 subprocess(`git -C repo`; 부재=REPO). rev-parse 해소 OID(prefix 검증용)가 필요한 곳이 쓴다."""
    if runner is None:
        if shutil.which("git") is None:
            return None
        repo = Path(repo) if repo is not None else REPO
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), *argv],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=_BOARD_GIT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — 타임아웃/바이너리 이상은 None(환경적).
            return None
        return r.returncode, r.stdout or ""
    try:
        rc, out = runner(argv)
    except Exception:  # noqa: BLE001 — 주입 runner raise 도 None.
        return None
    return rc, out


def _git_rc(argv: list[str], *, runner=None, repo: Path | None = None) -> int | None:
    """git 명령의 rc 만 반환(stdout 무시)·git 부재/예외 → None (`_git_run` 얇은 래퍼·사본 0)."""
    res = _git_run(argv, runner=runner, repo=repo)
    return res[0] if res is not None else None


def _rev_parse_ambiguous(sha: str, *, runner=None,
                         repo: Path | None = None) -> bool:
    """축약 `sha` 가 다중 매칭(모호)한가 — **non-quiet** rev-parse stderr 신호로 판정.

    `git rev-parse --verify --quiet <sha>^{commit}` 는 모호 시 stderr 를 **억제**(실측·rc1) 하므로,
    실패 경로에서 `--quiet` 없이 재질의해 stderr 의 `_AMBIGUOUS_STDERR_SIGNALS`(소문자 부분일치)를
    본다. 모호는 환경 오류가 아니라 **pin 의 속성**이라 호출부가 `_ANCHOR_AMBIGUOUS`(unverifiable)로
    표면화한다. git 부재/예외 → False(모호로 단정 안 함·rc 기반 분류로 폴백).

    `runner` 대역: `(rc, stdout)` 또는 `(rc, stdout, stderr)` — 3-tuple 이면 stderr 를 본다(hermetic
    테스트가 모호 stderr 를 주입)."""
    argv = ["rev-parse", "--verify", f"{sha}^{{commit}}"]   # non-quiet — stderr 억제 안 함
    if runner is None:
        if shutil.which("git") is None:
            return False
        repo = Path(repo) if repo is not None else REPO
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), *argv], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=_BOARD_GIT_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001 — 진단 실패는 False(rc 기반 분류 유지).
            return False
        stderr = r.stderr or ""
    else:
        try:
            result = runner(argv)
        except Exception:  # noqa: BLE001
            return False
        stderr = result[2] if len(result) >= 3 else ""   # 2-tuple 대역 → stderr 없음.
    low = stderr.lower()
    return any(sig in low for sig in _AMBIGUOUS_STDERR_SIGNALS)


def _sha_anchor_status(sha: str, *, runner=None,
                       repo: Path | None = None) -> tuple[str, str | None]:
    """`sha` 가 유효한 **backward freshness anchor** 인가 — `(verdict, full_oid)`.

    유효 anchor(`_ANCHOR_OK`) = (1) 고정 hex 형식 + (2) **고정 SHA 로** 유일 commit 해소(ref 아님) +
    (3) **HEAD 의 선조**. 셋 다여야 `<oid>..HEAD` range 가 "pin 이후 변경"을 정직히 센다. OK 면
    `full_oid` = rev-parse 로 얻은 **canonical full OID**(gitrevisions 유일 해소) — 호출부는
    이후 모든 git 명령(merge-base·`<oid>..HEAD` range·domain pin-이후 질의)을 **이 full OID 로** 돌려
    원 입력 문자열 재해석(ref/모호 축약 여지)을 없앤다. 비-OK verdict 는 `full_oid=None`. 실패 모드:
      - `_ANCHOR_NON_SHA`      : 형식 아님(`HEAD`/브랜치/태그·MF2) **또는** hex 이나 hex-이름 ref 로
        해소됨(`deadbeef` 라는 branch/tag — 해소 OID 가 입력을 prefix 로 안 가짐).
      - `_ANCHOR_UNRESOLVED`   : hex 이나 이 저장소 commit 아님(rev-parse rc1·타-git SHA·MF1).
      - `_ANCHOR_NON_ANCESTOR` : 해소되나 HEAD 선조 아님(`merge-base --is-ancestor` rc1).
      - `_ANCHOR_AMBIGUOUS`    : 축약 sha 가 다중 매칭(repo 성장·`_rev_parse_ambiguous` stderr 신호·
        ). 모호는 환경이 아니라 pin 속성 → unverifiable(full OID 로 재핀 안내).
    환경적 판정불가 → `_ANCHOR_UNKNOWN`(silent skip): 빈 sha·git 부재·rev-parse/merge-base **rc≥2**
    (rc128 non-repo·safe.directory·권한 등 fatal). 환경 오류를 unverifiable 로 오인하지 않는다(MF1).
    `_canonical_commit_oid`(backfill 쓰기 검증)는 verdict==OK 면 full_oid 반환(read/write 대칭).
    `runner` = 테스트 hermetic seam(argv→(rc, stdout[, stderr]))·미주입 시 실 subprocess
    (`git -C repo`; 부재=REPO)."""
    sha = (sha or "").strip()
    if not sha:
        return (_ANCHOR_UNKNOWN, None)
    if not _is_hex_sha(sha):
        return (_ANCHOR_NON_SHA, None)  # 움직이는 ref — rev-parse 시도 안 함.
    res = _git_run(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
                   runner=runner, repo=repo)
    if res is None:
        return (_ANCHOR_UNKNOWN, None)
    rc, resolved = res
    if rc != 0:
        # 실패 — `--quiet` 가 모호 stderr 를 억제하므로 non-quiet 재질의로 모호성 먼저 판정.
        # 모호(축약 다중 매칭)는 환경이 아니라 pin 속성 → unverifiable. 그 외 rc1=미해소·rc≥2=env.
        if _rev_parse_ambiguous(sha, runner=runner, repo=repo):
            return (_ANCHOR_AMBIGUOUS, None)
        if rc == 1:
            return (_ANCHOR_UNRESOLVED, None)  # 순수 미해소(repo 정상·sha 부재·타-git SHA).
        return (_ANCHOR_UNKNOWN, None)  # rc≥2 fatal(non-repo·safe.directory 등) — 환경적.
    full_oid = resolved.strip()
    # 해소 OID 가 입력 sha 를 실제 prefix 로 가져야 **고정 SHA** — 아니면 hex-이름 ref.
    if not full_oid.lower().startswith(sha.lower()):
        return (_ANCHOR_NON_SHA, None)
    # 이후 하류 명령은 **canonical full OID** 를 쓴다(원 입력 재해석 제거).
    rc = _git_rc(["merge-base", "--is-ancestor", full_oid, "HEAD"],
                 runner=runner, repo=repo)
    if rc is None:
        return (_ANCHOR_UNKNOWN, None)
    if rc == 0:
        return (_ANCHOR_OK, full_oid)
    if rc == 1:
        return (_ANCHOR_NON_ANCESTOR, None)
    return (_ANCHOR_UNKNOWN, None)  # rc≥2 fatal — 환경적.


def _unverifiable_sha_reason(status: str, sha: str) -> str:
    """anchor verdict → advisory 사유 문구 (`_verified_at_finding`·`lint_domain_freshness` 공유·DRY).

    `_ANCHOR_OK`/`_ANCHOR_UNKNOWN` 은 unverifiable 이 아니라 호출부가 이 함수를 안 부른다."""
    if status == _ANCHOR_NON_SHA:
        return f"verified_at({sha}) 이 고정 sha 아님(HEAD/브랜치/태그?)"
    if status == _ANCHOR_NON_ANCESTOR:
        return f"verified_at({sha[:12]}) 이 HEAD 선조 아님(딴 브랜치/앞선 checkout?)"
    if status == _ANCHOR_AMBIGUOUS:
        return f"verified_at({sha}) 축약 sha 가 모호(repo 성장으로 다중 매칭) — full OID 로 재핀"
    return f"verified_at({sha[:12]}) 이 이 저장소 commit 으로 해소 안 됨(타 git SHA?)"  # UNRESOLVED


_FRESHNESS_REPO_SELF = "self"
_FRESHNESS_REPO_UPSTREAM = "upstream"
_SCP_STYLE_UPSTREAM_RE = re.compile(
    r"^[^/\\:]+@[^/\\:]+:.+$|^[A-Za-z][A-Za-z0-9_.-]*:.+$")
_WINDOWS_DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _freshness_owner_repo(owner) -> tuple[Path | None, str | None]:
    """현재-진실 문서의 `repo:` 채널을 소유 git checkout 으로 해소한다.

    단일 규칙:
      - `self` → REPO. 키 부재는 파서/호출부가 `self`를 넘겨 solo로 자연 퇴화한다.
      - `upstream` → `local.conf upstream=` **경로형** checkout. 상대경로는 REPO 기준.

    URL upstream 은 lint 가 fetch/clone 하지 않는다는 기존 네트워크-0 경계를 지켜 미해소로
    남긴다. 경로 부재/이동·실 git checkout 검증 실패도 `(None, reason)` — 호출부가
    `*-unverifiable` advisory 로 표면화해 green 으로 흡수하지 않는다. 알 수 없는 `repo:`
    값(명시 null/빈 문자열·false·0·컨테이너 포함)도 동일하다.
    """
    if owner is None:
        return (None, "repo(null) 미지원 — 키 부재 또는 self/upstream 이어야 함")
    elif not isinstance(owner, str):
        return (None, f"repo({owner!r}) 미지원 — self 또는 upstream 이어야 함")
    else:
        value = owner.strip()
    if value == _FRESHNESS_REPO_SELF:
        return (REPO, None)
    if value != _FRESHNESS_REPO_UPSTREAM:
        shown = value if value else "<empty>"
        return (None, f"repo({shown}) 미지원 — self 또는 upstream 이어야 함")
    try:
        upstream = (local_config().get("upstream") or "").strip()
    except (OSError, UnicodeError, RuntimeError):
        return (None, "local.conf 읽기 실패로 upstream 소유 repo 미해소")
    if not upstream:
        return (None, "local.conf upstream 미설정으로 소유 repo 미해소")
    try:
        # pm_import.classify_upstream 과 같은 self-describing 경계의 필요한 부분만 유지한다.
        # board→pm_import 의존은 역방향이라 만들지 않는다.
        is_windows_path = bool(_WINDOWS_DRIVE_PATH_RE.match(upstream))
        is_url = (not is_windows_path and
                  ("://" in upstream or bool(_SCP_STYLE_UPSTREAM_RE.match(upstream))))
        if is_url:
            return (None, "local.conf upstream 이 URL이라 로컬 소유 repo 미해소")
        candidate = Path(upstream).expanduser()
        if not candidate.is_absolute():
            candidate = REPO / candidate
        candidate = candidate.resolve()
    except (OSError, RuntimeError):
        return (None, f"upstream 경로 해소 실패({upstream})")
    if not candidate.is_dir():
        return (None, f"upstream 경로 부재/이동({candidate})")
    if not (candidate / ".git").exists():
        return (None, f"upstream 경로가 git checkout 아님({candidate})")
    checkout = _git_run(["rev-parse", "--is-inside-work-tree"], repo=candidate)
    if checkout is None or checkout[0] != 0 or checkout[1].strip().lower() != "true":
        return (None, f"upstream 경로 git checkout 검증 실패({candidate})")
    return (candidate, None)


def _verified_at_finding(doc_file: Path, pathspecs, label: str, kind: str, *,
                         runner=None) -> list[tuple[str, str, str]]:
    """문서 `verified_at` sha 판정 → 유효 anchor 면 stale/`kind`·아니면 unverifiable advisory.

    fail-soft: 문서 부재·frontmatter 없음/깨짐·`verified_at` 부재·환경적 판정불가(`_ANCHOR_UNKNOWN`)
    → [](명시 skip — solo/신규 clone·아직 verified_at 미부여 문서 무영향·false-green 아님).

    **anchor 판정**: `verified_at` 이 유효 backward anchor
    (고정 hex + 해소 + HEAD 선조·`_sha_anchor_status`)가 아니면 종전엔 `_git_commits_between` 이
    rc≠0/빈 range 를 조용한 green 으로 흡수했다. 이제 `{kind 접두}-unverifiable` advisory 로
    표면화한다 — 형식 아님(움직이는 ref·MF2)/미해소(타-git SHA·MF1)/HEAD 선조 아님(descendant·
    딴 브랜치)를 사유 구분(never-block·억지 판정 금지). anchor OK 일 때만
    `<sha>..HEAD` stale 검사 → `(label, kind, detail)` 1개. (매핑 경로 `_CURRENT_TRUTH_ENGINE_PATHS`
    경로-관찰가능성 축은 여기 무해 — 그 축은 domain 전용.)"""
    if not doc_file.exists():
        return []
    try:
        fm, _ = load_ticket(doc_file)
    except Exception:  # noqa: BLE001 — frontmatter 없음/깨짐은 skip(비차단).
        return []
    sha = str((fm or {}).get("verified_at") or "").strip()
    if not sha:
        return []
    owner = fm["repo"] if "repo" in fm else _FRESHNESS_REPO_SELF
    owner_repo, owner_error = _freshness_owner_repo(owner)
    if owner_repo is None:
        return [(label, kind.replace("-stale", "-unverifiable"),
                 f"소유 repo freshness 검증 불가 — {owner_error} · architect 재검증 필요")]
    status, full_oid = _sha_anchor_status(sha, runner=runner, repo=owner_repo)
    if status == _ANCHOR_UNKNOWN:
        return []  # git 부재 등 환경적 판정불가 — 종전 fail-soft silent skip 유지.
    if status != _ANCHOR_OK:
        # 형식 아님/미해소/비-선조 — stale kind 와 대칭 "{x}-stale" → "{x}-unverifiable".
        return [(label, kind.replace("-stale", "-unverifiable"),
                 _unverifiable_sha_reason(status, sha)
                 + " — freshness 검증 불가·architect 재검증 필요")]
    # canonical full OID 로 stale 검사 (원 입력 재해석 제거). 표시는 원 sha(사용자 친화).
    if _git_commits_between(
            full_oid, list(pathspecs), runner=runner, repo=owner_repo) is not True:
        return []
    return [(label, kind,
             f"verified_at({sha[:12]}) 이후 매핑 경로에 커밋 있음 — architect 재검증 필요")]


def lint_architecture_freshness(*, runner=None) -> list[tuple[str, str, str]]:
    """architecture.md freshness advisory — `verified_at` sha 판정 (never-block).

    architecture.md frontmatter `verified_at: <sha>` *이후* 엔진 코드 트리
    (`_CURRENT_TRUTH_ENGINE_PATHS`)에 커밋이 있으면 "architect 재검증 필요" 권고를 낸다 —
    검증 기준 sha 이후 코드가 바뀌었다는 이진 기계 판정(date "최신이겠지" 해석 구멍 대체·
    ). kind=`architecture-stale`. verified_at 이 유효 anchor 아니면(타-git SHA·비-선조·
    움직이는 ref) `architecture-unverifiable` advisory(조용한 green 근절). 둘 다
    `_ADVISORY_LINT_KINDS` 등재·`--gate` 비기여. fail-soft: 문서 부재·frontmatter 없음·
    verified_at 부재·git 불가 → [](명시 skip)."""
    return _verified_at_finding(
        ARCHITECTURE_FILE, _CURRENT_TRUTH_ENGINE_PATHS,
        "architecture.md", "architecture-stale", runner=runner)


def lint_status_freshness(*, runner=None) -> list[tuple[str, str, str]]:
    """status.md freshness advisory — `verified_at` sha 판정 (never-block).

    status.md frontmatter `verified_at: <sha>` *이후* 엔진 코드 트리에 커밋이 있으면
    "모듈 진행 상태 재검증 필요" 권고. lint_architecture_freshness 와 동일 규칙·매핑 경로
    (status.md 는 엔진 모듈 진행 상태를 기록).
    kind=`status-stale`·verified_at 이 유효 anchor 아니면 `status-unverifiable`(advisory).
    fail-soft: 부재/verified_at 부재/git 불가 → []."""
    return _verified_at_finding(
        STATUS_FILE, _CURRENT_TRUTH_ENGINE_PATHS,
        "status.md", "status-stale", runner=runner)


def lint_domain_freshness(*, runner=None) -> list[tuple[str, str, str]]:
    """domain 페이지 freshness advisory — `verified_at` sha 판정 (never-block).

    각 domain 페이지(`covers:` 보유)의 frontmatter `verified_at: <sha>` *이후* 그 페이지
    covers 경로에 커밋이 있으면 `domain-stale` 권고. **경로 매핑은 domain.py 재사용**(신설 0):
    페이지 covers 글롭 → `domain.covers_pathspecs`(**HEAD 커밋 트리 기준 존재/부재 분할** —
    `git diff <빈-트리> HEAD`·index/staged 무관)로 git pathspec 을 만든다 — 별도 매핑 저장소를
    두지 않는다. 커밋-기준 관찰가능성이라 untracked/staged-only
    생성물은 부재로, sparse checkout tracked 는 존재로 정직히 분류한다(
    `Path.exists`·index 오판 회피).

    **관찰불가 사각을 `domain-unverifiable` advisory 로 표면화한다**(거짓 green 근절·
    판정을 억지로 만들지 않고 *검증 불가*임을 정직히 알림):
      - **anchor 판정 선행**(`_sha_anchor_status`): `verified_at` 이 유효 anchor(고정 hex+해소+
        HEAD 선조)가 아니면 형식 아님/미해소(타-git SHA)/비-선조(descendant·딴 브랜치) 사유로
        advisory — `<sha>..HEAD` range 자체가 성립 안 하니 covers 관찰가능성 판정은 하지 않는다.
      - **covers 관찰가능성**(anchor OK 일 때·`covers_pathspecs`·pin 경계): 미추적 + pin 이후
        델타 0 (never-tracked=`templates/**` 또는 pin *이전* 삭제) → `absent` → advisory.
        현재 tracked·pin 이후 삭제/rename → `present` → `<sha>..HEAD` stale 검사로.
    **소유 저장소 시계**: 페이지 `repo:`가 `self`(부재 기본)면 REPO, `upstream`이면
    `local.conf upstream=`의 경로형 checkout 에서 anchor·covers·delta를 모두 판정한다. 문서 위치는
    REPO에 그대로 두고 git 조회만 소유 repo로 바꾼다. upstream 미설정/URL/이동은
    `domain-unverifiable` advisory 이며 green 으로 흡수하지 않는다.

    advisory 는 never-block(`_ADVISORY_LINT_KINDS`)이라 `--gate` 종료코드에 기여하지 않는다.

    fail-soft: domain.py 부재/로드 실패·페이지 없음·verified_at 부재·covers 빈·git 불가(sha
    해소 None) → [](명시 skip·solo/신규 clone 무영향). `updated` date 기반 `lint_domain`
    stale 과는 별개 축(이건 sha 기준)."""
    domain = _load_domain_module()
    if domain is None:
        return []
    try:
        pages = domain.load_pages(domain.DOMAIN_DIR)
    except Exception as exc:  # noqa: BLE001 — 스캔 실패는 [] 로 흡수(board lint 정상 진행).
        if _is_engine_rev_skew(exc):
            raise
        return []
    findings: list[tuple[str, str, str]] = []
    _UNVERIFIABLE_PREFIX = ("이 저장소에서 freshness 검증 불가(소유 페이지일 수 있음·소유 "
                            "repo 에서 재검증) — ")
    for page in pages:
        sha = str(page.get("verified_at") or "").strip()
        if not sha:
            continue  # verified_at 미부여 페이지 — 명시 skip(false-green 아님).
        covers = page.get("covers") or []
        # covers 가 전부 빈/공백(코드-무관 개념)이면 sha 판정 없이 skip — git 무접촉. 비-공백 패턴은
        # 하나라도 있으면 판정한다(미지원 형태는 아래 covers_pathspecs 가 unmappable advisory 로;
        # `covers_glob_pathspec` 로 게이트하면 미지원-only 페이지가 조용히 skip 돼 false-green).
        if not any(str(g).strip() for g in covers):
            continue
        label = page.get("title") or domain.page_slug(page)
        owner_repo, owner_error = _freshness_owner_repo(page.get("repo"))
        if owner_repo is None:
            findings.append((
                label, "domain-unverifiable",
                _UNVERIFIABLE_PREFIX + f"소유 repo 미해소({owner_error})"))
            continue
        status, full_oid = _sha_anchor_status(sha, runner=runner, repo=owner_repo)
        if status == _ANCHOR_UNKNOWN:
            continue  # git 부재 등 환경적 판정불가 — 종전 fail-soft silent skip 유지.
        if status != _ANCHOR_OK:
            # sha 자체가 유효 anchor 아님 — `<oid>..HEAD` range 불가라 covers 관찰가능성 판정을
            # 하지 않는다(전제: 미해소 페이지는 pathspec 판정에 안 온다). 사유만 advisory.
            findings.append((label, "domain-unverifiable",
                             _UNVERIFIABLE_PREFIX + _unverifiable_sha_reason(status, sha)))
            continue
        # status OK — **canonical full OID** 를 pin 으로 관찰가능성/stale 판정(원 입력 재해석 제거·
        # :(glob)). covers_pathspecs 의 `<pin>..HEAD` 도 full OID 경유.
        present, absent, unmappable = domain.covers_pathspecs(
            covers, repo=owner_repo, git_runner=runner, verified_at=full_oid)
        reasons: list[str] = []
        if absent:
            reasons.append(
                f"covers 경로가 pin 이후 관찰 불가({', '.join(absent)}·pin 이전 삭제)")
        if unmappable:
            # 미지원 문법(비-경계 **)·repo 밖/절대 경로 등 두 방언 동일성 미보장 → 오번역 대신 표면화.
            reasons.append(
                f"covers 패턴 미지원(형태·repo 밖·절대 경로): {', '.join(unmappable)}")
        if reasons:
            findings.append((label, "domain-unverifiable",
                             _UNVERIFIABLE_PREFIX + " · ".join(reasons)))
        # present 글롭을 :(glob) magic pathspec 으로 넘겨 full OID 기준 stale 검사(접두사 손실 없이).
        present_specs = [domain.covers_glob_pathspec(g) for g in present]
        if present_specs and _git_commits_between(
                full_oid, present_specs, runner=runner, repo=owner_repo) is True:
            findings.append((
                label, "domain-stale",
                f"verified_at({sha[:12]}) 이후 covers 경로에 커밋 있음 — 페이지 재검증 필요"))
    return findings


# ── verified_at 초기 backfill  ────────────
# 기존 현재-진실 문서(architecture.md·status.md·covers 보유 domain 페이지)에 초기 `verified_at`
# sha 를 채운다. 런타임 fallback 누적([[prefer-data-migration-over-fallback]]) 대신 한 번의
# 마이그레이션 — 이후 lint 는 verified_at 이 항상 있다고 전제(부재는 fail-soft skip 유지).

def _repo_head_sha(repo: Path | None = None) -> str | None:
    """`repo`(부재=REPO)의 현재 HEAD sha (조회 불가 → None·fail-soft)."""
    if shutil.which("git") is None:
        return None
    repo = Path(repo) if repo is not None else REPO
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_BOARD_GIT_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 — 타임아웃/바이너리 이상은 None(fail-soft).
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


_FRONTMATTER_FENCE_RE = re.compile(r"^---\s*$")


def _insert_verified_at(text: str, sha: str) -> str | None:
    """frontmatter 닫는 `---` 바로 앞에 `verified_at: "<sha>"` 한 줄 삽입 (최소 변경).

    hand-authored 현재-진실 문서 frontmatter 를 **전체 재-dump 없이**(키 순서·date 정규화
    clobber 회피) 한 줄만 넣는다. **값은 따옴표 친 문자열**로 쓴다 — `0123456` 같은
    전부-숫자(특히 leading-zero=octal) OID 를 unquoted 로 쓰면 YAML 이 정수 파싱해 재-lint 서
    깨지기 때문(YAML string 강제). 반환 = 삽입된 새 텍스트, None = 대상 아님:
      - frontmatter 블록(`---` 시작 + 닫는 `---`)이 없음.
      - `verified_at:` 이 이미 있음(멱등·재실행 안전).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    close = next((i for i in range(1, len(lines))
                  if _FRONTMATTER_FENCE_RE.match(lines[i])), None)
    if close is None:
        return None
    if any(re.match(r"\s*verified_at\s*:", ln) for ln in lines[1:close]):
        return None
    # 닫는 --- 앞 라인이 개행으로 끝나지 않으면 보정(파일 끝 무개행 방어).
    if lines[close - 1] and not lines[close - 1].endswith("\n"):
        lines[close - 1] += "\n"
    lines.insert(close, f'verified_at: "{sha}"\n')
    return "".join(lines)


def _verified_at_backfill_targets(*, strict_domain: bool = False) -> list[Path]:
    """backfill/repin 대상 — architecture.md·status.md + covers 보유 domain 페이지.

    `strict_domain=True`는 repin의 validate-all-first 열거 모드다. frontmatter로 시작한 domain
    문서 하나라도 읽기/파싱 실패하면 예외를 전파해, 깨진 문서를 대상에서 조용히 제외한 채
    나머지만 쓰는 부분 성공을 금지한다. 일반 backfill은 기존 graceful skip을 유지한다.
    """
    targets: list[Path] = []
    if ARCHITECTURE_FILE.exists():
        targets.append(ARCHITECTURE_FILE)
    if STATUS_FILE.exists():
        targets.append(STATUS_FILE)
    domain = _load_domain_module()
    if strict_domain and domain is None and DOMAIN_PY.exists():
        raise RuntimeError(f"{DOMAIN_PY}: domain.py 로드 실패")
    if domain is not None:
        try:
            pages = (domain.load_pages(domain.DOMAIN_DIR, strict=True)
                     if strict_domain else domain.load_pages(domain.DOMAIN_DIR))
            for page in pages:
                if page.get("covers"):
                    targets.append(Path(page["path"]))
        except Exception as exc:  # noqa: BLE001 — strict repin은 열거 오류를 전체 중단으로 전파.
            if _is_engine_rev_skew(exc):
                raise
            if strict_domain:
                raise
    return targets


class _VerifiedAtPageSelectionError(ValueError):
    """`--page`가 canonical 현재-진실 문서를 가리키지 않을 때의 명시 실패."""

    def __init__(self, page: str, reason: str):
        self.path = REPO / page if page and not Path(page).is_absolute() else REPO
        super().__init__(f"--page {page!r}: {reason}")


def _verified_at_targets(
        pages: Sequence[str] | None, *, strict_domain: bool = False) -> list[Path]:
    """전체 또는 `--page`로 고른 현재-진실 문서 대상을 반환한다.

    선택자 미지정(`None`)은 기존 전체 열거를 그대로 호출한다. 선택자를 지정하면
    validate-all-first의 "all"은 **선택 집합 전체**로 좁아진다. 이때 선택 밖 문서의 파싱
    오류가 쓰기를 막지 않는 것은 의도된 계약이다. 그 문서들의 핀은 한 바이트도 바뀌지 않아
    새 green 주장이 생기지 않기 때문이다. 대신 빈/비존재/비대상 선택을 오류로 거부해
    "0개를 검증하고 성공"하는 false-green을 막고, domain 문서는 실제 소비 파서로 읽어
    covers 보유·비-draft인 canonical 대상인지 확인한다.
    """
    if pages is None:
        return _verified_at_backfill_targets(strict_domain=strict_domain)
    if not pages:
        raise _VerifiedAtPageSelectionError("", "선택 문서가 비어 있음")

    repo_root = REPO.resolve()
    fixed_targets = {Path(ARCHITECTURE_FILE).resolve(), Path(STATUS_FILE).resolve()}
    domain = None
    targets: list[Path] = []
    seen: set[Path] = set()
    for supplied in pages:
        raw = str(supplied).strip()
        relative = Path(raw)
        if not raw:
            raise _VerifiedAtPageSelectionError(raw, "빈 경로")
        if relative.is_absolute() or ".." in relative.parts:
            raise _VerifiedAtPageSelectionError(raw, "REPO 상대 경로만 허용")
        unresolved = REPO / relative
        try:
            candidate = unresolved.resolve()
            candidate.relative_to(repo_root)
        except (OSError, ValueError):
            raise _VerifiedAtPageSelectionError(raw, "REPO 밖 경로") from None
        except RuntimeError:
            # Python 3.12 의 `Path.resolve()` 는 symlink loop 에 `RuntimeError`("Symlink loop
            # from …") 를 낸다 — 3.13+ 는 같은 상황을 `OSError`(ELOOP) 로 낸다. 어느 하한에서도
            # 통제되지 않은 traceback 이 나지 않게 두 축을 함께 선택 오류로 정규화한다
            # (실측: 3.12.3 상호/자기 루프 모두 RuntimeError).
            raise _VerifiedAtPageSelectionError(raw, "경로 해소 실패(symlink loop)") from None
        if not candidate.is_file():
            raise _VerifiedAtPageSelectionError(raw, "문서가 존재하지 않음")

        if candidate not in fixed_targets:
            if domain is None:
                domain = _load_domain_module()
            if domain is None:
                raise _VerifiedAtPageSelectionError(
                    raw, "domain.py 로드 실패로 canonical domain 문서를 검증할 수 없음")
            domain_dir = Path(domain.DOMAIN_DIR).resolve()
            try:
                candidate.relative_to(domain_dir)
            except ValueError:
                raise _VerifiedAtPageSelectionError(
                    raw, "architecture.md/status.md/covers domain 문서가 아님") from None
            if candidate.suffix != ".md" or candidate.name in domain._NON_PAGE_FILES:
                raise _VerifiedAtPageSelectionError(raw, "canonical domain 문서가 아님")
            try:
                page = domain.parse_page(candidate)
            except Exception as exc:  # noqa: BLE001 — domain 소비 파서 오류를 선택 오류로 정규화.
                if _is_engine_rev_skew(exc):
                    raise
                raise _VerifiedAtPageSelectionError(
                    raw, f"domain 문서 실소비 파싱 실패:{exc}") from exc
            if page.get("status") == domain.DRAFT_STATUS or not page.get("covers"):
                raise _VerifiedAtPageSelectionError(
                    raw, "covers 보유·비-draft domain 문서가 아님")

        if candidate not in seen:
            targets.append(candidate)
            seen.add(candidate)
    return targets


def _canonical_commit_oid(sha: str, *, repo: Path | None = None) -> str | None:
    """`sha` 가 유효한 backward anchor 면 그 **canonical full OID** 를, 아니면 None (backfill 쓰기).

    backfill 이 **검증에서 얻은 full OID 를 그대로 기록**하게 하는 helper — 입력 축약/
    ref 문자열을 기록하면 (a) 하류 소비자가 다시 해석(canonical 위반) (b) `0123456` 같은 전부-숫자
    축약은 YAML unquoted 정수 파싱돼 재-lint 서 깨진다. `_sha_anchor_status` 재사용(read/write 대칭·
    사본 0): 고정 hex + 고정 SHA 해소(hex-이름 ref 아님) + HEAD 선조면 `(OK, full_oid)` →
    full_oid 반환·아니면 None. 검증 불가(git 부재·rc≥2 → UNKNOWN)도 None — 기록은 확실할 때만 한다."""
    verdict, full_oid = _sha_anchor_status(sha, repo=repo)
    return full_oid if verdict == _ANCHOR_OK else None


def backfill_verified_at(
        sha: str, *, dry_run: bool = False,
        pages: Sequence[str] | None = None) -> list[tuple[Path, str]]:
    """현재-진실 문서에 초기 `verified_at: <sha>` 를 1회 backfill.

    대상 = architecture.md·status.md + covers 보유 domain 페이지(`_verified_at_backfill_targets`).
    이미 verified_at 이 있으면/frontmatter 가 없으면 skip(멱등). 반환 = `[(경로, 상태), …]`
    (상태 ∈ `added`/`skip:already`/`skip:no-frontmatter`/`skip:read-error`). dry_run 이면 파일을
    쓰지 않고 무엇이 바뀔지만 계산한다."""
    results: list[tuple[Path, str]] = []
    for path in _verified_at_targets(pages):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            results.append((path, "skip:read-error"))
            continue
        new_text = _insert_verified_at(text, sha)
        if new_text is None:
            has_fm = text.lstrip().startswith("---")
            results.append((path, "skip:already" if has_fm else "skip:no-frontmatter"))
            continue
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")
        results.append((path, "added"))
    return results


def cmd_verified_at_backfill(args: argparse.Namespace) -> int:
    """`verified-at-backfill` — 현재-진실 문서에 초기 verified_at sha 를 채운다 (1회)."""
    sha = (args.sha or "").strip()
    if sha:
        # 명시 --sha 는 기록 전 commit 실존 검증(codex must-fix) — 오타 sha 가 영속되면
        # freshness 판정이 영구 조용 skip(false-green) 되므로 abort 가 정답. 검증에서 얻은
        # **canonical full OID 를 기록**한다(입력 축약/재해석 제거).
        canonical = _canonical_commit_oid(sha)
        if canonical is None:
            print(f"verified-at-backfill: --sha {sha} 가 이 repo 의 commit 으로 검증되지 않는다 "
                  "— 기록 중단(비존재 sha 는 freshness 판정 영구 skip·false-green).",
                  file=sys.stderr)
            return 1
        sha = canonical
    else:
        sha = _repo_head_sha() or ""   # HEAD 는 이미 full OID(canonical).
        if not sha:
            print("verified-at-backfill: --sha 미지정·HEAD 조회 실패 — 기준 sha 미정.",
                  file=sys.stderr)
            return 1
    try:
        results = backfill_verified_at(
            sha, dry_run=args.dry_run, pages=getattr(args, "pages", None))
    except _VerifiedAtPageSelectionError as exc:
        print(f"verified-at-backfill: 문서 선택 실패 — {exc}", file=sys.stderr)
        return 1
    if not results:
        print("verified-at-backfill: 대상 문서 없음 (architecture.md·status.md·domain 부재).")
        return 0
    prefix = "[dry-run] " if args.dry_run else ""
    added = 0
    for path, state in results:
        try:
            shown = path.relative_to(REPO).as_posix()
        except ValueError:
            shown = str(path)
        if state == "added":
            added += 1
            print(f"{prefix}verified_at: {sha[:12]} 삽입 → {shown}")
        else:
            print(f"{prefix}skip({state}) → {shown}")
    print(f"{prefix}{added}개 문서에 verified_at 삽입 (기준 sha {sha[:12]}).")
    return 0


def _replace_freshness_pin(text: str, sha: str, owner: str) -> str | None:
    """frontmatter 의 `verified_at`·`repo`를 한 소유 시계로 insert-or-replace한다.

    전체 YAML 재-dump 없이 두 scalar 줄만 바꿔 hand-authored 순서/날짜/본문을 보존한다.
    frontmatter가 없거나 닫히지 않으면 None. 같은 값이면 원문을 그대로 반환(호출부가 same 구분).
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    close = next((i for i in range(1, len(lines))
                  if _FRONTMATTER_FENCE_RE.match(lines[i])), None)
    if close is None:
        return None
    wanted = {"repo": owner, "verified_at": f'"{sha}"'}
    seen: set[str] = set()
    for i in range(1, close):
        # 컬럼 0 키만 freshness scalar 로 취급한다. 들여쓴 동명 키는 중첩 mapping 또는
        # block scalar 본문일 수 있으므로 건드리지 않고, 최상위 키가 없으면 아래에서 새로 넣는다.
        match = re.match(
            r"""^(?P<quote>["']?)(?P<key>repo|verified_at)(?P=quote)\s*:.*?(\r?\n)?$""",
            lines[i],
        )
        if not match:
            continue
        key, newline = match.group("key"), match.group(3) or ""
        lines[i] = f"{key}: {wanted[key]}{newline}"
        seen.add(key)
    missing = [key for key in ("repo", "verified_at") if key not in seen]
    if missing:
        if close > 0 and lines[close - 1] and not lines[close - 1].endswith(("\n", "\r")):
            lines[close - 1] += "\n"
        for key in missing:
            lines.insert(close, f"{key}: {wanted[key]}\n")
            close += 1
    return "".join(lines)


def _frontmatter_mapping(text: str) -> dict:
    """문서 frontmatter를 실제 YAML로 파싱해 mapping을 반환한다(repin 검증 전용).

    문자열 치환 성공과 YAML 안전은 별개다. 특히 `repo: >` 뒤 continuation 줄은 첫 줄만
    바꾸면 YAML이 깨지거나 `upstream <옛값>`으로 의미가 합쳐질 수 있다. 원본과 변환 결과
    모두 이 함수로 `yaml.safe_load`하고 mapping이 아니어도 안전 교체 불가로 본다.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError("frontmatter 시작 구분자 없음")
    close = next((i for i in range(1, len(lines))
                  if _FRONTMATTER_FENCE_RE.match(lines[i])), None)
    if close is None:
        raise ValueError("frontmatter 종료 구분자 없음")
    parsed = yaml.safe_load("".join(lines[1:close]))
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError(f"frontmatter가 mapping 아님({type(parsed).__name__})")
    return parsed


def _freshness_replacement_error(
        original: str, replaced: str, sha: str, owner: str) -> str | None:
    """repin 변환 전후 YAML 의미와 실소비 파서 정합을 검증한다.

    `_frontmatter_mapping`은 의미 보존 검사용이라 공백 fence/EOF fence도 읽지만, 실제 freshness
    소비자는 `load_ticket`의 엄격한 fence 문법을 쓴다. 최종 결과는 그 파서의 문자열 진입점으로
    다시 읽어, repin 성공 뒤 lint가 문서를 조용히 skip하는 false-green을 쓰기 전에 차단한다.
    """
    try:
        before = _frontmatter_mapping(original)
    except (ValueError, yaml.YAMLError) as exc:
        return f"원본 YAML 파싱 실패:{exc}"
    try:
        after = _frontmatter_mapping(replaced)
    except (ValueError, yaml.YAMLError) as exc:
        return f"변환 YAML 파싱 실패:{exc}"
    try:
        consumed, _body = _parse_ticket_text(replaced, "<repin 변환 결과>")
    except (ValueError, yaml.YAMLError) as exc:
        return f"변환 결과 실소비 파서 파싱 실패:{exc}"
    if not isinstance(consumed, dict):
        return f"변환 결과 실소비 frontmatter가 mapping 아님({type(consumed).__name__})"
    if after.get("repo") != owner or after.get("verified_at") != sha:
        return (
            "다중행/복합 freshness 값 안전 교체 불가"
            f"(변환값 repo={after.get('repo')!r}, verified_at={after.get('verified_at')!r})"
        )
    before_other = {key: value for key, value in before.items()
                    if key not in {"repo", "verified_at"}}
    after_other = {key: value for key, value in after.items()
                   if key not in {"repo", "verified_at"}}
    if after_other != before_other:
        return "freshness 외 frontmatter 의미 변경 감지"
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    """같은 디렉토리의 임시 파일을 완전히 flush한 뒤 `os.replace`로 원자 교체한다.

    임시 파일 쓰기/flush/fsync 또는 replace가 실패하면 임시 파일만 정리하고 원본은 건드리지
    않는다. 기존 파일 mode도 임시 파일에 복제해 교체가 권한을 바꾸지 않게 한다.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.chmod(tmp_path, path.stat().st_mode & 0o7777)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1  # fd 소유권은 handle로 이전됐다.
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def repin_verified_at(
        sha: str, owner: str, *, dry_run: bool = False,
        pages: Sequence[str] | None = None) -> list[tuple[Path, str]]:
    """architecture/status + covers domain 페이지를 validate-all-first 후 재핀한다.

    선택자 미지정이면 기존 일괄 대상 전부, 지정이면 선택 집합 전부를 1단계에서 읽고
    frontmatter 변환 가능 여부를 검증한다. 하나라도 읽기/frontmatter 오류면 **그 집합의 어느
    파일도 쓰지 않고** `error:*` + `not-written:validation-failed` 상태를 돌려준다. 전부
    유효할 때만 2단계 원자 교체를 시작한다. 쓰기 오류는 앞서 교체된 파일은 `updated`, 원본이
    보존된 실패 파일은 `error:write`, 뒤 대상은 `not-written:write-failed`로 명시해 호출부가
    실제 변경 범위를 정확히 보고할 수 있게 한다. dry-run은 검증까지만 하고 변경 예정 대상을
    `updated`로 표시한다.
    """
    prepared: list[tuple[Path, str, str | None]] = []
    try:
        targets = _verified_at_targets(pages, strict_domain=True)
    except Exception as exc:  # noqa: BLE001 — 대상 열거 실패도 validate-all-first 중단.
        if _is_engine_rev_skew(exc):
            raise
        error_path = Path(getattr(exc, "path", DOMAIN_PY.parent.parent / "wiki" / "domain"))
        return [(error_path, f"error:enumeration:{exc}")]
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            prepared.append((path, f"error:read:{exc}", None))
            continue
        new_text = _replace_freshness_pin(text, sha, owner)
        if new_text is None:
            prepared.append((path, "error:no-frontmatter", None))
            continue
        replacement_error = _freshness_replacement_error(text, new_text, sha, owner)
        if replacement_error is not None:
            prepared.append((path, f"error:yaml:{replacement_error}", None))
            continue
        if new_text == text:
            prepared.append((path, "same", None))
            continue
        prepared.append((path, "ready", new_text))

    if any(state.startswith("error:") for _path, state, _text in prepared):
        return [
            (path, "not-written:validation-failed" if state == "ready" else state)
            for path, state, _text in prepared
        ]

    if dry_run:
        return [(path, "updated" if state == "ready" else state)
                for path, state, _text in prepared]

    results: list[tuple[Path, str]] = []
    for index, (path, state, new_text) in enumerate(prepared):
        if state == "same":
            results.append((path, state))
            continue
        try:
            _atomic_write_text(path, new_text)
        except (OSError, UnicodeError) as exc:
            results.append((path, f"error:write:{exc}"))
            results.extend(
                (later_path, "not-written:write-failed" if later_state == "ready" else later_state)
                for later_path, later_state, _later_text in prepared[index + 1:]
            )
            break
        results.append((path, "updated"))
    return results


def cmd_verified_at_repin(args: argparse.Namespace) -> int:
    """`verified-at-repin` — 현재-진실 문서를 validate-all-first로 소유 repo full OID에 재핀한다."""
    owner = args.repo
    owner_repo, owner_error = _freshness_owner_repo(owner)
    if owner_repo is None:
        print(f"verified-at-repin: 소유 repo 미해소 — {owner_error}", file=sys.stderr)
        return 1
    requested = (args.sha or "").strip()
    if requested:
        sha = _canonical_commit_oid(requested, repo=owner_repo)
        if sha is None:
            print(f"verified-at-repin: --sha {requested} 가 {owner} repo의 HEAD 선조 commit으로 "
                  "검증되지 않는다 — 기록 중단.", file=sys.stderr)
            return 1
    else:
        sha = _repo_head_sha(owner_repo)
        if not sha:
            print(f"verified-at-repin: {owner} repo HEAD 조회 실패 — 기준 sha 미정.",
                  file=sys.stderr)
            return 1
    results = repin_verified_at(
        sha, owner, dry_run=args.dry_run, pages=getattr(args, "pages", None))
    if not results:
        print("verified-at-repin: 대상 문서 없음 (architecture.md·status.md·covers domain 부재).")
        return 0
    prefix = "[dry-run] " if args.dry_run else ""
    updated = 0
    shown_by_path: dict[Path, str] = {}
    for path, state in results:
        try:
            shown = path.relative_to(REPO).as_posix()
        except ValueError:
            shown = str(path)
        shown_by_path[path] = shown
        if state == "updated":
            updated += 1
        stream = sys.stderr if state.startswith("error:") else sys.stdout
        print(f"{prefix}{state}: {shown} → repo={owner}, verified_at={sha}", file=stream)
    validation_errors = [(path, state) for path, state in results
                         if state.startswith(("error:read:", "error:no-frontmatter",
                                              "error:yaml:", "error:enumeration:"))]
    if validation_errors:
        details = ", ".join(f"{shown_by_path[path]}({state})" for path, state in validation_errors)
        print("verified-at-repin: 전 대상 검증 실패 — 무변경·재핀 중단: " + details,
              file=sys.stderr)
        return 1
    write_errors = [(path, state) for path, state in results if state.startswith("error:write:")]
    if write_errors:
        changed = [shown_by_path[path] for path, state in results if state == "updated"]
        changed_text = ", ".join(changed) if changed else "(없음)"
        failed_text = ", ".join(shown_by_path[path] for path, _state in write_errors)
        print(f"verified-at-repin: 쓰기 실패({failed_text}) — 이미 변경된 파일: {changed_text}",
              file=sys.stderr)
        return 1
    print(f"{prefix}{updated}개 문서 재핀 (소유 repo={owner}, anchor={sha}).")
    return 0


# adapter-drift baseline 의 두 local.conf 키.
# 한 키가 baseline 과 현재-관찰을 겸하면 race/자기비교라 *분리*한다:
#   - upstream_rev      : baseline — 마지막 성공 sync 의 upstream revision (pm_import·pm_update 가 기록).
#   - upstream_seen_rev : 현재 관찰값 — URL 은 pm-update 스킬이 fetch 후 기록, 경로 upstream 은
#                         pm_update 가 sync 시 baseline 과 *함께* 기록(동기 시점 checkout rev = 관찰값·
#                         cache 부재 URL 은 이 키 부재 → graceful skip.
_DRIFT_BASELINE_KEY = "upstream_rev"
_DRIFT_SEEN_KEY = "upstream_seen_rev"


def lint_adapter_drift() -> list[tuple[str, str, str]]:
    """adapter-layer drift advisory (never-block).

    채택자의 **adapter-layer manifest-제외 파일**(facade·진입문서·settings)이 baseline(마지막 동기)
    *이후* upstream 에서 변경됐는지 가시화한다. 이 잔여는 전파 채널이 없어(B 전파=채택자
    customization clobber·비파괴 위배) 소리없이 stale 되므로, *전파 대신* PM 에게 경고만 낸다
    (kind=`adapter-drift`·`_ADVISORY_LINT_KINDS` 등재로 `--gate` 종료코드 비기여·visibility>enforcement).

    **drift 판정 = baseline B**(codex MUST-FIX 2): "공식판과 다름"(채택자 customization 오탐)이 아니라
    "마지막 동기 이후 upstream 변경". **lint 는 git network 를 하지 않는다**(codex round-2·3): `local.conf`
    의 **2개 키**만 비교한다 —

      - `upstream_rev`      (baseline·마지막 성공 sync·pm_import/pm_update 가 기록)
      - `upstream_seen_rev` (현재 관찰값·URL 은 pm-update 스킬이 fetch 후 기록·경로 upstream 은 pm_update 가
        sync 시 baseline 과 함께 기록)

    둘 다 존재하고 **다르면** drift 1 finding(두 rev 불일치 = adapter-layer 가 낡았을 수 있음). 한 키 2역
    금지(race/자기비교 회피·codex round-3 NEW-2). **메시지는 방향-중립**— lint 는 git 을 호출하지
    않으므로(rev 문자열 비교뿐) 두 rev 의 선후를 알 수 없다. 관찰값이 baseline 의 *조상*(구 흡수 잔재)인
    경우까지 "upstream 이 baseline 이후 변경됨"이라 단정하면 거짓 경보다(정상 흡수 직후 상시
    발화). 선후를 알려고 런타임 git ancestor 판정을 넣지 않고, 기록 시점 정합
    (pm_update.record_upstream_rev_baseline 이 경로 upstream 에서 두 키 동시 기록)으로 원천 해소한다.

    scope: 대상 = manifest-제외 adapter-layer(settings.json·루트 doc·
    facade·진입문서·local.conf — adopter config·전파 채널 없음) / 제외 = instance-state(status·
    architecture·tickets·log·decisions·README 스캐폴드·lite — 채택자 소유·diverge 정상). **hooks·driver 는
    engine-mirror 전파 대상**(manifest 등록) — pm-update 가 동기하므로
    silent-stale 클래스가 "대상 단정 안 함". lint 가 파일 단위 diff 를
    하지 않으므로(rev 비교만) scope 는 advisory 메시지로 안내한다.

    fail-soft / 관찰가시성:
      - `upstream` 미설정(솔로·non-adopter·templates/upstream 부재 환경) → [].
      - baseline(`upstream_rev`) 미기록(아직 revision 추적 전·구 import) → [](관찰 기준점 자체 부재).
      - baseline 은 있으나 seen(`upstream_seen_rev`) 미기록(cache 부재 URL·pm-update 미실행) → **관찰불가
        advisory 1줄**(never-block). 과거엔 조용한 [](silent skip)였으나, hooks/driver 등 safety-critical
        잔여가 *관찰 없이* 낡으면 "green 인데 고장"(checkpoint 가드 미발화·회귀 게이트 무력)이라 관찰불가 자체를
        표면화한다. advisory 라 `--gate` 미차단·1줄이라 flood 아님.
    """
    findings: list[tuple[str, str, str]] = []
    conf = local_config()

    # 솔로/non-adopter — upstream 자체가 없으면 비교할 대상이 없다 (graceful).
    if not (conf.get("upstream") or "").strip():
        return findings

    baseline = (conf.get(_DRIFT_BASELINE_KEY) or "").strip()
    seen = (conf.get(_DRIFT_SEEN_KEY) or "").strip()

    # baseline 미기록(구 import·revision 추적 전) → graceful [] (관찰 기준점 자체가 아직 없음).
    if not baseline:
        return findings

    # baseline 은 있으나 seen(현재 관찰값) 미기록 — drift 를 판정할 수 없다. 과거엔 조용한 []
    # (silent skip)였으나, hooks/driver·adapter-layer 잔여가 *관찰 없이* 낡으면 "green 인데 고장"
    # 이라 관찰불가 자체를 advisory 로 표면화한다(never-block·1줄이라 flood 아님).
    if not seen:
        findings.append((
            "adapter-layer", "adapter-drift",
            f"upstream_seen_rev 미기록 — upstream 관찰값이 없어 baseline({baseline[:12]}) 이후 "
            f"adapter-layer(settings·루트 doc·facade) drift 를 판정할 수 없음(관찰불가). "
            f"`pm-update`(upstream fetch)로 관찰값을 기록하면 추적된다 (never-block·hooks/driver 는 전파됨)"))
        return findings

    # 두 rev 가 같으면 baseline 이후 upstream 변경 없음 → clean.
    if baseline == seen:
        return findings

    # 다름 = 두 rev 불일치. 어느 쪽이 앞선지는 알 수 없으므로(git 미호출) **방향-중립**으로
    # 알리고, adapter-layer(facade·진입문서·settings)가 낡았을 가능성만 PM 에게 안내한다
    # (전파 아님·never-block). 실제 선후·변경분은 `pm-update --changes` 가 판정한다.
    findings.append((
        "adapter-layer", "adapter-drift",
        f"upstream baseline({baseline[:12]}) 과 관찰({seen[:12]}) 불일치 — "
        f"adapter-layer(facade·진입문서·settings) 가 낡았을 수 있음. "
        f"`pm-update` 로 동기 (instance-state·README·lite 는 채택자 소유·제외)"))
    return findings


def _load_domain_module():
    """domain.py 를 경로 import 해 모듈로 반환한다 (부재/실패 시 None).

    **순환 회피 deep-import seam** — domain.py 가 `board.load_ticket` 을 import 하므로
    board 가 모듈 최상단에서 domain 을 import 하면 순환이다. lint_domain *함수 내부*에서만
    이 헬퍼로 지연 로드한다. domain.py 부재
    (솔로/신규 clone·구버전)·로드 실패 → None (호출부가 graceful skip).
    """
    if not DOMAIN_PY.exists():
        return None
    try:
        mod = _load_module_from_path(
            DOMAIN_PY, "domain.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — 로드 실패는 None 으로 흡수(비차단).
        if _is_engine_rev_skew(exc):
            raise  # domain 사본 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def lint_domain() -> list[tuple[str, str, str]]:
    """domain freshness finding 을 board lint finding 으로 표면화 (advisory·비차단).

    domain.lint_pages 의 `(kind, label, detail)` 를 board 관례 `(label, kind, detail)` 로
    재배열해 돌려준다. kind 는 domain 의 `stale`/`orphan`/`oversized`/`history` 를 보존 —
    `_ADVISORY_LINT_KINDS` 에 등재돼 `--gate` 종료코드에 *절대* 기여하지 않는다(visibility>
    enforcement). domain.py 부재·로드 실패·깨진 페이지·git 부재 → [] (솔로/domain 미사용
    프로젝트 무영향). domain.py 가 이미 graceful 이므로 얇게 위임하되, 어떤 예외도 [] 로
    흡수해 board lint 자체는 항상 정상 진행한다.

    date freshness도 페이지별 `repo:` 소유 시계로 판정한다. owner checkout별 runner를
    캐시해 같은 repo 페이지끼리 공유하고, 소유 repo 미해소/runner 생성 실패는 그 페이지 stale을
    unknown으로 둔다(sha freshness 축은 별도 `domain-unverifiable` advisory로 표면화).
    """
    domain = _load_domain_module()
    if domain is None:
        return []
    try:
        # DOMAIN_DIR 을 명시 전달 — load_pages 의 기본 인자는 정의 시점에 굳어
        # monkeypatch(테스트)·재바인딩을 못 본다(domain.cmd_lint 동형). 호출 시점의
        # 모듈 전역 DOMAIN_DIR 을 읽게 한다.
        pages = domain.load_pages(domain.DOMAIN_DIR)
        runners: dict[Path, object] = {}

        def git_runner_factory(page):
            owner_repo, _owner_error = _freshness_owner_repo(page.get("repo"))
            if owner_repo is None:
                return None
            owner_repo = Path(owner_repo)
            if owner_repo not in runners:
                runners[owner_repo] = domain._real_git_runner(owner_repo)
            return runners[owner_repo]

        findings = domain.lint_pages(pages, git_runner_factory=git_runner_factory)
    except Exception as exc:  # noqa: BLE001 — 어떤 실패도 빈 결과로 흡수(board lint 정상 진행).
        if _is_engine_rev_skew(exc):
            raise
        return []
    # domain (kind, label, detail) → board (label, kind, detail) 재배열.
    return [(label, kind, detail) for kind, label, detail in findings]


def _load_pm_delegate_module():
    """pm_delegate.py 를 경로 import 해 모듈로 반환한다 (부재/실패 시 None).

    **순환 회피 deep-import seam** — pm_delegate 의 `lint_same_model` 은 순수 함수(board 를
    import 하지 않는다)라 board 가 이 헬퍼로 지연 로드해 호출한다(`_load_pm_update_module` 동형·
    stamped sibling 검증 포함). pm_delegate.py 부재(구버전 clone)·일반 로드 실패 → None (호출부
    graceful skip), 사본 skew 만은 재-raise 해 부분 동기를 숨기지 않는다."""
    if not PM_DELEGATE_PY.exists():
        return None
    try:
        mod = _load_module_from_path(
            PM_DELEGATE_PY, "pm_delegate.py", verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — 일반 로드 실패만 None, 중첩 skew는 fail-loud.
        if _is_engine_rev_skew(exc):
            raise
        return None
    return mod


def lint_delegate() -> list[tuple[str, str, str]]:
    """delegate 동일-모델 dev/reviewer 경고를 board lint finding 으로 표면화 (advisory·never-block).

    pm_delegate.lint_same_model(conf) 의 `(label, detail)` 를 board 관례 `(label, kind, detail)` 로
    감싼다. kind=`delegate-same-model`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드에 *절대*
    기여하지 않는다·visibility>enforcement — 사용자가 동일-모델 조합을 선택할 자유 유지). pm_delegate.py
    부재·일반 로드 실패·설정 미매핑 → [] (delegate 미사용 프로젝트·솔로 무영향). 사본 skew 는
    로더와 이 소비 지점 양쪽에서 재-raise 하고, 그 밖의 예외만 [] 로 흡수한다. board 의
    local.conf(local_config)로 판정한다."""
    try:
        mod = _load_pm_delegate_module()
        if mod is None:
            return []
        findings = mod.lint_same_model(local_config())
    except Exception as exc:  # noqa: BLE001 — 일반 실패만 빈 결과, skew는 fail-loud.
        if _is_engine_rev_skew(exc):
            raise
        return []
    return [(label, "delegate-same-model", detail) for label, detail in findings]


def lint_areas_duplicate_repo() -> list[tuple[str, str, str]]:
    """areas.md 에 같은 repo 행이 2개 이상이면 advisory (never-block).

    first-match 리졸버(`_repo_protected`·`_repo_base`·`_areas_git_url`·`_repo_area_owner`)가
    전부 첫 매칭 행에서 return 하므로 중복 행은 **조용히 first-match 로 굳는다** — 사용자가
    보는 표와 엔진이 읽는 값이 갈린다. legacy inline 형상(`merge=union` 활성)에서 두 clone 이
    같은 행을 각자 고치면 실제로 생길 수 있다. 자동 병합은 하지 않고(사람 판정) 존재만 보인다.
    `areas_set_cell`(setter)의 fail-loud 와 짝 — 그쪽은 쓰기를 막고, 이쪽은 상시 가시화한다.

    kind=`areas-duplicate-repo`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 비기여·push
    미차단). areas.md 부재/파싱 실패는 빈 결과(솔로 무영향).
    """
    try:
        _header, rows = _parse_areas()
    except Exception:  # noqa: BLE001 — areas 파싱 실패는 빈 결과(lint 를 깨지 않는다).
        return []
    counts: dict[str, int] = {}
    for row in rows:
        repo = row.get("repo")
        if repo:
            counts[repo] = counts.get(repo, 0) + 1
    return [
        (repo, "areas-duplicate-repo",
         f"areas.md 에 repo 행이 {n}개 — first-match 로 조용히 굳는다(어느 행이 이기는지 "
         f"merge 순서에 달림). 한 행만 남기고 수동 정리하라 "
         f"({_rel_to_repo(areas_file())}). 정리 전엔 `pm-config repo protected {repo} …` "
         "같은 셀 변경이 거부된다.")
        for repo, n in sorted(counts.items()) if n > 1
    ]


def lint_areas_merge_union() -> list[tuple[str, str, str]]:
    """areas.md 의 `merge=union` 이 **그 파일을 담은 git** 에 선언돼 있나 (never-block).

    union 이 빠지면 두 clone 의 동시 등록이 merge 에서 한쪽 행을 잃거나 충돌한다 — 레지스트리
    설계가 기대는 보장이 조용히 사라지는 상태라 상시 가시화한다. 형상별로 *유효한* 선언 위치가
    다르므로 그에 맞는 파일만 본다(`_BOARD_AREAS_ATTR_TARGETS` / `_INLINE_AREAS_ATTR_TARGETS`):
      - board 분리 → `board_root()/.gitattributes` 의 `areas.md merge=union`
      - inline     → 루트 `.gitattributes` 의 `.project_manager/areas.md merge=union`
    판정은 **파일 내용**만 본다 — `git check-attr` 를 런타임에 부르지 않는다(비용·이식성).
    그래서 사각이 둘 있다(둘 다 advisory 라 무해·완전 판정은 check-attr 이 필요): in-tree
    `.gitattributes` 보다 우선하는 `.git/info/attributes`·`core.attributesFile` 은 보이지
    않는다(거기서 union 을 줬어도 여기선 미배포로 읽힌다) · glob 패턴 한계는
    `_gitattributes_merge_attr` docstring 참조.

    kind=`areas-merge-union`(`_ADVISORY_LINT_KINDS` 등재 → `--gate` 종료코드 비기여·push 미차단).
    areas.md 부재(솔로 미등록)·읽기 실패는 빈 결과.
    """
    af = areas_file()
    if not af.exists():
        return []
    root = board_root()
    separated = (root / ".git").exists() and af.parent == root
    if separated:
        attrs, targets = root / ".gitattributes", _BOARD_AREAS_ATTR_TARGETS
        fix = (f"`{_rel_to_repo(attrs)}` 에 `areas.md merge=union` 을 추가하라 — 다음 board "
               "mutation(claim/new/complete 등)이 자동 보강해 그 commit 으로 함께 push 한다.")
    else:
        attrs, targets = REPO / ".gitattributes", _INLINE_AREAS_ATTR_TARGETS
        fix = (f"`{_rel_to_repo(attrs)}` 에 `.project_manager/areas.md merge=union` 을 "
               "추가하라(inline 형상의 선언 위치).")
    try:
        text = attrs.read_text(encoding="utf-8") if attrs.is_file() else ""
    except (OSError, UnicodeError):  # 읽기 실패·비-UTF8 은 무발화(advisory 가 lint 를 깨지 않는다).
        return []
    if _areas_union_declared(text, targets):
        return []
    return [(_rel_to_repo(af), "areas-merge-union",
             f"areas.md 에 merge=union 이 유효하지 않다 — 동시 등록(다른 clone 의 행 append)이 "
             f"merge 에서 충돌하거나 한쪽 행을 잃는다. {fix}")]


def lint_tickets() -> list[tuple[str, str, str]]:
    """All lint issues — ticket dependency graph + body self-containment +
    idea status/directory agreement + status.md ✅ 완성 행 누적 권고(judgment-only) +
    dangling wikilink + unstable (slug/filename) refs +
    family wiki scope 인지+
    domain freshness advisory(stale/orphan/oversized/history·never-block) +
    architecture.md freshness advisory(architecture-stale·never-block) +
    adapter-layer drift advisory(adapter-drift·never-block·baseline rev 비교) +
    render-leak(리터럴 `{{...}}` 누출·blocking·@render 산출물 한정·활성화 전 무발화) +
    un-migrated-overlay(어댑터 .md 리터럴 free-form 토큰 잔존·advisory·never-block) +
    adr-author(ADR `author: <user>/<pm-slot>` provenance 권고·advisory·never-block) +
    현재-진실 문서 freshness(architecture-stale·status-stale·domain-stale·verified_at sha 판정·
    advisory·never-block) +
    areas-duplicate-repo(areas.md 중복 repo 행 → first-match 고착 가시화·advisory·
    never-block) +
    areas-merge-union(areas.md 를 담은 git 에 merge=union 미배포 → 동시 등록 안전 상실 가시화·
    advisory·never-block) +
    delegate-same-model(delegate dev/reviewer 동일-모델 해소 → generate≠evaluate 침식 가시화·
    advisory·never-block)."""
    return (lint_dependencies() + lint_bodies() + lint_ideas()
            + lint_status()
            + lint_wikilinks() + lint_unstable_refs() + lint_scopes()
            + lint_domain() + lint_adr_lifecycle() + lint_adr_author()
            + lint_architecture_freshness() + lint_status_freshness()
            + lint_domain_freshness() + lint_adapter_drift()
            + lint_render_leak() + lint_unmigrated_overlay()
            + lint_areas_duplicate_repo() + lint_areas_merge_union()
            + lint_delegate())


# ── board.md regeneration ──────────────────────────────────────────────

def refresh_board() -> None:
    """Regenerate .project_manager/wiki/board.md.

    scan(tickets/) + render + write 를 *하나의* `board_lock()` 구간 안에서 한다
    (공유 단일파일 lost-update 방지). write 만 감싸면, 동시 변경 시
    A 가 stale 스냅샷을 떠 둔 사이 B 가 scan+write 를 끝내도, A 가 락을 잡아 자기
    stale 스냅샷으로 board.md 를 덮어써 B 의 갱신을 유실한다. scan 까지 락 안에서
    하면 *마지막 writer 가 모든 선행 write 이후의 ticket 상태를 scan* 하므로
    board.md 가 항상 최신을 반영한다.

    **재진입 금지**(board_lock docstring) — board_lock 보유 중에는 부르지 않는다.
    모든 호출자(cmd_new·claim·complete·block·unclaim·unblock·refresh)는 락 밖에서
    부른다(cmd_new 는 ID-발행 락 블록이 끝난 뒤 호출).
    """
    with board_lock():
        _refresh_board_locked()


def _refresh_board_locked() -> None:
    """board.md 재생성의 scan+render+write 본체. **board_lock 보유 전제**."""
    by_status: dict[str, list[dict]] = {s: [] for s in STATUS_DIRS}
    for status in STATUS_DIRS:
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            by_status[status].append(fm)

    lines: list[str] = [
        "---",
        "title: Ticket Board",
        "type: dashboard",
        f"updated: {now_utc()}",
        "---",
        "",
        "# Ticket Board",
        "",
        "> 자동 생성 파생물 (git-untracked) — `board.py` 변경 명령마다 로컬 갱신 · `board.py refresh` 로 재생성. 단일 진실은 `tickets/`, 라이브 상태는 `board.py list`. 수동 편집 금지.",
        "> 작업 흐름: [`tickets/README.md`](tickets/README.md).",
        "",
    ]
    totals = " · ".join(f"{s}={len(by_status[s])}" for s in STATUS_DIRS)
    lines.append(f"**현황:** {totals}")
    lines.append("")

    emoji = {"open": "🟢", "claimed": "🟡", "blocked": "🔴", "done": "✅"}

    for status in STATUS_DIRS:
        items = by_status[status]
        # Skip the done section header when empty so the board stays focused on live work
        if status == "done" and not items:
            continue
        lines.append(f"## {emoji[status]} {status.upper()} ({len(items)})")
        lines.append("")
        if not items:
            lines.append("*없음*")
            lines.append("")
            continue
        if status == "open":
            lines.append("| ID | Title | depends_on | touches | tags |")
            lines.append("|---|---|---|---|---|")
            for fm in items:
                dep = ", ".join(fm.get("depends_on") or []) or "—"
                tch = ", ".join((fm.get("touches") or [])[:3]) or "—"
                tag = ", ".join(_tag_values(fm))
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | {dep} | {tch} | {tag} |"
                )
        elif status == "claimed":
            lines.append("| ID | Title | Claimed by | Since (UTC) |")
            lines.append("|---|---|---|---|")
            for fm in items:
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"`{fm.get('claimed_by','')}` | {(fm.get('claimed_at') or '')[:19]} |"
                )
        elif status == "blocked":
            lines.append("| ID | Title | (reason at the bottom of the file) |")
            lines.append("|---|---|---|")
            for fm in items:
                lines.append(f"| {fm['id']} | {fm.get('title','')} | — |")
        elif status == "done":
            # Show most-recent 10
            lines.append("| ID | Title | Completed (UTC) |")
            lines.append("|---|---|---|")
            recent = sorted(items, key=lambda f: f.get("completed_at") or "",
                            reverse=True)[:10]
            for fm in recent:
                lines.append(
                    f"| {fm['id']} | {fm.get('title','')} | "
                    f"{(fm.get('completed_at') or '')[:19]} |"
                )
        lines.append("")

    # scan+render+write 가 모두 호출자(refresh_board)의 board_lock 구간 안이다 —
    # 마지막 writer 가 모든 선행 write 이후 상태를 scan 하므로 stale write 가 없다.
    BOARD_FILE.write_text("\n".join(lines), encoding="utf-8")


# ── argparse ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="board.py",
                                     description="Multi-session ticket board.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list tickets")
    p.add_argument("--status", choices=(*STATUS_DIRS, "all"),
                   help="status 뷰 셀렉터: 생략 시 활성만(open/claimed/blocked·done 접기)· "
                        "특정 status 하나만 보려면 그 값(예: done)· 전체(done 포함)는 `all`.")
    p.add_argument("--tag")
    p.add_argument("--mine", action="store_true",
                   help="내 것만 (렌즈·단일 보드 위 필터·user-first): 내 open"
                        "(area_owner==나 ∨ created_by==나) + 내 claim(claimed_by.user==나)·**전 슬롯**. "
                        "querying identity=현재 사용자(local.conf user= > git email). 타 사용자는 "
                        "안 나온다. solo(user 미상)는 전체 open + 내 슬롯 claim 으로 graceful degrade. "
                        "`--repo`/`--slot` 과 상호 배타(뷰 스코프는 하나만·cmd_list 런타임 검사).")
    # `--repo`/`--slot` 조회 전용 세션 뷰(`--repo X --slot N`, kind="slot")는 현재 사용자와
    # 세션이 모두 일치하는 생성 open + claim만 비춘다. 무인자 기본 뷰와 같은 의미론이며,
    # `--repo X` 단독은 기존 user 단위 렌즈다. actor 명령과 플래그명은 같지만 여기서는
    # 아무것도 바꾸지 않는다. 전체 보드(타 사용자 포함)는 `list --all`로 본다.
    p.add_argument("--all", action="store_true",
                   help="전체 보드: 무인자 기본 뷰(내 세션 스트림만 = 생성 open + 내 claim)를 "
                        "끄고 모든 세션·타 사용자 티켓을 상세로 보인다(경합 가시·타 PM 열람). 뷰 스코프라 "
                        "`--mine`/`--repo`/`--slot`/`--task` 와 상호 배타.")
    identity_args.add_identity_args(p)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id", metavar="T-NNNN")
    identity_args.add_identity_args(p)
    p.add_argument("--user", help="user 식별자 — claimed_by 의 user 차원 (default: local.conf user= / "
                   "git config user.email)")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("complete", help="mv claimed → done (sync gate enforced)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--tests-pass", action="store_true",
                   help="assert the regression suite passes "
                        "(required unless --allow-untested)")
    p.add_argument("--allow-missing-log", action="store_true",
                   help="bypass the log/current.md entry check")
    p.add_argument("--allow-untested", action="store_true",
                   help="bypass the regression check "
                        "(regression-irrelevant ticket)")
    p.set_defaults(fn=cmd_complete)

    p = sub.add_parser("block", help="mv open|claimed → blocked")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("unclaim", help="mv claimed → open")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_unclaim)

    p = sub.add_parser("unblock", help="mv blocked → open")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_unblock)

    p = sub.add_parser("new", help="create a new ticket")
    p.add_argument("title")
    p.add_argument("--touches", help="comma-separated file paths")
    p.add_argument("--depends", help="comma-separated ticket IDs")
    p.add_argument("--tag", help="comma-separated tags")
    p.add_argument("--estimate", choices=["small", "medium", "large"],
                   default="small")
    p.add_argument("--prefix", help="작업 카테고리 (자유 입력·배타 구획). "
                   "default: local.conf prefix / 없으면 none(무prefix 1급 → legacy T-NNNN)")
    p.add_argument("--user", help="user 식별자 — created_by 의 user 차원 (default: local.conf user= / "
                   "git config user.email)")
    p.add_argument("--task", help="task 이름 — task-mode 발행. `--prefix` 생략 시 task "
                   "설정 prefix(기본 없음)·created_by 는 <user>/<task>. 슬롯 세션 예약 패턴 <repo>_<N> 금지.")
    # `--repo`/`--slot`— 생성-세션 기록. claim 과 동일 identity 해소 경로
    # (`_actor_session_override`)를 재사용해 created_by = `<user>/<repo>_<N>` 로 박는다(세션 기본
    # 뷰 스트림 판정 입력). `--task`와 공존은 fail-loud. 미명시 시 현행
    # (user-only / 유도 세션) 유지. `add_identity_args` 는 `--task` 를 이미 위에서 등록해 미사용.
    p.add_argument("--repo", metavar="이름", default=None,
                   help="repo 이름 — 생성-세션 정체성. `--slot` 과 함께면 created_by = "
                        "<user>/<repo>_<N>. 단독이면 그 repo 의 활성 슬롯 유도(≥2 모호면 fail-loud).")
    p.add_argument("--slot", metavar="N", type=int, default=None,
                   help="슬롯 번호 — `--repo` 필수(단독 불가). 함께 주면 생성-세션 <repo>_<N>.")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("promote", help="draft(board-git 미커밋) 티켓을 승격 — 본문 채운 뒤 board-git sync "
                        "(발행 규율 게이트: board-git 공유 시 `new` 가 미충전 티켓을 draft 로 남긴다)")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_promote)

    p = sub.add_parser("init", help="clone 당 1회 setup (solo · multi-repo N×M) — pm_state·local.conf·pre-push 훅")
    p.add_argument("--prefix", help="multi-repo (N×M) ID 네임스페이스 (예: PAY). 생략 = solo(legacy T-NNNN)")
    p.add_argument("--area", help="영역 설명 (namespaced: 새 prefix 최초 등록 시 필요)")
    p.add_argument("--owner", help="등록 식별자(registrant·기본: session 이름)")
    p.add_argument("--user", help="area_owner = 그 area 의 user 소유 (`--mine` 풀 입력). "
                                  "미지정 시 local.conf user= / git config user.email 로 해소(없으면 빈 값).")
    identity_args.add_identity_args(p)
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("migrate-identity",
                       help="이전 데이터 일회성 backfill — areas area_owner·ticket "
                            "created_by·슬롯-only claimed_by (멱등·비파괴·dry-run 선검토). "
                            "단일-세션 op: 다른 세션이 claim/complete 중일 땐 실행 말 것"
                            "(조용한 창에서 1회). areas write 는 락 보호·티켓 backfill 은 "
                            "best-effort(동시 이동 시 해당 티켓 skip).")
    p.add_argument("--dry-run", action="store_true",
                   help="변경 미리보기(쓰기 0·per-file 요약). 먼저 실행 권장.")
    p.add_argument("--user", help="identity override (기본: local.conf user= / git config "
                   "user.email · 미해소 시 abort)")
    identity_args.add_identity_args(p)  # slot 표시값(기본: $PM_SESSION_NAME/local.conf session=) —
    # backfill 대상 슬롯을 *바꾸지 않음*. 슬롯-only claimed_by 는 기존 슬롯 토큰을 보존하고
    # user 차원만 prepend(비파괴).
    p.add_argument("--scope", choices=["active", "all"], default="all",
                   help="active=open+claimed 만 · all=done 포함(기본)")
    p.set_defaults(fn=cmd_migrate_identity)

    p = sub.add_parser("regression",
                       help="회귀 게이트 (run=측정·기록 / check=HEAD green 검증·pre-push 훅용)")
    p.add_argument("action", choices=["run", "check"])
    p.add_argument("--cmd", help="테스트 명령 (기본: 활성 repo areas.md test_cmd → local.conf test_cmd → pytest -q)")
    p.add_argument("--cwd", help="회귀 실행 cwd (seam·기본 REPO; multi-PM은 활성 repo worktree 배선)")
    identity_args.add_identity_args(p)  # 명시 슬롯(이 슬롯만 회귀·M>1 홈에서 무명시면 전 leased
    # 슬롯 all-or-nothing
    p.add_argument("--ticket", help="이 ticket 의 touches 로 스코프 (dev 빠른 루프·advisory)")
    p.add_argument("--touches", help="comma-separated 파일로 스코프 (advisory)")
    p.set_defaults(fn=cmd_regression)

    p = sub.add_parser("livegate",
                       help="릴리즈 라이브 게이트 — record=`pytest -m release` "
                            "실행·수집 pin 강제·기록 / check=보호훅이 HEAD-매칭 green 검증")
    p.add_argument("action", choices=["record", "check"])
    p.add_argument("--rev", help="check 대상 sha (보호훅이 push HEAD 를 넘김·record 는 무시)")
    p.add_argument("--cwd", help="record=pytest 실행 cwd(기본=활성 slot worktree) / "
                                 "check=livegate.json 해소 cwd(훅 정렬·기본=이 board.py 사본 REPO) "
                                 "(seam·record↔check 대칭)")
    identity_args.add_identity_args(p)  # record 의 슬롯(M>1 홈에서 cwd 해소·regression 과 동형·
    # 무명시+leased≥2 는 fail-loud·`--cwd` 우회 불요)
    p.set_defaults(fn=cmd_livegate)

    p = sub.add_parser("git-anchor", help=argparse.SUPPRESS)
    p.add_argument("--cwd", required=True, help=argparse.SUPPRESS)
    p.add_argument("--command", required=True, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_git_anchor)

    p = sub.add_parser("refresh", help="regenerate board.md")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("lint", help="check depends_on / blocks consistency")
    p.add_argument("--gate", action="store_true",
                   help="push 게이트 모드 — 차단 카테고리(dangling/unstable-ref/dependency/"
                        "thin)에만 종료코드 1, status drift 자문성은 0")
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser(
        "verified-at-backfill",
        help="현재-진실 문서(architecture.md·status.md·domain)에 초기 verified_at sha 채움 (1회)")
    p.add_argument("--sha", help="기준 커밋 sha (미지정 시 REPO HEAD — released 지점 권장)")
    p.add_argument(
        "--page", dest="pages", action="append", metavar="REPO_RELATIVE_PATH",
        help="이 현재-진실 문서만 backfill (REPO 상대경로·복수 지정 가능)")
    p.add_argument("--dry-run", action="store_true", help="쓰기 0·무엇이 바뀔지만 표시")
    p.set_defaults(fn=cmd_verified_at_backfill)

    p = sub.add_parser(
        "verified-at-repin",
        help="현재-진실 문서의 repo+verified_at을 소유 저장소 full OID로 일괄 전환")
    p.add_argument("--repo", choices=[_FRESHNESS_REPO_SELF, _FRESHNESS_REPO_UPSTREAM],
                   required=True,
                   help="freshness 소유 시계 — self 또는 local.conf의 경로형 upstream")
    p.add_argument("--sha", help="소유 repo 기준 커밋 sha (미지정 시 그 repo HEAD)")
    p.add_argument(
        "--page", dest="pages", action="append", metavar="REPO_RELATIVE_PATH",
        help="이 현재-진실 문서만 재핀 (REPO 상대경로·복수 지정 가능)")
    p.add_argument("--dry-run", action="store_true",
                   help="쓰기 0·페이지→repo/full-anchor 매핑 미리보기")
    p.set_defaults(fn=cmd_verified_at_repin)

    p = sub.add_parser("promote-scope",
                       help="family wiki scope retag — `repoA → shared`")
    p.add_argument("file", help="대상 문서 (decisions/·specs/ 의 .md · REPO 상대 또는 절대)")
    p.add_argument("--to", required=True,
                   help="새 family_scope 값 — `shared` 또는 repo prefix")
    p.set_defaults(fn=cmd_promote_scope)

    # idea subcommand group — pre-ADR candidates under ideas/{open,promoted,killed}/
    idea = sub.add_parser("idea", help="manage pre-ADR ideas")
    idea_sub = idea.add_subparsers(dest="idea_cmd", required=True)

    ip = idea_sub.add_parser("list", help="list ideas")
    ip.add_argument("--status", choices=IDEA_STATUS_DIRS)
    ip.add_argument("--tag")
    ip.set_defaults(fn=cmd_idea_list)

    ip = idea_sub.add_parser("new", help="create a new idea in open/")
    ip.add_argument("title")
    ip.add_argument("--tag", help="comma-separated tags")
    ip.set_defaults(fn=cmd_idea_new)

    ip = idea_sub.add_parser("promote", help="mv idea open → promoted")
    ip.add_argument("id")
    ip.set_defaults(fn=cmd_idea_promote)

    ip = idea_sub.add_parser("kill", help="mv idea open → killed")
    ip.add_argument("id")
    ip.set_defaults(fn=cmd_idea_kill)

    # reid — 단일 티켓 ID 재부여. 번호·prefix 변경 무손실
    # relabel + 전 참조 rewrite. prefix rename/merge 와 같은 파이프라인(`_prefix_relabel`) 재사용.
    p = sub.add_parser(
        "reid",
        help="단일 티켓 ID 재부여 <OLD-ID> <NEW-ID> — 번호·prefix 변경 무손실 relabel + 전 참조 rewrite")
    p.add_argument("old_id", metavar="OLD-ID", help="재부여할 기존 티켓 ID (예: T-0036)")
    p.add_argument("new_id", metavar="NEW-ID",
                   help="새 티켓 ID — T-NNNN 또는 T-<prefix>-NNN (발행 문법·prefix 자유 입력)")
    identity_args.add_identity_args(p)  # claim 중 티켓의 소유 세션 확인용
    p.add_argument("--dry-run", action="store_true", help="규모 preview(N ID·M refs·K 파일)·쓰기 0")
    p.set_defaults(fn=cmd_reid)

    # prefix subcommand group — 작업 카테고리 prefix 관리. list=현황(read-only) +
    # rename/strip/merge/delete=개명·통합 (`none`=무prefix 1급·collision abort·board-git 백업).
    prefix_p = sub.add_parser("prefix", help="ticket-ID prefix (작업 카테고리) 관리")
    prefix_sub = prefix_p.add_subparsers(dest="prefix_cmd", required=True)

    pp = prefix_sub.add_parser("list", help="prefix별 티켓 수·번호범위 현황 (read-only·비파괴)")
    pp.set_defaults(fn=cmd_prefix_list)

    pp = prefix_sub.add_parser(
        "rename", help="카테고리 개명 <A|none> <B|none> — 무충돌=번호유지 교체·충돌=merge 안내")
    pp.add_argument("src", help="원본 카테고리 (또는 none=무prefix)")
    pp.add_argument("dst", help="대상 카테고리 (또는 none=이름 지우기)")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview(N ID·M refs·K 파일)·쓰기 0")
    pp.set_defaults(fn=cmd_prefix_rename)

    pp = prefix_sub.add_parser("strip", help="이름 지우기 <A> — = rename <A> none 별칭")
    pp.add_argument("prefix", help="지울 카테고리")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview·쓰기 0")
    pp.set_defaults(fn=cmd_prefix_strip)

    pp = prefix_sub.add_parser(
        "merge", help="통합 <A> [B...] --into <T|none> — created 순(기본 append·저위험)")
    pp.add_argument("sources", nargs="+", help="통합할 source 카테고리(들) (또는 none)")
    pp.add_argument("--into", required=True, help="대상 카테고리 (또는 none)")
    pp.add_argument("--reorder-chronological", action="store_true",
                    help="전체 interleave 재번호(opt-in·고위험 17k refs). 기본=append(대상 max 뒤·기존 번호 무변경)")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview·쓰기 0")
    pp.set_defaults(fn=cmd_prefix_merge)

    pp = prefix_sub.add_parser(
        "delete", help="빈(0티켓) 카테고리 등록 제거 — 티켓 있으면 fail-loud(rename/merge 안내)")
    pp.add_argument("prefix", help="제거할 (빈) 카테고리")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview·쓰기 0")
    pp.set_defaults(fn=cmd_prefix_delete)

    return parser


def _main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # PM-홈 worktree 오실행 가드 — mutation subcommand 전수·단일 dispatch 지점.
    # board 상태를 쓰는 명령만(read·sidecar 제외) 착지 *전에* fail-loud. 분류는 위 상수·미래
    # 누락은 메타 가드 테스트가 잡는다.
    subcommand = _resolved_subcommand(args)
    if subcommand in _MUTATION_SUBCOMMANDS and _guard_worktree_misanchor(f"board.py {subcommand}"):
        return 1
    if subcommand in _READ_SUBCOMMANDS:
        resolution = _resolve_read_board(REPO)
        _print_read_anchor(
            subcommand=subcommand,
            pm_home=resolution.home if resolution.home != REPO else None,
            pm_inputs_missing=resolution.error is not None,
        )
        if resolution.error is not None:
            print(f"[중단] {resolution.error}", file=sys.stderr)
            return 1
        local_root = _board_root_at(REPO)
        if resolution.home is not None and resolution.home != REPO:
            with _read_pm_inputs_at(resolution.home, resolution.root):
                return args.fn(args)
        # 자기 board/solo/standalone은 import-time REPO 경로 그대로다. 별도 context에 넣지 않아
        # 폴백 문구·경로 재바인딩이 전혀 생기지 않는다.
        if resolution.root == local_root:
            return args.fn(args)
        # 방어적 경계: 현재 resolver 계약상 home 없는 비local root는 나올 수 없다.
        with _read_pm_inputs_at(REPO, resolution.root):
            return args.fn(args)
    return args.fn(args)


def main(argv: list[str] | None = None) -> int:
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
