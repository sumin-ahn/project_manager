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
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"
IDEAS_DIR = REPO / ".project_manager" / "wiki" / "ideas"
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"
SPECS_DIR = REPO / ".project_manager" / "wiki" / "specs"
ARCHITECTURE_FILE = REPO / ".project_manager" / "wiki" / "architecture.md"  # 현재-아키텍처 단일 진실 (ADR-0022·freshness lint 비교 대상)
HOOKS_DIR = REPO / ".project_manager" / "hooks"  # instance-owned lint hooks (ADR-0003)
BOARD_FILE = REPO / ".project_manager" / "wiki" / "board.md"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
STATUS_FILE = REPO / ".project_manager" / "wiki" / "status.md"
TEMPLATE_FILE = TICKETS_DIR / "_template.md"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored): prefix, session
AREAS_FILE = REPO / ".project_manager" / "areas.md"    # shared registry (committed, merge=union)
PM_STATE_FILE = REPO / ".project_manager" / "wiki" / "pm_state.md"          # per-clone (git-ignored)
PM_STATE_TEMPLATE = REPO / ".project_manager" / "wiki" / "pm_state.template.md"  # tracked skeleton
LOCAL_DIR = REPO / ".project_manager" / ".local"            # per-clone scratch (git-ignored)
REGRESSION_FLAG = LOCAL_DIR / "regression.json"             # last regression result, keyed by HEAD
BOARD_LOCK = LOCAL_DIR / "board.lock"                       # OS file lock — board write serialization (ADR-0012)
# worktree_pool 의 LEASES_FILE 와 *같은 위치*(그 규약 — `.local/worktree-leases.json`). board 는
# worktree_pool 을 import 하지 않으므로(ADR-0013 isolation·touches 격리) 경로를 자체 해소해 파일을
# 직접 read 한다(T-0066 슬롯 test_cmd 레이어·아래 _active_slot_test_cmd). areas.md 읽듯 데이터-결합만.
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"            # worktree 리스 장부 (ADR-0013·read-only here)
DOMAIN_PY = REPO / ".project_manager" / "tools" / "domain.py"  # domain lint deep-import (순환 회피·아래 lint_domain)
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")
# Ideas have a simpler lifecycle than tickets — no claim/complete middle
# states, just `open → promoted|killed`.
IDEA_STATUS_DIRS: tuple[str, ...] = ("open", "promoted", "killed")


# ── utilities ──────────────────────────────────────────────────────────

def local_config() -> dict[str, str]:
    """Per-clone local config (`.project_manager/local.conf`, git-ignored).

    Plain `KEY=value` lines; `#` comments and blank lines ignored. Missing → {}.
    Holds per-clone settings that must NOT be shared via git (prefix, session) —
    multi-repo (N×M·prefix 네임스페이스) 셋업의 per-clone 로컬 상태. Written by `pm-init`.
    """
    conf: dict[str, str] = {}
    if not LOCAL_CONF.exists():
        return conf
    for line in LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def session_name(override: str | None = None) -> str:
    """세션 식별자 해소 — 4단 우선순위 (T-0073·worktree_pool/pm_config 와 *동형*):

        override
          > $PM_SESSION_NAME (env·harness 무관 엔진 식별자)
          > $CLAUDE_SESSION_NAME (env·deprecated alias·silent back-compat)
          > local.conf `session=`
          > `<host>-<pid>`

    `PM_SESSION_NAME` 이 정식 이름(엔진 변수·하니스 무관). `CLAUDE_SESSION_NAME` 은
    구 이름으로 alias 만 — 둘 다 설정 시 `PM_SESSION_NAME` 승(마이그레이션 중 명시 우선).
    deprecation 경고는 print 하지 않는다(`--json`/machine-parse 출력 오염 방지) — alias 는
    조용히 동작하고 안내는 문서에만 둔다.

    저장측(worktree_pool._default_session)과 매칭측(여기)이 어긋나면 per-slot test_cmd·
    claim 소유권이 미스되므로([[T-0066]] must-fix) 세 모듈을 같은 우선순위로 통일한다.
    """
    if override:
        return override
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    sess = local_config().get("session")
    if sess:
        return sess
    return f"{socket.gethostname()}-{os.getpid()}"


def id_prefix(override: str | None = None) -> str | None:
    """Resolve ticket-ID namespace prefix (multi-repo areas·N×M·ADR-0016).

    prefix 는 M>1 repo 의 ID 네임스페이스(협업용 아님) — solo(N=1·M=1)는 부재.
    Order: override > local.conf `prefix=` > None. None → legacy `T-NNNN`
    (graceful / backward compatible). Non-None → `T-<PREFIX>-NNN` namespace.
    """
    if override:
        return override
    return local_config().get("prefix") or None


# areas.md 신/구 스키마 (ADR-0014 · T-0075 · T-0076).
#   - 구 스키마: `| prefix | area | owner |`                      (멀티-CLONE·ADR-0005)
#   - per-repo: `| repo | prefix | git | test_cmd | owner |`      (per-repo 레지스트리·ADR-0014)
#   - base 스키마: `| repo | prefix | git | test_cmd | owner | base |`  (base 브랜치·T-0075)
#   - 신 스키마: `| repo | prefix | git | test_cmd | owner | base | protected |`  (보호브랜치·T-0076)
# 파싱은 **헤더 행을 읽어 칼럼명→인덱스**로 매핑한다(위치-비의존) — 모든 스키마를 같은
# 코드로 읽고, 누락 칼럼은 빈 값으로 떨어뜨려 하위호환을 보장한다(`base`/`protected` 칼럼
# 없는 구 레지스트리 → 행 dict 에 그 키 없음 → `_repo_base`/`_repo_protected` 가 폴백).
_AREAS_SEP_RE = re.compile(r"^\|[\s:|-]+\|?$")  # markdown 구분선 `|---|---|`

# 보호 브랜치 default (T-0076·엔진 상수) — areas.md `protected` 칼럼이 미지정/미등록일 때
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


# areas.md canonical 칼럼 순서 (신 스키마·ADR-0014·T-0075·T-0076). 구 헤더(`base`/`protected`
# 없음)는 이 순서의 *prefix* 다(`repo|prefix|git|test_cmd|owner` 또는 …`|base`). 그래서 헤더보다
# 셀이 많은 행(구 헤더에 신 칼럼 row 가 append 된 *업그레이드* 프로젝트 — `repo add` 가 완전
# canonical row 를 더 짧은 헤더에 붙인 경우)을 이 canonical 순서로 이어 매핑해 `base`/`protected`
# 유실을 막는다(codex T-0075 게이트가 base 에 대해 건 가드를 protected 추가로 7칸까지 확장).
_AREAS_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base", "protected")


def _parse_areas() -> tuple[list[str], list[dict[str, str]]]:
    """areas.md 를 (header 칼럼명 리스트, 데이터 행 dict 리스트) 로 파싱한다.

    헤더-인식: 첫 table row 를 칼럼명(소문자)으로 보고, 이후 데이터 행을
    `{칼럼명: 셀값}` 으로 매핑한다. 누락 칼럼은 빈 문자열. 신/구 스키마 공용.

    **신 스키마 행 관용(하위호환·codex T-0075·T-0076)**: `areas_append` 는 *항상* 그 시점의 완전한
    canonical per-repo row 를 쓴다(T-0075=6칸 base·T-0076=7칸 protected). 구 헤더(6칸 base / 5칸
    per-repo `repo|…|owner` / 3칸 멀티-clone `prefix|area|owner`)에 그 *더 넓은* row 가 append 된
    업그레이드 프로젝트에서, 헤더 길이만큼만 매핑하면 `protected`/`base`(또는 `repo` 등)가
    유실/오매핑된다 → **셀 수가 헤더보다 많으면**(=더 넓은 신 스키마 row 를 더 좁은 구 헤더에 붙임)
    헤더와 무관하게 `_AREAS_COLUMNS`(canonical) 순서로 매핑한다(append-only 보존·파일 미수정).
    `== canonical폭` 만 보면 *직전 버전*(6칸)이 append 한 row 가 _AREAS_COLUMNS 가 7칸으로 자란 뒤
    헤더 매핑으로 떨어져 `base` 유실(codex T-0076) → `> len(header)` 가 6칸·7칸 신 row 둘 다 보존.
    셀 수가 헤더 이하인 행(구 6/5/3칸 데이터 row)은 자기 헤더로 매핑(현행). areas.md 부재 → ([], []).
    """
    if not AREAS_FILE.exists():
        return [], []
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in AREAS_FILE.read_text(encoding="utf-8").splitlines():
        cells = _split_areas_row(line)
        if cells is None:
            continue
        if not header:
            header = [c.lower() for c in cells]
            continue
        if len(cells) > len(header):
            # 헤더보다 넓은 행 = 신(더 넓은) 스키마 canonical row 를 더 좁은 구 헤더에 append 한
            # 업그레이드 케이스(T-0075 6칸·T-0076 7칸 row 를 5/3칸 헤더 아래). canonical 순서로
            # 매핑해 base/protected 유실 차단. 폭 초과(>canonical)는 col{i} 폴백(방어).
            row = {
                (_AREAS_COLUMNS[i] if i < len(_AREAS_COLUMNS) else f"col{i}"): cells[i]
                for i in range(len(cells))
            }
        else:
            row = {header[i]: (cells[i] if i < len(cells) else "")
                   for i in range(len(header))}
        rows.append(row)
    return header, rows


def _areas_row_for_prefix(prefix: str) -> dict[str, str] | None:
    """활성 prefix 의 areas.md 데이터 행(dict). 미등록/부재 → None."""
    _header, rows = _parse_areas()
    for row in rows:
        if row.get("prefix") == prefix:
            return row
    return None


def _repo_base(repo: str) -> str | None:
    """그 repo 의 areas.md `base` 브랜치 (T-0075). 미지정/미등록/구 스키마 → None.

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


def _repo_protected(repo: str) -> list[str]:
    """그 repo 의 보호 브랜치 목록 (T-0076). 미지정/미등록/구 스키마 → `DEFAULT_PROTECTED`.

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


def registered_prefixes() -> set[str]:
    """Prefixes registered in areas.md (shared registry). Empty set if no registry.

    The registry's *existence* is the multi-repo (N×M·prefix 네임스페이스) mode
    signal — when present, `board.py new` requires a registered prefix (see
    cmd_new guard). solo(N=1·M=1)는 레지스트리 부재 → 가드 off.

    헤더-인식 파서(`_parse_areas`)로 `prefix` 칼럼을 읽는다 — 구 스키마
    (`| prefix | … |`)와 신 스키마(`| repo | prefix | … |`·ADR-0014) 모두에서
    prefix 칼럼 위치에 상관없이 동작한다.
    """
    _header, rows = _parse_areas()
    return {p for row in rows if (p := row.get("prefix"))}


