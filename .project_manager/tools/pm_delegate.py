#!/usr/bin/env python3
"""pm_delegate — cross-harness 역할 위임 채널.

PM 메인세션(claude/codex/opencode 어디든)이 세션을 떠나지 않고 역할 노동
(developer/researcher/architect/code-reviewer)을 **다른 하네스 CLI subprocess** 로 위임하는
순수 CLI. N×N 대칭 — 호출측 하네스 조건 0 (additional_reviewer 와 동형 seam).

이 도구는 **엔진 코어**만 담는다:
  · config 해소  — `delegate.<role>[.<tier>].harness/.model/.reasoning` 3키를 **원자 tuple**
                  `(harness, model, reasoning)` 로 해소(티어 세트 통째·혼합 상속/부분 override 금지).
  · 3 드라이버   — codex(`-a never -s <mode> exec --json`·stdin)·claude(`-p --tools`·stdin)·
                  opencode(`run --file --agent --dir`). reasoning 은 드라이버별 플래그.
  · 권한 매핑    — 역할축(write=developer/architect·read=researcher/code-reviewer)을 argv/sandbox 로
                  강제하되 보장 수준을 정직 표기.
  · 쓰기-타깃 axis — 엔진 코드(`.project_manager/tools/`) write 위임이 PM 홈 cwd 면 canonical
                  worktree 재앵커 fail-loud(additional_reviewer `_pm_home_reanchor` 재사용).
                  allowlist 정제 + prompt-file containment. ack digest는 해소된 primary
                  harness:model과 합성 전문에 결속한다. ack으로 차단을 통과한 실행은 primary 인프라
                  실패여도 폴백하지 않는다 — 다른 수신자는 명시 재실행과 별도 승인 판단이 필요하다.
  · 결과 수집    — 최종 reply 텍스트만 stdout·raw+메타는 O_EXCL·0600·PID/UUID 파일 박제.
  · loud 폴백    — 역할/티어별 명시 fallback tuple 이 있을 때만 인프라 실패를 양성 분류해 1회 실행하고
                  실행 provenance 를 reply/raw 에 남김(미설정·비-인프라 실패는 기존 fail-loud).
                  **시간 예산**: 폴백은 primary 와 별개로 turn timeout 을 새로 쓴다 — 최악 소요는
                  primary·폴백 **각 하네스 예산의 합**이고, 세션 재사용(--resume-from) 라운드는
                  미일치 fresh 재실행으로 primary 축을 한 번 더 쓸 수 있다(codex/claude=timeout · opencode 는
                  첫-이벤트 워치독 재시도분이 더 붙는다·_harness_timeout_budget). 호출부(스킬·CI)의
                  대기 예산은 --dry-run 이 찍는 실수치로 잡아라.
  · 무음 대체 금지 — 채널 실행 실패(스폰 실패·비정상 rc·타임아웃·연결 실패)는 fail-loud rc 로
                  끝난다. 같은 호출 안에서 **명시 fallback tuple 밖의** 다른 하네스/모델로 자동
                  재시도하지 않고, 실패 안내도 다른 수신자를 권하지 않는다(권유가 곧 무기록
                  대행의 입구다). 재위임 = 사람의 명시 재호출 · 실패는 raw 장부에 잔존.
  · 위임 마스터 스위치 — `delegate.enabled`(기본 ON·채널 무관). off 면 `run`·`ticket prepare` 가
                  rc=3 + stderr 안내(false-green 차단)이고 하네스 훅의 `decide` 도 deny 한다.

설정 시드/lint·어댑터 배선은 별도 표면이다. 기본 단위 테스트는 run_fn DI 로
격리하고, Codex resume 2-turn 검증만 명시 환경변수 opt-in 라이브 테스트로 둔다.
"""

from __future__ import annotations



import argparse
import ast
import datetime
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Callable, NamedTuple
from functools import partial

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.app import delegate_cluster_review
from pm_engine.infra import delegate_copy_git
from pm_engine.app import delegate_execute
from pm_engine.app import delegate_harvest
from pm_engine.app import delegate_prepare
from pm_engine.app import delegate_prompt
from pm_engine.domain import delegate_rounds
from pm_engine.app import delegate_scope_audit
from pm_engine.infra import review_command
from pm_engine.domain import review_contract
from pm_engine.app import review_evidence
from pm_engine.infra import conf_store
from pm_engine.infra import delegate_config
from pm_engine.infra.harness import delegate_env
from pm_engine.infra import delegate_raw
from pm_engine.infra.harness import delegate_run
from pm_engine.domain import delegate_run_rules
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra.harness import delegate_transport
from pm_engine.domain import reviewer_result
from pm_engine.app import cluster_binding  # noqa: E402
from pm_engine.infra import cluster_store  # noqa: E402
from pm_engine.app import ticket_access, round_seed  # noqa: E402
from pm_engine.cli import task_args  # noqa: E402
from pm_engine.domain import conf_schema, delegate_errors, identity_args, markdown_text, model_lint, review_form, review_state, round_rules, ticket_rules  # noqa: E402
from pm_engine.infra import console, delegate_ledger, fs, git, pm_paths, raw_output, repo_files, ticket_store  # noqa: E402
from pm_engine.infra.harness import contract  # noqa: E402
_load_module_from_path = repo_files.load_module



# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다.
# 릴리즈 bump 는 `engine_rev.py --bump vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄
# 재작성한다(사람 N곳 편집 0).
ENGINE_REV = "v1.8.1"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV 를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            f"엔진 사본 버전 불일치 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 "
            f"형제 {sibling_filename}(rev={got!r})를 로드했다 (사본 skew: 부분/수동 복사 또는 "
            f"구형 사본). `pm-update`(또는 pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
        )
        err._engine_rev_skew = True
        raise err


def _is_engine_rev_skew(exc) -> bool:
    """예외가 rev-스탬프 skew 유래인지(fail-soft 소비 지점의 재-raise 식별)."""
    return getattr(exc, "_engine_rev_skew", False)


# ── REPO 앵커 (설치 깊이 고정 · hermetic 테스트 monkeypatch seam) ──────────
# 설치 깊이는 `pm_import` 가 못 박는다 — 도구는 `<root>/.project_manager/tools/` 에만 놓인다.
# REPO 는 module-level 상수로 유지해 hermetic 테스트가 monkeypatch 할 수 있게 한다
# 구현은 pm_engine의 실제 소유 모듈을 직접 사용하고, 이 앵커는 잔류 CLI 입력만 구성한다.

def _find_repo_root() -> Path:
    """엔진 루트 — 도구는 언제나 `<root>/.project_manager/tools/` 에 설치된다.

    상향 탐색을 하지 않는다. 설치 경로를 만드는 쪽(`pm_import`)이 그 깊이를 못 박으므로
    다른 깊이는 나올 수 없고, 탐색은 합성 트리에서 **자기 위 실 인스턴스**를 답으로 주는
    부작용만 낸다(실측: 실 PM 홈 log 오염·등록 안 된 worktree 가 등록으로 판정).
    나머지 도구 20곳과 같은 규칙이다.
    """
    return Path(__file__).resolve().parents[2]


REPO = _find_repo_root()
LOCAL_CONF = REPO / ".project_manager" / "local.conf"

# role preamble — 최소 4개(정체성 1줄 + 금지사항 + 결과 보고). identity 는 codex `exec`
# `--agent` 부재로 prompt 합성해야 하므로 엔진이 harness-중립 최소본을 소유한다. 합성 =
# preamble + "\n\n" + prompt-file 내용.
class _AdapterRegistryNotationError(RuntimeError):
    """등록부의 정적 literal 표기를 해석할 수 없음(프롬프트는 일반 문구로 degrade)."""


class _AdapterRegistrySchemaError(RuntimeError):
    """등록부 literal은 읽혔지만 `(tuple[str, ...], str)` 값 계약이 깨짐."""


def _module_assignment_value(
        tree: ast.Module, name: str, source: Path) -> ast.expr:
    """모듈 최상위 Assign/AnnAssign 하나의 RHS를 반환한다."""
    matches: list[ast.expr] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name
                   for target in node.targets):
                matches.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            matches.append(node.value)
    if len(matches) != 1:
        raise _AdapterRegistryNotationError(
            f"어댑터 단일 출처 {name}의 최상위 Assign/AnnAssign 하나를 찾지 못함: {source}"
        )
    return matches[0]


