"""위임 공개 명령 처리 (층: cli)."""
from __future__ import annotations

from collections.abc import Callable

from pathlib import Path
import argparse
from pm_engine.infra import cluster_store
from pm_engine.domain import conf_schema
from pm_engine.infra import conf_store
from pm_engine.infra import console
import datetime
from pm_engine.app import cluster_binding, cluster_code_tree, ticket_access
from pm_engine.cli import task_args
from pm_engine.domain import finish_rules, identity_args, install_rules
from pm_engine.infra import git
from pm_engine.app import delegate_changelog
from pm_engine.app import delegate_channel
from pm_engine.app import delegate_cluster_review
from pm_engine.infra import delegate_config
from pm_engine.domain import delegate_errors
from pm_engine.app import delegate_execute
from pm_engine.app import delegate_harvest
from pm_engine.infra import delegate_ledger
from pm_engine.app import delegate_prepare
from pm_engine.domain import delegate_rounds
from pm_engine.domain import delegate_run_rules
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra import fs
import json
from pm_engine.domain import model_lint
import os
from pm_engine.infra import pm_paths
from pm_engine.infra import raw_output
from pm_engine.domain import review_contract
from pm_engine.domain import review_form
from pm_engine.domain import review_state
from pm_engine.domain import round_rules
from pm_engine.app import round_seed
import sys
from pm_engine.domain import ticket_rules
from pm_engine.infra import ticket_store


def _cmd_raw_close(home, argv: list[str]) -> int:
    """명시한 미마감 raw 장부 레코드를 비정상 종료(rc=-1)로 마감한다."""
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py raw close",
        description="kill·비정상 종료로 남은 raw 장부 레코드 수동 마감",
    )
    parser.add_argument("record_ids", nargs="+", metavar="RECORD-ID")
    parser.add_argument(
        "--note", default="수동 마감(raw close)", help="마감 사유",
    )
    parser.add_argument(
        "--force", action="store_true", help="생존 PID가 있어도 마감",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="마감할 raw 출력 디렉터리(그 안의 raw_outputs.json 장부)",
    )
    args = parser.parse_args(argv)
    output_dir = Path(args.output_dir) if args.output_dir is not None else None
    _raw_dir, ledger_path = raw_output._raw_storage(home, output_dir)
    resolved_ledger = ledger_path.resolve()

    # 중복 id는 한 레코드에 대한 한 번의 마감 요청으로 정규화한다.
    record_ids = list(dict.fromkeys(args.record_ids))
    rows_by_id = {
        str(row.get("id")): row for row in raw_output.raw_records(ledger_path)
    }
    selected: list[tuple[dict, float]] = []
    current = datetime.datetime.now(datetime.timezone.utc)
    for record_id in record_ids:
        row = rows_by_id.get(record_id)
        if row is None:
            print(
                f"오류: raw 장부 레코드 미발견: {record_id} "
                f"(장부: {resolved_ledger})",
                file=sys.stderr,
            )
            return 1
        if row.get("finished_at") is not None:
            print(
                f"오류: raw 장부 레코드 이미 마감: {record_id}",
                file=sys.stderr,
            )
            return 1
        pid = row.get("pid")
        if not args.force and raw_output.pid_is_alive(pid):
            print(
                f"오류: raw 장부 레코드 PID가 실행 중이어서 마감 거부: "
                f"{record_id} (pid={pid}; 의도한 우회는 --force)",
                file=sys.stderr,
            )
            return 1
        started = raw_output._parse_raw_time(row.get("started_at"))
        if started is None:
            print(
                f"오류: raw 장부 started_at 형식 오류: {record_id}",
                file=sys.stderr,
            )
            return 1
        selected.append((row, (current - started).total_seconds()))

    for row, elapsed_sec in selected:
        record_id = str(row["id"])
        try:
            raw_output.finish_raw_record(
                ledger_path,
                record_id,
                rc=-1,
                elapsed_sec=elapsed_sec,
                silence_sec=None,
                now=current,
                finish_note=args.note,
            )
        except ValueError as exc:
            # 조회 후 다른 프로세스가 먼저 마감한 경합도 rc=1로 fail-loud.
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        print(f"raw 레코드 마감: {record_id}")
        if not any(
            str(item.get("id")) == record_id
            for item in raw_output.raw_records(ledger_path)
        ):
            print(
                f"경고: raw 레코드 {record_id}는 완료 보존창"
                f"({raw_output.RAW_LEDGER_COMPLETED_DAYS}일) 밖이어서 마감과 동시에 "
                "장부에서 제거됨"
            )
    print(f"마감 장부: {resolved_ledger}")
    return 0


def _print_orphan_raw_summary(
    home, output_dir: Path | None, ledger_path: Path, *, limit: int,
) -> None:
    """장부 미참조 엔진 명명 원문을 경고 1줄 + 읽기 전용 목록으로 표면화한다.

    삭제는 하지 않는다 — 목록화만 해서 사용자가 직접 처분하게
    한다. delegate·review 두 디렉터리를 모두 스캔한다(장부는 두 표면이 공유하는 한 파일).
    """
    repo_override = home.local_dir.parent.parent
    delegate_dir, _ = raw_output.raw_storage_paths(repo_override, "delegate", output_dir)
    review_dir, _ = raw_output.raw_storage_paths(repo_override, "review", output_dir)
    summary = raw_output.scan_orphan_raw_files((delegate_dir, review_dir), ledger_path)
    if summary.count == 0:
        return
    print(
        f"경고: 장부 미참조 원문 {summary.count}건 {summary.total_bytes}바이트 "
        "(엔진 명명 · 삭제 안 함 · 목록은 아래)"
    )
    shown = summary.paths[:limit]
    for path in shown:
        print(f"  · {path.resolve()}")
    omitted = summary.count - len(shown)
    if omitted > 0:
        print(f"  · (이하 {omitted}건 생략 — --limit 상향 시 더 표시)")