def areas_append(prefix: str, area: str, owner: str,
                 *, repo: str | None = None, git: str | None = None,
                 test_cmd: str | None = None, base: str | None = None,
                 protected: str | None = None) -> None:
    """Register a prefix in areas.md (append-only; create with header if missing).

    Append-only + `merge=union` (.gitattributes) → concurrent registrations from
    different clones never conflict.

    헤더 최초 생성(if-absent) + row append 를 **하나의 `board_lock()`** 구간으로
    원자화한다 (ADR-0012). 락이 없으면 동시 최초 등록 2개가 둘 다 "not exists" 를
    보고 → 둘 다 헤더를 write_text 해 한쪽이 다른쪽 append row 를 클로버한다(row 만
    O_APPEND 라도 헤더 race 가 남음). 락으로 감싸면 동시 최초 등록에도 헤더 1회·모든
    row 보존.

    스키마(ADR-0014·T-0075·T-0076): per-repo 레지스트리
    `| repo | prefix | git | test_cmd | owner | base | protected |`.
    `owner` = **등록 식별자(registrant)** — 협업 소유자(다중-사람)가 아니라 single user
    의 등록 출처 표식이다(ADR-0016·ADR-0002 amend). 기본 = 현 세션. 컬럼/형식은 보존
    (test_path 바인딩·regression 게이트가 의존) — 의미만 재정의.
    `repo`/`git`/`test_cmd`/`base`/`protected` 미지정 시 빈 칼럼으로 채운다(부분 등록
    허용·하위호환). `base`(T-0075)는 worktree 슬롯 브랜치가 파생될 base 브랜치 — 빈 값/
    누락이면 `_repo_base` 가 None 폴백(worktree add 가 현행 bare HEAD 동작). `protected`
    (T-0076)는 PM 이 자율 commit/push 못 하는 보호 브랜치(쉼표분리) — 빈 값/누락이면
    `_repo_protected` 가 `DEFAULT_PROTECTED`(main/master/develop) 폴백.
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
    with board_lock():
        if not AREAS_FILE.exists():
            AREAS_FILE.write_text(
                "# Area Registry\n\n"
                "> per-repo 레지스트리 (ADR-0014·T-0075·T-0076): repo → prefix → git → "
                "test_cmd → owner → base → protected. 멀티-PM ID 네임스페이스 + per-repo "
                "테스트 경로 + worktree base 브랜치 + 보호 브랜치의 단일 진실. "
                "append-only (`merge=union`).\n"
                "> `board.py init` / `pm-config repo add` 가 등록. "
                "prefix 유일성 = race-free ID 의 전제.\n\n"
                "| repo | prefix | git | test_cmd | owner | base | protected |\n"
                "|---|---|---|---|---|---|---|\n",
                encoding="utf-8")
        # O_APPEND atomic append (ADR-0012) — areas 는 append-only 레지스트리이므로
        # read-modify-write 가 아니라 OS 가 보장하는 원자 추가로 동시 등록 충돌을 없앤다.
        _append_atomic(
            AREAS_FILE,
            f"| {_repo} | {prefix} | {_git} | {_test} | {owner} | {_base} "
            f"| {_protected} |\n")


# ── 보드 동시성 (ADR-0012) ────────────────────────────────────────────────
# 단일 루트 동시 세션이 공유 `.project_manager` 파일을 안전하게 쓰게 한다.
#   - board_lock: OS 파일락 — ID 발행(new)·공유 단일파일 write(board.md) 직렬화.
#     프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock 없음).
#   - _append_atomic: O_APPEND — log/areas 같은 append-only 파일의 원자 추가.
#   - claim(`move_ticket` atomic rename)은 이미 원자적이라 신규 락을 안 씌운다.
#
# 크로스플랫폼(stdlib-only — 런타임 의존은 PyYAML 뿐): POSIX=fcntl.flock,
# Windows=msvcrt.locking. 둘 다 없으면 단일-머신 전제의 무락 폴백(락 파일만 생성).


def _flock_acquire(fd: int) -> None:
    """OS 배타락 획득 (블로킹). POSIX=fcntl.flock·Windows=msvcrt.locking·폴백 no-op.

    stdlib 만 사용한다 (외부 `filelock` 의존 금지). 둘 다 임포트 안 되는 희귀 환경은
    단일-머신 전제로 무락 폴백 — 락 파일 자체는 존재하므로 인터페이스는 동일하다.
    """
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        # 첫 1바이트에 배타락 — 블로킹(LK_LOCK). 빈 파일이면 한 바이트 확보가 필요.
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
def board_lock() -> Iterator[None]:
    """공유 보드 write 를 직렬화하는 OS 파일락 컨텍스트매니저 (ADR-0012).

    `.project_manager/.local/board.lock` 에 배타 OS 락을 건다. **프로세스가 죽으면
    OS 가 락을 자동 해제**하므로 stale-lock 이 없다(worktree 리스의 pid-회수와 수명이
    다른 이유). 읽기(list/show)는 락을 잡지 않는다 — *변경* 경로만 직렬화한다.

    **재진입 금지** — 같은 프로세스가 이 컨텍스트를 중첩하면 안 된다(flock 의 재진입
    동작은 OS 별로 다름). `cmd_new` 의 ID 발행 트랜잭션과 `refresh_board` 의 board.md
    write 는 *각자 독립* 락 구간으로 분리한다(중첩 아님).
    """
    BOARD_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(BOARD_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _flock_acquire(fd)
        try:
            yield
        finally:
            _flock_release(fd)
    finally:
        os.close(fd)  # close 만으로도 OS 가 락을 해제 (크래시 시 안전망)


def _append_atomic(path: Path, text: str) -> None:
    """O_APPEND 로 텍스트를 원자 추가한다 (ADR-0012 — log/areas 같은 append-only).

    `O_APPEND` 는 각 write 의 offset 이동+기록을 OS 가 원자로 처리해, 동시 writer 가
    서로의 추가를 덮어쓰지 않는다(read-modify-write 의 lost update 회피). 파일이 없으면
    생성한다. 인코딩은 엔진 규약대로 UTF-8.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


# ── 회귀 게이트 (R8) ──────────────────────────────────────────────────────
# 회귀 단위 ≡ push 단위 · green 인 것만 push. `regression run` 이 측정·기록(per-clone
# 로컬 플래그), pre-push 훅이 `regression check` 로 HEAD green 을 검증. 비차단 pre-warm 은
# PM 이 `run_in_background` 로 `regression run` 을 돌리는 워크플로(하니스 background).

def _git_head() -> str:
    r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout.strip() if r.returncode == 0 else ""


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
    """Install the R8 pre-push gate (회귀 + lint). Idempotent. False if not a git repo.

    두 단계를 AND 로 묶는다:
      1. 회귀 게이트 (R8) — green 회귀만 push (`regression check` 실패 시 `regression run`).
      2. lint 게이트 (T-0036) — `lint --gate` 차단 카테고리(dangling/unstable-ref/
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
        "# pm pre-push gate (R8 + T-0036) — green 회귀 AND lint 게이트만 push. board.py init 이 설치.\n"
        f"{py} .project_manager/tools/board.py regression check || \\\n"
        f"  {py} .project_manager/tools/board.py regression run || exit 1\n"
        f"{py} .project_manager/tools/board.py lint --gate || exit 1\n",
        encoding="utf-8")
    hook.chmod(0o755)
    return True


def _active_slot_test_cmd() -> str | None:
    """활성 worktree 슬롯(lease)에 바인딩된 test_cmd (T-0066·ADR-0014 amend·없으면 None).

    같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 1/2/3·full vs a-only 등)을
    지속적으로 가질 수 있게 — `_test_cmd` 가 이를 repo areas *위* 레이어로 끼운다.

    **board 는 worktree_pool 을 import 하지 않는다**(ADR-0013 isolation·touches 격리).
    대신 리스 장부 *파일*(`LEASES_FILE` = `.local/worktree-leases.json`·worktree_pool 과
    같은 위치)을 stdlib json 으로 직접 read 한다 — areas.md 를 읽듯 데이터-결합만(모듈 결합
    아님). 리스는 worktree_pool 이 atomic-replace(`os.replace`)로 쓰므로 **락 없는
    point-read 가 일관 스냅샷**을 본다(쓰기 경합과 분리 — 부분쓰기 장부를 못 본다).

    활성 슬롯 = `session_name()` == lease.session && state=="leased" 인 첫 행. 그 행의
    test_cmd 가 비어 있지 않으면 반환. 장부 부재/파싱실패/매칭없음/빈 test_cmd → None
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
    sess = session_name()
    for row in leases:
        if not isinstance(row, dict):
            continue
        if row.get("session") == sess and row.get("state") == "leased":
            cmd = row.get("test_cmd")
            return cmd or None   # 빈/None → None (이 활성 슬롯엔 바인딩 없음·다음 레이어로)
    return None


def _active_slot_path() -> str | None:
    """활성 worktree 슬롯(lease)의 절대 경로 (T-0122·ADR-0026·없으면 None).

    분리된 PM 홈(코드 없음)+worktree 모델([[ADR-0026]])에서 회귀는 활성 repo 의
    worktree cwd 에서 돌아야 한다 — 이 함수가 그 경로를 lease 장부에서 해소한다.

    `_active_slot_test_cmd` 와 *동형* 데이터-결합: **worktree_pool 을 import 하지 않고**
    (ADR-0013 isolation) 리스 장부 파일(`LEASES_FILE`)을 stdlib json 으로 직접 read 한다.
    slot 식별자는 `work/` 접두를 이미 포함(`work/<repo>_<N>`)하므로 worktree_pool 의
    `slot_path()`(= `REPO / slot`)와 동일하게 board 가 import 없이 `REPO / lease["slot"]` 로 직접 구성한다.
    리스는 worktree_pool 이 atomic-replace 로 쓰므로 락 없는 point-read 가 일관 스냅샷을 본다.

    활성 슬롯 = `session_name()` == lease.session && state=="leased" 인 첫 행. 그 행의
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
    sess = session_name()
    for row in leases:
        if not isinstance(row, dict):
            continue
        if row.get("session") == sess and row.get("state") == "leased":
            slot = row.get("slot")
            if not slot:
                return None  # 빈/None slot → None (다음 레이어[REPO]로)
            return str(REPO / slot)
    return None


def _test_cmd(override: str | None) -> str:
    """회귀에 쓸 테스트 명령을 해소한다 (ADR-0014 per-repo + T-0066 per-slot).

    우선순위:
      1. `override` (CLI `--cmd`).
      2. **활성 슬롯(lease)의 test_cmd** — worktree 리스 장부에서 이 세션의 leased 슬롯에
         바인딩된 명령(`_active_slot_test_cmd`). 같은 repo 슬롯별 빌드변형을 수용한다
         (T-0066·ADR-0014 amend). 장부 부재/매칭없음/빈 값이면 다음 레이어로 폴백.
      3. **활성 repo 의 areas.md test_cmd** — 멀티-PM 모드(areas.md 존재)에서
         활성 prefix(`id_prefix()`)의 레지스트리 행에 비어 있지 않은 `test_cmd` 가
         있으면 그것. per-repo 스택(pytest/go test…)을 수용한다.
      4. **솔로 폴백** — 위 전부 미스면 현 단일 `local.conf test_cmd`
         (없으면 `pytest -q`). 100% 하위호환(장부 없는 솔로/multi-PM-미배선 무영향).
    """
    if override:
        return override
    slot_cmd = _active_slot_test_cmd()
    if slot_cmd:
        return slot_cmd
    prefix = id_prefix()
    if prefix:
        row = _areas_row_for_prefix(prefix)
        if row and row.get("test_cmd"):
            return row["test_cmd"]
    return local_config().get("test_cmd") or "pytest -q"


def _regression_cwd(override: str | None = None) -> str:
    """회귀를 실행할 작업 디렉토리를 해소한다 (ADR-0014 cwd seam).

    multi-PM 모델에선 코드가 활성 repo 의 **worktree** 에 있고 multi-PM 루트(`REPO`)엔 코드/테스트가
    없다 — 회귀는 worktree cwd 에서 돌아야 한다(spike §8-4 c·[[ADR-0026]] 홈+worktree 표준).
    이 함수는 그 cwd 를 주입 가능한 seam 으로 노출한다.

    해소 순서 (T-0058 seam → T-0122 주입 완성):
      - `override`(CLI `--cwd`·미래 호출자가 worktree 경로를 넘김) 가 있으면 그것,
      - 없으면 **활성 슬롯 경로**(`_active_slot_path` — lease 장부에서 이 세션의 leased
        슬롯 worktree 경로·worktree_pool 미import),
      - 그것도 없으면 **현 `REPO` 기본** (솔로/multi-PM-미배선 — additive·솔로 무변경).
    """
    if override:
        return override
    return _active_slot_path() or str(REPO)


def _interp_runs(cmd: str) -> bool:
    """후보 인터프리터가 *실제로* 실행되는지 `--version` rc 로 검증한다.

    존재하지만 죽은 shim (Windows 의 비기능 `python3` WindowsApps 별칭 등) 을
    걸러내기 위함 — `shutil.which` 의 존재 확인만으론 부족하다. 짧은 timeout·
    예외 전부 흡수해 탐지가 절대 실패하지 않게 한다(fail-soft).
    """
    try:
        r = subprocess.run([cmd, "--version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=5)
        return r.returncode == 0
    except Exception:
        return False


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
    존재하지만 죽은 shim 을 건너뛴다. 아무 것도 통과 못 하면 `"python3"` 리터럴 폴백
    (리눅스 현행 동치). **bare 명령**을 반환한다(which 의 절대경로가 아니라) —
    subprocess 가 PATH 해석하고, CLAUDE.md `{{PY}}` 표시에도 가독하다.
    """
    candidates = ("python", "py", "python3") if os.name == "nt" else ("python3", "python")
    for cand in candidates:
        if shutil.which(cand) and _interp_runs(cand):
            return cand
    return "python3"


