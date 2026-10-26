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


import ast
import os
import sys
from pathlib import Path
from typing import Callable

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TOOLS_DIR not in sys.path:
    sys.path.insert(0, _TOOLS_DIR)
from pm_engine.cli import delegate_run
from pm_engine.infra import console, fs, pm_paths


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













def main(argv: list[str] | None = None, run_fn: Callable | None = None,
         git_run_fn: Callable | None = None) -> int:
    """CLI 진입 — 콘솔 인코딩 설정이 첫 동작이다(인자 오류처럼 CLI 본체 앞뒤에서 나가는 출력도
    같은 콘솔 설정 아래 있어야 한다 · 형제 진입점 관용구와 동형)."""
    console.configure_console_utf8()
    return delegate_run.main(pm_paths.home_at(REPO), argv, run_fn=run_fn,
                             git_run_fn=git_run_fn, adapter_directories_fn=_resolved_adapter_directories)




if __name__ == "__main__":
    sys.exit(main())