def _adapter_directories_from_engine_source() -> tuple[str, ...]:
    """pm_import 등록부에서 어댑터 루트 합집합을 순서 보존 파생한다.

    pm_import 전체 import는 위임 CLI 시작에 무관한 엔진을 실행하므로 정적 선언만 AST로 읽는다.
    다만 이 읽기도 stamped sibling 경계다. 따라서 registry를 소비하기 전에 같은 source의 baked
    ENGINE_REV를 `_verify_engine_rev`로 검증하며 skew는 fail-loud다.

    등록부는 ``dict[str, tuple[tuple[str, ...], str]]`` literal이어야 한다. 읽힌 literal의 값
    shape가 다르면 문자 단위 펼침 같은 조용한 오염을 막기 위해 명시 실패한다. 반면 변수 참조나
    ``frozenset(...)``처럼 AST literal이 아닌 *표기*는 `_resolved_adapter_directories`가 일반
    금지 문구로 degrade한다. 최소 형제 hermetic 사본의 파일 부재도 같은 빈 튜플 경로다.
    """
    source = Path(__file__).resolve().parent / "pm_import.py"
    if not source.is_file():
        return ()
    tree = ast.parse(fs.read_text_shared(source, encoding="utf-8"), filename=str(source))

    try:
        rev_node = _module_assignment_value(tree, "ENGINE_REV", source)
        sibling_rev = ast.literal_eval(rev_node)
    except _AdapterRegistryNotationError:
        sibling_rev = None
    except (ValueError, TypeError):
        sibling_rev = None
    source_module = type("_AdapterRegistryRev", (), {"ENGINE_REV": sibling_rev})()
    _verify_engine_rev(source_module, source.name)

    registry_node = _module_assignment_value(tree, "ADD_HARNESS_ADAPTER", source)
    try:
        registry = ast.literal_eval(registry_node)
    except (ValueError, TypeError) as exc:
        raise _AdapterRegistryNotationError(
            f"어댑터 단일 출처 ADD_HARNESS_ADAPTER가 정적 literal 표기가 아님: {source}"
        ) from exc

    if not isinstance(registry, dict) or not registry:
        raise _AdapterRegistrySchemaError(
            "ADD_HARNESS_ADAPTER는 비어 있지 않은 "
            "dict[str, tuple[tuple[str, ...], str]]여야 한다"
        )

    directories: list[str] = []
    for harness, value in registry.items():
        if (
            not isinstance(harness, str)
            or not harness
            or not isinstance(value, tuple)
            or len(value) != 2
        ):
            raise _AdapterRegistrySchemaError(
                f"ADD_HARNESS_ADAPTER[{harness!r}] 값은 "
                "(adapter_dirs: tuple[str, ...], root_doc: str)여야 한다"
            )
        adapter_dirs, root_doc = value
        if (
            not isinstance(adapter_dirs, tuple)
            or not adapter_dirs
            or not all(isinstance(item, str) and item for item in adapter_dirs)
            or not isinstance(root_doc, str)
            or not root_doc
        ):
            raise _AdapterRegistrySchemaError(
                f"ADD_HARNESS_ADAPTER[{harness!r}] 값은 "
                "(adapter_dirs: tuple[str, ...], root_doc: str)여야 한다"
            )
        directories.extend(adapter_dirs)
    return tuple(dict.fromkeys(directories))


def _resolved_adapter_directories() -> tuple[str, ...]:
    """금지 preamble 과 어댑터 경고 축이 **함께** 소비하는 파생 어댑터 루트.

    표기 변화(비-literal 등록부)는 빈 tuple 로 보수 강등하되 schema/skew 는 숨기지 않는다.
    강등 시 preamble 은 일반 문구로, 경고 축은 "판정 불가" advisory 로 각자 처리한다 —
    두 소비자가 같은 값을 봐야 하므로(실행 전 1회 파생해 ScopeAudit 에 스냅샷) 이 헬퍼
    밖에서 등록부를 재조회하지 마라.
    """
    try:
        return _adapter_directories_from_engine_source()
    except _AdapterRegistryNotationError:
        return ()


# 그중 하네스로 위임돼 slot run-dir 을 준비하는 역할. additional-reviewer 라운드는 슬롯 왕복이
# 아니라 additional_reviewer 엔진이 직접 쓴다(추가 리뷰어에게 슬롯 편집 권한을 주지 않는다).
# researcher 는 묶음 라운드 수열(architect → developer → code-reviewer → developer)의 단계가 아니다 —
# 티켓 라운드를 준비하지 않는다(읽기 전용 조사 결과는 회신으로 돌려받는다).


def _load_delegate_channel_guard():
    """엔진 delegate_channel_guard 를 로드해 cross-role 채널 판정(`decide`)을 재사용한다.

    native Agent 위임 훅과 같은 seam — `ticket prepare`가 별도 하네스 비교식을 다시 쓰지 않는다."""
    path = Path(__file__).resolve().parent / "delegate_channel_guard.py"
    return _load_module_from_path(
        path, "delegate_channel_guard.py", verifier=_verify_engine_rev, cache=True,
    )


# ── 설정 ──────────────────────────────────────────────────────────────────

_CONFIG_REPO_OVERRIDE: Path | None = None


def _cmd_raw_close(argv: list[str]) -> int:
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
    _raw_dir, ledger_path = raw_output._raw_storage(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), output_dir)
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
    output_dir: Path | None, ledger_path: Path, *, limit: int,
) -> None:
    """장부 미참조 엔진 명명 원문을 경고 1줄 + 읽기 전용 목록으로 표면화한다.

    삭제는 하지 않는다 — 목록화만 해서 사용자가 직접 처분하게
    한다. delegate·review 두 디렉터리를 모두 스캔한다(장부는 두 표면이 공유하는 한 파일).
    """
    repo_override = _CONFIG_REPO_OVERRIDE or REPO
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


def _cmd_raw(argv: list[str]) -> int:
    """공유 장부의 raw 조회 또는 명시 레코드 마감을 수행한다."""
    if argv and argv[0] == "close":
        return _cmd_raw_close(argv[1:])
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
    _raw_dir, ledger_path = raw_output._raw_storage(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), output_dir)
    resolved_ledger = ledger_path.resolve()
    print(f"조회 장부: {resolved_ledger}")
    for peer_ledger in raw_output._peer_engine_ledgers(pm_paths.home_at(REPO), _CONFIG_REPO_OVERRIDE):
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
    _print_orphan_raw_summary(output_dir, ledger_path, limit=args.limit)
    return 0

# ── config lint (동일-모델 dev/reviewer 경고·never-block) ────────────────


def _cmd_lint(argv: list[str]) -> int:
    """`pm_delegate.py lint` — 동일-모델 dev/reviewer 경고(never-block·항상 rc=0).

    설정 정합 점검일 뿐 강제가 아니다 — 경고가 있어도 rc=0(차단 금지). 경고는 stderr, 정합 시
    안내는 stdout. board lint advisory 훅과 짝을 이루는 명시 진입점."""
    parser = argparse.ArgumentParser(
        prog="pm_delegate.py lint",
        description="delegate 설정 정합 점검 — 동일-모델 dev/reviewer 경고(never-block).")
    parser.parse_args(argv)  # 현재 플래그 0(미래 확장 여지·미지원 인자는 usage error).
    conf = conf_store.local_config(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE))
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


def _reject_cross_role_prepare(role: str, tier: str, conf: dict[str, str]) -> None:
    """cross 역할(PM 하네스 ≠ 매핑된 하네스) 수동 prepare 를 거부한다.

    판정식은 `delegate_channel_guard.decide`(native Agent 위임 훅과 같은 seam) 하나 — 여기서
    하네스 비교를 다시 쓰지 않는다(불변식 1·2). `conf` 는 호출부가 이미 owner(PM 홈)에서 읽어 마스터
    스위치 판정과 함께 쓴 값을 그대로 받는다(불변식 3 — 실행 엔진 사본 repo 로 읽으면 역할 매핑이 없는
    사본에서 조용히 no-op). 거부는 `verdict=="deny"` 중에서도 cross-harness 불일치 사유뿐이고,
    그 구별은 사용자용 사유 문자열이 아니라 구조 필드(harness/model 이 비어 있지 않음)로 한다(불변식 1).
    가드 로드 실패·PM 하네스 미상 등 판정불능은 fail-open 이되 침묵하지 않는다(불변식 4). 다만
    형제 사본 rev 불일치는 판정불능이 아니라 엔진 손상이라 그대로 올린다 — 사본이 갈린 사실이
    "채널 판정 불가" 한 줄에 묻히면 그 사본으로 계속 위임한다.
    """
    try:
        guard = _load_delegate_channel_guard()
        pm_harness = delegate_execute._session_harness(os.environ) or ""
        result = guard.decide(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), role, tier, conf, pm_harness)
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise  # 형제 사본 불일치는 다른 형제 로더와 같은 규칙으로 fail-loud (fail-open 아님).
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
    cluster: str, *, cwd: Path, owner: Path, focus_path: Path | None,
    reopen_ordinal: int | None,
) -> int:
    """같은 하네스 리뷰 준비 — 교차 실행과 같은 준비·조립으로 프롬프트 파일을 쓴다.

    프롬프트 파일은 PM 홈 `.local/delegate/` 아래 O_EXCL·소유자 전용으로 한 번 쓴다. 멤버마다
    JSON 한 줄(`cluster·copy·ordinal·prompt·run_dir·ticket`)을 낸다 — 같은 run 의 줄은 같은
    프롬프트 경로다. PM 은 이 파일 경로를 native 리뷰어에게 넘기고 끝나면 run-dir 을 돌려받는다.
    """
    sources, plan = delegate_cluster_review.prepare_cluster_review(
        pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), cluster=cluster, cwd=cwd, focus_path=focus_path,
        owner_pid=None, reopen_ordinal=reopen_ordinal,
    )
    prompt = delegate_cluster_review.cluster_review_prompt(
        pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), sources, tree=cwd, plan=plan, adapter_roots=_resolved_adapter_directories(),
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