# ── ctx 임계 (context 정지-핸드오프 — T-0013) ──────────────────────────────
# 어댑터 훅(opencode·claude)이 컨텍스트 잔여 비율로 nudge/stop 을 판정할 기본값.
# local.conf `ctx_nudge_pct`·`ctx_stop_pct` 로 per-clone 조정 가능 (board.py init 기록).
CTX_NUDGE_PCT_DEFAULT = 20  # 잔여 ≤ 이 % → "곧 정지" nudge (아직 일은 계속).
CTX_STOP_PCT_DEFAULT = 10   # 잔여 ≤ 이 % → 정지·핸드오프 트리거 임계.
# 핸드오프 토큰 예산(위 nudge/stop %의 기준). 어댑터 ctx_guard.CTX_WINDOW_TOKENS_DEFAULT
# 와 값을 동기 — board 는 ctx_guard 를 import 하지 않고(touches 격리) 리터럴을 보유한다
# (nudge/stop pct 도 동형으로 board 자체 상수). 큰 물리 window(1M) 모델이라도 낮게 두면
# 이른 핸드오프 = 토큰 경제이므로 기본은 200K 유지(auto-detect 안 함). init 이 local.conf surface.
CTX_WINDOW_TOKENS_DEFAULT = 200000


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

    반환: {"nudge_pct": N, "stop_pct": M}. local.conf 우선·없으면 엔진 기본(20/10).
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


def cmd_regression(args: argparse.Namespace) -> int:
    """run = 측정+기록(HEAD 키), check = HEAD 가 green 인지 (pre-push 훅이 호출)."""
    if args.action == "run":
        touches = (_ticket_touches(args.ticket) if getattr(args, "ticket", None)
                   else (args.touches.split(",") if getattr(args, "touches", None) else []))
        scoped = bool(touches)
        parts = [_test_cmd(args.cmd)]
        if scoped:
            parts.append(_scope_args(touches))
        parts.append(_quarantine_args())
        cmd = " ".join(p for p in parts if p)
        print(f"regression: $ {cmd}")
        # shell=True 로 띄운 pytest 자식은 별도 프로세스 — 부모 콘솔 reconfigure 보호를
        # 못 받는다. 자식의 인코딩을 도구가 코드로 명시(env 워크어라운드 아님): 한국어
        # Windows(cp949 콘솔)에서도 자식 stdout/stderr·파일 IO 를 UTF-8 로 강제.
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        # cwd seam (ADR-0014) — multi-PM 모델은 활성 repo 의 worktree 에서 돌아야 한다.
        # `--cwd` 주입(미래·T-0060 bootstrap) 시 그 경로, 미주입(솔로/multi-PM-미배선)은 REPO.
        cwd = _regression_cwd(getattr(args, "cwd", None))
        rc = subprocess.run(cmd, shell=True, cwd=cwd, env=env).returncode
        status = "pass" if rc in (0, 5) else "fail"  # pytest rc5 = no tests collected
        if scoped:
            # 스코프 실행 = dev 빠른 피드백 (advisory). full 만 push 게이트 → 게이트 플래그 안 씀.
            print(f"regression(scoped, {len(touches)} touches): {status} (rc={rc}) "
                  "— dev 피드백 · push 게이트 아님")
            return 0 if status == "pass" else 1
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        REGRESSION_FLAG.write_text(json.dumps(
            {"head": _git_head(), "status": status, "rc": rc, "scope": "full",
             "ts": now_utc()}), encoding="utf-8")
        print(f"regression: {status} (rc={rc}) @ {_git_head()[:8] or '?'}")
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
        print(f"regression: RED @ {head[:8]} — push 차단.", file=sys.stderr)
        return 1
    print(f"regression: green @ {head[:8]} ✓")
    return 0


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
    """Return (status_dir, path). Raises FileNotFoundError if missing."""
    return find_item(TICKETS_DIR, STATUS_DIRS, tid, "ticket")


def find_idea(iid: str) -> tuple[str, Path]:
    """Return (status_dir, path) for idea `iid`. Raises FileNotFoundError."""
    return find_item(IDEAS_DIR, IDEA_STATUS_DIRS, iid, "idea")


def load_ticket(path: Path) -> tuple[dict[str, Any], str]:
    """Return (frontmatter dict, body string)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"missing frontmatter: {path}")
    # Split on the FIRST closing '---' after the opener
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        raise ValueError(f"unterminated frontmatter: {path}")
    fm_text = after_open[:end]
    body = after_open[end + 5:]
    fm = yaml.safe_load(fm_text) or {}
    return fm, body


def dump_ticket(path: Path, fm: dict[str, Any], body: str) -> None:
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    path.write_text(f"---\n{fm_text}\n---\n{body}", encoding="utf-8")


def move_item(base_dir: Path, src: Path, dst_status: str) -> Path:
    """Atomic mv of an item file into a sibling status directory.

    The POSIX rename(2) is the lock — a lost race surfaces as FileNotFoundError.
    Generic over tickets and ideas.
    """
    dst = base_dir / dst_status / src.name
    os.rename(src, dst)
    return dst


def move_ticket(src: Path, dst_status: str) -> Path:
    """Atomic mv into a ticket status directory."""
    return move_item(TICKETS_DIR, src, dst_status)


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


def _next_id(prefix: str | None = None) -> str:
    """Next ticket ID. Namespaced per prefix so concurrent areas never collide.

    prefix=None → legacy `T-NNNN` (4-digit). prefix="PAY" → `T-PAY-NNN` (3-digit),
    counted independently (scans only `T-PAY-*`). The legacy regex `T-(\\d+)-`
    never matches a prefixed file, so the two namespaces stay disjoint.
    """
    if prefix:
        n = next_numeric_id(TICKETS_DIR, STATUS_DIRS,
                            f"T-{prefix}-*.md", rf"T-{re.escape(prefix)}-(\d+)-")
        return f"T-{prefix}-{n:03d}"
    n = next_numeric_id(TICKETS_DIR, STATUS_DIRS, "T-*.md", r"T-(\d+)-")
    return f"T-{n:04d}"


def _next_idea_id() -> str:
    n = next_numeric_id(IDEAS_DIR, IDEA_STATUS_DIRS, "[0-9]*.md", r"(\d+)-")
    return f"{n:04d}"


def _slugify(text: str, max_len: int = 40) -> str:
    s = re.sub(r"[^a-z0-9가-힣-]+", "-", text.lower()).strip("-")
    return s[:max_len].rstrip("-") or "ticket"


# ── commands ───────────────────────────────────────────────────────────

def cmd_claim(args: argparse.Namespace) -> int:
    sess = session_name(args.session)
    try:
        status, path = find_ticket(args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot claim {args.id}: currently in {status}/", file=sys.stderr)
        return 1

    # find→load→rename has two race windows where a concurrent winner may move
    # the ticket out of open/ between our `find_ticket` and our own rename: the
    # loser's `load_ticket(path)` read or its `move_ticket(path)` rename then
    # raises FileNotFoundError. Both mean the same thing — we lost the claim
    # race — so both surface as one clean "claim race lost" (rc=1), never an
    # unhandled traceback (ADR-0012 contract). The atomic rename remains the
    # lock; this only unifies the loser's *reporting* across the two windows.
    # Note: a dependency's own FileNotFoundError is caught below and is a
    # *normal* rejection ("dependency not found"), distinct from a claim race.
    try:
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

        # Atomic rename is the lock
        new_path = move_ticket(path, "claimed")
    except FileNotFoundError:
        print(f"claim race lost on {args.id}", file=sys.stderr)
        return 1

    fm["status"] = "claimed"
    fm["claimed_by"] = sess
    fm["claimed_at"] = now_utc()
    dump_ticket(new_path, fm, body)
    print(f"claimed {args.id} as {sess}")
    refresh_board()
    return 0


def _complete_gate(tid: str, args: argparse.Namespace) -> list[str]:
    """Verify completion housekeeping before a ticket may move to done/.

    Returns a list of *blocking* problems (empty = gate passes). Non-blocking
    concerns are printed to stderr as warnings from here.

    The regression check trusts the caller's `--tests-pass` assertion rather
    than re-running the (slow) suite — see T-0020.
    """
    problems: list[str] = []
    id_re = re.compile(rf"\b{re.escape(tid)}\b")

    # 1. log/current.md must carry an entry for this ticket.
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
    print(f"completed {args.id}")
    refresh_board()
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
    print(f"blocked {args.id}: {args.reason}")
    refresh_board()
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
    print(f"unclaimed {args.id}")
    refresh_board()
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
    print(f"unblocked {args.id}")
    refresh_board()
    return 0


INIT_GUIDE = """\
─ init 완료 — 이 clone setup 끝 ({mode}) ─
  3계층: 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf · git-ignored)
  규칙: 내구 진실은 공유 채널에만 · pm_state 는 버려도 되는 로컬 · 공유 파일 직접 난편집 금지
  ID:   `board.py new` 로 {idfmt} 발행