def _cmd_raw(home, argv: list[str]) -> int:
    """공유 장부의 raw 조회 또는 명시 레코드 마감을 수행한다."""
    if argv and argv[0] == "close":
        return _cmd_raw_close(home, argv[1:])
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py raw",
        description=(
            "최근 위임·추가 리뷰 raw 장부 조회; close로 미마감 레코드 수동 마감"
        ),
        epilog=(
            "마감 표면: close <RECORD-ID> ... "
            "[--note NOTE] [--force] [--output-dir DIR]"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--unfinished", action="store_true", help="미마감 레코드만 표시"
    )
    parser.add_argument("--limit", type=int, default=20, help="최대 표시 건수(기본 20)")
    parser.add_argument(
        "--output-dir",
        default=None,
        metavar="DIR",
        help="조회할 raw 출력 디렉토리(그 안의 raw_outputs.json 장부)",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit은 양수여야 합니다")
    output_dir = Path(args.output_dir) if args.output_dir is not None else None
    _raw_dir, ledger_path = raw_output._raw_storage(home, output_dir)
    resolved_ledger = ledger_path.resolve()
    print(f"조회 장부: {resolved_ledger}")
    for peer_ledger in raw_output._peer_engine_ledgers(
        home, None if home.local_dir.parent.parent == home.root else home.local_dir.parent.parent,
    ):
        if peer_ledger != resolved_ledger:
            print(
                "경고: 다른 엔진 사본 장부가 있습니다"
                f"(이 조회에서는 읽지 않음): {peer_ledger}"
            )
    rows = raw_output.raw_records(
        ledger_path, unfinished_only=args.unfinished
    )[:args.limit]
    label = "미마감 raw" if args.unfinished else "최근 raw"
    if not rows:
        print(f"{label} 없음")
    else:
        print(f"{label} {len(rows)}건")
        for row in rows:
            status = (
                f"완료(rc={row.get('rc')})"
                if row.get("finished_at") is not None else "미마감"
            )
            print(
                f"{row.get('started_at')} · {status} · {row.get('surface')} · "
                f"{row.get('harness')} · role={row.get('role')} · "
                f"pid={row.get('pid')} · raw={row.get('raw_path')}"
            )
    # 고아(장부 미참조) 원문 표면화는 rows 유무와 독립이다 — 장부가 텅 비어 있어도(전량 prune)
    # 디스크에 원문이 남아 있을 수 있고 그게 바로 이 티켓이 잡는 역전이다.
    _print_orphan_raw_summary(home, output_dir, ledger_path, limit=args.limit)
    return 0

# ── config lint (동일-모델 dev/reviewer 경고·never-block) ────────────────


def _cmd_lint(home, argv: list[str]) -> int:
    """`pm_delegate.py lint` — 동일-모델 dev/reviewer 경고(never-block·항상 rc=0).

    설정 정합 점검일 뿐 강제가 아니다 — 경고가 있어도 rc=0(차단 금지). 경고는 stderr, 정합 시
    안내는 stdout. board lint advisory 훅과 짝을 이루는 명시 진입점."""
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py lint",
        description="delegate 설정 정합 점검 — 동일-모델 dev/reviewer 경고(never-block).")
    parser.parse_args(argv)  # 현재 플래그 0(미래 확장 여지·미지원 인자는 usage error).
    conf = conf_store.local_config(home)
    findings = model_lint.lint_same_model(conf)
    if not findings:
        print("delegate lint: 동일-모델 dev/reviewer 경고 없음(설정 정합).")
        return 0
    for _label, detail in findings:
        print(f"경고: {detail}", file=sys.stderr)
    return 0


# 묶음 조회의 티켓 구분선 — 세 렌더가 같은 한 줄로 이어 붙는다(PM 이 어느 티켓의 출력인지
# 산문 없이 안다). 크기 1 묶음도 같은 줄을 낸다(특례 없음).
_REVIEW_CLUSTER_TICKET_HEADER = "# {ticket}"


def _add_review_target_args(parser: argparse.ArgumentParser) -> None:
    """조회 대상 = 티켓 하나 또는 묶음 하나 — 세 서브커맨드가 같은 인자 쌍을 쓴다.

    `--cluster` 는 새 파서가 아니라 **같은 렌더를 티켓마다 반복**하는 입력이다(판정식 사본 0).
    """
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ticket", metavar="T-NNNN")
    target.add_argument(
        "--cluster", metavar="C-<ticket-group>",
        help="묶음 멤버 전부의 출력을 티켓별로 이어 낸다(내부는 티켓 반복)",
    )


