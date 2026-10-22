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
import contextlib
import datetime
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Callable, NamedTuple
from functools import partial

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.infra import conf_store
from pm_engine.infra import delegate_config
from pm_engine.infra.harness import delegate_env
from pm_engine.domain import delegate_prompt_rules
from pm_engine.infra import delegate_raw
from pm_engine.infra.harness import delegate_run
from pm_engine.domain import delegate_run_rules
from pm_engine.infra import delegate_scope_git
from pm_engine.domain import delegate_scope_rules
from pm_engine.infra.harness import delegate_temp
from pm_engine.infra.harness import delegate_transport
from pm_engine.domain import reviewer_result
from pm_engine.infra import reviewer_target
from pm_engine.app import cluster_binding, round_maintenance  # noqa: E402
from pm_engine.infra import cluster_store  # noqa: E402
from pm_engine.app import ticket_access, hooks, regression, round_seed  # noqa: E402
from pm_engine.cli import task_args  # noqa: E402
from pm_engine.domain import conf_schema, delegate_errors, identity_args, markdown_text, model_lint, review_form, review_state, review_verdict, round_rules, ticket_rules  # noqa: E402
from pm_engine.infra import console, delegate_ledger, fs, git, pm_paths, raw_output, repo_coordinates, repo_files, round_store, ticket_store  # noqa: E402
from pm_engine.infra.harness import contract, watchdog  # noqa: E402

from pm_engine.infra import board_git
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


# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "ticket_copy_gate_refund": (
        "F-001 — 단일 정리 경계의 환불(abandon)은 **원 거부/전파 예외를 덮지 않는 것**이 "
        "계약이다 — 여기서 예외를 올리면 이미 확정된 거부 rc·사유나 전파 중인 예외가 정리 실패로 "
        "뒤바뀐다. 환불 수단이 형제 모듈(`abandon_ticket_copy` → board/fs)이라 사본 "
        "불일치가 이 경계에 도달할 수 있는데, 그것도 정리 실패의 한 형태로 흡수하되 원인을 문구로 "
        "구분해 '정리 실패'와 '엔진 사본 불일치'가 같은 경고로 뭉개지지 않게 한다"
    ),
    "ticket_copy_prepare_rollback": (
        "F-001 — 예약 전 rollback 정리는 **원 예외를 덮지 않는 것**이 계약이다 — 여기서 "
        "갈아타면 예약이 왜 실패했는지가 사라진다. 삭제 수단이 형제 모듈(`fs.force_rmtree`)"
        "이라 사본 불일치가 이 경계에 닿을 수 있는데, 같은 이유로 흡수하되 경고 한 줄로 남겨 "
        "재동기 처방이 함께 사라지지 않게 한다"
    ),
}


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """정리 경계가 marked skew 를 의도적으로 흡수했음을 표시한다 (True=흡수·사유 등록 필수).

    반환값으로 일반 정리 실패와 사본 불일치를 구분해 호출부가 진단 문구를 달리한다 — 흡수는
    하되 조용하지는 않다. 사유가 등재되지 않은 경계는 fail-loud(ValueError) — 흡수는 장부에
    남은 판단이어야 감사 가능하다.
    """
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    return _is_engine_rev_skew(exc)


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


def fail_loud(message: str, *, rc: int = 1) -> int:
    """위임 실패 종료의 **단일 깔때기** — 사유 + 무음 대체 금지 안내를 stderr 에 내고 rc 를 돌려준다.

    실패 종료 지점은 여러 곳(타임아웃·비정상 rc·정리 실패·reply 미추출·폴백 소진)인데, 안내를
    지점마다 손으로 이어붙이면 **새 종료 경로가 조용히 안내를 빠뜨린다**. 그래서 `NO_SILENT_SUBSTITUTE_NOTE` 의 소비자는 이 함수 하나뿐이고(테스트가 그 불변을 못박는다),
    실패 경로는 `return fail_loud(...)` 로만 끝난다.
    """
    print(f"{message}\n{delegate_run_rules.NO_SILENT_SUBSTITUTE_NOTE}", file=sys.stderr)
    return rc

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


def _role_preamble(role: str, adapter_directories: Sequence[str]) -> str:
    lines = [delegate_prompt_rules._ROLE_IDENTITIES[role], delegate_prompt_rules._prohibition(adapter_directories)]
    if role in delegate_prompt_rules.REGRESSION_SCOPE_ROLES:
        lines.append(delegate_prompt_rules.REGRESSION_SCOPE_PREAMBLE)
    if role == review_form.INTERNAL_REVIEW_ROLE:
        lines.append(_internal_review_format_preamble())
    return "\n".join(lines)


class TerminalFixHarvestError(delegate_errors.DelegateError):
    """마지막 fix 회수 거부 — 증거는 보존하되 복구 라운드는 없는 종단 상태."""


# 그중 하네스로 위임돼 slot run-dir 을 준비하는 역할. additional-reviewer 라운드는 슬롯 왕복이
# 아니라 additional_reviewer 엔진이 직접 쓴다(추가 리뷰어에게 슬롯 편집 권한을 주지 않는다).
# researcher 는 묶음 라운드 수열(architect → developer → code-reviewer → developer)의 단계가 아니다 —
# 티켓 라운드를 준비하지 않는다(읽기 전용 조사 결과는 회신으로 돌려받는다).
TICKET_COPY_PREPARE_ROLES: frozenset[str] = delegate_ledger.TICKET_COPY_ROLES - {review_form.ADDITIONAL_REVIEWER_ROLE, "researcher"}
# 사본 루트를 숨기는 ignore 규칙의 정본 위치([[T-0704]]) — 이 파일 유래가 아니면 로컬 전용
# 소스(`.git/info/exclude`·전역 excludesFile 등)로 보고 fail-loud 한다.
TICKET_COPY_IGNORE_TRACKED_SOURCE = Path(".project_manager") / ".gitignore"
# 두-git 풀 슬롯(앱 저장소에 `.project_manager` 가 없는 형상)의 정본은 그 풀 bare 의
# `info/exclude` 에 있는 이 줄이다. bare 의 exclude 는 `$GIT_COMMON_DIR/info/exclude` 라 그 풀의
# 모든 linked worktree 가 공유하므로, 위 tracked 정본이 지키려는 성질(사본 루트를 숨기는 규칙이
# 이 코드 트리의 모든 사본에 있다)이 이 형상에서도 그대로 성립한다. 규칙을 심는 쪽은
# `pm_config._ensure_bare_ticket_copy_exclude` 이고 두 값은 같은 문자열이어야 한다
# (불변식은 테스트가 고정). 루트 앵커 디렉터리 규칙이라 저장소가 추적하는
# `.project_manager/tools`·`wiki` 의 새 파일은 숨기지 않는다.
TICKET_COPY_POOL_IGNORE_RULE = "/.project_manager/.local/"
_TICKET_COPY_FD_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in getattr(os, "supports_dir_fd", frozenset())
    and os.mkdir in getattr(os, "supports_dir_fd", frozenset())
)


# 준비가 슬롯 run-dir 에 까는 읽기 전용 입력. 쓸 수 있는 파일은 라운드 파일 하나뿐이라
# (`NN-<role>.md`) 회수는 그 하나만 본다 — "절 밖 bytes 대조" 가 필요한 자리가 없다.
TICKET_COPY_SPEC_NAME = "spec.md"
TICKET_COPY_ROUNDS_DIRNAME = "rounds"
# 증거 없는 예약을 지우는 유일한 통로 — 무엇을 강제하는지가 아니라 **운영자가 무엇을 확인했는지**
# 를 말하는 이름이다. 문구와 CLI 가 같은 값을 쓰도록 여기 한 자리에 둔다.
ABANDON_ASSUME_DEAD_FLAG = "--assume-dead"
# 재실행으로 대체된 라운드의 "시드 그대로" 거부를 여는 유일한 통로 — 값은 대체본 라운드의
# ordinal 이다(자기 자신 참조는 거부). `--assume-dead` 와 같은 문구/CLI 단일 출처 원칙.
ABANDON_SUPERSEDED_BY_FLAG = "--superseded-by"
# 재실행 대체가 아닌 **폐기**로 처분하는 유일한 통로 — 값은 사람이 읽는 한 줄 사유다. 처분은
# 회수 계약과 독립이지만 선언 없는 처분은 조용한 파괴라, 이 값이 그 라운드를 왜 지웠는지에 대한
# 유일한 기록이 된다(`--superseded-by` 와 배타 · 같은 문구/CLI 단일 출처 원칙).
ABANDON_DISCARD_REASON_FLAG = "--discard-reason"
# 준비가 장부 행에 `base_rev`(슬롯 HEAD)를 남기는 역할.
_BASE_REV_ROLES: frozenset[str] = frozenset({"developer", review_form.INTERNAL_REVIEW_ROLE})


class TicketCopyPlan(NamedTuple):
    """준비 1회의 좌표 — 슬롯 라운드 파일 하나와 그 run-dir, board 예약 결과.

    `run_id` 는 delegate-rounds 장부의 같은 필드와 값이 같다 — cross 실위임 raw 행이 이 값을
    `extra` 로 실으면 두 장부가 문자열 일치로 결속된다."""

    path: Path
    run_dir: Path
    ticket: str
    role: str
    ordinal: int
    board_path: Path
    run_id: str
    # 이 라운드가 쓸 첫 finding ID 실값(리뷰 채널이 아니면 빈 값). 준비가 시드에 실은 것과 같은
    # 값이라 사본 프리앰블이 다시 계산하지 않는다 — 프롬프트와 시드가 다른 번호를 말하지 않는다.
    # 기본값이 없다: 값을 넘기지 않은 조립은 옛 프롬프트로 접히지 않고 TypeError 로 터진다.
    next_finding_id: str
    # 이 준비가 속한 묶음 — 크기 1 도 묶음이라 항상 실값이다(경로·장부 행이 이 값으로 갈린다).
    cluster: str = ""
    # 이 라운드가 속한 **묶음 run-dir**(`ClusterCopyPlan.run_dir`) — 위 `run_dir` 은 이 티켓
    # 자리이고, 이 값은 그 자리들을 담는 run 전체다. 쓰기 허용 범위가 run-dir 전체라 하네스가
    # 여는 좌표는 이 값이다(자리 경로에서 역산하지 않는다 — 역산은 layout 이 바뀌면 조용히
    # 어긋난다).
    cluster_run_dir: Path | None = None


class ClusterCopyPlan(NamedTuple):
    """준비 1회의 묶음 좌표 — run-dir 하나와 그 안의 티켓별 라운드 좌표들.

    `rounds` 는 선언 순서(멤버 순서)를 지킨다 — 크기 1 이면 원소 하나이고 그 원소가 종전
    `prepare_ticket_copy` 의 반환값이다.
    """

    run_dir: Path
    cluster: str
    run_id: str
    role: str
    rounds: tuple[TicketCopyPlan, ...]


class TicketHarvestResult(NamedTuple):
    changed: bool
    sync_ready: bool
    # 회수 machine line 의 안정된 key 자리 — verify 판정은 회수 게이트
    # (`_developer_round_harvest_problem`) 한 층이 소유하므로 회수까지 온 산출에는 실을 값이
    # 없다(항상 빈 tuple). 태만한 accepted 행의 이름은 `review verify-template` 이 낸다.
    verify_missing: tuple[str, ...] = ()


class TicketAbandonResult(NamedTuple):
    """포기 처분의 **수렴 보고** — 상태 선언이 아니라 세 자산 재판독의 결과다.

    `changed` 는 이번 호출이 실제로 무언가를 바꿨는가이고, 나머지 셋은 호출이 끝난 뒤 다시 읽은
    값이다. `board_removed` 는 중간 순번 보존일 때 False 다(보존이 정상 종결이다).
    """

    changed: bool
    sync_ready: bool
    board_removed: bool = False
    run_dir_removed: bool = False
    converged: bool = False