"""

# 외부 리뷰어 기본 명령 (external_review.py 와 동일 default · ADR-0004)
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"


def _is_noninteractive() -> bool:
    """`PM_NONINTERACTIVE` env 가 truthy 면 True — 비대화 결정 신호 (T-0071).

    Windows 서 DEVNULL stdin 의 `isatty()` 가 신뢰불가([[T-0068]] 류 cross-OS 함정)라
    pm_import 가 board init 을 비대화로 부를 때 env 로 결정적 신호를 준다. truthy 판정은
    `"1"`/`"true"`/`"yes"`/`"on"`(대소문자 무관) — 빈/`"0"`/`"false"` 등은 미설정 취급(폴백).
    """
    return os.environ.get("PM_NONINTERACTIVE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def prompt_external_review_optin() -> None:
    """외부 코드리뷰(external_review) opt-in 프롬프트 → local.conf 에 기록 (ADR-0004).

    코드 diff 가 외부로 *전송*되므로 기본 거부. 이미 설정돼 있거나 비대화형(파이프·CI)이면
    묻지 않고 안전쪽(OFF 유지). 선택은 어느 쪽이든 기록해 다음 init/update 때 다시 묻지 않는다.
    """
    if "external_review_enabled" in local_config():
        return  # 이미 결정됨
    # 명시적 비대화 신호 우선 (T-0071): Windows DEVNULL 의 isatty() 신뢰불가 함정 회피.
    # PM_NONINTERACTIVE truthy 면 묻지 않고 안전쪽(OFF 유지). isatty 는 보조 폴백(env 없을 때).
    if _is_noninteractive() or not sys.stdin.isatty():
        print("  (비대화형 — 외부 리뷰 OFF 유지. 켜려면 local.conf 에 external_review_enabled=true)")
        return
    print("\n외부 코드리뷰(external_review)를 켤까요? 코드 diff 를 외부 리뷰어(codex 등)로 "
          "*전송*합니다 — 내부 code-reviewer 와 상보적이나 외부 전송이 발생합니다.")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        # stdin 이 EOF (비대화·파이프 종료) — 비대화 가드와 동일 계약: 결정 미기록,
        # 아무것도 쓰지 않고 반환. 기존 local.conf 의 결정을 덮어쓰지 않는다(preservation).
        return
    with LOCAL_CONF.open("a", encoding="utf-8") as f:
        if answer in ("y", "yes"):
            f.write("# 외부 코드리뷰 (ADR-0004)\n"
                    "external_review_enabled=true\n"
                    f"reviewer_cmd={DEFAULT_REVIEWER_CMD}\n")
            print("  ✓ 외부 리뷰 ON (reviewer_cmd 기본 codex — local.conf 에서 교체 가능)")
        else:
            f.write("# 외부 코드리뷰 (ADR-0004) — 기본 OFF. 켜려면 true 로.\n"
                    "external_review_enabled=false\n")
            print("  → 외부 리뷰 OFF (나중에 local.conf 에서 켤 수 있음).")


def cmd_init(args: argparse.Namespace) -> int:
    """clone 당 1회 setup. --prefix 있으면 multi-repo 네임스페이스, 없으면 solo (N=1·M=1).

    multi-PM = N 세션 × M repo 한 개념(ADR-0016) — *수가 1이냐 더냐*의 문제다.
    `--prefix` 는 협업(다중-사람)용이 아니라 **M>1 repo 의 ID 네임스페이스** — 같은
    single user 가 여러 repo 를 동시에 도는 multi-PM 셋업에서 ID 충돌을 막는다.

    공통: local.conf + pm_state(template) + pre-push 회귀 훅.
    namespaced(--prefix): areas.md 레지스트리 등록 + prefix(→ T-PREFIX-NNN·multi-repo 가드 활성).
    solo (N=1·M=1): areas.md 안 만듦 → 가드 off → legacy T-NNNN (오버헤드 0).
    """
    prefix = args.prefix
    namespaced = bool(prefix)  # prefix 있음 = multi-repo 네임스페이스 모드(협업 아님·ADR-0016)
    if namespaced:
        if prefix in registered_prefixes():
            print(f"prefix {prefix!r} 이미 등록됨 (areas.md) — local.conf 만 갱신.")
        else:
            if not args.area:
                print(f"새 prefix {prefix!r} 등록엔 --area <설명> 필요.", file=sys.stderr)
                return 1
            # owner = areas.md 등록 식별자(registrant) — 협업 소유자(다중-사람)가 아니라
            # single user 의 등록 출처 표식이다(ADR-0016·ADR-0002 amend). 기본 = 현 세션.
            owner = args.owner or session_name()
            areas_append(prefix, args.area, owner)
            print(f"✓ areas.md 등록: {prefix} | {args.area} | {owner}")
    sess = args.session or (f"{prefix.lower()}-pm" if namespaced else "pm")
    conf = "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
    if namespaced:
        conf += f"prefix={prefix}\n"
    conf += (f"session={sess}\n"
             "# 엔진 문서 operational placeholder 해소값 ({{PY}}·{{TEST_CMD}}·{{PROJECT_NAME}}):\n"
             f"py={_detect_py()}\ntest_cmd=pytest -q\nproject_name=\n"
             "# ctx 정지-핸드오프 임계 (어댑터 훅이 잔여 컨텍스트 %로 판정 — T-0013):\n"
             f"ctx_nudge_pct={CTX_NUDGE_PCT_DEFAULT}\nctx_stop_pct={CTX_STOP_PCT_DEFAULT}\n"
             "# ctx_window_tokens: 핸드오프 토큰 예산(위 nudge/stop %의 기준). 큰 window(1M)\n"
             "# 모델이라도 낮게 두면 이른 핸드오프 = 토큰 경제(큰 컨텍스트가 매 턴 소모 가속).\n"
             "# 올리면 세션당 더 길게. 물리 window 아님 — 사용자 비용/맥락 선택.\n"
             f"ctx_window_tokens={CTX_WINDOW_TOKENS_DEFAULT}\n")
    LOCAL_CONF.write_text(conf, encoding="utf-8")
    print(f"✓ local.conf: {('prefix=' + prefix + ' · ') if namespaced else ''}session={sess}")
    if not PM_STATE_FILE.exists() and PM_STATE_TEMPLATE.exists():
        PM_STATE_FILE.write_text(PM_STATE_TEMPLATE.read_text(encoding="utf-8"),
                                 encoding="utf-8")
        print(f"✓ pm_state.md 생성 ({_rel_to_repo(PM_STATE_TEMPLATE)} 에서)")
    if install_pre_push_hook():
        print("✓ pre-push 회귀 게이트 훅 설치 (green 회귀만 push)")
    prompt_external_review_optin()
    mode = f"multi-repo · {prefix}" if namespaced else "solo (N=1·M=1)"
    idfmt = f"T-{prefix}-NNN" if namespaced else "T-NNNN (legacy)"
    print(INIT_GUIDE.format(mode=mode, idfmt=idfmt))
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    prefix = id_prefix(getattr(args, "prefix", None))
    # multi-repo 네임스페이스 가드는 **레지스트리 *존재*가 아니라 등록 repo *개수*** 기준이다.
    # 등록 prefix 가 ≥2 면 진짜 ID 충돌 가능성이 있으니 prefix 필수(namespace 강제). 등록이
    # ≤1(0=레지스트리 부재/빈·1=단일 self-host) 이면 충돌이 없으므로 solo legacy `T-NNNN` 을
    # 허용한다(prefix optional) — 단일 self-host 가 areas.md 1행만으로 multi-PM 마찰을 떠안지
    # 않게(ADR-0027 분리 후 단일 등록 repo 케이스). 명시 prefix 가 *주어지면* 그건 그대로
    # 존중해(아래 등록 검증) prefixed ID 를 발행한다 — ≤1 라도 사용자가 골랐으면 따른다.
    registered = registered_prefixes()
    if len(registered) >= 2:
        if not prefix:
            print("multi-repo 네임스페이스 모드(등록 repo ≥2) — prefix 필요. 먼저 "
                  "`board.py init --prefix <PFX> --area <name>`.", file=sys.stderr)
            return 1
    if prefix and prefix not in registered:
        # 명시 prefix(override 또는 local.conf)는 등록된 것이어야 한다 — registry 가 존재할 때만
        # 의미 있는 검증(부재면 registered 가 빈 set → 솔로에서 prefix 를 명시한 비정상 케이스).
        if AREAS_FILE.exists():
            print(f"prefix {prefix!r} 미등록 (areas.md). `board.py init` 로 등록하거나 "
                  "등록된 prefix 사용.", file=sys.stderr)
            return 1

    tmpl_fm, tmpl_body = load_ticket(TEMPLATE_FILE)

    # ID 발행(`_next_id` = max+1·동시 발행 race)과 파일 생성을 단일 락으로 직렬화한다
    # (ADR-0012). 락 안에서 ID 를 *읽고* 곧바로 파일을 만들어, 다른 세션이 같은 ID 를
    # 발행할 틈을 없앤다. board.md 재생성은 락 밖(별도 트랜잭션 — 파생물).
    with board_lock():
        tid = _next_id(prefix)
        slug = _slugify(args.title)
        filename = f"{tid}-{slug}.md"

        # Replace placeholder tokens in body
        body = tmpl_body.replace("T-NNNN", tid).replace("<제목>", args.title)

        fm: dict[str, Any] = dict(tmpl_fm)
        fm["id"] = tid
        fm["title"] = args.title
        fm["status"] = "open"
        fm["created"] = datetime.date.today().isoformat()
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        fm["completed_at"] = None
        fm["touches"] = (args.touches.split(",") if args.touches else [])
        fm["depends_on"] = (args.depends.split(",") if args.depends else [])
        fm["blocks"] = []
        fm["tags"] = (args.tag.split(",") if args.tag else [])
        fm["estimate"] = args.estimate

        path = TICKETS_DIR / "open" / filename
        dump_ticket(path, fm, body)

    print(f"created {tid} ({_rel_to_repo(path)})")
    print("  → fill in 목표 / 완료 조건 / 참고, then `board.py lint` "
          "(placeholders left in the body fail lint)")
    refresh_board()
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    rows: list[tuple[str, dict]] = []
    for status in STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in (fm.get("tags") or []):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no tickets)")
        return 0
    for status, fm in rows:
        tags = ",".join(fm.get("tags") or [])
        claimed = fm.get("claimed_by") or ""
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:7s}] {fm['id']}  {title:60s}  {claimed:18s}  {tags}")
    return 0


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
            if args.tag and args.tag not in (fm.get("tags") or []):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no ideas)")
        return 0
    for status, fm in rows:
        tags = ",".join(str(t) for t in (fm.get("tags") or []))
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


def _run_lint_hooks() -> list[tuple[str, str]]:
    """Discover & run instance lint hooks — .project_manager/hooks/lint_*.py (ADR-0003).

    각 훅 모듈은 `check() -> list[str]`(이슈 detail 문자열)을 노출한다. fail-soft:
    로드/실행 실패·계약 불충족은 stderr 경고로 보고하고 계속한다(부분 실패가 lint 전체를
    막지 않음). 인스턴스가 엔진 board.py 를 안 건드리고 자기 검사를 더하는 seam — 프레임워크
    공통 검사(wikilink 등)는 엔진 내장(lint_wikilinks), 프로젝트 고유 검사는 여기로.
    """
    if not HOOKS_DIR.is_dir():
        return []
    out: list[tuple[str, str]] = []
    for hook in sorted(HOOKS_DIR.glob("lint_*.py")):
        try:
            spec = importlib.util.spec_from_file_location(hook.stem, hook)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
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

    무인자: 모든 issue 를 보고하고 issue 가 하나라도 있으면 종료코드 1 (현행 계약).
    `--gate`: 종료코드를 *차단 카테고리*에만 1 로 둔다 — status drift 같은 자문성
    (lint_status 의 "never blocks" 계약) 은 보고는 하되 종료코드에 반영하지 않는다.
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
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
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
_PLACEHOLDERS: tuple[str, ...] = (
    "무엇을 만들 / 바꿀 / 검증할지",
    "핵심 산출물 (파일, 동작)",
    "[[xxxxx]]",
    "<제목>",
)
_REQUIRED_SECTIONS: tuple[str, ...] = ("## 목표", "## 완료 조건", "## 참고")


def lint_bodies() -> list[tuple[str, str, str]]:
    """Lint open/claimed ticket bodies for self-containment.

    Checks:
      - placeholder: unfilled `_template.md` text still present as prose
      - thin:        a standard section (목표 / 완료 조건 / 참고) is missing

    done/blocked tickets are skipped — only live, claimable work is gated.
    """
    issues: list[tuple[str, str, str]] = []
    for status in ("open", "claimed"):
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
            fm, body = load_ticket(p)
            tid = fm.get("id") or p.name
            prose = _strip_code(body)
            for placeholder in _PLACEHOLDERS:
                if placeholder in prose:
                    issues.append((tid, "placeholder",
                                   f"unfilled template text: {placeholder!r}"))
            for section in _REQUIRED_SECTIONS:
                if section not in body:
                    issues.append((tid, "thin",
                                   f"missing standard section: {section}"))
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
# (ADR-0023: 헤더 scalar·테스트 수는 제거 — judgment-only status. 남은 가드는 ✅ 누적뿐.)
STATUS_DONE_ROW_WARN = 30

# 모듈 매트릭스 행 중 상태 셀이 ✅ 인 행 (범례 "- ✅ ..." 는 `|` 시작 아니라 제외).
_STATUS_DONE_ROW_RE = re.compile(r"^\|.*\| ✅ \|", re.MULTILINE)

def lint_status() -> list[tuple[str, str, str]]:
    """status.md 의 ✅ 완성 행 누적을 경고한다 (warn-only·judgment-only status·ADR-0023).

    Checks:
      - status-done-accum: 활성 매트릭스에 ✅ 완성 행이 누적 — status_done.md 로 archive 권고.

    (ADR-0023 a안: status.md 헤더 scalar·테스트 수·합계·소계·회귀 실측은 derivable 이라
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


# ── family wiki scope 태그 + 승격 (ADR-0015) ─────────────────────────────
# multi-PM wiki 하나 + repo 전용 문서를 `family_scope:` frontmatter 태그로 구분한다(ADR-0015).
#   - 값 = `shared`(기본) / repo 명(areas.md 의 등록 prefix). 부재 → shared 로 간주.
#   - "완료 시 공유" = 물리 머지 아니라 scope 승격(`repoA → shared` retag·idea-promote 동형).
#   - `board.py lint` 가 family_scope 를 *인지*(파싱·기본 shared)하되 차단은 최소 —
#     알 수 없는 형식만 자문성 권고(`scope-advice`·never-blocks). scope 자체로 hard-fail 안 함.
#
# 키 선택(`family_scope:` ≠ `scope:`): 기존 ADR frontmatter 의 `scope:` 는 이미 문서 전략
# 분류(`mission`·`internal-process`)로 점유돼 있어, 같은 키에 repo 네임스페이스를 얹으면 기존
# 의미를 깨고 오탐을 부른다. family wiki scope 는 전용 키 `family_scope:` 로 박제해 두 의미체계를
# 분리한다 — 솔로(키 부재) 회귀 0. (ADR-0015 본문은 `scope:` 라 적었으나 키 충돌 회피가 우선.)

FAMILY_SCOPE_DEFAULT = "shared"  # family_scope 부재/빈값 → shared 로 간주 (ADR-0015).
# family_scope 가 인지되는 wiki 디렉토리 — ADR(decisions/)·spec(specs/).
_SCOPE_AWARE_DIRS: tuple[Path, ...] = (DECISIONS_DIR, SPECS_DIR)
# 유효 family_scope 값 형식 — `shared` 또는 prefix 형(영숫자·`-`·`_`, 등록 prefix 와 동형).
# 형식만 검사(등록 여부는 advisory 메시지로) — areas.md 부재인 솔로에서도 동작.
_FAMILY_SCOPE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _in_scope_aware_dir(path: Path) -> bool:
    """path 가 scope-aware 디렉토리(decisions/·specs/) 안에 있는가.

    promote-scope 가 ADR/spec 문서로만 retag 를 제한하기 위한 가드(ADR-0015) — 임의
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
    """frontmatter dict 의 family wiki scope (ADR-0015). 부재/빈값 → shared.

    `family_scope:` 값을 strip 해 반환한다. 없거나 빈 문자열이면 `shared` 기본
    (ADR-0015 "부재 시 shared 로 간주"). 비-문자열(잘못 적힌 list 등)도 shared 로
    안전 폴백 — 파싱이 절대 예외를 던지지 않게 한다(lint fail-soft).
    """
    raw = fm.get("family_scope")
    if not isinstance(raw, str):
        return FAMILY_SCOPE_DEFAULT
    val = raw.strip()
    return val or FAMILY_SCOPE_DEFAULT


def lint_scopes() -> list[tuple[str, str, str]]:
    """family_scope 태그를 파싱·인지한다 (kind=`scope-advice`·자문성·ADR-0015).

    decisions/·specs/ 문서의 `family_scope:` 를 읽어 *인지*한다(부재 → shared 기본).
    차단은 최소 — 다음만 자문성 권고(never-blocks·`_ADVISORY_LINT_KINDS`):
      - 비문자열 family_scope (list/dict/number 등 — frontmatter 형식 오류).
      - 형식이 깨진 scope (공백/특수문자 등 `_FAMILY_SCOPE_RE` 불일치).
      - shared 도 아니고 areas.md 의 등록 prefix 도 아닌 미지의 repo scope (오타 신호).
        단 areas.md 부재(솔로)면 등록 대조를 건너뛴다 — 솔로에서 repo scope 는 미래값일 뿐.
    scope 자체로 hard-fail 을 만들지 않는다(ADR-0015 "차단은 최소·advisory 우선"). 솔로
    (family_scope 부재) 에선 항상 빈 리스트 — 회귀 0.

    *원본 값* 을 검사한다(파싱 헬퍼 `family_scope()` 의 fail-soft 폴백과 분리) — 헬퍼는
    비문자열을 shared 로 안전 폴백하지만, lint 는 그 형식 오류를 조용히 삼키지 않고
    `scope-advice` 로 권고해야 한다(ADR-0015 "형식 깨짐은 advisory").
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
                # lint 는 조용히 삼키지 않고 권고한다(ADR-0015 "형식 깨짐은 advisory").
                issues.append((src, "scope-advice",
                               f"family_scope 가 비문자열({type(raw).__name__}) — "
                               f"`shared` 또는 repo prefix 문자열이어야 함 (ADR-0015)"))
                continue
            if not raw.strip():
                continue  # 빈값 = shared 기본 — 정상, 보고 없음.
            scope = raw.strip()
            if not _FAMILY_SCOPE_RE.match(scope):
                issues.append((src, "scope-advice",
                               f"family_scope={scope!r} 형식이 깨짐 — `shared` 또는 "
                               f"repo prefix 여야 함 (ADR-0015)"))
            elif scope != FAMILY_SCOPE_DEFAULT and known and scope not in known:
                issues.append((src, "scope-advice",
                               f"family_scope={scope!r} 가 등록된 repo prefix 아님 "
                               f"(areas.md: {sorted(known)}) — 오타 또는 승격 누락 가능 "
                               f"(ADR-0015)"))
    return issues


# 승격 destination 으로 허용할 약식 (board.py promote-scope <file> --to <scope>).
# 임의 repo prefix 도 허용하되, 형식 검증(`_FAMILY_SCOPE_RE`)은 통과해야 한다.
def cmd_promote_scope(args: argparse.Namespace) -> int:
    """family_scope retag — `repoA → shared` 등 scope 값을 교체한다 (ADR-0015·idea-promote 동형).

    "완료 시 공유" = 물리 머지 아니라 scope 승격(ADR-0015). 대상 문서(decisions/·specs/ 의
    .md)의 frontmatter `family_scope:` 를 `--to` 값으로 교체(부재면 신규 기록)한다. 단순·최소 —
    파일 한 개 retag. `--to` 형식은 `_FAMILY_SCOPE_RE` 로 검증한다(깨진 값 차단). 대상은
    scope-aware 디렉토리(decisions/·specs/) 안이어야 한다 — ADR-0015 는 ADR/spec scope
    승격 명령이므로 임의 frontmatter 문서 retag 는 거부한다.
    """
    target = args.file
    new_scope = args.to.strip()
    if not _FAMILY_SCOPE_RE.match(new_scope):
        print(f"invalid --to scope {new_scope!r}: `shared` 또는 repo prefix "
              "(영숫자·-·_) 여야 함 (ADR-0015).", file=sys.stderr)
        return 1
    path = Path(target)
    if not path.is_absolute():
        path = (REPO / target).resolve()
    else:
        path = path.resolve()
    if not _in_scope_aware_dir(path):
        print(f"refusing to retag {_rel_to_repo(path)}: scope 승격은 decisions/·specs/ "
              "문서만 대상 (ADR-0015 — ADR/spec scope 승격 명령).", file=sys.stderr)
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


# ── wikilink lint (ADR-0003) ───────────────────────────────────────────
# 엔진은 *구조적으로 해석 가능한* 참조만 검증한다: [[ADR-NNNN]]·[[T-NNNN]]/[[T-PFX-NNN]]·
# [[idea-NNNN]] 가 실제 파일로 resolve 되는지. 자유어휘([[some-memory-slug]] 등)는 프로젝트마다
# 화이트리스트가 달라 엔진이 판정할 수 없으므로 건드리지 않는다(오탐 0) — 프로젝트 고유 링크 검사는
# lint 훅(.project_manager/hooks/lint_*.py·R15)으로 분리. placeholder([[T-NNNN]]·[[xxxxx]])는
# 숫자 패턴이 아니라 자연히 제외된다.

# [[name]] 또는 alias [[name|display]] — name 만 캡처. backtick 안도 포함.
_WIKILINK_RE = re.compile(r"\[\[([A-Za-z0-9_\s.\-]+?)(?:\|[^\]]+)?\]\]")

# 어댑터 scaffold 경로 — fresh adopter 에 출하되는 harness 어댑터(.claude/.opencode).
# 채택자(특히 framework ADR 0001~ 이 없는 다운스트림 앱)는 자기 repo 의 scaffold 에서
# framework ADR/idea 를 [[bracket]] 참조하면 *영구 dangling* 이 된다 — 이는 정상이며
# push 를 막아선 안 된다(T-0129·ADR-0015 "차단은 최소·advisory 우선"). `_collect_wikilink_files`
# 의 scaffold rel 목록과 동일 — POSIX 경계로 비교(_rel_to_repo 는 `/` 정규화).
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
    보던 동안 이 scaffold dangling 은 *구조적으로* 안 잡혔다(T-0116 이 scaffold ref 를 늘림).
    각 dir 은 harness 별로 존재 여부가 다르므로(claude 채택자엔 `.opencode` 부재·역도 마찬가지)
    `.is_dir()` 가드로 있을 때만 추가한다.
    """
    wiki = REPO / ".project_manager" / "wiki"
    files: list[Path] = list(wiki.rglob("*.md")) if wiki.is_dir() else []
    for name in ("CLAUDE.md", "README.md"):
        p = REPO / name
        if p.exists():
            files.append(p)
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
    코드 span/fence 안의 *예시* wikilink(규약 문서가 backtick 으로 보여주는
    `[[ADR-NNNN]]`)는 실 참조가 아니므로 `_strip_code` 로 제거 후 스캔한다 —
    `lint_unstable_refs` 와 동일한 처리(오탐 0·ADR-0003 철학).

    kind 분류 (T-0129·T-0118 push-block 정정):
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
            elif re.fullmatch(r"T-(?:[A-Za-z]+-)?\d+", name):
                ok = name in ticket_ids
                is_ticket = True
            elif m_idea:
                ok = (m_idea.group(1).lstrip("0") or "0") in idea_nums
            else:
                continue  # 자유어휘 — 엔진 판정 안 함 (R15 훅 영역)
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


# ── render-leak (리터럴 `{{...}}` 누출 차단 · T-0131·§3.4) ──────────────────
# 어댑터 파일 = render-overlay 산출물(ADR-0028). framework 본문 템플릿 + 채택자 overlay 가
# pm_update 재렌더로 자족 .md 가 된다. half-rendered 토큰(`{{...}}` 잔존)이 *출하 산출물* 에
# 새 나가면 harness-load 에이전트 지시가 무음 열화하므로 실결함 — blocking(경고 아님).
#
# ⚠️ 활성화 전 무발화 경계 (DoD): 스캔 대상을 **@render manifest path 의 산출물로 한정**한다.
#    현재 트리는 *어떤* 실 manifest path 도 @render 가 아니므로(D17-2/T-0133 활성화 전) 검사
#    대상이 0 → 이 lint 는 현 트리에서 무발화(기존 토큰을 가진 어댑터 .md 를 *검사하지 않음*).
#    토큰은 @render 로 활성화돼 렌더 산출물이 된 path 에서만 leak 으로 간주된다 — 활성화는
#    pm_render(post-render assertion) + 이 lint(상시 backstop)가 함께 자족성을 보증한다.

# leak 스캔 토큰 — 대문자/언더스코어 placeholder (`{{PROJECT_NAME}}`·`{{PROTECTED_PATHS}}` 등).
# pm_render._ANY_TOKEN_RE 와 동형(소문자/공백 토큰은 산문이라 제외·오탐 0).
_RENDER_TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")


def _render_managed_relpaths() -> set[str]:
    """engine.manifest 에서 `@render` 태그가 붙은 path 들(repo 기준 relpath·POSIX) — 검사 대상.

    pm_update.read_manifest 를 재사용해 `.render` 플래그가 True 인 항목만 모은다. manifest
    부재·로드 실패는 빈 set(검사 대상 0·무발화). manifest 의 @render path 가 디렉토리면 그
    하위 출하 어댑터가 전부 산출물이므로 prefix 매칭에 쓴다.
    """
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
    """이 트리에서 검사할 engine.manifest 파일들 — 루트 + templates/<harness>/ (있을 때만).

    채택자(단일 트리)는 루트 manifest 만, 도그푸딩 모노레포(이 repo)는 templates/* 도 본다.
    `.is_file()` 가드로 존재하는 것만(harness 별 부재 무영향)."""
    out: list[Path] = []
    root_manifest = REPO / ".project_manager" / "engine.manifest"
    if root_manifest.is_file():
        out.append(root_manifest)
    templates = REPO / "templates"
    if templates.is_dir():
        for child in sorted(templates.iterdir()):
            m = child / ".project_manager" / "engine.manifest"
            if m.is_file():
                out.append(m)
    return out


def _load_pm_update_module():
    """pm_update 모듈을 같은 tools/ 디렉토리에서 로드 (read_manifest @render 파싱 재사용).

    board.py 가 _detected_py 류 seam 으로 형제 모듈을 로드하는 패턴과 동형. 실패 시 None →
    호출부가 검사 대상 0(무발화)으로 흡수한다."""
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pm_update", pm_update_py)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 무발화(검사 대상 0).
        return None


def _load_pm_render_module():
    """pm_render 모듈을 같은 tools/ 디렉토리에서 로드 (FREEFORM_KEYS·OVERLAY_RELPATH 재사용).

    `_load_pm_update_module`·`_load_domain_module` 과 동형 deep-import seam (순환 회피·
    형제 모듈 지연 로드). free-form 토큰 집합과 overlay 경로의 단일 진실 = pm_render —
    board 가 중복 정의하지 않고 여기서 빌린다. 실패 시 None → 호출부가 무발화로 흡수한다."""
    pm_render_py = Path(__file__).resolve().parent / "pm_render.py"
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pm_render", pm_render_py)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — 로드 실패는 무발화(검사 대상 0).
        return None


def _is_render_managed(rel_posix: str, managed: set[str]) -> bool:
    """rel_posix 가 @render manifest path(파일 정확일치 OR 디렉토리 prefix) 하위인지."""
    for m in managed:
        if rel_posix == m or rel_posix.startswith(m.rstrip("/") + "/"):
            return True
    return False


def lint_render_leak() -> list[tuple[str, str, str]]:
    """render 산출물에 리터럴 `{{...}}` 누출 차단 (kind=`render-leak`·blocking·ADR-0028·§3.4).

    `_ADVISORY_LINT_KINDS` 밖 → `lint --gate` 차단 → pre-push exit 1(dangling-wikilink 미러).
    half-rendered 토큰은 harness-load 에이전트 지시의 무음 열화라 실결함(경고 아님).

    **활성화 전 무발화 경계**: 검사 대상 = engine.manifest 에서 `@render` 태그가 붙은 path 의
    산출물뿐(`_render_managed_relpaths`). 현 트리는 실 path @render 0(D17-2/T-0133 활성화 전)
    → 검사 대상 0 → 무발화(기존 토큰을 가진 미활성 어댑터 .md 는 *검사하지 않음*). pm_render
    의 post-render assertion 과 2중 backstop — pm_update 가 마지막 도구였는지 무관한 상시 가드.

    fail-soft: manifest 부재·로드 실패·파일 read 오류 → 그 부분 skip(검사 대상 0·솔로/신규 무영향).
    """
    managed = _render_managed_relpaths()
    if not managed:
        return []  # @render path 0 → 검사 대상 0 (활성화 전 무발화).
    issues: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for managed_rel in sorted(managed):
        target = REPO / managed_rel
        files: list[Path] = []
        if target.is_dir():
            files = sorted(p for p in target.rglob("*.md") if p.is_file())
        elif target.is_file():
            files = [target]
        for p in files:
            rel_posix = _rel_to_repo(p).replace("\\", "/")
            if rel_posix in seen:
                continue
            seen.add(rel_posix)
            try:
                text = p.read_text(encoding="utf-8")
            except OSError:
                continue
            leaked = sorted(set(_RENDER_TOKEN_RE.findall(text)))
            if leaked:
                issues.append((
                    rel_posix, "render-leak",
                    f"render 산출물에 미해소 토큰 잔존: {', '.join(leaked)} "
                    f"(@render 관리 path — overlay/local.conf 채널 누락 또는 미배선 토큰)"))
    return issues


# ── un-migrated overlay 검출 (advisory · T-0132·§3.6) ──────────────────────
# 어댑터 파일 = render-overlay 산출물(ADR-0028): 마이그레이션 후 출하 .md = 렌더 산출물 =
# 리터럴 free-form 토큰 0(overlay 채널이 값을 공급). 채택자가 *아직* 마이그레이션을 안 했으면
# (overlay 미생성·baked 값 손편집 유지) 어댑터 .md 에 리터럴 `{{PROTECTED_PATHS}}` 류가 잔존한다
# — 이 lint 가 그 신호를 표면화한다(§3.6 "un-migrated 검출"). render-leak(blocking·@render 산출물
# 한정)과 별개·상보: render-leak 은 *활성화된* render path 의 미해소 토큰을, 이 lint 는 *활성화
# 전* 어댑터 본문의 미마이그레이션 토큰 잔존을 본다.
#
# **advisory only** — 마이그레이션 누락은 push 결함이 아니라 채택자 운영 ritual 신호(§3.6
# "push-block 아님·advisory")라 `_ADVISORY_LINT_KINDS` 에 등재(`--gate` 미차단). free-form 3종
# (FREEFORM_KEYS)만 본다 — operational 토큰(`{{PROJECT_NAME}}` 등)은 import sed/local.conf 채널이라
# 별개. graceful: 어댑터 파일/디렉토리 부재 시 finding 0(솔로·non-adopter tree 무오염).

# 어댑터 스캐폴드 .md 글롭 — 채택자 tree 에 출하되는 harness 어댑터 본문 (존재하는 것만).
#   claude   : `.claude/agents/*.md`·`.claude/skills/**/SKILL.md`·root `CLAUDE.md`/`CLAUDE.lite.md`
#   opencode : `.opencode/agents/*.md`·`.opencode/command/*.md`·root `AGENTS.md`/`AGENTS.lite.md`
# 각 경로는 harness 별 존재 여부가 다르므로(claude 채택자엔 `.opencode` 부재·역도) 있을 때만 스캔.
_OVERLAY_ADAPTER_GLOBS: tuple[tuple[str, str], ...] = (
    (".claude/agents", "*.md"),
    (".claude/skills", "SKILL.md"),
    (".opencode/agents", "*.md"),
    (".opencode/command", "*.md"),
)
_OVERLAY_ADAPTER_ROOT_DOCS: tuple[str, ...] = (
    "CLAUDE.md", "CLAUDE.lite.md", "AGENTS.md", "AGENTS.lite.md")


def _collect_overlay_adapter_files() -> list[Path]:
    """un-migrated 검사 대상 어댑터 .md — harness 스캐폴드 디렉토리 + root 어댑터 doc (존재만).

    `.claude/skills` 는 `**/SKILL.md`(rglob), 그 외 디렉토리는 직속 `*.md`(glob)·root doc 은
    파일 정확 일치로 모은다. dedupe 는 호출부가 path 로 처리. `.is_dir()`/`.is_file()` 가드로
    부재 harness/솔로 tree 는 조용히 건너뛴다(graceful·finding 0)."""
    files: list[Path] = []
    for rel, pattern in _OVERLAY_ADAPTER_GLOBS:
        d = REPO / rel
        if not d.is_dir():
            continue
        files.extend(d.rglob(pattern) if pattern == "SKILL.md" else d.glob(pattern))
    for name in _OVERLAY_ADAPTER_ROOT_DOCS:
        p = REPO / name
        if p.is_file():
            files.append(p)
    return files


def lint_unmigrated_overlay() -> list[tuple[str, str, str]]:
    """어댑터 .md 에 리터럴 free-form 토큰이 잔존하면 un-migrated 신호 (kind=`un-migrated-overlay`).

    `_ADVISORY_LINT_KINDS` 등재 → `lint --gate` 미차단(advisory·§3.6 "push-block 아님"). 마이그레이션
    누락은 채택자 운영 ritual 신호이지 출하 결함이 아니므로 visibility 만 제공한다.

    검사 (정적·shipped tree 스캔):
      - 어댑터 .md(`_collect_overlay_adapter_files`)에 리터럴 free-form 토큰(FREEFORM_KEYS —
        `{{PROJECT_CONSTRAINTS}}`/`{{PROTECTED_PATHS}}`/`{{USER_GATE_ITEMS}}`)이 잔존 → 파일·토큰별
        finding 1건. 마이그레이션 후엔 출하 .md = 렌더 산출물 = 토큰 0(overlay 가 값 공급).
      - 위 토큰이 *하나라도* 발견됐는데 OVERLAY_RELPATH(`.project_manager/overlay.local.yaml`)가
        부재 → "overlay 채널 미생성" finding 1건 추가(마이그레이션의 핵심 단계 누락).

    오탐 0 경계:
      - **FREEFORM_KEYS·OVERLAY_RELPATH 는 pm_render 에서 import**(중복 정의 0·단일 진실). pm_render
        로드 실패 → 검사 불가 → [] (무발화·graceful).
      - operational 토큰(`{{PROJECT_NAME}}` 등)은 *검사 대상 아님* — import sed/local.conf 채널이라
        별개. free-form 3종만 매칭(render-overlay 가 관리하는 손편집 산문).
      - 코드 span/fence 안의 *예시* 토큰은 `_strip_code` 로 제거 후 스캔(문서가 토큰을 예시로
        보여줘도 오탐 안 됨).
      - graceful: 어댑터 파일/디렉토리 부재(솔로·non-adopter) → finding 0. 파일 read 오류는 skip.

    이 lint 는 "어느 overlay key 가 채워졌어야 하는지"는 추론하지 않는다 — baked 파일에 provenance
    마커가 없어 기계 oracle 불가(§3.6). robust 한 정적 신호(리터럴 토큰 잔존 + overlay 파일 부재)만 본다.
    """
    pm_render = _load_pm_render_module()
    if pm_render is None:
        return []  # 단일 진실(FREEFORM_KEYS·OVERLAY_RELPATH) 로드 실패 → 무발화(graceful).
    freeform_keys = tuple(getattr(pm_render, "FREEFORM_KEYS", ()))
    overlay_relpath = getattr(pm_render, "OVERLAY_RELPATH", None)
    if not freeform_keys or overlay_relpath is None:
        return []  # 계약 상수 부재(구버전 pm_render) → 무발화.
    token_re = re.compile(
        r"\{\{(" + "|".join(re.escape(k) for k in freeform_keys) + r")\}\}")

    issues: list[tuple[str, str, str]] = []
    any_token = False
    seen: set[str] = set()
    for p in _collect_overlay_adapter_files():
        rel_posix = _rel_to_repo(p).replace("\\", "/")
        if rel_posix in seen:
            continue
        seen.add(rel_posix)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # 코드 span/fence 예시 토큰은 실 placeholder 가 아니므로 제거 후 스캔(오탐 0).
        leaked = sorted(set(token_re.findall(_strip_code(text))))
        if leaked:
            any_token = True
            toks = ", ".join("{{" + k + "}}" for k in leaked)
            issues.append((
                rel_posix, "un-migrated-overlay",
                f"리터럴 free-form 토큰 잔존: {toks} — 어댑터가 아직 render-overlay 로 "
                f"마이그레이션되지 않았다(§3.6·재렌더 후엔 토큰 0)."))
    # 토큰이 하나라도 잔존했는데 overlay 채널 자체가 없으면 마이그레이션 핵심 단계 누락.
    if any_token and not (REPO / overlay_relpath).is_file():
        issues.append((
            overlay_relpath, "un-migrated-overlay",
            f"overlay 채널 미생성: {overlay_relpath} 부재 — free-form 토큰이 잔존하나 "
            f"마이그레이션 overlay 가 없다(§3.6 CREATE overlay 단계 누락)."))
    return issues


# ── 파일명-무관 참조 강제 (unstable-ref · T-0036, ADR-0003 연장) ──────────
# 엔진은 [[ADR-NNNN]] 를 *번호*로 resolve 하므로 슬러그는 무관하다(ADR-0003). 그러나 LLM 이
# 구조화 디렉토리(decisions/·tickets/·ideas/)를 **생파일명·슬러그**로 가리키면 — markdown 경로
# 링크 `](…/decisions/<slug>.md)` 나 숫자선두 자유어휘 wikilink `[[NNNN-slug]]` — 슬러그가 바뀌면
# 부정확 참조가 된다. 이 둘은 *번호로 resolve 가능*하므로 ADR-0003 의 "구조 참조" 범위에 든다:
# resolve 실패 = dangling(차단), 실재하지만 슬러그 의존 = 권고(canonical ID-wikilink 로 전환).
# 자유어휘 일반([[some-memory]])·산문 언급은 *건드리지 않는다*(오탐 0 — ADR-0003 철학 유지).

# 구조화 디렉토리를 가리키는 markdown 링크. 견고성을 위해 2단계로 본다(codex T-0036 must-fix·
# suggestion — 정규식만으론 link-form edge 가 새므로):
#   (a) `_MD_LINK_TARGET_RE` 로 링크 target 을 추출 — 선택적 `<…>` 꺾쇠·트레일링 `"title"` 허용.
#   (b) target 에서 fragment(`#…`)·query(`?…`)를 떼고, 외부 URL(`scheme://`)이면 건너뛴 뒤
#       `_STRUCT_PATH_RE` 로 구조화 경로(decisions/·tickets/<state>/·ideas/<state>/<file>.md)를 매칭.
# 이렇게 `.md)`·`.md#sec)`·`.md "title")`·앞 경로 유무를 다 흡수하고 외부 URL 오탐(오차단)을 막는다.
# `(?:^|/)` 로 segment 경계를 요구해 `mydecisions/` 류 비-경계 매치를 배제. 매핑: decisions→ADR,
# tickets/<state>→ticket, ideas/<state>→idea (tickets/ideas 는 상태 디렉토리 필수 — README·_template 제외).
# title 은 CommonMark 3형 모두 흡수 — `"…"`·`'…'`·`(…)` (codex T-0036: single-quote/괄호 title 누락 방지).
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
# slug 를 허용하므로 `[[0001-한글아이디어]]` 도 포착(codex T-0036 must-fix·false-negative 방지).
# alias `|표시명` 은 `_WIKILINK_RE` 계약과 동일하게 흡수(codex suggestion).
_NUM_LEAD_WIKILINK_RE = re.compile(r"\[\[(\d+)(?:-[^\]|]+)?(?:\|[^\]]+)?\]\]")

