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
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored): py·test_cmd·ctx_* + solo-legacy prefix/session (multi 홈은 유도·ADR-0040)


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


def drafts_dir() -> Path:
    """미충전 draft 격리 디렉토리 — tickets_dir()/.drafts (board_root 추종·T-0198).

    `board.py new` 가 board-git 활성(공유) 상태에서 placeholder/thin 본문을 감지하면
    티켓을 `tickets/open/` 이 아니라 이 디렉토리에 둔다 — STATUS_DIRS(`open/claimed/
    blocked/done`) *밖*이라 STATUS_DIRS 를 순회하는 어떤 mutation(`_board_git_stage_and_commit`
    의 `git add -A`·`_board_git_status_porcelain` 의 `git status --porcelain`·list·lint 등)도
    draft 를 보지 못한다 — 다음 mutation 이 무관 draft 를 실수로 board-git 에 커밋하는 leak
    을 원천 차단한다(T-0196 이 생성 시점만 막고 후속 mutation 은 못 막던 결함의 재발 방지).
    `_BOARD_GIT_DRAFT_PATHSPEC` 로 board-git 호출에서도 이중으로 명시 제외한다(방어적 이중화).
    본문을 채운 뒤 `board.py promote <id>` 가 `open/` 으로 이동 + board-git 커밋한다.
    """
    return tickets_dir() / ".drafts"


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
LIVEGATE_FLAG = LOCAL_DIR / "livegate.json"                 # last release live-gate result, keyed by ① worktree HEAD (ADR-0039·per-clone)
BOARD_LOCK = LOCAL_DIR / "board.lock"                       # OS file lock — board write serialization (ADR-0012)
# worktree_pool 의 LEASES_FILE 와 *같은 위치*(그 관례 — `.local/worktree-leases.json`). board 는
# worktree_pool 을 import 하지 않으므로(ADR-0013 isolation·touches 격리) 경로를 자체 해소해 파일을
# 직접 read 한다(T-0066 슬롯 test_cmd 레이어·아래 _active_slot_test_cmd). areas.md 읽듯 데이터-결합만.
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"            # worktree 리스 장부 (ADR-0013·read-only here)
DOMAIN_PY = REPO / ".project_manager" / "tools" / "domain.py"  # domain lint deep-import (순환 회피·아래 lint_domain)
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")
# Ideas have a simpler lifecycle than tickets — no claim/complete middle
# states, just `open → promoted|killed`.
IDEA_STATUS_DIRS: tuple[str, ...] = ("open", "promoted", "killed")


# ── PM-홈 worktree 오실행 가드 (T-0345·mutation dispatch 게이트) ───────────────
# board 계열 도구를 PM 홈(②)의 등록 worktree(`work/<repo>_<N>`) cwd 에서 실행하면 도구가 cwd
# 기준 자기-앵커(REPO)로 그 worktree 트리에 조용히 착지해 stray 산출을 낸다 — `board.py new`
# 가 잘못된 ID 네임스페이스의 `T-0001` 을, `ticket_finish` 가 stray `wiki/log/current.md` 를
# 만든다(PM 71 한 세션 3회 실측). worktree(①)는 코드 전용이고 board 는 PM 홈(②)이 소유하기
# 때문이다(ADR-0027). 이 silent-misanchor 클래스를 **fail-loud** 로 폐쇄한다 — mutation 명령이
# 착지 *전에* 실제 PM 홈 경로를 안내하며 중단한다. 자동 재앵커(silent redirect)는 택하지
# 않았다: 오탐 시 *다른* board 에 조용히 쓰는 더 나쁜 silent 결과가 되고, 두-git 경계를
# 가로지르는 board-git sync/counter 재해소가 취약하다. fail-loud 는 최소·안전하며 [[T-0335]]
# (silent 자동화보다 명시)·livegate seam(T-0287)의 동형 규율이다.
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
# retag·refresh=파생 board.md 재생성(worktree refresh 는 정당 사용 없음·stray dashboard 방지).
_MUTATION_SUBCOMMANDS: frozenset[str] = frozenset({
    "new", "promote", "claim", "complete", "block", "unclaim", "unblock",
    "init", "migrate-identity", "promote-scope", "reid", "refresh",
    "idea new", "idea promote", "idea kill",
    "prefix rename", "prefix strip", "prefix merge", "prefix delete",
})
# 조회(read-only·board 상태 미변경) — 게이트 없음.
_READ_SUBCOMMANDS: frozenset[str] = frozenset({
    "list", "show", "lint", "idea list", "prefix list",
})
# anchor-keyed sidecar — board 상태 아님. regression=`.local/regression.json`, livegate=
# 공유 engine-root `livegate.json`(둘 다 anchor HEAD 로 키·**worktree cwd 실행이 설계 의도**).
# 게이트하면 릴리즈/회귀 흐름이 깨진다(ADR-0039·T-0287) — 비게이트.
_SIDECAR_SUBCOMMANDS: frozenset[str] = frozenset({
    "regression", "livegate",
})


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


def _git_rev_parse(anchor: Path, *args: str, runner: Any = subprocess.run) -> str | None:
    """`git -C <anchor> rev-parse <args>` 결과(strip)를 반환. git 아님/오류/빈 값이면 None
    (fail-soft — 솔로/standalone·비-git 트리 무영향). `runner` 는 hermetic 테스트 주입 seam."""
    try:
        r = runner(["git", "-C", str(anchor), "rev-parse", *args],
                   capture_output=True, text=True, check=False)
    except Exception:
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

    board/ 분리(ADR-0033 ①·submodule)면 `board/tickets`, legacy 면 `wiki/tickets` 의 상태
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
          `<pm_home>/.repos/<repo>.git`(ADR-0027 두-git) 또는 `<pm_home>/.git`(단일 git worktree).
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
      1. anchor 자신이 *실 board* 를 소유하지 않음 — 소유하면 정당(①-자체 board 사용이 있는
         가상 채택자도 무영향). ①(공개 제품 worktree)은 코드 전용·board 미소유라 항상 통과
         (ADR-0027).
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
        f"PM 홈이 소유합니다(ADR-0027). 이대로면 이 worktree 에 stray 티켓/log 를 잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {anchor})",
        file=sys.stderr,
    )
    return True


# ── utilities ──────────────────────────────────────────────────────────

def local_config() -> dict[str, str]:
    """Per-clone local config (`.project_manager/local.conf`, git-ignored).

    Plain `KEY=value` lines; `#` comments and blank lines ignored. Missing → {}.
    Holds per-clone settings that must NOT be shared via git (py·test_cmd·ctx_*·
    upstream 등). Written by `pm-init`. `session=`/`prefix=` 는 **solo 형상 전용 legacy**
    (ADR-0040) — leased ≥2 인 multi 홈에서는 이 키가 있어도 무시되고 세션/prefix 는
    lease 장부에서 유도된다(session_name·id_prefix). solo 홈만 이 키로 폴백.
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


def _set_conf_keys(text: str, updates: dict[str, str]) -> str:
    """local.conf 텍스트에서 지정 키만 set-or-replace. 나머지 줄·주석은 보존.

    있으면 그 자리에서 `key=value` 로 교체(첫 등장만), 없으면 끝에 추가. stdlib only.
    pm_import._set_conf_keys 와 동형 — board 는 pm_import 를 import 하지 않으므로
    (의존 방향: pm_import 가 board init 을 subprocess 로 부르는 상위) board-local 사본을
    둔다(중복 최소·순수 프리미티브). cmd_init 의 비파괴 병합에 쓴다(T-0184).
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


