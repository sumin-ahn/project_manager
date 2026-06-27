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
IDEAS_DIR = REPO / ".project_manager" / "wiki" / "ideas"
DECISIONS_DIR = REPO / ".project_manager" / "wiki" / "decisions"
SPECS_DIR = REPO / ".project_manager" / "wiki" / "specs"
ARCHITECTURE_FILE = REPO / ".project_manager" / "wiki" / "architecture.md"  # 현재-아키텍처 단일 진실 (ADR-0022·freshness lint 비교 대상)
HOOKS_DIR = REPO / ".project_manager" / "hooks"  # instance-owned lint hooks (ADR-0003)
BOARD_FILE = REPO / ".project_manager" / "wiki" / "board.md"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
STATUS_FILE = REPO / ".project_manager" / "wiki" / "status.md"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored): prefix, session


# ── board root (graceful 탐지·ADR-0033 ① 분리) ───────────────────────────────
# board(tickets+areas)는 `.project_manager/board/`(submodule)로 분리될 수 있다 — 그러면
# git 형상이 design(superproject)/board(submodule) 둘로 갈려 PM 운영 commit 이 코드 git 을
# 오염하지 않는다(ADR-0033 ①). 분리되지 않은 legacy(솔로·미마이그 adopter)에선 board 가
# wiki/ 안에 그대로 산다. 아래 board_root() 가 *실측*으로 둘을 가른다 — board/tickets 가
# 실제 dir 이면 board/ 루트, 아니면 wiki/ 루트(legacy). install_pre_push_hook 의 git-path
# 탐지 패턴 동형(존재할 때만 새 경로·없으면 현 위치).
#
# board-관련 경로(tickets·_template·areas)는 *상수가 아니라 함수*로 lazy 해소한다 — board/
# 존재 여부가 런타임(설치/마이그레이션)에 바뀔 수 있고, hermetic 테스트가 REPO 를 monkeypatch
# 한 뒤 board_root 가 그 tmp REPO 를 따라야 하기 때문(import-time 상수면 실 REPO 에 굳음).
# 나머지 wiki 잔류 경로(ideas/board.md/decisions/specs/architecture/log/status)는 board 가
# 아니라 설계축/파생물이므로 상수 그대로 둔다.

def board_root() -> Path:
    """board(tickets+areas) 루트 — board/ 분리 시 `<REPO>/.project_manager/board`, 아니면
    legacy `<REPO>/.project_manager/wiki`.

    `.project_manager/board/tickets` 가 실 디렉토리면 board 가 submodule 로 분리된
    형상(ADR-0033 ①) → board/ 루트. 아니면 board 가 아직 wiki/ 안에 있는 legacy 형상 →
    wiki/ 루트(현 위치·무변경). install_pre_push_hook 의 디렉토리-탐지와 동형 — *존재할
    때만* 새 경로로 갈리고, 없으면 현재 위치로 100% 폴백한다(솔로·미마이그 무영향).
    """
    base = REPO / ".project_manager"
    if (base / "board" / "tickets").is_dir():
        return base / "board"
    return base / "wiki"


def tickets_dir() -> Path:
    """ticket 디렉토리 — board_root()/tickets (board/ 분리 추종·legacy=wiki/tickets)."""
    return board_root() / "tickets"


def template_file() -> Path:
    """ticket 본문 템플릿 — tickets_dir()/_template.md (board_root 추종)."""
    return tickets_dir() / "_template.md"


def areas_file() -> Path:
    """areas 레지스트리 경로 (board_root 추종·*조건분기*).

    areas.md 는 legacy 에서 `.project_manager/areas.md`(wiki *밖*·committed shared registry)에
    산다 — tickets 처럼 wiki/ 안이 아니다. board/ 분리 시엔 board submodule *안*으로 옮겨야
    PM 운영(repo add 가 append)이 코드 git 을 오염하지 않는다(ADR-0033 ①). 그래서:
      - board/ 존재 → `board_root()/areas.md` (= board/areas.md·submodule 안)
      - legacy     → `<REPO>/.project_manager/areas.md` (현 위치·wiki 밖·무변경)
    """
    if (REPO / ".project_manager" / "board" / "tickets").is_dir():
        return board_root() / "areas.md"
    return REPO / ".project_manager" / "areas.md"


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


# user identity 해소 git 폴백 timeout — `git config user.email` 은 로컬 config 읽기라
# 즉답이지만(네트워크 0) 환경 이상에 대비해 짧은 상한을 둔다(엔진 subprocess 규약·_interp_runs 동류).
_GIT_USER_TIMEOUT_SECONDS = 5


def _git_config_email() -> str | None:
    """`git config user.email` 을 읽어 반환 — 미설정/git 부재/실패 → None (fail-soft).

    user identity 해소(`user_name`)의 폴백 레이어다 — `local.conf user=` 가 없을 때
    git 의 commit author email 을 user 식별자로 쓴다(spike §3.5·§6.3). subprocess 는
    엔진 규약대로 UTF-8 고정(한글 이름·메시지 안전)·짧은 timeout. git 바이너리 부재
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
    """user 식별자 해소 — `session_name()` 과 *동형* 우선순위 (T-0073 패턴·spike §3.5):

        override > local.conf `user=` > `git config user.email` > None

    `pm`(슬롯)이 *어느 PM 컨텍스트*인지(=`session_name()`)와 직교하는 **누가**(사람) 차원이다
    (ADR-0033 ③). multi-user 보드 공유에서 `created_by`(provenance)·`claimed_by`(assignee)·
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
                 user_override: str | None = None) -> str:
    """현재 identity 를 `<user>/<pm-slot>` 토큰으로 합성한다 (spike §3.2·ADR-0033 ③).

    `created_by`(provenance)·`claimed_by`(assignee) frontmatter 에 박는 값이다. user 가
    해소되면 `<user>/<pm>`, 미상(None)이면 슬롯만(`<pm>`) — **기존 슬롯-only 값과 형태가
    같다**(graceful·하위호환). 읽기측은 `/` 로 split 해 user/slot 을 분리하되, `/` 없는 값
    (구 ticket·user 미상)은 slot-only 로 읽어야 한다(fail-soft).
    """
    pm = session_name(session_override)
    user = user_name(user_override)
    return f"{user}/{pm}" if user else pm


def id_prefix(override: str | None = None) -> str | None:
    """Resolve ticket-ID namespace prefix (multi-repo areas·N×M·ADR-0016).

    prefix 는 M>1 repo 의 ID 네임스페이스(협업용 아님) — solo(N=1·M=1)는 부재.
    Order: override > local.conf `prefix=` > None. None → legacy `T-NNNN`
    (graceful / backward compatible). Non-None → `T-<PREFIX>-NNN` namespace.
    """
    if override:
        return override
    return local_config().get("prefix") or None


# areas.md 신/구 스키마 (ADR-0014 · T-0075 · T-0076 · T-0161).
#   - 구 스키마: `| prefix | area | owner |`                      (멀티-CLONE·ADR-0005)
#   - per-repo: `| repo | prefix | git | test_cmd | owner |`      (per-repo 레지스트리·ADR-0014)
#   - base 스키마: `| repo | prefix | git | test_cmd | owner | base |`  (base 브랜치·T-0075)
#   - protected 스키마: `| repo | prefix | git | test_cmd | owner | base | protected |`  (보호브랜치·T-0076)
#   - 신 스키마: `| … | protected | area_owner |`                 (user 소유·T-0161·ADR-0033 ③·refines ADR-0014)
#     area_owner = `--mine` 기본 풀 입력의 *user* 소유(spike §3.3·§6.4). ADR-0014 의 기존 `owner`
#     (per-repo registry registrant)를 overload 하지 않는 *별도* 칼럼이다(codex sug — 의미 충돌 회피).
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


# areas.md canonical 칼럼 순서 (신 스키마·ADR-0014·T-0075·T-0076·T-0161). 구 헤더(`base`/
# `protected`/`area_owner` 없음)는 이 순서의 *prefix* 다(`repo|prefix|git|test_cmd|owner` 또는
# …`|base`/…`|protected`). 그래서 헤더보다 셀이 많은 행(구 헤더에 신 칼럼 row 가 append 된
# *업그레이드* 프로젝트 — `repo add` 가 완전 canonical row 를 더 짧은 헤더에 붙인 경우)을 이
# canonical 순서로 이어 매핑해 `base`/`protected`/`area_owner` 유실을 막는다(codex T-0075 게이트가
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