def build_subcommand_parser(command: str) -> argparse.ArgumentParser | None:
    """flat delegate parser 밖 special command의 실제 argparse 표면을 introspection에 공개한다.

    문서↔CLI 존재 가드는 `main()`의 선행 dispatch를 실행하지 않고 parser만 읽어야 한다. 실행과
    검사가 다른 parser를 만들면 문서의 중첩 flag가 실재해도 미등록으로 오판하거나, 반대로 검사만
    green인 가짜 표면이 생긴다. 현재 공개 대상은 라운드 사본 `ticket`이고 소비자는 반환 parser에
    command 뒤 argv를 그대로 넣는다.
    """
    if command == "review":
        parser = argparse.ArgumentParser(
            prog="pm_delegate.py review",
            description="PM disposition을 적용한 accepted-only reviewer delta",
        )
        sub = parser.add_subparsers(dest="review_command", required=True)
        delta = sub.add_parser(
            "delta", help="티켓 라운드에서 developer에게 허용된 finding만 렌더"
        )
        _add_review_target_args(delta)
        disposition = sub.add_parser(
            "disposition-template",
            help="리뷰 라운드의 미판정 finding ID를 PM disposition 골격으로 렌더",
        )
        _add_review_target_args(disposition)
        disposition.add_argument(
            "--ordinal", type=int, default=None, metavar="N",
            help="대상 reviewer ordinal (기본: 최신 절)",
        )
        disposition.add_argument(
            "--reviewer-role", default=None, choices=review_form.REVIEW_ROLES,
            help="대상 리뷰 채널 (기본: 두 채널 통틀어 최신 절의 채널)",
        )
        verify = sub.add_parser(
            "verify-template",
            help="accepted finding 의 기계 확인 대상을 pm-review-confirmation-v1 골격으로 렌더",
        )
        _add_review_target_args(verify)
        verify.add_argument(
            "--round", type=int, default=None, metavar="N", dest="round_ordinal",
            help=(
                "그 developer 라운드 시점까지의 누적으로 판정 (기본: 티켓 전역 누적)"
            ),
        )
        return parser
    if command == "changelog":
        parser = argparse.ArgumentParser(
            prog="pm_delegate.py changelog",
            description="완료 티켓 본문에서 릴리즈 노트 재료 추출(문안은 PM)",
        )
        sub = parser.add_subparsers(dest="changelog_command", required=True)
        material = sub.add_parser(
            "material",
            help="완료 시점이 지정 rev 이후인 done 티켓의 목표·결정·완료 조건을 재료로 낸다",
        )
        material.add_argument(
            "--since", required=True, metavar="<tag|rev>",
            help="구간 시작 — 코드 체크아웃에서 이 rev 의 커밋 시각을 해소해 완료 시점과 비교한다",
        )
        return parser
    if command != "ticket":
        return None
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py ticket",
        description="slot 라운드 파일 준비·PM 홈 board 라운드 회수",
    )
    sub = parser.add_subparsers(dest="ticket_command", required=True)
    prepare = sub.add_parser(
        "prepare", help="board 에 라운드 순번을 예약하고 slot run-dir 을 준비",
    )
    # 준비 단위는 묶음이다 — `--ticket` 은 크기 1 묶음을 가리키는 표기이고 내부 경로가 같다.
    target = prepare.add_mutually_exclusive_group(required=True)
    target.add_argument("--ticket", metavar="T-NNNN")
    target.add_argument(
        "--cluster", metavar="C-<ticket-group>",
        help="클러스터 장부의 멤버 전부에 라운드를 예약한다(run-dir 1 · 라운드 파일 N)",
    )
    prepare.add_argument(
        "--role", required=True,
        choices=tuple(sorted((delegate_ledger.TICKET_COPY_ROLES - {review_form.ADDITIONAL_REVIEWER_ROLE, "researcher"}))),
        help="code-reviewer 는 --cluster 로만 준비한다(리뷰 단위 = 묶음 · 엔진 프롬프트 파일을 낸다)",
    )
    prepare.add_argument(
        "--focus", default=None, metavar="PATH",
        help="PM 검토 중점 문단 파일(code-reviewer 전용) — 엔진 프롬프트에 그대로 실린다",
    )
    prepare.add_argument(
        "--tier", default=None, choices=delegate_run_rules.TIER_CHOICES,
        help="developer 2티어(normal/hard) — 그 외 역할은 항상 normal 로 강제(cross 채널 판정 입력, "
             "flat CLI와 같은 규칙)",
    )
    prepare.add_argument(
        review_state.PREPARE_REOPEN_ORDINAL_FLAG, type=int, default=None, dest="reopen_ordinal",
        metavar="N",
        help=(
            "이미 예약된 순번 N 을 다시 연다(이어 시키는 단계 — 새 순번을 만들지 않는다). "
            "그 순번이 실재하고 역할이 --role 과 같아야 하며, 슬롯 사본은 board 라운드 파일의 "
            "현재 내용으로 시드된다(회수가 그 파일을 교체하고 지나간 판은 board git 이 갖는다). "
            f"{delegate_rounds.ABANDON_DISCARD_REASON_FLAG} 로 폐기한 순번을 다시 시키려면 이 값으로 재개방한 "
            f"뒤 슬롯 사본에서 `{review_form.PM_REVIEW_REFUSED_MARKER}` 표식 줄을 지운다 — 회수는 그 줄을 "
            "실은 회신을 거부한다"
        ),
    )
    prepare.add_argument("--cwd", required=True, metavar="ABSPATH")
    harvest = sub.add_parser(
        "harvest", help="slot 라운드 파일로 board 라운드 파일을 교체하고 run 을 닫음",
    )
    harvest.add_argument(
        "--copy", required=True, metavar="ABSPATH",
        help="라운드 파일 하나 또는 run-dir(디렉터리 인자는 그 run 의 티켓 전부를 회수한다)",
    )
    harvest.add_argument("--cwd", required=True, metavar="ABSPATH")
    abandon = sub.add_parser(
        "abandon",
        help="kill 로 죽은 예약을 포기 — 장부 행 마감 + board 라운드 정리 + run-dir 닫음",
    )
    abandon.add_argument("--copy", required=True, metavar="ABSPATH")
    abandon.add_argument("--cwd", required=True, metavar="ABSPATH")
    abandon.add_argument(
        delegate_rounds.ABANDON_ASSUME_DEAD_FLAG, action="store_true", dest="assume_dead",
        help="run 이 끝났음을 운영자가 확인했다 (종료 증거가 없거나 소유 pid 가 살아 있어도 진행)",
    )
    abandon.add_argument(
        delegate_rounds.ABANDON_SUPERSEDED_BY_FLAG, type=int, default=None, dest="superseded_by_ordinal",
        metavar="N",
        help=(
            "이 라운드는 재실행된 ordinal N 라운드로 대체됐다 — 산출이 시드와 달라도 포기를 "
            "허용한다(생존 확인은 별도 축 — --assume-dead 필요 여부는 그대로)"
        ),
    )
    abandon.add_argument(
        delegate_rounds.ABANDON_DISCARD_REASON_FLAG, default=None, dest="discard_reason", metavar="사유",
        help=(
            "이 라운드를 폐기한다(재실행 대체가 아니다) — 산출이 있으면 board 라운드 파일로 "
            "옮겨 표식을 붙이고, 사본도 board 파일도 없는 행은 이 사유로 장부만 닫는다. "
            f"{delegate_rounds.ABANDON_SUPERSEDED_BY_FLAG} 와 함께 줄 수 없다. 폐기한 순번은 예산 한 칸을 "
            f"계속 차지하므로, 그 단계를 다시 시키려면 {review_state.PREPARE_REOPEN_ORDINAL_FLAG} 로 "
            f"재개방한 뒤 슬롯 사본에서 `{review_form.PM_REVIEW_REFUSED_MARKER}` 표식 줄을 지운다"
        ),
    )
    copies = sub.add_parser("copies", help="PM 홈 delegate-rounds 장부 조회")
    copies.add_argument("--ticket", default=None, metavar="T-NNNN")
    copies.add_argument(
        "--unharvested", action="store_true", help="미회수 준비만 표시",
    )
    return parser


# cross 채널 판정이 "결정 못함"으로 접는 allow reason — 이 둘만 침묵 통과다(그 밖 verdict!=deny 는
# 판정불능이라 stderr 경고 1줄을 낸다. 불변식 4). native harness 일치·역할 harness 미설정은 decide()가
# 이미 확정적으로 답한 상태라 노이즈를 더하지 않는다.
_PREPARE_CROSS_ROLE_SILENT_ALLOW_PREFIXES = (
    "[delegate-channel/allow] conf 와 native harness 일치",
    "[delegate-channel/allow] 역할 harness 미설정",
)

# 이 게이트가 거부로 접는 유일한 deny 는 매핑된 하네스가 PM 하네스와 달라 cross 인 경우
# (decide() Row 5)뿐이다 — 그 판정은 verdict=="deny" 와 함께 항상 비어 있지 않은 harness/model 을
# 실어 온다(resolve_delegate 가 harness·model 을 원자 tuple 로만 해소하므로 하나만 비는 경우가
# 없다). 마스터 스위치 꺼짐(Row 0.5, "위임이 꺼져 있습니다")은 verdict=="deny" 이되 harness·model 이
# 둘 다 비어 있고, 여기 도달하기 전에 호출부가 owner_conf 로 이미 걸러낸다(별개 축이라 여기서
# 거부하지 않되 침묵하지도 않는다) — 사용자용 한국어 사유 문자열은 진단 출력에만 쓰고 정책 분기에는
# 쓰지 않는다(문구가 바뀌어도 verdict/harness/model 구조는 안정적이다).


def _reject_cross_role_prepare(home, role: str, tier: str, conf: dict[str, str]) -> None:
    """cross 역할(PM 하네스 ≠ 매핑된 하네스) 수동 prepare 를 거부한다.

    판정식은 `delegate_channel.decide`(native Agent 위임 훅과 같은 seam) 하나 — 여기서
    하네스 비교를 다시 쓰지 않는다(불변식 1·2). `conf` 는 호출부가 이미 owner(PM 홈)에서 읽어 마스터
    스위치 판정과 함께 쓴 값을 그대로 받는다(불변식 3 — 실행 엔진 사본 repo 로 읽으면 역할 매핑이 없는
    사본에서 조용히 no-op). 거부는 `verdict=="deny"` 중에서도 cross-harness 불일치 사유뿐이고,
    그 구별은 사용자용 사유 문자열이 아니라 구조 필드(harness/model 이 비어 있지 않음)로 한다(불변식 1).
    PM 하네스 미상이나 실제 판정 실패는 기존 fail-open 경고로 보고한다.
    """
    try:
        pm_harness = delegate_execute._session_harness(os.environ) or ""
        result = delegate_channel.decide(home, role, tier, conf, pm_harness)
    except Exception as exc:
        detail = " ".join(str(exc).splitlines()).strip() or type(exc).__name__
        print(
            "[pm-delegate/warn] cross 역할 채널 판정 불가"
            f"(가드 로드/실행 실패: {type(exc).__name__}: {detail}) — prepare 통과(fail-open)",
            file=sys.stderr,
        )
        return
    reason = result.get("reason", "")
    is_cross_harness_deny = (
        result.get("verdict") == "deny"
        and result.get("harness")
        and result.get("model")
    )
    if is_cross_harness_deny:
        target = (
            "--cluster C-<ticket-group>" if role == review_form.INTERNAL_REVIEW_ROLE else "--ticket <T-NNNN>"
        )
        raise delegate_errors.DelegateError(
            f"cross 역할은 수동 prepare 가 거부됩니다({reason}) — 고아 시드를 만들지 않는다. "
            f"이 역할의 위임은 실 실행(pm_delegate.py --role {role} {target} --cwd <worktree> ...)이 "
            "안에서 자동으로 prepare 한다."
        )
    if not reason.startswith(_PREPARE_CROSS_ROLE_SILENT_ALLOW_PREFIXES):
        print(f"[pm-delegate/warn] {reason} — prepare 통과(fail-open)", file=sys.stderr)