def _write_exclusive_file(
    path: Path, data: bytes, mode: int, *, parent_fd: int | None = None,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    target = path.name if parent_fd is not None else str(path)
    open_kwargs = {"dir_fd": parent_fd} if parent_fd is not None else {}
    fd = os.open(target, flags, mode, **open_kwargs)
    try:
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(fd)


def _append_delegate_rounds_ledger(pm_home: Path, row: dict) -> None:
    """공유 atomic-append seam 으로 0600 장부에 한 행을 덧붙인다(기존 bytes 불변)."""
    delegate_ledger._delegate_rounds_ledger_row(row, line_number=0)
    local_dir, local_fd = _secure_machine_dir(
        pm_home, delegate_ledger.DELEGATE_ROUNDS_LEDGER_REL_PATH.parent.parts,
        label="PM delegate-rounds 장부 디렉터리",
    )
    path = local_dir / delegate_ledger.DELEGATE_ROUNDS_LEDGER_REL_PATH.name
    try:
        try:
            observed = path.lstat()
        except FileNotFoundError:
            observed = None
        if observed is not None and not stat.S_ISREG(observed.st_mode):
            raise delegate_errors.DelegateError(f"delegate-rounds 장부가 regular file 이 아님: {path}")
        payload = (
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        fs.append_atomic(path, payload, mode=0o600)
    except OSError as exc:
        raise delegate_errors.DelegateError(f"delegate-rounds 장부 append 실패: {path}: {exc}") from exc
    finally:
        if local_fd is not None:
            os.close(local_fd)


def _delegate_round_record(pm_home: Path, copy_path: Path) -> dict:
    """이 슬롯 파일의 **준비 기록** — 없으면 회수를 거부한다(신뢰 뿌리는 PM 홈 장부다)."""
    resolved = str(Path(copy_path).resolve())
    matches = [
        row for row in delegate_ledger._delegate_rounds_ledger_records(pm_home)
        if str(Path(row["copy"]).resolve()) == resolved
    ]
    if not matches:
        raise delegate_errors.DelegateError(
            f"delegate-rounds 장부에 준비 기록 없음: {resolved} — 준비하지 않은 파일은 "
            "회수하지 않습니다"
        )
    return matches[-1]


def _secure_machine_dir(
    root: Path, relative_parts: tuple[str, ...], *, label: str,
) -> tuple[Path, int | None]:
    """root 아래 기계 디렉터리를 no-symlink chain으로 만들고 0700으로 고정한다."""
    root = root.resolve()
    current = root
    if _TICKET_COPY_FD_SUPPORTED:
        try:
            current_fd = os.open(
                str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        except OSError as exc:
            raise delegate_errors.DelegateError(f"slot cwd를 안전하게 열 수 없음: {root}: {exc}") from exc
        try:
            for part in relative_parts:
                if part in ("", ".", "..") or "/" in part or "\\" in part:
                    raise delegate_errors.DelegateError(f"{label} 경로 성분 거부: {part!r}")
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=current_fd,
                    )
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"{label} 경로 symlink/비-directory 거부: {current / part}: {exc}"
                    ) from exc
                os.close(current_fd)
                current_fd = child_fd
                current = current / part
            delegate_temp._assert_attempt_child_path(root, current, label=label)
            return current, current_fd
        except BaseException:
            os.close(current_fd)
            raise
    for part in relative_parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part:
            raise delegate_errors.DelegateError(f"{label} 경로 성분 거부: {part!r}")
        candidate = current / part
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            observed = candidate.lstat()
        except OSError as exc:
            raise delegate_errors.DelegateError(f"{label} 검사 실패: {candidate}: {exc}") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise delegate_errors.DelegateError(f"{label} symlink/비-directory 거부: {candidate}")
        resolved = candidate.resolve()
        if resolved.parent != current.resolve():
            raise delegate_errors.DelegateError(f"{label} escape/교체 거부: {candidate}")
        current = candidate
    delegate_temp._assert_attempt_child_path(root, current, label=label)
    return current, None


def _secure_cluster_run_dir(cwd: Path, cluster: str, run_id: str) -> tuple[Path, int | None]:
    return _secure_machine_dir(
        cwd, delegate_ledger._cluster_run_relative_dir(cluster, run_id).parts,
        label="티켓 라운드 run-dir",
    )


def _secure_ticket_copy_dir(
    cwd: Path, cluster: str, run_id: str, ticket: str, *,
    extra_parts: tuple[str, ...] = (),
) -> tuple[Path, int | None]:
    return _secure_machine_dir(
        cwd,
        (*delegate_ledger._ticket_run_relative_dir(cluster, run_id, ticket).parts, *extra_parts),
        label="티켓 라운드 run-dir",
    )


def _ticket_copy_relative_path(
    cluster: str, run_id: str, ticket: str, filename: str,
) -> Path:
    """run-dir 안 한 파일의 cwd 상대 경로 (ignore 검증·경로 예산 단언의 입력)."""
    return delegate_ledger._ticket_run_relative_dir(cluster, run_id, ticket) / filename


def _parse_check_ignore_verbose_line(line: str) -> tuple[str, str, str] | None:
    """`git check-ignore -v` 한 줄을 (source, linenum, pattern) 으로 분해한다.

    비-`-z` 출력 형식은 `소스:줄번호:패턴` 다음에 탭 하나, 그리고 pathname 이 온다([[T-0704]] F-005 —
    대안으로 `git check-ignore -v -z --stdin` 에 pathname 하나를 stdin 으로 주면 rc=0 에 소스·줄번호·
    패턴·pathname 4필드가 NUL 로 구분돼 나와 모호성이 0 이지만(실측), 위임 호출은 pathname 1건
    고정값이라 stdin 배관을 더할 이득이 낮아 채택하지 않았다 — 단일 인자에 `-vz`(`--stdin` 없이)는
    "fatal: -z 옵션은 --stdin 옵션과 같이 써야만 의미가 있습니다" 로 즉시 실패한다(git 2.43 실측)).
    pathname 은 호출부가 구성한 알려진 상대경로라 마지막 탭 분리(`rpartition`)로 안전하게 떼어낸다 —
    패턴 문자열 자체에 탭이 들어 있어도 pathname 은 안 잘린다. 남는 `소스:줄번호:패턴`은 그리디
    정규식으로 오른쪽에서부터 역추적해 최우측 콜론-숫자-콜론 구분자를 찾는다 — Windows 절대경로
    출처(드라이브 문자 뒤 콜론, 확장경로 접두 표기의 백슬래시 두 개·물음표·백슬래시)의 콜론이
    숫자로 이어지지 않아 구분자로 오인되지 않고, source 경로 내부에 콜론이 더 있어도 줄번호와
    가장 가까운(최우측) 매치를 고른다.
    """
    prefix, tab, _pathname = line.rpartition("\t")
    if not tab:
        return None
    match = re.match(r"^(.*):(\d+):(.*)$", prefix)
    if match is None:
        return None
    return match.group(1), match.group(2), match.group(3)


def _ignore_source_definitely_untracked(cwd: Path, source: str) -> bool:
    """`ls-files` 호출 없이도 tracked 일 수 없다고 판정 가능한 출처 형태([[T-0704]] F-001/F-002/F-006).

    저장소 밖 출처(전역 `core.excludesFile` — 절대경로뿐 아니라 `../outside_ignore` 처럼 저장소
    밖을 가리키는 상대경로도 있다·실측 — 와 linked worktree 의 `.git/info/exclude` 는 공유 gitdir
    바깥 절대경로로 보고된다)와 `.git/` 내부 경로(`.git/info/exclude`)는 git 인덱스가 원천 추적할
    수 없는 위치다. 이런 값을 `ls-files` 에 그대로 넘기면 "저장소 밖" 류 fatal 로 엉뚱하게 실패하므로,
    호출 전에 `(cwd/source).resolve()` 가 `cwd.resolve()` 아래인지로 절대·상대 두 형태를 한 규칙으로
    걸러 바로 untracked 로 판정한다.
    """
    resolved_source = (cwd / source).resolve()
    if not delegate_env._is_relative_to(resolved_source, cwd.resolve()):
        return True
    return Path(source).parts[:1] == (".git",)


def _check_ignore_source_is_tracked(cwd: Path, source: str) -> bool:
    """`source`(check-ignore 가 보고한 -C cwd 상대경로)가 git 인덱스에 tracked 인지 확인한다.

    rc=0 tracked · rc=1 untracked · 그 외는 도구 실패로 fail-loud(board.py:4405 `ls-files
    --error-unmatch` 와 같은 seam).
    """
    if _ignore_source_definitely_untracked(cwd, source):
        return False
    result = subprocess.run(
        ["git", "-C", str(cwd), "ls-files", "--error-unmatch", "--", source],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    cause = result.stderr.strip() or result.stdout.strip() or "원인 미상"
    raise delegate_errors.DelegateError(f"git ls-files 실패(rc={result.returncode}): {cause}")


def _pool_bare_for_code_root(cwd: Path) -> Path | None:
    """이 코드 트리가 두-git 풀 슬롯이면 그 풀의 공용 bare 를, 임베드면 None 을 돌려준다.

    형상 규칙 사본을 만들지 않는다 — 소유 PM 홈은 `pm_paths.owning_pm_home` 이 이 트리의 `.git`
    선언에서 유도하고, 여기서는 두 값만 비교한다. 그 유도가 인정하는 공용 저장소 형상은
    `<X>/.git`(임베드·단일 clone·그 clone 이 판 linked worktree)과 `<X>/.repos/<repo>.git`
    (풀 슬롯) 둘뿐이고 그 밖은 예외이므로, 공용 저장소가 `<PM 홈>/.git` 이 아니면 남는 형상은
    풀 bare 하나이며 그 값이 곧 bare 다.
    """
    common = Path(_cluster_git(cwd, "rev-parse", "--git-common-dir").strip())
    if not common.is_absolute():
        common = cwd / common
    common = common.resolve()
    pm_home = Path(pm_paths.owning_pm_home(cwd)).resolve()
    if common == (pm_home / ".git").resolve():
        return None
    return common


def _check_ignore_observation(result: subprocess.CompletedProcess) -> str:
    """check-ignore 관측을 진단 한 조각으로 요약한다 — 이 문자열은 **판정 입력이 아니다**.

    풀 갈래가 보는 것은 rc(사본 루트가 실제로 무시되는가)와 bare 규칙 존재 둘뿐이고, 어느
    출처가 그 판정을 냈는지는 진단 문구에만 싣는다.
    """
    if result.returncode == 1:
        return "현재 이 경로를 무시하는 규칙이 없다"
    lines = [entry for entry in (result.stdout or "").splitlines() if entry]
    parsed = _parse_check_ignore_verbose_line(lines[0]) if len(lines) == 1 else None
    if parsed is None:
        return f"현재 check-ignore 출력={result.stdout!r}"
    source, linenum, pattern = parsed
    return f"현재 check-ignore 출처={source}:{linenum} 패턴={pattern!r}"


def _assert_pool_bare_ignore_rule(bare: Path, *, ignored: bool, observation: str) -> None:
    """풀 슬롯의 정본 — 사본 루트가 실제로 무시되고 그 규칙이 이 풀 bare 의 `info/exclude` 에
    있는지 fail-loud 확인한다. 둘을 함께 요구한다.

    규칙 존재만 보면 슬롯 안 `.gitignore` 의 부정 규칙 한 줄이 정본을 뒤집어도 통과한다 — 그
    형상에서 사본 루트는 실제로 무시되지 않아 게이트가 지키려던 성질이 이 갈래에서만 사라진다.
    거꾸로 check-ignore 가 어느 출처를 보고했는지는 판정 입력이 아니다. 중첩 `.gitignore` 가
    `info/exclude` 보다 우선순위가 높아, 출처로 판정하면 규칙이 없는 풀도 그 중첩 파일 하나로
    우연히 통과한다. 파일 부재는 규칙 부재와 같은 거부이고, 읽기 실패는 그대로 올린다
    (빈 값으로 바꾸지 않는다).
    """
    exclude = bare / "info" / "exclude"
    if not ignored:
        raise delegate_errors.DelegateError(
            f"사본 루트가 이 슬롯에서 실제로 무시되지 않음 — {observation} · 정본은 {exclude} 의 "
            f"`{TICKET_COPY_POOL_IGNORE_RULE}` 줄이고 `pm-config repo add {bare.stem}` 1회로 "
            "엔진이 멱등 설치한다 · 그 줄이 이미 있으면 슬롯 안 `.gitignore` 의 부정 규칙이 "
            "정본보다 우선해 뒤집은 것이니 그 줄을 지워라"
        )
    lines = (
        fs
        .read_text_shared(exclude, encoding="utf-8", errors="replace")
        .splitlines()
        if exclude.is_file() else []
    )
    if any(line.strip() == TICKET_COPY_POOL_IGNORE_RULE for line in lines):
        return
    raise delegate_errors.DelegateError(
        f"사본 루트를 숨기는 ignore 규칙이 이 풀의 bare 에 없음 — 정본은 {exclude} 의 "
        f"`{TICKET_COPY_POOL_IGNORE_RULE}` 줄이다({observation}) · 이 bare 를 공유하는 모든 "
        f"슬롯이 그 규칙을 함께 받는다 · `pm-config repo add {bare.stem}` 1회로 엔진이 멱등 "
        "설치한다"
    )


def _assert_ticket_copy_root_ignored(
    cwd: Path, *, cluster: str, run_id: str, ticket: str,
) -> None:
    """ignore 규칙이 실제 ticket-copy 경로 형상을 숨기는지, 임베드 형상이면 그 규칙이 tracked
    `.project_manager/.gitignore` 유래인지 fail-loud 확인한다([[T-0704]]).

    정본은 형상마다 정확히 하나다 — 임베드·단일 clone 은 tracked
    `.project_manager/.gitignore`(아래 본문), 두-git 풀 슬롯은 그 풀 bare 의 `info/exclude`
    (`_assert_pool_bare_ignore_rule`)다. 한쪽이 거부돼도 다른 후보로 재시도하지 않는다.
    앱 저장소에 `.project_manager` 가 없는 풀 슬롯은 tracked 정본을 가질 수 없어, 정본을
    파일 경로 하나로 못박으면 구조적으로 통과할 수 없다.

    경로가 정본 위치와 같아도 그 파일 자체가 untracked 면(예: fresh import 직후 아직 `git add`
    하지 않은 형상) 다른 클론에는 없는 로컬 산출물일 수 있어 별도로 `ls-files --error-unmatch` 로
    확인한다(F-001). 정본 위치가 아닌 출처는 그 출처 자신이 tracked 인지에 따라 진단 문구를
    가른다(F-002) — tracked 비정본 위치(예: 루트 `.gitignore`)는 다른 클론에도 있으므로 "이
    클론에만 있다"는 말은 거짓이고, untracked 출처(`.git/info/exclude`·전역 `core.excludesFile`·
    untracked 상위 `.gitignore`)만 그 말이 사실이다. tracked 판정은 `ls-files --error-unmatch`
    그대로라 인덱스 등록(staged)까지를 tracked 로 본다 — 커밋 여부는 보지 않는다(F-009: 첫 커밋
    전 채택자 트리를 오차단하지 않기 위해서다).
    """
    result = subprocess.run(
        [
            "git", "-C", str(cwd), "check-ignore", "-v",
            _ticket_copy_relative_path(
                cluster, run_id, ticket, TICKET_COPY_SPEC_NAME,
            ).as_posix(),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    if result.returncode not in (0, 1):
        cause = result.stderr.strip() or result.stdout.strip() or "원인 미상"
        raise delegate_errors.DelegateError(f"git check-ignore 실패(rc={result.returncode}): {cause}")
    bare = _pool_bare_for_code_root(cwd)
    if bare is not None:
        _assert_pool_bare_ignore_rule(
            bare,
            ignored=result.returncode == 0,
            observation=_check_ignore_observation(result),
        )
        return
    if result.returncode == 1:
        raise delegate_errors.DelegateError(
            "사본 루트가 git 무시 대상이 아님 — `.project_manager/.gitignore` 의 "
            "`.local/` 규칙을 복원하라"
        )
    lines = [entry for entry in (result.stdout or "").splitlines() if entry]
    parsed = _parse_check_ignore_verbose_line(lines[0]) if len(lines) == 1 else None
    if parsed is None:
        raise delegate_errors.DelegateError(
            "git check-ignore -v 출력을 해석하지 못함(source:linenum:pattern 형식 아님): "
            f"{result.stdout!r}"
        )
    source, linenum, pattern = parsed
    expected = (cwd / TICKET_COPY_IGNORE_TRACKED_SOURCE).resolve()
    observed = (cwd / source).resolve()
    if observed != expected:
        if _check_ignore_source_is_tracked(cwd, source):
            reason = "tracked 비정본 위치 유래 — 다른 클론에도 있지만 정본 위치가 아님"
        else:
            reason = "로컬 전용 소스 유래 — 다른 클론·채택자 트리에는 이 규칙이 없음"
        raise delegate_errors.DelegateError(
            "사본 루트를 숨기는 ignore 규칙이 정본 위치(`.project_manager/.gitignore`) 밖 출처 — "
            f"{reason} — 출처={source}:{linenum} 패턴={pattern!r} · `.project_manager/.gitignore` "
            "에 `.local/` 규칙을 복원하라"
        )
    if not _check_ignore_source_is_tracked(cwd, TICKET_COPY_IGNORE_TRACKED_SOURCE.as_posix()):
        raise delegate_errors.DelegateError(
            "사본 루트를 숨기는 `.project_manager/.gitignore` 규칙이 untracked 상태 — "
            f"출처={source}:{linenum} 패턴={pattern!r} · 이 클론에만 있는 파일이라 다른 "
            "클론·채택자 트리에는 없어 사본이 노출됩니다 — `.project_manager/.gitignore` 를 "
            "git add 로 커밋 이력에 넣어라"
        )


_TICKET_PREPARE_STATUSES: tuple[str, ...] = ("open", "claimed")


def _ticket_round_seed(
    role: str, ticket_text: str, previous_round, *,
    today: str, rounds: Sequence = (),
) -> str:
    """이 라운드 파일의 시드 bytes — 렌더 규약은 공용 seam 이 소유한다."""
    return round_seed.render_round_seed(
        role, ticket_text, today=today, previous_round=previous_round, rounds=rounds,
    )


def _reserved_round_residue(board_rel: str, ordinal: int) -> str:
    """예약 뒤 실패 경로의 진단 꼬리 — 남은 board 라운드 좌표와 그 라운드의 이후 상태.

    예약은 되돌리지 않는다(보상 삭제는 동시 준비가 다음 순번을 이미 잡았을 때 순번 빈틈을
    만든다). 그래서 실패 진단이 무엇이 남았는지 좌표로 말해야 운영자가 board 에서 그 라운드를
    찾을 수 있다.
    """
    return (
        f" · 예약한 board 라운드는 남습니다: {board_rel}(순번 {ordinal}) — 산출 없음으로 남고 "
        "다음 준비는 다음 순번을 씁니다"
    )


def _expected_slot_round_path(cwd: Path, row: dict) -> Path:
    """장부 행이 인가하는 **유일한** 슬롯 경로 — 회수 입력 검증의 단일 단언.

    행의 (ticket, run_id, ordinal, role) 로 경로를 다시 조립해 전량 일치를 요구한다. 규칙이
    하나라 containment(사본 루트 밖)·깊이(run-dir 상위·하위)·파일명 결속(순번·역할)·티켓 결속이
    함께 닫힌다 — 축마다 다른 검사를 두면 하나가 빠졌을 때 조용히 넓어진다.

    비교는 **어휘적**이다(경로 해소를 끼우지 않는다). 해소하면 그 자리가 symlink 로 바뀐 형상에서
    양쪽이 같은 대상으로 접혀 결속 판정이 눈먼다 — 대상 무결성은 chain lstat 과 nofollow 판독이
    따로 본다.
    """
    return Path(cwd) / delegate_ledger._row_ticket_relative_dir(row) / round_rules.round_filename(
        row["ordinal"], row["role"],
    )


def _same_lexical_path(left: Path, right: Path) -> bool:
    """두 절대경로가 같은 자리를 가리키는가 (정규화만 · 해소 없음)."""
    return os.path.normpath(str(left)) == os.path.normpath(str(right))


def _prune_empty_run_dir(ticket_dir: Path) -> None:
    """티켓 자리를 닫은 뒤 **빈** run-dir 을 걷는다 — 마지막 자리가 닫히면 run 도 닫힌다.

    크기 1 묶음이면 자리가 하나라 회수·포기 즉시 run-dir 이 사라진다(종전과 같은 관측).
    다른 티켓의 자리가 남아 있으면 아무것도 하지 않는다. `rmdir` 은 빈 디렉터리에서만
    성공하므로 이 정리가 남의 산출을 지울 수 없다(재귀 삭제 아님).
    """
    parent = Path(ticket_dir).parent
    if not parent.name or parent.name == delegate_ledger.TICKET_COPY_REL_ROOT.name:
        return
    with contextlib.suppress(OSError):
        parent.rmdir()


def _read_slot_round_text(copy_path: Path) -> str:
    """슬롯 라운드 파일을 이식 경계 seam 으로 읽는다 (정규 파일·nofollow·UTF-8).

    경로가 장부 행이 인가한 그것인지는 호출부가 이미 단언했다. 여기서 막는 것은 **그 경로가
    가리키는 대상**이 회수 뒤에 바뀐 형상이다 — 최종 파일의 symlink 와, 사본 루트로 가는 길목의
    디렉터리 교체.
    """
    copy_path = Path(copy_path)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        descriptor = os.open(copy_path, flags)
    except OSError as exc:
        raise delegate_errors.DelegateError(f"티켓 라운드 사본 읽기 실패: {copy_path}: {exc}") from exc
    try:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise delegate_errors.DelegateError(
                f"티켓 라운드 사본이 정규 파일이 아님: {copy_path}"
            )
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read()
    except OSError as exc:
        raise delegate_errors.DelegateError(f"티켓 라운드 사본 읽기 실패: {copy_path}: {exc}") from exc
    finally:
        os.close(descriptor)
    try:
        return raw.decode("utf-8")
    except UnicodeError as exc:
        raise delegate_errors.DelegateError(
            f"티켓 라운드 사본 UTF-8 손상: {copy_path}: {exc}"
        ) from exc


def _assert_slot_run_dir_chain_is_plain(cwd: Path, relative_dir: Path) -> None:
    """티켓 자리까지의 **모든** 마디가 실 디렉터리인지 본다 (symlink 교체 거부).

    시야는 검사 대상 표면과 같아야 한다 — 사본 루트까지만 보면 `<묶음>/<run_id>/<티켓>` 세
    마디가 검사 밖이라, 그 자리를 symlink 로 갈아끼우면 다른 트리의 파일이 회수되고
    `force_rmtree` 가 남의 디렉터리를 지운다. 최종 파일의 nofollow 판독은 그 다음 층이다.
    입력은 장부 행이 인가한 상대 경로 그대로다(`_row_ticket_relative_dir`) — 여기서 규약을
    다시 조립하면 옛 세대 행의 마디를 놓친다.
    """
    current = Path(cwd)
    for part in Path(relative_dir).parts:
        current = current / part
        try:
            observed = current.lstat()
        except OSError as exc:
            raise delegate_errors.DelegateError(f"티켓 라운드 사본 경로 검사 실패: {current}: {exc}") from exc
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise delegate_errors.DelegateError(
                f"티켓 라운드 사본 경로 symlink/비-directory 거부: {current}"
            )


def _board_relative_path(board_path: Path, pm_home: Path) -> str:
    """장부에 싣는 board 상대 경로 — PM 홈 밖으로 해소되면 경계 오류로 표면화한다."""
    try:
        return board_path.resolve().relative_to(pm_home).as_posix()
    except ValueError as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 경로가 PM 홈 밖으로 해소됨: board={board_path} · pm_home={pm_home}"
        ) from exc


# ── 묶음 고정 예산 ────────────────────────────────────────────────────────
# 묶음 하나가 도는 라운드는 정해져 있다: 설계(architect) → 구현(developer · 티켓마다 1) →
# 리뷰(code-reviewer) → fix(developer). 장부 `budget` 의 4키가 그 수열의 **길이**를 값으로
# 말하고, 아래 표가 키↔역할 대응과 순서를 소유한다. 초과나 순서 밖 요청은 티켓을 정지시키며
# 예산 리셋·추가 설계·추가 fix로 다시 열 수 없다.
CLUSTER_BUDGET_ROLE_SEQUENCE: tuple[tuple[str, str], ...] = (
    ("architect", "architect"),
    ("developer", "developer_per_ticket"),
    (review_form.INTERNAL_REVIEW_ROLE, "code-reviewer"),
    ("developer", "fix"),
)
_CLUSTER_BUDGET_OVER = (
    "묶음 고정 라운드 종료 — {cluster} · {ticket} role={role} · 라운드 "
    "{count}건(예산 {limit}건)\n"
    "  고정 수열: {sequence}\n"
    "  · fix가 마지막 사람 라운드입니다. 티켓을 완료하지 못하면 추가 라운드 없이 정지·보고합니다."
)
_CLUSTER_BUDGET_ORDER = (
    "묶음 라운드 순서 밖 역할 — {cluster} · {ticket} 다음 라운드는 {expected} 인데 "
    "{role} 을 요청했습니다\n"
    "  고정 수열: {sequence}\n"
    "  · 순번이 곧 단계입니다 — 단계를 건너뛰거나 되돌리는 경로는 없습니다."
)


_CLUSTER_LEDGER_ABSENT = (
    "묶음 장부가 없습니다: {cluster} — 고정 라운드 수열은 장부가 소유하고, 준비는 "
    "그 선언만 읽습니다(선언 없는 묶음은 판정 입력이 아니라 정지 사유입니다).\n"
    "  · `python3 .project_manager/tools/board.py cluster show {cluster}` 로 장부를 확인하고, "
    "없으면 `board.py cluster new <ticket-group> --tickets <T-...> --task <task>`"
    "(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가) 으로 선언하세요 — 통합 "
    "브랜치를 둘 코드 트리는 명시가 필수입니다."
)
_CLUSTER_BUDGET_UNDECLARED = (
    "묶음 라운드 예산이 선언되지 않았습니다: {cluster} · 장부 `budget` 의 {key} 가 없거나 "
    "정수가 아닙니다\n"
    "  · 예산은 장부를 만들 때 박습니다(`cluster new` · 발행이 만드는 크기 1 장부도 같은 "
    "기본값) — 없는 값을 기본값으로 지어내지 않습니다.\n"
    "  · `python3 .project_manager/tools/board.py cluster show {cluster}` 로 확인하고 장부의 "
    "`budget` 4키를 채운 뒤 다시 실행하세요."
)


def cluster_round_sequence(budget: object, *, cluster: str) -> tuple[str, ...]:
    """예산 4키를 라운드 역할 **수열**로 편다 — 장부 값이 곧 순서다.

    네 값은 모두 정확히 1이어야 한다. 묶음마다 예산을 늘이거나 단계를 생략하면 고정 수열이
    다시 가변 루프가 되므로 장부 손상으로 거부한다.
    """
    if not isinstance(budget, Mapping):
        raise delegate_errors.DelegateError(_CLUSTER_BUDGET_UNDECLARED.format(
            cluster=cluster, key="(없음)"))
    sequence: list[str] = []
    for role, key in CLUSTER_BUDGET_ROLE_SEQUENCE:
        value = budget.get(key)
        if not isinstance(value, int) or isinstance(value, bool):
            raise delegate_errors.DelegateError(_CLUSTER_BUDGET_UNDECLARED.format(
                cluster=cluster, key=key))
        if value != 1:
            raise delegate_errors.DelegateError(
                f"묶음 고정 라운드 예산은 {key}=1이어야 합니다: {cluster} · got={value}"
            )
        sequence.append(role)
    return tuple(sequence)


def _cluster_cycle_roles(
    existing: Sequence, roles: frozenset[str],
) -> tuple[str, ...]:
    """예약된 라운드 중 고정 수열 역할만 — 순번 오름차순.

    산출 유무는 보지 않는다(예약 자체가 한 단계 소비다 · per-ticket 상한과 같은 규칙).
    수열에 없는 역할(추가 리뷰어 채널 등)은 단계가 아니라 주기 자리를 먹지 않는다.
    """
    return tuple(
        item.role for item in sorted(existing, key=lambda entry: entry.ordinal)
        if item.role in roles
    )


def _cluster_budget_refusal(
    *, cluster: str, ticket: str, role: str, existing: Sequence,
    sequence: Sequence[str],
) -> str | None:
    """이 티켓에 그 역할 라운드를 더 예약할 수 있는가 — 거부 사유 문자열 또는 None."""
    cycle = _cluster_cycle_roles(existing, frozenset(sequence))
    rendered = " → ".join(cycle) if cycle else "(없음)"
    if len(cycle) >= len(sequence):
        return _CLUSTER_BUDGET_OVER.format(
            cluster=cluster, ticket=ticket, role=role, count=len(cycle),
            limit=len(sequence), sequence=rendered,
        )
    expected = sequence[len(cycle)]
    if role != expected:
        return _CLUSTER_BUDGET_ORDER.format(
            cluster=cluster, ticket=ticket, role=role, expected=expected,
            sequence=rendered,
        )
    return None


def _reopen_round_path(
    tickets_dir, ticket: str, role: str, *, ordinal: int, lock,
) -> Path:
    """재개방 대상 라운드의 경로 — 판정은 사이드카 seam 하나가 소유한다.

    준비는 이 호출을 두 번 한다(run-dir 만들기 전 사전판정 · board_lock 안 최종). 두 지점이
    같은 규칙을 봐야 흔한 실패가 잔여 없이 끝나고 경합 실패도 같은 문구로 거부된다. 오류형만
    이 층의 것으로 옮긴다 — 준비의 다른 거부와 같은 rc·같은 경로로 나온다.
    """
    try:
        return round_store.reopen_round(
            tickets_dir, ticket, role, ordinal=ordinal, lock=lock,
        )
    except round_rules.RoundsError as exc:
        raise delegate_errors.DelegateError(f"{ticket}: {exc}") from exc


def prepare_ticket_copy(
    *, ticket: str, role: str, cwd: Path, pm_home: Path, owner_pid: int | None = None,
    reopen_ordinal: int | None = None,
) -> TicketCopyPlan:
    """티켓 하나의 라운드를 준비한다 — **크기 1 묶음 호출**의 얇은 래퍼.

    티켓당 경로를 따로 두지 않는다(특례 없음). 이 티켓이 속한 묶음은 명세 frontmatter 가
    말한다 — 필드가 없으면 그 티켓 이름으로 읽히고, 그 이름의 장부가 없으면 준비가 멈춘다
    (장부는 open 진입 세 경로가 만든다 · 부재는 판정 입력이 아니라 정지 사유다).
    반환은 종전대로 라운드 파일 하나의 좌표다.
    """
    home = pm_paths.home_at(Path(pm_home).resolve())
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
    _status, source = found
    try:
        spec_text = fs.read_bytes_shared(source).decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(f"PM 홈 티켓 읽기 실패: {source}: {exc}") from exc
    cluster = cluster_store.ticket_cluster_from_text(ticket, spec_text)
    plan = prepare_cluster_copy(
        cluster=cluster, tickets=(ticket,), role=role, cwd=cwd, pm_home=pm_home,
        owner_pid=owner_pid, reopen_ordinal=reopen_ordinal,
    )
    return plan.rounds[0]


def prepare_cluster_copy(
    *, cluster: str, tickets: Sequence[str] | None = None, role: str, cwd: Path,
    pm_home: Path, owner_pid: int | None = None, reopen_ordinal: int | None = None,
) -> ClusterCopyPlan:
    """board 에 티켓별 라운드 순번을 시드로 예약하고, 같은 파일들을 run-dir 하나에 깐다.

    **run 이 단위다** — 묶음의 티켓 N 개가 run-dir 하나를 공유하고, 그 안에서 티켓마다
    `<티켓>/{NN-<역할>.md, spec.md, rounds/}` 를 갖는다. 쓸 수 있는 자리는 run-dir 전체이고
    (하네스가 여는 범위가 원래 run-dir 통째다) 티켓별 명세·이전 라운드는 읽기 전용 입력이다.
    크기 1 묶음이면 티켓 하나짜리 run-dir 이라 종전 준비와 동작이 같다(별도 코드 경로 없음).

    **거부 판정은 전부 예약 앞에 둔다** — 역할·티켓 조회·상태·ignore 규칙·run-dir·시드 렌더
    (설계 근거 게이트 포함)가 **N 티켓 전부** board 를 건드리기 전에 끝난다.
    뒤에 두면 거부된 준비가 board 에 회수 불가능한 고아 라운드를 남기고 순번을 영구 소모한다.

    순서는 여섯이다. (1) 역할·멤버 해소·티켓별 상태 판정 (2) ignore 검증과
    run-dir 확보(슬롯 전용 부작용) (3) 티켓별 시드 렌더 (4) **상태 재확인 + 역할 count 재확인 +
    채번 + O_EXCL 예약을 하나의 board_lock 임계구역에서 최신 스냅샷으로 수행** (5) 슬롯 파일
    laydown + PM 홈 장부 기록(회수의 신뢰 뿌리 · 티켓당 한 행) (6) 공개된 신규 시드의
    경로 한정 보드 기록. (6)은 모든 사본과 장부가 완성된 뒤 수행한다.

    명세 스냅샷(1)과 최종 예약(4)은 board_lock 안에서 처리한다. 재확인·채번·생성이 같은
    구간에서 끝나므로 두 준비의 동일 순번 예약을 막는다. (6)의 기록 seam은 락 밖에서 부른다. 순번은
    **티켓별**이다 — 묶음 하나의 라운드가 티켓마다 다른 번호를 받을 수 있다.

    `owner_pid` 는 **호출자가 이 run 의 소유자일 때만** 준다 — 준비·실행·회수가 한 프로세스의
    try/finally 인 cross 위임이 그 경우다. 그 값이 장부에 실리면 포기(abandon) 판정이 "이 run 이
    아직 살아 있는가"를 기계 관측할 수 있다. 준비 프로세스가 곧바로 끝나고 실제 writer 가 다른
    세션인 native 위임은 이 값을 **주지 않는다**: 즉사한 준비 pid 를 표식으로 실으면 살아 있는
    run 이 죽은 것으로 읽힌다. 키 부재는 "죽음"이 아니라 "증거 없음"이다.

    묶음 고정 예산은 **모든 준비 표면**에서 같은 판정을 받는다 — 표면마다 켜고 끄는 인자가
    없으므로 티켓 하나짜리 준비가 단계 게이트를 비켜 가는 길도 없다. 판정은 사전판정과
    board_lock 재확인 **두 지점**에 함께 건다 — 한쪽에만 걸면 동시 준비 둘이 같은 예산을
    함께 통과한다.
    장부가 없거나 예산을 선언하지 않았으면 그 요청은 멈춘다(무제한·순서 무검사로 접는 갈래가
    없다).

    `reopen_ordinal` 은 **이어 시키는 단계**의 명시 요청이다 — 그 순번을 채번하지 않고 다시 열어
    (`reopen_round`) 슬롯 사본을 **board 라운드 파일의 현재 내용**으로 시드한다(시드 골격이
    아니다). 재개방은 라운드를 만들지 않으므로 수를 세는 판정(묶음 고정 예산 수열)의 대상이
    아니고, 대신 "그 순번이 있고 역할이 같다"를 예약 seam 이 판정한다.
    요청이 없으면 종전대로 새 순번을 채번한다(암묵 재개방 없음).
    """
    if role not in TICKET_COPY_PREPARE_ROLES:
        raise delegate_errors.DelegateError(f"티켓 라운드 준비 미지원 역할: {role}")
    pm_home = Path(pm_home).resolve()
    home = pm_paths.home_at(pm_home)
    cwd = Path(cwd).resolve()
    # 슬롯 HEAD 를 장부 행에 남기는 역할 — developer(변경 경로 판정의 기준)와 code-reviewer
    # (돌려받기 트리 조건의 기준 · 준비 뒤 HEAD 가 바뀌면 거부한다).
    base_rev = (
        _cluster_git(cwd, "rev-parse", "HEAD").strip()
        if role in _BASE_REV_ROLES else ""
    )
    cluster = str(cluster or "").strip()
    if not delegate_ledger._is_valid_cluster_id(cluster):
        raise delegate_errors.DelegateError(f"클러스터 id 형식이 아닙니다: {cluster!r}")
    members = tuple(tickets) if tickets else cluster_store.cluster_live_members(home, cluster)
    if not members:
        raise delegate_errors.DelegateError(
            f"클러스터 멤버가 없습니다: {cluster} — `board.py cluster show {cluster}` 로 "
            "장부를 확인하세요"
        )

    # ── (1) 티켓별 상태 판정 + 명세 판독 (board 무영향) ─────────────────────
    specs: dict[str, str] = {}
    with fs.board_lock(home):
        for ticket in members:
            found = ticket_store.find_ticket_exact(home, ticket)
            if found is None:
                raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
            status, source = found
            if status not in _TICKET_PREPARE_STATUSES and not (
                status == "draft" and role == "architect"
            ):
                raise delegate_errors.DelegateError(
                    "티켓 라운드 준비는 open/claimed 또는 draft×architect만 허용: "
                    f"{ticket} role={role} currently in {status}/"
                )
            try:
                specs[ticket] = fs.read_bytes_shared(source).decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise delegate_errors.DelegateError(f"PM 홈 티켓 읽기 실패: {source}: {exc}") from exc

    tickets_dir = pm_paths.tickets_dir(home)
    existing_rounds = {
        ticket: round_seed.load_rounds(tickets_dir, ticket, ticket_text=text)
        for ticket, text in specs.items()
    }
    # 묶음 예산 입력 — 장부가 선언한 고정 수열.
    def _budget_inputs() -> tuple[str, ...]:
        """장부에서 고정 수열을 **읽는 자리에서** 해소한다(캐시 없음).

        사전판정과 board_lock 재확인이 각자 읽는다. 장부 판독은 잠금 없는 파일 읽기라
        board_lock 안에서 불러도 재진입이 아니다.

        장부가 없으면 예약하지 않고 멈춘다 — 판정 입력이 없는 요청을 통과시키면 그 묶음만
        무제한·순서 무검사가 되고, 장부 삭제가 곧 예산 우회 수단이 된다.
        """
        ledger = cluster_store.load_cluster(home, cluster)
        if ledger is None:
            raise delegate_errors.DelegateError(_CLUSTER_LEDGER_ABSENT.format(cluster=cluster))
        return cluster_round_sequence(ledger.get("budget"), cluster=cluster)

    budget_sequence: tuple[str, ...] = ()
    budget_sequence = _budget_inputs()

    # ── (1.4) 묶음 예산 사전판정 — 여기도 신뢰 뿌리가 아니다. 빈 수열은 판정을
    # 건너뛰는 값이 아니라 모든 요청을 거부하는 값이다.
    for ticket in members:
        if reopen_ordinal is not None:
            # 재개방은 라운드를 만들지 않는다 — 수열은 예약 수를 세는 판정이라 대상이
            # 아니다(불변식 4). 대신 대상 실재·역할 일치를 여기서 미리 본다: 신뢰 뿌리는 아래
            # (4) 의 board_lock 안 같은 호출이고, 이 자리는 run-dir 을 만들기 전에 흔한 실패를
            # 끝내는 자리다.
            _reopen_round_path(
                tickets_dir, ticket, role,
                ordinal=reopen_ordinal, lock=fs.board_lock(home),
            )
            continue
        refusal = _cluster_budget_refusal(
            cluster=cluster, ticket=ticket, role=role,
            existing=existing_rounds[ticket], sequence=budget_sequence,
        )
        if refusal is not None:
            raise delegate_errors.ClusterRoundBudgetExceeded(refusal)

    # developer 준비의 입력 계약은 실제 prepare 경계에서 검증한다. 순수 시드 renderer는
    # 리뷰/verify 골격 단위 테스트에서도 쓰이므로 architect 라운드에 결합하지 않는다. 고정
    # 수열 판정을 먼저 낸 뒤, 아직 run-dir/board 예약 전인 여기서 누락·미완성
    # architect 계약을 잔여 없이 거부한다.
    if role == "developer":
        for ticket in members:
            try:
                architect_tests_from_rounds(existing_rounds[ticket])
            except delegate_errors.DelegateError as exc:
                raise delegate_errors.DelegateError(f"{ticket}: {exc}") from exc

    # ── (2) 예약 전 거부 판정 (board 무영향) ──────────────────────────────
    run_id = uuid.uuid4().hex
    _assert_ticket_copy_root_ignored(
        cwd, cluster=cluster, run_id=run_id, ticket=members[0],
    )
    run_dir, run_dir_fd = _secure_cluster_run_dir(cwd, cluster, run_id)
    ticket_dirs: dict[str, tuple[Path, int | None, Path, int | None]] = {}
    seeds: dict[str, str] = {}
    next_finding_ids: dict[str, str] = {}
    # 예약 성공분의 복구 좌표 — 티켓마다 `ticket·board_path·ordinal·board_rel`. 예약 직후
    # fallback 값으로 먼저 채우고 더 나은 값으로 교체한다(아래 (4) 주석).
    reserved: list[dict[str, Any]] = []
    plans: list[TicketCopyPlan] = []
    seed_sync_paths: list[Path] = []
    # 예약(reserve_round) 성공 전에 이 블록을 벗어나면(동시 prepare 경합 패자 포함) 이 run_dir
    # 은 이 호출 하나만의 소유라 안전하게 지운다. 성공 후 실패(장부 append 등)는 board 라운드가
    # 이미 살아 있어 `_reserved_round_residue` 진단으로 수동 복구 좌표를 남긴다(지우지 않는다).
    try:
        try:
            for ticket in members:
                ticket_dir, ticket_fd = _secure_ticket_copy_dir(
                    cwd, cluster, run_id, ticket,
                )
                rounds_dir, rounds_fd = _secure_ticket_copy_dir(
                    cwd, cluster, run_id, ticket,
                    extra_parts=(TICKET_COPY_ROUNDS_DIRNAME,),
                )
                ticket_dirs[ticket] = (ticket_dir, ticket_fd, rounds_dir, rounds_fd)

            # ── (3) 티켓별 시드 렌더 — 설계 근거 게이트가 여기서 거부한다(예약 전·잔여 0).
            for ticket in members:
                try:
                    # 재개방의 시드는 골격이 아니라 board 라운드 파일의 현재 내용이라 여기서
                    # 렌더하지 않는다 — 최신 bytes 를 (4) 의 board_lock 안에서 읽어 채운다.
                    if reopen_ordinal is None:
                        seeds[ticket] = _ticket_round_seed(
                            role, specs[ticket],
                            # 프리필 공급원 규칙(같은 역할 직전 라운드 · 산출 없는 라운드
                            # 배제)은 사이드카 seam 하나가 소유한다 — 여기서 다시 구현하면
                            # 예약측·판정측 규칙이 갈린다.
                            round_rules.previous_round_of_role(
                                existing_rounds[ticket], role,
                            ),
                            today=datetime.date.today().isoformat(),
                            # developer 골격의 verify 프리필 입력 — 이미 손에 든 라운드를
                            # 그대로 넘긴다(신규 로드 0).
                            rounds=existing_rounds[ticket],
                        )
                    # 사본 프리앰블이 실을 값 — 시드가 방금 쓴 것과 같은 함수·같은 입력이다
                    # (리뷰 채널이 아닌 역할은 실을 값 자체가 없다).
                    next_finding_ids[ticket] = (
                        review_state.next_review_finding_id(
                            specs[ticket], role, existing_rounds[ticket],
                        )
                        if role in review_form.REVIEW_ROLES else ""
                    )
                except round_rules.RoundsError as exc:
                    # 시드 seam 은 자기 오류형으로 거부한다(board `section-add` 가 잡는 형).
                    # 여기서 되받아 이 CLI 의 오류형으로 옮긴다 — 두 진입점이 같은 규칙·같은
                    # rc 를 낸다. 시드 seam 은 실 ticket id 를 모르므로(ticket_text 만 받는다)
                    # `<T-NNNN>` placeholder 로 처방을 낸다(리뷰 결정) — 이 층은 실값을 쥐고
                    # 있으므로 여기서만 치환해 커맨드가 그대로 실행 가능하게 한다.
                    raise delegate_errors.DelegateError(str(exc).replace("<T-NNNN>", ticket)) from exc

            # ── (4) 여기부터 board 를 건드린다: 상태 재확인·역할 count 재확인·최종 reserve 를
            # 하나의 board_lock 임계구역에서 최신 스냅샷으로 재수행한다 — 위 사전판정과 이
            # 지점 사이의 gap 이 TOCTOU 였다(두 준비가 사전판정을 함께 통과해도 여기서
            # 재확인하는 최신 스캔이 그중 하나만 통과시킨다). 재확인은 **N 티켓 전부**를 먼저
            # 통과시키고 그다음에야 첫 예약을 쓴다(부분 예약 금지).
            board_paths: dict[str, Path] = {}
            with fs.board_lock(home):
                # 재확인은 최신 스냅샷으로 낸다(스냅샷 1회 · 멤버 전부가 같은 장부 판독을 공유한다).
                # 그사이 장부가 사라졌으면 같은 판독이 그 자리에서 멈춘다(예약 없음).
                budget_sequence = _budget_inputs()
                formal_tickets: set[str] = set()
                for ticket in members:
                    refreshed = ticket_store.find_ticket_exact(home, ticket)
                    if refreshed is None:
                        raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
                    fresh_status, _fresh_source = refreshed
                    if fresh_status not in _TICKET_PREPARE_STATUSES and not (
                        fresh_status == "draft" and role == "architect"
                    ):
                        raise delegate_errors.DelegateError(
                            "티켓 라운드 준비는 open/claimed 또는 draft×architect만 허용: "
                            f"{ticket} role={role} currently in {fresh_status}/"
                        )
                    if fresh_status != "draft":
                        formal_tickets.add(ticket)
                    if reopen_ordinal is not None:
                        # 재개방은 순번을 늘리지 않아 수열이 볼 것이 없다 — 이 티켓의
                        # 재확인은 아래 예약 루프의 `reopen_round` 가 최신 스캔으로 낸다.
                        continue
                    fresh_existing = round_seed.load_rounds(
                        tickets_dir, ticket, ticket_text=specs[ticket],
                    )
                    refusal = _cluster_budget_refusal(
                        cluster=cluster, ticket=ticket, role=role,
                        existing=fresh_existing, sequence=budget_sequence,
                    )
                    if refusal is not None:
                        raise delegate_errors.ClusterRoundBudgetExceeded(refusal)
                for ticket in members:
                    # 이 함수가 이미 board_lock 을 쥐고 있다(재진입 금지) — 두 seam 자신의
                    # `with lock:` 은 null 컨텍스트로 채워 판정+생성이 같은 임계구역 안에서 돈다.
                    if reopen_ordinal is None:
                        board_path = round_store.reserve_round(
                            tickets_dir, ticket, role, content=seeds[ticket],
                            lock=contextlib.nullcontext(),
                        )
                    else:
                        board_path = _reopen_round_path(
                            tickets_dir, ticket, role,
                            ordinal=reopen_ordinal, lock=contextlib.nullcontext(),
                        )
                        # 슬롯 시드 = 이 임계구역에서 읽은 board bytes. 회수의 "산출 없음"
                        # 판정이 슬롯과 board 를 직접 대조하므로, 낡은 판독을 깔면 손대지 않은
                        # 사본이 산출로 읽혀 옛 내용을 board 에 다시 쓴다.
                        seeds[ticket] = _read_board_round_text(board_path)
                    board_paths[ticket] = board_path
                    # 반환 즉시, **락 이탈 전** 같은 락 본문 안에서 예약 성공 상태와 복구 좌표를
                    # 기록한다. `exclusive_file_lock` 은 unlock/close 실패를 올리는 계약이라,
                    # 이 기록이 `with` 밖에 있으면 락 이탈 실패가 board 만 남기고 run-dir·좌표를
                    # 지우는 복구 불능 형상을 만든다. 아래 두 계산은 각각 실패 가능한 seam 이라
                    # (`parse_round_filename` 은 None 반환 · `_board_relative_path` 는 PM 홈 밖
                    # 해소를 계약형 오류로 던진다) **던지지 않는 fallback 좌표를 먼저 확정한 뒤**
                    # 더 나은 값으로 교체를 시도한다 — 그래야 복구 진단이 미초기화 값을 참조해
                    # 원 예외를 덮지 않는다.
                    record: dict[str, Any] = {
                        "ticket": ticket, "board_path": board_path,
                        "ordinal": 0, "board_rel": str(board_path),
                    }
                    reserved.append(record)
                    parsed_round = round_rules.parse_round_filename(board_path.name)
                    if parsed_round is not None:
                        record["ordinal"] = parsed_round[0]
                    record["board_rel"] = _board_relative_path(board_path, pm_home)
                    if reopen_ordinal is None and ticket in formal_tickets:
                        seed_sync_paths.append(board_path)

            # ── (5) 슬롯 파일 laydown ─────────────────────────────────────
            for record in reserved:
                ticket = record["ticket"]
                board_rel = record["board_rel"]
                ordinal = record["ordinal"]
                ticket_dir, ticket_fd, rounds_dir, rounds_fd = ticket_dirs[ticket]
                board_path = record["board_path"]
                copy_path = ticket_dir / board_path.name
                try:
                    _write_exclusive_file(
                        copy_path, seeds[ticket].encode("utf-8"), 0o600,
                        parent_fd=ticket_fd,
                    )
                    _write_exclusive_file(
                        ticket_dir / TICKET_COPY_SPEC_NAME,
                        specs[ticket].encode("utf-8"),
                        0o400,
                        parent_fd=ticket_fd,
                    )
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"티켓 라운드 사본 생성 실패: {ticket_dir}: {exc}"
                        + _reserved_round_residue(board_rel, ordinal)
                    ) from exc
                # 이전 라운드는 읽기 전용 입력이다 — 시드 그대로인 미회수 라운드도 함께 깐다
                # (진행 중 다른 역할의 자리를 숨기지 않는다). 재개방한 순번은 **쓰기 사본**이
                # 그 자리라 여기서 빠진다 — 같은 파일을 읽기 전용으로 한 벌 더 깔면 어느 쪽에
                # 쓰는지가 모호해진다.
                try:
                    for item in existing_rounds[ticket]:
                        if item.path.name == board_path.name:
                            continue
                        _write_exclusive_file(
                            rounds_dir / item.path.name,
                            item.text.encode("utf-8"),
                            0o400,
                            parent_fd=rounds_fd,
                        )
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"이전 라운드 사본 생성 실패: {rounds_dir}: {exc}"
                        + _reserved_round_residue(board_rel, ordinal)
                    ) from exc
                plans.append(TicketCopyPlan(
                    copy_path.resolve(), ticket_dir, ticket, role, ordinal, board_path,
                    run_id, next_finding_ids[ticket], cluster, run_dir,
                ))
        finally:
            for _ticket_dir, ticket_fd, _rounds_dir, rounds_fd in ticket_dirs.values():
                if ticket_fd is not None:
                    os.close(ticket_fd)
                if rounds_fd is not None:
                    os.close(rounds_fd)
            if run_dir_fd is not None:
                os.close(run_dir_fd)
    except BaseException as exc:
        # 정리(force_rmtree)가 `KeyboardInterrupt`·`SystemExit`·비-OSError 로 죽어도 이미
        # pending 인 **원** 예외(`exc`)를 대체하지 않는다: 아래는 어떤 예외형이든 잡아 loud
        # 경고만 남기고 마지막 `raise`(bare)는 항상 `exc` 를 그대로 재전파한다.
        if not reserved and os.path.lexists(run_dir):
            try:
                fs.force_rmtree(run_dir)
            except BaseException as cleanup_exc:
                skew = _absorb_engine_rev_skew_for_recovery(
                    cleanup_exc, "ticket_copy_prepare_rollback")
                cause = (
                    f"엔진 사본 불일치 — {cleanup_exc}" if skew
                    else f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
                print(
                    "경고: 라운드 준비 실패(예약 전) 후 run-dir 정리 실패 — 잔류할 수 "
                    f"있습니다: {run_dir}: {cause}",
                    file=sys.stderr,
                )
        elif reserved:
            # board 예약은 이미 성공했지만(예: 락 이탈 실패) 이후 단계가 실패했다. run_dir 은
            # 지우지 않는다(위 분기가 스킵) — 다음 조작자가 board 좌표로 찾을 수 있게 예약된
            # 라운드 전부의 복구 좌표를 loud 하게 남긴다(복구 불능 — board 만 남고 run-dir·
            # 좌표 모두 없음 — 을 만들지 않는다).
            residue = " · ".join(
                f"{record['ticket']}"
                f"{_reserved_round_residue(record['board_rel'], record['ordinal'])}"
                for record in reserved
            )
            print(
                f"경고: board 라운드 예약 후 준비가 실패했습니다({type(exc).__name__}: {exc})"
                f" · run-dir={run_dir} · {residue}",
                file=sys.stderr,
            )
        raise

    for plan, record in zip(plans, reserved):
        ledger_row = {
            "ticket": plan.ticket,
            "role": role,
            "ordinal": plan.ordinal,
            "run_id": run_id,
            "cluster": cluster,
            "copy": str(plan.path),
            "board_rel": record["board_rel"],
            "prepared_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "harvested_at": None,
        }
        if owner_pid is not None:
            ledger_row["owner_pid"] = owner_pid
        if base_rev:
            ledger_row["base_rev"] = base_rev
        try:
            _append_delegate_rounds_ledger(pm_home, ledger_row)
        except delegate_errors.DelegateError as exc:
            # 장부 행이 없으면 이 라운드는 회수 자체가 막힌다 — 남은 좌표를 진단에 싣는다.
            raise delegate_errors.DelegateError(
                f"{exc}; run-dir={run_dir}"
                f"{_reserved_round_residue(ledger_row['board_rel'], plan.ordinal)}"
            ) from exc
    # 모든 사본과 장부 행이 완성된 뒤, board_lock 밖에서 이번 공개 시드만 기록한다.
    # 재개방은 새 시드가 없고 draft는 promote가 공개하므로 이 집합에 들어오지 않는다.
    if seed_sync_paths:
        try:
            ready = round_maintenance._rounds_mutation_sync_paths(home,
                f"ticket-prepare {cluster} {role}", tuple(seed_sync_paths))
        except OSError as exc:
            residue = " · ".join(
                _reserved_round_residue(record["board_rel"], record["ordinal"])
                for record in reserved)
            raise delegate_errors.DelegateError(
                f"준비 완료 후 보드 기록 실패: {exc}; run-dir={run_dir}{residue}"
            ) from exc
        if not board_git._board_git_enabled(home):
            print("ticket prepare: 별도 board-git 없음 (기존 no-op 기록)", file=sys.stderr)
        elif ready:
            print("ticket prepare: 신규 시드 로컬 기록 완료 "
                  "(원격 동기 보류는 board sync 경고로 안내)", file=sys.stderr)
        else:
            print("ticket prepare: 신규 시드 로컬 기록 보류 (local-only/uncommitted)", file=sys.stderr)
    return ClusterCopyPlan(run_dir, cluster, run_id, role, tuple(plans))


def _ticket_commit_message(home, ticket: str) -> str:
    """그 티켓의 커밋 문안 — 명세 제목(없으면 티켓 ID). 조회 실패는 그대로 올린다."""
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        return ticket
    fm, _body = ticket_store.load_ticket(found[1])
    title = str((fm or {}).get("title") or "").strip() if isinstance(fm, dict) else ""
    return title or ticket


def _assert_developer_commit_tree(cwd: Path) -> None:
    """dev 산출을 커밋할 트리가 코드 트리인지 확인한다 — 장부 루트면 거부한다.

    티켓 제목 커밋은 코드 트리에만 만든다. 장부 루트(PM 홈)를 `--cwd` 로 받으면 board·wiki·
    log 변경이 `add -A` 에 함께 실려 그 티켓 제목의 커밋으로 묶인다. 판정 입력은 형상 하나다 —
    소유 PM 홈이 자기 자신이면 그 트리가 곧 PM 홈이고, 그 홈이 슬롯 풀의 공용 bare
    (`.repos/<repo>.git` · 풀을 만든 도구가 써 넣은 자리)를 가지면 코드 트리는 그 풀의 슬롯이지
    이 트리가 아니다. 임베드·단일 clone(풀 없음)은 PM 홈이 곧 코드 트리라 그대로 통과한다.

    자리는 회수의 다른 developer 게이트와 같다(board 교체 앞) — 커밋 직전으로 밀면 거부 시점에
    board 라운드 교체·장부 append·run-dir 삭제가 이미 끝나 같은 자리로 다시 회수할 수 없다.
    """
    if Path(pm_paths.owning_pm_home(cwd)).resolve() != Path(cwd).resolve():
        return                              # 풀 슬롯 — 소유 PM 홈이 따로 있다.
    bares = sorted((Path(cwd) / ".repos").glob("*.git"))
    if not bares:
        return                              # 임베드·단일 clone — PM 홈이 곧 코드 트리다.
    pools = []
    for bare in bares:
        # git 은 Windows 에서도 `/` 경로를 찍는다 — 장부 루트와 같은 절대경로 표기로 싣는다.
        slots = [
            Path(line[len("worktree "):].strip()).resolve()
            for line in _cluster_git(
                bare, "worktree", "list", "--porcelain",
            ).splitlines()
            if line.startswith("worktree ")
        ]
        slots = [slot for slot in slots if slot != bare.resolve()]
        # 슬롯이 하나도 없는 풀은 나열할 값이 없다 — 그 자리에는 나열 대신 슬롯을 만드는
        # 커맨드를 싣는다(빈 나열로 끝나면 문구만 읽고 `--cwd` 를 무엇으로 바꿀지 알 수 없다).
        pools.append(
            (f"{bare} 의 슬롯 = " + " · ".join(map(str, slots))) if slots
            else (
                f"{bare} 에는 아직 슬롯이 없습니다 — `pm-config worktree add {bare.stem} "
                f"--user-ack {bare.stem}` 로 슬롯을 만드세요"
            )
        )
    raise delegate_errors.DelegateError(
        "developer 라운드 회수 거부 — --cwd 가 슬롯 풀을 운영하는 PM 홈입니다"
        f"(장부 루트: {Path(cwd).resolve()}). 티켓 제목 커밋은 코드 트리에만 만듭니다: "
        + " / ".join(pools)
        + ". board 라운드 파일과 slot run-dir 은 그대로 보존했습니다 — `--cwd` 를 그 슬롯으로 "
        "바꿔 같은 회수를 다시 실행하세요."
    )


def _commit_developer_round_output(cwd: Path, *, ticket: str, home) -> str | None:
    """dev 산출을 그 슬롯에서 커밋한다 — 커밋했으면 문안, 커밋할 변경이 없으면 None.

    dev 산출의 커밋 자리는 여기 하나다(손 커밋 0). 리뷰 입력은 묶음 브랜치 tip 이고 리뷰 준비는
    작업 트리 변경 0 을 요구하므로, 회수한 산출이 커밋되지 않으면 리뷰가 서지 않는다. 변경
    관측은 리뷰 준비·돌려받기 트리 조건과 같은 하나다(`_slot_pending_changes`). 실패는 삼키지 않는다: 커밋되지
    않은 산출을 커밋된 것처럼 보고하면 그 라운드가 리뷰에서 통째로 빠진다.
    """
    if not _slot_pending_changes(cwd):
        return None
    message = _ticket_commit_message(home, ticket)
    _cluster_git(cwd, "add", "-A")
    _cluster_git(cwd, "commit", "-m", message)
    return message


def _assert_review_tree_unchanged(cwd: Path, row: Mapping[str, object], *, copy: Path) -> None:
    """리뷰 돌려받기 트리 조건 — 슬롯 HEAD 가 준비 기록 `base_rev` 와 같고 작업 트리 변경이 0 인가.

    리뷰어가 본 트리와 판정할 트리가 같아야 라운드가 증거다. 라운드 파일과 ignore 대상(사본
    루트·테스트 산출)은 작업 트리 변경이 아니다. 어긋나면 board·장부·run-dir 을 바꾸지 않고
    멈춘다. `base_rev` 가 없는 행(이 엔진 이전 준비)은 HEAD 로 대신하지 않는다.
    """
    base_rev = str(row.get("base_rev") or "")
    ticket = row["ticket"]
    ordinal = int(row["ordinal"])
    if not base_rev:
        raise delegate_errors.DelegateError(
            f"리뷰 라운드 돌려받기 거부 — 장부 행에 base_rev 가 없습니다(이 엔진 이전 준비라 "
            f"준비 뒤 슬롯 변경을 판정할 수 없음): {ticket} ordinal={ordinal} · copy={copy}. "
            f"`pm_delegate.py ticket abandon --copy {copy} --cwd {cwd} "
            f"{ABANDON_ASSUME_DEAD_FLAG}`(사본에 산출이 있으면 "
            f"`{ABANDON_DISCARD_REASON_FLAG} <사유>` 추가)로 닫은 뒤, board 에 그 순번 라운드가 "
            f"남아 있으면 `pm_delegate.py ticket prepare --cluster C-<ticket-group> --role "
            f"{review_form.INTERNAL_REVIEW_ROLE} {review_state.PREPARE_REOPEN_ORDINAL_FLAG} {ordinal} --cwd {cwd}` "
            f"(슬롯 사본의 `{review_form.PM_REVIEW_REFUSED_MARKER}` 표식 줄은 지운다), 없으면 "
            f"{review_state.PREPARE_REOPEN_ORDINAL_FLAG} 없이 다시 준비하세요"
        )
    head = _cluster_git(cwd, "rev-parse", "HEAD").strip()
    pending = _slot_pending_changes(cwd)
    if head == base_rev and not pending:
        return
    changes = " · ".join(line.strip() for line in pending) or "없음"
    raise delegate_errors.DelegateError(
        f"리뷰 라운드 돌려받기 거부 — 준비 뒤 슬롯이 바뀌었습니다: {ticket} "
        f"ordinal={ordinal} · 준비 HEAD={base_rev} · 현재 HEAD={head} · 작업 트리 변경: "
        f"{changes}. board 라운드·장부·run-dir 은 그대로입니다. 리뷰어 산출이 아닌 잔여면 "
        f"되돌린 뒤 같은 `--copy` 로 다시 돌려받고, 리뷰가 바뀐 트리를 봤으면 "
        f"`pm_delegate.py ticket abandon --copy {copy} --cwd {cwd} {ABANDON_ASSUME_DEAD_FLAG} "
        f"{ABANDON_DISCARD_REASON_FLAG} <사유>` 로 폐기한 뒤 `ticket prepare --cluster C-<ticket-group> "
        f"--role {review_form.INTERNAL_REVIEW_ROLE} {review_state.PREPARE_REOPEN_ORDINAL_FLAG} {ordinal} --cwd {cwd}` 로 "
        "같은 순번을 다시 여세요"
    )


def harvest_ticket_copy(
    *, copy_path: Path, cwd: Path, pm_home: Path,
) -> TicketHarvestResult:
    """슬롯 라운드 파일로 board 라운드 파일을 원자 교체하고 그 티켓의 자리를 닫는다.

    회수 성공 = 그 티켓 자리 삭제 = 그 자리 닫힘(빈 run-dir 은 함께 걷는다 — 크기 1 이면 곧
    run 닫힘). 재회수 개념이 없고, 닫힌 자리에 다시 부르면 파일이 없어 자연 실패한다. 슬롯
    파일이 시드 그대로면 board 를 바꾸지 않고 경고만 낸다 — 자리는 남겨 같은 세션을 이어 시킬
    수 있게 한다(게이트가 아니다).

    "시드 그대로"는 **board 라운드 파일의 현재 bytes**와의 직접 대조로 판정한다(개행 표기만
    정규화). 그 파일이 이 run 의 예약이 쓴 시드 자신이라 판정 입력이 시점에 의존하지 않는다 —
    시드를 다시 렌더해 비교하면 그 사이 같은 역할의 다른 라운드가 회수될 때 프리필 입력이 바뀌어
    손대지 않은 산출이 "산출 있음"으로 뒤집힌다.
    """
    pm_home = Path(pm_home).resolve()
    cwd = Path(cwd).resolve()
    row = _delegate_round_record(pm_home, copy_path)
    # 한 copy 는 회수 또는 포기 중 **하나로만** 종결된다. 포기된 행의 회수를 열어 두면, 그 순번을
    # 다시 쓴 새 라운드를 옛 run 의 회수가 덮어쓴다(잘못된 포기의 유일한 조용한 손상 경로).
    if "abandoned_at" in row:
        raise delegate_errors.DelegateError(f"이미 포기된 준비는 회수할 수 없습니다: {copy_path}")
    # 장부 행이 인가하는 경로와 **전량 일치**해야 한다 — containment·깊이·순번/역할 결속이
    # 이 단언 하나로 닫힌다.
    expected = _expected_slot_round_path(cwd, row)
    given = Path(copy_path)
    if not given.is_absolute() or not _same_lexical_path(given, expected):
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부가 인가한 라운드 경로와 불일치 — 회수하지 않습니다: "
            f"요청={given} · 장부 인가={expected}"
        )
    _assert_slot_run_dir_chain_is_plain(cwd, delegate_ledger._row_ticket_relative_dir(row))
    text = _read_slot_round_text(expected)
    run_dir = expected.parent
    board_path = pm_home / Path(*PurePosixPath(row["board_rel"]).parts)
    if board_path.name != expected.name:
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부의 board 경로와 라운드 이름 불일치: "
            f"board={board_path.name} · 기대={expected.name}"
        )
    if row["role"] == review_form.INTERNAL_REVIEW_ROLE:
        # 돌려받기 트리 조건 — 시드 대조보다 앞이다. 산출 없음 판정의 조기 반환이 바뀐 슬롯을 가리지 않는다.
        _assert_review_tree_unchanged(cwd, row, copy=expected)

    try:
        reserved = fs.read_text_shared(
            board_path, encoding="utf-8", newline="",
        )
    except FileNotFoundError as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 파일이 없습니다(예약 소실): {board_path}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 파일 읽기 실패: {board_path}: {exc}"
        ) from exc
    if markdown_text._normalized_newlines(text) == markdown_text._normalized_newlines(reserved):
        print(
            "경고: ticket harvest 산출 없음 — 준비가 시드한 라운드 골격이 그대로입니다: "
            f"ticket={row['ticket']} {board_path.name} · run-dir 을 유지합니다: {run_dir}",
            file=sys.stderr,
        )
        return TicketHarvestResult(False, True)

    home = pm_paths.home_at(Path(pm_home).resolve())
    if row["role"] == "architect":
        try:
            parse_architect_tests(text)
        except delegate_errors.DelegateError as exc:
            raise delegate_errors.DelegateError(
                f"architect 라운드 회수 거부 — {exc}. board 라운드 파일과 slot run-dir 은 "
                f"그대로 보존했습니다 — 사본을 고쳐 같은 경로로 다시 회수하세요: "
                f"round={board_path.name} · copy={expected}"
            ) from exc
    if row["role"] in review_form.REVIEW_ROLES:
        # 판정 표면에 올릴 수 없는 리뷰 산출은 board 라운드가 되지 못한다 — 들어가고 나면
        # 티켓 전역 delta 가 막히고 라운드 파일을 되돌릴 정식 수단이 없다(회수면에서 끊는다).
        problem = _review_round_harvest_problem(
            text, ticket=row["ticket"], reviewer_role=row["role"],
            ordinal=int(row["ordinal"]), home=home,
        )
        if problem is not None:
            raise delegate_errors.DelegateError(
                f"리뷰 라운드 회수 거부 — {problem}. board 라운드 파일과 slot run-dir 은 "
                f"그대로 보존했습니다 — 사본을 고쳐 같은 경로로 다시 회수하세요: "
                f"round={board_path.name} · copy={expected}"
            )
    if row["role"] == "developer":
        # 커밋 대상 트리 판정도 같은 자리다 — 이 회수가 만들 티켓 제목 커밋의 트리를 board
        # 교체 앞에서 확정한다.
        _assert_developer_commit_tree(cwd)
        # 검증 게이트는 리뷰 게이트와 **같은 자리**(교체 앞)다 — 교체 뒤로 밀면 낡은 기대값이
        # 이미 board 에 착지한 뒤라 되돌릴 정식 수단이 없다(라운드 파일은 회수 후 불변).
        terminal_fix = _terminal_developer_round(
            ticket=row["ticket"], ordinal=int(row["ordinal"]),
            home=home,
        )
        problem = _developer_round_harvest_problem(
            text, reserved, ticket=row["ticket"], ordinal=int(row["ordinal"]),
            home=home, cwd=cwd,
            base_rev=str(row.get("base_rev") or "HEAD"),
        )
        if problem is not None:
            if terminal_fix:
                raise TerminalFixHarvestError(
                    f"최종 fix 회수 거부 — {problem}. terminal stop: board 라운드 "
                    f"파일과 slot run-dir 증거를 그대로 보존했습니다. 현재 티켓 "
                    f"상태와 실패 근거를 사용자에게 보고하세요: "
                    f"round={board_path.name} · copy={expected}"
                )
            raise delegate_errors.DelegateError(
                f"developer 라운드 회수 거부 — {problem}. board 라운드 파일과 slot run-dir 은 "
                f"그대로 보존했습니다 — 사본을 고쳐 같은 경로로 다시 회수하세요: "
                f"round={board_path.name} · copy={expected}"
            )
    # promote와 같은 락에서 공개 상태 확인·교체를 마친다. draft 회수는 로컬 산출이며,
    # promote가 명세와 충전된 라운드를 함께 기록한다.
    with fs.board_lock(home):
        found = ticket_store.find_ticket_exact(home, row["ticket"])
        if found is None:
            raise delegate_errors.DelegateError(f"회수할 티켓 명세가 없습니다: {row['ticket']}")
        is_draft = found[0] == "draft"
        round_store.replace_round(board_path, text)
    message = f"ticket-harvest {row['ticket']} {row['role']}"
    # board 부분 커밋 seam 은 직접 부른다 — 이름을 더듬어 찾으면 부분 동기된 사본에서 커밋만
    # 조용히 빠진 rc0(라운드는 바뀌었는데 board git 은 그대로)이 된다. 이름이 갈리면
    # AttributeError 로 loud 하게 죽는 편이 그 침묵보다 낫다.
    sync_ready = True
    if not is_draft:
        sync_ready = bool(round_maintenance._rounds_mutation_sync_paths(home, message, (board_path,)))
    harvested = dict(row)
    harvested["harvested_at"] = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat()
    _append_delegate_rounds_ledger(pm_home, harvested)

    # run-dir 은 엔진 소유 임시 산출물이다(사용자 파일 아님) — 삭제가 곧 run 닫힘이다.
    fs.force_rmtree(run_dir)
    _prune_empty_run_dir(run_dir)
    if row["role"] == "developer":
        # 회수가 끝난 **뒤** 커밋한다 — run-dir 이 이미 걷혀 임시 라운드 사본이 커밋에 실리지
        # 않는다. 커밋 문안은 티켓 제목이고, 종결의 커밋 단계는 이 커밋 뒤 "커밋할 변경 없음"
        # 을 관측해 건너뛴다(커밋 자리는 하나다).
        message = _commit_developer_round_output(
            cwd, ticket=row["ticket"], home=home,
        )
        if message is not None:
            print(f"슬롯 커밋: {row['ticket']} — {message} ({cwd})")
    return TicketHarvestResult(True, sync_ready)