def _repo_area_owner(repo: str) -> str | None:
    """그 repo 의 areas.md `area_owner`(user 소유) — 미지정/미등록/구 스키마 → None (T-0161·ADR-0033 ③).

    `--mine` 기본 풀(내 area 의 open 티켓) 판정의 입력이다(spike §3.3·후속 T-0164). ADR-0014 의
    기존 `owner`(per-repo registry registrant)와 의미가 다른 *별도* 칼럼 — overload 금지(codex sug).
    단일 user 토큰이다(목록/구분자 아님·spike §6.4). repo 명은 areas.md `repo` 칼럼과 매칭한다
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


# ── `--mine` 뷰 필터 (T-0164·ADR-0033 ④·spike §2.D · T-0168 단순화) ─────────
# 단일 공유 보드 위의 *렌즈*(별도 저장 아님). `board list --mine` 은 두 풀의 합집합:
#   (a) 내 area 의 open — status=open ∧ 그 티켓 area 의 `area_owner` == 내 user.
#   (b) 내 claim — `claimed_by` 의 *user* == 내 user (새 형태) ∨ `claimed_by` == 내 슬롯
#       (legacy 슬롯-only·user 차원 없는 claim). 상태 무관(연속성).
# graceful degrade(핵심·spike §2.D): user 미해소(None)이거나 보드에 area_owner 가 운영 중이지
# 않으면(미마이그레이션 채택자·솔로) (a)=전체 open 으로 떨어진다(빈 보드 금지·plain list 처럼).
# `cmd_list` 가 `_area_owner_in_use()`(areas.md 전역 1회 스캔)로 (a) 범위를 정한다 — per-user
# 2축 분기(`_owns_any_area`+`area_filter`)를 전역 플래그 1개로 단순화(T-0168 동반·사용자 결정
# 2026-06-26: 데이터 정합은 `board migrate-identity` 가 책임·런타임 폴백은 최소).

# 티켓 ID prefix 추출 — `_next_id` ID 발행 규약의 *정확한 역*:
#   prefixed = `T-{prefix}-{NNN}` (`_next_id` line 1011·prefix 는 리터럴 삽입·끝 -NNN 은 숫자),
#   legacy   = `T-{NNNN}`        (`_next_id` line 1013·하이픈 1개·prefix 없음).
# prefix 문법은 **등록/검증측(`pm_config._REPO_NAME_RE`·`^[A-Za-z0-9][A-Za-z0-9_-]*$`)과 정합**한다
# — repo add·init `--prefix` 가 그 패턴으로 prefix 를 검증·등록하므로 소비측도 같은 grammar 여야
# 한다(T-0164 round-3 must-fix: 소비 grammar 가 등록 grammar 보다 좁으면 `123` 같은 순수-숫자
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
# 동형 grammar)을 쓴다 — grammar drift 방지(T-0164 round-3 클래스: 한 곳 고치면 같은 가정의 다른
# 파서가 어긋남). `P0`(숫자)·`service-a`(하이픈)·`x_y`(언더스코어)·`123`(순수 숫자) prefix 포섭.
_TICKET_PREFIX_BODY = r"[A-Za-z0-9][A-Za-z0-9_-]*"
_TICKET_PREFIX_RE = re.compile(
    rf"^T-(?P<prefix>{_TICKET_PREFIX_BODY})-\d+$")
# prefixed | legacy 둘 다 매칭하는 ID 본체 (anchor 없음 — 호출측이 ^…$·\b 등으로 감싼다).
# 파일명/wikilink 파서가 공유한다(자체 `[A-Za-z]+` regex 두지 말 것 — `P0`/`service-a` 누락).
_TICKET_ID_BODY = rf"T-(?:{_TICKET_PREFIX_BODY}-\d+|\d+)"


def _ticket_prefix(tid: str) -> str | None:
    """티켓 ID 에서 네임스페이스 prefix 추출. legacy `T-NNNN`(prefix 없음) → None.

    `_next_id` 의 ID 발행 규약의 역이다: prefixed = `T-<PREFIX>-NNN`, legacy = `T-NNNN`.
    PREFIX 문법은 등록/검증측(`pm_config._REPO_NAME_RE`·`[A-Za-z0-9][A-Za-z0-9_-]*`)과 정합이라
    숫자(`P0`)·하이픈(`service-a`)·순수 숫자(`123`)를 포함할 수 있고 그런 ID(`T-P0-001`·
    `T-service-a-001`·`T-123-001`)도 해소된다. legacy 4자리 숫자 ID(`T-0164`)는 full-ID regex
    가 *내부 하이픈*(prefix-NNN)을 요구하는데 하이픈이 1개뿐이라 매칭 안 됨 → None(구조적 구분).
    """
    if not tid:
        return None
    m = _TICKET_PREFIX_RE.match(tid)
    return m.group("prefix") if m else None


def _ticket_area_owner(tid: str) -> str | None:
    """티켓의 area `area_owner`(user 소유) 해소 — 미상이면 None (T-0164·`--mine` (a) 입력).

    매핑 경로: ID prefix(`_ticket_prefix`) → areas.md 의 그 prefix 행(`_areas_row_for_prefix`)에서
    `area_owner` 를 *직접* 읽는다(`_active_test_cmd`/line 737 의 prefix-행 직접-읽기와 동형).
    미등록 prefix·area_owner 빈값은 None(area 비소유 처리).

    prefix 행에서 직접 읽는 이유(repo 칼럼 경유 재스캔 금지): areas registry 는 prefix-unique 만
    보장하고 repo-unique 는 아니다 — 두 prefix 가 같은 `repo` 칼럼값을 공유하면 `repo` 로 재스캔할
    경우 *그 repo 의 첫 행* area_owner 를 돌려줘 잘못된 소유자가 나온다. prefix 로 이미 정확한 행을
    잡았으니 그 행에서 바로 읽는다(이중 스캔도 제거).

    **no-prefix(솔로 self-host) 폴백 (T-0164 실버그·sole-area)**: 솔로 self-host(T-0123·
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
    """areas.md 에 non-empty `area_owner` 행이 **하나라도** 있는가 (T-0168 동반 단순화).

    `--mine` (a) 풀(내 area 의 open)을 area_owner 로 좁히는 건 *소유권 데이터가 보드에 실제로
    구성돼 있을 때만* 의미가 있다. 이건 **전역**(per-user 아님) 1회 판정이다 — areas.md 전체를
    한 번 스캔해 `area_owner` 칼럼이 어디든 채워져 있으면 True. 채워져 있으면 area_owner 파티션이
    운영 중(마이그레이션됨·multi-user)이라 (a) 를 area_owner==me 로 좁히고, 비어 있으면(미마이그레이션
    채택자·솔로) (a) 를 전체 open 으로 degrade 한다(빈 보드 금지·plain list 처럼).

    이전 per-user `_owns_any_area(my_user)`(내 소유 area ≥1 인가)를 대체한다 — 데이터 정합은
    마이그레이션 도구(`board migrate-identity`·T-0168)가 책임지고, 런타임 폴백은 **전역 플래그
    하나**로 최소화한다(사용자 결정 2026-06-26). area_owner 가 운영 중인데 *내* area 가 0개면
    (a) 는 자연히 빈다 — 그건 회귀가 아니라 '내 area 의 open 이 없음'이라는 올바른 결과다.

    areas.md 부재(솔로)·모든 area_owner 빈 값이면 False. ≥1 채워짐이면 True.
    """
    _header, rows = _parse_areas()
    return any((row.get("area_owner") or "").strip() for row in rows)


def _claimed_by_user(claimed_by: str | None) -> str | None:
    """`claimed_by`(`<user>/<pm-slot>`)에서 *user* 토큰 추출 — 슬롯-only/빈값은 None (T-0164·codex sug).

    `claimed_by` 는 이제 `<user>/<slot>`(ADR-0033 ③·T-0161) 또는 구 슬롯-only(`<slot>`)다.
    user 추출은 **마지막 `/` 분리** 규약(`rsplit('/', 1)[0]`) — slot 이 마지막 토큰이므로 user 에
    `/` 가 들어가도(이메일은 보통 없지만 안전) 정확히 분리한다. `/` 가 없으면(구 슬롯-only·user
    미상) None 을 반환해 (b) 매칭에서 graceful 제외한다.
    """
    if not claimed_by or "/" not in claimed_by:
        return None
    return claimed_by.rsplit("/", 1)[0] or None


def _ticket_is_mine(status: str, fm: dict, my_user: str | None,
                    my_slot: str, area_owner_in_use: bool) -> bool:
    """이 티켓이 `--mine` 뷰에 들어가는지 — (a) 내 area open ∨ (b) 내 claim.

    단일 전역 플래그 `area_owner_in_use`(보드에 area_owner 가 운영 중인가·`cmd_list` 가 1회 계산)로
    (a) 의 범위를 정한다 — per-user 2축 분기를 폐기했다(T-0168 동반 단순화·사용자 결정 2026-06-26).
    데이터 정합은 `board migrate-identity` 가 책임지고 런타임 폴백은 전역 1개로 최소화한다.

    (b) 내 claim — 상태 무관 연속성. 두 갈래를 OR 한다:
      - user 일치: `claimed_by` 의 user(`_claimed_by_user`) == my_user (새 `<user>/<slot>` 형태).
      - slot 일치: `claimed_by` == my_slot (legacy 슬롯-only·user 차원 없는 claim·round-4 must-fix).
        `my_user is not None and` 가드로 user-일치는 식별자가 있을 때만 — 무-identity 시 남의
        슬롯-only claim 을 내 것으로 오인하지 않는다(slot 일치는 항상 내 슬롯만 잡으므로 안전).
    (a) 내 area open — status==open 한정:
      - my_user 미상(None) 또는 area_owner 미운영(¬area_owner_in_use): 전체 open(빈 보드 금지·
        미마이그레이션/솔로 안전 degrade — plain list 처럼).
      - 그 외(user 해소 ∧ area_owner 운영): 그 티켓 area 의 area_owner == my_user 만.
    """
    tid = fm.get("id") or ""
    cb = fm.get("claimed_by") or ""
    # (b) 내 claim — user 일치(새 형태) OR slot 일치(legacy 슬롯-only·무-identity).
    if cb:
        cb_user = _claimed_by_user(cb)
        if (my_user is not None and cb_user == my_user) or cb == my_slot:
            return True
    # (a) 내 area 의 open.
    if status == "open":
        if my_user is None or not area_owner_in_use:
            return True
        return _ticket_area_owner(tid) == my_user
    return False


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
                 protected: str | None = None, area_owner: str | None = None) -> None:
    """Register a prefix in areas.md (append-only; create with header if missing).

    Append-only + `merge=union` (.gitattributes) → concurrent registrations from
    different clones never conflict.

    헤더 최초 생성(if-absent) + row append 를 **하나의 `board_lock()`** 구간으로
    원자화한다 (ADR-0012). 락이 없으면 동시 최초 등록 2개가 둘 다 "not exists" 를
    보고 → 둘 다 헤더를 write_text 해 한쪽이 다른쪽 append row 를 클로버한다(row 만
    O_APPEND 라도 헤더 race 가 남음). 락으로 감싸면 동시 최초 등록에도 헤더 1회·모든
    row 보존.

    스키마(ADR-0014·T-0075·T-0076·T-0161): per-repo 레지스트리
    `| repo | prefix | git | test_cmd | owner | base | protected | area_owner |`.
    `owner` = **등록 식별자(registrant)** — 협업 소유자(다중-사람)가 아니라 single user
    의 등록 출처 표식이다(ADR-0016·ADR-0002 amend). 기본 = 현 세션. 컬럼/형식은 보존
    (test_path 바인딩·regression 게이트가 의존) — 의미만 재정의.
    `repo`/`git`/`test_cmd`/`base`/`protected`/`area_owner` 미지정 시 빈 칼럼으로 채운다
    (부분 등록 허용·하위호환). `base`(T-0075)는 worktree 슬롯 브랜치가 파생될 base 브랜치
    — 빈 값/누락이면 `_repo_base` 가 None 폴백(worktree add 가 현행 bare HEAD 동작).
    `protected`(T-0076)는 PM 이 자율 commit/push 못 하는 보호 브랜치(쉼표분리) — 빈 값/
    누락이면 `_repo_protected` 가 `DEFAULT_PROTECTED`(main/master/develop) 폴백.
    `area_owner`(T-0161·ADR-0033 ③)는 그 area 의 *user* 소유(`--mine` 풀 입력) — `owner`
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
                "> per-repo 레지스트리 (ADR-0014·T-0075·T-0076·T-0161): repo → prefix → git → "
                "test_cmd → owner → base → protected → area_owner. 멀티-PM ID 네임스페이스 + "
                "per-repo 테스트 경로 + worktree base 브랜치 + 보호 브랜치 + user 소유의 단일 진실. "
                "append-only (`merge=union`).\n"
                "> `board.py init` / `pm-config repo add` 가 등록. "
                "prefix 유일성 = race-free ID 의 전제.\n\n"
                + _areas_header_line() + "\n"
                + _areas_separator_line() + "\n",
                encoding="utf-8")
        # O_APPEND atomic append (ADR-0012) — areas 는 append-only 레지스트리이므로
        # read-modify-write 가 아니라 OS 가 보장하는 원자 추가로 동시 등록 충돌을 없앤다.
        _append_atomic(
            af,
            f"| {_repo} | {prefix} | {_git} | {_test} | {owner} | {_base} "
            f"| {_protected} | {_area_owner} |\n")


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


def _configure_board_submodule() -> bool:
    """board submodule 의 `ignore = all` 을 자동 설정 (ADR-0033 ①·누출 0). 멱등·fail-soft.

    board 가 submodule 로 분리(`.project_manager/board/.git` 존재)된 형상에서만 동작한다 —
    superproject(design·코드 git)에서 `submodule.<path>.ignore all` 을 켜면, board(submodule)가
    PM 운영 commit 으로 전진해도 design 의 `git status`/`git diff` 가 그 gitlink drift 를 숨겨
    routine `git add -A` 가 board 포인터 bump 를 *우발 stage* 하지 않는다(board↔design 누출 0).

    fail-soft: git 바이너리 부재·git repo 아님·submodule 미분리(`.../board/.git` 없음·솔로/
    legacy)면 아무 것도 하지 않고 False 반환(솔로·미마이그 adopter 100% 무영향). 멱등:
    `git config` 는 같은 키를 덮어쓰므로 재실행 안전. 반환 True = 설정 적용.

    config 키 = `submodule.<.gitmodules-path>.ignore`(실측·hermetic git fixture로 확정·A5). board
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