# 코드 span/fence 안의 *예시* 링크·wikilink 는 실제 참조가 아니므로 스캔 전 제거(codex T-0036·오탐 0).
# 문서가 "나쁜 예시"로 `[x](decisions/9999-ghost.md)` 를 코드로 보여줘도 게이트를 막지 않게 한다.
# fenced(``` … ``` · ~~~ … ~~~) 를 먼저(여러 줄·비-greedy), 그 다음 inline(`…`·한 줄) 을 지운다.
_FENCED_CODE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """markdown 코드 span/fence 를 공백으로 치환 — 그 안의 예시 링크가 lint 에 안 걸리게."""
    return _INLINE_CODE_RE.sub(" ", _FENCED_CODE_RE.sub(" ", text))


def lint_unstable_refs() -> list[tuple[str, str, str]]:
    """파일명/슬러그-의존 구조 참조를 포착한다 (kind=`unstable-ref`/`unstable-ref-advice` · T-0036).

    두 형태를 본다 (둘 다 *구조화 디렉토리*를 가리킬 때만):
      - markdown 경로 링크 `](…/decisions/<slug>.md)`·`](…/tickets/<state>/<slug>.md)`·
        `](…/ideas/<state>/<slug>.md)` → 대상 파일이 실재 안 하면 dangling(차단), 실재하면 권고.
        (명시적 구조 경로라 의도가 분명 → 차단 가능.)
      - 숫자선두 wikilink `[[NNNN-slug]]`·`[[NNNN]]` → 번호가 ADR/idea 로 **resolve 될 때만**
        canonical `[[ADR-NNNN]]`/`[[idea-NNNN]]` 권고. resolve 안 되면 자유어휘(`[[2026-roadmap]]`
        등)와 구분 불가 → **불검사**(차단 안 함 · 오탐 0).

    kind 분류 (T-0036 결정 "차단은 dangling 만"):
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
        # raw/ 스냅샷(ADR-0010 — sealed 면 immutable)은 슬러그-경로 *권고*(never-blocks)를 면제한다:
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
        # 코드 span/fence 안의 예시 링크·wikilink 는 실제 참조 아님 → 제거 후 스캔(오탐 0·codex T-0036).
        text = _strip_code(text)

        # (1) markdown 링크 — target 추출 → fragment/query 제거·외부 URL 제외 → 구조화 경로 분류.
        for raw_target in _MD_LINK_TARGET_RE.findall(text):
            # 외부 URL 제외(오탐·오차단 방지) — scheme `http://` 형 + protocol-relative `//host/…`
            # (codex T-0036: `//ex.com/…/decisions/x.md` 오차단 방지). Windows `C:/` 는 `://`·`//`
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
        #     (ADR-0003 "자유어휘 불검사·오탐 0"; codex T-0036: 미resolve hard-block 은 자유어휘를
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
        p = TICKETS_DIR / status / filename
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
    """ticket 파일명에서 canonical ID 추출 ('T-0036-foo.md' → 'T-0036'). 없으면 None."""
    m = re.match(r"(T-(?:[A-Za-z]+-)?\d+)", filename)
    return m.group(1) if m else None


# 차단되지 않는 자문성 lint kind — push 를 막지 않는 권고/드리프트 카테고리.
# `lint --gate` 는 이 카테고리를 종료코드에서 제외한다 (push 게이트용 엄격 부분집합):
#   - status-done-accum : status.md ✅ 완성 행 누적 archive 권고 (lint_status "never blocks" 계약).
#     (ADR-0023: status-header-bloat·scalar-anchor-broken 은 status judgment-only 화로 제거.)
#   - unstable-ref-advice : 실재 파일을 슬러그로 가리키는 링크 — 작동은 함, ID-wikilink 권고만
#     (T-0036 결정 "차단은 dangling 만"). resolve 실패는 kind=`unstable-ref` 로 차단됨.
#   - scope-advice : family_scope 형식/등록 권고 — scope 자체로 hard-fail 안 함 (ADR-0015
#     "차단은 최소·advisory 우선").
#   - stale·orphan·oversized : domain freshness finding (lint_domain·ADR-0018). domain lint 는
#     enforcement 아닌 visibility — push 를 절대 막지 않는다(advisory only·`--gate` 제외).
#   - dangling-wikilink-scaffold : 어댑터 scaffold(.claude/.opencode) 에서만 등장하는 framework
#     ADR/idea dangling (T-0129). 채택자(framework ADR 부재 다운스트림)의 scaffold bracket-ref 는
#     영구 dangling 이 정상 — visibility 만, push 미차단. ticket dangling·wiki/root-doc dangling 은
#     여전히 `dangling-wikilink`(blocking).
#   - un-migrated-overlay : 어댑터 .md 에 리터럴 free-form 토큰 잔존 + overlay 부재 (T-0132·§3.6).
#     render-overlay 마이그레이션 누락 신호 — 채택자 운영 ritual 이지 출하 결함 아니므로 visibility
#     만, push 미차단. render-leak(@render 산출물 한정·blocking)과 별개·상보(활성화 전 본문 스캔).
_ADVISORY_LINT_KINDS: frozenset[str] = frozenset(
    {"status-done-accum", "unstable-ref-advice", "scope-advice",
     "stale", "orphan", "oversized", "adr-lifecycle", "architecture-stale",
     "dangling-wikilink-scaffold", "un-migrated-overlay"})


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
    """ADR lifecycle 정합 advisory (ADR-0021·never-block).

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
                if tgt_status != want_status:
                    findings.append((tgt, "adr-lifecycle",
                                     f"{adr_id} 이 {verb} 하는데 status={tgt_status or '없음'} (기대 {want_status})"))
        # 자가일관 — status 가 amended/superseded 면 back-ref 가 있어야.
        if status == "amended" and not _as_id_list(fm.get("amended_by")):
            findings.append((adr_id, "adr-lifecycle", "status: amended 인데 amended_by 없음"))
        if status == "superseded" and not _as_id_list(fm.get("superseded_by")):
            findings.append((adr_id, "adr-lifecycle", "status: superseded 인데 superseded_by 없음"))
    return findings