class ClusterHarvestOutcome(NamedTuple):
    """묶음 회수 1건의 결과 — 티켓별로 독립이다(교차 원자성 없음).

    `result` 가 있으면 그 티켓의 회수 판정이 끝난 것이고(`changed=False` 는 시드 그대로라
    board 를 안 바꾼 정상 산출), `refusal` 이 있으면 그 티켓만 거부다 — 다른 티켓의 교체를
    되돌리지 않는다(실패한 자리는 장부 행이 복구 좌표를 소유한다).
    """

    ticket: str
    copy: Path
    result: TicketHarvestResult | None
    refusal: str | None
    terminal: bool = False


def _cluster_run_rows(pm_home: Path, run_dir: Path) -> list[dict]:
    """run-dir 이 가리키는 **미처분** 장부 행들 — `(묶음, run_id)` 로 해소한다.

    디렉터리 인자는 행을 찾는 **입력 형식**일 뿐이고 인가는 여전히 행이 한다 — 행마다 기존
    단일-경로 전량 일치 단언을 다시 통과시킨다(판정식 완화 0). 이미 회수·포기된 행은 빠진다
    (재회수 개념이 없다).
    """
    run_dir = Path(run_dir).resolve()
    run_id = run_dir.name
    cluster = run_dir.parent.name
    latest: dict[str, dict] = {}
    for row in delegate_ledger._delegate_rounds_ledger_records(pm_home):
        if row.get("run_id") == run_id and str(row.get("cluster") or "") == cluster:
            latest[row["copy"]] = row
    rows = [
        row for row in latest.values()
        if row["harvested_at"] is None and "abandoned_at" not in row
    ]
    if not rows:
        raise delegate_errors.DelegateError(
            f"delegate-rounds 장부에 이 run 의 미회수 준비가 없습니다: {run_dir} — "
            "준비하지 않았거나 이미 처분된 run 입니다"
        )
    return sorted(rows, key=lambda row: (row["ticket"], row["ordinal"]))


def harvest_cluster_copy(
    *, run_dir: Path, cwd: Path, pm_home: Path,
) -> tuple[ClusterHarvestOutcome, ...]:
    """run-dir 하나에 깔린 라운드 파일들을 **티켓별로** 회수한다.

    회수는 티켓별 독립이다 — 파일마다 교체·경고·거부를 따로 내고 호출부가 요약 1줄로 집계한다.
    N 파일을 한꺼번에 되돌리는 교차 원자성은 없다(있는 척하면 실패한 자리의 산출을 잃는다).
    각 행은 단일 경로 회수(`harvest_ticket_copy`)를 그대로 통과하므로 판정식이 한 벌뿐이고,
    크기 1 묶음이면 이 함수는 그 한 번의 회수와 같다.
    """
    cwd = Path(cwd).resolve()
    pm_home = Path(pm_home).resolve()
    outcomes: list[ClusterHarvestOutcome] = []
    for row in _cluster_run_rows(pm_home, run_dir):
        expected = _expected_slot_round_path(cwd, row)
        try:
            result = harvest_ticket_copy(
                copy_path=expected, cwd=cwd, pm_home=pm_home,
            )
        except delegate_errors.DelegateError as exc:
            outcomes.append(
                ClusterHarvestOutcome(
                    row["ticket"], expected, None, str(exc),
                    isinstance(exc, TerminalFixHarvestError),
                ))
            continue
        outcomes.append(ClusterHarvestOutcome(row["ticket"], expected, result, None))
    return tuple(outcomes)


def _review_round_harvest_problem(
    text: str, *, ticket: str, reviewer_role: str, ordinal: int, home,
) -> str | None:
    """내부 채널 회수 직전 리뷰 라운드 내용 판정 — 위반 사유 또는 None.

    판정 자체는 두 채널 공용(`review_harvest_problem`)이고 이 함수가 하는 일은 그 판정의 입력
    (명세·라운드 목록)을 PM 홈 board 좌표에서 읽어 오는 것뿐이다. 추가 리뷰어 채널은 같은 입력을
    회수 직전에 읽는다 — 두 채널이 같은 스냅샷 규칙을 쓴다.

    입력에서 **회수 대상 라운드 자신은 뺀다**. 그 자리는 장부 행의 `(역할, 순번)` 좌표가 이미
    가리키고 있다. 새로 예약한 라운드는 시드 그대로라 `pending` 배제에 걸리지만, 재개방
    (`--reopen-ordinal`)한 라운드는 직전 회수 산출로 채워져 있어 `pending` 도 엔진 표식도
    아니다 — 좌표로 빼지 않으면 그 라운드가 앞서 선언한 ID 가 자기 자신과 충돌해 이어 시키는
    회수가 통째로 막힌다. 다른 라운드가 이미 쓴 ID 의 재선언은 종전대로 거부다.

    입력을 읽지 못하면 통과가 아니라 거부다. 판정 불능인 채로 회수하면 그 라운드가 판정 표면에
    올라 티켓 전체를 막을 수 있고, 거부는 산출을 파괴하지 않으므로(run-dir 유지) 되돌릴 수 있다.
    """
    try:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            return f"티켓 명세를 찾지 못해 회수 판정을 낼 수 없습니다: {ticket}"
        _status, spec_path = found
        spec_text = fs.read_text_shared(
            spec_path, encoding="utf-8", newline="",
        )
        rounds = round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
        )
    except (delegate_errors.DelegateError, round_rules.RoundsError, OSError, UnicodeError) as exc:
        return f"회수 판정 입력을 읽지 못했습니다: {ticket}: {exc}"
    corpus = tuple(
        item for item in rounds
        if not (item.role == reviewer_role and item.ordinal == ordinal)
    )
    return review_harvest_problem(
        text, ticket_text=spec_text, rounds=corpus, reviewer_role=reviewer_role,
    )


def _run_required_test(command: str, expected: str | None, *, cwd: Path) -> str | None:
    """필수 테스트 한 건을 실행하고 실패 사유만 반환한다."""
    if expected is None:
        try:
            review_form._pm_review_assert_verify_command_shape(command, "full-regression.command")
            result = subprocess.run(
                shlex.split(command), cwd=str(cwd), capture_output=True, text=True,
                encoding="utf-8", errors="replace",
                timeout=PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC, check=False,
            )
        except (review_form.PMReviewError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return f"실행할 수 없습니다: {exc}"
        if result.returncode != 0:
            observed = (result.stdout or "") + (result.stderr or "")
            return f"green이 아닙니다: `{command}` · rc={result.returncode} · {observed[-2000:]}"
        return None
    try:
        status, observed = run_pm_review_confirmation_command(
            command, cwd=cwd, expected=expected,
        )
    except (review_form.PMReviewError, ValueError) as exc:
        return f"실행할 수 없습니다: {exc}"
    if status != review_form.PM_REVIEW_CONFIRMATION_RESOLVED:
        return f"green이 아닙니다: `{command}` · expected={expected!r} · 관측: {observed}"
    return None


def _full_regression_command(cwd: Path) -> str:
    """developer 단계 종료의 프로젝트 test_cmd(areas/slot/local/default 해소 전부)."""
    repo = Path(cwd).resolve()
    config_root = Path(pm_paths.resolve_pm_home_for_repo(repo)).resolve()
    command = str(regression._test_cmd(pm_paths.home_at(config_root), None, session=None)).strip()
    if not command:
        raise delegate_errors.DelegateError("developer 단계 종료 전체 회귀 test_cmd를 해소하지 못했습니다")
    return command


class DeveloperRegressionRecord(NamedTuple):
    command: str
    result: str


_DEVELOPER_REGRESSION_HEADING = "## 회귀"
_DEVELOPER_REGRESSION_COMMAND_PREFIX = "- 커맨드: `"
_DEVELOPER_REGRESSION_RESULT_PREFIX = "- 결과: "
_DEVELOPER_REGRESSION_GREEN_RE = re.compile(r"(?:^|[ ·])rc\s*=\s*0(?:$|[ ·])")


def parse_developer_regression_record(text: str) -> DeveloperRegressionRecord:
    """developer가 직접 실행해 라운드에 남긴 stage-exit 전체 회귀 기록."""
    lines = markdown_text._normalized_newlines(text).splitlines()
    headings = [index for index, line in enumerate(lines) if line == _DEVELOPER_REGRESSION_HEADING]
    if len(headings) != 1:
        raise delegate_errors.DelegateError("developer 전체 회귀 기록은 `## 회귀` 절 정확히 1개여야 합니다")
    start = headings[0]
    section_end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    section = [line for line in lines[start + 1:section_end] if line.strip()]
    if len(section) != 2:
        raise delegate_errors.DelegateError(
            "developer 전체 회귀 기록은 커맨드·결과 두 행이어야 합니다"
        )
    command_line, result_line = section
    if not (
        command_line.startswith(_DEVELOPER_REGRESSION_COMMAND_PREFIX)
        and command_line.endswith("`")
    ):
        raise delegate_errors.DelegateError("developer 전체 회귀 커맨드 기록 형식이 올바르지 않습니다")
    if not result_line.startswith(_DEVELOPER_REGRESSION_RESULT_PREFIX):
        raise delegate_errors.DelegateError("developer 전체 회귀 결과 기록 형식이 올바르지 않습니다")
    command = command_line[len(_DEVELOPER_REGRESSION_COMMAND_PREFIX):-1].strip()
    result = result_line[len(_DEVELOPER_REGRESSION_RESULT_PREFIX):].strip()
    for value, field in ((command, "커맨드"), (result, "결과")):
        if not value or review_form._CONTRACT_PLACEHOLDER_RE.search(value):
            raise delegate_errors.DelegateError(
                f"developer 전체 회귀 {field}를 placeholder가 아닌 실값으로 기록해야 합니다"
            )
    if not _DEVELOPER_REGRESSION_GREEN_RE.search(result):
        raise delegate_errors.DelegateError(
            "developer 전체 회귀 결과가 green이 아닙니다 — `rc=0` 관측을 기록해야 합니다"
        )
    return DeveloperRegressionRecord(command, result)


def _developer_round_changed_paths(
    repo_root: Path, *, base_rev: str,
) -> frozenset[str]:
    """직전 developer 커밋 후 현재 라운드가 바꾼 경로.

    developer 산출은 harvest 성공 뒤에만 커밋되므로 ``HEAD..작업트리``가
    해당 단계의 자연스런 기준선이다. 존재 이유가 사라진 코드·테스트를 지우는 것도
    그 라운드의 산출이라 같이 세고, untracked 테스트는 별도로 포함한다.

    삭제(``D``)를 세는 이 집합은 **두 축이 공유한다** — reviewer fix 계약 대상 검사와
    architect 필수 테스트 대상 검사가 같은 값을 본다. 그래서 architect 계약이 지목한 테스트
    파일을 **지운** 라운드도 대상 검사만으로는 통과한다. 그 자리를 인자로 가르지 않는 이유는
    바로 뒤에서 그 계약의 command 를 실제로 실행하기 때문이다 — 지목된 파일이 없으면 명령
    자체가 red 라 같은 라운드가 그 자리에서 멈춘다. 축을 가르는 인자는 이미 실행이 내는 답을
    한 번 더 재는 장치다.
    """
    changed = {
        line.strip()
        for line in _cluster_git(
            repo_root, "diff", "--name-only", "--diff-filter=ACMRTUXBD",
            base_rev, "--",
        ).splitlines()
        if line.strip()
    }
    changed.update(
        line.strip()
        for line in _cluster_git(
            repo_root, "ls-files", "--others", "--exclude-standard", "--",
        ).splitlines()
        if line.strip()
    )
    return frozenset(changed)


def _fix_round(rounds: Sequence, ordinal: int) -> bool:
    """현재 developer 자리가 reviewer 뒤의 마지막 fix 자리인가."""
    return any(
        item.role == review_form.INTERNAL_REVIEW_ROLE and item.ordinal < ordinal
        and not getattr(item, "pending", False)
        for item in rounds
    )


def _terminal_developer_round(
    *, ticket: str, ordinal: int, home,
) -> bool:
    """회수 거부 문구가 사람을 다시 투입해도 되는지 판정하는 단일 seam."""
    try:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            return False
        _status, spec_path = found
        spec_text = fs.read_text_shared(
            spec_path, encoding="utf-8", newline="",
        )
        rounds = round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
        )
    except (round_rules.RoundsError, OSError, UnicodeError):
        return False
    return _fix_round(rounds, ordinal)


def _developer_round_harvest_problem(
    text: str, reserved: str, *, ticket: str, ordinal: int, home,
    cwd: Path, base_rev: str = "HEAD",
) -> str | None:
    """developer 라운드 회수 직전 verify 판정 — 위반 사유 또는 None(리뷰 게이트와 같은 자리).

    세 가지를 본다.

    (1) **지운 행**: 시드가 실은 verify 행이 산출에 그대로 있는가. 기준선은 판정 시점에 다시
    계산한 분류기 산출이 아니라 **그 run 의 예약 bytes** 다 — 그 사이 같은 티켓의 다른 라운드가
    회수되면 프리필 입력이 바뀌어, 손대지 않은 행이 "지워졌다"로 뒤집힌다(무편집 판정과 같은 규율).
    지운 행을 통과시키면 시드가 열린 accepted 전건을 싣는 의미가 사라진다 — 개발자가 이번 라운드에
    보지 않은 finding 을 산출에서 빼는 것으로 범위를 스스로 좁히게 된다.

    (2) **선언 검증**: 이번 라운드가 선언한 `machine_verifiable=true` 행의 커맨드를 실제로 돌려
    기대값이 관측되는가. 실행 대상은 이번 라운드의 선언뿐이라(티켓 전역 최신 행이 아니다) 실행
    시간이 티켓 수명에 비례하지 않는다. 설계 축 finding 은 기계 확인 대상이 아니므로 여기서도
    돌리지 않는다(확인 파서와 같은 규칙). 관측이 기대와 다르면 거부다 — 낡은 기대값을 board
    라운드에 착지시키면 PM 의 기계 확인이 그 값으로 막히고, 라운드 파일은 회수 뒤 불변이라
    되돌릴 정식 수단이 없다.

    (3) **stage-exit 전체 회귀 기록**: developer가 local.conf `test.cmd`를 직접 실행한 정확한
    커맨드와 ``rc=0`` 결과가 `## 회귀` 절에 있는가. harvest가 full을 다시 실행하면 inner-loop
    탐색에 전체 회귀를 반복하는 경로가 되므로 최초 developer와 final fix 모두 기록을 엄격 검증하고
    이 경계에서는 targeted architect/reviewer 계약만 재실행한다. final fix developer가 terminal
    전체 회귀를 직접 실행하며, 기록 누락·명령 불일치·nonzero 또는 계약 red는 terminal stop이다.

    이 관측은 **확인 증거가 아니다**. 회수는 슬롯 트리(개발자가 고친 브랜치)에서 재고, PM 의 기계
    확인은 묶음 통합 브랜치를 강제한다 — 측정 트리가 다르므로 두 관측을 섞지 않는다.

    입력을 읽지 못하면 통과가 아니라 거부다(리뷰 게이트와 같은 이유 · run-dir 보존이라 되돌릴 수
    있다).
    """
    try:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            return f"티켓 명세를 찾지 못해 회수 판정을 낼 수 없습니다: {ticket}"
        _status, spec_path = found
        spec_text = fs.read_text_shared(
            spec_path, encoding="utf-8", newline="",
        )
        rounds = round_seed.load_rounds(
            pm_paths.tickets_dir(home), ticket, ticket_text=spec_text,
        )
    except (round_rules.RoundsError, OSError, UnicodeError) as exc:
        return f"회수 판정 입력을 읽지 못했습니다: {ticket}: {exc}"

    repo_root = pm_paths._repo_root_for_cwd(cwd)
    try:
        architect_tests = architect_tests_from_rounds(rounds)
    except delegate_errors.DelegateError as exc:
        return str(exc)
    is_fix = _fix_round(rounds, ordinal)
    try:
        changed_paths = _developer_round_changed_paths(repo_root, base_rev=base_rev)
    except delegate_errors.DelegateError as exc:
        return f"developer 단계 diff를 읽지 못했습니다: {exc}"

    # accepted reviewer 계약과 결속하기 전에 **이번 산출의 선언**을 먼저 확정한다. PM-owned
    # 예외는 disposition 산문만으로 열리지 않고 같은 ID의 채워진 false verify 행까지 요구한다.
    seed_rows = round_seed._dev_round_seed_verify_rows(markdown_text._normalized_newlines(reserved))
    if seed_rows is None:
        return (
            f"예약 골격의 {review_form.PM_REVIEW_VERIFY_BLOCK} 블록을 읽지 못해 회수 판정을 낼 수 "
            f"없습니다: ordinal={ordinal}"
        )
    filled: dict[str, review_form.PMReviewVerifyRow] = {}
    unfilled: tuple[str, ...] = ()
    if seed_rows:
        try:
            filled, unfilled = review_state._pm_review_verify_round_declarations(
                review_state._RoundView("developer", ordinal, text),
            )
        except review_form.PMReviewError as exc:
            return f"{review_form.PM_REVIEW_VERIFY_BLOCK} 블록을 읽지 못했습니다[{exc.code}]: {exc}"
        declared = set(filled) | set(unfilled)
        deleted = [
            finding_id for finding_id, _values in seed_rows
            if finding_id not in declared
        ]
        if deleted:
            return (
                f"시드가 실은 verify 행을 지웠습니다: {', '.join(deleted)} — 행은 지우지 말고 값만 "
                "갱신하세요(이번 라운드에 구현하지 않은 finding 은 machine_verifiable=false · "
                f"reason={review_form.PM_REVIEW_VERIFY_GAP_REASON} 로 선언합니다)"
            )
    if not is_fix:
        for case in architect_tests:
            try:
                targets = _contract_test_targets(
                    case.target, f"architect 필수 테스트 {case.id}.target",
                )
            except delegate_errors.DelegateError as exc:
                return str(exc)
            missing = [target for target in targets if target not in changed_paths]
            if missing:
                return (
                    f"architect 필수 테스트 {case.id} 대상이 이 developer diff에 "
                    f"추가·수정되지 않았습니다: {', '.join(missing)}"
                )
    for case in architect_tests:
        problem = _run_required_test(case.command, case.expected, cwd=repo_root)
        if problem is not None:
            return f"architect 필수 테스트 {case.id} {problem}"

    delta: review_form.PMReviewDelta | None = None
    if is_fix:
        try:
            delta = review_state.parse_pm_review_delta(spec_text, rounds)
        except review_form.PMReviewError as exc:
            return f"fix 입력 리뷰 delta를 읽지 못했습니다[{exc.code}]: {exc}"
        try:
            pm_owned_ids = review_form._pm_review_pm_owned_contract_ids(delta, filled)
        except delegate_errors.DelegateError as exc:
            return str(exc)
        for finding, _disposition in delta.accepted:
            if finding.fix_contract is None:
                return (
                    f"fix 입력 {finding.id}에 reviewer 수정·테스트 계약이 없습니다 — "
                    "코드 위치·오류 거동·수정 설계·회귀 테스트·명령·기대값이 모두 필요합니다"
                )
            if finding.id in pm_owned_ids:
                continue
            try:
                test_kind, targets = _reviewer_fix_test_requirement(
                    finding.fix_contract["test"],
                    f"reviewer 추가 회귀 {finding.id}.test",
                )
                location_paths = _contract_repo_paths(
                    finding.fix_contract["location"],
                    f"reviewer 수정 계약 {finding.id}.location",
                )
            except delegate_errors.DelegateError as exc:
                return str(exc)
            if test_kind == "existing" and not location_paths:
                return f"reviewer 기존 시험 {finding.id}은 location에 repo-relative 수정 경로가 필요합니다"
            if not targets and not location_paths:
                return (
                    f"reviewer 수정 계약 {finding.id}이 이 저장소의 경로를 하나도 "
                    "지목하지 않았습니다 — test 의 repo-relative 테스트 대상"
                    "(tests/*.py)이나 location 의 repo-relative 파일 경로가 필요합니다"
                )
            # 시험 변경 계약은 시험 파일의 변경을 요구한다. 기존 시험 재실행과 시험
            # 대상이 없는 지적은 location의 수정 경로 중 하나가 이번 diff에 있어야 한다.
            if test_kind == "changed":
                missing = [target for target in targets if target not in changed_paths]
            else:
                missing = (
                    [] if changed_paths.intersection(location_paths)
                    else list(location_paths)
                )
            if missing:
                return (
                    f"reviewer 추가 회귀 {finding.id} 대상이 이 fix diff에 "
                    f"추가·수정되지 않았습니다: {', '.join(missing)}"
                )
            if test_kind == "existing":
                for target in targets:
                    try:
                        observed = (repo_root / target).stat()
                    except FileNotFoundError:
                        return f"reviewer 기존 시험 {finding.id} 파일이 없습니다: {target}"
                    if not stat.S_ISREG(observed.st_mode):
                        return f"reviewer 기존 시험 {finding.id} 정규 파일이 아닙니다: {target}"
            problem = _run_required_test(
                finding.fix_contract["command"], finding.fix_contract["expected"],
                cwd=repo_root,
            )
            if problem is not None:
                return f"reviewer 추가 회귀 {finding.id} {problem}"
    full_command = _full_regression_command(repo_root)
    try:
        regression = parse_developer_regression_record(text)
    except delegate_errors.DelegateError as exc:
        return str(exc)
    if regression.command != full_command:
        return (
            "developer 전체 회귀 커맨드가 stage-exit 명령과 다릅니다: "
            f"기록={regression.command!r} · 기대={full_command!r}"
        )
    if not seed_rows:
        return None
    declared_rows = [row for row in filled.values() if row.machine_verifiable]
    if not declared_rows:
        return None
    try:
        delta = delta or review_state.parse_pm_review_delta(spec_text, rounds)
    except (
        delegate_errors.DelegateError, review_form.PMReviewError, round_rules.RoundsError, OSError, UnicodeError,
    ) as exc:
        return f"회수 판정 입력을 읽지 못했습니다: {ticket}: {exc}"
    machine_axis_ids = {
        finding.id for finding, _disposition in delta.accepted
    }
    for row in declared_rows:
        if row.id not in machine_axis_ids:
            continue
        try:
            status, observed = run_pm_review_confirmation_command(
                row.command, cwd=repo_root, expected=row.expected,
            )
        except (review_form.PMReviewError, ValueError) as exc:
            return f"verify {row.id} 재현 커맨드를 실행하지 못했습니다: {exc}"
        if status != "resolved":
            return (
                f"verify {row.id} 재현 커맨드의 관측이 기대와 다릅니다: `{row.command}` "
                f"(cwd={repo_root}) · expected={row.expected!r} · 관측: {observed}"
            )
    return None


