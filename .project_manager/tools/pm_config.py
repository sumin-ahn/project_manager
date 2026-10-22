#!/usr/bin/env python3
"""single-user multi-repo (N×M) 셋업/관리 파사드 — 가벼운 디스패처.

multi-PM = N 세션 × M repo 한 개념— *수가 1이냐 더냐*. 이 파사드는 single user 가 여러
repo(multi-PM 셋업)를 도는 토폴로지의 셋업·조회·진단과 슬롯 대여·반납(`alloc`·`release`)을
맡는다(다중-사람 협업 아님). 부트스트랩·핸드오프는 슬롯을 빌리거나 반납하지 않는다.

각 서브커맨드는 board·pm_update CLI와 pm_engine 패키지를 호출한다.
보호 정책·훅·git 조회는 repo_protection·protected_hooks·git·areas_store가 소유한다.

사용:
    pm-config init [<board init 인자>]                     # clone 당 1회 셋업 (board.py init 흡수)
    pm-config repo add <name> [--git <url>] [--test "<cmd>"] [--protected "main,develop"] # repo 등록 + .repos clone (신규=--git 필수 / 기등록 hydrate=areas URL)
    pm-config repo protected <name> [<목록>|default]        # 보호 브랜치 목록 조회/설정 (areas → 훅 sidecar 정합화)
    pm-config repo list                                    # 등록 repo 표 (repo·prefix·base·protected·test_cmd·area_owner)
    pm-config worktree add <repo> [--folders <폴더,…>] --user-ack <repo>        # 사용자 승인값 결속 + 새 슬롯 생성 + submodule init
    pm-config worktree prune-stale                         # worktree 사라진 dangling 장부 엔트리 정리
    pm-config worktree remove <slot> --user-ack <slot> [--user-ack <브랜치>]... [--force] # 사용자 승인값 결속 + 슬롯 통째 제거 — worktree+브랜치 판정+장부 (--force=점유만 무시)
    pm-config status | whoami                              # 풀/리스 + 이 세션 repo/슬롯/branch
    pm-config alloc <repo> --task <task> [--folders <폴더,…>]                    # task 명의로 idle 최소 번호 슬롯 대여 (자동 생성 안 함)
    pm-config release <slot> [--task <task>] [--force]      # 작업완료 반납 (--task 소유검사) / 수동 강제(백스톱)
    pm-config task new <task>                               # task 정체성 생성 — 장부 행(닫힘)+pm_state (세션 묶기 없음·진입은 부트스트랩)
    pm-config task close <task>                             # 열린 채 남은 task 세션 기록 닫기 (기록만·복원 없음)
    pm-config task branch <task> <슬롯> <브랜치> --from <부모>     # 슬롯에 새 브랜치 — 부모 최신 커밋에서 만들고 부모·갈라진 커밋 기록
    pm-config task prefix <task> <p|none> [--user-ack <p>] # 신규 prefix 사용자 승인값 결속·변경/해제 (`none`=해제)
    pm-config task end <task>                               # task 종료 — claimed 소진 게이트·dirty 게이트·일괄 반납 + _ended 아카이브
    pm-config task reopen <task> [--archive <basename>]     # 종료 archive를 동일 task 정체성으로 복원
    pm-config task todo add <task> <할 일 한 줄>            # 열린 todo 등록 — 파일 수정 전에 지금 하는 일을 적는다(없으면 쓰기 판정이 경고)
    pm-config task todo list <task>                         # 열린 todo — ID·등록일·묵은 일수
    pm-config task todo done <task> <ID>                    # 완료 = 목록에서 제거 (완료 표시를 남기지 않는다·기록은 log)
    pm-config update [--from <upstream>]                   # 엔진 갱신 (pm-update 흡수)
    pm-config upstream show | set <url|path>               # upstream 조회/전환 (검증·fail-closed)
    pm-config add-harness <harness> [--from <src>] [--dry-run]  # 라이브 인스턴스에 두 번째 harness 어댑터 추가
    pm-config sync-adapter-config [--list | --check | --accept <경로> | --accept-all] # 어댑터 config 판정 조회 / 수렴 게이트 / 상류 값 수용(단건·세트)
    pm-config apply [--json] [--set 키=값 ...]             # 값 검사 → local.conf 기록 → 파생 파일 반영 (반영 전 거부 rc 3)
    pm-config delegate models --harness <h> --json         # 그 하네스가 인정하는 모델 목록과 모델별 추론 강도(variant) 조회 (읽기 전용·JSON 한 줄)

서브커맨드별 엔진 배선:
  - init      → board.main(["init", ...]) verbatim forward (clone 당 1회 셋업·N=1·M=1 ~ N×M 공용).
  - repo add  → board.areas_append(per-repo 레지스트리 줄) + `git clone --bare`
                로 `.repos/<name>.git`(worktree 풀 공유 .git 원).
  - repo protected → (조회) areas_store._repo_protected 실효값 + areas raw 셀(출처) + 훅 sidecar 정합 /
                (설정) board.areas_set_cell(areas.md 단일 진실·board_lock in-place) →
                repo_protection → protected_hooks.install_protected_hook(파생 sidecar·순서 고정) → board-git 동기.
  - repo list → areas_store._parse_areas() 표 렌더(빈 셀은 폴백을 괄호로 명시).
  - worktree add → repo 값과 같은 `--user-ack` 사용자 승인 게이트 뒤
                pm_engine.app.worktree_pool.create_slot(새 슬롯 + `git submodule update --init`).
                `--task <task>` 면 owner_task 로 넘겨 생성 직후 그 슬롯 task 명의 대여(ⓓB).
  - status|whoami → pool_store.list_leases() + 이 세션 식별(repo/슬롯/branch surface).
  - alloc    → pm_engine.app.worktree_pool.alloc(repo, owner_task=<task>) — 항상 신규 idle 최소 번호 대여(멱등
               같은 repo 복수 보유). 풀 소진(NeedsCreate)이면 자동 생성 안 하고
               `worktree add --task` 승인 요청(생성+대여 한 흐름·ⓓB·물리층=사용자).
  - release → pm_engine.app.worktree_pool.release(--task 면 owner_task 소유검사·--force 면 force_release) — 수동 반납/강제만.
  - task prefix → board._validate_prefix(입력 sanity·소비 grammar 단일 진실) → task_lifecycle.set_task_prefix
                (장부 flock/스키마 단일 소유·`none`=해제·직접 JSON write 금지).
  - task end → board.scan_task_tickets(claimed 소진 게이트) → task_lifecycle.end_task(dirty 게이트·
               일괄 idle 반납·서술 폴더 `_ended/<task>-<날짜>/` 아카이브 이동·삭제 아님).
  - update → pm_update.main(argv) verbatim forward (rename 비용 0·중복 구현 금지).
  - add-harness → pm_import.add_harness_cli(dest, harness, dry_run=, source_root=) verbatim forward
                  (복사 스코프+인터페이스 예외 번역+소스 해소는 pm_import 단일 진실·
                  중복 0). `--from` 생략 시 dest local.conf upstream.path 자동 해소(imported 인스턴스).
  - delegate models → contract.model_listing(모델 원천 표 `MODEL_SOURCE` 한 행의 조회 — 별칭 표 그대로 /
                  표가 선언한 조회 명령 실행). 응답에 모델별 `variants`(opencode)가 실린다.
                  읽기 전용이고 조회 실패는 빈 목록이 아니라 rc 3 이다(사유는 stderr 한 줄).
  - sync-adapter-config → pm_import.judge_adapter_configs(조회) / accept_adapter_config(수용·백업
                  후 template 채택 + 원장 기록). manifest 밖 instance-owned config 는 동기가 무편집
                  분만 자동 갱신하고 나머지는 보존+보고하므로, 이 커맨드가 보존분을 채택자가
                  명시적으로 받는 유일한 채널이다(소스 해소는 add-harness 와 같은 규칙).
                  수용한 파일이 하네스 설정 파일이면 수용 직후 사용 한도를 다시 반영한다.
  - apply → pm_update.apply_conf(키 묶음 표·한 락·전부 계획 뒤 기록). 관리자 저장은 키와 무관하게
                  이 명령 한 번이다. 반영 전 거부는 rc 3 이고 local.conf·파생 파일을 바꾸지 않는다 —
                  처리하지 못한 예외의 rc 1 과 겹치지 않아 관리자 화면은 rc 3 만 잘못된 값으로 보인다.


  - thin forwarder(`pm-config.sh/.cmd`)는 로직 0 — 이 디스패처가 엔진 배선의 단일 지점.
  - 브랜치 할당은 `task branch`(새 브랜치)·`worktree_pool.py switch`(있는 브랜치) 소관이다.
  - update 는 pm_update 로직을 *위임*(import 호출) — pm_update.py 는 그대로 두고 흡수만.
  - 엔진 호출은 DI seam(주입 가능 callable) — 테스트는 mock 주입으로 hermetic(실 clone/
    worktree 부작용 없이 배선만 검증·pm_bootstrap 의 DI 패턴 동류).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import inspect
import json
import os
import shutil
import shlex
import subprocess
import sys
import tomllib
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.domain import areas_rules, conf_schema, upstream_rules  # noqa: E402
from pm_engine.infra import areas_store, conf_store, console, fs, lease_store, pm_paths, repo_files  # noqa: E402
from pm_engine.infra.harness import contract  # noqa: E402
_load_module_from_path = repo_files.load_module

from pm_engine.app import branch_checkout, repo_protection
from pm_engine.infra import branch_store, git, protected_hooks
from pm_engine.infra import pool_store
from pm_engine.infra import sparse_checkout
from pm_engine.app import task_lifecycle
from pm_engine.domain import task_rules
from pm_engine.infra import task_store
from pm_engine.infra import worktree_git
from pm_engine.infra import worktree_paths
import pm_engine.app.worktree_pool
from pm_engine.domain import worktree_records
from pm_engine.app import worktree_status

# REPO = 스크립트 위치 기반(cwd 무관) — board.py·worktree_pool.py 와 동일 앵커 관례
# (어느 worktree cwd 에서 호출돼도 multi-PM 루트 .project_manager 를 자동 타깃).
REPO = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / ".project_manager" / "tools"
REPOS_DIR = REPO / ".repos"   # worktree 풀이 공유하는 bare .git 원


# git argv → (returncode, stdout). DI seam 의 타입 (pm_import.GitRunner·worktree_pool 선례).
GitRunner = Callable[[list], "tuple[int, str]"]


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다(런타임
# 공유-읽기였다면 같은 디렉토리 안 자기-일치라 미검출). 릴리즈 bump 는 `engine_rev.py --bump
# vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄 재작성한다(사람 N곳 편집 0). 평시 회귀 가드
# (test_engine_rev_stamp)가 전 모듈 리터럴 == engine_rev.ENGINE_REV 를 강제한다.
ENGINE_REV = "v1.8.1"

# rev 스탬프를 지닌 형제 파일만 대조 대상. pm_update는 복구 채널이라 의도적으로 제외한다.
# deep-import AST 가드가 실제 호출 target에서 목록/검증 누락을 자동 적발한다.
_STAMPED_SIBLINGS = frozenset({
    "board.py", "worktree_pool.py", "pm_import.py",
})


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
    판정이 True 인 예외(중첩 로드에서 검출된 형제 skew)는 재-raise 해 fail-loud 를 보존한다
    (예: 신 pm_config→신 board→구 형제 검출이 None 강등되지 않게)."""
    return getattr(exc, "_engine_rev_skew", False)


