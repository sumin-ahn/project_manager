#!/usr/bin/env python3
"""보드 CLI와 어댑터 진입점.

티켓·아이디어 명령은 pm_engine.cli, 완료 판정은 pm_engine.app이 소유한다.
parser와 남은 상태 조회·lint·보호 훅 진입은 이 파일에서 연결한다.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.app import areas_lint, body_lint, conf_lint, doc_lint, freshness_lint, freshness_pin, hooks, lint_hooks, regression, release, ticket_lint, wiki_lint, wikilink_lint
from pm_engine.cli import regression_cmd, task_args  # noqa: E402
from pm_engine.domain import design_rules, git_policy, identity_args, pytest_report, round_rules, shell_policy, ticket_rules
from pm_engine.infra import clock, console, delegate_ledger, fs, git, lease_store, manifest_store, pm_paths, pre_push_hook, regression_store, repo_coordinates, repo_files, ticket_store
from pm_engine.infra import task_store
from pm_engine.infra import branch_store
from pm_engine.infra import worktree_git

from pm_engine.app import board_dashboard
from pm_engine.cli import board_list
from pm_engine.app import engine_guard
from pm_engine.app import tier_signals
from pm_engine.cli import board_cluster
from pm_engine.cli import board_dispatch
from pm_engine.cli import board_round
from pm_engine.domain import board_command_rules
from pm_engine.infra import worktree_paths

from pm_engine.app import completion_gate
from pm_engine.cli import board_init
from pm_engine.cli import board_migrate_identity
from pm_engine.cli import board_prefix
from pm_engine.cli import board_ticket

_load_module_from_path = repo_files.load_module

REPO = Path(__file__).resolve().parents[2]


# Home은 실행 코드 루트와 PM 데이터 루트를 구분한다. 등록 worktree의 읽기 폴백은
# 명시 Home을 패키지에 전달하며, 잔류 조회 경로 두 개만 context에서 재앵커한다.
_READ_BOARD_ROOT_OVERRIDE: Path | None = None
_READ_PM_HOME_OVERRIDE: Path | None = None


def _home():
    """이 실행의 기준 경로 — 자기 앵커 `REPO`, read 폴백 중이면 소유 PM 홈의 board·로컬 입력."""
    return pm_paths.home_at(REPO, _READ_PM_HOME_OVERRIDE, _READ_BOARD_ROOT_OVERRIDE)


DELEGATE_CHANNEL_GUARD_PY = REPO / ".project_manager" / "tools" / "delegate_channel_guard.py"
CODEX_DELEGATE_OBSERVATIONS = (
    REPO / ".project_manager" / ".local" / "delegate-channel" / "codex-observations.jsonl"
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
    "DELEGATE_CHANNEL_GUARD_PY": "tools/delegate_channel_guard.py",
    "CODEX_DELEGATE_OBSERVATIONS": ".local/delegate-channel/codex-observations.jsonl",
}


@contextlib.contextmanager
def _read_pm_inputs_at(pm_home: Path, board_root_path: Path) -> Iterator[None]:
    """읽기 dispatch의 Home override와 잔류 조회 경로 두 개를 설정하고 복원한다.

    패키지의 티켓·wiki·설정 경로는 명시 Home에서 해소한다.
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


# conf 예시 시드는 없다 — 키 목록·설명은 레지스트리 한 곳이 소유한다(`local_conf.py --keys`).
# 파일이 자기 규칙("여기엔 실값만 둔다")을 스스로 어기지 않게, 엔진은 conf 에 주석 스키마를
# 배달하지 않는다. 모르는 키는 `board.py lint` advisory 로 드러나므로 배달 경로가 필요 없다.


# `pm-verified` 재검증이 스코프하는 **리뷰 채널** — 게이트가 실린 장부가 곧 채널이다. 그 채널만
# 본다(다른 채널의 accepted 잔여·기계 확인은 이 게이트의 증거가 아니다 — 채널 격리). role
# 문자열의 단일 진실은 review_form 이라 사본을 두지 않고 재검증 시점에 해소한다
# (`_pm_verified_channel_role`). code-reviewer 잔여는 장부가 아니라 리뷰 라운드 파일에서 판정한다
# (`_review_completion_problem`).


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
        problems = completion_gate._sync_release_must_fix_marker(_home(), flag)
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
        if live_session and len(lease_store.leased_sessions(pm_paths.leases_file(_home()))) >= 2
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