def _abandon_raw_ledger_hint(pm_home: Path, row: dict) -> str:
    """거부 문구에 붙이는 **비-권위 참고** — 같은 (ticket, role) 의 미마감 raw 레코드.

    판정 술어에는 넣지 않는다. cross 실위임 raw 행에는 `run_id`·`copy` 가 실려 이 예약과
    문자열로 결속되지만, 이 참고는 그 결속을 판정에 쓰지 않고 여전히 (ticket, role) 휴리스틱
    조인만 쓴다 — 판정식 교체는 이 함수 소유가 아니다. 하네스 subprocess 를 띄우지 않는 native
    위임은 raw 행 자체를 만들지 않는다(결속 대상 없음). 휴리스틱을 결정 입력으로 쓰면 증거 없는
    예약이 조용히 "증거 있음"으로 뒤바뀐다. 참고를 읽지 못하면 참고만 빠진다(판정은 그대로다).
    """
    try:
        _raw_dir, ledger_path = raw_output.raw_storage_paths(pm_home, "delegate")
        rows = raw_output.raw_records(ledger_path, unfinished_only=True, lock=False)
    except (OSError, ValueError, UnicodeError):
        return ""
    matches = [
        item for item in rows
        if item.get("ticket") == row["ticket"] and item.get("role") == row["role"]
    ]
    if not matches:
        return ""
    latest = matches[0]
    pid = latest.get("pid")
    liveness = "생존" if raw_output.pid_is_alive(pid) else "사망"
    return (
        " · 참고(판정 입력 아님): 같은 티켓·역할의 미마감 raw 레코드가 있습니다 — "
        f"id={latest.get('id')} pid={pid}({liveness})"
    )


def _read_board_round_text(board_path: Path) -> str:
    """board 라운드 파일의 현재 bytes — 개행 표기를 보존해 읽는다(대조·기준선 단일 판독).

    부재는 여기서 답하지 않는다 — 호출부가 부재를 먼저 관측해 그 자리마다 다른 결론(선언을
    받아 종결 / 예약 소실 거부)을 낸다.
    """
    try:
        return fs.read_text_shared(
            board_path, encoding="utf-8", newline="",
        )
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(
            f"board 라운드 파일 읽기 실패: {board_path}: {exc}"
        ) from exc


def _abandon_liveness_problem(
    row: dict, *, copy_path: Path, pm_home: Path,
) -> str | None:
    """종료 증거 판정 — 거부 사유(통과면 None). **파괴 연산의 기본값은 거부다.**

    기계가 증명할 수 있는 것만 기계가 막는다: 행에 `owner_pid` 가 있고 그 프로세스가 살아 있으면
    run 은 진행 중이다. 표식이 없는 행(기존 준비 전부·native 위임)은 증명 수단 자체가 없으므로
    통과시키지 않고 운영자 명시 확인을 요구한다 — 거부도 무조건 허용도 아닌 제3 선택지다.

    거부 문구는 좌표를 값으로 찍는다. 매번 "무엇을 지우는지" 를 보게 하는 것이 명시 확인 플래그가
    습관이 되는 것을 막는 유일한 수단이다.
    """
    coordinates = (
        f"ticket={row['ticket']} · role={row['role']} · ordinal={row['ordinal']} · "
        f"prepared_at={row['prepared_at']} · copy={copy_path}"
    )
    hint = _abandon_raw_ledger_hint(pm_home, row)
    owner_pid = row.get("owner_pid")
    if owner_pid is None:
        return (
            "이 예약에는 종료 증거가 없습니다(장부 행에 owner_pid 없음) — run 이 끝났음을 "
            f"확인했다면 {ABANDON_ASSUME_DEAD_FLAG} 로 다시 부르세요: {coordinates}{hint}"
        )
    if raw_output.pid_is_alive(owner_pid):
        return (
            f"준비를 소유한 프로세스가 실행 중이라 포기하지 않습니다 — pid={owner_pid} "
            f"(`ps -p {owner_pid}` 로 확인) · 의도한 우회는 {ABANDON_ASSUME_DEAD_FLAG}: "
            f"{coordinates}{hint}"
        )
    return None


def abandon_ticket_copy(
    *, copy_path: Path, cwd: Path, pm_home: Path, assume_dead: bool = False,
    superseded_by_ordinal: int | None = None, discard_reason: str | None = None,
) -> TicketAbandonResult:
    """kill 로 죽어 이어 갈 수 없는 준비를 명시적으로 정리한다 — 장부 행·board 라운드·run-dir.

    인가 경로는 회수(harvest)와 같다(같은 신뢰 뿌리 · 같은 경로 전량 일치 · 같은 run-dir chain
    plain 판정). 그 위에 이 처분만의 규칙 셋이 있다.

    1. **종료 증거**(`_abandon_liveness_problem`) — 살아 있는 run 을 지우지 않는다.
    2. **시드 그대로** — 슬롯 사본 bytes 와 board 라운드 파일의 현재 bytes 직접 대조(개행 표기만
       정규화). 산출이 있으면 harvest 로 회수할 대상이지 지울 대상이 아니다. **예외는 운영자의
       선언 둘**이고 서로 배타다: `superseded_by_ordinal` 은 "이 라운드는 재실행된 그 라운드로
       대체됐다"(finding ID 재선언 등으로 harvest 도 거부하는 재실행 대체본 한정),
       `discard_reason` 은 "이 라운드는 폐기한다"다. 선언이 하나도 없으면 여전히 거부다 —
       선언 없는 처분은 조용한 파괴다. 값 형식(대체본: 자기 자신 아님·1 이상 · 폐기: 한 줄
       비지 않은 사유)과 실재(그 ordinal 의 라운드가 이 티켓에 있음) 검증은 인자를 받는
       즉시(시드 대조와 무관하게) 끝낸다.
    3. **최대 순번일 때만 board 파일 삭제** — 중간 순번을 지우면 `round-gap`(완료 게이트 red)을
       만든다. 중간 순번이면 board 파일을 **보존**하고 장부 행과 run-dir 만 종결한다. 거부가
       아니라 분기라서 프로토콜이 항상 수렴한다. 보존한 파일에는 엔진 표식 줄을 발행한다 —
       그 라운드는 영원히 시드 그대로라 표식이 없으면 `round-pending` 과 판정 표면에 자리표시
       골격째로 남는다(발행은 파괴 판정 기준선을 읽은 **뒤**다). **폐기 선언은 순번과 무관하게
       보존**이고, 보존한 파일에 관측한 슬롯 산출을 그대로 옮긴 뒤 같은 표식을 붙인다 — 슬롯
       사본이 그 산출의 유일본이라 지우기 전에 board 로 옮기지 않으면 산출이 사라진다.
    4. **지울 자산이 둘 다 없는 행**(슬롯 사본도 board 라운드 파일도 없다) — 폐기 선언을 받아
       장부 행만 닫는다. 자산 부재는 "판정 불능"이 아니라 그 단계가 끝난 상태다. 사본만 없고
       board 가 살아 있으면 지울 대상이 있는데 기준선을 못 읽는 상태라 종전대로 거부다.

    순서는 되돌릴 수 없는 삭제를 마지막에 두고 그 앞을 전부 멱등 기록으로 만든다 — journal 없이
    재개가 성립한다. (1) 인가 + 선언 배타·값·실재 검증 (2) 종료 판단·시드 대조 → 선언 분기가
    실제로 발화했을 때만(값이 그냥 주어졌다는 사실만으로는 아님) 생존 게이트 통과 뒤 loud +
    `abandoned_at` 마감 행 append 에 `superseded_by_ordinal`·`discard_reason` 동반 (3)
    `board_lock` 안에서 최대 순번 재확인 + 최대면 unlink(폐기는 보존) (4) 락 밖 산출 이관·표식
    발행·sync (5) 슬롯 파일 재판독 후 시드(또는 **장부에 기록된** 선언이 있으면 이번 호출이
    관측한 산출) 그대로일 때만 run-dir 삭제 — 재호출이 인자를 다시 줘도 장부에 기록되지 않은
    값은 이 판정에 쓰지 않는다 (6) 세 자산 재판독 수렴 단언. **재호출은 rollback 이 아니라 남은
    작업의 완료**다 — 마감 행이 이미 있는 행은 거부하지 않고 2 를 건너뛰고, 선언 여부는 장부
    행에서 다시 읽는다(재호출은 선언 인자를 다시 주지 않아도 된다).

    `board_lock` 안에는 순번 판정과 unlink 만 둔다. `_rounds_mutation_sync_paths` 는
    `refresh_board` → `board_lock` 재진입이고 전역 락 순서(board_git_lock → board_lock) 역전이라
    락 안에서 부르면 교착한다.
    """
    pm_home = Path(pm_home).resolve()
    cwd = Path(cwd).resolve()
    # 선언이 둘이면 장부에 어느 것이 실릴지 모호하다 — 상태를 읽기 전에 거부한다.
    if superseded_by_ordinal is not None and discard_reason is not None:
        raise delegate_errors.DelegateError(
            f"처분 선언은 하나여야 합니다 — {ABANDON_SUPERSEDED_BY_FLAG}(재실행 대체)와 "
            f"{ABANDON_DISCARD_REASON_FLAG}(폐기)를 함께 줄 수 없습니다"
        )
    if discard_reason is not None and not delegate_ledger._is_valid_discard_reason(discard_reason):
        raise delegate_errors.DelegateError(
            f"폐기 사유는 비어 있지 않은 한 줄이어야 합니다: "
            f"{ABANDON_DISCARD_REASON_FLAG}={discard_reason!r}"
        )
    # ── 1단계: 인가 ────────────────────────────────────────────────────────
    row = _delegate_round_record(pm_home, copy_path)
    if row["harvested_at"] is not None:
        raise delegate_errors.DelegateError(f"이미 회수된 준비는 포기할 수 없습니다: {copy_path}")
    expected = _expected_slot_round_path(cwd, row)
    given = Path(copy_path)
    if not given.is_absolute() or not _same_lexical_path(given, expected):
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부가 인가한 라운드 경로와 불일치 — 포기하지 않습니다: "
            f"요청={given} · 장부 인가={expected}"
        )
    run_dir = expected.parent
    # chain 검사는 **지울 것이 남아 있을 때만** 한다 — 이미 지운 run-dir 을 다시 요구하면
    # 수렴한 처분의 재호출이 "경로 검사 실패"로 막혀 전진 수렴이 깨진다.
    if os.path.lexists(run_dir):
        _assert_slot_run_dir_chain_is_plain(cwd, delegate_ledger._row_ticket_relative_dir(row))
    board_path = pm_home / Path(*PurePosixPath(row["board_rel"]).parts)
    if board_path.name != expected.name:
        raise delegate_errors.DelegateError(
            "delegate-rounds 장부의 board 경로와 라운드 이름 불일치: "
            f"board={board_path.name} · 기대={expected.name}"
        )
    home = pm_paths.home_at(Path(pm_home).resolve())
    if superseded_by_ordinal is not None:
        # 값·실재 검증은 시드 대조(mismatch) 분기와 무관하게 인자를 받는 즉시 끝낸다 — 시드
        # 그대로인 호출(플래그가 no-op 인 호출)에도 잘못된 값을 걸러 장부에 남을 값을 항상
        # 유효하게 유지한다.
        if superseded_by_ordinal < 1 or superseded_by_ordinal == row["ordinal"]:
            raise delegate_errors.DelegateError(
                f"대체 라운드 ordinal 이 올바르지 않습니다(자기 자신이거나 1 미만): "
                f"{ABANDON_SUPERSEDED_BY_FLAG}={superseded_by_ordinal} · "
                f"이 라운드 ordinal={row['ordinal']}"
            )
        existing_ordinals = {
            item.ordinal
            for item in round_seed.load_rounds(pm_paths.tickets_dir(home), row["ticket"])
        }
        if superseded_by_ordinal not in existing_ordinals:
            raise delegate_errors.DelegateError(
                "대체 라운드가 존재하지 않습니다 — 없는 ordinal 을 대체본으로 대지 않습니다: "
                f"{ABANDON_SUPERSEDED_BY_FLAG}={superseded_by_ordinal} · ticket={row['ticket']} · "
                f"준비된 순번={sorted(existing_ordinals)}"
            )
    changed = False
    sync_ready = False
    # 이번 호출이 관측한 슬롯 bytes. 5단계가 삭제 직전에 다시 읽어 이 값과 대조한다 — 사전 read
    # 이후 살아 있는 agent 가 쓴 산출을 지우지 않기 위해서다.
    observed_slot_text = (
        _read_slot_round_text(expected) if os.path.lexists(expected) else None
    )

    # ── 2단계: 종료 판단 + 시드 대조 → 마감 행 append(내구성 있는 intent) ──
    # 이번 호출이 실제로 선언 분기를 발화해 대체-확인·폐기를 확정했는지 — 장부 기록·loud 는 이
    # 값이 True 일 때만 한다(값이 그냥 주어졌다는 사실만으로는 기록하지 않는다).
    superseded_confirmed_this_call = False
    discard_confirmed_this_call = False
    if "abandoned_at" not in row:
        reserved = (
            _read_board_round_text(board_path) if os.path.lexists(board_path) else None
        )
        if observed_slot_text is None and reserved is None:
            # 지울 자산이 하나도 없다 — 남은 것은 장부 행뿐이고, 그 행을 닫는 조건은 "무엇을
            # 지우나" 가 아니라 "무엇을 밝혔나" 다.
            if discard_reason is None:
                raise delegate_errors.DelegateError(
                    "슬롯 사본도 board 라운드 파일도 없어 지울 자산이 없습니다 — 이 예약을 왜 "
                    f"폐기하는지 {ABANDON_DISCARD_REASON_FLAG} 로 밝히고 다시 부르세요: "
                    f"ticket={row['ticket']} · role={row['role']} · "
                    f"ordinal={row['ordinal']} · copy={expected}"
                )
            discard_confirmed_this_call = True
        elif observed_slot_text is None:
            raise delegate_errors.DelegateError(
                f"슬롯 라운드 파일이 없습니다(포기 판정 불능): {expected}"
            )
        elif reserved is None:
            raise delegate_errors.DelegateError(
                f"board 라운드 파일이 없습니다(예약 소실): {board_path}"
            )
        elif markdown_text._normalized_newlines(observed_slot_text) != markdown_text._normalized_newlines(reserved):
            # 값·실재 검증은 1단계에서 이미 끝났다 — 여기서는 이번 호출이 그 검증을 거쳐
            # 선언 분기를 실제로 발화했다는 사실만 표시한다(장부 기록·loud 를 여는 유일한
            # 조건).
            if discard_reason is not None:
                discard_confirmed_this_call = True
            elif superseded_by_ordinal is not None:
                superseded_confirmed_this_call = True
            else:
                raise delegate_errors.DelegateError(
                    "산출이 있어 포기할 수 없습니다(시드 그대로가 아님) — `ticket harvest` 로 "
                    "회수하거나, 재실행으로 대체된 라운드라면 그 대체본 ordinal 을 "
                    f"{ABANDON_SUPERSEDED_BY_FLAG} 로, 폐기하는 라운드라면 그 사유를 "
                    f"{ABANDON_DISCARD_REASON_FLAG} 로 밝히고 다시 부르세요: "
                    f"ticket={row['ticket']} {board_path.name}"
                )
        if not assume_dead:
            problem = _abandon_liveness_problem(
                row, copy_path=expected, pm_home=pm_home,
            )
            if problem is not None:
                raise delegate_errors.DelegateError(problem)
        if superseded_confirmed_this_call:
            # loud 는 생존 게이트를 통과한 뒤에만 — 거부된 호출에 "포기합니다" 를 남기지 않는다.
            print(
                "[pm-delegate] 대체-확인: 산출이 시드와 달라도 포기합니다 — "
                f"ticket={row['ticket']} role={row['role']} ordinal={row['ordinal']} 을 "
                f"ordinal={superseded_by_ordinal} 대체본으로 간주합니다(산출은 board 판정 표면에 "
                f"오르지 않습니다): copy={copy_path}",
                file=sys.stderr,
            )
        if discard_confirmed_this_call:
            print(
                "[pm-delegate] 폐기-확인: 이 라운드를 폐기합니다 — "
                f"ticket={row['ticket']} role={row['role']} ordinal={row['ordinal']} · "
                f"사유={discard_reason}(산출이 있으면 board 라운드 파일로 옮기고 판정 표면에는 "
                f"오르지 않습니다): copy={copy_path}",
                file=sys.stderr,
            )
        abandoned = dict(row)
        abandoned["abandoned_at"] = datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat()
        if superseded_confirmed_this_call:
            abandoned["superseded_by_ordinal"] = superseded_by_ordinal
        if discard_confirmed_this_call:
            abandoned["discard_reason"] = discard_reason
        _append_delegate_rounds_ledger(pm_home, abandoned)
        changed = True

    # 5단계의 파괴 효력은 **장부에 기록된** 선언만 인정한다 — 재호출 인자는(다시 줬어도)
    # 무시한다. 이번 호출이 2단계를 거쳤으면 방금 기록한 값을, 건너뛰었으면(행이 이미 닫혀
    # 있었으면) 그 닫힌 행에 이미 기록됐던 값을 그대로 읽는다 — 검증을 거치지 않은 이번
    # 호출의 인자가 파괴 판정에 섞이지 않는다.
    superseded_marker_recorded: int | None
    if superseded_confirmed_this_call:
        superseded_marker_recorded = superseded_by_ordinal
    else:
        recorded = row.get("superseded_by_ordinal")
        superseded_marker_recorded = (
            recorded if isinstance(recorded, int) and not isinstance(recorded, bool) else None
        )
    discarded_recorded: bool = (
        discard_confirmed_this_call or delegate_ledger._is_valid_discard_reason(row.get("discard_reason"))
    )

    # ── 3단계: board_lock 안 — 최대 순번 재확인 + 최대면 unlink (락 안은 여기까지) ──
    # 폐기는 순번과 무관하게 보존이다 — 그 자리가 산출을 옮겨 받는 자리라 지울 수 없다.
    board_kept_reason: str | None = None
    reserved_now: str | None = None
    unlinked = False
    with fs.board_lock(home):
        if os.path.lexists(board_path):
            existing = round_seed.load_rounds(pm_paths.tickets_dir(home), row["ticket"])
            max_ordinal = max((item.ordinal for item in existing), default=row["ordinal"])
            if not discarded_recorded and row["ordinal"] >= max_ordinal:
                try:
                    board_path.unlink()
                except OSError as exc:
                    raise delegate_errors.DelegateError(
                        f"board 라운드 파일 삭제 실패: {board_path}: {exc}"
                    ) from exc
                unlinked = True
                changed = True
            else:
                board_kept_reason = (
                    "폐기 처분(산출 이관 대상)" if discarded_recorded
                    else f"중간 순번(현재 최대 순번={max_ordinal})"
                )
                reserved_now = _read_board_round_text(board_path)

    # ── 4단계: 락 밖 — 보존 분기 산출 이관·표식 발행 + sync ────────────────
    # 발행은 `reserved_now` 재판독 **뒤**다: 5단계 파괴 판정이 그 bytes 를 기준선으로 쓰므로,
    # 앞서 붙이면 표식 자체가 "산출이 생겼다" 로 읽혀 정상 포기가 거부로 뒤집힌다. 락 안에도
    # 두지 않는다 — 그 임계구역은 순번 판정과 unlink 만 담는다(재진입 교착 주석 참조).
    # 폐기 분기는 표식만 붙이지 않고 **관측한 슬롯 산출로 본문을 교체**한다 — 그 사본이 유일본
    # 이라, 옮기지 않고 run-dir 을 지우면 산출이 사라진다. 표식 유무가 이관 완료의 관측이라
    # 재호출은 두 번 쓰지 않는다.
    marker_issued = False
    if reserved_now is not None:
        baseline = _round_text_without_refused_marker(reserved_now, row["role"])
        if baseline == reserved_now:
            moved = (
                observed_slot_text
                if discarded_recorded and observed_slot_text is not None
                else reserved_now
            )
            try:
                round_store.replace_round(
                    board_path, _round_text_with_refused_marker(moved, row["role"]),
                )
            except (OSError, UnicodeError) as exc:
                raise delegate_errors.DelegateError(
                    f"board 라운드 표식 발행 실패: {board_path}: {exc}"
                ) from exc
            marker_issued = True
            changed = True
        # 기준선은 언제나 표식 이전 bytes 다 — 재호출도 같은 값으로 5단계를 판정한다.
        reserved_now = baseline
    # 이번에 지웠든(첫 통과) 앞선 시도가 지웠든(재개), board 라운드가 사라졌는데 아직 정리할 것이
    # 남아 있으면 부른다 — 앞선 시도의 sync 실패가 영구 미커밋으로 굳지 않게 한다(`git add -A`
    # + commit 이라 재실행은 no-op). 보존 분기는 표식이 그 파일을 바꾸므로 같은 이유로 함께
    # 커밋한다.
    if (
        (not os.path.lexists(board_path) and (unlinked or os.path.lexists(run_dir)))
        or (reserved_now is not None and (marker_issued or os.path.lexists(run_dir)))
    ):
        message = f"ticket-abandon {row['ticket']} {row['role']}"
        # board 부분 커밋 seam 은 이름으로 직접 부른다 — harvest 와 같은 이유다(부분 동기 사본에서
        # 커밋만 조용히 빠지는 rc0 을 피한다).
        sync_ready = bool(round_maintenance._rounds_mutation_sync_paths(home, message, (board_path,)))

    # ── 5단계: 슬롯 파일 재판독 → 시드 그대로일 때만 run-dir 삭제 ──────────
    if os.path.lexists(run_dir):
        if os.path.lexists(expected):
            current = _read_slot_round_text(expected)
            references = [observed_slot_text]
            # board 파일이 남아 있으면(중간 순번 보존) 그 bytes 가 시드의 단일 진실이다 — 호출
            # 사이에 착지한 쓰기까지 이 대조가 잡는다. **선언이 기록된 행은 예외다**: 그 board
            # 파일은 이 run 이 산출을 낸 뒤로도 시드 그대로 남아 있거나(대체-확인) 이관으로
            # 산출을 받았으므로(폐기) 시드와 영구히 어긋난다 — 이 축에서는 관측된
            # 산출(`observed_slot_text`)이 유일한 기준선이다.
            if (
                reserved_now is not None
                and superseded_marker_recorded is None
                and not discarded_recorded
            ):
                references.append(reserved_now)
            if any(
                reference is None
                or markdown_text._normalized_newlines(current) != markdown_text._normalized_newlines(reference)
                for reference in references
            ):
                raise delegate_errors.DelegateError(
                    "슬롯 라운드 파일이 시드와 달라져 run-dir 을 지우지 않았습니다(산출 보존) — "
                    f"산출을 직접 꺼낸 뒤 정리하세요: copy={expected} · run-dir={run_dir}"
                )
        try:
            fs.force_rmtree(run_dir)
        except OSError as exc:
            raise delegate_errors.DelegateError(
                f"run-dir 삭제 실패: {run_dir}: {exc} — 같은 명령을 다시 실행하면 남은 정리를 "
                "이어 갑니다"
            ) from exc
        _prune_empty_run_dir(run_dir)
        changed = True

    # ── 6단계: 세 자산 재판독 수렴 단언 ───────────────────────────────────
    board_present = os.path.lexists(board_path)
    run_dir_removed = not os.path.lexists(run_dir)
    ledger_closed = "abandoned_at" in _delegate_round_record(pm_home, copy_path)
    board_converged = board_present == (board_kept_reason is not None)
    return TicketAbandonResult(
        changed=changed,
        sync_ready=sync_ready,
        board_removed=not board_present,
        run_dir_removed=run_dir_removed,
        converged=bool(ledger_closed and run_dir_removed and board_converged),
    )


def _refund_gate_rejected_ticket_copy(
    ticket_copy: TicketCopyPlan, *, cwd: Path, pm_home: Path,
) -> None:
    """예약(`prepare_ticket_copy`) 이후 ~ 실행 인계(`_execute_and_collect`) 이전 구간의
    **단일 정리 경계**. `main()` 의 finally 하나가 이 함수를 부른다 — 그 구간을 벗어나는 모든
    경로(명시 return · 전파 예외)가 지점 삽입 없이 여기로 수렴한다(리뷰 라운드가 꽂았던 6개
    지점 삽입을 대체). board 라운드 파일·delegate-rounds 장부 행·run-dir 세 축을 같은 프로세스가
    즉시 종결한다. 새 경로를 만들지 않고 기존 `abandon_ticket_copy` 를 그대로 재사용한다.
    `assume_dead=True` — 이 run 의 `owner_pid` 는 **이 살아 있는 프로세스 자신**이라(핸드오프 전
    이탈) 생존 판정이 자기 자신을 "진행 중"으로 오판해 막는다; 정상 스폰 후 죽은 run 의
    교차-프로세스 정리와는 다른 축이다.
    원 종료 사유(rc·메시지)는 이 함수 호출 **전**에 이미 stderr 로 loud 하게 남거나(명시 거부) 그대로
    전파 중이다(예외 이탈). 여기서는 환불 결과만 덧붙이고, 환불 자체가 어떤 예외형으로 실패해도
    (`DelegateError` 뿐 아니라) 원 결과를 대체하지 않는다 — `return`/`raise` 없이 경고만 추가해,
    이 함수를 부른 `finally` 통과 뒤 원래 return 값·전파 예외가 그대로 유지된다.
    """
    try:
        result = abandon_ticket_copy(
            copy_path=ticket_copy.path, cwd=cwd, pm_home=pm_home, assume_dead=True,
        )
    except BaseException as exc:
        # F-001 — 환불이 `KeyboardInterrupt`·`SystemExit`·비-DelegateError 로 죽어도
        # 이미 pending 인 원 return 값/전파 예외를 대체하지 않는다: 어떤 예외형이든 여기서 잡아
        # loud 경고만 남기고 `return`(정상 반환) — 호출부 `finally` 통과 뒤 원 결과가 그대로다.
        skew = _absorb_engine_rev_skew_for_recovery(exc, "ticket_copy_gate_refund")
        cause = f"엔진 사본 불일치 — {exc}" if skew else f"{type(exc).__name__}: {exc}"
        print(
            "경고: 위임 이탈(거부/예외) 후 ticket 예약 환불 실패 — board 라운드·run-dir 이 잔류할 수 "
            f"있습니다. 같은 정리를 `ticket abandon --copy {ticket_copy.path} --cwd {cwd} "
            f"{ABANDON_ASSUME_DEAD_FLAG}` 로 다시 시도하세요: {cause}",
            file=sys.stderr,
        )
        return
    print(
        "[pm-delegate] 위임 이탈(거부/예외) 후 ticket 예약 환불: "
        f"board_removed={str(result.board_removed).lower()} · "
        f"run_dir_removed={str(result.run_dir_removed).lower()} · "
        f"converged={str(result.converged).lower()} · copy={ticket_copy.path}",
        file=sys.stderr,
    )


def _ticket_copy_preamble(plan: TicketCopyPlan) -> str:
    """이 위임의 산출 자리 안내 — 좌표도 다음 finding ID 도 준비가 확정한 실값이다.

    리뷰 채널은 라운드마다 fresh 라 이전 라운드가 쓴 번호를 모른다. 시드에만 값을 실으면 사본을
    열기 전에 프롬프트만 읽는 위임에서 번호가 추측이 된다 — 두 자리가 같은 값을 말해야 한다.
    """
    seat = (
        f"라운드 산출 기록: 이 위임의 산출은 {plan.path} **하나에만** 쓴다"
        f"(첫 줄 헤더는 그대로 두고 그 아래 골격을 채운다). 같은 디렉터리의 "
        f"`{TICKET_COPY_SPEC_NAME}`(티켓 명세)와 `{TICKET_COPY_ROUNDS_DIRNAME}/`"
        "(이전 라운드)는 **읽기 전용**이다. PM 홈 티켓은 편집하지 마라. "
        "이 파일은 응답과 별개로 종료 시 기계 회수된다."
    )
    if not plan.next_finding_id:
        return seat
    return seat + "\nfinding ID: " + review_form.NEXT_FINDING_ID_RULE.format(
        next_id=plan.next_finding_id,
    )


def pm_review_refused_line(role: str) -> str:
    """그 역할 라운드에 붙는 표식 줄 — 발행이 이 함수 하나를 본다."""
    return f"<!-- {review_form.PM_REVIEW_REFUSED_MARKER} role={role} -->"


# 추가 리뷰어 채널 인스턴스 — 옛 산출에 이미 박혀 있는 그 줄이다(마이그레이션 없음).
ADDITIONAL_REVIEWER_REFUSED_LINE = pm_review_refused_line(review_form.ADDITIONAL_REVIEWER_ROLE)


def _round_text_with_refused_marker(text: str, role: str) -> str:
    """표식을 끝줄로 붙인 본문 — 개행 표기는 원문을 따른다(bytes 대조를 쓰는 자리들 때문)."""
    newline = "\r\n" if "\r\n" in text else "\n"
    separator = "" if text.endswith(("\n", "\r")) else newline
    return f"{text}{separator}{pm_review_refused_line(role)}{newline}"


def _round_text_without_refused_marker(text: str, role: str) -> str:
    """그 역할 표식을 끝에서 걷어 낸 본문 — 발행 이전 bytes 를 되돌리는 역함수.

    포기(`abandon_ticket_copy`)의 파괴 판정 기준선은 표식을 붙이기 **전** bytes 다. 재호출은
    이미 표식이 붙은 파일을 읽으므로 그 자리에서 기준선을 되돌리지 않으면, 엔진이 자기가 쓴
    줄을 "산출이 생겼다" 로 읽어 남은 정리를 영영 끝내지 못한다.
    """
    line = pm_review_refused_line(role)
    for terminator in ("\r\n", "\n", ""):
        suffix = f"{line}{terminator}"
        if text.endswith(suffix):
            return text[: -len(suffix)]
    return text


def validate_review_block(
    reply_text: str, *, reviewer_role: str = review_form.ADDITIONAL_REVIEWER_ROLE,
) -> str | None:
    """리뷰 산출의 `pm-review-v1` 블록을 회수 전에 검증한다(위반 사유 또는 None).

    채널은 finding ID 접두만 가른다 — 스키마·중복·자리 규칙은 두 채널이 같다.
    """
    try:
        blocks = review_form._pm_review_json_blocks(reply_text)
    except review_form.PMReviewError as exc:
        return str(exc)
    dispositions = [
        block for block in blocks if block.kind == review_form.PM_REVIEW_DISPOSITION_BLOCK
    ]
    if dispositions:
        return (
            f"리뷰어 산출에 {review_form.PM_REVIEW_DISPOSITION_BLOCK} 이 있습니다 — PM 판정 블록은 "
            "라운드 파일 밖 명세(PM 영역)가 소유합니다"
        )
    reviews = [block for block in blocks if block.kind == review_form.PM_REVIEW_BLOCK]
    if len(reviews) != 1:
        return f"{review_form.PM_REVIEW_BLOCK} block 이 정확히 하나가 아닙니다(발견 {len(reviews)}개)"
    value = reviews[0].value
    try:
        review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
        version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
        if not isinstance(value["findings"], list) or not isinstance(
            value["confirmations"], list
        ):
            raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
        parsed = [
            review_form._pm_review_parse_finding(
                item, 0, reviewer_role=reviewer_role, version=version,
            )
            for item in value["findings"]
        ]
        parsed += [
            review_form._pm_review_parse_confirmation(
                item, 0, reviewer_role=reviewer_role,
            )
            for item in value["confirmations"]
        ]
    except review_form.PMReviewError as exc:
        return str(exc)
    ids = [item.id for item in parsed]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        # 같은 ID 를 `findings` 와 `confirmations` 양쪽에 실은 형상이 여기로 온다 — 사유가
        # 이름을 짚어야 리뷰어가 어느 항목을 옮겨야 하는지 그 자리에서 안다.
        return (
            f"finding/confirmation ID 중복: {', '.join(duplicated)} — 기존 finding 은 "
            "`confirmations` 로만 참조하고 `findings` 에는 신규 ID 만 씁니다"
        )
    return None


def collect_confirmable_finding_ids(
    ticket_text: str, reviewer_role: str, rounds: Sequence,
) -> list[str]:
    """확인 라운드가 `confirmations` 에 실을 수 있는 그 채널 ID (정렬).

    배제는 둘이고 리뷰 라운드 시드 프리필(`render_ticket_growth_section_seed`)과 **같은 규칙**
    이다: 회수 거부 라운드(판정 표면 밖)와 PM 이 `rejected` 로 판정한 ID(재등장을 표면이
    malformed 로 막는다). 두 채널이 서로 다른 목록을 보면 한쪽 리뷰어가 표면이 거부할 ID 를
    확인 대상으로 받는다.
    """
    declared = review_state.collect_review_finding_declarations(
        ticket_text, reviewer_role, rounds,
    )
    if not declared:
        return []
    rejected: set[str] = set()
    for item in review_state._pm_review_surface_rounds(rounds):
        if item.role != reviewer_role:
            continue
        try:
            rejected |= review_state._pm_review_rejected_finding_ids(
                ticket_text, reviewer_role=reviewer_role,
                reviewer_ordinal=item.ordinal,
            )
        except delegate_errors.DelegateError:
            continue        # 읽을 수 없는 판정 블록은 표면 파서가 loud 하게 잡는다.
    return sorted(declared - rejected)