def _review_prompt_path(owner: Path, run_id: str) -> Path:
    """native 리뷰 프롬프트 파일 자리 — PM 홈 위임 raw 디렉터리 아래 run 마다 하나."""
    delegate_dir, _ledger = raw_output.raw_storage_paths(owner, "delegate")
    return Path(delegate_dir) / f"review-prompt-{run_id}.md"


def _prepare_native_review(
    home, cluster: str, *, cwd: Path, focus_path: Path | None,
    reopen_ordinal: int | None,
) -> int:
    """같은 하네스 리뷰 준비 — 교차 실행과 같은 준비·조립으로 프롬프트 파일을 쓴다.

    프롬프트 파일은 PM 홈 `.local/delegate/` 아래 O_EXCL·소유자 전용으로 한 번 쓴다. 멤버마다
    JSON 한 줄(`cluster·copy·ordinal·prompt·run_dir·ticket`)을 낸다 — 같은 run 의 줄은 같은
    프롬프트 경로다. PM 은 이 파일 경로를 native 리뷰어에게 넘기고 끝나면 run-dir 을 돌려받는다.
    """
    owner = home.local_dir.parent.parent
    sources, plan = delegate_cluster_review.prepare_cluster_review(
        home, cluster=cluster, cwd=cwd, focus_path=focus_path,
        owner_pid=None, reopen_ordinal=reopen_ordinal,
    )
    adapter_roots = install_rules.adapter_directories()
    prompt = delegate_cluster_review.cluster_review_prompt(
        home, sources, tree=cwd, plan=plan, adapter_roots=adapter_roots,
    )
    prompt_path = _review_prompt_path(owner, plan.run_id)
    try:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        delegate_temp._portable_exclusive_write(prompt_path, prompt)
    except OSError as exc:
        raise delegate_errors.DelegateError(
            f"리뷰 프롬프트 파일 쓰기 실패: {prompt_path}: {exc} — 준비한 run-dir 은 "
            f"`ticket abandon --copy <사본> --cwd {cwd} {delegate_rounds.ABANDON_ASSUME_DEAD_FLAG}` 로 닫습니다: "
            f"{plan.run_dir}"
        ) from exc
    for round_plan in plan.rounds:
        console.write_machine_line(json.dumps({
            "cluster": plan.cluster,
            "copy": str(round_plan.path),
            "ordinal": round_plan.ordinal,
            "prompt": str(prompt_path),
            "run_dir": str(plan.run_dir),
            "ticket": round_plan.ticket,
        }, sort_keys=True))
    print(
        f"ticket prepare: {plan.cluster} 리뷰 라운드 {len(plan.rounds)}건 · "
        f"run-dir={plan.run_dir} · 프롬프트={prompt_path}"
    )
    print(
        f"  돌려받기: pm_delegate.py ticket harvest --copy {plan.run_dir} --cwd {cwd}"
    )
    return 0