def _load_identity_args():
    """공용 정체성 인자 모듈(`identity_args.py`·T-0322·ADR-0057)을 같은 tools/ 디렉토리에서
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
    spec = importlib.util.spec_from_file_location("identity_args", ia_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


identity_args = _load_identity_args()


def session_name(override: str | None = None, *, required: bool = False) -> str | None:
    """세션 식별자 해소 — count-based 유도 (ADR-0040 D1·T-0073 층위 amend·3모듈 *동형*):

        override(`--repo`/`--slot` 해소값·ADR-0057 — 액터 서브가 `_actor_session_override` 로
                 미리 계산해 넘긴다)
          > $PM_SESSION_NAME (env·harness 무관 엔진 식별자)
          > $CLAUDE_SESSION_NAME (env·deprecated alias·silent back-compat)
          > lease 장부 state=="leased" 행이 정확히 1개면 그 session   (단일-lease 유도)
          > (장부 부재·leased 0 = solo 홈) local.conf `session=`        (legacy 폴백)
          > None

    `PM_SESSION_NAME` 이 정식 이름(엔진 변수·하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias
    (둘 다면 PM 승·조용히 동작·안내는 문서만·`--json` 출력 오염 방지).

    **leased ≥2 (모호)면 local.conf 층을 건너뛴다** — per-clone 저장값(`session=`)으로 남의
    세션을 silent 오귀속하던 클래스를 원천 차단한다(ADR-0040·Windows 4슬롯 홈 리포트). 단일-lease
    값과 local.conf 값이 다르면 유도값(lease) 승 — 저장 쪽지보다 슬롯 파생 진실.

    `required=True`(귀속 쓰기: claim·migrate·init owner 기본값)에서 미해소(None)면 fail-loud —
    `--repo <repo> --slot <N>` 명시를 안내하고 `sys.exit` 한다(silent 오귀속 금지). `required=False`
    (surface: whoami/status/list --mine)면 None 을 반환한다(호출부가 "(비바인딩)" 표시). 구
    `<host>-<pid>` 최종 폴백은 세션-귀속 아닌 국소 용처(worktree_pool `_default_session` 의
    lease 취득 임시 명명)에만 잔존한다 — 여기(귀속 해소)선 제거.

    저장측(worktree_pool._default_session)과 매칭측(여기)이 어긋나면 per-slot test_cmd·claim
    소유권이 미스되므로([[T-0066]] must-fix) 세 모듈(board.session_name·worktree_pool._default_
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
    git 의 commit author email 을 user 식별자로 쓴다(spike §3.5·§6.3). subprocess 는
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
                 user_override: str | None = None) -> str | None:
    """현재 identity 를 `<user>/<pm-slot>` 토큰으로 합성한다 (spike §3.2·ADR-0033 ③).

    `created_by`(provenance)·`claimed_by`(assignee) frontmatter 에 박는 값이다. user 가
    해소되면 `<user>/<pm>`, 미상(None)이면 슬롯만(`<pm>`) — **기존 슬롯-only 값과 형태가
    같다**(graceful·하위호환). 읽기측은 `/` 로 split 해 user/slot 을 분리하되, `/` 없는 값
    (구 ticket·user 미상)은 slot-only 로 읽어야 한다(fail-soft).

    세션 미바인딩(`session_name` None·surface·required=False)이면 슬롯 토큰이 없으므로 user
    만(있으면) 반환하고 둘 다 없으면 None 이다(ADR-0040·graceful). 귀속 쓰기 호출부(claim)는
    이 함수 전에 `session_name(required=True)` 로 세션을 확정하므로 pm 이 None 으로 새지 않는다.
    """
    pm = session_name(session_override)
    user = user_name(user_override)
    if pm is None:
        return user
    return f"{user}/{pm}" if user else pm


def _actor_session_override(args: argparse.Namespace) -> str | None:
    """`--repo`/`--slot`(ADR-0057)에서 actor 연산(claim·init·migrate-identity·regression·
    livegate record·reid)의 세션 override 문자열을 해소한다 — 구 `args.session` 을 대체하는
    단일 seam(전 actor 서브 공유·중복 0).

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
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as e:
        sys.exit(f"[중단] {e}")
    if identity.task:
        # task-mode 귀속(F0 1단·F5b·spike §3b) — claimed_by/created_by 는 `<user>/<task>`. 작업공간
        # (F6 2단·실행 위치)이 아니라 **정체성 축**이라 여기서 task 이름을 그대로 세션 override 로
        # 돌린다(`--repo --slot` 공존 시에도 task 가 귀속을 이김·⑥ 예약으로 slot 세션과 기계 판별).
        #
        # **깔때기 1회 검증(must-fix·T-0355 게이트)**: 이 분기를 claim/new/migrate/reid 가 지나고, F6
        # 실행-위치 도구(regression/livegate/ticket_finish)는 `_resolve_task_workspace_cwd` 가 F6 이전
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
        # lease 세션으로 claim). ADR-0057 의 명시-정체성 계약·오귀속 방지 목적이라 explicit-unresolved
        # 는 폴백 아니라 명시적 실패로 닫는다(codex r2·kind=none 만 폴백·[[answer-feasibility-dont-decide]] 정신).
        try:
            session = identity_args.resolve_actor_slot(identity.repo, LEASES_FILE)
        except identity_args.SlotResolutionError as e:
            sys.exit(f"[중단] {e}")
        if session is None:
            sys.exit(
                f"[중단] repo '{identity.repo}' 의 활성 슬롯을 해소할 수 없다(활성 리스 0개). "
                f"`--repo {identity.repo} --slot <N>` 으로 슬롯을 명시하거나, 인자 없이(자동 해소) 실행하라."
            )
        return session
    return None


def _validate_actor_task_or_exit(task: str) -> None:
    """task 명 공유 validator 소비 — 불법이면 fail-loud (must-fix·T-0355 게이트·깔때기 단일 지점).

    정체성 깔때기(`_actor_session_override`·귀속)와 F6 실행-위치 해소(`_resolve_task_workspace_cwd`)가
    **같은 한 지점**을 소비해, 무검증 task 명이 created_by/claimed_by/lease-session 으로 영속되거나
    실행-위치로 쓰이기 **이전** 부작용 0 로 거부한다. 공유 validator = `identity_args.validate_task_name`
    (worktree_pool 엔진 validator 와 동형·로직 중복 0). 예약패턴(`<repo>_<N>`·⑥) 판정용 registered_repos
    는 areas 에서 fail-soft 해소(파싱 실패 시 char/traversal 검증만·cmd_alloc[T-0354] 패턴 동형).
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
    """task-mode(`--task`) 실행 위치를 F6 로 해소해 `(절대경로, 슬롯 test_cmd)` 를 반환한다 (T-0355·⑦).

    `--task` 미지정이면 `None`(task 아님·호출부는 기존 slot-mode 경로 유지). task 지정이면
    `identity_args.resolve_task_workspace`(F6 4행 표·모호=에러)로 슬롯을 특정하고 그 worktree
    **절대경로**(`REPO / ws.slot`)를 돌린다 — cwd 는 해소에 참여하지 않고(T-0345 불변) 순전히 장부+
    명시 인자로만 판정한다. F6 모호/미보유는 fail-loud(`[중단]` 접두·board 관례). 슬롯 test_cmd 는
    바인딩된 회귀명령(None=미바인딩·호출부 폴백).
    """
    if not getattr(args, "task", None):
        return None
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as e:
        sys.exit(f"[중단] {e}")
    # F6 해소 이전 깔때기 검증(must-fix) — 불법 task 명이 실행-위치로 쓰이기 전 fail-loud(귀속 경로와
    # 동일 validator·일관 메시지). F6 의 "보유 작업공간 없음" 보다 앞서 정확한 사유를 준다.
    _validate_actor_task_or_exit(identity.task)
    try:
        ws = identity_args.resolve_task_workspace(identity, LEASES_FILE)
    except identity_args.WorkspaceResolutionError as e:
        sys.exit(f"[중단] {e}")
    return str(REPO / ws.slot), ws.test_cmd


def _repo_from_session(session: str) -> str | None:
    """세션명 `<repo>_<N>` 에서 repo 를 추출한다 (ADR-0040 D3·id_prefix 세션 유도).

    슬롯 세션명은 `{repo}_{N}`(N=숫자 슬롯·worktree_pool `_slot_for`·pm_bootstrap lean
    T-0074)의 전단사 파생이다. 끝의 `_<숫자>` 마디를 슬롯 번호로 떼고 나머지를 repo 로
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
    """바인딩된 세션의 repo → areas.md 그 repo 행의 prefix (ADR-0040 D3·id_prefix 세션 유도층).

    `session_name(session)`(count-based·surface·required=False)이 세션을 해소하면 세션명을
    `<repo>_<N>` 로 파싱해(`_repo_from_session`) repo 를 얻고, areas.md 에서 그 repo 행의
    prefix 를 돌려준다 — per-repo prefix 의 단일 진실은 areas.md 칼럼이다(ADR-0040). 다음은
    모두 None → id_prefix 가 다음 층(count-based)으로 폴백:
      - 세션 미해소(None·모호 M>1·비바인딩) — `session_name()` 이 None(required=False라 fail-loud 아님).
      - 세션명이 `<repo>_<N>` 형태 아님(솔로 커스텀 세션명) — `_repo_from_session` None.
      - 그 repo 가 areas.md 에 미등록(또는 prefix 칼럼 빈 값·areas.md 부재).

    `session` 명시는 M>1 슬롯 순회(ADR-0040 D2·`_regression_run_slot`→`_test_cmd`)가 슬롯별로
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
    """Resolve ticket-ID namespace prefix (multi-repo areas·N×M·ADR-0016·ADR-0040 D3).

    prefix 는 M>1 repo 의 ID 네임스페이스(협업용 아님)다. 해소 체인(ADR-0040 D3·count-based
    유도·spike §3 D3):

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
    전용으로 강등**(ADR-0040) — 등록 repo 가 있으면(≥1) per-repo prefix 는 areas.md(세션/
    count 유도)가 단일 진실이고 clone 전역 키는 무시한다(남의 prefix 로 silent 오네임스페이스
    하던 클래스 차단). multi-repo(등록 repo ≥2) 홈에서 세션 유도·count-based 가 둘 다 실패
    (None)하면 `cmd_new` 가 fail-loud(오네임스페이스 방지).
    """
    if override:
        # override 를 등록된 canonical case 로 해소 (ADR-0055·prefix 동일성=case-insensitive fold):
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


def _areas_git_url(repo: str) -> str | None:
    """그 repo 의 areas.md `git` 칼럼 URL (bare mirror clone 원·ADR-0014). 미등록/부재/빈 값 → None.

    multi-user 공유 채택 폴더(= 하나의 git repo 를 여러 사람이 clone)에서 2번째 사용자가
    `.repos/<repo>.git` bare mirror 를 hydrate 할 때(`pm-config repo add <repo>` — `--git`
    불요·T-0291) clone 원 URL 을 여기서 해소한다. areas.md(git-tracked·공유)엔 URL 이 있으나
    `.repos/`(gitignore·per-clone)엔 mirror 가 없는 상황 — 등록된 URL 을 재제공 없이 재사용한다.

    repo 명은 areas.md `repo` 칼럼과 매칭한다(prefix 칼럼은 ADR-0042 로 비므로 **repo 명 키**).
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

# 티켓 ID prefix 추출 — `_next_id` ID 발행 규칙의 *정확한 역*:
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
# 명시 NEW-ID(`reid`·T-0259) 형식 sanity 용 full-match 앵커 — 발행 문법(`_TICKET_ID_BODY`)을 그대로
# 감싸 `T-NNNN`·`T-<pfx>-NNN` 둘 다 받고 그 외(빈 문자열·`foo`·`T-`·`X-1`)는 거른다. prefix 마디는
# 소비측 grammar(`_TICKET_PREFIX_BODY`·대문자/하이픈 legacy 포함)라 **자유 입력**이다 — 기존
# 네임스페이스로의 relabel 을 위해 좁은 `_validate_prefix`(소문자 카테고리)를 적용하지 않는다(ADR-0042).
# 앵커는 `\A…\Z`(codex T-0259 R3) — `$` 는 Python 에서 trailing newline 앞에서도 매치해 `T-0250\n`
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
    `T-service-a-001`·`T-123-001`)도 해소된다. legacy 4자리 숫자 ID(`T-0164`)는 full-ID regex
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


# 명시 `--prefix` 입력측 sanity (ADR-0042·spike §3.2) — 소비측 grammar(`_TICKET_PREFIX_BODY`·
# 대문자/하이픈 포함·legacy ID 역파싱용)와 **의도적으로 다르다**. 소비측은 기존 발행분(대문자·
# 하이픈 acronym)을 *해소*해야 하므로 넓지만, *새* 카테고리 prefix 입력은 좁게 권장형식으로 못박아
# mess 재발을 막는다(작업 카테고리 = 짧은 소문자·언더스코어). 유도/등록된 legacy prefix 는 검증
# 안 한다(cmd_new 는 명시 override 만·cmd_init 은 명시 --prefix 만 검사).
_PREFIX_RESERVED: frozenset[str] = frozenset({"none"})   # `none`=무prefix(T-NNNN) 1급 인자(T-0239)·실 prefix 예약 (case-insensitive·ADR-0055)
_PREFIX_FORMAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_]*$")    # 형식 sanity (영숫자 시작·이후 영숫자/`_`·대소문자 허용·ADR-0055)


def _validate_prefix(prefix: str) -> str | None:
    """명시 `--prefix` 형식 sanity — 위반 사유 메시지 반환, 정상이면 None (ADR-0042·ADR-0055).

    - 예약어(`none`·대소문자 무관): 무prefix(`T-NNNN`) 네임스페이스의 1급 인자로 예약(rename/
      merge 의 from/to/into·T-0239)이라 실 prefix 로 등록/사용 금지. prefix 동일성이
      case-insensitive fold 이므로 `NONE`/`None` 도 같은 예약어로 fold 돼 거부된다(ADR-0055).
    - 형식 `[A-Za-z0-9][A-Za-z0-9_]*`(첫 글자 영숫자·이후 영숫자/`_`): **대소문자 모두 허용**
      (ADR-0055 — prefix 동일성은 case-insensitive fold 이되 canonical case 는 보존). 하이픈·
      공백·특수문자·빈 문자열은 fail-loud(하이픈은 ID 구분자와 충돌 — 이번 변경 범위 아님).
    """
    if prefix.lower() in _PREFIX_RESERVED:
        return (f"prefix {prefix!r} 은 예약어 — 무prefix(T-NNNN) 네임스페이스의 1급 인자다"
                "(ADR-0042 §3.2·rename/merge·`none` 은 case-insensitive·ADR-0055). 실 prefix 로 쓸 수 없다.")
    if not _PREFIX_FORMAT_RE.match(prefix):
        return (f"prefix {prefix!r} 형식 위반 — 작업 카테고리는 "
                "`[A-Za-z0-9][A-Za-z0-9_]*`(첫 글자 영숫자·이후 영숫자/`_`·대소문자 허용·"
                "ADR-0055). 하이픈·특수문자·공백·빈 문자열은 금지"
                "(하이픈은 `T-{prefix}-NNN` ID 구분자와 충돌·ADR-0042).")
    return None


def _fold_lookup(prefix: str, pool: set[str]) -> str | None:
    """`pool` 에서 `prefix` 와 case-insensitive fold 로 같은 항목을 찾아 반환·없으면 None (ADR-0055).

    prefix 동일성은 소문자 fold 비교다 — 등록/발행된 canonical case 를 대소문자 무관하게 되찾는다
    (`aaa` 입력 → 등록 `AAA` 반환). 첫 매치를 돌려준다(레지스트리는 fold-유일하므로 매치는 최대 1개).
    """
    fold = prefix.lower()
    return next((p for p in pool if p.lower() == fold), None)


def _case_only_conflict(prefix: str, existing: set[str]) -> str | None:
    """`prefix` 가 기존 항목에 *대소문자만 다르게* fold-매치되면 그 기존 항목, 아니면 None (ADR-0055).

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
    """prefix 의 네임스페이스 동일성 키 — 문자열은 소문자 fold, None(무prefix)은 그대로 (ADR-0055).

    prefix 동일성은 case-insensitive fold 이므로 `AAA`≡`aaa` 는 같은 키(`aaa`)로, legacy 무prefix
    (None)는 None 과만 같은 키다. rename/merge/delete 의 **source-측 매칭·collision 정규화**가 이
    키로 비교해 `T-AAA-*` 를 `aaa` source 로 잡고, `T-AAA-001`/`T-aaa-001` 오염 공존을 collision 으로
    잡는다(canonical case 보존과 별개 — 여긴 *비교* 층이라 소문자 fold 만).
    """
    return prefix.lower() if prefix is not None else None


# ── 티켓 ID 참조 rewriter 코어 (ADR-0042 §3.3 step 4·T-0238) ───────────────────
# prefix rename/merge(T-0239)가 소비하는 순수·hermetic 프리미티브. old→new ID 맵을 받아
# 대상 파일(board tickets 본문·wiki/·log/)의 참조를 **토큰단위 정확치환**한다 — frontmatter
# `depends_on`·`[[wikilink]]`·bare inline·산문 임베드(`**A(T-0424 x)·B(...)**`)가 전부
# 리터럴 ID 토큰이라(spike §1.4 표기형 실측) 한 rewriter 로 전부 커버된다.
#
# 경계 규칙(spike §3.3 step 4·부분매치 방지): 매치된 old ID 양옆이 식별자 문자면 치환하지
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
    스캔 대상(depends_on 등 frontmatter 참조도 치환). 파일명 slug rename 은 범위 밖(T-0239).
    """
    root = Path(root)
    files: list[Path] = []
    for sub in (root / "board" / "tickets", root / "wiki", root / "log"):
        if sub.is_dir():
            files.extend(sub.rglob("*.md"))
    return list(dict.fromkeys(files))  # 순서보존 dedup (겹치는 서브트리 방어적 중복 제거)


def rewrite_refs(root: str | Path, id_map: dict[str, str], *, dry_run: bool) -> dict[str, int]:
    """대상 파일 전부에 토큰단위 rewrite 적용·규모 집계 반환 (rename/merge 공용 코어).

    반환 `{"ids": N, "refs": M, "files": K}` — N=id_map 중 *실제 참조된* old ID 수, M=총
    치환 refs, K=치환이 일어난 파일 수. `dry_run=True` 면 파일을 기록하지 않고 카운트만 낸다
    (`board.py prefix … --dry-run` 의 "N ID·M refs" preview 원천·T-0239 가 소비).
    """
    referenced: set[str] = set()
    total_refs = 0
    files_changed = 0
    for path in collect_rewrite_targets(root):
        # `newline=""` (universal-newline OFF): 원본 개행을 그대로 읽어 재쓰기 때 보존한다 —
        # CRLF 채택자(Windows 회사 실측)의 파일을 rewrite 가 LF 로 무단 정규화하지 않게 한다.
        # 읽기 실패는 OSError(권한·소실)뿐 아니라 UnicodeDecodeError(비-UTF-8 바이너리·깨진
        # 인코딩)도 포함해 넓게 잡되, **silent 누락 금지** — skip 한 파일을 stderr 로 1줄 경고해
        # 참조가 조용히 남는 것을 표면화한다(T-0238 reviewer should-fix·ADR-0042).
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
        if not dry_run:
            with path.open("w", encoding="utf-8", newline="") as fh:
                fh.write(new_text)
    return {"ids": len(referenced), "refs": total_refs, "files": files_changed}


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


def _distinct_area_owners() -> int:
    """areas.md 의 non-empty `area_owner` 칼럼에서 해소되는 *distinct user* 수 (T-0312·codex R3).

    `_area_owner_in_use`(채워졌나 bool)와 달리 **다중성**을 센다 — `multi_user` 신호의 두 번째
    축이다. `_distinct_ticket_users`(티켓 귀속만 셈)는 다중-owner 보드라도 claim 이 전부 legacy
    슬롯-only(user 토큰 0)면 ≤1 로 떨어져 그 보드를 solo 로 오판한다(→ legacy 슬롯-only 포함 경로가
    발동해 `--slot N` 이 타 area 의 legacy `<repo>_N` 을 suffix 매칭으로 끌어오는 누출·ADR-0056
    위반). areas 에 area_owner 가 2명 이상이면 티켓 user 토큰이 비어도 multi-user 보드다 — 그
    다중성을 여기서 세어 `multi_user = distinct ticket-user >1 OR distinct area_owner >1` 로 solo
    정의를 완결한다. areas.md 부재/전부 빈 값이면 0(솔로 신호 보존·회귀 0).
    """
    _header, rows = _parse_areas()
    return len({owner for row in rows if (owner := (row.get("area_owner") or "").strip())})


def _claimed_by_user(claimed_by: str | None) -> str | None:
    """`claimed_by`(`<user>/<pm-slot>`)에서 *user* 토큰 추출 — 슬롯-only/빈값은 None (T-0164·codex sug).

    `claimed_by` 는 이제 `<user>/<slot>`(ADR-0033 ③·T-0161) 또는 구 슬롯-only(`<slot>`)다.
    user 추출은 **마지막 `/` 분리** 규칙(`rsplit('/', 1)[0]`) — slot 이 마지막 토큰이므로 user 에
    `/` 가 들어가도(이메일은 보통 없지만 안전) 정확히 분리한다. `/` 가 없으면(구 슬롯-only·user
    미상) None 을 반환해 (b) 매칭에서 graceful 제외한다.
    """
    if not claimed_by or "/" not in claimed_by:
        return None
    return claimed_by.rsplit("/", 1)[0] or None


def _created_by_user(created_by: str | None) -> str | None:
    """`created_by`(`<user>/<pm-slot>`·`<user>`·슬롯-only)에서 *user* 토큰 추출 (T-0302·ADR-0053).

    `created_by` 는 `identity_tag` 산출(`<user>/<slot>`·session 미바인딩 시 user-only·user 미상
    시 슬롯-only) 또는 `migrate-identity` backfill(부재 → *순수 user*·line `_migrate_ticket_fm`)이다.
    `_ticket_owner` 의 2차 폴백 소유자(area_owner 미해소 시 항상-존재 소유)로 쓴다.

    `_claimed_by_user`(슬롯-only=None)와 갈리는 지점: **`/` 없는 값을 user 로 본다** — backfill 이
    부재 created_by 를 슬롯 없는 순수 user 로 채우므로(다중사용자 보드 migrate-identity 후 흔함) 그
    항상-존재 소유자를 살려야 유출을 없앤다([[prefer-data-migration-over-fallback]]·spike §2 옵션 (i)).
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


def _slot_matches(claimed_by: str, my_slot: str, *, mode: str = "exact") -> bool:
    """`claimed_by`(`<user>/<pm-slot>` 또는 legacy 슬롯-only)의 slot 토큰이 `my_slot` 인가.

    `mode="exact"`(기본·`--mine`/`--repo X --slot N`): 완전 일치 — slot 식별자 전체를 안다.
    `mode="repo"`(`--repo X` 단독·view/actor repo-scope·ADR-0057 결정 3): slot 규칙
    (`work/<repo>_<N>` → 세션 이름 `<repo>_<N>`)상 `my_slot` 이 repo 이름이므로, slot 토큰의
    repo(`_repo_from_session`·repo 명 `_` 안전·`--repo project` 가 `project_manager_1` 오매칭 안 함)가
    `my_slot`(그 repo 의 어느 슬롯이든) 이거나 토큰 자체가 `my_slot`(드문 비-숫자
    커스텀 슬롯)이면 매칭한다 — "그 repo 의 내 슬롯 전체"(spike §3.1).

    (구 `suffix=True` — 숫자 N 만으로 repo 불문 cross-repo 매칭하던 bare `--slot N` 뷰는
    ADR-0057 로 제거됐다: `--slot` 은 이제 `--repo` 없이는 fail-loud 라 도달 불가능한 경로.)
    """
    if not claimed_by:
        return False
    slot_token = claimed_by.rsplit("/", 1)[-1]
    if mode == "repo":
        return slot_token == my_slot or _repo_from_session(slot_token) == my_slot
    return slot_token == my_slot


def _ticket_owner(fm: dict, area_owner_in_use: bool) -> str | None:
    """open 티켓의 소유 user — `area_owner`(1차) ?? `created_by.user`(2차·항상-존재) (T-0302·ADR-0053).

    세션 뷰 (a) "내 소유 open" 판정의 소유자를 완전-데이터로 해소한다(spike §2 옵션 (i)·사인오프):
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
    """보드 전체 티켓의 `created_by`/`claimed_by` 에서 해소되는 *distinct user* 수 (T-0302·ADR-0053).

    **데이터-유도 다중사용자 신호**다(config 플래그 아님) — 세션 뷰 격리(`_ticket_is_mine`)가 소유
    미해소 open 을 solo(≤1)면 all-open degrade(회귀 0), 다중(≥2)이면 strict-exclude 로 가르는 게이트다.
    전 status 디렉토리를 1회 스캔해 `created_by`(→`_created_by_user`)·`claimed_by`(→`_claimed_by_user`)의
    user 토큰을 집합에 모아 크기를 센다 — 슬롯-only claimed_by·미상은 집합에 안 든다(graceful). 깨진
    티켓은 신호 산정에서 skip(fail-soft·크래시 0). areas 의 area_owner 가 아니라 *티켓* 귀속만 세는 건
    spike §3 정의(소유 데이터가 티켓에 실려야 다중사용자로 본다)다.
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
    """task end 소진 게이트용 티켓 스캔 (⑲·T-0354·자체 스캔·조회 전용·부작용 0).

    `board list --task` 렌즈(T-0365·wave 3)가 아직 없으므로 `pm-config task end` 가 이걸로
    자체 스캔한다. 두 축을 한 번의 status 디렉토리 순회로 모은다:

      - **claimed** — **진행 중(open/claimed/blocked)** 티켓 중 `claimed_by` 의 slot 토큰(마지막 `/`
        뒤·`_slot_matches`)이 `task` 와 일치하는 것. `task` 세션의 claim 형태 = `<user>/<task>`
        (identity_tag·⑥ slot 값 `<repo>_<N>` 예약과 기계 판별·task 명은 자유 포맷). `user` 가 주어지고
        claimed_by 에 user 토큰이 있으면 그 user 도 일치해야 한다(교차사용자 동명 task 오귀속 방지).
        slot-only claim(user 토큰 없음)은 graceful 포함. **terminal status(`done`)는 제외한다(must-fix
        ①)** — `cmd_complete` 는 done 이동 시 `claimed_by` 를 지우지 않으므로(status/completed_at 만),
        `<user>/<task>` 로 claim→complete 한 티켓이 done/ 에 claimed_by 를 남긴다. done 을 담으면 그
        티켓은 `unclaim`(status=="claimed" 요구)도 불가라 **해소 수단이 없어 task end 가 영구 차단**된다
        (⑲ "해소=complete 또는 unclaim" 설계와 모순). 완료된 작업은 소진 게이트 대상이 아니다. 이게
        비어야 task end 가 반납/이동으로 진행한다(거부 게이트).
      - **prefix_open** — `prefix` 가 주어지면 그 prefix(`_ticket_prefix`)의 `open` 티켓. **정보
        표시만**(차단 안 함·①·prefix≠경계) — task 지정 prefix 의 backlog 를 참고로 보여줄 뿐.

    각 row = {"id", "title", "status"}. 깨진 티켓은 skip(fail-soft). board 를 import 하지 않는
    pm_config 가 `_load_module` 로 로드해 소비한다(ADR-0013 isolation·ticket-스캔 단일 진실=board).
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
            # 해소 수단(unclaim=claimed 전용)이 없어 담으면 task end 영구 차단(must-fix ①·회귀 가드 존재).
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

    **단일 불변식**(전 surface 수렴·ADR-0053·ADR-0056·point-patch 금지): 필터 뷰 멤버십 = (내
    claim) ∪ (내 소유 open). 타 사용자의 claim·미claim open 은 **어떤 필터 뷰에도 안 나온다** —
    전체는 무필터 `list` 전용. querying identity(`my_user`)는 항상 **현재 사용자**(cmd_list 가
    `user_name()` 해소·ADR-0056·area_owner-derived 폐기)다. degrade("전체 open=mine")는
    **solo(distinct user ≤1·`multi_user` False)에서만** 허용한다(spike §1).

    (b) 내 claim — 상태 무관 연속성 (user-first·ADR-0056). `claimed_by` 의 user 토큰 유무로 가른다:
      - **user-qualified**(`_claimed_by_user(cb)` non-None·`<user>/<slot>`): 내 것 iff `cb_user ==
        my_user`. **slot-scoped 뷰**(`--repo`/`--slot`·ADR-0057)면 `_slot_matches` AND 로 *내 것
        ∩ 그 슬롯(또는 그 repo 의 내 슬롯 전체)*(타 슬롯의 내 claim 은 slot 뷰서 제외·`--mine`
        엔 나옴). **비제약 뷰**(`--mine`)면 user 만(전 슬롯). 타 사용자 claim(user 불일치)·my_user
        미해소(귀속 불가)는 제외 — 남의 user-claim 을 slot 번호로 끌어오는 누출 0.
      - **legacy 슬롯-only**(`cb_user is None`·user 토큰 없음): **진짜 solo(distinct user ≤1·
        `not multi_user`)에서만** `_slot_matches`(내 슬롯)로 포함한다. 게이트가 `my_user is None` 이
        아니라 `not multi_user` 인 건 `user_name()` 이 git email 폴백으로 solo 도 my_user 를 해소할
        수 있어(흔함) — my_user proxy 면 그 solo 의 자기 슬롯 legacy claim 을 잘못 숨긴다(codex R2
        회귀). multi_user 면 legacy 는 ambiguous → strict-exclude(migrate-identity backfill).
    (a) 내 소유 open — status==open 한정. `owner = _ticket_owner(fm, area_owner_in_use)`
        (area_owner ?? created_by.user):
      - my_user·owner 둘 다 해소 → strict `owner == my_user`(유출 0·유일 포함 규칙).
      - 미해소(my_user None ∨ owner None) + `multi_user` → `return False`(strict-exclude).
      - 미해소 + solo(¬multi_user) → `return True`(all-open degrade 보존·빈 보드 금지·회귀 0).
    open 은 미claim 이라 슬롯이 없다 — slot-scoped 뷰에서도 (a) 는 슬롯 무관 backlog(`--mine` 과
    동일 풀)로 두고 슬롯으로 좁히지 않는다(ADR-0056 #3).
    """
    cb = fm.get("claimed_by") or ""
    # (b) 내 claim — user-first (ADR-0056·T-0312). user 토큰 유무로 가른다.
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
            # proxy 면 그 solo 의 자기 슬롯 legacy claim 을 잘못 숨기기 때문(codex R2 회귀). multi_user
            # 면 legacy 는 ambiguous → strict-exclude(migrate-identity backfill).
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


def registered_repos() -> set[str]:
    """Repo names registered in areas.md (per-repo registry `repo` 칼럼·ADR-0014). 부재→빈 set.

    `registered_prefixes` 의 repo-명 짝. `repo add`(pm_config)가 멱등 재등록을 판별할 때
    쓴다 — repo명 자동시드 폐지(ADR-0042) 후 prefix 칼럼이 비므로 repo 존재 여부를 prefix
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
#   - claim(`cmd_claim` 의 load→rename 임계구역)도 board_lock 으로 직렬화한다 — POSIX rename 은
#     원자적이나 Windows os.rename 은 동시 프로세스에 배타적이지 않아(ADR-0012 Amendment·T-0213)
#     락으로 배타성을 복원한다(패배자는 깨끗한 `claim race lost`). complete/block 같은 비경합
#     전이(단일 소유 ticket)는 race 가 없어 락 없이 rename 만 쓴다.
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
    생성한다. 인코딩은 엔진 관례대로 UTF-8.
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

def _git_head_at(cwd: str) -> str:
    """주어진 작업 디렉토리의 git HEAD sha 를 반환한다 (실패/비-repo 면 '').

    `_git_head` 는 board 프로세스의 `REPO` 기준이지만, livegate 기록은 테스트가 실제로
    돈 **활성 slot worktree**(=`_regression_cwd`)의 HEAD 를 키로 삼아야 한다 — 보호훅이
    push 하는 sha(=① worktree HEAD)와 대조되기 때문. 이 함수가 그 cwd-매개 HEAD 를 낸다.
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
# 호출은 fail-soft subprocess(엔진 관례·UTF-8 고정·짧은 timeout) — 거짓 원자성/락 보장을
# 만들지 않는다(best-effort 는 정직하게 경고, claim 만 명시 실패).

# board git 호출 timeout — pull/push 는 네트워크 왕복이라 user-email 폴백(5s)보다 길게
# 둔다. 환경 이상(hang·offline DNS)에서 무한 대기를 막는 상한(엔진 subprocess 관례).
_BOARD_GIT_TIMEOUT_SECONDS = 30

# claim prefetch 반환 sentinel — board submodule 에 uncommitted 변경이 있어 pull --rebase
# 가 "스테이징하지 않은 변경" 으로 거부되는 상태(offline 아님·네트워크 정상·T-0175). prefetch
# 반환 4분(`""`=비활성 / 이 sentinel=dirty / None=offline·no-anchor / sha=anchor)에서 dirty 를
# offline 과 *메시지로* 가르기 위한 고유 토큰 — 어떤 git SHA(40-hex)와도 충돌하지 않는다.
_CLAIM_PREFETCH_DIRTY = "\0dirty"

# claim prefetch 반환 sentinel — board submodule 이 detached HEAD 인 상태(네트워크 정상·dirty
# 도 offline 도 아닌 제3의 상태·T-0203). detached 에선 `pull --rebase` 가 rc≠0 로 거부되는데,
# 이를 offline(None) 으로 오판하지 않도록 dirty sentinel 선례대로 별도 토큰으로 가른다. dirty
# 패턴 동형(`\0` 접두)이라 어떤 git SHA(40-hex)와도 충돌하지 않는다. best-effort sync 가
# detached 에서 commit 을 skip 해(T-0204) board 가 dirty 로 남을 수 있으므로, prefetch 는 이
# sentinel 을 dirty 보다 *먼저* 판정한다(detached 안내가 dirty 안내보다 우선·원인 정확).
_CLAIM_PREFETCH_DETACHED = "\0detached"

# board-git pathspec — `tickets/.drafts/`(drafts_dir()) 를 `git add`/`git status` 에서 명시
# 제외한다(T-0198). draft 는 STATUS_DIRS 순회 밖이라 이미 안 보이지만, 이 pathspec 은 방어적
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


def _board_git(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    """board git working dir(`board_root()`)에서 git 명령을 실행한다 (UTF-8·timeout 고정).

    엔진 subprocess 관례: UTF-8 디코딩(한글 ticket/경로 안전)·짧은 timeout·`errors=replace`.
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


def _board_git_head_detached() -> bool:
    """board git 의 HEAD 가 detached 인가 (T-0203·T-0204 공유 primitive).

    `git symbolic-ref -q HEAD` 는 HEAD 가 브랜치를 가리키면(attached) 그 ref 를 출력하며
    rc=0, HEAD 가 커밋을 직접 가리키면(detached) **rc=1** 이다(`-q` 로 에러 메시지 억제·locale
    무관). detached HEAD 는 dirty 도 offline 도 아닌 제3의 상태로, prefetch 오진(T-0203)과
    best-effort orphan 누적(T-0204)의 공통 근원이라 두 경로가 이 판정을 공유한다.

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
    예외는 빈 문자열로 fail-soft 처리해 호출부가 dirty 아님(=clean·pull 진행)으로 보게 한다
    (dirty 선체크가 정상 claim 경로를 막지 않도록 보수적으로).

    `_BOARD_GIT_DRAFT_PATHSPEC` 으로 `tickets/.drafts/`(미충전 draft·T-0198)를 제외한다 —
    아니면 draft 가 untracked 로 남아 있는 동안 무관 claim 이 "board dirty" 로 오판돼 차단된다
    (draft 는 board-git 관점에서 아예 존재하지 않는 것처럼 보여야 한다).
    """
    try:
        r = _board_git(["status", "--porcelain", "--", *_BOARD_GIT_DRAFT_PATHSPEC])
    except Exception:  # noqa: BLE001 — fail-soft: status 예외(timeout 등)는 clean 취급(보수적).
        return ""
    return r.stdout if r.returncode == 0 else ""


def _board_git_stage_and_commit(message: str) -> bool:
    """board git 에 tickets/ + areas.md 변경을 stage 하고 commit 한다 (로컬·항상 시도).

    누출 0: board git 엔 board 파일밖에 없으므로 `add -A` 가 설계(superproject)를 끌고
    가지 않는다(ADR-0033 ①). nothing-to-commit(변경 없음)이면 commit 은 rc≠0 이지만 그건
    정상(이미 동기)이라 호출부가 무시한다. 반환 True = 새 commit 생성, False = 변경 없음/
    실패(둘 다 호출부에서 best-effort 로는 무차단).

    `_BOARD_GIT_DRAFT_PATHSPEC` 으로 `tickets/.drafts/`(미충전 draft·T-0198)를 stage 에서
    제외한다 — draft 는 STATUS_DIRS 순회 밖이라 이미 이 mutation 이 만든 변경 목록엔 안 잡히지만,
    `add -A` 자체가 board_root 전체를 스캔하므로 무관한 draft 를 *이* mutation 의 commit 에
    쓸어담을 수 있었다(T-0196 게이트가 생성 시점만 막고 후속 mutation 은 못 막던 leak). 이
    pathspec 이 그 leak 을 원천 차단한다 — 어떤 mutation(자기·무관 무엇이든)도 draft 를
    커밋하지 않는다. promote 는 draft 를 `open/` 으로 옮긴 *뒤* 이 함수를 부르므로 무영향.
    """
    _board_git(["add", "-A", "--", *_BOARD_GIT_DRAFT_PATHSPEC])
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

    **단 detached HEAD 는 예외(T-0204)**: commit *전* HEAD 상태를 점검해 detached 면
    commit/pull/push 를 전부 skip 하고 loud 경고만 낸다. detached 위의 commit 은 orphan 으로
    쌓이고 catch-up 이 구조적으로 불가하므로(pull --rebase 계속 실패), 침묵 누적 대신 부기를
    보류하고 복귀를 안내한다(파일 mutation 은 이미 완료라 작업 무차단은 유지).
    """
    if not _board_git_enabled():
        return
    # detached HEAD 가드 (T-0204): detached 에선 commit 이 orphan 으로 쌓이고 `pull --rebase`
    # 가 계속 실패해 "다음 mutation 이 catch-up" 약속이 *구조적으로* 성립하지 않는다(attached
    # 브랜치의 일시 offline/conflict 만 상정한 동작). commit/pull/push 를 모두 skip 하고 loud
    # 경고만 내 orphan 무한 누적을 원천 차단한다. 파일 mutation(rename·frontmatter)은 이미
    # 끝난 뒤라 작업은 무차단 — git 부기만 보류한다(best-effort=작업 무차단 원칙 유지). 자동
    # 복구(checkout/cherry-pick)는 PM 편집/브랜치 의도 침해라 하지 않고 안내만 한다(T-0203 동형).
    if _board_git_head_detached():
        print("  ⚠ board sync 보류 — detached HEAD. board git 부기를 건너뛴다(orphan commit "
              "누적 방지). `git -C .project_manager/board checkout <branch>`(예: main) 로 브랜치에 "
              "복귀하면 다음 mutation 이 일괄 commit·catch-up 한다. detached 에서 이미 쌓인 로컬 "
              "commit 이 있으면 복귀 후 `git -C .project_manager/board cherry-pick <sha>` 로 이식.",
              file=sys.stderr)
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
    이면 pull --rebase *전* board submodule 의 상태를 순서대로 선점검하고
    (detached → dirty → pull), 그 다음 pull --rebase 를 시도한다:
      - detached HEAD → `_CLAIM_PREFETCH_DETACHED` sentinel 반환 (T-0203). detached 에선
        `pull --rebase` 가 rc≠0 로 거부되는데 이를 offline(None) 으로 오판하지 않도록 가른다
        (네트워크는 정상). **dirty 보다 먼저** 판정한다 — best-effort sync 가 detached 에서
        commit 을 skip 해(T-0204) board 가 dirty 로 남을 수 있는데, 그때 dirty 안내
        ("commit 후 재시도") 는 오도이기 때문이다(detached 라 단순 commit 으론 복구 안 됨).
        호출부가 브랜치 복귀(checkout)/cherry-pick 을 안내하고 claim 을 차단한다(anchor 없음).
      - dirty(staged/unstaged/untracked 변경 있음) → `_CLAIM_PREFETCH_DIRTY` sentinel 반환.
        `pull --rebase` 는 dirty tree 에서 "스테이징하지 않은 변경" 으로 거부되는데, 이를
        offline 으로 오판하지 않도록 *먼저* 가른다(네트워크는 정상). 발행 직후 ticket 본문을
        Edit 하면 흔히 발생한다 — 자동 commit/stash 는 PM 의 편집 의도를 임의 처리해 위험하므로
        하지 않고, 호출부가 "commit 후 재시도" 를 안내하고 claim 을 차단한다.
      - 성공 → board git HEAD SHA 반환(claim commit 의 rollback 복귀 지점·truthy anchor).
      - 실패(offline·DNS·auth·rebase conflict) → None 반환. 호출부가 이를 **offline/도달
        불가**로 보고 claim 을 명시 실패시킨다(best-effort 로 "내가 claim" 을 남기면 중복작업
        — claim 은 조율 primitive 라 remote 도달 없이는 claim 불가).
      - pull 은 성공했으나 HEAD SHA 를 못 구함(빈 board git — detached 는 위 선체크가 이미
        걸러 정상적으론 여기 도달 안 함·fail-soft 판정실패 엣지만) → **None**.
        enabled 인데 rollback anchor 가 없으면 push 실패 시 거짓 소유를 되돌릴 수 없으므로,
        strict-claim 은 안전하게 *실패*해야 한다(로컬 변경 0·anchor 없는 진행 금지).
    반환 의미 5분: `""` = sync 비활성(legacy·confirm early-return True) ·
    `_CLAIM_PREFETCH_DETACHED` = board detached HEAD(checkout 안내·offline 아님·claim 차단) ·
    `_CLAIM_PREFETCH_DIRTY` = board dirty(commit 안내·offline 아님·claim 차단) ·
    `None` = enabled-but-unreachable/no-anchor(offline·claim 명시 실패) · `<sha>` = 유효
    anchor(정상 진행). detached·dirty·offline 을 *메시지로* 가르는 게 핵심 — 각 케이스에 다른
    원인의 메시지가 섞여 나오면 안 된다(오판·이중출력 0).
    pull 이 winner 의 claim 을 끌어오면 working tree 에서 ticket 이 claimed/ 로 이동돼,
    뒤따르는 `find_ticket`/status 검사가 자연히 race-lost 를 표면화한다(로컬 변경 0).
    """
    if not _board_git_enabled():
        return ""  # sync 비활성 — pull 없이 검증만 진행(legacy·솔로).
    # pull --rebase *전* detached HEAD 선체크 — detached 면 pull 이 rc≠0 로 거부되는데 이를
    # offline 으로 오판하지 않도록 가른다(네트워크 정상·브랜치 복귀가 정답). **dirty 보다 먼저**
    # 둔다: best-effort 가 detached 에서 commit 을 skip 해(T-0204) board 가 dirty 로 남을 수
    # 있는데, 그때 dirty 안내("commit 후 재시도")는 오도라(detached 라 단순 commit 불가) detached
    # 안내가 우선해야 원인 정확(순서: detached → dirty → pull·T-0204 상호작용).
    if _board_git_head_detached():
        return _CLAIM_PREFETCH_DETACHED
    # pull --rebase *전* dirty 선체크 — dirty 면 pull 이 "스테이징하지 않은 변경" 으로 rc≠0 인데
    # 이를 offline 으로 오판하지 않도록 먼저 가른다(네트워크 정상·commit 후 재시도가 정답).
    if _board_git_status_porcelain().strip():
        return _CLAIM_PREFETCH_DIRTY
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


def _active_slot_test_cmd(session: str | None = None) -> str | None:
    """활성 worktree 슬롯(lease)에 바인딩된 test_cmd (T-0066·ADR-0014 amend·없으면 None).

    같은 repo 의 슬롯들이 서로 다른 빌드 타깃(HIL config 1/2/3·full vs a-only 등)을
    지속적으로 가질 수 있게 — `_test_cmd` 가 이를 repo areas *위* 레이어로 끼운다.

    **board 는 worktree_pool 을 import 하지 않는다**(ADR-0013 isolation·touches 격리).
    대신 리스 장부 *파일*(`LEASES_FILE` = `.local/worktree-leases.json`·worktree_pool 과
    같은 위치)을 stdlib json 으로 직접 read 한다 — areas.md 를 읽듯 데이터-결합만(모듈 결합
    아님). 리스는 worktree_pool 이 atomic-replace(`os.replace`)로 쓰므로 **락 없는
    point-read 가 일관 스냅샷**을 본다(쓰기 경합과 분리 — 부분쓰기 장부를 못 본다).

    활성 슬롯 = (`session` 인자 또는 `session_name()`) == lease.session && state=="leased" 인
    첫 행 — `session` 명시는 M>1 슬롯 순회(ADR-0040 D2·`_regression_multi_*`)가 슬롯별 test_cmd
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
        # `state: "leased"`)에 None==None 으로 false-match 하지 않게 한다(ADR-0040 reviewer).
        if sess and row.get("session") == sess and row.get("state") == "leased":
            cmd = row.get("test_cmd")
            return cmd or None   # 빈/None → None (이 활성 슬롯엔 바인딩 없음·다음 레이어로)
    return None


def _active_slot_path(session: str | None = None) -> str | None:
    """활성 worktree 슬롯(lease)의 절대 경로 (T-0122·ADR-0026·없으면 None).

    분리된 PM 홈(코드 없음)+worktree 모델([[ADR-0026]])에서 회귀는 활성 repo 의
    worktree cwd 에서 돌아야 한다 — 이 함수가 그 경로를 lease 장부에서 해소한다.

    `_active_slot_test_cmd` 와 *동형* 데이터-결합: **worktree_pool 을 import 하지 않고**
    (ADR-0013 isolation) 리스 장부 파일(`LEASES_FILE`)을 stdlib json 으로 직접 read 한다.
    slot 식별자는 `work/` 접두를 이미 포함(`work/<repo>_<N>`)하므로 worktree_pool 의
    `slot_path()`(= `REPO / slot`)와 동일하게 board 가 import 없이 `REPO / lease["slot"]` 로 직접 구성한다.
    리스는 worktree_pool 이 atomic-replace 로 쓰므로 락 없는 point-read 가 일관 스냅샷을 본다.

    활성 슬롯 = (`session` 인자 또는 `session_name()`) == lease.session && state=="leased" 인
    첫 행 — `session` 명시는 M>1 슬롯 순회(ADR-0040 D2)가 슬롯별 cwd 를 뽑을 때 쓴다. 그 행의
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
        # `and sess` 방어가드 (ADR-0040 reviewer) — sess None 시 손상 행 false-match 방지.
        if sess and row.get("session") == sess and row.get("state") == "leased":
            slot = row.get("slot")
            if not slot:
                return None  # 빈/None slot → None (다음 레이어[REPO]로)
            return str(REPO / slot)
    return None


def _test_cmd(override: str | None, session: str | None = None) -> str:
    """회귀에 쓸 테스트 명령을 해소한다 (ADR-0014 per-repo + T-0066 per-slot).

    우선순위:
      1. `override` (CLI `--cmd`).
      2. **활성 슬롯(lease)의 test_cmd** — worktree 리스 장부에서 이 세션의 leased 슬롯에
         바인딩된 명령(`_active_slot_test_cmd(session)`). 같은 repo 슬롯별 빌드변형을 수용한다
         (T-0066·ADR-0014 amend). `session` 명시는 M>1 슬롯 순회(ADR-0040 D2)가 슬롯별로
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
    """회귀를 실행할 작업 디렉토리를 해소한다 (ADR-0014 cwd seam).

    multi-PM 모델에선 코드가 활성 repo 의 **worktree** 에 있고 multi-PM 루트(`REPO`)엔 코드/테스트가
    없다 — 회귀는 worktree cwd 에서 돌아야 한다(spike §8-4 c·[[ADR-0026]] 홈+worktree 표준).
    이 함수는 그 cwd 를 주입 가능한 seam 으로 노출한다.

    해소 순서 (T-0058 seam → T-0122 주입 완성 → T-0298 모호 fail-loud):
      - `override`(CLI `--cwd`·미래 호출자가 worktree 경로를 넘김) 가 있으면 그것,
      - 없으면 **활성 슬롯 경로**(`_active_slot_path(session)` — lease 장부에서 이 세션의 leased
        슬롯 worktree 경로·worktree_pool 미import·`session` 명시는 M>1 슬롯 순회용·ADR-0040 D2),
      - 활성 슬롯이 미해소인데 **leased ≥2·세션/cwd 미지정**(진짜 모호)이면 `REPO` 침묵 폴백
        대신 **fail-loud**(`sys.exit`) — `--repo <repo> --slot <N>`/`--cwd <path>` 명시를 안내한다.
        REPO(PM 홈·`tests/` 없음)로 조용히 폴백하면 livegate/회귀가 broken slot 을 수집해
        false fail 을 내던 것(livegate `--cwd` 우회의 근원·PM 61+62 이월)을 근절한다 —
        session_name 의 귀속-쓰기 fail-loud·T-0201(bare slot 입구 거부)·T-0220(rc5 vacuous-pass
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
# T-0207 상향(20/10→30/20): 잔여 10% 정지는 rich 핸드오프 돌릴 컨텍스트가 아슬(PM 47 실측).
# 어댑터 사본(claude ctx_guard.py·opencode ctx-guard.js)과 미러(test_ctx_default_mirror 가드).
CTX_NUDGE_PCT_DEFAULT = 30  # 잔여 ≤ 이 % → "곧 정지" nudge (아직 일은 계속).
CTX_STOP_PCT_DEFAULT = 20   # 잔여 ≤ 이 % → 정지·핸드오프 트리거 임계.
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


def _regression_rc5_note(rc: int, cwd: str, override: str | None) -> str:
    """rc5(pytest 수집 0 · "no tests ran") 진단 힌트를 만든다 (rc≠5 면 '').

    "no tests ran"(exit 5)은 pass 로 기록하지 않는다(T-0220) — 수집 0 은 테스트 루트/cwd 가
    어긋났다는 신호지 green 이 아니다(T-0190 pin 가드의 "수집 N 확인" 원칙을 board 회귀
    채널로 확장). 나아가 lease/세션 미매칭으로 cwd 가 REPO 로 폴백했고 그 REPO 에 `tests/` 가
    없으면(② PM 홈이 worktree cwd 를 못 가리키는 형상·T-0124) 세션 해소 경로를 시끄럽게
    표면화한다 — 훅 env 에 세션 정체성이 없어 상시 vacuous green 을 만들던 침묵 폴백
    (PM 49차 실증)을 근절한다. `override`(명시 `--cwd`)면 폴백이 아니므로 힌트를 붙이지 않는다.
    """
    if rc != 5:
        return ""
    note = " · 수집 0 — 테스트 루트/cwd 확인"
    fell_back_to_repo = not override and cwd == str(REPO)
    if fell_back_to_repo and not (Path(cwd) / "tests").is_dir():
        note += (f" · 활성 slot lease 미매칭(session=`{session_name() or '(비바인딩)'}`) — "
                 "`PM_SESSION_NAME` 또는 local.conf `session=` 확인")
    return note


# ── M>1 회귀 슬롯 해소 (ADR-0040 D2·b-1) ────────────────────────────────────
# 훅은 `--repo/--slot` 을 못 넘긴다(pre-push 훅 스크립트 무변경·check||run 체인). 명시/env/단일-lease
# 는 그 슬롯(현행 결과 동일)이지만, 활성 슬롯이 여럿(leased ≥2·무명시)이면 **어느 세션이 push 하든**
# 전 leased 슬롯이 green 이어야 한다 — check-first(저비용·기록 baseline)로 이미 green 인 슬롯의
# pytest 재실행을 억제하고 stale/red 만 run, 하나라도 red 면 push 차단(ADR-0039 보호훅 all-or-
# nothing 철학과 동형). 슬롯별 회귀 플래그를 분리해(같은 `.local/` 공유) 결과가 서로 덮이지 않게 한다.

def _regression_flag_for(session: str | None) -> Path:
    """세션(슬롯)별 회귀 플래그 경로 — M>1 all-or-nothing 순회용 (ADR-0040 D2·b-1).

    `session` None(솔로·단일-lease·현행 단일-슬롯) → 공유 `REGRESSION_FLAG`(무변경·후방호환).
    지정(M>1 슬롯 순회) → `regression-<slug>.json` 로 슬롯별 분리 — 여러 slot 이 같은 `.local/`
    을 공유하므로 세션명 슬러그를 파일명에 담아 슬롯 결과가 서로 덮이지 않게 한다.
    """
    if not session:
        return REGRESSION_FLAG
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", session)
    return LOCAL_DIR / f"regression-{slug}.json"


def _regression_slot_state(session: str, cwd: str) -> tuple[str, int | None]:
    """슬롯별 회귀 플래그를 읽어 상태를 판정 — (`'green'|'stale'|'red'|'missing'`, rc).

    all-or-nothing check-first 의 저비용 판정 (pytest 미실행): per-slot 플래그
    (`_regression_flag_for(session)`)를 읽고 그 슬롯 worktree HEAD(`_git_head_at(cwd)`·각 슬롯은
    독립 worktree·독립 commit)와 대조한다. green(HEAD 일치·pass)이면 재실행 skip, 그 외
    (stale=HEAD 불일치·red=fail·missing=기록없음/손상)는 run 대상. 손상 플래그는 missing 강등
    (fail-soft — 장부 손상이 회귀해소를 깨면 안 된다·`_regression_slot_state` 는 재실행을 유도).
    """
    flag = _regression_flag_for(session)
    if not flag.exists():
        return ("missing", None)
    try:
        data = json.loads(flag.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ("missing", None)
    if not isinstance(data, dict):
        return ("missing", None)
    if data.get("head") != _git_head_at(cwd):
        return ("stale", data.get("rc"))
    if data.get("status") != "pass":
        return ("red", data.get("rc"))
    return ("green", data.get("rc"))


def _regression_run_slot(args: argparse.Namespace, session: str, cwd: str) -> int:
    """한 슬롯의 회귀를 pytest 로 측정·기록(슬롯별 플래그) — rc 반환 (ADR-0040 D2).

    단일-슬롯 run 본체와 동형(같은 env·shell·인코딩·rc0 만 pass·rc5 vacuous 근절)이되,
    cwd/test_cmd/플래그/HEAD 를 슬롯별로 해소한다. 스코프(touches)는 훅 M>1 경로엔 없으므로
    full 만(플래그는 push 게이트 대상). 플래그 키는 그 슬롯 worktree HEAD 다(`_git_head_at`).
    """
    cmd = " ".join(p for p in (_test_cmd(args.cmd, session=session),
                               _quarantine_args()) if p)
    print(f"regression[{session}]: $ {cmd}  (cwd={cwd})")
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    rc = subprocess.run(cmd, shell=True, cwd=cwd, env=env).returncode
    status = "pass" if rc == 0 else "fail"
    head = _git_head_at(cwd)
    note = " · 수집 0 — 테스트 루트/cwd 확인" if rc == 5 else ""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(_regression_flag_for(session),
                       {"head": head, "status": status, "rc": rc, "scope": "full",
                        "session": session, "ts": now_utc()})
    print(f"regression[{session}]: {status} (rc={rc}{note}) @ {head[:8] or '?'}")
    return rc


def _slot_state_label(session: str, state: str, rc: int | None) -> str:
    """미검증 슬롯 라벨 — `<session>=<state>(rc=N·수집0)` (rc None 이면 상태만·codex sug).

    check 실패 메시지에 슬롯별 rc 를 실어 단일-슬롯 진단과 균질화한다 — 특히 rc5(수집 0)는
    테스트 루트/cwd 결함 신호이므로 힌트를 붙인다(`_regression_slot_state` 가 이미 rc 반환·저비용).
    """
    if rc is None:
        return f"{session}={state}"
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
        state, rc = _regression_slot_state(session, cwd)
        if state != "green":
            not_green.append(_slot_state_label(session, state, rc))
    if not_green:
        print(f"regression(M={len(sessions)}): 미검증 슬롯 [{', '.join(not_green)}] "
              "— `regression run` 필요 (push 차단).", file=sys.stderr)
        return 1
    print(f"regression(M={len(sessions)}): 전 슬롯 green ✓ [{', '.join(sessions)}]")
    return 0


def _regression_multi_run(args: argparse.Namespace, sessions: list[str]) -> int:
    """M>1 run — 슬롯별 check-first(green skip)·stale/red 만 pytest·하나라도 red 면 rc1.

    all-or-nothing (ADR-0040 D2·b-1·ADR-0039 보호훅 철학과 동형): 어느 세션이 push 하든 전
    leased 슬롯이 green 이어야 통과. check-first 로 이미 green(기록 baseline·HEAD 일치)인 슬롯의
    pytest 재실행을 억제(비용 억제)하고, 실패 슬롯(red)을 종합 메시지에 명시한다(디버깅 동선).
    """
    skipped: list[str] = []
    ran: list[str] = []
    red: list[str] = []
    for session in sessions:
        cwd = _active_slot_path(session) or str(REPO)
        state, _rc = _regression_slot_state(session, cwd)
        if state == "green":
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

    M>1(leased ≥2) 홈은 전 leased 슬롯 all-or-nothing 으로 해소한다 (ADR-0040 D2·b-1): 훅은
    --repo/--slot 을 못 넘기므로 활성 슬롯이 여럿이면 슬롯별 check-first(저비용) 후 stale/red 만
    run 하며 하나라도 red 면 push 를 차단한다 (ADR-0039 보호훅 all-or-nothing 철학과 동형).

    **보호 게이트는 ambient env(PM_SESSION_NAME/CLAUDE_SESSION_NAME)로 좁혀지면 안 된다**
    (codex): 훅 프로세스가 env 세션을 상속하면 "어느 세션이 push 하든 전 leased 슬롯 green"이
    조용히 자기 슬롯 단일 경로로 우회된다. 그래서 M>1 디스패치 판정은 **CLI `--repo`/`--slot` 명시**
    (문서화된 의도적 조작·ADR-0057)만 단일-슬롯으로 좁히고 env 는 이 판정에서 제외한다 — 단일-lease/
    솔로/명시는 현행 결과 동일. (env 는 단일-슬롯 threading 등 다른 해소엔 그대로 유효.)
    """
    # task-mode(`--task`) 실행 위치 F6 해소(spike §3b F6·⑦) — 특정 슬롯 worktree 절대경로를 cwd
    # 로 고정하고 슬롯 test_cmd 를 실어 잘못된 형제-슬롯 유도를 피한다(F6 이 슬롯 확정). 절대경로를
    # surface 해 dev/git 짐작 여지를 없앤다(cwd 비참여·T-0345 불변). run 만 실행 위치가 필요하다.
    if args.action == "run" and getattr(args, "task", None):
        task_cwd, task_test_cmd = _resolve_task_workspace_cwd(args)
        if getattr(args, "cwd", None) is None:
            args.cwd = task_cwd
        if getattr(args, "cmd", None) is None:
            # F6 이 슬롯을 **확정**했으므로 그 슬롯 test_cmd 를 직접 싣는다 — session 유도
            # (`_active_slot_test_cmd(<task>)`)는 같은 task 의 **형제 슬롯 첫-매칭**을 반환해 오매칭
            # 여지가 있다(reviewer). 미바인딩(None)이면 슬롯 레이어를 건너뛰고 repo/local 폴백
            # (`session=None`)으로 — 형제 슬롯 test_cmd 를 타지 않는다(결정론).
            args.cmd = task_test_cmd or _test_cmd(None, session=None)
        print(f"regression: 작업공간(task {args.task}) → {task_cwd}")
    # 디스패치 판정은 CLI --repo/--slot(명시) 만 본다 — env 세션은 M>1 게이트를 조용히 좁히므로
    # 여기선 제외(위 docstring·codex). explicit 없고 leased ≥2 면 env 유무 무관 전-슬롯 순회.
    explicit_override = _actor_session_override(args)
    if explicit_override is None:
        leased = identity_args.leased_sessions(LEASES_FILE)
        if len(leased) >= 2:
            slots = sorted(set(leased))
            return (_regression_multi_run(args, slots) if args.action == "run"
                    else _regression_multi_check(slots))
    # 단일-슬롯 (명시 --repo/--slot·단일-lease·솔로·env<M2) — 현행 경로. sess 는 슬롯 test_cmd/cwd
    # threading 용(env 유효·위에서 M>1 만 걸러냄).
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
        # cwd seam (ADR-0014) — multi-PM 모델은 활성 repo 의 worktree 에서 돌아야 한다.
        # `--cwd` 주입(미래·T-0060 bootstrap) 시 그 경로, 미주입(솔로/multi-PM-미배선)은 REPO.
        cwd = _regression_cwd(getattr(args, "cwd", None), session=sess)
        rc = subprocess.run(cmd, shell=True, cwd=cwd, env=env).returncode
        # pass = rc0 한정. pytest rc5(수집 0·"no tests ran")는 fail — 수집 0 은 green 이
        # 아니라 테스트 루트/cwd 결함이다(T-0220). 이전엔 rc5 를 pass 로 삼켜 훅 세션
        # 미해소 시 상시 vacuous green 이었다(PM 49차).
        status = "pass" if rc == 0 else "fail"
        detail = f"{status} (rc={rc}{_regression_rc5_note(rc, cwd, getattr(args, 'cwd', None))})"
        if scoped:
            # 스코프 실행 = dev 빠른 피드백 (advisory). full 만 push 게이트 → 게이트 플래그 안 씀.
            print(f"regression(scoped, {len(touches)} touches): {detail} "
                  "— dev 피드백 · push 게이트 아님")
            return 0 if status == "pass" else 1
        LOCAL_DIR.mkdir(parents=True, exist_ok=True)
        REGRESSION_FLAG.write_text(json.dumps(
            {"head": _git_head(), "status": status, "rc": rc, "scope": "full",
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
        # rc5(수집 0)는 fail 로 기록된다(T-0220) — RED 사유를 push 게이트에서 드러내
        # "테스트가 안 돌았는데 green" 이던 침묵 폴백을 진단 가능하게 한다(run/check 일관).
        extra = " · 수집 0 — 테스트 루트/cwd 확인" if rc == 5 else ""
        print(f"regression: RED @ {head[:8]} (rc={rc}){extra} — push 차단.",
              file=sys.stderr)
        return 1
    print(f"regression: green @ {head[:8]} ✓")
    return 0


# ── 릴리즈 라이브 게이트 (ADR-0039) ────────────────────────────────────────
# 라이브 LLM 검증(실 하네스 smoke)을 릴리즈(① main 머지) 단일 지점으로 모은 게이트.
# `livegate record` 가 `pytest -m release` 를 회귀와 동일한 cwd 해소로 실행·측정하고
# (실행=기록·손기록 없음), 보호훅이 `livegate check --rev <sha>` 로 push HEAD 가 green 인지
# 소비한다. false-green 방어를 위해 rc0 만으로는 부족하고 수집 N==pin 을 함께 요구한다
# (T-0190 수집 pin·T-0220 rc5 vacuous-pass 근절의 원칙을 라이브 채널로 확장).
LIVEGATE_RELEASE_PIN = 14  # `pytest -m release` 로 돌아야 하는 라이브 케이스 수 (단일 진실·T-0278 worktree 라이브 +2·T-0309 멀티유저 composite +1·T-0349 pm-release 라이브 +2).
                           # tests/test_release_wave.py `_EXPECTED_RELEASE_TESTS` 와 값 공유.
LIVEGATE_TEST_CMD = "pytest -m release -q"   # 라이브 릴리즈 wave selection.


def _livegate_ran_count(output: str) -> int:
    """`pytest -m release -q` 요약행에서 *실제 실행된* release 테스트 수(수집 N)를 센다.

    N = passed + failed + error(s). deselected 는 release 마커 밖이라 세지 않는다. "no tests
    ran"(exit5)처럼 요약에 카운트가 없으면 0. 이 수집 N 을 pin 과 대조해 마커 소실·wrong-cwd
    로 인한 false-green(수집 위장)을 차단한다 — rc0 만으로는 "0개 수집됐지만 red 아님"을
    green 으로 삼킬 수 있다(T-0190 수집 pin·T-0220 vacuous-pass 근절의 라이브 확장).
    """
    total = 0
    for kind in ("passed", "failed", "errors?"):
        m = re.search(rf"(\d+) {kind}\b", output)
        if m:
            total += int(m.group(1))
    return total


def _write_json_atomic(path: Path, data: dict) -> None:
    """dict → JSON 을 temp + `os.replace` 로 원자 교체한다 (crash 시 잔재/부분기록 방지).

    `dump_ticket_atomic` 과 동형 — 같은 디렉토리 안 tmp 에 전체를 쓰고 atomic rename.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(str(tmp), str(path))


# ── livegate 기록 위치 = 훅 read 위치 정렬 (T-0287·two-git 단일 소스) ─────────────
# `livegate record` 는 push 보호훅이 읽는 **바로 그** livegate.json 에 기록해야 한다. 훅은 repo 의
# git `core.hooksPath` 에 설치돼(worktree_pool.install_protected_hook), 같은 디렉토리의 sidecar
# `engine-root`(PM 홈 REPO 절대경로 1줄)로 `<engine-root>/.project_manager/tools/board.py` 를 해소하고
# 그 사본의 `.local/livegate.json` 을 `livegate check` 로 읽는다(worktree_pool `_PROTECTED_PRE_PUSH_HOOK`).
# two-git 토폴로지(PM 홈+worktree·ADR-0027)에서 record 를 호출된 사본의 `REPO/.local` 에 그냥 쓰면,
# worktree board.py 로 record 할 때 훅이 안 읽는 worktree `.local` 에 조용히 기록→pass 위장→push
# 순간에야 불일치로 드러난다(PM 60 v1.1.0 릴리즈 실측). 그래서 record 도 훅과 **동일한 engine-root
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
    """record 가 기록할 livegate.json 경로 = **push 보호훅이 읽는 파일**로 정렬한다 (T-0287).

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
    # 프레임워크 설치는 절대경로라 평시 무영향이나, 수동/상대 설정 방어로 git 시맨틱을 미러(T-0287).
    hp = Path(hooks_path)
    if not hp.is_absolute():
        hp = Path(cwd) / hp
    sidecar = hp / "engine-root"
    if not sidecar.is_file():
        # `core.hooksPath` 는 있으나 engine-root sidecar 부재 = livegate 보호훅 아님(예: PM 홈
        # 자신의 R8 회귀 훅·채택자 custom 훅) → 솔로 폴백(오탐 fail-loud 방지).
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
    `fail` 기록 + rc1(사유 표면화). 손기록 경로는 없다(위조/착오 차단·ADR-0039 D2). 기록 위치는
    push 보호훅 read 위치(`_resolve_livegate_flag` — engine-root sidecar)와 정렬해 단일 소스다
    (worktree/PM 홈 어느 board.py 로 돌려도 훅이 읽는 한 파일·T-0287).

    `--repo`/`--slot`(ADR-0057)은 regression/handoff 과 동형으로 `session_name` 해소를 거쳐
    `_regression_cwd` 에 thread 한다 (M>1 홈에서 슬롯 cwd 를 명시 — `--cwd` 절대경로 핀 우회
    불요·T-0298). 무명시 + leased ≥2 는 seam 이 fail-loud(모호는 시끄럽게) 하며, 그 메시지가
    안내하는 `--repo <repo> --slot <N>` 이 실제로 이 subparser 에서 수용돼 dead-end 가 아니다
    (remedy 정직·T-0285 anti-pattern 회피).
    """
    # task-mode(`--task`) 실행 위치 F6 해소(spike §3b F6·⑦) — 특정 슬롯 worktree 절대경로를 cwd 로
    # 고정하고 surface 한다(cwd 비참여·T-0345 불변·livegate 는 고정 release cmd 라 test_cmd 는 불요).
    if getattr(args, "task", None):
        task_cwd, _ = _resolve_task_workspace_cwd(args)
        if getattr(args, "cwd", None) is None:
            args.cwd = task_cwd
        print(f"livegate: 작업공간(task {args.task}) → {task_cwd}")
    cwd = _regression_cwd(getattr(args, "cwd", None),
                          session=session_name(_actor_session_override(args)))
    # 기록 위치를 push 보호훅 read 위치와 정렬(단일 소스·T-0287) — **실행 전에** 해소한다. 훅과 같은
    # engine-root sidecar 해소를 공유해, worktree board.py·PM 홈 board.py 어느 쪽으로 돌려도 훅이
    # 읽는 한 파일에 기록. engine-root 무효(BROKEN)는 실행 전에 알 수 있으니, 값비싼 `pytest -m
    # release`(라이브 wave)를 헛돌리기 전에 fail-loud 로 조기 거부한다(T-0287 리뷰 should-fix).
    flag, mode = _resolve_livegate_flag(cwd)
    if mode == _LG_BROKEN:
        # 보호훅은 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소)
        # → 기록 위치와 훅 read 위치가 갈릴 수 있어 pass 위장을 찍지 않고 fail-loud 거부한다
        # (false-green 백스톱·ADR-0039). engine-root sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단·T-0287). engine-root sidecar 수리 또는 "
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
    를 거친다(단일 소스·T-0306). 모듈상수 `LIVEGATE_FLAG` 직독이 아니라, 어느 board.py 사본/cwd 로
    check 하든 push 보호훅이 기록한 **바로 그** 파일을 읽어 stale/wrong-copy 오독(false-green/
    false-red)을 원천 차단한다 — record(`_livegate_record`)의 기록-위치 정렬(T-0287)과 대칭이다.
    `cwd` = `--cwd`(record 와 대칭·미지정 시 이 board.py 사본의 `REPO` — 그 repo 의 `core.hooksPath`
    로 engine-root 해소). engine-root 무효(`_LG_BROKEN`)는 record 와 **동형** fail-loud(조용한 통과 금지).
    """
    rev = getattr(args, "rev", None)
    if not rev:
        print("livegate check: --rev <sha> 필요 (push 대상 커밋).", file=sys.stderr)
        return 1
    # 읽을 위치를 push 보호훅 read 위치와 정렬(record 와 대칭·단일 소스·T-0306/T-0287). 훅과 같은
    # engine-root sidecar 해소를 공유해, worktree board.py·PM 홈 board.py 어느 사본으로 check 해도
    # 훅이 기록한 한 파일을 읽는다. hooksPath 미설정/솔로면 현행 LIVEGATE_FLAG(REPO/.local) 폴백 무변경.
    cwd = getattr(args, "cwd", None) or str(REPO)
    flag, mode = _resolve_livegate_flag(cwd)
    if mode == _LG_BROKEN:
        # 보호훅 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소) →
        # 기록 위치와 훅 read 위치가 갈릴 수 있어 pass/fail 판정을 신뢰 못 한다. 조용한 통과 대신
        # record 와 동형 fail-loud 거부(false-green/false-red 백스톱·ADR-0039·T-0306). engine-root
        # sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단·T-0287). engine-root sidecar 수리 또는 "
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
    (`drafts_dir()`·T-0198)를 폴백으로 스캔해 pseudo-status `"draft"` 로 반환한다 — `show`/
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

    On POSIX rename(2) is atomic and a lost race surfaces as FileNotFoundError
    (the source is already gone). On Windows os.rename is NOT exclusive across
    concurrent processes (ADR-0012 Amendment · T-0213), so a caller that needs
    mutual exclusion for a *contended* transition must serialize it itself —
    `cmd_claim` wraps its load→rename in `board_lock` for exactly this reason.
    Uncontended transitions (complete/block on a ticket a single session already
    owns) never race, so this primitive stays lock-free. Generic over tickets
    and ideas.
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


# ID 스캔용 status 튜플 — STATUS_DIRS + draft 격리 디렉토리(`.drafts`·T-0198). draft 도 이미
# 발행된 ID(파일명에 박제)이므로 스캔에서 빠지면 promote 전 다음 `new` 가 같은 번호를 재사용해
# 충돌한다. `next_numeric_id` 는 `base_dir / d` 로 순회하므로 디렉토리명 그대로 넣으면 된다.
_ID_SCAN_STATUSES: tuple[str, ...] = (*STATUS_DIRS, ".drafts")


def _next_prefixed_id(prefix: str) -> str:
    """Next `T-<canonical>-NNN` for `prefix`, matched **case-insensitively** (ADR-0055).

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
    `--prefix aaa` 는 기존 `T-AAA-*` 시리즈를 이어간다(`_next_prefixed_id`·ADR-0055).
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
    # claim = 귀속 쓰기(spike §1 row a·최악 클래스 silent 오귀속) — 세션 미해소면 fail-loud
    # (ADR-0040 D1·required=True). 명시 --repo/--slot(ADR-0057) > env > 단일-lease 유도 >
    # (solo) local.conf.
    override = _actor_session_override(args)
    sess = session_name(override, required=True)
    # claimed_by 는 `<user>/<slot>` (assignee·ADR-0033 ③·T-0161) — user 미상이면 슬롯만
    # (graceful·기존 슬롯-only 값과 형태 동일). 진행메시지/board surface 는 슬롯(sess)을 쓴다.
    assignee = identity_tag(session_override=override,
                            user_override=getattr(args, "user", None))

    # claim STRICT 1단계 (ADR-0033 ②·spike §3.6): board 가 별도 git 이면 먼저 pull --rebase
    # 로 remote 선점을 로컬에 반영한다. pull 이 winner 의 claim 을 끌어오면 ticket 이
    # claimed/ 로 이동돼 아래 status 검사가 race-lost 를 표면화한다(로컬 변경 0). pull 자체가
    # 실패(offline/도달 불가)하면 claim 불가 — best-effort 로 claim 을 남기면 중복작업이라
    # claim 만 strict offline-fail 한다. orig_head = pull 직후 SHA(claim commit rollback 지점·
    # legacy/sync 비활성이면 ""). detached sentinel = board detached HEAD(checkout 안내·offline
    # 아님·T-0203). dirty sentinel = board uncommitted(commit 안내·offline 아님·T-0175). None =
    # offline. detached 를 dirty 보다 먼저 분기한다(원인 우선순위·prefetch 순서와 일치).
    orig_head = _board_git_claim_prefetch()
    if orig_head == _CLAIM_PREFETCH_DETACHED:
        # board submodule 이 detached HEAD — pull --rebase 가 거부되지만 *네트워크는 정상*이다
        # (offline 아님). detached 에선 claim 의 rollback anchor 를 확정할 수 없고 best-effort
        # sync 가 orphan 을 쌓으므로(T-0204), offline 으로 오판하지 않고 브랜치 복귀를 안내하며
        # claim 을 차단한다. 자동 checkout 은 PM 의 브랜치 의도를 침해해 위험하므로 안내만 한다.
        print(
            f"board submodule 이 detached HEAD 상태 — {args.id} claim 불가(rollback anchor 없음). "
            f"`git -C .project_manager/board checkout <branch>`(예: main) 로 브랜치에 복귀 후 "
            f"claim 재시도 (offline 아님·네트워크 정상). detached 에서 이미 쌓인 로컬 commit 이 "
            f"있으면 복귀 후 `git -C .project_manager/board cherry-pick <sha>` 로 이식.",
            file=sys.stderr)
        return 1
    if orig_head == _CLAIM_PREFETCH_DIRTY:
        # board submodule 에 uncommitted 변경(흔히 발행 직후 ticket 본문 Edit) — pull --rebase
        # 가 거부되지만 *네트워크는 정상*이다. offline 으로 오판하지 않고 commit 후 재시도를
        # 안내한다(자동 commit/stash 는 PM 편집 의도를 임의 처리해 위험·안내만·ADR-0033 ②).
        print(
            f"board submodule 에 uncommitted 변경 — "
            f"`git -C .project_manager/board add -A && git -C .project_manager/board commit` "
            f"후 {args.id} claim 재시도 (offline 아님·네트워크 정상)",
            file=sys.stderr)
        return 1
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

    # The claim critical section (load → dependency check → open→claimed rename)
    # runs under board_lock so the transition is *serialized* across sessions.
    # On POSIX the atomic rename(2) alone was exclusive; on Windows os.rename is
    # NOT exclusive across concurrent processes, so without serialization every
    # concurrent claimer could "win" and duplicate the claim (ADR-0012
    # Amendment · T-0213 — the "rename is the lock" premise is Windows-invalid).
    # Holding board_lock makes the rename effectively exclusive everywhere: once
    # the winner moves the ticket out of open/, every serialized loser's
    # load_ticket/move_ticket hits the now-missing path → FileNotFoundError. The
    # two race windows (the loser's `load_ticket(path)` read or its
    # `move_ticket(path)` rename) both mean the same thing — we lost the claim
    # race — so both surface as one clean "claim race lost" (rc=1), never an
    # unhandled traceback (ADR-0012 contract · T-0057). board_lock is OS-flock
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

            # board_lock (not the bare os.rename) is now the exclusive gate.
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
        # stdin 이 EOF (비대화·파이프 종료) — 비대화 가드와 동일 동작: 결정 미기록,
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
    # 명시 --prefix sanity (ADR-0042·예약어 `none`·형식 [a-z0-9_]+) — 위반이면 areas 등록·
    # local.conf write 어떤 부작용보다 앞에서 부작용 0 으로 거부(cmd_init 첫 문장 뒤).
    if prefix:
        reason = _validate_prefix(prefix)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
    namespaced = bool(prefix)  # prefix 있음 = multi-repo 네임스페이스 모드(협업 아님·ADR-0016)
    if namespaced:
        registered = registered_prefixes()
        # case-only 근접중복 검출은 등록 ∪ **티켓** prefix (ADR-0055·`_validate_dst_prefix` 와 대칭) —
        # 미등록 `T-aaa-*` 티켓이 있는데 `init --prefix AAA` 하는 case-불일치도 fail-loud 로 안내한다.
        existing = registered | _existing_ticket_prefixes()
        if prefix in registered:
            print(f"prefix {prefix!r} 이미 등록됨 (areas.md) — local.conf 만 갱신.")
        elif (conflict := _case_only_conflict(prefix, existing)) is not None:
            # case-only 중복 거부 (ADR-0055) — 이미 있는 `AAA` 와 대소문자만 다른 `aaa` 등록은
            # 레지스트리/티켓과 fold-충돌한다(네임스페이스 분할). 기존 canonical case 로 안내.
            print(f"[중단] prefix {prefix!r} 은 기존 {conflict!r} 과 대소문자만 다르다 "
                  f"(prefix 동일성은 case-insensitive·ADR-0055). 기존 case {conflict!r} 를 "
                  "그대로 쓰라 (areas 미변경·부작용 0).", file=sys.stderr)
            return 1
        else:
            if not args.area:
                print(f"새 prefix {prefix!r} 등록엔 --area <설명> 필요.", file=sys.stderr)
                return 1
            # owner = areas.md 등록 식별자(registrant) — 협업 소유자(다중-사람)가 아니라
            # single user 의 등록 출처 표식이다(ADR-0016·ADR-0002 amend). 기본 = 현 세션.
            # 등록 owner 기본값 = 귀속 쓰기(ADR-0040 D1·required=True) — 세션 미해소면 fail-loud
            # (`--owner`/`--repo`/`--slot` 명시 유도). --owner 명시면 session_name 미호출(short-circuit).
            owner = args.owner or _actor_session_override(args) or session_name(required=True)
            # area_owner = 그 area 의 *user* 소유(`--mine` 풀 입력·ADR-0033 ③·T-0161) —
            # registrant `owner`(슬롯/세션)와 별개 칼럼(overload 금지·ADR-0014 refine).
            # cmd_repo_add 와 동형 해소: `--user` 명시 > local.conf user= > git config
            # user.email > None(빈 칼럼·_repo_area_owner None 폴백·현행 `--mine` 미포함).
            area_owner = user_name(getattr(args, "user", None))
            areas_append(prefix, args.area, owner, area_owner=area_owner)
            ao_surface = area_owner if area_owner else "(미상 — local.conf user= / git user.email 미설정)"
            print(f"✓ areas.md 등록: {prefix} | {args.area} | owner={owner} | area_owner={ao_surface}")
    # session=/prefix= write 는 **solo 형상 전용 legacy** (ADR-0040 D4) — leased ≥2 인
    # multi 홈은 이 키를 무시하고 세션/prefix 를 lease 장부에서 유도한다(session_name·
    # id_prefix). solo 채택자 폴백(후방호환)을 위해 write 는 유지하되, multi 홈은 흡수 후
    # 이 키를 제거해도 동작 동일(위생).
    # --repo/--slot(ADR-0057) 로 명시하면 "<repo>_<N>" 으로 완전 해소(리스 조회 불요·kind=slot).
    # --repo 단독이면 그 repo 의 활성 리스가 1개일 때만 해소(0개/무인자 → None → 아래 default).
    override = _actor_session_override(args)
    sess = override or (f"{prefix.lower()}-pm" if namespaced else "pm")
    if not LOCAL_CONF.exists():
        # 부재 시(첫 생성) — 현행 그대로 전체 default conf write. 회귀 0.
        conf = "# per-clone 설정 (git-ignored). board.py init 생성. clone 마다 다름.\n"
        if namespaced:
            conf += f"prefix={prefix}\n"  # solo-legacy — multi 홈은 areas.md 유도(ADR-0040)
        conf += (f"session={sess}\n"  # solo-legacy — multi 홈은 lease 유도(ADR-0040)
                 "# 엔진 문서 operational placeholder 해소값 ({{PY}}·{{TEST_CMD}}·{{PROJECT_NAME}}):\n"
                 f"py={_detect_py()}\ntest_cmd=pytest -q\nproject_name=\n"
                 "# ctx 정지-핸드오프 임계 (어댑터 훅이 잔여 컨텍스트 %로 판정 — T-0013):\n"
                 f"ctx_nudge_pct={CTX_NUDGE_PCT_DEFAULT}\nctx_stop_pct={CTX_STOP_PCT_DEFAULT}\n"
                 "# ctx_window_tokens: 핸드오프 토큰 예산(위 nudge/stop %의 기준). 큰 window(1M)\n"
                 "# 모델이라도 낮게 두면 이른 핸드오프 = 토큰 경제(큰 컨텍스트가 매 턴 소모 가속).\n"
                 "# 올리면 세션당 더 길게. 물리 window 아님 — 사용자 비용/맥락 선택.\n"
                 f"ctx_window_tokens={CTX_WINDOW_TOKENS_DEFAULT}\n"
                 "# 하네스별 오버라이드(옵션·주석 해제 시 활성): 한 repo 를 claude·opencode 동시\n"
                 "# 운용 시 하네스별 예산 분리(ADR-0041 — ctx_window_tokens_<harness> > generic > 200K·\n"
                 "# 물리한도 개념 폐기·claude/opencode 독립). 미설정 시 위 generic 값이 분모.\n"
                 "# ctx_window_tokens_claude=500000\n"
                 "# ctx_window_tokens_opencode=200000\n")
        LOCAL_CONF.write_text(conf, encoding="utf-8")
        surface_sess = sess
    else:
        # 존재 시 — 비파괴 병합(T-0184). init 이 안 쓰는 사용자/operational 키
        # (external_review_enabled·reviewer_cmd·upstream·upstream_rev·opencode_pro_model·
        # status_total_style·user 등)를 절대 삭제/변경하지 않는다. 통째 write 금지.
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
        # multi 홈은 유도로 무시 — ADR-0040 D4.)
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
        LOCAL_CONF.write_text(merged, encoding="utf-8")
        surface_sess = existing.get("session") or sess
        if override:
            surface_sess = override
    print(f"✓ local.conf: {('prefix=' + prefix + ' · ') if namespaced else ''}session={surface_sess}")
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

    `--repo`/`--slot`(ADR-0057)은 출력·기본 identity 표시용이며 **backfill 대상 슬롯을 바꾸지
    않는다**. 슬롯-only `claimed_by`(`pm-1` 같은 `/` 없는 값)는 user 차원만 prepend 하고 *기존
    슬롯 토큰을 보존*한다(`pm-1` → `<user>/pm-1`). `--repo`/`--slot` 은 부재 created_by 의
    표시값과 로그 표기에만 쓰이고, 이미 기록된 슬롯 토큰을 자신의 값으로 덮어쓰지 않는다(비파괴).

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
    # backfill slot = 귀속 쓰기(claimed_by 에 박음·ADR-0040 D1·required=True) — 세션 미해소면
    # fail-loud(None 슬롯으로 backfill 하면 오귀속). 명시 --repo/--slot(ADR-0057) > env >
    # 단일-lease 유도.
    slot = session_name(_actor_session_override(args), required=True)
    dry_run = bool(getattr(args, "dry_run", False))
    scope = getattr(args, "scope", "all") or "all"
    statuses = ("open", "claimed") if scope == "active" else STATUS_DIRS

    tag = "[dry-run] " if dry_run else ""
    # scope 문구 정합(ADR-0056·T-0312): migrate 는 소유 무관 *전 티켓*을 스캔·backfill 한다
    # (all=done 포함 전체·active=open+claimed). `list --mine`/`--repo`/`--slot`(=현재-사용자 ∩
    # 슬롯 뷰)와 스캔 대상이 다르므로 "전체 스캔" 을 명시해 두 기준이 어긋나 보이는 오독(S2)을 없앤다.
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
    # 명시 --prefix sanity (ADR-0042·예약어 `none`·형식 [a-z0-9_]+) — 위반이면 ID 스캔·파일
    # 발행 전에 부작용 0 으로 즉시 거부. 유도/count-based 로 해소된 legacy prefix 는 검증하지
    # 않는다(기존 발행분 존중) — 사용자가 *명시*한 override 만 입력측 sanity 대상이다.
    # task-mode(`--task`)는 정체성 깔때기(`_actor_session_override`)를 지나 **task 명 검증**(무검증
    # created_by 영속 차단·must-fix·T-0355 게이트)을 받고 세션 override(=task 이름·F5b)를 해소한다 —
    # cmd_new 는 구조상 이 깔때기를 안 지나므로 여기서 명시 소비해 소비 지점 폐쇄에 합류한다. 무-task 는
    # None(기존 created_by 체인 무변경). 부작용(ID 발행·파일 write) 이전이라 불법 task 는 여기서 fail-loud.
    task_session = _actor_session_override(args)
    override = getattr(args, "prefix", None)
    if override:
        reason = _validate_prefix(override)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
    elif task_session:
        # F5 우선순위: --prefix 명시 > task 설정 prefix > 유도 체인. task 설정값은 `task prefix`
        # (T-0357)가 검증하고 기록하므로 여기선 신뢰(입력측 sanity 재검증 불요·기본 None=무prefix).
        task_pfx = identity_args.task_prefix(task_session, LEASES_FILE)
        if task_pfx:
            override = task_pfx
    prefix = id_prefix(override)
    # multi-repo 네임스페이스 가드는 **레지스트리 *존재*가 아니라 등록 repo *개수*** 기준이다.
    # 등록 prefix 가 ≥2 면 진짜 ID 충돌 가능성이 있으니 prefix 필수(namespace 강제). 등록이
    # ≤1(0=레지스트리 부재/빈·1=단일 self-host) 이면 충돌이 없으므로 solo legacy `T-NNNN` 을
    # 허용한다(prefix optional) — 단일 self-host 가 areas.md 1행만으로 multi-PM 마찰을 떠안지
    # 않게(ADR-0027 분리 후 단일 등록 repo 케이스). 명시 prefix 가 *주어지면* 그건 그대로
    # 존중해 prefixed ID 를 발행한다 — ≤1 라도 사용자가 골랐으면 따른다.
    #
    # **명시 prefix 의 "등록돼 있어야 한다" 가드는 없다** (ADR-0042 §3.1 자유 입력·"등록 제약
    # 없음"). prefix 는 이제 repo 네임스페이스가 아니라 작업 카테고리 — 새 카테고리를 즉석에서
    # 붙일 수 있어야 한다. 입력측 sanity 는 위 `_validate_prefix`(예약어+형식)만으로 끝난다.
    # 아래 ≥2 가드는 별개 — 등록 repo 가 ≥2 인데 prefix 를 *안* 준 implicit 모호성 방지다
    # (ADR-0040 D3 유도 체인·미해소 fail-loud).
    registered = registered_prefixes()
    if len(registered) >= 2:
        if not prefix:
            print("multi-repo 네임스페이스 모드(등록 repo ≥2) — prefix 필요(미해소·ADR-0040 D3). "
                  "`--prefix <PFX>` 로 명시하거나 세션을 바인딩하라"
                  "(`PM_SESSION_NAME=<repo>_<N>` env 또는 단일 활성 슬롯 lease → areas.md "
                  "repo→prefix 유도). 미등록이면 먼저 `board.py init --prefix <PFX> --area <name>`.",
                  file=sys.stderr)
            return 1

    tmpl_fm, tmpl_body = load_ticket(template_file())

    # 발행 규율 게이트(T-0196·T-0198): board-git 이 공유(별도 git·submodule) 상태일 때만
    # 의미가 있다 — 미충전 stub 이 board-git 에 커밋돼 다른 slot 의 handoff/bootstrap 을
    # 오염시키는 게 문제(T-0191/T-0192 실패)이므로, board 가 별도 git 이 아니면(legacy·솔로)
    # 게이트 없이 기존처럼 즉시 open/ 에 발행한다. 별도 git 이면 본문을 *쓰기 전에* 미리
    # 검사(`_body_lint_issues` — `lint_bodies` 와 동일 로직)해 placeholder/thin 이 남아있으면
    # `open/` 이 아니라 `drafts_dir()`(STATUS_DIRS 밖·T-0198)에 쓴다 — draft 가 STATUS_DIRS
    # 순회 대상이 되는 창(open/ 에 잠깐이라도 존재)이 아예 없어야, 이후의 **어떤** mutation
    # (자기 자신의 board-git sync 뿐 아니라 무관한 claim/complete/promote 등)도 draft 를
    # board-git 에 잘못 쓸어담을 수 없다(T-0196 이 자기 sync 만 skip 하고 후속 mutation 의
    # `git add -A` 가 leak 시키던 결함의 재발 방지 — board-git 에 아예 안 보이는 게 핵심).
    # `list`/`show`/`promote` 는 `find_ticket`/`drafts_dir()` 로 draft 를 계속 인지한다.
    #
    # ID 발행(`_next_id` = max+1·동시 발행 race)과 파일 생성을 단일 락으로 직렬화한다
    # (ADR-0012). 락 안에서 ID 를 *읽고* 곧바로 파일을 만들어, 다른 세션이 같은 ID 를
    # 발행할 틈을 없앤다. board.md 재생성은 락 밖(별도 트랜잭션 — 파생물).
    with board_lock():
        tid = _next_id(prefix)
        slug = _slugify(args.title)
        filename = f"{tid}-{slug}.md"

        # Replace placeholder tokens in body
        body = tmpl_body.replace("T-NNNN", tid).replace("<제목>", args.title)
        # lint 판정은 `tid` 치환과 무관(placeholder/section 검사가 `T-NNNN` 자체를 안 봄) —
        # 발행 전에 판정해 쓰기 경로(open/ vs drafts_dir())를 정한다(open/ 창 노출 0).
        is_draft = _board_git_enabled() and bool(_body_lint_issues(tid, body))

        fm: dict[str, Any] = dict(tmpl_fm)
        fm["id"] = tid
        fm["title"] = args.title
        fm["status"] = "draft" if is_draft else "open"
        fm["created"] = datetime.date.today().isoformat()
        # created_by = `<user>/<pm-slot>` (provenance·불변·생성 시 set·ADR-0033 ③·T-0161).
        # "누가 추가했나" = 중복-작업 방지의 출처 표식. user 미상이면 슬롯만(graceful).
        fm["created_by"] = identity_tag(
            # task-mode(`--task`)면 created_by = <user>/<task>(F5b 귀속 축·provenance·깔때기 검증 통과분).
            # 무-task 는 종전대로 session_override=None → identity_tag 내부 체인 해소(ADR-0057·거동 무변경).
            session_override=(task_session or None),
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
        print(f"  ⚠ draft — board-git 미커밋(공유 board 오염 방지·T-0196/T-0198): "
              f"미충전(placeholder/thin) 본문. 본문을 채운 뒤 "
              f"`board.py promote {tid}` 로 승격(open/ 이동 + board-git 커밋)하라.",
              file=sys.stderr)
        return 0
    refresh_board()
    _board_git_sync_best_effort(f"new {tid}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """draft(drafts_dir() 격리·board-git 미커밋) 티켓을 승격 — 재검사 후 open/ 이동 + board-git sync.

    `board.py new` 가 생성 시점 게이트(T-0196·T-0198)로 `drafts_dir()`(STATUS_DIRS 밖)에 남긴
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
        remaining = _body_lint_issues(args.id, body)
        if remaining:
            print(f"promote 거부 — {args.id} 에 아직 미충전 {len(remaining)}건:",
                  file=sys.stderr)
            for tid, kind, detail in remaining:
                print(f"  ✗ [{kind}] {detail}", file=sys.stderr)
            return 1
    if status == "draft":
        # drafts_dir() → open/ 이동 — 이제서야 STATUS_DIRS 스캔·board-git 대상이 된다.
        fm["status"] = "open"
        new_path = tickets_dir() / "open" / path.name
        dump_ticket(path, fm, body)  # status 갱신을 먼저 디스크에 반영한 뒤 이동.
        os.rename(path, new_path)
        refresh_board()
    _board_git_sync_best_effort(f"promote {args.id}")
    print(f"promoted {args.id} (board-git 승격 완료)")
    return 0


# `list` 기본뷰(무-status)의 활성 상태 — done 을 접어 범람을 해소한다(T-0197). `--status all`
# 이 STATUS_DIRS 전체(done 포함)를 연다.
_LIST_ACTIVE_STATUSES: tuple[str, ...] = ("open", "claimed", "blocked")


def _tag_values(fm: dict[str, Any]) -> list[str]:
    """frontmatter 의 tags 를 전부 str 로 캐스팅한 리스트 (T-0264).

    YAML 은 `tags: [2026, cleanup]` 의 `2026` 을 int 로 로드한다 — 이를 그대로
    `str.join` 하면 TypeError 로 크래시하고(cmd_list·마크다운 렌더), 문자열 `--tag`
    와 `in` 비교하면 조용히 매치 실패한다(필터). tags 를 표시·필터하는 전 호출부가
    이 str 캐스팅을 단일 지점에서 거치게 해 두 결함을 함께 없앤다. 문자열 태그는
    str→str 로 무변경(형식·매치 회귀 없음).
    """
    return [str(t) for t in (fm.get("tags") or [])]


def cmd_list(args: argparse.Namespace) -> int:
    # `--mine`(T-0164·ADR-0033 ④) / `--repo`·`--slot`(ADR-0057 — 구 `--session`/bare `--slot`
    # 뷰 렌즈[T-0197] 를 흡수) 뷰: 단일 공유 보드의 렌즈 — **현재 사용자**의 area open + claim.
    # identity 입력(T-0161)을 한 번 해소해 행마다 재계산 안 함. 무플래그 list 는 필터 없이
    # 전체(status 셀렉터만 적용).
    #
    # user-first (ADR-0056·T-0312): 필터 뷰의 "me" 는 **항상 현재 사용자**(`user_name()` =
    # local.conf user= > git config user.email)다. `--repo`/`--slot` 의 my_user 를
    # area_owner-derived(`_area_owner_from_session`/`_area_owner_for_single_area`·T-0198/T-0302)로
    # 유도하던 배선을 폐기한다 — area_owner 미설정(흔함)이면 my_user=None 이 돼 slot-only 매칭·
    # all-open degrade 로 타 슬롯 claim·타 사용자 티켓이 유출되던 근본을 없앤다. area_owner 는
    # 이제 open-티켓 *소유* 정의(`_ticket_owner`)로만 남고, "누가 조회하는가" 와 무관하다.
    #
    # 뷰 스코프(ADR-0057 결정 3·spike §3.1 — 인자 표면만 흡수·ADR-0056 뷰 로직 자체는 불변):
    #   - `--mine` = 내 것(내 claim ∪ 내 open), **전 슬롯**(slot_scoped=False).
    #   - `--repo X --slot N`(kind=slot) = 내 것 **∩ 그 슬롯**(slot_scoped=True·완전 일치) — 옛
    #     `--session <repo>_<N>` 과 동형(`_slot_matches` mode="exact").
    #   - `--repo X`(kind=repo·슬롯 무) = 내 것 **∩ 그 repo 의 내 슬롯 전체**(slot_scoped=True·
    #     `_slot_matches` mode="repo"·prefix 매칭) — 신규 repo-scope 뷰. 옛 bare `--slot N`
    #     (repo 불문 cross-repo suffix 매칭)은 제거됐다 — `--slot` 단독은 이제 fail-loud.
    #   `--mine` 과 `--repo`(+`--slot`)는 상호 배타(뷰 스코프는 하나만) — 타 사용자는 어느 필터
    #   뷰에도 안 나온다(무필터 `list` 전용).
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as e:
        sys.exit(f"[중단] {e}")
    explicit_mine = bool(getattr(args, "mine", False))
    if explicit_mine and identity.kind != "none":
        sys.exit("[중단] --mine 은 --repo/--slot 과 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    mine = explicit_mine or identity.kind != "none"
    # slot-scoped 뷰(--repo/--slot)는 claim 을 그 슬롯(또는 그 repo)으로 교집합한다(--mine 은
    # 전 슬롯·ADR-0056).
    slot_scoped = identity.kind != "none"
    slot_mode = "repo" if identity.kind == "repo" else "exact"
    if identity.kind == "slot":
        my_user = user_name()
        my_slot = identity.session
    elif identity.kind == "repo":
        my_user = user_name()
        my_slot = identity.repo
    else:
        my_user = user_name() if mine else None
        my_slot = session_name() if mine else ""
        if mine and my_slot is None:
            # 세션 미바인딩(surface·required=False·ADR-0040) — slot-claim 필터를 못 좁힌다.
            # 안내는 stderr 로 내 stdout 티켓 목록 포맷을 오염시키지 않는다(소유 open 은 계속 표시).
            print("(비바인딩 — 세션 미해소·claim 필터 비활성; `--repo <repo> --slot <N>` 로 지정)",
                  file=sys.stderr)
    # graceful degrade(T-0168 단순화): (a) 풀(내 소유 open) 필터는 보드에 area_owner 가 *운영
    # 중일 때만* 그 파티션을 1차 소유로 쓴다. areas.md 에 area_owner 가 하나도 안 채워졌으면
    # (미마이그레이션 채택자·솔로) area_owner_in_use=False → 소유는 created_by.user 2차 폴백으로
    # 해소한다(`_ticket_owner`).
    #
    # 다중사용자 판정(`multi_user`·solo 정의 완결·codex R3): **티켓 user 토큰이든 area_owner 든
    # 둘 중 하나라도 distinct ≥2 면 multi-user**. `_distinct_ticket_users`(티켓 귀속만 셈) 단독이면
    # 다중-owner 보드라도 claim 이 전부 legacy 슬롯-only(user 토큰 0)일 때 ≤1 로 떨어져 solo 로
    # 오판 → legacy 슬롯-only 포함 경로가 발동해 (당시) bare `--slot N`(repo 불문 cross-repo
    # suffix 매칭 — ADR-0057 로 제거됨)이 타 area 의 legacy `<repo>_N` 을 끌어오는 누출이 났다
    # (ADR-0056 위반). `_distinct_area_owners`(areas 소유 다중성)를 OR 로 더해 그 클래스를 닫는다.
    # solo(둘 다 ≤1)면 미해소 open all-open degrade + legacy 슬롯-only 포함 보존, 다중이면
    # strict-exclude(ADR-0053·ADR-0056).
    area_owner_in_use = mine and _area_owner_in_use()
    multi_user = mine and (_distinct_ticket_users() > 1 or _distinct_area_owners() > 1)
    # status 뷰 셀렉터(T-0197): 기본(무-status)=활성만(done 접기) · `--status all`=전체(done 포함)
    # · `--status <특정>`=그 status 만(기존 동작 무변경).
    status_arg = getattr(args, "status", None)
    if status_arg == "all":
        allowed_statuses = STATUS_DIRS
    elif status_arg:
        allowed_statuses = (status_arg,)
    else:
        allowed_statuses = _LIST_ACTIVE_STATUSES
    rows: list[tuple[str, dict]] = []
    # 세션격리 strict-exclude 신호(ADR-0053 #4·anti-degrade): 다중사용자 보드에서 소유 미해소
    # open 을 이 뷰에서 조용히 드롭했는지 잡는다 — 아래 loud-warn(빈 spam 금지)의 실-드롭 트리거.
    # solo(multi_user False)면 항상 False 라 재평가 자체를 안 함(오버헤드/오탐 0·회귀 0).
    strict_exclude_fired = False
    for status in STATUS_DIRS:
        if status not in allowed_statuses:
            continue
        for p in sorted((tickets_dir() / status).glob("T-*.md")):
            fm, _ = load_ticket(p)
            if args.tag and args.tag not in _tag_values(fm):
                continue
            if mine and not _ticket_is_mine(status, fm, my_user, my_slot,
                                            area_owner_in_use, multi_user,
                                            slot_mode=slot_mode,
                                            slot_scoped=slot_scoped):
                # 이 제외가 *strict-exclude* 였는지 판정: 같은 predicate 를 multi_user=False 로
                # 재평가해 solo(all-open degrade)라면 포함됐을 open 이면 = 다중사용자라서 드롭한
                # 것(`_ticket_is_mine` 미해소 분기). 판정을 복제하지 않고 단일 predicate 를 재사용
                # (ADR-0053 #6 point-patch 금지)해 실 드롭만 신호로 잡는다. 이미 발동했으면 재평가
                # 생략(short-circuit). 소유 해소된 타 사용자 티켓 제외는 solo 에서도 제외라 무신호.
                if multi_user and not strict_exclude_fired and _ticket_is_mine(
                        status, fm, my_user, my_slot,
                        area_owner_in_use, False, slot_mode=slot_mode,
                        slot_scoped=slot_scoped):
                    strict_exclude_fired = True
                continue
            rows.append((status, fm))
    # anti-degrade loud-warn(ADR-0053 #4·fail-loud): 다중사용자 격리가 조용히 티켓을 드롭했거나
    # (strict_exclude_fired) 정체성이 미해소(my_user None)면 목록 출력 *전* stderr 1줄 경고 —
    # silent degrade 근절. **stderr 라 stdout 목록 포맷 무오염**(회귀 파서·pm_bootstrap counts
    # 무영향). solo(distinct user ≤1 → multi_user False)는 무경고(회귀 0·빈 warn spam 금지).
    if mine and multi_user and (strict_exclude_fired or my_user is None):
        print(
            "⚠ 세션격리(strict-exclude): 다중사용자 보드에서 소유 미해소 open 을 이 뷰에서 "
            "제외했다(타 사용자·미귀속 티켓). 내 티켓만 정확히 보려면 정체성 설정 — "
            "`board init --owner <you>` 또는 `board migrate-identity`.",
            file=sys.stderr,
        )
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
# 인자(ADR-0042 §3.2)이므로 그 라벨을 그대로 쓴다(예약어와 표기 일치).
_NO_PREFIX_LABEL = "none"


def cmd_prefix_list(args: argparse.Namespace) -> int:
    """prefix별 티켓 수·번호범위 현황을 출력한다 (read-only·비파괴·ADR-0042).

    STATUS_DIRS(open/claimed/blocked/done)의 전 티켓 ID 를 파싱해 `T-<p>-NNN` → 그 prefix,
    `T-NNNN` → `none`(무prefix)로 버킷팅한다. mess(카테고리 난립·번호 재시작)를 표면화하는
    도구 — board mutation·rewrite 는 하지 않는다(그건 T-0238/T-0239 rename/merge 소관).
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


# ── prefix rename/strip/merge/delete 코어 (ADR-0042 §3.2/§3.3·T-0239) ─────────
# 카테고리 개명/통합 동사. T-0238 rewriter(`rewrite_refs`)를 소비해 참조까지 무손실 relabel
# 한다. 공통 파이프라인(`_prefix_relabel`): old→new 맵 → collision abort → 본문 토큰치환 +
# slug 파일명 rename → 홈 git clean 가드 → board-git 백업 commit. 티켓 물리삭제 없음.

def _parse_prefix_arg(raw: str) -> str | None:
    """CLI prefix 인자 → 실 prefix(str) 또는 None(무prefix). 예약어 `none`(대소문자 무관) → None.

    `none` 은 이름 없는(`T-NNNN`) 네임스페이스의 1급 인자(ADR-0042 §3.2)라 from/to/into 어디서든
    `None`(무prefix) 로 해소된다. prefix 동일성이 case-insensitive fold 이므로 `NONE`/`None` 도
    같은 예약어로 fold 된다(ADR-0055). `none` 이 실 prefix 로 등록될 수 없게 예약돼 있어
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
    # .drafts 포함(codex T-0239 R2 must-fix) — draft 도 이미 발행된 ID(find_ticket/_next_id 인지).
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
    """티켓 ID 에 실제로 쓰인 prefix 집합 (canonical case·ADR-0055 fold 네임스페이스 판정용).

    `_scan_prefix_tickets` 의 canonical ID 해소(frontmatter id 우선·파일명 폴백)를 재사용해
    숫자-slug 모호성 없이 실 prefix 만 모은다. rename/merge dst 의 case-only 중복 검출이 이
    집합에 fold-비교한다(기존 `T-AAA-*` 가 있으면 dst `aaa` 를 fail-loud).
    """
    return {p for t in _scan_prefix_tickets() if (p := t["prefix"])}


def _rename_map(src: str | None, dst: str | None,
                tickets: list[dict[str, Any]]) -> dict[str, str]:
    """rename 무충돌 맵 — src 네임스페이스 티켓의 prefix 만 dst 로 교체(번호 유지).

    src 매칭은 **case-insensitive fold**(`_fold_key`·ADR-0055) — `rename aaa bbb` 가 기존
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
    created 안에서 기존 상대순서를 보존한다(ADR-0042 §Decision 3·finance_dev append 사례).
    created 부재(빈 문자열) 티켓은 정렬에서 맨 앞(최고령)으로 간다 — created 없는 티켓은 대개
    구세대(초기 도입 전) 산출물이라 최고령 배치가 자연스럽다(suggestion 채택·현행 유지).

    source membership 은 **case-insensitive fold**(`_fold_key`·ADR-0055) — `merge aaa --into bbb`
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

    source membership 은 case-insensitive fold(`_fold_key`·ADR-0055)·into 그룹은 exact(`== into`·
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
    ADR-0055) — 이미 오염된 `T-AAA-001`/`T-aaa-001` 공존 보드에서 rename/merge 가 그 case-split 를
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
    잃으므로 abort 해야 한다(무손실 원칙·ADR-0042 §3.3 step 3).
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
    codex T-0259 R2 MF-2). `dump_ticket`/`_next_id` 이 발행 시 확정해 쓰는 frontmatter `id:` 가
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
    prefixed ID 로 오인해 rename 을 누락시키므로(codex R2 MF-2), 스캔측(`_scan_prefix_tickets`)과
    같은 fm-우선 해소로 통일한다. canonical 이 파일명 접두가 아니면(코너/corrupt) slug 슬라이스가
    어긋나므로 파일명 파싱값으로 폴백해 접두 일치를 보장한다(비손실·기존 동작 보존).

    **본문 rewrite 가 스킵될 티켓 파일(비-UTF-8·읽기 실패)은 rename 도 제외**한다(suggestion 채택·
    T-0239 rework): `rewrite_refs` 가 그 파일의 content id 를 못 바꿔 남겨두는데 파일명만 new ID
    로 바꾸면 파일명↔content id 가 어긋난다. 같은 read 프로브(`_is_rewritable`)로 걸러 파일명을
    유지하고 stderr 경고로 수동 확인을 유도한다(silent 누락 금지·`rewrite_refs` 의 skip 경고와 짝).
    """
    renames: list[tuple[Path, Path]] = []
    for status in (*STATUS_DIRS, ".drafts"):   # .drafts 포함 — _scan_prefix_tickets 와 lockstep(codex R2).
        for p in (tickets_dir() / status).glob("T-*.md"):
            tid = _canonical_ticket_id(p)       # frontmatter id 1차 진실 (숫자-slug 모호성 해소·MF-2).
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

    None = git 부재/repo 아님(가드 skip·솔로 안전) · `""` = clean · non-empty = dirty. prefix
    rewrite 는 wiki/log(홈 git)를 건드리므로, 적용 전 홈 git 이 clean 이어야 사용자가 relabel
    diff 를 리뷰·revert 할 수 있다(ADR-0042 §Consequences·spike §6). fail-soft: 예외는 None.
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
    """검증된 old→new 맵을 적용하는 공통 파이프라인 (rename/merge/reid 공용·ADR-0042 §3.3).

    `noun` = 출력·commit 메시지의 op 접두어(카테고리 동사는 "prefix", 단건 재부여는 "reid" —
    `op = f"{noun} {verb}".strip()`·verb 빈 문자열이면 noun 만). 기본값은 prefix 동사 하위호환.

    dry-run: 규모 preview("N ID·M refs·K 파일")만·쓰기 0(read-only 라 락 불요). 적용: 홈 git
    clean 가드 → **단일 `board_lock()` 구간에서** 본문 토큰 rewrite(`rewrite_refs`) + slug 파일명
    rename → board.md 재생성 → board-git 백업 commit(분리 형상)·legacy skip 안내. 티켓 물리삭제
    없음.

    **락 직렬화(codex must-fix)**: rewrite→rename→refresh→board-git 백업 전체를 `cmd_new` 의 ID
    발행·`cmd_claim` 이 쓰는 그 board_lock 으로 감싼다(ADR-0012) — 동시 new/claim 과 직렬화해
    relabel 이 그들 사이에 끼어 참조를 절반만 고치는 것을 막는다. **재진입 정리**: 이 구간 안에서
    부르는 board.md 재생성은 `refresh_board`(자체 board_lock·재진입 데드락)가 아니라 락-보유 전제
    변형 `_refresh_board_locked` 를 직접 부른다. board-git 백업(`_board_git_*`)은 board_lock 이
    아니라 별도 git repo(subprocess)를 만지므로 재획득이 없다(구간 안이어도 데드락 없음).
    complete/block/unclaim 은 설계상 board_lock 을 안 잡는 lock-free rename 이라 이 락이 못 막는다
    (migrate-identity 와 같은 정직한 한계 — 단일-세션 admin op 전제·spike §6).

    **TOCTOU 봉합(codex R3 must-fix)**: `build_map` 은 (id_map|None, rc) 를 반환하는 클로저다 —
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

    # 홈 git clean 가드 — wiki/log rewrite 는 홈(superproject) git 에 떨어진다. dirty 면 relabel
    # diff 가 무관 변경과 뒤섞여 리뷰·revert 불가하므로 abort(dry-run 으로 규모 먼저 확인 안내).
    dirty = _home_git_status_porcelain()
    if dirty:
        print("[중단] 홈 git 에 uncommitted 변경 — prefix rewrite 는 wiki/log 를 건드리므로 먼저 "
              "commit/stash 로 홈 git 을 clean 하게 만들어 relabel diff 를 격리·revert 가능하게 하라. "
              "규모는 `--dry-run` 으로 먼저 확인.", file=sys.stderr)
        return 1

    # board-git 백업 rev — 분리 형상에서 relabel *직전* HEAD(되돌아갈 지점). rewrite 뒤 새 commit
    # 이 relabel 을 board-git 에 기록하므로 이 rev 로 `reset --hard` 하면 원복된다.
    backup_rev = _board_git_head() if _board_git_enabled() else None

    # 전체 mutation 을 단일 board_lock 으로 직렬화 (동시 new/claim 과 상호배제·ADR-0012). 파일명
    # rename 계획은 락 보유 하에 fresh scan 으로 세운다(rewrite 와 같은 스냅샷). refresh 는 락-보유
    # 변형(`_refresh_board_locked`)을 직접 불러 board_lock 재진입(데드락)을 피한다.
    with board_lock():
        # 맵 생성+collision 검사도 락 안 fresh snapshot 으로 — 검사↔적용 사이 cmd_new 창 봉합(R3).
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
        scale = rewrite_refs(root, id_map, dry_run=False)
        _apply_file_renames(renames)
        _refresh_board_locked()
        if _board_git_enabled():
            _board_git_stage_and_commit(f"{op} {label}")

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
    """대상 카테고리(dst/into) 형식 sanity + case-only 중복 거부 — 위반 메시지·정상이면 None (ADR-0055).

    `none`(parsed=None)은 항상 허용(이름 지우기·무prefix). 실 prefix 는 새/재사용 카테고리
    이름이므로 `_validate_prefix`(예약어+`[A-Za-z0-9][A-Za-z0-9_]*`·대소문자 허용·ADR-0055)로
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
                f"(prefix 동일성은 case-insensitive·ADR-0055·네임스페이스 분할 방지). "
                f"기존 case {conflict!r} 로 지정하라.")
    return None


def cmd_prefix_rename(args: argparse.Namespace) -> int:
    """`prefix rename <A|none> <B|none>` — 무충돌=번호유지 교체·충돌=merge 안내(ADR-0042)."""
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
        # 락 안 fresh snapshot 에서 스캔→맵→collision (codex R3 TOCTOU 봉합·dry-run 은 락 밖 호출).
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
    """`prefix strip <A>` — `rename <A> none` 의 별칭(별도 로직 없음·ADR-0042 §3.2)."""
    return cmd_prefix_rename(argparse.Namespace(
        src=args.prefix, dst="none", dry_run=getattr(args, "dry_run", False)))


def cmd_prefix_merge(args: argparse.Namespace) -> int:
    """`prefix merge <A> [B...] --into <T|none>` — created 순 통합(기본 append·ADR-0042)."""
    sources = [_parse_prefix_arg(s) for s in args.sources]
    into = _parse_prefix_arg(args.into)
    reason = _validate_dst_prefix(args.into, into)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    if _fold_key(into) in {_fold_key(s) for s in sources}:
        # 자기-merge 가드도 case-insensitive (ADR-0055) — `merge aaa --into AAA` 는 fold-동일
        # 네임스페이스라 자기 자신에 merge 다(source fold-매칭이 노출한 클래스).
        print(f"[중단] --into 대상({args.into})이 source 목록에 있다 — 자기 자신에 merge 불가"
              "(대소문자 무관·ADR-0055).", file=sys.stderr)
        return 1
    reorder = bool(getattr(args, "reorder_chronological", False))

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot — merge 의 append start=max(...) 도 stale 이면 clobber (codex R3).
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

    repo 등록 행 자체는 남기고 prefix 칼럼만 비운다 — ② 홈의 수동 조치(등록 보존·이름만 지움)와
    동형·무손실. areas write 는 진짜 공유 mutation 이라 board_lock 으로 동시 `areas_append` 와의
    lost-update 를 막는다(ADR-0012·`_migrate_areas_apply` 동형). **재진입 금지**: 락 안에서
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
    """`prefix delete <A>` — 빈(0티켓) prefix 의 areas 등록을 지운다·티켓 있으면 fail-loud(ADR-0042).

    prefix 는 티켓으로만 존재하는 작업 카테고리(자동시드 폐지·ADR-0042)다. 0 티켓이면:
      - areas.md 에 A 가 등록돼 있으면 → 그 행의 **prefix 셀을 실제로 비운다**(행·repo 등록 보존·
        무손실·② 수동 조치와 동형). promise=do — 메시지가 실제 셀 편집과 일치한다.
      - 미등록이면 → 지울 등록이 없으므로 "확인만"(변경 0)으로 정직하게 보고한다.
    티켓이 있으면 물리삭제 없이 rename/merge 로 안내한다(fail-loud). `--dry-run` 은 쓰기 0·규모
    preview(다른 동사와 공통 표기·spike §3.2).
    """
    target = _parse_prefix_arg(args.prefix)
    if target is None:
        print("[중단] none(무prefix) 네임스페이스는 delete 불가 — 기준 네임스페이스다.",
              file=sys.stderr)
        return 1
    dry_run = bool(getattr(args, "dry_run", False))
    # 티켓 존재 카운트는 **case-insensitive fold**(`_fold_key`·ADR-0055) — `delete AAA` 가 case-변종
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


# ── reid — 단일 티켓 ID 재부여 (ADR-0042 관리도구 per-ticket 확장·T-0259) ──────────
# 카테고리 일괄(`prefix` 네임스페이스)과 달리 **티켓 1장**의 오발행 ID(번호·prefix)를 고친다.
# T-0239 prefix rename/merge 와 *같은 파이프라인*(`_prefix_relabel`)을 재사용한다 — old→new 맵을
# `{OLD: NEW}` 단일 항으로 세워 T-0238 토큰단위 rewriter·slug 파일명 rename·홈 git clean 가드·단일
# board_lock·board-git 백업·dry-run 규모 preview 를 그대로 상속한다(새 rewrite 엔진 없음).

def cmd_reid(args: argparse.Namespace) -> int:
    """`reid <OLD-ID> <NEW-ID> [--dry-run]` — 단일 티켓 ID 를 무손실 재부여한다 (ADR-0042·T-0259).

    잘못 발행된 티켓 1장의 ID(`T-0036`→`T-0250`·`T-0036`→`T-finance-036`·역방향)를 파일명·
    frontmatter·**전 참조**(board 내 depends_on/blocks·타 티켓 본문·wiki/log wikilink)까지 한 번에
    고친다. 카테고리 일괄이 아니라 단건이므로 top-level 서브커맨드다(`prefix` 네임스페이스는 카테고리
    전용 유지).

    가드(값싼 정적 → 상태 의존 순): NEW-ID 형식 sanity(발행 문법·prefix 자유 입력) → src≠dst → (락
    안 fresh snapshot) OLD 실재 → 타 세션 claim abort(단일세션 op) → NEW collision(전 상태
    디렉토리+.drafts 에 이미 존재하면 abort). 번호 자동발급 카운터는 `_next_id` 가 max 기반이라 어느
    번호로 옮겨도 무충돌이다 — 다음 발급이 최대치를 자연히 이으므로 별도 조정 없이 확인만 한다(결정).
    """
    old_id, new_id = args.old_id, args.new_id
    dry_run = bool(getattr(args, "dry_run", False))

    # 정적 sanity(락·재조회 불요) — 값싼 거부 먼저. OLD/NEW 형식·자기 자신.
    # OLD-ID 도 형식 선검증(codex T-0259 must-fix): find_ticket 은 glob 기반이라 메타문자(`*`·`?`·`[]`)
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
        # 수행한다 — 검사↔적용 사이 cmd_new/claim 창을 봉합한다(prefix rename build_map 동형·codex R3).
        # OLD 티켓을 canonical ID 로 정확 선택한다. `find_ticket` 은 `{old_id}-*.md` glob 의 *첫*
        # 매치만 반환해 두 결함이 있었다: (R2 MF-1) legacy `T-0036` 부재 시 숫자-prefix `T-0036-001-*.md`
        # 가 glob 에 걸려 silent-noop / (R4) `T-0036-slug.md` 와 `T-0036-001-slug.md` 공존 시 디렉토리
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
        # 타 세션 claim 가드 — 단일세션 op(migrate-identity·prefix rename 동류·spike §6). claim 중인
        # 티켓은 그 소유 세션만 reid 할 수 있다(다른 세션이 작업 중인 ID 를 바꿔 참조를 흔들지 않게).
        # claim/complete/block 이 board_lock-free 라 미세 TOCTOU 는 하드 보장하지 않는다(정직한 한계).
        claimed_by = fm.get("claimed_by")
        if claimed_by:
            claimed_slot = str(claimed_by).rsplit("/", 1)[-1]   # `<user>/<slot>` → slot (또는 slot-only)
            current = session_name(_actor_session_override(args))
            if current is None or claimed_slot != current:
                # remedy 는 ADR-0057 canonical `--repo/--slot` 로 안내 — `claimed_slot` 이
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


def _run_lint_hooks() -> list[tuple[str, str]]:
    """Discover & run instance lint hooks — .project_manager/hooks/lint_*.py (ADR-0003).

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
_PLACEHOLDERS: tuple[str, ...] = (
    "무엇을 만들 / 바꿀 / 검증할지",
    "핵심 산출물 (파일, 동작)",
    "[[xxxxx]]",
    "<제목>",
)
_REQUIRED_SECTIONS: tuple[str, ...] = ("## 목표", "## 완료 조건", "## 참고")


def _body_lint_issues(tid: str, body: str) -> list[tuple[str, str, str]]:
    """단일 티켓 본문의 self-containment issue — `lint_bodies` 와 `cmd_new` 발행-게이트가 공유(T-0196).

    `lint_bodies` 의 검사 로직(placeholder·thin)을 단일-티켓 단위로 추출한 것 — `board.py new`
    가 방금 만든 티켓 하나만 즉석 검사해 board-git 승격(sync) 여부를 정할 때도 재사용한다.
    """
    issues: list[tuple[str, str, str]] = []
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
    코드 span/fence 안의 *예시* wikilink(관례 문서가 backtick 으로 보여주는
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
# alias `|표시명` 은 `_WIKILINK_RE` 동작과 동일하게 흡수(codex suggestion).
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
#   - status-done-accum : status.md ✅ 완성 행 누적 archive 권고 (lint_status "never blocks" 보장).
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
#     "누가 결정했나"(provenance·연속성 아님)를 박는 발행측 규칙 — board.py 는 ADR 을 발행하지 않으므로
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
    """ADR `author` provenance 권고 advisory (T-0165·ADR-0033 ③·never-block).

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

    scope(codex MUST-FIX 4·T-0305 Q3 결착): 대상 = manifest-제외 adapter-layer(settings.json·루트 doc·
    facade·진입문서·local.conf — adopter config·전파 채널 없음) / 제외 = instance-state(status·
    architecture·tickets·log·decisions·README 스캐폴드·lite — 채택자 소유·diverge 정상). **hooks·driver 는
    이제 engine-mirror 전파 대상**(manifest 등록·T-0305·ADR-0032 Q3 결착) — pm-update 가 동기하므로
    silent-stale 클래스가 근절됐다(과거 Q3 open·"대상 단정 안 함"에서 결착). lint 가 파일 단위 diff 를
    하지 않으므로(rev 비교만) scope 는 advisory 메시지로 안내한다.

    fail-soft / 관찰가시성 (T-0305):
      - `upstream` 미설정(솔로·non-adopter·templates/upstream 부재 환경) → [].
      - baseline(`upstream_rev`) 미기록(아직 revision 추적 전·구 import) → [](관찰 기준점 자체 부재).
      - baseline 은 있으나 seen(`upstream_seen_rev`) 미기록(cache 부재 URL·pm-update 미실행) → **관찰불가
        advisory 1줄**(never-block). 과거엔 조용한 [](silent skip)였으나, hooks/driver 등 safety-critical
        잔여가 *관찰 없이* 낡으면 "green 인데 고장"(hard-stop 미발화·회귀 게이트 무력)이라 관찰불가 자체를
        표면화한다(T-0305·ADR-0032 Q3). advisory 라 `--gate` 미차단·1줄이라 flood 아님.
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
    # 이라 관찰불가 자체를 advisory 로 표면화한다(T-0305·ADR-0032 Q3·never-block·1줄이라 flood 아님).
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
                   help="status 뷰 셀렉터(T-0197): 생략 시 활성만(open/claimed/blocked·done 접기)· "
                        "특정 status 하나만 보려면 그 값(예: done)· 전체(done 포함)는 `all`.")
    p.add_argument("--tag")
    p.add_argument("--mine", action="store_true",
                   help="내 것만 (렌즈·단일 보드 위 필터·ADR-0033 ④·ADR-0056·user-first): 내 open"
                        "(area_owner==나 ∨ created_by==나) + 내 claim(claimed_by.user==나)·**전 슬롯**. "
                        "querying identity=현재 사용자(local.conf user= > git email). 타 사용자는 "
                        "안 나온다. solo(user 미상)는 전체 open + 내 슬롯 claim 으로 graceful degrade. "
                        "`--repo`/`--slot` 과 상호 배타(뷰 스코프는 하나만·cmd_list 런타임 검사).")
    # `--repo`/`--slot`(ADR-0057 canonical) — 조회 전용 뷰 스코프: 내 open(슬롯무관 backlog) + 그
    # 슬롯(또는 그 repo 의 내 슬롯 전체)에서의 내 claim 만 비춘다(user-first·ADR-0056). actor
    # `--repo`/`--slot`(claim 등 귀속 쓰기)과 플래그명은 같으나 여기선 아무것도 안 바꾸는 뷰
    # 렌즈일 뿐이다(ADR-0057 §갈림 A — 구 `--session` 뷰 렌즈/bare `--slot` 을 흡수). 전체 보드
    # (타 사용자 포함)는 무필터 `list`.
    identity_args.add_identity_args(p)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id", metavar="T-NNNN")
    identity_args.add_identity_args(p)
    p.add_argument("--user", help="user 식별자 — claimed_by 의 user 차원 (default: local.conf user= / "
                   "git config user.email · ADR-0033 ③)")
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
    p.add_argument("--prefix", help="작업 카테고리 (ADR-0042·자유 입력·배타 구획). "
                   "default: local.conf prefix / 없으면 none(무prefix 1급 → legacy T-NNNN)")
    p.add_argument("--user", help="user 식별자 — created_by 의 user 차원 (default: local.conf user= / "
                   "git config user.email · ADR-0033 ③)")
    p.add_argument("--task", help="task 이름 — task-mode 발행 (F5·spike §3b). `--prefix` 생략 시 task "
                   "설정 prefix(기본 없음)·created_by 는 <user>/<task>. 슬롯 세션 예약 패턴 <repo>_<N> 금지(⑥).")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("promote", help="draft(board-git 미커밋) 티켓을 승격 — 본문 채운 뒤 board-git sync "
                        "(T-0196 발행 규율 게이트: board-git 공유 시 `new` 가 미충전 티켓을 draft 로 남긴다)")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_promote)

    p = sub.add_parser("init", help="clone 당 1회 setup (solo · multi-repo N×M) — pm_state·local.conf·pre-push 훅")
    p.add_argument("--prefix", help="multi-repo (N×M) ID 네임스페이스 (예: PAY). 생략 = solo(legacy T-NNNN)")
    p.add_argument("--area", help="영역 설명 (namespaced: 새 prefix 최초 등록 시 필요)")
    p.add_argument("--owner", help="등록 식별자(registrant·기본: session 이름)")
    p.add_argument("--user", help="area_owner = 그 area 의 user 소유 (`--mine` 풀 입력·ADR-0033 ③·T-0161). "
                                  "미지정 시 local.conf user= / git config user.email 로 해소(없으면 빈 값).")
    identity_args.add_identity_args(p)
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
    p.add_argument("--cwd", help="회귀 실행 cwd (ADR-0014 seam·기본 REPO; multi-PM은 활성 repo worktree·T-0060 배선)")
    identity_args.add_identity_args(p)  # 명시 슬롯(이 슬롯만 회귀·M>1 홈에서 무명시면 전 leased
    # 슬롯 all-or-nothing·ADR-0040 D2)
    p.add_argument("--ticket", help="이 ticket 의 touches 로 스코프 (dev 빠른 루프·advisory)")
    p.add_argument("--touches", help="comma-separated 파일로 스코프 (advisory)")
    p.set_defaults(fn=cmd_regression)

    p = sub.add_parser("livegate",
                       help="릴리즈 라이브 게이트 (ADR-0039) — record=`pytest -m release` "
                            "실행·수집 pin 강제·기록 / check=보호훅이 HEAD-매칭 green 검증")
    p.add_argument("action", choices=["record", "check"])
    p.add_argument("--rev", help="check 대상 sha (보호훅이 push HEAD 를 넘김·record 는 무시)")
    p.add_argument("--cwd", help="record=pytest 실행 cwd(기본=활성 slot worktree) / "
                                 "check=livegate.json 해소 cwd(훅 정렬·기본=이 board.py 사본 REPO) "
                                 "(ADR-0014 seam·record↔check 대칭·T-0306)")
    identity_args.add_identity_args(p)  # record 의 슬롯(M>1 홈에서 cwd 해소·regression 과 동형·
    # 무명시+leased≥2 는 fail-loud·`--cwd` 우회 불요·T-0298)
    p.set_defaults(fn=cmd_livegate)

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

    # reid — 단일 티켓 ID 재부여 (ADR-0042 관리도구 per-ticket 확장·T-0259). 번호·prefix 변경 무손실
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

    # prefix subcommand group — 작업 카테고리 prefix 관리 (ADR-0042). list=현황(read-only) +
    # rename/strip/merge/delete=개명·통합 (T-0239·`none`=무prefix 1급·collision abort·board-git 백업).
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
    # PM-홈 worktree 오실행 가드 (T-0345) — mutation subcommand 전수·단일 dispatch 지점.
    # board 상태를 쓰는 명령만(read·sidecar 제외) 착지 *전에* fail-loud. 분류는 위 상수·미래
    # 누락은 메타 가드 테스트가 잡는다.
    subcommand = _resolved_subcommand(args)
    if subcommand in _MUTATION_SUBCOMMANDS and _guard_worktree_misanchor(f"board.py {subcommand}"):
        return 1
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