def _review_id_collisions(
    body: str, existing_finding_ids: Sequence[str],
) -> list[str]:
    """회신 블록의 **신규 finding** ID 중 티켓에 이미 있는 것(확인 라운드는 대상 아님)."""
    existing = set(existing_finding_ids)
    if not existing:
        return []
    blocks = [
        block for block in review_form._pm_review_json_blocks(body)
        if block.kind == review_form.PM_REVIEW_BLOCK
    ]
    if len(blocks) != 1:
        return []
    findings = blocks[0].value.get("findings")
    if not isinstance(findings, list):
        return []
    collided = {
        item["id"] for item in findings
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and item["id"] in existing
    }
    return sorted(collided)


def _review_missing_confirmation_targets(
    body: str, declared_finding_ids: Sequence[str],
) -> list[str]:
    """회신 블록의 confirmation ID 중 티켓 판정 표면에 **없는** 것.

    판정 표면은 confirmation 이 선행 finding 을 참조할 것을 요구한다. 라운드가 파일 하나라
    차등 판정·반사실 프로브가 사라졌으므로, 표면에 없는 ID(회수 거부된 라운드의
    ID·환각 ID)를 실은 블록을 막는 것은 이 대조 하나다.
    """
    declared = set(declared_finding_ids)
    blocks = [
        block for block in review_form._pm_review_json_blocks(body)
        if block.kind == review_form.PM_REVIEW_BLOCK
    ]
    if len(blocks) != 1:
        return []
    confirmations = blocks[0].value.get("confirmations")
    if not isinstance(confirmations, list):
        return []
    missing = {
        item["id"] for item in confirmations
        if isinstance(item, dict) and isinstance(item.get("id"), str)
        and item["id"] not in declared
    }
    return sorted(missing)


def review_harvest_problem(
    reply_text: str, *, ticket_text: str, rounds: Sequence,
    reviewer_role: str = review_form.ADDITIONAL_REVIEWER_ROLE,
) -> str | None:
    """리뷰 산출을 라운드로 회수해도 되는지 판정한다 — 위반 사유 또는 None.

    두 회수 경로가 이 함수 하나를 부른다: 추가 리뷰어 회수(`additional_reviewer`)와 내부 채널 회수
    (`harvest_ticket_copy`). 채널이 가르는 것은 finding ID 접두뿐이고 사유·강도는 같다 — 판정
    표면이 하나라 한쪽만 관대하면 그쪽 산출이 표면을 막는다.

    거부한 산출은 판정 표면에 오르지 않되 사라지지도 않는다. 추가 리뷰어 채널은 라운드 파일을
    만들지 않고 raw 에 남기고, 내부 채널은 board 라운드 파일 bytes 를 그대로 두고 슬롯 run-dir 을
    유지한다 — 사본을 고쳐 같은 경로로 다시 회수할 수 있다.

    신규 finding ID 대조의 corpus 에서 시드 그대로인(`pending`) 라운드와 엔진 표식이 붙은
    라운드는 뺀다 — 그 라운드가 실은 ID 는 엔진이 넣은 자리표시이지 선언이 아니고, 회수 대상
    라운드 자신도 그 상태다.

    사유는 네 축이고 종류를 가리지 않고 같은 처리(거부)다: 엔진 전용 표식 선언 · `pm-review-v1`
    블록 규칙 위반(부재·중복·스키마·JSON 손상 · 같은 ID 를 `findings` 와 `confirmations` 양쪽
    기재) · 티켓에 이미 있는 finding ID 재선언 · 티켓 판정 표면에 없는 confirmation 대상.

    대조는 두 축이고 시야가 다르다. 신규 finding ID 는 **넓은** 스캔(산문 인용 포함)과,
    confirmation 대상은 **판정 표면 선언**과 맞춘다. 차등 판정·반사실 프로브는 없다 — 이 산출이
    다른 라운드의 판정을 오염시킬 자리가 없다(F-028 구조 폐쇄).
    """
    if review_form.pm_review_refused_marker_present(reply_text):
        return (
            f"엔진 전용 표식({review_form.PM_REVIEW_REFUSED_MARKER})을 회신이 선언했습니다 — 그 줄은 "
            "라운드를 판정 표면에서 빼므로 산출이 스스로 쓸 수 없습니다"
        )
    problem = validate_review_block(reply_text, reviewer_role=reviewer_role)
    if problem is not None:
        return problem
    existing_ids = review_state.collect_review_finding_ids(ticket_text, reviewer_role)
    for item in rounds:
        if (
            getattr(item, "pending", False)
            or review_form.pm_review_refused_marker_present(item.text)
        ):
            # 엔진이 시드에 넣은 ID 는 **선언이 아니다**. 회수 대상 라운드 자신이 이 목록에
            # 들어 있으므로(예약된 board 파일은 회수 전까지 시드 그대로다), 시드가 실은 다음 ID
            # 를 그대로 쓴 회신이 자기 자신과 충돌해 거부되는 자기 충돌을 여기서 닫는다.
            # 표식이 붙은 라운드도 같은 자리에서 뺀다 — 표식은 bytes 를 바꿔 `pending` 을
            # 무너뜨리지만 그 골격의 ID 는 여전히 엔진이 넣은 자리표시다(종결된 예약).
            continue
        existing_ids |= review_state.collect_review_finding_ids(item.text, reviewer_role)
    collisions = _review_id_collisions(reply_text, existing_ids)
    if collisions:
        return (
            f"finding ID 재선언: {', '.join(collisions)} — 티켓에 이미 있는 ID 라 이 라운드를 "
            "회수하지 않았습니다(다음 라운드에서 새 ID 로 다시 내십시오)"
        )
    missing = _review_missing_confirmation_targets(
        reply_text,
        review_state.collect_review_finding_declarations(
            ticket_text, reviewer_role, rounds,
        ),
    )
    if missing:
        return (
            f"confirmation 대상 finding 부재: {', '.join(missing)} — 티켓 판정 표면에 없는 "
            "ID 라 이 라운드를 회수하지 않았습니다(존재하지 않는 ID 이거나 회수되지 않은 "
            "산출의 ID 입니다 · 다음 라운드에서 실재하는 ID 로 다시 내십시오)"
        )
    return None


def _with_ticket_copy_preamble(
    prompt: str, ticket_copy: TicketCopyPlan | None,
    cluster_plan: "ClusterCopyPlan | None" = None,
) -> str:
    """full/resume delta/fallback/fresh 모든 wire가 이번 run의 같은 사본 지시를 받는 seam.

    자리가 N 이면 N 자리 안내가 붙는다 — 붙이는 쪽과 준비한 쪽이 같은 판정을 쓴다.
    """
    note = _round_copy_preamble(ticket_copy, cluster_plan)
    if note is None or note in prompt:
        return prompt
    return note + "\n\n" + prompt


# ── 형제 모듈 deep-import seam (PYTHONPATH 무의존) ─────

def _load_additional_reviewer():
    """아직 평면 reviewer에 남은 묶음 리뷰 프롬프트 생성기를 읽는다."""
    path = Path(__file__).resolve().parent / "additional_reviewer.py"
    return _load_module_from_path(
        path, "additional_reviewer.py", verifier=_verify_engine_rev,
    )


def _load_delegate_channel_guard():
    """엔진 delegate_channel_guard 를 로드해 cross-role 채널 판정(`decide`)을 재사용한다.

    native Agent 위임 훅과 같은 seam — `ticket prepare`가 별도 하네스 비교식을 다시 쓰지 않는다."""
    path = Path(__file__).resolve().parent / "delegate_channel_guard.py"
    return _load_module_from_path(
        path, "delegate_channel_guard.py", verifier=_verify_engine_rev, cache=True,
    )


# ── 내부 code-reviewer 라운드 장부 ────────────────────────────────────────

_INTERNAL_MUST_FIX_CANONICAL_HEADER = "## must-fix"


def _internal_verdict_declaration_forms() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """external verdict 파서의 정확일치 토큰에서 preamble 선언형을 파생한다."""

    def declarations(tokens: Sequence[str]) -> tuple[str, ...]:
        # casefold가 같은 대소문자 변형도 parser가 각각 받으므로 생략하지 않는다.
        ordered = sorted(tokens, key=lambda token: (token.isascii(), token.casefold(), token))
        return tuple(f"판정: {token}" for token in ordered)

    return (
        declarations(review_verdict._PASS_VERDICT_TOKENS),
        declarations(review_verdict._REJECT_VERDICT_TOKENS),
    )


def _internal_review_format_preamble() -> str:
    """내부 reviewer가 보는 산출 계약 — 판정/0건 parser 원천에서 매번 합성."""
    pass_forms, reject_forms = _internal_verdict_declaration_forms()
    canonical_pass, canonical_reject = review_verdict._internal_canonical_verdict_forms()
    pass_examples = ", ".join(f"`{form}`" for form in pass_forms)
    reject_examples = ", ".join(f"`{form}`" for form in reject_forms)
    none_examples = ", ".join(
        f"`- {token}`" for token in review_verdict._INTERNAL_NONE_ITEM_TOKENS
    )
    canonical_none = f"- {review_verdict._INTERNAL_NONE_ITEM_TOKENS[0]}"
    return (
        "내부 리뷰 산출 형식(판정 파서 계약):\n"
        f"- 판정 선언은 행 선두에 `{canonical_pass}` 또는 `{canonical_reject}`로 정확히 "
        "한 번 쓴다. 강조·헤딩 접두는 허용하지만 "
        "인용문·코드펜스 안 선언은 판정으로 세지 않는다.\n"
        f"- must-fix 절은 markdown 제목 `{_INTERNAL_MUST_FIX_CANONICAL_HEADER}`로 열고 "
        "목록으로 쓴다. 0건이면 표준형 "
        f"`{canonical_none}` 한 항목을 남긴다(파서가 같은 원천에서 받는 0건 항목: "
        f"{none_examples}). 산문 `must-fix 없습니다`는 0건으로 읽히지 않는다.\n"
        "- 판정 낱말은 파서 허용 토큰 중 하나만 쓰며 통과·반려 또는 허용 밖 낱말을 "
        f"섞지 않는다(통과 허용형: {pass_examples}; 반려 허용형: {reject_examples}).\n"
        "- 같은 code-reviewer 사본 절에 section-add가 시드한 리뷰 골격을 그대로 채운다. "
        "키·상태·분류를 다시 쓰거나 골격 밖 schema를 만들지 않는다."
    )


class ArchitectTest(NamedTuple):
    id: str
    target: str
    command: str
    expected: str
    negative: str


def _contract_test_targets(
    value: str, field: str, *, required: bool = True,
) -> tuple[str, ...]:
    """계약이 지목한 repo-relative Python 테스트 대상 — 값의 **첫 토큰** 하나뿐이다.

    산문 전역을 훑으면 "옛 경로 대신 새 경로" 같은 서술의 옛 경로까지 diff 결속 대상이 된다 —
    이번 라운드가 손댈 자리가 아닌 파일을 회수가 요구하게 되므로, 대상은 값이 맨 앞에 적은
    하나로 고정한다. 여러 파일을 결속하려면 계약 행을 여러 개 쓴다.

    ``required`` 는 architect 축(설계 라운드가 확정하는 필수 테스트)의 규칙이다 —
    거기서는 테스트 대상이 계약이라 하나도 없으면 거부한다. reviewer fix 축은
    붙일 테스트가 없는 지적(죽은 코드 삭제)을 표현할 수 있어야 하므로 끈다.
    """
    head = value.split(maxsplit=1)
    match = delegate_run_rules._CONTRACT_TEST_TARGET_RE.match(head[0]) if head else None
    if match is not None:
        target = match.group("path")
        path = PurePosixPath(target)
        if path.is_absolute() or ".." in path.parts or path.parts[0] != "tests":
            raise delegate_errors.DelegateError(f"{field} 테스트 대상은 repo-relative tests/*.py여야 합니다: {target}")
        return (target,)
    if required:
        raise delegate_errors.DelegateError(
            f"{field}에 repo-relative 테스트 대상(tests/*.py)이 없습니다"
        )
    return ()


def _reviewer_fix_test_requirement(value: str, field: str) -> tuple[str, tuple[str, ...]]:
    """reviewer의 기존 시험 재실행 선언과 파일 변경 계약을 구분한다."""
    parts = value.split(maxsplit=1)
    prefix = review_form.PM_REVIEW_EXISTING_TEST_PREFIX
    if parts and parts[0].startswith(prefix):
        if parts[0] != prefix or len(parts) != 2:
            raise delegate_errors.DelegateError(
                f"{field}는 {prefix} 다음에 공백과 테스트 경로가 필요합니다"
            )
        return "existing", _contract_test_targets(parts[1], field)
    targets = _contract_test_targets(value, field, required=False)
    return ("changed" if targets else "location"), targets


def _contract_repo_paths(value: str, field: str) -> tuple[str, ...]:
    """계약 산문이 지목한 이 저장소의 파일 경로.

    fix 축의 작업 증명은 "계약이 적은 자리를 이번 라운드가 바꿨는가"다. 그 자리는
    새 선언이 아니라 계약이 이미 채운 ``location`` 산문이라, 여기서 diff 와 대조할 수
    있는 repo-relative 파일 경로만 뽑는다. 절대경로는 저장소 경로가 아니라 애초에
    잡히지 않고, 상위 탈출은 loud 거부다.
    """
    paths: list[str] = []
    for match in delegate_run_rules._CONTRACT_REPO_PATH_RE.finditer(value):
        target = match.group("path")
        path = PurePosixPath(target)
        if ".." in path.parts:
            raise delegate_errors.DelegateError(f"{field} 경로는 repo-relative여야 합니다: {target}")
        if path.suffix and target not in paths:
            paths.append(target)
    return tuple(paths)


def parse_architect_tests(text: str) -> tuple[ArchitectTest, ...]:
    """architect 라운드의 유일한 테스트 계약을 엄격 파싱한다."""
    lines = text.splitlines()
    openings = [index for index, line in enumerate(lines) if line == f"```{review_form.ARCHITECT_TEST_BLOCK}"]
    malformed = [
        line for line in lines
        if review_form.ARCHITECT_TEST_BLOCK in line and line != f"```{review_form.ARCHITECT_TEST_BLOCK}"
    ]
    if malformed or len(openings) != 1:
        raise delegate_errors.DelegateError(
            f"architect 테스트 계약은 `{review_form.ARCHITECT_TEST_BLOCK}` block 정확히 1개여야 합니다"
        )
    start = openings[0]
    try:
        end = lines.index("```", start + 1)
    except ValueError as exc:
        raise delegate_errors.DelegateError("architect 테스트 계약 종료 fence가 없습니다") from exc
    if any(line.startswith("```") for line in lines[start + 1:end]):
        raise delegate_errors.DelegateError("architect 테스트 계약 안에 중첩 fence가 있습니다")
    try:
        payload = json.loads(
            "\n".join(lines[start + 1:end]), object_pairs_hook=review_form._pm_review_json_object,
        )
    except ValueError as exc:
        raise delegate_errors.DelegateError(f"architect 테스트 계약 JSON 파싱 실패: {exc}") from exc
    if not isinstance(payload, dict):
        raise delegate_errors.DelegateError("architect 테스트 계약 payload는 JSON object여야 합니다")
    try:
        review_form._pm_review_exact_keys(payload, review_form.ARCHITECT_TEST_PAYLOAD_KEYS, review_form.ARCHITECT_TEST_BLOCK)
    except review_form.PMReviewError as exc:
        raise delegate_errors.DelegateError(str(exc)) from exc
    if payload.get("version") != review_form.ARCHITECT_TEST_VERSION:
        raise delegate_errors.DelegateError(
            f"architect 테스트 계약 version은 {review_form.ARCHITECT_TEST_VERSION}이어야 합니다"
        )
    raw_tests = payload.get("tests")
    if not isinstance(raw_tests, list) or not raw_tests:
        raise delegate_errors.DelegateError("architect 테스트 계약에는 필수 테스트가 1건 이상 필요합니다")
    tests: list[ArchitectTest] = []
    seen: set[str] = set()
    for raw in raw_tests:
        if not isinstance(raw, dict):
            raise delegate_errors.DelegateError("architect test 행은 JSON object여야 합니다")
        try:
            review_form._pm_review_exact_keys(raw, review_form.ARCHITECT_TEST_ROW_KEYS, "architect test")
        except review_form.PMReviewError as exc:
            raise delegate_errors.DelegateError(str(exc)) from exc
        values: dict[str, str] = {}
        for key in review_form.ARCHITECT_TEST_ROW_KEYS:
            try:
                values[key] = review_form._pm_review_contract_string(
                    raw.get(key), f"architect test.{key}",
                )
            except review_form.PMReviewError as exc:
                raise delegate_errors.DelegateError(str(exc)) from exc
        if delegate_run_rules._ARCHITECT_TEST_ID_RE.fullmatch(values["id"]) is None:
            raise delegate_errors.DelegateError(f"architect test.id 형식 불일치: {values['id']!r}")
        if values["id"] in seen:
            raise delegate_errors.DelegateError(f"architect test.id 중복: {values['id']}")
        seen.add(values["id"])
        try:
            review_form._pm_review_assert_verify_command_shape(
                values["command"], f"architect test {values['id']}.command",
            )
        except review_form.PMReviewError as exc:
            raise delegate_errors.DelegateError(str(exc)) from exc
        _contract_test_targets(
            values["target"], f"architect test {values['id']}.target",
        )
        tests.append(ArchitectTest(**values))
    return tuple(tests)


def architect_tests_from_rounds(
    rounds: Sequence, *, required: bool = True,
) -> tuple[ArchitectTest, ...]:
    """마지막 architect 산출의 테스트 계약. 신규 준비면 부재도 fail-loud한다."""
    candidates = [
        item for item in rounds
        if item.role == "architect" and not getattr(item, "pending", False)
    ]
    if not candidates:
        if required:
            raise delegate_errors.DelegateError("developer 착수 전 architect 테스트 계약이 없습니다")
        return ()
    latest = max(candidates, key=lambda item: item.ordinal)
    if review_form.ARCHITECT_TEST_BLOCK not in latest.text and not required:
        return ()
    return parse_architect_tests(latest.text)


def render_pm_review_disposition_template(
    ticket_text: str, rounds: Sequence, reviewer_ordinal: int | None = None,
    *, reviewer_role: str | None = None,
) -> str:
    """기존 판정을 보존하고 미판정 행을 채운 단일 PM disposition 골격을 렌더한다.

    입력은 명세(`ticket_text` — PM 판정 블록의 자리)와 라운드 목록이다. 채널(`reviewer_role`)
    미지정이면 두 리뷰 채널을 통틀어 **최신 라운드**의 채널을 쓴다 — PM 은 채널마다 다른 절차를
    밟지 않고 같은 명령으로 판정한다.
    """
    review_rounds = review_state._pm_review_surface_rounds(rounds)
    if reviewer_role is None:
        reviewer_role = (
            review_rounds[-1].role if review_rounds else review_form.INTERNAL_REVIEW_ROLE
        )
    elif reviewer_role not in review_form.REVIEW_ROLES:
        raise review_form.PMReviewError("malformed", f"review 채널이 아닌 역할: {reviewer_role}")
    reviewer_sections = [
        item for item in review_rounds if item.role == reviewer_role
    ]
    if not reviewer_sections:
        raise review_form.PMReviewError("malformed", f"{reviewer_role} 라운드가 없습니다")
    if reviewer_ordinal is None:
        section = reviewer_sections[-1]
    else:
        matches = [item for item in reviewer_sections if item.ordinal == reviewer_ordinal]
        if not matches:
            raise review_form.PMReviewError(
                "malformed",
                f"{reviewer_role} ordinal={reviewer_ordinal} 라운드가 없습니다",
            )
        section = matches[0]

    block = review_state._pm_review_block_for_round(section)
    value = block.value
    review_form._pm_review_exact_keys(value, review_form.PM_REVIEW_PAYLOAD_KEYS, review_form.PM_REVIEW_BLOCK)
    version = review_form._pm_review_version(value, review_form.PM_REVIEW_BLOCK)
    if not isinstance(value["findings"], list) or not isinstance(value["confirmations"], list):
        raise review_form.PMReviewError("malformed", "findings/confirmations는 JSON array여야 합니다")
    findings = [
        review_form._pm_review_parse_finding(
            item, section.ordinal, reviewer_role=section.role, version=version,
        )
        for item in value["findings"]
    ]
    confirmations = [
        review_form._pm_review_parse_confirmation(
            item, section.ordinal, reviewer_role=section.role,
        )
        for item in value["confirmations"]
    ]
    finding_ids = [item.id for item in findings]
    if len(finding_ids) != len(set(finding_ids)):
        raise review_form.PMReviewError("malformed", f"reviewer ordinal={section.ordinal} finding ID 중복")
    confirmation_ids = [item.id for item in confirmations]
    if len(confirmation_ids) != len(set(confirmation_ids)):
        raise review_form.PMReviewError(
            "malformed", f"reviewer ordinal={section.ordinal} confirmation ID 중복",
        )

    existing_block, existing_rows = review_state._pm_review_disposition_rows_for_ordinal(
        ticket_text, section.ordinal, reviewer_role=section.role,
    )
    existing_by_id = {parsed.id: raw for parsed, raw in existing_rows}
    if set(existing_by_id) - set(finding_ids):
        raise review_form.PMReviewError(
            "malformed",
            f"reviewer ordinal={section.ordinal} extra disposition ID: "
            f"{sorted(set(existing_by_id) - set(finding_ids))}",
        )
    if finding_ids and existing_block is not None and "finding_zero" in existing_block:
        raise review_form.PMReviewError("malformed", "finding이 있는 reviewer에 finding_zero disposition")

    # 골격의 시야는 대상 라운드 하나인데 판정 표면의 finding ID 유일성은 티켓 전역이다. 두
    # 시야가 갈리면 골격이 프리필한 ID 를 PM 이 채워도 `parse_pm_review_delta` 가 그 판정을
    # 되돌려보낸다. 그런 ID 가 하나라도 있으면 **골격을 내지 않는다** — 나머지 ID 만 부분
    # 출력해도 그 골격을 채운 산출은 같은 라운드 때문에 표면이 통째로 거부하므로(왕복 불변식은
    # 복구되지 않고 판정 대상 finding 만 조용히 사라진다) 이 라운드는 여기서 끝낸다.
    # 형상은 둘이다: 선행 같은 채널 라운드가 이미 선언한 ID(재선언), 그리고 이 라운드가 스스로
    # `confirmations` 에 실은 자기 ID(자기-확인). 판정 표면(`parse_pm_review_delta`)도 같은
    # 라운드를 malformed 로 막으므로 두 명령이 같은 상태에 같은 판정을 낸다.
    redeclared = review_state.collect_review_finding_declarations(
        ticket_text, section.role, rounds, before_ordinal=section.ordinal,
    )
    self_confirmed = set(confirmation_ids)
    refused_ids = [
        finding_id for finding_id in finding_ids
        if finding_id in redeclared or finding_id in self_confirmed
    ]
    if refused_ids:
        listed = ", ".join(
            f"{finding_id}("
            + ("티켓 전역 재선언" if finding_id in redeclared else "같은 라운드 자기-확인")
            + ")"
            for finding_id in refused_ids
        )
        raise review_form.PMReviewError(
            "malformed",
            f"reviewer {section.role} ordinal={section.ordinal}에 판정 표면이 받지 않는 "
            f"finding ID 가 있어 판정 골격을 내지 않습니다: {listed} — 기존 finding 은 "
            "`confirmations` 로만 참조하고 `findings` 에는 신규 ID 만 씁니다",
        )

    if not finding_ids:
        if confirmation_ids:
            raise delegate_errors.DelegateError(
                f"reviewer {section.role} ordinal={section.ordinal}은 confirmation-only "
                "라운드라 PM이 판정할 신규 finding이 없습니다"
            )
        if existing_block is not None:
            raise review_form.PMReviewError(
                "pending", f"reviewer ordinal={section.ordinal}은 이미 finding-zero 판정됨",
            )
        payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_FINDING_ZERO_PAYLOAD_KEYS, {
            "version": review_form.PM_REVIEW_DISPOSITION_VERSION,
            review_form.PM_REVIEW_DISPOSITION_ROLE_KEY: section.role,
            "reviewer_ordinal": section.ordinal,
            "finding_zero": "accepted",
        })
    else:
        pending_ids = [
            finding_id for finding_id in finding_ids
            if finding_id not in existing_by_id
        ]
        if not pending_ids:
            raise review_form.PMReviewError(
                "pending", f"reviewer ordinal={section.ordinal}의 미판정 finding이 없습니다",
            )
        decision_placeholder = "<accepted|rejected>"
        dispositions = [
            review_form._pm_review_seed_object(
                review_form.PM_REVIEW_DISPOSITION_KEYS,
                existing_by_id.get(finding_id, {
                    "id": finding_id,
                    "decision": decision_placeholder,
                }),
            )
            for finding_id in finding_ids
        ]
        payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_DISPOSITION_PAYLOAD_KEYS, {
            "version": review_form.PM_REVIEW_DISPOSITION_VERSION,
            review_form.PM_REVIEW_DISPOSITION_ROLE_KEY: section.role,
            "reviewer_ordinal": section.ordinal,
            "dispositions": dispositions,
        })
    return f"```{review_form.PM_REVIEW_DISPOSITION_BLOCK}\n" + review_form._pm_review_render_json(payload) + "\n```\n"


def render_pm_review_delta(ticket: str, delta: review_form.PMReviewDelta) -> str:
    """developer prompt에 붙이는 accepted-only 최소 renderer. 0건이면 빈 문자열.

    심각도는 블록이 단일 진실이라 여기서 그대로 표기한다 — dev 는 산문을 다시 읽지 않고 이
    렌더에서 우선순위를 안다.
    """
    if not delta.accepted:
        return ""
    lines = [f"## PM 승인 리뷰 delta — {ticket}", ""]
    for finding, disposition in delta.accepted:
        lines.extend([
            f"### {finding.id}",
            f"- 채널: {finding.reviewer_role}",
            f"- 심각도: {finding.severity or review_form.PM_REVIEW_SEVERITY_UNSPECIFIED_LABEL}",
            f"- 분류: {finding.classification}",
            f"- 권위 근거: {finding.authority}",
            f"- 관측 증거: {finding.evidence}",
            f"- reviewer 권고: {finding.recommendation}",
            f"- 설계 변경 여부: {'true' if finding.design_change else 'false'}",
            f"- PM 판정 근거: {disposition.reason}",
            f"- 허용 수정 범위: {disposition.scope}",
        ])
        if disposition.prerequisite:
            lines.append(f"- 선행 권위: {disposition.prerequisite}")
        lines.append("")
    body = "\n".join(lines).rstrip()
    return f"{body}\n\n{delegate_prompt_rules.PM_REVIEW_FIX_SCOPE_NOTICE}"


# ── verify-template — 기계 확인 대상 분류 + PM 확인 골격 ────────────


def render_pm_review_verify_template(template: review_state.PMReviewVerifyTemplate) -> str:
    """기계 확인 대상 행의 PM 확인 골격 — source round 별 블록을 순번 오름차순으로 낸다.

    `id`·`command` 는 그대로 옮기고 `status`·`observed` 는 자리표시자다(no-hand-retyping).
    `expected` 는 판정 참고용 안내 줄로만 보인다 — 확인 블록 스키마 자체에는 없는 필드다(단일
    진실은 verify 행 · 불변식 5 의 정신 상속).

    블록이 여럿인 이유는 확인 블록의 `round` 가 **그 행이 실린 developer 라운드**에 결속되기
    때문이다. 오름차순은 파서의 전역 단조 증가 규칙과 같은 순서라, 낸 순서대로 명세에 붙이면
    그대로 수용된다(왕복 정합).
    """
    if not template.machine_rows:
        return ""
    grouped: dict[int, list[review_form.PMReviewVerifyRow]] = {}
    for source_round, row in template.machine_rows:
        grouped.setdefault(source_round, []).append(row)
    blocks: list[str] = []
    for source_round in sorted(grouped):
        lines = [f"## 기계 확인 대상 — developer round {source_round}", ""]
        for row in grouped[source_round]:
            lines.append(f"- {row.id}: `{row.command}` → expected: {row.expected}")
        lines.append("")
        confirmations = [
            review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS, {
                "id": row.id,
                "status": "<" + "|".join(review_form.PM_REVIEW_CONFIRMATION_STATES) + ">",
                "command": row.command,
                "observed": "<관측값>",
            })
            for row in grouped[source_round]
        ]
        payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS, {
            "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
            "round": source_round,
            "confirmations": confirmations,
        })
        lines.append(
            review_form._pm_review_fenced_json(review_form.PM_REVIEW_CONFIRMATION_BLOCK, payload).rstrip("\n")
        )
        blocks.append("\n".join(lines) + "\n")
    return "\n".join(blocks)


# ── 확인 커맨드 실행 — 엔진이 돌리고 엔진이 기입한다 ────────────────
# 확인 골격(`render_pm_review_verify_template`)이 내던 자리표시자 두 칸(`status`·`observed`)을
# 엔진이 실행 결과로 채운다. 사람이 커맨드를 옮겨 치고 관측값을 옮겨 적는 왕복이 사라지므로
# 옮겨 적기 drift 도 사라진다. 판정은 여전히 파서가 소유한다 — 여기서는 `expected ⊂ observed`
# 와 rc 를 그대로 재현할 뿐 완화하지 않는다(비-0 rc 는 통과로 접지 않는다).
#
# 안전 경계는 신설하지 않는다: 커맨드는 이미 "금지 토큰 없는 단일 명령"(불변식 12)이라
# `shell=False` + 인자 분해로 그대로 실행된다. 셸 해석을 열면 그 경계가 무의미해진다.
PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC = 1800
# 관측값 발췌 상한 — 명세에 그대로 실리는 값이라 회귀 전문을 담지 않는다. 기대값이 실제로
# 관측됐다면 그 자리를 중심으로 자르고(판정과 기입이 갈리지 않게), 아니면 꼬리를 남긴다
# (실패 진단은 대개 끝에 있다).
PM_REVIEW_CONFIRMATION_OBSERVED_LIMIT = 2000
_PM_REVIEW_OBSERVED_ELLIPSIS = "…"
# 명세 PM 영역에 기입하는 절 제목 — PM 관행이던 문자열을 **엔진 소유 상수**로 승격한다.
# 엔진이 그 절을 쓰기 시작하는 순간 제목은 관행이 아니라 기계 표면이다.
PM_REVIEW_CONFIRMATION_SECTION = "## PM 기계 확인"


def _pm_review_observed_excerpt(text: str, expected: str) -> str:
    """관측값 발췌 — 기대값이 있으면 그 자리를 포함하도록 자른다.

    판정(전문 기준)과 기입(발췌)이 갈리면 엔진이 resolved 로 적은 행을 파서가 malformed 로
    거부한다(왕복 정합 위반). 그래서 자르는 자리는 기대값 위치가 정한다.
    """
    limit = PM_REVIEW_CONFIRMATION_OBSERVED_LIMIT
    if len(text) <= limit:
        return text
    index = text.find(expected) if expected else -1
    if index < 0:
        return _PM_REVIEW_OBSERVED_ELLIPSIS + text[-limit:]
    end = min(len(text), max(index + len(expected), index + limit))
    start = max(0, end - limit)
    prefix = _PM_REVIEW_OBSERVED_ELLIPSIS if start > 0 else ""
    suffix = _PM_REVIEW_OBSERVED_ELLIPSIS if end < len(text) else ""
    return prefix + text[start:end] + suffix


def run_pm_review_confirmation_command(
    command: str, *, cwd: Path, expected: str,
    timeout: int = PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC,
    run_fn: Callable | None = None,
) -> tuple[str, str]:
    """확인 커맨드 1회 실행 → `(status, observed)`.

    실행 실패(스폰 불가·타임아웃·비-0 rc)도 관측값에 실어 `unresolved` 로 남긴다 — 실행이
    안 됐다는 사실을 통과로 접으면 기계 확인이 확인이 아니게 된다.
    """
    review_form._pm_review_assert_verify_command_shape(command, "confirmation.command")
    argv = shlex.split(command)
    if not argv:
        raise review_form.PMReviewError("malformed", f"확인 커맨드가 비어 있습니다: {command!r}")
    runner = run_fn or subprocess.run
    try:
        result = runner(
            argv, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return "unresolved", f"rc=timeout({timeout}s)"
    except OSError as exc:
        return "unresolved", f"rc=실행 실패({type(exc).__name__}: {exc})"
    returncode = int(getattr(result, "returncode", 1))
    output = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
    status = "resolved" if returncode == 0 and expected and expected in output else "unresolved"
    return status, f"rc={returncode}\n" + _pm_review_observed_excerpt(output, expected)


def run_pm_review_confirmations(
    template: review_state.PMReviewVerifyTemplate, *, cwd: Path,
    run_fn: Callable | None = None,
) -> tuple[tuple[int, review_form.PMReviewMachineConfirmation], ...]:
    """확인 가능한 행 전부를 실행해 확인 행으로 만든다(source round 오름차순 보존)."""
    rows: list[tuple[int, review_form.PMReviewMachineConfirmation]] = []
    for source_round, verify_row in template.machine_rows:
        status, observed = run_pm_review_confirmation_command(
            verify_row.command, cwd=cwd, expected=verify_row.expected,
            timeout=PM_REVIEW_CONFIRMATION_COMMAND_TIMEOUT_SEC, run_fn=run_fn,
        )
        rows.append((source_round, review_form.PMReviewMachineConfirmation(
            verify_row.id, status, verify_row.command, observed, source_round,
        )))
    for source_round, verify_row in template.pm_owned_rows:
        rows.append((source_round, review_form.PMReviewMachineConfirmation(
            verify_row.id,
            "resolved",
            review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND,
            verify_row.expected,
            source_round,
        )))
    rows.sort(key=lambda item: item[0])
    return tuple(rows)


def render_pm_review_confirmation_section(
    rows: Sequence[tuple[int, review_form.PMReviewMachineConfirmation]],
) -> str:
    """실행 결과를 명세 PM 영역에 붙일 절로 렌더한다 — 자리표시자 0.

    블록을 source round 별로 나누는 규칙·오름차순은 확인 골격(`render_pm_review_verify_template`)
    과 같다 — 파서의 전역 단조 증가 규칙과 같은 순서라 낸 순서대로 붙으면 그대로 수용된다.
    """
    if not rows:
        return ""
    grouped: dict[int, list[review_form.PMReviewMachineConfirmation]] = {}
    for source_round, row in rows:
        grouped.setdefault(source_round, []).append(row)
    sections: list[str] = []
    for source_round in sorted(grouped):
        sections.append(
            f"{PM_REVIEW_CONFIRMATION_SECTION} (developer round {source_round})\n\n"
            + _render_pm_review_confirmation_block(source_round, grouped[source_round])
        )
    return "\n".join(sections)


def _render_pm_review_confirmation_block(
    source_round: int, rows: Sequence[review_form.PMReviewMachineConfirmation],
) -> str:
    """한 developer round의 confirmation fence — 새 append와 기존 block merge 공용."""
    payload = review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS, {
        "version": review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,
        "round": source_round,
        "confirmations": [
            review_form._pm_review_seed_object(review_form.PM_REVIEW_MACHINE_CONFIRMATION_ROW_KEYS, {
                "id": row.id,
                "status": row.status,
                "command": row.command,
                "observed": row.observed,
            })
            for row in rows
        ],
    })
    return review_form._pm_review_fenced_json(review_form.PM_REVIEW_CONFIRMATION_BLOCK, payload)