def _cmd_ticket(home, argv: list[str]) -> int:
    """native 위임도 cross와 같은 사본 helper를 쓰는 prepare/harvest CLI."""
    parser = build_subcommand_parser("ticket")
    assert parser is not None  # 같은 모듈의 literal command — introspection/실행 단일 parser.
    args = parser.parse_args(argv)
    try:
        if args.ticket_command == "copies":
            if args.ticket is not None and not ticket_rules._is_valid_ticket_id(args.ticket):
                parser.error("--ticket은 board 발행 ticket ID 형식이어야 합니다")
            owner = pm_paths.resolve_pm_home_for_repo(home.local_dir.parent.parent)
            home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
            ledger = delegate_ledger._delegate_rounds_ledger_path(owner)
            print(f"조회 장부: {ledger}")
            rows = delegate_ledger.ticket_copy_records(
                owner, ticket=args.ticket, unharvested=args.unharvested,
            )
            label = "미회수 라운드 준비" if args.unharvested else "최근 라운드 준비"
            if not rows:
                print(f"{label} 없음")
                return 0
            print(f"{label} {len(rows)}건")
            for row in rows:
                if row["harvested_at"] is not None:
                    status = f"회수({row['harvested_at']})"
                elif "abandoned_at" in row:
                    status = f"포기({row['abandoned_at']})"
                else:
                    status = "미회수"
                print(
                    f"{row['prepared_at']} · {status} · {row['ticket']} · "
                    f"role={row['role']} · ordinal={row['ordinal']} · "
                    f"run_id={row['run_id']} · copy={row['copy']}"
                )
            return 0
        cwd = Path(args.cwd)
        cwd_repo = pm_paths._repo_root_for_cwd(cwd)
        owner = delegate_execute._ticket_cli_owner(home, cwd_repo)
        home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
        if args.ticket_command == "prepare":
            # 위임 마스터 스위치 — **준비 단계에서** 막는다. native 위임도 이 CLI 로 라운드 파일을
            # 준비하므로(스킬 규정) 여기가 엔진이 확실히 차단할 수 있는 지점이다. run-dir 생성·
            # 장부 순번 예약보다 앞이라 off 형상에서 고아 산출물이 남지 않는다.
            # `harvest`·`copies` 는 게이트 밖이다 — 이미 준비된 라운드를 회수/조회하는 길까지
            # 막으면 스위치를 끄는 순간 진행 중 라운드가 고아가 된다.
            # owner_conf 를 한 번만 읽어 마스터 스위치와 cross 판정에 함께 넘긴다 — 실행 엔진
            # 사본(REPO) conf 는 판정에 관여하지 않는다(불변식 3: owner 가 provenance 단일 진실).
            owner_conf = conf_store.local_config(home)
            if not delegate_config._is_enabled(owner_conf):
                print(
                    "위임이 꺼져 있습니다 — 채널(native/cross) 무관 "
                    f"(local.conf: {delegate_execute._local_conf_path(home)} · {delegate_run_rules.DELEGATE_ENABLED_KEY}=false).\n"
                    f"켜기: local.conf 에서 `{delegate_run_rules.DELEGATE_ENABLED_KEY}=true` "
                    "(또는 그 줄을 지우면 기본 허용).",
                    file=sys.stderr,
                )
                return 3
            if args.ticket and not ticket_rules._is_valid_ticket_id(args.ticket):
                parser.error("--ticket은 board 발행 ticket ID 형식이어야 합니다")
            if args.cluster and not delegate_ledger._is_valid_cluster_id(args.cluster):
                parser.error("--cluster는 board 클러스터 장부 id 형식이어야 합니다")
            if args.role == review_form.INTERNAL_REVIEW_ROLE and not args.cluster:
                parser.error("code-reviewer 는 --cluster 로만 준비합니다(리뷰 단위 = 묶음)")
            if args.focus is not None and args.role != review_form.INTERNAL_REVIEW_ROLE:
                parser.error("--focus 는 code-reviewer 준비 전용입니다")
            tier = args.tier if (args.role in conf_schema.TIERED_ROLES and args.tier) else "normal"
            _reject_cross_role_prepare(home, args.role, tier, owner_conf)
            if args.reopen_ordinal is not None and args.reopen_ordinal < 1:
                parser.error(
                    f"{review_state.PREPARE_REOPEN_ORDINAL_FLAG} 은 1 이상의 라운드 순번이어야 합니다"
                )
            if args.role == review_form.INTERNAL_REVIEW_ROLE:
                return _prepare_native_review(
                    home, args.cluster, cwd=cwd_repo, focus_path=Path(args.focus) if args.focus else None,
                    reopen_ordinal=args.reopen_ordinal,
                )
            if args.cluster:
                cluster_plan = delegate_prepare.prepare_cluster_copy(
                    home, cluster=args.cluster, role=args.role, cwd=cwd_repo, reopen_ordinal=args.reopen_ordinal,
                )
                for round_plan in cluster_plan.rounds:
                    console.write_machine_line(json.dumps({
                        "cluster": cluster_plan.cluster,
                        "copy": str(round_plan.path),
                        "ordinal": round_plan.ordinal,
                        "run_dir": str(cluster_plan.run_dir),
                        "ticket": round_plan.ticket,
                    }, sort_keys=True))
                print(
                    f"ticket prepare: {cluster_plan.cluster} 라운드 "
                    f"{len(cluster_plan.rounds)}건 · run-dir={cluster_plan.run_dir}"
                )
                return 0
            plan = delegate_prepare.prepare_ticket_copy(
                home, ticket=args.ticket, role=args.role, cwd=cwd_repo, reopen_ordinal=args.reopen_ordinal,
            )
            console.write_machine_line(json.dumps({
                "copy": str(plan.path),
                "ordinal": plan.ordinal,
                "run_dir": str(plan.run_dir),
            }, sort_keys=True))
            return 0
        copy = Path(args.copy)
        if not copy.is_absolute():
            parser.error("--copy 는 prepare가 출력한 절대경로여야 합니다")
        if args.ticket_command == "abandon":
            abandon_result = delegate_prepare.abandon_ticket_copy(
                home, copy_path=copy, cwd=cwd_repo, assume_dead=args.assume_dead,
                superseded_by_ordinal=args.superseded_by_ordinal,
                discard_reason=args.discard_reason,
            )
            console.write_machine_line(json.dumps({
                "copy": str(copy.resolve()),
                "changed": abandon_result.changed,
                "sync_ready": abandon_result.sync_ready,
                "board_removed": abandon_result.board_removed,
                "run_dir_removed": abandon_result.run_dir_removed,
                "converged": abandon_result.converged,
            }, sort_keys=True))
            if not abandon_result.converged:
                # 수렴 단언이 깨진 상태는 rc 로 말한다 — 재호출이 남은 작업을 끝낸다(rollback 없음).
                print(
                    "오류: ticket abandon 미수렴 — 같은 명령을 다시 실행하세요: "
                    f"copy={copy}",
                    file=sys.stderr,
                )
                return 1
            return 0
        if copy.is_dir():
            # 디렉터리 인자 = run-dir. 행마다 같은 회수 판정을 통과시키고 결과를 티켓별로 낸다.
            outcomes = delegate_harvest.harvest_cluster_copy(
                home, run_dir=copy, cwd=cwd_repo, )
            for outcome in outcomes:
                if outcome.refusal is not None:
                    print(
                        f"오류: ticket harvest 실패: {outcome.refusal}", file=sys.stderr)
                    continue
                console.write_machine_line(json.dumps({
                    "changed": outcome.result.changed,
                    "copy": str(outcome.copy),
                    "sync_ready": outcome.result.sync_ready,
                    "ticket": outcome.ticket,
                    "verify_missing": list(outcome.result.verify_missing),
                }, sort_keys=True))
            replaced = sum(
                1 for item in outcomes if item.result is not None and item.result.changed)
            unedited = sum(
                1 for item in outcomes
                if item.result is not None and not item.result.changed)
            refused = sum(1 for item in outcomes if item.refusal is not None)
            print(
                f"ticket harvest: 교체 {replaced} · 산출 없음 {unedited} · 거부 {refused} "
                f"(run-dir={copy})"
            )
            return 1 if refused else 0
        result = delegate_harvest.harvest_ticket_copy(
            home, copy_path=copy, cwd=cwd_repo, )
        console.write_machine_line(json.dumps({
            "copy": str(copy.resolve()),
            "changed": result.changed,
            "sync_ready": result.sync_ready,
            "verify_missing": list(result.verify_missing),
        }, sort_keys=True))
        return 0
    except delegate_errors.DelegateError as exc:
        print(f"오류: ticket {args.ticket_command} 실패: {exc}", file=sys.stderr)
        return 1


def _emit_pm_review_verify_template(template: review_state.PMReviewVerifyTemplate) -> int:
    """`review verify-template` 의 출력과 rc — rc 는 판정 함수가 아니라 이 자리가 정한다.

    rc≠0 은 `missing`(태만)·`stale`(확인 창을 지난 선언) 두 상태뿐이다. `gap`(빈틈 보고)과
    `reviewer`(사람 확인 전용)는 정상 산출이라 rc=0 이고 stderr 로 크게 알린다. 확인 골격은 rc 와
    무관하게 **먼저** 낸다 — 해소된 finding 의 부분 확인이 다른 finding 의 상태에 인질로 잡히면
    다음 fix 라운드가 이미 끝난 finding 을 다시 싣는다.
    """
    rendered = review_contract.render_pm_review_verify_template(template)
    if rendered:
        sys.stdout.write(rendered)
    for finding_id, source_round, reason in template.reviewer_required:
        print(
            f"안내: {finding_id} 는 기계 확인 대상이 아닙니다 — {reason} "
            f"(선언 round={source_round})",
            file=sys.stderr,
        )
    for finding_id, source_round, summary in template.gap:
        print(
            f"안내: {finding_id} 는 빈틈 보고(reason={review_form.PM_REVIEW_VERIFY_GAP_REASON}) 상태입니다 — "
            f"선언 round={source_round} · 요지: {summary} · 보강 처방을 낸 뒤 다음 fix 라운드로 "
            "보내세요(이번 라운드 무활동은 정상 산출입니다)",
            file=sys.stderr,
        )
    if template.stale:
        detail = ", ".join(
            f"{finding_id}(선언 round={source_round} ≤ 확인 커서={cursor})"
            for finding_id, source_round, cursor in template.stale
        )
        print(
            f"오류: 확인 창을 지난 verify 행: {detail} — 그 ID 는 다음 developer 라운드에서 "
            "verify 행을 다시 선언해야 기계 확인이 가능합니다. 관측이 기대와 달라 이 상태가 "
            "됐다면 기대값이 낡은 것과 회귀 두 가설이 있습니다 — 같은 커맨드를 손으로 1회 "
            "재현해 가른 뒤, 어느 쪽이든 그 ID 의 행을 다음 developer 라운드에서 다시 "
            "선언하세요(확인 블록의 round 는 티켓 전역 고유 키라 같은 순번으로 다시 적을 수 "
            "없습니다)",
            file=sys.stderr,
        )
    if template.missing:
        print(
            "오류: verify 행이 없는 accepted finding(자리표시자 그대로 포함): "
            f"{', '.join(template.missing)} — developer 라운드의 검증 골격을 accepted ID "
            f"전수로 채우세요(처방 빈틈으로 구현하지 않았다면 reason="
            f"{review_form.PM_REVIEW_VERIFY_GAP_REASON} 행으로 선언하게 하세요)",
            file=sys.stderr,
        )
    return 1 if (template.stale or template.missing) else 0