def _coerce_date(val) -> datetime.date | None:
    """frontmatter date 값을 `datetime.date` 로 정규화한다 (파싱 불가 → None·fail-soft).

    yaml 은 unquoted `2026-06-19` 를 `datetime.date` 로, quoted `'2026-06-19'` 를 str 로
    파싱한다(ticket 은 quote·ADR 은 unquote 관례). 둘 다 수용한다. datetime(시각 포함)은
    `.date()` 로, ISO str 은 `fromisoformat` 로, 그 외(None·빈값·잘못된 형식)는 None.
    """
    if isinstance(val, datetime.datetime):
        return val.date()
    if isinstance(val, datetime.date):
        return val
    if isinstance(val, str):
        try:
            return datetime.date.fromisoformat(val.strip())
        except ValueError:
            return None
    return None


def lint_architecture_freshness() -> list[tuple[str, str, str]]:
    """architecture.md freshness 강제함수 advisory (ADR-0022 Decision 3·never-block).

    `decisions/` 의 최신 ADR date(frontmatter `updated` 우선·없으면 `created` 의 최대값)가
    `architecture.md` frontmatter `updated` 보다 *더 최신*이면 "architect 가 architecture.md
    갱신 필요" 권고를 낸다 — 새 ADR 이 결정을 바꿨는데 현재-진실 doc(architecture.md)이
    따라가지 않은 신호. kind=`architecture-stale`(`_ADVISORY_LINT_KINDS` 등재로 `--gate`
    종료코드 비기여·visibility>enforcement).

    fail-soft: architecture.md 부재·frontmatter 없음·date 파싱 불가·decisions/ 부재 →
    [] (솔로/신규 clone·architecture 미사용 무영향). decisions/README.md·_template 류
    비-ADR 파일은 `[0-9]*.md` 글롭으로 제외(NNNN-slug 만)."""
    findings: list[tuple[str, str, str]] = []
    if not DECISIONS_DIR.is_dir() or not ARCHITECTURE_FILE.exists():
        return findings

    # architecture.md frontmatter updated — 부재/파싱 불가면 비교 불가 → graceful skip.
    try:
        arch_fm, _ = load_ticket(ARCHITECTURE_FILE)
    except Exception:  # noqa: BLE001 — frontmatter 없음/깨짐은 skip(비차단).
        return findings
    arch_updated = _coerce_date((arch_fm or {}).get("updated"))
    if arch_updated is None:
        return findings

    # decisions/ 최신 ADR date (updated>created 의 최대값) + 그 ADR id 추적.
    latest_date: datetime.date | None = None
    latest_adr = ""
    for p in sorted(DECISIONS_DIR.glob("[0-9]*.md")):
        try:
            fm, _ = load_ticket(p)
        except Exception:  # noqa: BLE001 — 깨진/frontmatter 없는 ADR 은 skip.
            continue
        fm = fm or {}
        d = _coerce_date(fm.get("updated")) or _coerce_date(fm.get("created"))
        if d is None:
            continue
        if latest_date is None or d > latest_date:
            latest_date = d
            latest_adr = _adr_id_from_path(p)

    if latest_date is not None and latest_date > arch_updated:
        findings.append((
            "architecture.md", "architecture-stale",
            f"최신 ADR({latest_adr}·{latest_date.isoformat()}) > "
            f"architecture.md updated({arch_updated.isoformat()}) — "
            f"architect 가 architecture.md 갱신 필요"))
    return findings