def _pm_review_confirmation_blocks_for_merge(
    text: str, *, confirmation_only: bool,
) -> tuple[tuple[review_form._PMReviewBlock, int, tuple[review_form.PMReviewMachineConfirmation, ...]], ...]:
    """confirmation block을 merge 가능한 strict 행으로 읽는다."""
    parsed: list[tuple[review_form._PMReviewBlock, int, tuple[review_form.PMReviewMachineConfirmation, ...]]] = []
    for block in review_form._pm_review_json_blocks(text):
        if block.kind != review_form.PM_REVIEW_CONFIRMATION_BLOCK:
            if confirmation_only:
                raise review_form.PMReviewError(
                    "malformed",
                    f"confirmation append 입력에 다른 review block이 있습니다: {block.kind}",
                )
            continue
        value = block.value
        review_form._pm_review_exact_keys(
            value, review_form.PM_REVIEW_MACHINE_CONFIRMATION_PAYLOAD_KEYS,
            review_form.PM_REVIEW_CONFIRMATION_BLOCK,
        )
        review_form._pm_review_version(
            value, review_form.PM_REVIEW_CONFIRMATION_BLOCK,
            allowed=(review_form.PM_REVIEW_MACHINE_CONFIRMATION_VERSION,),
        )
        source_round = value.get("round")
        if (
            not isinstance(source_round, int) or isinstance(source_round, bool)
            or source_round < 1
        ):
            raise review_form.PMReviewError(
                "malformed", "confirmation.round은 1 이상 정수여야 합니다",
            )
        raw_rows = value.get("confirmations")
        if not isinstance(raw_rows, list):
            raise review_form.PMReviewError("malformed", "confirmations는 JSON array여야 합니다")
        rows = tuple(
            review_form._pm_review_parse_machine_confirmation_row(
                raw, round_ordinal=source_round,
            )
            for raw in raw_rows
        )
        ids = [row.id for row in rows]
        if len(ids) != len(set(ids)):
            raise review_form.PMReviewError(
                "malformed", f"{review_form.PM_REVIEW_CONFIRMATION_BLOCK} round={source_round} ID 중복",
            )
        parsed.append((block, source_round, rows))
    if confirmation_only and not parsed:
        raise review_form.PMReviewError("malformed", "append할 confirmation block이 없습니다")
    return tuple(parsed)


def _merge_pm_review_confirmation_section(
    body: str, section: str, *, rounds: Sequence,
) -> tuple[str, bool]:
    """같은 developer round block에는 빠진 strict PM-owned 행만 원자 병합한다."""
    existing = _pm_review_confirmation_blocks_for_merge(
        body, confirmation_only=False,
    )
    incoming = _pm_review_confirmation_blocks_for_merge(
        section, confirmation_only=True,
    )
    existing_rounds = [source_round for _block, source_round, _rows in existing]
    incoming_rounds = [source_round for _block, source_round, _rows in incoming]
    if len(existing_rounds) != len(set(existing_rounds)):
        raise review_form.PMReviewError(
            "malformed", f"기존 confirmation round 중복: {existing_rounds}",
        )
    if len(incoming_rounds) != len(set(incoming_rounds)):
        raise review_form.PMReviewError(
            "malformed", f"append confirmation round 중복: {incoming_rounds}",
        )

    by_round = {
        source_round: (block, rows)
        for block, source_round, rows in existing
    }
    replacements: list[tuple[int, int, str]] = []
    additions: list[tuple[int, review_form.PMReviewMachineConfirmation]] = []
    changed = False
    for _incoming_block, source_round, rows in incoming:
        current = by_round.get(source_round)
        if current is None:
            additions.extend((source_round, row) for row in rows)
            changed = True
            continue
        block, existing_rows = current
        merged = list(existing_rows)
        existing_by_id = {row.id: row for row in existing_rows}
        round_changed = False
        for row in rows:
            present = existing_by_id.get(row.id)
            if present is not None:
                if present != row:
                    raise review_form.PMReviewError(
                        "malformed",
                        f"confirmation round={source_round} id={row.id} 기존 행과 충돌합니다",
                    )
                continue
            if not (
                row.status == "resolved"
                and row.command == review_form.PM_REVIEW_PM_OWNED_CONFIRMATION_COMMAND
            ):
                raise review_form.PMReviewError(
                    "malformed",
                    f"confirmation round={source_round} 기존 block에는 빠진 strict PM-owned "
                    f"terminal 행만 병합할 수 있습니다: {row.id}",
                )
            merged.append(row)
            existing_by_id[row.id] = row
            round_changed = True
        if round_changed:
            replacements.append((
                block.start, block.end,
                _render_pm_review_confirmation_block(source_round, merged),
            ))
            changed = True

    merged_body = body
    for start, end, replacement in sorted(replacements, reverse=True):
        merged_body = merged_body[:start] + replacement + merged_body[end:]
    if additions:
        merged_body = (
            merged_body.rstrip("\n") + "\n\n"
            + render_pm_review_confirmation_section(additions)
        )
    # merge 뒤 full parser가 disposition/verify 이중 결속과 spoof를 최종 판정한다. write는 이
    # 검증 뒤라 conflict/malformed가 원본 일부를 바꾸는 창이 없다.
    review_state.parse_pm_review_delta(merged_body, rounds)
    return merged_body, changed


def append_pm_review_confirmation(
    pm_home: Path, ticket: str, section: str, *, rounds: Sequence | None = None,
) -> Path:
    """확인 절을 티켓 명세 PM 영역에 append/동일 round 병합하고 board-git 에 싣는다.

    명세 쓰기 seam 은 새로 만들지 않는다 — 기존 조합(`ticket_store.load_ticket` → board 의
    `dump_ticket_atomic` → `_rounds_mutation_sync_paths`)을 그대로 쓴다.
    """
    home = pm_paths.home_at(Path(pm_home).resolve())
    changed = False
    with fs.board_lock(home):
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
        _status, path = found
        fm, body = ticket_store.load_ticket(path)
        if rounds is None:
            rounds = round_seed.load_rounds(
                pm_paths.tickets_dir(home), ticket, ticket_text=body,
            )
        merged, changed = _merge_pm_review_confirmation_section(
            body, section, rounds=rounds,
        )
        if changed:
            ticket_store.dump_ticket_atomic(path, fm, merged)
    if changed:
        round_maintenance._rounds_mutation_sync_paths(home, f"pm-review confirmation {ticket}", (path,))
    return path


# ── 설정 ──────────────────────────────────────────────────────────────────

_CONFIG_REPO_OVERRIDE: Path | None = None


def _delegate_task_env(cwd: Path) -> dict[str, str]:
    """위임 하네스에 실을 슬롯 주인 task — 위임 세션은 세션 ID 가 따로라 슬롯 쓰기 가드가 이 값으로 판정한다.

    `cwd` 가 task 가 잡은 슬롯이면 `{PM_DELEGATE_TASK: <task>}` 다. 슬롯 밖이거나 task 가 잡지 않은
    슬롯이면 빈 dict 다 — 그 위임의 슬롯 쓰기는 가드가 거부한다(`board.judge_tool_input`)."""
    found = hooks.slot_containing(REPO, cwd)
    if found is None:
        return {}
    row, tasks = found
    owner = row.get("session")
    if row.get("state") != "leased" or not any(task.get("name") == owner for task in tasks):
        return {}
    return {hooks.SLOT_WRITE_DELEGATE_TASK_ENV: owner}


def _local_conf_path(repo: Path | None = None) -> Path:
    """위임 profile을 해소한 엔진 사본 local.conf의 절대경로(provenance 단일 입력)."""
    return ((repo if repo is not None else REPO) / ".project_manager" / "local.conf").resolve()


def check_local_conf_divergence(
    cwd: Path, conf: dict[str, str], role: str, tier: str, *,
    config_repo: Path,
    cli_override: bool = False,
    compare_fallback: bool = False,
) -> tuple[Path | None, object | None]:
    """기존 `--cwd` repo축으로 이번 role/tier의 **유효 primary/fallback 프로필** 분기를 검사한다.

    additional_reviewer의 repo-root/conf 비교 seam을 재사용해 두 표면이 별도 판정 규칙을 갖지 않게 한다.
    양쪽 모두 `resolve_delegate`를 거쳐 reasoning 미지정(None)까지 포함한 완전 tuple을 비교하므로,
    한쪽만 키를 생략한 실제 수신 프로필 차이도 잡는다. 대상 역할/티어가 불완전하거나 잘못돼
    해소되지 않으면 실행 가능한 비교 대상이 아니므로 skip한다. CLI 완전지정은 local.conf를 primary
    프로필 입력으로 쓰지 않는 원자 override라 divergence만 명시 skip한다. fallback은 CLI override와
    primary 동일 skip을 해소한 뒤 실제 발동 가능한 실행에서만 같은 role/tier를
    비교한다. 또한 양쪽 `resolve_fallback` 결과가 모두 tuple이고 각 후보의 primary와 harness/model이
    다를 때만 값 차이를 판정한다. 한쪽 미설정·primary 동일은 실제 발동 불가능하므로 무소음이며,
    불완전/잘못된 대상 tuple도 실행 가능한 비교값이 아니어서 skip한다. 반환 target_repo는
    이 skip들과 무관하게 write-target 재앵커에도
    재사용한다.
    """
    target_repo = pm_paths.repo_root_from_cwd(cwd)
    if cli_override:
        return target_repo, None

    prefix = f"delegate.{role}" + (".hard" if tier == "hard" else "")

    def effective_profile(candidate: dict[str, str]) -> dict[str, object]:
        try:
            harness, model, reasoning = delegate_config.resolve_delegate(
                candidate, role, tier, None, None, None,
            )
        except delegate_errors.DelegateError:
            return {}
        values: dict[str, object] = {
            f"{prefix}.harness": harness,
            f"{prefix}.model": model,
            f"{prefix}.reasoning": reasoning,
        }
        if compare_fallback:
            try:
                fallback = delegate_config.resolve_fallback(candidate, role, tier)
            except delegate_errors.DelegateError:
                return values
            # main의 비발동 규칙과 후보별로 대칭이다. reasoning만 달라도 같은 harness/model 한도를
            # 재타격하므로 유효 fallback이 아니며, 한쪽만 유효하면 공통 비교 키가 생기지 않는다.
            if (
                fallback is not None
                and (fallback[0], fallback[1]) != (harness, model)
            ):
                for field, value in zip(
                    ("harness", "model", "reasoning"), fallback,
                ):
                    values[f"{prefix}.fallback.{field}"] = value
        return values

    divergence = reviewer_target.local_conf_divergence(
        engine_repo=config_repo,
        engine_conf=conf,
        target_repo=target_repo,
        selector=effective_profile,
        engine_label="pm-home",
        target_label="cwd-worktree",
    )
    return target_repo, divergence


def check_write_target_reanchor(role: str, cwd: Path, prompt: str) -> Path | None:
    """write 역할이 PM 홈 cwd 에서 엔진 코드(import 사본)를 write 타깃하면 재앵커 대상 worktree 반환.

    재앵커는 cwd 자체가 아니라 **쓰기-타깃 axis** 로 판정 — PM-doc(wiki/ADR/spike) 작업은 PM 홈 cwd
    정당. 판정 = additional_reviewer `_pm_home_reanchor`(실 board 소유 + `work/*` canonical 보유·파일 존재
    휴리스틱 금지) 재사용. read 역할·비-엔진-코드 타깃·PM 홈 아닌 cwd 는 None(통과)."""
    if role not in delegate_run_rules.WRITE_ROLES:
        return None
    if not delegate_prompt_rules._prompt_targets_engine_code(prompt):
        return None
    return pm_paths._pm_home_reanchor(cwd)


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


def _resume_unavailable(reason: str) -> None:
    """재사용 불가 사유를 1줄로 알리고 fresh 로 간다(조용한 강등 금지·차단은 아님)."""
    print(
        f"세션 재사용 미적용: {reason} — fresh 스폰 + full payload 로 진행합니다.",
        file=sys.stderr,
    )


def resolve_resume_plan(
    selector: str | None,
    *,
    harness: str,
    role: str,
    task_text: str,
    output_dir: Path | None,
) -> delegate_run_rules.ResumePlan | None:
    """`--resume-from` 지시자를 확정된 재사용 1건으로 해소한다(불가하면 None + loud 1줄).

    막히는 자리는 넷이고 전부 **비차단**이다: 재개 미지원 하네스, 장부 부재, 후보 부재(보존 창
    밖·다른 role·미마감/실패 레코드), 세션 id 형식 불일치. 어느 쪽이든 fresh + full payload 로
    진행한다.

    장부 부재는 **락을 잡기 전에** 끝낸다 — 조회는 읽기인데 락 획득이 장부 상위 디렉터리와
    `.lock` 파일을 만든다(`fs.exclusive_file_lock`). 한 번도 위임하지 않은 트리에서
    `--dry-run` 이 `.project_manager/.local/` 을 새로 만드는 건 미리보기의 부작용 0 원칙 위반이다.
    """
    if not selector:
        return None
    if not contract.harness_supports_resume(harness):
        _resume_unavailable(f"{harness} 하네스는 재개 argv 가 미검증(선언표 미지원)")
        return None
    _raw_dir, ledger_path = raw_output._raw_storage(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), output_dir)
    if not ledger_path.is_file():
        _resume_unavailable(f"raw 장부 없음({ledger_path}) — 이 위치에 아직 위임 기록이 없음")
        return None
    try:
        rows = raw_output.raw_records(ledger_path)
    except (OSError, ValueError) as exc:
        _resume_unavailable(f"raw 장부를 읽지 못함({exc})")
        return None
    record = delegate_run_rules.select_resume_record(
        rows, selector=selector, role=role, harness=harness,
    )
    if record is None:
        # 사유를 갈라 낸다 — "정말 없다"와 "있는데 이어받을 수 없다"는 처방이 다르다.
        _resume_unavailable(
            delegate_run_rules.resume_unusable_reason(rows, selector=selector, role=role, harness=harness)
            or (f"{selector!r} 의 성공 마감 위임 레코드(role={role}·harness={harness}) 없음 — "
                "장부 보존 창 밖이거나 아직 없는 라운드")
        )
        return None
    session_id = record.get(delegate_run_rules.RESUME_FIELD_SESSION_ID)
    if not contract.is_resumable_session_id(session_id):
        _resume_unavailable(
            f"레코드 {record.get('id')} 의 세션 id 형식 불일치({session_id!r})"
        )
        return None
    must_fix_items = record.get(delegate_run_rules.RESUME_FIELD_MUST_FIX)
    base_rev = record.get(delegate_run_rules.RESUME_FIELD_BASE_REV)
    record_ticket = record.get(delegate_run_rules.RESUME_FIELD_TICKET)
    return delegate_run_rules.ResumePlan(
        session_id=session_id,
        record_id=str(record.get("id")),
        ticket=record_ticket if isinstance(record_ticket, str) and record_ticket else None,
        delta_prompt=delegate_run_rules.build_resume_delta_payload(
            must_fix_items=(
                must_fix_items if isinstance(must_fix_items, list) else ()
            ),
            base_rev=base_rev if isinstance(base_rev, str) else None,
            task_text=task_text,
        ),
    )


def _execute_attempt(
    *,
    harness: str,
    model: str,
    reasoning: str | None,
    role: str,
    cwd: Path,
    prompt: str,
    timeout: int,
    output_dir: Path | None,
    run_fn: Callable,
    attempt: str,
    primary_raw: str | None = None,
    local_conf_path: Path | None = None,
    profile_source: str = "local-conf",
    resume_session_id: str | None = None,
    ticket: str | None = None,
    fresh_reason: str | None = None,
    base_rev: str | None = None,
    cluster_run_dir: Path | None = None,
    run_id: str | None = None,
    round_copy_path: str | None = None,
) -> delegate_run.DelegateAttempt:
    """하네스 1회를 실행하고 raw를 박제한다.

    폴백도 같은 드라이버/권한축/env allowlist/timeout 계약을 탄다(그래서 폴백이 발동한 실행의 최악
    소요는 두 시도의 하네스별 예산 합이고, 세션 재사용 라운드는 primary 축을 두 번 쓸 수
    있다·_harness_timeout_budget). opencode의 합성 prompt-file은
    attempt마다 만들고 즉시 정리한다. DI run_fn이 예외를 직접 raise해도 _default_run_fn과 **같은
    분류 신호**로 정규화한다 — 스폰 단계 예외(_LAUNCH_STAGE_ERRORS)만 RUN_RESULT_LAUNCH_FAILED 이고,
    그 밖의 OSError는 호출 후일 수 있어 미분류 실패로 남긴다(폴백 금지·중복 호출 차단).

    `resume_session_id`/`ticket`/`base_rev` 는 이 실행의 **장부 구조화 필드**다. 재개 id 는 지원
    선언표를 통과한 축에서만 받는다(미지원 축으로 미검증 argv 가 나가지 않는다).

    `run_id`/`round_copy_path` 는 cross 라운드 준비가 있었던 실행에서만 채워진다 — 있으면
    delegate-rounds 장부의 같은 이름 필드와 값이 같다. native 위임은 라운드 준비 자체가 없어
    이 실행 경로를 타지 않는다.

    이 함수 호출부(`_execute_and_collect`)는 한 run 에서 최대 3회 이 함수를 부른다(primary·
    세션 재사용 불일치 뒤 fresh 재실행·인프라 실패 폴백) — 셋 다 같은 `run_id`/`round_copy_path`
    를 싣는다(raw 행 → 예약은 many-to-one). 종료 판정 입력은 "결속된 행 중 아무 하나의 마감"이
    아니라 **가장 최신 attempt 행의 마감**이다 — primary 가 실패로 마감되고 폴백이 진행 중인
    창에서 전자를 쓰면 아직 도는 run 을 끝난 것으로 오판한다.
    """
    if resume_session_id is not None and not contract.harness_supports_resume(harness):
        raise delegate_errors.DelegateError(
            f"{harness} 하네스는 세션 재사용 미지원인데 재개 id 가 전달됐다 — "
            "미검증 argv 를 만들지 않는다."
        )
    # 슬롯 주인 task 는 준비 산출물을 만들기 전에 해소한다 — 장부 판독 실패가 되감을 산출물을 남기지 않는다.
    delegate_task_env = _delegate_task_env(cwd)
    argv, stdin_text, prompt_path, read_tmp = delegate_transport._prepare_attempt_transport(
        pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness, model, reasoning, role, cwd, prompt, resume_session_id,
        cluster_run_dir,
    )
    # env 해소도 실패할 수 있다(하네스 필수 키 누락은 fail-loud) — 그 실패가 attempt 사본과
    # read tmp 를 남기지 않도록 준비 산출물과 같은 소유권 seam 에서 되감는다.
    try:
        env = contract.with_harness_runtime_role(
            delegate_transport._apply_read_tmp_env(delegate_env.build_env(harness), harness, read_tmp), harness, role,
        )
        env.update(delegate_task_env)
    except BaseException:
        delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)
        raise

    # raw 경로와 미마감 장부는 자식 프로세스 실행 전에 확정한다. 이 순서가 하네스 Bash/stdout
    # 유실 시에도 경로를 남기는 핵심 계약이다. 이후 BaseException으로 마감 경로를 건너뛰면
    # 레코드가 의도적으로 미마감 상태로 남아 kill/비정상 종료의 조회 입력이 된다.
    try:
        raw_path = raw_output._reserve_raw_output(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), harness, output_dir)
        _raw_dir, ledger_path = raw_output._raw_storage(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE), output_dir)
        record_id = raw_output.start_raw_record(
            ledger_path,
            surface=delegate_run_rules.DELEGATE_RAW_SURFACE,
            harness=harness,
            model=model,
            role=role,
            raw_path=raw_path,
            attempt=attempt,
            # 실행 **전**에 아는 구조화 필드 — 다음 라운드의 후보 선택(ticket)과 delta 기준(rev),
            # 그리고 이 실행이 어떤 세션을 이어받으려 했는지가 장부 한 행에 남는다. opencode는
            # PM 홈 감사 raw와 cwd wire 사본 좌표를 같은 행에 두어 조인 없이 회수한다.
            extra={
                key: value for key, value in (
                    (delegate_run_rules.RESUME_FIELD_TICKET, ticket),
                    (delegate_run_rules.RESUME_FIELD_BASE_REV, base_rev),
                    (delegate_run_rules.RESUME_FIELD_RESUME_FROM, resume_session_id),
                    (delegate_run_rules.FRESH_REASON_FIELD, fresh_reason),
                    # cross 라운드 준비가 있었던 실행만 채운다 — delegate-rounds 장부 행과
                    # 값이 같아야 조인이 성립한다.
                    (delegate_run_rules.RESUME_FIELD_RUN_ID, run_id),
                    (delegate_run_rules.RESUME_FIELD_TICKET_COPY, round_copy_path),
                    (
                        delegate_run_rules.OPENCODE_TRANSPORT_PROMPT_FIELD,
                        str(prompt_path) if prompt_path is not None else None,
                    ),
                ) if value is not None
            },
        )
    except BaseException:
        # transport 준비 뒤 raw 예약/장부 시작이 실패해도 지시 사본을 남기지 않는다.
        delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)
        raise
    started = time.monotonic()

    def _record_pre_spawn_rejection(exc: delegate_errors.DelegateError) -> None:
        """예측 가능한 runner 전 거부를 kill/비정상 종료와 구분되는 마감 레코드로 남긴다."""
        rejection_elapsed = time.monotonic() - started
        rejection_stderr = (
            f"pm_delegate pre-spawn rejection: {type(exc).__name__}: {exc}"
        )
        if prompt_path is not None and prompt_path.launch_binding_mode is None:
            prompt_path.launch_binding_mode = "fail-closed"
        # raw 쓰기 자체가 실패해도 finally에서 장부는 반드시 마감한다. 그래야 0-byte raw가
        # 남더라도 finished_at/rc가 kill 증거와 pre-spawn 거부를 기계적으로 구분한다.
        try:
            raw_output._write_reserved_raw(
                raw_path,
                delegate_config._format_meta(
                    argv, 1, harness, model, rejection_elapsed, "", rejection_stderr,
                    attempt=attempt,
                    primary_raw=primary_raw,
                    reasoning=reasoning,
                    local_conf_path=local_conf_path,
                    profile_source=profile_source,
                    silence_sec=None,
                    idle_killed=False,
                    transport_sandbox_path=(
                        str(prompt_path.sandbox) if prompt_path is not None else None
                    ),
                    transport_prompt_path=(
                        str(prompt_path) if prompt_path is not None else None
                    ),
                    transport_binding_mode=(
                        prompt_path.launch_binding_mode
                        if prompt_path is not None else None
                    ),
                ),
            )
        finally:
            try:
                raw_output.finish_raw_record(
                    ledger_path,
                    record_id,
                    rc=1,
                    elapsed_sec=rejection_elapsed,
                    silence_sec=None,
                    finish_note="pre-spawn rejection",
                    extra={"pre_spawn_rejected": True},
                )
            except raw_output.RawRecordAlreadyFinished as finish_exc:
                print(
                    f"경고: 장부 마감 충돌 — {finish_exc} "
                    "(수동 마감 보존·pre-spawn 거부 raw는 기록 시도됨)",
                    file=sys.stderr,
                )

    try:
        try:
            launch_argv = argv
            launch_cwd = str(
                read_tmp.path
                if (
                    read_tmp is not None
                    and delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
                )
                else cwd
            )
            if (
                read_tmp is not None
                and delegate_run_rules._READ_TMP_REANCHOR_EXEC_ROOT_BY_HARNESS[harness]
            ):
                # codex read 역할만 -C를 격리 tmp로 재앵커한다 — 그 tmp는 절대 git 저장소가
                # 아니므로 여기서 다시 rev-parse해 봐야 무의미하다. 재앵커 **이전**의 --cwd 자신을
                # 스폰 전에 검증한다(`_preflight_codex_read_exec_root` 선언부 주석 참고).
                try:
                    delegate_transport._preflight_codex_read_exec_root(cwd)
                except delegate_errors.DelegateError as exc:
                    _record_pre_spawn_rejection(exc)
                    raise
            if prompt_path is not None:
                # raw 예약/장부 시작처럼 준비 뒤 실행 전에 낀 작업까지 포함해 lexical
                # containment와 symlink 아닌 entry를 다시 확인한다.
                try:
                    launch_argv, launch_cwd = delegate_transport._opencode_transport_launch_target(
                        prompt_path, argv,
                    )
                except delegate_errors.DelegateError as exc:
                    _record_pre_spawn_rejection(exc)
                    raise
                # raw 감사와 DelegateAttempt도 runner가 실제 받은 argv를 기록한다. 준비 lexical
                # 경로는 OPENCODE_TRANSPORT_PROMPT_FIELD에 별도로 남아 cleanup 진단과 조인된다.
                argv = launch_argv
            result = run_fn(
                argv, stdin_text=stdin_text, cwd=launch_cwd,
                env=env,
                timeout=timeout, harness=harness,
            )
        except delegate_run._LAUNCH_STAGE_ERRORS as exc:
            result = delegate_run._launch_failure_result(harness, exc)
        except OSError as exc:
            result = delegate_run._midrun_failure_result(harness, exc)
    finally:
        elapsed = time.monotonic() - started
        # 이 finally는 runner 예외와 watchdog timeout/child-kill을 모두 회수한다. 다만 OS kill -9,
        # host crash 등 pm_delegate 프로세스 자체가 강제 종료되면 사용자 공간 finally는 실행될 수
        # 없으므로 그 경우의 잔여 0까지 보장하지 않는다.
        cleanup_residue = delegate_temp._cleanup_attempt_transport(prompt_path, read_tmp)

    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    stderr = result.get("stderr", "")
    if cleanup_residue is not None:
        # 잔존 트리를 남긴 실행은 실패다 — 경고만 찍고 rc=0 으로 끝내면 200MB 단위 잔재가
        # 저장소에 쌓이는 동안 호출부는 정상으로 읽는다. 위 `finally` 안에서 raise 하면 주
        # 결과와 전파 중인 예외를 덮으므로, 여기서 결과 자체를 실패로 내린다(분류 신호는
        # 붙이지 않는다 — 인프라 실패가 아니므로 폴백 대상이 아니다).
        rc = rc if rc != 0 else delegate_run_rules._READ_TMP_RESIDUE_RC
        stderr = f"{stderr}\n{cleanup_residue}" if stderr else cleanup_residue
        result = {**result, "returncode": rc, "stderr": stderr}
    raw_output._write_reserved_raw(
        raw_path,
        delegate_config._format_meta(
            argv, rc, harness, model, elapsed, stdout, stderr, attempt=attempt,
            primary_raw=primary_raw, reasoning=reasoning, local_conf_path=local_conf_path,
            profile_source=profile_source,
            silence_sec=result.get(delegate_run_rules.RUN_RESULT_SILENCE_SEC),
            idle_killed=bool(result.get(delegate_run_rules.RUN_RESULT_IDLE_KILLED, False)),
            transport_sandbox_path=(
                str(prompt_path.sandbox) if prompt_path is not None else None
            ),
            transport_prompt_path=(
                str(prompt_path) if prompt_path is not None else None
            ),
            transport_binding_mode=(
                prompt_path.launch_binding_mode
                if prompt_path is not None else None
            ),
        ),
    )
    observed = delegate_run._observe_harness_result(harness, stdout)
    try:
        raw_output.finish_raw_record(
            ledger_path,
            record_id,
            rc=rc,
            elapsed_sec=elapsed,
            silence_sec=result.get(delegate_run_rules.RUN_RESULT_SILENCE_SEC),
            extra=delegate_run._finished_ledger_fields(observed, resume_session_id),
        )
    except raw_output.RawRecordAlreadyFinished as exc:
        # 수동 `raw close`(--force) 가 먼저 마감한 충돌 — 첫 마감을 보존하고 실행 결과는
        # 실패로 바꾸지 않는다(회신은 raw 파일에 이미 박제됨).
        print(f"경고: 장부 마감 충돌 — {exc} (수동 마감 보존·회신은 raw 파일 참조)",
              file=sys.stderr)
    return delegate_run.DelegateAttempt(
        harness, model, argv, result, raw_path, observed.session_id,
    )


# ── 병렬 위임 touches 교집합 (같은 세션 claimed ticket·never-block) ─────────
# 같은 트리에서 dev 를 동시에 띄우면 touches 가 겹치는 두 위임이 서로의 WIP 를 덮는다. PM 이
# 손으로 하던 disjoint 확인을 계산으로 옮긴다 — 차단하지 않고 표면화만 한다([[T-0701]]).
#
# 좌표계는 **이 기능 전체에 하나**다 — 사전(교집합)·사후(겹친 파일 변경) 두 축 모두 board 선언을
# `_folded_touch_pairs` 로 이 workspace 좌표(repo-relative)까지 접은 뒤 비교한다. 실 보드에는 같은
# 파일이 slot 접두형(`work/<repo>_<N>/x`)과 무접두형(`x`) 두 표기로 공존하므로, 표기 정규화만 하고
# 비교하면 접두/무접두 혼합 선언에서 교집합이 조용히 0이 된다.

_TOUCH_OVERLAP_HEADER = "=== ⚠ 병렬 위임 touches 겹침 ==="
_TOUCH_OVERLAP_PRESCRIPTION = (
    "  · 같은 트리 동시 편집은 WIP 를 덮을 수 있다 — 차단하지 않으며 PM 이 판정한다.\n"
    "  · 처방: 순차 실행 또는 `pm-config worktree add <repo>` 로 슬롯 분리"
)
# 진단 목록 표시 상한 — 넘치면 잔여 건수를 병기한다.
_TOUCH_OVERLAP_DISPLAY_LIMIT = 8


class TicketTouchOverlap(NamedTuple):
    """같은 세션이 claim 중인 다른 ticket 하나와의 touches 교집합."""

    ticket: str                      # 상대 ticket id
    claimed_by: str                  # 두 ticket 이 공유하는 claim 주체(= "같은 세션" 근거)
    declarations: tuple[str, ...]    # 상대 ticket 의 **원 선언**(board 에서 그대로 grep 되는 표기)
    paths: tuple[str, ...]           # 접힌 좌표의 **좁은 쪽** 공유 경로(디렉토리∩파일=파일·트리 기준)


def _folded_touch_pairs(
    items: Sequence[str], *, pm_root: Path | str, workspace: Path | str,
) -> tuple[tuple[str, str], ...]:
    """touches 를 `(원 선언, 이 workspace 좌표)` 쌍으로 접는다 — **사전·사후 공용 좌표 규칙**.

    검증된 slot 접두만 벗기는 규칙은 repo_coordinates 단일 진실이다(`delegate_scope_git.allowed_paths`
    가 쓰는 것과 같은 primitive). 이 workspace 로 해소되지 않는 선언(다른 슬롯·traversal)은 항목
    단위로 드롭한다 — 여기서는 정상 입력이라 기본은 조용하다(선언 축의 드롭은 `_warn_dropped_touch`
    가 이미 알린다). 정규화 예외 타입은 좁게 잡는다: 엔진 사본 skew 등 marked 예외는 이 경계에서
    삼키지 않고 그대로 올린다.
    """
    pairs: list[tuple[str, str]] = []
    for item in items:
        try:
            normalized = repo_coordinates.normalize_repo_paths(
                [item], pm_root=Path(pm_root), workspace=Path(workspace),
            )
        except (ValueError, OSError) as exc:   # RepoCoordinateError(ValueError) = 타 슬롯/미실재
            continue
        for path in normalized:
            value = ticket_rules._touch_notation(repo_coordinates.canonicalize_path_notation(str(path)))
            if value is not None and value != ".":
                pairs.append((item, value))
    return tuple(dict.fromkeys(pairs))


def _touch_notations(
    raw: object, *, pm_root: Path | str, workspace: Path | str,
) -> tuple[tuple[str, str], ...] | None:
    """frontmatter touches → 접힌 `(원 선언, 좌표)` 쌍. **형식 불명이면 None**(판정 보류).

    형식 판정은 단일 진실(`ticket_rules._normalized_touches`)을 쓴다 — 스칼라/비문자열 원소를
    여기서 다시 해석하면 같은 frontmatter 를 두 문법으로 읽게 된다.
    """
    items = ticket_rules._normalized_touches(raw)
    if items is None:
        return None
    return _folded_touch_pairs(
        items, pm_root=pm_root, workspace=workspace,
    )


def _narrower_touch_path(left: str, right: str) -> str:
    """겹치는 두 선언이 실제로 공유하는 경로 — 접두(디렉토리)가 아닌 좁은 쪽."""
    return right if right.startswith(left + "/") else left