def _cmd_ticket(argv: list[str]) -> int:
    """native 위임도 cross와 같은 사본 helper를 쓰는 prepare/harvest CLI."""
    parser = build_subcommand_parser("ticket")
    assert parser is not None  # 같은 모듈의 literal command — introspection/실행 단일 parser.
    args = parser.parse_args(argv)
    try:
        if args.ticket_command == "copies":
            if args.ticket is not None and not ticket_rules._is_valid_ticket_id(args.ticket):
                parser.error("--ticket은 board 발행 ticket ID 형식이어야 합니다")
            owner = _activate_internal_rounds_cli_owner()
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
        owner = delegate_execute._ticket_cli_owner(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), cwd_repo)
        if args.ticket_command == "prepare":
            # 위임 마스터 스위치 — **준비 단계에서** 막는다. native 위임도 이 CLI 로 라운드 파일을
            # 준비하므로(스킬 규정) 여기가 엔진이 확실히 차단할 수 있는 지점이다. run-dir 생성·
            # 장부 순번 예약보다 앞이라 off 형상에서 고아 산출물이 남지 않는다.
            # `harvest`·`copies` 는 게이트 밖이다 — 이미 준비된 라운드를 회수/조회하는 길까지
            # 막으면 스위치를 끄는 순간 진행 중 라운드가 고아가 된다.
            # owner_conf 를 한 번만 읽어 마스터 스위치와 cross 판정에 함께 넘긴다 — 실행 엔진
            # 사본(REPO) conf 는 판정에 관여하지 않는다(불변식 3: owner 가 provenance 단일 진실).
            owner_conf = conf_store.local_config(pm_paths.home_at(owner))
            if not delegate_config._is_enabled(owner_conf):
                print(
                    "위임이 꺼져 있습니다 — 채널(native/cross) 무관 "
                    f"(local.conf: {delegate_execute._local_conf_path(pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())))} · {delegate_run_rules.DELEGATE_ENABLED_KEY}=false).\n"
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
            _reject_cross_role_prepare(args.role, tier, owner_conf)
            if args.reopen_ordinal is not None and args.reopen_ordinal < 1:
                parser.error(
                    f"{review_state.PREPARE_REOPEN_ORDINAL_FLAG} 은 1 이상의 라운드 순번이어야 합니다"
                )
            if args.role == review_form.INTERNAL_REVIEW_ROLE:
                return _prepare_native_review(
                    args.cluster, cwd=cwd_repo, owner=owner,
                    focus_path=Path(args.focus) if args.focus else None,
                    reopen_ordinal=args.reopen_ordinal,
                )
            if args.cluster:
                cluster_plan = delegate_prepare.prepare_cluster_copy(
                    pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), cluster=args.cluster, role=args.role, cwd=cwd_repo, reopen_ordinal=args.reopen_ordinal,
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
                pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), ticket=args.ticket, role=args.role, cwd=cwd_repo, reopen_ordinal=args.reopen_ordinal,
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
                pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), copy_path=copy, cwd=cwd_repo, assume_dead=args.assume_dead,
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
                pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), run_dir=copy, cwd=cwd_repo, )
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
            pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), copy_path=copy, cwd=cwd_repo, )
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


def _activate_internal_rounds_cli_owner() -> Path:
    """현재 엔진 사본에서 소유 PM 홈을 해소해 내부/공유 raw 장부 좌표를 맞춘다."""
    global _CONFIG_REPO_OVERRIDE
    if _CONFIG_REPO_OVERRIDE is None:
        owner = pm_paths.resolve_pm_home_for_repo(REPO)
        _CONFIG_REPO_OVERRIDE = Path(owner).resolve()
    return _CONFIG_REPO_OVERRIDE


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


def _cmd_review(argv: list[str]) -> int:
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
        return _cmd_review_ticket(args, args.ticket)
    try:
        owner = _activate_internal_rounds_cli_owner()
        members = cluster_store.cluster_live_members(pm_paths.home_at(Path(owner).resolve()), args.cluster)
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
        rc = max(rc, _cmd_review_ticket(args, ticket))
    return rc


def _cmd_review_ticket(args: argparse.Namespace, ticket: str) -> int:
    """대상 티켓 하나의 렌더 — 묶음 반복도 이 한 판정을 그대로 쓴다."""
    ticket_text: str | None = None
    try:
        owner = _activate_internal_rounds_cli_owner()
        home = pm_paths.home_at(Path(owner).resolve())
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



# ── CHANGELOG 재료 추출 (완료 티켓 본문 → 릴리즈 노트 재료) ────────────────────
#
# 릴리즈 노트의 **문안은 사람이 쓴다**. 이 표면이 내는 것은 재료뿐이다 — 어떤 티켓이 이 릴리즈
# 구간에 들어갔고, 그 티켓이 자기 본문에서 무엇을 하겠다고 선언했는가. 분류(Added/Changed/…)는
# **후보**만 세운다: 확정은 채택자 관점의 판단이고, 기계가 그 판단을 대신하면 아무도 읽고 고르지
# 않은 문장이 그대로 릴리즈 노트가 된다.
#
# 구간의 기준은 **코드 저장소의 rev 시각**이다. board 는 PM 홈에 있고 태그는 코드 저장소에 있어
# 두 좌표를 잇는 규칙이 하나 필요하다 — 태그·rev 를 코드 체크아웃에서 커밋 시각으로 바꾸고, 그
# 시각 이후에 완료된 티켓을 고른다. 코드 체크아웃이나 rev 를 해소하지 못하면 빈 목록이 아니라
# rc≠0 이다(조용한 빈 손은 곧 릴리즈 노트 누락이다).
#
# 이 표면은 board 를 **읽기만** 한다 — 순회는 board 의 공용 strict 로더(그 안에서 공유 읽기
# seam)를 쓰고 어떤 티켓 파일도 쓰지 않는다. done 티켓 하나가 손상돼 있으면 그 경로를 찍고
# 멈춘다: 건너뛴 티켓은 출력에서 '원래 없던 재료'와 구별되지 않는다.

# 코드 체크아웃 좌표를 담은 per-clone conf 키 — 엔진 갱신이 쓰는 그 키 하나다(사본 0).
CHANGELOG_UPSTREAM_KEY = "upstream.path"
# 재료로 싣는 본문 절 — 무엇을(목표)·왜 그렇게(결정)·무엇으로 끝났나(완료 조건).
CHANGELOG_MATERIAL_SECTIONS: tuple[str, ...] = ("## 목표", "## 결정", "## 완료 조건")
# 분류 **후보** 신호. 확정이 아니라 후보라 겹치면 겹친 대로 전부 싣는다(선언 순서 보존).
CHANGELOG_CATEGORY_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Added", ("신설", "추가", "도입")),
    ("Changed", ("교체", "변경", "개편", "전환")),
    ("Removed", ("폐지", "제거", "삭제")),
    ("Fixed", ("결함", "버그", "오류", "회귀")),
)
# 채택자 트리에서 무엇이 달라지는가를 말하는 줄의 신호 — 그 줄을 원문 그대로 인용한다.
CHANGELOG_ADOPTER_SIGNALS: tuple[str, ...] = (
    "채택자", "마이그레이션", "기본값", "호환", "폐지", "출하", "업그레이드",
)
# 인용 상한 — 재료는 사람이 읽는 것이라 티켓 하나가 화면을 통째로 먹으면 안 된다.
CHANGELOG_ADOPTER_QUOTE_MAX = 5
_CHANGELOG_BLOCK_HEADER = "## {ticket} — {title}"
_CHANGELOG_EMPTY = "(없음)"
_CHANGELOG_CATEGORY_JOIN = " · "


class ChangelogMaterial(NamedTuple):
    """완료 티켓 하나의 재료 블록 — 판단이 아니라 인용과 후보만 담는다."""

    ticket: str
    title: str
    completed_at: str
    categories: tuple[str, ...]
    adopter_quotes: tuple[str, ...]
    sections: tuple[tuple[str, str], ...]


def _changelog_instant(value: object, *, source: str) -> datetime.datetime:
    """시각 값 → 비교 가능한 시각 — 판독 불가는 그 자리(`source`)를 찍고 터진다.

    tz 없는 값은 UTC 로 읽는다: offset 이 빠진 값도 시각으로는 읽히므로 판독 실패가 아니다
    (board 가 기록하는 값은 offset 을 달고 있다).
    """
    parsed = (value if isinstance(value, datetime.datetime)
              else delegate_run_rules._parse_ledger_timestamp(value))
    if parsed is None:
        raise delegate_errors.DelegateError(f"시각을 읽지 못했습니다: {source} → {value!r}")
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed


def _changelog_required(value: object, *, field: str, path: Path) -> str:
    """완료 티켓의 필수 값 — 없거나 비었으면 그 경로를 찍고 터진다(파일명 폴백 없음)."""
    text = str(value or "").strip()
    if not text:
        raise delegate_errors.DelegateError(f"완료 티켓의 `{field}` 가 비어 있습니다: {path}")
    return text


