#!/usr/bin/env python3
"""Ticket board CLI — multi-session development coordination.

Atomic claim via POSIX rename(2). Tickets live as markdown files in
`.project_manager/wiki/tickets/{open,claimed,blocked,done,discarded}/`. Each command
updates `.project_manager/wiki/board.md` automatically.

`board.py idea …` manages pre-ADR ideas under
`.project_manager/wiki/ideas/{open,promoted,killed}/` with the same
atomic-rename + frontmatter-sync mechanics (see the `idea` subcommand group).

See `.project_manager/wiki/tickets/README.md` and
`.project_manager/wiki/ideas/README.md` for the workflows.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.app import areas_lint, body_lint, conf_lint, doc_lint, freshness_lint, freshness_pin, hooks, lint_hooks, regression, release, round_seed, ticket_lint, wiki_lint, wikilink_lint  # noqa: E402
from pm_engine.cli import regression_cmd, task_args  # noqa: E402
from pm_engine.domain import areas_rules, design_rules, git_policy, identity_args, markdown_text, pytest_report, review_form, review_state, round_rules, shell_policy, ticket_rules  # noqa: E402
from pm_engine.infra import areas_store, clock, cluster_store, conf_store, console, delegate_ledger, fs, git, lease_store, manifest_store, pm_paths, pre_push_hook, regression_store, repo_coordinates, repo_files, review_rounds, round_store, ticket_store  # noqa: E402
from pm_engine.infra import task_store
from pm_engine.infra import branch_store
from pm_engine.infra import worktree_git

from pm_engine.app import areas_registry
from pm_engine.app import board_claim
from pm_engine.app import board_dashboard
from pm_engine.infra import board_git
from pm_engine.domain import board_git_rules
from pm_engine.cli import board_list
from pm_engine.app import board_sync
from pm_engine.app import ticket_access
from pm_engine.app import cluster_binding
from pm_engine.app import engine_guard
from pm_engine.app import round_maintenance
from pm_engine.app import tier_signals
from pm_engine.cli import board_cluster
from pm_engine.cli import board_dispatch
from pm_engine.cli import board_round
from pm_engine.domain import board_command_rules
from pm_engine.domain import cluster_rules
from pm_engine.infra import worktree_paths

_load_module_from_path = repo_files.load_module

REPO = Path(__file__).resolve().parents[2]
IDEAS_DIR = REPO / ".project_manager" / "wiki" / "ideas"
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored): py·test_cmd·ctx_* (세션·prefix 는 여기 없다 — lease 장부·areas.md)


# ── board root (graceful 탐지) ───────────────────────────────
# board(tickets+areas)는 `.project_manager/board/`(submodule)로 분리될 수 있다 — 그러면
# git 형상이 design(superproject)/board(submodule) 둘로 갈려 PM 운영 commit 이 코드 git 을
# 오염하지 않는다. 분리되지 않은 legacy(미마이그 adopter)에선 board 가
# wiki/ 안에 그대로 산다. `pm_paths._board_root_at` 이 *실측*으로 둘을 가른다 — board/tickets 가
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


def _home():
    """이 실행의 기준 경로 — 자기 앵커 `REPO`, read 폴백 중이면 소유 PM 홈의 board·로컬 입력."""
    return pm_paths.home_at(REPO, _READ_PM_HOME_OVERRIDE, _READ_BOARD_ROOT_OVERRIDE)


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
# worktree_pool 의 LEASES_FILE 와 *같은 위치*(그 관례 — `.local/worktree-leases.json`). board 는
# worktree_pool 을 import 하지 않으므로 경로를 자체 해소해 파일을
# 직접 read 한다(슬롯 test_cmd 레이어·아래 _active_slot_test_cmd). areas.md 읽듯 데이터-결합만.
LEASES_FILE = LOCAL_DIR / "worktree-leases.json"            # worktree 리스 장부 (read-only here)
DELEGATE_CHANNEL_GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
CODEX_DELEGATE_OBSERVATIONS = (
    LOCAL_DIR / "delegate-channel" / "codex-observations.jsonl"
)


# ── PM-홈 worktree 오실행 가드 (mutation dispatch 게이트) ───────────────
# board 계열 도구를 PM 홈의 등록 worktree(`work/<repo>_<N>`) cwd 에서 실행하면 도구가 cwd
# 기준 자기-앵커(REPO)로 그 worktree 트리에 조용히 착지해 stray 산출을 낸다 — `board.py new`
# 가 잘못된 네임스페이스의 새 티켓 ID를, `ticket_finish` 가 stray `wiki/log/current.md` 를
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
# worktree 에서 도는 게 설계 의도·`.local` sidecar 만 씀)·standalone(worktree 형상 아님)은
# 무영향(감지 실패 시 현행 동작·오탐 0·fail-soft). `_worktree_cwd`/`_auto_slot`(PM 홈에서 공개 제품 worktree 찾기·
# 회귀 cwd 해소)의 *역방향*: 여기선 공개 제품 worktree에서 실행된 걸 잡는다.
#
# 분류 원칙: **PM 홈 소유 상태를 쓰는 명령**(티켓 상태전이·생성·ID/prefix relabel·idea·wiki
# family-scope retag·clone init·파생 board.md 재생성)은 게이트. **조회(read)** 와 **anchor-keyed
# sidecar**(regression/livegate·설계상 worktree 실행)는 비게이트. 각 항목 근거는 아래 상수 주석.

# board 상태(티켓/idea/prefix/id/areas/wiki-scope/파생 board.md)를 쓰거나 clone 을 init 하는
# mutation subcommand — dispatch 게이트가 이 집합에만 worktree 오실행 가드를 적용한다.
# (idea/prefix 서브그룹은 `<group> <sub>` 점표기.) reid=ID 재부여·promote-scope=wiki family_scope
# retag·refresh=파생 board.md 재생성(worktree refresh 는 정당 사용 없음·stray dashboard 방지)·
# verified-at-repin=PM 홈 현재-진실 문서(architecture/status/domain) frontmatter 쓰기(
# worktree 엔 그 문서가 없어 no-op 이나 PM 홈 실행이 설계 의도라 오실행 가드)·
# design=frontmatter `design:` 값 기록/갱신(tier와 같은 in-place writer).
# 조회(read-only·board 상태 미변경) — 게이트 없음.
# read leaf별 PM-owned 입력 전수. dispatch 첫 줄이 이 값을 그대로 표시하므로, worktree에서
# 어느 홈의 무엇을 읽는지 조용히 숨지 않는다. 제품 코드(REPO 루트 문서·adapter/template 및
# git freshness 대상)는 현재 worktree를 계속 읽는다. PM 운영 상태만 단일 소유 홈으로 묶는다.
# anchor-keyed sidecar — board 상태 아님. regression=`.local/regression.json`, livegate=
# 공유 engine-root `livegate.json`(둘 다 anchor HEAD 로 키·**worktree cwd 실행이 설계 의도**).
# 게이트하면 릴리즈/회귀 흐름이 깨진다— 비게이트.
# 서브그룹 이름 → 그 그룹 leaf 를 담은 argparse dest. 분류 키(`<group> <leaf>` 점표기)를 만드는
# `_resolved_subcommand` 의 단일 표다 — 파서에 그룹을 더하면 여기에도 등재한다.


def judge_tool_input(
    root: str | os.PathLike[str], session: str, cwd: str, tool_name: str, tool_input: object,
) -> dict[str, str]:
    """셸이 아닌 도구(파일 쓰기·편집·위임 등) 입력 판정 — 허용 위조, 그리고 슬롯 쓰기 주인.

    허용 기록 폴더 이름이 어느 값에든 있으면(파일 경로·patch 본문) 기록 파일 쓰기로, 줄 머리에
    허용 표식이 있으면(파일 내용·위임 프롬프트) 중첩 세션 프롬프트로 허용을 만들려는 시도로 본다.
    파일 쓰기·편집 도구의 대상이 `root`(훅이 설치된 PM 홈) 슬롯 안이면 이 세션 task 가 잡은 슬롯일
    때만 통과한다. 그리고 대상이 어디든, 이 세션 task 에 **열린 todo 가 없으면** 경고를 붙인다
    (`_no_open_todo_judgment`·막지는 않는다) — 무엇을 하는 중인지 장부에 없다는 사실을 화면에 올린다.
    거부가 이기고 경고는 거부가 없을 때만 나간다(같은 호출에 두 말을 겹치지 않는다).
    """
    for text in git_policy._tool_input_strings(tool_input):
        if git_policy.GIT_GRANT_DIR_NAME in text.replace("\\", "/") or git_policy.GIT_GRANT_LINE.search(text):
            judgment = git_policy._git_grant_forgery_judgment(
                f"도구 입력에 허용 기록 폴더(`{git_policy.GIT_GRANT_DIR_NAME}`) 또는 줄 머리 허용 표식"
                "(`git 허용:`)이 있음"
            )
            judgment["reason"] = git_policy._git_guard_stop_reason(
                [judgment.pop("case")], judgment["reason"], grants_available=False,
            )
            return judgment
    targets = hooks._write_tool_targets(cwd, tool_name, tool_input) if isinstance(tool_input, dict) else []
    # 파일을 바꾸는 호출에만 본다 — 읽기 도구는 대상이 없어 여기 들어오지 않는다.
    todo_warning = engine_guard._no_open_todo_judgment(pm_paths.home_at(root), session) if targets else None
    denies = [
        judgment for judgment in (hooks._slot_write_judgment(root, session, target) for target in targets)
        if judgment is not None and judgment["verdict"] == "deny"
    ]
    if denies:
        return {
            "verdict": "deny", "cwd_identity": "slot", "code": git_policy.GIT_DESTRUCTIVE_DENY_CODE,
            "reason": git_policy._git_guard_stop_reason(
                [item["case"] for item in denies], " | ".join(item["reason"] for item in denies),
                grants_available=False,
            ),
        }
    if todo_warning is not None:
        return todo_warning
    return {"verdict": "ok", "cwd_identity": "non-repo", "reason": "허용 위조 표식·남의 슬롯 쓰기 없음"}


def record_git_grants(
    root: str | os.PathLike[str], session: str, cwd: str, prompt: str,
) -> list[str]:
    """사용자 프롬프트의 `git 허용: <명령 원문>` 줄마다 허용 기록 1건을 남긴다.

    사용자 프롬프트 훅 전용이다 — 도구 호출 경로에서 부르지 않는다. 한 줄이라도 형식이 틀리면
    아무것도 기록하지 않고 `GitGrantShapeError` 를 올린다. 반환은 기록한 명령(정규화 표기)이다.
    """
    if not session:
        raise ValueError("세션 id 가 없어 허용을 세션에 묶을 수 없다")
    records = [
        hooks._git_grant_record(session, cwd, hooks._git_grant_argv(match.group(1)))
        for match in git_policy.GIT_GRANT_LINE.finditer(prompt)
    ]
    for record in records:
        path = hooks._git_grant_path(root, record)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}-{time.monotonic_ns()}")
        temporary.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        fs.atomic_replace(temporary, path)
    return [shlex.join(record["argv"]) for record in records]


def judge_git_anchor_command(
    root: str | os.PathLike[str], session: str, cwd: str, command: str, *, grants: bool = False,
    runner: Any = subprocess.run,
) -> dict[str, str]:
    """셸 command의 모든 git/엔진/파일 삭제 호출을 중앙 파싱하고 가장 강한 판정을 반환한다.

    멈춤 거부(손 git 파괴 형태·슬롯/공용 저장소 파일 삭제·허용 위조·남의 슬롯 git 쓰기)는 첫 줄 멈춤
    지시와 경우별 다음 행동을 붙인다. ``root`` 는 훅이 설치된 PM 홈, ``session`` 은 호출한 하네스 세션
    ID 다 — ``root`` 슬롯 쓰기 주인 판정에 쓰고, 거부가 아니면 `pm_bootstrap.py --task <task>` 호출의
    세션 기록을 남긴다(`_record_bootstrap_session_claims`). ``grants`` 는 이 하네스가 사용자 1회 허용을
    쓸 수 있을 때 준다 — 그러면 1회 허용으로 풀 수 있는 거부에서 ``root`` 에 남은 이 세션·정확히 같은
    명령의 허용 기록을 소비하고 통과시킨다. 없으면 그 거부는 사용자가 터미널에서 직접 실행하도록 안내한다.
    """
    parsed = hooks._git_invocations_from_shell(cwd, command)
    invocations = parsed.invocations
    judgments: list[dict[str, str]] = []
    for call in invocations:
        judgment = engine_guard.judge_git_anchor(pm_paths.home_at(root), session, call.cwd, list(call.argv), runner=runner, cwd_certain=call.anchor_certain)
        if (
            not call.certain and judgment["verdict"] != "warn"
            and judgment.get("code") != git_policy.GIT_DESTRUCTIVE_DENY_CODE
        ):
            judgment = {
                **judgment,
                "verdict": "warn",
                "reason": f"조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음; {judgment['reason']}",
            }
        judgments.append(judgment)
    for call in parsed.engine_invocations:
        judgment = engine_guard.judge_engine_invocation(call.cwd, list(call.argv), runner=runner)
        if not call.certain and judgment["verdict"] != "warn":
            judgment = {
                **judgment,
                "verdict": "warn",
                "reason": f"조건/cwd를 정적으로 단일 증명할 수 없음 — 차단하지 않음; {judgment['reason']}",
            }
        judgments.append(judgment)
    for call in parsed.removal_invocations:
        removal = engine_guard.judge_file_removal(
            call.cwd, call.argv, cwd_certain=call.anchor_certain, runner=runner,
        )
        if removal is not None:
            judgments.append(removal)
    forgery = git_policy._git_grant_forgery_in_command(command) if isinstance(command, str) else None
    if forgery is not None:
        judgments.append(forgery)
    if parsed.persistent_cd:
        judgments.append({
            "verdict": "warn", "cwd_identity": "non-repo",
            "reason": (
                "cd는 셸 cwd를 호출 뒤에도 잔존시킬 수 있음 — 대상을 절대경로로 지정하라; "
                "cd가 꼭 필요하면 이 호출 안에서만 사용하고, 다음 호출은 cwd를 가정하지 마라"
            ),
        })
    if parsed.uncertain:
        judgments.append({
            "verdict": "warn", "cwd_identity": "non-repo",
            "reason": f"{parsed.reason} — 차단하지 않음; cwd를 직접 확인",
        })
    if isinstance(command, str):
        judgments.extend(hooks._shell_slot_warnings(root, session, cwd, command))
    if not judgments:
        return {
            "verdict": "ok", "cwd_identity": "non-repo",
            "reason": "git mutation/엔진 호출 없음",
        }
    result = hooks._finish_git_guard_judgment(
        git_policy._strongest_git_guard_judgment(judgments), judgments, cwd=cwd, command=command,
        grant_root=root if grants else None, grant_session=session if grants else None,
    )
    if result["verdict"] != "deny":
        engine_guard._record_bootstrap_session_claims(session, parsed)
    return result


# 부트스트랩 사본 홈의 `worktree_pool` 로 세션 기록 한 건을 남기는 하위 프로세스 프로그램.
# argv: tools 경로 · task · 세션 ID.


# 등록 worktree read 폴백에서 같은 PM 홈으로 옮겨야 하는 import-time 경로 상수 전수.
# board_root/tickets/areas는 lazy override가 담당하고, 이 표는 wiki 잔류 상태·
# PM 로컬 설정·그 상태를 읽는 deep-import 모듈을 담당한다. 상대경로가 단일 진실이라 새
# PM-owned 입력을 추가할 때 이 표와 `_READ_PM_INPUTS_BY_SUBCOMMAND`를 함께 갱신하게 된다.
_READ_PM_PATH_RELS: dict[str, str] = {
    "IDEAS_DIR": "wiki/ideas",
    "LOG_FILE": "wiki/log/current.md",
    "LOCAL_CONF": "local.conf",
    "TICKETS_DIR": "wiki/tickets",
    "TEMPLATE_FILE": "wiki/tickets/_template.md",
    "AREAS_FILE": "areas.md",
    "PM_STATE_FILE": "wiki/pm_state.md",
    "PM_STATE_TEMPLATE": "wiki/pm_state.template.md",
    "LOCAL_DIR": ".local",
    "LEASES_FILE": ".local/worktree-leases.json",
    "DELEGATE_CHANNEL_GUARD_PY": "tools/delegate_channel_guard.py",
    "CODEX_DELEGATE_OBSERVATIONS": ".local/delegate-channel/codex-observations.jsonl",
}


@contextlib.contextmanager
def _read_pm_inputs_at(pm_home: Path, board_root_path: Path) -> Iterator[None]:
    """read dispatch 동안 PM-owned 입력 전부를 `pm_home` 하나로 재앵커한다.

    module 상수는 import 시 REPO에 굳으므로 board_root override만으로는 decisions/ideas 등이
    worktree에 남는다. 이 scope는 그 상수를 바꾸고 finally에서 원복한다.
    mutation/sidecar는 이 context에 진입하지 않아 쓰기 게이트 의미론은 바뀌지 않는다.
    """
    global _READ_BOARD_ROOT_OVERRIDE, _READ_PM_HOME_OVERRIDE
    saved = {name: globals()[name] for name in _READ_PM_PATH_RELS}
    saved_board_override = _READ_BOARD_ROOT_OVERRIDE
    saved_home_override = _READ_PM_HOME_OVERRIDE
    pm_dir = pm_home / ".project_manager"
    try:
        for name, rel in _READ_PM_PATH_RELS.items():
            globals()[name] = pm_dir / rel
        _READ_BOARD_ROOT_OVERRIDE = board_root_path
        _READ_PM_HOME_OVERRIDE = pm_home
        yield
    finally:
        for name, value in saved.items():
            globals()[name] = value
        _READ_BOARD_ROOT_OVERRIDE = saved_board_override
        _READ_PM_HOME_OVERRIDE = saved_home_override


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.8.1"


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


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


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
        ticket_rules._REWRITE_LEADING_BOUNDARY
        + "(?:" + "|".join(re.escape(o) for o in olds) + ")"
        + ticket_rules._REWRITE_TRAILING_BOUNDARY)

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
            with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as fh:
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


def _configure_board_submodule() -> bool:
    """board submodule 의 `ignore = all` 을 자동 설정 (누출 0). 멱등·fail-soft.

    board 가 submodule 로 분리(`.project_manager/board/.git` 존재)된 형상에서만 동작한다 —
    superproject(design·코드 git)에서 `submodule.<path>.ignore all` 을 켜면, board(submodule)가
    PM 운영 commit 으로 전진해도 design 의 `git status`/`git diff` 가 그 gitlink drift 를 숨겨
    routine `git add -A` 가 board 포인터 bump 를 *우발 stage* 하지 않는다(board↔design 누출 0).

    fail-soft: git 바이너리 부재·git repo 아님·submodule 미분리(`.../board/.git` 없음·
    legacy)면 아무 것도 하지 않고 False 반환(미마이그 adopter 100% 무영향). 멱등:
    `git config` 는 같은 키를 덮어쓰므로 재실행 안전. 반환 True = 설정 적용.

    config 키 = `submodule.<.gitmodules-path>.ignore`. board
    의 `.gitmodules` 서브섹션 *이름*을 권위로 읽어(표준 `git submodule add` 는 name==path) 키를
    구성한다 — 이름이 path 와 달라도 정확한 키로 set.
    """
    board_git = REPO / ".project_manager" / "board" / ".git"
    if not board_git.exists():
        return False  # submodule 미분리(legacy) — no-op
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


# conf 예시 시드는 없다 — 키 목록·설명은 레지스트리 한 곳이 소유한다(`local_conf.py --keys`).
# 파일이 자기 규칙("여기엔 실값만 둔다")을 스스로 어기지 않게, 엔진은 conf 에 주석 스키마를
# 배달하지 않는다. 모르는 키는 `board.py lint` advisory 로 드러나므로 배달 경로가 필요 없다.


# `pm-verified` 재검증이 스코프하는 **리뷰 채널** — 게이트가 실린 장부가 곧 채널이다. 그 채널만
# 본다(다른 채널의 accepted 잔여·기계 확인은 이 게이트의 증거가 아니다 — 채널 격리). role
# 문자열의 단일 진실은 review_form 이라 사본을 두지 않고 재검증 시점에 해소한다
# (`_pm_verified_channel_role`). code-reviewer 잔여는 장부가 아니라 리뷰 라운드 파일에서 판정한다
# (`_review_completion_problem`).
GATE_CHANNEL_ADDITIONAL = "additional"    # review_rounds.json (추가 리뷰어)


_UTC_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$"
)
_TARGET_REV_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _parse_utc_iso(value: object) -> datetime.datetime | None:
    """writer 형식의 ISO-8601 UTC 시각만 datetime 으로 파싱한다 (아니면 None).

    `datetime.fromisoformat` 단독은 공백 separator·offset 생략 등도 받아들인다. 장부 선후는 보안
    경계라 writer 가 내는 `YYYY-MM-DDTHH:MM:SS[.ffffff]+00:00`(`Z`도 동치)만 받고, 실제 달력값과
    UTC offset 을 다시 확인한다. 문자열 사전순 비교는 손상값 `zzz`가 미래로 통하는 구멍이라 쓰지
    않는다."""
    if not isinstance(value, str) or _UTC_ISO_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() == datetime.timedelta(0) else None


def _unresolved_must_fix_data(data: object, ledger: Path) -> list[str]:
    """파싱된 라운드 장부의 미해소 must-fix 차단 사유 (writer/reader 공용)."""
    if not isinstance(data, dict):
        return ["  · 라운드 장부 형식 오류(최상위가 매핑이 아님) — 잔여 must-fix 를 확인할 수 "
                "없어 차단합니다"]
    problems: list[str] = []
    for gate, entry in sorted(data.items()):
        if gate == review_rounds._REVIEW_LEDGER_WAVE_KEY:
            continue                     # 예약 키(wave 예산 절)는 게이트가 아니다.
        # 추가 리뷰어(release) 축은 pm-verified 처분을 허용한다 — PM rejected/기계 확인 증거로
        # 채널 폐지 뒤에도 재호출 없이 잔여를 종결할 수 있어야 한다(콜백은 그 게이트의
        # 잔여 must-fix 건수로 스코프한 채널 격리 재검증).
        problem = review_rounds._gate_disposition_problem(
            gate, entry,
            allow_pm_verified=True,
            pm_verified_problem=_gate_pm_verified_problem(
                gate, entry, GATE_CHANNEL_ADDITIONAL,
            ),
        )
        if problem is not None:
            problems.append(problem)
    return problems


def _unresolved_must_fix_gates(ledger: Path) -> list[str]:
    """미해소 must-fix 잔여의 차단 사유 목록 — 없으면 빈 목록(=릴리즈 진행).

    장부가 **아예 없으면 무대상**이다(빈 목록) — 추가 리뷰어를 쓰지 않는 채택자에겐 검사할 사실
    자체가 없고, 그 형상에 없는 파일을 요구하면 게이트가 아니라 장애물이다. 반면 장부가 있는데
    **읽지 못하면 차단**한다: "잔여가 없다"를 확인하지 못한 것과 "잔여가 없다"는 다르다. 그 원칙은
    파일 단위에서 끝나지 않고 **항목 단위**까지 간다(`gate_entry_corruption`).
    """
    if not ledger.exists():
        return []
    try:
        data = json.loads(fs.read_text_shared(ledger, encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        return [f"  · 라운드 장부를 읽지 못했습니다 ({type(exc).__name__}: {exc}) — 잔여 "
                "must-fix 를 확인할 수 없어 차단합니다 (장부 수리 후 재실행)"]
    return _unresolved_must_fix_data(data, ledger)


def _sync_release_must_fix_marker(flag: Path) -> list[str]:
    """장부 스냅샷 판정과 잔여 표식 교체를 writer 락 하나로 직렬화한다.

    livegate record/check 경로다. additional_reviewer 는 이미 같은 락을 보유한 `_save_round_ledger` 안에서
    `_unresolved_must_fix_data` + `_write_release_must_fix_marker` 를 직접 호출해 재진입을 피한다."""
    ledger = release._release_rounds_ledger(flag)
    with fs.exclusive_file_lock(release._release_rounds_lock(ledger)):
        problems = _unresolved_must_fix_gates(ledger)
        release._write_release_must_fix_marker(flag, problems)
    return problems


def _refuse_release_for_engine_drift(flag: Path, cwd: str) -> int:
    """실행 엔진 사본(REPO)이 upstream 과 drift 상태면 fail 기록 + rc1 — 라이브 wave **전** 조기 거부.

    판정은 `pm_update.drift_changes(REPO)` 하나뿐이다(별도 diff 재구현 없음). 판정 불능
    (`None` — upstream 미설정·URL upstream·upstream 경로 미존재·manifest 부재·engine_root 에
    local.conf 부재는 모두 같은 "upstream 미설정" 경로로 수렴)은 **거부가 아니다** — 게이트 부재가
    아니라 이 형상엔 적용 불가이므로 stderr 경고 1줄만 내고 기존 경로를 그대로 진행한다(upstream
    미등록 채택자의 릴리즈를 막지 않는다). fail 기록은 must-fix 축과 **같은 기록 함수**
    (`fs.write_json_atomic`)를 쓴다(손기록 경로 신설 금지).

    마킹된 엔진 사본 skew(`pm_update.drift_changes` 가 접지 않고 그대로 올리는 그 예외 —
    `_is_engine_rev_skew`)는 판정 불능이 아니라 이 게이트의 가장 강한 **확정 양성**이다 — 사본끼리
    rev 가 갈린 트리는 drift 판정 자체가 신뢰 불가하므로, must-fix 축과 같은 원자 fail 기록으로
    번역해 rc1·라이브 wave 미실행을 고정한다(사전 pass 기록이 남아 있어도 이 fail 로 교체한다).
    """
    pm_update = _load_pm_update_module()
    if pm_update is None:
        # 형제 로더 실패 — `_load_pm_update_module` 의 기존 계약(호출부가 검사 대상 0 으로 흡수)과
        # 같은 무차단이되, 이 게이트는 원인을 stderr 로 알린다(완전 침묵은 "적용됐는데 통과"와
        # "판정 자체를 못 함"을 구분 못 하게 한다).
        print("livegate: 경고 — pm_update 로더 실패로 엔진 drift 판정 불능(무차단으로 진행).",
              file=sys.stderr)
        return 0
    try:
        changes = pm_update.drift_changes(REPO)
    except RuntimeError as exc:
        if _is_engine_rev_skew(exc):
            # 원자 fail 기록(must-fix 축과 같은 기록 함수) 뒤 표준 skew 진단 경계로 넘긴다 —
            # 사전 pass 기록이 남아 있어도 이 fail 로 교체한다(false-green 잔존 0).
            flag.parent.mkdir(parents=True, exist_ok=True)   # 기록 경로 보장 (must-fix 거부와 같은 규칙)
            fs.write_json_atomic(flag, {
                "head": git._git_head_at(cwd), "status": "fail", "n": 0, "rc": None,
                "ts": clock.now_utc(), "reason": release._LIVEGATE_ENGINE_DRIFT_REASON,
            })
            return _report_engine_rev_skew_at_terminal(exc)
        raise   # 마킹 안 된 RuntimeError(예 EmptyShippingInventoryError)는 종전대로 전파.
    if changes is None:
        print("livegate: 경고 — 실행 엔진 사본 drift 판정 불능(upstream 미설정/URL/경로 미존재/"
              "manifest 부재) — 이 게이트는 이 형상에 적용되지 않는다(무차단). upstream 을 등록하려면 "
              "python3 .project_manager/tools/pm_update.py --dry-run 안내를 따르세요.",
              file=sys.stderr)
        return 0
    if not changes:
        return 0
    flag.parent.mkdir(parents=True, exist_ok=True)   # 기록 경로 보장 (must-fix 거부와 같은 규칙)
    fs.write_json_atomic(flag, {
        "head": git._git_head_at(cwd), "status": "fail", "n": 0, "rc": None,
        "ts": clock.now_utc(), "reason": release._LIVEGATE_ENGINE_DRIFT_REASON,
    })
    shown = changes[:release._ENGINE_DRIFT_LISTED_MAX]
    items = "\n".join(f"  · {rel}" for rel in shown)
    remainder = len(changes) - len(shown)
    if remainder > 0:
        items += f"\n  … 외 {remainder}건"
    print(release._ENGINE_DRIFT_BLOCK_GUIDANCE.format(count=len(changes), items=items), file=sys.stderr)
    return 1


def _livegate_record(args: argparse.Namespace) -> int:
    """`pytest -m release` 를 실행·측정하고 결과를 livegate.json 에 기록한다 (실행=기록).

    슬롯 cwd 해소(`_livegate_cwd` — 활성 slot worktree)로 subprocess 실행 →
    **rc0 그리고 수집 N==pin** 일 때만 `pass @ <worktree HEAD>` 기록. rc!=0 또는 N!=pin 이면
    `fail` 기록 + rc1(사유 표면화). 손기록 경로는 없다(위조/착오 차단). 기록 위치는
    push 보호훅 read 위치(`_resolve_livegate_flag` — engine-root sidecar)와 정렬해 단일 소스다
    (worktree/PM 홈 어느 board.py 로 돌려도 훅이 읽는 한 파일).

    직접 `--cwd`는 단독으로 지정하며 `--task`/`--repo`/`--slot`과 함께 쓰거나 비울 수 없다.
    `--task`(폴더가 2개 이상이면 `--repo`/`--slot` 위치 인자 추가)가 실행 위치를 고른다(M>1 홈에서
    슬롯 cwd 를 명시 — `--cwd` 절대경로 핀 우회 불요). `--task` 없는 `--repo`/`--slot` 은 실행
    명령 판정 함수가 거부한다. 무명시 + leased ≥2 는 seam 이 fail-loud(모호는 시끄럽게) 하며, 그
    메시지가 안내하는 `--task <task>` 가 실제로 이 subparser 에서 수용돼 dead-end 가 아니다
    (remedy 정직·anti-pattern 회피).
    """
    # 호출자가 준 cwd를 task 경로 주입 전에 검사한다.
    requested_cwd = getattr(args, "cwd", None)
    if requested_cwd is not None:
        if any(getattr(args, key, None) is not None for key in ("task", "repo", "slot")):
            sys.exit(
                "[중단] livegate record의 --cwd는 --task/--repo/--slot과 함께 쓸 수 없다. "
                "외부 경로 override를 허용하지 않는다. "
                "경로 핀은 --cwd <경로>만, task 작업공간은 --cwd 없이 "
                "--task <task> [--repo <repo> --slot <N>]로 실행하라."
            )
        if requested_cwd == "":
            sys.exit("[중단] livegate record의 --cwd 경로가 비어 있다. 검증할 코드 트리 경로를 지정하라.")
    # task-mode(`--task`) 실행 위치 — 특정 슬롯 worktree 절대경로를 cwd 로
    # 고정하고 surface 한다(cwd 비참여·livegate 는 고정 release cmd 라 test_cmd 는 불요).
    if getattr(args, "task", None):
        task_cwd, _ = task_args._resolve_task_workspace_cwd(_home(), args)
        if getattr(args, "cwd", None) is None:
            args.cwd = task_cwd
        print(f"livegate: 작업공간(task {args.task}) → {task_cwd}")
    # `--cwd` 명시(릴리즈 readonly 슬롯 핀)면 session 해소 자체를 생략한다 —
    # `_livegate_cwd` 는 override 최우선이라 session 이 불요하다.
    _explicit_cwd = getattr(args, "cwd", None)
    live_session = (None if _explicit_cwd
                    else lease_store.session_name(_home(), task_args._execution_actor_session_override(_home(), args)))
    cwd = regression_cmd._livegate_cwd(_home(), _explicit_cwd, session=live_session)
    # 기록 위치를 push 보호훅 read 위치와 정렬(단일 소스) — **실행 전에** 해소한다. 훅과 같은
    # engine-root sidecar 해소를 공유해, worktree board.py·PM 홈 board.py 어느 쪽으로 돌려도 훅이
    # 읽는 한 파일에 기록. engine-root 무효(BROKEN)는 실행 전에 알 수 있으니, 값비싼 `pytest -m
    # release`(라이브 wave)를 헛돌리기 전에 fail-loud 로 조기 거부한다.
    flag, mode = regression_store._resolve_livegate_flag(_home(), cwd)
    if mode == regression_store._LG_BROKEN:
        # 보호훅은 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소)
        # → 기록 위치와 훅 read 위치가 갈릴 수 있어 pass 위장을 찍지 않고 fail-loud 거부한다
        # (false-green 백스톱). engine-root sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단). engine-root sidecar 수리 또는 "
              "`pm-config repo add` 재실행. 릴리즈 차단.", file=sys.stderr)
        return 1
    cwd = os.path.abspath(cwd)
    flag.parent.mkdir(parents=True, exist_ok=True)
    fs.write_json_atomic(flag, {"head": "", "status": "fail",
                              "n": 0, "rc": "pending", "ts": clock.now_utc()})
    # 미해소 must-fix 잔여 — 값비싼 라이브 wave 를 돌리기 **전에** 본다(BROKEN 조기 거부와 같은
    # 축). 장부가 없는 형상(추가 리뷰어 비활성 채택자)은 무대상이라 그대로 지나간다.
    try:
        # 장부 판정과 shell 소비 표식을 같은 review_rounds.lock 아래 갱신한다. 표식 쓰기 실패는
        # 우회 경로를 현행화하지 못한 상태라 release wave 를 돌리지 않고 fail-closed 한다.
        problems = _sync_release_must_fix_marker(flag)
    except OSError as exc:
        print("livegate: fail — must-fix 잔여 표식을 원자 갱신할 수 없어 차단 "
              f"({type(exc).__name__}: {exc}). 장부/`.local` 권한을 복구한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return 1
    if problems:
        return release._refuse_release_for_must_fix(flag, cwd, problems)
    # 실행 엔진 사본 drift — must-fix 거부 직후·라이브 wave **전**(같은 축: 값비싼 wave 를 헛돌리기
    # 전에 조기 거부). stale 사본이면 이 판정 자체가 false 가 되므로 wave 보다 앞에 둔다.
    drift_rc = _refuse_release_for_engine_drift(flag, cwd)
    if drift_rc:
        return drift_rc
    regression_session = (
        live_session
        if live_session and len(lease_store.leased_sessions(LEASES_FILE)) >= 2
        else None
    )
    regression_data, platform_anchor, platform_commands, problem = release._release_core_context(
        _home(), flag, cwd, regression_session,
    )
    start_head = git._git_head_at(cwd) if platform_commands else ""
    snapshot = {}
    cells = []
    provisional = None
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    if problem is None and platform_commands:
        if not start_head or regression_data is None:
            problem = "core evidence missing"
        else:
            problem = regression._core_record_problem(
                _home(), regression_data, platform_anchor, start_head, session=regression_session,
                local_dir=flag.parent, release=True,
            )
            core_flag, core_mode = regression_store._resolve_regression_flag(_home(), platform_anchor)
            if core_mode == regression_store._LG_BROKEN or core_flag.parent != flag.parent:
                problem = "core evidence coordinates mismatch"
        if problem is None and not Path(cwd).is_dir():
            problem = "release cwd missing"
        if problem is None:
            snapshot = {key: regression_data[key]
                        for key in ("scope", "collected", "previous_collected", "command", "conf_anchor")}
            if "session" in regression_data:
                snapshot["session"] = regression_data["session"]
            cells, platforms_passed, platform_rc = regression._run_platform_cells(
                platform_commands, cwd, start_head, env,
                core_passed=True, head_after_core=start_head,
            )
            provisional = {"head": start_head, "status": "pass", "n": regression.LIVEGATE_RELEASE_PIN,
                           "rc": 0, "ts": clock.now_utc(), "cwd": cwd,
                           "platforms": cells, **snapshot}
            if not platforms_passed:
                provisional.update(status="fail", n=0, rc=platform_rc)
                fs.write_json_atomic(flag, provisional)
                problem = f"platform red: {platform_rc}"
            else:
                problem = release._release_evidence_problem(_home(), provisional, flag, start_head)
    if problem is not None:
        if provisional is None:
            fs.write_json_atomic(flag, {"head": start_head, "status": "fail", "n": 0,
                                      "rc": problem, "ts": clock.now_utc()})
        elif provisional["status"] == "pass":
            provisional.update(status="fail", n=0, rc="evidence-drift")
            fs.write_json_atomic(flag, provisional)
        print(f"livegate: fail — {problem}. 릴리즈 차단.", file=sys.stderr)
        return 1
    print(f"livegate: $ {release.LIVEGATE_TEST_CMD}  (cwd={cwd})")
    # 자식 pytest 인코딩을 코드로 명시(env 워크어라운드 아님) — cp949 콘솔에서도 UTF-8.
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(release.LIVEGATE_TEST_CMD, shell=True, cwd=cwd, env=env,
                            capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    rc = result.returncode
    output = (result.stdout or "") + (result.stderr or "")
    if output.strip():
        print(output if output.endswith("\n") else output + "\n", end="")
    n = pytest_report._livegate_ran_count(output)
    head = git._git_head_at(cwd)
    passed = (rc == 0 and n == regression.LIVEGATE_RELEASE_PIN
              and (not platform_commands or (bool(start_head) and head == start_head)))
    status = "pass" if passed else "fail"
    flag.parent.mkdir(parents=True, exist_ok=True)
    record = {"head": head, "status": status, "n": n, "rc": rc, "ts": clock.now_utc()}
    if platform_commands:
        record.update(snapshot)
        record.update(head=start_head, cwd=cwd, platforms=cells)
        if passed:
            problem = release._release_evidence_problem(_home(), record, flag, start_head)
            if problem is not None:
                record.update(status="fail", rc="evidence-drift")
                passed = False
        elif head != start_head:
            record["rc"] = "head-drift"
    fs.write_json_atomic(flag, record)
    if passed:
        print(f"livegate: pass @ {head[:8] or '?'} "
              f"(release {n}/{regression.LIVEGATE_RELEASE_PIN} green) ✓")
        return 0
    if platform_commands and head != start_head:
        reason = f"HEAD drift ({start_head[:8]} → {head[:8] or '?'})"
    elif rc != 0:
        reason = f"release red (rc={rc})"
    else:
        reason = (f"수집 {n} ≠ pin {regression.LIVEGATE_RELEASE_PIN} — 마커 소실/wrong-cwd 의심"
                  " (수집 위장 차단)")
    print(f"livegate: fail @ {head[:8] or '?'} — {reason}. 릴리즈 차단.", file=sys.stderr)
    return 1


def _livegate_check(args: argparse.Namespace) -> int:
    """기록된 라이브 게이트가 `--rev <sha>` 에 대해 green 인지 검증한다 (보호훅 소비 채널).

    기록 존재 ∧ status==pass ∧ head==rev → rc0. 아니면 rc1 + 사유 3분화(기록 부재 / red /
    rev 불일치 — 각각 다른 메시지로 훅이 그대로 surface). fail-soft 아님(명확 rc).

    읽을 livegate.json 은 record 와 **동일 해소**(`_resolve_livegate_flag` — engine-root sidecar)
    를 거친다(단일 소스). 기본 위치(`pm_paths.livegate_flag`) 직독이 아니라, 어느 board.py 사본/cwd 로
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
    # 훅이 기록한 한 파일을 읽는다. hooksPath 미설정/훅 부재면 현행 `pm_paths.livegate_flag`(REPO/.local) 폴백 무변경.
    cwd = getattr(args, "cwd", None) or str(REPO)
    flag, mode = regression_store._resolve_livegate_flag(_home(), cwd)
    if mode == regression_store._LG_BROKEN:
        # 보호훅 활성(hooksPath+engine-root sidecar)인데 engine-root 가 무효(board.py 미해소) →
        # 기록 위치와 훅 read 위치가 갈릴 수 있어 pass/fail 판정을 신뢰 못 한다. 조용한 통과 대신
        # record 와 동형 fail-loud 거부(false-green/false-red 백스톱). engine-root
        # sidecar 수리 또는 `pm-config repo add` 재실행.
        print("livegate: fail — 보호훅 engine-root sidecar 무효 "
              "(hooksPath 설치됐으나 PM 홈 board.py 미해소) — 기록 위치와 훅 read 위치가 갈릴 수 "
              "있어 거부(false-green 차단). engine-root sidecar 수리 또는 "
              "`pm-config repo add` 재실행. 릴리즈 차단.", file=sys.stderr)
        return 1
    # 미해소 must-fix 잔여 — record 와 **같은 검사**를 소비 시점에도 한다(대칭). 기록은 HEAD 로
    # 키되지만 라운드 장부는 그 뒤로도 자란다: record 가 green 을 찍은 뒤 같은 HEAD 에서 새 반려
    # 라운드가 기록되면 옛 green 이 push 를 통과시킨다(check 만 스냅샷을 믿는 창). 판정을 소비
    # 지점에서 다시 물어 그 창을 닫는다 — 읽기 전용이라 기록은 손대지 않는다.
    try:
        # record 와 같은 장부 스냅샷으로 표식까지 현행화한다. check 가 라운드 writer 와 경합해
        # 옛 clear 를 새 blocked 위에 덮지 않도록 둘 다 review_rounds.lock 을 공유한다.
        problems = _sync_release_must_fix_marker(flag)
    except OSError as exc:
        print("livegate: fail — must-fix 잔여 표식을 원자 갱신할 수 없어 차단 "
              f"({type(exc).__name__}: {exc}). 장부/`.local` 권한을 복구한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return 1
    if problems:
        print(release._MUST_FIX_BLOCK_GUIDANCE.format(
            count=len(problems), items="\n".join(problems),
            ledger=release._release_rounds_ledger(flag)), file=sys.stderr)
        return 1
    if not flag.exists():
        print("livegate: 기록 없음 — `board.py livegate record` 필요 (릴리즈 차단).",
              file=sys.stderr)
        return 1
    data = json.loads(fs.read_text_shared(flag, encoding="utf-8"))
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
    if "platforms" in data:
        problem = release._release_evidence_problem(_home(), data, flag, rev)
        if problem is not None:
            print(f"livegate: platform snapshot 불일치({problem}) — 릴리즈 차단.", file=sys.stderr)
            return 1
    else:
        _core, _anchor, commands, problem = release._release_core_context(_home(), flag, os.path.abspath(cwd))
        if commands or problem is not None:
            print(f"livegate: platform snapshot 누락({problem or '현재 선언 플랫폼 존재'}) — "
                  "livegate record 재실행 필요. 릴리즈 차단.", file=sys.stderr)
            return 1
    print(f"livegate: green @ {str(rev)[:8]} ✓")
    return 0


def cmd_livegate(args: argparse.Namespace) -> int:
    """record = `pytest -m release` 실행+기록 / check = push HEAD green 검증 (보호훅용)."""
    if args.action == "record":
        return _livegate_record(args)
    return _livegate_check(args)


# ── 티켓 라운드 + 위임 티어 ────────────────────────────────────────
# 역할 산출 1건 = 라운드 파일 1개이고 그 자리는 명세 파일 밖(`tickets/rounds/<티켓>/`)이다.
# 역할 집합·사람용 라벨·파일명 문법의 권위는 사이드카 seam 이며 board 는 파생으로만 읽는다.


# 엔진 구현 패키지 이름 — `from pm_engine.<층> import <모듈>` 을 모듈 파일 의존으로 센다.


def cmd_claim(args: argparse.Namespace) -> int:
    # claim = 귀속 쓰기 — `--task` 가 없으면 어떤 쓰기·git 호출보다 먼저 멈춘다. 함께 준
    # `--repo`/`--slot` 은 그 task 가 보유한 폴더(claim 앵커)를 고른다.
    override = task_args._execution_actor_session_override(_home(), args)
    if override is None:
        sys.exit(task_args.TASK_REQUIRED.format(action="claim", command=f"board.py claim {args.id}"))
    # claimed_by 는 `<user>/<task>` — user 미상이면 task 만(graceful).
    assignee = lease_store.identity_tag(_home(), session_override=override,
                            user_override=getattr(args, "user", None))
    # 코드 트리 HEAD 박제 — 락 밖(읽기 전용 git 조회)이라 board mutation 창을 넓히지 않는다.
    claimed_rev = task_args._claim_head_rev(_home(), args)

    # strict claim 전체(prefetch → local move/dump → commit/push → confirm/rollback)를 두 락으로
    # 감싼다. 순서는 **board_git_lock → board_lock** 고정. board_lock은 network 동안도 유지해
    # rollback이 동시 in-place write를 옛 bytes로 덮는 창을 닫는다(짧은 전역 coordination 비용 수용).
    # `_cmd_claim_locked`는 두 락 보유 전제라 내부에서 어느 락도 재진입하지 않는다.
    with board_git.board_git_lock(_home()):
        with fs.board_lock(_home()):
            return _cmd_claim_locked(args, assignee, claimed_rev)


def _cmd_claim_locked(args: argparse.Namespace, assignee: str, claimed_rev: str) -> int:
    """`cmd_claim` 본체 — board_git_lock + board_lock 보유 전제(둘 다 재진입 금지)."""
    # claim STRICT 1단계: 선점 감지는 **읽기 전용**(fetch + 원격 트리
    # 직접 조회)이고, `pull --rebase` 는 원격이 앞섰을 때만 시도한다. 차단은 "원격이 앞섰고
    # 통합 불가" 로만 남고, 사유(dirty/rebase 충돌/offline/upstream 없음)와 진단 재료(behind·
    # 더러운 파일)를 구조체로 받아 **한 곳에서** 안내를 렌더한다(오판·이중출력 0).
    prefetch = board_claim._board_git_claim_prefetch(_home(), args.id)
    if prefetch.block is not None:
        print(board_claim._claim_block_message(args.id, prefetch), file=sys.stderr)
        return 1
    orig_head = prefetch.anchor

    # 아래 local write뿐 아니라 이 함수 앞의 prefetch와 뒤의 confirm/rollback도 호출자가 잡은
    # board_lock 안이다. 따라서 strict claim 전체가 다른 ticket writer에 하나의 순서로 관측된다.
    # On POSIX the atomic rename(2) alone was exclusive; on Windows os.rename is
    # NOT exclusive across concurrent processes, so without serialization every
    # concurrent claimer could "win" and duplicate the claim
    # the "rename is the lock" premise is Windows-invalid.
    # Holding board_lock makes the complete ticket write exclusive everywhere: once
    # the winner moves the ticket out of open/, every serialized loser's
    # lookup/load/move hits the now-missing path → FileNotFoundError. Those windows
    # all mean the same thing — we lost the claim
    # race — so all surface as one clean "claim race lost" (rc=1), never an
    # unhandled traceback. board_lock is OS-flock
    # based, so a crash mid critical-section auto-releases the lock (no stale
    # lock). Note: a dependency's own FileNotFoundError is caught below and is a
    # *normal* rejection ("dependency not found"), distinct from a claim race.
    try:
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status != "open":
            print(f"cannot claim {args.id}: currently in {status}/", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # Check dependencies
        for dep in fm.get("depends_on") or []:
            try:
                # 의존성 조회도 정확 일치만 — 이 판정이 claim 이동 여부를 정하므로, 폴백이
                # 답한 *다른* 티켓의 done 상태로 게이트가 열리면 안 된다(미해소 = 거부).
                dep_status, _ = ticket_store.find_ticket_for_mutation(_home(), dep)
            except FileNotFoundError:
                print(f"dependency {dep} not found", file=sys.stderr)
                return 1
            if dep_status != "done":
                print(f"dependency {dep} is {dep_status}/, not done",
                      file=sys.stderr)
                return 1

        # 설계 단계 게이트 — `design: required` 티켓은 설계 절을 완성하고
        # 필드를 `done` 으로 승격하기 전에는 집을 수 없다. 의존성 거부와 같은 자리
        # (이동 *전*·board_lock 안)라 티켓은 open/ 에 그대로 남는다.
        # `n/a`·필드 부재(구세대 티켓)는 무영향 — 판정은 `_design_issues` 단일
        # 깔때기(lint advisory·promote 게이트와 같은 규칙).
        design_verdict = design_rules._design_issues(args.id, body, fm.get("design"))
        if design_verdict:
            print(f"cannot claim {args.id}: 설계 단계 미완 — "
                  f"{design_verdict[0][2]}", file=sys.stderr)
            print("  → 설계 절(경계 실측·불변식·표면 상한·테스트 전략)을 채우고 설계 "
                  "검토를 마친 뒤 frontmatter 를 승격하라.", file=sys.stderr)
            return 1

        # 롤백 복원 기준 = 이동 *전* 원본 바이트
        # frontmatter 갱신(claimed_by/claimed_at)을 되돌릴 원본을 우리가 들고 있어야 한다.
        original_bytes = fs.read_bytes_shared(path)
        new_path = ticket_store.move_ticket(_home(), path, "claimed")
        fm["status"] = "claimed"
        fm["claimed_by"] = assignee
        fm["claimed_at"] = clock.now_utc()
        fm = task_args._frontmatter_with_claimed_rev(fm, claimed_rev)
        ticket_store.dump_ticket(new_path, fm, body)
    except FileNotFoundError:
        print(f"claim race lost on {args.id}", file=sys.stderr)
        return 1

    # claim STRICT 3·4단계: 로컬 claim 을 **그 티켓 경로만** commit 하고
    # push 가 성공해야 *비로소* 소유 확정. push 실패(non-FF/거부)면 `reset --soft <anchor>` +
    # 티켓 파일 역이동·원본 복원으로 **이 claim 이 만진 것만** 되돌리고(공유 트리의 무관한
    # 미커밋 작업은 불변) race-lost 로 명시 실패한다 — 거짓 소유를 남기지 않는다. board 가 별도
    # git 이 아니면 confirm 은 True(로컬 rename 만으로 확정·legacy 무변경).
    claim_files = board_git_rules._ClaimFiles(old=path, new=new_path, original=original_bytes)
    if not board_claim._board_git_claim_confirm(_home(), orig_head, claim_files):
        print(f"claim race lost on {args.id} (board push 충돌·소유 미확정·롤백됨)",
              file=sys.stderr)
        # board_lock 보유 중 public refresh_board()는 재진입이다. rollback까지 끝난 현재 상태를
        # locked 본체로 render하고 두 락을 함께 해제한 뒤 반환한다.
        board_dashboard._refresh_board_locked(_home())
        return 1
    print(f"claimed {args.id} as {assignee}")
    board_dashboard._refresh_board_locked(_home())
    return 0


def _pm_verified_channel_role(channel: str) -> str | None:
    """게이트 채널 → review_form 의 reviewer role 문자열 (모르는 채널이면 None → fail-closed).

    role 문자열은 review_form 이 단일 진실이라 board 에 사본을 두지 않는다."""
    return {
        GATE_CHANNEL_ADDITIONAL: review_form.ADDITIONAL_REVIEWER_ROLE,
    }.get(channel)


def _pm_verified_delegate_problem(
    tid: str, *, channel: str, entry: dict, checker_name: str,
) -> str | None:
    """티켓/채널 입력을 해소해 review_state 의 지정 pm-verified checker를 호출한다.

    preflight와 post-resolution evidence가 티켓/채널/표면 하한 해소를 복제하지 않게 하는 공용
    배선이다. 실제 판정은 checker_name이 가리키는 review_state 함수만 소유한다.

    추가 리뷰어 release 게이트는 `GATE_CHANNEL_ADDITIONAL` 로 호출하고, 판정 표면 하한은 **그
    채널 장부 entry 의 잔여 must-fix 건수**다. 채널 스코프 없이 티켓 전역을 보면 다른 채널의
    기계 확인이 이 게이트의 잔여를 여는 구멍이 생긴다."""
    found = ticket_store.find_ticket_exact(_home(), tid)
    if found is None:
        return f"티켓을 찾지 못했습니다: {tid}"
    _status, path = found
    try:
        spec_text = fs.read_text_shared(path, encoding="utf-8", newline="")
    except (OSError, UnicodeError) as exc:
        return f"티켓 명세를 읽을 수 없습니다: {exc}"
    reviewer_role = _pm_verified_channel_role(channel)
    if reviewer_role is None:
        return f"pm-verified 재검증 채널을 확정할 수 없습니다: {channel!r}"
    rounds = round_seed.load_rounds(pm_paths.tickets_dir(_home()), tid, ticket_text=spec_text)
    checker = getattr(review_state, checker_name)
    return checker(
        spec_text, rounds,
        reviewer_role=reviewer_role,
        surface_floor=review_rounds.gate_residual_must_fix(entry),
    )


def _pm_verified_evidence_problem(tid: str, *, channel: str, entry: dict) -> str | None:
    """`pm-verified` 발동·완료의 post-resolution 증거를 엄격 재검증한다."""
    return _pm_verified_delegate_problem(
        tid, channel=channel, entry=entry,
        checker_name="pm_verified_evidence_problem",
    )


def _gate_pm_verified_problem(
    gate: str, entry: dict, channel: str,
) -> Callable[[], str | None]:
    """게이트별 pm-verified 콜백 factory — 루프 클로저의 뒤늦은 바인딩을 피한다.

    `_gate_disposition_problem` 은 이 콜백을 같은 순회 안에서 즉시 호출하지만, 인자를 함수
    시그니처로 고정해 두면 나중에 호출 지점이 지연·재배치되더라도 클로저가 마지막 `gate`/`entry`
    를 참조하는 사고가 구조적으로 막힌다."""
    return lambda: _pm_verified_evidence_problem(gate, channel=channel, entry=entry)


def _review_problem(tid: str, checker_name: str, spec_text: str | None) -> str | None:
    """리뷰 잔여 판정 배선 — 명세와 리뷰 라운드 파일을 읽어 review_state 판정 함수에 넘긴다.

    판정 입력은 board 라운드 파일과 명세 PM 영역뿐이다. 판정 불능(티켓·명세·라운드 파일을
    읽지 못함)은 통과가 아니라 차단 사유다.
    """
    if spec_text is None:
        found = ticket_store.find_ticket_exact(_home(), tid)
        if found is None:
            return f"티켓을 찾지 못해 리뷰 잔여를 판정할 수 없습니다: {tid}"
        try:
            spec_text = fs.read_text_shared(found[1], encoding="utf-8", newline="")
        except (OSError, UnicodeError) as exc:
            return f"티켓 명세를 읽을 수 없어 리뷰 잔여를 판정할 수 없습니다: {exc}"
    checker = getattr(review_state, checker_name)
    try:
        rounds = round_seed.load_rounds(pm_paths.tickets_dir(_home()), tid, ticket_text=spec_text)
    except round_rules.RoundsError as exc:
        return f"리뷰 라운드를 읽지 못해 리뷰 잔여를 판정할 수 없습니다: {exc}"
    return checker(spec_text, rounds)


def _review_completion_problem(tid: str, spec_text: str | None = None) -> str | None:
    """완료 판정의 리뷰 잔여 — 처분·확인되지 않은 잔여가 있으면 사유(없으면 None).

    `spec_text` 를 주면 그 명세로 판정한다(완료 게이트는 lock 안에서 읽은 본문을 넘긴다).
    """
    return _review_problem(tid, "review_completion_problem", spec_text)


def _review_resolution_input_problem(tid: str) -> str | None:
    """종결 선검사의 리뷰 입력 — 돌려받은 리뷰가 없거나 확인 입력이 모자라면 사유."""
    return _review_problem(tid, "review_resolution_input_problem", None)


def _round_completion_problems(tid: str, spec_text: str) -> list[str]:
    """완료 게이트가 보는 라운드 판정 — **순번 유일성·연속성이 깨진 둘만 차단**한다.

    빈틈(`round-gap`)과 중복(`round-dup`)은 라운드가 지워졌거나 순서가 모호하다는 뜻이라
    완료 증거가 성립하지 않는다. 이름 문법 위반과 산출 없는 라운드(시드 그대로)는 정보로만
    낸다 — 미회수는 게이트가 아니라는 결정이고, 판정 코드의 단일 진실은 사이드카 seam 이다.
    """
    problems = round_seed.verify_rounds(pm_paths.tickets_dir(_home()), tid, ticket_text=spec_text)
    blocking_codes = (round_rules.PROBLEM_GAP, round_rules.PROBLEM_DUPLICATE)
    blocking: list[str] = []
    for item in problems:
        if item.code in blocking_codes:
            blocking.append(f"{item.code}: {item.detail}")
        else:
            print(f"ⓘ  {item.code}: {item.detail}", file=sys.stderr)
    return blocking


def _complete_gate(tid: str, args: argparse.Namespace,
                   body: str | None = None, *, spec_text: str | None = None,
                   ticket_path: Path | None = None) -> list[str]:
    """Verify completion housekeeping before a ticket may move to done/.

    Returns a list of *blocking* problems (empty = gate passes). Non-blocking
    concerns are printed to stderr as warnings from here.

    The regression check trusts the caller's `--tests-pass` assertion rather
    than re-running the (slow) suite.

    `body` = 완료 대상 티켓 본문(§3 DoD 체크 입력). None 이면 DoD·라운드·리뷰 잔여 검사를 건너뛴다 —
    본문 없이 기록 축만 묻는 호출자(단위 테스트 등)를 위한 것이고, 실 complete 경로
    (`cmd_complete`)는 항상 본문과 명세 전문을 넘긴다.

    `spec_text` = 명세 파일 전문(frontmatter 포함) — PM 판정 블록이 사는 자리다(없으면 본문으로
    대신한다). 라운드 산출 없음 판정의 입력은 아니다(그 판정은 라운드 파일 내용만 본다).
    """
    problems: list[str] = []
    id_re = re.compile(rf"\b{re.escape(tid)}\b")

    # 1. log/current.md must contain an entry for this ticket.
    if not args.allow_missing_log:
        log_text = fs.read_text_shared(LOG_FILE, encoding="utf-8") if LOG_FILE.exists() else ""
        if not id_re.search(log_text):
            problems.append(
                f"no log/current.md entry mentions {tid} — append one to "
                f"{pm_paths._rel_to_repo(_home(), LOG_FILE)} (or pass --allow-missing-log)")

    # 2. regression must be confirmed by the implementing session.
    if not (args.tests_pass or args.allow_untested):
        problems.append(
            "regression not confirmed — run the project test_cmd, then pass "
            "--tests-pass (or --allow-untested for a regression-irrelevant "
            "ticket)")

    # 3. DoD 전항이 체크(`- [x]`)되었거나 사유와 함께 이월(`- [>] … (이월: …)`)되어야 한다.
    #    미체크 항목이 남은 채 done 이 되면 그 항목은 보드에서 증발한다.
    if body is not None:
        problems.extend(_dod_open_items(body))

        problems.extend(_round_completion_problems(
            tid, body if spec_text is None else spec_text))

        # 5. 리뷰 잔여 — 마지막 판정 표면 리뷰 라운드의 must-fix 가 처분·확인돼야 한다(입력은
        #    리뷰 라운드 파일과 명세 PM 영역). 리뷰 라운드가 없는 티켓은 대상이 아니다.
        review_problem = _review_completion_problem(
            tid, body if spec_text is None else spec_text)
        if review_problem is not None:
            problems.append(review_problem)

    return problems


def cmd_complete(args: argparse.Namespace) -> int:
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status != "claimed":
            print(f"cannot complete {args.id}: in {status}/, must be claimed",
                  file=sys.stderr)
            return 1

        # 본문은 게이트 **전**에 읽는다 — §3 DoD 체크의 입력이다(이동 전 읽기라 차단 시
        # 티켓은 claimed/ 에 그대로 남는다). lookup부터 최종 dump까지 같은 lock이라 tier 가
        # 이 body snapshot 뒤에 끼어들어 유실되지 않는다.
        with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as handle:
            spec_text = handle.read()
        fm, body = ticket_store.load_ticket(path)

        # 묶음 멤버 lifecycle mutation은 정상 종결 파이프라인에 결속한다. ownership, DoD,
        # move/dump와 board-git sync보다 앞이라 거부 시 ticket/ledger/git write가 0이다.
        binding = getattr(args, "cluster_close", None)
        binding_problem = cluster_binding.cluster_complete_binding_problem(_home(), args.id, fm, binding)
        if binding_problem is None and binding is not None:
            # 결속 완료는 종결 파이프라인이 연 장부(closing)에서만 받는다 — 파이프라인 밖의 직접
            # 호출은 티켓만 done 으로 옮기고 장부·머지를 남긴다(복구 전용 반쪽 상태).
            ledger = cluster_store.load_cluster(_home(), binding)
            if ledger is not None and str(ledger.get("status") or "").strip() != "closing":
                owner = cluster_rules.cluster_id_for_name(binding)
                binding_problem = (
                    f"묶음 멤버의 완료는 종결 파이프라인 안에서만 한다 — {owner} 장부가 종결 중"
                    f"(closing)이 아니다(현재 {ledger.get('status')}). "
                    f"`ticket_finish.py --cluster {owner}` 로 닫아라")
        if binding_problem is not None:
            print(f"cannot complete {args.id}: {binding_problem}", file=sys.stderr)
            return 1

        # 소유 대조 — claim 한 정체성만 완료할 수 있다. 이동·기록 **전**이라 거부 시 티켓은
        # claimed/ 에 한 바이트도 안 바뀐 채 남는다.
        ownership = ticket_access._ticket_ownership(_home(), fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("complete", args.id, ownership), file=sys.stderr)
            return 1

        # Sync gate — refuse to mark done until housekeeping is verified.
        problems = _complete_gate(
            args.id, args, body, spec_text=spec_text, ticket_path=path)
        if problems:
            print(f"cannot complete {args.id}: sync gate failed —", file=sys.stderr)
            for msg in problems:
                print(f"  ✗ {msg}", file=sys.stderr)
            return 1

        new_path = ticket_store.move_ticket(_home(), path, "done")
        fm["status"] = "done"
        fm["completed_at"] = clock.now_utc()
        ticket_store.dump_ticket(new_path, fm, body)
    board_dashboard.refresh_board(_home())
    # 스코프 = 이 mutation 이 만진 두 경로만 — 공유 board 의 무관한 미커밋 작업이
    # 이 커밋에 실려 push 되지 않는다. best-effort sync는 기존 lifecycle 6 callsite
    # (new/promote/complete/block/unclaim/unblock) + 공용 helper 1곳(section-add/tier 공유)이
    # 모두 명시 path를 넘긴다(AST 메타가드가 7 callsite를 감사).
    ready = board_sync._board_git_sync_best_effort(_home(), f"complete {args.id}", (path, new_path))
    print(f"completed {args.id}{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in ("open", "claimed"):
            print(f"cannot block from {status}/", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — claimed 티켓(=소유자 있음)의 상태 변경은 그 소유 세션만. open 티켓은
        # claimed_by 가 없어 무대상이다(현행 무변경).
        ownership = ticket_access._ticket_ownership(_home(), fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("block", args.id, ownership), file=sys.stderr)
            return 1
        new_path = ticket_store.move_ticket(_home(), path, "blocked")
        fm["status"] = "blocked"
        # claimed_by/claimed_at 는 무접촉 — block 은 작업 중단이지 소유 포기가 아니다.
        # 소유 해제 문은 unclaim 하나뿐. `unblock` 이 이 값의 유무로 복귀 목적지를 정한다.
        note = f"\n## Blocked\n{args.reason} — {datetime.date.today().isoformat()}\n"
        ticket_store.dump_ticket(new_path, fm, body + note)
    board_dashboard.refresh_board(_home())
    ready = board_sync._board_git_sync_best_effort(_home(), f"block {args.id}", (path, new_path))
    print(f"blocked {args.id}: {args.reason}{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


# unclaim 이 받는 출발 상태 — `claimed`(소유 해제 후 open 복귀)와 `blocked`(상태 유지·소유만
# 해제)다. blocked 를 받는 이유: unblock 은 claimed_by 를 보고 claimed/ 로 복귀시키므로,
# blocked + 소유자 부재(퇴장한 세션) 조합은 unclaim 이 blocked 를 못 받으면 어떤 문으로도
# 풀리지 않는 교착이 된다.
_UNCLAIM_SOURCE_STATUSES: tuple[str, ...] = ("claimed", "blocked")


def cmd_unclaim(args: argparse.Namespace) -> int:
    takeover = bool(getattr(args, "takeover", False))
    reason = (getattr(args, "reason", None) or "").strip()
    # `--takeover` 는 귀속 쓰기다(본문 Takeover 절에 이전을 실행한 task 를 남긴다) — `--task` 가
    # 없으면 락·쓰기 전에 거부한다. takeover 없는 unclaim 은 소유 대조라 `--task` 가 선택이다.
    takeover_task = task_args._actor_session_override(_home(), args) if takeover else None
    if takeover and takeover_task is None:
        print(task_args.TASK_REQUIRED.format(
            action=f"unclaim {args.id} --takeover",
            command=f"board.py unclaim {args.id} --takeover --reason <사유>"), file=sys.stderr)
        return 1
    if takeover and not reason:
        # 사유 없는 강제 이전은 이력이 남지 않는다 — 락을 잡기 전에 거부한다(부작용 0).
        print(f"[중단] unclaim {args.id}: `--takeover` 는 `--reason <사유>` 가 필수다 "
              "— 소유자 부재 이전의 근거가 티켓 본문과 board-git 에 남아야 한다.",
              file=sys.stderr)
        return 1
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in _UNCLAIM_SOURCE_STATUSES:
            print(f"cannot unclaim {args.id}: in {status}/, must be "
                  f"{'/'.join(_UNCLAIM_SOURCE_STATUSES)}", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — 정상 경로는 소유자만. `--takeover` 는 **소유자 부재 이전의 유일한 문**
        # 이다(complete/block/unblock 엔 이 인수를 두지 않는다 — 문이 넷이면 소유 규칙이 네 벌이
        # 된다). 이전 사유는 본문 노트와 board-git 커밋 메시지 양쪽에 남긴다(board-git 없는
        # 채택자도 이력이 남아야 한다).
        ownership = ticket_access._ticket_ownership(_home(), fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok and not takeover:
            print(ticket_rules._ownership_rejection("unclaim", args.id, ownership), file=sys.stderr)
            return 1
        # blocked 는 상태를 유지한다 — unclaim 은 "소유 해제" 지 "차단 해제" 가 아니다.
        dest_status = "open" if status == "claimed" else status
        new_path = ticket_store.move_ticket(_home(), path, dest_status) if dest_status != status else path
        fm["status"] = dest_status
        previous_owner = fm.get("claimed_by")
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        # claimed_rev 도 함께 비운다 — 소유가 풀린 티켓에 옛 claim 앵커를 남기지 않는다.
        fm.pop("claimed_rev", None)
        if takeover:
            body = body + (f"\n## Takeover\n{reason} — {clock.now_utc()} "
                           f"(소유 `{previous_owner}` 해제 · 실행 task "
                           f"`{takeover_task}`)\n")
        ticket_store.dump_ticket(new_path, fm, body)
    board_dashboard.refresh_board(_home())
    message = (f"unclaim {args.id} (takeover: {reason})" if takeover
               else f"unclaim {args.id}")
    ready = board_sync._board_git_sync_best_effort(_home(), message, (path, new_path))
    detail = f" → {dest_status}/" if dest_status != "open" else ""
    takeover_note = f" (takeover: {reason})" if takeover else ""
    print(f"unclaimed {args.id}{detail}{takeover_note}"
          f"{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status != "blocked":
            print(f"cannot unblock {args.id}: in {status}/, must be blocked",
                  file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — claimed_by 를 보유한 blocked(=claimed 기원)는 그 소유 세션만 되돌린다.
        # 소유자 없는 open-기원 blocked 는 무대상(현행 무변경).
        ownership = ticket_access._ticket_ownership(_home(), fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("unblock", args.id, ownership), file=sys.stderr)
            return 1
        # claimed_by 보유 티켓(claimed-origin blocked)은 claimed/ 로 복귀시켜 소유 표식을
        # 보존한다 — 무조건 open 으로 옮기면 "open + claimed_by 잔존" 모순이 생겼다.
        # open-origin blocked(claimed_by 없음)는 현행대로 open/ 으로 돌아간다(왕복 불변식).
        dest_status = "claimed" if fm.get("claimed_by") else "open"
        new_path = ticket_store.move_ticket(_home(), path, dest_status)
        fm["status"] = dest_status
        ticket_store.dump_ticket(new_path, fm, body)
    board_dashboard.refresh_board(_home())
    ready = board_sync._board_git_sync_best_effort(_home(), f"unblock {args.id}", (path, new_path))
    print(f"unblocked {args.id} → {dest_status}/{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


# ── 처분 종결(discard) · 오처리 복구(reopen) ────────────────────────────────
#
# 종결은 두 종류다: `done`(구현 완료) · `discarded`(처분 — 병합·폐기). 처분을 complete 로
# 내보내면 done/ 이 "구현 0" 티켓을 담아 done 의 의미가 무너진다. 두 종결은 디렉토리로
# 갈리므로 조회·렌더·집계에서 자동으로 섞이지 않는다.
# 처분·복구 모두 기존 lifecycle 4단 프리미티브(move_ticket → frontmatter 수정 → refresh_board
# → _board_git_sync_best_effort)를 그대로 쓴다 — 별도 이동 장치·별도 sync 경로를 만들지 않는다.
# pseudo-status "draft" 도 출처에 넣는다 — draft(drafts_dir())는 번호만 배정된 채 board-git
# 밖에 있고, discard 가 아니면 그 번호를 설명하는 기록이 보드에 영영 안 남는다(사람이 `rm`
# 으로 치우던 자리). draft 는 `claimed_by` 가 없어 아래 소유 대조를 그대로 통과한다.
_DISCARD_SOURCE_STATUSES: tuple[str, ...] = (*ticket_rules.ACTIVE_STATUS_DIRS, "draft")


def cmd_discard(args: argparse.Namespace) -> int:
    """`discard <T-NNNN> <merged|dropped> --reason <사유>` — 처분 종결(비-완료 종결).

    병합(다른 티켓이 이 일을 흡수)·폐기(취소·대체)를 `done` 과 다른 디렉토리로 닫는다.
    병합 대상 티켓 ID 는 `--reason` 산문에 남긴다(기계 링크 필드는 두지 않는다 — 플래그가
    늘고 처분 종류를 인자로 또 갈라야 한다).
    """
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print(f"[중단] discard {args.id}: `--reason <사유>` 가 필수다 — 처분 근거 없이 "
              "티켓을 닫지 않는다.", file=sys.stderr)
        return 1
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in _DISCARD_SOURCE_STATUSES:
            print(f"cannot discard {args.id}: in {status}/, must be "
                  f"{'/'.join(_DISCARD_SOURCE_STATUSES)} "
                  f"(이미 종결된 티켓은 `reopen` 후 다시 처분하라)", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        # 소유 대조 — 남이 claim 해 진행 중인 티켓을 다른 세션이 처분으로 닫으면 그 작업이
        # 통째로 사라진다(complete 와 같은 축).
        ownership = ticket_access._ticket_ownership(_home(), fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            print(ticket_rules._ownership_rejection("discard", args.id, ownership), file=sys.stderr)
            return 1
        round_maintenance._discard_round_pending_notice(_home(), args.id)
        new_path = ticket_store.move_ticket(_home(), path, "discarded")
        fm["status"] = "discarded"
        fm["disposition"] = args.disposition
        fm["disposition_reason"] = reason
        # 출처 status 를 모든 discard 에 기록한다 — draft 출처면 reopen 이 `.drafts/` 로
        # 되돌아갈 목적지를 이 값으로 판정한다(부재 = 비-draft 출처 · 기존 discarded 자산과
        # 하위호환).
        fm["discarded_from"] = status
        note = (f"\n## Discarded\n{args.disposition} — {reason} — {clock.now_utc()}\n")
        ticket_store.dump_ticket(new_path, fm, body + note)
    board_dashboard.refresh_board(_home())
    ready = board_sync._board_git_sync_best_effort(
        _home(), f"discard {args.id} ({args.disposition}: {reason})", (path, new_path))
    print(f"discarded {args.id} as {args.disposition}: {reason}"
          f"{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


def cmd_reopen(args: argparse.Namespace) -> int:
    """`reopen <T-NNNN> --reason <사유>` — 종결(done·discarded)을 open(또는 draft)으로 되돌린다.

    잘못 처리된 종결을 되돌리는 **유일한 문**이다(옛 안내는 "홈 git 으로 revert" 뿐이었다).
    완료·처분 표식(`completed_at`·`disposition*`)과 소유 표식(`claimed_by`·`claimed_at`·
    `claimed_rev`)을 전부 비워 정상 형상으로 되돌리고, 사유·시각을 본문과 board-git 커밋에
    남긴다. 소유 대조는 하지 않는다 — 되돌릴 대상은 이미 소유가 끝난 종결 티켓이고,
    오처리 복구는 그 종결을 낸 세션이 이미 없을 때가 정상이다.

    `discarded_from: draft` 로 기록된 티켓(=discard 직전이 draft 였다)은 `open/` 이 아니라
    `.drafts/` 로 되돌아간다 — draft 본문은 placeholder 게이트를 안 거쳤으므로 open 진입을
    허용하면 board-git 에 미충전 본문이 노출된다(`open/` 승격은 `promote` 전용 문).
    `discarded_from` 부재(기존 자산 전부)는 현행대로 `open/`.
    """
    reason = (getattr(args, "reason", None) or "").strip()
    if not reason:
        print(f"[중단] reopen {args.id}: `--reason <사유>` 가 필수다 — 종결을 되돌린 근거가 "
              "남아야 한다.", file=sys.stderr)
        return 1
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in ticket_rules.TERMINAL_STATUS_DIRS:
            print(f"cannot reopen {args.id}: in {status}/, must be "
                  f"{'/'.join(ticket_rules.TERMINAL_STATUS_DIRS)}", file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        to_draft = fm.get("discarded_from") == "draft"
        dest_status = "draft" if to_draft else "open"
        new_path = ticket_store.move_ticket(_home(), path, ".drafts" if to_draft else "open")
        fm["status"] = dest_status
        fm["completed_at"] = None
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        for field in ("claimed_rev", "disposition", "disposition_reason", "discarded_from"):
            fm.pop(field, None)
        # open 진입은 `new`(승격)·`promote` 와 같은 자리에서 묶음에 귀속한다 — 판정 표면
        # (라운드 예산·완료 기록)은 장부 선언만 읽으므로 장부 없이 돌아온 티켓은 위임에서
        # 멈춘다. 필드가 이미 있으면 손대지 않는다: 그 장부가 이 티켓을 담고 있고, 덮으면
        # 그 장부의 선언(status·budget·tickets)이 지워지거나 중복 귀속이 남는다. `.drafts/`
        # 복귀는 귀속하지 않는다 — draft 명세는 공유 board 밖이다(`cmd_new` 와 같은 규칙).
        # 필드가 비었어도 **장부 파일이** 이 티켓을 담고 있으면(구세대 티켓·손으로 만든
        # 장부) 만들지 않고 역참조만 복구한다 — 같은 이름으로 새로 만들면 그 장부의
        # base_branch·status·close_step 이 기본값으로 덮인다(실측).
        cluster_ledger: Path | None = None
        if dest_status == "open" and not str(fm.get("cluster") or "").strip():
            of_record = cluster_store._cluster_of_record(_home(), args.id)
            if of_record is not None:
                fm["cluster"] = of_record
            else:
                fm["cluster"], cluster_ledger = board_cluster._cluster_auto_attach(_home(), args.id, args)
        note = f"\n## Reopened\n{reason} — {clock.now_utc()} (이전 상태 {status}/)\n"
        ticket_store.dump_ticket(new_path, fm, body + note)
    board_dashboard.refresh_board(_home())
    touched = ((path, new_path) if cluster_ledger is None
               else (path, new_path, cluster_ledger))
    ready = board_sync._board_git_sync_best_effort(
        _home(), f"reopen {args.id} (from {status}: {reason})", touched)
    print(f"reopened {args.id} (was {status}/): {reason}"
          f"{board_git._board_git_mutation_state_suffix(ready)}")
    return 0


INIT_GUIDE = """\
─ init 완료 — 이 clone setup 끝 ({mode}) ─
  3계층: 엔진(upstream) / 공유상태(main: board·status·log·ADR) / per-clone 로컬(pm_state·local.conf · git-ignored)
  규칙: 내구 진실은 공유 채널에만 · pm_state 는 버려도 되는 로컬 · 공유 파일 직접 난편집 금지
  ID:   {id_line}
"""
# 등록 repo 이름 canonical 형식 (`pm-config repo add` 의 검증과 같은 문법). init 은 이 형식을
# **차단 기준으로 쓰지 않는다** — 폴더명은 사용자 입력이 아니라 이미 존재하는 사실이고, 점·공백이
# 든 폴더에서 인자 0 온보딩(`pm_import.run_board_init`)을 rc≠0 으로 만들면 그게 더 큰 회귀다.
# 표를 깨는 문자만 차단하고, 형식 이탈은 `--repo` 처방과 함께 1줄로 표면화한다.
_INIT_REPO_NAME_CANONICAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _init_repo_name(args: argparse.Namespace) -> str | None:
    """init 이 areas.md 에 등록할 **repo 이름** — `--repo` 명시 > clone 루트 폴더명.

    repo 칼럼은 *이 clone 의 정체성*이고 prefix 칼럼은 *작업 카테고리*다 — 두 축은 섞이지
    않는다. prefix 를 repo 이름으로 승격하면 (a) 무prefix init 이 이미 만든 이 clone 의 행을
    못 찾아 사용자가 승인까지 지불한 `--prefix` 가 조용히 버려지고 (b) 이름이 다르면 한 물리
    repo 가 areas 2행이 되며 (c) 명시 `--repo` 가 조용히 무시된다.

    표를 깨는 문자(`|`·개행)나 빈 이름이면 **아무것도 쓰지 않고** None 을 돌려준다(호출부가 rc 1)
    — 그 경우의 처방은 `--repo <repo>` 명시다. canonical 형식(영숫자 시작·이후 영숫자/`_`/`-`)을
    벗어난 폴더명은 등록은 하되 1줄로 알린다(폴더 이름 `<repo>_<N>` 가독성·`repo add` 와의 정합).
    """
    name = (getattr(args, "repo", None) or REPO.name or "").strip()
    if not name or any(ch in name for ch in areas_rules._AREAS_CELL_BREAKERS):
        print(f"[중단] areas.md 에 등록할 repo 이름을 유도할 수 없다 (후보 {name!r}) — "
              "`--repo <repo>` 으로 명시하라 (등록·conf 어떤 것도 쓰지 않았다).",
              file=sys.stderr)
        return None
    if not _INIT_REPO_NAME_CANONICAL.match(name):
        print(f"  ⚠ 등록 repo 이름 {name!r} 이 canonical 형식(`[A-Za-z0-9][A-Za-z0-9_-]*`)을 "
              "벗어난다 — 그대로 등록하지만, 폴더 이름(`<repo>_<N>`)과 `pm-config repo` 조작이 "
              "읽기 어려워진다. `board.py init --repo <repo>` 으로 등록명을 지정할 수 있다.")
    return name


def cmd_init(args: argparse.Namespace) -> int:
    """clone 당 1회 setup — areas.md repo 행 등록 + local.conf + pm_state + pre-push 게이트 훅.

    multi-PM = N 세션 × M repo 한 개념 — *수가 1이냐 더냐*의 문제이고 N=1·M=1 도 그 부분집합이다.
    그래서 등록 경로가 갈리지 않는다: **`--prefix` 유무와 무관하게** 이 clone 의 repo 행을
    areas.md 에 항상 등록한다(등록 0 형상 없음 → 해소 체인이 레지스트리에서 끝난다).

    `--prefix` 는 협업(다중-사람)용이 아니라 **작업 카테고리** ID 네임스페이스다. 주면 그
    카테고리로 `T-<PREFIX>-NNN` 을, 안 주면 prefix 칼럼이 빈 채(=`none` 카테고리) `T-NNNN` 을
    발행한다 — 둘 다 1급 형상이라 `--prefix` 는 필수가 아니다(비대화형 온보딩이 인자 0으로 돈다).

    **등록 축은 둘이 분리돼 있다** — repo 칼럼 = 이 clone 의 정체성(`--repo` 명시 > 루트 폴더명),
    prefix 칼럼 = 작업 카테고리(`--prefix`). 멱등 키는 **이 clone 의 repo 행**이다:
      - 행 없음            → 행 1개 append(prefix 칼럼은 `--prefix` 또는 빈 칸).
      - 행 있음·`--prefix` 무 → no-op(행 추가 없음).
      - 행 있음·같은 카테고리 → no-op.
      - 행 있음·카테고리 비었음 → 그 행의 **prefix 셀만 갱신**(행 추가 아님·스키마 불변).
      - 행 있음·다른 카테고리 → 부작용 0 fail-loud(카테고리 변경은 `board.py prefix rename` 소관).
    `--area <설명>` 은 새 행을 만들며 카테고리를 함께 등록할 때만 필요하다.
    """
    requested_prefix = args.prefix
    prefix = requested_prefix
    prefix_precheck: str | None = None
    # 명시 --prefix sanity (예약어 `none`·형식 [a-z0-9_]+) — 위반이면 areas 등록·
    # local.conf write 어떤 부작용보다 앞에서 부작용 0 으로 거부(cmd_init 첫 문장 뒤).
    if prefix:
        reason = ticket_rules._validate_prefix(prefix)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
        canonical = areas_registry.require_prefix_user_ack(
            _home(), prefix,
            getattr(args, "user_ack", None),
            surface="board init --prefix",
        )
        if canonical is None:
            return 1
        prefix = canonical
        prefix_precheck = canonical
    # owner 기본값은 `--task` 다(환경변수·단일 대여에서 유도하지 않는다). `--repo` 는 등록명이다.
    owner_task = task_args._actor_session_override(_home(), args)
    repo_name = _init_repo_name(args)
    if repo_name is None:
        return 1
    area_message: str | None = None
    wrote_areas = False
    # areas 등록은 board_lock 안에서 fresh 3소스 판정을 선판정과 대조한 뒤 수행한다. 캐시된
    # 선판정 뒤 다른 세션이 다른 case를 만들었으면 areas/local.conf 모두 쓰기 전에 중단한다
    # (TOCTOU·부분 init 0).
    with fs.board_lock(_home()):
        if prefix:
            fresh = areas_registry.revalidate_prefix_user_ack(
                _home(), requested_prefix,
                getattr(args, "user_ack", None),
                prefix_precheck,
                surface="board init --prefix",
            )
            if fresh is None:
                return 1
            prefix = fresh
        # 멱등 키 = **이 clone 의 repo 행**. prefix 존재만으로 등록을 생략하면 사용자가 승인까지
        # 지불한 카테고리가 조용히 버려지고, prefix 를 repo 이름으로 쓰면 한 물리 repo 가 2행이 된다.
        _header, _rows = areas_store._parse_areas(_home())
        own_rows = [row for row in _rows if row.get("repo") == repo_name]
        if len(own_rows) > 1:
            print(f"[중단] areas.md 에 repo {repo_name!r} 행이 {len(own_rows)}개다(중복) — 어느 행이 "
                  "이 clone 인지 기계가 정할 수 없다. 한 행만 남기고 수동 정리한 뒤 다시 실행하라 "
                  f"({pm_paths._rel_to_repo(_home(), pm_paths.areas_file(_home()))}·`board.py lint` 의 areas-duplicate-repo 참조). "
                  "등록·conf 어떤 것도 쓰지 않았다.", file=sys.stderr)
            return 1
        # 다른 repo 행이 이미 그 카테고리를 쓰고 있으면 ID 네임스페이스가 겹친다 — 두 repo 가
        # 같은 `T-<PFX>-NNN` 시리즈를 나눠 쓰면 순번이 충돌한다(부작용 0 fail-loud).
        if prefix:
            taken = [row for row in _rows
                     if (row.get("prefix") or "").lower() == prefix.lower()
                     and row.get("repo") != repo_name]
            if taken:
                print(f"[중단] 카테고리 {prefix!r} 는 이미 repo {taken[0].get('repo')!r} 행에 "
                      "등록돼 있다 — 두 repo 가 같은 ID 네임스페이스를 나눠 쓰면 순번이 충돌한다. "
                      "다른 카테고리를 쓰거나 `board.py prefix rename` 으로 먼저 정리하라 "
                      "(등록·conf 어떤 것도 쓰지 않았다).", file=sys.stderr)
                return 1
        own_row = own_rows[0] if own_rows else None
        registered_cell = (own_row.get("prefix") or "").strip() if own_row else ""
        if own_row is None:
            if prefix and not args.area:
                print(f"새 prefix {prefix!r} 등록엔 --area <설명> 필요.", file=sys.stderr)
                return 1
            # owner = areas.md 등록 식별자(registrant) — 협업 소유자가 아니라 등록 출처 표식.
            # `--owner` > `--task` > 빈 칼럼(미상) 순이고 환경변수·단일 대여에서 유도하지 않는다.
            owner = args.owner or owner_task or ""
            area_owner = lease_store.user_name(_home(), getattr(args, "user", None))
            # 자기 행(폴더 이름 등록)이면 git·base 를 함께 싣는다 — 안내 명령(`pm-config worktree
            # add <repo>`)이 base 와 clone 원 주소를 이 행에서 읽는다. `--repo` 로 폴더와 다른 이름을
            # 등록하면 싣지 않는다(PM 홈 주소가 제품 행에 적히는 일 방지).
            self_row = repo_name == REPO.name
            coordinates = areas_registry.self_repo_coordinates(_home()) if self_row else None
            git, base = coordinates or ("", "")
            areas_store._areas_append_locked(_home(), prefix or "", owner, repo=repo_name, area_owner=area_owner,
                                 git=git, base=base)
            wrote_areas = True
            ao_surface = (area_owner if area_owner else
                          "(미상 — local.conf identity.user= / git user.email 미설정)")
            area_message = (f"✓ areas.md 등록: repo={repo_name} | "
                            f"prefix={prefix or '(빈 칼럼 · none 카테고리)'} | "
                            f"owner={owner or '(미상 — --owner·--task 미지정)'} | "
                            f"area_owner={ao_surface} | git={git or '(없음)'} | base={base or '(없음)'}")
            if self_row and coordinates is None:
                print(areas_rules._SELF_COORDINATES_UNKNOWN, file=sys.stderr)
        elif not prefix:
            area_message = (f"repo {repo_name!r} 이미 등록됨 (areas.md) — 행 추가 없음 "
                            f"(카테고리 {registered_cell or '(빈 칼럼 · none)'}).")
        elif registered_cell.lower() == prefix.lower():
            area_message = (f"repo {repo_name!r} 이 이미 카테고리 {registered_cell!r} 로 "
                            "등록됨 (areas.md) — 변경 없음.")
        elif registered_cell:
            print(f"[중단] repo {repo_name!r} 행은 이미 카테고리 {registered_cell!r} 다 — "
                  f"{prefix!r} 로 바꾸려면 기발행 ID 까지 옮기는 `board.py prefix rename "
                  f"{registered_cell} {prefix}` 를 쓰라(init 은 카테고리를 갈아끼우지 않는다). "
                  "등록·conf 어떤 것도 쓰지 않았다.", file=sys.stderr)
            return 1
        else:
            # 이 clone 의 행은 있는데 카테고리 칼럼만 비었다(무prefix init 뒤 카테고리 지정) —
            # **행 추가가 아니라 셀 갱신**이다(스키마 불변·이미 board_lock 보유 → locked 변형).
            areas_store._areas_set_cell_locked(_home(), repo_name, "prefix", prefix)
            wrote_areas = True
            area_message = (f"✓ areas.md 카테고리 등록: repo={repo_name} | "
                            f"prefix={prefix} (기존 행의 prefix 칼럼 갱신·행 추가 없음)")
        conf_store._write_init_local_conf(_home())
    areas_registry.invalidate_known_prefixes_cache()
    if wrote_areas:
        # 등록 행은 board git 의 공유 파일이다 — 파일만 쓰고 두면 board 가 dirty 로 남아
        # (`M areas.md`) 온보딩 다음 단계가 그 잔여를 사용자 편집으로 만난다. `pm-config repo
        # add`/`repo protected` 와 **같은 스코프 채널**로 이 mutation 이 만진 경로만 기록한다
        # (board 가 별도 git 이 아니면 no-op·best-effort 라 실패해도 init 을 되돌리지 않는다).
        # board_lock 밖에서 부른다(락 순서: board_git_lock → board_lock).
        board_sync._board_git_sync_best_effort(_home(), "init areas 등록", (pm_paths.areas_file(_home()),))
    print(area_message)
    print("✓ local.conf: py·test_cmd·ctx 비율 (세션·prefix 는 conf 키가 아니다 — "
          "세션=lease 장부 · prefix=areas.md)")
    # 병합 경로가 손대지 않은 기존 키(오타·폐기)도 같은 목록·같은 함수로 표면화한다 —
    # `board.py lint` 를 따로 실행하지 않아도 온보딩 시점에 1줄로 보이게(사본 없음).
    for _, _, detail in conf_lint.lint_local_conf_keys(_home()):
        print(f"  ⚠ {detail}")
    if not PM_STATE_FILE.exists() and PM_STATE_TEMPLATE.exists():
        # `{{DATE}}` 의 소유자는 **생성 시점**이다 — 템플릿은 채택자 디스크에 토큰-form
        #   으로 남아야 pm_update 의 manifest byte-copy 와 진동하지 않는다. 그러니 그 템플릿으로
        #   산출물을 만드는 이 지점이 오늘 날짜를 채운다(task_store.ensure_task_pm_state 의
        #   task pm_state 렌더와 같은 규칙·소유 선언은 pm_import.CONSUMPTION_TIME_TOKENS).
        seed = fs.read_text_shared(PM_STATE_TEMPLATE, encoding="utf-8").replace(
            "{{DATE}}", datetime.date.today().isoformat())
        PM_STATE_FILE.write_text(seed, encoding="utf-8", newline="\n")
        print(f"✓ pm_state.md 생성 ({pm_paths._rel_to_repo(_home(), PM_STATE_TEMPLATE)} 에서)")
    # 미설치면 사유를 `install_pre_push_hook` 이 stderr 에 남긴다 — 여기서는 설치한 자리를 밝힌다.
    if pre_push_hook.install_pre_push_hook(_home()):
        print("✓ pre-push 게이트 훅 설치 — 자기 저장소의 .git/hooks "
              "(lint 게이트는 항상 · 회귀 게이트는 tests/ 있는 트리만)")
    # board submodule 이 분리된 형상이면 ignore=all 자동 설정 — design(코드) git 이
    # board PM-commit 으로 오염되지 않게(누출 0). 미분리/git 부재면 no-op(fail-soft·무영향).
    if _configure_board_submodule():
        print("✓ board submodule ignore=all 설정 (코드 git 누출 0)")
    # areas.md `merge=union` 배포는 여기서 하지 않는다 — init 의 board git 기록은
    # `_board_git_sync_best_effort(..., (areas_file(),))` 로 **areas.md 경로만** stage/commit 하는
    # scoped 채널이라 `.gitattributes` 는 그 범위 밖이다. 여기서 파일만 쓰면 board 가
    # dirty(`?? .gitattributes`)로 남아 다음 `claim` 이 STRICT dirty 가드에 막힌다(엔진이 만든
    # 파일을 사용자 편집으로 오인·clone→init→claim 온보딩 직격) — 미배포 근거는 그대로 유효하다.
    # 배포는 `_board_git_stage_and_commit`(write→stage→commit 이 한 호출에 닫힘) **단일 채널**로 한다.
    # 완료 안내의 ID 포맷은 **이 clone 에서 `board.py new` 가 실제로 발행할 값**이다 —
    # 등록 결과가 아니라 해소 체인(`cmd_new` 와 같은 `id_prefix`)에서 뽑는다.
    # 등록이 생략/거부된 형상에서 발행되지 않을 포맷을 성공 메시지로 내던 거짓 안내 차단.
    issued = areas_store.id_prefix(_home(), None)
    # `id_prefix(None)` 의 미해소(None)는 두 형상을 가린다 — (a) 등록 ≤1(count-based 로 곧
    # 무prefix `T-NNNN` 이 발행) (b) 등록 ≥2(모호). `cmd_new` 는 후자에서
    # 자신의 ≥2 가드로 fail-loud 하므로 무명시 `T-NNNN` 은 실제로 발행되지 않는다 — 완료 안내가
    # 그 포맷을 성공 메시지로 내면 거짓이다(같은 `registered_prefixes()` 로 `cmd_new` 와 lockstep).
    ambiguous = issued is None and len(areas_store.registered_prefixes(_home())) >= 2
    if ambiguous:
        mode = f"repo {repo_name} · 카테고리 모호(등록 카테고리 ≥2)"
        id_line = ("무명시 `board.py new` 발행 불가 — 등록 카테고리가 여럿이다. "
                   "`board.py new --prefix <PFX> --user-ack <PFX>` 로 "
                   "명시하거나 task prefix 를 지정하라(`pm-config task prefix <task> <PFX>`).")
    else:
        mode = f"repo {repo_name} · 카테고리 {issued or 'none(무prefix)'}"
        idfmt = f"T-{issued}-NNN" if issued else "T-NNNN (none 카테고리)"
        id_line = f"`board.py new` 로 {idfmt} 발행"
    print(INIT_GUIDE.format(mode=mode, id_line=id_line))
    # 이 도구는 lease 장부의 writer 가 아니다(단일 writer 규약) — 작업 폴더를 만드는 것은
    # `pm-config worktree add` 다. 채택자가 그 다음 순서를 모른 채 남지 않도록 상태만 알린다.
    if not lease_store.leased_sessions(LEASES_FILE):
        print(
            "  ⚠ 이 홈은 아직 작업 폴더가 없다 — 귀속 조작(claim·complete)이 세션 미해소로 "
            "중단된다. `pm-config worktree add <repo> --user-ack <repo>` 로 먼저 만들어라.",
            file=sys.stderr,
        )
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
    canonical_ao = areas_rules._AREAS_COLUMNS.index("area_owner")
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
        cells = areas_rules._split_areas_row(body)
        if cells is None:
            # 비-table 줄(주석·빈 줄·구분선)은 기본 verbatim. 단 업그레이드 모드에서 헤더
            # 직후 첫 구분선(`|---|`)은 새 칼럼 수에 맞춰 교체한다(헤더 폭과 정합).
            if upgrade and not sep_replaced and areas_rules._AREAS_SEP_RE.match(body.strip()):
                out.append("|" + "|".join("---" for _ in range(sep_cols)) + "|" + nl)
                sep_replaced = True
            else:
                out.append(line)
            continue
        if header_cells is None:
            header_cells = [c.lower() for c in cells]
            # 공용 헬퍼 — 칼럼이 있으면 그 인덱스(헤더 verbatim), 없으면 구-헤더 업그레이드
            # 계획(canonical 8칼럼 교체 또는 기존 헤더 끝 append)을 함께 돌려준다.
            ao_idx, new_header, sep_cols = areas_store._areas_column_index(header_cells, "area_owner")
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
        idx = areas_store._areas_row_cell_index(
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


def _migrate_ticket_fm(fm: dict, user: str, task: str, status: str,
                       registered: set[str]) -> tuple[list[str], list[str]]:
    """티켓 frontmatter 를 *제자리* backfill 하고 `(변경 요약, 바꾸지 않은 타 사용자 토큰)` 을 반환.

    멱등·비파괴: 부재/빈 `created_by` 만 user 로, 슬롯-only(`/` 없음·non-empty) `claimed_by`
    만 `<user>/<토큰>` 으로 채운다. 기존 non-empty 값(이미 `<user>/<토큰>` 형태 포함)은 불변.
    키 순서는 dict 제자리 수정이라 보존(없던 created_by 추가는 끝에 붙음 → dump 순서 유지).

    **슬롯 세션 토큰 이전**: 비종결(open·claimed·blocked)과 draft 의 `created_by`·`claimed_by` 중
    마지막 `/` 뒤 토큰이 등록 repo 의 폴더 이름 패턴(`<repo>_<N>`)인 값은 사용자 부분이 실행
    사용자이거나 없으면 `<user>/<task>` 로 옮긴다. 다른 사용자의 토큰은 바이트 그대로 두고 두 번째
    목록으로 돌려준다(그 사용자가 자기 task 로 실행한다). done·discarded 는 옮기지 않는다.
    """
    changes: list[str] = []
    foreign: list[str] = []
    created_by = fm.get("created_by")
    # 부재(키 없음·None)거나 빈/공백 문자열이면 backfill. 기존 non-empty 값은 불변(멱등).
    if not (str(created_by).strip() if created_by is not None else ""):
        fm["created_by"] = user
        changes.append(f"created_by → {user}")
    if status not in ticket_rules.TERMINAL_STATUS_DIRS:
        for field in ("created_by", "claimed_by"):
            value = fm.get(field)
            if not (isinstance(value, str) and value.strip()):
                continue
            owner_user, _sep, token = value.strip().rpartition("/")
            if not identity_args.is_reserved_task_name(token, registered):
                continue
            if owner_user and owner_user != user:
                foreign.append(f"{field}={value}")
                continue
            fm[field] = f"{user}/{task}"
            changes.append(f"{field} {value} → {fm[field]}")
    cb = fm.get("claimed_by")
    if isinstance(cb, str) and cb.strip() and "/" not in cb:
        # 슬롯-only(구 형태·user 차원 없음) → user 차원 prepend(슬롯값 보존).
        fm["claimed_by"] = f"{user}/{cb}"
        changes.append(f"claimed_by {cb} → {user}/{cb}")
    return changes, foreign


def _print_migrate_foreign(tid: str, status: str, foreign: list[str]) -> None:
    """바꾸지 않은 타 사용자 슬롯 토큰을 stderr 로 알린다(rc 불변)."""
    for item in foreign:
        print(f"  {tid} ({status}/): {item} — 다른 사용자의 옛 슬롯 표기라 바꾸지 않는다"
              "(그 사용자가 `board.py migrate-identity --task <task>` 로 옮긴다)", file=sys.stderr)


def _migrate_scan_dirs(statuses: tuple[str, ...]) -> list[tuple[str, Path]]:
    """migrate 스캔 디렉토리 — 요청 status 디렉토리 + draft 격리 디렉토리(pseudo-status `draft`)."""
    return [(status, pm_paths.tickets_dir(_home()) / status) for status in statuses] + [("draft", pm_paths.drafts_dir(_home()))]


def _migrate_identity_preview(
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """--dry-run 경로: read-only 스캔 + per-file 미리보기. 락·쓰기 0.

    어떤 파일도 옮기거나 쓰지 않으므로 board_lock 을 *전혀* 잡지 않는다(read-only 보장).
    반환 `(total, wrote)` 에서 wrote 는 항상 False(쓰기 없음 → refresh_board 미호출).
    """
    total = 0
    # areas.md 미리보기(읽기 전용).
    af = pm_paths.areas_file(_home())
    if af.exists():
        text = fs.read_text_shared(af, encoding="utf-8")
        _, area_changes = _migrate_areas_text(text, user)
        for c in area_changes:
            print(f"  areas.md: {c}")
        total += len(area_changes)
    # 티켓 미리보기 — glob 스캔 후 변경 산출만(쓰기 없음).
    registered = areas_store.registered_repos(_home())
    for status, directory in _migrate_scan_dirs(statuses):
        for p in sorted(directory.glob("T-*.md")):
            fm, _body = ticket_store.load_ticket(p)
            changes, foreign = _migrate_ticket_fm(fm, user, slot, status, registered)
            tid = fm.get("id") or p.stem
            _print_migrate_foreign(tid, status, foreign)
            if not changes:
                continue
            total += len(changes)
            for c in changes:
                print(f"  {tid} ({status}/): {c}")
    return total, False


def _migrate_areas_apply(user: str) -> tuple[int, bool]:
    """areas.md 의 빈 area_owner backfill (read→transform→write)을 board_lock 으로 보호.

    areas.md 는 `areas_append`가 *진짜* 공유 mutation 으로
    board_lock 을 잡고 쓰는 단일 파일이라, 본 RMW 의 write 도 같은 락으로 직렬화해야
    동시 repo-add 의 lost-update(전체 write_text 가 append 된 row 를 클로버)를 막는다.
    이 락은 areas 구간 *한정* — 티켓 backfill은 뒤의 독립 board_lock 구간에서 수행한다.

    **재진입 금지**: board_lock 은 OS flock(non-reentrant). 락 안에서 부르는 IO
    (`_migrate_areas_text`·AREAS_FILE read/write)는 락을 다시 잡지 않는다. 반환 `(total, wrote)`.
    """
    af = pm_paths.areas_file(_home())
    if not af.exists():
        return 0, False
    total = 0
    wrote = False
    with fs.board_lock(_home()):
        text = fs.read_text_shared(af, encoding="utf-8")
        new_text, area_changes = _migrate_areas_text(text, user)
        for c in area_changes:
            print(f"  areas.md: {c}")
        if area_changes:
            total += len(area_changes)
            if new_text != text:
                af.write_text(new_text, encoding="utf-8", newline="\n")
                wrote = True
    return total, wrote


def _migrate_tickets_apply(
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """티켓 backfill — scan/read/transform/relookup/atomic dump 전체를 board_lock으로 직렬화.

    migrate는 status 이동 없이 frontmatter만 고치는 in-place ticket writer다. tier writer가 값을
    기록한 뒤 migration이 lock 밖의 옛 ``fm/body`` snapshot을 dump하면 그 기록이 유실된다.
    따라서 lifecycle/tier writer와 같은 board_lock을 후보 scan부터 마지막 dump까지 유지한다.

    lock 안 재조회는 방어적으로 유지한다. 엔진 writer는 모두 같은 lock을 따르므로 정상 경합에서는
    경로가 바뀌지 않지만, 수동/구버전 writer가 경로를 옮긴 경우 stale write 대신 skip+loud한다.
    dump는 temp+``fs.atomic_replace`` 원자 교체다. refresh는 호출자 ``cmd_migrate_identity``가 이 lock을
    놓은 뒤 1회 수행한다(재진입 없음). board-git sync/다른 lock 호출은 이 함수에 없다.
    반환 ``(total, wrote)``.
    """
    total = 0
    wrote = False
    registered = areas_store.registered_repos(_home())
    with fs.board_lock(_home()):
        for status, directory in _migrate_scan_dirs(statuses):
            for p in sorted(directory.glob("T-*.md")):
                try:
                    fm, body = ticket_store.load_ticket(p)
                except FileNotFoundError:
                    print(f"  skip {p.name}: 스캔 후 사라짐(수동/구버전 writer 이동) — "
                          "backfill 안 함", file=sys.stderr)
                    continue
                changes, foreign = _migrate_ticket_fm(fm, user, slot, status, registered)
                tid = fm.get("id") or p.stem
                _print_migrate_foreign(tid, status, foreign)
                if not changes:
                    continue
                try:
                    cur_status, cur_path = ticket_store.find_ticket_for_mutation(_home(), tid)
                except FileNotFoundError:
                    print(f"  skip {tid}: 재조회 시 없음(수동/구버전 writer 완료/삭제) — "
                          "backfill 안 함", file=sys.stderr)
                    continue
                if cur_path != p:
                    print(f"  skip {tid}: {status}/ → {cur_status}/ 이동됨(쓰기 직전) — "
                          f"stale 쓰기 안 함", file=sys.stderr)
                    continue
                total += len(changes)
                for c in changes:
                    print(f"  {tid} ({status}/): {c}")
                ticket_store.dump_ticket_atomic(cur_path, fm, body)
                wrote = True
    return total, wrote


def _migrate_identity_apply(
        user: str, slot: str, statuses: tuple[str, ...]) -> tuple[int, bool]:
    """비-dry-run 경로: areas + 티켓 backfill을 서로 독립된 board_lock 구간으로 수행.

    - **areas.md**: `_migrate_areas_apply` 가 board_lock 으로 RMW 를 보호한다(`areas_append`
      와의 lost-update 방지·진짜 공유 mutation).
    - **티켓**: `_migrate_tickets_apply`가 scan/read/transform/relookup/dump 전체를 같은 board_lock으로
      보호해 lifecycle/tier writer와 stale overwrite 없이 직렬화한다.

    **재진입 금지**: areas와 ticket helper가 각각 락을 획득하고 반환한 뒤 다음 helper로 간다.
    board.md 재생성(`refresh_board` — 자체 board_lock)은 **호출자(`cmd_migrate_identity`)가
    락 밖에서 1회** 한다(데드락 방지). 반환 `(total, wrote)`.
    """
    area_total, area_wrote = _migrate_areas_apply(user)
    ticket_total, ticket_wrote = _migrate_tickets_apply(user, slot, statuses)
    return area_total + ticket_total, area_wrote or ticket_wrote


def cmd_migrate_identity(args: argparse.Namespace) -> int:
    """backfill — areas area_owner·ticket created_by/claimed_by.

    `--user` override > `user_name()`(local.conf identity.user= / git config user.email). 미해소(None)면
    abort(식별자 없이는 backfill 불가). `--dry-run` 은 쓰기 0·per-file 미리보기. `--scope`
    active(open+claimed) | all(기본·done 포함). 멱등(빈 필드만)·비파괴(순서/body/표 보존).

    `--task` 는 필수다(귀속 쓰기). 비종결·draft 티켓의 옛 슬롯 세션 토큰(`<repo>_<N>`)은 실행
    사용자 몫(사용자 부분이 같거나 없음)이면 `<user>/<task>` 로 옮기고, 다른 사용자 몫은 그대로
    두고 stderr 로 알린다(rc 0). 그 밖의 슬롯-only `claimed_by`(`pm-1` 같은 `/` 없는 값)는 user
    차원만 prepend 하고 *기존 토큰을 보존*한다(`pm-1` → `<user>/pm-1`). done·discarded 는 사용자
    backfill 외에 바꾸지 않는다.

    **단일-세션 업그레이드 op (동시성 모델)**: 일회성 전체 backfill이라는 운영 성격은 유지한다.
    다만 areas와 티켓 모두 board_lock writer이며, 티켓 scan/read/transform/dump는 lifecycle/tier와
    하나의 순서로 관측된다. 두 helper의 lock 구간은 중첩되지 않고 board.md 재생성은 모두 끝난 뒤
    lock 밖에서 1회 한다.
    """
    user = lease_store.user_name(_home(), getattr(args, "user", None))
    if not user:
        print("[중단] user 식별자 미해소 — `--user <id>` 를 주거나 local.conf identity.user= / "
              "git config user.email 를 설정하라(식별자 없이는 backfill 불가).",
              file=sys.stderr)
        return 1
    # 이전 대상 task = 귀속 쓰기(claimed_by·created_by 에 박음) — `--task` 가 없으면 어떤 쓰기보다
    # 먼저 멈춘다(환경변수·단일 대여 값으로 대신하지 않는다).
    slot = task_args._actor_session_override(_home(), args)
    if slot is None:
        sys.exit(task_args.TASK_REQUIRED.format(action="migrate-identity",
                                      command="board.py migrate-identity"))
    dry_run = bool(getattr(args, "dry_run", False))
    scope = getattr(args, "scope", "all") or "all"
    statuses = ("open", "claimed") if scope == "active" else ticket_rules.STATUS_DIRS

    tag = "[dry-run] " if dry_run else ""
    # scope 문구 정합: migrate 는 소유 무관 *전 티켓*을 스캔·backfill 한다
    # (all=done 포함 전체·active=open+claimed, 둘 다 draft 포함). `list --mine`/`--task`(=현재-사용자
    # 뷰)와 스캔 대상이 다르므로 "전체 스캔" 을 명시해 두 기준이 어긋나 보이는 오독을 없앤다.
    scope_note = "전체 스캔·소유 무관" if scope == "all" else "open+claimed 스캔·소유 무관"
    print(f"{tag}migrate-identity — user={user} · task={slot} · scope={scope} ({scope_note})")

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
        board_dashboard.refresh_board(_home())
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    # 발행 = 귀속 쓰기(created_by = `<user>/<task>`). `--task` 가 없으면 prefix 판정·draft 생성·
    # ID 발행 어떤 부작용보다 먼저 멈춘다. `--task` 는 정체성 깔때기에서 task 명 검증을 받고,
    # 함께 준 `--repo`/`--slot` 은 그 task 가 보유한 폴더를 고르는 위치 인자일 뿐이다.
    actor_session = task_args._execution_actor_session_override(_home(), args)
    if actor_session is None:
        sys.exit(task_args.TASK_REQUIRED.format(action="new", command='board.py new "<제목>"'))
    # 명시 --prefix sanity (예약어 `none`·형식 [a-z0-9_]+) — 위반이면 ID 스캔·파일
    # 발행 전에 부작용 0 으로 즉시 거부. 유도/count-based 로 해소된 legacy prefix 는 검증하지
    # 않는다(기존 발행분 존중) — 사용자가 *명시*한 override 만 입력측 sanity 대상이다.
    requested_override = getattr(args, "prefix", None)
    override = requested_override
    if override:
        reason = ticket_rules._validate_prefix(override)
        if reason:
            print(f"[중단] {reason}", file=sys.stderr)
            return 1
    else:
        # 우선순위: --prefix 명시 > task 설정 prefix(`task prefix`·검증·기록) > count-based.
        task_pfx = lease_store.task_prefix(actor_session, LEASES_FILE)
        if task_pfx:
            override = task_pfx
    # 명시/task 값은 우선순위상 이미 최종 후보다. areas-only fold lookup(`id_prefix(override)`)으로
    # 먼저 canonical화하지 않고 아래 **공통 3소스 funnel**에 원 표기 그대로 넘긴다. 그래야
    # 오염된 areas `AAA`/`aaa`의 set 순회가 명시 경로 진단 입력을 다시 비결정적으로 고르지 않는다.
    prefix = override if override else areas_store.id_prefix(_home(), None)
    prefix_precheck: str | None = None
    prefix_surface = "board new --prefix" if requested_override else "board new derived prefix"
    if prefix is not None:
        # 명시/유도 분기 **뒤의 최종 발행 prefix**가 canonical 판정의 단일 funnel이다.
        # task·단일 areas 유도값도 명시 --prefix와 같은 3소스 snapshot을
        # 통과해야 한다. 여기서 복수 case면 같은 fail-loud 진단으로 막고, 단일 case면 그
        # canonical 표기를 발행값으로 고정한다. 신규 명시값만 user_ack가 실제 의미를 가지며,
        # 유도값은 자신을 제공한 3소스 중 하나에 이미 존재하므로 승인 없이 existing으로 통과한다.
        canonical = areas_registry.require_prefix_user_ack(
            _home(), prefix,
            getattr(args, "user_ack", None),
            surface=prefix_surface,
        )
        if canonical is None:
            return 1
        prefix = canonical
        prefix_precheck = canonical
    # multi-repo 네임스페이스 가드는 **레지스트리 *존재*가 아니라 등록 repo *개수*** 기준이다.
    # 등록 prefix 가 ≥2 면 진짜 ID 충돌 가능성이 있으니 prefix 필수(namespace 강제). 등록이
    # ≤1(0=레지스트리 부재/빈·1=단일 self-host) 이면 충돌이 없으므로 무prefix legacy `T-NNNN` 을
    # 허용한다(prefix optional) — 단일 self-host 가 areas.md 1행만으로 multi-PM 마찰을 떠안지
    # 않게(단일 등록 repo 케이스). 명시 prefix 가 *주어지면* 그건 그대로
    # 존중해 prefixed ID 를 발행한다 — ≤1 라도 사용자가 골랐으면 따른다.
    #
    # **명시 prefix 의 "등록돼 있어야 한다" 가드는 없다**. prefix 는 repo
    # 네임스페이스가 아니라 작업 카테고리이므로 미등록 라벨도 사용자가 그 대상값을
    # `--user-ack`으로 승인하면 즉석 신설할 수 있다. 입력측 sanity와 신설 승인은 위에서
    # `_validate_prefix` + `require_prefix_user_ack` 두 단계로 닫히며, 기존 fold-매치는 무마찰이다.
    # 아래 ≥2 가드는 별개 — 등록 repo 가 ≥2 인데 prefix 를 *안* 준 implicit 모호성 방지다
    # (유도 체인·미해소 fail-loud).
    registered = areas_store.registered_prefixes(_home())
    if len(registered) >= 2:
        if not prefix:
            print("multi-repo 네임스페이스 모드(등록 repo ≥2) — prefix 필요(미해소). "
                  "`--prefix <PFX>` 로 명시하거나 task prefix 를 지정하라"
                  "(`pm-config task prefix <task> <PFX>`). 새 카테고리가 필요하면 1순위로 사용자에게 "
                  "그 prefix 승인을 요청하라. 승인한 사용자만 부차적으로 "
                  "`board.py init --prefix <PFX> --area <name> --user-ack <PFX>`를 "
                  "대상값에 결속해 실행한다(세션 자동 부착 금지).",
                  file=sys.stderr)
            return 1

    # 설계 단계 기본값 — 명시 `--design` 우선, 무명시는 estimate=large → required · 그 외 n/a
    # (T-0594). 값 형식은 발행 시점에 fail-loud 로 검증한다 — 잘못된 값이 티켓에 박히면
    # claim 게이트가 나중에야 걸린다.
    design = design_rules._resolve_design(getattr(args, "design", None),
                             getattr(args, "estimate", None))
    design_reason = design_rules._validate_design(design)
    if design_reason:
        print(f"[중단] {design_reason}", file=sys.stderr)
        return 1

    tmpl_fm, tmpl_body = ticket_store.load_ticket(pm_paths.template_file(_home()))
    # ID 발행 전에는 템플릿의 ID로 본문 경로만 판정한다.
    draft_lint_id = str(tmpl_fm.get("id") or "")
    draft_body = tmpl_body.replace("T-NNNN", draft_lint_id).replace("<제목>", args.title)
    is_draft = board_git._board_git_enabled(_home()) and bool(
        body_lint._body_lint_issues(_home(), draft_lint_id, draft_body, strict_sections=True, design=design))
    # 코드 없는 task draft는 허용한다. 위치 인자(`--repo`/`--slot`)를 줬으면 draft에서도 검증한다.
    binding_required = (not is_draft
                        or getattr(args, "repo", None) or getattr(args, "slot", None))
    binding_message = ("[중단] 발행의 코드 트리와 기준 브랜치를 task 폴더에서 읽지 못했다 "
                       "(`--task <task>`, task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가). "
                       "코드 없는 초안은 draft로 작성한 뒤 기존 cluster new로 결속하라.")
    if binding_required:
        tree, base = board_cluster._cluster_publish_base_branch(_home(), args)
        if not tree or not base:
            print(binding_message, file=sys.stderr)
            return 1

    # 발행 규율 게이트: board-git 이 공유(별도 git·submodule) 상태일 때만
    # 의미가 있다 — 미충전 stub 이 board-git 에 커밋돼 다른 slot 의 handoff/bootstrap 을
    # 오염시키는 게 문제이므로, board 가 별도 git 이 아니면(legacy)
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
    with fs.board_lock(_home()):
        if prefix is not None:
            # 모든 non-None 발행 prefix를 ID 발행 락 안에서 cache-free 3소스 snapshot으로
            # 다시 확인한다. 명시/유도 어느 경로든 그사이 다른 case가 생기면 `_next_id`와
            # 파일 write 전에 중단한다.
            fresh = areas_registry.revalidate_prefix_user_ack(
                _home(), prefix,
                getattr(args, "user_ack", None),
                prefix_precheck,
                surface=prefix_surface,
            )
            if fresh is None:
                return 1
            prefix = fresh
        tid = ticket_store._next_id(_home(), prefix)
        slug = ticket_rules._slugify(args.title)
        filename = f"{tid}-{slug}.md"

        # Replace placeholder tokens in body
        body = tmpl_body.replace("T-NNNN", tid).replace("<제목>", args.title)
        # lint 판정은 `tid` 치환과 무관(placeholder/section 검사가 `T-NNNN` 자체를 안 봄) —
        # 발행 전에 판정해 쓰기 경로(open/ vs drafts_dir())를 정한다(open/ 창 노출 0).
        is_draft = board_git._board_git_enabled(_home()) and bool(
            body_lint._body_lint_issues(_home(), tid, body, strict_sections=True, design=design))

        fm: dict[str, Any] = dict(tmpl_fm)
        fm["id"] = tid
        fm["title"] = args.title
        fm["status"] = "draft" if is_draft else "open"
        fm["created"] = datetime.date.today().isoformat()
        # created_by = `<user>/<task>` (provenance·불변·생성 시 set·세션 기본 뷰 스트림 판정 입력).
        # "누가 추가했나" = 중복-작업 방지의 출처 표식. user 미상이면 task 만(graceful).
        fm["created_by"] = lease_store.identity_tag(_home(),
            session_override=actor_session,
            user_override=getattr(args, "user", None))
        fm["claimed_by"] = None
        fm["claimed_at"] = None
        fm["completed_at"] = None
        fm["touches"] = (args.touches.split(",") if args.touches else [])
        fm["depends_on"] = (args.depends.split(",") if args.depends else [])
        fm["blocks"] = []
        fm["tags"] = (args.tag.split(",") if args.tag else [])
        fm["estimate"] = args.estimate
        # 설계 단계 상태 — 템플릿에 이미 `design:` 이 있으면 그 자리를, 없으면(구 템플릿) 끝에
        # 붙는다. 값 자체는 위에서 해소·검증했다.
        fm["design"] = design
        # 운영 단위 — 새 티켓은 자기 크기 1 장부를 갖는다(기준 브랜치·예산은 그 자리에서
        # 박는다). **draft 는 미룬다**: 장부는 board-git 공유인데 draft 명세는 격리(공유 밖)라,
        # 지금 멤버로 올리면 다른 클론에는 없는 티켓을 가리키는 장부가 공유된다. 승격
        # 전에 기존 cluster new로 제품 브랜치와 멤버를 명시 결속한다.
        cluster_ledger: Path | None = None
        # 마지막 제품 판정과 기록에 같은 결과를 사용한다. helper의 재조회로 바꾸지 않는다.
        final_binding_required = (not is_draft
                                  or getattr(args, "repo", None) or getattr(args, "slot", None))
        if not is_draft or final_binding_required:
            publish_basis = board_cluster._cluster_publish_base_branch(_home(), args)
            if final_binding_required and (not publish_basis[0] or not publish_basis[1]):
                print(binding_message, file=sys.stderr)
                return 1
        if not is_draft:
            fm["cluster"], cluster_ledger = board_cluster._cluster_auto_attach(_home(), tid, args, publish_basis=publish_basis)

        if is_draft:
            pm_paths.drafts_dir(_home()).mkdir(parents=True, exist_ok=True)
            path = pm_paths.drafts_dir(_home()) / filename
        else:
            path = pm_paths.tickets_dir(_home()) / "open" / filename
        ticket_store.dump_ticket(path, fm, body)
    areas_registry.invalidate_known_prefixes_cache()

    print(f"created {tid} ({pm_paths._rel_to_repo(_home(), path)})")
    print("  → fill in 목표 / 완료 조건 / 참고, then `board.py lint --gate` "
          "(placeholders left in the body fail lint)")
    if cluster_ledger is not None:
        # 기존 stdout 줄은 그대로 두고 생성 사실만 stderr 1줄로 고지한다(크기 1 = 현행 동작).
        print(f"  ⓘ 크기 1 묶음 장부 생성: {fm['cluster']}", file=sys.stderr)

    if is_draft:
        # draft 는 STATUS_DIRS 밖(drafts_dir())에 있어 board.md(STATUS_DIRS 스캔)에도, 다른
        # slot 의 board-git pull/handoff 에도 나타나지 않는다 — 로컬 `board.py show <id>` 로만
        # 조회 가능(`find_ticket` 이 drafts_dir() 폴백으로 찾는다). draft **파일**은 기존 금지
        # pathspec 으로 계속 커밋하지 않되, sync funnel 은 한 번 태운다. 그래야 `.gitignore` 없는
        # board 의 첫 mutation 이 바로 draft 인 경우에도 루트 backfill 이 commit/push 되어, 방금
        # 만든 draft 가 handoff dirty 관측에 untracked 로 남는 창이 없다.
        print(f"  ⚠ draft — board-git 미커밋(공유 board 오염 방지): "
              f"미충전(placeholder/thin) 본문. 본문을 채운 뒤 "
              f"`board.py promote {tid}` 로 승격(open/ 이동 + board-git 커밋)하라.",
              file=sys.stderr)
    else:
        board_dashboard.refresh_board(_home())
    sync_needed = not is_draft or board_git._board_git_root_files_need_backfill(_home())
    ready = True
    if sync_needed:
        sync_label = f"new {tid} draft isolation" if is_draft else f"new {tid}"
        sync_paths = (path,) if cluster_ledger is None else (path, cluster_ledger)
        ready = board_sync._board_git_sync_best_effort(_home(), sync_label, sync_paths)
    if not ready:
        label = "draft 격리 기록" if is_draft else "기록"
        print(f"  ⚠ board-git {label} 보류: local-only/uncommitted", file=sys.stderr)
    # 발행 시점 판단 재료 — 겹침 0 이면 전부 침묵. 차단 없음(rc 불변).
    # 슬롯 재료의 repo 는 이 발행의 위치 인자 `--repo` 다 — REPO.name(물리 폴더명)과 다른
    # 등록명을 공식 지원하므로 그 값을 재사용한다(첫 줄의 실행 명령 판정 함수가 이미 검증한 args).
    new_repo = identity_args.parse_identity(args).repo
    for line in cluster_binding._publish_overlap_material(_home(), tid, fm.get('touches'), new_repo or REPO.name):
        print(line, file=sys.stderr)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """draft(drafts_dir() 격리·board-git 미커밋) 티켓을 승격 — 재검사 후 open/ 이동 + board-git sync.

    `board.py new` 가 생성 시점 게이트로 `drafts_dir()`(STATUS_DIRS 밖)에 남긴
    draft 를, 본문을 채운 뒤 이 명령으로 승격한다. 여전히 placeholder/thin 이 남아있으면
    거부(rc=1)하고 남은 이슈를 안내한다(파일은 drafts_dir() 에 그대로 — 재수정 후 재시도).
    형식(placeholder/thin/설계 절)에 더해 본문이 **주장하는 사실**도 이 자리에서 판정한다
    (`content_facts=True` — repo-relative `파일:줄` 인용의 부재·범위 초과와 `design:
    required|done` 티켓의 architect 점검 라운드 미회수는 차단, touches 미해소와 판정불능
    개수는 경고 1줄). 판정 대상은 승격되는 티켓 하나이며 기존 open 티켓을 소급하지 않는다.
    통과해야 `open/` 으로 옮겨져 STATUS_DIRS 스캔 대상이 되고 board-git 에 커밋돼 공유 board
    에 존재하게 된다(생성~claim 사이 handoff 창에 미충전 stub 이 인계되는 실패를 원천 차단).
    legacy board에서는 본문 게이트와 sync가 no-op이다. 제품 branch/base와 멤버 결속은
    공유 여부와 관계없이 검증한다.
    """
    moved = False
    with fs.board_lock(_home()):
        try:
            status, path = ticket_store.find_ticket_for_mutation(_home(), args.id)
        except FileNotFoundError as e:
            print(e, file=sys.stderr)
            return 2
        if status not in ("open", "draft"):
            print(f"cannot promote {args.id}: currently in {status}/ (promote 는 open/draft 만)",
                  file=sys.stderr)
            return 1
        fm, body = ticket_store.load_ticket(path)
        if board_git._board_git_enabled(_home()):
            issues = body_lint._body_lint_issues(_home(), args.id, body, strict_sections=True,
                                       design=fm.get("design"),
                                       touches=fm.get("touches"), content_facts=True)
            # 경고 축(`_PROMOTE_ADVISORY_KINDS`)은 승격을 막지 않는다 — 차단 여부와 무관하게
            # 먼저 표면화해, 거부된 경우에도 같은 1줄을 본다.
            advisories = [i for i in issues if i[1] in body_lint._PROMOTE_ADVISORY_KINDS]
            for tid, kind, detail in advisories:
                print(f"  ⚠ [{kind}] {detail}", file=sys.stderr)
            remaining = [i for i in issues if i[1] not in body_lint._PROMOTE_ADVISORY_KINDS]
            if remaining:
                print(f"promote 거부 — {args.id} 에 아직 미충전 {len(remaining)}건:",
                      file=sys.stderr)
                for tid, kind, detail in remaining:
                    print(f"  ✗ [{kind}] {detail}", file=sys.stderr)
                return 1
        # 스코프= 이 promote 가 만진 경로. draft 쪽 옛 경로는 `_board_git_scope_pathspec`
        # 이 걸러낸다.
        touched: list[Path] = [path]
        final_path = path
        # 공유 전에 기존 cluster new의 제품·멤버 결속을 읽기로 검증한다.
        # 선언된 장부와 티켓 cluster 값은 승격이 재작성하지 않는다.
        if board_git._board_git_enabled(_home()):
            problem = cluster_binding.cluster_binding_problem(_home(), cluster_rules.ticket_cluster(args.id, fm))
            if problem is None:
                problem = cluster_binding.cluster_complete_binding_problem(_home(), args.id, fm, str(fm.get('cluster') or ''))
            if problem:
                print(f"[중단] promote 결속 검증: {problem}", file=sys.stderr)
                return 1
        if status == "draft":
            # drafts_dir() → open/ 이동 — 이제서야 STATUS_DIRS 스캔·board-git 대상이 된다.
            # status dump와 rename도 같은 lock 안이라 claim/tier writer가 중간 형상을 못 본다.
            fm["status"] = "open"
            ticket_store.dump_ticket(path, fm, body)
            new_path = ticket_store.move_ticket(_home(), path, "open")
            touched.append(new_path)
            final_path = new_path
            moved = True
        # draft 의 라운드도 promote 가 출하한다 — draft 명세는 board-git ignore 지만
        # `tickets/<라운드 디렉터리>/<티켓>/` 은 ignore 밖이라, 승격 커밋에 함께 싣지 않으면
        # 준비된 라운드가 미커밋으로 눌러앉는다(스코프 전개는 디렉터리를 파일로 펼친다).
        round_dir = round_store.rounds_dir_for_ticket(args.id, pm_paths.tickets_dir(_home()))
        if round_dir.is_dir():
            touched.append(round_dir)
    if moved:
        board_dashboard.refresh_board(_home())
    ready = board_sync._board_git_sync_best_effort(_home(), f"promote {args.id}", touched)
    if not board_git._board_git_enabled(_home()):
        # board 가 홈 git 안이면 board-git 커밋도 공유도 없다 — 실제로 한 일(이동 여부)만 말한다.
        placement = "draft → open/" if moved else "open/ 그대로"
        print(f"promoted {args.id} ({placement} · board 가 홈 git 안이라 커밋 없음)")
    elif ready:
        print(f"promoted {args.id} (board-git 승격 완료)")
    else:
        print(f"promoted {args.id} (board-git 기록 보류: local-only/uncommitted)")
    # 발행 시점 판단 재료 — 성공 경로 세 갈래(홈 git board/승격 완료/기록 보류) 공통 뒤,
    # `_board_git_enabled()` 게이트 분기 밖(솔로/legacy 형상에서도 나온다). 겹침 0 이면 침묵.
    # `promote` 는 `--repo`/`--slot` 인자가 없다 — 슬롯 재료의 repo 는 이 티켓의 provenance
    # (`created_by`)에서 세션 토큰을 뽑아(`_created_by_session` — 기존 세션-뷰 seam) 장부
    # 등록 repo 로 재해소한다(`lease_store.session_coordinates` — 행이 있으면 행의 repo,
    # 없으면 세션명 키 분해로 폴백). 둘 다 기존 seam 재사용 — 새 해소 축 아님.
    promote_repo, _promote_slot_number = lease_store.session_coordinates(
        ticket_rules._created_by_session(fm.get("created_by")), LEASES_FILE)
    for line in cluster_binding._publish_overlap_material(_home(), args.id, fm.get('touches'), promote_repo or REPO.name):
        print(line, file=sys.stderr)
    return 0


# `list` 기본뷰(무-status)의 활성 상태 — 종결(done·discarded)을 접어 범람을 해소한다.
# `--status all` 이 STATUS_DIRS 전체(종결 포함)를 연다.


# ── 발행 시점 touches 겹침 재료 (판단 지점에 판단 재료가 있어야 한다) ──────────
# `new`/`promote` 판단 지점에 다른 활성·draft 티켓과의 touches 겹침 + 가용(idle) 슬롯 수를
# stderr 재료로 낸다. **절대 차단하지 않는다** — 재료를 놓을 뿐 판정은 PM 이 한다. 좌표
# 정규화는 `repo_coordinates` 단일 소유자, 비교 규칙(`_touch_paths_overlap`)은 `ticket_rules`
# 단일 소유자를 재사용한다(board 는 이 축에 자체 문자열 비교를 만들지 않는다 — 접두
# `work/<repo>_<N>/` 선언이 활성 티켓 상당수라 순진한 비교는 조용히 0이 된다).
# 헤더 + 경로/잔여-요약 + 슬롯 줄을 **전부 포함한** 총 상한(리뷰 F-003 — 개별 상한만 지키면
# 헤더 1 + 경로 8 + 잔여요약 1 + 슬롯 1 = 11줄로 티켓 §결정의 9줄 상한을 넘는다).


# 장부 status 열거 — 종결(`closed`)만 비활성이고 나머지는 진행 중(자동 귀속 후보)이다.
# 라운드 예산 기본값 — 초과 판정(예약 거부)은 라운드 예약 표면이 소유하고, 여기서는 장부에
# 박는 기본값 한 벌만 정의한다(각 단계 정확히 1회).
# 장부 frontmatter 키 선언 순서 — 사람이 읽는 자리라 순서를 고정한다(미래 키는 뒤에 붙는다).
# 이름 문법 — 파일명과 git 브랜치 이름에 그대로 들어가므로 경로 구분자·refname 금칙을 배제한
# 보수적 집합만 받는다(`_validate_prefix` 와 같은 입력측 sanity 자리).
# 문자 집합으로는 걸리지 않는 refname 금칙 — 이름은 통합 브랜치의 마지막 마디로 그대로 들어가
# 므로 `git check-ref-format --branch <브랜치 접두><ticket-group>` 과 같은 판정을 순수 술어로 낸다.
# 제어문자·공백·`~^:?*[\`·`/`·`@{` 는 위 문자 집합이 이미 배제하고, 남는 셋이 아래다: 이중 점 ·
# `.lock` 으로 끝나는 마디 · 점으로 끝나는 마디. 이걸 통과시키면 **만들 수 없는 브랜치**를
# 장부에 선언한 채 명령이 성공하고(브랜치 부재 advisory 만 남는다) 그 묶음은 영구히 통합
# 브랜치를 못 갖는다.
# 멤버가 될 수 없는 상태 — 종결된 티켓은 운영 단위의 구성원이 아니다.
# 클러스터 내부 겹침 줄의 경로 표시 상한 — 발행 재료와 같은 규율(줄 수를 예산 안에 둔다).


def cmd_list(args: argparse.Namespace) -> int:
    # `--mine` / `--repo`·`--slot`
    # 뷰: 단일 공유 보드의 렌즈 — **현재 사용자**의 area open + claim.
    # identity 입력을 한 번 해소해 행마다 재계산 안 함. **무인자 list 는 세션 기본 뷰**
    # (내 세션 스트림만 = 생성 open + 내 claim·타 세션분 완전 비노출·아래 default_view)이고,
    # 필터 없는 전체(모든 세션·타 사용자)는 `--all` 이다(status 셀렉터는 어느 뷰에도 적용).
    #
    # user-first: 필터 뷰의 "me" 는 **항상 현재 사용자**(`user_name()` =
    # local.conf identity.user= > git config user.email)다. 렌즈의 my_user 를
    # area_owner-derived(`_area_owner_from_session`/`_area_owner_for_single_area`)로
    # area_owner 미설정(흔함)이면 my_user=None 이 돼 slot-only 매칭·
    # all-open degrade 로 타 슬롯 claim·타 사용자 티켓이 유출되던 근본을 없앤다. area_owner 는
    # 이제 open-티켓 *소유* 정의(`_ticket_owner`)로만 남고, "누가 조회하는가" 와 무관하다.
    #
    # 뷰 스코프:
    #   - `--mine` = 내 것(내 claim ∪ 내 open), **전 task**(slot_scoped=False).
    #   - `--task <task>` = 내 것 **∩ 그 task**(slot_scoped=True·
    #     완전 일치) — 멤버십 = claimed_by 바인딩이 그 task(`<user>/<task>`)인
    #     claim + 내 소유 open backlog. `_slot_matches` exact 가 claimed_by 의 `/` 뒤 토큰을 task 와
    #     대조한다. `_ticket_is_mine` 단일 predicate 를 그대로 상속(point-patch 금지).
    #   `--mine`·`--task` 는 상호 배타(뷰 스코프는 하나만) — 타 사용자는 어느
    #   필터 뷰에도 안 나온다(`list --all` 전용). 슬롯·repo 렌즈는 없다.
    explicit_mine = bool(getattr(args, "mine", False))
    task_lens = getattr(args, "task", None)
    if task_lens is not None:
        # read 경로도 깔때기 소비 — 미검증이면 `--task alpha_1`(폴더
        # 이름 패턴) 입력이 옛 슬롯 claim 을 task 처럼 매칭한다. 부작용 0 조회지만 소비 지점
        # 폐쇄(전 surface 단일 규칙)에 합류한다.
        ticket_access._validate_actor_task_or_exit(_home(), task_lens)
    if task_lens is not None and explicit_mine:
        sys.exit("[중단] --task 는 --mine 과 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    mine = explicit_mine or task_lens is not None
    # `--all`= 기존 무인자 전체 뷰(모든 세션·타 사용자 포함)의 이관. 무인자
    # 기본은 이제 **세션 기본 뷰**(default_view — 내 세션 스트림만·타 세션분 완전 비노출)다. `--all` 은
    # 렌즈(`--mine`/`--task`)와 상호 배타(뷰 스코프는 하나만). default_view 는
    # 어떤 렌즈도 `--all` 도 없을 때(순수 무인자) 발동한다.
    all_flag = bool(getattr(args, "all", False))
    if all_flag and mine:
        sys.exit("[중단] --all 은 --mine/--task 와 함께 쓸 수 없다 — 뷰 스코프는 하나만 지정하라.")
    default_view = not all_flag and not mine
    # 세션 뷰 = **무인자 기본 뷰**. "그 세션 스트림"(생성 open + 그 세션 claim)만 출력한다
    # (`_in_default_view`). `--mine`(user-wide)·`--task`(task 렌즈)는 세션 뷰가 아니라 기존
    # `_ticket_is_mine` 렌즈(의미론 불변).
    session_view = default_view
    # task 렌즈는 claim 을 그 task 로 교집합한다(--mine 은 전 task).
    slot_scoped = task_lens is not None
    if task_lens is not None:
        my_user = lease_store.user_name(_home())
        my_slot = task_lens
    else:
        # 무렌즈 경로 — `--mine`(user-wide) 또는 기본 뷰(default_view·세션 스코프). 둘 다
        # 현재 사용자/세션을 해소한다(전체 뷰 `--all` 만 정체성 무해소·additive). 기본 뷰의 my_slot
        # (=현 세션)은 내 세션 claim 매칭과 생성-세션 스트림 판별(created_by 세션 일치)의 입력이다.
        resolve_identity = mine or default_view
        my_user = lease_store.user_name(_home()) if resolve_identity else None
        my_slot = lease_store.session_name(_home()) if resolve_identity else ""
        if mine and my_slot is None:
            # 세션 미바인딩(surface) — claim 필터를 못 좁힌다.
            # 안내는 stderr 로 내 stdout 티켓 목록 포맷을 오염시키지 않는다(소유 open 은 계속 표시).
            print("(비바인딩 — 세션 미해소·claim 필터 비활성; `--task <task>` 로 지정)",
                  file=sys.stderr)
    # graceful degrade: (a) 풀(내 소유 open) 필터는 보드에 area_owner 가 *운영
    # 중일 때만* 그 파티션을 1차 소유로 쓴다. areas.md 에 area_owner 가 하나도 안 채워졌으면
    # (미마이그레이션 채택자) area_owner_in_use=False → 소유는 created_by.user 2차 폴백으로
    # 해소한다(`_ticket_owner`).
    #
    # 다중사용자 판정(`multi_user`·단일 사용자 정의 완결): **티켓 user 토큰이든 area_owner 든
    # 둘 중 하나라도 distinct ≥2 면 multi-user**. `_distinct_ticket_users`(티켓 귀속만 셈) 단독이면
    # 다중-owner 보드라도 claim 이 전부 legacy 슬롯-only(user 토큰 0)일 때 ≤1 로 떨어져 단일 사용자로
    # 오판 → legacy 슬롯-only 포함 경로가 발동해 (당시) bare `--slot N`(repo 불문 cross-repo
    # suffix 매칭은 제거됨)이 타 area 의 legacy `<repo>_N` 을 끌어오는 누출이 났다.
    # `_distinct_area_owners`(areas 소유 다중성)를 OR 로 더해 그 클래스를 닫는다.
    # 단일 사용자(둘 다 ≤1)면 미해소 open all-open degrade + legacy 슬롯-only 포함 보존, 다중이면
    # strict-exclude한다.
    area_owner_in_use = (mine or default_view) and ticket_access._area_owner_in_use(_home())
    multi_user = (mine or default_view) and (
        ticket_access._distinct_ticket_users(_home()) > 1 or ticket_access._distinct_area_owners(_home()) > 1)
    # status 뷰 셀렉터: 기본(무-status)=활성만(done 접기) · `--status all`=전체(done 포함)
    # · `--status <특정>`=그 status 만(기존 동작 무변경).
    status_arg = getattr(args, "status", None)
    if status_arg == "all":
        allowed_statuses = ticket_rules.STATUS_DIRS
    elif status_arg:
        allowed_statuses = (status_arg,)
    else:
        allowed_statuses = cluster_binding._LIST_ACTIVE_STATUSES
    rows: list[tuple[str, dict]] = []
    # 세션격리 strict-exclude 신호: 다중사용자 판정 때문에 귀속 미해소/불일치 티켓을 이 뷰에서
    # 조용히 드롭했는지 잡는다. 단일 사용자면 재평가 자체를 생략해 오버헤드와 오탐을 피한다.
    strict_exclude_fired = False
    for status in ticket_rules.STATUS_DIRS:
        if status not in allowed_statuses:
            continue
        for p in sorted((pm_paths.tickets_dir(_home()) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(_home(), p)
            if loaded is None:
                continue
            fm, _ = loaded
            if args.tag and args.tag not in ticket_rules._tag_values(fm):
                continue
            if session_view:
                # 세션 뷰(무인자 기본) — 그 세션 스트림(생성 open +
                # 그 세션 claim)만. 타 세션분은 카운트 줄 포함 완전 비노출(전체는 `--all`).
                in_view = ticket_access._in_default_view(
                    _home(), status, fm, my_user, my_slot, area_owner_in_use, multi_user)
                if in_view:
                    rows.append((status, fm))
                elif multi_user and not strict_exclude_fired:
                    # 같은 predicate 를 단일 사용자로 재평가해 다중사용자 strict-exclude 때문에 숨긴
                    # 티켓인지 판정한다. 해소된 사용자 값이 과거 스탬프와 어긋난 경우도 같은
                    # 세션 후보인지 확인하도록 조회 user만 미해소 상태로 완화한다. 세션 축은
                    # 그대로이므로 타 세션의 정상 제외는 경고 신호가 되지 않는다.
                    strict_exclude_fired = (
                        ticket_access._in_default_view(
                            _home(), status, fm, my_user, my_slot, area_owner_in_use, False)
                        or ticket_access._in_default_view(
                            _home(), status, fm, None, my_slot, area_owner_in_use, False)
                    )
                continue
            if mine and not ticket_access._ticket_is_mine(_home(), status, fm, my_user, my_slot,
                                            area_owner_in_use, multi_user,
                                            slot_scoped=slot_scoped):
                # 이 제외가 *strict-exclude* 였는지 판정: 같은 predicate 를 multi_user=False 로
                # 재평가해 단일 사용자(all-open degrade)라면 포함됐을 open 이면 = 다중사용자라서 드롭한
                # 것(`_ticket_is_mine` 미해소 분기). 판정을 복제하지 않고 단일 predicate 를 재사용
                # 해 실 드롭만 신호로 잡는다. 이미 발동했으면 재평가를 생략한다. 소유 해소된 타
                # 사용자 티켓 제외는 단일 사용자에서도 제외라 무신호다.
                if multi_user and not strict_exclude_fired and ticket_access._ticket_is_mine(
                        _home(), status, fm, my_user, my_slot,
                        area_owner_in_use, False,
                        slot_scoped=slot_scoped):
                    strict_exclude_fired = True
                continue
            rows.append((status, fm))
    # anti-degrade loud-warn: 다중사용자 격리가 조용히 티켓을 드롭했거나 정체성이 미해소면 목록
    # 출력 전에 stderr로 한 줄 경고한다. stdout 목록 포맷은 그대로이며 단일 사용자는 경고하지 않는다.
    if (mine or session_view) and multi_user and (
            strict_exclude_fired or my_user is None):
        print(
            "⚠ 세션격리(strict-exclude): 다중사용자 보드에서 귀속이 미해소되거나 현재 사용자와 "
            "어긋난 티켓을 이 뷰에서 제외했다. 단일 사용자인데 email(git config user.email)을 바꿨다면 "
            "옛 티켓 귀속(old→new)이 어긋나 2인으로 오판된 것 — created_by/claimed_by 를 backfill "
            "로 정합: `python3 .project_manager/tools/board.py migrate-identity --task <task> --dry-run` "
            "(단일-세션 op·다른 세션 claim 중 실행 금지). 진짜 다중사용자면 정체성 설정 "
            "`board init --owner <you>`.",
            file=sys.stderr,
        )
    # board-git freshness 표면화 (advisory·stderr) — 각 list 변형 공통·양 return 경로
    # ("(no tickets)"·행 있음)를 커버하도록 여기서 한 번 소환. board 비-git 이면 무출력.
    board_list._print_board_freshness(_home())
    if not rows:
        print("(no tickets)")
        return 0
    for status, fm in rows:
        tags = ",".join(ticket_rules._tag_values(fm))
        claimed = fm.get("claimed_by") or ""
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:7s}] {fm['id']}  {title:60s}  {claimed:18s}  {tags}")
    return 0


# 무prefix(legacy `T-NNNN`) 티켓의 prefix-list 라벨. `none` 은 무prefix 네임스페이스의 1급
# 인자이므로 그 라벨을 그대로 쓴다(예약어와 표기 일치).
_NO_PREFIX_LABEL = "none"


def cmd_prefix_list(args: argparse.Namespace) -> int:
    """prefix별 티켓 수·번호범위 현황을 출력한다 (read-only·비파괴).

    STATUS_DIRS(전 상태 디렉토리)의 전 티켓 ID 를 파싱해 `T-<p>-NNN` → 그 prefix,
    `T-NNNN` → `none`(무prefix)로 버킷팅한다. mess(카테고리 난립·번호 재시작)를 표면화하는
    도구 — board mutation·rewrite 는 하지 않는다.
    """
    # prefix(또는 None=무prefix) → [(순번, 실 ID), …]. 실 ID 를 들고 있어 min/max 를 재구성
    # 없이 그대로 범위로 쓴다(zero-pad 재구성 mismatch 회피).
    buckets: dict[str | None, list[tuple[int, str]]] = {}
    for status in ticket_rules.STATUS_DIRS:
        for p in sorted((pm_paths.tickets_dir(_home()) / status).glob("T-*.md")):
            loaded = ticket_store.load_ticket_soft(_home(), p)
            if loaded is None:
                continue
            fm, _ = loaded
            tid = ticket_store._issued_ticket_id(_home(), p, fm)
            if not tid:
                continue
            num = ticket_rules._ticket_id_number(tid)
            if num is None:
                continue
            buckets.setdefault(ticket_rules._ticket_prefix(tid), []).append((num, tid))
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


def _rename_map(src: str | None, dst: str | None,
                tickets: list[dict[str, Any]]) -> dict[str, str]:
    """rename 무충돌 맵 — src 네임스페이스 티켓의 prefix 만 dst 로 교체(번호 유지).

    src 매칭은 **case-insensitive fold**(`_fold_key`) — `rename aaa bbb` 가 기존
    `T-AAA-*`(prefix `AAA`)를 잡아 relabel 한다(case 만 다르면 silent no-op 하던 갭 봉합). dst 는
    호출자가 ``require_prefix_user_ack`` 공유 게이트에서 3소스 canonical case로
    해소한 값이므로 그대로 발행 case다(`_validate_dst_prefix`는 형식 sanity만 담당).
    """
    src_key = ticket_rules._fold_key(src)
    id_map: dict[str, str] = {}
    for t in tickets:
        if ticket_rules._fold_key(t["prefix"]) != src_key:
            continue
        new_id = areas_registry._format_ticket_id(dst, t["num"])
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
    가 기존 `T-AAA-*` source 를 모은다. into(대상) max 계산은 exact(`== into`) — 호출자의
    ``require_prefix_user_ack``가 into를 canonical case로 해소하므로 case-혼동이 없다
    (source 측만 fold·`_validate_dst_prefix`는 형식 sanity만 담당).
    """
    src_index = {ticket_rules._fold_key(p): i for i, p in enumerate(sources)}
    src_tickets = [t for t in tickets if ticket_rules._fold_key(t["prefix"]) in src_index]
    start = max((t["num"] for t in tickets if t["prefix"] == into), default=0)
    ordered = sorted(
        src_tickets, key=lambda t: (t["created"], src_index[ticket_rules._fold_key(t["prefix"])], t["num"]))
    id_map: dict[str, str] = {}
    for i, t in enumerate(ordered, start=start + 1):
        new_id = areas_registry._format_ticket_id(into, i)
        if new_id != t["id"]:
            id_map[t["id"]] = new_id
    return id_map


def _merge_reorder_map(sources: list[str | None], into: str | None,
                       tickets: list[dict[str, Any]]) -> dict[str, str]:
    """merge --reorder-chronological 맵 — 대상+source 전체를 created 순으로 1..N 재번호(opt-in).

    전체 interleave 라 대상 티켓 ID 도 바뀌어 전 참조 rewrite(17k refs 고위험)라 opt-in 이다.
    tiebreak: 대상 그룹 먼저(-1), 그다음 source 목록 순서, 그다음 기존 번호.

    source membership 은 case-insensitive fold(`_fold_key`)·into 그룹은 exact(`== into`·
    호출자의 ``require_prefix_user_ack``가 canonical case 해소) — `_merge_append_map`과 동일 규율.
    """
    src_index = {ticket_rules._fold_key(p): i for i, p in enumerate(sources)}
    involved = [t for t in tickets
                if t["prefix"] == into or ticket_rules._fold_key(t["prefix"]) in src_index]

    def _key(t: dict[str, Any]) -> tuple[str, int, int]:
        group = -1 if t["prefix"] == into else src_index[ticket_rules._fold_key(t["prefix"])]
        return (t["created"], group, t["num"])

    id_map: dict[str, str] = {}
    for i, t in enumerate(sorted(involved, key=_key), start=1):
        new_id = areas_registry._format_ticket_id(into, i)
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
    num = ticket_rules._ticket_id_number(tid)
    if num is None:
        return tid
    return (ticket_rules._fold_key(ticket_rules._ticket_prefix(tid)), num)


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
        with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as fh:
            fh.read()
    except (OSError, UnicodeDecodeError):
        return False
    return True


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
    for status in (*ticket_rules.STATUS_DIRS, ".drafts"):   # .drafts 포함 — _scan_prefix_tickets 와 lockstep.
        for p in (pm_paths.tickets_dir(_home()) / status).glob("T-*.md"):
            tid = ticket_store._canonical_ticket_id(p)       # frontmatter id 1차 진실 (숫자-slug 모호성 해소).
            if not (tid and p.name.startswith(tid)):
                tid = ticket_rules._ticket_id_from_filename(p.name)   # 접두 불일치(corrupt) → 파일명 폴백.
            if not (tid and tid in id_map):
                continue
            if not _is_rewritable(p):
                print(f"  ⚠ rename skip {p.name} — 본문 rewrite 불가(비-UTF-8/읽기 실패): "
                      "파일명↔content id 불일치 방지 위해 파일명 유지. 수동 확인.", file=sys.stderr)
                continue
            new_name = id_map[tid] + p.name[len(tid):]
            renames.append((p, p.with_name(new_name)))
    return renames


def _plan_rounds_dir_renames(
        renames: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """티켓 파일 rename 계획을 따라 그 티켓의 라운드 디렉터리도 함께 옮길 계획을 세운다.

    라운드는 티켓 ID 로 이름 붙은 디렉터리 하나에 모여 있으므로 옮길 것도 **디렉터리 하나**다
    (파일별 계획이 아니다). 두고 가면 새 ID 는 라운드가 없는 티켓이 되고 옛 ID 자리에는
    티켓 없는 라운드가 남는다 — 그 상태에서 순번 연속성 판정은 아무 것도 못 본다.
    """
    plans: list[tuple[Path, Path]] = []
    for src, dst in renames:
        old_id = ticket_rules._ticket_id_from_filename(src.name)
        new_id = ticket_rules._ticket_id_from_filename(dst.name)
        if not old_id or not new_id or old_id == new_id:
            continue
        src_dir = round_store.rounds_dir_for_ticket(old_id, pm_paths.tickets_dir(_home()))
        if src_dir.is_dir():
            plans.append(
                (src_dir, round_store.rounds_dir_for_ticket(new_id, pm_paths.tickets_dir(_home())))
            )
    return plans


def _apply_file_renames(renames: list[tuple[Path, Path]]) -> None:
    """이름 rename 을 2단계(src→tmp→dst)로 적용 — 번호 swap(reorder) 시 clobber 방지.

    대상은 티켓 파일과 그 티켓의 라운드 디렉터리다(둘 다 같은 부모 안 rename 이라 규칙이 같다).

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

    None = git 부재/repo 아님(보고 skip·안전) · `""` = clean · non-empty = dirty. prefix
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
            timeout=git._BOARD_GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):  # fail-soft: 판정 예외는 가드 skip(None).
        return None
    if r.returncode != 0:
        return None  # not a git repo → 가드 skip
    return r.stdout


def _prefix_relabel(build_map, *, verb: str, label: str,
                    dry_run: bool, noun: str = "prefix",
                    pre_write_check: Callable[[], bool] | None = None,
                    preview_notice: str | None = None) -> int:
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
    ticket lifecycle/tier/migrate writer도 같은 board_lock을 쓰므로 relabel 적용 구간과 직렬화된다.

    **TOCTOU 봉합**: `build_map` 은 (id_map|None, rc) 를 반환하는 클로저다 —
    스캔→맵→collision 검사를 *락 안 fresh snapshot* 에서 수행해, 검사와 적용 사이에 `cmd_new` 가
    같은 번호를 발행해 stale 검사를 통과하는 창을 없앤다. 추가 belt: stage1 전에 계획된 dst 가
    (계획 밖 파일로) 이미 존재하면 **아무것도 쓰기 전에 abort** 한다(덮어쓰기 원천 차단).
    ``pre_write_check``가 있으면 락 안 fresh snapshot 판정을 맵 재생성보다 먼저 수행한다.
    dry-run 은 read-only preview 라 락 없이 빌드하고 이 write-only check를 호출하지 않는다
    (정확성보다 규모 감이 목적).
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
        if preview_notice:
            print(preview_notice)
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
    backup_rev = board_git._board_git_head(_home()) if board_git._board_git_enabled(_home()) else None

    # 전체 mutation 을 단일 board_lock 으로 직렬화 (동시 new/claim 과 상호배제). 파일명
    # rename 계획은 락 보유 하에 fresh scan 으로 세운다(rewrite 와 같은 스냅샷). refresh 는 락-보유
    # 변형(`_refresh_board_locked`)을 직접 불러 board_lock 재진입(데드락)을 피한다.
    # 백업 commit 이 이 구간 안에 있으므로 **board_git_lock 을 board_lock 보다 먼저** 잡는다
    # (락 순서 고정 — 역순 획득이 하나라도 있으면 claim 과 데드락). board-git 비활성이면
    # board_git_lock 은 no-op 이라 legacy 경로는 무변경.
    with board_git.board_git_lock(_home()), fs.board_lock(_home()):
        if pre_write_check is not None and not pre_write_check():
            return 1
        # 맵 생성+collision 검사도 락 안 fresh snapshot 으로 — 검사↔적용 사이 cmd_new 창 봉합.
        id_map, rc = build_map()
        if not id_map:
            return rc
        renames = _plan_file_renames(id_map)
        rounds_renames = _plan_rounds_dir_renames(renames)
        # dst 선검증(belt·아무것도 쓰기 전): 계획 밖 파일이 dst 를 점유하면 abort — 덮어쓰기 차단.
        planned_srcs = {src for src, _ in renames}
        planned_srcs |= {src for src, _dst in rounds_renames}
        occupied = [dst for _, dst in renames + rounds_renames
                    if dst.exists() and dst not in planned_srcs]
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
        all_renames = renames + rounds_renames
        _apply_file_renames(all_renames)
        for src, dst in all_renames:
            touched.extend((src, dst))
        board_dashboard._refresh_board_locked(_home())
        if board_git._board_git_enabled(_home()):
            board_git._board_git_stage_and_commit(_home(), f"{op} {label}", touched)

    areas_registry.invalidate_known_prefixes_cache()

    if board_git._board_git_enabled(_home()):
        if backup_rev:
            print(f"  board-git 백업 rev(되돌리려면 "
                  f"`git -C .project_manager/board reset --hard {backup_rev[:12]}`)")
    else:
        print("  legacy(board-git 미분리) — board 변경은 홈 git 에 함께 있다. "
              "되돌리려면 홈 git 으로 revert.")
    print(f"{op} {label} — {_scale_line(len(id_map), scale, len(renames))} 적용 완료.")
    return 0


def _validate_dst_prefix(raw: str, parsed: str | None) -> str | None:
    """대상 카테고리(dst/into) 형식 sanity — 위반 메시지·정상이면 None.

    `none`(parsed=None)은 항상 허용(이름 지우기·무prefix). 실 prefix 는 새/재사용 카테고리
    이름이므로 `_validate_prefix`(예약어+`[A-Za-z0-9][A-Za-z0-9_]*`·대소문자 허용)로
    못박아 malformed ID 발행을 막는다. source prefix 는 *기존* 발행분이라 검증하지 않는다(하이픈
    legacy 존중). 기존 prefix와의 fold 동일성 및 canonical case 해소는 네 표면이 공유하는
    ``require_prefix_user_ack``가 3소스 합집합으로 맡는다.
    """
    if parsed is None:
        return None
    reason = ticket_rules._validate_prefix(raw)
    if reason:
        return reason
    return None


def cmd_prefix_rename(args: argparse.Namespace) -> int:
    """`prefix rename <A|none> <B|none>` — 무충돌=번호유지 교체·충돌=merge 안내."""
    src = areas_registry._parse_prefix_arg(args.src)
    requested_dst = areas_registry._parse_prefix_arg(args.dst)
    dst = requested_dst
    dry_run = bool(getattr(args, "dry_run", False))
    if src == dst:
        print(f"[중단] src 와 dst 가 같다({args.src} → {args.dst}) — 변경 없음.",
              file=sys.stderr)
        return 1
    reason = _validate_dst_prefix(args.dst, dst)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    dst_is_new = False
    if requested_dst is not None:
        candidate, dst_is_new = areas_registry._prefix_target_snapshot(
            _home(), requested_dst, surface="board prefix rename dst",
        )
        if candidate is None:
            return 1
        dst = candidate

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot 에서 스캔→맵→collision (TOCTOU 봉합·dry-run 은 락 밖 호출).
        tickets, scan_rc = areas_registry._prefix_scan(_home(), "prefix rename")
        if tickets is None:
            return None, scan_rc
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

    preview_notice = None
    pre_write_check = None
    if dry_run:
        if dst_is_new:
            preview_notice = (
                f"[dry-run] 실행 시 새 prefix {requested_dst!r} 신설에는 사용자 승인이 필요하다 — "
                f"승인한 사용자만 `--user-ack {requested_dst}`를 대상값 그대로 붙여 실행하라."
            )
    else:
        # 승인 게이트는 실제 변경 맵이 있을 때만 연다. source 0건 no-op은 여기서 바로 끝나
        # 쓰기·승인 모두 0이고, 실제 적용은 아래에서 같은 맵을 락 안 fresh scan으로 다시 만든다.
        planned, rc = build_map()
        if not planned:
            return rc
        if requested_dst is not None:
            canonical = areas_registry.require_prefix_user_ack(
                _home(), requested_dst,
                getattr(args, "user_ack", None),
                surface="board prefix rename dst",
            )
            if canonical is None:
                return 1
            dst = canonical
            pre_write_check = lambda: areas_registry.revalidate_prefix_user_ack(
                _home(), requested_dst,
                getattr(args, "user_ack", None),
                canonical,
                surface="board prefix rename dst",
            ) is not None

    dst_label = dst if dst is not None else "none"
    return _prefix_relabel(build_map, verb="rename", label=f"{args.src} → {dst_label}",
                           dry_run=dry_run, pre_write_check=pre_write_check,
                           preview_notice=preview_notice)


def cmd_prefix_strip(args: argparse.Namespace) -> int:
    """`prefix strip <A>` — `rename <A> none` 의 별칭(별도 로직 없음)."""
    return cmd_prefix_rename(argparse.Namespace(
        src=args.prefix, dst="none", dry_run=getattr(args, "dry_run", False)))


def cmd_prefix_merge(args: argparse.Namespace) -> int:
    """`prefix merge <A> [B...] --into <T|none>` — created 순 통합(기본 append)."""
    sources = [areas_registry._parse_prefix_arg(s) for s in args.sources]
    requested_into = areas_registry._parse_prefix_arg(args.into)
    into = requested_into
    dry_run = bool(getattr(args, "dry_run", False))
    reason = _validate_dst_prefix(args.into, into)
    if reason:
        print(f"[중단] {reason}", file=sys.stderr)
        return 1
    into_is_new = False
    if requested_into is not None:
        candidate, into_is_new = areas_registry._prefix_target_snapshot(
            _home(), requested_into, surface="board prefix merge --into",
        )
        if candidate is None:
            return 1
        into = candidate
    if ticket_rules._fold_key(into) in {ticket_rules._fold_key(s) for s in sources}:
        # 자기-merge 가드도 case-insensitive — `merge aaa --into AAA` 는 fold-동일
        # 네임스페이스라 자기 자신에 merge 다(source fold-매칭이 노출한 클래스).
        print(f"[중단] --into 대상({args.into})이 source 목록에 있다 — 자기 자신에 merge 불가"
              "(대소문자 무관).", file=sys.stderr)
        return 1
    reorder = bool(getattr(args, "reorder_chronological", False))

    def build_map() -> "tuple[dict[str, str] | None, int]":
        # 락 안 fresh snapshot — merge 의 append start=max(...) 도 stale 이면 clobber.
        tickets, scan_rc = areas_registry._prefix_scan(_home(), "prefix merge")
        if tickets is None:
            return None, scan_rc
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

    preview_notice = None
    pre_write_check = None
    if dry_run:
        if into_is_new:
            preview_notice = (
                f"[dry-run] 실행 시 새 prefix {requested_into!r} 신설에는 사용자 승인이 필요하다 — "
                f"승인한 사용자만 `--user-ack {requested_into}`를 대상값 그대로 붙여 실행하라."
            )
    else:
        # rename과 같은 정책: source 0건이면 승인 없이 no-op, 실제 변경 맵이 있을 때만 gate.
        planned, rc = build_map()
        if not planned:
            return rc
        if requested_into is not None:
            canonical = areas_registry.require_prefix_user_ack(
                _home(), requested_into,
                getattr(args, "user_ack", None),
                surface="board prefix merge --into",
            )
            if canonical is None:
                return 1
            into = canonical
            pre_write_check = lambda: areas_registry.revalidate_prefix_user_ack(
                _home(), requested_into,
                getattr(args, "user_ack", None),
                canonical,
                surface="board prefix merge --into",
            ) is not None

    mode = "reorder" if reorder else "append"
    into_label = into if into is not None else "none"
    label = f"{'+'.join(args.sources)} → {into_label} [{mode}]"
    return _prefix_relabel(build_map, verb="merge", label=label,
                           dry_run=dry_run, pre_write_check=pre_write_check,
                           preview_notice=preview_notice)


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
    af = pm_paths.areas_file(_home())
    with fs.board_lock(_home()):
        if not af.exists():
            return 0
        text = fs.read_text_shared(af, encoding="utf-8")
        ends_nl = text.endswith("\n")
        header_seen = False
        pidx: int | None = None
        cleared = 0
        out: list[str] = []
        for line in text.splitlines():
            cells = areas_rules._split_areas_row(line)
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
            af.write_text(
                "\n".join(out) + ("\n" if ends_nl else ""),
                encoding="utf-8", newline="\n")
    if cleared:
        areas_registry.invalidate_known_prefixes_cache()
    return cleared


def cmd_prefix_delete(args: argparse.Namespace) -> int:
    """`prefix delete <A>` — 빈(0티켓) prefix 의 areas 등록을 지운다·티켓 있으면 fail-loud.

    prefix 는 티켓으로만 존재하는 작업 카테고리(자동시드 폐지)다. 0 티켓이면:
      - areas.md 에 A 가 등록돼 있으면 → 그 행의 **prefix 셀을 실제로 비운다**(행·repo 등록 보존·
        무손실·PM 홈 수동 조치와 동형). promise=do — 메시지가 실제 셀 편집과 일치한다.
      - 미등록이면 → 지울 등록이 없으므로 "확인만"(변경 0)으로 정직하게 보고한다.
    티켓이 있으면 물리삭제 없이 rename/merge 로 안내한다(fail-loud). `--dry-run` 은 쓰기 0·규모
    preview(다른 동사와 공통 표기).
    """
    target = areas_registry._parse_prefix_arg(args.prefix)
    if target is None:
        print("[중단] none(무prefix) 네임스페이스는 delete 불가 — 기준 네임스페이스다.",
              file=sys.stderr)
        return 1
    dry_run = bool(getattr(args, "dry_run", False))
    # 티켓 존재 카운트는 **case-insensitive fold**(`_fold_key`) — `delete AAA` 가 case-변종
    # `T-aaa-*` 도 세어, fold-비지 않은 네임스페이스를 "빈 것"으로 오판해 등록만 지우는 것을 막는다.
    target_key = ticket_rules._fold_key(target)
    # 손상 티켓이 있으면 "0 티켓" 판정 자체를 믿을 수 없다 — 그 티켓이 이 네임스페이스일 수 있다.
    tickets, scan_rc = areas_registry._prefix_scan(_home(), "prefix delete")
    if tickets is None:
        return scan_rc
    count = sum(1 for t in tickets if ticket_rules._fold_key(t["prefix"]) == target_key)
    if count > 0:
        print(f"[중단] prefix {args.prefix} 에 티켓 {count}개 — delete 는 빈(0티켓) prefix 전용"
              f"(무손실·물리삭제 없음). 개명은 `board.py prefix rename {args.prefix} <B|none>`, "
              f"통합은 `board.py prefix merge {args.prefix} --into <T|none>` 로. "
              "B/T가 새 라벨이면 1순위로 사용자에게 승인을 요청하고, 승인한 "
              "사용자만 대상값과 같은 `--user-ack <B|T>`를 붙인다(세션 자동 부착 금지).",
              file=sys.stderr)
        return 1
    registered = areas_store._areas_row_for_prefix(_home(), target) is not None
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

    가드(값싼 정적 → 상태 의존 순): NEW-ID 형식 sanity(발행 문법·legacy prefix 포함) →
    NEW prefix 3소스 canonical/신설 승인 판정 → src≠dst → (락 안 fresh snapshot) OLD 실재 →
    타 세션 claim abort(단일세션 op) → NEW collision(전 상태
    디렉토리+.drafts 에 이미 존재하면 abort). 번호 자동발급 카운터는 `_next_id` 가 max 기반이라 어느
    번호로 옮겨도 무충돌이다 — 다음 발급이 최대치를 자연히 이으므로 별도 조정 없이 확인만 한다(결정).
    """
    old_id, requested_new_id = args.old_id, args.new_id
    new_id = requested_new_id
    dry_run = bool(getattr(args, "dry_run", False))

    # 정적 sanity(락·재조회 불요) — 값싼 거부 먼저. OLD/NEW 형식·자기 자신.
    # OLD-ID 도 형식 선검증: find_ticket 은 glob 기반이라 메타문자(`*`·`?`·`[]`)
    # 든 OLD 가 임의 티켓에 매치된 뒤 rewrite 는 리터럴 키라 no-op 인데 성공처럼 끝날 수 있다.
    if not ticket_rules._is_valid_ticket_id(old_id):
        print(f"[중단] OLD-ID {old_id!r} 형식 위반 — `T-NNNN`(무prefix) 또는 `T-<prefix>-NNN`"
              "(발행 문법) 이어야 한다.", file=sys.stderr)
        return 1
    if not ticket_rules._is_valid_ticket_id(requested_new_id):
        print(f"[중단] NEW-ID {requested_new_id!r} 형식 위반 — `T-NNNN`(무prefix) 또는 "
              "`T-<prefix>-NNN`(발행 문법·legacy prefix 포함) 이어야 한다.",
              file=sys.stderr)
        return 1

    requested_new_prefix = ticket_rules._ticket_prefix(requested_new_id)
    new_prefix_is_new = False
    canonical_new_prefix: str | None = None
    if requested_new_prefix is not None:
        canonical_new_prefix, new_prefix_is_new = areas_registry._prefix_target_snapshot(
            _home(), requested_new_prefix, surface="board reid NEW-ID prefix",
        )
        if canonical_new_prefix is None:
            return 1
        if new_prefix_is_new:
            reason = ticket_rules._validate_prefix(canonical_new_prefix)
            if reason:
                print(
                    f"[중단] 신규 prefix {canonical_new_prefix!r} 형식 위반 — {reason}",
                    file=sys.stderr,
                )
                return 1
        # fold-매치 기존 prefix는 그 canonical case로 무마찰 재사용한다.
        # 사용자가 쓴 번호 마디의 zero-pad 폭은 reid 기존 계약대로 보존한다.
        new_id = f"T-{canonical_new_prefix}-{requested_new_id.rsplit('-', 1)[1]}"

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
        # canonical ID 가 정확히 old_id 인 레코드만 고른다. 스냅샷은 **한 번만** 뜬다 — OLD 선택과
        # NEW collision 판정이 같은 스냅샷을 봐야 그 사이의 변화로 판정이 갈리지 않는다.
        tickets, scan_rc = areas_registry._prefix_scan(_home(), "reid")
        if tickets is None:
            return None, scan_rc
        matches = [t for t in tickets if t["id"] == old_id]
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
        fm, _ = ticket_store.load_ticket(path)
        # 타 세션 claim 가드 — 단일세션 op(migrate-identity·prefix rename 동류). claim 중인
        # 티켓은 그 소유 세션만 reid 할 수 있다(다른 세션이 작업 중인 ID 를 바꿔 참조를 흔들지 않게).
        # 적용 경로에서는 아래 `_prefix_relabel`이 fresh map/소유 검사를 board_lock 안에서 다시
        # 수행한다. 이 선판정은 승인 안내를 위한 preview이며 쓰기 권위가 아니다.
        # 소유 판정은 lifecycle mutation 과 **같은 함수**다(`_ticket_ownership`) — 판정을 두 벌로
        # 두면 한쪽만 user 축을 보는 식으로 갈린다. 문구만 이 surface(단일세션 op)의 것을 쓴다.
        ownership = ticket_access._ticket_ownership(_home(), fm, task=getattr(args, "task", None), user=getattr(args, "user", None))
        if not ownership.ok:
            remedy = ticket_rules._owner_session_remedy(ownership.owner or "")
            print(f"[중단] {old_id} 은 `{ownership.owner}` 가 claim 중 — reid 는 단일세션 op 다. "
                  f"{remedy}. 또는 먼저 unclaim 하라.",
                  file=sys.stderr)
            return None, 1
        # NEW collision — `_detect_collisions` 재사용(zero-pad 폭 정규화 포함). {OLD:NEW} 를 전 티켓
        # ID 집합에 적용해 두 티켓이 같은 논리 ID(전 상태 디렉토리+.drafts)로 떨어지면 잡는다.
        id_map = {old_id: new_id}
        collisions = _detect_collisions(id_map, {t["id"] for t in tickets})
        if collisions:
            sample = ", ".join(collisions[:5])
            print(f"[중단] NEW-ID {new_id} collision — 이미 존재하는 티켓과 번호가 겹친다({sample}). "
                  "미사용 ID 로 재부여하라(`board.py prefix list` 로 현황 확인).", file=sys.stderr)
            return None, 1
        return id_map, 0

    preview_notice = None
    pre_write_check = None
    if dry_run:
        if new_prefix_is_new:
            preview_notice = (
                f"[dry-run] 실행 시 새 prefix {requested_new_prefix!r} 신설에는 사용자 "
                f"승인이 필요하다 — 승인한 사용자만 `--user-ack {requested_new_prefix}`를 "
                "대상값 그대로 붙여 실행하라."
            )
    else:
        # rename/merge와 동형: OLD 미존재·NEW collision 등으로 실제 맵이 없으면
        # 승인을 요구하지 않고, 쓰기 계획이 있을 때만 값-결속 게이트를 연다.
        planned, rc = build_map()
        if not planned:
            return rc
        if requested_new_prefix is not None:
            canonical = areas_registry.require_prefix_user_ack(
                _home(), requested_new_prefix,
                getattr(args, "user_ack", None),
                surface="board reid NEW-ID prefix",
            )
            if canonical is None:
                return 1
            pre_write_check = lambda: areas_registry.revalidate_prefix_user_ack(
                _home(), requested_new_prefix,
                getattr(args, "user_ack", None),
                canonical,
                surface="board reid NEW-ID prefix",
            ) is not None

    return _prefix_relabel(
        build_map,
        verb="",
        label=f"{old_id} → {new_id}",
        dry_run=dry_run,
        noun="reid",
        pre_write_check=pre_write_check,
        preview_notice=preview_notice,
    )


def cmd_show(args: argparse.Namespace) -> int:
    """명세 전문 + 라운드 파일들을 순번 순으로 이어 보여준다.

    티켓 하나는 명세 파일 하나와 라운드 파일 N 개다 — 조회는 그 둘을 한 화면에 조립하는
    자리다. 라운드 렌더(구분선·산출 없음 표시)는 사이드카 seam 이 소유하고, 리뷰 finding
    요약은 versioned 블록에서만 파생한다(산문에 같은 항목을 다시 적지 않는다).
    """
    try:
        status, path = ticket_store.find_ticket(_home(), args.id)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    # board-git freshness 표면화 (advisory·stderr) — list 와 같은 seam 소환 + 이
    # 티켓 단위 로컬↔원격 status 불일치 1줄.
    #
    # 대조할 원격이 아예 없는 형상(board 비-git · upstream 미설정)은 **둘 다 무출력**이다:
    # 신선도라는 개념 자체가 없는데 advisory 를 내면 조회 표면에 무의미한 경고가 상주한다
    # (upstream 판별은 네트워크 0 인 기존 `_board_git_upstream` seam 이 한다). `list` 는
    # 현행 동작 그대로 둔다(이 게이트는 show 경로에만 있다).
    if board_git._board_git_enabled(_home()) and board_git._board_git_upstream(_home()) is not None:
        snapshot_verified = board_list._print_board_freshness(_home())
        board_list._print_ticket_remote_mismatch(_home(), args.id, status,
                                      snapshot_verified=snapshot_verified)
    print(f"-- {args.id} ({status}/) --\n")
    body = fs.read_text_shared(path, encoding="utf-8")
    print(body)
    try:
        rounds = round_seed.load_rounds(pm_paths.tickets_dir(_home()), args.id, ticket_text=body)
    except round_rules.RoundsError as exc:
        # 표시면은 조회를 막지 않는다 — 사유는 그 자리에 loud 하게 남기고 판정은 lint·
        # 완료 게이트(같은 seam)가 한다.
        print(f"⚠ 라운드 조회 실패: {exc}", file=sys.stderr)
        return 0
    if rounds:
        print(f"-- 라운드 {len(rounds)}건 --\n")
        print(round_rules.render_rounds_for_show(rounds))
        summary = review_state.render_pm_review_summary(body, rounds)
        if summary:
            print(summary)
        pending = [item for item in rounds if item.pending]
        if pending:
            print(f"-- 미회수 {len(pending)}건: "
                  + ", ".join(item.path.name for item in pending) + " --")
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
    for status in ticket_rules.IDEA_STATUS_DIRS:
        if args.status and args.status != status:
            continue
        for p in sorted((IDEAS_DIR / status).glob("[0-9]*.md")):
            loaded = ticket_store.load_ticket_soft(_home(), p)
            if loaded is None:
                continue
            fm, _ = loaded
            if args.tag and args.tag not in ticket_rules._tag_values(fm):
                continue
            rows.append((status, fm))
    if not rows:
        print("(no ideas)")
        return 0
    for status, fm in rows:
        tags = ",".join(ticket_rules._tag_values(fm))
        iid = str(fm.get("id") or "")
        title = (fm.get("title") or "")[:60]
        print(f"  [{status:8s}] {iid:6s} {title:60s}  {tags}")
    return 0


def cmd_idea_new(args: argparse.Namespace) -> int:
    iid = ticket_store._next_idea_id(_home())
    slug = ticket_rules._slugify(args.title)
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
    ticket_store.dump_ticket(path, fm, body)
    print(f"created idea {iid} ({pm_paths._rel_to_repo(_home(), path)})")
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
        status, path = ticket_store.find_idea(_home(), iid)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        return 2
    if status != "open":
        print(f"cannot {verb} idea {iid}: currently in {status}/",
              file=sys.stderr)
        return 1
    fm, body = ticket_store.load_ticket(path)
    new_path = ticket_store.move_idea(_home(), path, dst_status)
    fm["status"] = dst_status
    fm["updated"] = datetime.date.today().isoformat()
    ticket_store.dump_ticket(new_path, fm, body)
    print(f"{dst_status} idea {iid} ({pm_paths._rel_to_repo(_home(), new_path)})")
    return 0


def cmd_idea_promote(args: argparse.Namespace) -> int:
    return _transition_idea(args.id, "promoted")


def cmd_idea_kill(args: argparse.Namespace) -> int:
    return _transition_idea(args.id, "killed")


def cmd_refresh(_args: argparse.Namespace) -> int:
    board_dashboard.refresh_board(_home())
    print(f"board refreshed: {pm_paths._rel_to_repo(_home(), pm_paths.wiki_dir(_home()) / "board.md")}")
    issues = lint_tickets()
    if issues:
        print(f"⚠️  {len(issues)} lint issue(s) — run `board.py lint` for detail",
              file=sys.stderr)
    return 0


def cmd_git_anchor(args: argparse.Namespace) -> int:
    """하네스 훅용 JSON 판정 표면. 한 줄 JSON만 출력한다.

    ``--grants`` 를 주면 이 엔진 루트의 사용자 1회 허용을 쓸 수 있는 하네스로 판정한다
    (정확히 같은 명령·같은 세션의 허용 기록이 있으면 소비한다).
    """
    judgment = judge_git_anchor_command(
        REPO, args.session, args.cwd, args.command, grants=args.grants,
    )
    board_dispatch._write_machine_line(json.dumps(judgment, ensure_ascii=False))
    return 0


def cmd_git_grant(args: argparse.Namespace) -> int:
    """사용자 프롬프트 훅 전용 — stdin 프롬프트의 `git 허용:` 줄을 이 엔진 루트의 허용 기록으로 남긴다."""
    prompt = sys.stdin.buffer.read().decode("utf-8")
    try:
        granted = record_git_grants(REPO, args.session, args.cwd, prompt)
    except ValueError as exc:
        sys.exit(f"[중단] {exc}")
    board_dispatch._write_machine_line(json.dumps({"granted": granted}, ensure_ascii=False))
    return 0


def cmd_git_tool_input(args: argparse.Namespace) -> int:
    """하네스 훅용 — stdin JSON 도구 입력의 판정(허용 위조·슬롯 쓰기 주인)을 한 줄 JSON 으로 출력한다."""
    tool_input = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    judgment = judge_tool_input(REPO, args.session, args.cwd, args.tool, tool_input)
    board_dispatch._write_machine_line(json.dumps(judgment, ensure_ascii=False))
    return 0


def _sidecar_document(path: Path) -> dict | None:
    """기계 기록 sidecar 한 개를 읽는다 — 부재·손상·비-dict 는 None(판정 없음)."""
    try:
        data = json.loads(fs.read_text_shared(path, encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def status_snapshot() -> dict:
    """board 현황 기계 조회 — 상태별 티켓 수 · open 목록 · lint 판정 · 회귀/라이브게이트 기록.

    `pm-config status --json --with-board` 가 부르는 **얇은 조립**이다. 수집은 전부 기존 함수가
    하고(`_all_tickets` · `lint_tickets` + `_run_lint_hooks` · 회귀/라이브게이트 sidecar) 여기선
    dict 로 묶기만 한다 — `cmd_list`·`cmd_lint` 의 필터·출력·종료코드 경로는 건드리지 않는다
    (판정 사본 0). lint 는 **목록으로만** 실린다: 이슈가 있어도 이 함수는 실패하지 않는다(rc 는
    `cmd_lint` 의 관심사다).

    세는 축은 `STATUS_DIRS` 다섯이다 — `.drafts`(발행 전 초안)는 그 밖이라 세지 않는다.
    sidecar 좌표는 push 보호훅이 쓰는 것과 **같은 해소**를 거친다(사본/cwd 별 오독 차단).
    """
    counts = {status: 0 for status in ticket_rules.STATUS_DIRS}
    open_rows: list[dict] = []
    for status, front_matter in ticket_store._all_tickets(_home()):
        counts[status] = counts.get(status, 0) + 1
        if status == "open":
            open_rows.append({
                "id": front_matter.get("id"),
                "title": front_matter.get("title"),
                # open 티켓의 `claimed_by` 는 불변식상 null 이다(`lint_ticket_status_fields`) —
                # 값이 실리면 그 자체가 드리프트 신호라 접지 않고 그대로 보인다.
                "claimed_by": front_matter.get("claimed_by"),
            })
    open_rows.sort(key=lambda row: str(row.get("id") or ""))
    lint: list[dict] = [
        {"kind": kind, "message": f"{subject}: {detail}"}
        for subject, kind, detail in lint_tickets()
    ]
    lint.extend({"kind": hook_name, "message": detail}
                for hook_name, detail in lint_hooks._run_lint_hooks(_home()))
    return {
        "counts": counts,
        "open": open_rows,
        "lint": lint,
        "regression": _sidecar_document(regression_store._resolve_regression_flag(_home(), str(REPO))[0]),
        "livegate": _sidecar_document(regression_store._resolve_livegate_flag(_home(), str(REPO))[0]),
    }


def cmd_lint(args: argparse.Namespace) -> int:
    """전체 lint 보고 (무인자) 또는 push 게이트 (`--gate`).

    무인자: 모든 issue 를 보고하고 issue 가 하나라도 있으면 종료코드 1 (현행 동작).
    `--gate`: 종료코드를 *차단 카테고리*에만 1 로 둔다 — status drift 같은 자문성
    (lint_status 의 "never blocks" 보장) 은 보고는 하되 종료코드에 반영하지 않는다.
    즉 `--gate` 는 pre-push 게이트용 엄격 부분집합이다.

    `--gate` 진입은 **훅 세대 판정**도 함께 탄다(`_stale_pre_push_hook_refusal`). 회귀 게이트는
    `tests/` 있는 트리에서만 도는데, 세대 판정의 호출자가 거기뿐이면 스위트 없는 트리(분리 형상
    PM 홈)는 다음 세대 교체 처방을 받을 자리가 없다 — 반면 lint 줄은 **두 형상 모두** 훅의 마지막
    줄로 항상 돈다. 새 게이트가 아니라 기존 판정의 부착 지점을 하나 더 두는 것이고, 판정은 lint
    수집보다 **앞**이다(옛 훅 아래서 통과한 lint 가 push 허가로 읽히지 않게).
    """
    gate = getattr(args, "gate", False)
    if gate:
        stale_hook = pre_push_hook._stale_pre_push_hook_refusal(_home())
        if stale_hook is not None:
            print(stale_hook, file=sys.stderr)
            return 1
    issues = lint_tickets()
    hook_issues = lint_hooks._run_lint_hooks(_home())
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


# ── 완료 조건(DoD) 기록 게이트 (complete 차단 — T-0596) ────────────────────────
#
# DoD 항목이 미체크인 채 done 으로 넘어가면 그 항목은 **증발**한다 — 보드 어디에도 "남았다"는
# 기록이 없어 후속 세션이 이미 끝난 일로 읽는다(실사고: 라이브 probe DoD 가 기계 표면 없이 사라짐).
# 그래서 done 이행의 통과 조건을 두 형태로 못박는다:
#   `- [x] <원문>`                       = 실제로 했다.
#   `- [>] <원문> (이월: <사유·귀속>)`    = 안 했고, 사유·귀속을 남기고 이월했다.
# 그 밖(미체크 `- [ ]`·사유 없는 `- [>]`·알 수 없는 마커)은 전부 차단이다 — 판정이 문법이라
# 사람/에이전트의 해석 재량이 없다. 검사 범위는 `## 완료 조건` **절 안의 체크박스만**이다
# (메모·설계 절의 체크박스는 DoD 가 아니다). 소급 검사는 없다 — 이미 done 인 티켓은 안 본다.
_DOD_SECTION = "## 완료 조건"
_DOD_CHECKED_MARKS = frozenset({"x", "X"})
_DOD_DEFERRED_MARK = ">"
# 체크박스 줄: `- [ ] …` / `* [x] …` / `+ [>] …`(들여쓰기 허용·마커 길이 무제한).
# 불릿 세 종(`-`·`*`·`+`)을 모두 본다 — markdown 이 셋 다 리스트로 렌더하므로 하나라도 빼면
# 그 표기의 미체크 항목이 조용히 통과한다(fail-open 방향). 마커 길이를 열어 두는 이유도 같다 —
# 빈 브라켓(`- []`)과 **다문자 마커**(`- [xx]`)는 사람 눈에 체크박스지만 0~1글자만 보면 이 줄이
# 체크박스로 인식조차 안 돼(=판정 대상 밖) 미체크 항목이 통과했다. 이제 브라켓 불릿은 전부
# 체크박스로 보고, 통과 마커(`x`·`X`·`>`) 밖은 **미지 마커**로 차단한다.
#
# 링크는 체크박스가 아니다 — `- [라벨](경로)`·`- [라벨][ref]` 는 markdown 링크 문법이라 닫는
# 브라켓 뒤에 `(`/`[` 가 붙는다. 그 두 형태만 lookahead 로 제외한다(정상 본문 오차단 0).
# `- [[wikilink]]` 는 여는 브라켓이 겹쳐 애초에 매칭되지 않는다.
_DOD_CHECKBOX_RE = re.compile(
    r"^\s*[-*+]\s+\[(?P<mark>[^\[\]]*)\](?![\(\[])\s?(?P<text>.*)$")
# 이월 사유: 같은 줄의 `(이월: <사유·귀속>)` — 괄호 안이 비면 사유가 아니다.
_DOD_DEFERRED_REASON_RE = re.compile(r"\(이월:\s*\S[^)]*\)")
# 사람 표면(차단 메시지)의 두 통과 형식 — 한 곳에서 렌더한다.
_DOD_VALUE_FORMS = "`- [x] <원문>` 또는 `- [>] <원문> (이월: <사유·귀속>)`"


def _dod_section_texts(body: str) -> list[str]:
    """`## 완료 조건` 절 본문 **전부** — 공용 슬라이서(`_section_texts`)의 DoD 이름(정확 일치).

    존재 판정은 설계 절과 같은 규칙(`exact`)이다: 헤딩 줄이 그 이름 자체이거나 괄호 부제만
    붙어야 한다(`## 완료 조건 (Definition of Done)`). 자유 접두 일치는 `## 완료 조건 검토 이력`
    같은 **다른 절**을 DoD 로 잡아 실제 DoD 를 통째로 가린다 — 그 절엔 체크박스가 없으니 미체크
    항목이 있어도 완료가 통과했다(fail-open).

    **중복 절은 합산한다**(거부하지 않는다). 기존 판정 구조가 "있는 체크박스의 미결만 막는다"
    (절 부재·체크박스 0개는 무영향)이므로, 새 형식 거부를 만드는 것보다 판정 대상을 전 절로
    넓히는 쪽이 그 구조와 같은 축이다 — 우회(앞 빈 절로 뒤 절 가리기)는 그대로 닫히고 레거시
    본문에는 새 차단 사유가 생기지 않는다."""
    return markdown_text._section_texts(body, _DOD_SECTION, exact=True)


def _dod_heading_mismatch(body: str) -> str | None:
    """`## 완료 조건` 으로 시작하지만 절 이름 규칙에 안 맞는 헤딩 줄 (없으면 None).

    정확 일치 규칙은 오인 매칭을 닫는 대신 **새 사각**을 만든다: `## 완료 조건 (DoD) — 부제`
    처럼 괄호 밖 부제가 붙은 헤딩은 어느 절로도 안 잡혀 DoD 가 통째로 안 보인다(미체크 항목이
    있어도 통과 — 닫으려던 그 형상 그대로). 그래서 "정확 일치 실패 + 접두는 일치"를 조용한 통과가
    아니라 차단 사유로 표면화한다. 펜스 안 인용 헤딩은 슬라이서와 같은 규칙으로 먼저 걷는다."""
    if markdown_text._section_text(body, _DOD_SECTION, exact=True) is not None:
        return None
    lines = markdown_text._FENCED_CODE_RE.sub("", body).splitlines()
    return next((line.strip() for line in lines if line.startswith(_DOD_SECTION)), None)


def _dod_open_items(body: str) -> list[str]:
    """done 을 막는 DoD 사유 목록 — 전부 통과하면 빈 리스트.

    `_complete_gate` 가 쓰는 단일 깔때기다(ticket_finish 의 선-검사도 이 함수를 부른다).
    막는 형상 넷:

      1. **절 부재** — `## 완료 조건` 절이 없다. 판정할 완료 조건이 없으면 완료를 증명할 수
         없다. 레거시 면제는 두지 않는다 — 같은 판정이 `lint`(thin·`_REQUIRED_SECTIONS`)로
         이미 open/claimed 를 차단하고 있어 이 게이트로 새로 잠기는 활성 티켓이 없다.
      2. **헤딩 미인식** — 절 이름 규칙 밖 헤딩(기존 차단·`_dod_heading_mismatch`).
      3. **체크박스 0개**(산문 DoD) — 기계로 마감을 판정할 항목이 없다.
      4. **전량 이월** — 항목은 있는데 **전부** 사유 있는 이월이라 실제로 한 게 하나도 없다.
         그건 구현 완료가 아니라 처분이므로 `discard` 로 종결한다. 부분 이월(`[x]` ≥1)은
         통과다. 미체크·미지 마커가 하나라도 섞이면 처분이 아니라 **미완료**이므로 이 진단을
         내지 않는다 — 그 항목들은 위 형상들이 각각 지목한다(틀린 방향 안내 금지).

    `## 완료 조건` 절이 여럿이면 **전 절을 합산**한다 — 첫 절만 보면 앞에 빈 절 하나를 두고 뒤
    절에 미체크 항목을 남기는 우회가 성립한다.
    """
    sections = _dod_section_texts(body)
    if not sections:
        heading = _dod_heading_mismatch(body)
        if heading is None:
            return [f"DoD 절 부재: `{_DOD_SECTION}` 절이 없어 완료를 판정할 항목이 하나도 "
                    f"없다 — 절을 채우고 각 항목을 {_DOD_VALUE_FORMS} 로 마감하라"]
        return [f"DoD 절 헤딩 형식: {heading!r} — 절을 식별할 수 없어 미체크 항목을 판정하지 "
                f"못한다. `{_DOD_SECTION}` 또는 `{_DOD_SECTION} (부제)` 형식으로 쓰라"]
    problems: list[str] = []
    checkbox_count = 0
    checked_count = 0
    deferred_count = 0        # 사유가 붙은 유효 이월만 센다 — 전량 이월 판정의 분자.
    for line in "\n".join(sections).splitlines():
        match = _DOD_CHECKBOX_RE.match(line)
        if match is None:
            continue
        checkbox_count += 1
        # 마커는 좌우 공백을 걷고 본다 — `- [ x ]` 는 사람이 체크한 표기다. 걷고도 남는 다문자
        # (`xx`)는 통과 형식이 아니므로 미지 마커로 차단한다(비인식=통과 폐쇄).
        mark = match.group("mark").strip()
        text = match.group("text").strip()
        if mark in _DOD_CHECKED_MARKS:
            checked_count += 1
            continue
        if mark == _DOD_DEFERRED_MARK:
            if _DOD_DEFERRED_REASON_RE.search(text):
                deferred_count += 1
                continue
            problems.append(
                f"DoD 이월 사유 누락: {text!r} — 이월은 같은 줄에 `(이월: <사유·귀속>)` 필수")
            continue
        unknown = f"마커 `[{mark}]` 는 통과 형식이 아니다 · " if mark else ""
        problems.append(f"DoD 미체크: {text!r} — {unknown}{_DOD_VALUE_FORMS} 로 마감하라")
    if checkbox_count == 0:
        problems.append(
            f"DoD 체크박스 0개: `{_DOD_SECTION}` 절에 기계로 판정할 항목이 없다(산문 DoD) — "
            f"각 항목을 {_DOD_VALUE_FORMS} 형식으로 적어라")
    elif deferred_count == checkbox_count:
        problems.append(
            f"DoD 전량 이월(사유 있는 이월 {deferred_count} / 체크박스 {checkbox_count}개): "
            "구현된 항목이 하나도 없다 "
            "— done 이 아니라 **처분**이다. 병합·폐기라면 "
            f"`board.py discard <ID> {'|'.join(ticket_rules.DISPOSITION_KINDS)} --reason <사유>` 로 "
            "종결하라(부분 이월은 `[x]` 가 하나 이상일 때 통과한다)")
    return problems


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
# 분리한다 — 키 부재 회귀 0.


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
    if not wiki_lint._FAMILY_SCOPE_RE.match(new_scope):
        print(f"invalid --to scope {new_scope!r}: `shared` 또는 repo prefix "
              "(영숫자·-·_) 여야 함.", file=sys.stderr)
        return 1
    path = Path(target)
    if not path.is_absolute():
        path = (REPO / target).resolve()
    else:
        path = path.resolve()
    if not wiki_lint._in_scope_aware_dir(_home(), path):
        print(f"refusing to retag {pm_paths._rel_to_repo(_home(), path)}: scope 승격은 decisions/·specs/ "
              "문서만 대상 (ADR/spec scope 승격 명령).", file=sys.stderr)
        return 1
    if not path.exists():
        print(f"file not found: {pm_paths._rel_to_repo(_home(), path)}", file=sys.stderr)
        return 2
    try:
        fm, body = ticket_store.load_ticket(path)
    except ValueError as exc:
        print(f"cannot retag {pm_paths._rel_to_repo(_home(), path)}: {exc}", file=sys.stderr)
        return 1
    old_scope = wiki_lint.family_scope(fm)
    if old_scope == new_scope and isinstance(fm.get("family_scope"), str):
        print(f"family_scope already {new_scope!r} — no change ({pm_paths._rel_to_repo(_home(), path)})")
        return 0
    fm["family_scope"] = new_scope
    ticket_store.dump_ticket(path, fm, body)
    print(f"promoted scope {old_scope!r} → {new_scope!r} ({pm_paths._rel_to_repo(_home(), path)})")
    return 0


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

    manifest_store.read_manifest 를 재사용해 `.render` 플래그가 True 인 항목만 모은다. manifest
    부재·파싱 실패는 빈 set(검사 대상 0·무발화). manifest 의 @render path 가 디렉토리면 그
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

    순수 manifest 파생 — render-leak(`_render_managed_relpaths` 가 트리성격 게이트 후 호출)의
    스코프 소스다. 출하 인벤토리(engine.manifest `@render` = 프레임워크가 렌더-관리하는 어댑터
    본문 트리)에서 스코프를 파생하므로, 새 하네스·새 어댑터 항목이 manifest 에 등재되면 자동
    편입된다(손-열거 0). config·hooks·엔진 코드·`node_modules` 등 런타임/설정
    파일은 `@render` 가 아니라 `@source`(byte-copy)/미등재라 이 집합에 애초에 안 든다 — 스코프가
    구조적으로 어댑터 본문으로 좁혀진다. manifest_store.read_manifest 재사용. manifest 부재는 빈
    set(검사 대상 0·무발화)."""
    managed: set[str] = set()
    for manifest_path in _engine_manifest_paths():
        try:
            for entry in manifest_store.read_manifest(manifest_path):
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
    """pm_update 모듈을 같은 tools/ 디렉토리에서 로드 (drift 판정·출하 타깃 탐색·스킬 표기 렌더·텍스트 판정 재사용).

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
                for entry in manifest_store.read_manifest(manifest)
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
        source_bytes = fs.read_bytes_shared(path)
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
                    drifted = (not exists) or fs.read_bytes_shared(candidate) != expected
        except (OSError, UnicodeError, RuntimeError, AttributeError):
            drifted = True  # 사본 상태를 확인 못 했다 — 확인 못 한 것을 면제하지 않는다(보수).
        matched += 1
        if drifted:
            drifted_targets.append(template_root.name)
    if drifted_targets:
        return _TEMPLATE_MIRROR_DIFFERS, sorted(drifted_targets)
    return (_TEMPLATE_MIRROR_IDENTICAL if matched else _TEMPLATE_MIRROR_ABSENT), []


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

    fail-soft: manifest 부재·파싱 실패·파일 read 오류 → 그 부분 skip(검사 대상 0·신규 무영향).
    """
    managed = _render_managed_relpaths()
    if not managed:
        return []  # @render path 0 → 검사 대상 0 (활성화 전 무발화).
    # 텍스트 판정은 pm_update._is_text_source 를 **공유**한다: render-leak 은
    # `.md` 뿐 아니라 @render 산출물 하위 *모든 텍스트 파일*(.toml·.json·.yaml·확장자 없는 텍스트
    # 등)의 미해소 토큰을 잡아야 한다 — `.md` 만 스캔하면 새 하니스 형식을 조용히 놓치는 클래스
    # (codex `.codex/agents/*.toml`)가 이 blocking 백스톱을 통과한다. 확장자 열거·판정 사본을 새로
    # 만들지 않고 render 채널(pm_update.plan)이 쓰는 그 함수에 위임한다(네 번째 판정 지점 방지).
    # pm_update 적재가 None 이면 아래 read 의 넓힌 except 가 바이너리를 흡수한다(graceful degrade).
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
            rel_posix = pm_paths._rel_to_repo(_home(), p).replace("\\", "/")
            if rel_posix in seen:
                continue
            seen.add(rel_posix)
            # 바이너리 리소스(폰트·이미지 등)는 스캔 대상 아님 — 텍스트 판정 공유(위 주석).
            if pm_update is not None and not pm_update._is_text_source(p):
                continue
            try:
                # rglob("*") 로 넓히면 바이너리가 섞여 UnicodeDecodeError 로 죽을 수 있어
                # OSError 와 함께 잡는다 (_is_text_source 통과 후 TOCTOU·pm_update None 폴백 안전판).
                text = fs.read_text_shared(p, encoding="utf-8")
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
#   - adapter-drift : 채택자의 adapter-layer(facade·진입문서·settings) 가 baseline(마지막 동기) 이후
#     전파 채널 없는 manifest-제외 잔여라 *전파 대신*
#     PM 에게 경고만 — `pm-update` 안내(visibility>enforcement). B 전파는 채택자 customization clobber(비파괴
#     위배)라 의도적 비-전파. instance-state(status·architecture·tickets·log·decisions·README·lite)는 채택자
#     소유·diverge 정상이라 scope 제외. push 미차단(never-block).
#   - adr-author : ADR frontmatter `author: <user>/<pm-slot>` provenance 권고.
#     "누가 결정했나"(provenance·연속성 아님)를 박는 발행측 규칙 — board.py 는 ADR 을 발행하지 않으므로
#     부재/형식어긋남을 권고만 한다. 구 ADR(author 부재)은 정상이라 push 미차단(never-block).
#   - architecture-stale·status-stale·domain-stale : 현재-진실 문서 freshness — 문서
#     frontmatter `verified_at: <sha>` 이후 그 문서 매핑 경로에 커밋이 있으면 "재검증 필요"
#     권고(date 비교 대체). architect 재검증·PM 점검 대상이지 push 결함이 아니므로
#     visibility 만·never-block.
#   - areas-repo-unregistered : areas.md 에 등록된 repo 행이 0개. init 이 repo 행을 항상
#     등록하기 전에 setup 된 구 clone 이라는 뜻이라, 처방(init 재실행) 1줄만 상시 보인다 —
#     무prefix 발행 자체는 1급 형상이라 push 결함이 아니다(never-block).
#   - areas-duplicate-repo : areas.md 에 같은 repo 행이 2개 이상. first-match 리졸버
#     4종이 첫 행에서 return 하므로 조용히 굳는 걸 보이게만 한다 — 자동 병합은 사람 판정 영역이고
#     레지스트리 정리가 push 결함도 아니라 never-block(`areas_set_cell` 의 fail-loud 와 짝).
#   - areas-merge-union : areas.md 를 담은 git 에 `merge=union` 이 선언되지 않음. 동시 등록
#     안전(양쪽 행 보존)이 사라진 상태를 보이게만 한다 — 엔진이 backfill 하는 채널이 이미 있고
#     (`_ensure_board_gitattributes`) 채택자가 자기 `.gitattributes` 를 가질 수 있어 never-block.
#   - design-pending : 티켓 설계 단계(`design: required`) 미완 — 설계 절 미충전 또는 필드
#     미승격. 차단은 **그 티켓을 실제로 집는 claim 게이트**가 하므로, 전역 lint 는 남의
#     설계-중 티켓이 내 push 를 막지 않게 visibility 만 준다(never-block).
#   - design-estimate : `estimate=large` 인데 `design: n/a`(또는 필드 부재). 자동 required 는
#     발행 시점 1회뿐이라 estimate 를 사후 교정한 티켓엔 설계 게이트가 영영 안 붙는다. 올릴지
#     면제할지는 **사람 판정**이라 엔진은 재유도만 한다(never-block).
#   - codex-delegate-matcher-miss : SubagentStart 는 `(session_id, turn_id, agent_id)`로 식별하고,
#     같은 session 의 선행 allow-spawn을 FIFO 1:1 대조했을 때 대응 PreToolUse 처리 기록이 없는
#     start를 가시화한다. 이미 시작된 native spawn의 사후 advisory라 push는 막지 않는다.
#     (`SubagentStart.agent_type="default"`는 역할도 상관 키도 아니다 — 라이브 payload 실측)
_ADVISORY_LINT_KINDS: frozenset[str] = frozenset(
    {"status-done-accum", "unstable-ref-advice", "scope-advice", "coverage",
     "stale", "orphan", "oversized", "history", "adr-lifecycle", "architecture-stale",
     "status-stale", "domain-stale", "domain-unverifiable",
     "architecture-unverifiable", "status-unverifiable",
     "dangling-wikilink-scaffold", "adapter-drift",
     "adr-author", "areas-repo-unregistered", "areas-duplicate-repo", "areas-merge-union",
     "delegate-same-model", "design-pending", "design-estimate",
     "codex-delegate-matcher-miss", "open-claimed-contradiction", "local-conf",
     "local-conf-unknown-key", "local-conf-missing-required",
     "local-conf-unreadable",
     # 라운드 판정 코드(사이드카 seam 소유) + board 고유 잔여·판정불능 관측. 차단은 완료
     # 게이트가 한다.
     "round-name", "round-gap", "round-dup", "round-pending", "round-temporary",
     "round-stray", "round-unreadable",
     # 클러스터 장부 관측 — 운영 재료지 push 결함이 아니다(never-block · `lint_clusters`).
     ticket_lint._CLUSTER_MEMBER_LINT_KIND, ticket_lint._CLUSTER_DUPLICATE_LINT_KIND})


# ── verified_at 핀 쓰기 (repin) ────────────
# 현재-진실 문서(architecture.md·status.md·covers 보유 domain 페이지)의 `verified_at`·`repo` 를 쓰는
# 채널은 `verified-at-repin` 하나다. 핀이 없는 문서도 `--page` 로 골라 같은 명령으로 처음 쓴다.

def _repo_head_sha(repo: Path | None = None) -> str | None:
    """`repo`(부재=REPO)의 현재 HEAD sha (조회 불가 → None·fail-soft)."""
    if shutil.which("git") is None:
        return None
    repo = Path(repo) if repo is not None else REPO
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=git._BOARD_GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):  # 타임아웃/바이너리 이상은 None(fail-soft).
        return None
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def _canonical_commit_oid(sha: str, *, repo: Path | None = None) -> str | None:
    """`sha` 가 유효한 backward anchor 면 그 **canonical full OID** 를, 아니면 None (repin 쓰기).

    repin 이 **검증에서 얻은 full OID 를 그대로 기록**하게 하는 helper — 입력 축약/
    ref 문자열을 기록하면 (a) 하류 소비자가 다시 해석(canonical 위반) (b) `0123456` 같은 전부-숫자
    축약은 YAML unquoted 정수 파싱돼 재-lint 서 깨진다. `_sha_anchor_status` 재사용(read/write 대칭·
    사본 0): 고정 hex + 고정 SHA 해소(hex-이름 ref 아님) + HEAD 선조면 `(OK, full_oid)` →
    full_oid 반환·아니면 None. 검증 불가(git 부재·rc≥2 → UNKNOWN)도 None — 기록은 확실할 때만 한다."""
    verdict, full_oid = freshness_lint._sha_anchor_status(_home(), sha, repo=repo)
    return full_oid if verdict == freshness_lint._ANCHOR_OK else None


def cmd_verified_at_repin(args: argparse.Namespace) -> int:
    """`verified-at-repin` — 현재-진실 문서를 validate-all-first로 소유 repo full OID에 재핀한다."""
    owner = args.repo
    owner_repo, owner_error = freshness_lint._freshness_owner_repo(_home(), owner)
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
    results = freshness_pin.repin_verified_at(
        _home(),
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


def _load_delegate_channel_guard_module():
    """Load the guard's read-only Codex observation scanner (graceful)."""
    if not DELEGATE_CHANNEL_GUARD_PY.exists():
        return None
    try:
        return _load_module_from_path(
            DELEGATE_CHANNEL_GUARD_PY,
            "delegate_channel_guard.py",
            verifier=_verify_engine_rev,
        )
    except Exception as exc:  # noqa: BLE001 — advisory loader is fail-soft.
        if _is_engine_rev_skew(exc):
            raise
        return None


def lint_codex_delegate_observations() -> list[tuple[str, str, str]]:
    """Surface unmatched Codex SubagentStart observations once (never-block).

    The guard owns JSONL rotation and the idempotent set comparison.  Board lint
    only aggregates all retained misses into one PM-facing advisory line.
    """
    try:
        guard = _load_delegate_channel_guard_module()
        if guard is None:
            return []
        misses = guard.scan_codex_observation_misses(
            observation_path=CODEX_DELEGATE_OBSERVATIONS
        )
    except Exception as exc:  # noqa: BLE001 — observation visibility never blocks lint.
        if _is_engine_rev_skew(exc):
            raise
        return []
    if not misses:
        return []

    identities: list[str] = []
    for entry in misses:
        identity = "/".join(
            str(entry.get(field) or "?")
            for field in ("session_id", "turn_id", "agent_id")
        )
        if identity not in identities:
            identities.append(identity)
    sample = ", ".join(identities[:3])
    remainder = len(identities) - min(len(identities), 3)
    suffix = f" 외 {remainder}개 identity" if remainder else ""
    return [(
        "Codex SubagentStart",
        "codex-delegate-matcher-miss",
        f"일치하는 PreToolUse 처리 기록이 없는 SubagentStart {len(misses)}건 — "
        f"spawn matcher drift 또는 가드 미발화 가능({sample}{suffix}). "
        "append-only 관측은 소비하지 않았으며 native spawn은 사후 관측만 한다.",
    )]


def lint_tickets() -> list[tuple[str, str, str]]:
    """All lint issues — ticket dependency graph + body self-containment +
    idea status/directory agreement + status.md ✅ 완성 행 누적 권고(judgment-only) +
    dangling wikilink + unstable (slug/filename) refs +
    family wiki scope 인지+
    domain freshness advisory(stale/orphan/oversized/history·never-block) +
    architecture.md freshness advisory(architecture-stale·never-block) +
    adapter-layer drift advisory(adapter-drift·never-block·baseline rev 비교) +
    render-leak(리터럴 `{{...}}` 누출·blocking·@render 산출물 한정·활성화 전 무발화) +
    adr-author(ADR `author: <user>/<pm-slot>` provenance 권고·advisory·never-block) +
    현재-진실 문서 freshness(architecture-stale·status-stale·domain-stale·verified_at sha 판정·
    advisory·never-block) +
    areas-repo-unregistered(areas.md 등록 repo 행 0 → 구 clone 이관 안내·advisory·never-block) +
    areas-duplicate-repo(areas.md 중복 repo 행 → first-match 고착 가시화·advisory·
    never-block) +
    areas-merge-union(areas.md 를 담은 git 에 merge=union 미배포 → 동시 등록 안전 상실 가시화·
    advisory·never-block) +
    delegate-same-model(delegate dev/reviewer 동일-모델 해소 → 맹점 공유로 인한 검출력 저하 가시화·
    advisory·never-block) +
    codex-delegate-matcher-miss(unmatched SubagentStart → PM 표면 1줄·advisory·never-block) +
    round-*(라운드 순번 유일성·연속성·산출 없음·임시 파일 잔여 가시화·advisory·never-block·
    차단은 완료 게이트) + legacy-growth-section(명세 본문에 남은 구 역할 절 → 사람이
    라운드 파일로 옮기거나 marker 를 지워 해소·blocking) +
    design-pending(티켓 설계 단계 `design: required` 미완 — 설계 절 미충전/필드 미승격 가시화·
    advisory·never-block·차단은 claim 게이트) +
    open-claimed-contradiction(status=open 인데 claimed_by 잔존 — 상태-소유 모순 가시화·
    advisory·never-block) +
    local-conf-unknown-key(local.conf 의 레지스트리 밖 키 — 오타/폐기 키 조용한 무시 표면화·
    advisory·never-block) +
    local-conf-missing-required(local.conf 에 필수 키 미선언 — 핵심 명령이 못 도는 형상 표면화·
    advisory·never-block) +
    local-conf-unreadable(local.conf 가 있는데 판독 실패 — 값 0 개로 위장되던 상태 표면화·
    advisory·never-block) +
    cluster-*(클러스터 장부의 멤버 부재·통합 브랜치 부재·중복 귀속 가시화·advisory·
    never-block)."""
    return (ticket_lint.lint_dependencies(_home()) + body_lint.lint_bodies(_home()) + body_lint.lint_ideas(_home())
            + wiki_lint.lint_status(_home())
            + wikilink_lint.lint_wikilinks(_home()) + wikilink_lint.lint_unstable_refs(_home()) + wiki_lint.lint_scopes(_home())
            + doc_lint.lint_domain(_home()) + wiki_lint.lint_adr_lifecycle(_home()) + wiki_lint.lint_adr_author(_home())
            + freshness_lint.lint_architecture_freshness(_home()) + freshness_lint.lint_status_freshness(_home())
            + doc_lint.lint_domain_freshness(_home()) + conf_lint.lint_adapter_drift(_home())
            + lint_render_leak()
            + areas_lint.lint_areas_repo_unregistered(_home())
            + areas_lint.lint_areas_duplicate_repo(_home()) + areas_lint.lint_areas_merge_union(_home())
            + conf_lint.lint_delegate(_home()) + conf_lint.lint_local_conf(_home()) + conf_lint.lint_local_conf_keys(_home())
            + conf_lint.lint_local_conf_required(_home()) + conf_lint.lint_local_conf_readable(_home())
            + ticket_lint.lint_rounds(_home()) + ticket_lint.lint_clusters(_home())
            + ticket_lint.lint_legacy_growth_sections(_home())
            + lint_codex_delegate_observations() + ticket_lint.lint_claim_identity(_home()))


# ── argparse ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="board.py",
                                     description="Multi-session ticket board.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list tickets")
    p.add_argument("--status", choices=(*ticket_rules.STATUS_DIRS, "all"),
                   help="status 뷰 셀렉터: 생략 시 활성만(open/claimed/blocked·done 접기)· "
                        "특정 status 하나만 보려면 그 값(예: done)· 전체(done 포함)는 `all`.")
    p.add_argument("--tag")
    p.add_argument("--mine", action="store_true",
                   help="내 것만 (렌즈·단일 보드 위 필터·user-first): 내 open"
                        "(area_owner==나 ∨ created_by==나) + 내 claim(claimed_by.user==나)·**전 task**. "
                        "querying identity=현재 사용자(local.conf identity.user= > git email). 타 사용자는 "
                        "안 나온다. user 미상이면 전체 open + 내 세션 claim 으로 graceful degrade. "
                        "`--task` 와 상호 배타(뷰 스코프는 하나만·cmd_list 런타임 검사).")
    # 렌즈는 `--task`·`--mine`·`--all`·무인자 기본 뷰다(슬롯·repo 렌즈 없음). 전체 보드(타 사용자
    # 포함)는 `list --all`로 본다.
    p.add_argument("--all", action="store_true",
                   help="전체 보드: 무인자 기본 뷰(내 세션 스트림만 = 생성 open + 내 claim)를 "
                        "끄고 모든 세션·타 사용자 티켓을 상세로 보인다(경합 가시·타 PM 열람). 뷰 스코프라 "
                        "`--mine`/`--task` 와 상호 배타.")
    identity_args.add_task_arg(p)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id", metavar="T-NNNN")
    identity_args.add_identity_args(p)
    p.add_argument("--user", help="user 식별자 — claimed_by 의 user 차원 (default: local.conf identity.user= / "
                   "git config user.email)")
    p.set_defaults(fn=cmd_claim)

    # claimed 티켓을 만지는 mutation 4종(complete·block·unclaim·unblock)과 처분(discard)은
    # `claimed_by` 와 실행 세션을 대조한다 — 그래서 claim 과 **같은** 정체성 `--task` 를 받는다
    # (폴더를 고르지 않으므로 위치 인자 `--repo`/`--slot` 은 없다·공용 `add_task_arg`).
    p = sub.add_parser("complete", help="mv claimed → done (sync gate enforced · 소유자만)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--tests-pass", action="store_true",
                   help="assert the regression suite passes "
                        "(required unless --allow-untested)")
    p.add_argument("--allow-missing-log", action="store_true",
                   help="bypass the log/current.md entry check")
    p.add_argument("--allow-untested", action="store_true",
                   help="bypass the regression check "
                        "(regression-irrelevant ticket)")
    # 보안 capability가 아니라 ClusterCloser의 사고 방지용 exact path-binding이다.
    # 사용자 완료 표면을 하나로 유지하려 help에서는 감춘다.
    p.add_argument("--cluster-close", default=None, help=argparse.SUPPRESS)
    identity_args.add_task_arg(p)   # claim 소유 대조용 세션 해소
    p.set_defaults(fn=cmd_complete)

    p = sub.add_parser("discard",
                       help="mv open|claimed|blocked → discarded (처분 종결 — 병합·폐기)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("disposition", metavar="|".join(ticket_rules.DISPOSITION_KINDS),
                   choices=ticket_rules.DISPOSITION_KINDS,
                   help="처분 종류 — merged(다른 티켓으로 흡수) · dropped(취소·폐기)")
    p.add_argument("--reason", required=True,
                   help="처분 사유 (병합 대상 티켓 ID 도 여기에 적는다)")
    identity_args.add_task_arg(p)
    p.set_defaults(fn=cmd_discard)

    p = sub.add_parser("reopen",
                       help="mv done|discarded → open (오처리 복구 — 종결을 되돌리는 유일한 문)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--reason", required=True, help="복구 사유 (본문·board-git 에 남는다)")
    p.set_defaults(fn=cmd_reopen)

    p = sub.add_parser("block", help="mv open|claimed → blocked (claimed 는 소유자만)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--reason", required=True)
    identity_args.add_task_arg(p)
    p.set_defaults(fn=cmd_block)

    p = sub.add_parser(
        "unclaim",
        help="mv claimed → open · blocked 는 상태 유지하고 소유만 해제 (소유자만 · "
             "소유 세션이 없으면 --takeover)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--takeover", action="store_true",
                   help="소유자 부재 이전 — 타 세션 claim 을 강제 해제한다(--reason 필수). "
                        "소유자 이전의 유일한 문이다.")
    p.add_argument("--reason", help="--takeover 사유 (티켓 본문·board-git 커밋에 남는다)")
    identity_args.add_task_arg(p)
    p.set_defaults(fn=cmd_unclaim)

    p = sub.add_parser(
        "unblock",
        help="mv blocked → claimed(claimed_by 보유·소유 보존) 또는 open(무소유) · 소유자만")
    p.add_argument("id", metavar="T-NNNN")
    identity_args.add_task_arg(p)
    p.set_defaults(fn=cmd_unblock)

    p = sub.add_parser("new", help="create a new ticket")
    p.add_argument("title")
    p.add_argument("--touches", help="comma-separated file paths")
    p.add_argument("--depends", help="comma-separated ticket IDs")
    p.add_argument("--tag", help="comma-separated tags")
    p.add_argument("--estimate", choices=["small", "medium", "large"],
                   default="small")
    p.add_argument("--design", metavar="값",
                   help=f"설계 단계 상태 ({design_rules.DESIGN_VALUE_FORMS}). 생략 시 "
                        f"estimate={design_rules.DESIGN_REQUIRED_ESTIMATE} → {design_rules.DESIGN_REQUIRED}, "
                        f"그 외 {design_rules.DESIGN_NA}. required 면 설계 절 완성 + `{design_rules.DESIGN_DONE}` 승격 "
                        f"전까지 claim 이 차단된다.")
    p.add_argument("--prefix", help="작업 카테고리 (자유 입력·배타 구획). 생략 시 task 설정 "
                   "prefix → 등록 prefix 가 정확히 1개면 그것 / 그래도 미해소면 "
                   "none(무prefix 1급 → legacy T-NNNN)")
    p.add_argument("--user-ack", metavar="<prefix>",
                   help="새 prefix 신설에 대한 사용자 승인 토큰(대상 prefix 값과 정확히 결속)")
    p.add_argument("--user", help="user 식별자 — created_by 의 user 차원 (default: local.conf identity.user= / "
                   "git config user.email)")
    # `--task` 필수(created_by = <user>/<task>) · `--repo`/`--slot` 은 그 task 가 보유한 폴더(코드
    # 트리·기준 브랜치)를 고르는 위치 인자 — claim 과 같은 실행 소비자 인자.
    identity_args.add_identity_args(p)
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("promote", help="draft(board-git 미커밋) 티켓을 승격 — 본문 채운 뒤 board-git sync "
                        "(발행 규율 게이트: board-git 공유 시 `new` 가 미충전 티켓을 draft 로 남긴다)")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=cmd_promote)

    # cluster subcommand group — 운영 단위(티켓 묶음) 장부. 쓰기(new)와 조회(show)가 섞여
    # 있으므로 leaf 별로 분류되는 서브그룹으로 둔다(`idea`·`prefix` 와 같은 형상).
    cluster_p = sub.add_parser("cluster", help="운영 단위(티켓 묶음) 장부")
    cluster_sub = cluster_p.add_subparsers(dest="cluster_cmd", required=True)

    cp = cluster_sub.add_parser(
        "new", help="장부 + 통합 브랜치 생성 · 멤버 귀속 · 겹침/슬롯 재료 "
                    "(코드 트리는 `--task` 의 폴더로 명시 필수·폴더가 2개 이상이면 `--repo/--slot` "
                    "위치 인자 추가 — 없으면 거부)")
    cp.add_argument("name", metavar="<ticket-group>",
                    help="클러스터 이름(장부 id 는 `C-<ticket-group>` · 통합 브랜치는 `task/<ticket-group>`)")
    cp.add_argument("--tickets", metavar="T-...,T-...", required=True,
                    help="멤버 티켓 ID 쉼표 구분. 발행이 만든 크기 1 장부는 흡수하고 이미 여러 "
                         "티켓의 묶음에 속한 티켓은 거부한다.")
    cp.add_argument("--spike", metavar="경로",
                    help="설계 단일 진실 문서 경로 (선택 · 장부에 기록만 한다)")
    identity_args.add_identity_args(cp)  # 통합 브랜치를 둘 코드 트리 — 명시 필수(claim 과 같은 규칙)
    cp.set_defaults(fn=board_cluster.cmd_cluster)

    cp = cluster_sub.add_parser("show", help="장부 1건 조회(선언값 + 멤버 현재 status)")
    cp.add_argument("name", metavar="<ticket-group>")
    cp.set_defaults(fn=board_cluster.cmd_cluster)

    p = sub.add_parser(
        "section-add",
        help="묶음 장부가 없는 티켓의 다음 라운드를 빈 시드로 예약(슬롯 없는 준비) — "
             "장부가 있는 티켓의 라운드는 위임 준비(`pm_delegate.py ticket prepare`)가 연다")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--role", required=True, choices=round_rules._round_role_choices(),
                   help="라운드 역할(파일명과 기본 절명을 함께 결정)")
    p.add_argument("--label", help="사람용 절명 오버라이드(예: 재설계)")
    p.set_defaults(fn=board_round.cmd_section_add)

    p = sub.add_parser("tier", help="open/claimed 티켓의 위임 tier 기록·갱신")
    p.add_argument("id", metavar="T-NNNN")
    # choices를 쓰면 argparse가 rc=2로 선점한다. 계약의 인식 불가 값 rc=1을 cmd_tier가 직접
    # 보장하도록 자유 문자열로 받고 허용집합은 런타임에서 검증한다.
    p.add_argument("tier", metavar="pm-direct|normal|hard")
    p.set_defaults(fn=board_round.cmd_tier)

    p = sub.add_parser(
        "design",
        help="open/claimed/draft 티켓의 설계 단계(design:) 값 발행 후 기록·갱신")
    p.add_argument("id", metavar="T-NNNN")
    # choices를 쓰면 argparse가 rc=2로 선점한다. 계약의 인식 불가 값 rc=1을 cmd_design이 직접
    # 보장하도록 자유 문자열로 받고 허용집합은 런타임에서 검증한다(`_validate_design`).
    p.add_argument("value", metavar=design_rules.DESIGN_VALUE_FORMS)
    p.set_defaults(fn=board_round.cmd_design)

    p = sub.add_parser(
        "tier-signals",
        help="touches의 h1 모듈·h2 공용 코드·문서-전용 신호 표시(advisory)",
    )
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=board_round.cmd_tier_signals)

    p = sub.add_parser("init", help="clone 당 1회 setup — areas repo 행 등록·pm_state·local.conf·pre-push 훅")
    p.add_argument("--prefix", help="ticket ID 작업 카테고리 (예: PAY). 생략 = 무prefix(T-NNNN·none 카테고리)")
    p.add_argument("--user-ack", metavar="<prefix>",
                   help="새 prefix 등록에 대한 사용자 승인 토큰(대상 prefix 값과 정확히 결속)")
    p.add_argument("--area", help="영역 설명 (새 prefix 최초 등록 시 필요)")
    p.add_argument("--owner", help="등록 식별자(registrant·기본: --task 값, 없으면 빈 칸)")
    p.add_argument("--user", help="area_owner = 그 area 의 user 소유 (`--mine` 풀 입력). "
                                  "미지정 시 local.conf identity.user= / git config user.email 로 해소(없으면 빈 값).")
    p.add_argument("--repo", metavar="이름", default=None,
                   help="areas.md 에 등록할 repo 이름(기본: 이 clone 루트 폴더명)")
    p.add_argument("--task", metavar="이름", default=None,
                   help="등록 식별자(owner) 기본값으로 쓸 task 이름")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("migrate-identity",
                       help="이전 데이터 일회성 backfill — areas area_owner·ticket "
                            "created_by·슬롯-only claimed_by·비종결 티켓의 옛 슬롯 세션 토큰 → "
                            "<user>/<task> (`--task` 필수·멱등·비파괴·dry-run 선검토). "
                            "일회성 전체 스캔 op. areas와 ticket RMW는 각각 board_lock으로 "
                            "lifecycle/tier writer와 직렬화한다.")
    p.add_argument("--dry-run", action="store_true",
                   help="변경 미리보기(쓰기 0·per-file 요약). 먼저 실행 권장.")
    p.add_argument("--user", help="identity override (기본: local.conf identity.user= / git config "
                   "user.email · 미해소 시 abort)")
    identity_args.add_task_arg(p)  # 필수 — 옛 슬롯 세션 토큰을 옮길 실행 사용자의 task
    p.add_argument("--scope", choices=["active", "all"], default="all",
                   help="active=open+claimed 만 · all=done 포함(기본)")
    p.set_defaults(fn=cmd_migrate_identity)

    p = sub.add_parser("regression",
                       help="회귀 게이트 (run=측정·기록 / check=HEAD green 검증·pre-push 훅용)")
    p.add_argument("action", choices=["run", "check"])
    p.add_argument("--cmd", help="테스트 명령 (기본: 활성 repo areas.md test_cmd → local.conf test.cmd → Python pytest 병렬 기본)")
    p.add_argument("--cwd", help="회귀 실행 cwd (seam·기본=이 트리 REPO · 슬롯 자동해소 없음 — "
                                 "다른 트리는 여기서 명시)")
    identity_args.add_identity_args(p)  # `--task`(+위치 인자) = 디스패치(M>1 순회 생략)와 그 폴더
    # 실행 · 실행 트리는 REPO(다른 트리는 `--cwd`/`--task` 로 명시) · M>1 홈에서 무명시면
    # 전 leased 슬롯 all-or-nothing
    p.add_argument("--ticket", help="이 ticket 의 touches 로 스코프 (dev 빠른 루프·advisory)")
    p.add_argument("--touches", help="comma-separated 파일로 스코프 (advisory)")
    p.add_argument("--final", action="store_true",
                   help="수렴 후 FULL 게이트 — 활성 리뷰 사이클 중에도 강등하지 않는다 "
                        "(pre-push 훅·핸드오프가 쓰는 경로)")
    p.set_defaults(fn=lambda args: regression_cmd.cmd_regression(_home(), args),
                   _board_subcommand="regression")

    p = sub.add_parser("livegate",
                       help="릴리즈 라이브 게이트 — record=`pytest -m release` "
                            "실행·수집 pin 강제·기록 / check=보호훅이 HEAD-매칭 green 검증")
    p.add_argument("action", choices=["record", "check"])
    p.add_argument("--rev", help="check 대상 sha (보호훅이 push HEAD 를 넘김·record 는 무시)")
    p.add_argument("--cwd", help="record=pytest 실행 cwd(기본=활성 slot worktree, 빈 값 및 "
                                 "--task/--repo/--slot과 혼합 불가, 경로는 --cwd만 지정) / "
                                 "check=livegate.json 해소 cwd(훅 정렬·기본=이 board.py 사본 REPO) "
                                 "(seam·record↔check 대칭)")
    identity_args.add_identity_args(p)  # record 의 task 폴더(M>1 홈에서 cwd 해소·regression 과 동형·
    # 무명시+leased≥2 는 fail-loud·`--cwd` 우회 불요)
    p.set_defaults(fn=cmd_livegate)

    p = sub.add_parser("git-anchor", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True, help=argparse.SUPPRESS)
    p.add_argument("--cwd", required=True, help=argparse.SUPPRESS)
    p.add_argument("--command", required=True, help=argparse.SUPPRESS)
    p.add_argument("--grants", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_git_anchor)

    p = sub.add_parser("git-grant", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True, help=argparse.SUPPRESS)
    p.add_argument("--cwd", required=True, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_git_grant)

    p = sub.add_parser("git-tool-input", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True, help=argparse.SUPPRESS)
    p.add_argument("--cwd", required=True, help=argparse.SUPPRESS)
    p.add_argument("--tool", required=True, help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_git_tool_input)

    p = sub.add_parser("refresh", help="regenerate board.md")
    p.set_defaults(fn=cmd_refresh)

    p = sub.add_parser("lint", help="check depends_on / blocks consistency")
    p.add_argument("--gate", action="store_true",
                   help="push 게이트 모드 — 차단 카테고리(dangling/unstable-ref/dependency/"
                        "thin)에만 종료코드 1, status drift 자문성은 0")
    p.set_defaults(fn=cmd_lint)

    p = sub.add_parser(
        "verified-at-repin",
        help="현재-진실 문서의 repo+verified_at을 소유 저장소 full OID로 일괄 전환")
    p.add_argument("--repo", choices=[freshness_lint._FRESHNESS_REPO_SELF, freshness_lint._FRESHNESS_REPO_UPSTREAM],
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
    ip.add_argument("--status", choices=ticket_rules.IDEA_STATUS_DIRS)
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
                   help="새 티켓 ID — T-NNNN 또는 T-<prefix>-NNN (발행 문법·legacy prefix 포함)")
    identity_args.add_task_arg(p)  # claim 중 티켓의 소유 세션 확인용
    p.add_argument("--user-ack", metavar="<prefix>",
                   help="NEW-ID가 새 prefix를 신설할 때 사용자 승인 토큰(대상 prefix 값과 정확히 결속)")
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
    pp.add_argument("--user-ack", metavar="<prefix>",
                    help="dst가 새 prefix일 때 사용자 승인 토큰(대상값과 정확히 결속)")
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
    pp.add_argument("--user-ack", metavar="<prefix>",
                    help="--into가 새 prefix일 때 사용자 승인 토큰(대상값과 정확히 결속)")
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
    subcommand = board_dispatch._resolved_subcommand(args)
    if subcommand not in (board_command_rules._MUTATION_SUBCOMMANDS
                          | board_command_rules._READ_SUBCOMMANDS
                          | board_command_rules._SIDECAR_SUBCOMMANDS):
        raise ValueError(f"분류되지 않은 board 명령: {subcommand}")
    package_handler = args.fn in (board_cluster.cmd_cluster, board_round.cmd_section_add,
                                  board_round.cmd_tier, board_round.cmd_design,
                                  board_round.cmd_tier_signals)
    if subcommand in board_command_rules._MUTATION_SUBCOMMANDS and board_dispatch._guard_worktree_misanchor(_home(), f'board.py {subcommand}'):
        return 1
    if subcommand in board_command_rules._READ_SUBCOMMANDS:
        resolution = pm_paths._resolve_read_board(pm_paths.home_at(REPO))
        board_dispatch._print_read_anchor(_home(), subcommand=subcommand, pm_home=resolution.home if resolution.home != REPO else None)
        local_root = pm_paths._board_root_at(REPO)
        if resolution.home is not None and resolution.home != REPO:
            with _read_pm_inputs_at(resolution.home, resolution.root):
                return (args.fn(_home(), args) if package_handler else args.fn(args))
        # 자기 board/standalone은 import-time REPO 경로 그대로다. 별도 context에 넣지 않아
        # 폴백 문구·경로 재바인딩이 전혀 생기지 않는다.
        if resolution.root == local_root:
            return (args.fn(_home(), args) if package_handler else args.fn(args))
        # 방어적 경계: 현재 resolver 계약상 home 없는 비local root는 나올 수 없다.
        with _read_pm_inputs_at(REPO, resolution.root):
            return (args.fn(_home(), args) if package_handler else args.fn(args))
    return (args.fn(_home(), args) if package_handler else args.fn(args))


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치·소유 PM 홈 미확정을 traceback 대신 안내로 번역한다.

    소유 PM 홈 유도 실패는 read·mutation 어느 경로에서 나든 같은 원인이므로 번역 지점도
    하나다 — 실패는 여전히 실패다(rc1·부작용 0). 자기 앵커로 접지 않는다.
    """
    try:
        console.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew·PM 홈 미확정만 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        if isinstance(exc, pm_paths.PmHomeResolutionError):
            print(f"[중단] 소유 PM 홈을 확정할 수 없습니다: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