def _load_domain_module():
    """domain.py 를 경로 import 해 모듈로 반환한다 (부재/실패 시 None).

    **순환 회피 deep-import seam** — domain.py 가 `board.load_ticket` 을 import 하므로
    board 가 모듈 최상단에서 domain 을 import 하면 순환이다. lint_domain *함수 내부*에서만
    이 헬퍼로 지연 로드한다([[T-0081]] ticket_finish→domain 패턴 동형). domain.py 부재
    (솔로/신규 clone·구버전)·로드 실패 → None (호출부가 graceful skip).
    """
    if not DOMAIN_PY.exists():
        return None
    spec = importlib.util.spec_from_file_location("_domain_lint", DOMAIN_PY)
    if spec is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 — 로드 실패는 None 으로 흡수(비차단).
        return None
    return mod


def lint_domain() -> list[tuple[str, str, str]]:
    """domain freshness finding 을 board lint finding 으로 표면화 (advisory·비차단·ADR-0018).

    domain.lint_pages 의 `(kind, label, detail)` 를 board 관례 `(label, kind, detail)` 로
    재배열해 돌려준다. kind 는 domain 의 `stale`/`orphan`/`oversized` 를 보존 —
    `_ADVISORY_LINT_KINDS` 에 등재돼 `--gate` 종료코드에 *절대* 기여하지 않는다(visibility>
    enforcement). domain.py 부재·로드 실패·깨진 페이지·git 부재 → [] (솔로/domain 미사용
    프로젝트 무영향). domain.py 가 이미 graceful 이므로 얇게 위임하되, 어떤 예외도 [] 로
    흡수해 board lint 자체는 항상 정상 진행한다.

    git_runner 는 board 의 REPO 컨텍스트로 1회 생성해 domain.lint_pages 에 주입한다
    (per-page git 호출·테스트 hermetic seam). 생성 실패 시 stale 판정은 unknown 으로 떨어져
    finding 에서 빠진다(비차단).
    """
    domain = _load_domain_module()
    if domain is None:
        return []
    try:
        # DOMAIN_DIR 을 명시 전달 — load_pages 의 기본 인자는 정의 시점에 굳어
        # monkeypatch(테스트)·재바인딩을 못 본다(domain.cmd_lint 동형). 호출 시점의
        # 모듈 전역 DOMAIN_DIR 을 읽게 한다.
        pages = domain.load_pages(domain.DOMAIN_DIR)
        try:
            git_runner = domain._real_git_runner(REPO)
        except Exception:  # noqa: BLE001 — runner 생성 실패는 stale unknown 으로 흡수.
            git_runner = None
        findings = domain.lint_pages(pages, git_runner=git_runner)
    except Exception:  # noqa: BLE001 — 어떤 실패도 빈 결과로 흡수(board lint 정상 진행).
        return []
    # domain (kind, label, detail) → board (label, kind, detail) 재배열.
    return [(label, kind, detail) for kind, label, detail in findings]


