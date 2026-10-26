"""`board.py` 명령 파서와 분류·읽기/쓰기 dispatch (층: cli).

평면 `board.py` 와 `pm-config init` 이 명시 Home 과 raw argv 를 넘겨 부른다. 각 명령의 처리는
cli.board_*·cli.hooks·cli.regression_cmd 의 소유 함수가 맡는다.
"""
from __future__ import annotations

import argparse
import sys

from pm_engine.app import freshness_lint
from pm_engine.cli import board_cluster
from pm_engine.cli import board_dispatch
from pm_engine.cli import board_init
from pm_engine.cli import board_lint
from pm_engine.cli import board_list
from pm_engine.cli import board_migrate_identity
from pm_engine.cli import board_prefix
from pm_engine.cli import board_repin
from pm_engine.cli import board_round
from pm_engine.cli import board_ticket
from pm_engine.cli import hooks
from pm_engine.cli import regression_cmd
from pm_engine.domain import board_command_rules
from pm_engine.domain import design_rules
from pm_engine.domain import home
from pm_engine.domain import identity_args
from pm_engine.domain import round_rules
from pm_engine.domain import ticket_rules
from pm_engine.infra import pm_paths


def build_parser(*, prog: str = "board.py") -> argparse.ArgumentParser:
    """board 명령 파서. 모든 핸들러는 `(home, args)` 를 받는다."""
    parser = argparse.ArgumentParser(prog=prog,
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
    p.set_defaults(fn=regression_cmd.cmd_regression, _board_subcommand="regression")

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
    p.set_defaults(fn=hooks.cmd_git_anchor)

    p = sub.add_parser("git-grant", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True, help=argparse.SUPPRESS)
    p.add_argument("--cwd", required=True, help=argparse.SUPPRESS)
    p.set_defaults(fn=hooks.cmd_git_grant)

    p = sub.add_parser("git-tool-input", help=argparse.SUPPRESS)
    p.add_argument("--session", required=True, help=argparse.SUPPRESS)
    p.add_argument("--cwd", required=True, help=argparse.SUPPRESS)
    p.add_argument("--tool", required=True, help=argparse.SUPPRESS)
    p.set_defaults(fn=hooks.cmd_git_tool_input)

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


def _read_home(base: home.Home, subcommand: str) -> home.Home:
    """읽기 명령이 쓸 Home — 코드 root 는 그대로 두고 PM 데이터(board·로컬 입력)만 소유 홈으로 고른다.

    첫 줄 앵커 안내를 함께 출력한다. 소유 홈 유도 실패는 `PmHomeResolutionError` 로 전파된다.
    """
    resolution = pm_paths._resolve_read_board(base)
    board_dispatch._print_read_anchor(
        base, subcommand=subcommand, pm_home=resolution.home if resolution.home != base.root else None,
    )
    if resolution.home is not None and resolution.home != base.root:
        return pm_paths.home_at(base.root, resolution.home, resolution.root)
    # 자기 board/standalone은 받은 Home 그대로다. 폴백 문구·경로 재바인딩이 전혀 생기지 않는다.
    if resolution.root == pm_paths._board_root_at(base.root):
        return base
    # 방어적 경계: 현재 resolver 계약상 home 없는 비local root는 나올 수 없다.
    return pm_paths.home_at(base.root, base.root, resolution.root)


def _main(home: home.Home, argv: list[str] | None = None, *, prog: str = "board.py") -> int:
    args = build_parser(prog=prog).parse_args(argv)
    # PM-홈 worktree 오실행 가드 — mutation subcommand 전수·단일 dispatch 지점.
    # board 상태를 쓰는 명령만(read·sidecar 제외) 핸들러 *전에* fail-loud. 분류는
    # board_command_rules 상수·미래 누락은 메타 가드 테스트가 잡는다.
    subcommand = board_dispatch._resolved_subcommand(args)
    if subcommand not in (board_command_rules._MUTATION_SUBCOMMANDS
                          | board_command_rules._READ_SUBCOMMANDS
                          | board_command_rules._SIDECAR_SUBCOMMANDS):
        raise ValueError(f"분류되지 않은 board 명령: {subcommand}")
    if subcommand in board_command_rules._MUTATION_SUBCOMMANDS and board_dispatch._guard_worktree_misanchor(home, f'board.py {subcommand}'):
        return 1
    if subcommand in board_command_rules._READ_SUBCOMMANDS:
        return args.fn(_read_home(home, subcommand), args)
    return args.fn(home, args)


def main(home: home.Home, argv: list[str] | None = None, *, prog: str = "board.py") -> int:
    """소유 PM 홈 미확정을 traceback 대신 안내로 번역한다.

    소유 PM 홈 유도 실패는 read·mutation 어느 경로에서 나든 같은 원인이므로 번역 지점도
    하나다 — 실패는 여전히 실패다(rc1·부작용 0). 자기 앵커로 접지 않는다.
    """
    try:
        return _main(home, argv, prog=prog)
    except pm_paths.PmHomeResolutionError as exc:
        print(f"[중단] 소유 PM 홈을 확정할 수 없습니다: {exc}", file=sys.stderr)
        return 1