def _cmd_review(home, argv: list[str]) -> int:
    """review delta/disposition-template/verify-template — read-only 구조화 렌더 CLI.

    대상은 티켓 하나 또는 묶음 하나다. 묶음은 **같은 렌더의 티켓별 반복**이고 rc 는 하나라도
    실패하면 1 이다(부분 성공을 성공으로 접지 않는다).
    """
    parser = build_subcommand_parser("review")
    assert parser is not None
    args = parser.parse_args(argv)
    if args.ticket is not None and not ticket_rules._is_valid_ticket_id(args.ticket):
        parser.error("--ticket은 board 발행 ticket ID 형식이어야 합니다")
    if args.cluster is not None and not delegate_ledger._is_valid_cluster_id(args.cluster):
        parser.error("--cluster는 board 클러스터 장부 id 형식이어야 합니다")
    if args.ticket is not None:
        return _cmd_review_ticket(home, args, args.ticket)
    try:
        owner = pm_paths.resolve_pm_home_for_repo(home.local_dir.parent.parent)
        home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
        members = cluster_store.cluster_live_members(home, args.cluster)
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: review {args.review_command} 실패: {exc}", file=sys.stderr)
        return 1
    if not members:
        print(
            f"오류: 클러스터 멤버가 없습니다: {args.cluster} — "
            f"`board.py cluster show {args.cluster}` 로 장부를 확인하세요",
            file=sys.stderr,
        )
        return 1
    rc = 0
    for ticket in members:
        print(_REVIEW_CLUSTER_TICKET_HEADER.format(ticket=ticket))
        rc = max(rc, _cmd_review_ticket(home, args, ticket))
    return rc


def _cmd_review_ticket(home, args: argparse.Namespace, ticket: str) -> int:
    """대상 티켓 하나의 렌더 — 묶음 반복도 이 한 판정을 그대로 쓴다."""
    ticket_text: str | None = None
    try:
        owner = pm_paths.resolve_pm_home_for_repo(home.local_dir.parent.parent)
        home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
        with fs.board_lock(home):
            found = ticket_store.find_ticket_exact(home, ticket)
            if found is None:
                raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
            status, path = found
            if status not in ("open", "claimed"):
                raise delegate_errors.DelegateError(
                    f"review delta는 open/claimed 티켓만 허용: {ticket} in {status}/"
                )
            try:
                with fs.open_shared(path, binary=False, encoding="utf-8", newline="") as handle:
                    ticket_text = handle.read()
            except (OSError, UnicodeError) as exc:
                raise delegate_errors.DelegateError(f"티켓 읽기 실패: {path}: {exc}") from exc
            rounds = round_seed.load_rounds(
                pm_paths.tickets_dir(home), ticket, ticket_text=ticket_text,
            )
            problems = round_seed.verify_rounds(
                pm_paths.tickets_dir(home), ticket, ticket_text=ticket_text,
            )
        # 순번 유일성·연속성이 깨진 상태만 차단한다 — 산출 없음(`round-pending`)과 이름 문법
        # (`round-name`)은 표시용이라 판정을 막지 않는다(심각도는 소비자 소유다).
        blocking = [
            problem for problem in problems
            if problem.code in (
                round_rules.PROBLEM_GAP, round_rules.PROBLEM_DUPLICATE,
            )
        ]
        if blocking:
            raise review_form.PMReviewError(
                "unsealed",
                "; ".join(f"[{item.code}] {item.detail}" for item in blocking),
            )
        if args.review_command == "disposition-template":
            rendered = review_contract.render_pm_review_disposition_template(
                ticket_text, rounds, args.ordinal, reviewer_role=args.reviewer_role,
            )
        elif args.review_command == "verify-template":
            template = review_state.pm_review_verify_template(
                ticket_text, rounds, round_ordinal=args.round_ordinal,
            )
            return _emit_pm_review_verify_template(template)
        else:
            delta = review_state.parse_pm_review_delta(ticket_text, rounds)
            rendered = review_contract.render_pm_review_delta(ticket, delta)
        if rendered:
            sys.stdout.write(rendered)
        return 0
    except review_form.PMReviewError as exc:
        print(
            f"오류: review {args.review_command} 거부[{exc.code}]: {exc}\n"
            f"  · {review_form._pm_review_prescription(exc.code, ticket, channels=exc.channels)}",
            file=sys.stderr,
        )
        return 1
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: review {args.review_command} 실패: {exc}", file=sys.stderr)
        return 1


