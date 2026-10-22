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
import os
import sys
import tempfile
from pathlib import Path
from typing import Callable
from functools import partial

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.app import delegate_changelog
from pm_engine.cli import delegate_cmd
from pm_engine.app import delegate_cluster_review
from pm_engine.app import delegate_execute
from pm_engine.app import delegate_harvest
from pm_engine.app import delegate_prepare
from pm_engine.app import delegate_prompt
from pm_engine.domain import delegate_rounds
from pm_engine.app import delegate_scope_audit
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
from pm_engine.app import ticket_access  # noqa: E402
from pm_engine.cli import task_args  # noqa: E402
from pm_engine.domain import conf_schema, delegate_errors, identity_args, review_form  # noqa: E402
from pm_engine.infra import console, delegate_ledger, fs, git, pm_paths, repo_files  # noqa: E402
from pm_engine.infra.harness import contract  # noqa: E402
_load_module_from_path = repo_files.load_module


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — 이 값은 이 파일 코드 안에 고정된다(engine_rev.py 런타임 읽기 아님). 부분/수동
# 복사로 신 로더 + 구 형제가 섞이면 각자 새/옛 리터럴을 지녀 대조에서 skew 로 검출된다.
# 릴리즈 bump 는 `engine_rev.py --bump vX.Y.Z` 가 전 stamped 모듈 리터럴을 기계 일괄
# 재작성한다(사람 N곳 편집 0).
ENGINE_REV = "v1.8.1"


# ── REPO 앵커 (설치 깊이 고정 · hermetic 테스트 monkeypatch seam) ──────────
# 설치 깊이는 `pm_import` 가 못 박는다 — 도구는 `<root>/.project_manager/tools/` 에만 놓인다.
# REPO 는 module-level 상수로 유지해 hermetic 테스트가 monkeypatch 할 수 있게 한다
# 구현은 pm_engine의 실제 소유 모듈을 직접 사용하고, 이 앵커는 잔류 CLI 입력만 구성한다.

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


# ── 설정 ──────────────────────────────────────────────────────────────────

_CONFIG_REPO_OVERRIDE: Path | None = None


def _activate_internal_rounds_cli_owner() -> Path:
    """현재 엔진 사본에서 소유 PM 홈을 해소해 내부/공유 raw 장부 좌표를 맞춘다."""
    global _CONFIG_REPO_OVERRIDE
    if _CONFIG_REPO_OVERRIDE is None:
        owner = pm_paths.resolve_pm_home_for_repo(REPO)
        _CONFIG_REPO_OVERRIDE = Path(owner).resolve()
    return _CONFIG_REPO_OVERRIDE


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
        rc = max(rc, delegate_changelog._resolve_ticket_pm_verified(
            home, ticket, cluster=cluster, tree=tree,
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
        return delegate_cmd._cmd_lint(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), resolved[1:])
    if resolved and resolved[0] == "raw":
        return delegate_cmd._cmd_raw(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), resolved[1:])
    if resolved and resolved[0] == "rounds":
        return _cmd_rounds(resolved[1:])
    if resolved and resolved[0] == "ticket":
        return delegate_cmd._cmd_ticket(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), resolved[1:], adapter_roots=_resolved_adapter_directories())
    if resolved and resolved[0] == "review":
        return delegate_cmd._cmd_review(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), resolved[1:])
    if resolved and resolved[0] == "changelog":
        return delegate_cmd._cmd_changelog(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE, board_root=pm_paths._board_root_at(_CONFIG_REPO_OVERRIDE or REPO)), resolved[1:], git_run_fn=git_run_fn)
    parser = delegate_cmd.build_arg_parser()
    args = parser.parse_args(resolved)
    try:
        cwd = delegate_cmd._validate_args(parser, args)
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
                note, _transport_note = delegate_cmd._dry_run_harness_annotations(
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
            _budget_note, transport_note = delegate_cmd._dry_run_harness_annotations(harness)
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
