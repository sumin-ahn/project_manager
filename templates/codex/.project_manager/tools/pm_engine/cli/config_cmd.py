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
  - init      → cli.board_cmd.main(home, ["init", ...], prog="pm-config") verbatim forward (clone 당 1회 셋업·N=1·M=1 ~ N×M 공용).
  - repo add  → areas_registry.areas_append(per-repo 레지스트리 줄) + `git clone --bare`
                로 `.repos/<name>.git`(worktree 풀 공유 .git 원).
  - repo protected → (조회) areas_store._repo_protected 실효값 + areas raw 셀(출처) + 훅 sidecar 정합 /
                (설정) areas_registry.areas_set_cell(areas.md 단일 진실·board_lock in-place) →
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
  - task prefix → ticket_rules._validate_prefix(입력 sanity·소비 grammar 단일 진실) → task_lifecycle.set_task_prefix
                (장부 flock/스키마 단일 소유·`none`=해제·직접 JSON write 금지).
  - task end → ticket_access.scan_task_tickets(claimed 소진 게이트) → task_lifecycle.end_task(dirty 게이트·
               일괄 idle 반납·서술 폴더 `_ended/<task>-<날짜>/` 아카이브 이동·삭제 아님).
  - update → pm_update.main(argv) verbatim forward (rename 비용 0·중복 구현 금지).
  - add-harness → app.add_harness.add_harness_cli(home, dest, harness, dry_run=, source_root=) verbatim forward
                  (복사 스코프+인터페이스 예외 번역+소스 해소는 app.add_harness 단일 진실·
                  중복 0). `--from` 생략 시 dest local.conf upstream.path 자동 해소(imported 인스턴스).
  - delegate models → contract.model_listing(모델 원천 표 `MODEL_SOURCE` 한 행의 조회 — 별칭 표 그대로 /
                  표가 선언한 조회 명령 실행). 응답에 모델별 `variants`(opencode)가 실린다.
                  읽기 전용이고 조회 실패는 빈 목록이 아니라 rc 3 이다(사유는 stderr 한 줄).
  - sync-adapter-config → adapter_report.judge_adapter_configs(조회) / adapter_accept.accept_adapter_config(수용·백업
                  후 template 채택 + 원장 기록). manifest 밖 instance-owned config 는 동기가 무편집
                  분만 자동 갱신하고 나머지는 보존+보고하므로, 이 커맨드가 보존분을 채택자가
                  명시적으로 받는 유일한 채널이다(소스 해소는 add-harness 와 같은 규칙).
                  수용한 파일이 하네스 설정 파일이면 수용 직후 사용 한도를 다시 반영한다.
  - apply → app.conf_apply.apply_conf(키 묶음 표·한 락·전부 계획 뒤 기록). 관리자 저장은 키와 무관하게
                  이 명령 한 번이다. 반영 전 거부는 rc 3 이고 local.conf·파생 파일을 바꾸지 않는다 —
                  처리하지 못한 예외의 rc 1 과 겹치지 않아 관리자 화면은 rc 3 만 잘못된 값으로 보인다.


  - thin forwarder(`pm-config.sh/.cmd`)는 로직 0 — 이 디스패처가 엔진 배선의 단일 지점.
  - 브랜치 할당은 `task branch`(새 브랜치)·`worktree_pool.py switch`(있는 브랜치) 소관이다.
  - update 는 pm_update 로직을 *위임*(import 호출) — pm_update.py 는 그대로 두고 흡수만.
  - 작업 공간 명령은 cli.config_repo/config_slots/config_tasks/config_console이 소유한다.
    경로는 명시 Home으로 전달하고 입력·git runner는 실제 IO seam에서 격리한다.