def changelog_code_checkout(conf: Mapping[str, str], *, pm_home: Path) -> Path:
    """재료 구간의 rev 를 해소할 코드 체크아웃 — 미해소는 fail-loud."""
    raw = str(conf.get(CHANGELOG_UPSTREAM_KEY, "") or "").strip()
    if not raw:
        raise delegate_errors.DelegateError(
            f"코드 체크아웃을 해소하지 못했습니다 — {pm_home}/.project_manager/local.conf 의 "
            f"`{CHANGELOG_UPSTREAM_KEY}=` 가 비어 있습니다"
        )
    path = Path(raw)
    if not path.is_absolute():
        path = pm_home / path
    resolved = path.resolve()
    if not resolved.is_dir():
        raise delegate_errors.DelegateError(
            f"코드 체크아웃이 로컬 디렉터리가 아닙니다: {raw} — rev 시각은 로컬 체크아웃에서만 "
            f"해소합니다(`{CHANGELOG_UPSTREAM_KEY}=` 를 코드 체크아웃 경로로 두세요)"
        )
    return resolved


def changelog_since_instant(
    checkout: Path, rev: str, *, git_run_fn: Callable | None = None,
) -> datetime.datetime:
    """`--since` 의 태그·rev → 그 커밋 시각(해소 실패는 fail-loud).

    git 실행은 이 모듈이 이미 가진 조회 seam 하나를 그대로 쓴다(runner 사본 0).
    """
    try:
        raw = delegate_copy_git._cluster_git(
            checkout, "log", "-1", "--format=%cI", rev, git_run_fn=git_run_fn,
        ).strip()
    except delegate_errors.DelegateError as exc:
        raise delegate_errors.DelegateError(
            f"rev 를 해소하지 못했습니다: {rev} (체크아웃 {checkout}) — {exc}"
        ) from exc
    return _changelog_instant(raw, source=f"rev {rev} (체크아웃 {checkout})")


def _changelog_categories(text: str) -> tuple[str, ...]:
    """분류 후보 — 신호가 하나도 없으면 빈 튜플(렌더가 없음으로 표시한다)."""
    return tuple(
        label for label, signals in CHANGELOG_CATEGORY_SIGNALS
        if any(signal in text for signal in signals)
    )


def _changelog_adopter_quotes(text: str) -> tuple[str, ...]:
    """채택자 영향을 말하는 줄의 원문 인용 — 상한까지만."""
    quotes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-*+ ").strip()
        if not stripped:
            continue
        if any(signal in stripped for signal in CHANGELOG_ADOPTER_SIGNALS):
            quotes.append(stripped)
        if len(quotes) >= CHANGELOG_ADOPTER_QUOTE_MAX:
            break
    return tuple(quotes)


def _changelog_section_text(body: str, heading: str, *, path: Path) -> str:
    """절 본문 — 절 경계 규칙은 board 의 슬라이서 하나가 소유한다(사본 0).

    근거 절이 없거나 비었으면 그 경로와 절 이름을 찍고 터진다 — 빈 값으로 실으면 그 티켓의
    근거가 사라진 것과 재료가 원래 없는 것이 같은 출력이 된다.
    """
    section = markdown_text._section_text(body, heading)
    text = "" if section is None else "\n".join(
        line.rstrip() for line in section.strip("\n").splitlines()
    ).strip("\n")
    if not text:
        raise delegate_errors.DelegateError(f"완료 티켓의 `{heading}` 절이 없거나 비어 있습니다: {path}")
    return text


def changelog_material(*, home, since: datetime.datetime) -> list[ChangelogMaterial]:
    """`since` 이후에 완료된 done 티켓의 재료 — 완료 시각 오름차순.

    done 티켓은 strict 로더로 읽고 필수 값(완료 시각·id·제목·근거 절)을 전부 요구한다. 하나라도
    없거나 판독 불가면 그 경로를 찍고 터진다 — 건너뛰면 '손상된 완료 티켓'과 '구간에 재료가
    없음'이 같은 빈 stdout·rc 0 이 돼 릴리즈 노트 누락이 조용히 생긴다. 재료에서 빠지는 유일한
    사유는 구간 밖(경계 포함) 완료다.
    """
    materials: list[tuple[datetime.datetime, ChangelogMaterial]] = []
    for path in sorted((pm_paths.tickets_dir(home) / "done").glob("T-*.md")):
        fm, body = ticket_store.load_ticket(path)
        completed = _changelog_instant(
            fm.get("completed_at"), source=f"완료 티켓 `completed_at`: {path}",
        )
        if completed <= since:
            continue
        sections = tuple(
            (heading.removeprefix("## ").strip(),
             _changelog_section_text(body, heading, path=path))
            for heading in CHANGELOG_MATERIAL_SECTIONS
        )
        joined = "\n".join(text for _label, text in sections)
        materials.append((completed, ChangelogMaterial(
            ticket=_changelog_required(fm.get("id"), field="id", path=path),
            title=_changelog_required(fm.get("title"), field="title", path=path),
            completed_at=str(fm.get("completed_at")),
            categories=_changelog_categories(joined),
            adopter_quotes=_changelog_adopter_quotes(joined),
            sections=sections,
        )))
    materials.sort(key=lambda item: (item[0], item[1].ticket))
    return [material for _instant, material in materials]


def render_changelog_material(materials: Sequence[ChangelogMaterial]) -> str:
    """재료 블록 렌더 — 재료가 없으면 빈 문자열(빈 손은 오류가 아니다)."""
    lines: list[str] = []
    for material in materials:
        lines.append(_CHANGELOG_BLOCK_HEADER.format(
            ticket=material.ticket, title=material.title))
        lines.append(f"- 완료: {material.completed_at}")
        categories = (_CHANGELOG_CATEGORY_JOIN.join(material.categories)
                      if material.categories else _CHANGELOG_EMPTY)
        lines.append(f"- 분류 후보: {categories}")
        if material.adopter_quotes:
            lines.append("- 채택자 영향 인용:")
            lines.extend(f"  - {quote}" for quote in material.adopter_quotes)
        else:
            lines.append(f"- 채택자 영향 인용: {_CHANGELOG_EMPTY}")
        for label, text in material.sections:
            lines.append(f"- 근거 · {label}:")
            lines.extend(f"  {line}" if line else "" for line in text.splitlines())
        lines.append("")
    return "\n".join(lines) + ("\n" if lines else "")


def _cmd_changelog(argv: list[str], git_run_fn: Callable | None = None) -> int:
    """`changelog material --since <tag|rev>` — 완료 티켓 재료를 stdout 으로(board 무변경)."""
    parser = build_subcommand_parser("changelog")
    assert parser is not None
    args = parser.parse_args(argv)
    try:
        owner = _activate_internal_rounds_cli_owner()
        checkout = changelog_code_checkout(conf_store.local_config(pm_paths.home_at(owner)), pm_home=owner)
        since = changelog_since_instant(checkout, args.since, git_run_fn=git_run_fn)
        materials = changelog_material(
            home=pm_paths.home_at(Path(owner).resolve()), since=since,
        )
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: changelog {args.changelog_command} 실패: {exc}", file=sys.stderr)
        return 1
    rendered = render_changelog_material(materials)
    if rendered:
        sys.stdout.write(rendered)
    return 0


def _cluster_confirmation_tree(
    home, cluster: str, *, identity: argparse.Namespace | None = None,
) -> Path:
    """확인 커맨드를 돌릴 트리 1곳 — 묶음 통합 브랜치를 체크아웃한 코드 트리.

    명시 선택은 task_args의 task 보유 조인을 사용한다. finish가 넘긴 task 단독 입력은
    기존 ClusterCloser의 묶음 branch/base_branch 탐색을 재사용한다. 어느 경로든
    확인 실행 트리가 묶음 브랜치와 다르면 기입 전에 거부한다.
    """
    selection = identity if identity is not None else argparse.Namespace()
    # finish는 task 귀속만 전달한다. 실행 위치는 finish의 기존 묶음 브랜치 탐색을 재사용한다.
    task_only = (getattr(selection, "task", None)
                 and getattr(selection, "repo", None) is None
                 and getattr(selection, "slot", None) is None)
    if task_only:
        ticket_access._validate_actor_task_or_exit(home, selection.task)
        finish = _load_module_from_path(
            Path(__file__).resolve().with_name("ticket_finish.py"), "ticket_finish.py",
            verifier=_verify_engine_rev,
        )
        finisher = finish.TicketFinisher()
        closer = finish.ClusterCloser(cluster, finisher=finisher, home=home)
        try:
            resolved, problem = closer._resolve_code_tree()
        except finish._CloseObservationFailure as exc:
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


def _ticket_spec_and_rounds(home, ticket: str) -> tuple[str, list]:
    """확인·처분 판정 입력 — 명세 원문과 그 티켓의 라운드 목록."""
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
    _status, path = found
    try:
        spec_text = fs.read_text_shared(
            path, encoding="utf-8", newline="",
        )
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(f"티켓 명세 읽기 실패: {path}: {exc}") from exc
    return spec_text, round_seed.load_rounds(
        pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
    )