def claimed_touch_overlaps(
    ticket: str, *, pm_root: Path | str, workspace: Path | str,
) -> tuple[TicketTouchOverlap, ...]:
    """대상 ticket 과 **같은 세션**이 claim 중인 다른 ticket 들의 touches 교집합.

    "같은 세션" 은 `claimed_by` 값 일치다(`<user>/<task>` 또는 슬롯-only 둘 다 그 값 그대로
    비교한다 — 다른 사용자/다른 task 의 동시 claim 은 이 트리를 공유하지 않는다). 대상 ticket
    자신과 `claimed_by` 가 없는 ticket 은 제외한다.

    비교 좌표는 `workspace`(= 이 위임의 git 최상위) 기준으로 접은 값이라 접두/무접두 혼합 선언도
    같은 파일로 만난다. board 형상 둘(board-git 분리 `.project_manager/board/tickets/` · legacy
    `wiki/tickets/`)은 `pm_paths.tickets_dir` 해소를 그대로 타므로 여기서 갈라 보지 않는다.
    손상 frontmatter 는 순회 로더(`ticket_store.load_ticket_soft`)가 경고 1줄과 함께 건너뛴다.
    """
    home = pm_paths.home_at(Path(pm_root).resolve())
    found = ticket_store.find_ticket_exact(home, ticket)
    if found is None:
        return ()
    target = ticket_store.load_ticket_soft(home, found[1])
    if target is None:
        return ()
    target_fm = target[0]
    claimed_by = str(target_fm.get("claimed_by") or "").strip()
    target_pairs = _touch_notations(
        target_fm.get("touches"),
        pm_root=pm_root, workspace=workspace,
    )
    if not claimed_by or not target_pairs:
        return ()

    overlaps: list[TicketTouchOverlap] = []
    for path in sorted((pm_paths.tickets_dir(home) / "claimed").glob("*.md")):
        loaded = ticket_store.load_ticket_soft(home, path)
        if loaded is None:
            continue
        other_fm = loaded[0]
        other_id = str(other_fm.get("id") or "").strip()
        if not other_id or other_id == ticket:
            continue
        if str(other_fm.get("claimed_by") or "").strip() != claimed_by:
            continue
        other_pairs = _touch_notations(
            other_fm.get("touches"),
            pm_root=pm_root, workspace=workspace,
        )
        if not other_pairs:
            continue
        declarations: list[str] = []
        shared: list[str] = []
        for other_raw, other_folded in other_pairs:
            for _target_raw, target_folded in target_pairs:
                if not ticket_rules._touch_paths_overlap(other_folded, target_folded):
                    continue
                declarations.append(other_raw)
                shared.append(_narrower_touch_path(other_folded, target_folded))
        if shared:
            overlaps.append(TicketTouchOverlap(
                other_id,
                claimed_by,
                tuple(dict.fromkeys(declarations)),
                tuple(dict.fromkeys(shared)),
            ))
    return tuple(overlaps)


def _format_overlap_items(items: Sequence[str]) -> str:
    """진단 목록 1줄 — 상한 초과분은 개수로 접는다."""
    listed = tuple(items)
    head = ", ".join(listed[:_TOUCH_OVERLAP_DISPLAY_LIMIT])
    remainder = len(listed) - _TOUCH_OVERLAP_DISPLAY_LIMIT
    return head + (f" … 외 {remainder}건" if remainder > 0 else "")


def format_touch_overlap_warning(
    ticket: str, overlaps: Sequence[TicketTouchOverlap],
) -> str:
    """교집합을 사전 loud 경고 1블록으로 만든다. 겹침이 없으면 출력도 없다."""
    if not overlaps:
        return ""
    lines = [
        _TOUCH_OVERLAP_HEADER,
        f"경고: 병렬 위임 touches 겹침 — 같은 세션({overlaps[0].claimed_by})이 claim 중인 "
        f"ticket 과 {ticket} 의 범위가 겹칩니다.",
    ]
    lines.extend(
        f"  - {overlap.ticket}({_format_overlap_items(overlap.declarations)}) ∩ {ticket}: "
        f"{_format_overlap_items(overlap.paths)}"
        for overlap in overlaps[:_TOUCH_OVERLAP_DISPLAY_LIMIT]
    )
    hidden = len(overlaps) - _TOUCH_OVERLAP_DISPLAY_LIMIT
    if hidden > 0:
        lines.append(f"  - … 외 {hidden}건의 ticket 이 더 겹칩니다")
    lines.append(_TOUCH_OVERLAP_PRESCRIPTION)
    return "\n".join(lines)


def overlap_touch_paths(overlaps: Sequence[TicketTouchOverlap]) -> tuple[str, ...]:
    """모든 교집합 경로의 합집합 — 회수 시점 "겹친 파일이 실제로 바뀌었나" 판정 입력."""
    paths: list[str] = []
    for overlap in overlaps:
        paths.extend(overlap.paths)
    return tuple(sorted(dict.fromkeys(paths)))


def _changed_overlap_paths(
    audit: ScopeAudit, after, role: str,
) -> tuple[str, ...]:
    """교집합 경로 중 이 위임 시간창에 **실제로 바뀐** workspace 경로들.

    `ScopeAudit.overlap_paths` 는 사전 축이 이미 이 workspace 좌표로 접어 둔 값이라(같은
    `_folded_touch_pairs` 규칙) 여기서 좌표를 다시 만들지 않는다 — 한 기능에 좌표 규칙 하나.
    """
    if not audit.overlap_paths or role not in delegate_run_rules.WRITE_ROLES:
        return ()
    changed = set(delegate_scope_rules.changed_status_paths(audit.before, after))
    changed.update(delegate_scope_git.committed_paths(
        audit.before, after, workspace=audit.workspace,
    ))
    return tuple(sorted(
        path for path in changed
        if any(ticket_rules._touch_paths_overlap(path, root) for root in audit.overlap_paths)
    ))


def format_changed_overlap_warning(paths: Sequence[str]) -> str:
    """회수 시점 1줄 — 겹친 파일이 실제로 바뀌었으면 공존 여부 확인을 요구한다."""
    if not paths:
        return ""
    return (
        "경고: 겹친 파일 변경됨 — 다른 dev 산출 공존 여부를 확인하라: "
        + _format_overlap_items(paths)
    )


# ── 위임 범위 밖 변경 감지 훅 (delegate_scope 판정 재사용·never-block) ──────

class ScopeAudit(NamedTuple):
    """위임 **전체 단위**(primary + 폴백 attempt 포함) 범위 판정 입력."""

    touches: tuple[str, ...] | None  # None이면 generic 축만 강등(ticket 준비 실패)
    before: delegate_scope_rules.WorktreeState
    workspace: Path               # git toplevel(=--cwd 가 하위 디렉토리여도 판정 기준은 루트)
    pm_root: Path                # --cwd lease에서 해소한 board/config 소유 PM 홈
    adapter_roots: tuple[str, ...] = ()  # preamble과 같은 실행-전 등록부 스냅샷
    overlap_paths: tuple[str, ...] = ()  # 같은 세션 claimed ticket과 겹치는 touches 경로


def _warn_dropped_touch(item: str, reason: str) -> None:
    """정규화 못 한 touches 항목을 loud 하게 알린다(드롭 = 그만큼 허용 범위가 좁아진다)."""
    print(
        f"경고: touches 항목 '{item}' 을 이 workspace 좌표로 해소하지 못해 범위 판정에서 뺍니다"
        f"({reason}).",
        file=sys.stderr,
    )


def _with_template_propagation(
    touches: Sequence[str], *, workspace: Path, pm_root: Path | None = None,
) -> tuple[str, ...]:
    """엔진 tools touch에 대응하는 실재 templates 하네스 경로를 허용 집합에 보탠다."""
    templates = workspace / "templates"
    harnesses = tuple(
        path.name for path in sorted(templates.iterdir()) if path.is_dir()
    ) if templates.is_dir() else ()
    expanded = list(touches)
    for raw in touches:
        parts = PurePosixPath(raw.replace("\\", "/")).parts
        # ticket이 실제 workspace 루트 엔진 tools를 허용한 경우에만 기계 전파본을 보탠다.
        # 이미 templates 아래인 touch나 경로 중간의 tools 표기는 다른 하네스 전체를 열지 않는다.
        marker = next(
            (
                index for index in range(len(parts) - 1)
                if parts[index:index + 2] == (".project_manager", "tools")
            ),
            None,
        )
        if marker is None:
            continue
        prefix = PurePosixPath(*parts[:marker])
        root_touch = not prefix.parts
        if pm_root is not None and prefix.parts:
            root_touch = (pm_root / prefix).resolve() == workspace.resolve()
        if not root_touch:
            continue
        suffix = parts[marker:]
        for harness in harnesses:
            candidate = workspace / "templates" / harness
            if (candidate / ".project_manager" / "tools").is_dir():
                expanded.append(PurePosixPath("templates", harness, *suffix).as_posix())
    return tuple(dict.fromkeys(expanded))


def begin_scope_audit(
    ticket: str | None,
    cwd: Path,
    *,
    pm_root: Path | None = None,
    adapter_roots: Sequence[str] | None = None,
) -> ScopeAudit | None:
    """위임 실행 **직전** worktree 상태를 캡처한다.

    호출 시점은 실행-전 게이트(매핑·재앵커·dry-run)를 **모두 통과한
    뒤**다 — 아무것도 실행하지 않은 경로에서 판정을 켜면 무의미한 git 호출·오탐만 는다.
    캡처/정규화 기준은 `--cwd` 가 아니라 **git toplevel** 이다 — repo 하위 디렉토리를 --cwd 로 주면
    슬롯 루트와 좌표가 어긋나 판정이 통째로 꺼진다. `--ticket` 이 없으면 `touches=()` 라 허용 경로가
    0이다(delegate_scope_rules·delegate_scope_git 계약 — 변경이 있으면 전부 경고).

    ``ScopeAudit.overlap_paths``는 실행 **전**에 이미 계산해 경고한 병렬 위임 교집합 경로다
    (같은 세션이 claim 중인 다른 ticket 과 겹치는 선언). 이 함수는 그 축을 계산하지 않는다 —
    호출부가 회수 시점 판정용으로 결과에 실어 준다(``_replace``).

    공통 베이스라인(toplevel·worktree 캡처)이 실패하면 전후 차이를 만들 수 없어 generic과
    어댑터 두 축이 모두 죽는다. 반면 board 로드·ticket 부재/손상으로 touches 준비만 실패하면
    generic 축만 loud 강등하고, 같은 worktree 캡처를 쓰는 어댑터 축은 보존한다. 일반 실패는 위임을 막지 않고 경고한다."""
    roots = tuple(
        _resolved_adapter_directories()
        if adapter_roots is None
        else adapter_roots
    )
    try:
        workspace = delegate_scope_git.resolve_workspace_root(cwd)
        before = delegate_scope_git.capture_worktree_state(workspace)
    except Exception as exc:  # noqa: BLE001 — 공통 캡처 실패만 두 축을 함께 끈다.
        print(
            "경고: 위임 범위 판정 준비 실패"
            f"(공통 worktree 캡처: {exc}) — generic·어댑터 두 축 모두 판정 불가, 비차단 진행.",
            file=sys.stderr,
        )
        return None

    # 해시 대상이 있는데 지문을 하나도 못 구했으면 이미 dirty 한 파일의 재수정을 못 잡는다 —
    # 강등된 채로 조용히 통과시키지 않는다(축소된 감지력을 PM 이 알아야 한다).
    try:
        degraded = delegate_scope_git.content_signal_missing(before, workspace)
    except Exception as exc:  # noqa: BLE001 — 보강 신호 진단 실패로 두 판정축을 버리지 않는다.
        degraded = False
        print(f"경고: 내용 해시 보강 상태 확인 실패({exc}) — 비차단 진행.", file=sys.stderr)
    if degraded:
        print(
            "경고: 내용 해시 보강 신호 없음 — 이미 dirty 한 파일의 재수정은 감지 불가"
            "(상태코드/mode 신호로만 판정).",
            file=sys.stderr,
        )

    try:
        resolved_pm_root = pm_root or REPO
        touches = delegate_scope_git.ticket_touches(
            pm_paths.home_at(resolved_pm_root), ticket,
        ) if ticket else ()
        touches = _with_template_propagation(
            touches, workspace=workspace, pm_root=resolved_pm_root,
        )
    except Exception as exc:  # noqa: BLE001 — touches의 형제인 어댑터 축은 보존한다.
        touches = None
        print(
            "=== ⚠ 위임 범위 판정 축 강등 ===\n"
            f"ticket touches 준비 실패({exc}) — generic 범위 축만 판정할 수 없습니다. "
            "공통 worktree 캡처가 살아 있어 어댑터 편집 축은 계속 판정합니다.",
            file=sys.stderr,
        )
    return ScopeAudit(touches, before, workspace, pm_root or REPO, roots)


def _adapter_edit_paths(
    before,
    after,
    *,
    workspace: Path,
    roots: Sequence[str],
) -> tuple[str, ...] | None:
    """touches와 무관하게 등록 어댑터 루트 전체에서 바뀐 경로를 반환한다.

    대상은 출하 카드 하위로 좁히지 않는다. 실제 템플릿의 등록 루트에는 카드뿐 아니라
    settings/hooks/runner/agent 정의도 함께 있고, 역할 preamble도 "통합 루트 전체"를 금지한다.
    여기서 별도 하위 목록을 만들면 새 어댑터 표면이 다시 빠지므로 preamble 합성 때 선행 등록부에서
    파생해 ``ScopeAudit``에 보존한 동일 스냅샷을 소비한다. 회수 시 등록부를 다시 읽으면 위임 중
    registry 변경에 따라 전달한 금지 범위와 판정 범위가 갈린다.

    ``None``은 비-literal 표기/형제 파일 부재가 기존 graceful-degrade 경로를 타 등록 루트가
    0개가 된 경우다. 추측 목록으로 보충하지 않고 호출부가 판정 불가를 loud 경고한다.
    """
    roots = tuple(roots)
    if not roots:
        return None
    changed = set(delegate_scope_rules.changed_status_paths(before, after))
    changed.update(delegate_scope_git.committed_paths(before, after, workspace=workspace))
    return tuple(
        path for path in sorted(changed)
        if any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)
    )


def _format_adapter_edit_warning(paths: Sequence[str]) -> str:
    """역할 공통 금지 위반을 기존 범위-밖 축과 구분된 advisory 블록으로 만든다."""
    unique = tuple(sorted(set(paths)))
    if not unique:
        return ""
    lines = [
        "=== ⚠ 역할 공통 금지 위반: 어댑터 편집 ===",
        "어댑터 디렉토리 수정이 감지되었습니다. 이는 ticket touches 포함 여부와 무관한 "
        "역할 공통 금지 위반입니다.",
        "차단하지 않으며 PM이 정당한 어댑터 작업으로 수용할지 격리/복원할지 판정해야 합니다.",
        "  · gitignored 산출물(.project_manager/.local 등)은 git status 입력에 없어 판정 대상이 아닙니다.",
        "  · 위임 시간창 기준이라 다른 터미널/도구가 만든 변경도 섞일 수 있습니다.",
        "  · 판정 범위는 이 git repo(toplevel) 안입니다 — 그 밖/중첩 repo 안의 변경은 보이지 않습니다.",
    ]
    lines.extend(f"  - {path}" for path in unique)
    return "\n".join(lines)