def lint_tickets() -> list[tuple[str, str, str]]:
    """All lint issues — ticket dependency graph + body self-containment +
    idea status/directory agreement + status.md ✅ 완성 행 누적 권고(judgment-only·ADR-0023) +
    dangling wikilink + unstable (slug/filename) refs (ADR-0003) +
    family wiki scope 인지(ADR-0015) +
    domain freshness advisory(stale/orphan/oversized·ADR-0018·never-block) +
    architecture.md freshness advisory(architecture-stale·ADR-0022·never-block) +
    render-leak(리터럴 `{{...}}` 누출·ADR-0028·blocking·@render 산출물 한정·활성화 전 무발화) +
    un-migrated-overlay(리터럴 free-form 토큰 잔존·overlay 부재·T-0132·§3.6·advisory·never-block)."""
    return (lint_dependencies() + lint_bodies() + lint_ideas()
            + lint_status()
            + lint_wikilinks() + lint_unstable_refs() + lint_scopes()
            + lint_domain() + lint_adr_lifecycle()
            + lint_architecture_freshness()
            + lint_render_leak() + lint_unmigrated_overlay())


# ── board.md regeneration ──────────────────────────────────────────────

def refresh_board() -> None:
    """Regenerate .project_manager/wiki/board.md.

    scan(tickets/) + render + write 를 *하나의* `board_lock()` 구간 안에서 한다
    (ADR-0012 — 공유 단일파일 lost-update 방지). write 만 감싸면, 동시 변경 시
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
        for p in sorted((TICKETS_DIR / status).glob("T-*.md")):
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
                tag = ", ".join(fm.get("tags") or [])
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
    p.add_argument("--status", choices=STATUS_DIRS)
    p.add_argument("--tag")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id")
    p.add_argument("--session", help="session name (default $PM_SESSION_NAME or hostname-pid)")
    p.set_defaults(fn=cmd_claim)

    p = sub.add_parser("complete", help="mv claimed → done (sync gate enforced)")
    p.add_argument("id")
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
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser("unclaim", help="mv claimed → open")
    p.add_argument("id")
    p.set_defaults(fn=cmd_unclaim)

    p = sub.add_parser("unblock", help="mv blocked → open")
    p.add_argument("id")
    p.set_defaults(fn=cmd_unblock)

    p = sub.add_parser("new", help="create a new ticket")
    p.add_argument("title")
    p.add_argument("--touches", help="comma-separated file paths")
    p.add_argument("--depends", help="comma-separated ticket IDs")
    p.add_argument("--tag", help="comma-separated tags")
    p.add_argument("--estimate", choices=["small", "medium", "large"],
                   default="small")
    p.add_argument("--prefix", help="ID namespace prefix (default: local.conf "
                   "prefix / none → legacy T-NNNN)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("init", help="clone 당 1회 setup (solo · multi-repo N×M) — pm_state·local.conf·pre-push 훅")
    p.add_argument("--prefix", help="multi-repo (N×M) ID 네임스페이스 (예: PAY). 생략 = solo(legacy T-NNNN)")
    p.add_argument("--area", help="영역 설명 (namespaced: 새 prefix 최초 등록 시 필요)")
    p.add_argument("--owner", help="등록 식별자(registrant·기본: session 이름)")
    p.add_argument("--session", help="세션 이름 (기본: <prefix>-pm)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("regression",
                       help="회귀 게이트 (run=측정·기록 / check=HEAD green 검증·pre-push 훅용)")
    p.add_argument("action", choices=["run", "check"])
    p.add_argument("--cmd", help="테스트 명령 (기본: 활성 repo areas.md test_cmd → local.conf test_cmd → pytest -q)")
    p.add_argument("--cwd", help="회귀 실행 cwd (ADR-0014 seam·기본 REPO; multi-PM은 활성 repo worktree·T-0060 배선)")
    p.add_argument("--ticket", help="이 ticket 의 touches 로 스코프 (dev 빠른 루프·advisory)")
    p.add_argument("--touches", help="comma-separated 파일로 스코프 (advisory)")
    p.set_defaults(fn=cmd_regression)

    p = sub.add_parser("refresh", help="regenerate board.md")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("lint", help="check depends_on / blocks consistency")
    p.add_argument("--gate", action="store_true",
                   help="push 게이트 모드 — 차단 카테고리(dangling/unstable-ref/dependency/"
                        "thin)에만 종료코드 1, status drift 자문성은 0")
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser("promote-scope",
                       help="family wiki scope retag — `repoA → shared` (ADR-0015)")
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

    return parser


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