def _resolve_ticket_pm_verified(
    ticket: str, *, cluster: str, home, owner: Path, tree: Path,
    run_fn: Callable | None = None,
) -> int:
    """티켓 하나의 리뷰 확인 입력 preflight → terminal 확인 기입 → 완료 판정 재확인.

    순서가 계약이다: (1) 입력 preflight (2) machine command/PM-owned terminal 확인 생성
    (3) 확인 절 기입 (4) 명세를 **다시 읽어** 완료 판정의 리뷰 잔여가 열렸는지 확인한다.
    판정 입력은 리뷰 라운드 파일과 명세 PM 영역뿐이다(다른 기록을 쓰지 않는다).
    """
    try:
        spec_text, rounds = _ticket_spec_and_rounds(home, ticket)
        preflight_problem = review_state.review_resolution_input_problem(spec_text, rounds)
        if preflight_problem is not None:
            raise delegate_errors.DelegateError(f"리뷰 확인 입력 preflight 실패: {preflight_problem}")
        residual = review_state.review_residual(rounds)
        if residual is not None and residual.must_fix == 0:
            print(f"{ticket}: 처분할 리뷰 잔여 없음(must-fix 0건) — 무대상")
            return 0
        template = review_state.pm_review_verify_template(spec_text, rounds)
        rows = review_command.run_pm_review_confirmations(template, cwd=tree, run_fn=run_fn)
        if rows:
            review_evidence.append_pm_review_confirmation(
                pm_paths.home_at(REPO, pm_home=Path(owner).resolve(), board_root=pm_paths._board_root_at(Path(owner).resolve())), ticket, review_contract.render_pm_review_confirmation_section(rows), rounds=rounds,
            )
            unresolved = [row.id for _round, row in rows if row.status != "resolved"]
            pm_owned_count = len(template.pm_owned_rows)
            if pm_owned_count:
                machine_count = len(rows) - pm_owned_count
                print(
                    f"{ticket}: 기계 확인 {machine_count}건 + PM-owned terminal 확인 "
                    f"{pm_owned_count}건 기입 (미해소 {len(unresolved)}"
                    f"{': ' + ', '.join(unresolved) if unresolved else ''}) · 실행 트리={tree}"
                )
            else:
                print(
                    f"{ticket}: 기계 확인 {len(rows)}건 기입 "
                    f"(미해소 {len(unresolved)}"
                    f"{': ' + ', '.join(unresolved) if unresolved else ''}) "
                    f"· 실행 트리={tree}"
                )
        else:
            print(f"{ticket}: 기계 확인 대상 0건 — 실행 없이 완료 판정으로 넘어갑니다")
        spec_text, rounds = _ticket_spec_and_rounds(home, ticket)
        completion_problem = review_state.review_completion_problem(spec_text, rounds)
        if completion_problem is not None:
            raise delegate_errors.DelegateError(
                f"확인 기입 뒤에도 리뷰 잔여가 완료 판정을 막습니다: {completion_problem}"
            )
    except review_form.PMReviewError as exc:
        print(f"오류: 리뷰 잔여 확인 실패[{exc.code}]: {ticket}: {exc}", file=sys.stderr)
        return 1
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: 리뷰 잔여 확인 실패: {ticket}: {exc}", file=sys.stderr)
        return 1
    print(f"{ticket}: 리뷰 잔여 확인 완료 — 완료 판정의 리뷰 잔여 없음 · PM 홈={owner}")
    return 0


def _cmd_rounds_resolve_cluster(
    cluster: str, *, owner: Path, identity: argparse.Namespace | None = None,
    run_fn: Callable | None = None,
) -> int:
    """`resolve --cluster --pm-verified` — 멤버마다 확인 실행·기입·완료 판정 재확인.

    rc 는 하나라도 실패하면 1 이다. 앞 티켓의 기입은 되돌리지 않는다 — 티켓별 독립이라
    실패한 자리만 다시 부르면 이어진다(회수와 같은 규율). 잔여 0 멤버는 무대상이다.
    """
    home = pm_paths.home_at(Path(owner).resolve())
    home = pm_paths.home_at(Path(owner).resolve())
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
        rc = max(rc, _resolve_ticket_pm_verified(
            ticket, cluster=cluster, home=home, owner=owner, tree=tree,
            run_fn=run_fn,
        ))
    return rc


def _cmd_rounds(argv: list[str], run_fn: Callable | None = None) -> int:
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
        owner = _activate_internal_rounds_cli_owner()
        return _cmd_rounds_resolve_cluster(
            args.cluster, owner=owner, identity=args, run_fn=run_fn,
        )
    except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
        print(f"오류: 리뷰 잔여 확인 실패: {exc}", file=sys.stderr)
        return 1


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


def main(argv: list[str] | None = None, run_fn: Callable | None = None,
         git_run_fn: Callable | None = None) -> int:
    """CLI 진입 — 콘솔 인코딩 설정이 첫 동작이다(인자 오류처럼 CLI 본체 앞뒤에서 나가는 출력도
    같은 콘솔 설정 아래 있어야 한다 · 형제 진입점 관용구와 동형)."""
    console.configure_console_utf8()
    return _run_delegate_cli(argv, run_fn=run_fn, git_run_fn=git_run_fn)