"""

from __future__ import annotations

import argparse
import sys
import pm_engine

from pm_engine.app import repo_protection
from pm_engine.cli import board_cmd
from pm_engine.cli import config_adapter
from pm_engine.cli import config_console
from pm_engine.cli import config_install
from pm_engine.cli import config_models
from pm_engine.cli import config_repo
from pm_engine.cli import config_slots
from pm_engine.cli import config_status
from pm_engine.cli import config_tasks
from pm_engine.cli import config_upstream
from pm_engine.domain import home
from pm_engine.infra import console, lease_store


def cmd_init(home: home.Home, forward_args: list[str]) -> int:
    """`init [<board init 인자>]` — clone 당 1회 셋업 (board.py init 흡수).

    board 명령 진입 `board_cmd.main(home, ["init", *forward_args], prog="pm-config")` 로
    verbatim forward 한다 — board init 이 CLI 규격의 단일 진실이고, 이 서브커맨드는 위임만
    한다(중복 구현 0). forward_args 는 `init` 뒤의 raw 토큰을 *그대로*(argparse 미가공) 넘긴다 —
    `--prefix`·`--area`·`--owner`·`--session` 등 option-like 플래그를 디스패처가 가로채지 않게
    `_main` 이 (raw[0]=="init" special-case 로) argparse 를 우회해 이 핸들러로 raw 토큰을 넘긴다.
    usage 의 prog 는 값으로 넘겨 init 서브파서 usage 가 "pm-config init" 으로 파생된다.
    """
    return board_cmd.main(home, ["init", *forward_args], prog=repo_protection._FACADE_PROG)


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
    p_repo_add.set_defaults(func=config_repo.cmd_repo_add)

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
    p_repo_protected.set_defaults(func=config_repo.cmd_repo_protected)

    # repo list — 등록 repo 표(repo·prefix·base·protected·test_cmd·area_owner).
    p_repo_list = repo_sub.add_parser(
        "list", help="등록 repo 표 (repo·prefix·base·protected·test_cmd·area_owner)")
    p_repo_list.set_defaults(func=config_repo.cmd_repo_list)

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
             f"갱신은 `{lease_store._runtime_skill_entry('pm-worktree')} refresh` 로만·"
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
    p_wt_add.set_defaults(func=config_slots.cmd_worktree_add)
    # worktree prune-stale — worktree 가 사라진 dangling 장부 엔트리 안전 정리.
    # status reconcile 의 stale/incomplete(worktree 부재) 정리 진입점(조회-전용 reconcile 과 분리·
    # 명시 user-invoked). orphan worktree(git 측)는 `git worktree remove` 로 사용자가.
    p_wt_prune = wt_sub.add_parser(
        "prune-stale",
        help="worktree dir 이 사라진 dangling 장부 엔트리 정리 (stale/incomplete·안전)",
    )
    p_wt_prune.set_defaults(func=config_slots.cmd_worktree_prune_stale)
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
    p_wt_remove.set_defaults(func=config_slots.cmd_worktree_remove)

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
    p_status.set_defaults(func=config_status.cmd_status)
    p_whoami = sub.add_parser("whoami", help="status 의 별칭 — 이 세션 리스를 머리에 표면")
    p_whoami.set_defaults(func=config_status.cmd_status)

    # upstream show | set <value> — upstream 값 조회/전환
    p_upstream = sub.add_parser(
        "upstream", help="upstream(URL|경로) 조회/전환 — show | set <value>")
    up_sub = p_upstream.add_subparsers(dest="upstream_action", metavar="<upstream-action>")
    up_sub.add_parser("show", help="현재 upstream 값 surface (local.conf)")
    p_up_set = up_sub.add_parser(
        "set", help="upstream 값 설정 (검증: URL→ls-remote 도달성·경로→존재+checkout·fail-closed)")
    p_up_set.add_argument("value", help="upstream 값 (git URL 또는 로컬 경로·self-describing)")
    p_upstream.set_defaults(func=config_upstream.cmd_upstream)

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
    p_alloc.set_defaults(func=config_slots.cmd_alloc)

    # release <slot> [--task <task>] [--force]
    p_release = sub.add_parser("release", help="작업완료 반납 / 수동 강제(백스톱)")
    p_release.add_argument("slot", help="반납할 슬롯 (work/<repo>_<N>)")
    p_release.add_argument(
        "--task", metavar="<task>", default=None,
        help="task 소유검사 — 이 슬롯이 그 task 명의(session)가 아니면 반납 거부(다른 task 슬롯 보호)",
    )
    p_release.add_argument("--force", action="store_true",
                           help="점유(leased)·--task 소유검사 무시 강제 idle 화 (커밋 안 한 변경은 그래도 거부)")
    p_release.set_defaults(func=config_slots.cmd_release)

    # task new <task> — 정체성만 생성(닫힘·세션 묶기 없음) / task end <task> — task 종료:
    # claimed 소진 게이트·dirty 게이트·clean 시 일괄 반납 + 아카이브 이동
    p_task = sub.add_parser("task", help="task 정체성 관리 (new·prefix·branch·close·end·reopen)")
    task_sub = p_task.add_subparsers(dest="task_command", metavar="<task-command>")
    p_task_new = task_sub.add_parser(
        "new",
        help="task 정체성 생성 — 장부 행(닫힘)+pm_state 만 만들고 세션에 안 묶는다"
             f"(진입은 `{lease_store._runtime_skill_entry('pm-bootstrap')} --task <task>` 이 resume 으로 이어받음)",
    )
    p_task_new.add_argument("name", help="생성할 task 이름")
    p_task_new.set_defaults(func=config_tasks.cmd_task_new)

    p_task_end = task_sub.add_parser(
        "end",
        help="task 종료 — claimed 소진 게이트·dirty 게이트·전부 clean 시 일괄 idle 반납 + "
             "서술 폴더 _ended 아카이브 이동(삭제 아님·worktree 미삭제)",
    )
    p_task_end.add_argument("name", help="종료할 task 이름")
    p_task_end.set_defaults(func=config_tasks.cmd_task_end)

    # task close <task> — 종료 기록 없이 남은 세션을 사용자가 닫는다(기록만·복원 없음).
    p_task_close = task_sub.add_parser(
        "close",
        help="열린 채 남은 task 세션 기록을 닫는다 — 기록만 닫고 복원하지 않는다"
             "(작업 폴더 쪽 짝은 `release <slot> --force`)",
    )
    p_task_close.add_argument("name", help="기록을 닫을 task 이름")
    p_task_close.set_defaults(func=config_tasks.cmd_task_close)

    p_task_reopen = task_sub.add_parser(
        "reopen",
        help="종료 archive를 같은 task identity로 복원(bootstrap은 별도 실행)",
    )
    p_task_reopen.add_argument("name", help="복원할 task 이름")
    p_task_reopen.add_argument(
        "--archive", metavar="<basename>", default=None,
        help="복수 후보 중 정확히 고를 _ended archive basename",
    )
    p_task_reopen.set_defaults(func=config_tasks.cmd_task_reopen)

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
    p_task_prefix.set_defaults(func=config_tasks.cmd_task_prefix)

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
    p_todo_add.set_defaults(func=config_tasks.cmd_task_todo_add)

    p_todo_list = todo_sub.add_parser("list", help="열린 todo 를 ID·등록일·묵은 일수와 함께 본다")
    p_todo_list.add_argument("name", help="대상 task 이름")
    p_todo_list.set_defaults(func=config_tasks.cmd_task_todo_list)

    p_todo_done = todo_sub.add_parser(
        "done", help="열린 todo 하나를 완료 처리한다 (목록에서 제거·완료 표시를 남기지 않는다)")
    p_todo_done.add_argument("name", help="대상 task 이름")
    p_todo_done.add_argument(
        "todo_id", type=int, help="`task todo list` 가 보여준 ID(`#3` 이면 3·재사용되지 않는다)")
    p_todo_done.set_defaults(func=config_tasks.cmd_task_todo_done)

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
    p_task_branch.set_defaults(func=config_tasks.cmd_task_branch)

    # add-harness <harness> [--dry-run] — 라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가
    # app.add_harness.add_harness_cli 로 verbatim 위임(cmd_add_harness) — harness 는
    # choices 로 재검증하지 않고(add_harness 가 CLI 규격 단일 진실·미지원은 add_harness_cli 가
    # ValueError→rc 1) 그대로 넘긴다. repo/worktree/release 와 동형의 func-dispatch 서브커맨드다.
    p_add_harness = sub.add_parser(
        "add-harness",
        help="라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가 (add_harness 위임)",
    )
    p_add_harness.add_argument(
        "harness",
        help="추가할 harness 어댑터 (claude|opencode|codex·add_harness 가 검증). 어댑터 네임스페이스만 복사(비파괴).",
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
    p_add_harness.set_defaults(func=config_install.cmd_add_harness)

    # sync-adapter-config [--list|--check|--accept <경로>] — instance-owned config 채널.
    #   동기(pm-update)는 무편집분만 자동 갱신하고 편집분·원장 부재는 보존+보고한다. 이 커맨드가
    #   그 보존분을 채택자가 명시적으로 받는 유일한 채널이다(판정은 adapter_report 단일 진실).
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
    p_sync_cfg.set_defaults(func=config_adapter.cmd_sync_adapter_config)

    # apply [--json] [--set 키=값 ...] — 값 검사 → conf 기록 → 파생 파일 반영(설정 저장의 유일한 입구).
    p_apply = sub.add_parser(
        "apply",
        help="local.conf 값을 검사·기록하고 하네스 설정 파일과 렌더 산출물에 반영 "
             "(`--set` 이 없으면 현재 local.conf 전체를 다시 반영)",
    )
    p_apply.add_argument(
        "--set", action="append", type=config_models._conf_assignment, metavar="키=값", default=None,
        help="local.conf 에 쓸 값(여러 번 가능·`키=` 는 줄 삭제). 반영 전 검사를 통과해야 쓴다.",
    )
    p_apply.add_argument(
        "--json", action="store_true",
        help="기계 판독 한 줄 `{\"changed\", \"applied\"}` 로 낸다.",
    )
    p_apply.set_defaults(func=config_install.cmd_apply)

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
    p_delegate_models.set_defaults(func=config_models.cmd_delegate_models)

    # update [--from ...] — pm-update 흡수. 실제 forward 는 _main 이 argparse 우회로
    # 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--from·--dry-run)를 이 디스패처가 가로채면 안 되므로 forward
    # 토큰을 subparser 로 파싱하지 않는다.
    sub.add_parser(
        "update",
        help="엔진 갱신 (pm-update 흡수) — 뒤 인자는 pm_update 로 verbatim forward",
        add_help=False,
    )

    # init [<board init 인자>] — board.py init 흡수. 실제 forward 는 _main 이 argparse
    # 우회로 처리한다(아래 special-case) — 여기 등록은 `--help` 목록 surface(발견성)용이다.
    # option-like 플래그(--prefix·--area·--owner·--session)를 이 디스패처가 가로채면
    # 안 되므로 forward 토큰을 subparser 로 파싱하지 않는다(update 와 동형).
    sub.add_parser(
        "init",
        help="clone 당 1회 셋업 (board.py init 흡수) — 뒤 인자는 board 로 verbatim forward",
        add_help=False,
    )

    return parser


def _main(home: home.Home, argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)

    # 무인자 분기: 인자 0 일 때
    #   - tty(stdin·stdout 둘 다)  → 대화형 콘솔(run_console). multi-PM이 복잡(다수 repo·슬롯·
    #     빌드변형)해 CLI 플래그를 외우기 어려운 걸 상태 가시화 + 메뉴로 해소.
    #   - 비-tty(파이프/CI)         → 현행 help(아래 argparse 경로). input() 으로 안 멈춘다
    #     (pm_import 비-tty 폴백 패턴 동류·CI 안전). 서브커맨드를 주면 기존 CLI 경로 그대로
    #     (이 분기 미진입) — 커맨드형 동작 0 변경.
    if not raw and console._stdin_is_tty():
        return config_console.run_console(home)

    # `update` 는 argparse 를 우회해 뒤 인자를 갱신 CLI 로 *verbatim* forward 한다 —
    # `--from`·`--dry-run` 같은 option-like 플래그를 디스패처가 가로채지 않게 한다
    # (cli.update_cmd 가 CLI 규격의 단일 진실·중복 파싱 0). `update` 가 첫 토큰일 때만.
    if raw and raw[0] == "update":
        return config_install.cmd_update(home, raw[1:])

    # `init` 도 동형 — 뒤 인자를 board.py init 으로 *verbatim* forward 한다.
    # `--prefix`·`--area`·`--owner`·`--session` 같은 option-like 플래그를 디스패처가
    # 가로채지 않게 한다(board.py init 이 CLI 규격의 단일 진실·중복 파싱 0). `init` 이
    # 첫 토큰일 때만.
    if raw and raw[0] == "init":
        return cmd_init(home, raw[1:])

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

    if args.func is config_status.cmd_status:
        return args.func(home, args, engine_rev=pm_engine.ENGINE_REV)
    return args.func(home, args)


def main(home: home.Home, argv: list[str] | None = None) -> int:
    """서브커맨드를 dispatch 한다. status 에는 엔진 세대 값 `pm_engine.ENGINE_REV` 를 넘긴다."""
    return _main(home, argv)