def _report_engine_rev_skew_at_terminal(exc) -> int:
    """명시된 CLI 종료 경계에서 marked skew를 진단하고 실패 rc로 바꾼다."""
    print(
        f"[중단] 엔진 사본 불일치: {exc} — 먼저 pm-update로 엔진 전체를 "
        "동기화한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return 1


# ── 엔진 모듈 동적 로드 (스크립트-위치 앵커·pm_bootstrap 선례) ──────────────────
# board.py·worktree_pool.py 는 같은 tools/ 에 있다. spec_from_file_location 으로
# 로드한다 — 패키지 설치 없이 동작(board.py·pm_*.py 와 같은 로드 관례). 부재/로드
# 실패는 해당 서브커맨드 경로에서만 명시 에러(침묵 무력화 금지).


def _load_module(name: str, filename: str):
    """tools/<filename> 를 모듈로 로드한다. 부재/실패 → None (호출부가 명시 에러)."""
    path = TOOLS_DIR / filename
    if not path.exists():
        return None
    try:
        if filename in _STAMPED_SIBLINGS:
            mod = _load_module_from_path(
                path, filename, verifier=_verify_engine_rev,
            )
        else:
            mod = _load_module_from_path(
                path, filename, allow_unverified=True,
            )
    except Exception as exc:  # noqa: BLE001 — 로드 실패는 호출부가 명시 에러로 보고.
        if _is_engine_rev_skew(exc):
            raise  # 중첩 로드 형제 skew 는 fail-loud(삼키지 않는다).
        return None
    return mod


def _runtime_skill_entry(skill: str) -> str:
    """현재 실행 하네스의 사용자 호출 표기(Codex env marker 외 slash)."""
    prefix = "$" if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI") else "/"
    return f"{prefix}{skill}"


def _default_session(*, identity=None) -> str | None:
    """세션 식별자 — `lease_store.session_name()` 과 *동형* 우선순위:
    `$PM_SESSION_NAME` > `$CLAUDE_SESSION_NAME`(deprecated alias·silent) > lease 장부
    state=="leased" 행이 정확히 1개면 그 session (단일-lease 유도) > None.

    `PM_SESSION_NAME` 이 정식 엔진 변수(하니스 무관)·`CLAUDE_SESSION_NAME` 은 구 alias(둘 다면
    PM 승·조용히 동작). **per-clone `local.conf session=` 층은 없다**(board 와 동형) — slot 종속
    값이 프로젝트 공용 conf 에 있던 범위 오류라 읽지 않는다. 미해소면 None — `cmd_status`/
    `whoami` surface(required=False)가 "(비바인딩)" 으로 표시한다(비바인딩 세션이 남의 리스로
    self-identify 하던 직접 증상 수정). `<host>-<pid>` 꼬리는 두지 않는다.

    lease 장부(`state=="leased"` 행의 session 목록) 읽기는 공용 `lease_store.leased_sessions`
    로 위임한다(board.py/worktree_pool 의 동형 사본과 단일 진실로 수렴). `identity` 주입으로
    hermetic 테스트 가능(미주입 시 `lease_store`). 장부 경로는 호출 시점 `REPO` 에서 구성한다
    (monkeypatch 존중·`_local_conf_user` 와 동형).

    저장측(worktree_pool)과 매칭측(여기)이 어긋나면 "이 세션의 리스" surface 가 board 매칭과
    어긋나므로 세 모듈이 같은 우선순위를 공유한다. 장부 0행에서 세션 이름을 유도하는 층은
    없다 — 등록은 장부 행이 하고, 행이 없으면 미해소다.
    """
    env = os.environ.get("PM_SESSION_NAME") or os.environ.get("CLAUDE_SESSION_NAME")
    if env:
        return env
    identity_mod = identity or lease_store
    leases_file = REPO / ".project_manager" / ".local" / "worktree-leases.json"
    leased = identity_mod.leased_sessions(leases_file)
    if len(leased) == 1:
        return leased[0]
    # leased 0(미등록) 또는 ≥2(모호) → 미해소(surface 는 "(비바인딩)").
    return None


def _local_conf_user() -> str | None:
    """`.project_manager/local.conf` 의 `identity.user=` (없거나 OSError → None).

    `repo_protection._local_conf_test_cmd`와 동형 — 해소는 공용 로더(`conf_store`)가 하고 이 함수는
    키 이름과 빈값→None 관례만 소유한다(`conf_store.local_config(home).get("identity.user")` 와 동일 의미).
    부재/읽기실패는 None(폴백)이고, 구표기 키 잔존은 소비 지점 계약대로 fail-loud 다.
    """
    return repo_protection._local_conf_value(pm_paths.home_at(REPO), "identity.user") or None


def _git_config_email() -> str | None:
    """`git config user.email` (미설정/git 부재/실패 → None·fail-soft).

    board.py `_git_config_email` 와 *동형* — board 직접 import 금지라
    `_default_user` 의 폴백 레이어를 stdlib subprocess 로 자체 구현한다. UTF-8 고정(한글
    이름·메시지 안전)·짧은 timeout. git 바이너리 부재·rc≠0·예외는 None 으로 강등(크래시 0).
    """
    git_binary = shutil.which("git")
    if git_binary is None:
        return None
    try:
        r = subprocess.run(
            [git_binary, "-C", str(REPO), "config", "user.email"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):  # git 호출 실패는 None(미상)으로 강등.
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _default_user() -> str | None:
    """user 식별자 — board.py `user_name()` 과 *동형* 우선순위:
    `local.conf identity.user=` > `git config user.email` > None (graceful·user 미상 허용).

    `pm`(슬롯·`_default_session`)과 직교하는 **누가**(사람) 차원이다. `repo add` 의 areas.md
    `area_owner` 칼럼(그 area 의 user 소유·`--mine` 풀 입력) 기본값으로 쓴다. 단일 사용자는 보통
    `local.conf identity.user=` 미설정 → `git config user.email` 폴백·그마저 없으면 None(빈 area_owner).
    """
    conf_user = _local_conf_user()
    if conf_user:
        return conf_user
    return _git_config_email()


# areas.md canonical 칼럼 순서 (`areas_rules._AREAS_COLUMNS` 와 같은 순서). 구 헤더는 이 순서의
# *prefix* 라, 헤더보다 넓은 행(구 헤더에 신 canonical row 가 append 된 업그레이드 행)은 이
# 순서로 이어 매핑해 `base`/`protected`/`area_owner` 유실을 막는다(`areas_rules._areas_row_dict` 동형).
_AREAS_CANONICAL_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base",
                            "protected", "area_owner")


def _areas_rows() -> list[dict[str, str]]:
    """areas.md 데이터 행 → `{칼럼명: 값}` 목록 — pm_config 자체 파싱(board.py 미로드).

    `areas_store._parse_areas` 와 **같은 매핑 규칙**이다. `cmd_status`
    (정체성 posture)와 기계 조회(`status --json` 의 `repos`)가 이 파서 하나를 공유한다 — 같은
    파일을 두 번 파싱하지 않는다. 구분선(`|---|`)은 건너뛰고 첫 table row 를 헤더로 본다.
    파일 부재·읽기 실패·손상 UTF-8 은 빈 목록(fail-soft — 조회가 크래시하지 않는다).

    자리는 `pm_paths.areas_file` 과 같은 판정이다 — 보드가 별도 저장소(`.project_manager/board/tickets`
    실재)면 그 안, 아니면 `.project_manager/areas.md`. 고정 경로로 읽던 때는 보드 분리 홈에서
    `repos` 가 늘 빈 목록이었다.
    """
    board_dir = REPO / ".project_manager" / "board"
    areas_file = (board_dir / "areas.md" if (board_dir / "tickets").is_dir()
                  else REPO / ".project_manager" / "areas.md")
    try:
        lines = fs.read_text_shared(areas_file, encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return []
    header: list[str] = []
    rows: list[dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if cells and all(c and set(c) <= {"-", ":"} for c in cells):
            continue  # 구분선(|---|:--:|) skip.
        if not header:
            header = [c.lower() for c in cells]
            continue
        if len(cells) > len(header):
            rows.append({
                (_AREAS_CANONICAL_COLUMNS[i] if i < len(_AREAS_CANONICAL_COLUMNS)
                 else f"col{i}"): cells[i]
                for i in range(len(cells))
            })
        else:
            rows.append({header[i]: (cells[i] if i < len(cells) else "")
                         for i in range(len(header))})
    return rows


def _distinct_area_owners() -> int:
    """areas.md 의 distinct non-empty `area_owner` 수 — 다중사용자 최소 신호.

    `cmd_status` 의 isolation posture(strict/degrade/single-user) 판정용 *coarse* 신호다. board.py 를
    import 하지 않으므로 `_local_conf_user` 동형으로
    areas.md 를 stdlib 로 직접 읽는다(`_areas_rows`) — board 의 격리 *판정*(`_ticket_is_mine`·티켓
    스캔 `_distinct_ticket_users`)은 복제하지 않고, 공유 레지스트리(areas.md)의 `area_owner` 칼럼만
    헤더-인식으로 센다. 실 strict-exclude 여부는 `board list --mine` loud-warn이
    authoritative — 여긴 정체성·모드 posture 만.

    파일/칼럼 부재·파싱 실패는 0(fail-soft·단일 사용자 취급·크래시 0).
    """
    return len({owner for owner in (row.get("area_owner") for row in _areas_rows())
                if owner})


def _resolve_repo_base(repo: str, *, board=None) -> str | None:
    """그 repo 의 areas.md `base` 브랜치 — 등록 칸이 비었으면 None.

    `cmd_worktree_add` 가 `create_slot(base=)` 로 넘기는 값이자 슬롯 브랜치의 부모다. None 이면 부모를
    적을 수 없어 호출부가 슬롯을 만들지 않는다. board 로드 실패·areas 판독 실패는 그대로 올린다 —
    "base 없음" 으로 바꾸면 실제 원인이 가려진다. board 직접 import 금지(`_load_module` DI).
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        raise RuntimeError(
            f"board.py 를 로드하지 못해 repo {repo!r} 의 base 를 읽을 수 없다 ({TOOLS_DIR / 'board.py'})."
        )
    return board_mod._repo_base(repo)


# ── 보호목록 설정 채널 ────────────────────────────────────
# areas.md `protected` 칼럼이 **단일 진실**이고 훅 sidecar(`.local/repo-hooks/<repo>/protected`)는
# 순수 파생 캐시다. 값 변경은 areas → sidecar **순서 고정**(역순이면 훅이 비준되지 않은 목록을
# 강제한다). "보호 없음" 은 표현 불가 — 빈 값은 항상 `DEFAULT_PROTECTED` 폴백이므로 빈 문자열
# 지정은 거부하고 `default` 리터럴(칼럼 비움)로 안내한다.


def _stdin_is_tty() -> bool:
    """무인자 분기·프롬프트 게이트용 tty 판정 — stdin·stdout 둘 다 tty 일 때만 True.

    파이프/CI(둘 중 하나라도 비-tty)면 False → 콘솔/프롬프트로 안 멈춘다(input() 블록
    회피·pm_import 비-tty 폴백 패턴 동류). 테스트는 이 헬퍼를 monkeypatch 해 분기를
    결정적으로 친다(라이브 tty 없이).
    """
    return bool(
        getattr(sys.stdin, "isatty", lambda: False)()
        and getattr(sys.stdout, "isatty", lambda: False)()
    )


# ── repo name 입력 검증 ─────────────────────────────────────────────


def _validate_repo_name(name: str) -> bool:
    """repo name 이 허용 패턴(`^[A-Za-z0-9][A-Za-z0-9_-]*$`)에 맞는가.

    `cmd_repo_add` 가 **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 부른다 —
    `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-` 옵션 오인·빈
    문자열을 입구에서 막는다(fail-closed). 모듈수준 헬퍼라 테스트 가능(주입 불필요).
    """
    # fullmatch — `re.match` 의 `$` 는 trailing 개행 직전에도 매칭해 `"billing\n"` 이
    # 가드를 통과(bare 폴더명 개행·areas.md 줄 corruption — 막으려던 결함 클래스)한다.
    return bool(areas_rules._REPO_NAME_RE.fullmatch(name))


# ── base 브랜치 해소 ─────────────────────────────────────────────────

# `--base` 검증 실패(없는 브랜치)를 빈문자열("미해소")과 구별하는 sentinel. None 폴백
# (bare HEAD 해소 실패)은 빈 문자열로 surface 하지만, *명시 base 가 검증 실패*하면 등록을
# 막아야 하므로(잘못된 base 기록 방지) 이 sentinel 로 호출부에 신호한다.
_BASE_INVALID = object()

# `git symbolic-ref HEAD` 의 브랜치 full ref 접두 — `refs/heads/<name>`. 이 접두 **정확히**를
# 제거해야 순수 브랜치명이 된다(동명 태그 존재 시 `heads/<name>` 로 오염되는 `--short` 대신 full
# ref 를 읽는 이유·worktree_git._SYMREF_BRANCH_PREFIX 와 동일 규칙).
_SYMREF_BRANCH_PREFIX = "refs/heads/"


def _resolve_base(base_arg: str | None, bare_path: Path, *, runner: GitRunner):
    """repo add base 브랜치를 해소한다. bare(`.repos/<name>.git`)는 존재 전제.

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_real_clone_runner` 가 `git <argv>` 형태라 `-C` 를 argv 로 넣으면 그 repo 컨텍스트).

      - `base_arg` 미지정(None) → bare HEAD 해소(`git -C <bare> symbolic-ref HEAD` full ref
        = 원격 default 브랜치)를 base 로 명시값화한다. 해소 실패(rc≠0/비-브랜치 ref)는 빈 문자열
        ("미해소"·worktree add 가 bare HEAD 폴백·현행 동작) — repo 등록 자체는 막지 않는다.
      - `base_arg` 지정 → **로컬 브랜치** 검증(`git -C <bare> show-ref --verify --quiet
        refs/heads/<b>` rc==0). `show-ref --verify` 는 exact-ref primitive(revision 문법
        미적용)라 태그·SHA·`HEAD`·원격 ref 는 물론 `main~0`·`main^{}` 같은 revision 표현도
        통과하지 못한다. 통과면
        반환값은 **기존대로 bare 브랜치명(`base_arg`)**(areas.md base 칼럼 규격 불변), 실패면
        `_BASE_INVALID`(호출부가 명확한 에러 rc 1 로 surface·등록 차단).

    반환: 해소된 base 문자열(빈 문자열 = 미해소·None 동등) 또는 `_BASE_INVALID`(검증 실패).
    """
    if base_arg is None:
        # full ref(`--short` 아님) → `refs/heads/` 접두 정확 제거 (동명 태그 존재 시
        # `--short` 가 `heads/<name>` 로 오염되던 모호성 회피). 실패·비-브랜치 ref → 미해소("").
        rc, out = runner(["-C", str(bare_path), "symbolic-ref", "HEAD"])
        ref = out.strip()
        if rc != 0 or not ref.startswith(_SYMREF_BRANCH_PREFIX):
            return ""  # bare HEAD 해소 실패 → 미해소(worktree add 가 bare HEAD 폴백·현행).
        return ref[len(_SYMREF_BRANCH_PREFIX):]
    # refs/heads/<b> exact-ref 검증 — 태그·SHA·HEAD·원격 ref·revision 문법(main~0·main^{}) 거부,
    # 로컬 브랜치만 통과. show-ref --verify 는 revision 문법을 적용 안 하는 exact-ref primitive.
    rc, _out = runner(
        ["-C", str(bare_path), "show-ref", "--verify", "--quiet", f"refs/heads/{base_arg}"]
    )
    if rc != 0:
        return _BASE_INVALID  # 비-로컬-브랜치(태그/SHA/HEAD/부재) → 등록 차단(잘못된 base 기록 방지).
    return base_arg


# ── bare clone fetch refspec 보정 ───────────────────────────────────

# `git clone --bare` 가 설정하지 않는 fetch refspec — 일반(non-bare) clone 은 이 줄을
# remote.origin.fetch 에 박지만 `--bare` 는 생략한다. 그 결과 bare 에 origin/* remote-tracking
# ref(origin/main 등)가 안 생겨, 그 bare 를 공유하는 worktree 슬롯이 핸드오프 라이브-게이트
#의 baseline(@{upstream}/origin/main)을 해소 못 한다. multi-PM 패밀리 공통 결함.
_BARE_FETCH_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"


def _set_bare_fetch_refspec(bare_path: Path, *, runner: GitRunner) -> None:
    """bare repo 에 fetch refspec 을 박고 origin/* remote-tracking ref 를 채운다 (fail-soft).

    `git clone --bare` 는 일반 clone 과 달리 `remote.origin.fetch` 를 설정하지 않아 bare 에
    `refs/remotes/origin/*` 가 영영 안 생긴다 → 그 bare 를 공유하는 worktree 슬롯이 핸드오프
    라이브-게이트의 baseline(origin/main)을 해소 못 한다(ambiguous→surface). 이를:
      1. `git -C <bare> config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'`
         (set=덮어쓰기·멱등 — 재실행/refspec-없는 과거 bare 보정 안전).
      2. `git -C <bare> fetch origin` (origin/* remote-tracking ref 채움 → origin/main 생성).
    로 근절한다(`--mirror` 가 아니라 명시 refspec — 국소·안전·push --mirror 부작용 없음·결정).

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·base
    해소가 symbolic-ref/show-ref 를 `-C <bare>` 로 호출하는 패턴과 동일). **fail-soft**:
    refspec set 실패·fetch 실패(네트워크)는 경고를 surface 하되 repo add 자체는 막지 않는다
    (refspec 은 박혔으니 이후 fetch 로 복구 가능). bare 는 존재 전제(clone 성공/재사용 후 호출).
    """
    rc, out = runner(
        ["-C", str(bare_path), "config", "remote.origin.fetch", _BARE_FETCH_REFSPEC]
    )
    if rc != 0:
        print(
            f"[경고] bare fetch refspec 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config remote.origin.fetch '{_BARE_FETCH_REFSPEC}'` "
            "후 `git -C <bare> fetch origin` 하라 (origin/* remote-tracking ref·baseline 해소).",
            file=sys.stderr,
        )
        return  # refspec 미설정이면 fetch 해도 origin/* 안 채워짐 — skip.
    rc, out = runner(["-C", str(bare_path), "fetch", "origin"])
    if rc != 0:
        print(
            f"[경고] bare `git -C {bare_path} fetch origin` 실패 (rc={rc}): {out.strip()[:200]}\n"
            "  refspec 은 설정됨 — 네트워크 복구 후 `git -C <bare> fetch origin` 으로 origin/* 채우면 "
            "라이브-게이트 baseline(origin/main)이 해소된다.",
            file=sys.stderr,
        )
        return
    print("✓ bare fetch refspec 설정 + fetch origin — origin/* remote-tracking ref 채움.")


def _ensure_bare_branch_tracking(bare_path: Path, *, runner: GitRunner) -> None:
    """bare repo 의 기본 브랜치에 origin tracking(branch.<d>.remote/merge)을 박는다 (fail-soft).

    `git clone --bare` 는 일반(non-bare) clone 과 달리 로컬 브랜치 tracking config
    (`branch.<n>.remote`/`branch.<n>.merge`)를 설정하지 않는다 → 그 bare 를 공유하는 worktree
    슬롯의 **기본 브랜치가 `@{upstream}`(origin/<default>)을 해소 못 한다**(livegate baseline·
    handoff freshness·`git status` ahead/behind·`git pull` 무인자 영향).
    (`_set_bare_fetch_refspec`)가 tracking 의 한 절반(origin/* remote-tracking ref)을 채웠고,
    이 함수가 나머지 절반(로컬 `branch.<d>.*` tracking config)을 채운다. bare 에 설정하면
    worktree 가 공유 common config(`branch.*`)로 **상속**한다(worktree-local config.worktree 아님).

    동작:
      1. 기본 브랜치 = bare HEAD 해소(`git -C <bare> symbolic-ref HEAD` full ref, 실패·
         비-브랜치 ref 시 `git -C <bare> rev-parse --abbrev-ref HEAD` 폴백).
      2. `git -C <bare> config branch.<d>.remote origin` (set=덮어쓰기·멱등).
      3. `git -C <bare> config branch.<d>.merge refs/heads/<d>`.
    비-bare `git clone` 이 checked-out 기본 브랜치만 tracking 거는 동작을 미러 — **기본 브랜치만**
    설정한다(전 브랜치 일괄 tracking 아님·로컬-전용 의도 브랜치를 잘못 tracking 걸지 않기 위해).

    git 호출은 주입된 clone `runner` 를 `-C <bare>` 로 재사용한다(별도 DI seam 안 만듦·
    `_set_bare_fetch_refspec`·base 해소 동형). **fail-soft**: bare HEAD 미해소·config
    실패는 경고를 surface 하되 repo add 자체는 막지 않는다(tracking 은 *추가 가드*이지 repo add
    핵심 부작용 아님). bare 는 존재 전제(clone 성공/재사용 후 호출).
    """
    # full ref(`--short` 아님) → `refs/heads/` 접두 정확 제거 (동명 태그 모호성 회피).
    rc, out = runner(["-C", str(bare_path), "symbolic-ref", "HEAD"])
    ref = out.strip()
    if rc == 0 and ref.startswith(_SYMREF_BRANCH_PREFIX):
        default_branch = ref[len(_SYMREF_BRANCH_PREFIX):]
    else:
        # detached·비-symbolic HEAD(비-브랜치 ref) 폴백 — 현재 브랜치명을 rev-parse 로 시도.
        rc, out = runner(["-C", str(bare_path), "rev-parse", "--abbrev-ref", "HEAD"])
        default_branch = out.strip()
    if rc != 0 or not default_branch or default_branch == "HEAD":
        print(
            f"[경고] bare 기본 브랜치 해소 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config branch.<d>.remote origin` + "
            f"`git -C {bare_path} config branch.<d>.merge refs/heads/<d>` 하라 "
            "(기본 브랜치 @{upstream} 해소).",
            file=sys.stderr,
        )
        return  # 기본 브랜치 미해소면 tracking 을 걸 대상이 없음 — skip(fail-soft).
    rc, out = runner(
        ["-C", str(bare_path), "config", f"branch.{default_branch}.remote", "origin"]
    )
    if rc != 0:
        print(
            f"[경고] bare `branch.{default_branch}.remote` 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  수동으로 `git -C {bare_path} config branch.{default_branch}.remote origin` + "
            f"`git -C {bare_path} config branch.{default_branch}.merge refs/heads/{default_branch}` "
            "하라 (기본 브랜치 @{upstream} 해소).",
            file=sys.stderr,
        )
        return
    rc, out = runner(
        ["-C", str(bare_path), "config",
         f"branch.{default_branch}.merge", f"refs/heads/{default_branch}"]
    )
    if rc != 0:
        print(
            f"[경고] bare `branch.{default_branch}.merge` 설정 실패 (rc={rc}): {out.strip()[:200]}\n"
            f"  remote 는 설정됨 — 수동으로 `git -C {bare_path} config "
            f"branch.{default_branch}.merge refs/heads/{default_branch}` 로 보완하라.",
            file=sys.stderr,
        )
        return
    print(
        f"✓ bare 기본 브랜치 origin tracking 설정: branch.{default_branch}.remote=origin + "
        f"merge=refs/heads/{default_branch} (슬롯 @{{upstream}} 해소)."
    )


# ── 풀 슬롯 사본 루트 ignore 규칙 ───────────────────────────────────

# 위임 사본 루트(run-dir)를 숨기는 정본 규칙 — 두-git 풀 슬롯의 정본은 그 풀 bare 의
# `info/exclude` 다. bare 의 exclude 는 `$GIT_COMMON_DIR/info/exclude` 라 그 풀의 모든 linked
# worktree 가 공유하므로, 앱 저장소에 프레임워크 파일을 두거나 사람이 exclude 를 손으로
# 고치는 절차가 사라진다. 값은 `pm_delegate.TICKET_COPY_POOL_IGNORE_RULE` 과 같은
# 문자열이어야 한다(판정하는 쪽과 설치하는 쪽의 불변식은 테스트가 고정). 루트 앵커 디렉터리
# 규칙이라 저장소가 추적하는 `.project_manager/tools`·`wiki` 는 숨기지 않는다.
TICKET_COPY_POOL_IGNORE_RULE = "/.project_manager/.local/"
_TICKET_COPY_EXCLUDE_COMMENT = "# pm_delegate ticket copy run-dir (engine-owned)"


def _ensure_bare_ticket_copy_exclude(bare_path: Path) -> bool:
    """bare `info/exclude` 에 사본 루트 ignore 규칙을 멱등 설치한다 (순수 파일 IO·fail-soft).

    규칙 줄이 이미 있으면 아무것도 쓰지 않는다(두 번 불러도 줄은 하나). 없을 때만 주석 1줄 +
    규칙 1줄을 append 한다 — **남이 넣은 줄은 건드리지 않는다**(옛 손 편집 줄 제거는 사람이
    1회 하는 데이터 정리이지 이 함수의 일이 아니다).

    bare 부재는 no-op + 경고다. 없는 자리에 `info/exclude` 를 만들면 `<repo>.git` 디렉터리가
    생겨 나중 `git clone --bare` 가 "destination path already exists" 로 죽는다.

    **fail-soft**: 쓰기 실패는 경고를 surface 하되 repo 등록·슬롯 생성 자체는 막지 않는다
    (옆 두 헬퍼 `_set_bare_fetch_refspec`·`_ensure_bare_branch_tracking` 과 같은 계약). 조용한
    생략이 아니다 — 진짜 게이트는 `pm_delegate` 의 사본 루트 검사(fail-loud)이고, 여기서 rc 를
    1 로 만들면 exclude 쓰기 실패가 repo 등록 자체를 막는다. 반환 = 규칙이 설치된 상태인지.
    """
    exclude = bare_path / "info" / "exclude"
    if not bare_path.is_dir():
        print(
            f"[경고] 사본 루트 ignore 규칙을 설치하지 못했다 — bare 부재: {bare_path}. "
            f"이 풀의 슬롯에서 위임 라운드 준비(`ticket prepare`)가 거부된다 — bare 복구 후 "
            f"`{repo_protection._FACADE_PROG} repo add <repo>` 로 재설치하라 (멱등).",
            file=sys.stderr,
        )
        return False
    try:
        existing = (
            fs.read_text_shared(exclude, encoding="utf-8")
            if exclude.is_file() else ""
        )
        if any(line.strip() == TICKET_COPY_POOL_IGNORE_RULE
               for line in existing.splitlines()):
            return True
        exclude.parent.mkdir(parents=True, exist_ok=True)
        lead = "" if (not existing or existing.endswith("\n")) else "\n"
        with exclude.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                f"{lead}{_TICKET_COPY_EXCLUDE_COMMENT}\n{TICKET_COPY_POOL_IGNORE_RULE}\n"
            )
    except OSError as exc:
        print(
            f"[경고] 사본 루트 ignore 규칙 설치 실패: {exclude} — {exc}. 이 풀의 슬롯에서 위임 "
            f"라운드 준비(`ticket prepare`)가 거부된다 — 수동으로 그 파일에 "
            f"`{TICKET_COPY_POOL_IGNORE_RULE}` 줄을 넣거나 권한 복구 후 "
            f"`{repo_protection._FACADE_PROG} repo add <repo>` 를 재실행하라 (멱등).",
            file=sys.stderr,
        )
        return False
    print(f"✓ 사본 루트 ignore 규칙 설치: {exclude} (`{TICKET_COPY_POOL_IGNORE_RULE}`).")
    return True


# ── 위임 forward 시 usage prog 정합 ────────────────────────


@contextlib.contextmanager
def _forwarded_prog(prog_map: "dict[str, str]"):
    """위임 동안 argparse ArgumentParser 의 지정된 top-level prog 만 치환한다.

    board.py·pm_update.py 는 prog 를 파일명으로 하드코딩하고 main() 에 prog 인자가 없어,
    pm-config 가 위임할 때 usage 줄에 파일명이 새어 나온다. 두 엔진을 수정하지 않고(touches
    격리·CLI 규격 단일 진실 보존) 파서 *생성 시점*에 명시 `prog=` kwarg 가 매핑 키와 정확히
    일치할 때만 값을 갈아끼운다:
      - board: "board.py"→"pm-config" — init 서브파서 usage 는 부모 prog 에서 "pm-config init"
        로 **자동 파생**(argparse add_parser 관례)되므로 이 top-level 치환 하나로 forward 된
        전 usage 줄이 정합한다.
      - pm_update: "pm_update.py"→"pm-config update"(플랫 파서·서브커맨드 없음).
    파생된 subparser prog(예: "pm-config init")·매핑 밖 prog 는 그대로 통과한다. 위임 종료
    후 `__init__` 를 원복한다 — `--help`/인자 에러의 SystemExit·기타 예외에도 finally 로 복구.
    """
    original_init = argparse.ArgumentParser.__init__

    def _patched_init(self, *args, **kwargs):
        prog = kwargs.get("prog")
        if prog in prog_map:
            kwargs["prog"] = prog_map[prog]
        original_init(self, *args, **kwargs)

    argparse.ArgumentParser.__init__ = _patched_init
    try:
        yield
    finally:
        argparse.ArgumentParser.__init__ = original_init


# ── 서브커맨드 핸들러 ─────────────────────────────────────────────────────────


def _resolve_clone_git_url(name, cli_git, already_registered):
    """bare clone 소스 git URL 을 해소한다 (multi-user hydrate). 실패 → None(fail-loud 신호).

    `--git` 미제공 시 areas.md `git` 칼럼(레지스트리·git-tracked·공유)에서 URL 을 읽어,
    already-registered repo 의 `.repos/<repo>.git` bare mirror 를 재제공 없이 hydrate 한다 —
    하나의 채택 폴더를 여러 사람이 clone 할 때 mirror(`.repos/`·gitignore·per-clone)가 없는
    2번째 사용자 시나리오. 반환 `None` 은 호출자(cmd_repo_add)가 rc 1 로 바꾸는 fail-loud
    신호다(에러/경고 문구는 이 함수가 이미 print — 부작용 0·clone 전에 호출됨).

      - already_registered:
          - `--git` 미제공          → areas URL 로 해소(hydrate). areas 에도 URL 없으면 fail-loud.
          - `--git` 제공·areas 값과 다름 → 경고 + **areas 값 우선**(등록=단일 진실·mirror origin 은
                                        등록과 일치해야). CLI URL 로 바꾸려면 areas.md 를 직접 수정.
          - `--git` 제공·일치/areas 빔  → `--git` 값.
      - 미등록:
          - `--git` 제공            → 그 값(신규 repo 등록).
          - `--git` 미제공          → fail-loud(신규 repo 는 clone 원 URL 을 areas 에서 해소 불가).
    """
    areas_url = areas_store._areas_git_url(pm_paths.home_at(REPO), name) if already_registered else None

    if cli_git:
        if already_registered and areas_url and areas_url != cli_git:
            print(
                f"[경고] `--git {cli_git}` 가 areas.md 등록 URL({areas_url}) 과 다르다 — "
                "등록이 단일 진실이므로 areas URL 로 bare mirror 를 만든다(mirror origin 은 "
                "등록과 일치해야 한다). CLI URL 로 바꾸려면 areas.md 를 직접 수정하라.",
                file=sys.stderr,
            )
            return areas_url
        return cli_git

    # `--git` 미제공
    if already_registered:
        if areas_url:
            print(f"✓ `--git` 미제공 — areas.md 등록 URL 로 hydrate: {areas_url} (2번째 사용자).")
            return areas_url
        print(
            f"[중단] repo {name!r} 는 areas.md 등록됐으나 `git` 칼럼이 비어 clone 원 URL 을 "
            "해소할 수 없다 — `--git <url>` 로 명시하라 (clone/등록 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return None
    print(
        f"[중단] `--git <url>` 필수 — repo {name!r} 는 areas.md 미등록이라 clone 원 URL 을 "
        "areas 에서 해소할 수 없다(신규 repo 등록). multi-user 2번째 사용자 hydrate 라면 먼저 "
        "등록(areas.md)을 공유받아야 한다 (clone/등록 전혀 하지 않았다).",
        file=sys.stderr,
    )
    return None


# `git ls-remote --exit-code <원본> HEAD` 가 일치 ref 없음(HEAD 없음 = 커밋 0개 원본)에 내는 rc.
_LS_REMOTE_NO_MATCHING_REF = 2


def cmd_repo_add(
    args: argparse.Namespace,
    *,
    board=None,
    clone_runner: GitRunner | None = None,
    repos_dir: Path | None = None,
) -> int:
    """`repo add <name> [--git <url>] [--test "<cmd>"] [--base <branch>]` — 패밀리에 repo 등록.

    **`--git` 은 optional**: 미제공 시 이미 등록된 repo 는 areas.md `git` 칼럼 URL 로
    bare mirror 를 hydrate 한다(하나의 채택 폴더를 여러 사람이 clone 할 때 `.repos/` mirror 가
    없는 2번째 사용자 시나리오·재제공 없이 재사용). 미등록 신규 repo 는 `--git` 필수(부작용 0
    fail-loud). clone 소스 URL 해소는 `_resolve_clone_git_url` 이 담당한다(불일치 시 areas 우선).

    1. areas.md 레지스트리에 per-repo 줄을 기록한다(board.areas_append — repo/prefix/
       git/test_cmd/owner/base 칼럼). **prefix 는 빈 값으로 등록**한다 —
       prefix=작업 카테고리이지 repo명이 아님.
       카테고리가 필요하면 `board.py new --prefix <cat>` 로 티켓별 명시하거나 `board.py
       prefix` 로 사후 관리한다. owner = 등록 식별자(registrant·협업 소유자 아님) —
       기본 현 세션.
    2. `.repos/<name>.git` 로 bare clone 한다 — worktree 풀이 공유하는 .git 원(
       worktree add 가 이 bare 를 base 로 슬롯을 만든다).

    **base 브랜치 해소** — clone 후, areas 등록 *전*(등록 줄에 base 를 박아야 하므로):
      - `--base` 미지정 → bare HEAD(`git -C .repos/<name>.git symbolic-ref HEAD` full ref
        = 원격 default 브랜치)를 해소해 명시값화·기록. worktree add 가 슬롯 브랜치를 그
        base 에서 파생한다. HEAD 해소 실패 시 base 빈 값(현행 bare HEAD 동작 폴백).
      - `--base <b>` 지정 → 로컬 브랜치 검증(`git -C .repos/<name>.git show-ref --verify --quiet refs/heads/<b>` rc==0).
        없으면 명확한 에러 rc 1(clone 은 됐으나 areas 등록은 막아 잘못된 base 기록 방지).

    **멱등·재시도 가능** (두 부작용 — bare clone + areas 등록 — 이 부분 성공할 수 있음):
      - 이미 등록 + bare 존재     → 완전 no-op rc0 (둘 다 이미 됨·친절 메시지).
      - 이미 등록 + bare 부재     → clone *진행*(재시도) 후 등록 건너뜀(append-only).
                                    첫 실행이 등록만 남기고 clone 실패한 경우의 복구 경로다.
      - 미등록                    → clone → base 해소·검증 → areas_append.
    clone 직전에 원본을 `git ls-remote --exit-code <url> HEAD` 로 조회한다 — 커밋 0개(rc 2)나
    조회 실패면 rc 1 로 멈추고 빈/부분 bare 를 만들지 않는다.
    base 해소가 clone 에 의존하므로(bare HEAD 해소/존재 검증) **clone 을 먼저** 한다 —
    미등록 경로는 clone→base 해소→areas_append. 이미 등록 경로는 base 가 이미 박혀 있어
    재해소하지 않는다(append-only·중복 등록 금지).
    clone 실패해도 등록이 남았지만, 이제 clone 성공 후에만 등록한다(재실행이 둘 다 다시 함).

    board / clone_runner / repos_dir 주입으로 hermetic 테스트(실 등록·clone 없이 배선 검증).
    base 해소 git 호출(symbolic-ref/show-ref)은 같은 `clone_runner` 를 `-C <bare>` 로 재사용한다.
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — areas.md 등록 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    # repos_dir 미주입 시 모듈 전역(REPOS_DIR)을 *호출 시점*에 해소한다 — 함수 default 로
    # 굳히면 테스트의 monkeypatch(REPOS_DIR)가 안 먹는다.
    repos_dir = repos_dir if repos_dir is not None else REPOS_DIR

    name = args.name

    # name 가드 — **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 검증한다.
    # `../x` 폴더탈출·슬래시/공백/`.` 의 areas.md 줄 corruption·leading `-`·빈 문자열을 입구에서
    # 막는다(fail-closed·부분 부작용 0). CLI·콘솔 두 경로가 결국 이 sink 로 수렴하므로 여기 한 곳.
    if not _validate_repo_name(name):
        print(
            f"[중단] repo 이름 {name!r} 형식 위반 — 허용: 영숫자로 시작, 이후 영숫자/`_`/`-` "
            "(정규식 `^[A-Za-z0-9][A-Za-z0-9_-]*$`). 경로분리자(`/`)·`.`·공백·leading `-`·빈 "
            "이름은 금지(폴더탈출·areas.md 줄 corruption 방지). clone/등록/훅 전혀 하지 않았다.",
            file=sys.stderr,
        )
        return 1

    # 보호목록 형식 검증 — **어떤 부작용(bare clone·areas_append·훅 설치)보다
    # 앞에서**. 미지정(None)이면 빈 칼럼 = `_repo_protected` 의 DEFAULT_PROTECTED 폴백(현행).
    # 명시했으면 토큰 형식만 본다(브랜치 실재는 검증 안 함 — 아직 없는 `release` 를 미리 보호하는
    # 게 정상). 빈 문자열은 "보호 없음" 을 뜻할 수 없으므로 fail-loud(플래그 생략으로 안내).
    protected_arg = getattr(args, "protected", None)
    protected_cell = ""
    protected_tokens: list[str] = []
    if protected_arg is not None:
        tokens, reason = areas_rules._validate_protected_tokens(protected_arg)
        if tokens is None:
            print(
                f"[중단] --protected {protected_arg!r} 거부 — {reason} "
                "(clone/등록/훅 전혀 하지 않았다·부작용 0). 기본값(main/master/develop)이면 "
                "`--protected` 를 생략하라.",
                file=sys.stderr,
            )
            return 1
        protected_tokens = tokens
        protected_cell = ",".join(tokens)

    # case-only 중복 거부 (repo명=prefix 동일성=case-insensitive fold) — 정확-case 는
    # 아래 already_registered 재사용(멱등) 경로로 빠지지만, case 만 다른 근접 중복(`svc` vs 등록
    # `SVC`)은 레지스트리에 fold-충돌하는 두 행을 만든다(네임스페이스 분할). **어떤 부작용(clone·
    # mkdir·등록·훅)보다 앞에서** 등록 canonical case 로 안내하고 fail-loud(부작용 0).
    registered_names = areas_store.registered_repos(pm_paths.home_at(REPO))
    if name not in registered_names:
        fold = name.lower()
        conflict = next((r for r in registered_names if r.lower() == fold), None)
        if conflict is not None:
            print(
                f"[중단] repo {name!r} 은 이미 등록된 {conflict!r} 과 대소문자만 다르다 "
                f"(repo명·prefix 동일성은 case-insensitive). 등록된 case {conflict!r} 를 "
                "그대로 쓰라 (clone/등록/훅 전혀 하지 않았다·부작용 0).",
                file=sys.stderr,
            )
            return 1

    # owner = areas.md 등록 식별자(registrant) — **사람** 차원이다. 슬롯 세션에서 유도하지
    # 않는다: 등록은 어느 슬롯에서 쳤는지와 무관하고, 작업 폴더가 아직 없는 홈(첫 등록)에서는
    # 세션이 아예 없다. 값의 단일 진실은 `identity.user`(없으면 git user.email)이고 그 선언을
    # 그대로 쓴다(`_default_user`·`board.user_name` 동형). `--owner` 명시는 종전대로 이긴다.
    # 미해소면 fail-loud — **어떤 부작용(bare clone·areas_append·훅 설치)보다 앞에서** 차단한다
    # (그대로 넘기면 board 가 owner 를 문자열 "None" 으로 areas.md 에 누출한다).
    owner = args.owner or _default_user()
    if not owner:
        print(
            "[중단] 등록 owner 미해소 — `identity.user` 도 git user.email 도 없다. "
            "`--owner <id>` 로 등록 식별자를 명시하거나 `local.conf` 에 `identity.user=` 를 "
            "적어라 (clone/등록/훅 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return 1
    # area_owner = 그 area 의 *user* 소유(`--mine` 풀 입력) — registrant
    # `owner`(등록한 사람)와 별개 칼럼(overload 금지·codex sug). `--user` 명시 > local.conf identity.user=
    # > git config user.email > None(빈 칼럼·소비자가 None 으로 읽음·현행 동작).
    area_owner = getattr(args, "user", None) or _default_user()
    base_arg = getattr(args, "base", None)
    bare_path = repos_dir / f"{name}.git"
    # 멱등 재등록 판별은 **repo명**으로 한다(prefix 로 세지 않는다) — 자동시드 폐지
    # 후 prefix 칼럼이 비므로 `registered_prefixes()` 는 이 repo 를 못 센다. `registered_repos()`
    # 는 repo 칼럼을 직접 세어 중복 append(같은 repo 두 줄)를 막는다. 위 case-only 가드에서 이미
    # 조회한 `registered_names` 를 재사용한다(같은 areas 스냅샷·중복 조회 dedupe).
    already_registered = name in registered_names
    runner = clone_runner or git._real_clone_runner()

    # bare 는 *경로 존재*(exists)만이 아닌 *실 bare git repo* 인지로 판정한다 — 중단된
    # `git clone --bare`(하네스 타임아웃·Ctrl-C)가 남긴 부분/빈/깨진 `.repos/<name>.git` 도
    # exists()=True 라, 경로 존재만 보면 "존재 → 재사용"으로 조용히 통과시켜 무효 bare 를 재사용한다
    # (repo add 는 rc0 인데 invariant 깨진 채·나중 worktree add 가 날 git 에러로 죽음·audit #1→#4).
    # `_is_valid_bare`(rev-parse --is-bare-repository·worktree_pool)로 실 bare 를 판정해 그 통과를
    # fail-loud 로 닫는다. **파괴적 재clone 신중(§결정)**: 깨진 bare 자동삭제는 위험(사용자 데이터
    # 오판)이라 하지 않고, 정확 진단 + 수동 삭제 위임으로 안내한다(삭제는 사용자 위임 원칙). fail-soft:

    bare_present = bare_path.exists()
    if bare_present and (not worktree_git._is_valid_bare(bare_path, runner=runner)):
        print(
            f"[중단] `.repos/{name}.git` 경로는 있으나 유효한 bare git repo 가 아니다 — 부분/깨진 "
            f"bare (중단된 `git clone --bare` 잔존 가능성·하네스 타임아웃/Ctrl-C). 경로 존재만 보면 "
            f"재사용으로 조용히 통과하지만 `git worktree add` 의 base 로 못 써 나중 날 git 에러로 죽는다. "
            f"자동 삭제는 하지 않는다(사용자 데이터 오판 위험·삭제는 사용자 위임) — `.repos/{name}.git` 를 "
            f"수동 삭제 후 `pm-config repo add {name}`(--git 불요·areas 등록 URL 로 재hydrate·미등록이면 "
            f"`--git <url>` 재제공)로 재생성하라 (부작용 0·clone/등록/훅 전혀 하지 않았다).",
            file=sys.stderr,
        )
        return 1
    # 여기 도달 = bare 부재이거나 유효 bare (무효는 위에서 return 1) — 유효 bare 존재면 clone 건너뛰고
    # 재사용, 부재면 clone (재)시도. 종전 `bare_exists` 의미(존재 → 재사용)를 실 bare 로 좁힌 것.
    bare_exists = bare_present

    # clone 소스 git URL 해소 — `--git` 미제공 시 areas.md `git` 칼럼에서 해소해
    # multi-user 2번째 사용자의 bare mirror hydrate 를 완성한다(재제공 없이). None 이면
    # fail-loud(미등록+`--git` 없음 등) — 어떤 부작용(clone·mkdir·등록)보다 앞에서 걸러
    # 부작용 0 을 보장한다. 해소된 URL 을 이하 clone/areas_append 가 일관되게 쓴다.
    # URL 은 **실제로 필요할 때만** 해소한다 — clone(bare 부재) 또는 신규 등록(미등록)일 때. 이미
    # 등록 + bare 존재면 순수 no-op(refspec/tracking/보호훅 자가치유·아래 이미 등록 분기)라 URL 이
    # 불필요하니, areas `git` 칼럼이 빈 레거시/부분 등록이어도 fail-loud 하지 않는다(codex 지적·
    # `repo add` 재실행=훅 자가치유 경로 보존·`--git` optional 계약 완결).
    git_url = None
    if (not bare_exists) or (not already_registered):
        git_url = _resolve_clone_git_url(name, args.git, already_registered)
        if git_url is None:
            return 1

    # 두 부작용(bare clone + areas 등록)을 멱등화한다 — base 해소가 bare 에 의존
    # 하므로(bare HEAD/존재 검증) **clone 을 먼저**, 그 다음 base 해소·areas 등록 순서다.

    # 1) bare clone → .repos/<name>.git (worktree 풀 공유 .git 원). bare 가 이미
    #    있으면 건너뛴다(재사용·중복 clone/클로버 방지). bare 부재면 clone(재)시도.
    if bare_exists:
        print(f"✓ .repos/{name}.git 이미 존재 — clone 건너뜀 (재사용).")
    else:
        # 원본 조회 — 원본 HEAD 가 해소되지 않으면 clone 이 HEAD 없는 bare 를 만들고 엔진 명령으로는
        # 되돌릴 수 없다. `ls-remote --exit-code` 는 HEAD 가 없으면 rc 2 다(커밋 0개, 또는 원본 HEAD
        # 가 없는 브랜치를 가리킴). 그 밖의 실패(없는 경로·네트워크·권한)도 clone 전에 멈춘다 — 두
        # 경우 모두 mkdir·clone·등록·훅 0.
        rc, out = runner(["ls-remote", "--exit-code", git_url, "HEAD"])
        if rc == _LS_REMOTE_NO_MATCHING_REF:
            print(
                f"[중단] 원본 {git_url} 에 커밋이 없다(또는 원본 HEAD 가 없는 브랜치를 가리킨다). 빈 "
                "bare 를 만들지 않는다. 원본 저장소에서 먼저 커밋하거나 원본 HEAD 를 있는 브랜치로 "
                "바꾼 뒤 같은 명령을 다시 실행하라 (clone/등록/훅 전혀 하지 않았다).",
                file=sys.stderr,
            )
            return 1
        if rc != 0:
            print(
                f"[중단] 원본 {git_url} 을 조회하지 못했다 (git ls-remote rc={rc}): {out.strip()}. "
                "주소·네트워크·권한을 확인한 뒤 다시 실행하라 (clone/등록/훅 전혀 하지 않았다).",
                file=sys.stderr,
            )
            return 1
        repos_dir.mkdir(parents=True, exist_ok=True)
        rc, out = runner(["clone", "--bare", git_url, str(bare_path)])
        if rc != 0:
            print(
                f"[경고] `git clone --bare {git_url}` 실패 (rc={rc}):\n{out}\n"
                f"  네트워크/URL 확인 후 수동으로 `git clone --bare {git_url} {bare_path}` 하거나 "
                "재시도하라(등록은 clone 성공 후·멱등).",
                file=sys.stderr,
            )
            return 1
        print(f"✓ .repos/{name}.git bare clone 완료.")

    # 1b) bare fetch refspec 보정 — clone 성공/기존 bare 재사용 *둘 다* 에서 수행한다.
    #     `git clone --bare` 가 remote.origin.fetch 를 설정하지 않아 origin/* remote-tracking
    #     ref(origin/main)가 안 생기는 결함을 근절한다(멱등 — refspec-없는 과거 bare 도 보정).
    #     clone 실패 시엔 위에서 이미 return 1 했으므로 여기 도달 = bare 존재 전제(fail-soft).
    _set_bare_fetch_refspec(bare_path, runner=runner)

    # 1c) bare 기본 브랜치 origin tracking 보정. refspec 이 origin/*
    #     remote-tracking ref 를 채웠고, 이 헬퍼가 로컬 branch.<d>.remote/merge tracking config 를
    #     박아 그 bare 슬롯 기본 브랜치의 @{upstream}(origin/<default>) 해소를 닫는다. refspec 보정과
    #     같은 위치(clone 성공/기존 bare 재사용 둘 다·already_registered early-return 이전)라 과거
    #     tracking-없는 bare 도 다음 repo add 에 자가치유된다(멱등·fail-soft).
    _ensure_bare_branch_tracking(bare_path, runner=runner)

    # 1d) 사본 루트 ignore 규칙 설치 — 이 풀의 모든 슬롯이 공유하는 bare `info/exclude` 에
    #     위임 run-dir 규칙을 멱등으로 박는다. refspec/tracking 보정과 같은 자리
    #     (already_registered early-return 이전)라 기존 풀도 다음 `repo add` 에 규칙을 얻는다.
    _ensure_bare_ticket_copy_exclude(bare_path)

    # 2) 이미 등록돼 있으면 base 재해소/등록을 건너뛴다(append-only·중복 등록 금지). base 는
    #    첫 등록 때 박힌 값 그대로(clone 만 실패했던 재시도 경로는 위 clone 으로 복구됨).
    #    보호 훅은 멱등 자가치유라 *재등록 경로에서도* (재)설치한다 — 엔진 update 후
    #    기존 repo 도 다음 repo add/worktree add 에 훅을 얻는다(별도 명령 불요).
    if already_registered:
        print(f"✓ repo {name!r} 이미 areas.md 등록됨 — 등록 건너뜀.")
        # `--protected` 는 *등록 줄*에 실리는 값이라 등록을 건너뛰는 이 경로에선 반영되지 않는다.
        # 조용히 삼키면 사용자는 보호목록을 바꿨다고 믿는데 areas 는 그대로다(값-연결 끊김과 같은
        # 클래스) → loud 안내 + 정확한 대체 커맨드.
        if protected_arg is not None:
            print(
                f"[경고] `--protected {protected_arg!r}` 는 이미 등록된 repo 라 **반영되지 않았다** "
                "(등록 줄은 append-only·이 경로는 등록을 건너뛴다). 기존 행의 보호목록을 바꾸려면: "
                f"`{repo_protection._FACADE_PROG} repo protected {name} \"{protected_cell}\"`.",
                file=sys.stderr,
            )
        repo_protection._install_protected_hook_reporting(pm_paths.home_at(REPO),
            name, action="(재)설치")
        return 0

    # 3) base 브랜치 해소 — bare 가 존재하는 지금 시점에 해소·검증한다.
    #    --base 지정 → 존재 검증(없으면 rc 1·areas 등록 막음). 미지정 → bare HEAD 명시값화.
    base = _resolve_base(base_arg, bare_path, runner=runner)
    if base is _BASE_INVALID:
        print(
            f"[중단] --base {base_arg!r} 가 `.repos/{name}.git` 에 없다 "
            f"(`git -C {bare_path} show-ref --verify --quiet refs/heads/{base_arg}` 실패). "
            "브랜치명을 확인하거나 `--base` 를 생략(기본 브랜치 사용)하라.",
            file=sys.stderr,
        )
        return 1

    # 4) areas.md 등록 — repo/prefix/git/test_cmd/owner/base/protected 칼럼.
    #    prefix(2번째 positional)는 **빈 값**으로 등록 — repo명 자동시드 폐지: repo명은
    #    작업 카테고리가 아니다.
    #    으로 튀게 했다. protected 는 `--protected` 인자— 미지정이면 빈 값이라
    #    `_repo_protected` 가 DEFAULT_PROTECTED(main/master/develop) 폴백하고, 사후 변경은
    #    `pm-config repo protected <name> <목록>`(areas → sidecar 정합화)로 한다.
    board_mod.areas_append(
        "", "", owner, repo=name, git=git_url, test_cmd=args.test, base=base,
        protected=protected_cell, area_owner=area_owner,
    )
    # --test 미지정(None) 이면 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로
    # 폴백한다. 빌드명령은 worktree add 프롬프트·콘솔 [b] 에서 채울 수 있다.
    test_surface = args.test if args.test else "(미지정 — worktree add/콘솔 [b] 에서 설정)"
    base_surface = base if base else "(미해소 — worktree add 가 bare HEAD 사용)"
    area_owner_surface = area_owner if area_owner else "(미상 — local.conf identity.user= / git user.email 미설정)"
    protected_surface = protected_cell if protected_cell else "(미지정 — main/master/develop 기본값)"
    print(
        f"✓ areas.md 등록: {name} | git={git_url} | test_cmd={test_surface} | "
        f"owner={owner} | base={base_surface} | protected={protected_surface} | "
        f"area_owner={area_owner_surface}"
    )
    # 5) 보호 브랜치 pre-push 훅 설치 (멱등 자가치유) — 보호목록(areas protected→
    #    default) sidecar + bare core.hooksPath wiring. 회사 repo 무영향(.project_manager/.local).
    #    설치 실패는 조용히 넘기지 않는다(공용 깔때기 — 훅 미설치 침묵 = 보호 가드 무력화).
    repo_protection._install_protected_hook_reporting(pm_paths.home_at(REPO),
        name, action="설치")
    # 6) 브랜치 실재 경고 1줄 — `repo protected` set 경로와 **같은 공용 깔때기**.
    #    거부 아님(미래 브랜치 선-보호가 정상)·오타(`mian`)가 기본 보호목록을 덮으며 조용히
    #    통과하는 것만 막는다. bare 는 방금 clone/재사용돼 존재하므로 조회 가능(불가면 생략).
    repo_protection._warn_missing_protected_branches(pm_paths.home_at(REPO),
        name, protected_tokens, clone_runner=runner, repos_dir=repos_dir)
    return 0


def _print_protected_status(
    name: str,
    *,
    clone_runner: GitRunner | None = None,
) -> int:
    """`repo protected <name>` 조회 — 실효값 + 출처 + sidecar 정합/drift 3줄.

    "빈 값이라 기본값 폴백 중" 과 "이 clone 의 훅은 아직 옛 값으로 동작" 두 사실이 안 보이면
    사용자가 계속 헷갈린다 — 그래서 **실효값**(board 가 리졸브한 목록)·**출처**(명시/기본값
    폴백/미등록)·**sidecar 정합**(훅이 실제로 읽는 파일과 같은가)을 함께 낸다. 조회는 순수
    읽기라 어떤 파일도 쓰지 않는다(rc 0).

    **sidecar 줄은 두 축을 본다(should-fix)**:  내용 정합(sidecar == 실효값)  **배선**
    (`core.hooksPath` 가 우리 훅 디렉토리를 가리키나·`protected_hook_wired`).
    "sidecar 는 최신인데 훅이 아예 안 걸린" 부분성공(`install_protected_hook` 3단계 실패)이
    `✓ 정합` 으로 보인다 — 보호가 꺼져 있는데 정합하다고 거짓 보고하는 것이다.
    """
    effective = branch_store._resolve_protected(pm_paths.home_at(REPO), name)
    print(f"{name} · protected = {', '.join(effective)}")

    cell = repo_protection._areas_protected_cell(pm_paths.home_at(REPO), name)
    if cell is None:
        print("  출처: 미등록 repo (areas.md 에 그 행이 없음) — DEFAULT_PROTECTED 기본값 폴백")
    elif cell.strip():
        print("  출처: 명시 (areas.md `protected` 칼럼)")
    else:
        print("  출처: 기본값 폴백 (areas.md `protected` 칼럼 비어 있음 — DEFAULT_PROTECTED)")

    path = protected_hooks._protected_sidecar_path(pm_paths.home_at(REPO), name)
    sidecar = protected_hooks._read_protected_sidecar(pm_paths.home_at(REPO), name)
    if sidecar is None:
        print(f"  훅 sidecar: {path} → (미설치) "
              f"⚠ 이 clone 엔 보호 훅이 아직 없다")
        print(f"    → 설치:  {repo_protection._FACADE_PROG} repo add {name}  (멱등·bare 존재 전제)")
        return 0
    wired = protected_hooks.protected_hook_wired(pm_paths.home_at(REPO),
        name, git_runner=clone_runner)
    listed = ", ".join(sidecar) or "(빈 파일)"
    # 재실행 안내는 **현재 상태**(명시/폴백/미등록)에 맞춰야 한다 — 폴백 상태에 명시 커맨드를
    # 안내하면 사용자가 그대로 실행했을 때 출처가 조용히 "명시" 로 바뀐다(단일 분기 헬퍼).
    retry = repo_protection.protected_retry_command(pm_paths.home_at(REPO), name)
    if sidecar != effective:
        # 내용 drift 가 지배적 — 배선 여부와 무관하게 재설치가 답이다(재설치가 둘 다 고친다).
        print(f"  훅 sidecar: {path} → {listed}  "
              f"⚠ 옛 목록({', '.join(sidecar) or '없음'}) — 이 clone 의 훅은 아직 옛 값으로 동작")
        print(f"    → 정합화:  {retry}"
              f"   (또는 `{_runtime_skill_entry('pm-bootstrap')}` 이 세션 시작에 자동 정합화)")
        return 0
    if wired is False:
        # 내용은 최신인데 배선이 끊긴 부분성공 — sidecar 만 보면 `✓ 정합` 으로 보이는 그 상태.
        print(f"  훅 sidecar: {path} → {listed}  "
              "⚠ 훅 미배선 — 목록은 최신이나 bare `core.hooksPath` 가 이 디렉토리를 가리키지 "
              "않아 pre-push·pre-commit 훅이 아예 발화하지 않는다(보호 꺼짐)")
        print(f"    → 재배선:  {retry}   (멱등·hooksPath 재설정)")
        return 0
    if wired is None:
        print(f"  훅 sidecar: {path} → {listed}  "
              "✓ 목록 정합 (배선 확인 불가 — bare 부재/git 미해소)")
        return 0
    print(f"  훅 sidecar: {path} → {listed}  ✓ 정합 (목록·hooksPath 배선)")
    return 0


def cmd_repo_protected(
    args: argparse.Namespace,
    *,
    board=None,
    clone_runner: GitRunner | None = None,
    repos_dir: Path | None = None,
) -> int:
    """`repo protected <name> [<목록>|default]` — 보호 브랜치 목록 조회/설정.

    값 인자 유무로 get/set 이 갈린다(`upstream show|set`·`task prefix <name> <p|none>` family
    동형). `default`(대소문자 무관)는 **칼럼 비움** = `DEFAULT_PROTECTED`
    (main/master/develop) 폴백이지 "보호 해제" 가 아니다 — 빈 문자열 지정은 fail-loud 한다.

    설정 순서(**areas → sidecar 고정**·역순 금지):
      1. 형식 검증(토큰만·브랜치 실재는 검증 안 함) — 실패면 부작용 0 rc1.
      2. `board.areas_set_cell(name, "protected", …)` — `board_lock()` 하 비파괴 in-place 재기록.
         중복 repo 행이면 fail-loud(부작용 0·수동 정리 안내), 미등록이면 `repo add` 안내.
      3. 훅 sidecar 재설치(`_install_protected_hook` 재사용·멱등). 실패는 loud 경고 + 재실행
         안내 — areas 는 이미 비준됐으므로 rc 0(다음 `repo add`/`/pm-bootstrap` 이 자가치유).
      4. board-git best-effort 동기(공유 정책 변경은 즉시 공유돼야 값-연결이 산다).
      5. bare 에 없는 브랜치가 있으면 경고 1줄(거부 아님 — 미래 브랜치 선-보호가 정상).

    board / clone_runner / repos_dir 주입으로 hermetic 테스트.
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — 보호목록 조회/설정 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    name = args.name
    if not _validate_repo_name(name):
        print(
            f"[중단] repo 이름 {name!r} 형식 위반 — 허용: 영숫자로 시작, 이후 영숫자/`_`/`-` "
            "(정규식 `^[A-Za-z0-9][A-Za-z0-9_-]*$`). areas.md 는 전혀 건드리지 않았다.",
            file=sys.stderr,
        )
        return 1

    value = getattr(args, "value", None)
    if value is None:
        return _print_protected_status(
            name,
            clone_runner=clone_runner)

    # ── set ──────────────────────────────────────────────────────────────
    if value.strip().lower() == areas_rules._PROTECTED_DEFAULT_LITERAL:
        tokens: list[str] = []
        new_cell = ""            # 칼럼 비움 → `_repo_protected` 가 DEFAULT_PROTECTED 폴백.
    else:
        parsed, reason = areas_rules._validate_protected_tokens(value)
        if parsed is None:
            print(f"[중단] 보호목록 거부 (areas.md 미변경) — {reason}", file=sys.stderr)
            return 1
        tokens = parsed
        new_cell = ",".join(tokens)

    # 2) areas.md 셀 변경 — 단일 진실 먼저(역순이면 훅이 비준되지 않은 목록을 강제한다).
    duplicate_exc = getattr(board_mod, "AreasDuplicateRepo", None)
    missing_exc = getattr(board_mod, "AreasRepoNotFound", None)
    try:
        old_cell, _new = board_mod.areas_set_cell(name, "protected", new_cell)
    except Exception as exc:  # noqa: BLE001 — 종류별 안내로 번역(부작용 0·아래 rc 1).
        if duplicate_exc is not None and isinstance(exc, duplicate_exc):
            print(
                f"[중단] areas.md 에 repo {name!r} 행이 {getattr(exc, 'count', 2)}개 있다(중복) — "
                "어느 행을 고쳐야 하는지 기계가 정할 수 없다(리졸버는 first-match). 추측해서 한쪽만 "
                "고치지 않는다(areas.md 미변경·부작용 0). 한 행만 남기고 수동 정리한 뒤 다시 실행하라 "
                "(`board.py lint` 의 `areas-duplicate-repo` 권고가 같은 상태를 상시 표면화한다).",
                file=sys.stderr,
            )
            return 1
        if missing_exc is not None and isinstance(exc, missing_exc):
            print(
                f"[중단] repo {name!r} 이(가) areas.md 에 등록돼 있지 않다 — 보호목록 설정은 *기존* "
                "행의 셀을 고칠 뿐 등록을 만들지 않는다. 먼저 등록하라: "
                f"`{repo_protection._FACADE_PROG} repo add {name} --git <url> --protected \"{value}\"`.",
                file=sys.stderr,
            )
            return 1
        print(f"[중단] areas.md 기록 실패 (미변경) — {exc}", file=sys.stderr)
        return 1

    old_surface = old_cell.strip() or "(빈 값 — 기본값 폴백)"
    new_surface = new_cell or "(빈 값 — 기본값 폴백 main/master/develop)"
    print(f"✓ areas.md `protected` 갱신: {name}  {old_surface} → {new_surface}")

    # 3) 파생 sidecar 정합화 — 훅이 *실제로 읽는* 파일. 멱등 재설치(`repo add`·`worktree add` 공용).
    # areas 는 이미 비준됐으므로 sidecar 실패는 rc 를 바꾸지 않는다(다음 repo add/`/pm-bootstrap`
    # 이 자가치유) — 대신 공용 깔때기가 실패를 loud 하게 낸다(침묵 금지).
    # 재실행 안내는 방금 비준한 상태 기준 — `default` 로 되돌린 경우엔 `default` 를 안내해야
    # 정확하다(`tokens` 가 빈 리스트라 falsy → 옛 코드는 `repo add` 로 잘못 떨어졌다).
    repo_protection._install_protected_hook_reporting(pm_paths.home_at(REPO),
        name, action="정합화",
        retry=repo_protection.protected_retry_command(pm_paths.home_at(REPO), name))

    # 4) board-git best-effort 동기 — 공유 정책 변경은 즉시 공유돼야 값-연결이 산다.
    #    **경로 스코프 필수**: paths 없이 부르면 board 전체가 "repo protected"
    #    커밋에 쓸려 들어가 *남의 미커밋 편집*까지 대신 커밋한다. claim 이 dirty 를 안 막게 된
    #    지금은 board 가 상시 dirty 라 그 노출이 더 크다. areas.md 만 이 mutation 의 산출물이다.
    sync = getattr(board_mod, "_board_git_sync_best_effort", None)
    if sync is not None:
        try:
            sync("repo protected", (pm_paths.areas_file(pm_paths.home_at(REPO)),))
        except Exception as exc:  # noqa: BLE001 — best-effort: 동기 실패가 설정을 되돌리지 않는다.
            pass

    # 5) 브랜치 실재 경고 1줄 — `repo add --protected` 와 **같은 공용 깔때기**(검증 비대칭 방지).
    #    거부 아님(미래 브랜치 선-보호가 정상·오타 가시화용).
    repo_protection._warn_missing_protected_branches(pm_paths.home_at(REPO),
        name, tokens, clone_runner=clone_runner, repos_dir=repos_dir)
    return 0


# `repo list` 표 칼럼 — areas.md 레지스트리에서 PM 이 조정하는 값 + 그 repo 가 어느 원격에
# 붙어 있는지(`git`). owner 는 등록 시점 메타라 제외. 헤더 라벨 = areas.md 칼럼명 그대로.
_REPO_LIST_COLUMNS = ("repo", "prefix", "git", "base", "protected", "test_cmd", "area_owner")


def _display_width(text: str) -> int:
    """터미널 표시 폭 — 전각(한글·CJK·전각기호) 문자를 2칸으로 센다.

    `len()` 은 코드포인트 수라 한글 셀이 섞이면 표 정렬이 눈에 띄게 깨진다(reviewer 실측). 이
    프로젝트의 출력은 사실상 전부 한국어라 실사용에서 계속 보이는 문제다. `unicodedata.
    east_asian_width` 의 W(Wide)·F(Fullwidth)만 2칸으로 세고, 결합문자(Mn·Me)는 0칸으로 뺀다.
    """
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _pad_display(text: str, width: int) -> str:
    """`_display_width` 기준으로 오른쪽 공백 패딩 (`str.ljust` 의 전각-인지 버전)."""
    return text + " " * max(0, width - _display_width(text))


def cmd_repo_list(args: argparse.Namespace) -> int:
    """`repo list` — 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner).

    areas.md 레지스트리의 조회 표면이다 — 보호목록을 고치려면 *지금 뭐가 등록돼 있는지* 부터
    보여야 한다. 빈 셀은 그 칼럼의 폴백을 괄호로 밝힌다(특히 `protected` 빈 값 = 기본값 폴백 —
    "빈 칸" 을 "보호 없음" 으로 오독하지 않게). 순수 읽기(rc 0·미등록이어도 안내 후 0).
    """
    try:
        _header, rows = areas_store._parse_areas(pm_paths.home_at(REPO))
    except Exception as exc:  # noqa: BLE001 — 파싱 실패는 명시 에러(조용한 빈 표 금지).
        print(f"[중단] areas.md 파싱 실패 — {exc}", file=sys.stderr)
        return 1
    rows = [row for row in rows if row.get("repo")]
    if not rows:
        print(f"등록된 repo 없음 — `{repo_protection._FACADE_PROG} repo add <name> --git <url>` 로 등록하라.")
        return 0
    default_protected = ",".join(areas_rules.DEFAULT_PROTECTED)
    table: list[list[str]] = [list(_REPO_LIST_COLUMNS)]
    for row in sorted(rows, key=lambda r: r.get("repo", "")):
        cells = []
        for column in _REPO_LIST_COLUMNS:
            val = (row.get(column) or "").strip()
            if val:
                cells.append(val)
            elif column == "protected":
                cells.append(f"({default_protected} · 기본값)")
            else:
                cells.append("-")
        table.append(cells)
    # 폭은 **표시 폭** 기준(전각 2칸) — `len()` 이면 한글 셀(예 test_cmd·area_owner)에서 정렬이
    # 깨진다. 구분선도 같은 기준이라 헤더와 자리가 맞는다.
    widths = [max(_display_width(r[i]) for r in table)
              for i in range(len(_REPO_LIST_COLUMNS))]
    for i, cells in enumerate(table):
        print("  ".join(_pad_display(c, w) for c, w in zip(cells, widths)).rstrip())
        if i == 0:
            print("  ".join("-" * w for w in widths))
    print(f"\n보호목록 조회/변경: `{repo_protection._FACADE_PROG} repo protected <repo> [<목록>|default]`")
    return 0


def _prompt_test_cmd(input_fn: Callable[[str], str], *, default: str) -> str | None:
    """worktree add 빌드명령 프롬프트 — `빌드/테스트 명령? [Enter=repo 기본값 유지 <default>]:`.

    **빈 입력(엔터만) → None(슬롯 미바인딩)** — 슬롯 리스 test_cmd 가 board 의 해소 체인서
    areas per-repo test_cmd 보다 *우선*이라, 빈입력에 기본값을 박으면 areas 의
    그 repo test_cmd(예: `go test ./...`)를 잘못 덮는다(must-fix 1·codex). 빈입력은 None 으로
    슬롯을 비워 해소 체인이 areas/local.conf 로 폴백하게 한다(기존 동작 보존). `default` 는
    *표시 전용* — Enter 시 적용될 repo 폴백값(areas→local.conf→pytest-q)을 투명하게 보일 뿐
    슬롯엔 안 박는다. 비어있지 않은 입력만 그 값으로 슬롯에 바인딩된다.

    `EOFError`/`KeyboardInterrupt` 는 우아하게 None 으로 흡수(미지정 폴백·크래시 0). input_fn
    주입으로 hermetic 테스트(라이브 input 블록 0·주입 시퀀스로 결정적).
    """
    try:
        raw = input_fn(f"빌드/테스트 명령? [Enter=repo 기본값 유지: {default}]: ")
    except (EOFError, KeyboardInterrupt):
        return None
    cmd = raw.strip()
    return cmd if cmd else None


def _require_worktree_add_user_ack(args: argparse.Namespace) -> bool:
    """물리 슬롯 생성에 repo 값-결속 사용자 승인 토큰을 요구한다.

    CLI의 ``--user-ack <repo>``와 콘솔 ``[w]``의 repo 재입력 확인이 이 한 sink로
    수렴한다. 토큰은 자가-증언형 감사/마찰 장치이며 raw git/장부 직접 편집 같은 적대적
    우회까지 막는 보안 경계는 아니다.
    """
    repo = args.repo
    user_ack = getattr(args, "user_ack", None)
    if user_ack == repo:
        print(f"[승인 감사] worktree add: 사용자 승인 토큰이 repo {repo!r}에 값-결속됨.")
        return True
    mismatch = (
        f" 제공된 값 {user_ack!r}은 repo {repo!r}에 결속되지 않았다."
        if user_ack is not None
        else ""
    )
    print(
        f"[중단] worktree add {repo!r}: 물리 슬롯 생성에는 사용자 명시 승인이 필요하다.{mismatch}\n"
        f"  1순위: 사용자에게 repo {repo!r}의 새 슬롯 생성 승인을 요청하라.\n"
        f"  부차 수단: 승인한 사용자만 `--user-ack {repo}`를 대상값 그대로 붙여 실행하라"
        "(세션 자동 부착 금지).",
        file=sys.stderr,
    )
    return False


def cmd_worktree_add(
    args: argparse.Namespace,
    *,
    board=None,
    input_fn: Callable[[str], str] = input,
    is_tty: Callable[[], bool] | None = None,
    clone_runner: GitRunner | None = None,
) -> int:
    """`worktree add <repo> [--test "<cmd>"] [--task <task>] [--readonly]` — 새 슬롯 생성 + submodule init.

    **`--task <task>`**: 생성 직후 그 슬롯을 task 명의로 대여한다(session=task·alloc
    동형·reclaim/재부착 보호는 tasks 장부 조인). 풀 소진 시 alloc 이 안내하는 "생성+대여 한 흐름" — min-idle 재탐색의
    오슬롯 리스크가 없다. task 명 검증 + 기바인딩은 alloc 과 같은 헬퍼로. `--readonly` 와 상호배타.

    pm_engine.app.worktree_pool.create_slot(repo, base=) 를 호출한다 — `<repo>_<N>`(브랜치 무관 재사용
    컨테이너) 슬롯을 `git worktree add` 로 만들고 `git submodule update --init` 한다.
    브랜치 무관(슬롯 브랜치는 `task branch`·`worktree_pool.py switch` 가 바꾼다).

    **base 브랜치**: areas.md 의 그 repo base(`pm-config repo add --base`/clone-time
    bare HEAD 가 기록)를 `_resolve_repo_base` 로 읽어 `create_slot(base=)` 로 전달한다 — 슬롯
    브랜치 `<repo>_<N>` 가 그 base(develop 등)에서 파생되고 base 가 그 브랜치의 부모로 기록된다.
    areas 에 base 가 없으면 부모를 적을 수 없어 슬롯을 만들지 않는다(rc 1).

    test_cmd(슬롯 리스 바인딩) 해소:
      - `--test "<cmd>"` 명시 → 그 값을 바인딩(현행·CLI 정확작업·CI).
      - `--test` 미지정 + **tty** → 슬롯 생성 후 빌드명령 프롬프트. **빈입력(Enter) → None
        (슬롯 미바인딩)** → 해소 체인이 areas/local.conf 로 폴백(기존 동작 보존·must-fix 1).
        프롬프트 `[Enter=repo 기본값 유지: …]` 표시값은 그 repo 의 *실제 폴백*(areas test_cmd
        → local.conf → pytest -q·`_resolve_repo_test_cmd`)이라 Enter 가 무엇을 적용하는지 투명.
      - `--test` 미지정 + 비-tty(CI/파이프) → 프롬프트 생략·None(현행·repo areas/local.conf 로 해소).
    regression._test_cmd 가 활성 슬롯의 이 값을 areas 위 레이어로 읽으므로, 빈입력에 기본값을 박으면
    areas per-repo test_cmd 를 잘못 덮는다 → 빈입력은 반드시 None(슬롯 미바인딩)이어야 한다.

    **성공 출력 다음 단계**: task 없이 만든 슬롯은 idle 이다. 출력은 그 슬롯을 쓰는 순서
    (`task new` → `alloc <repo> --task <task>`)와 세션 시작(`/pm-bootstrap --task <task>`, 사용자
    몫)을 안내한다. `--task` 로 만든 슬롯은 생성 직후 그 task 명의로 대여되고 반납 명령을 안내한다.

    **bare 준비**: 모든 검사 뒤 슬롯 생성 직전에 `.repos/<repo>.git` 이 없으면 등록 저장소
    hydrate(`cmd_repo_add` 를 이름만으로)로 areas `git` 주소에서 bare 를 만든다. 원본에 커밋이
    없거나 조회에 실패하면 그 rc 로 멈추고 bare 를 만들지 않는다. bare 경로가 있으나 무효면
    `create_slot` 의 BareRepoMissing 안내가 그대로 나간다(자동 삭제 없음).

    board/input_fn/is_tty/clone_runner 주입으로 hermetic 테스트(실 worktree add·
    라이브 input·실 clone 없이 배선·분기 검증). board 는 base 해소(`_repo_base`)·hydrate 재사용용(콘솔이
    로드한 board 전달). `clone_runner` 는 hydrate 의 `cmd_repo_add` 로 넘기는 파이썬 키워드다(CLI 아님).
    """
    if not _require_worktree_add_user_ack(args):
        return 1


    # test_cmd 해소: --test 명시면 그 값. 미지정 + tty 면 프롬프트(빌드명령). 비-tty 면 None.
    # 프롬프트 표시 기본값은 그 repo 의 실제 폴백(areas→local.conf→pytest-q)을 resolve 해
    # 보여준다(Enter 시 적용될 값 투명화) — 빈입력은 그래도 None 으로 슬롯을 비운다(must-fix 1).
    tty_check = is_tty if is_tty is not None else _stdin_is_tty
    test_cmd = getattr(args, "test", None)
    if test_cmd is None and tty_check():
        display_default = repo_protection._resolve_repo_test_cmd(pm_paths.home_at(REPO), args.repo)
        test_cmd = _prompt_test_cmd(input_fn, default=display_default)

    # base 해소 — areas.md 의 그 repo base 를 읽어 create_slot(base=) 로 전달한다.
    # 슬롯 브랜치 `<repo>_<N>` 가 그 base 에서 파생되고 base 가 그 브랜치의 부모로 기록된다.
    # base 가 없으면 부모를 적을 수 없으므로 만들지 않는다.
    # board 직접 import 금지 — 주입/로드된 board 의 `_repo_base` 만 쓴다.
    base = _resolve_repo_base(args.repo, board=board)
    if base is None:
        print(
            f"[중단] repo {args.repo!r} 등록에 base 브랜치가 없다 — 슬롯 브랜치의 부모를 적을 수 "
            "없어 슬롯을 만들지 않는다. areas.md 의 그 repo 행 `base` 칸을 채운 뒤 다시 실행하라.",
            file=sys.stderr,
        )
        return 1

    # readonly 공유 슬롯 — research 전용 read-only 기준면. detached HEAD·role="readonly"·
    # session 없음. 생성 자체는 사용자 승인 flow(디스크=코드 전체 사본).
    readonly = getattr(args, "readonly", False)

    # --task <task> (ⓓB) — 새 슬롯을 만들고 **생성 직후 그 슬롯을** task 명의로 대여한다
    # (풀 소진 시 alloc 이 안내하는 생성+대여 한 흐름·min-idle 재탐색 오슬롯 리스크 제거). readonly 와
    # 상호배타(readonly=무소유 공유 자산). task 명 검증 + 기바인딩은 alloc 과 동일 헬퍼로.
    folders_arg = getattr(args, "folders", None)
    folders = None if folders_arg is None else [item.strip() for item in folders_arg.split(",")]
    if readonly and folders is not None:
        print("[중단] `--readonly` 와 `--folders` 는 함께 쓸 수 없다.", file=sys.stderr)
        return 1
    task = getattr(args, "task", None)
    if task is not None:
        if readonly:
            print(
                "[중단] `--readonly` 와 `--task` 는 함께 쓸 수 없다 — readonly 공유 슬롯은 무소유"
                "(session 없음·배타 대여 없음)라 task 명의 대여 대상이 아니다.",
                file=sys.stderr,
            )
            return 1
        rc = _validate_prebound_task( task)
        if rc:
            return rc

    # bare 부재 = 등록 저장소 hydrate(areas `git` 주소로 clone). 커밋 0개 원본은 그 안에서 거부된다.
    if not pm_paths.bare_repo_path(pm_paths.home_at(REPO), args.repo).exists():
        hydrated = cmd_repo_add(
            argparse.Namespace(name=args.repo, git=None, test=None, base=None, owner=None,
                               user=None, protected=None),
            board=board, clone_runner=clone_runner)
        if hydrated:
            return hydrated
    try:
        lease = pm_engine.app.worktree_pool.create_slot(pm_paths.home_at(REPO), args.repo, base=base, test_cmd=test_cmd, readonly=readonly,
                               owner_task=task, folders=folders)
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 생성 실패: {exc}", file=sys.stderr)
        return 1
    slot_path = pm_paths.slot_path(pm_paths.home_at(REPO), lease.slot)
    test_line = f"\n  test_cmd 바인딩: {lease.test_cmd!r} (이 슬롯 회귀명령)" if lease.test_cmd else ""
    # 슬롯 번호 N(`lease.slot` = `work/<repo>_<N>`) 파싱. readonly 슬롯 refresh 안내에 쓴다.
    # 이미 보유한 lease 에서만 뽑고, 형식이 다르면 슬롯 식별자를 그대로 쓴다.
    slot_prefix = f"work/{lease.repo}_"
    slot_tail = lease.slot[len(slot_prefix):] if lease.slot.startswith(slot_prefix) else ""
    slot_num = slot_tail if slot_tail.isdigit() else lease.slot
    if readonly:
        # readonly 공유 슬롯 — 대여 대상 아님(무소유·공유 자산). 갱신은 refresh 로만.
        print(
            f"✓ readonly 공유 슬롯 생성: {lease.slot} (repo={lease.repo}·role=readonly) → {slot_path}\n"
            "  research 전용 read-only 기준면(detached HEAD·배타 대여 없음·session 없음). 코드를\n"
            "  *읽어* PM 홈 wiki(domain·architecture·status)를 쓰는 역할의 읽기 기준면이다.\n"
            f"  갱신은 `{_runtime_skill_entry('pm-worktree')} refresh "
            f"{lease.repo}_{slot_num}` 로만(fetch→detach 이동·dirty=거부). "
            "rebase/dev/sync 는 거부된다."
        )
    elif task is not None:
        # task-명의 생성(ⓓB) — 생성 직후 그 슬롯을 task 명의로 leased(별도 alloc 불요).
        print(
            f"✓ worktree 슬롯 생성 + task 대여: {lease.slot} (repo={lease.repo} · task={task!r}) "
            f"→ {slot_path}{test_line}\n"
            f"  생성 직후 이 슬롯을 task {task!r} 명의로 leased — 코드 작업은 이 슬롯 cwd 에서. "
            "보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
            f"  반납: `pm-config release {lease.slot} --task {task}` (작업완료 시 idle 반납)."
        )
    else:
        print(
            f"✓ worktree 슬롯 생성: {lease.slot} (repo={lease.repo}) → {slot_path}{test_line}\n"
            "  코드 작업은 이 슬롯 cwd 에서 — 보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
            f"  이 슬롯은 idle 이다. task 가 빌려 쓴다: `pm-config alloc {lease.repo} --task <task>` "
            "(task 가 없으면 먼저 `pm-config task new <task>`)."
        )
        print(
            f"  세션 시작은 사용자가 `{_runtime_skill_entry('pm-bootstrap')} --task <task>` 로 한다. "
            "세션이 이 안내를 읽고 스스로 실행하지 않는다."
        )
    try:
        actual_folders = sparse_checkout.slot_folders(pm_paths.home_at(REPO), lease.slot)
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print("  폴더: " + (", ".join(actual_folders) if actual_folders is not None else "(제한 없음)"))
    # 보호 브랜치 pre-push 훅 (재)설치 (멱등 자가치유) — 슬롯 op 마다 (재)설치해 엔진
    # update 후 기존 repo 도 다음 worktree add 에 훅을 얻는다(별도 명령 불요·회사 repo 무영향).
    # 설치 실패는 조용히 넘기지 않는다 — repo add·repo protected 와 **같은 공용 깔때기**
    # (훅 설치 False 침묵은 보호 가드 무력화를 감춘다).
    repo_protection._install_protected_hook_reporting(pm_paths.home_at(REPO),
        args.repo, action="(재)설치")
    # 사본 루트 ignore 규칙 (재)설치 (멱등 자가치유) — 훅과 같은 자리에서 슬롯 op
    # 마다 (재)설치해, 규칙 없이 만들어진 기존 풀도 다음 worktree add 에 규칙을 얻는다.
    _ensure_bare_ticket_copy_exclude(pm_paths.bare_repo_path(pm_paths.home_at(REPO), args.repo))
    # task-명의 생성이면 집합 변경 직후 재열거 — 지금 보유한 슬롯 집합을 surface.
    if task is not None:
        _render_task_slots( task)
    return 0


# 슬롯 git 요약(cmd_status cockpit)에서 head/base 커밋 sha 를 단축 표기하는 길이.
# git 관용 short-sha(7)보다 한 자리 넉넉히 잡아 멀티-repo 풀에서 충돌 여지를 줄인다.
_SLOT_SHA_SHORT = 8


def _short_sha(sha: "str | None") -> str:
    """커밋 sha 를 cockpit 표기용으로 단축한다 — None/빈값은 `?` (fail-soft 표시)."""
    if not sha:
        return "?"
    return sha[:_SLOT_SHA_SHORT]


def _slot_git_summary(status: dict, *, readonly: bool = False) -> str:
    """슬롯 git 상태 dict(`worktree_status.slot_git_status`)를 한 줄 요약으로 렌더한다.

    포맷 = `<branch>@<head 단축> (부모: <parent>@<갈라진 커밋 단축> · 앞 N · 뒤 M)`.
      - 부모 기록이 없으면 `(부모 기록 없음)`, 부모 브랜치가 지워졌으면 앞/뒤 대신 `부모 브랜치 없음`.
      - readonly 슬롯 = branch 축은 `(detached)`·부모는 따라갈 브랜치.
    """
    if readonly:
        branch_disp = "(detached)"
    else:
        branch_disp = status.get("branch") or "(detached/조회불가)"
    head_disp = _short_sha(status.get("head"))
    parent = status["parent"]
    if parent is None:
        return f"{branch_disp}@{head_disp} (부모 기록 없음)"
    named = f"부모: {parent['name']}@{_short_sha(parent['fork'])}"
    if parent["ref"] is None:
        return f"{branch_disp}@{head_disp} ({named} · 부모 브랜치 없음)"
    return f"{branch_disp}@{head_disp} ({named} · 앞 {parent['ahead']} · 뒤 {parent['behind']})"


# ── 기계 현황 계약 (`status --json`) ──────────────────────────────────────────
# 관리자(설치본)가 프로젝트 사본에서 현황을 읽는 **유일한 계약**이다. 새 명령을 만들지 않고
# 이미 읽기 전용인 `status` 에 기계 출력을 붙였다 — 사람 출력(텍스트)은 바이트 그대로다.
#
# 절은 비용으로 갈린다: 기본은 가벼운 절(슬롯·슬롯 없는 보존 브랜치·task·git·repo·하네스·설정)이고,
# `--with-board`(티켓·lint·게이트 기록)와 `--with-adapters`(어댑터 판정·diff·기능 표)가 무거운
# 절을 붙인다. 기본 호출은 board 를 로드하지 않는다(티켓 파일을 읽지 않는다).
STATUS_JSON_SCHEMA = 1


def _write_machine_line(text: str) -> None:
    """기계 판독 한 줄(JSON 페이로드)을 콘솔 코덱 전환과 무관하게 UTF-8 로 내보낸다.

    사람 출력(`print`)은 PowerShell 캡처에서 콘솔 codepage(cp949 등)로 강등될 수 있고 그 치환은
    되돌릴 수 없다 — 다른 프로세스가 파싱하는 출력은 공용 seam 으로 UTF-8 bytes 를 직접 쓴다
    (board `_write_machine_line` 동형).
    """
    console.write_machine_line(text)


class RemoteCheckError(RuntimeError):
    """`--with-remote` 의 원격 대조를 끝내지 못했다 — 반쪽 대조를 싣지 않고 조회를 멈춘다."""


def _git_ok(git_runner: GitRunner, argv: list, what: str) -> str:
    """git 한 번 — rc≠0 이면 무엇을 하다 실패했는지 말하며 멈춘다."""
    rc, out = git_runner(argv)
    if rc != 0:
        raise RemoteCheckError(f"{what} 실패 (rc={rc}): {out.strip()[:300]}")
    return out.strip()


_UPSTREAM_FORMAT = "%(upstream)%09%(upstream:short)%09%(upstream:remotename)%09%(upstream:remoteref)"


def _ahead_behind(git_runner: GitRunner, path: str, ref: str, what: str) -> tuple[int, int]:
    """`HEAD...ref` 의 (슬롯에만 있는 커밋 수, ref 에만 있는 커밋 수)."""
    counts = _git_ok(git_runner, ["-C", path, "rev-list", "--left-right", "--count", f"HEAD...{ref}"],
                     what)
    ahead, behind = (int(part) for part in counts.split())
    return ahead, behind


def _status_json_remote( leases, git_runner: GitRunner) -> dict[str, dict]:
    """슬롯별 원격 정보 — origin 주소 · 추적 원격 브랜치(이름·끝 커밋·앞/뒤).

    `upstream` 은 git 이 슬롯 브랜치에 연결해 둔 원격 브랜치(`@{u}`)다. 브랜치 없이 커밋에 멈춘 슬롯과
    원격 브랜치에 연결되지 않은 브랜치는 null 이고, 연결은 있는데 원격에서 지워진 브랜치는
    `head`·`ahead`·`behind` 가 null 이다.

    원격마다 `ls-remote` 1회로 슬롯들의 추적 원격 브랜치가 원격에 있는지 묻고, 있는 것만 fetch 1회로
    받는다(repo·원격 당 1회 — 슬롯들이 git 저장소를 공유한다). 받는 자리를 refspec 으로 명시하므로
    저장소의 fetch 설정과 무관하다. `ahead` = 슬롯에만 있는 커밋 수, `behind` = 원격 브랜치에만 있는
    커밋 수. 네트워크·인증·origin 부재는 `RemoteCheckError` 다(빈 값으로 접지 않는다).
    """
    slots: list[tuple] = []
    wanted: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}   # (repo, 원격) → (경로, 원격 ref → 받을 자리)
    for lease in leases:
        path = str(pm_paths.slot_path(pm_paths.home_at(REPO), lease.slot))
        rc, ref = git_runner(["-C", path, "symbolic-ref", "-q", "HEAD"])
        if rc not in (0, 1):                          # 1 = 브랜치 없이 커밋에 멈춘 슬롯
            raise RemoteCheckError(f"{lease.slot}: 현재 브랜치 조회 실패 (rc={rc}): {ref.strip()[:300]}")
        upstream = None
        if rc == 0:
            fields = _git_ok(git_runner, ["-C", path, "for-each-ref", f"--format={_UPSTREAM_FORMAT}",
                                          ref.strip()], f"{lease.slot}: 추적 원격 브랜치 조회").split("\t")
            if fields[0] and fields[2] != ".":        # "." = 원격이 아닌 로컬 브랜치에 연결
                upstream = dict(zip(("ref", "name", "remote", "remote_ref"), fields))
                wanted.setdefault((lease.repo, upstream["remote"]), (path, {}))[1][upstream["remote_ref"]] = upstream["ref"]
        slots.append((lease, path, upstream))

    present: dict[tuple[str, str], set[str]] = {}     # (repo, 원격) → 원격에 있는 추적 대상 ref
    for (repo, remote), (path, targets) in wanted.items():
        listed = _git_ok(git_runner, ["-C", path, "ls-remote", remote, *sorted(targets)],
                         f"{repo}: {remote} 원격 조회")
        advertised = {line.partition("\t")[2] for line in listed.splitlines()}
        present[(repo, remote)] = set(targets) & advertised
        if present[(repo, remote)]:
            _git_ok(git_runner, ["-C", path, "fetch", remote,
                                 *(f"+{source}:{targets[source]}" for source in sorted(present[(repo, remote)]))],
                    f"{repo}: git fetch {remote}")

    rows: dict[str, dict] = {}
    for lease, path, upstream in slots:
        tracked = None
        if upstream is not None:
            tracked = {"name": upstream["name"], "head": None, "ahead": None, "behind": None}
            if upstream["remote_ref"] in present[(lease.repo, upstream["remote"])]:
                ahead, behind = _ahead_behind(git_runner, path, upstream["ref"],
                                              f"{lease.slot}: 추적 원격 브랜치 대비 커밋 수")
                tracked.update(head=_git_ok(git_runner, ["-C", path, "rev-parse", upstream["ref"]],
                                            f"{lease.slot}: {upstream['ref']} 해소"),
                               ahead=ahead, behind=behind)
        rows[lease.slot] = {
            "url": _git_ok(git_runner, ["-C", path, "remote", "get-url", "origin"],
                           f"{lease.slot}: origin 주소"),
            "upstream": tracked,
        }
    return rows


def _status_json_slots( leases, task_names: set,
                       remote: dict[str, dict] | None = None) -> list[dict]:
    """슬롯 축 — 리스 장부 + 슬롯당 git 요약. 판정은 전부 `worktree_pool` 것을 그대로 싣는다.

    `remote` 는 `--with-remote` 로 부른 때만 슬롯마다 원격 대조(`_status_json_remote`)이고 그
    밖에는 null 이다(네트워크를 쓰는 절이라 가벼운 조회에 넣지 않는다).

    `alive` 는 `worktree_records.lease_is_open` 그대로다(판정 재구현 0) — 열림 기록(leased +
    session)이 있고 그에 대응하는 종료 기록(release/force_release/end_task 의 idle+session 비움)이
    없으면 참이다. 프로세스 조회는 0회다. `role` 은 장부에 키가 없는 행이 있어 `work`/`readonly` 두 값으로 정규화한다 —
    표시 축이 아니라 판정 축이라 빈 값을 남기지 않는다.
    """
    rows: list[dict] = []
    repo_reads: dict = {}   # 저장소 공용 git 값(부모 기록·stash·부모 비교 ref)은 조회 1회에 한 번만 읽는다.
    for lease in leases:
        git = worktree_status.slot_git_status(pm_paths.home_at(REPO), lease.slot, repo_reads=repo_reads)
        rows.append({
            "slot": lease.slot,
            "repo": lease.repo,
            "state": lease.state,
            "session": lease.session or None,
            "alive": bool(worktree_records.lease_is_open(lease)),
            "role": getattr(lease, "role", "work") or "work",
            "task": lease.session if (lease.session and lease.session in task_names) else None,
            "branch": git.get("branch"),
            "head": git.get("head"),
            "parent": git["parent"],
            "children": git["children"],
            "stashes": git["stashes"],
            "dirty": bool(git.get("dirty")),
            "folders": git.get("folders"),
            "remote": None if remote is None else remote[lease.slot],
        })
    return rows


def _status_json_tasks( tasks) -> list[dict]:
    """task 축 — 정체성 + 보유 슬롯(`slots_for_task` · session 축 귀속).

    `alive` = 장부 task 행의 열림 기록(`open`) 그대로다 — 프로세스 조회 0회.
    """
    return [{
        "name": task.name,
        "prefix": getattr(task, "prefix", None) or None,
        "alive": bool(task.open),
        "started": getattr(task, "started", "") or None,
        "slots": [lease.slot for lease in (pool_store.slots_for_task(pm_paths.home_at(REPO), task.name) or [])],
        # 열린 todo — 관리자 화면이 task 줄에 그대로 그린다(가벼운 절이라 5초 주기로 갱신된다).
        # task 별 파일(`.local/tasks/<task>/todo.json`)이 출처다.
        "todos": [dict(todo) for todo in task_store.read_task_todos(pm_paths.home_at(REPO), task.name)],
    } for task in tasks]


def _status_json_git(git_runner: GitRunner) -> dict:
    """PM 홈 checkout 의 git 요약 — branch·head·dirty.

    슬롯 축(`slots[].branch`)과 다른 축이다 — 이건 board·wiki·장부를 담은 **이 사본 자신**이고,
    회귀·라이브게이트 기록이 어느 HEAD 에 대한 판정인지 대조하는 기준이다. git checkout 이
    아니면 세 값 모두 None(그 사실이 곧 신호다).

    브랜치는 `worktree_git.current_branch` 와 **같은 primitive** 하나로 읽는다 —
    `symbolic-ref HEAD`(full ref·`--short` 없이) rc 0 이면 `refs/heads/` 접두만 정확히 벗겨
    브랜치명, rc≠0 이면 detached 라 None 이다. 두 축이 같은 상태를 같은 값(null)으로 말해야
    읽는 쪽이 판정을 다시 구현하지 않는다. `rev-parse --abbrev-ref HEAD` 를 쓰지 않는 이유는
    `current_branch` docstring 에 기록된 실측 사고 그대로다 — detached 를 문자열 `"HEAD"` 로,
    브랜치명과 동명의 태그가 있으면 `heads/<branch>` 으로, 커밋 0 인 브랜치는 rc≠0 으로 준다.

    커밋 0 인 저장소(`pm_import --new` 직후 fresh clone)는 정상 형상이다 — `symbolic-ref` 가
    `refs/heads/<branch>` 을 rc 0 으로 주므로 브랜치명은 있고 `head` 만 None 이다
    (`rev-parse HEAD` 가 rc≠0 · `pm_bootstrap._collect_git` 과 같은 규약).
    """
    rc, ref = git_runner(["-C", str(REPO), "symbolic-ref", "HEAD"])
    branch: str | None = None
    if rc == 0 and ref.strip().startswith(_SYMREF_BRANCH_PREFIX):
        branch = ref.strip()[len(_SYMREF_BRANCH_PREFIX):] or None
    head_rc, head_out = git_runner(["-C", str(REPO), "rev-parse", "HEAD"])
    dirty_rc, dirty_out = git_runner(["-C", str(REPO), "status", "--porcelain"])
    return {
        "branch": branch,
        "head": head_out.strip() if head_rc == 0 else None,
        "dirty": bool(dirty_out.strip()) if dirty_rc == 0 else None,
    }


def _status_json_repos() -> list[dict]:
    """등록 repo — areas.md 레지스트리 행(`repo list` 와 같은 칼럼·빈 셀은 null)."""
    return [
        {column: (row.get(column) or None) for column in _REPO_LIST_COLUMNS}
        for row in _areas_rows() if row.get("repo")
    ]


def _source_missing_required(source_root: Path, values: dict) -> list[str]:
    """**소스** 레지스트리 기준 필수 누락 — "갱신하면 필요" 축.

    비교 기준은 소스 checkout 의 레지스트리다(이 사본의 선언이 아니라 갱신하면 오게 될 선언).
    이 프로세스에는 이 사본의 `pm_engine` 이 이미 적재돼 있어 소스 패키지를 import 하면 이 사본
    선언을 읽게 된다. 그래서 소스의 진입 파일 `local_conf.py --keys --json` 을 하위 프로세스로
    돌려 그 키 표에서 `kind == "key"` 이고 `required` 가 참인 키를 선언 순서로 모으고 `values` 에
    없는 것을 돌려준다(`conf_schema.missing_required` 와 같은 판정). 하위 프로세스 실패·JSON 파싱
    실패·필드 부재는 그대로 올라간다(비교 못 하는 상태를 빈 목록으로 접지 않는다).

    **신뢰 경계**: 이 호출은 소스 트리의 Python 을 **실행**한다 — 채택자가
    `upstream.path` 로 등록한 checkout(URL 이면 이 실행이 clone/fetch 한 캐시) 한정이고,
    그 트리의 엔진은 `pm-config update` 가 이미 복사·실행하는 것과 같은 경계다.
    """
    entry = Path(source_root) / ".project_manager" / "tools" / "local_conf.py"
    result = subprocess.run(
        [sys.executable, str(entry), "--keys", "--json"],
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"소스 키 표를 읽지 못했다 ({entry} --keys --json · rc={result.returncode}): "
            f"{result.stderr.strip()}")
    document = json.loads(result.stdout)
    return [row["key"] for row in document["keys"]
            if row["kind"] == "key" and row["required"] and row["key"] not in values]


def _status_json_conf(pm_import_mod, source_root: Path | None) -> dict:
    """설정 완전성 — 필수 누락·폐지 키·미지 키·표준과 다른 값·최신 기준 누락 다섯 목록.

    판정은 `conf_schema` 레지스트리 하나가 낸다(값 해석 사본 0). 압축 한도(`ctx_budget`)는 기록과
    도구 설정 파일을 함께 읽는 `pm_import.ctx_budget_status` 가 낸다. `missing_for_latest` 는 소스
    checkout 이 해소된 호출(`--from` 또는 `--with-adapters`)에서만 채워지고 그 밖에는 null 이다.
    """
    result = conf_store.load(REPO / ".project_manager" / "local.conf")
    values = result.values
    return {
        "ctx_budget": pm_import_mod.ctx_budget_status(REPO, values),
        "missing_required": list(conf_schema.missing_required(values)),
        "legacy": [{"key": key, "replacement": replacement}
                   for key, replacement in sorted(result.legacy.items())],
        "unknown": list(result.unknown),
        "nonstandard": [{"key": key, "value": value, "default": default}
                        for key, (value, default) in sorted(conf_schema.nonstandard(values).items())],
        "missing_for_latest": (None if source_root is None
                               else _source_missing_required(source_root, values)),
    }


def _status_json_adapters(pm_import_mod, source_root: Path) -> dict:
    """어댑터 파일별 판정·diff 둘·기능 표 + 문서 파일(검사 없음) 한 줄.

    판정 어휘(`mode`·`status`)는 엔진 값 그대로다 — 표시 라벨은 읽는 쪽(관리자) 몫이라 여기서
    번역하지 않는다. 문서 파일(`CLAUDE.md`·`AGENTS.md`)은 채널이 없어 검사 대상이 아니고,
    그 사실 자체를 한 줄로 싣는다(목록에서 빠지면 "없는 파일" 과 구분되지 않는다).
    """
    files: list[dict] = [{
        "path": report.relpath,
        "harness": report.harness,
        "mode": report.mode,
        "status": report.status,
        "template_rev": report.template_rev,
        "diff_lines": {"added": report.added, "removed": report.removed},
        "diff_vs_baseline": report.diff_vs_baseline,
        "diff_vs_latest": report.diff_vs_latest,
        "features": [{"name": feature.name, "project": feature.project,
                      "latest": feature.latest, "verdict": feature.verdict}
                     for feature in report.features],
        "distance": report.distance,
    } for report in pm_import_mod.adapter_config_reports(REPO, source_root)]
    files.extend(
        {"path": target.relpath, "ownership": "instance", "status": "unchecked"}
        for target in pm_import_mod.instance_owned_template_targets(REPO, source_root)
        if target.mode == pm_import_mod.ADAPTER_CONFIG_NO_CHANNEL
    )
    files.sort(key=lambda row: row["path"])
    return {"source": str(Path(source_root).resolve()), "files": files}


def _status_json_document(args,  *, board_mod, pm_import_mod,
                          source_root: Path | None, git_runner: GitRunner) -> dict:
    """현황 JSON 한 벌 — 절 순서는 계약이 아니고 키가 계약이다(스키마 번호는 상수 하나)."""
    leases = pool_store.list_leases(pm_paths.home_at(REPO))
    tasks = pool_store.list_tasks(pm_paths.home_at(REPO)) or []
    task_names = {name for name in (getattr(task, "name", None) for task in tasks) if name}
    remote = (_status_json_remote( leases, git_runner)
              if getattr(args, "with_remote", False) else None)
    return {
        "schema": STATUS_JSON_SCHEMA,
        "engine_rev": ENGINE_REV,
        "pm_home": str(REPO.resolve()),
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "slots": _status_json_slots( leases, task_names, remote),
        "preserved_branches": branch_store.preserved_branches(pm_paths.home_at(REPO)),
        "tasks": _status_json_tasks( tasks),
        "git": _status_json_git(git_runner),
        "repos": _status_json_repos(),
        "harnesses": list(pm_import_mod.installed_harnesses(REPO)),
        "conf": _status_json_conf(pm_import_mod, source_root),
        "board": None if board_mod is None else board_mod.status_snapshot(),
        "adapters": (None if not getattr(args, "with_adapters", False)
                     else _status_json_adapters(pm_import_mod, source_root)),
    }


def _emit_status_json(args,  *, board=None, pm_import=None) -> int:
    """`status --json` — 문서를 **전부 만든 뒤** 한 번에 낸다(부분 산출 없음).

    소스가 필요한 절(`--with-adapters`·`--from`)에서 소스를 못 잡으면 stdout 에 아무것도 쓰지
    않고 rc≠0 이다 — 반쪽 JSON 을 파싱한 관리자가 "그 절이 비어 있다" 를 정상으로 읽는 것이
    이 계약이 막으려는 상태다(텍스트 경로 `sync-adapter-config` 의 `[중단]` + rc 1 과 같은 판정).
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print("[중단] pm_import.py 엔진을 찾을 수 없다 — 하네스·어댑터 조회 불가 "
              f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).", file=sys.stderr)
        return 1
    board_mod = None
    if getattr(args, "with_board", False):
        board_mod = board or _load_module("board", "board.py")
        if board_mod is None:
            print("[중단] board.py 엔진을 찾을 수 없다 — 보드 현황 조회 불가 "
                  f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).", file=sys.stderr)
            return 1
    source_root: Path | None = None
    if getattr(args, "with_adapters", False) or getattr(args, "source", None):
        try:
            source_root = pm_import_mod.resolve_adapter_config_source(
                REPO, getattr(args, "source", None))
        except FileNotFoundError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        except pm_import_mod.UpstreamCacheError as exc:
            # URL upstream 캐시 clone/fetch 실패 — 옛 캐시로 진행하지 않고 조회를 멈춘다.
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
    try:
        document = _status_json_document(
            args,  board_mod=board_mod, pm_import_mod=pm_import_mod,
            source_root=source_root,
            git_runner=git._real_clone_runner())
    except (FileNotFoundError, pm_import_mod.AdapterConfigChannelUnavailable) as exc:
        # 판정 기준이 없는 상태 둘만 rc 로 번역한다 — 소스에 대조할 파일이 없거나(잘못된 `--from`)
        # 어댑터 판정 채널이 unavailable 인 경우. 그 밖의 실패는 삼키지 않는다(정상 산출로 위장 금지).
        print(f"[중단] 현황 JSON 조회 실패: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except RemoteCheckError as exc:
        print(f"[중단] 원격 대조 실패 — {exc}", file=sys.stderr)
        return 1
    _write_machine_line(json.dumps(document, ensure_ascii=False))
    return 0


def _status_json_usage_error(args) -> str | None:
    """`--json` 없이 준 범위 플래그 — usage error 문구(없으면 None).

    조용히 무시하면 관리자가 "무거운 절을 받았다" 고 오독한다. 텍스트 경로는 이 플래그들의
    소비 지점이 아니므로 받는 척하지 않는다.
    """
    given = [flag for flag, attribute in (
        ("--with-board", "with_board"), ("--with-adapters", "with_adapters"),
        ("--with-remote", "with_remote"), ("--from", "source")) if getattr(args, attribute, None)]
    if not given or getattr(args, "json", False):
        return None
    return (f"[중단] {' · '.join(given)} 은 `--json` 과 함께만 쓴다 — 텍스트 출력에는 그 절이 "
            "없다. `pm-config status --json " + " ".join(given) + "` 로 다시 실행하라.")


def cmd_status(
    args: argparse.Namespace,
    *,

    board=None,
    pm_import=None,
) -> int:
    """`status | whoami` — 2축 cockpit(task 상황 + slot 풀·슬롯당 git 요약).

    **2축 cockpit** — 다슬롯 관리 부담을 한눈에 보이게 두 축으로 나눈다:
      - **task 축** — 사람이 명명한 task 별 {보유 작업공간(`work/<repo>_<N>` 목록)·prefix}
        (`list_tasks`+`slots_for_task`). slot 축과 깔끔히 분리된다.
      - **slot 풀 축** — 슬롯별 {state·보유 task·role} + **슬롯당 git 요약**
        (`<branch>@<head> (base: <b>@<sha> · N behind)`·`slot_git_status`). behind 는 base 기록
        (`git.base`)이 있을 때만·미기록은 `-`+이유(자동 추론 금지).

    브랜치는 `worktree_git.current_branch(slot)` 로 슬롯 worktree 의 git HEAD 에서 **live**
    조회한다(git=진실). 사용자가 슬롯서 직접 `git
    checkout` 해도 즉시 반영·드리프트 없음. detached/조회불가는 "(detached/조회불가)".

    status·whoami 는 같은 데이터·같은 핸들러 — whoami 는 이 세션 줄을 머리에 둔다. 데이터는
    전부 장부(기계)에서 파생([[living-truth-docs-anti-ossification]]) — 신설 저장소 없음. 전
    surface(task/slot/git)는 일반 조회 실패에 fail-soft 이되 marked engine skew 는 막는다.

    `status --json` 은 같은 수집을 **기계 계약**으로 낸다(`_emit_status_json` — 관리자 읽기
    표면). 텍스트 출력은 그 플래그가 없을 때 종전 바이트 그대로다. `--json` 은 `status` 전용이라
    `whoami` 는 텍스트 별칭으로 남는다(계약 하나).

    worktree_pool 주입으로 hermetic 테스트(board·pm_import 도 같은 관례).
    """
    usage_error = _status_json_usage_error(args)
    if usage_error is not None:
        print(usage_error, file=sys.stderr)
        return 2                              # 인자 오용 — argparse 와 같은 종료코드.

    if getattr(args, "json", False):
        return _emit_status_json(args,  board=board, pm_import=pm_import)
    leases = pool_store.list_leases(pm_paths.home_at(REPO))
    sess = _default_session()
    mine = [l for l in leases if l.state == "leased" and l.session == sess]

    # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회한다
    # git=진실·장부 저장 폐지). 사용자가 슬롯서 직접 `git checkout` 해도 즉시 반영.
    # detached/조회불가는 None → "(detached/조회불가)" 로 surface.
    def _live_branch(slot: str) -> str:
        return worktree_git.current_branch(pm_paths.home_at(REPO), slot) or "(detached/조회불가)"

    print(f"# pm-config {args.command} — 세션: {sess or '(비바인딩)'}")

    # 정체성·세션격리 posture surface: resolved user + isolation
    # 상태(strict/degrade/single-user) + remedy. board.py 를 import 하지 않고
    # user 는 `_default_user`(자체 해소·`_local_conf_user` 동형), 다중사용자 여부는 areas.md
    # `area_owner` 자체 파싱(`_distinct_area_owners`)으로 판정한다 — board 의 격리 *판정*은 복제
    # 하지 않고 최소 신호만.
    #
    # **정직화(should-fix·오안심 방지)**: 여기 posture 는 areas.md `area_owner`(registry) *coarse*
    # 신호다. board `list --mine` 의 실 strict-exclude 는 티켓 귀속(created_by/claimed_by·
    # `_distinct_ticket_users`)으로 판정하며 — 부분마이그레이션 보드(티켓은 2명·area_owner 미채움/
    # 1개)에서 두 신호가 갈린다(그게 degrade-risk 케이스). 그래서 무조건 "정상"
    # 단언을 금하고, 신호 출처·한계를 문구에 노출하고 authoritative 신호로 `board list --mine`
    # (strict-exclude loud-warn)을 가리킨다([[robustness-value-connections-before-ship]] silent-degrade
    # 근절 취지). board 티켓 스캔 복제는 하지 않는다.
    resolved_user = _default_user()
    multi_user = _distinct_area_owners() > 1
    _remedy = "`board init --owner <you>` 또는 `board migrate-identity --task <task>`"
    _authoritative = "실 격리는 `board list --mine`(strict-exclude loud-warn)이 authoritative"
    print(f"## 정체성(user): {resolved_user or '(미해소 — local.conf identity.user= / git config user.email 미설정)'}")
    if not multi_user:
        print("## 세션격리(registry/area_owner 기준): single-user (단일/미등록 registry) — 단, 티켓 귀속"
              "(created_by/claimed_by)이 다중이면 세션 뷰가 strict-exclude 될 수 있다. "
              f"{_authoritative}.")
    elif resolved_user is not None:
        print("## 세션격리(registry/area_owner 기준): strict (다중사용자·정체성 해소 — 세션 뷰 = "
              f"내 소유 open + claim). {_authoritative}.")
    else:
        print("## 세션격리(registry/area_owner 기준): ⚠ degrade-risk (다중사용자·정체성 미해소 — "
              f"소유 미해소 open 이 `board list --mine` 에서 strict-exclude). remedy: {_remedy}")

    if mine:
        print("## 이 세션의 리스:")
        for l in mine:
            print(f"  - {l.slot} (repo={l.repo} · branch={_live_branch(l.slot)})")
    else:
        print("## 이 세션의 리스: (없음)")

    # task 축 — 사람이 명명한 task 별 {보유 작업공간·prefix}. list_tasks
    # + slots_for_task(session==task 명의 leased 슬롯). task 명 집합은 slot 풀 축의 "보유 task"
    # 귀속에도 쓴다. 형제 사본은 rev 검증을 통과한 것만 로드되므로(`_load_module`) 이 함수들이
    # 없는 사본은 여기 오지 못한다 — 있는지 더듬지 않고 그대로 부른다.
    tasks = []
    try:
        tasks = pool_store.list_tasks(pm_paths.home_at(REPO)) or []
    except Exception as exc:  # noqa: BLE001 — task 조회 실패가 status 를 막지 않는다(조회 전용·fail-soft).
        tasks = []
    # truthy name 만 — 레코드가 name 미보유(None/빈값)면 집합에 넣지 않는다(방어). None 이 섞이면
    # idle lease(session=None/"")가 `보유 task=None` 으로 오귀인될 수 있다(reviewer suggestion).
    task_names = {n for n in (getattr(t, "name", None) for t in tasks) if n}
    print("## task 상황 (사람이 명명한 작업스트림):")
    if not tasks:
        print("  (task 없음 — 명명한 task 미등록·세션은 아래 풀 축에만 나타남)")
    for t in tasks:
        slots = []
        try:
            slots = pool_store.slots_for_task(pm_paths.home_at(REPO), t.name) or []
        except Exception as exc:  # noqa: BLE001 — 슬롯 귀속 실패는 fail-soft(작업공간 미표시).
            slots = []
        ws = ", ".join(s.slot for s in slots) or "(보유 작업공간 없음)"
        prefix = getattr(t, "prefix", None) or "없음(기본)"
        print(f"  - {t.name} · prefix={prefix} · 작업공간: {ws}")

    # 슬롯 git 요약 헬퍼 — slot_git_status를 한 줄로.
    # 조회 실패는 fail-soft(요약 줄 생략) — 리스 장부 축은 그대로 surface.
    # 저장소 공용 git 값(부모 기록·stash·부모 비교 ref)은 조회 1회에 한 번만 읽는다.
    repo_reads: dict = {}

    def _slot_git_line(slot: str, *, readonly: bool) -> "str | None":
        try:
            status = worktree_status.slot_git_status(pm_paths.home_at(REPO), slot, repo_reads=repo_reads)
        except worktree_records.SlotFoldersRefused:
            raise
        except Exception as exc:  # noqa: BLE001 — 일반 git 조회 실패만 요약 생략.
            return None
        if not isinstance(status, dict):
            return None
        summary = _slot_git_summary(status, readonly=readonly)
        folders = status.get("folders")
        return summary + (" · 폴더 " + ", ".join(folders) if folders is not None else "")

    print("## slot 풀 (풀 전체 리스 장부 · 슬롯별 state·보유 task·role + git 요약):")
    if not leases:
        print("  (리스 없음 — 아직 worktree 슬롯이 생성되지 않음)")
    for l in leases:
        # role(work/readonly) — readonly 공유 슬롯은 무소유(session 없음)·detached.
        role = getattr(l, "role", "work")
        role_str = f" · role={role}" if role != "work" else ""
        print(
            f"  - {l.slot} · repo={l.repo} · branch={_live_branch(l.slot)} · "
            f"state={l.state} · session={l.session or '-'} · "
            f"{'열림' if worktree_records.lease_is_open(l) else '닫힘'}{role_str}"
        )
        # 보유 task 귀속(장부 축) — lease.session 이 task 명이면 그 task 가 이 슬롯을 보유(session
        # 축). idle 슬롯(session 이 task 명 아님/빈값)은 `-`. **장부 유래라 항상 출력** —
        # slot 풀 축 명세({state·보유 task·role})는 git 요약과 분리다. git 요약만
        # fail-soft: 조회 실패/API 부재면 그 부분만 `(조회 불가)` 로 대체하고 task 귀속은 잃지 않는다.
        task_disp = l.session if (l.session and l.session in task_names) else "-"
        try:
            git_line = _slot_git_line(l.slot, readonly=role == "readonly")
        except worktree_records.SlotFoldersRefused as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
        git_part = git_line if git_line is not None else "(조회 불가)"
        print(f"      ↳ 보유 task={task_disp} · git: {git_part}")

    # 슬롯 없는 보존 브랜치 — 슬롯 제거의 삭제 판정이 남긴 브랜치(공용 git 설정 기록). 있을 때만 절을 낸다.
    preserved = branch_store.preserved_branches(pm_paths.home_at(REPO))
    if preserved:
        print("## 슬롯 없는 보존 브랜치 (슬롯 제거 때 삭제 판정이 남긴 브랜치 · 정리는 사용자):")
    for row in preserved:
        parent_part = f"부모 {row['parent']}" if row["parent"] is not None else "부모 기록 없음"
        unmerged_part = ("합쳐짐 판정 불가" if row["unmerged"] is None
                         else f"합쳐지지 않은 커밋 {row['unmerged']}개")
        print(f"  - {row['repo']} · {row['name']} · 쓰던 슬롯 {row['slot']} · "
              f"보존 {row['preserved_at']} · 사유 {row['reason']} · {parent_part} · {unmerged_part}")

    # git worktree × 장부 정합(reconcile) — 실 git worktree 와 장부를 대조해 drift 를
    # surface 한다. **조회 전용·부작용 0**: 삭제/prune/이동 안 함 — 판정·복구 안내만(자동삭제는
    # 사용자 위임·파일 삭제 원칙). reconcile 실패는 status 를 막지 않는다(fail-soft·advisory).
    #   - orphan = git worktree 존재·장부 미등록(중단된 create/수동 add 잔존·audit #2·다음 create
    #     번호 충돌·audit #4 의 근원). status 가 이걸 못 보던 게 audit #3.
    #   - stale = 장부 등록·worktree 없음(dir 삭제/prune).
    #   - incomplete = provisional("creating") — worktree add 후 확정 전 중단된 create 흔적.
    try:
        recon = pm_engine.app.worktree_pool.reconcile_worktrees(pm_paths.home_at(REPO))
    except Exception as exc:  # noqa: BLE001 — reconcile 실패가 status 를 막지 않는다(조회 전용).
        recon = None
    if recon is not None and (recon.orphans or recon.stale or recon.incomplete):
        print("## ⚠ worktree × 장부 drift (조회 전용 — 자동삭제 안 함·정리는 사용자):")
        for w in recon.orphans:
            print(
                f"  - [orphan] {w.slot} · git worktree 존재·장부 미등록 "
                "(중단된 create/수동 add 잔존) — 다음 create 번호 충돌·status blind 원인"
            )
        for l in recon.stale:
            print(
                f"  - [stale] {l.slot} · 장부 등록(state={l.state})·git worktree 없음 "
                "(dir 삭제/prune) — 장부 잔여"
            )
        for l in recon.incomplete:
            print(
                f"  - [incomplete] {l.slot} · 중단된 생성(state=creating·provisional) "
                "— worktree add 후 확정 전 중단(SIGKILL 등) 가능성"
            )
        print(
            "  복구:\n"
            "    - orphan worktree(git 측·disk 에 존재·작업 있을 수 있음) → 확인 후 "
            "`git -C .repos/<repo>.git worktree remove <경로>` 로 사용자가 삭제(위임 원칙).\n"
            "    - stale/incomplete 중 worktree dir 이 사라진 dangling 장부 엔트리 → "
            "`pm-config worktree prune-stale`(안전·이미 없는 worktree 의 기록만 정리).\n"
            "    - incomplete 인데 worktree 가 남아있으면 위 `git worktree remove` 후 prune-stale."
        )
    return 0


def cmd_worktree_prune_stale(
    args: argparse.Namespace) -> int:
    """`worktree prune-stale` — worktree dir 이 사라진 dangling 장부 엔트리를 안전 정리.

    status reconcile 이 surface 한 stale/incomplete 중 **worktree 가 물리적으로 부재**한 장부
    엔트리를 제거한다(worktree_pool.prune_stale_leases). **안전**: 지울 worktree 파일이 없는
    dangling 기록만 정리 — 사용자 데이터/worktree 삭제가 아니라 삭제-위임 원칙 위반이 아니다.
    orphan *worktree*(git 측·disk 존재·작업 가능)는 손대지 않는다(그건 `git worktree remove` 로
    사용자가). reconcile(status)이 조회-전용인 것과 분리된 **명시 user-invoked 정리 진입점**이다.

    worktree_pool 주입으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """

    pruned = pm_engine.app.worktree_pool.prune_stale_leases(pm_paths.home_at(REPO))
    if pruned:
        print(
            f"✓ dangling 장부 엔트리 {len(pruned)}개 정리 (worktree 부재·이미 사라진 기록): "
            f"{', '.join(pruned)}"
        )
    else:
        print("정리할 dangling 엔트리 없음 (worktree 부재 장부 엔트리 0).")
    return 0


def _require_worktree_remove_user_ack(args: argparse.Namespace) -> bool:
    """슬롯 제거에 슬롯 값-결속 사용자 승인 토큰을 요구한다(`_require_worktree_add_user_ack` 와 같은 모양).

    `--user-ack` 는 반복 가능하다. 입력한 slot 인자가 값 중에 그대로 있어야 엔진을 부르고, 값 전부가
    합쳐지지 않은 브랜치 삭제 승인값으로 엔진에 넘어간다(`remove_slot(acked_branches=)`). 토큰은
    자가-증언형 감사/마찰 장치이며 적대적 우회까지 막는 보안 경계는 아니다.
    """
    slot = args.slot
    user_acks = list(getattr(args, "user_ack", None) or [])
    if slot in user_acks:
        print(f"[승인 감사] worktree remove: 사용자 승인 토큰이 슬롯 {slot!r}에 값-결속됨.")
        return True
    mismatch = (
        f" 제공된 값 {user_acks!r}에 슬롯 {slot!r}이 없다."
        if user_acks
        else ""
    )
    print(
        f"[중단] worktree remove {slot!r}: 슬롯 제거에는 사용자 명시 승인이 필요하다.{mismatch}\n"
        f"  1순위: 사용자에게 슬롯 {slot!r}의 제거 승인을 요청하라.\n"
        f"  부차 수단: 승인한 사용자만 `--user-ack {slot}`를 대상값 그대로 붙여 실행하라"
        "(세션 자동 부착 금지). 합쳐지지 않은 브랜치도 지우려면 그 브랜치 이름을 "
        "`--user-ack <브랜치>` 로 함께 붙인다.",
        file=sys.stderr,
    )
    return False


# 슬롯 제거 출력에 싣는 합쳐지지 않은 커밋 줄 상한 — 넘으면 "외 N개" 로 접는다.
_UNMERGED_COMMITS_SHOWN = 10


def _print_branch_deletion(deletion) -> None:
    """`BranchDeletion` 을 출력한다 — 행동·사유 한 줄 + 근거(합쳐지지 않은 커밋 목록)."""
    print(f"  {deletion.summary()}")
    commits = deletion.unmerged_commits
    for line in commits[:_UNMERGED_COMMITS_SHOWN]:
        print(f"    {line}")
    if len(commits) > _UNMERGED_COMMITS_SHOWN:
        print(f"    … 외 {len(commits) - _UNMERGED_COMMITS_SHOWN}개")


def cmd_worktree_remove(
    args: argparse.Namespace) -> int:
    """`worktree remove <slot> --user-ack <slot> [--user-ack <브랜치> ...] [--force]` — 슬롯 통째 제거.

    pm_engine.app.worktree_pool.remove_slot(slot, force=, acked_branches=) 로 리스 확인 → 등록 조회 → 커밋 안 한
    변경 검사 → `git worktree remove` → 브랜치 삭제 판정 → 장부 엔트리 제거를 한 번에 한다.
    footgun 체인(수동 remove → dangling 장부 → `add` 가 번호 skip → 뒤늦은 prune)을 원천
    종결한다 — 장부 엔트리 제거로 `add` 가 빈 번호를 재사용한다.

    **사용자 승인값 필수** — `--user-ack` 값에 입력한 슬롯이 없으면 엔진을 부르지 않고 rc1
    (`_require_worktree_remove_user_ack`). `prune-stale`(worktree 부재 장부만 정리)과 달리 **실
    worktree 를 지운다**. orphan worktree(장부 미등록)는 여전히 `git worktree remove` 로 사용자가.

    - 활성 리스(leased·사용 중) → 거부. `--force` 가 넘는 것은 이 점유 하나뿐이다.
    - 커밋 안 한 변경·git 미등록 트리 → `--force` 와 무관하게 거부(RemoveRefused·rc1). 엔진은
      stash 하지 않고 미등록 트리를 지우지 않는다.
    - 장부에 슬롯 없으면 무해 종료(rc0·이미 정리됨).
    - 브랜치: `BranchDeletion` 을 그대로 출력한다(삭제/보존 사유·자식·합쳐지지 않은 커밋·origin).
      합쳐지지 않은 브랜치는 그 브랜치 이름이 `--user-ack` 값에 있을 때만 지운다.
    - 엔진 RuntimeError(worktree remove 실패·브랜치 조회 실패) → rc1. 그 실패가 무엇을 남겼는지는
      엔진이 등록을 다시 물어 판정하므로, 여기서 재시도 방법을 지어내지 않는다.

    worktree_pool 주입으로 hermetic 테스트(실 worktree remove·장부 쓰기 없이 배선 검증).
    """
    if not _require_worktree_remove_user_ack(args):
        return 1


    try:
        result = pm_engine.app.worktree_pool.remove_slot(pm_paths.home_at(REPO), args.slot, force=args.force,
                                acked_branches=frozenset(args.user_ack))
    except worktree_records.RemoveRefused as exc:
        if exc.reason == "active-lease":
            print(
                f"[중단] 슬롯 {args.slot!r} 이 활성(사용 중·state={exc.state}) — 제거 거부. "
                "먼저 `release` 로 반납하거나 `worktree remove --force`(점유 무시).",
                file=sys.stderr,
            )
        elif exc.reason == "dirty":
            print(
                f"[중단] 슬롯 {args.slot!r} 에 커밋 안 한 변경이 있다 — 제거 거부(엔진은 stash 하지 "
                "않는다). 사용자에게 물어 커밋하거나 사용자가 요청하면 stash 한 뒤 다시 실행하라. "
                "`--force` 는 이 거부를 넘지 않는다.",
                file=sys.stderr,
            )
        else:
            print(
                f"[중단] 슬롯 {args.slot!r} 의 트리 {exc.path} 는 git 이 worktree 로 등록하지 않았다 — "
                "커밋 안 한 변경을 물을 수 없어 엔진이 지우지 않는다. 사용자가 그 경로를 직접 확인·"
                "삭제한 뒤 `pm-config worktree prune-stale` 로 장부를 정리하라.",
                file=sys.stderr,
            )
        return 1
    except RuntimeError as exc:
        print(f"[중단] worktree 슬롯 제거 실패: {exc}", file=sys.stderr)
        return 1

    if result is None:
        print(
            f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해). "
            "orphan worktree(git 측·장부 미등록)는 `git worktree remove` 로 사용자가."
        )
        return 0

    # ⚠ 활성 리스 강제 회수 경고 — `--force` 로 leased/creating(사용 중) 슬롯을 override 했을 때
    # (reviewer should-fix·티켓 결정 §활성 리스 문구 정합). 다른 세션이 쓰던 슬롯일 수 있음을 stderr 로.
    if getattr(result, "forced_state", None):
        print(
            f"⚠ 활성(state={result.forced_state}) 슬롯 {args.slot!r} 을 강제 회수 "
            "(--force override — 다른 세션이 사용 중이었을 수 있다).",
            file=sys.stderr,
        )

    print(f"✓ 슬롯 {args.slot!r} 제거 — worktree remove + 장부 엔트리 삭제(빈 번호 재사용 가능).")
    # git 이 등록만 지우고 트리를 남긴 형상(읽기 전용 잔재 등) — 엔진이 마저 지웠음을 고지한다.
    if result.tree_cleaned:
        print("  git 이 등록만 지우고 남긴 디렉터리 잔재를 엔진이 마저 정리했다.")
    deletion = result.branch_deletion
    if deletion is None:
        return 0
    _print_branch_deletion(deletion)
    # 보존 브랜치 캐비앗 — 같은 번호 base-경로 재생성이 막힌다(SlotBranchExists 선-검출).
    if deletion.action == worktree_records.BRANCH_PRESERVED:
        print(
            f"  ⚠ 브랜치 {deletion.branch} 보존됨 — 같은 번호 슬롯 재생성은 브랜치 잔존을"
            " 선-검출해 명확히 멈춘다(SlotBranchExists). 그 브랜치 작업을 이어가려면 다른 슬롯에서"
            f" `worktree_pool.py switch <슬롯> {deletion.branch}`(리셋 없음)."
        )
    return 0


def cmd_set_test_cmd(
    slot: str,
    cmd: str | None) -> int:
    """슬롯 빌드/테스트 명령 설정·변경 — 콘솔 `[b]`·"나중에 변경".

    pm_engine.app.worktree_pool.set_test_cmd(slot, cmd) 로 기존 슬롯 리스의 test_cmd 를 갱신한다(flock +
    atomic write·worktree_pool 책임). 별도 CLI 서브커맨드는 없다 — 콘솔 `[b]` 와 worktree
    add 프롬프트가 변경 경로를 흡수(결정 §setter 단순화). 장부에 슬롯이 없으면(`KeyError`)
    명시 에러 rc 1(침묵 무력화 금지). `cmd=None`/빈 문자열이면 바인딩 해제(폴백·현행).

    worktree_pool 주입으로 hermetic 테스트(실 장부 쓰기 없이 배선 검증).
    """

    normalized = (cmd.strip() or None) if cmd else None
    try:
        lease = pm_engine.app.worktree_pool.set_test_cmd(pm_paths.home_at(REPO), slot, normalized)
    except KeyError:
        print(
            f"[중단] 슬롯 {slot!r} 에 대한 리스가 없다 — 새 슬롯이 필요하면 먼저 사용자에게 "
            "생성 승인을 요청하라. 승인한 사용자만 대상 repo와 같은 `--user-ack <repo>`로 "
            "`worktree add`를 직접 실행한다(세션 자동 실행 금지).",
            file=sys.stderr,
        )
        return 1
    if lease.test_cmd:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 설정: {lease.test_cmd!r} (이 슬롯 회귀명령).")
    else:
        print(f"✓ 슬롯 {slot} 빌드/테스트 명령 해제 — repo areas/local.conf 로 폴백(현행).")
    return 0


def cmd_release(
    args: argparse.Namespace) -> int:
    """`release <slot> [--task <task>] [--force]` — 작업완료 반납 / 수동 강제 백스톱.

    - 기본: pm_engine.app.worktree_pool.release(slot) — 커밋 안 한 변경이 있으면 ReleaseRefused(엔진은 stash 하지 않는다).
    - `--task <task>`: 소유검사 — 이 슬롯이 그 task 명의(lease.session)가 아니면 `NotTaskOwner`
      로 거부(다른 task/세션 슬롯을 실수로 idle 화 방지). dirty 판정보다 먼저. `--task` 미지정은
      현행 slot-only 반납(백스톱).
    - `--force`: pm_engine.app.worktree_pool.force_release(slot) — 점유(leased)·소유 **무시 강제** idle 화.
      커밋 안 한 변경은 force 여도 ReleaseRefused. 장부에 슬롯 없으면 무해 종료. force 는 순수
      백스톱이라 `--task` 소유검사도 건너뛴다(override 의도).

    부트스트랩·핸드오프는 슬롯을 반납하지 않는다 — 반납은 이 명령이 한다. worktree_pool 주입으로
    hermetic 테스트.
    """


    if args.force:
        try:
            lease = pm_engine.app.worktree_pool.force_release(pm_paths.home_at(REPO), args.slot)
        except pool_store.ReadonlySlotNotLeasable as exc:
            print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — 강제 반납도 대상 아님.
            return 1
        except worktree_records.ReleaseRefused:
            print(
                f"[중단] 슬롯 {args.slot!r} 에 커밋 안 한 변경이 있다 — 강제 반납도 거부(엔진은 stash "
                "하지 않는다). 사용자에게 물어 커밋하거나 사용자가 요청하면 stash 한 뒤 다시 실행하라.",
                file=sys.stderr,
            )
            return 1
        if lease is None:
            print(f"✓ 슬롯 {args.slot!r} 장부에 없음 — 이미 정리됨(무해).")
        else:
            print(f"✓ 슬롯 {args.slot!r} 강제 반납(idle 화).")
        return 0

    owner_task = getattr(args, "task", None)
    try:
        pm_engine.app.worktree_pool.release(pm_paths.home_at(REPO), args.slot, owner_task=owner_task)
    except KeyError:
        print(f"[중단] 슬롯 {args.slot!r} 에 대한 리스가 없다.", file=sys.stderr)
        return 1
    except pool_store.ReadonlySlotNotLeasable as exc:
        print(f"[중단] {exc}", file=sys.stderr)   # readonly 공유 슬롯 — 반납 대상 아님.
        return 1
    except worktree_records.NotTaskOwner as exc:
        held = f"session {exc.holder!r} 보유 중" if exc.holder else "미점유(idle)"
        print(
            f"[중단] 슬롯 {args.slot!r} 은 task {owner_task!r} 소유가 아니다 — 반납 거부({held}). "
            "내 task 가 대여한 슬롯만 `--task` 로 반납할 수 있다(다른 task 슬롯 보호).",
            file=sys.stderr,
        )
        return 1
    except worktree_records.ReleaseRefused:
        print(
            f"[중단] 슬롯 {args.slot!r} 에 커밋 안 한 변경이 있다 — 반납 거부(작업 유실 방지·엔진은 "
            "stash 하지 않는다). 사용자에게 물어 커밋하거나 사용자가 요청하면 stash 한 뒤 다시 "
            "실행하라. `release --force` 는 점유·소유검사만 넘는다.",
            file=sys.stderr,
        )
        return 1
    task_line = f" (task {owner_task!r} 소유검사 통과)" if owner_task else ""
    print(f"✓ 슬롯 {args.slot!r} 작업완료 반납(idle 화){task_line} — 풀에 재사용 컨테이너로 반환.")
    # task-명의 반납이면 집합 변경 직후 재열거 — 남은 보유 슬롯 집합을 surface.
    # slot-only 백스톱(owner_task=None)은 대상 task 가 없어 재열거 없음.
    if owner_task:
        _render_task_slots( owner_task)
    return 0


def _registered_repos() -> "set[str] | None":
    """등록 repo 이름 — task 명 예약패턴(`<repo>_<N>`) 판별 근거. areas.md 에서 fail-soft 해소.

    areas.md 읽기 실패를 `None` 으로 내린다. `None` 은
    "등록 목록을 못 읽었다" 는 뜻이고, 그 상태에서도 `_validate_task_name` 의 traversal·절대경로·
    빈 이름 검증은 그대로 강제된다 — 완화되는 것은 예약패턴 하나다. 해소 규칙이 이 한 자리라
    `task new`(생성)와 `alloc`/`worktree add --task`(기바인딩 요구)가 같은 근거로 판정한다.
    """
    try:
        return areas_store.registered_repos(pm_paths.home_at(REPO))
    except (OSError, UnicodeError):
        # areas.md 를 못 읽은 경우다 — None(예약패턴 검증만 완화·traversal 검증은 유지).
        return None


def _validate_prebound_task(task: str) -> int:
    """task 명 검증(예약패턴·traversal) + 기바인딩 요구 — `alloc`/`worktree add --task` 공용.

    task 이름이 lease session 으로 **기록**되는 write-capable 경로의 선검증이자,
    정체성 *생성*(`task new`·bootstrap)이 슬롯 부착보다 먼저라는 순서를 강제한다. 통과면 0,
    실패면 1(진단은 이 함수가 stderr 로 출력). 예약패턴 판별 근거는 `_registered_repos` 하나가
    해소한다(부재면 None → traversal 검증만·예약패턴만 완화).
    """
    registered = _registered_repos()
    try:
        task_rules._validate_task_name(task, registered)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {task!r} — {exc.reason}. `--task` 는 안전한 단일 이름이어야 "
            "하고 슬롯 예약패턴(`<repo>_<N>`)은 쓸 수 없다.",
            file=sys.stderr,
        )
        return 1
    # 기바인딩 요구 — 생성은 `task new`·bootstrap 이 하고 여기선 붙이기만 한다. 미바인딩이면 안내 rc1.
    if pool_store.find_task(pm_paths.home_at(REPO), task) is None:
        print(
            f"[중단] task {task!r} 이(가) 아직 없다 — alloc/add 는 기존 task 에 슬롯을 붙일 뿐 "
            f"정체성을 생성하지 않는다. 먼저 `pm-config task new {task}`(정체성만) 또는 "
            f"`{_runtime_skill_entry('pm-bootstrap')} --task {task}`(생성+진입) 로 task 를 만든 뒤 "
            "다시 시도하라.",
            file=sys.stderr,
        )
        return 1
    return 0


def _slot_live_branch(slot: str) -> str:
    """슬롯 worktree 의 live 브랜치를 fail-soft 조회.

    `current_branch(slot)`(git=진실·장부 저장 폐지)를 부른다 — 미해소/조회불가는 표시 문자열로
    흡수(재열거가 침묵/크래시 하지 않게).
    """
    try:
        return worktree_git.current_branch(pm_paths.home_at(REPO), slot) or "(detached)"
    except Exception as exc:  # noqa: BLE001 — 일반 조회 실패는 표시 흡수(재열거는 부작용 0).
        return "(조회불가)"


def _render_task_slots(task_name: str) -> None:
    """task 보유 슬롯 **집합**을 재열거해 surface 한다.

    집합을 바꾸는 연산(alloc·release·task end·`worktree add --task`) **직후** 결과 집합(슬롯·
    repo·branch·state 행렬)을 출력한다 — 사용자/PM 이 매 연산 후 현재 보유 집합을 한눈에 본다
    (묶음 처리 1급화). 부트스트랩 진입 열거와 같은 렌더 문법을 공유하려는 공용 헬퍼다.
    `slots_for_task`(session==task 명의 leased·장부 진실)에서 파생 — 신설 저장소 0·부작용 0.
    """
    try:
        slots = list(pool_store.slots_for_task(pm_paths.home_at(REPO), task_name) or [])
    except Exception as exc:  # noqa: BLE001 — 일반 재열거 실패는 흡수(연산 자체는 이미 성공).
        return
    label = f"작업공간 (task {task_name!r} 보유 {len(slots)})"
    if not slots:
        print(f"{label}: (없음)")
        return
    print(f"{label}:")
    for l in sorted(slots, key=lambda x: x.slot):
        branch = _slot_live_branch( l.slot)
        print(f"  - {l.slot} · repo={l.repo} · branch={branch} · {l.state}")


def cmd_alloc(
    args: argparse.Namespace) -> int:
    """`alloc <repo> --task <task>` — 기바인딩 task 명의로 idle 최소 번호 슬롯 대여.

    pm_engine.app.worktree_pool.alloc(repo, owner_task=<task>) 로 idle **최소 번호** 슬롯을 leased 로 전이한다 —
    session 축에 task 정체성을 실어 이후 `release --task`/`task end` 소유검사가 매칭한다.
    **항상 신규 대여**: 같은 task 가 이미 이 repo 슬롯을 보유해도 그걸 재반환하지 않고
    다른 idle 슬롯을 대여해 **같은 repo 복수 보유**(병렬 dev 격리
    등)를 자연 지원한다. 대여한 lease 의 reclaim/재부착 보호는 **tasks 장부 조인**(session ∈ tasks
    장부)이 담당한다 — 자동 회수는 없다. idle 슬롯이 없으면 NeedsCreate → **대여 실패 + 사용자 생성 요청**
    (자동 생성 금지·`worktree add --task` 안내로 생성+대여 한 흐름). 새 슬롯은 디스크(코드 전체
    사본×슬롯)라 물리층이고 사용자 승인(`worktree add`)이 게이트다 — alloc/release=PM 자율(논리
    층)·create/remove=사용자 승인(물리층)의 2층 분리를 CLI 로 표면화한다.

    **명 검증 + 기바인딩 요구**: `--task` 이름이 lease session 으로 **기록**되므로 write
    -capable 진입점이다 — 공유 헬퍼 `_validate_prebound_task`(`worktree add --task` 와 동일 계약·엔진
    validator `_validate_task_name` + `find_task` 를 묶음)로 선검증해 traversal/절대경로/빈 이름/
    `<repo>_<N>` 예약(기계판별 구멍)을 fail-loud rc1 하고, **task 는 기바인딩이어야 한다**를
    강제한다 — 미바인딩이면 "먼저 `pm-config task new <task>` 또는 `/pm-bootstrap --task <task>` 으로
    task 를 만들라" 안내(정체성 *생성*은 그 둘 소관·alloc은 이미 존재하는 task 에 슬롯을 붙일 뿐).
    예약패턴 판별 근거(등록 repo)는 `_registered_repos` 가 areas.md 에서 해소한다(읽기 실패면 traversal
    검증만·예약패턴 완화).

    worktree_pool 주입으로 hermetic 테스트(실 git alloc 없이 배선·분기 검증).
    """


    # 명 검증 + 기바인딩 요구 — 공유 헬퍼(`worktree add --task` 와 동일 규율).
    # InvalidTaskName/미바인딩 → rc1(부작용·alloc 이전·진단은 헬퍼가 출력).
    rc = _validate_prebound_task( args.task)
    if rc:
        return rc

    try:
        # owner_task = task-명의 alloc — 항상 신규 idle 슬롯 대여(같은 repo
        # 복수 보유). session=owner_task 로 기록되고, 그 슬롯의 reclaim/재부착 보호는 tasks 장부 조인이
        # 담당한다(bound 아님).
        folders_arg = getattr(args, "folders", None)
        folders = None if folders_arg is None else [item.strip() for item in folders_arg.split(",")]
        lease = pm_engine.app.worktree_pool.alloc(pm_paths.home_at(REPO), args.repo, owner_task=args.task, folders=folders)
    except worktree_records.NeedsCreate:
        folders_option = f" --folders {shlex.quote(folders_arg)}" if folders_arg is not None else ""
        print(
            f"[중단] repo {args.repo!r} 에 대여 가능한 idle 슬롯이 없다 — 풀 소진. 새 슬롯은 "
            "디스크(코드 전체 사본×슬롯·clone 시간)라 자동 생성하지 않는다(물리층=사용자 승인). "
            f"사용자에게 repo {args.repo!r} 슬롯 생성 승인을 요청하라. 승인한 사용자만 "
            f"`pm-config worktree add {args.repo} --task {args.task}{folders_option} --user-ack {args.repo}` 를 "
            "직접 실행해 새 슬롯을 만들며 곧바로 이 task 명의로 대여한다"
            "(생성+대여 한 흐름·ⓓB·오슬롯 없음·세션 자동 실행 금지).",
            file=sys.stderr,
        )
        return 1
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except worktree_records.BareRepoMissing as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    except worktree_records.CheckoutFailed as exc:
        print(f"[중단] 슬롯 체크아웃 실패: {exc}", file=sys.stderr)
        return 1

    slot_path = pm_paths.slot_path(pm_paths.home_at(REPO), lease.slot)
    print(
        f"✓ 슬롯 대여: {lease.slot} (repo={lease.repo} · task={args.task}) → {slot_path}\n"
        f"  이 슬롯은 task {args.task!r} 명의로 leased — 코드 작업은 이 슬롯 cwd 에서. "
        f"보드/wiki 는 multi-PM 공유 `.project_manager`.\n"
        f"  반납: `pm-config release {lease.slot} --task {args.task}` (작업완료 시 idle 반납)."
    )
    try:
        actual_folders = sparse_checkout.slot_folders(pm_paths.home_at(REPO), lease.slot)
    except worktree_records.SlotFoldersRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print("  폴더: " + (", ".join(actual_folders) if actual_folders is not None else "(제한 없음)"))
    if folders is not None:
        print("  폴더를 줄이면 빠진 폴더 안의 추적 밖 파일(빌드 결과물 등)은 git 이 지운다")
    # 집합 변경 직후 재열거 — task 가 지금 보유한 슬롯 집합을 surface.
    _render_task_slots( args.task)
    return 0


def cmd_task_new(
    args: argparse.Namespace) -> int:
    """`task new <task>` — task 정체성만 생성(세션 묶기 없음·닫힘·슬롯 0).

    `task_lifecycle.create_task` 로 장부 `tasks[]` 행(`prefix=None`·닫힘)과 서술 pm_state 를
    만든다 — 관리자/사용자가 아이들 상태에서 task 목록을 조정하는 진입이다. 세션에 묶는 것은
    부트스트랩 몫이고, 닫힘은 종료 기록과 같은 값이라 이어지는 `/pm-bootstrap --task <task>` 이
    **resume** 으로 이어받는다(신규 생성 아님).

    `--prefix` 는 두지 않는다 — 새 prefix 신설은 `task prefix` 의 `--user-ack` 승인 게이트
    단일 지점이고, 여기에 같은 인자를 두면 그 게이트를 우회한다. 생성 후
    `pm-config task prefix <task> <p>` 로 지정한다(출력이 그 다음 단계를 안내한다).

    거부(각각 rc1·장부 무변경): 부적합 이름(`InvalidTaskName` — traversal·`<repo>_<N>` 예약) ·
    동명 종료 archive(`TaskArchived` — `task reopen` 안내) · **이미 있는 이름**(재개는 bootstrap
    몫이라 여기서 열림 표시를 만지지 않는다).

    worktree_pool 주입으로 hermetic 테스트. 예약패턴 판별 근거는 `_registered_repos` 가
    해소한다(읽기 실패면 None → traversal 검증만·`cmd_alloc` 과 같은 한 자리).
    """

    name = args.name

    # 예약패턴(`<repo>_<N>`) 판별 근거 — areas fail-soft 해소(실패면 None·traversal 검증은 유지).
    registered = _registered_repos()

    try:
        task = task_lifecycle.create_task(pm_paths.home_at(REPO), name, registered_repos=registered)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {name!r} — {exc.reason}. task 이름은 안전한 단일 이름이어야 "
            "하고 슬롯 예약패턴(`<repo>_<N>`)은 쓸 수 없다.",
            file=sys.stderr,
        )
        return 1
    except worktree_records.TaskArchived as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        print(
            f"  → `pm-config task reopen {name}` (복수 후보면 `--archive <basename>` 추가) 로 "
            "같은 정체성을 복원하라.",
            file=sys.stderr,
        )
        return 1

    if task is None:
        print(
            f"[중단] task {name!r} 이(가) 이미 있다 — `task new` 는 정체성을 새로 만들 뿐 "
            f"재개하지 않는다. 재개는 `{_runtime_skill_entry('pm-bootstrap')} --task {name}` 다.",
            file=sys.stderr,
        )
        return 1

    print(f"✓ task 생성: {name!r} (닫힘 · 슬롯 0) — 정체성만 만들었다(세션 묶기 없음).")
    print(
        f"  다음 진입: `{_runtime_skill_entry('pm-bootstrap')} --task {name}` "
        "(이 정체성을 재개로 이어받는다)."
    )
    print(
        f"  슬롯 대여: `pm-config alloc <repo> --task {name}` · "
        f"prefix 지정: `pm-config task prefix {name} <p>`."
    )
    return 0


def cmd_task_branch(
    args: argparse.Namespace) -> int:
    """`task branch <task> <슬롯> <브랜치> --from <부모>` — 슬롯에 새 브랜치를 만들고 부모를 기록한다.

    슬롯 안에서 새 브랜치를 만드는 유일한 명령이다(`switch` 는 기존 브랜치 전환만 한다). 브랜치는
    부모 브랜치의 최신 커밋에서 만들고, 부모 이름과 갈라진 커밋을 git 저장소 설정에 적는다
    (`task_lifecycle.create_task_branch`). `<task>` 은 그 슬롯을 쥔 task 다.
    슬롯은 늘 적는다(`release <slot>` 과 같은 위치 인자·암묵 선택 없음). 엔진 거부는 사유 문구 그대로 rc 1 이다.
    """

    try:
        slot = worktree_paths._normalize_slot(pm_paths.home_at(REPO), args.slot)
    except worktree_records.SlotResolutionError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    try:
        result = branch_checkout.create_task_branch(pm_paths.home_at(REPO), slot, args.branch, parent=args.parent, owner_task=args.name)
    except (worktree_records.TaskBranchRefused, pool_store.ReadonlySlotMutation) as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    print(
        f"✓ 슬롯 {slot} 에 새 브랜치 `{result.branch}` 를 만들었다 — 부모 `{args.parent}` "
        f"@ {_short_sha(result.head)} 에서 갈라짐(부모 기록 완료)."
    )
    return 0


def cmd_task_close(
    args: argparse.Namespace) -> int:
    """`task close <task>` — 종료 기록 없이 열린 채 남은 task 세션 기록을 닫는다.

    **기록만 닫는다** — 복원(claim 티켓·라운드 단계 등)은 이 경로의 관심사가 아니다(그건
    부트스트랩 `--force` 인계 몫). 세션이 핸드오프 없이 끝나면 장부 task 행이 열린 채 남아
    다음 부트스트랩이 거부하는데, 그것을 닫는 것은 **사용자 행위**다(엔진 자동 회수·만료 시각
    없음). 작업 폴더 쪽 짝은 기존 `pm-config release <slot> --force` 다 — 새로 만들지 않았다.

    write 는 기존 프리미티브 `task_lifecycle.close_task_session` 하나만 부른다(장부 write 재구현 0).
    값은 열림/닫힘 둘뿐이라 세 번째 상태값도, 만료/유예 인자도 없다.
    """

    name = args.name
    try:
        record = pool_store.find_task(pm_paths.home_at(REPO), name)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    if record is None:
        print(f"[중단] 등록 task 가 아니다 — {name!r}.", file=sys.stderr)
        return 1
    if not record.open:
        print(f"  task {name!r} 은 이미 닫혀 있다 — 바꿀 기록 없음(무해).")
        return 0
    task_lifecycle.close_task_session(pm_paths.home_at(REPO), name)
    print(f"✓ task {name!r} 세션 기록을 닫았다 (열림 → 닫힘) — 복원 없음(기록만).")
    print(
        f"  이어서 진입하려면: `{_runtime_skill_entry('pm-bootstrap')} --task {name}`."
    )
    return 0


def cmd_task_end(
    args: argparse.Namespace,
    *,

    board=None,
) -> int:
    """`task end <task>` — task 종료: claimed 소진 게이트 → dirty 게이트 → clean 시 일괄 반납 + 아카이브 이동.

    순서(각 단계 통과해야 다음):
      1. **claimed 티켓 소진 게이트** — board 스캔(`scan_task_tickets`·자체 스캔·`list --task`
         이 task 명의(`<user>/<task>`)로 claimed 인 티켓이 남아있으면 목록 +
         **거부**. 해소 = `board complete`(완료) 또는 `board unclaim`(claimed→open) — **사용자 판단**
         이라 task end 가 자동 실행하지 않는다(목록+거부까지). task 지정 prefix 의 open 티켓은
         **정보 표시만**(차단 안 함·prefix≠경계).
      2. **dirty 게이트** — 보유 작업공간(session==task)에 미커밋 변경이 있으면 목록 + 거부(사용자
         정리 후 재시도). task_lifecycle.end_task 가 판정(아무 부작용 0).
      3. **전부 clean** → 보유 슬롯 일괄 idle 반납(worktree **삭제 안 함**·반납=idle) → 장부 task
         레코드 제거 → 서술 폴더 `.local/tasks/<task>/` 를 `_ended/<task>-<날짜>/` 로 **이동**
         (삭제 아님·이름 재사용 시 옛 pm_state resume 오염 방지).

    worktree_pool/board 주입으로 hermetic 테스트. board 부재/스캔 실패는 claimed 게이트를
    graceful skip(엔진 dirty/반납은 진행) — board 없는 순수 슬롯 정리도 되게(크래시 0).
    """

    name = args.name

    # 명 검증 — end_task 가 장부 write·`shutil.move` 하는 write-capable 진입점이라
    # 엔진이 자체 검증(fail-loud)하지만, CLI 도 board 스캔 이전에 선-fail 로 clean 하게 rc1 한다
    # (traversal/절대경로/빈 이름 → 잘못된 스캔·이동 시도 자체를 안 함). InvalidTaskName 이 없는
    # 구 엔진(사본 lag)이면 getattr 로 graceful skip(엔진 검증이 최종 백스톱).
    try:
        task_rules._validate_task_name(name)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {name!r} — {exc.reason}. task 이름은 안전한 단일 이름이어야 한다.",
            file=sys.stderr,
        )
        return 1

    # 1) claimed 소진 게이트 — board 스캔. task 레코드 prefix 로 prefix-open 정보도 함께.
    task_rec = pool_store.find_task(pm_paths.home_at(REPO), name)
    prefix = task_rec.prefix if task_rec is not None else None
    board_mod = board or _load_module("board", "board.py")
    scan = None
    scanner = getattr(board_mod, "scan_task_tickets", None) if board_mod else None
    if scanner is not None:
        try:
            scan = scanner(_default_user(), name, prefix)
        except Exception as exc:  # noqa: BLE001 — board 스캔 실패는 claimed 게이트 skip.
            scan = None
    if scan and scan.get("claimed"):
        print(
            f"[중단] task {name!r} 명의로 claimed 인 티켓이 남아있다 — 종료 거부(소진 게이트):",
            file=sys.stderr,
        )
        for row in scan["claimed"]:
            print(f"  - {row['id']} [{row['status']}] {row['title']}", file=sys.stderr)
        print(
            "  해소: 각 티켓을 `board complete`(완료) 또는 `board unclaim`(claimed→open) 처리 후 "
            "재시도하라. task end 는 자동 unclaim/complete 하지 않는다(완료/보류는 사용자 판단).",
            file=sys.stderr,
        )
        return 1

    # 2) dirty 게이트 + 3) clean 시 일괄 반납 + 서술 폴더 아카이브 이동 — 엔진 end_task.
    try:
        result = task_lifecycle.end_task(pm_paths.home_at(REPO), name)
    except worktree_records.TaskEndRefused as exc:
        print(
            f"[중단] task {name!r} 종료 거부 — {exc.reason}. "
            "task end 는 종료 기록(핸드오프 또는 `task close`) 뒤에만 허용된다.",
            file=sys.stderr,
        )
        return 1
    if result.refused:
        print(
            f"[중단] task {name!r} 보유 작업공간에 dirty(미커밋 변경)가 있다 — 종료 거부:",
            file=sys.stderr,
        )
        for slot in result.dirty:
            print(f"  - {slot}", file=sys.stderr)
        print(
            "  각 슬롯에서 커밋(또는 사용자가 요청하면 stash) 후 재시도하라.",
            file=sys.stderr,
        )
        return 1

    # 성공 — 반납 + 이동 결과 surface.
    if result.released:
        print(
            f"✓ task {name!r} 보유 슬롯 {len(result.released)}개 idle 반납(worktree 유지·풀 재사용): "
            f"{', '.join(result.released)}"
        )
    else:
        print(f"✓ task {name!r} 보유 슬롯 없음(반납 대상 0).")
    if result.moved_to is not None:
        print(f"✓ 서술 폴더 아카이브 이동(삭제 아님·PM 홈): {result.moved_from} → {result.moved_to}")
    else:
        print(f"  서술 폴더 없음(.local/tasks/{name}/ 부재) — 장부 task 레코드만 제거.")

    # task 지정 prefix 의 open 티켓 = 정보 표시만(차단 안 함·prefix≠경계).
    if scan and scan.get("prefix_open"):
        print(f"ℹ task 지정 prefix {prefix!r} 의 open 티켓(정보·경계 아님·차단 안 함):")
        for row in scan["prefix_open"]:
            print(f"  - {row['id']} {row['title']}")

    print(
        f"✓ task {name!r} 종료 완료 — 이름 재사용 안전(아카이브 이동으로 옛 pm_state 오염 없음·PM 홈)."
    )
    # 집합 변경(일괄 반납) 직후 재열거 — 종료 후 보유 슬롯 0(전부 idle 반납)을 확인.
    _render_task_slots( name)
    return 0


def cmd_task_reopen(
    args: argparse.Namespace) -> int:
    """`task reopen <task> [--archive <basename>]` — 종료 task identity 복원."""

    try:
        result = task_lifecycle.reopen_task(pm_paths.home_at(REPO), args.name, archive=args.archive)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    except worktree_records.TaskReopenRefused as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        if exc.archives:
            print(
                "  archive 후보: " + ", ".join(path.name for path in exc.archives),
                file=sys.stderr,
            )
        return 1

    print(f"✓ task {args.name!r} 동일 정체성 복원: {result.moved_from} → {result.moved_to}")
    print(
        "  장부 재등록: prefix=None(reset) · 닫힘(종료 기록 상태) · "
        f"started={result.task.started}"
    )
    print(
        f"  다음 진입: `{_runtime_skill_entry('pm-bootstrap')} --task {args.name}`"
    )
    return 0


# `none` = prefix 해제 리터럴(무prefix·T-NNNN 발행) — board `_PREFIX_RESERVED` 와 동형(case-insensitive
# fold). setter 는 이 리터럴을 board `_validate_prefix`(예약어 거부) 이전에 가로채 해제로 번역한다.
_TASK_PREFIX_NONE_LITERAL = "none"


def _todo_missing_task(name: str) -> int:
    """todo 대상 task 가 장부에 없을 때의 단일 안내(세 명령 공용·문구 사본 0)."""
    print(
        f"[중단] task {name!r} 이(가) 장부에 없다 — todo 는 기존 task 레코드에 달린다. "
        f"먼저 `pm-config task new {name}`(정체성만) 또는 "
        f"`{_runtime_skill_entry('pm-bootstrap')} --task {name}`(생성+진입) 로 task 를 만들어라.",
        file=sys.stderr,
    )
    return 1


def _print_open_todos( name: str, todos) -> None:
    """열린 todo 목록 출력 — 세 명령 공용(문구·형식 사본 0)."""
    print(f"task {name} 열린 todo {len(todos)}건:")
    for todo in todos:
        print(f"  {task_store.format_todo_line(todo)}")


def cmd_task_todo_add(
    args: argparse.Namespace) -> int:
    """`task todo add <task> <내용>` — 열린 todo 를 하나 더한다.

    쓰기 판정이 "열린 todo 가 없으면 경고" 를 이 목록으로 내므로, 이 명령이 작업을 시작하는
    진입이다. 등록 시각(UTC ISO)과 ID 는 엔진이 박고 사용자가 주지 않는다 — 묵은 정도의 근거를
    손으로 적게 하면 그 값이 곧 틀린다.
    """

    try:
        updated = task_lifecycle.add_task_todo(pm_paths.home_at(REPO), args.name, args.text)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    if updated is None:
        return _todo_missing_task(args.name)
    _print_open_todos( args.name, updated)
    return 0


def cmd_task_todo_list(
    args: argparse.Namespace) -> int:
    """`task todo list <task>` — 열린 todo 를 ID·등록일·묵은 일수와 함께 본다."""

    try:
        task = pool_store.find_task(pm_paths.home_at(REPO), args.name)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    if task is None:
        return _todo_missing_task(args.name)
    todos = task_store.read_task_todos(pm_paths.home_at(REPO), task.name)
    if not todos:
        print(f"task {task.name} 열린 todo 없음 — `pm-config task todo add {task.name} <내용>`")
        return 0
    _print_open_todos( task.name, todos)
    return 0


def cmd_task_todo_done(
    args: argparse.Namespace) -> int:
    """`task todo done <task> <ID>` — 열린 todo 하나를 완료 처리한다(목록에서 제거).

    완료 표시를 남기지 않는다 — 무엇을 했는지의 단일 진실은 log handoff entry 다. ID 는 재사용
    하지 않으므로 목록이 줄어도 남은 줄의 번호가 밀리지 않는다. 없는 ID 면 rc1 로 멈춘다
    (무시하면 지운 줄 알고 넘어간다).
    """

    try:
        result = task_lifecycle.close_task_todo(pm_paths.home_at(REPO), args.name, args.todo_id)
    except worktree_records.InvalidTaskName as exc:
        print(f"[중단] 부적합 task 명 {args.name!r} — {exc.reason}.", file=sys.stderr)
        return 1
    except LookupError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1
    if result is None:
        return _todo_missing_task(args.name)
    updated, removed = result
    print(f"완료: {str(removed.get('text', '')).strip()}")
    if not updated:
        print(f"task {args.name} 열린 todo 없음 — 이 상태에서는 파일 수정에 경고가 붙는다.")
        return 0
    _print_open_todos( args.name, updated)
    return 0


def cmd_task_prefix(
    args: argparse.Namespace,
    *,

    board=None,
) -> int:
    """`task prefix <task> <p|none>` — task 의 ticket prefix 지정/변경/해제 (중간 변경 자유).

    task 레코드(장부 top-level `tasks`)의 board prefix 를 opt-in 으로 지정/변경(`<p>`)하거나
    해제(`none`)한다 — prefix 는 task 와 **완전 독립·분류 라벨이지 경계 아님**(claim 강제 없음). 지정
    시 `board.py new --task <task>` 이 3단 해소(명시 `--prefix` > task 설정 > 기본 없음)로 그 prefix 를
    자동으로 단다. **중간 변경 자유**(task 종속으로 못 바꾸는 설계 금지) — 진행 중 언제든 바꾼다.

    검증 순서(각 통과해야 다음·부작용 이전 fail-loud rc1):
      1. **task 명 검증** — 공유 엔진 validator(`_validate_task_name`·예약패턴엔 등록 repo 전달)로
         traversal/절대경로/빈 이름/`<repo>_<N>` 예약을 거부. alloc/task end 와 동일 깔때기.
      2. **prefix 값 해소** — `none`(대소문자 무관)은 **해제 리터럴** → None(무prefix).
         그 외는 board **소비측 grammar 단일 진실**(`_validate_prefix` `[a-z0-9_]` 형식·예약어)로
         선검증 — `board.py new` 가 task prefix 를 재검증 없이 신뢰하므로 여기가 유일 입력 게이트다.
      3. **저장** — `task_lifecycle.set_task_prefix`(장부 flock/스키마 단일 소유·직접 JSON write 금지)로
         atomic 갱신. task 부재면 rc1 안내(생성은 `task new`·bootstrap 소관).

    worktree_pool/board 주입으로 hermetic 테스트. board 부재/헬퍼 부재는 prefix 형식·
    신설 승인·락 안 fresh 재검증을 증명할 수 없으므로 저장 없이 fail-closed rc1이다.
    단, task 예약명 검증에 넘기는 registered_repos 조회만 실패 시 None으로 완화하고
    traversal 검증은 엔진 validator로 계속 강제한다.
    """

    name = args.name
    value = args.value

    # 1) task 명 검증(must-fix) — 공유 엔진 validator. registered_repos 는 예약패턴(`<repo>_<N>`)
    # 거부용(board fail-soft 해소·부재면 None → traversal 검증만). InvalidTaskName → rc1(부작용 이전).
    board_mod = board or _load_module("board", "board.py")
    registered = _registered_repos()
    try:
        task_rules._validate_task_name(name, registered)
    except worktree_records.InvalidTaskName as exc:
        print(
            f"[중단] 부적합 task 명 {name!r} — {exc.reason}. task 이름은 안전한 단일 이름이어야 한다.",
            file=sys.stderr,
        )
        return 1

    # 2) prefix 값 해소 — `none`(fold)은 해제, 그 외는 board `_validate_prefix`(소비 grammar 단일 진실).
    if value.lower() == _TASK_PREFIX_NONE_LITERAL:
        new_prefix: str | None = None
    else:
        validate = getattr(board_mod, "_validate_prefix", None) if board_mod else None
        if validate is not None:
            reason = validate(value)
            if reason:
                print(f"[중단] {reason}", file=sys.stderr)
                return 1
        require_ack = (
            getattr(board_mod, "require_prefix_user_ack", None) if board_mod else None
        )
        if require_ack is None:
            print(
                "[중단] board prefix 승인 게이트를 찾을 수 없다 — 엔진 전체를 동기화한 뒤 "
                "task prefix를 다시 실행하라.",
                file=sys.stderr,
            )
            return 1
        new_prefix = require_ack(
            value,
            getattr(args, "user_ack", None),
            surface="pm-config task prefix",
        )
        if new_prefix is None:
            return 1

    # 3) 저장 — 신규/기존 prefix 지정은 board_lock 안 fresh 3소스 snapshot을 선판정과
    # 대조한 직후 worktree_pool 장부 락으로 내려간다. lock 순서는 board→lease로 고정한다
    # (worktree_pool은 board를 import/호출하지 않아 역순 경로 없음). 엔진 primitive의 직접
    # 소비는 사용자 승인 CLI 표면이 아니므로 이 재검증을 성립시킬 수 없고, 설계대로 CLI 단일
    # 깔때기에서만 강제한다. `none` 해제는 신규 생성 축이 아니어서 종전 경로 그대로다.
    if new_prefix is not None:
        board_lock_fn = getattr(board_mod, "board_lock", None) if board_mod else None
        revalidate = (
            getattr(board_mod, "revalidate_prefix_user_ack", None)
            if board_mod else None
        )
        if board_lock_fn is None or revalidate is None:
            print(
                "[중단] board prefix fresh 재검증 경계를 찾을 수 없다 — 엔진 전체를 "
                "동기화한 뒤 task prefix를 다시 실행하라.",
                file=sys.stderr,
            )
            return 1
        with board_lock_fn():
            fresh = revalidate(
                value,
                getattr(args, "user_ack", None),
                new_prefix,
                surface="pm-config task prefix",
            )
            if fresh is None:
                return 1
            updated = task_lifecycle.set_task_prefix(pm_paths.home_at(REPO), name, fresh)
            new_prefix = fresh
    else:
        updated = task_lifecycle.set_task_prefix(pm_paths.home_at(REPO), name, None)
    if updated is None:
        print(
            f"[중단] task {name!r} 이(가) 아직 없다 — prefix 설정은 기존 task 레코드를 갱신할 뿐 "
            f"정체성을 생성하지 않는다. 먼저 `pm-config task new {name}`(정체성만) 또는 "
            f"`{_runtime_skill_entry('pm-bootstrap')} --task {name}`(생성+진입) 로 task 를 만든 뒤 "
            "다시 설정하라.",
            file=sys.stderr,
        )
        return 1

    invalidate = (
        getattr(board_mod, "invalidate_known_prefixes_cache", None)
        if board_mod else None
    )
    if invalidate is not None:
        invalidate()

    if new_prefix is None:
        print(
            f"✓ task {name!r} prefix 해제 — 이후 `board.py new --task {name}` 은 무prefix(T-NNNN)로 발행 "
            "(명시 `--prefix` 는 여전히 1회 오버라이드)."
        )
    else:
        print(
            f"✓ task {name!r} prefix = {new_prefix!r} — 이후 `board.py new --task {name}` 이 이 prefix 를 "
            "자동으로 단다(명시 `--prefix` 가 이김 3단 해소). 분류 라벨이지 경계 아님(claim 강제 없음)."
        )
    return 0


def cmd_update(
    forward_args: list[str],
    *,
    pm_update=None,
) -> int:
    """`update [--from ...]` — 엔진 갱신 (pm-update 흡수).

    pm_update.main(forward_args) 로 verbatim forward 한다 — pm_update 가 CLI 규격의
    단일 진실이고, 이 서브커맨드는 그 main 으로 위임만 한다(중복 구현 0·rename 비용 0).
    forward_args 는 `update` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--from`·`--dry-run` 등 option-like 플래그를 디스패처가 가로채지 않게 `pm_config.main`
    이 (raw[0]=="update" special-case 로) argparse 를 우회해 이 핸들러로 raw 토큰을 넘긴다.
    pm_update.main 은 자체 argparse 로 `--from` 등을 직접 받는다(update 서브커맨드 개념 없음·
    우회 주체는 pm_update 가 아니라 pm_config.main 이다).

    pm_update 주입으로 hermetic 테스트(실 동기화 없이 forward 배선 검증).
    """
    pm_update_mod = pm_update or _load_module("pm_update", "pm_update.py")
    if pm_update_mod is None:
        print(
            "[중단] pm_update.py 엔진을 찾을 수 없다 — 엔진 갱신 불가 "
            f"({TOOLS_DIR / 'pm_update.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    # usage prog 정합 — pm_update.main 은 prog="pm_update.py" 하드코딩·prog
    # 인자 없음 → 위임 동안만 "pm_update.py"→"pm-config update" 로 치환한다(에이전트가 칠 실
    # 커맨드와 정합·파일명 leak 0). 실 배선 검증 테스트가 mock 을 주입하면 fake.main 은 argparse
    # 를 만들지 않으므로 이 래핑은 무해(패치 후 즉시 원복).
    with _forwarded_prog({"pm_update.py": f"{repo_protection._FACADE_PROG} update"}):
        return pm_update_mod.main(forward_args)


# ── upstream show/set  ───────────────────────────────────
# upstream 값(URL|경로 self-describing)을 조회/전환한다. set 은 검증(URL→ls-remote 도달성·
# 경로→존재+checkout)을 통과해야 local.conf 에 atomic 재기록(타 키 보존)된다 — fail-closed
# (나쁜 값 silently 기록 금지). URL 안전 계약(scheme allowlist·credential 거부·leading-dash·
# argv-list·timeout)·local.conf set-or-replace 는 pm_import 헬퍼를 재사용한다(중복 구현 0).


def _validate_upstream_reachable(
    value: str,
    *,
    pm_import_mod,
    git_runner: GitRunner | None = None,
) -> tuple[bool, str]:
    """upstream 값 도달성 검증 — URL→`git ls-remote`·경로→존재+git checkout (fail-closed).

    pm_import.validate_upstream_value(순수 형태 안전·scheme/credential/dash)를 먼저 통과시킨 뒤
    *도달성*을 본다:
      - URL  → `git ls-remote <url>` rc==0(원격 reachable·argv-list·timeout·GIT_TERMINAL_PROMPT=0).
      - 경로 → 디렉토리 존재 + `git -C <path> rev-parse --is-inside-work-tree` rc==0(git checkout).
    git 호출은 pm_import._real_upstream_git_runner(URL 안전 계약)를 기본으로, 테스트는 git_runner
    주입(라이브 git 0). (ok, reason). 실패는 모두 거부(fail-closed) — silently 기록 금지.
    """
    ok, reason = pm_import_mod.validate_upstream_value(value)
    if not ok:
        return False, reason
    runner = git_runner if git_runner is not None else pm_import_mod._real_upstream_git_runner()
    kind = upstream_rules.classify_upstream(value)
    if kind == "url":
        # `--` 로 위치인자 종결(value 가 옵션으로 오인되지 않게·leading-dash 는 이미 거부됨이나 방어).
        rc, out = runner(["ls-remote", "--", value])
        if rc != 0:
            return False, (
                f"URL upstream 도달 불가 (`git ls-remote {value}` rc={rc}): {out.strip()[:200]} "
                "— URL/네트워크/접근권한 확인."
            )
        return True, ""
    # 경로 — 존재 + git work tree 검증.
    path = Path(value).expanduser()
    if not path.is_dir():
        return False, f"경로 upstream 이 디렉토리가 아니거나 존재하지 않음: {path}"
    rc, _out = runner(["-C", str(path), "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return False, (
            f"경로 upstream 이 git checkout 이 아님: {path} "
            "(`git rev-parse --is-inside-work-tree` 실패)."
        )
    return True, ""


def cmd_upstream(
    args: argparse.Namespace,
    *,
    pm_import=None,
    git_runner: GitRunner | None = None,
) -> int:
    """`upstream show | set <value>` — upstream 값 조회/전환.

    - show: local.conf 의 현재 `upstream.path=` 값을 surface(미등록이면 안내).
    - set <value>: 검증(URL→ls-remote 도달성·경로→존재+checkout·fail-closed) 통과 후 local.conf
      `upstream.path=` atomic 재기록(타 키 보존). 나쁜 값은 거부(rc 1·기록 안 함). 값 self-describing
      이라 path↔URL 전환이 자동(스킬 freshness 분기가 모양으로 적응).

    local.conf set-or-replace(pm_import._set_conf_keys·타 키·주석 보존)·URL 안전 검증(pm_import.
    validate_upstream_value)을 재사용한다(중복 0). pm_import/git_runner 주입으로 hermetic 테스트
    (라이브 git/실 conf-write 없이 배선·검증 분기 검증).
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print(
            "[중단] pm_import.py 엔진을 찾을 수 없다 — upstream 조회/전환 불가 "
            f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1

    local_conf = REPO / ".project_manager" / "local.conf"
    action = getattr(args, "upstream_action", None)

    if action == "show":
        if not local_conf.is_file():
            print(f"upstream: (local.conf 없음 — {local_conf})")
            return 0
        conf = pm_import_mod._parse_conf_keys(fs.read_text_shared(local_conf, encoding="utf-8"))
        value = conf.get("upstream.path", "").strip()
        if not value:
            print("upstream: (미등록) — `pm-config upstream set <url|path>` 로 설정하라.")
            return 0
        kind = upstream_rules.classify_upstream(value)
        print(f"upstream: {value}  ({kind})")
        return 0

    # set <value> — 검증 후 atomic 재기록(타 키 보존). fail-closed.
    value = args.value

    # suggestion(codex): local.conf 부재 확인을 *네트워크(reachability) 전* 으로 옮긴다 —
    #   기록 불가 상태(conf 없음)에서 굳이 ls-remote 로 네트워크를 치지 않게(낭비·지연 회피).
    if not local_conf.is_file():
        print(
            f"[중단] local.conf 없음 ({local_conf}) — 먼저 `pm-config init` 으로 셋업하라.",
            file=sys.stderr,
        )
        return 1

    ok, reason = _validate_upstream_reachable(
        value, pm_import_mod=pm_import_mod, git_runner=git_runner)
    if not ok:
        print(f"[중단] upstream 값 거부 (기록 안 함) — {reason}", file=sys.stderr)
        return 1

    try:
        pm_import_mod._write_conf_keys(local_conf, {"upstream.path": value})
    except RuntimeError as exc:
        print(
            f"[중단] {exc}",
            file=sys.stderr,
        )
        return 1
    # upstream은 보호 push gate의 provenance 입력이다. 값이 같아도 끊긴/stale sidecar를
    # 자가치유하도록 등록 repo 전수를 중앙 설치 깔때기로 보낸다.
    repo_protection._refresh_protected_gate_contracts(pm_paths.home_at(REPO))
    kind = upstream_rules.classify_upstream(value)
    print(f"✓ upstream 설정: {value}  ({kind}) — pm_update 가 --from 생략 시 이 값을 쓴다.")
    return 0


def cmd_init(
    forward_args: list[str],
    *,
    board=None,
) -> int:
    """`init [<board init 인자>]` — clone 당 1회 셋업 (board.py init 흡수).

    board.main(["init", *forward_args]) 로 verbatim forward 한다 — board.py init 이
    CLI 규격의 단일 진실이고, 이 서브커맨드는 그 main 으로 위임만 한다(중복 구현 0).
    forward_args 는 `init` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--prefix`·`--area`·`--owner`·`--session` 등 option-like 플래그를 디스패처가
    가로채지 않게 `pm_config.main` 이 (raw[0]=="init" special-case 로) argparse 를
    우회해 이 핸들러로 raw 토큰을 넘긴다. board.main 은 자체 argparse 의 `init`
    서브커맨드로 그 플래그를 직접 받는다(우회 주체는 board 가 아니라 pm_config.main 이다).

    init 은 N=1·M=1 ~ N×M 공용 보편 셋업 — pm-config init 은 그걸 single-user
    multi-repo front door 로 노출만 한다(동작 불변·새 동작 0·cmd_update 의 위임 패턴 동형).

    board 주입으로 hermetic 테스트(실 셋업 부작용 없이 forward 배선 검증).
    """
    board_mod = board or _load_module("board", "board.py")
    if board_mod is None:
        print(
            "[중단] board.py 엔진을 찾을 수 없다 — clone 셋업 불가 "
            f"({TOOLS_DIR / 'board.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    # usage prog 정합 — board.main 은 prog="board.py" 하드코딩·prog 인자
    # 없음 → 위임 동안만 "board.py"→"pm-config" 로 치환한다. init 서브파서 usage 는 부모 prog
    # 에서 "pm-config init" 로 자동 파생돼 에이전트가 칠 실 커맨드와 정합(파일명 leak 0).
    with _forwarded_prog({"board.py": repo_protection._FACADE_PROG}):
        rc = board_mod.main(["init", *forward_args])
    return rc


def cmd_add_harness(
    args: argparse.Namespace,
    *,
    pm_import=None,
    dest_root: Path | None = None,
) -> int:
    """`add-harness <harness> [--dry-run]` — 라이브 인스턴스에 두 번째 harness 어댑터 추가.

    pm_import.add_harness_cli(dest, harness, dry_run=) 로 verbatim 위임한다 — 복사 스코프(어댑터
    네임스페이스만)·비파괴 백업·토큰 치환은 pm_import 의 add_harness 가, 인터페이스 예외의 친화
    번역(rc 1)은 그 main-style 래퍼 add_harness_cli 가 단일 진실로 소유한다(pm_import 가 CLI 규격·
    로직/에러경계 중복 0). harness 는 choices 로 재검증하지 않고 verbatim 으로 넘긴다 — 미지원
    harness('both'/오타)는 add_harness_cli 가 ValueError→rc 1 로 거른다(pm_config 자체 검증 0).

    dest 해소는 기존 pm_config 관례(REPO=스크립트-위치 앵커·cwd 무관·cmd_upstream 과 동형) —
    pm_config.py 가 사는 이 인스턴스 루트가 곧 harness 를 추가할 라이브 인스턴스다. dest_root
    미주입 시 *호출 시점*에 REPO 로 해소한다(테스트 monkeypatch/주입 존중·cmd_repo_add repos_dir
    동형 — 함수 default 로 굳히면 주입이 안 먹는다).

    pm_import/dest_root 주입으로 hermetic 테스트(실 복사 부작용 없이 위임 배선 검증).
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print(
            "[중단] pm_import.py 엔진을 찾을 수 없다 — harness 추가 불가 "
            f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    dest = dest_root if dest_root is not None else REPO
    # --from(source_root) 은 optional — 생략 시 pm_import 가 dest local.conf upstream.path 에서 어댑터
    # 소스를 자동 해소한다(imported 인스턴스 갭). 기존 Namespace(테스트/구 호출)에 source 가
    # 없어도 getattr 로 안전하게 None 폴백(하위호환·verbatim forward).
    source_root = getattr(args, "source", None)
    # usage prog 정합 — init/update forward 와 동형 위임-경계 가드. add_harness_cli
    # 는 자체 argparse 를 만들지 않고 main-style 로 출력·rc 반환하지만, 위임 경계를 init/update 와
    # 균일하게 감싸 pm_import 가 어떤 argparse usage 를 surface 하더라도 파일명("pm_import.py")이 새지
    # 않게 한다(에이전트가 칠 실 커맨드 pm-config add-harness 와 정합·경계 leak 0). rc 는 그대로 전파.
    with _forwarded_prog({"pm_import.py": f"{repo_protection._FACADE_PROG} add-harness"}):
        return pm_import_mod.add_harness_cli(
            dest, args.harness, dry_run=args.dry_run, source_root=source_root)


# instance-owned 어댑터 config 판정 → 사람이 읽을 라벨. 판정 이름 자체는 pm_import 소유이고
# 여기선 표시만 한다(판정 사본 0). ⚠ **라벨은 mode 와 함께 판정한다** — `unedited` 는 managed 에서만
# 자동 갱신을 뜻하고 report 대상에선 "무편집이지만 갱신 안 함" 이다(status 만 보면 거짓 안내).
_ADAPTER_CONFIG_STATUS_LABEL = {
    "in-sync": "최신 (template 과 동일)",
    "unedited": "무편집 — 다음 동기가 자동 갱신",
    "edited": "채택자 편집 — 보존(수용은 --accept)",
    "unrecorded": "원장 부재 — 보존(수용은 --accept)",
}
# 보고-전용 대상의 override 라벨(그 밖의 status 는 위 공통 라벨을 그대로 쓴다).
_ADAPTER_CONFIG_REPORT_LABEL = {
    "unedited": "무편집 — 보고 전용(자동 갱신 안 함·수용은 --accept)",
    "edited": "채택자 편집 — 보고 전용(수용은 --accept)",
    "unrecorded": "원장 부재 — 보고 전용(수용은 --accept)",
}
# 수용 결과 → rc·안내. `accepted` 만 성공이고 나머지는 사유를 그대로 노출한다(거짓 성공 금지).
_ADAPTER_ACCEPT_FAILURE_HINT = {
    "raced": "다시 실행해 새 판정을 받아라(그 사이 파일이 바뀌었다).",
    "ledger-blocked": "엔진을 갱신한 뒤 다시 실행하라(원장이 더 새로운 형식이다).",
    "write-failed": "경로 권한/디스크 상태를 확인한 뒤 다시 실행하라.",
    "ledger-failed": "원장 경로 권한을 고친 뒤 다시 실행하라(파일 내용은 이미 상류 값이다).",
}


def _accepts_kwarg(func, name: str) -> bool:
    """그 함수가 이 키워드를 받는가 — **엔진 사본 세대 차** 흡수 지점.

    복구 채널·부분 전파 창에서는 형제 pm_import 가 직전 세대일 수 있다(그 창을 열어 두는 게
    복구 exemption 의 목적이다). 새 키워드를 무조건 넘기면 그 상태에서 CLI 가 TypeError 로
    죽는다 — 있으면 쓰고 없으면 구 시그니처로 강등하되, 강등은 **loud** 다(무음 강등 금지)."""
    if func is None:
        return False
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # 시그니처를 못 읽는 호출가능 객체 — 보수적으로 구형 취급.
        return False


def _warn_engine_downgrade(what: str, effect: str) -> None:
    """구형 형제 엔진 때문에 기능을 낮춰 실행한다는 사실을 알린다(관측 불가 금지)."""
    print(f"[경고] 형제 pm_import 가 구세대라 {what} 없이 진행한다 — {effect}. "
          "pm-update 로 엔진 사본을 맞춘 뒤 다시 실행하면 정상 판정을 받는다.", file=sys.stderr)


def _adapter_accept_decision(pm_import_mod, dest: Path, source_root: Path, relpath: str):
    """수용 전 세대 판정 — 막을 사유 + 판정에 쓴 template 해시(`None` = 강제할 게이트 부재).

    **mutation 게이트라 상류 세대 선언으로 판정한다**: 설치본 선언은 상류가 이번에 들여오는 새
    플래그를 모르므로, 그 위에서 config 를 앞세우면 게이트가 있으나 마나다. 상류를 못 읽으면
    엔진이 fail-closed 로 blocker 를 낸다(모르면 멈춘다).

    형제 사본이 구세대면 **있는 만큼 게이트를 살린다**(강등은 3단이고 각 단은 loud 다):
      신 API 전부      상류 선언 판정 + 스냅샷 결속.
      `hook_set_accept_blockers` 만  직전 세대 — 순서 게이트 판정은 그대로 적용하고 결속만 뺀다.
                       게이트가 실재하는데 통째로 끄면 그 창에서 락아웃을 그대로 설치한다.
      둘 다 부재       강제할 선언 자체가 없는 세대 — loud 후 게이트 없이 진행(도입 이전 경로)."""
    decide = getattr(pm_import_mod, "hook_set_accept_decision", None)
    resolve = getattr(pm_import_mod, "hook_set_declarations", None)
    if decide is not None and resolve is not None:
        return decide(dest, source_root, relpath,
                      declarations=resolve(source_root, required=True))
    blockers_fn = getattr(pm_import_mod, "hook_set_accept_blockers", None)
    if blockers_fn is not None:
        _warn_engine_downgrade(
            "상류 세대 선언·수용 스냅샷 결속",
            "직전 세대의 순서 게이트로 판정한다(설치본 선언 기준이라 이번 상류가 새로 들여오는 "
            "플래그는 못 보고, 판정한 상류 bytes 와 설치 bytes 도 결속되지 않는다)")
        return SimpleNamespace(
            blockers=list(blockers_fn(dest, source_root, relpath)),
            template_sha256=None, generation_sha256=None,
            generation="로컬", reasons=())
    _warn_engine_downgrade(
        "훅 세트 세대 게이트", "미지원 드라이버 위에 config 를 앞세워도 막지 못한다")
    return None


def _adapter_accept_order_blocked(decision, relpath: str) -> bool:
    """엔진 파일 선행 순서를 어기는 수용인가 — 어기면 처방을 내고 True(파일 미변경).

    수용은 dest config 를 상류 세대로 **앞세우는** 행위다. 그 세대가 요구하는 래퍼/드라이버가
    dest 에 아직 없으면 훅이 미지원 플래그를 rc2 로 거부하고, 그 rc 는 도구 차단으로 번역된다
    (v1.7.0 흡수 실측 락아웃). 위험 방향은 이쪽 하나뿐이라 "엔진 파일 선행 · config 후행"
    순서를 여기서 기계가 강제한다 — 단건 `--accept` 와 세트 수용이 같은 게이트를 탄다."""
    if decision is None or not decision.blockers:
        return False
    print(f"[중단] 어댑터 config 수용 거부 ({relpath}) — 이 세대가 요구하는 훅 파일이 아직 "
          "dest 에 설치돼 있지 않다(파일을 바꾸지 않았다).", file=sys.stderr)
    for finding in decision.blockers:
        print(f"  - {finding.detail}", file=sys.stderr)
    print("  → pm-update 로 엔진 파일을 먼저 받은 뒤 이 수용을 다시 실행하라 "
          "(엔진 파일 선행 · config 후행).", file=sys.stderr)
    return True


def _accept_adapter_config_one(pm_import_mod, dest: Path, source_root: Path,
                               relpath: str, judgments) -> int:
    """config 한 개 수용 — 선행조건 게이트 → 원자 교체 → rc·안내 (단건·세트 공용 경로).

    단건과 세트가 이 함수 하나만 호출하므로 게이트·백업·경쟁 판정·안내가 갈릴 수 없다."""
    decision = _adapter_accept_decision(pm_import_mod, dest, source_root, relpath)
    if _adapter_accept_order_blocked(decision, relpath):
        return 1
    # 수용은 **판정을 먼저 받고** 그 시점 해시를 넘긴다 — 판정과 쓰기 사이에 파일이 바뀌면
    #   엔진이 raced 로 중단한다(검증 없는 덮어쓰기 0). 축은 둘이다: dest(채택자 동시 편집)와
    #   **template**(게이트가 검사한 bytes 와 실제 복사할 bytes 의 결속) — 후자가 없으면 상류가
    #   그 사이 바뀌어도 "검사한 세대" 와 다른 내용이 설치된다.
    judged = next((item for item in judgments if item.relpath == relpath), None)
    accept = pm_import_mod.accept_adapter_config
    snapshot = {}
    if decision is not None:
        # 스냅샷 결속은 **둘이 한 벌**이다(상류 config bytes + 선언 소스 bytes) — 한쪽만 지원하는
        #   사본은 없지만, 지원 여부는 각각 확인해 구세대에서 TypeError 로 죽지 않게 한다.
        for name, value in (("expected_template_sha256", decision.template_sha256),
                            ("expected_generation_sha256", decision.generation_sha256)):
            if _accepts_kwarg(accept, name):
                snapshot[name] = value
            elif value is not None:
                _warn_engine_downgrade(
                    f"수용 스냅샷 결속({name})",
                    "판정한 상류 내용과 다른 bytes 가 설치돼도 감지하지 못한다")
    try:
        outcome = accept(
            dest, source_root, relpath,
            expected_sha256=judged.dest_sha256 if judged is not None else None,
            **snapshot)
    except ValueError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"오류: 어댑터 config 를 교체하지 못했습니다 ({relpath}: {exc}) — "
              "원본은 그대로입니다.", file=sys.stderr)
        return 1
    if outcome.status != "accepted":
        print(f"오류: 어댑터 config 수용 실패 ({relpath} · {outcome.status}): "
              f"{outcome.detail}", file=sys.stderr)
        hint = _ADAPTER_ACCEPT_FAILURE_HINT.get(outcome.status)
        if hint:
            print(f"  → {hint}", file=sys.stderr)
        if outcome.backup is not None:
            print(f"  백업: {_dest_relative_label(outcome.backup, dest)}", file=sys.stderr)
        return 1
    print(f"✓ 어댑터 config 수용: {relpath} "
          f"(백업 {_dest_relative_label(outcome.backup, dest)})")
    print("  원장 기록까지 확인했다 — 다음 동기부터 무편집 판정(자동 갱신 궤도)이다.")
    note = pm_import_mod.ADAPTER_CONFIG_REAPPROVAL_NOTE.get(relpath)
    if note:
        print(f"  ⚠️ {note}")
    ctx_files = getattr(pm_import_mod, "HARNESS_CTX_FILES", None)
    if ctx_files is None:
        _warn_engine_downgrade(
            "사용 한도·압축 비율 반영",
            "하네스 설정 파일이면 관리 키가 local.conf 값이 아니라 템플릿 값으로 남는다")
        return 0
    if relpath in ctx_files.values():
        # 템플릿 값으로 돌아간 관리 키에 local.conf 의 사용 한도를 다시 쓴다(원장이 방금 증언한
        #   파일이라 반영 뒤에도 무편집 판정이 유지된다). 설치·add-harness 와 같은 한 번이다 —
        #   옛 키가 남은 conf 면 반영을 건너뛰고 알리며, 검사 거부만 rc 1(설정 파일 무변경)이다.
        try:
            pm_import_mod._print_harness_ctx_apply(dest)
        except pm_import_mod.HarnessCtxError as exc:
            print(f"[중단] {exc}", file=sys.stderr)
            return 1
    return 0


# 세트 수용에서 빼는 판정 → 제외 사유(출력 문구). 여기 없는 판정만 세트가 받는다 —
# 실질적으로 `unedited`(원장으로 무편집이 확인된 파일) 하나다.
_ADAPTER_SET_ACCEPT_EXCLUDED = {
    "edited": "채택자 편집분",
    "unrecorded": "원장 부재로 편집 여부 판정 불가",
}


def _warn_hook_set_query_fallback(pm_import_mod, generation) -> None:
    """조회 축 강등 사유 표면화 — 상류 선언을 못 읽어 설치본 세대로 판정했다는 사실을 남긴다.

    조회 축은 관대 계약이라 차단하지 않는다(판정을 통째로 잃는 것보다 한 세대 뒤 선언으로라도 보는
    편이 낫다). 다만 사유까지 버리면 `--check` 가 **상류 전용 플래그를 못 본 채** 무경고 green 이라,
    채택자는 강등된 판정을 정상 판정으로 읽는다 — 차단이 아니라 침묵만 제거한다(mutation 축의
    fail-closed 와 대비).

    **문구는 pm_import 단일 진실**(`hook_set_query_fallback_lines` — `hook_set_remedy_lines` 와 같은
    규약)이다. 그 함수가 없는 세대 사본이면 조용히 건너뛴다(그 세대엔 이 안내 자체가 없다)."""
    lines = getattr(pm_import_mod, "hook_set_query_fallback_lines", None)
    if lines is None:
        return
    for line in lines(generation):
        print(line, file=sys.stderr)


def _report_hook_set_findings(pm_import_mod, dest: Path, source_root: Path) -> bool:
    """훅 세트 세대 불일치를 stderr 로 내고 하나라도 있으면 True (`--check` 소비·write 0).

    판정·처방은 pm_import 단일 진실을 그대로 쓴다(pm-update 와 같은 문구). 선언은 **상류 세대**를
    우선한다(같은 해소 지점) — 조회 성격이라 상류를 못 읽으면 설치본 선언으로 내려가되 **그 사유는
    알린다**(무경고 green 금지). 판정 함수가 없는 구형 사본이면 조용히 건너뛴다 — 강제할 선언
    자체가 없는 세대다."""
    judge = getattr(pm_import_mod, "judge_adapter_hook_sets", None)
    if judge is None:
        return False
    resolve = getattr(pm_import_mod, "hook_set_declarations", None)
    if not _accepts_kwarg(judge, "declarations") or resolve is None:
        # 구세대 사본 — 판정은 하되(그 세대가 아는 만큼) 상류 선언은 못 태운다.
        _warn_engine_downgrade(
            "훅 세트 상류 선언", "이번 상류가 새로 들여오는 플래그·묶음은 판정되지 않는다")
        findings = judge(dest, source_root)
    else:
        generation = resolve(source_root)
        _warn_hook_set_query_fallback(pm_import_mod, generation)
        findings = judge(dest, source_root, declarations=generation.declarations)
    for finding in findings:
        print(f"[중단] 어댑터 훅 세트 세대 불일치({finding.harness}) — {finding.detail}.",
              file=sys.stderr)
        for line in pm_import_mod.hook_set_remedy_lines(finding):
            print(f"    → {line}", file=sys.stderr)
    return bool(findings)


def _accept_adapter_config_set(pm_import_mod, dest: Path, source_root: Path,
                               judgments) -> int:
    """세트 수용 — 대상 선정·선-표시·파일당 수용 (`--accept-all`).

    선정 규칙 둘:
      - 이미 상류와 같은 파일은 후보에서 빠진다(바꿀 게 없다·byte churn 0).
      - **채택자 값이 걸린 판정은 기본 제외**한다. report 모드 대상(settings.json·
         opencode.jsonc·config.toml)에는 권한 allowlist·모델·threshold 같은 실 노브가 들어 있고,
         세트 커맨드 한 번이 그것을 무지목 일괄 교체하면 "채택자 커스텀은 안 덮는다" 하한선이
         깨진다. 제외 대상은 둘이다 — `edited`(편집이 확인됨)와 `unrecorded`(원장이 없어 편집
         여부를 **판정할 수 없음**). 후자를 받으면 원장 도입 전 구세대 채택자의 커스텀이 조용히
         사라진다(모르면 덮지 않는다). 둘 다 단건 `--accept <경로>` 로만 받는다(제외 사유를 함께
         낸다). 결국 세트가 받는 건 `unedited` — 설치가 내려놓은 그대로임이 원장으로 확인된
         파일뿐이다.

    무엇을 건드릴지 **먼저 전부 보이고** 나서 파일당 수용한다. 파일 1개 단위는 기존 원자 교체를
    그대로 재사용하고 다중 파일 롤백은 만들지 않는다 — 순서 게이트 + per-file 원자로 관측
    클래스가 닫히므로 상태 기계는 과설계다."""
    candidates = [item for item in judgments if item.status != "in-sync"]
    excluded = [item for item in candidates
                if item.status in _ADAPTER_SET_ACCEPT_EXCLUDED]
    targets = [item for item in candidates
               if item.status not in _ADAPTER_SET_ACCEPT_EXCLUDED]
    for item in excluded:
        print(f"→ 세트 수용 제외({_ADAPTER_SET_ACCEPT_EXCLUDED[item.status]}): {item.relpath} · "
              f"{item.harness} · {item.mode} "
              f"— 받으려면 `{repo_protection._FACADE_PROG} sync-adapter-config --accept {item.relpath}`")
    if not targets:
        print("어댑터 config 세트 수용 대상 없음 "
              + ("(편집분 제외 후 남은 대상 없음)." if excluded else "(전부 상류와 동일)."))
        return 1 if excluded else 0
    print(f"# 세트 수용 대상 {len(targets)}건 (소스: {source_root})")
    for item in targets:
        print(f"  - {item.relpath} · {item.harness} · {item.mode} · "
              f"{_adapter_config_label(item, pm_import_mod)}")
    failed = 0
    for item in targets:
        if _accept_adapter_config_one(
                pm_import_mod, dest, source_root, item.relpath, judgments) != 0:
            failed += 1
    print(f"# 세트 수용 결과: {len(targets) - failed}/{len(targets)} 수용"
          + (f" · {failed} 실패/거부" if failed else "")
          + (f" · {len(excluded)} 편집분 제외" if excluded else ""))
    return 1 if (failed or excluded) else 0


def _adapter_config_label(judgment, pm_import_mod) -> str:
    """판정 → 표시 라벨 (mode 와 status 를 함께 본다·mode 상수는 엔진 소유)."""
    common = _ADAPTER_CONFIG_STATUS_LABEL.get(judgment.status, judgment.status)
    if judgment.mode == pm_import_mod.ADAPTER_CONFIG_REPORT:
        return _ADAPTER_CONFIG_REPORT_LABEL.get(judgment.status, common)
    return common


def cmd_sync_adapter_config(
    args: argparse.Namespace,
    *,
    pm_import=None,
    dest_root: Path | None = None,
) -> int:
    """`sync-adapter-config [--list | --check | --accept <relpath> | --accept-all]` — config 채널.

    이 파일들(`.codex/hooks.json`·`config.toml`·`.claude/settings.json`·`.opencode/opencode.jsonc`)
    은 manifest 밖이라 pm-update 가 절대 덮지 않는다. 동기는 무편집(원장 일치)인 managed 대상만
    자동 갱신하고 나머지는 보존+보고한다 — `--accept`(단건)·`--accept-all`(세트)가 그 보존분을
    채택자가 **명시적으로** 받는 유일한 채널이다(이게 없으면 원장 도입 전 채택자가 영구 보고
    모드에 갇힌다). 두 경로 모두 수용 전에 훅 세트 선행조건을 같은 게이트로 검사한다
    (`_adapter_accept_order_blocked` — 엔진 파일 선행 · config 후행).

    판정·수용 로직은 pm_import 단일 진실(`judge_adapter_configs`·`accept_adapter_config`)이고
    여기선 표시와 rc 번역만 한다. dest 해소는 pm_config 관례(REPO 앵커·cmd_add_harness 동형),
    소스 해소는 add-harness 와 같은 규칙(`--from` > local.conf upstream.path(경로) > dest
    자기전환 > local.conf upstream.path(URL→로컬 캐시 clone/fetch))이다.

    ``--check``의 인스턴스-소유 세대 요약은 경로별 기준 원장을 따른다. 동기가
    ``upstream.rev``를 전진시키는 경로는 직후 check에서 사라질 수 있지만, report-drift·
    edited managed 파일은 보존된 파일별 ``template_rev``를 쓰므로 명시적 ``--accept``
    전까지 매 check에 반복된다. 백업/git을 대신하는 durable backstop은 아니다.
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print(
            "[중단] pm_import.py 엔진을 찾을 수 없다 — 어댑터 config 판정 불가 "
            f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))
    dest = dest_root if dest_root is not None else REPO
    try:
        source_root = pm_import_mod.resolve_adapter_config_source(
            dest, getattr(args, "source", None))
    except FileNotFoundError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))
    except pm_import_mod.UpstreamCacheError as exc:
        # URL upstream 캐시 clone/fetch 실패 — 옛 캐시로 진행하지 않고 판정 자체를 멈춘다.
        print(f"[중단] {exc}", file=sys.stderr)
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))

    # 완료 게이트가 보는 같은 source checkout에서 instance-owned 전량(진입문서 ``none`` 포함)의
    # 세대 델타도 읽기 전용으로 노출한다. 판정 실패는 config 수렴 rc와 섞지 않는 advisory지만,
    # 변경 없음으로 접지 않고 전량 확인을 명시한다.
    if getattr(args, "check", False):
        try:
            delta_lines = pm_import_mod.instance_owned_template_delta_lines(
                dest, source_root)
        except Exception as exc:  # noqa: BLE001 — 구/부분 엔진도 check 본류는 계속한다.
            if _is_engine_rev_skew(exc):
                raise
            delta_lines = [
                "⚠️  인스턴스 소유 템플릿 세대 판정 unavailable — "
                f"{exc} · 판정 불가(변경 없음 아님)·전량 확인 권장."
            ]
        for line in delta_lines:
            print(line)

    try:
        judgments = pm_import_mod.judge_adapter_configs(dest, source_root)
    except Exception as exc:  # noqa: BLE001 — 판정 채널 unavailable은 완료 게이트 red.
        if _is_engine_rev_skew(exc):
            raise
        print(f"[중단] 어댑터 config 판정 채널 unavailable: {exc}", file=sys.stderr)
        print(
            "  → `--from <framework checkout>`이 해당 managed template을 포함한 올바른 "
            "source인지 확인하고 pm-update 후 다시 검사하라.",
            file=sys.stderr,
        )
        return _finish_sync_adapter_config(1, check=getattr(args, "check", False))
    accept = getattr(args, "accept", None)
    if accept:
        return _finish_sync_adapter_config(
            _accept_adapter_config_one(
                pm_import_mod, dest, source_root, accept, judgments),
            check=getattr(args, "check", False),
        )
    if getattr(args, "accept_all", False):
        return _finish_sync_adapter_config(
            _accept_adapter_config_set(
                pm_import_mod, dest, source_root, judgments),
            check=getattr(args, "check", False),
        )

    if not judgments:
        print("어댑터 config 채널 대상 없음 (설치 하네스의 config 가 없거나 소스에 template 부재).")
        # config 채널 대상이 0 이어도 훅 세트는 따로 볼 수 있다(설치된 훅 + 채택자 settings 는
        #   실재). 여기서 조용히 green 을 내면 게이트 표면이 통째로 우회된다.
        if getattr(args, "check", False):
            return _finish_sync_adapter_config(
                1 if _report_hook_set_findings(pm_import_mod, dest, source_root) else 0,
                check=True,
            )
        return _finish_sync_adapter_config(0, check=False)
    print(f"# 어댑터 config 판정 (소스: {source_root})")
    for judgment in judgments:
        label = _adapter_config_label(judgment, pm_import_mod)
        print(f"  - {judgment.relpath} · {judgment.harness} · {judgment.mode} · {label}")
    if getattr(args, "check", False):
        # `--check`는 판정만 읽는다(write 0). 완료 판정은 pm_import helper가 소유하므로
        # pm-update와 이 CLI가 상태를 서로 다르게 해석할 수 없다.
        # 훅 세트 세대도 **같은 자리에서** 본다 — 스킬 카드가 이 커맨드를 완료 게이트로 지목하는데
        #   pm-update 가 rc1 로 막는 클래스를 여기서 green 으로 통과시키면 게이트가 갈린다.
        hook_set_failed = _report_hook_set_findings(pm_import_mod, dest, source_root)
        unconverged = pm_import_mod.unconverged_managed_adapter_configs(judgments)
        if not unconverged:
            if hook_set_failed:
                return _finish_sync_adapter_config(1, check=True)
            print("✓ managed 어댑터 config 수렴 확인 (template 내용 + 원장 dest hash).")
            return _finish_sync_adapter_config(0, check=True)
        print("[중단] managed 어댑터 config 미수렴:", file=sys.stderr)
        for judgment, status in unconverged:
            print(f"  - {judgment.relpath}: {status}", file=sys.stderr)
            if status == "unedited":
                print(
                    "    → `pm-update`를 재실행해 백업·자동 갱신·원장화를 완료하라.",
                    file=sys.stderr,
                )
            elif status == "unrecorded" and judgment.status == "in-sync":
                print(
                    "    → 실 `pm-update`를 재실행하면 파일 byte 변경 없이 원장을 backfill한다.",
                    file=sys.stderr,
                )
            else:
                print(
                    f"    → `{repo_protection._FACADE_PROG} sync-adapter-config --accept "
                    f"{judgment.relpath}` (백업 후 상류 값 수용·원장화).",
                    file=sys.stderr,
                )
        return _finish_sync_adapter_config(1, check=True)
    # 세트 수용도 여기서 병기한다 — 단건만 노출하면 무편집분이 여러 개인 채택자가 세트 커맨드의
    #   존재를 모른 채 한 파일씩 수용한다(발견성). 제외 규칙은 세트 커맨드가 스스로 알린다.
    print(f"  수용(백업 후 상류 값 채택): {repo_protection._FACADE_PROG} sync-adapter-config --accept <경로> "
          "· 무편집분 일괄은 --accept-all (Windows 는 `.\\pm-config.cmd`)")
    return _finish_sync_adapter_config(0, check=False)


def _finish_sync_adapter_config(rc: int, *, check: bool) -> int:
    """``--check`` 말미에 경로별 세대 요약 수명을 명시한다."""
    if check:
        print("세대 요약은 기준 원장별 수명 — upstream.rev 전진분은 이후 사라질 수 있고, "
              "보존·미수용 managed 파일은 --accept 전까지 매 검사 반복; "
              "지난 세대는 백업/git 로 확인")
    return rc


def _dest_relative_label(path, dest_root) -> str:
    """dest 기준 상대 표기 — 밖이면 절대 경로 그대로(표시가 죽지 않게)."""
    try:
        return Path(path).relative_to(Path(dest_root)).as_posix()
    except ValueError:
        return str(path)


# ── 대화형 콘솔 ──────────────────────────────────────────────────────
# 무인자(tty) `pm-config` 의 휴먼 프론트엔드. 상태를 렌더하고 메뉴로 액션을 받고
# 입력마다 바뀐 상태를 재렌더한다. 액션은 모두 *기존 핸들러*(cmd_repo_add·
# cmd_worktree_add·cmd_set_test_cmd)로 위임한다 — 콘솔은 얇은 셸(동작 분기 0·중복 0).
# 커맨드형 CLI 와 공존(같은 핸들러). 엔진/입력은 DI seam → hermetic 테스트.


def _render_repos() -> None:
    """areas.md per-repo 레지스트리 행을 surface 한다 (`areas_store._parse_areas` 재사용).

    areas.md 부재(미배선)면 빈 안내. 중복 파싱 구현 0 — 헤더-인식 파서를 그대로 쓴다.
    """
    print("## repos (areas.md per-repo 레지스트리):")
    _header, rows = areas_store._parse_areas(pm_paths.home_at(REPO))
    if not rows:
        print("  (등록된 repo 없음 — [r] 로 추가하라)")
        return
    for row in rows:
        name = row.get("repo") or row.get("prefix") or "(?)"
        print(
            f"  - {name} · prefix={row.get('prefix') or '-'} · "
            f"git={row.get('git') or '-'} · test_cmd={row.get('test_cmd') or '(미지정)'} · "
            f"owner={row.get('owner') or '-'} · base={row.get('base') or '(bare HEAD)'}"
        )


def _render_slots() -> None:
    """worktree 풀 리스 장부를 slot·repo·build(test_cmd)·state·session 으로 surface.

    pool_store.list_leases() 재사용. wp 가 없으면 안내만(크래시 0). cmd_status 의 풀
    surface 와 같은 데이터 — 콘솔은 build(test_cmd) 칼럼을 강조한다(이 콘솔의 1급 관심사).
    """
    print("## slots (worktree 풀 리스):")
    leases = pool_store.list_leases(pm_paths.home_at(REPO))
    if not leases:
        print("  (슬롯 없음 — [w] 로 추가하라)")
        return
    for l in leases:
        print(
            f"  - {l.slot} · repo={l.repo} · build={l.test_cmd or '(미지정)'} · "
            f"state={l.state} · session={l.session or '-'}"
        )


def _render_state() -> None:
    """콘솔 상태 1회 렌더 — repos(areas) + slots(리스). 액션마다 재호출(재렌더)."""
    print()
    print(f"# pm-config 콘솔 — 세션: {_default_session() or '(비바인딩)'}")
    _render_repos()
    _render_slots()


_CONSOLE_MENU = (
    "\n메뉴: [r] repo 추가 · [w] worktree 추가 · [b] 슬롯 빌드명령 설정/변경 · "
    "[m] 위임 모델 안내 · [u] 엔진 갱신 · [s] 새로고침 · [q] 종료"
)

# ── 위임 모델 안내 (`delegate.<role>[.hard].model` 에 무엇을 넣는가) ────────────
# 모델 **목록을 엔진에 하드코딩하지 않는다** — 모델은 자주 바뀌고 하드코딩은 곧 stale 이다.
# 조회 수단이 있는 하네스만 실조회 목록을 보여주고, 없는 하네스는 값 **형식**과 확인 방법만
# 안내한다. 값 형식이 하네스마다 근본적으로 달라 값 자체의 통일은 불가능하고, 통일 대상은
# 취급 방식(= local.conf 단일 진실)이다.
#
# 형식·확인 방법·원천의 표는 이 파일에 없다 — `contract.MODEL_SOURCE` 한 자리이고 사람 안내
# (`[m]`)·기계 조회(`delegate models`)·설정 화면 선택지가 모두 그 표를 읽는다.
# 실조회 목록은 길 수 있다 — 안내는 콘솔 한 화면을 넘기지 않는다(전량은 조회 명령으로).
_DELEGATE_MODEL_LIST_LIMIT = 12
_MODEL_ALIAS_CONF_PREFIX = "delegate.model_alias."

# 콘솔 프롬프트 중단(EOF/Ctrl-C) sentinel — `_console_input` 이 예외 대신 이걸 돌려준다.
# 호출부(메뉴 루프·각 액션)는 `is _CONSOLE_ABORT` 로 판정해 일관되게 취소/종료한다.
# `None`/`""` 과 구별돼야(그건 정상 빈입력 의미) — 고유 sentinel 객체로 둔다.
_CONSOLE_ABORT = object()


def _console_input(input_fn: Callable[[str], str], prompt: str):
    """콘솔 공유 입력 헬퍼 — `EOFError`/`KeyboardInterrupt` 를 `_CONSOLE_ABORT` 로 흡수.

    **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]` 이름/git/test·`[w]` repo·`[b]` slot/
    빌드명령)가 이 헬퍼를 거친다 — 어느 프롬프트서 Ctrl-C/EOF 가 나도 예외가 전파돼 크래시
    하는 것을 막는다(must-fix 2·codex — "우아 종료/크래시 0" 보장). 반환:
      - 정상 입력 → `str`(strip 안 함 — 호출부가 의미에 맞게 strip; 빈입력 보존).
      - EOF/Ctrl-C → `_CONSOLE_ABORT`(호출부가 액션 취소/메뉴 복귀 또는 루프 종료).
    input_fn 주입으로 hermetic(라이브 input 블록 0).
    """
    try:
        return input_fn(prompt)
    except (EOFError, KeyboardInterrupt):
        return _CONSOLE_ABORT


def _console_repo_add(input_fn, board_mod):
    """`[r]` — repo 이름/git/test/base 를 프롬프트로 받아 cmd_repo_add 위임 (기존 핸들러 재사용).

    각 프롬프트가 `_console_input` 을 거쳐 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 를 받는다 —
    그 경우 이 액션 자체가 `_CONSOLE_ABORT` 를 *반환*해 run_console 이 루프를 우아 종료한다
    (must-fix 2 — 액션 내부 중단도 traceback 0·rc 0). 빈입력은 취소(None 반환·메뉴 복귀).
    """
    name = _console_input(input_fn, "repo 이름 (= prefix): ")
    if name is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    name = name.strip()
    if not name:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    git = _console_input(input_fn, "git URL: ")
    if git is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    git = git.strip()
    if not git:
        print("  (git URL 비어 있음 — 취소)")
        return None
    test = _console_input(
        input_fn, "test 명령 (빈 입력 = 미지정·나중에 worktree/[b] 에서 설정): "
    )
    if test is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    test = test.strip()
    base = _console_input(input_fn, "base 브랜치 (빈 입력 = 기본 브랜치 사용): ")
    if base is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    base = base.strip()
    # 빈입력 → base=None(= CLI `--base` 생략 = bare HEAD 기본·기존 동작). 비어있지 않으면
    # 그 브랜치명을 cmd_repo_add 로 — 형식/존재 검증은 콘솔이 따로 안 하고 `_resolve_base`
    # 의 `show-ref --verify`단일 sink 가 거른다(중복 검사 0).
    args = argparse.Namespace(
        name=name, git=git, test=(test or None), owner=None, base=(base or None),
        user=None,  # area_owner 는 cmd_repo_add 가 local.conf identity.user= / git email 로 해소.
    )
    cmd_repo_add(args, board=board_mod)
    return None


def _console_worktree_add(input_fn, board_mod=None):
    """`[w]` — repo 를 받아 cmd_worktree_add 위임. 빌드명령은 그 핸들러가 프롬프트(tty 경로).

    repo 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). 빈입력은 취소.
    물리 슬롯 생성 전에 repo 이름을 한 번 더 입력하는 사용자-present 확인 프롬프트를
    띄우고, 정확히 일치한 값만 CLI ``--user-ack <repo>``와 동치인 토큰으로 넘긴다.
    board_mod 를 cmd_worktree_add 에 전달해 빌드명령 프롬프트의 표시 기본값(areas 해소)을
    콘솔이 이미 로드한 board 로 재사용한다(중복 로드 0). 빌드명령 프롬프트 내부 중단은
    cmd_worktree_add → _prompt_test_cmd 가 None 으로 흡수(크래시 0·기존 폴백).

    이 액션은 핸들러 rc 를 읽지 않는다(취소/중단 sentinel 만 반환) — 그래서 엔진 사본 불일치
    (marked engine skew)는 rc 가 아니라 예외로 올라와야 루프가 끝난다. cmd_worktree_add 가
    그 경계를 재전파하고 `main` 이 CLI 와 같은 문구·rc 로 번역한다.
    """
    repo = _console_input(input_fn, "슬롯을 만들 repo 이름: ")
    if repo is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    repo = repo.strip()
    if not repo:
        print("  (repo 이름 비어 있음 — 취소)")
        return None
    confirmation = _console_input(
        input_fn,
        f"사용자 승인 확인 — 생성할 repo 이름 {repo!r}을 다시 입력: ",
    )
    if confirmation is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    if confirmation.strip() != repo:
        print("  [중단] repo 재입력이 일치하지 않아 슬롯 생성을 취소했습니다.")
        return None
    # 콘솔은 항상 대화형(tty 전제) → cmd_worktree_add 가 빌드명령 프롬프트를 띄우게
    # is_tty=lambda: True 로 강제(콘솔 진입 자체가 tty 보장·main 분기). --test 는 미지정.
    args = argparse.Namespace(repo=repo, test=None, user_ack=repo)
    cmd_worktree_add(
        args, board=board_mod, input_fn=input_fn, is_tty=lambda: True
    )
    return None


def _console_set_test_cmd(input_fn):
    """`[b]` — slot·새 빌드명령을 받아 cmd_set_test_cmd 위임 (worktree_pool.set_test_cmd).

    slot·빌드명령 프롬프트가 EOF/Ctrl-C 면 `_CONSOLE_ABORT` 반환(루프 우아 종료). slot 빈입력은
    취소. 빌드명령 빈입력은 None(바인딩 해제·현행).
    """
    slot = _console_input(input_fn, "빌드명령을 바꿀 슬롯 (work/<repo>_<N>): ")
    if slot is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    slot = slot.strip()
    if not slot:
        print("  (슬롯 비어 있음 — 취소)")
        return None
    cmd = _console_input(input_fn, "새 빌드/테스트 명령 (빈 입력 = 바인딩 해제): ")
    if cmd is _CONSOLE_ABORT:
        return _CONSOLE_ABORT
    cmd = cmd.strip()
    cmd_set_test_cmd(slot, cmd or None)
    return None


def _installed_harnesses(pm_import_mod) -> list[str]:
    """등록 하네스 중 **이 환경에 설치된** 것만 (registry 순서 보존).

    설치되지 않은 하네스의 모델을 안내하지 않는다 — 채택자가 쓸 수 없는 값을 고르게 만드는
    안내는 안내가 아니다. 판정은 `pm_import._harness_binary_available`(shutil.which) 재사용이라
    새 감지 수단을 만들지 않는다.
    """
    return [
        harness
        for harness in pm_import_mod.REGISTERED_HARNESSES
        if pm_import_mod._harness_binary_available(harness)
    ]


def _model_alias_members(conf: dict) -> dict[str, list[str]]:
    """`delegate.model_alias.<name>=m1, m2` → {alias: [members]} (선언 없으면 빈 dict).

    alias 표의 **의미는 바꾸지 않는다** — 같은 기반 모델의 묶음이고(lint `delegate-same-model`
    판정용), 안내는 그 멤버를 "이 환경에서 쓰는 모델" 로 보여줄 뿐이다. 목록 선언용으로 alias
    를 강제하지 않는다(한 모델만 쓰는 채택자가 목록 때문에 alias 를 만들 이유가 없어야 한다).
    """
    members: dict[str, list[str]] = {}
    for key, raw in conf.items():
        if not key.startswith(_MODEL_ALIAS_CONF_PREFIX):
            continue
        name = key[len(_MODEL_ALIAS_CONF_PREFIX):].strip()
        values = [part.strip() for part in str(raw).split(",") if part.strip()]
        if name and values:
            members[name] = values
    return members


def _print_delegate_model_guidance(
    *,
    pm_import=None,
    conf=None,
) -> int:
    """`[m]` — 설치된 하네스별로 `delegate.<role>[.hard].model` 에 넣을 값을 안내한다.

    키 형식은 문서에 있지만 **유효한 값의 목록·확인 방법**이 어디에도 없었다. 위임 시점 검증
    함수는 만들지 않는다(codex 값은 자유 문자열이라 형식 검증이 공허하고, alias 미선언 채택자에선
    멤버십 검증이 아무것도 잡지 않는다) — 이 비대칭은 안내로 닫는다.

    조회 실패(바이너리 부재·rc·timeout·파싱 0)는 형식 안내로 **강등**하되 조용하지 않다:
    강등 사실을 그 자리에 적고, 실패 사유 자체는 조회 seam 이 stderr 한 줄로 남긴다.
    pm_import·conf 주입으로 hermetic 테스트(라이브 CLI 미구동).
    """
    pm_import_mod = pm_import or _load_module("pm_import", "pm_import.py")
    if pm_import_mod is None:
        print(
            "[중단] pm_import.py 엔진을 찾을 수 없다 — 모델 안내 불가 "
            f"({TOOLS_DIR / 'pm_import.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    model_source = contract.MODEL_SOURCE

    print("\n## 위임 모델 안내 (local.conf `delegate.<role>[.hard].model` 단일 진실):")
    installed = _installed_harnesses(pm_import_mod)
    if not installed:
        print("  (설치된 하네스 없음 — 안내할 대상이 없다. 하네스를 설치한 뒤 다시 보라.)")
    for harness in installed:
        source = model_source[harness]
        value_format, how_to_check = source.value_format, source.how_to_check
        print(f"  - {harness}: 값 형식 {value_format} · 확인 {how_to_check}")
        if source.kind != "live":
            continue
        runner = pm_import_mod._real_models_runner
        ok, models = runner(source)
        if not ok or not models:
            print(
                "      ⚠ 목록 조회 실패 — 위 형식 안내로 강등한다(사유는 위 stderr 한 줄). "
                f"직접 확인: {how_to_check}"
            )
            continue
        shown = models[:_DELEGATE_MODEL_LIST_LIMIT]
        print(f"      가용 모델 {len(models)}개: {', '.join(shown)}")
        if len(models) > len(shown):
            print(f"      (앞 {len(shown)}개만 표시 — 전량은 {how_to_check})")

    values = (conf if conf is not None
              else conf_store.load_checked(REPO / ".project_manager" / "local.conf"))
    aliases = _model_alias_members(values or {})
    if aliases:
        print("  이 환경에서 쓰는 모델 (`delegate.model_alias` 선언 멤버):")
        for name in sorted(aliases):
            print(f"    - {name}: {', '.join(aliases[name])}")
    else:
        print(
            "  (`delegate.model_alias` 선언 없음 — 같은 기반 모델의 다른 표기를 묶을 때만 "
            "선언한다. 목록을 위해 만들 필요는 없다.)"
        )
    return 0


def _console_delegate_models() -> None:
    """`[m]` 콘솔 액션 — 모델 안내를 출력만 한다(장부 변경 0)."""
    _print_delegate_model_guidance()
    return None


# ── 모델 목록 조회 (`delegate models --harness <h> --json`) ────────────────────
# 설정 화면과 사람이 "이 하네스에 무엇을 적나" 를 받는 **읽기 전용** 자리다. 조회는 `contract.model_listing`
# 하나이고(하네스 이름 분기 0) 응답은 `{"harness", "kind", "models", "variants"}` 네 필드다. 조회는 파일을
# 쓰지 않고, 실패를 빈 목록으로 위장하지 않는다 — 실패(0개 포함)는 rc 3 이다.
_DELEGATE_MODELS_QUERY_FAILED = 3


def _conf_assignment(text: str) -> tuple[str, str]:
    """`--set 키=값` 한 개 → (키, 값). 빈 값(`키=`)은 그 줄을 지우라는 뜻이다."""
    key, separator, value = text.partition("=")
    if not separator or not key.strip():
        raise argparse.ArgumentTypeError(f"`키=값` 꼴이 아니다: {text!r}")
    return key.strip(), value.strip()


# 반영 전 거부 — 처리하지 못한 예외로 끝난 파이썬의 1 과 argparse 인자 오류의 2 와 겹치지 않는 전용
# 값이다. 관리자 저장은 이 값만 "잘못된 값"(`invalid-value`)으로 보인다
# (`manager/bridge_settings.py` 의 `APPLY_REFUSED_RC`).
_APPLY_REFUSED = 3


def cmd_apply(args, *, pm_update=None, dest_root: Path | None = None) -> int:
    """`apply [--json] [--set 키=값 ...]` — 값 검사 → local.conf 기록 → 파생 파일 반영.

    설정 저장의 유일한 입구다(관리자 저장·[기본값으로] 포함). `--set` 이 있으면 바뀐 키가 걸리는 반영
    단계만, 없으면 모든 단계를 다시 돈다(손으로 고친 local.conf 를 반영할 때). 단계는 사용 한도(하네스
    설정 파일·압축 한도 기록)와 렌더 입력(`@render` 산출물을 설치 세대 원본으로 다시 그림) 둘이고,
    어느 묶음에도 없는 키는 local.conf 기록만 한다(`pm_update.apply_conf`).

    rc 0 반영(바뀐 줄 0 포함) · 3 반영 전 거부(값 검사·모델 한도 조회·렌더 검사·렌더 원본 세대·옛 키 —
    local.conf·파생 파일 무변경 · 사유는 stderr) · 1 그 밖의 실패(엔진·local.conf 부재, 쓰기 단계 실패,
    처리하지 못한 예외 — 쓰기 단계에서 멈추면 stderr 가 교체한 파일을 적는다) · 2 인자 오류(argparse).
    `--json` 은 한 줄 `{"changed": local.conf 가 바뀌었나, "applied": 반영 줄}` 이다."""
    pm_update_mod = pm_update or _load_module("pm_update", "pm_update.py")
    if pm_update_mod is None:
        print(
            "[중단] pm_update.py 엔진을 찾을 수 없다 — 설정 반영 불가 "
            f"({TOOLS_DIR / 'pm_update.py'} 부재 또는 로드 실패).",
            file=sys.stderr,
        )
        return 1
    dest = dest_root if dest_root is not None else REPO
    conf_file = dest / ".project_manager" / "local.conf"
    updates = dict(getattr(args, "set", None) or ())
    if updates and not conf_file.is_file():
        print(f"[중단] local.conf 없음 ({conf_file}) — 먼저 `pm-config init` 으로 셋업하라.",
              file=sys.stderr)
        return 1
    pm_import_mod = pm_update_mod._load_pm_import()
    try:
        result = pm_update_mod.apply_conf(
            dest, updates={key: value or None for key, value in updates.items()},
            opencode_config_dir=pm_import_mod.opencode_global_config_dir(os.environ, Path.home()))
    except pm_update_mod.ConfApplyError as exc:
        print(f"[중단] {exc}", file=sys.stderr)
        return 1 if exc.write_stage else _APPLY_REFUSED
    except RuntimeError as exc:
        if _is_engine_rev_skew(exc):
            raise
        # 옛 키가 남은 conf(또는 `--set` 이 옛 키) — 값을 읽는 지점의 fail-loud 를 반영 전 거부로 낸다.
        if not getattr(exc, "_legacy_conf_key", False):
            raise
        print(f"[중단] {exc}", file=sys.stderr)
        return _APPLY_REFUSED
    if getattr(args, "json", False):
        _write_machine_line(json.dumps(
            {"changed": result.changed, "applied": list(result.applied)}, ensure_ascii=False))
        return 0
    if result.changed:
        print(f"✓ local.conf 기록: {', '.join(updates)}")
    for line in result.applied:
        print(f"✓ 반영: {line}")
    if not result.applied:
        print("파생 파일: 바뀐 줄 없음 (이미 local.conf 값과 같다).")
    return 0


def cmd_delegate_models(args, *, listing=None) -> int:
    """`delegate models --harness <h> --json` — 그 하네스가 인정하는 모델 목록(읽기 전용).

    응답은 `{"harness", "kind", "models", "variants"}` 하나다. `variants` 는 모델별 추론 강도
    (`{모델: [variant…]}`, variant 없는 모델은 `[]`)이고 그것을 내는 원천(opencode)만 채운다 — 나머지는
    null. 모델 목록과 같은 한 번의 조회에서 나온다. `free_text`(모델은 검증상 언제나 자유 문자열)는
    싣지 않는다 — 그 사실은 키 레지스트리의 `choices.free_text` 가 소유하고, 두 자리에 두면 갈린다.
    `listing` 은 조회 함수(원천 행 → `ModelListing`)이고 기본은 `contract.model_listing` 을 이 사본
    폴더·이 프로세스 env 로 부르는 것이다.
    rc: 0 성공 · 2 `--harness`/`--json` 미지정·미지원 · 3 조회 실패(사유는 stderr 한 줄).
    """
    table = contract.MODEL_SOURCE
    source = table.get(args.harness)
    if source is None:
        print(f"[중단] 미지원 harness {args.harness!r} — 지원: {', '.join(table)}.",
              file=sys.stderr)
        return 2
    lookup = listing if listing is not None else (
        lambda row: contract.model_listing(row, cwd=REPO, env=dict(os.environ)))
    try:
        result = lookup(source)
    except contract.HarnessContractError as exc:
        print(f"[중단] {args.harness} 모델 목록을 얻지 못했다 — {exc}", file=sys.stderr)
        return _DELEGATE_MODELS_QUERY_FAILED
    variants = None if result.variants is None else {
        model: list(names) for model, names in result.variants.items()}
    _write_machine_line(json.dumps(
        {"harness": args.harness, "kind": source.kind, "models": list(result.models),
         "variants": variants},
        ensure_ascii=False))
    return 0


def _console_update() -> None:
    """`[u]` — 엔진 갱신을 `cmd_update([])` 로 위임 (= pm_update.main verbatim).

    `[r]`/`[w]`/`[b]` 와 동형의 얇은 래퍼 — 입력 프롬프트 없이(인자 없는 갱신) 기존 핸들러로
    위임만 한다(중복 0). 갱신은 repos/slots 장부를 안 바꿔도(엔진 파일 동기화) 호출부가
    상태를 재렌더한다(무해·일관). pm_update 주입으로 hermetic(실 동기화 없이 배선 검증).
    """
    cmd_update([])
    return None


def run_console(
    *,
    input_fn: Callable[[str], str] = input,
    board=None,
) -> int:
    """대화형 콘솔 루프 — 상태 렌더 + 메뉴 + 액션 + 재렌더 (tty 전용).

    무인자 `pm-config`(tty)가 진입한다(비-tty 는 main 이 help 로 분기). 흐름:
      1. 상태 렌더(repos via areas · slots via 리스) — cmd_status/list_leases/areas 파서 재사용.
      2. 메뉴 프롬프트 → 키 1자.
      3. 액션(`[r]`·`[w]`·`[b]`·`[m]`·`[u]`)은 *기존 핸들러*로 위임 → 상태 재렌더.
      4. `[s]` 새로고침(재렌더만)·`[q]` 종료. (`[u]` 엔진 갱신 = cmd_update.)

    입력 견고성: 빈 입력/오타 메뉴키 → 재프롬프트(크래시 0)·`EOFError`/`KeyboardInterrupt`
    → 우아 종료(메시지 + rc 0). **메뉴 입력뿐 아니라 모든 액션 내부 프롬프트**(`[r]`·`[w]`·
    `[b]` 의 이름/git/repo/slot 입력)가 공유 `_console_input` 헬퍼를 거친다 — 어느 프롬프트서
    중단해도 액션이 `_CONSOLE_ABORT` 를 반환하고 루프가 우아 종료한다(must-fix 2·codex —
    "우아 종료/크래시 0" 보장·traceback 0·rc 0). 엔진(board·worktree_pool)/입력(input_fn)
    주입으로 hermetic 테스트(실 clone/worktree·라이브 input 블록 0 — 입력 시퀀스 주입 + 핸들러 mock).
    """
    board_mod = board or _load_module("board", "board.py")

    _render_state()
    while True:
        print(_CONSOLE_MENU)
        # 메뉴 선택도 액션 프롬프트와 *같은* 공유 헬퍼로 통일 — 중단(EOF/Ctrl-C) → 우아 종료.
        choice = _console_input(input_fn, "선택: ")
        if choice is _CONSOLE_ABORT:
            print("\n콘솔 종료.")
            return 0
        choice = choice.strip().lower()

        if choice == "q":
            print("콘솔 종료.")
            return 0
        if choice == "s":
            _render_state()
            continue
        if choice == "r":
            result = _console_repo_add(input_fn, board_mod)
        elif choice == "w":
            result = _console_worktree_add(input_fn, board_mod)
        elif choice == "b":
            result = _console_set_test_cmd(input_fn)
        elif choice == "m":
            # 위임 모델 안내 — 출력 전용(장부 변경 0). 입력 프롬프트가 없어 중단 축이 없다.
            result = _console_delegate_models()
        elif choice == "u":
            # 엔진 갱신— 입력 프롬프트 없이 cmd_update([]) 위임 후 재렌더.
            result = _console_update()
        else:
            # 빈 입력/오타 메뉴키 → 재프롬프트(크래시 0). 액션 안 함·상태 재렌더 안 함.
            if choice:
                print(f"  (알 수 없는 선택 {choice!r} — r/w/b/m/u/s/q 중 하나)")
            continue
        # 액션 내부 프롬프트가 중단(EOF/Ctrl-C)됐으면 _CONSOLE_ABORT 를 반환 — 루프 우아 종료
        # (must-fix 2 — 메뉴뿐 아니라 액션 프롬프트 중단도 traceback 0·rc 0).
        if result is _CONSOLE_ABORT:
            print("\n콘솔 종료.")
            return 0
        # 액션 수행 후 바뀐 상태를 재렌더(입력마다 상태 변화 확인).
        _render_state()


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """서브커맨드 디스패처 파서 (argparse subparsers·pm_update epilog 단일소스 계승)."""
    parser = argparse.ArgumentParser(
        prog="pm-config",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "각 서브커맨드는 엔진(board.py / worktree_pool.py / pm_update.py)으로 위임하는 "
            "얇은 배선이다. 슬롯 대여는 `alloc`, 반납은 `release`(`--force` 는 강제 반납)이고 "
            "부트스트랩·핸드오프는 슬롯을 빌리거나 반납하지 않는다. 슬롯에 새 브랜치는 "
            "`task branch`, 있는 브랜치로 전환은 `worktree_pool.py switch` 다."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # repo add <name> [--git <url>] [--test "<cmd>"]  (--test optional·clone 우선·--git 은 신규 repo 필수/기등록 hydrate 는 areas URL)
    p_repo = sub.add_parser("repo", help="repo 등록 관리 (add·protected·list)")
    repo_sub = p_repo.add_subparsers(dest="repo_command", metavar="<repo-command>")
    p_repo_add = repo_sub.add_parser(
        "add", help="패밀리에 repo 등록 + .repos/<name>.git bare clone"
    )
    p_repo_add.add_argument("name", help="repo 이름 (= prefix · per-repo ID 네임스페이스)")
    p_repo_add.add_argument("--git", metavar="URL", default=None,
                            help="repo git URL (bare clone 원). 미제공 시 — already-registered "
                                 "repo 는 areas.md `git` 칼럼 URL 로 hydrate(bare mirror clone·"
                                 "multi-user 2번째 사용자), 미등록 신규 repo 는 명확 에러"
                                 "(URL 필수·부작용 0).")
    p_repo_add.add_argument("--test", metavar="CMD", default=None,
                            help="per-repo 테스트 명령 (areas.md test_cmd·회귀 게이트가 worktree 에서 실행). "
                                 "미지정 시 areas test_cmd 빈 값 — 해소 체인이 슬롯/local.conf 로 폴백. "
                                 "빌드명령은 worktree add 프롬프트·콘솔 [b] 에서도 설정 가능.")
    p_repo_add.add_argument("--owner", metavar="이름", default=None,
                            help="등록 owner = registrant (기본: local.conf identity.user, "
                                 "없으면 git user.email · 둘 다 없으면 중단)")
    p_repo_add.add_argument("--user", metavar="이름", default=None,
                            help="area_owner = 그 area 의 user 소유 (`--mine` 풀 입력). "
                                 "미지정 시 local.conf identity.user= / git config user.email 로 해소(없으면 빈 값).")
    p_repo_add.add_argument("--base", metavar="BRANCH", default=None,
                            help="worktree 슬롯 브랜치가 파생될 base 브랜치 (develop 등). "
                                 "미지정 시 clone 된 bare 의 기본 브랜치(원격 default)로 해소·기록. "
                                 "지정 시 존재 검증(없는 base 거부). worktree add 가 이 base 에서 슬롯 브랜치를 판다.")
    p_repo_add.add_argument("--protected", metavar='"main,develop"', default=None,
                            help="PM 이 자율 commit/push 못 하는 보호 브랜치 목록 (쉼표분리·areas.md "
                                 "`protected` 칼럼). 미지정 시 빈 칼럼 = 기본값 폴백"
                                 "(main/master/develop). 브랜치 실재는 검증하지 않는다(아직 없는 "
                                 "`release` 선-보호가 정상·bare 에 없으면 경고 1줄). 사후 변경은 "
                                 "`repo protected <name> <목록>`.")
    p_repo_add.set_defaults(func=cmd_repo_add)

    # repo protected <name> [<목록>|default] — 보호목록 조회/설정.
    # 값 인자 유무로 get/set(`upstream show|set`·`task prefix <name> <p|none>` family 동형).
    # `default` 리터럴 = 칼럼 비움(= DEFAULT_PROTECTED 폴백·"보호 해제" 아님·빈 문자열은 거부).
    p_repo_protected = repo_sub.add_parser(
        "protected",
        help="보호 브랜치 목록 조회/설정 — 값 없으면 조회(실효값·출처·훅 sidecar 정합), "
             "값 주면 areas.md 갱신 + 훅 sidecar 정합화",
    )
    p_repo_protected.add_argument("name", help="대상 repo 이름 (areas.md 등록된 것)")
    p_repo_protected.add_argument(
        "value", nargs="?", default=None,
        help="설정할 보호목록 (쉼표분리·예 `main,release`) 또는 `default`(칼럼 비움 = "
             "main/master/develop 기본값 폴백). 생략하면 조회.",
    )
    p_repo_protected.set_defaults(func=cmd_repo_protected)

    # repo list — 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner).
    p_repo_list = repo_sub.add_parser(
        "list", help="등록 repo 표 (repo·prefix·base·protected·test_cmd·area_owner)")
    p_repo_list.set_defaults(func=cmd_repo_list)

    # worktree add <repo>
    p_wt = sub.add_parser("worktree", help="worktree 슬롯 관리 (add)")
    wt_sub = p_wt.add_subparsers(dest="worktree_command", metavar="<worktree-command>")
    p_wt_add = wt_sub.add_parser(
        "add", help="새 슬롯 생성(<repo>_<N>) + git submodule update --init"
    )
    p_wt_add.add_argument("repo", help="슬롯을 만들 repo 이름 (areas.md 등록된 것)")
    p_wt_add.add_argument(
        "--test", metavar="<cmd>", default=None,
        help="이 슬롯에 바인딩할 회귀/빌드명령 (같은 repo 슬롯별 빌드변형·HIL config). "
             "미지정 시 repo areas/local.conf 로 해소(현행).",
    )
    p_wt_add.add_argument(
        "--readonly", action="store_true",
        help="research 전용 read-only 공유 슬롯 생성 (detached HEAD·role=readonly·"
             "session 없음·배타 대여 없음). 코드를 읽어 PM 홈 wiki 를 쓰는 읽기 기준면. "
             f"갱신은 `{_runtime_skill_entry('pm-worktree')} refresh` 로만·"
             "rebase/dev/sync 는 거부.",
    )
    p_wt_add.add_argument(
        "--task", metavar="<task>", default=None,
        help="생성 직후 그 슬롯을 이 task 명의로 대여 (풀 소진 시 생성+대여 한 흐름·"
             "기바인딩 task 요구·`--readonly` 와 상호배타). 미지정이면 idle 슬롯만 만든다"
             "(대여는 `alloc <repo> --task <task>`).",
    )
    p_wt_add.add_argument(
        "--user-ack", metavar="<repo>", default=None,
        help="새 슬롯 생성에 대한 사용자 승인 토큰(대상 repo 값과 정확히 결속). "
             "readonly/--task 변형에도 필수.",
    )
    p_wt_add.add_argument("--folders", metavar="<폴더,…>", default=None,
                          help="슬롯에 꺼낼 폴더 목록 (cone, git 2.36 이상, readonly와 함께 사용 불가)")
    p_wt_add.set_defaults(func=cmd_worktree_add)
    # worktree prune-stale — worktree 가 사라진 dangling 장부 엔트리 안전 정리.
    # status reconcile 의 stale/incomplete(worktree 부재) 정리 진입점(조회-전용 reconcile 과 분리·
    # 명시 user-invoked). orphan worktree(git 측)는 `git worktree remove` 로 사용자가.
    p_wt_prune = wt_sub.add_parser(
        "prune-stale",
        help="worktree dir 이 사라진 dangling 장부 엔트리 정리 (stale/incomplete·안전)",
    )
    p_wt_prune.set_defaults(func=cmd_worktree_prune_stale)
    # worktree remove <slot> --user-ack <slot> [--user-ack <브랜치> ...] [--force] — 슬롯 통째 제거.
    # 리스 확인→등록 조회→커밋 안 한 변경 검사→git worktree remove→브랜치 삭제 판정→장부 엔트리 삭제를
    # 한 번에. prune-stale(worktree 부재 장부만 정리·안전)과 달리 **실 worktree 를 지운다** — 사용자
    # 승인값 결속 전제. 장부 엔트리 제거로 `add` 가 빈 번호 재사용(번호 skip footgun 종결).
    p_wt_remove = wt_sub.add_parser(
        "remove",
        help="슬롯 통째 제거 — worktree remove + 브랜치 삭제 판정 + 장부 엔트리 삭제 (사용자 "
             "승인값 필수·prune-stale 과 달리 실 worktree 삭제·번호 재사용). ⚠ 합쳐지지 않은 "
             "브랜치는 그 이름의 승인값이 없으면 보존되며 같은 번호 base-경로 재생성을 막는다.",
    )
    p_wt_remove.add_argument("slot", help="제거할 슬롯 (work/<repo>_<N>·release 와 동일 식별 방식)")
    p_wt_remove.add_argument(
        "--user-ack", metavar="<값>", action="append", default=[],
        help="사용자 승인 토큰 — 반복 가능. 입력한 슬롯 값이 있어야 제거하고, 합쳐지지 않은 "
             "브랜치는 그 브랜치 이름이 값에 있을 때만 지운다.",
    )
    p_wt_remove.add_argument(
        "--force", action="store_true",
        help="점유(활성 리스·사용 중)만 무시하고 제거 — 커밋 안 한 변경·git 미등록 트리는 그래도 거부",
    )
    p_wt_remove.set_defaults(func=cmd_worktree_remove)

    # status | whoami (같은 핸들러) — `--json` 은 status 전용 기계 계약(관리자 읽기 표면).
    p_status = sub.add_parser(
        "status",
        help="풀/리스 상태 + 이 세션 repo/슬롯/branch (`--json` 으로 기계 현황)",
    )
    p_status.add_argument(
        "--json", action="store_true",
        help="현황을 JSON 한 줄로 출력한다 (관리자 읽기 계약·읽기 전용). 기본은 가벼운 절 "
             "(슬롯·task·git·repo·하네스·설정 완전성)이고 무거운 절은 아래 플래그로 붙인다.",
    )
    p_status.add_argument(
        "--with-board", action="store_true",
        help="티켓 수·open 목록·lint 판정·회귀/라이브게이트 기록을 함께 낸다 (`--json` 필요·"
             "티켓 전수 스캔이라 느리다).",
    )
    p_status.add_argument(
        "--with-adapters", action="store_true",
        help="어댑터 config 파일별 판정·기준/최신 diff·기능 표를 함께 낸다 (`--json` 필요·"
             "소스 checkout 필요). 소스를 못 잡으면 부분 산출 없이 오류다.",
    )
    p_status.add_argument(
        "--with-remote", action="store_true",
        help="슬롯마다 origin 주소와 추적 원격 브랜치(이름·끝 커밋·앞/뒤)를 함께 낸다 "
             "(`--json` 필요·원격에 있는 추적 브랜치만 fetch 한다·실패하면 부분 산출 없이 오류다).",
    )
    p_status.add_argument(
        "--from", dest="source", metavar="SOURCE", default=None,
        help="어댑터·최신 설정 레지스트리를 읽을 소스 프레임워크 checkout (생략 시 local.conf "
             "upstream.path 에서 자동 해소·add-harness 와 같은 규칙·URL 은 로컬 캐시).",
    )
    p_status.set_defaults(func=cmd_status)
    p_whoami = sub.add_parser("whoami", help="status 의 별칭 — 이 세션 리스를 머리에 표면")
    p_whoami.set_defaults(func=cmd_status)

    # upstream show | set <value> — upstream 값 조회/전환
    p_upstream = sub.add_parser(
        "upstream", help="upstream(URL|경로) 조회/전환 — show | set <value>")
    up_sub = p_upstream.add_subparsers(dest="upstream_action", metavar="<upstream-action>")
    up_sub.add_parser("show", help="현재 upstream 값 surface (local.conf)")
    p_up_set = up_sub.add_parser(
        "set", help="upstream 값 설정 (검증: URL→ls-remote 도달성·경로→존재+checkout·fail-closed)")
    p_up_set.add_argument("value", help="upstream 값 (git URL 또는 로컬 경로·self-describing)")
    p_upstream.set_defaults(func=cmd_upstream)

    # alloc <repo> --task <task> — task 명의로 idle 최소 번호 슬롯 대여 (논리층·자동 생성 안 함)
    p_alloc = sub.add_parser(
        "alloc",
        help="task 명의로 idle 최소 번호 슬롯 대여 (논리층·풀 소진 시 생성 요청·자동 생성 안 함)",
    )
    p_alloc.add_argument("repo", help="슬롯을 대여할 repo 이름 (areas.md 등록된 것)")
    p_alloc.add_argument(
        "--task", required=True, metavar="<task>",
        help="이 슬롯을 대여할 task 이름 (lease session 명의 — release/task end 소유검사 근거)",
    )
    p_alloc.add_argument("--folders", metavar="<폴더,…>", default=None,
                         help="대여할 슬롯에 꺼낼 폴더 목록 (미지정이면 전체)")
    p_alloc.set_defaults(func=cmd_alloc)

    # release <slot> [--task <task>] [--force]
    p_release = sub.add_parser("release", help="작업완료 반납 / 수동 강제(백스톱)")
    p_release.add_argument("slot", help="반납할 슬롯 (work/<repo>_<N>)")
    p_release.add_argument(
        "--task", metavar="<task>", default=None,
        help="task 소유검사 — 이 슬롯이 그 task 명의(session)가 아니면 반납 거부(다른 task 슬롯 보호)",
    )
    p_release.add_argument("--force", action="store_true",
                           help="점유(leased)·--task 소유검사 무시 강제 idle 화 (커밋 안 한 변경은 그래도 거부)")
    p_release.set_defaults(func=cmd_release)

    # task new <task> — 정체성만 생성(닫힘·세션 묶기 없음) / task end <task> — task 종료:
    # claimed 소진 게이트·dirty 게이트·clean 시 일괄 반납 + 아카이브 이동
    p_task = sub.add_parser("task", help="task 정체성 관리 (new·prefix·branch·close·end·reopen)")
    task_sub = p_task.add_subparsers(dest="task_command", metavar="<task-command>")
    p_task_new = task_sub.add_parser(
        "new",
        help="task 정체성 생성 — 장부 행(닫힘)+pm_state 만 만들고 세션에 안 묶는다"
             f"(진입은 `{_runtime_skill_entry('pm-bootstrap')} --task <task>` 이 resume 으로 이어받음)",
    )
    p_task_new.add_argument("name", help="생성할 task 이름")
    p_task_new.set_defaults(func=cmd_task_new)

    p_task_end = task_sub.add_parser(
        "end",
        help="task 종료 — claimed 소진 게이트·dirty 게이트·전부 clean 시 일괄 idle 반납 + "
             "서술 폴더 _ended 아카이브 이동(삭제 아님·worktree 미삭제)",
    )
    p_task_end.add_argument("name", help="종료할 task 이름")
    p_task_end.set_defaults(func=cmd_task_end)

    # task close <task> — 종료 기록 없이 남은 세션을 사용자가 닫는다(기록만·복원 없음).
    p_task_close = task_sub.add_parser(
        "close",
        help="열린 채 남은 task 세션 기록을 닫는다 — 기록만 닫고 복원하지 않는다"
             "(작업 폴더 쪽 짝은 `release <slot> --force`)",
    )
    p_task_close.add_argument("name", help="기록을 닫을 task 이름")
    p_task_close.set_defaults(func=cmd_task_close)

    p_task_reopen = task_sub.add_parser(
        "reopen",
        help="종료 archive를 같은 task identity로 복원(bootstrap은 별도 실행)",
    )
    p_task_reopen.add_argument("name", help="복원할 task 이름")
    p_task_reopen.add_argument(
        "--archive", metavar="<basename>", default=None,
        help="복수 후보 중 정확히 고를 _ended archive basename",
    )
    p_task_reopen.set_defaults(func=cmd_task_reopen)

    # task prefix <task> <p|none> — task 의 ticket prefix 지정/변경/해제(중간 변경 자유).
    # `board.py new --task` 가 3단 해소(명시 --prefix > task 설정 > 기본 없음)로 이 값을 소비한다.
    p_task_prefix = task_sub.add_parser(
        "prefix",
        help="task 의 ticket prefix 지정/변경/해제 (중간 변경 자유·`none`=해제·분류 라벨≠경계)",
    )
    p_task_prefix.add_argument("name", help="대상 task 이름")
    p_task_prefix.add_argument(
        "value",
        help="설정할 prefix (`[a-z0-9_]` 형식·소문자 권장) 또는 `none`(해제·무prefix)",
    )
    p_task_prefix.add_argument(
        "--user-ack", metavar="<prefix>", default=None,
        help="새 prefix 신설에 대한 사용자 승인 토큰(대상 prefix 값과 정확히 결속)",
    )
    p_task_prefix.set_defaults(func=cmd_task_prefix)

    # task todo <add|list|done> <task> — 이 task 의 **열린** 할 일. 상태 필드가 없다(존재=열림·
    # 완료=제거). 쓰기 판정이 "열린 todo 없으면 경고" 를 이 목록으로 낸다.
    p_task_todo = task_sub.add_parser(
        "todo",
        help="task 의 열린 할 일 등록/조회/완료 (완료 = 목록에서 제거·상태 필드 없음)",
    )
    todo_sub = p_task_todo.add_subparsers(dest="todo_command", metavar="<todo-command>")

    p_todo_add = todo_sub.add_parser("add", help="열린 todo 를 하나 더한다")
    p_todo_add.add_argument("name", help="대상 task 이름")
    p_todo_add.add_argument("text", help="할 일 한 줄")
    p_todo_add.set_defaults(func=cmd_task_todo_add)

    p_todo_list = todo_sub.add_parser("list", help="열린 todo 를 ID·등록일·묵은 일수와 함께 본다")
    p_todo_list.add_argument("name", help="대상 task 이름")
    p_todo_list.set_defaults(func=cmd_task_todo_list)

    p_todo_done = todo_sub.add_parser(
        "done", help="열린 todo 하나를 완료 처리한다 (목록에서 제거·완료 표시를 남기지 않는다)")
    p_todo_done.add_argument("name", help="대상 task 이름")
    p_todo_done.add_argument(
        "todo_id", type=int, help="`task todo list` 가 보여준 ID(`#3` 이면 3·재사용되지 않는다)")
    p_todo_done.set_defaults(func=cmd_task_todo_done)

    # task branch <task> <슬롯> <브랜치> --from <부모> — 슬롯 안 새 브랜치의 유일한 생성 경로(부모 기록 동반).
    p_task_branch = task_sub.add_parser(
        "branch",
        help="슬롯에 새 브랜치를 부모 브랜치 최신 커밋에서 만들고 부모를 기록한다 "
             "(슬롯 안 새 브랜치의 유일한 경로 · 기존 브랜치 전환은 `pm-worktree switch`)",
    )
    p_task_branch.add_argument("name", help="슬롯을 쥔 task 이름")
    p_task_branch.add_argument("slot", help="브랜치를 만들 슬롯(`work/<repo>_<N>` 또는 `<repo>_<N>`)")
    p_task_branch.add_argument("branch", help="만들 브랜치 이름")
    p_task_branch.add_argument("--from", dest="parent", required=True, metavar="<부모>",
                               help="부모 브랜치 — 이 브랜치를 다시 합칠 곳")
    p_task_branch.set_defaults(func=cmd_task_branch)

    # add-harness <harness> [--dry-run] — 라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가
    # pm_import.add_harness_cli 로 verbatim 위임(cmd_add_harness) — harness 는
    # choices 로 재검증하지 않고(pm_import 가 CLI 규격 단일 진실·미지원은 add_harness_cli 가
    # ValueError→rc 1) 그대로 넘긴다. repo/worktree/release 와 동형의 func-dispatch 서브커맨드다.
    p_add_harness = sub.add_parser(
        "add-harness",
        help="라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가 (pm_import.add_harness 위임)",
    )
    p_add_harness.add_argument(
        "harness",
        help="추가할 harness 어댑터 (claude|opencode|codex·pm_import 가 검증). 어댑터 네임스페이스만 복사(비파괴).",
    )
    p_add_harness.add_argument(
        "--from", dest="source", metavar="SOURCE", default=None,
        help="어댑터 소스 프레임워크 checkout (생략 시 local.conf upstream.path 에서 자동 해소·"
             "imported 인스턴스 갭). URL upstream 이면 로컬 checkout 경로를 명시해야 한다.",
    )
    p_add_harness.add_argument(
        "--dry-run", action="store_true",
        help="적용 없이 복사 plan 만 출력 (파일시스템 미변경).",
    )
    p_add_harness.set_defaults(func=cmd_add_harness)

    # sync-adapter-config [--list|--check|--accept <경로>] — instance-owned config 채널.
    #   동기(pm-update)는 무편집분만 자동 갱신하고 편집분·원장 부재는 보존+보고한다. 이 커맨드가
    #   그 보존분을 채택자가 명시적으로 받는 유일한 채널이다(판정은 pm_import 단일 진실).
    p_sync_cfg = sub.add_parser(
        "sync-adapter-config",
        help="어댑터 config(hooks.json·settings.json 등) 판정 조회 / 상류 값 수용",
    )
    # 조회와 수용은 **상호 배타**다 — 함께 주면 한쪽이 조용히 무시되는 오사용이 된다.
    sync_cfg_mode = p_sync_cfg.add_mutually_exclusive_group()
    sync_cfg_mode.add_argument(
        "--list", action="store_true",
        help="현재 판정(최신/무편집/편집됨/원장부재)을 조회한다 (기본 동작).",
    )
    sync_cfg_mode.add_argument(
        "--check", action="store_true",
        help="managed 대상의 template 내용 + 원장 dest hash 수렴을 읽기 전용으로 검사한다 "
             "(미수렴/판정 unavailable rc 1·report-only 차이는 비차단).",
    )
    sync_cfg_mode.add_argument(
        "--accept", metavar="RELPATH", default=None,
        help="그 경로를 백업 후 현행 template 으로 교체하고 원장에 기록한다 "
             "(보고 모드 탈출 채널·경로는 인스턴스 루트 기준 예 `.codex/hooks.json`).",
    )
    sync_cfg_mode.add_argument(
        "--accept-all", action="store_true",
        help="상류와 다른 config 를 세트로 수용한다(파일당 백업+원자 교체·대상 선-표시). "
             "채택자 편집분은 기본 제외이고(단건 --accept 로만), 훅 세트 선행조건(엔진 파일 선행)을 "
             "못 갖춘 파일은 건드리지 않고 거부한다. 남은 대상이 있으면 rc 1.",
    )
    p_sync_cfg.add_argument(
        "--from", dest="source", metavar="SOURCE", default=None,
        help="어댑터 소스 프레임워크 checkout (생략 시 local.conf upstream.path 에서 자동 해소·"
             "add-harness 와 같은 규칙).",
    )
    p_sync_cfg.set_defaults(func=cmd_sync_adapter_config)

    # apply [--json] [--set 키=값 ...] — 값 검사 → conf 기록 → 파생 파일 반영(설정 저장의 유일한 입구).
    p_apply = sub.add_parser(
        "apply",
        help="local.conf 값을 검사·기록하고 하네스 설정 파일과 렌더 산출물에 반영 "
             "(`--set` 이 없으면 현재 local.conf 전체를 다시 반영)",
    )
    p_apply.add_argument(
        "--set", action="append", type=_conf_assignment, metavar="키=값", default=None,
        help="local.conf 에 쓸 값(여러 번 가능·`키=` 는 줄 삭제). 반영 전 검사를 통과해야 쓴다.",
    )
    p_apply.add_argument(
        "--json", action="store_true",
        help="기계 판독 한 줄 `{\"changed\", \"applied\"}` 로 낸다.",
    )
    p_apply.set_defaults(func=cmd_apply)

    # delegate models --harness <h> --json — 위임 모델 목록 조회(읽기 전용·장부 변경 0).
    #   `--harness` 값의 닫힌 집합은 `contract.MODEL_SOURCE` 의 키다. 그 표를 파서 조립 시점에
    #   읽지 않는 것은 의도다 — 파서는 실패를 보고할 채널이 없어서, 표를 못 읽는 사본에서
    #   무관한 서브커맨드(`status` 등)까지 함께 죽는다. 값 판정은 핸들러가 같은 표로 하고 rc 2 다.
    p_delegate = sub.add_parser("delegate", help="위임 설정 조회 (models)")
    delegate_sub = p_delegate.add_subparsers(
        dest="delegate_command", metavar="<delegate-command>")
    p_delegate_models = delegate_sub.add_parser(
        "models", help="하네스가 인정하는 모델 목록 조회 (읽기 전용·JSON)")
    p_delegate_models.add_argument(
        "--harness", required=True,
        help="조회할 하네스 (claude|codex|opencode — 등재는 contract.MODEL_SOURCE 가 소유·"
             "그 밖의 값은 rc 2).",
    )
    p_delegate_models.add_argument(
        "--json", action="store_true", required=True,
        help="기계 판독 한 줄(JSON)로 받는다 — 이 명령이 받는 유일한 모양이다.",
    )
    p_delegate_models.set_defaults(func=cmd_delegate_models)

    # update [--from ...] — pm-update 흡수. 실제 forward 는 main 이 argparse 우회로
    # 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--from·--dry-run)를 이 디스패처가 가로채면 안 되므로 forward
    # 토큰을 subparser 로 파싱하지 않는다.
    sub.add_parser(
        "update",
        help="엔진 갱신 (pm-update 흡수) — 뒤 인자는 pm_update 로 verbatim forward",
        add_help=False,
    )

    # init [<board init 인자>] — board.py init 흡수. 실제 forward 는 main 이 argparse
    # 우회로 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--prefix·--area·--owner·--session)를 이 디스패처가 가로채면
    # 안 되므로 forward 토큰을 subparser 로 파싱하지 않는다(update 와 동형).
    sub.add_parser(
        "init",
        help="clone 당 1회 셋업 (board.py init 흡수) — 뒤 인자는 board 로 verbatim forward",
        add_help=False,
    )

    return parser


def _main(argv: list[str] | None = None) -> int:
    console.configure_console_utf8()

    raw = list(sys.argv[1:] if argv is None else argv)

    # 무인자 분기: 인자 0 일 때
    #   - tty(stdin·stdout 둘 다)  → 대화형 콘솔(run_console). multi-PM이 복잡(다수 repo·슬롯·
    #     빌드변형)해 CLI 플래그를 외우기 어려운 걸 상태 가시화 + 메뉴로 해소.
    #   - 비-tty(파이프/CI)         → 현행 help(아래 argparse 경로). input() 으로 안 멈춘다
    #     (pm_import 비-tty 폴백 패턴 동류·CI 안전). 서브커맨드를 주면 기존 CLI 경로 그대로
    #     (이 분기 미진입) — 커맨드형 동작 0 변경.
    if not raw and _stdin_is_tty():
        return run_console()

    # `update` 는 argparse 를 우회해 뒤 인자를 pm_update 로 *verbatim* forward 한다 —
    # `--from`·`--dry-run` 같은 option-like 플래그를 디스패처가 가로채지 않게 한다
    # (pm_update 가 CLI 규격의 단일 진실·중복 파싱 0). `update` 가 첫 토큰일 때만.
    if raw and raw[0] == "update":
        return cmd_update(raw[1:])

    # `init` 도 동형 — 뒤 인자를 board.py init 으로 *verbatim* forward 한다.
    # `--prefix`·`--area`·`--owner`·`--session` 같은 option-like 플래그를 디스패처가
    # 가로채지 않게 한다(board.py init 이 CLI 규격의 단일 진실·중복 파싱 0). `init` 이
    # 첫 토큰일 때만.
    if raw and raw[0] == "init":
        return cmd_init(raw[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    # 서브커맨드 미지정 — 등록 안내 surface(--help 단일 소스).
    if getattr(args, "command", None) is None:
        parser.print_help()
        return 1

    # `repo`/`worktree` 만 주고 하위 동작(add)을 안 줬으면 그 그룹 도움말 surface.
    if args.command == "repo" and getattr(args, "repo_command", None) is None:
        parser.parse_args(["repo", "--help"])
        return 1
    if args.command == "worktree" and getattr(args, "worktree_command", None) is None:
        parser.parse_args(["worktree", "--help"])
        return 1
    # `upstream` 만 주고 하위 동작(show/set)을 안 줬으면 그 그룹 도움말 surface(repo/worktree 동형).
    if args.command == "upstream" and getattr(args, "upstream_action", None) is None:
        parser.parse_args(["upstream", "--help"])
        return 1
    # `task` 만 주고 하위 동작(end)을 안 줬으면 그 그룹 도움말 surface(repo/worktree/upstream 동형).
    if args.command == "task" and getattr(args, "task_command", None) is None:
        parser.parse_args(["task", "--help"])
        return 1
    # `task todo` 만 주고 하위 동작(add/list/done)을 안 줬으면 그 그룹 도움말 surface(위와 동형).
    if (args.command == "task" and getattr(args, "task_command", None) == "todo"
            and getattr(args, "todo_command", None) is None):
        parser.parse_args(["task", "todo", "--help"])
        return 1
    # `delegate` 만 주고 하위 동작(models)을 안 줬으면 그 그룹 도움말 surface(위 넷과 동형).
    if args.command == "delegate" and getattr(args, "delegate_command", None) is None:
        parser.parse_args(["delegate", "--help"])
        return 1

    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    """CLI 최외곽에서 엔진 사본 불일치를 traceback 대신 복구 안내로 번역한다."""
    try:
        console.configure_console_utf8()
        return _main(argv)
    except Exception as exc:  # noqa: BLE001 — marked skew만 사용자 진단+rc로 종료.
        if _is_engine_rev_skew(exc):
            return _report_engine_rev_skew_at_terminal(exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