# ── board git 즉시 sync (ADR-0033 ②·T-0163) ──────────────────────────────────
# board(tickets+areas)가 별도 git(submodule·standalone)으로 분리된 형상에서, ticket
# mutation 마다 board git 에 자동 commit + pull --rebase + push 한다. mutation 별 sync
# 강도가 다르다(spike §3.6·ADR-0033 ②):
#
#   - **claim = STRICT(원자·조율 primitive)**: pull 로 remote 선점을 먼저 반영 → 이미
#     남이 claim 했으면 작업 시작을 차단(race-lost·로컬 변경 0) → 로컬 claim commit →
#     push 가 성공해야 *비로소* 소유 확정. non-FF/conflict/offline 면 로컬 claim 을
#     rollback(티켓 open 복귀) + 명시 실패. best-effort 로 "내가 claim" 을 남기면 둘이
#     같은 일 = 중복작업 방지가 깨지므로 claim 만 strict 다.
#   - **new/complete/block/unclaim/unblock = best-effort local-first**: 로컬 commit 은
#     항상 성공(로컬) → pull --rebase ; push 는 best-effort → 실패 시 stale 경고 + 무차단
#     계속. active retry 루프는 두지 않는다 — 다음 mutation 의 pull-rebase+push 가 밀린
#     commit 을 자연 catch-up 한다(spike §3.6 "retry" 의 해석).
#
# **활성 게이트 = board 가 별도 git 일 때만**(`board_root()/.git` 존재). legacy(board 가
# wiki/ 안·별도 git 아님)면 sync 는 전부 no-op(git 호출 0·현 동작 byte-identical) —
# board_root() graceful 탐지와 동형이고, 기존 회귀가 green 으로 남는 핵심이다. 모든 git
# 호출은 fail-soft subprocess(엔진 규약·UTF-8 고정·짧은 timeout) — 거짓 원자성/락 보장을
# 만들지 않는다(best-effort 는 정직하게 경고, claim 만 명시 실패).

# board git 호출 timeout — pull/push 는 네트워크 왕복이라 user-email 폴백(5s)보다 길게
# 둔다. 환경 이상(hang·offline DNS)에서 무한 대기를 막는 상한(엔진 subprocess 규약).
_BOARD_GIT_TIMEOUT_SECONDS = 30


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