def _run_delegate_cli(argv: list[str] | None = None, run_fn: Callable | None = None,
                      git_run_fn: Callable | None = None) -> int:
    global _CONFIG_REPO_OVERRIDE
    # main() 재호출 간 config 앵커가 새지 않게 모든 분기(lint/raw 포함) 진입 전에 초기화한다.
    _CONFIG_REPO_OVERRIDE = None
    # `lint` 서브커맨드 — flat 위임 옵션(--role/--prompt-file/--cwd required)과 분리한 별도 경로.
    # 위임과 인자 형상이 다르므로 build_arg_parser 앞에서 분기(subparsers 로 위임 required 를 흩지
    # 않는다). never-block(항상 rc=0).
    resolved = list(sys.argv[1:] if argv is None else argv)
    if resolved and resolved[0] == "lint":
        return _cmd_lint(resolved[1:])
    if resolved and resolved[0] == "raw":
        return _cmd_raw(resolved[1:])
    if resolved and resolved[0] == "rounds":
        return _cmd_rounds(resolved[1:])
    if resolved and resolved[0] == "ticket":
        return _cmd_ticket(resolved[1:])
    if resolved and resolved[0] == "review":
        return _cmd_review(resolved[1:])
    if resolved and resolved[0] == "changelog":
        return _cmd_changelog(resolved[1:], git_run_fn=git_run_fn)
    parser = build_arg_parser()
    args = parser.parse_args(resolved)
    try:
        cwd = _validate_args(parser, args)
    except delegate_errors.DelegateError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    effective_ticket = args.ticket

    tier = args.tier if (args.role in conf_schema.TIERED_ROLES and args.tier) else "normal"
    profile_source = "cli-override" if (args.harness and args.model) else "local-conf"

    # --cwd는 실행 타깃뿐 아니라 config/board 소유자를 정하는 명시 입력이다. 먼저 git 루트를
    # 확정한 뒤 worktree lease 장부로 PM 홈을 해소해, 어느 엔진 사본을 호출했는지와 분리한다.
    if not delegate_env._cwd_in_git_repo(cwd, git_run_fn):
        print(
            f"오류: --cwd 는 git 저장소 루트이거나 그 하위여야 합니다: {cwd}\n"
            "  광범위 경로(홈 디렉토리 등 non-repo)는 신뢰 작업공간이 아닙니다 — worktree/repo 를 지정하세요.",
            file=sys.stderr,
        )
        return 1
    cwd_repo = pm_paths._repo_root_for_cwd(cwd)
    try:
        config_repo = pm_paths.resolve_pm_home_for_repo(cwd_repo)
    except pm_paths.AnchorResolutionError as exc:
        print(f"오류: --cwd 소유 PM 홈 해소 실패 — {exc}", file=sys.stderr)
        return 1
    _CONFIG_REPO_OVERRIDE = config_repo
    board_repo = config_repo
    if (
        effective_ticket
        and pm_paths._owns_real_board(REPO / ".project_manager")
        and REPO.resolve() != config_repo.resolve()
    ):
        print(
            "오류: 실행 엔진 board와 --cwd 소유 PM 홈이 다릅니다 — 같은 ticket id를 다른 "
            f"board에서 읽을 수 없어 중단합니다: engine={REPO.resolve()} owner={config_repo.resolve()}",
            file=sys.stderr,
        )
        return 1
    conf_path = delegate_execute._local_conf_path(pm_paths.home_at(REPO, pm_home=Path(config_repo).resolve(), board_root=pm_paths._board_root_at(Path(config_repo).resolve())))
    try:
        conf = conf_store.local_config(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE))
    except (OSError, UnicodeError) as exc:
        print(
            f"오류: 해소된 local.conf 읽기 실패 — 호출 전에 중단합니다: "
            f"{conf_path} ({type(exc).__name__}: {exc})",
            file=sys.stderr,
        )
        return 1

    # 위임 마스터 스위치 (비-dry-run·기본 ON·명시 off = rc=3). **매핑 해소보다 앞** — 위임 자체가
    # 비허용인 형상은 "매핑 미설정"(rc=1)이 아니라 disabled(rc=3)로 응답해야 진단이 정확하다.
    # dry-run 은 항상 미리보기 허용(미호출)이라 이 게이트를 통과시킨다.
    if not args.dry_run and not delegate_config._is_enabled(conf):
        print(
            "위임이 꺼져 있습니다 — 채널(native/cross) 무관 "
            f"(local.conf: {conf_path} · {delegate_run_rules.DELEGATE_ENABLED_KEY}=false).\n"
            f"켜기: local.conf 에서 `{delegate_run_rules.DELEGATE_ENABLED_KEY}=true` (또는 그 줄을 지우면 기본 허용). "
            "미리보기는 `--dry-run`.",
            file=sys.stderr,
        )
        return 3  # 명시적 비성공(rc=0 no-op 금지·빈 stdout 성공 오인 차단)

    # config 해소 (원자 tuple·fail-loud rc=1). 추론 값 확인은 해소 뒤 실행 cwd·env 로 한다(dry-run 포함).
    try:
        harness, model, reasoning = delegate_config.resolve_delegate(
            conf, args.role, tier, args.harness, args.model, args.reasoning)
        fallback = delegate_config.resolve_fallback(conf, args.role, tier)
        delegate_config._validate_reasoning(harness, reasoning, model=model, cwd=cwd)
        if fallback is not None:
            fallback_harness, fallback_model, fallback_reasoning = fallback
            delegate_config._validate_reasoning(
                fallback_harness, fallback_reasoning, model=fallback_model, cwd=cwd)
    except delegate_errors.DelegateError as exc:
        print(f"오류: {exc}\n  · local.conf: {conf_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.output_dir) if args.output_dir else None
    # [R] cold 재투입 비용 가드. write 역할만 코드 재섭취 중복을 막는다. read 역할은
    # generate≠evaluate 독립성을 위해 라운드마다 cold 판정하는 것이 정상이라 무마찰 통과시킨다.
    # resume/dry-run은 대상이 아니고, `--fresh <사유>`는 명시 우회다.
    # 장부 부재·손상은 이 가드만 fail-open하며, 안전 경계인 raw 기록 자체를 복구/초기화하지 않는다.
    if (
        effective_ticket
        and args.role in delegate_run_rules.WRITE_ROLES
        and args.resume_from is None
        and not args.dry_run
        and args.fresh is None
    ):
        completed = delegate_raw.cold_reinjection_record(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), effective_ticket, args.role, output_dir)
        if completed is not None:
            return delegate_execute.fail_loud(delegate_raw.cold_reinjection_rejection(
                effective_ticket, args.role, harness, completed,
            ))

    # 폴백 **비발동** 판정(loud skip·설정은 그대로 두고 이번 실행만 끈다). 설정 자체는 위에서 해소해
    # 불완전 폴백 설정을 fail-loud 로 잡되(설정 정합은 override 와 무관), 아래 사유면 이번 실행에선
    # 쓰지 않는다 — "설정돼 있는데 안 썼다"를 정확히 말할 수 있어야 loud skip 이 성립한다:
    #   CLI 완전지정(--harness AND --model) = 설정 미참조 원자 override(resolve_delegate 불변) —
    #      일회성 명시 실행이 요청 밖 하네스로 넘어가면 그 불변이 깨진다.
    #   폴백 tuple 의 하네스/모델이 primary 와 동일 — 한도 소진된 같은 채널을 유료로 재타격할 뿐이다
    #      (reasoning 만 다른 경우도 같은 계정/모델 한도라 skip 한다).
    fallback_skip: str | None = None
    if fallback is not None:
        if args.harness and args.model:
            fallback_skip = ("CLI 완전지정(--harness/--model)은 설정 미참조 원자 override — "
                             "설정 폴백을 쓰지 않는다")
            fallback = None
        elif (fallback[0], fallback[1]) == (harness, model):
            fallback_skip = (f"폴백 tuple 이 primary 와 동일({harness}/{model}) — "
                             "한도 소진된 같은 채널 재타격 금지")
            fallback = None

    # prompt-file 존재 확인 — 없으면 읽을 것이 없다.
    prompt_file = Path(args.prompt_file or "")
    task_text = ""
    if args.cluster is None and not prompt_file.is_file():
        print(f"오류: --prompt-file 이 없음: {prompt_file}", file=sys.stderr)
        return 1
    if args.cluster is None:
        try:
            read_target = prompt_file.resolve()
        except OSError:
            read_target = prompt_file
        try:
            task_text = fs.read_text_shared(read_target, encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            print(f"오류: --prompt-file 읽기 실패: {exc}", file=sys.stderr)
            return 1

    # [A] 실제 공유 장부와 그 행이 가리키는 raw 파일에서 최종 reply를 읽는다. 첨부는 task 원문
    # 말미에 붙으므로 full payload와 resume delta 어느 쪽에서도 같은 원문 1회가 실제로 넘어간다.
    try:
        attached_raw = delegate_raw.resolve_attached_raw(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), args.attach_raw, output_dir=output_dir)
    except delegate_errors.DelegateError as exc:
        return delegate_execute.fail_loud(f"오류: {exc}")
    if args.cluster is None:
        task_text = delegate_raw.append_attached_raw(task_text, attached_raw)

    # cross 실위임의 라운드 준비. dry-run은 미호출/무부수효과 계약이라 만들지 않고, 하네스로
    # 위임되는 역할의 실제 실행만 prepare한다(ticket 없는 legacy 호출은 종전 형상 유지).
    # 묶음 리뷰는 native 준비와 같은 함수(`prepare_cluster_review`)로 준비 트리 조건·리뷰 입력 판독 뒤
    # 예약한다 — dry-run 은 같은 입력을 읽기만 한다(`cluster_review_sources`).
    ticket_copy: delegate_rounds.TicketCopyPlan | None = None
    cluster_plan: delegate_rounds.ClusterCopyPlan | None = None
    cluster_sources: delegate_cluster_review.ClusterReviewSources | None = None
    if args.cluster is not None:
        focus_path = Path(args.focus) if args.focus else None
        try:
            if args.dry_run:
                cluster_sources = delegate_cluster_review.cluster_review_sources(
                    pm_paths.home_at(Path(config_repo).resolve()), args.cluster, repo=cwd_repo,
                    focus_path=focus_path, git_run_fn=git_run_fn,
                )
            else:
                cluster_sources, cluster_plan = delegate_cluster_review.prepare_cluster_review(
                    pm_paths.home_at(REPO, pm_home=Path(config_repo).resolve(), board_root=pm_paths._board_root_at(Path(config_repo).resolve())), cluster=args.cluster, cwd=cwd_repo, focus_path=focus_path, owner_pid=os.getpid(), git_run_fn=git_run_fn,
                )
                # 쓰기 자리·run_id·환불 좌표는 run 단위라 대표 자리 하나로 잡아도 같은 값이다.
                # 티켓별로 갈리는 회수는 아래에서 `cluster_plan.rounds` 를 그대로 순회한다.
                ticket_copy = cluster_plan.rounds[0]
        except delegate_errors.DelegateError as exc:
            return delegate_execute.fail_loud(f"오류: 묶음 리뷰 준비 실패: {exc}")
    elif not args.dry_run and effective_ticket and args.role in (delegate_ledger.TICKET_COPY_ROLES - {review_form.ADDITIONAL_REVIEWER_ROLE, "researcher"}):
        try:
            ticket_copy = delegate_prepare.prepare_ticket_copy(
                pm_paths.home_at(REPO, pm_home=Path(config_repo).resolve(), board_root=pm_paths._board_root_at(Path(config_repo).resolve())), ticket=effective_ticket, role=args.role, cwd=cwd_repo, owner_pid=os.getpid(),
            )
        except delegate_errors.DelegateError as exc:
            return delegate_execute.fail_loud(f"오류: 위임 티켓 라운드 준비 실패: {exc}")

    # 예약(prepare_ticket_copy) 이후 ~ 실행 인계(_execute_and_collect) 이전 구간
    # 전체를 하나의 정리 경계로 감싼다. 이 구간을 벗어나는 모든 경로(명시 return · 전파
    # 예외)가 이 finally 하나로 수렴한다 — 지점마다 호출을 꽂지 않는다(리뷰 라운드가 꽂았던
    # 6개 지점 삽입을 대체한다). `_ticket_copy_handed_off` 는 `_execute_and_collect` 호출
    # 직전 단 한 곳에서만 True 로 바뀐다 — 그 전에 return 이든 전파 예외든 이 경계를
    # 빠져나가면 전부 환불(abandon) 로 정리한다.
    _ticket_copy_handed_off = False
    try:
        # 프롬프트 합성과 회수 판정이 같은 실행-전 등록부 스냅샷을 쓴다. 위임 중 pm_import.py가
        # 바뀌어도 전달한 금지 루트와 다른 현재값으로 재판정하지 않는다.
        adapter_roots = _resolved_adapter_directories()
        if cluster_sources is not None:
            # native 준비가 프롬프트 파일에 쓰는 것과 같은 조립이다(`cluster_review_prompt`).
            task_text = delegate_raw.append_attached_raw(delegate_cluster_review.build_cluster_review_prompt(
                pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), cluster_sources.review, cluster_sources.specs,
                tree=cwd_repo, focus=cluster_sources.focus,
            ), attached_raw)
            prompt = delegate_raw.append_attached_raw(delegate_cluster_review.cluster_review_prompt(
                pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), cluster_sources, tree=cwd_repo, plan=cluster_plan,
                adapter_roots=adapter_roots,
            ), attached_raw)
        else:
            preamble = delegate_prompt._role_preamble(args.role, adapter_roots)
            round_note = delegate_prompt._round_copy_preamble(ticket_copy, None)
            if round_note is not None:
                preamble += "\n" + round_note
            prompt = preamble + "\n\n" + task_text

        # 세션 재사용 해소 — **호출 전 게이트보다 앞**이다. 재사용 라운드가 실제로 내보내는 건 delta
        # payload 라, 게이트(재앵커·시크릿 스캔)가 그걸 못 보면 검사받지 않은 본문이 나간다. 재사용
        # 실패 시 full payload 로 돌아가므로 두 본문 **모두**를 게이트 입력으로 합친다.
        resume = delegate_execute.resolve_resume_plan(
            pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), args.resume_from, harness=harness, role=args.role,
            task_text=task_text, output_dir=output_dir,
        )
        outgoing_text = (
            prompt if resume is None else "\n".join((resume.delta_prompt, prompt))
        )

        # 쓰기-타깃 axis 재앵커 게이트 (엔진 코드 write + PM 홈 cwd·dry-run 전 = 미리보기서 노출)
        # divergence와 같은 cwd repo 해소 입력을 기존 write-target 가드에도 재사용한다. 하위 디렉토리
        # `--cwd`가 PM 홈 가드를 우회하는 별도 판정축을 만들지 않는다.
        reanchor = delegate_execute.check_write_target_reanchor(args.role, cwd_repo or cwd, outgoing_text)
        if reanchor is not None:
            print(
                "오류: 엔진 코드(.project_manager/tools/) write 위임을 adopter#0 PM 홈 cwd 에서 실행했습니다 —\n"
                "  import 사본을 수정하면 canonical worktree 와 갈려 stale·false-green 이 납니다.\n"
                f"  · canonical worktree 로 재앵커하세요:  --cwd {reanchor}\n"
                "  · PM-doc(wiki/ADR/spike) 작업이면 PM 홈 cwd 가 정당합니다 — 그 경우 프롬프트가 엔진 코드\n"
                "    경로를 write 타깃으로 지목하지 않게 하세요.",
                file=sys.stderr,
            )
            return 1

        profile = delegate_config.resolved_delegate_profile(harness, model, reasoning)
        print(
            f"[pm-delegate] config provenance: local_conf={conf_path} "
            f"· pm_home={config_repo.resolve()} · source={profile_source} · resolved_profile={profile}",
            file=sys.stderr,
        )
        # 드라이버 argv 준비 (dry-run·실행 공용). opencode 는 실행 시 합성 프롬프트를 ``--dir`` 아래
        # wire 사본으로 만들어 --file 전달하므로, dry-run argv 는 사용자 prompt-file 경로로만 표시한다.
        argv_display = delegate_run._build_target_argv(
            harness, model, reasoning, args.role, cwd, prompt_file,
            None if resume is None else resume.session_id,
        )

        # 타임아웃은 dry-run 미리보기(폴백 시간 예산 표시)와 실행이 같은 값을 쓴다. 값이 하네스별이라
        # **시도마다 자기 하네스의 예산**을 쓴다(폴백이 다른 축이면 예산도 그 축의 것).
        timeout = delegate_env._resolve_timeout(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), args, conf, harness)
        fallback_timeout = (delegate_env._resolve_timeout(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), args, conf, fallback[0])
                            if fallback is not None else timeout)
        execution_budget = delegate_env._harness_timeout_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness, timeout)
        if resume is not None:
            # 재사용 라운드는 primary 축을 두 번 쓸 수 있다(재사용 turn → 세션 불일치 시 fresh
            # 재실행). 그 뒤 인프라 폴백까지 겹치면 이 실행의 최악 스폰은 3회다 — 호출층 상한
            # advisory 가 두 시도만 요구하면 하네스가 엔진 진단보다 먼저 kill 한다.
            execution_budget += delegate_env._harness_timeout_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness, timeout)
        if fallback is not None:
            execution_budget += delegate_env._harness_timeout_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), fallback[0], fallback_timeout)
        cap_warning = delegate_execute.harness_cap_advisory(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), execution_budget=execution_budget)
        if cap_warning is not None:
            print(cap_warning, file=sys.stderr)

        # 기존 `--cwd` 신뢰 repo축을 파생 config_repo와 대조한다. 양쪽 모두 설정된 fallback 값만
        # 공용 seam이 비교한다.
        compare_fallback = fallback is not None
        try:
            _resolved_cwd_repo, divergence = delegate_execute.check_local_conf_divergence(
                cwd, conf, args.role, tier,
                config_repo=config_repo,
                cli_override=(profile_source == "cli-override"),
                compare_fallback=compare_fallback,
            )
        except reviewer_result.TargetLocalConfReadError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
        if divergence is not None:
            print(
                reviewer_result.format_local_conf_divergence(
                    divergence,
                    surface="pm_delegate",
                    cwd_label="--cwd worktree",
                    source_label="해소된 PM 홈",
                    resolution_note=(
                        "이번 실행에서는 --cwd의 lease 소유자로 해소된 PM 홈 conf가 적용됩니다: "
                        f"{divergence.engine_conf_path}\n"
                        "  차단하지 않고 계속합니다. 같은 프로필을 원하면 --cwd worktree conf의 위 "
                        "키 값을 PM 홈 conf와 맞추세요. 다른 PM 홈 conf를 선택하려면 --cwd가 "
                        "가리키는 worktree와 lease 소유 관계를 확인하세요."
                    ),
                ),
                file=sys.stderr,
            )

        # 병렬 위임 touches 교집합 사전 경고 — 호출-전 게이트를 모두 통과한 뒤, **dry-run 미리보기
        # 에도** 낸다(띄우기 전에 보는 것이 이 경고의 값이다). rc 는 바꾸지 않는다. 리뷰어는
        # 트리를 바꾸지 않으므로(돌려받기 트리 조건) 쓰기 역할만 대상이다.
        # 이 경고는 **정보성**이다 — 차단 게이트(재앵커)와 독립이라 뒤따르는 게이트가
        # 실행을 막아도 출력은 그대로 남는다(rc·상태 불변). 출력 위치는 미리보기 요구가 정한다.
        touch_overlaps: tuple[delegate_scope_audit.TicketTouchOverlap, ...] = ()
        if effective_ticket and args.role in delegate_run_rules.WRITE_ROLES:
            try:
                touch_overlaps = delegate_scope_audit.claimed_touch_overlaps(
                    effective_ticket, pm_root=board_repo, workspace=cwd_repo,
                )
            except (delegate_errors.DelegateError, OSError, UnicodeError, ValueError) as exc:
                # 조용히 삼키지 않는다 — 계산이 죽으면 "겹침 0" 과 구분되지 않는다. 엔진 사본 skew
                # 등 marked 예외는 여기서 잡지 않고 그대로 올린다(형제 로드 규율).
                print(
                    f"경고: 병렬 위임 touches 교집합 계산 실패({type(exc).__name__}: {exc}) "
                    "— 비차단 진행.",
                    file=sys.stderr,
                )
            else:
                overlap_warning = delegate_scope_audit.format_touch_overlap_warning(
                    effective_ticket, touch_overlaps,
                )
                if overlap_warning:
                    print(overlap_warning, file=sys.stderr)

        # dry-run — 합성 프롬프트 요약 + argv 출력·미실행 (비활성이어도 허용·rc=0)
        if args.dry_run:
            print("=== [dry-run] pm_delegate 미리보기 (미실행) ===")
            print(f"role: {args.role} · tier: {tier} · 권한축: {contract.perm_axis(args.role)}")
            if cluster_sources is not None:
                review = cluster_sources.review
                print(
                    f"리뷰 대상: {review.cluster} 멤버 {len(review.members)}건 "
                    f"({', '.join(review.members)})"
                )
                print(
                    f"리뷰 입력: {review.base_branch} 와 {review.branch} 의 "
                    f"merge-base({review.merge_base[:12]}) 이후 · 변경 파일 "
                    f"{len(review.paths)}건"
                )
                print("리뷰 라운드 자리: dry-run 은 만들지 않는다(실행 시 준비·부작용 0 계약)")
            print(f"해소: harness={harness} model={model} reasoning={reasoning}")
            if fallback_skip is not None:
                print(f"폴백: 비발동 — {fallback_skip}")
            elif fallback is None:
                print("폴백: 미설정 (인프라 실패 시 기존 fail-loud)")
            else:
                fallback_harness, fallback_model, fallback_reasoning = fallback
                primary_budget = delegate_env._harness_timeout_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness, timeout)
                fallback_budget = delegate_env._harness_timeout_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), fallback_harness, fallback_timeout)
                note, _transport_note = _dry_run_harness_annotations(
                    harness, fallback_harness
                )
                print(
                    "폴백: "
                    f"harness={fallback_harness} model={fallback_model} "
                    f"reasoning={fallback_reasoning} (인프라 실패에만 1회)"
                )
                print(
                    f"폴백 시간 예산: 최악 primary {primary_budget}s + 폴백 {fallback_budget}s = "
                    f"{primary_budget + fallback_budget}s (2차 폴백 없음{note})"
                )
                print(
                    "  (실행+정리 예산 — 부모 wait/pipe drain을 startup 재시도마다 산입; "
                    f"진단·raw 박제 여유 {delegate_run_rules._HARNESS_CAP_MARGIN_SEC}s는 호출층 상한에 별도)"
                )
            print(f"cwd: {cwd}")
            argv_display = delegate_transport._apply_workspace_git_dirs(
                argv_display, delegate_run_rules._READ_TMP_ARGV_MODE_BY_HARNESS[harness], cwd,
            )
            print(f"argv: {' '.join(argv_display)}")
            _budget_note, transport_note = _dry_run_harness_annotations(harness)
            if transport_note is not None:
                print(transport_note)
            if args.resume_from is not None:
                # 재사용을 **요청한** 실행만 이 줄을 낸다(미요청 실행의 미리보기 형상은 불변).
                print(
                    "세션 재사용: 미적용 — fresh + full payload (사유는 stderr 안내)"
                    if resume is None else
                    f"세션 재사용: session_id={resume.session_id} "
                    f"· 장부 레코드={resume.record_id} · delta payload 전달 "
                    f"(세션 불일치 시 fresh 재실행 1회 = primary 예산 "
                    f"{delegate_env._harness_timeout_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness, timeout)}s 추가)"
                )
            print("--- 합성 프롬프트 ---")
            dry_run_prompt = prompt if resume is None else resume.delta_prompt
            # read 역할 + 재앵커 하네스(현재 codex 단독)는 실행과 같은 3-절대경로 preamble을 미리보기
            # 에도 낸다 — 조건은 재앵커 플래그 단일이다(claude·opencode 특례
            # 없음). dry-run은 부작용 0 계약이라 read_tmp를 만들지 않고 `_predict_read_tmp_paths`로
            # 같은 이름 규칙의 대표 경로만 계산한다.
            #
            # 리뷰 must-fix(F-001): 이 조건은 실행 경로가 `read_tmp is not None`(=
            # `_read_tmp_strategy()` 가 전략을 낸다)으로 판정하는 것과 **같은 사실**을 봐야 한다 —
            # 예측 전용 사본을 새로 만들지 않고, 실행이 `_create_read_role_temp`를 통해 소비하는 바로
            # 그 함수(`_read_tmp_strategy()`)를 여기서도 그대로 부른다. 전략이 없는 플랫폼(fd 결속도
            # 소유자 ACL 도 없음)은 실행도 재앵커를 안 하므로(`read_tmp=None` → `_apply_read_tmp_argv`
            # 가 argv 를 그대로 반환 → `-C`는 원 `--cwd`) dry-run 도 존재하지 않을 좌표를 예고하지
            # 않는다(실재하지 않는 좌표를 주는 것은 좌표를 아예 안 주는 것보다 나쁘다 — PM 판정).
            if (
                args.role in delegate_run_rules.READ_ROLES
                and delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
                and delegate_temp._read_tmp_strategy() is not None
            ):
                predicted_exec_root, predicted_writable = delegate_transport._predict_read_tmp_paths(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness)
                dry_run_prompt = dry_run_prompt + "\n\n" + delegate_transport._reanchor_exec_root_preamble(
                    cwd, predicted_exec_root, predicted_writable,
                )
            print(dry_run_prompt)
            print("=== [dry-run] 위임 호출 생략 ===")
            return 0

        # env allowlist 정제 + 실행. 인프라 실패일 때만 명시 폴백을 같은 드라이버 계약으로
        # 1회 실행한다(최악 소요 = 두 시도의 하네스별 예산 합·2차 폴백 없음). 보안/재앵커 게이트는 이
        # 지점보다 앞이라 폴백 대상이 될 수 없다. (`output_dir` 은 재사용 후보 조회에 먼저 쓰였다.)
        _run = run_fn or partial(delegate_run._default_run_fn, pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE))

        # 범위 판정 캡처 — **위임 전체 단위**로 1회. 폴백 attempt 도 같은 위임의 일부라
        # attempt 마다 재캡처하지 않는다(PM 이 회수 시점에 "이 위임이 범위 밖을 만졌나"를 본다). 아래
        # 실행·회수 블록의 모든 종료 경로(성공·폴백·fail-loud·예외)에서 finally 가 정확히 1회 보고한다.
        scope_audit = delegate_scope_audit.begin_scope_audit(
            pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), effective_ticket, cwd, pm_root=board_repo, adapter_roots=adapter_roots,
        )
        if scope_audit is not None and touch_overlaps:
            # 실행 전에 이미 경고한 겹침 경로를 회수 시점 판정 입력으로 싣는다. 캡처 함수의
            # 입력이 아니라 이 위임이 무엇을 겹친다고 알렸는지의 기록이다.
            scope_audit = scope_audit._replace(
                overlap_paths=delegate_scope_audit.overlap_touch_paths(touch_overlaps),
            )
        try:
            target_rev = (
                scope_audit.before.head or None if scope_audit is not None else None
            )
            print(
                f"하네스 {harness}(model={model}) 실행 중 (유료 호출).",
                file=sys.stderr,
            )
            advisory = delegate_execute.native_advisory(harness, args.role)
            if advisory is not None:
                print(advisory, file=sys.stderr)
            _ticket_copy_handed_off = True  # 여기부터 harvest 가 정리를 넘겨받는다
            return delegate_execute._execute_and_collect(
                pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), args=args, harness=harness, model=model, reasoning=reasoning,
                fallback=fallback, fallback_skip=fallback_skip, cwd=cwd,
                prompt=prompt,
                timeout=timeout, fallback_timeout=fallback_timeout,
                output_dir=output_dir, run_fn=_run,
                local_conf_path=conf_path,
                profile_source=profile_source,
                resume=resume,
                # 재개 라운드의 티켓 식별자 **계승** — 명시 `--ticket` 이 우선이고, 없으면 이어받은
                # 레코드의 티켓을 그대로 싣는다. 안 실으면 이 라운드 레코드에 ticket 이 없어 **다음**
                # 재개의 티켓 지시자 선택(최신 1건)에서 빠지고, 재개 사슬이 한 라운드 만에 끊긴다.
                # 범위 판정(scope audit)은 계승하지 않는다 — 그건 선언 touches 의 축이라 이 실행이
                # 무엇을 선언했는지가 기준이다.
                ticket=effective_ticket or (resume.ticket if resume is not None else None),
                fresh_reason=(args.fresh.strip() if args.fresh is not None else None),
                # 이 위임의 기준 rev — 이미 캡처한 실행-전 worktree 상태를 그대로 쓴다(추가 git 호출 0).
                base_rev=target_rev,
                ticket_copy=ticket_copy,
                cluster_plan=cluster_plan,
            )
        finally:
            pending_exception = sys.exception()
            delegate_scope_audit.report_scope_audit(scope_audit, args.role)
            if ticket_copy is not None and _ticket_copy_handed_off:
                try:
                    if cluster_plan is not None:
                        # 묶음은 자리마다 독립 회수다 — 한 자리의 거부가 다른 자리의 교체를
                        # 되돌리지 않는다(회수 판정식은 단일 경로와 같은 하나다).
                        delegate_execute._report_cluster_harvest(delegate_harvest.harvest_cluster_copy(
                            pm_paths.home_at(REPO, pm_home=Path(config_repo).resolve(), board_root=pm_paths._board_root_at(Path(config_repo).resolve())), run_dir=cluster_plan.run_dir, cwd=cwd_repo,
                            ))
                    else:
                        result = delegate_harvest.harvest_ticket_copy(
                            pm_paths.home_at(REPO, pm_home=Path(config_repo).resolve(), board_root=pm_paths._board_root_at(Path(config_repo).resolve())), copy_path=ticket_copy.path, cwd=cwd_repo, )
                        print(
                            "[pm-delegate] ticket harvest: "
                            f"{'applied' if result.changed else 'unchanged'} · "
                            f"sync_ready={str(result.sync_ready).lower()} · "
                            f"copy={ticket_copy.path}",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    # 정상 return rc에는 harvest 실패 rc=1이 더 강하다. 반면 runner/실행 예외가 이미
                    # 전파 중이면 finally return으로 삼키지 않고 원 예외를 그대로 보존하며 두 원인을
                    # 모두 stderr에 남긴다.
                    residue = (
                        cluster_plan.run_dir if cluster_plan is not None
                        else ticket_copy.path
                    )
                    recovery = delegate_execute._delegation_harvest_failure_message(exc, residue)
                    if pending_exception is not None:
                        print(
                            "오류: 위임 실행 예외도 그대로 전파합니다: "
                            f"{type(pending_exception).__name__}: {pending_exception}",
                            file=sys.stderr,
                        )
                        delegate_execute.fail_loud(recovery)
                    else:
                        if _is_engine_rev_skew(exc):
                            raise
                        return delegate_execute.fail_loud(recovery)
    finally:
        if ticket_copy is not None and not _ticket_copy_handed_off:
            seats = (
                tuple(cluster_plan.rounds) if cluster_plan is not None
                else (ticket_copy,)
            )
            for seat in seats:
                delegate_prepare._refund_gate_rejected_ticket_copy(
                    pm_paths.home_at(REPO, pm_home=Path(config_repo).resolve(), board_root=pm_paths._board_root_at(Path(config_repo).resolve())), seat, cwd=cwd_repo, )


if __name__ == "__main__":
    sys.exit(main())
