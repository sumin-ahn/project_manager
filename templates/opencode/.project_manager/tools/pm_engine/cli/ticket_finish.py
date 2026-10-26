"""ticket_finish 공개 CLI — argv 파싱·오실행 가드·정체성 검증·완료 기록/종결 배선 (층: cli)."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pm_engine.app import slot_resolution
from pm_engine.cli import cluster_close, finish_record
from pm_engine.domain import identity_args
from pm_engine.infra import areas_store, finish_store, lease_store, pm_paths


# ── PM-홈 worktree 오실행 가드 (쓰기-경로 전용) ─────────────────────────
# ticket_finish 를 PM 홈의 등록 worktree cwd 에서 오실행하면 실행 앵커가 worktree 로 착지해
# stray `wiki/log/current.md` append(+ board.py complete 오실행)를 낸다. board.py
# 와 같은 pm_paths detector를 명시 Home으로 호출한다. 실제 판정 오류는 기존 fail-soft를
# 유지하며 쓰기 단계 전에 등록 worktree 오실행을 거부한다.

def _pm_home_misanchor(home) -> Path | None:
    """명시 Home으로 등록 worktree를 검사한다. 실제 판정 실패는 None으로 반환한다."""
    try:
        return pm_paths._pm_home_worktree_misanchor(home)
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 통과(오탐 0·standalone 무영향).
        return None


def _guard_worktree_misanchor(home) -> bool:
    """쓰기-경로 진입 가드 — 오실행이면 fail-loud 후 True(차단), 아니면 False(통과)."""
    pm_home = _pm_home_misanchor(home)
    if pm_home is None:
        return False
    print(
        "[중단] `ticket_finish` 를 worktree(코드 전용) 트리에서 실행했습니다 — 완료 기록(log·"
        "board·git)는 PM 홈이 소유합니다. 이대로면 이 worktree 에 stray log/티켓을 "
        "잘못 만듭니다.\n"
        f"  → PM 홈에서 실행하세요:  cd {pm_home}\n"
        f"  (현재 앵커: {home.root})",
        file=sys.stderr,
    )
    return True


# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_finish.py",
        description="ticket 완료 시 PM 기록 자동화 헬퍼 (v1 축소판).",
    )
    parser.add_argument("ticket_id", metavar="T-NNNN", nargs="?", default=None,
                        help="완료 기록할 ticket ID (묶음 종결은 `--cluster`)")
    parser.add_argument(
        "--cluster",
        metavar="C-<ticket-group>",
        default=None,
        help="묶음 종결(close) — 기계 확인·게이트 처분·티켓별 완료 기록·커밋·재배치·머지·"
             "board 기록을 고정 순서로 실행한다. 실패 지점에서 멈추고, 다시 "
             "실행하면 끝난 단계는 건너뛴다(재실행이 곧 재개).",
    )
    parser.add_argument(
        "--section",
        metavar="섹션명",
        default=None,
        help=(
            "(deprecated·no-op) status.md 합계표 섹션 행은 제거됐다 — "
            "받기만 하고 무시한다(후방호환). status.md = judgment-only."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="편집·board·git 없이 무엇을 바꿀지만 출력한다.",
    )
    parser.add_argument(
        "--reconcile-integrated", action="store_true",
        help="이미 all-done인 open cluster를 제품 git evidence 검증 뒤 장부만 복구한다.",
    )
    parser.add_argument(
        "--integrated-rev", action="append", default=[], metavar="T-NNNN=<git-rev>",
        help="reconcile 멤버별 통합 evidence commit (멤버마다 정확히 1개).",
    )
    parser.add_argument(
        "--legacy-base-rev", action="append", default=[], metavar="T-NNNN=<git-rev>",
        help="claimed_rev 없는 legacy 멤버의 명시 anchor commit.",
    )
    parser.add_argument(
        "--user-ack", default=None, metavar="C-<ticket-group>",
        help="reconcile 대상 cluster ID와 정확히 같은 사용자 승인값.",
    )
    # ── 실행 정체성 `--task` · 복구 문 제품 트리 `--repo`/`--slot` ──────────
    # `--task` 는 멤버 `claimed_by` 와 대조하는 소유 확인 정체성이다 — 작업 트리는 묶음 기록의
    # 브랜치로 찾으므로 이 값으로 고르지 않는다. `--repo --slot` 은 복구 문 전용(복구할 제품 트리).
    identity_args.add_identity_args(parser)
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help=f"[1/5] 회귀 단계를 skip 한다 (측정은 {lease_store._runtime_skill_entry('pm-qa')} 등으로 "
             "별도·board complete 는 --tests-pass 유지). 회귀 실행만 skip — 코드 트리 해소·"
             "diff 측정·stage 는 그대로다.",
    )
    return parser


def main(home, argv: list[str] | None = None) -> int:
    """공개 argv 를 검증하고 명시 Home 으로 완료 기록/종결 파이프라인을 실행한다."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # PM-홈 worktree 오실행 가드— 기록의 어떤 부작용(회귀/log append/complete/stage)도
    # 나기 *전에* fail-loud. 읽기 경로 없음(ticket_finish 는 전부 쓰기 기록)이라 진입에서 한 번.
    if _guard_worktree_misanchor(home):
        return 1

    # 완료 기록 단위는 하나다 — 티켓 하나(현행)이거나 묶음 하나(종결)다. 둘 다 주면 어느
    # 단위로 도는지가 인자에 두 번 적힌 것이고, 둘 다 없으면 대상이 없다.
    if (args.ticket_id is None) == (args.cluster is None):
        parser.error(
            "완료 기록 대상은 하나다 — ticket ID 를 주거나 `--cluster <ticket-group>` 으로 묶음을 "
            "종결하라(둘 다 지정하거나 둘 다 생략할 수 없다)."
        )

    recovery_args_present = bool(
        args.reconcile_integrated or args.integrated_rev or args.legacy_base_rev
        or args.user_ack is not None
    )
    if recovery_args_present and not args.reconcile_integrated:
        parser.error("--integrated-rev/--legacy-base-rev/--user-ack은 --reconcile-integrated 전용이다.")
    if args.reconcile_integrated:
        if args.cluster is None:
            parser.error("--reconcile-integrated는 --cluster <ticket-group> 전용이다.")
        if args.task is not None or args.repo is None or args.slot is None:
            parser.error(
                "--reconcile-integrated는 --repo <repo> --slot <N> 둘 다 필수이고 --task를 받지 않는다."
            )

    # `--repo`/`--slot` 은 복구 문의 제품 트리 지정 전용이다 — parse_identity 의 bare-slot 오류보다
    # 먼저 거부해 `--repo` 를 추가하라는 잘못된 해결책을 안내하지 않는다.
    if not args.reconcile_integrated and (args.repo is not None or args.slot is not None):
        parser.error(
            "--repo/--slot 은 --reconcile-integrated 의 제품 트리 지정에만 쓴다 — 정상 종결의 "
            "정체성은 --task <task> 다."
        )
    # 정상 종결은 귀속 쓰기다(종결 log 머리말 `(task:이름)`·board complete 소유 대조) — `--task` 가
    # 없으면 어떤 쓰기보다 먼저 멈춘다. cwd·단일 활성 task 에서 정체성을 유도하지 않는다.
    if not args.reconcile_integrated and args.task is None:
        parser.error(
            "정상 종결은 --task <task> 가 필요하다 — 종결의 정체성(log 머리말·소유 대조)은 task "
            "하나다. `ticket_finish.py --cluster C-<ticket-group> --task <task>` 로 다시 실행하라."
        )

    # 정체성 인자 *검증*(`slot < 1` = uniform fail-loud)은 `--no-pytest` 와
    # 무관하게 **항상** 수행한다. 정체성은 소유 확인의 입력이라 회귀 skip 으로 건너뛰지 않는다.
    try:
        identity = identity_args.parse_identity(args)
    except ValueError as exc:
        parser.error(str(exc))

    # task 명 검증(must-fix 게이트) — board 정체성 깔때기와 **동일 공유 validator**
    # (`identity_args.validate_task_name`·`task_rules._validate_task_name`과 동형·로직 중복 0)를 소비해,
    # 실행-위치 해소 이전 불법 task 명(traversal/공백/`<repo>_<N>` 예약)을 fail-loud 한다. `--slot`
    # 검증과 동형으로 `--no-pytest` 무관 **항상** 수행(회귀 skip 여도 정체성 검증은 우회 안 됨). 예약패턴
    # 판정용 registered_repos 는 `areas_store.registered_repos` 로 fail-soft 해소(실패 시 char/traversal 검증만).
    if identity.task:
        try:
            registered = areas_store.registered_repos(home)
        except Exception:  # noqa: BLE001 — areas 파싱 실패는 예약패턴 검증만 완화(char/traversal 유지).
            registered = None
        try:
            identity_args.validate_task_name(identity.task, registered)
        except identity_args.InvalidTaskName as exc:
            print(
                f"\n[중단] 부적합 task 명 {identity.task!r} — {exc.reason} "
                "(`--task` 는 안전한 단일 이름이어야 하고 슬롯 예약패턴 `<repo>_<N>`은 쓸 수 없다).",
                file=sys.stderr,
            )
            return 1

    # 복구 문이 복구할 제품 트리(`work/<repo>_<N>`). 정상 종결의 작업 트리는 이 값이 아니라
    # 묶음 기록의 브랜치로 찾는다(`app.cluster_code_tree.resolve_code_tree`).
    close_slot: str | None = None
    # board 하위 호출(complete)에 실을 정체성 인자 — task-mode 귀속은 `<user>/<task>` 라 board 도
    # 같은 축으로 소유를 대조한다. 복구 문은 board 정체성 인자를 싣지 않는다.
    board_identity: list[str] = []
    if identity.task:
        board_identity = ["--task", identity.task]
    else:
        worktree_slot, problem = slot_resolution._resolve_explicit_identity_slot(
            home, identity.repo, identity.slot)
        if problem is not None:
            print(f"\n[중단] 복구 문의 제품 트리 해소 실패 — {problem}", file=sys.stderr)
            return 1
        close_slot = worktree_slot

    # 코드 트리는 여기서 정하지 않는다 — 종결이 묶음 기록의 브랜치로 찾아 넘긴다(`use_code_tree`).
    board_py = home.root / ".project_manager" / "tools" / "board.py"
    finisher = finish_record.TicketFinisher(home)
    # 완료 기록 단위는 **묶음 하나**다(특례 없음) — 티켓 하나를 준 호출도 그 티켓의 묶음으로
    # 해소해 같은 파이프라인으로 보낸다. 묶음을 선언하지 않은 티켓은 board 가 크기 1 로 접어
    # 주고(마이그레이션 0), 장부가 통합 브랜치를 선언하지 않았으면 판정 기준이 없어 멈춘다.
    # 티켓용 별도 코드 경로를 남기면 그 경로에서만 장부 기록·포인터 커밋이 빠진다.
    cluster = args.cluster
    if cluster is None:
        cluster, _ledger = finish_store.ticket_cluster_ledger(home, args.ticket_id)
        if not cluster:
            print(
                f"\n[중단] {args.ticket_id} 의 묶음을 해소하지 못했다 ({board_py}) — 완료 "
                "기록 단위는 묶음이다(티켓 하나면 크기 1). board 사본을 확인하라.",
                file=sys.stderr,
            )
            return 1
    return cluster_close.ClusterCloser(
        home,
        cluster,
        finisher=finisher,
        board_py=board_py,
        slot=close_slot,
        task=identity.task,
        board_identity_args=board_identity,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
        reconcile_integrated=args.reconcile_integrated,
        integrated_revisions=args.integrated_rev,
        legacy_base_revisions=args.legacy_base_rev,
        user_ack=args.user_ack,
        explicit_slot=(identity.kind == "slot"),
    ).run()
