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
import shlex
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
from pm_engine.app import freshness_lint, hooks
from pm_engine.cli import regression_cmd  # noqa: E402
from pm_engine.domain import design_rules, git_policy, identity_args, round_rules, shell_policy, ticket_rules
from pm_engine.infra import console, delegate_ledger, fs, pm_paths, repo_coordinates, repo_files
from pm_engine.infra import task_store
from pm_engine.infra import branch_store
from pm_engine.infra import worktree_git

from pm_engine.cli import board_list
from pm_engine.app import engine_guard
from pm_engine.app import tier_signals
from pm_engine.cli import board_cluster
from pm_engine.cli import board_dispatch
from pm_engine.cli import board_lint
from pm_engine.cli import board_repin
from pm_engine.cli import board_round
from pm_engine.domain import board_command_rules
from pm_engine.infra import worktree_paths

from pm_engine.cli import board_init
from pm_engine.cli import board_migrate_identity
from pm_engine.cli import board_prefix
from pm_engine.cli import board_ticket


REPO = Path(__file__).resolve().parents[2]


# Home은 실행 코드 루트와 PM 데이터 루트를 구분한다. 등록 worktree의 읽기 폴백은
# 명시 Home을 패키지에 전달하며, board 루트·Home override 두 개만 context에서 바꾼다.
_READ_BOARD_ROOT_OVERRIDE: Path | None = None
_READ_PM_HOME_OVERRIDE: Path | None = None


def _home():
    """이 실행의 기준 경로 — 자기 앵커 `REPO`, read 폴백 중이면 소유 PM 홈의 board·로컬 입력."""
    return pm_paths.home_at(REPO, _READ_PM_HOME_OVERRIDE, _READ_BOARD_ROOT_OVERRIDE)


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
    todo_warning = engine_guard._no_open_todo_judgment(pm_paths.home_at(Path(root)), session) if targets else None
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
        judgment = engine_guard.judge_git_anchor(pm_paths.home_at(Path(root)), session, call.cwd, list(call.argv), runner=runner, cwd_certain=call.anchor_certain)
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


@contextlib.contextmanager
def _read_pm_inputs_at(pm_home: Path, board_root_path: Path) -> Iterator[None]:
    """읽기 dispatch의 board 루트·Home override 두 개를 설정하고 복원한다.

    패키지의 티켓·wiki·설정 경로는 명시 Home에서 해소한다.
    mutation/sidecar는 이 context에 진입하지 않아 쓰기 게이트 의미론은 바뀌지 않는다.
    """
    global _READ_BOARD_ROOT_OVERRIDE, _READ_PM_HOME_OVERRIDE
    saved_board_override = _READ_BOARD_ROOT_OVERRIDE
    saved_home_override = _READ_PM_HOME_OVERRIDE
    try:
        _READ_BOARD_ROOT_OVERRIDE = board_root_path
        _READ_PM_HOME_OVERRIDE = pm_home
        yield
    finally:
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


# conf 예시 시드는 없다 — 키 목록·설명은 레지스트리 한 곳이 소유한다(`local_conf.py --keys`).
# 파일이 자기 규칙("여기엔 실값만 둔다")을 스스로 어기지 않게, 엔진은 conf 에 주석 스키마를
# 배달하지 않는다. 모르는 키는 `board.py lint` advisory 로 드러나므로 배달 경로가 필요 없다.


# `pm-verified` 재검증이 스코프하는 **리뷰 채널** — 게이트가 실린 장부가 곧 채널이다. 그 채널만
# 본다(다른 채널의 accepted 잔여·기계 확인은 이 게이트의 증거가 아니다 — 채널 격리). role
# 문자열의 단일 진실은 review_form 이라 사본을 두지 않고 재검증 시점에 해소한다
# (`_pm_verified_channel_role`). code-reviewer 잔여는 장부가 아니라 리뷰 라운드 파일에서 판정한다
# (`_review_completion_problem`).


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
    p.set_defaults(fn=regression_cmd.cmd_livegate)

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
    p.set_defaults(fn=board_dispatch.cmd_refresh)

    p = sub.add_parser("lint", help="check depends_on / blocks consistency")
    p.add_argument("--gate", action="store_true",
                   help="push 게이트 모드 — 차단 카테고리(dangling/unstable-ref/dependency/"
                        "thin)에만 종료코드 1, status drift 자문성은 0")
    p.set_defaults(fn=board_lint.cmd_lint)

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
    p.set_defaults(fn=board_repin.cmd_verified_at_repin)

    p = sub.add_parser("promote-scope",
                       help="family wiki scope retag — `repoA → shared`")
    p.add_argument("file", help="대상 문서 (decisions/·specs/ 의 .md · REPO 상대 또는 절대)")
    p.add_argument("--to", required=True,
                   help="새 family_scope 값 — `shared` 또는 repo prefix")
    p.set_defaults(fn=board_ticket.cmd_promote_scope)

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
        board_ticket.cmd_promote_scope,
        board_dispatch.cmd_refresh,
        board_lint.cmd_lint,
        board_repin.cmd_verified_at_repin,
        regression_cmd.cmd_livegate,
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
            return board_dispatch._report_engine_rev_skew_at_terminal(exc)
        if isinstance(exc, pm_paths.PmHomeResolutionError):
            print(f"[중단] 소유 PM 홈을 확정할 수 없습니다: {exc}", file=sys.stderr)
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