def cmd_livegate(args: argparse.Namespace) -> int:
    """record = `pytest -m release` 실행+기록 / check = push HEAD green 검증 (보호훅용)."""
    if args.action == "record":
        return _livegate_record(args)
    return completion_gate._livegate_check(_home(), args)


# ── 티켓 라운드 + 위임 티어 ────────────────────────────────────────
# 역할 산출 1건 = 라운드 파일 1개이고 그 자리는 명세 파일 밖(`tickets/rounds/<티켓>/`)이다.
# 역할 집합·사람용 라벨·파일명 문법의 권위는 사이드카 seam 이며 board 는 파생으로만 읽는다.


# 엔진 구현 패키지 이름 — `from pm_engine.<층> import <모듈>` 을 모듈 파일 의존으로 센다.


# unclaim 이 받는 출발 상태 — `claimed`(소유 해제 후 open 복귀)와 `blocked`(상태 유지·소유만
# 해제)다. blocked 를 받는 이유: unblock 은 claimed_by 를 보고 claimed/ 로 복귀시키므로,
# blocked + 소유자 부재(퇴장한 세션) 조합은 unclaim 이 blocked 를 못 받으면 어떤 문으로도
# 풀리지 않는 교착이 된다.


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


# 등록 repo 이름 canonical 형식 (`pm-config repo add` 의 검증과 같은 문법). init 은 이 형식을
# **차단 기준으로 쓰지 않는다** — 폴더명은 사용자 입력이 아니라 이미 존재하는 사실이고, 점·공백이
# 든 폴더에서 인자 0 온보딩(`pm_import.run_board_init`)을 rc≠0 으로 만들면 그게 더 큰 회귀다.
# 표를 깨는 문자만 차단하고, 형식 이탈은 `--repo` 처방과 함께 1줄로 표면화한다.


# ── identity backfill 마이그레이션 ────────
# 이전 데이터(areas `area_owner` 부재·ticket `created_by` 부재·`claimed_by` 슬롯-only)를
# *일회성* backfill 해 `--mine`·provenance 가 기존 보드에서 동작하게 한다. graceful-null 우회를
# 정합 데이터로 대체하는 정식 업그레이드 경로.
#   - 멱등: 빈/부재 필드만 채운다. 기존 non-empty 값은 절대 덮어쓰지 않는다(재실행 no-op).
#   - 비파괴: frontmatter 키 순서·body 보존(dump_ticket 가 sort_keys=False)·areas.md 표/주석 보존.
#   - 대상: areas 빈 area_owner → user · 티켓 부재 created_by → user · 슬롯-only claimed_by
#     (`/` 없음·non-empty) → `<user>/<slot>`(기존 슬롯값을 slot 으로 보존·user 차원만 prepend).


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


# 무prefix(legacy `T-NNNN`) 티켓의 prefix-list 라벨. `none` 은 무prefix 네임스페이스의 1급
# 인자이므로 그 라벨을 그대로 쓴다(예약어와 표기 일치).


# ── reid — 단일 티켓 ID 재부여  ──────────
# 카테고리 일괄(`prefix` 네임스페이스)과 달리 **티켓 1장**의 오발행 ID(번호·prefix)를 고친다.
# *같은 파이프라인*(`_prefix_relabel`)을 재사용한다 — old→new 맵을
# `{OLD: NEW}` 단일 항으로 세워 토큰단위 rewriter·slug 파일명 rename·홈 git clean 가드·단일
# board_lock·board-git 백업·dry-run 규모 preview 를 그대로 상속한다(새 rewrite 엔진 없음).