def _board_git(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    """board git working dir(`board_root()`)에서 git 명령을 실행한다 (UTF-8·timeout 고정).

    엔진 subprocess 규약: UTF-8 디코딩(한글 ticket/경로 안전)·짧은 timeout·`errors=replace`.
    `-C board_root()` 로 작업 디렉토리를 board git 으로 고정한다(cwd 의존 0). `check=False`
    가 기본 — 호출부가 returncode 로 분기하며, 예외(timeout·바이너리 이상)는 호출부가
    fail-soft 로 처리한다.
    """
    return subprocess.run(
        ["git", "-C", str(board_root()), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=_BOARD_GIT_TIMEOUT_SECONDS, check=check)


def _board_git_head() -> str | None:
    """board git 의 현재 HEAD SHA (없으면 None) — claim rollback 의 복귀 지점 기록용."""
    r = _board_git(["rev-parse", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else None


def _board_git_stage_and_commit(message: str) -> bool:
    """board git 에 tickets/ + areas.md 변경을 stage 하고 commit 한다 (로컬·항상 시도).

    누출 0: board git 엔 board 파일밖에 없으므로 `add -A` 가 설계(superproject)를 끌고
    가지 않는다(ADR-0033 ①). nothing-to-commit(변경 없음)이면 commit 은 rc≠0 이지만 그건
    정상(이미 동기)이라 호출부가 무시한다. 반환 True = 새 commit 생성, False = 변경 없음/
    실패(둘 다 호출부에서 best-effort 로는 무차단).
    """
    _board_git(["add", "-A"])
    r = _board_git(["commit", "-m", message])
    return r.returncode == 0


def _board_git_pull_rebase() -> subprocess.CompletedProcess:
    """board git 을 remote 최신화 (`pull --rebase`) — 선점/원격 변경을 로컬에 반영."""
    return _board_git(["pull", "--rebase"])


def _board_git_push() -> subprocess.CompletedProcess:
    """board git 을 remote 로 push — claim 소유 확정(strict)·best-effort 동기(나머지)."""
    return _board_git(["push"])


def _board_git_sync_best_effort(message: str) -> None:
    """best-effort local-first sync (new/complete/block/unclaim/unblock·spike §3.6).

    board 가 별도 git 이 아니면 no-op(legacy·솔로). 별도 git 이면: 로컬 commit(항상
    시도·로컬은 성공) → pull --rebase ; push 를 best-effort 로. offline/auth/conflict 등
    어떤 실패도 **작업을 차단하지 않는다** — stale 경고만 stderr 로 내고 계속한다. active
    retry 루프는 없다 — 밀린 commit 은 다음 mutation 의 pull-rebase+push 가 catch-up 한다.
    """
    if not _board_git_enabled():
        return
    try:
        _board_git_stage_and_commit(message)
        pull = _board_git_pull_rebase()
        push = _board_git_push() if pull.returncode == 0 else None
    except Exception as exc:  # noqa: BLE001 — fail-soft: best-effort sync 는 절대 작업을 막지 않는다.
        print(f"  ⚠ board sync 보류(다음 mutation 이 catch-up): {exc}", file=sys.stderr)
        return
    if pull.returncode != 0:
        print("  ⚠ board sync 보류 — pull --rebase 실패(offline/conflict). 로컬 commit 은 "
              "보존되며 다음 mutation 이 catch-up 한다.", file=sys.stderr)
    elif push is not None and push.returncode != 0:
        print("  ⚠ board sync 보류 — push 실패(offline/auth/non-FF). 로컬 commit 은 보존되며 "
              "다음 mutation 이 catch-up 한다.", file=sys.stderr)


def _board_git_claim_prefetch() -> str | None:
    """claim STRICT 1단계: `pull --rebase` 로 remote 선점을 로컬에 먼저 반영한다.

    board 가 별도 git 이 아니면 no-op·`""`(sentinel: sync 비활성·검증 진행). 별도 git
    이면 pull --rebase 를 시도한다:
      - 성공 → board git HEAD SHA 반환(claim commit 의 rollback 복귀 지점·truthy anchor).
      - 실패(offline·DNS·auth·rebase conflict) → None 반환. 호출부가 이를 **offline/도달
        불가**로 보고 claim 을 명시 실패시킨다(best-effort 로 "내가 claim" 을 남기면 중복작업
        — claim 은 조율 primitive 라 remote 도달 없이는 claim 불가).
      - pull 은 성공했으나 HEAD SHA 를 못 구함(빈 board git·detached 이상) → **None**.
        enabled 인데 rollback anchor 가 없으면 push 실패 시 거짓 소유를 되돌릴 수 없으므로,
        strict-claim 은 안전하게 *실패*해야 한다(로컬 변경 0·anchor 없는 진행 금지).
    반환 의미 3분: `""` = sync 비활성(legacy·confirm early-return True) · `None` = enabled-
    but-unreachable/no-anchor(claim 명시 실패) · `<sha>` = 유효 anchor(정상 진행).
    pull 이 winner 의 claim 을 끌어오면 working tree 에서 ticket 이 claimed/ 로 이동돼,
    뒤따르는 `find_ticket`/status 검사가 자연히 race-lost 를 표면화한다(로컬 변경 0).
    """
    if not _board_git_enabled():
        return ""  # sync 비활성 — pull 없이 검증만 진행(legacy·솔로).
    try:
        pull = _board_git_pull_rebase()
    except Exception:  # noqa: BLE001 — fail-soft: pull 예외(timeout 등)는 offline 취급.
        return None
    if pull.returncode != 0:
        return None
    # enabled 인데 HEAD 를 못 구하면 rollback anchor 부재 → None(거짓 소유 위험·안전 실패).
    return _board_git_head() or None


def _board_git_claim_rollback(orig_head: str) -> None:
    """로컬 claim 을 통째로 되돌린다 — `reset --hard <orig_head>` + winner 상태 반영 (절대 throw 금지).

    `orig_head`(prefetch 가 기록한 pull 직후 SHA)로 hard-reset 해 claim commit 을 되돌리고
    working tree 의 ticket 을 open/ 으로 복원한다(거짓 소유 0). 이어 `pull --rebase` 로 winner
    의 claimed 상태를 로컬에 best-effort 반영한다. **어떤 git 호출이 throw(timeout·git 소실
    등)해도 예외를 삼킨다** — confirm 이 ADR-0012 "loser 는 깨끗한 race-lost rc=1·never
    traceback" 을 어기지 않도록(rollback 이 cmd_claim 까지 예외를 새지 않게). reset/pull 자체가
    실패하면 복원이 불완전할 수 있으나, 그건 claim 을 *확정하지 않는다*(False 경로)는 사실과
    독립이다 — confirm 은 여전히 False 를 내고, 다음 mutation/claim 의 prefetch pull-rebase 가
    상태를 catch-up 한다.
    """
    with contextlib.suppress(Exception):
        _board_git(["reset", "--hard", orig_head])
    with contextlib.suppress(Exception):
        _board_git_pull_rebase()  # winner 의 claimed 상태를 로컬에 반영(best-effort).


def _board_git_claim_confirm(orig_head: str | None) -> bool:
    """claim STRICT 3·4단계: 로컬 claim 을 commit 하고 push 가 성공해야 소유 확정.

    board 가 별도 git 이 아니거나 prefetch 가 sync 를 비활성(`""`)으로 판단했으면 True
    (sync 무관 — 로컬 atomic-rename 만으로 claim 확정·legacy 동작 무변경). 별도 git 이고
    유효 anchor(`orig_head` = truthy SHA)면:
      1. commit(tickets/ + areas.md) — 로컬 claim 박제. **commit 이 새 commit 을 못 내면
         (identity 부재·hook·nothing-to-commit) push 가 "up-to-date" rc=0 을 내 remote 미전파
         인데 확정될 수 있다(거짓 소유) → commit 실패는 즉시 rollback + False.** claim 경로는
         항상 rename 변경이 있으므로 commit 은 반드시 새 commit 을 내야 정상이다.
      2. push — 성공(rc=0)해야 *비로소* 소유 확정(True).
      3. (commit 실패 ∨ push 실패 ∨ 예외) → `_board_git_claim_rollback` 후 False (거짓 소유 0).
    **어떤 경로에서도 bool 만 반환**한다(rollback 은 절대 throw 안 함) — cmd_claim(try 없음)이
    깨끗한 race-lost rc=1 을 내도록(ADR-0012·never traceback). False = 호출부가 race-lost /
    offline 으로 명시 실패시킨다.

    `orig_head` 가 빈 문자열(`""`)이면 = sync 비활성(legacy)이라 early-return True. None 은
    prefetch 가 이미 cmd_claim 에서 명시 실패로 걸러내므로(enabled-but-no-anchor·offline) 여기
    도달하지 않지만, 방어적으로 함께 True 가 아닌 *비활성* 으로만 취급한다(아래 not orig_head).
    """
    if not _board_git_enabled() or not orig_head:
        return True  # sync 비활성(legacy·anchor 없음) — 로컬 rename 만으로 확정(무변경).
    try:
        committed = _board_git_stage_and_commit("claim")
        if not committed:
            # commit 이 새 commit 을 못 냄 → push rc=0(up-to-date)이 거짓 확정을 낼 수 있다.
            _board_git_claim_rollback(orig_head)
            return False
        push = _board_git_push()
        if push.returncode == 0:
            return True
        _board_git_claim_rollback(orig_head)
        return False
    except Exception:  # noqa: BLE001 — fail-soft: 어떤 sync 예외도 claim 을 거짓 확정시키지 않는다.
        _board_git_claim_rollback(orig_head)
        return False


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
    return find_item(tickets_dir(), STATUS_DIRS, tid, "ticket")


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


def dump_ticket_atomic(path: Path, fm: dict[str, Any], body: str) -> None:
    """`dump_ticket` 과 같은 바이트를 쓰되 temp 파일 + `os.replace` 로 원자 교체한다.

    부분쓰기로 티켓 frontmatter 가 깨지는 것을 막는다(worktree_pool `_write_ledger`
    동형 — tmp 에 전체를 쓰고 같은 디렉토리 안에서 atomic rename). backfill 처럼
    *기존* 티켓을 제자리 갱신할 때 쓴다 — 같은 status 디렉토리 안 rename 이라 원자적이다.
    """
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).rstrip()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(f"---\n{fm_text}\n---\n{body}", encoding="utf-8")
    os.replace(str(tmp), str(path))


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


def _next_id(prefix: str | None = None) -> str:
    """Next ticket ID. Namespaced per prefix so concurrent areas never collide.

    prefix=None → legacy `T-NNNN` (4-digit). prefix="PAY" → `T-PAY-NNN` (3-digit),
    counted independently (scans only `T-PAY-*`). The legacy regex `T-(\\d+)-`
    never matches a prefixed file, so the two namespaces stay disjoint.
    """
    if prefix:
        n = next_numeric_id(tickets_dir(), STATUS_DIRS,
                            f"T-{prefix}-*.md", rf"T-{re.escape(prefix)}-(\d+)-")
        return f"T-{prefix}-{n:03d}"
    n = next_numeric_id(tickets_dir(), STATUS_DIRS, "T-*.md", r"T-(\d+)-")
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
    # claimed_by 는 `<user>/<slot>` (assignee·ADR-0033 ③·T-0161) — user 미상이면 슬롯만
    # (graceful·기존 슬롯-only 값과 형태 동일). 진행메시지/board surface 는 슬롯(sess)을 쓴다.
    assignee = identity_tag(session_override=args.session,
                            user_override=getattr(args, "user", None))

    # claim STRICT 1단계 (ADR-0033 ②·spike §3.6): board 가 별도 git 이면 먼저 pull --rebase
    # 로 remote 선점을 로컬에 반영한다. pull 이 winner 의 claim 을 끌어오면 ticket 이
    # claimed/ 로 이동돼 아래 status 검사가 race-lost 를 표면화한다(로컬 변경 0). pull 자체가
    # 실패(offline/도달 불가)하면 claim 불가 — best-effort 로 claim 을 남기면 중복작업이라
    # claim 만 strict offline-fail 한다. orig_head = pull 직후 SHA(claim commit rollback 지점·
    # legacy/sync 비활성이면 ""). None = offline.
    orig_head = _board_git_claim_prefetch()
    if orig_head is None:
        print(f"offline — board 도달 불가, {args.id} claim 불가", file=sys.stderr)
        return 1

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
    fm["claimed_by"] = assignee
    fm["claimed_at"] = now_utc()
    dump_ticket(new_path, fm, body)

    # claim STRICT 3·4단계 (spike §3.6): 로컬 claim 을 commit 하고 push 가 성공해야 *비로소*
    # 소유 확정. push 실패(non-FF/conflict/offline)면 로컬 claim 을 rollback(reset --hard
    # orig_head → ticket open/ 복원·commit 되돌림)하고 race-lost 로 명시 실패한다 — 거짓
    # 소유를 남기지 않는다. board 가 별도 git 이 아니면 confirm 은 True(로컬 rename 만으로
    # 확정·legacy 무변경).
    if not _board_git_claim_confirm(orig_head):
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
    _board_git_sync_best_effort(f"complete {args.id}")
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
    _board_git_sync_best_effort(f"block {args.id}")
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
    _board_git_sync_best_effort(f"unclaim {args.id}")
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
    _board_git_sync_best_effort(f"unblock {args.id}")
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
            # area_owner = 그 area 의 *user* 소유(`--mine` 풀 입력·ADR-0033 ③·T-0161) —
            # registrant `owner`(슬롯/세션)와 별개 칼럼(overload 금지·ADR-0014 refine).
            # cmd_repo_add 와 동형 해소: `--user` 명시 > local.conf user= > git config
            # user.email > None(빈 칼럼·_repo_area_owner None 폴백·현행 `--mine` 미포함).
            area_owner = user_name(getattr(args, "user", None))
            areas_append(prefix, args.area, owner, area_owner=area_owner)
            ao_surface = area_owner if area_owner else "(미상 — local.conf user= / git user.email 미설정)"
            print(f"✓ areas.md 등록: {prefix} | {args.area} | owner={owner} | area_owner={ao_surface}")
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
    # board submodule 이 분리된 형상(ADR-0033 ①)이면 ignore=all 자동 설정 — design(코드) git 이
    # board PM-commit 으로 오염되지 않게(누출 0). 솔로/미분리/git 부재면 no-op(fail-soft·무영향).
    if _configure_board_submodule():
        print("✓ board submodule ignore=all 설정 (코드 git 누출 0·ADR-0033 ①)")
    prompt_external_review_optin()
    mode = f"multi-repo · {prefix}" if namespaced else "solo (N=1·M=1)"
    idfmt = f"T-{prefix}-NNN" if namespaced else "T-NNNN (legacy)"
    print(INIT_GUIDE.format(mode=mode, idfmt=idfmt))
    return 0


# ── identity backfill 마이그레이션 (T-0168·ADR-0033 업그레이드 경로) ────────
# ADR-0033 이전 데이터(areas `area_owner` 부재·ticket `created_by` 부재·`claimed_by` 슬롯-only)를
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

    **구-헤더 업그레이드(T-0168 must-fix)**: ADR-0033 *이전* areas.md 는 `area_owner` 칼럼
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
            if "area_owner" in header_cells:
                ao_idx = header_cells.index("area_owner")
                out.append(line)  # 이미 신 스키마 헤더 — verbatim.
            else:
                # 구 헤더(area_owner 칼럼 부재) → 업그레이드. canonical prefix(per-repo
                # 레지스트리 계열·5/6/7칼럼이 `_AREAS_COLUMNS` 앞 N개와 일치)면 **canonical
                # 8칼럼 헤더로 교체**(본문 요구·base/protected 미지정분도 표면화). 그 외
                # 비호환 구 헤더(멀티-clone `prefix|area|owner`[3] 등 — 칼럼 의미가 canonical
                # 과 어긋남)는 정렬을 깨지 않게 **기존 헤더 끝에 area_owner 칼럼만 append**한다.
                upgrade = True
                if tuple(header_cells) == _AREAS_COLUMNS[:len(header_cells)]:
                    ao_idx = canonical_ao
                    out.append(_areas_header_line() + nl)
                    sep_cols = len(_AREAS_COLUMNS)
                else:
                    ao_idx = len(header_cells)  # 기존 칼럼 뒤에 append.
                    out.append("| " + " | ".join(header_cells + ["area_owner"])
                               + " |" + nl)
                    sep_cols = len(header_cells) + 1
            continue
        # 헤더보다 넓은 row 는 `upgrade` 여부와 무관하게 canonical area_owner 인덱스(7)로
        # 매핑한다 — `_parse_areas` 가 wider row(`len(cells) > len(header)`)를 헤더 무시하고
        # `_AREAS_COLUMNS` 순서로 매핑하는 것과 정확히 동형이다(area_owner=index 7). 비-canonical
        # 구 헤더(예 멀티-clone `prefix|area|owner`[3]) 아래에 canonical 8칼럼 row 가 append 된
        # 케이스에서 `ao_idx`(=헤더 폭) 로 읽으면 index 3(`test_cmd`)을 area_owner 로 오인해
        # backfill 못 한다 → wider row 면 무조건 canonical_ao 로 보정(must-fix·_parse_areas 정합).
        idx = ao_idx if ao_idx is not None else canonical_ao
        if len(cells) > len(header_cells):
            idx = canonical_ao
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

    areas.md 는 `areas_append`(repo 등록·ADR-0012/0014)가 *진짜* 공유 mutation 으로
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
    을 안 타고 lock-free atomic-rename(`move_ticket`)만 쓴다(ADR-0012). 따라서 migration 이
    board_lock 을 쥐어도 티켓 이동을 막지 못한다 — 락은 거짓 안전(차단만 유발)이라 안 잡는다.
    일회성 backfill 도구를 위해 claim/complete 같은 코어 hot-path 를 락-직렬화로 재설계하는
    것은 과설계다(PM 결정·T-0168). 대신 정직한 best-effort 로 착지한다:

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
      와의 lost-update 방지·진짜 공유 mutation·ADR-0012/0014).
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
    """ADR-0033 이전 데이터 일회성 backfill — areas area_owner·ticket created_by/claimed_by.

    `--user` override > `user_name()`(local.conf user= / git config user.email). 미해소(None)면
    abort(식별자 없이는 backfill 불가). `--dry-run` 은 쓰기 0·per-file 미리보기. `--scope`
    active(open+claimed) | all(기본·done 포함). 멱등(빈 필드만)·비파괴(순서/body/표 보존).

    `--session`/slot 은 출력·기본 identity 표시용이며 **backfill 대상 슬롯을 바꾸지 않는다**.
    슬롯-only `claimed_by`(`pm-1` 같은 `/` 없는 값)는 user 차원만 prepend 하고 *기존 슬롯
    토큰을 보존*한다(`pm-1` → `<user>/pm-1`). `--session` 은 부재 created_by 의 표시값과
    로그 표기에만 쓰이고, 이미 기록된 슬롯 토큰을 자신의 값으로 덮어쓰지 않는다(비파괴).

    **단일-세션 업그레이드 op (동시성 모델·T-0168)**: migrate-identity 는 *단일-세션* 업그레이드
    op 다. 다른 세션이 claim/complete 로 보드를 변경하는 중엔 실행하지 말 것 — 조용한 창에서
    1회 돌린다. 보드의 티켓 이동(claim/complete/block/unclaim)은 *설계상* board_lock 을 안
    타고 lock-free atomic-rename 만 쓰므로(ADR-0012), migration 이 락을 쥐어도 티켓 이동을
    막지 못한다. 따라서:
      - **areas write** 는 board_lock 으로 보호한다(`areas_append` 와의 lost-update 방지 —
        areas 는 진짜 락-보호 공유 mutation).
      - **티켓 backfill** 은 best-effort 다 — 각 티켓을 쓰기 직전 재조회해, 동시에 이동/완료
        됐으면 해당 티켓을 skip(경고)하고 살아 있으면 atomic write 한다. 재조회↔쓰기 사이의
        미세 TOCTOU 는 *하드 보장하지 않는다*(단일-세션 전제로 수용). 원자성·이동-차단을
        주장하지 않는다.
    board.md 재생성은 데드락 방지를 위해 (areas) 락 밖에서 1회 한다.
    """
    user = user_name(getattr(args, "user", None))
    if not user:
        print("[중단] user 식별자 미해소 — `--user <id>` 를 주거나 local.conf user= / "
              "git config user.email 를 설정하라(식별자 없이는 backfill 불가).",
              file=sys.stderr)
        return 1
    slot = session_name(getattr(args, "session", None))
    dry_run = bool(getattr(args, "dry_run", False))
    scope = getattr(args, "scope", "all") or "all"
    statuses = ("open", "claimed") if scope == "active" else STATUS_DIRS

    tag = "[dry-run] " if dry_run else ""
    print(f"{tag}migrate-identity — user={user} · slot={slot} · scope={scope}")

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
    # 파생 board.md 갱신("board.py 변경 명령마다 파생 보드 갱신" 계약·codex sug). migrate 가
    # claimed_by 를 바꾸면 board.md claimed 표시도 달라진다 — 실제 쓰기가 있었고 dry-run 이
    # 아닐 때만 1회 재생성한다(dry-run 은 파생물도 안 건드림·읽기-only 미리보기 보장).
    if wrote:
        refresh_board()
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
        if areas_file().exists():
            print(f"prefix {prefix!r} 미등록 (areas.md). `board.py init` 로 등록하거나 "
                  "등록된 prefix 사용.", file=sys.stderr)
            return 1

    tmpl_fm, tmpl_body = load_ticket(template_file())

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
        # created_by = `<user>/<pm-slot>` (provenance·불변·생성 시 set·ADR-0033 ③·T-0161).
        # "누가 추가했나" = 중복-작업 방지의 출처 표식. user 미상이면 슬롯만(graceful).
        fm["created_by"] = identity_tag(
            session_override=getattr(args, "session", None),
            user_override=getattr(args, "user", None))
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        fm["completed_at"] = None
        fm["touches"] = (args.touches.split(",") if args.touches else [])
        fm["depends_on"] = (args.depends.split(",") if args.depends else [])
        fm["blocks"] = []
        fm["tags"] = (args.tag.split(",") if args.tag else [])
        fm["estimate"] = args.estimate

        path = tickets_dir() / "open" / filename
        dump_ticket(path, fm, body)

    print(f"created {tid} ({_rel_to_repo(path)})")
    print("  → fill in 목표 / 완료 조건 / 참고, then `board.py lint` "
          "(placeholders left in the body fail lint)")
    refresh_board()
    _board_git_sync_best_effort(f"new {tid}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    # `--mine` 뷰(T-0164·ADR-0033 ④): 단일 공유 보드의 렌즈 — 내 area open + 내 claim.
    # identity 입력(T-0161)을 한 번 해소해 행마다 재계산 안 함. 무플래그 list 는 무변경.
    mine = getattr(args, "mine", False)
    my_user = user_name() if mine else None
    my_slot = session_name() if mine else ""
    # graceful degrade(T-0168 단순화): (a) 풀(내 area open) 필터는 보드에 area_owner 가 *운영
    # 중일 때만* 적용한다. areas.md 에 area_owner 가 하나도 안 채워졌으면(미마이그레이션 채택자·
    # 솔로) area_owner_in_use=False → (a) 가 전체 open 으로 degrade(빈 보드 금지·plain list 처럼).
    # per-user `_owns_any_area`+`area_filter` 2축 분기를 전역 1회 스캔 1개로 단순화(사용자 결정
    # 2026-06-26: 데이터 정합은 migrate-identity 가 책임·런타임 폴백은 최소).
    area_owner_in_use = mine and _area_owner_in_use()
    rows: list[tuple[str, dict]] = []
    for status in STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in (fm.get("tags") or []):
                continue
            if mine and not _ticket_is_mine(status, fm, my_user, my_slot,
                                            area_owner_in_use):
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
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
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
    # board/ 분리(ADR-0033 ①) 시 ticket 본문이 wiki/ 밖(board/tickets)으로 빠진다 — 그러면
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
            elif re.fullmatch(_TICKET_ID_BODY, name):
                # prefixed(`T-PAY-001`)·legacy(`T-0164`) wikilink 둘 다 ticket 참조로 본다
                # (multi-repo 보드·T-0164). grammar 는 `_TICKET_ID_BODY` 공유(자체 regex 금지).
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
# @render 어댑터 파일 = render_adapter 산출물(operational 토큰 치환·ADR-0028·ADR-0031). half-rendered
# 토큰(`{{...}}` 잔존)이 *출하 산출물* 에 새 나가면 harness-load 에이전트 지시가 무음 열화하므로
# 실결함 — blocking(경고 아님).
#
# 스캔 대상 = **@render manifest path 의 산출물**(T-0133 으로 활성: .claude/agents·skills·
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

    **트리 성격 게이트 (local.conf·ADR-0028 render-overlay 의미론)**: render-leak 은 *렌더
    산출물*(operational 토큰이 concrete 로 치환된 어댑터)의 미해소 토큰을 잡는 가드다. 그런데
    토큰-form *소스 트리*(① canonical worktree)는 산출물이 아니라 출하 전 원본이라 토큰이 정상
    이다. local.conf 부재 ⟺ 소스 트리(채택/init 전), 존재 ⟺ 채택 인스턴스(render 산출물 보유)
    이므로, local.conf 가 파일로 없으면 검사 대상 0(무발화)으로 잘라낸다 — `.opencode`(templates
    =소스)가 스캔에서 빠지는 것의 *트리-단위 일반화*. 이로써 루트 manifest 가 `.claude/* @render`
    여도 ① worktree(local.conf 부재)에선 토큰-form 어댑터를 산출물로 오인하지 않는다.
    """
    if not (REPO / ".project_manager" / "local.conf").is_file():
        return set()  # 토큰-form 소스 트리(local.conf 부재·① canonical) — render 산출물 아님.
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
    토큰을 잡는 가드다. 그 산출물 트리는 **루트 트리**다 — 채택자/②는 루트 manifest 가 @render 면
    루트 `.claude/`·`.opencode/` 가 렌더된 산출물이다. 도그푸딩 모노레포(이 repo·① canonical)는
    루트 manifest 가 `.claude/* @render` 여도 토큰-form 소스라 산출물이 아니다 — 그 트리-성격
    판별은 `_render_managed_relpaths` 의 local.conf 게이트가 한다(부재=소스 트리→검사 0·ADR-0028
    render-overlay 의미론). 따라서 이 함수는 manifest *위치*만 정하고, 토큰-form 소스의 무발화는
    local.conf 게이트가 보장한다.

    ⚠️ `templates/<harness>/` 는 **스캔하지 않는다**: 출하 템플릿은 *token-form 소스*다(`--target`
    이 copy2 로 토큰을 보존). 그 manifest 가 `.claude/agents @render` 여도 그건 *채택자가 import/
    update 할 때 렌더하라*는 표식이지 템플릿 자신이 렌더 산출물이란 뜻이 아니다 — 템플릿은 늘 토큰을
    가지므로 스캔하면 영구 오탐(T-0133: 활성화가 이 오탐을 표면화). 옛 구현은 templates/* 도 봤으나
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

    **트리 성격 무발화 경계**: 검사 대상 = engine.manifest 에서 `@render` 태그가 붙은 path 의
    산출물뿐(`_render_managed_relpaths`). 그 헬퍼는 local.conf 부재 트리(토큰-form 소스·①
    canonical)를 검사 0 으로 잘라낸다(local.conf=트리 성격 판별·ADR-0028 render-overlay 의미론)
    — 루트 manifest 가 `.claude/* @render` 여도 소스 트리에선 무발화, 채택 인스턴스(local.conf
    보유·render 산출물)에선 미해소 토큰을 잡는다. pm_render 의 post-render assertion 과 2중
    backstop — pm_update 가 마지막 도구였는지 무관한 상시 가드.

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


# ── un-migrated overlay 검출 (advisory · T-0132·§3.6·ADR-0031) ─────────────
# free-form(채택자 손편집 산문)의 canonical home 은 root doc(§프로젝트 고유 제약)·
# `pm_role.local.md`(§보호 영역)이고, pm_import 의 FILL 채널이 거기서 전담한다(ADR-0030). 따라서
# 어댑터 .md 는 free-form-free 여야 한다(토큰 0). 채택자가 *아직* 마이그레이션을 안 했으면 어댑터
# .md 에 리터럴 `{{PROTECTED_PATHS}}` 류가 잔존한다 — 이 lint 가 그 신호를 표면화한다(§3.6
# "un-migrated 검출"). render-leak(blocking·@render 산출물 한정)과 별개·상보: render-leak 은
# *활성화된* render path 의 미해소 토큰을, 이 lint 는 어댑터 본문의 미마이그레이션 토큰 잔존을 본다.
#
# **advisory only** — 마이그레이션 누락은 push 결함이 아니라 채택자 운영 ritual 신호(§3.6
# "push-block 아님·advisory")라 `_ADVISORY_LINT_KINDS` 에 등재(`--gate` 미차단). free-form 3종
# (로컬 `_UNMIGRATED_FREEFORM_KEYS`·ADR-0031 디커플)만 본다 — operational 토큰(`{{PROJECT_NAME}}`
# 등)은 import sed/local.conf 채널이라 별개. graceful: 어댑터 파일/디렉토리 부재 시 finding 0.

# 어댑터 스캐폴드 .md 글롭 — 채택자 tree 에 출하되는 harness 어댑터 본문 (존재하는 것만).
#   claude   : `.claude/agents/*.md`·`.claude/skills/**/SKILL.md`
#   opencode : `.opencode/agents/*.md`·`.opencode/command/*.md`
# 각 경로는 harness 별 존재 여부가 다르므로(claude 채택자엔 `.opencode` 부재·역도) 있을 때만 스캔.
# root 문서(CLAUDE.md·AGENTS.md 등)는 *제외* (T-0133): 채택자가 통째로 손편집하는 instance-owned
# scaffold 라 free-form 의 canonical home 이다(manifest 제외). 거기의 raw free-form 토큰은
# "미마이그레이션"이 아니라 "채택자가 아직 안 채움"이라 이 lint 의 오분류 대상이 아니다.
_OVERLAY_ADAPTER_GLOBS: tuple[tuple[str, str], ...] = (
    (".claude/agents", "*.md"),
    (".claude/skills", "SKILL.md"),
    (".opencode/agents", "*.md"),
    (".opencode/command", "*.md"),
)

# free-form 3종 토큰 — un-migrated-overlay lint 가 어댑터 .md 에서 스캔하는 리터럴 토큰 집합.
# pm_render 의 free-form value-fill 기계(FREEFORM_KEYS·overlay)는 ADR-0031 로 제거됐으므로,
# 이 lint 는 그 심볼에 의존하지 않고 자체 로컬 튜플로 검출 대상을 정의한다(디커플·단일 책임).
# pm_import.FREE_FORM_TOKENS(FILL 채널·canonical home 전담)와 동일 집합을 bare key 로 본다.
_UNMIGRATED_FREEFORM_KEYS: tuple[str, ...] = (
    "PROJECT_CONSTRAINTS",
    "PROTECTED_PATHS",
    "USER_GATE_ITEMS",
)


def _collect_overlay_adapter_files() -> list[Path]:
    """un-migrated 검사 대상 어댑터 .md — harness 스캐폴드 디렉토리만 (존재하는 것만).

    `.claude/skills` 는 `**/SKILL.md`(rglob), 그 외 디렉토리는 직속 `*.md`(glob)로 모은다.
    root 문서(CLAUDE.md·AGENTS.md 등)는 제외 — instance-owned scaffold 라 render-overlay
    관리 대상이 아니다(T-0133). dedupe 는 호출부가 path 로 처리. `.is_dir()` 가드로 부재
    harness/솔로 tree 는 조용히 건너뛴다(graceful·finding 0)."""
    files: list[Path] = []
    for rel, pattern in _OVERLAY_ADAPTER_GLOBS:
        d = REPO / rel
        if not d.is_dir():
            continue
        files.extend(d.rglob(pattern) if pattern == "SKILL.md" else d.glob(pattern))
    return files


def lint_unmigrated_overlay() -> list[tuple[str, str, str]]:
    """어댑터 .md 에 리터럴 free-form 토큰이 잔존하면 un-migrated 신호 (kind=`un-migrated-overlay`).

    `_ADVISORY_LINT_KINDS` 등재 → `lint --gate` 미차단(advisory·§3.6 "push-block 아님"). 마이그레이션
    누락은 채택자 운영 ritual 신호이지 출하 결함이 아니므로 visibility 만 제공한다.

    검사 (정적·shipped tree 스캔):
      - 어댑터 .md(`_collect_overlay_adapter_files`)에 리터럴 free-form 토큰
        (`{{PROJECT_CONSTRAINTS}}`/`{{PROTECTED_PATHS}}`/`{{USER_GATE_ITEMS}}`)이 잔존 → 파일·토큰별
        finding 1건. 마이그레이션 후엔 어댑터 .md 가 free-form-free(ADR-0030·토큰 0)다.

    디커플 (ADR-0031): render-overlay free-form value-fill 기계(`FREEFORM_KEYS`·overlay.local.yaml)
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
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # 코드 span/fence 예시 토큰은 실 placeholder 가 아니므로 제거 후 스캔(오탐 0).
        leaked = sorted(set(token_re.findall(_strip_code(text))))
        if leaked:
            toks = ", ".join("{{" + k + "}}" for k in leaked)
            issues.append((
                rel_posix, "un-migrated-overlay",
                f"리터럴 free-form 토큰 잔존: {toks} — 어댑터가 아직 canonical home(root doc·"
                f"pm_role.local.md)으로 마이그레이션되지 않았다(§3.6·ADR-0030·free-form-free)."))
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
    """ticket 파일명에서 canonical ID 추출 ('T-0036-foo.md' → 'T-0036'). 없으면 None.

    prefixed(`T-PAY-001-foo.md` → `T-PAY-001`·`T-service-a-001-…`)도 추출 — 발행측
    `_next_id` 가 prefixed 파일을 만드므로(T-0164). grammar 는 `_TICKET_ID_BODY` 공유.
    """
    m = re.match(rf"({_TICKET_ID_BODY})", filename)
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
#   - un-migrated-overlay : 어댑터 .md 에 리터럴 free-form 토큰 잔존 (T-0132·§3.6·ADR-0031 디커플).
#     canonical home(root doc·pm_role.local.md) 마이그레이션 누락 신호 — 채택자 운영 ritual 이지
#     출하 결함 아니므로 visibility 만, push 미차단. render-leak(@render 산출물 한정·blocking)과 별개.
#   - adapter-drift : 채택자의 adapter-layer(facade·진입문서·settings) 가 baseline(마지막 동기) 이후
#     upstream 에서 변경됨 (T-0141·ADR-0032 Decision 2). 전파 채널 없는 manifest-제외 잔여라 *전파 대신*
#     PM 에게 경고만 — `pm-update` 안내(visibility>enforcement). B 전파는 채택자 customization clobber(비파괴
#     위배)라 의도적 비-전파. instance-state(status·architecture·tickets·log·decisions·README·lite)는 채택자
#     소유·diverge 정상이라 scope 제외. push 미차단(never-block).
#   - adr-author : ADR frontmatter `author: <user>/<pm-slot>` provenance 권고 (T-0165·ADR-0033 ③).
#     "누가 결정했나"(provenance·연속성 아님)를 박는 발행측 규약 — board.py 는 ADR 을 발행하지 않으므로
#     부재/형식어긋남을 권고만 한다. solo·구 ADR(author 부재)은 정상이라 push 미차단(never-block).
_ADVISORY_LINT_KINDS: frozenset[str] = frozenset(
    {"status-done-accum", "unstable-ref-advice", "scope-advice",
     "stale", "orphan", "oversized", "adr-lifecycle", "architecture-stale",
     "dangling-wikilink-scaffold", "un-migrated-overlay", "adapter-drift",
     "adr-author"})


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


def _parse_adr_author(val) -> tuple[str, str] | None:
    """ADR frontmatter `author` 를 `(user, slot)` 으로 파싱한다 (ADR-0033 ③·spike §3.4).

    규약 = `<user>/<pm-slot>` — `created_by`/`claimed_by` identity 토큰과 동일 형태(`identity_tag`).
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
    """ADR `author` provenance 권고 advisory (T-0165·ADR-0033 ③·never-block).

    각 ADR frontmatter 에 `author: <user>/<pm-slot>`(누가 결정했나·provenance·연속성 아님)가
    박혀 있는지 권고한다 — board.py 가 ADR 을 *발행*하지 않으므로 발행측 규약을 강제하는 대신
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


# adapter-drift baseline 의 두 local.conf 키 (T-0141·ADR-0032 Decision 2·codex round-3 NEW-2).
# 한 키가 baseline 과 현재-관찰을 겸하면 race/자기비교라 *분리*한다:
#   - upstream_rev      : baseline — 마지막 성공 sync 의 upstream revision (pm_import·pm_update 가 기록·T-0145).
#   - upstream_seen_rev : 현재 관찰값 — pm-update 스킬이 upstream fetch 후 기록 (T-0142)·경로 upstream 은
#                         로컬 checkout rev 직접. cache 부재 URL 은 이 키 부재 → graceful skip.
_DRIFT_BASELINE_KEY = "upstream_rev"
_DRIFT_SEEN_KEY = "upstream_seen_rev"


def lint_adapter_drift() -> list[tuple[str, str, str]]:
    """adapter-layer drift advisory (T-0141·ADR-0032 Decision 2·never-block).

    채택자의 **adapter-layer manifest-제외 파일**(facade·진입문서·settings)이 baseline(마지막 동기)
    *이후* upstream 에서 변경됐는지 가시화한다. 이 잔여는 전파 채널이 없어(B 전파=채택자
    customization clobber·비파괴 위배) 소리없이 stale 되므로, *전파 대신* PM 에게 경고만 낸다
    (kind=`adapter-drift`·`_ADVISORY_LINT_KINDS` 등재로 `--gate` 종료코드 비기여·visibility>enforcement).

    **drift 판정 = baseline B**(codex MUST-FIX 2): "공식판과 다름"(채택자 customization 오탐)이 아니라
    "마지막 동기 이후 upstream 변경". **lint 는 git network 를 하지 않는다**(codex round-2·3): `local.conf`
    의 **2개 키**만 비교한다 —

      - `upstream_rev`      (baseline·마지막 성공 sync·pm_import/pm_update 가 기록)
      - `upstream_seen_rev` (현재 관찰값·pm-update 스킬이 upstream fetch 후 기록·경로 upstream 은 로컬 rev)

    둘 다 존재하고 **다르면** drift 1 finding(baseline 이후 upstream 이 앞섰다 = adapter-layer 가 낡았을 수
    있음). 한 키 2역 금지(race/자기비교 회피·codex round-3 NEW-2).

    scope(codex MUST-FIX 4): 대상 = adapter-layer(facade·진입문서·settings) / 제외 = instance-state
    (status·architecture·tickets·log·decisions·README 스캐폴드·lite — 채택자 소유·diverge 정상) /
    hooks·driver = open(Q3·대상 단정 안 함). lint 가 파일 단위 diff 를 하지 않으므로(rev 비교만) scope 는
    advisory 메시지로 안내하고, 제외 집합은 애초 비교 대상이 아니라 자동 충족된다.

    fail-soft (graceful 0-finding):
      - `upstream` 미설정(솔로·non-adopter·templates/upstream 부재 환경) → [].
      - baseline(`upstream_rev`) 미기록(아직 revision 추적 전·구 import) → [].
      - seen(`upstream_seen_rev`) 미기록(cache 부재 URL·pm-update 미실행) → [] + 안내는 내지 않음
        (false-positive flood 회피 — 관찰값 없으면 비교 불가). drift 는 *변경 확인*된 경우만 경고한다.
    """
    findings: list[tuple[str, str, str]] = []
    conf = local_config()

    # 솔로/non-adopter — upstream 자체가 없으면 비교할 대상이 없다 (graceful).
    if not (conf.get("upstream") or "").strip():
        return findings

    baseline = (conf.get(_DRIFT_BASELINE_KEY) or "").strip()
    seen = (conf.get(_DRIFT_SEEN_KEY) or "").strip()

    # baseline 미기록(구 import·revision 추적 전) 또는 seen 미기록(cache 부재 URL·pm-update 미실행)
    # → graceful skip. 한쪽이라도 없으면 "마지막 동기 이후 변경"을 단정할 수 없어 경고하지 않는다
    # (false-positive flood 회피·baseline B 의 핵심).
    if not baseline or not seen:
        return findings

    # 두 rev 가 같으면 baseline 이후 upstream 변경 없음 → clean.
    if baseline == seen:
        return findings

    # 다름 = baseline(마지막 동기) 이후 upstream 이 앞섰다. adapter-layer(facade·진입문서·settings)가
    # 낡았을 수 있으니 PM 에게 `pm-update` 안내 (전파 아님·never-block).
    findings.append((
        "adapter-layer", "adapter-drift",
        f"upstream 이 baseline({baseline[:12]}) 이후 변경됨(현재 관찰 {seen[:12]}) — "
        f"adapter-layer(facade·진입문서·settings) 가 낡았을 수 있음. "
        f"`pm-update` 로 동기 (instance-state·README·lite 는 채택자 소유·제외)"))
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
    adapter-layer drift advisory(adapter-drift·T-0141·ADR-0032·never-block·baseline rev 비교) +
    render-leak(리터럴 `{{...}}` 누출·ADR-0028·blocking·@render 산출물 한정·활성화 전 무발화) +
    un-migrated-overlay(어댑터 .md 리터럴 free-form 토큰 잔존·T-0132·§3.6·ADR-0031·advisory·never-block) +
    adr-author(ADR `author: <user>/<pm-slot>` provenance 권고·T-0165·ADR-0033 ③·advisory·never-block)."""
    return (lint_dependencies() + lint_bodies() + lint_ideas()
            + lint_status()
            + lint_wikilinks() + lint_unstable_refs() + lint_scopes()
            + lint_domain() + lint_adr_lifecycle() + lint_adr_author()
            + lint_architecture_freshness() + lint_adapter_drift()
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
    p.add_argument("--mine", action="store_true",
                   help="내 것만 (렌즈·단일 보드 위 필터·ADR-0033 ④): 내 area 의 open"
                        "(area_owner==나) + 내 in-progress(claimed_by.user==나). "
                        "솔로(user 미상)는 전체 open + 내 슬롯 claim 으로 graceful 폴백.")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id")
    p.add_argument("--session", help="session name = pm slot (default $PM_SESSION_NAME or hostname-pid)")
    p.add_argument("--user", help="user 식별자 — claimed_by 의 user 차원 (default: local.conf user= / "
                   "git config user.email · ADR-0033 ③)")
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
    p.add_argument("--user", help="user 식별자 — created_by 의 user 차원 (default: local.conf user= / "
                   "git config user.email · ADR-0033 ③)")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("init", help="clone 당 1회 setup (solo · multi-repo N×M) — pm_state·local.conf·pre-push 훅")
    p.add_argument("--prefix", help="multi-repo (N×M) ID 네임스페이스 (예: PAY). 생략 = solo(legacy T-NNNN)")
    p.add_argument("--area", help="영역 설명 (namespaced: 새 prefix 최초 등록 시 필요)")
    p.add_argument("--owner", help="등록 식별자(registrant·기본: session 이름)")
    p.add_argument("--user", help="area_owner = 그 area 의 user 소유 (`--mine` 풀 입력·ADR-0033 ③·T-0161). "
                                  "미지정 시 local.conf user= / git config user.email 로 해소(없으면 빈 값).")
    p.add_argument("--session", help="세션 이름 (기본: <prefix>-pm)")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("migrate-identity",
                       help="ADR-0033 이전 데이터 일회성 backfill — areas area_owner·ticket "
                            "created_by·슬롯-only claimed_by (멱등·비파괴·dry-run 선검토). "
                            "단일-세션 op: 다른 세션이 claim/complete 중일 땐 실행 말 것"
                            "(조용한 창에서 1회). areas write 는 락 보호·티켓 backfill 은 "
                            "best-effort(동시 이동 시 해당 티켓 skip).")
    p.add_argument("--dry-run", action="store_true",
                   help="변경 미리보기(쓰기 0·per-file 요약). 먼저 실행 권장.")
    p.add_argument("--user", help="identity override (기본: local.conf user= / git config "
                   "user.email · 미해소 시 abort)")
    p.add_argument("--session", help="slot 표시값 (기본: $PM_SESSION_NAME / local.conf "
                   "session= / hostname-pid) — backfill 대상 슬롯을 *바꾸지 않음*. 슬롯-only "
                   "claimed_by 는 기존 슬롯 토큰을 보존하고 user 차원만 prepend(비파괴)")
    p.add_argument("--scope", choices=["active", "all"], default="all",
                   help="active=open+claimed 만 · all=done 포함(기본)")
    p.set_defaults(fn=cmd_migrate_identity)

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