def report_scope_audit(audit: ScopeAudit | None, role: str) -> None:
    """위임 **회수 시점**(모든 attempt 종료 후 1회)의 변경을 두 독립 축으로 loud 경고한다.

    축 1은 ticket touches 기반 범위 밖 변경, 축 2는 touches와 독립적인 역할 공통 어댑터 편집
    금지 위반이다. 축 1에는 병렬 위임 교집합(실행 전 경고한 겹침 경로)이 실제로 바뀌었는지
    1줄이 따라붙는다 — 같은 전후 캡처를 소비하는 같은 축의 사후 신호다.
    반환값/rc 를 바꾸지 않는다 — 격리/복원/수용 판정은 PM 몫이다. 한 축의 판정
    실패가 다른 축까지 지우지 않으며 둘 다 비차단이다. 쓰기 허용 역할집합은 이 모듈의
    WRITE_ROLES 를 주입해 단일 출처로 쓴다(감지기 기본값과의 드리프트는 테스트가 막는다).
    raw 박제 기본 `.project_manager/.local/delegate/`는 gitignored라 판정에 안 잡히며,
    `--output-dir`를 repo 안의 비-ignore 경로로 주면 그 산출물도
    '위임이 만든 변경'으로 잡힌다(의도). 일반 판정 실패는 경고하고 비차단 진행한다.

    형제-가지 여집합 결정: 어댑터 축은 ``--ticket``이 있을 때만 돌지 않는다. 생략 실행에서도
    반드시 돌며, 그때 같은 경로가 "허용 0의 범위 밖"과 "역할 공통 금지 위반" 두 블록에 나오는
    중복은 의도적이다. 한 블록으로 합치면 PM이 정책 성격을 오판하므로 경로 중복보다 축 식별을
    우선한다."""
    if audit is None:
        return
    try:
        after = delegate_scope_git.capture_worktree_state(audit.workspace)
        if delegate_scope_rules.head_moved(audit.before, after):
            # 커밋 자체가 역할 계약 위반 신호다(위임 역할은 commit/push 금지) — 범위 안이든 밖이든
            # 별도로 알린다. 커밋된 경로는 아래 판정 입력에 합산된다(worktree 는 clean 이라 무증거).
            print(
                "경고: 위임 중 커밋이 발생했습니다(위임 역할은 commit 금지) — "
                f"HEAD {audit.before.head or '(없음)'} → {after.head or '(없음)'}. "
                "커밋된 변경도 범위 판정에 합산합니다.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — 공통 캡처 실패면 두 축 모두 판정 불가.
        print(f"경고: 위임 범위 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)
        return

    if audit.touches is not None or audit.overlap_paths:
        try:
            if audit.touches is not None:
                paths = delegate_scope_git.out_of_scope_changes(
                    audit.before, after,
                    touches=audit.touches, role=role, pm_root=audit.pm_root,
                    workspace=audit.workspace,
                    write_roles=delegate_run_rules.WRITE_ROLES, on_drop=_warn_dropped_touch,
                )
                warning = delegate_scope_rules.format_warning(paths)
                # **계산 직후 즉시** 출력한다 — 뒤따르는 교집합 계산이 실패해도 이미 확정된
                # 범위-밖 신호가 함께 사라지면 "한 축 실패가 다른 축을 지우지 않는다"가 깨진다.
                if warning:
                    print(warning, file=sys.stderr)
            # 교집합 축은 같은 전후 캡처를 소비하므로 같은 판정 경계 안에서 계산한다(같은
            # 비차단 경고 하나). 위 출력이 선행하므로 이 축의 실패는 기존 신호를 못 지운다.
            overlap_warning = format_changed_overlap_warning(
                _changed_overlap_paths(audit, after, role)
            )
            if overlap_warning:
                print(overlap_warning, file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — 일반 판정 실패만 비차단, 엔진 skew는 fail-loud.
            print(f"경고: 위임 범위 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)

    try:
        adapter_paths = _adapter_edit_paths(
            audit.before, after, workspace=audit.workspace,
            roots=audit.adapter_roots,
        )
    except Exception as exc:  # noqa: BLE001 — 어댑터 축만 강등, 기존 범위 축은 보존.
        print(f"경고: 어댑터 편집 경고 축 판정 실패({exc}) — 비차단 진행.", file=sys.stderr)
        return
    if adapter_paths is None:
        print(
            "=== ⚠ 어댑터 편집 경고 축 강등 ===\n"
            "선행 등록부 파생 경로가 0개라 역할 공통 어댑터 수정 금지 위반을 판정할 수 없습니다. "
            "새 목록으로 추측하지 않으며 비차단 진행합니다.",
            file=sys.stderr,
        )
        return
    adapter_warning = _format_adapter_edit_warning(adapter_paths)
    if adapter_warning:
        print(adapter_warning, file=sys.stderr)


# ── native 단락 advisory (never-block 백스톱) ────────────────────────────

# 라이브 실측(2026년 7월 29일): Codex 세션에서 ``python3 -c``로 CODEX/CLAUDE/OPENCODE
# 이름만 dump했다 — CODEX_THREAD_ID=<set>, CODEX_CI=<set>. 세션 판정 근거는 이 두 키다.
# Claude Code는 이전 실측(2026년 7월 28일)에서 CLAUDECODE=<set>,
# CLAUDE_CODE_SESSION_ID/ENTRYPOINT/EXECPATH/CHILD_SESSION=<set>였고, 판정 근거는
# CLAUDECODE다.
#
# 라이브 실측(2026년 7월 29일): Claude 부모 셸과 그 셸에서
# ``opencode run -m ollama/glm-5.2:cloud``로 띄운 OpenCode 세션 안의 env를 diff했다.
# 부모에는 AI_AGENT/CLAUDECODE/CLAUDE_CODE_CHILD_SESSION/CLAUDE_CODE_ENTRYPOINT/
# CLAUDE_CODE_EXECPATH/CLAUDE_CODE_SESSION_ID/CLAUDE_EFFORT/CLAUDE_PID 및
# OPENCODE_CONFIG_DIR/ORCA_OPENCODE_CONFIG_DIR가 이미 있었고, OpenCode 세션은 이를 상속한
# 뒤 OPENCODE=<set>, OPENCODE_PID=<set>만 추가했다. 따라서 OPENCODE(보조로
# OPENCODE_PID)는 런타임 주입 세션 마커다. 반대로 OPENCODE_CONFIG_DIR는 세션 없는 부모에도
# 있으므로 설정 경로로 확정했고, OPENCODE_CONFIG는 양쪽 어디에도 없어 판정에서 제외한다.
def _session_harnesses(env: dict[str, str]) -> tuple[str, ...]:
    """실측 세션 마커와 일치하는 하네스를 모두 반환한다."""
    session_markers = watchdog.HARNESS_SESSION_MARKERS
    return tuple(
        name for name, keys in session_markers.items()
        if any(env.get(key) for key in keys)
    )


def _session_harness(env: dict[str, str]) -> str | None:
    """실측 세션 마커가 정확히 한 하네스와 일치할 때만 그 하네스를 반환한다."""
    matches = _session_harnesses(env)
    return matches[0] if len(matches) == 1 else None


def native_advisory(harness: str | None, role: str) -> str | None:
    """설계·구현·리뷰·조사를 target 하네스 == PM 하네스로 CLI 실행하면 native 규칙 안내 1줄(never-block).

    규칙: 모든 역할이 같은 하네스면 native 위임, 다르면 `pm_delegate.py` cross 다. 판정 입력은
    하네스뿐이다(역할 예외 없음). PM 하네스 env 마커(codex CODEX_THREAD_ID·claude CLAUDECODE·
    opencode OPENCODE)를 감지해 안내가 필요하면
    경고 문자열을 반환한다(호출부가 stderr 로 냄). 1차 판정은 어댑터 스킬 카드·이건 백스톱."""
    # 직접 호출 seam 에도 공개 하네스 도메인을 적용한다. 이 진단은 하네스 이름별 표현을 소유한다.
    if harness not in conf_schema.HARNESS_CHOICES:
        return None
    pm_harness = _session_harness(os.environ)
    if pm_harness == harness:
        return (f"[pm-delegate] target 하네스({harness}) == PM 하네스 — 설계·구현·리뷰·조사는 "
                "native 위임이 규칙이다(어댑터 스킬 카드 transport 절 · advisory).")
    return None

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


def _ticket_cli_owner(cwd: Path) -> Path:
    if not cwd.is_absolute():
        raise delegate_errors.DelegateError("--cwd 는 절대경로여야 합니다")
    resolved = cwd.resolve()
    if resolved == resolved.parent or not delegate_env._cwd_in_git_repo(resolved):
        raise delegate_errors.DelegateError(f"--cwd 는 파일시스템 루트가 아닌 git 작업공간이어야 합니다: {resolved}")
    try:
        owner = Path(pm_paths.resolve_pm_home_for_repo(resolved)).resolve()
    except pm_paths.AnchorResolutionError as exc:
        raise delegate_errors.DelegateError(f"--cwd 소유 PM 홈 해소 실패: {exc}") from exc
    if pm_paths._owns_real_board(REPO / ".project_manager") and REPO.resolve() != owner:
        raise delegate_errors.DelegateError(
            "실행 엔진 board와 --cwd 소유 PM 홈이 달라 ticket 사본 좌표를 확정할 수 없습니다: "
            f"engine={REPO.resolve()} · owner={owner}"
        )
    return owner


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
        choices=tuple(sorted(TICKET_COPY_PREPARE_ROLES)),
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
            f"{ABANDON_DISCARD_REASON_FLAG} 로 폐기한 순번을 다시 시키려면 이 값으로 재개방한 "
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
        ABANDON_ASSUME_DEAD_FLAG, action="store_true", dest="assume_dead",
        help="run 이 끝났음을 운영자가 확인했다 (종료 증거가 없거나 소유 pid 가 살아 있어도 진행)",
    )
    abandon.add_argument(
        ABANDON_SUPERSEDED_BY_FLAG, type=int, default=None, dest="superseded_by_ordinal",
        metavar="N",
        help=(
            "이 라운드는 재실행된 ordinal N 라운드로 대체됐다 — 산출이 시드와 달라도 포기를 "
            "허용한다(생존 확인은 별도 축 — --assume-dead 필요 여부는 그대로)"
        ),
    )
    abandon.add_argument(
        ABANDON_DISCARD_REASON_FLAG, default=None, dest="discard_reason", metavar="사유",
        help=(
            "이 라운드를 폐기한다(재실행 대체가 아니다) — 산출이 있으면 board 라운드 파일로 "
            "옮겨 표식을 붙이고, 사본도 board 파일도 없는 행은 이 사유로 장부만 닫는다. "
            f"{ABANDON_SUPERSEDED_BY_FLAG} 와 함께 줄 수 없다. 폐기한 순번은 예산 한 칸을 "
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
        pm_harness = _session_harness(os.environ) or ""
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


def _parse_ledger_timestamp(value: object) -> datetime.datetime | None:
    """장부 시각 문자열 → datetime(판독 불가는 None · 판정에서 그 조건만 뺀다)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        return None


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
    sources, plan = prepare_cluster_review(
        cluster=cluster, cwd=cwd, pm_home=owner, focus_path=focus_path,
        owner_pid=None, reopen_ordinal=reopen_ordinal,
    )
    prompt = cluster_review_prompt(
        sources, tree=cwd, plan=plan, adapter_roots=_resolved_adapter_directories(),
    )
    prompt_path = _review_prompt_path(owner, plan.run_id)
    try:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        delegate_temp._portable_exclusive_write(prompt_path, prompt)
    except OSError as exc:
        raise delegate_errors.DelegateError(
            f"리뷰 프롬프트 파일 쓰기 실패: {prompt_path}: {exc} — 준비한 run-dir 은 "
            f"`ticket abandon --copy <사본> --cwd {cwd} {ABANDON_ASSUME_DEAD_FLAG}` 로 닫습니다: "
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
        owner = _ticket_cli_owner(cwd_repo)
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
                    f"(local.conf: {_local_conf_path(owner)} · {delegate_run_rules.DELEGATE_ENABLED_KEY}=false).\n"
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
                cluster_plan = prepare_cluster_copy(
                    cluster=args.cluster, role=args.role, cwd=cwd_repo, pm_home=owner,
                    reopen_ordinal=args.reopen_ordinal,
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
            plan = prepare_ticket_copy(
                ticket=args.ticket, role=args.role, cwd=cwd_repo, pm_home=owner,
                reopen_ordinal=args.reopen_ordinal,
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
            abandon_result = abandon_ticket_copy(
                copy_path=copy, cwd=cwd_repo, pm_home=owner,
                assume_dead=args.assume_dead,
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
            outcomes = harvest_cluster_copy(
                run_dir=copy, cwd=cwd_repo, pm_home=owner,
            )
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
        result = harvest_ticket_copy(
            copy_path=copy, cwd=cwd_repo, pm_home=owner,
        )
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
    rendered = render_pm_review_verify_template(template)
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
            rendered = render_pm_review_disposition_template(
                ticket_text, rounds, args.ordinal, reviewer_role=args.reviewer_role,
            )
        elif args.review_command == "verify-template":
            template = review_state.pm_review_verify_template(
                ticket_text, rounds, round_ordinal=args.round_ordinal,
            )
            return _emit_pm_review_verify_template(template)
        else:
            delta = review_state.parse_pm_review_delta(ticket_text, rounds)
            rendered = render_pm_review_delta(ticket, delta)
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
              else _parse_ledger_timestamp(value))
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
        raw = _cluster_git(
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
        rows = run_pm_review_confirmations(template, cwd=tree, run_fn=run_fn)
        if rows:
            append_pm_review_confirmation(
                owner, ticket, render_pm_review_confirmation_section(rows), rounds=rounds,
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


# ── 묶음 리뷰 — 리뷰 입력 · 트리 검사 · 프롬프트 조립 · 준비 ──────────────────
#
# 리뷰 단위는 묶음이다. 같은 하네스(native 서브에이전트)와 다른 하네스(교차 CLI) 두 전송이 같은
# 준비(`prepare_cluster_review`)·같은 조립(`cluster_review_prompt`)·같은 돌려받기
# (`harvest_ticket_copy`)를 쓴다. 수단은 전부 기존 것이다:
#   · 프롬프트 = `additional_reviewer.build_prompt`(맥락 헤더 + 출력 형식 + 티켓 본문 N + 검토
#     중점 + diff).
#   · 라운드 자리 = `prepare_cluster_copy`(run-dir 1 · 티켓당 라운드 파일 1 · `base_rev` 기록).
# 리뷰 입력은 `merge-base(<통합 tip>, <묶음 브랜치>)..<묶음 브랜치>` 다 — 통합 브랜치가
# 앞서 가도 이 묶음이 만든 것만 본다. 리뷰어는 슬롯을 그대로 읽는다 — 준비는 슬롯이 묶음
# 브랜치이고 작업 트리 변경이 0 일 때만(준비 트리 조건), 돌려받기는 HEAD·작업 트리가 준비 시점과
# 같을 때만(돌려받기 트리 조건) 통과한다.


class ClusterReviewInput(NamedTuple):
    """묶음 리뷰 1회의 브랜치 좌표와 변경 파일 목록."""

    cluster: str
    members: tuple[str, ...]
    base_branch: str
    branch: str
    merge_base: str
    paths: tuple[str, ...]


class ClusterReviewSources(NamedTuple):
    """리뷰 프롬프트의 읽기 전용 입력 — 브랜치 diff·멤버 본문·검토 중점."""

    review: ClusterReviewInput
    specs: dict[str, str]
    focus: str | None


def _cluster_git(
    repo: Path, *args: str, git_run_fn: Callable | None = None,
) -> str:
    """묶음 브랜치 조회 git — 실패는 그대로 올린다(조용한 빈 값 금지)."""
    runner = git_run_fn or subprocess.run
    result = runner(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if getattr(result, "returncode", 1) != 0:
        detail = (
            (getattr(result, "stderr", "") or "").strip()
            or (getattr(result, "stdout", "") or "").strip()
            or "원인 미상"
        )
        raise delegate_errors.DelegateError(f"git {' '.join(args)} 실패: {detail}")
    return getattr(result, "stdout", "") or ""


def _slot_pending_changes(
    cwd: Path, *, git_run_fn: Callable | None = None,
) -> tuple[str, ...]:
    """슬롯 작업 트리 변경 — `git status --porcelain` 줄(ignore 대상 제외).

    추적 파일 수정·추적 안 된 새 파일이 모두 잡힌다. 서브모듈 내부 dirty 는 세지 않는다 — 상위
    repo 가 커밋할 수 있는 것은 포인터뿐이다(종결 단계와 같은 관측). 리뷰 준비·리뷰
    돌려받기의 트리 조건과 developer 돌려받기 커밋이 이 한 관측을 쓴다.
    """
    return tuple(
        line for line in _cluster_git(
            cwd, "status", "--porcelain", "--untracked-files=all",
            "--ignore-submodules=dirty", git_run_fn=git_run_fn,
        ).splitlines() if line.strip()
    )


def cluster_review_input(
    home, cluster: str, *, repo: Path, git_run_fn: Callable | None = None,
) -> ClusterReviewInput:
    """리뷰 대상 diff 와 그 범위를 묶음 장부에서 해소한다.

    기준은 `merge-base(<통합 브랜치 tip>, <묶음 브랜치>)` 다 — 통합 브랜치가 그동안 앞서
    갔어도 이 묶음이 만든 변경만 리뷰 입력이 된다(흡수분은 조상이라 빠진다). 입력을 해소한
    자리에서 트리를 결속한다(준비 트리 조건 · `assert_cluster_review_tree`).
    """
    fm = cluster_store.load_cluster(home, cluster)
    if fm is None:
        raise delegate_errors.DelegateError(
            f"클러스터 장부가 없습니다: {cluster} — 리뷰 단위는 선언된 묶음입니다"
            f"(`board.py cluster new <ticket-group> --tickets <T-...> --task <task>`"
            f"(task 의 폴더가 2개 이상이면 `--repo <repo> --slot <N>` 추가) 으로 먼저 "
            f"선언하세요 — 코드 트리는 명시가 필수입니다)"
        )
    branch = str(fm.get("branch") or "").strip()
    base_branch = str(fm.get("base_branch") or "").strip()
    if not branch or not base_branch:
        raise delegate_errors.DelegateError(
            f"묶음 장부에 통합 브랜치 좌표가 없습니다: {cluster} "
            f"(branch={branch or '—'} · base_branch={base_branch or '—'})"
        )
    members = tuple(cluster_store.cluster_live_members(home, cluster))
    if not members:
        raise delegate_errors.DelegateError(f"클러스터 멤버가 없습니다: {cluster}")
    merge_base = _cluster_git(
        repo, "merge-base", base_branch, branch, git_run_fn=git_run_fn,
    ).strip()
    if not merge_base:
        raise delegate_errors.DelegateError(
            f"merge-base 를 해소하지 못했습니다: {base_branch}..{branch}"
        )
    span = f"{merge_base}..{branch}"
    paths = tuple(
        path for path in _cluster_git(
            repo, "diff", "--name-only", "-z", span, git_run_fn=git_run_fn,
        ).split("\0") if path
    )
    if not paths:
        raise delegate_errors.DelegateError(
            f"리뷰할 diff 가 없습니다: {span} — 묶음 브랜치에 이 주기의 변경이 없습니다"
            "(dev 라운드 산출이 묶음 브랜치에 올라와 있는지 확인하세요)"
        )
    review = ClusterReviewInput(
        cluster, members, base_branch, branch, merge_base, paths,
    )
    assert_cluster_review_tree(review, repo=repo, git_run_fn=git_run_fn)
    return review


def assert_cluster_review_tree(
    review: ClusterReviewInput, *, repo: Path,
    git_run_fn: Callable | None = None,
) -> None:
    """리뷰 대상 트리가 묶음 브랜치이고 작업 트리 변경이 0 인지 결속한다(준비 트리 조건 · fail-closed).

    프롬프트의 비교 범위는 장부 브랜치의 `merge-base..tip` 이고, 리뷰어가 실제로 읽는 파일은
    이 트리의 내용이다. 트리가 그 브랜치가 아니거나 커밋되지 않은 변경이 있으면 둘이 서로 다른
    코드가 되어 판정 전체가 헛돈다. 브랜치 대조 seam 은 확인 커맨드 실행 트리와 같은 하나다
    (`git._cluster_current_branch`).
    """
    current = git._cluster_current_branch(str(repo))
    if current != review.branch:
        raise delegate_errors.DelegateError(
            f"리뷰 대상 트리가 묶음 브랜치가 아닙니다: {repo} "
            f"(현재 {current or '미상'} · 필요 {review.branch}) — 다른 브랜치의 파일을 "
            "리뷰 입력으로 넘기지 않습니다"
        )
    pending = _slot_pending_changes(repo, git_run_fn=git_run_fn)
    if pending:
        raise delegate_errors.DelegateError(
            f"리뷰 대상 트리에 커밋되지 않은 변경이 있습니다: {repo} — "
            + " · ".join(line.strip() for line in pending)
            + " — 구현 라운드를 돌려받았는지 확인하고(돌려받기가 커밋한다) 그 밖 변경은 "
            "커밋하거나 되돌린 뒤 다시 준비하세요"
        )


def _cluster_review_focus(path: Path, *, cwd: Path, pm_home: Path) -> str:
    """PM 검토 중점 파일을 읽는다."""
    focus_path = Path(path)
    if not focus_path.is_file():
        raise delegate_errors.DelegateError(f"--focus 파일이 없습니다: {focus_path}")
    try:
        return fs.read_text_shared(
            focus_path.resolve(), encoding="utf-8",
        )
    except (OSError, UnicodeError) as exc:
        raise delegate_errors.DelegateError(f"--focus 파일 읽기 실패: {focus_path}: {exc}") from exc


def build_cluster_review_prompt(
    review: ClusterReviewInput, specs: Mapping[str, str], *,
    tree: Path, focus: str | None,
) -> str:
    """묶음 리뷰 프롬프트 본문 — 조립기는 추가 리뷰어 채널과 **같은 하나**다.

    구조화 블록 요구는 이 프롬프트가 소유하지 않는다: 산출 자리가 티켓별 라운드 파일이고 그
    시드가 이미 채널 골격과 다음 ID 실값을 들고 있다(요구가 두 벌이면 갈린다).
    """
    external = _load_additional_reviewer()
    header = (
        f"## 리뷰 단위: {review.cluster} (티켓 {len(review.members)})\n\n"
        "- 준비 뒤 이 트리의 HEAD 나 작업 트리를 바꾸면 돌려받기가 거부된다(라운드 파일만 쓴다 · "
        "임시 수정은 종료 전에 되돌린다)\n"
        f"- 리뷰 입력: {review.base_branch} 와 {review.branch} 의 merge-base "
        f"({review.merge_base[:12]}) 이후 {review.branch} 변경 전부\n\n"
    )
    return header + external.build_prompt(
        tree=tree, base=review.merge_base, target=f"브랜치 {review.branch}",
        changed_paths=review.paths,
        read_commands=[(
            "git", "-C", str(tree.resolve()), "--no-pager", "diff", "--no-ext-diff",
            "--no-color", f"{review.merge_base}..{review.branch}", "--",
        )],
        ticket_bodies=[(ticket, specs[ticket]) for ticket in review.members],
        focus=focus,
        versioned_block=False,
    )


def _cluster_copy_preamble(plan: ClusterCopyPlan) -> str:
    """묶음 산출 자리 안내 — 티켓마다 자기 라운드 파일 하나다(좌표는 엔진이 준다).

    다음 finding ID 도 티켓마다 준비가 확정한 실값을 싣는다(`_ticket_copy_preamble` 과 같은
    규칙) — 리뷰 세션은 fresh 라 프롬프트에 없으면 번호가 추측이 된다.
    """
    seats = "\n".join(
        f"- {round_plan.ticket}: {round_plan.path}"
        + (
            "\n  finding ID: "
            + review_form.NEXT_FINDING_ID_RULE.format(next_id=round_plan.next_finding_id)
            if round_plan.next_finding_id else ""
        )
        for round_plan in plan.rounds
    )
    return (
        "라운드 산출 기록: 이 위임의 산출은 아래 티켓별 라운드 파일에만 쓴다"
        "(첫 줄 헤더는 그대로 두고 그 아래 골격을 채운다). 티켓 하나에 대한 지적은 그 티켓의 "
        "파일에만 적는다.\n"
        f"{seats}\n"
        f"같은 디렉터리의 `{TICKET_COPY_SPEC_NAME}`(티켓 명세)와 "
        f"`{TICKET_COPY_ROUNDS_DIRNAME}/`(이전 라운드)는 **읽기 전용**이다. PM 홈 티켓은 "
        "편집하지 마라. 이 파일들은 응답과 별개로 종료 시 기계 회수된다."
    )


def _round_copy_preamble(
    ticket_copy: TicketCopyPlan | None, cluster_plan: ClusterCopyPlan | None,
) -> str | None:
    """이 실행의 산출 자리 안내 — 자리가 N 이면 N 을 적는다(자리 없으면 None).

    쓰는 쪽(준비)과 읽는 쪽(프롬프트)이 이 한 함수를 지나야 "하나만 쓰라"는 안내와 실제로
    깔린 자리 수가 갈리지 않는다.
    """
    if cluster_plan is not None:
        return _cluster_copy_preamble(cluster_plan)
    if ticket_copy is not None:
        return _ticket_copy_preamble(ticket_copy)
    return None


def _cluster_member_specs(home, members: Sequence[str]) -> dict[str, str]:
    """멤버 티켓 본문 — 프롬프트에 실릴 실값(선언 순서 보존)."""
    specs: dict[str, str] = {}
    for ticket in members:
        found = ticket_store.find_ticket_exact(home, ticket)
        if found is None:
            raise delegate_errors.DelegateError(f"ticket not found: {ticket}")
        _status, path = found
        try:
            specs[ticket] = fs.read_text_shared(
                path, encoding="utf-8", newline="",
            )
        except (OSError, UnicodeError) as exc:
            raise delegate_errors.DelegateError(f"PM 홈 티켓 읽기 실패: {path}: {exc}") from exc
    return specs


def cluster_review_sources(
    home, cluster: str, *, repo: Path, focus_path: Path | None,
    pm_home: Path, git_run_fn: Callable | None = None,
) -> ClusterReviewSources:
    """리뷰 프롬프트 입력을 읽는다(부작용 0) — 입력 해소 안에서 준비 트리 조건을 결속한다."""
    review = cluster_review_input(home, cluster, repo=repo, git_run_fn=git_run_fn)
    specs = _cluster_member_specs(pm_paths.home_at(Path(pm_home).resolve()), review.members)
    focus = (
        _cluster_review_focus(Path(focus_path), cwd=repo, pm_home=pm_home)
        if focus_path is not None else None
    )
    return ClusterReviewSources(review, specs, focus)


def prepare_cluster_review(
    *, cluster: str, cwd: Path, pm_home: Path, focus_path: Path | None,
    owner_pid: int | None, reopen_ordinal: int | None = None,
    git_run_fn: Callable | None = None,
) -> tuple[ClusterReviewSources, ClusterCopyPlan]:
    """두 전송이 같이 쓰는 리뷰 준비 — 준비 트리 조건·리뷰 입력·명세·검토 중점 판독 뒤에 예약한다.

    거부는 전부 예약 앞이다(board·장부·run-dir 무변경). 예약은 `prepare_cluster_copy` 가
    멤버마다 code-reviewer 라운드를 깔고 장부 행에 `base_rev`(돌려받기 트리 조건의 기준)를 남긴다.
    `owner_pid` 는 준비 프로세스가 run 의 소유자일 때(교차 실행)만 준다.
    """
    home = pm_paths.home_at(Path(pm_home).resolve())
    sources = cluster_review_sources(
        home, cluster, repo=cwd, focus_path=focus_path, pm_home=pm_home,
        git_run_fn=git_run_fn,
    )
    plan = prepare_cluster_copy(
        cluster=cluster, role=review_form.INTERNAL_REVIEW_ROLE, cwd=cwd, pm_home=pm_home,
        owner_pid=owner_pid, reopen_ordinal=reopen_ordinal,
    )
    return sources, plan


def cluster_review_prompt(
    sources: ClusterReviewSources, *, tree: Path, plan: ClusterCopyPlan | None,
    adapter_roots: Sequence[str],
) -> str:
    """리뷰 프롬프트 전문 — 역할 머리말 + 좌석 안내 + 리뷰 본문(두 전송이 같은 조립).

    `plan` 이 없으면(미리보기) 좌석 안내가 빠진다 — 자리가 없는 실행은 자리를 말하지 않는다.
    """
    preamble = _role_preamble(review_form.INTERNAL_REVIEW_ROLE, adapter_roots)
    note = _round_copy_preamble(None, plan)
    if note is not None:
        preamble += "\n" + note
    return preamble + "\n\n" + build_cluster_review_prompt(
        sources.review, sources.specs, tree=tree, focus=sources.focus,
    )


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



def harness_cap_advisory(env: dict[str, str] | None = None,
                         *, execution_budget: int | None = None) -> str | None:
    """기존 채택자의 런타임 Bash 상한이 엔진 실행 경로보다 낮으면 loud advisory.

    adapter settings 는 engine.manifest 밖 인스턴스 소유라 pm_update 로 정적 상향할 수 없다. 실행
    시 실제 env 를 읽어 조용한 무력화를 막는다. 기본 선언 최악과 이번 실행의 해소 예산 중 큰 쪽을
    요구치로 삼아 local.conf/CLI 상향도 놓치지 않는다. 알 수 없는/불량 값 역시 조용히 통과시키지
    않고 설정 표면을 명시한다(advisory·실행은 차단하지 않음).
    """
    env = os.environ if env is None else env
    if not _session_harnesses(env):
        return None
    session_markers = watchdog.HARNESS_SESSION_MARKERS
    cap_env = watchdog.HARNESS_CAP_ENV
    required = delegate_env.max_declared_execution_path_budget(pm_paths.home_at(REPO, pm_home=_CONFIG_REPO_OVERRIDE))
    if execution_budget is not None:
        required = max(required, execution_budget)
    return watchdog.harness_cap_advisory(
        env, execution_budget=required,
        session_markers=session_markers, cap_env=cap_env,
        render_missing=lambda pm_harness, cap_key, required: (
                f"[pm-delegate] 경고: {pm_harness} 하네스 상한 {cap_key} 미해소 — "
                f"엔진 최악 실행 경로+정리 여유 {required}s 이상으로 설정해야 하네스 선행 kill을 막습니다."
        ),
        render_invalid=lambda pm_harness, cap_key, raw, required: (
                f"[pm-delegate] 경고: {pm_harness} 하네스 상한 {cap_key}={raw!r} 해석 불가 — "
                f"유한한 정수 ms로 {required}s 이상 설정하세요."
        ),
        render_low=lambda pm_harness, cap_key, cap_sec, required: (
                f"[pm-delegate] 경고: {pm_harness} 하네스 상한 {cap_sec:g}s < "
                f"엔진 최악 실행 경로+정리 여유 {required}s — 엔진 무진행 진단/부분 산출물 보존 전에 "
                f"하네스가 kill할 수 있습니다. {cap_key}를 최소 {required * 1000}ms로 상향하세요."
        ),
    )


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
    conf_path = _local_conf_path(config_repo)
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
            return fail_loud(delegate_raw.cold_reinjection_rejection(
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
        return fail_loud(f"오류: {exc}")
    if args.cluster is None:
        task_text = delegate_raw.append_attached_raw(task_text, attached_raw)

    # cross 실위임의 라운드 준비. dry-run은 미호출/무부수효과 계약이라 만들지 않고, 하네스로
    # 위임되는 역할의 실제 실행만 prepare한다(ticket 없는 legacy 호출은 종전 형상 유지).
    # 묶음 리뷰는 native 준비와 같은 함수(`prepare_cluster_review`)로 준비 트리 조건·리뷰 입력 판독 뒤
    # 예약한다 — dry-run 은 같은 입력을 읽기만 한다(`cluster_review_sources`).
    ticket_copy: TicketCopyPlan | None = None
    cluster_plan: ClusterCopyPlan | None = None
    cluster_sources: ClusterReviewSources | None = None
    if args.cluster is not None:
        focus_path = Path(args.focus) if args.focus else None
        try:
            if args.dry_run:
                cluster_sources = cluster_review_sources(
                    pm_paths.home_at(Path(config_repo).resolve()), args.cluster, repo=cwd_repo,
                    focus_path=focus_path, pm_home=config_repo, git_run_fn=git_run_fn,
                )
            else:
                cluster_sources, cluster_plan = prepare_cluster_review(
                    cluster=args.cluster, cwd=cwd_repo, pm_home=config_repo,
                    focus_path=focus_path, owner_pid=os.getpid(), git_run_fn=git_run_fn,
                )
                # 쓰기 자리·run_id·환불 좌표는 run 단위라 대표 자리 하나로 잡아도 같은 값이다.
                # 티켓별로 갈리는 회수는 아래에서 `cluster_plan.rounds` 를 그대로 순회한다.
                ticket_copy = cluster_plan.rounds[0]
        except delegate_errors.DelegateError as exc:
            return fail_loud(f"오류: 묶음 리뷰 준비 실패: {exc}")
    elif not args.dry_run and effective_ticket and args.role in TICKET_COPY_PREPARE_ROLES:
        try:
            ticket_copy = prepare_ticket_copy(
                ticket=effective_ticket, role=args.role, cwd=cwd_repo, pm_home=config_repo,
                # 이 run 의 소유자는 **이 프로세스**다 — 준비·실행·회수가 한 try/finally 라
                # 이 pid 가 살아 있으면 run 도 살아 있다. 포기(abandon) 판정의 유일한 기계
                # 증거이고, 소유자가 아닌 native 준비는 이 값을 싣지 않는다.
                owner_pid=os.getpid(),
            )
        except delegate_errors.DelegateError as exc:
            return fail_loud(f"오류: 위임 티켓 라운드 준비 실패: {exc}")

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
            task_text = delegate_raw.append_attached_raw(build_cluster_review_prompt(
                cluster_sources.review, cluster_sources.specs,
                tree=cwd_repo, focus=cluster_sources.focus,
            ), attached_raw)
            prompt = delegate_raw.append_attached_raw(cluster_review_prompt(
                cluster_sources, tree=cwd_repo, plan=cluster_plan,
                adapter_roots=adapter_roots,
            ), attached_raw)
        else:
            preamble = _role_preamble(args.role, adapter_roots)
            round_note = _round_copy_preamble(ticket_copy, None)
            if round_note is not None:
                preamble += "\n" + round_note
            prompt = preamble + "\n\n" + task_text

        # 세션 재사용 해소 — **호출 전 게이트보다 앞**이다. 재사용 라운드가 실제로 내보내는 건 delta
        # payload 라, 게이트(재앵커·시크릿 스캔)가 그걸 못 보면 검사받지 않은 본문이 나간다. 재사용
        # 실패 시 full payload 로 돌아가므로 두 본문 **모두**를 게이트 입력으로 합친다.
        resume = resolve_resume_plan(
            args.resume_from, harness=harness, role=args.role,
            task_text=task_text, output_dir=output_dir,
        )
        outgoing_text = (
            prompt if resume is None else "\n".join((resume.delta_prompt, prompt))
        )

        # 쓰기-타깃 axis 재앵커 게이트 (엔진 코드 write + PM 홈 cwd·dry-run 전 = 미리보기서 노출)
        # divergence와 같은 cwd repo 해소 입력을 기존 write-target 가드에도 재사용한다. 하위 디렉토리
        # `--cwd`가 PM 홈 가드를 우회하는 별도 판정축을 만들지 않는다.
        reanchor = check_write_target_reanchor(args.role, cwd_repo or cwd, outgoing_text)
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
        cap_warning = harness_cap_advisory(execution_budget=execution_budget)
        if cap_warning is not None:
            print(cap_warning, file=sys.stderr)

        # 기존 `--cwd` 신뢰 repo축을 파생 config_repo와 대조한다. 양쪽 모두 설정된 fallback 값만
        # 공용 seam이 비교한다.
        compare_fallback = fallback is not None
        try:
            _resolved_cwd_repo, divergence = check_local_conf_divergence(
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
        touch_overlaps: tuple[TicketTouchOverlap, ...] = ()
        if effective_ticket and args.role in delegate_run_rules.WRITE_ROLES:
            try:
                touch_overlaps = claimed_touch_overlaps(
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
                overlap_warning = format_touch_overlap_warning(
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
        scope_audit = begin_scope_audit(
            effective_ticket, cwd, pm_root=board_repo, adapter_roots=adapter_roots,
        )
        if scope_audit is not None and touch_overlaps:
            # 실행 전에 이미 경고한 겹침 경로를 회수 시점 판정 입력으로 싣는다. 캡처 함수의
            # 입력이 아니라 이 위임이 무엇을 겹친다고 알렸는지의 기록이다.
            scope_audit = scope_audit._replace(
                overlap_paths=overlap_touch_paths(touch_overlaps),
            )
        try:
            target_rev = (
                scope_audit.before.head or None if scope_audit is not None else None
            )
            print(
                f"하네스 {harness}(model={model}) 실행 중 (유료 호출).",
                file=sys.stderr,
            )
            advisory = native_advisory(harness, args.role)
            if advisory is not None:
                print(advisory, file=sys.stderr)
            _ticket_copy_handed_off = True  # 여기부터 harvest 가 정리를 넘겨받는다
            return _execute_and_collect(
                args=args, harness=harness, model=model, reasoning=reasoning,
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
            report_scope_audit(scope_audit, args.role)
            if ticket_copy is not None and _ticket_copy_handed_off:
                try:
                    if cluster_plan is not None:
                        # 묶음은 자리마다 독립 회수다 — 한 자리의 거부가 다른 자리의 교체를
                        # 되돌리지 않는다(회수 판정식은 단일 경로와 같은 하나다).
                        _report_cluster_harvest(harvest_cluster_copy(
                            run_dir=cluster_plan.run_dir, cwd=cwd_repo,
                            pm_home=config_repo,
                        ))
                    else:
                        result = harvest_ticket_copy(
                            copy_path=ticket_copy.path, cwd=cwd_repo, pm_home=config_repo,
                        )
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
                    recovery = _delegation_harvest_failure_message(exc, residue)
                    if pending_exception is not None:
                        print(
                            "오류: 위임 실행 예외도 그대로 전파합니다: "
                            f"{type(pending_exception).__name__}: {pending_exception}",
                            file=sys.stderr,
                        )
                        fail_loud(recovery)
                    else:
                        if _is_engine_rev_skew(exc):
                            raise
                        return fail_loud(recovery)
    finally:
        if ticket_copy is not None and not _ticket_copy_handed_off:
            seats = (
                tuple(cluster_plan.rounds) if cluster_plan is not None
                else (ticket_copy,)
            )
            for seat in seats:
                _refund_gate_rejected_ticket_copy(
                    seat, cwd=cwd_repo, pm_home=config_repo,
                )


def _delegation_harvest_failure_message(exc: Exception, residue: Path) -> str:
    """cross 위임 종료의 회수 실패 처방 — terminal fix만 복구 루프를 열지 않는다."""
    if isinstance(exc, TerminalFixHarvestError):
        return (
            "오류: 최종 fix ticket harvest 실패 — terminal stop. "
            "board 라운드와 slot run-dir 증거를 그대로 보존했습니다. "
            "현재 티켓 상태와 실패 근거를 사용자에게 보고하세요. "
            f"copy={residue} · {exc}"
        )
    return (
        "오류: 위임 종료 ticket harvest 실패 — board 라운드와 slot run-dir 을 "
        "그대로 보존했습니다. run-dir 을 진단한 뒤 같은 경로로 "
        "`ticket harvest` 를 다시 부르거나 새 prepare/위임으로 복구하세요. "
        f"copy={residue} · {exc}"
    )


def _report_cluster_harvest(outcomes: Sequence[ClusterHarvestOutcome]) -> None:
    """묶음 회수 결과 요약 — 자리마다 한 줄. 거부가 하나라도 있으면 실패로 올린다.

    자리별 독립 회수라 성공한 자리는 그대로 두되, 남은 거부를 성공 rc 로 덮지 않는다
    (단일 경로의 회수 실패 처리와 같은 축 — 호출부의 복구 안내가 그대로 붙는다).
    """
    refused: list[str] = []
    for outcome in outcomes:
        if outcome.refusal is not None:
            refused.append(outcome.ticket)
            print(
                f"오류: 위임 종료 ticket harvest 거부: {outcome.ticket}: "
                f"{outcome.refusal}",
                file=sys.stderr,
            )
            continue
        print(
            "[pm-delegate] ticket harvest: "
            f"{'applied' if outcome.result.changed else 'unchanged'} · "
            f"sync_ready={str(outcome.result.sync_ready).lower()} · "
            f"copy={outcome.copy}",
            file=sys.stderr,
        )
    if refused:
        message = f"묶음 회수 거부 {len(refused)}건: {', '.join(refused)}"
        if any(outcome.terminal for outcome in outcomes):
            raise TerminalFixHarvestError(message)
        raise delegate_errors.DelegateError(message)


def _round_write_scope(
    ticket_copy: "TicketCopyPlan | None", role: str,
) -> Path | None:
    """이 실행에 열어 줄 쓰기 자리 — 라운드 준비가 있었던 리뷰 실행만 실값이다.

    값은 준비 계획이 실은 **묶음 run-dir**(`ClusterCopyPlan.run_dir`) 그대로다. 좌표가 없는
    계획으로 여기 오면 쓰기 자리를 조용히 좁히지 않고 멈춘다 — 권한 표면을 말없이 강등하면
    "실행은 됐는데 산출을 못 쓴" 라운드가 위임 실패로만 보인다.
    """
    if ticket_copy is None or role != review_form.INTERNAL_REVIEW_ROLE:
        return None
    if ticket_copy.cluster_run_dir is None:
        raise delegate_errors.DelegateError(
            f"라운드 준비 계획에 묶음 run-dir 좌표가 없습니다: {ticket_copy.path} — "
            "쓰기 자리를 좁히지 않고 중단합니다."
        )
    return ticket_copy.cluster_run_dir


def _execute_and_collect(
    *,
    args: argparse.Namespace,
    harness: str,
    model: str,
    reasoning: str | None,
    fallback: tuple[str, str, str | None] | None,
    fallback_skip: str | None,
    cwd: Path,
    prompt: str,
    timeout: int,
    output_dir: Path | None,
    run_fn: Callable,
    fallback_timeout: int | None = None,
    local_conf_path: Path | None = None,
    profile_source: str = "local-conf",
    resume: delegate_run_rules.ResumePlan | None = None,
    ticket: str | None = None,
    fresh_reason: str | None = None,
    base_rev: str | None = None,
    ticket_copy: TicketCopyPlan | None = None,
    cluster_plan: ClusterCopyPlan | None = None,
) -> int:
    """primary(+선택적 폴백) 실행과 결과 회수 — main 의 종료 rc 를 그대로 낸다.

    main 에서 분리한 이유는 위임 범위 판정이 **모든 종료 경로에서 정확히 1회** 돌아야 하기
    때문이다 — 호출부의 try/finally 가 그 경계다.

    `fallback_timeout` = 폴백 하네스의 벽시계 백스톱(미지정이면 primary 값). 값이 하네스별이라
    다른 축으로 폴백하면 예산도 그 축의 것이어야 한다(클라우드→로컬 GPU 폴백에서 로컬 작업이
    클라우드 예산에 잘리는 걸 막는다).

    `resume` 가 있으면 **delta payload 를 그 세션으로** 먼저 보낸다. 성공 판정은 회신 세션 id 가
    요청한 id 와 같은가 하나뿐이다(rc 로 판정하지 않는다). 확정된 "세션 없음"은 delta 미소비라
    write/read 모두 fresh + full payload 재실행이 안전하다. 반면 rc=0 명시적 id 불일치는 turn 이
    이미 실행됐을 수 있어 `RESUME_MUTATING_ROLES`(위임 4역할 전부. write 역할은 작업 트리, read
    역할은 라운드 파일을 쓴다)는 재실행하지 않는다. 그 밖의 역할만 fresh 로 다시 돈다. 미분류
    `rc≠0` 은 **호출 후** 실패일 수 있어 재실행하지 않는다(중복 과금·중복 호출 차단·기존
    fail-loud)."""
    try:
        primary = _execute_attempt(
            harness=harness,
            model=model,
            reasoning=reasoning,
            role=args.role,
            cwd=cwd,
            prompt=_with_ticket_copy_preamble(
                prompt if resume is None else resume.delta_prompt, ticket_copy,
                cluster_plan,
            ),
            timeout=timeout,
            output_dir=output_dir,
            run_fn=run_fn,
            attempt="primary" if resume is None else delegate_run_rules.RESUME_ATTEMPT,
            local_conf_path=local_conf_path,
            profile_source=profile_source,
            resume_session_id=None if resume is None else resume.session_id,
            ticket=ticket,
            fresh_reason=fresh_reason,
            base_rev=base_rev,
            cluster_run_dir=_round_write_scope(ticket_copy, args.role),
            run_id=(ticket_copy.run_id if ticket_copy is not None else None),
            round_copy_path=(
                str(ticket_copy.path) if ticket_copy is not None else None
            ),
        )
        # 재실행은 **재사용 축의 확정된 실패에만** 쓴다(`resume_rerun_reason`) — 깨끗한 완료의
        # 명시적 세션 id 불일치, 또는 확정된 "세션 없음" 오류. 미분류 `rc≠0` 은 **호출 후** 죽은
        # 실행일 수 있어(프롬프트는 이미 나갔다) full payload 재실행이 곧 중복 과금·중복 외부
        # 호출이다 — 그런 실패는 기존 fail-loud/폴백 축이 그대로 소유한다. 인프라 실패(스폰·
        # 타임아웃·한도)도 세션 id 를 못 남겨 형식만 보면 "불일치"라, 분류 결과를 함께 본다.
        rerun_reason = (
            None if resume is None
            else delegate_run.resume_rerun_reason(
                primary.result, primary.session_id, resume.session_id)
        )
        if (
            rerun_reason is not None
            and delegate_run.classify_infrastructure_failure(primary.result) is None
        ):
            # 확정된 세션 부재(rc≠0 양성 오류)는 delta 가 소비되지 않아 모든 역할이 fresh 로
            # 안전하다. rc=0 세션-id 불일치는 turn 이 이미 돌았을 수 있으므로 쓰기 자리가 있는
            # 역할(`RESUME_MUTATING_ROLES`: write 역할은 작업 트리, read 역할은 라운드 파일)은
            # 재실행을 막는다.
            if (
                args.role in delegate_run_rules.RESUME_MUTATING_ROLES
                and rerun_reason == delegate_run.RESUME_RERUN_ID_MISMATCH
            ):
                return fail_loud(
                    f"오류: 세션 재사용 실패({args.role}·{rerun_reason}) — 요청 "
                    f"{resume.session_id} · 회신 {primary.session_id or '없음'}.\n"
                    "  · 회신이 다른 세션에서 왔다. 이 실행이 이미 작업 트리나 라운드 파일을 "
                    "고쳤을 수 있어 fresh 재실행을 자동으로 하지 않는다(중복·충돌 기록 차단).\n"
                    "  · 먼저 작업 트리(`git status` · `git diff`)와 이 역할의 라운드 파일을 "
                    "확인한다. 남은 변경이 없으면 `--resume-from` 없이 같은 위임을 다시 부르고, "
                    "부분 적용이 있으면 그 상태를 반영해 프롬프트를 고쳐 부른다.\n"
                    f"  · resume raw: {primary.raw_path}"
                )
            print(
                f"세션 재사용 실패({rerun_reason}): 요청 {resume.session_id} · 회신 "
                f"{primary.session_id or '없음'} — fresh 스폰 + full payload 로 이 라운드를 "
                f"1회 다시 실행합니다(적재 비용 1회 추가). resume raw: {primary.raw_path}",
                file=sys.stderr,
            )
            primary = _execute_attempt(
                harness=harness,
                model=model,
                reasoning=reasoning,
                role=args.role,
                cwd=cwd,
                prompt=_with_ticket_copy_preamble(prompt, ticket_copy, cluster_plan),
                timeout=timeout,
                output_dir=output_dir,
                run_fn=run_fn,
                attempt=delegate_run_rules.RESUME_FRESH_FALLBACK_ATTEMPT,
                primary_raw=str(primary.raw_path),
                    local_conf_path=local_conf_path,
                profile_source=profile_source,
                ticket=ticket,
                fresh_reason=fresh_reason,
                base_rev=base_rev,
                cluster_run_dir=_round_write_scope(ticket_copy, args.role),
                run_id=(ticket_copy.run_id if ticket_copy is not None else None),
                round_copy_path=(
                    str(ticket_copy.path) if ticket_copy is not None else None
                ),
            )
    except (OSError, delegate_errors.DelegateError) as exc:
        return fail_loud(f"오류: 위임 실행 준비/raw 박제 실패: {exc}")

    # 실패 분류 → 선택적 loud 폴백. rc=0 reply(반려/must-fix 포함)는 분류 함수가 절대 폴백시키지
    # 않는다. 알려지지 않은 rc≠0도 기존 fail-loud로 남는다(오분류 보수 방향).
    result = primary.result
    raw_path = primary.raw_path
    rc = result.get("returncode", 1)
    stdout = result.get("stdout", "")
    timed_out = result.get("timed_out", False)
    failure_class = delegate_run.classify_infrastructure_failure(result)

    if failure_class is not None and fallback is None and fallback_skip is not None:
        # 설정은 있으나 이번 실행에선 폴백을 끈다 — 조용히 지나가지 않는다(loud skip).
        print(
            f"폴백 비발동: 인프라 실패({failure_class})이지만 {fallback_skip}. "
            "기존 fail-loud 로 진행한다.",
            file=sys.stderr,
        )

    if failure_class is not None and fallback is not None:
        fallback_harness, fallback_model, fallback_reasoning = fallback
        loud = (
            f"폴백: {harness}→{fallback_harness}({fallback_model}) — "
            f"사유: {failure_class}"
        )
        print(loud, file=sys.stderr)
        print(
            f"하네스 {fallback_harness}(model={fallback_model}) 로 폴백 프롬프트 실행 중 "
            "(과금·1단 폴백).",
            file=sys.stderr,
        )
        advisory = native_advisory(fallback_harness, args.role)
        if advisory is not None:
            print(advisory, file=sys.stderr)
        try:
            fallback_attempt = _execute_attempt(
                harness=fallback_harness,
                model=fallback_model,
                reasoning=fallback_reasoning,
                role=args.role,
                cwd=cwd,
                prompt=_with_ticket_copy_preamble(prompt, ticket_copy, cluster_plan),
                timeout=fallback_timeout if fallback_timeout is not None else timeout,
                output_dir=output_dir,
                run_fn=run_fn,
                attempt=f"fallback-from-{harness}:{failure_class}",
                primary_raw=str(raw_path),
                local_conf_path=local_conf_path,
                profile_source="local-conf",
                ticket=ticket,
                fresh_reason=fresh_reason,
                base_rev=base_rev,
                cluster_run_dir=_round_write_scope(ticket_copy, args.role),
                run_id=(ticket_copy.run_id if ticket_copy is not None else None),
                round_copy_path=(
                    str(ticket_copy.path) if ticket_copy is not None else None
                ),
            )
        except (OSError, delegate_errors.DelegateError) as exc:
            return fail_loud(
                f"오류: 폴백 실행 준비/raw 박제 실패: {exc}. primary raw: {raw_path}")

        fallback_result = fallback_attempt.result
        fallback_rc = fallback_result.get("returncode", 1)
        fallback_stdout = fallback_result.get("stdout", "")
        if fallback_result.get("timed_out", False):
            actual_timeout = delegate_run._timeout_result_summary(
                fallback_result,
                fallback_timeout=(fallback_timeout if fallback_timeout is not None else timeout),
            )
            return fail_loud(
                f"오류: 폴백 위임 turn 타임아웃({actual_timeout}) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")
        if fallback_rc != 0:
            return fail_loud(
                f"오류: 폴백 하네스 실패(rc={fallback_rc}) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")
        try:
            fallback_reply = (
                delegate_run.extract_reply(fallback_harness, fallback_stdout) if fallback_stdout else None
            )
        except (ValueError, UnicodeError, delegate_errors.DelegateError) as exc:
            return fail_loud(
                f"오류: 폴백 reply 추출 실패: {exc}. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")
        if not fallback_reply or not fallback_reply.strip():
            return fail_loud(
                "오류: 폴백 위임 reply 미추출(빈 출력·파싱 실패) — 2차 폴백 없음. "
                f"primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}")

        # stdout 결과에 provenance를 넣어 PM 회수 reply 자체가 폴백 사실을 보존한다.
        print(
            "[pm-delegate] 실행 provenance: "
            f"fallback={fallback_harness}(model={fallback_model}) · "
            f"primary={harness}(model={model}) · 사유={failure_class}"
        )
        print(fallback_reply)
        print(
            f"[pm-delegate] primary raw: {raw_path} · fallback raw: {fallback_attempt.raw_path}",
            file=sys.stderr,
        )
        return 0

    # 미설정 또는 비-인프라 실패 → 현행 fail-loud(rc=1 + stderr + raw 경로). 종료는 전부
    # `fail_loud` 단일 깔때기를 거친다 — 실패 안내가 다른 수신자를 권하면 그 권유가 곧 무기록
    # 대행의 입구이고, 지점마다 손으로 안내를 잇는 방식은 새 경로에서 반드시 빠진다.
    if timed_out:
        actual_timeout = delegate_run._timeout_result_summary(result, fallback_timeout=timeout)
        return fail_loud(
            f"오류: 위임 turn 타임아웃({actual_timeout}) — 프로세스그룹 종료. raw: {raw_path}")
    if result.get(delegate_run_rules.RUN_RESULT_CLEANUP_FAILED):
        return fail_loud(
            "오류: 위임 프로세스 정리 실패 — primary가 아직 살아 있을 수 있어 자동 폴백을 "
            f"실행하지 않습니다(중복 실행·과금·worktree 동시 편집 차단). 사람 확인 필요. raw: {raw_path}")
    if rc != 0:
        return fail_loud(f"오류: 위임 하네스 실패(rc={rc}). raw: {raw_path}")

    try:
        reply = delegate_run.extract_reply(harness, stdout) if stdout else None
    except (ValueError, UnicodeError, delegate_errors.DelegateError) as exc:
        return fail_loud(f"오류: reply 추출 실패: {exc}. raw: {raw_path}")
    if not reply or not reply.strip():
        return fail_loud(f"오류: 위임 reply 미추출(빈 출력·파싱 실패). raw: {raw_path}")

    print(reply)
    print(f"[pm-delegate] raw: {raw_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