# ── idea commands ──────────────────────────────────────────────────────
#
# Ideas are pre-ADR candidates living under ideas/{open,promoted,killed}/.
# They reuse the ticket frontmatter/body helpers (load_ticket / dump_ticket)
# and the generic find_item / move_item / next_numeric_id helpers — only the
# lifecycle differs (no claim/complete; just open → promoted|killed).

# Body skeleton for `idea new`. Mirrors the 권장 섹션 list in ideas/README.md.


# Maps an idea's destination status to the imperative verb used in messages.


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
        "regression": board_ticket._sidecar_document(regression_store._resolve_regression_flag(_home(), str(REPO))[0]),
        "livegate": board_ticket._sidecar_document(regression_store._resolve_livegate_flag(_home(), str(REPO))[0]),
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
    p.set_defaults(fn=board_list.cmd_list)

    p = sub.add_parser("show", help="show one ticket")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=board_list.cmd_show)

    p = sub.add_parser("claim", help="atomic claim — mv open → claimed")
    p.add_argument("id", metavar="T-NNNN")
    identity_args.add_identity_args(p)
    p.add_argument("--user", help="user 식별자 — claimed_by 의 user 차원 (default: local.conf identity.user= / "
                   "git config user.email)")
    p.set_defaults(fn=board_ticket.cmd_claim)

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
    p.set_defaults(fn=board_ticket.cmd_complete)

    p = sub.add_parser("discard",
                       help="mv open|claimed|blocked → discarded (처분 종결 — 병합·폐기)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("disposition", metavar="|".join(ticket_rules.DISPOSITION_KINDS),
                   choices=ticket_rules.DISPOSITION_KINDS,
                   help="처분 종류 — merged(다른 티켓으로 흡수) · dropped(취소·폐기)")
    p.add_argument("--reason", required=True,
                   help="처분 사유 (병합 대상 티켓 ID 도 여기에 적는다)")
    identity_args.add_task_arg(p)
    p.set_defaults(fn=board_ticket.cmd_discard)

    p = sub.add_parser("reopen",
                       help="mv done|discarded → open (오처리 복구 — 종결을 되돌리는 유일한 문)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--reason", required=True, help="복구 사유 (본문·board-git 에 남는다)")
    p.set_defaults(fn=board_ticket.cmd_reopen)

    p = sub.add_parser("block", help="mv open|claimed → blocked (claimed 는 소유자만)")
    p.add_argument("id", metavar="T-NNNN")
    p.add_argument("--reason", required=True)
    identity_args.add_task_arg(p)
    p.set_defaults(fn=board_ticket.cmd_block)

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
    p.set_defaults(fn=board_ticket.cmd_unclaim)

    p = sub.add_parser(
        "unblock",
        help="mv blocked → claimed(claimed_by 보유·소유 보존) 또는 open(무소유) · 소유자만")
    p.add_argument("id", metavar="T-NNNN")
    identity_args.add_task_arg(p)
    p.set_defaults(fn=board_ticket.cmd_unblock)

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
    p.set_defaults(fn=board_ticket.cmd_new)

    p = sub.add_parser("promote", help="draft(board-git 미커밋) 티켓을 승격 — 본문 채운 뒤 board-git sync "
                        "(발행 규율 게이트: board-git 공유 시 `new` 가 미충전 티켓을 draft 로 남긴다)")
    p.add_argument("id", metavar="T-NNNN")
    p.set_defaults(fn=board_ticket.cmd_promote)

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
    p.set_defaults(fn=board_init.cmd_init)

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
    p.set_defaults(fn=board_migrate_identity.cmd_migrate_identity)

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
    ip.set_defaults(fn=board_ticket.cmd_idea_list)

    ip = idea_sub.add_parser("new", help="create a new idea in open/")
    ip.add_argument("title")
    ip.add_argument("--tag", help="comma-separated tags")
    ip.set_defaults(fn=board_ticket.cmd_idea_new)

    ip = idea_sub.add_parser("promote", help="mv idea open → promoted")
    ip.add_argument("id")
    ip.set_defaults(fn=board_ticket.cmd_idea_promote)

    ip = idea_sub.add_parser("kill", help="mv idea open → killed")
    ip.add_argument("id")
    ip.set_defaults(fn=board_ticket.cmd_idea_kill)

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
    p.set_defaults(fn=board_prefix.cmd_reid)

    # prefix subcommand group — 작업 카테고리 prefix 관리. list=현황(read-only) +
    # rename/strip/merge/delete=개명·통합 (`none`=무prefix 1급·collision abort·board-git 백업).
    prefix_p = sub.add_parser("prefix", help="ticket-ID prefix (작업 카테고리) 관리")
    prefix_sub = prefix_p.add_subparsers(dest="prefix_cmd", required=True)

    pp = prefix_sub.add_parser("list", help="prefix별 티켓 수·번호범위 현황 (read-only·비파괴)")
    pp.set_defaults(fn=board_prefix.cmd_prefix_list)

    pp = prefix_sub.add_parser(
        "rename", help="카테고리 개명 <A|none> <B|none> — 무충돌=번호유지 교체·충돌=merge 안내")
    pp.add_argument("src", help="원본 카테고리 (또는 none=무prefix)")
    pp.add_argument("dst", help="대상 카테고리 (또는 none=이름 지우기)")
    pp.add_argument("--user-ack", metavar="<prefix>",
                    help="dst가 새 prefix일 때 사용자 승인 토큰(대상값과 정확히 결속)")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview(N ID·M refs·K 파일)·쓰기 0")
    pp.set_defaults(fn=board_prefix.cmd_prefix_rename)

    pp = prefix_sub.add_parser("strip", help="이름 지우기 <A> — = rename <A> none 별칭")
    pp.add_argument("prefix", help="지울 카테고리")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview·쓰기 0")
    pp.set_defaults(fn=board_prefix.cmd_prefix_strip)

    pp = prefix_sub.add_parser(
        "merge", help="통합 <A> [B...] --into <T|none> — created 순(기본 append·저위험)")
    pp.add_argument("sources", nargs="+", help="통합할 source 카테고리(들) (또는 none)")
    pp.add_argument("--into", required=True, help="대상 카테고리 (또는 none)")
    pp.add_argument("--user-ack", metavar="<prefix>",
                    help="--into가 새 prefix일 때 사용자 승인 토큰(대상값과 정확히 결속)")
    pp.add_argument("--reorder-chronological", action="store_true",
                    help="전체 interleave 재번호(opt-in·고위험 17k refs). 기본=append(대상 max 뒤·기존 번호 무변경)")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview·쓰기 0")
    pp.set_defaults(fn=board_prefix.cmd_prefix_merge)

    pp = prefix_sub.add_parser(
        "delete", help="빈(0티켓) 카테고리 등록 제거 — 티켓 있으면 fail-loud(rename/merge 안내)")
    pp.add_argument("prefix", help="제거할 (빈) 카테고리")
    pp.add_argument("--dry-run", action="store_true", help="규모 preview·쓰기 0")
    pp.set_defaults(fn=board_prefix.cmd_prefix_delete)

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
    package_handler = args.fn in (
        board_cluster.cmd_cluster, board_round.cmd_section_add, board_round.cmd_tier,
        board_round.cmd_design, board_round.cmd_tier_signals,
        board_init.cmd_init,
        board_list.cmd_list,
        board_list.cmd_show,
        board_migrate_identity.cmd_migrate_identity,
        board_prefix.cmd_prefix_list,
        board_prefix.cmd_prefix_rename,
        board_prefix.cmd_prefix_strip,
        board_prefix.cmd_prefix_merge,
        board_prefix.cmd_prefix_delete,
        board_prefix.cmd_reid,
        board_ticket.cmd_claim,
        board_ticket.cmd_complete,
        board_ticket.cmd_block,
        board_ticket.cmd_unclaim,
        board_ticket.cmd_unblock,
        board_ticket.cmd_discard,
        board_ticket.cmd_reopen,
        board_ticket.cmd_new,
        board_ticket.cmd_promote,
        board_ticket.cmd_idea_list,
        board_ticket.cmd_idea_new,
        board_ticket.cmd_idea_promote,
        board_ticket.cmd_idea_kill,
    )
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
