#!/usr/bin/env python3
"""보드 CLI 진입점.

명령 파서·분류·dispatch 는 pm_engine.cli.board_cmd, 어댑터 git 훅 판정은 pm_engine.cli.hooks,
각 명령의 처리는 pm_engine.cli·app 이 소유한다. 이 파일은 콘솔 설정·설치 앵커 Home·
엔진 사본 불일치 진단과 CLI 호출만 맡는다.
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

from pm_engine.cli import board_cmd
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


def _home():
    """이 설치 사본의 CLI 앵커 Home — 자기 앵커 `REPO`."""
    return pm_paths.home_at(REPO)




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


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 안내로 번역한다."""
    try:
        console.configure_console_utf8()
        return board_cmd.main(_home(), argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return board_dispatch._report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