def _cmd_changelog(home, argv: list[str], git_run_fn: Callable | None = None) -> int:
    """`changelog material --since <tag|rev>` — 완료 티켓 재료를 stdout 으로(board 무변경)."""
    parser = build_subcommand_parser("changelog")
    assert parser is not None
    args = parser.parse_args(argv)
    try:
        owner = pm_paths.resolve_pm_home_for_repo(home.local_dir.parent.parent)
        home = pm_paths.home_at(home.root, pm_home=owner, board_root=pm_paths._board_root_at(owner))
        checkout = delegate_changelog.changelog_code_checkout(home, conf_store.local_config(home), )
        since = delegate_changelog.changelog_since_instant(checkout, args.since, git_run_fn=git_run_fn)
        materials = delegate_changelog.changelog_material(
            home, since=since,
        )
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: changelog {args.changelog_command} 실패: {exc}", file=sys.stderr)
        return 1
    rendered = delegate_changelog.render_changelog_material(materials)
    if rendered:
        sys.stdout.write(rendered)
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py",
        description="역할 위임 채널 (마스터 스위치 delegate.enabled·기본 ON·채널 무관)",
        epilog=(
            "local.conf loud 폴백 예시(엔진 기본값 아님):\n"
            "  delegate.developer.fallback.harness=claude\n"
            "  delegate.developer.fallback.model=opus\n"
            "hard 티어는 delegate.developer.hard.fallback.* 처럼 별도 완전 세트를 설정합니다.\n"
            "폴백은 인프라 실패(스폰 실패·타임아웃·opencode 첫-이벤트 stall·한도/인증)에만 1회 —\n"
            "정상 완료 판정(반려·must-fix)은 대상이 아니고, --harness/--model 완전지정 실행이나\n"
            "폴백이 primary 와 같은 하네스/모델이면 loud 로 건너뜁니다. 폴백이 발동하면 최악 소요는\n"
            "primary·폴백 각 하네스 예산의 합입니다 — codex/claude 는 timeout, opencode 는 첫-이벤트\n"
            "워치독 재시도분(retries×창)이 더 붙습니다. --dry-run 이 실수치를 표시합니다(2차 폴백 없음).\n"
            "실패 분류 커버리지: codex 실근거와 claude 2.1.220 실측/바이너리 enum,\n"
            "opencode 1.18.4 provider passthrough 실측·Anthropic API enum을 편입했습니다.\n"
            "opencode 무진단 연결/fetch 침묵은 첫-이벤트 stall 축이 커버하며 그 밖은 fail-loud입니다."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--role", required=True, choices=conf_schema.ROLE_CHOICES,
                        help="위임 역할(권한축 자동 매핑)")
    parser.add_argument("--prompt-file", default=None, metavar="PATH",
                        help="PM 이 만든 task 프롬프트 파일(자족·repo 경계 안). "
                             "code-reviewer 묶음 리뷰는 엔진이 프롬프트를 조립하므로 주지 않는다")
    parser.add_argument("--cwd", required=True, metavar="ABSPATH",
                        help="위임 대상 작업공간(절대경로·모든 역할 필수)")
    parser.add_argument("--tier", default=None, choices=delegate_run_rules.TIER_CHOICES,
                        help="developer 2티어(normal/hard·비-개발 역할 지정 시 usage error)")
    parser.add_argument("--harness", default=None, choices=conf_schema.HARNESS_CHOICES,
                        help="CLI override(--model 동반 필수·원자 tuple)")
    parser.add_argument("--model", default=None, metavar="PROFILE",
                        help="CLI override(--harness 동반 필수)")
    parser.add_argument("--reasoning", default=None, metavar="VAL",
                        help="reasoning override(--harness/--model 동반 시만·드라이버별 허용값)")
    parser.add_argument("--timeout", type=int, default=None, metavar="SEC",
                        help="위임 turn 벽시계 백스톱(초·기본은 하네스 프로필: 클라우드 축 "
                             "codex/claude 3600 · 로컬 GPU 축 opencode 14400). 주 판정은 무진행이며 "
                             "배포별 조정은 local.conf harness.<name>.wall_timeout/.idle_timeout")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="raw 출력 박제 디렉토리"
                             "(기본 .project_manager/.local/delegate)")
    parser.add_argument("--ticket", default=None, metavar="T-NNNN",
                        help="위임 대상 ticket — touches 로 범위 밖 변경을 경고 판정"
                             "(생략 시 허용 경로 0·차단 아님 · code-reviewer 는 --cluster 를 쓴다)")
    parser.add_argument("--cluster", default=None, metavar="C-<ticket-group>",
                        help="리뷰 단위 = 묶음(code-reviewer 필수·전용). 리뷰 트리 검사·프롬프트 "
                             "조립·run-dir 1/라운드 파일 N 예약을 엔진이 하고 실행 뒤 돌려받는다")
    parser.add_argument("--focus", default=None, metavar="PATH",
                        help="PM 검토 중점 문단 파일(--cluster 전용) — 조립 프롬프트에 그대로 실린다")
    parser.add_argument("--resume-from", default=None, metavar="T-NNNN|RECORD-ID",
                        help="직전 위임 세션을 이어받아 delta 만 보낸다(캐시 단가 재적재·도구 "
                             "재읽기 0). 티켓 표기면 그 티켓·같은 역할의 성공 마감 레코드 중 "
                             "started_at 최신 1건, 아니면 raw 장부 레코드 id 정확일치. 후보가 "
                             "raw 장부 완료 보존 창 밖이거나 세션 id 형식이 어긋나거나 재개 "
                             "미지원 하네스면 fresh + full payload 로 진행한다(안내 1줄·차단 아님)")
    parser.add_argument("--fresh", default=None, metavar="REASON",
                        help="같은 ticket+role 완료 기록이 있어도 의도적으로 새 세션을 시작한다. "
                             "비어 있지 않은 사유가 필요하며 raw 장부 레코드에 박제된다")
    parser.add_argument("--attach-raw", default=None, metavar="T-NNNN|RECORD-ID",
                        help="지목 raw 마감 레코드의 최종 reply 원문을 합성 프롬프트 말미의 "
                             "'직전 검토 보고 원문' 절에 첨부한다. 티켓 표기면 그 티켓의 최근 "
                             "완료 레코드이며, 미존재·미마감·raw/reply 손상은 rc=1")
    parser.add_argument("--dry-run", action="store_true",
                        help="합성 프롬프트 요약 + argv 만 출력·미실행(비활성이어도 허용)")
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> Path:
    """CLI 검증 후 한 번 해소한 cwd 반환 — usage error(rc=2)와 보안 경로 오류(rc=1) 분리."""
    cwd_path = Path(args.cwd)
    if not cwd_path.is_absolute():
        parser.error("--cwd 는 절대경로여야 한다(모든 역할 필수·기본값 없음).")
    try:
        resolved_cwd = cwd_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise delegate_errors.DelegateError(
            f"--cwd 실경로 해소 실패: {cwd_path}: {type(exc).__name__}: {exc}"
        ) from exc
    if resolved_cwd == resolved_cwd.parent:  # 파일시스템 루트(`/`·`C:\`)
        parser.error("--cwd 는 파일시스템 루트일 수 없다(작업공간 절대경로 요구·containment 우회 차단).")
    if args.tier is not None and args.role not in conf_schema.TIERED_ROLES:
        parser.error("--tier 는 developer 전용이다(비-개발 역할 지정 = usage error·무시 아님).")
    if args.cluster is not None:
        if args.role != review_form.INTERNAL_REVIEW_ROLE:
            parser.error("--cluster 는 code-reviewer 역할 전용이다(리뷰 단위 = 묶음).")
        if args.ticket is not None:
            parser.error("--cluster 는 --ticket 과 병용할 수 없다(대상이 둘이 된다).")
        if args.prompt_file is not None:
            parser.error(
                "--cluster 는 프롬프트를 엔진이 조립한다 — --prompt-file 을 주지 않는다."
            )
        if not delegate_ledger._is_valid_cluster_id(args.cluster):
            parser.error("--cluster 는 board 클러스터 장부 id 형식이어야 한다.")
    elif args.role == review_form.INTERNAL_REVIEW_ROLE:
        parser.error("code-reviewer 는 --cluster 가 필수다(리뷰 단위 = 묶음).")
    elif args.prompt_file is None:
        parser.error("--prompt-file 은 필수다(엔진이 조립하는 --cluster 묶음 리뷰만 예외).")
    if args.focus is not None and args.cluster is None:
        parser.error("--focus 는 --cluster 전용이다.")
    if bool(args.harness) != bool(args.model):
        parser.error("--harness 와 --model 은 동반 필수(부분 override 금지·원자 tuple).")
    if args.reasoning is not None and not (args.harness and args.model):
        parser.error("--reasoning 은 --harness/--model 동반 시만 허용된다.")
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 은 양의 정수여야 한다(0/음수 금지).")
    if args.fresh is not None and not args.fresh.strip():
        parser.error("--fresh 는 비어 있지 않은 사유가 필요하다.")
    if args.fresh is not None and args.resume_from is not None:
        parser.error("--fresh 와 --resume-from 은 병용할 수 없다(새 세션/기존 세션 의도 충돌).")
    return resolved_cwd


def _dry_run_harness_annotations(
    harness: str, fallback_harness: str | None = None
) -> tuple[str, str | None]:
    """dry-run 표시용 하네스별 문구만 반환한다(timeout/판정은 소유하지 않는 표현 어댑터)."""
    names = (harness,) if fallback_harness is None else (harness, fallback_harness)
    budget_note = (
        " · opencode 는 첫-이벤트 워치독 재시도분 포함"
        if "opencode" in names else ""
    )
    transport_note = (
        "  (opencode: 실행 시 합성 프롬프트를 --dir 하위 .project_manager/.local/delegate/의 "
        "0600 임시 파일로 --file 전달)"
        if harness == "opencode" else None
    )
    return budget_note, transport_note


def _cluster_confirmation_tree(
    home, cluster: str, *, identity: argparse.Namespace | None = None,
) -> Path:
    """확인 커맨드를 돌릴 트리 1곳 — 묶음 통합 브랜치를 체크아웃한 코드 트리.

    명시 선택은 task_args의 task 보유 조인을 사용한다. finish가 넘긴 task 단독 입력은
    공용 해소 함수의 묶음 branch 탐색을 사용한다. 어느 경로든
    확인 실행 트리가 묶음 브랜치와 다르면 기입 전에 거부한다.
    """
    selection = identity if identity is not None else argparse.Namespace()
    # finish는 task 귀속만 전달한다. 실행 위치는 finish의 공용 묶음 브랜치 탐색을 사용한다.
    task_only = (getattr(selection, "task", None)
                 and getattr(selection, "repo", None) is None
                 and getattr(selection, "slot", None) is None)
    if task_only:
        ticket_access._validate_actor_task_or_exit(home, selection.task)
        try:
            resolved, problem = cluster_code_tree.resolve_code_tree(home, cluster)
        except finish_rules._CloseObservationFailure as exc:
            raise delegate_errors.DelegateError(str(exc)) from exc
        if problem:
            raise delegate_errors.DelegateError(problem)
    else:
        try:
            resolved = task_args._claim_code_tree(home, selection)
        except SystemExit as exc:
            raise delegate_errors.DelegateError(str(exc)) from exc
    if not resolved:
        raise delegate_errors.DelegateError(
            f"확인 커맨드를 돌릴 코드 트리를 해소하지 못했습니다: {cluster} — "
            "`--task <task>`(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가) 으로 "
            "트리를 명시하세요"
        )
    tree = Path(resolved)
    problem = cluster_binding.cluster_binding_problem(home, cluster, tree=str(tree))
    if problem:
        raise delegate_errors.DelegateError(problem)
    if not git._git_head_at(str(tree)):
        raise delegate_errors.DelegateError(f"확인 실행 코드 트리의 HEAD를 읽지 못했습니다: {tree}")
    branch = str((cluster_store.load_cluster(home, cluster) or {}).get("branch") or "").strip()
    if branch:
        current = git._cluster_current_branch(str(tree))
        if current != branch:
            raise delegate_errors.DelegateError(
                f"확인 커맨드 실행 트리가 묶음 통합 브랜치가 아닙니다: {tree} "
                f"(현재 {current or '미상'} · 필요 {branch}) — 다른 브랜치에서 잰 관측을 "
                "기계 확인으로 적지 않습니다"
            )
    return tree


def _cmd_rounds_resolve_cluster(
    home, cluster: str, *, identity: argparse.Namespace | None = None,
    run_fn: Callable | None = None,
) -> int:
    """`resolve --cluster --pm-verified` — 멤버마다 확인 실행·기입·완료 판정 재확인.

    rc 는 하나라도 실패하면 1 이다. 앞 티켓의 기입은 되돌리지 않는다 — 티켓별 독립이라
    실패한 자리만 다시 부르면 이어진다(회수와 같은 규율). 잔여 0 멤버는 무대상이다.
    """
    members = cluster_store.cluster_live_members(home, cluster)
    if not members:
        print(
            f"오류: 클러스터 멤버가 없습니다: {cluster} — "
            f"`board.py cluster show {cluster}` 로 장부를 확인하세요",
            file=sys.stderr,
        )
        return 1
    try:
        tree = _cluster_confirmation_tree(home, cluster, identity=identity)
    except delegate_errors.DelegateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    rc = 0
    for ticket in members:
        rc = max(rc, delegate_changelog._resolve_ticket_pm_verified(
            home, ticket, cluster=cluster, tree=tree,
            run_fn=run_fn,
        ))
    return rc


def _cmd_rounds(home, argv: list[str], run_fn: Callable | None = None) -> int:
    """리뷰 잔여의 확인 생성·기입 서브커맨드(`rounds resolve --cluster`).

    `run_fn` 은 확인 커맨드 실행 seam(테스트 DI) — 생략하면 `subprocess.run` 이다.
    """
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py rounds",
        description=(
            "code-reviewer 리뷰 잔여의 기계 확인 생성·기입. 판정 입력은 리뷰 라운드 파일과 "
            "명세 PM 영역이다."
        ),
    )
    subparsers = parser.add_subparsers(dest="rounds_command", required=True)
    resolve = subparsers.add_parser(
        "resolve",
        help="묶음 멤버마다 리뷰 잔여를 현재 fix 의 기계 확인으로 기입하고 완료 판정을 연다",
    )
    resolve.add_argument(
        "--cluster", required=True, metavar="C-<ticket-group>",
        help="묶음 멤버 전부를 대상으로 한다(확인 커맨드는 엔진이 실행)",
    )
    resolve.add_argument(
        "--pm-verified",
        action="store_true",
        required=True,
        help=(
            "기계 확인 증거로 해소. 발동 조건(code-reviewer 채널로 스코프): delta 가 정상 "
            "파싱되고 그 채널 accepted 잔여가 0 이며 판정 표면 finding 수가 리뷰 라운드 잔여 "
            "이상이고, accepted 가 있었다면 그 채널 기계 확인이 1건 이상 존재"
        ),
    )
    identity_args.add_identity_args(resolve)
    args = parser.parse_args(argv)
    try:
        identity_args.parse_identity(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not delegate_ledger._is_valid_cluster_id(args.cluster):
        parser.error("--cluster는 board 클러스터 장부 id 형식이어야 합니다")
    try:
        owner = pm_paths.resolve_pm_home_for_repo(home.root)
        owner_home = pm_paths.home_at(Path(owner).resolve())
        return _cmd_rounds_resolve_cluster(
            owner_home, args.cluster, identity=args, run_fn=run_fn,
        )
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: 리뷰 잔여 확인 실패: {exc}", file=sys.stderr)
        return 1
