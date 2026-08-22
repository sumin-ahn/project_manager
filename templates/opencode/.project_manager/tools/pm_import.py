#!/usr/bin/env python3
"""pm_import — PM 프레임워크 import 단일 진입 커맨드 (--new = PM 홈 생성 / --into = 기존 프로젝트 임베드).

현행 채택 플로우(`docs/manual-import.md` 의 수동 longhand: `cp -r` + sed +
`board.py init` + 손)의 **기계 단계**(결정적·무LLM)를 1 커맨드로 대체하고, 그 위에
sed 로 못 채우는 **자유서술 placeholder** 채움(하니스 헤드리스 구동·opt-in)을 얹는다.

사용:
    pm_import.py (--into <기존프로젝트> | --new <프로젝트>)   # 모드 택1
                 [--harness <harness[,harness...]|all>]      # 어댑터 집합 (default: all)
                 --weight  {full,lite}                        # 무게축 (default: full)
                 [--from <프레임워크-checkout>]               # 소스 (default: 이 repo 루트)
                 [--name <표시이름>]                          # {{PROJECT_NAME}} (default: 디렉토리명)
                 [--fill {auto,manual}]                       # 자유서술 채움 (default: manual)
                 [--fill-harness {@REGISTERED_FILL_HARNESSES@}] # 구동 하니스 (default: --harness)
                 [--dry-run]                                  # 적용 없이 계획만 출력(fill 미호출)

동작:
  소스 = <--from>/templates/<harness>/ 트리(엔진 + 어댑터)를 대상으로 복사한다.
  콤마 선택 또는 `all`이면 선택된 어댑터 트리를 집합으로 병합 복사한다.
  복사 후 operational placeholder 를 sed 치환하고 `board.py init`(무인자)을 호출한다
  — 그 호출이 areas.md 에 이 clone 의 repo 행을 빈 prefix(=none 카테고리)로 등록한다.
    - sed 대상 = {{PROJECT_NAME}}·{{PROJECT_TAGLINE}}·{{PROJECT_ROOT}}·{{PY}}·{{TEST_CMD}}·{{DATE}}.
    - 엔진 문서(wiki/pm_role.md·pm_playbook.md)는 sed 제외 — local.conf 가 런타임 해소.
    - 자유서술 3종({{PROJECT_CONSTRAINTS}}·{{PROTECTED_PATHS}}·{{USER_GATE_ITEMS}})은 보존(아래 fill).
  board init·local.conf 동기화 직후 **fill 단계**가 자유서술 placeholder 를 처리한다:
    - --fill manual(기본): 하니스 미구동, placeholder 를 `<!-- TODO: ... -->` 로 표시(채택자가 손으로).
    - --fill auto: 대상 repo 분석 프롬프트로 하니스(claude -p / opencode run --format json /
      codex exec --json)를
      헤드리스 구동해 placeholder 값 + (해당 시) CLAUDE.md/pm_role.local.md 초안을 *제안*한다.
      생성물은 제안일 뿐 — 적용은 사용자 리뷰 전제(비가역 회피). --dry-run 이면 실 하니스를
      호출하지 않고 fill *계획*(채울 대상 토큰·결정된 harness·opt-in 게이트 상태)만 출력한다
      (파일 미변경·비용 0 — opt-in 게이트상 dry-run 에서 실호출 금지).
  --into: 기존 충돌 파일은 중앙 디렉토리 .pm_import_backups/<DATE>/<relpath> 에 백업 후 덮음
          (비파괴). 단 git 이 추적 중이고 미변경인 파일은 백업 생략(git 이 복원). --new:
          디렉토리 생성 + git init.

결정:
  - 독립 pm_import.py (board.py 비대화). stdlib only.
  - idempotent — 재실행 시 백업하고 안전. --dry-run 은 파일시스템 미변경(plan/apply 분리).
  - --weight lite 는 진입 파일 선택만 영향. 어댑터의 `X.lite.md`(예 CLAUDE.lite.md·
    AGENTS.lite.md)를 dst `X.md` 로 rename 배치하고 full `X.md`·원본 `*.lite.md` 는 제외한다.
    full(기본)은 모든 `*.lite.md` 를 제외하고 full 진입(X.md)만 깐다.
  - **적용 단계의 파일 단위 제외는 rc 0 이고 신호는 stderr 요약이다.** 계획을 통과한 대상이 적용
    중에 교체·삭제되거나 백업 자리가 막혀 빠지면 그 파일만 건너뛰고 `⚠️ …` 요약을 stderr 로 낸다
    (백업 못 하는 파일은 고치지 않는다). 나머지 설치·추가는 그대로 완료되므로 비0 은 "아무것도
    안 됐다"는 잘못된 신호가 된다 — 백업 자리가 막힌 공유 문서를 재렌더에서 빼고 rc 0 으로 끝내는
    기존 처리와 같은 규칙이다. 계획 단계 위반은 반대다: 아직 아무것도 복사하지 않았으므로 전체를
    rc 1 로 멈춘다(부분 설치 0). dest 루트 자체 교체만 예외로 적용 중에도 즉시 전체 중단이다.
  - fill opt-in 게이트(external_review 선례): 하니스 실구동은 토큰·외부모델 비용 → 기본 OFF.
    **실호출은 환경변수 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.** 둘 중 하나라도
    없으면 실 runner 를 호출하지 않는다(CI·기본 테스트는 stub). 회사 배포(claude code 없음)는
    opencode 구동 경로 1급 — 혼합이면 등록 순서상 첫 가용 하네스를 택한다.

opt-in 실 e2e (CI 비포함 — 토큰·외부모델 비용 발생):
    1) 대상 하니스 바이너리 설치 확인 (@REGISTERED_FILL_BINARIES@ 중 선택한 것이 PATH 에 있어야 함).
    2) 환경변수와 플래그를 *동시* 지정해 실구동:
           PM_IMPORT_LIVE_HARNESS=1 pm_import.py --into <repo> --fill auto [--fill-harness opencode]
       - 둘 중 하나만 주면 실호출이 차단되고 stub/manual 로 폴백한다(안전).
    3) 출력된 자유서술 placeholder 값·초안은 *제안*이다 — 사람이 검토 후 손으로 반영한다
       (pm_import 는 자유서술 채움을 자동 확정하지 않는다). --dry-run 은 실 하니스를 호출하지
       않고 *fill 계획*(채울 토큰·harness·게이트 상태)만 미리 보여준다(파일 미변경).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import difflib
import errno
import functools
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path, PurePosixPath
from typing import Callable, NamedTuple, NoReturn

_TOOLS_BOOTSTRAP = os.path.dirname(os.path.abspath(__file__))
_TOOLS_BOOTSTRAP_FILE = os.path.realpath(
    os.path.join(_TOOLS_BOOTSTRAP, "repo_owned_files.py")
)
_TOOLS_BOOTSTRAP_KEY = f"_project_manager_repo_owned_files_bootstrap:{_TOOLS_BOOTSTRAP_FILE}"
_TOOLS_BOOTSTRAP_MODULE = sys.modules.get(_TOOLS_BOOTSTRAP_KEY)
_TOOLS_BOOTSTRAP_SENTINEL = object()
try:
    if (
        _TOOLS_BOOTSTRAP_MODULE is not None
        and os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
        != _TOOLS_BOOTSTRAP_FILE
    ):
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
        _TOOLS_BOOTSTRAP_MODULE = None
    if _TOOLS_BOOTSTRAP_MODULE is None:
        _TOOLS_BOOTSTRAP_PREVIOUS = sys.modules.pop(
            "repo_owned_files", _TOOLS_BOOTSTRAP_SENTINEL
        )
        _TOOLS_BOOTSTRAP_ADDED = not sys.path or sys.path[0] != _TOOLS_BOOTSTRAP
        if _TOOLS_BOOTSTRAP_ADDED:
            sys.path.insert(0, _TOOLS_BOOTSTRAP)
        try:
            import repo_owned_files as _TOOLS_BOOTSTRAP_MODULE
            if (
                os.path.realpath(getattr(_TOOLS_BOOTSTRAP_MODULE, "__file__", ""))
                != _TOOLS_BOOTSTRAP_FILE
            ):
                raise ImportError(
                    "repo_owned_files 형제 경로 불일치: "
                    f"{getattr(_TOOLS_BOOTSTRAP_MODULE, '__file__', None)!r}"
                )
            sys.modules[_TOOLS_BOOTSTRAP_KEY] = _TOOLS_BOOTSTRAP_MODULE
        finally:
            # 엔진 import bootstrap은 메인 스레드 전용이다. 그래도 위치를 가정한 pop(0)은
            # 피하고, 우리가 넣은 값이 남아 있을 때 그 값만 제거한다.
            if _TOOLS_BOOTSTRAP_ADDED:
                try:
                    sys.path.remove(_TOOLS_BOOTSTRAP)
                except ValueError:
                    pass
            if sys.modules.get("repo_owned_files") is _TOOLS_BOOTSTRAP_MODULE:
                sys.modules.pop("repo_owned_files", None)
            if _TOOLS_BOOTSTRAP_PREVIOUS is not _TOOLS_BOOTSTRAP_SENTINEL:
                sys.modules["repo_owned_files"] = _TOOLS_BOOTSTRAP_PREVIOUS
    _load_module_from_path = _TOOLS_BOOTSTRAP_MODULE.load_module
except Exception as _TOOLS_BOOTSTRAP_ERROR:
    if sys.modules.get(_TOOLS_BOOTSTRAP_KEY) is _TOOLS_BOOTSTRAP_MODULE:
        sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)

    def _load_module_from_path(
        path,
        expected_filename,
        *,
        verifier=None,
        allow_unverified=False,
        cache=False,
        cache_key=None,
    ):
        """구형/손상 중앙 seam에서 복구 명령까지 띄우는 import-by-name 폴백."""
        target = os.path.realpath(os.fspath(path))
        if os.path.basename(target) != expected_filename:
            raise ValueError(
                f"module filename mismatch: expected {expected_filename!r}, "
                f"got {os.path.basename(target)!r}"
            )
        if verifier is not None and allow_unverified:
            raise ValueError("choose verifier or allow_unverified=True, not both")
        if verifier is None and not allow_unverified:
            raise ValueError(
                "module load requires verifier or explicit allow_unverified=True"
            )
        module_key = cache_key or f"_project_manager_legacy_loaded:{target}"
        module = sys.modules.get(module_key) if cache else None
        inserted = False
        try:
            if module is None:
                if (
                    target == _TOOLS_BOOTSTRAP_FILE
                    and _TOOLS_BOOTSTRAP_MODULE is not None
                ):
                    module = _TOOLS_BOOTSTRAP_MODULE
                else:
                    import_name = os.path.splitext(expected_filename)[0]
                    previous = sys.modules.pop(
                        import_name, _TOOLS_BOOTSTRAP_SENTINEL
                    )
                    parent = os.path.dirname(target)
                    # 런타임에 만든 형제 모듈(중앙 로더 선복구가 방금 복사한 seam 등)을
                    # 이름으로 import 한다 — FileFinder 는 디렉터리 목록을 mtime 으로 캐시하고
                    # 인터프리터 시작 뒤 생긴 파일은 invalidate 없이는 인식이 보장되지 않는다
                    # (Python 문서 `importlib.invalidate_caches` · Windows 실측 간헐
                    # ModuleNotFoundError). 블록은 stdlib-only 라 지역 import 로 두되 sys.path 에
                    # parent 를 넣기 전에 가져와 그 트리의 동명 파일이 stdlib 를 가리지 않게 한다.
                    import importlib as _bootstrap_importlib
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
                        _bootstrap_importlib.invalidate_caches()
                        module = __import__(import_name)
                        if os.path.realpath(getattr(module, "__file__", "")) != target:
                            raise ImportError(
                                f"{expected_filename} 형제 경로 불일치"
                            )
                    finally:
                        if added:
                            try:
                                sys.path.remove(parent)
                            except ValueError:
                                pass
                        if sys.modules.get(import_name) is module:
                            sys.modules.pop(import_name, None)
                        if previous is not _TOOLS_BOOTSTRAP_SENTINEL:
                            sys.modules[import_name] = previous
                if cache:
                    sys.modules[module_key] = module
                    inserted = True
            if verifier is not None:
                verifier(module, expected_filename)
            return module
        except Exception as exc:
            if cache and (inserted or sys.modules.get(module_key) is module):
                sys.modules.pop(module_key, None)
            if target == _TOOLS_BOOTSTRAP_FILE:
                raise RuntimeError(
                    f"엔진 공용 로더 {target}를 불러올 수 없음; "
                    "pm-update로 .project_manager/tools 전체를 재동기화하라."
                ) from exc
            raise


# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# Python 하한 probe보다 먼저 평가되므로 3.10에서도 파싱 가능한 문법만 쓴다.
ENGINE_REV = "v1.7.8"


def _runtime_skill_entry(skill: str) -> str:
    """현재 실행 하네스의 사용자 호출 표기(Codex env marker 외 slash)."""
    prefix = "$" if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_CI") else "/"
    return f"{prefix}{skill}"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
    got = getattr(sibling_module, "ENGINE_REV", None)
    if got != ENGINE_REV:
        err = RuntimeError(
            "엔진 사본 버전 불일치 — 로더 %s(rev=%r)가 형제 %s(rev=%r)를 로드했다 "
            "(사본 skew: 부분/수동 복사 또는 구형 사본). `pm-update`(또는 pm_update.py)로 "
            ".project_manager/tools/ 전체를 재동기하라."
            % (Path(__file__).name, ENGINE_REV, sibling_filename, got)
        )
        err._engine_rev_skew = True
        raise err


def _is_engine_rev_skew(exc):
    """fail-soft 소비 지점에서 rev skew만 재-raise하기 위한 구조화 판정."""
    return getattr(exc, "_engine_rev_skew", False)


def _load_min_python() -> tuple[int, int] | None:
    """engine_rev.py 의 하한을 읽되 불완전/구형 사본이면 확인을 건너뛴다."""
    try:
        path = Path(__file__).resolve().with_name("engine_rev.py")
        mod = _load_module_from_path(
            path, "engine_rev.py", allow_unverified=True,
        )
        return tuple(mod.MIN_PYTHON)
    except Exception:
        # 부분/구형 사본에서 새 선행검사가 import 자체를 깨지 않게 한다. 정상 배포에서는
        # engine_rev.MIN_PYTHON을 읽고 아래 명시 오류를 유지한다(board._detect_py 동형 fail-soft).
        return None


def _require_python(version_info=None) -> None:
    """tomllib import 전에 현재 인터프리터 하한을 명시적으로 검증한다."""
    current = sys.version_info if version_info is None else version_info
    minimum = _load_min_python()
    if minimum is None:
        return
    if tuple(current[:2]) < minimum:
        major, minor = minimum
        raise SystemExit(
            f"Python {major}.{minor}+ 필요 · 현재 {current[0]}.{current[1]}"
        )


# 반드시 tomllib 보다 먼저 실행한다. 이 파일은 Python 3.10 문법으로 전체 parse 가능하며
# future annotations 덕분에 아래 list[...] / X | None 표기도 체크 전 평가되지 않는다.
_require_python()

import tomllib

REPO = Path(__file__).resolve().parents[2]

# import 어댑터 registry의 단일 진실. 공개 ``--harness``는 이 키들의 콤마 집합 또는 ``all``을
# 받는다. ``all``은 별도 튜플을 갖지 않고 항상 이 mapping의 키 전체에서 파생된다. 값은 template
# 디렉터리 튜플 shape를 유지해 기존 소비자/테스트 축이 새 하네스를 자동 발견하게 한다.
HARNESS_TEMPLATE_DIRS = {
    "claude": ("claude_code",),
    "opencode": ("opencode",),
    "codex": ("codex",),
}
REGISTERED_HARNESSES = tuple(HARNESS_TEMPLATE_DIRS)
HARNESS_ALL = "all"

# legacy harness alias seam — v1.5.x 의 `both`(=claude,opencode)는 예약대로 v1.6.0 에서
# 제거됐다(실사용 0 + v1.5.x 경고 한 주기 유예·아래 ratchet 이 기한을 기계 강제한 결과).
# 새 legacy alias 를 들일 땐 여기에 (alias → 정식 하네스 튜플)로 등록하고 제거 기한을 아래
# 상수로 예약한다 — 기한 도달 시 _enforce_legacy_harness_alias_deadline 이 모듈 로드 자체를
# 막아 제거가 사람 기억에 의존하지 않는다. 파서 확장 분기는 이 dict 로 구동되는 범용 기계다.
LEGACY_HARNESS_ALIASES: dict[str, tuple[str, ...]] = {}
LEGACY_ALIAS_REMOVAL_VERSION = "v1.6.0"
HARNESS_CHOICES = (*REGISTERED_HARNESSES, HARNESS_ALL, *LEGACY_HARNESS_ALIASES)
WEIGHT_CHOICES = ("full", "lite")


def _engine_rev_tuple(value: str) -> tuple[int, int, int]:
    """정규 engine rev(``vMAJOR.MINOR.PATCH``)를 비교 가능한 정수 튜플로 바꾼다."""
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise RuntimeError(f"기계 비교할 수 없는 ENGINE_REV 형식: {value!r}")
    return tuple(int(part) for part in match.groups())


def _enforce_legacy_harness_alias_deadline(
    engine_rev: str = ENGINE_REV,
    aliases: dict[str, tuple[str, ...]] | None = None,
) -> None:
    """제거 버전에 도달한 legacy alias가 사람의 기억에만 기대어 잔존하지 않게 한다."""
    active_aliases = LEGACY_HARNESS_ALIASES if aliases is None else aliases
    if (
        active_aliases
        and _engine_rev_tuple(engine_rev)
        >= _engine_rev_tuple(LEGACY_ALIAS_REMOVAL_VERSION)
    ):
        raise RuntimeError(
            f"ENGINE_REV {engine_rev}에서는 legacy harness alias를 제거해야 합니다: "
            f"{', '.join(active_aliases)} (기한 {LEGACY_ALIAS_REMOVAL_VERSION})"
        )


_enforce_legacy_harness_alias_deadline()


def parse_harness_selection(
    value: str,
    *,
    harness_template_dirs: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """콤마 선택을 registry 순서의 중복 없는 하네스 튜플로 정규화한다.

    입력 순서는 결과/충돌 우선순위에 영향을 주지 않는다. ``all``은 호출 시점 registry의 키
    전체로 확장돼 네 번째 하네스가 추가돼도 자동 편입된다. ``LEGACY_HARNESS_ALIASES`` 에
    등록된 alias 도 집합 원소처럼 확장된다(현재 빈 seam — `both` 는 v1.6.0 에서 제거).
    """
    registry = HARNESS_TEMPLATE_DIRS if harness_template_dirs is None else harness_template_dirs
    if not isinstance(value, str):
        raise ValueError(f"--harness 값은 문자열이어야 합니다: {value!r}")
    raw_tokens = value.split(",")
    tokens = [token.strip() for token in raw_tokens]
    if not tokens or any(not token for token in tokens):
        raise ValueError(
            "--harness 선택에 빈 항목이 있습니다 — 예: claude,codex 또는 all"
        )
    known = set(registry) | {HARNESS_ALL} | set(LEGACY_HARNESS_ALIASES)
    unknown = sorted(set(tokens) - known)
    if unknown:
        raise ValueError(
            f"미지원 harness: {', '.join(unknown)} — 지원: "
            f"{', '.join(registry)}; 조합은 콤마 구분, 전체는 all"
        )

    selected: set[str] = set()
    for token in tokens:
        if token == HARNESS_ALL:
            selected.update(registry)
        elif token in LEGACY_HARNESS_ALIASES:
            aliases = LEGACY_HARNESS_ALIASES[token]
            missing = [harness for harness in aliases if harness not in registry]
            if missing:
                raise ValueError(
                    f"legacy harness alias {token!r}가 미등록 하네스를 가리킵니다: "
                    f"{', '.join(missing)}"
                )
            selected.update(aliases)
        else:
            selected.add(token)
    if not selected:
        raise ValueError("--harness 선택 결과가 비었습니다.")
    return tuple(harness for harness in registry if harness in selected)


def _parse_harness_arg(value: str) -> tuple[str, ...]:
    """argparse 경계: 집합 파서 오류를 표준 CLI usage 오류로 바꾼다."""
    try:
        return parse_harness_selection(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc

FILL_CHOICES = ("auto", "manual")


def _cli_description() -> str:
    """모듈 도움말의 fill 목록도 현재 registry에서 렌더한다."""
    registered = tuple(HARNESS_TEMPLATE_DIRS)
    return (__doc__ or "").replace(
        "@REGISTERED_FILL_HARNESSES@", ",".join(registered)
    ).replace(
        "@REGISTERED_FILL_BINARIES@",
        "·".join(f"`{harness}`" for harness in registered),
    )

# fill 단계가 채우는 자유서술 placeholder 3종 (sed 로 못 채움 — repo 분석 필요).
# operational(보존) 토큰과 달리 board init 후 하니스 구동(auto) 또는 TODO 표시(manual)로 처리.
FREE_FORM_TOKENS = (
    "{{PROJECT_CONSTRAINTS}}",
    "{{PROTECTED_PATHS}}",
    "{{USER_GATE_ITEMS}}",
)

# opencode 어댑터 고유 harness 설정값 — operational/자유서술 어디에도 안 속하는 모델 ID.
# `.opencode/agents/*.md` 의 `model:` 필드로 등장.
# *아니라* `opencode models` 결정적 조회로 해소한다(resolve_opencode_model) — 따라서 fill
# 후보에서 분리됐다(중복·환각 제거). opencode 트리가 복사됐을 때만 해소 단계를 탄다.
OPENCODE_MODEL_TOKEN = "{{OPENCODE_PRO_MODEL}}"

# `opencode models` 조회 명령 — 가용 모델의 단일 진실(LLM 추측 아님). 줄당 `provider/model`.
OPENCODE_MODELS_CMD = ("opencode", "models")

# fill 실 하니스 구동 opt-in 게이트 환경변수 (external_review 선례). PM_IMPORT_LIVE_HARNESS=1
# AND --fill auto 동시 충족 시만 실 runner 호출 — 둘 중 하나라도 없으면 stub/manual 강제.
LIVE_HARNESS_ENV = "PM_IMPORT_LIVE_HARNESS"

# 하니스 헤드리스 구동 명령 (fill auto). claude 는 stdout 캡처, opencode 는 --format json 파싱,
# codex 는 `exec --json` JSONL 파싱(최종 agent_message). codex 는 stdin 미닫힘 시 무기한 대기(실측·
# spike )라 _real_harness_runner 가 빈 stdin PIPE를 즉시 닫아 EOF를 주고, -s workspace-write·--skip-git-repo-check·
# -C <dest> 는 _build_runner_argv 가 붙인다. codex 는 `model` 생략=사용자 config 상속(harness-특수 분기 0).
CLAUDE_FILL_CMD = ("claude", "-p")
OPENCODE_FILL_CMD = ("opencode", "run")
CODEX_FILL_CMD = ("codex", "exec")

# fill 실행도 위임·추가 리뷰와 같은 하네스 프로필을 쓴다. 과거 fill 전용 300초 wall 상한은
# cloud idle 임계보다 먼저 발화해 무진행 판정을 사실상 무력화했고, opencode 로컬 GPU 축의 실측
# 장시간 실행도 잘랐다. 채택 시점 1회성이라는 UX 차이는 별도 판정값의 근거가 아니므로 제거한다.
# 배포 환경 차이는 대상 repo local.conf 의 `harness.<name>.{idle,wall}_timeout` 으로 조정한다.
# 실제 fill argv 계약을 선언한다: (하네스, 증분 진행 신호 유무, stdin 입력).
# claude `-p`는 종료 시 평문 blob 하나를 내므로 profile의 일반 CLI event-stream 선언을 그대로
# 적용하면 정상 침묵도 idle kill한다. 반면 opencode `--format json`·codex `--json`은 실행 중
# 이벤트를 내므로 profile 신호를 소비한다. codex만 빈 PIPE를 닫아 즉시 EOF를 전달한다.
FILL_DRIVER_BY_CMD = {
    CLAUDE_FILL_CMD: ("claude", False, None),
    OPENCODE_FILL_CMD: ("opencode", True, None),
    CODEX_FILL_CMD: ("codex", True, ""),
}

# fill 가능 여부는 runner argv 계약 선언에서 파생한다. 등록 하네스 전체가 자동 fill을 할 수 있다는
# 가정은 두지 않는다; 새 등록분은 여기의 명시 runner 계약 없이는 --fill auto를 통과할 수 없다.
FILL_CAPABLE_HARNESSES = tuple(
    harness for harness in REGISTERED_HARNESSES
    if any(driver[0] == harness for driver in FILL_DRIVER_BY_CMD.values())
)
# ``--fill-harness`` argparse choices는 등록 adapter 전부다. auto 가능 여부는 설치 전
# ``FILL_CAPABLE_HARNESSES`` 게이트가 별도로 판정하므로 manual 경로를 여기서 막지 않는다.
FILL_HARNESS_CHOICES = REGISTERED_HARNESSES

# `opencode models` 조회 타임아웃 (초). 모델 목록 나열은 빠른 로컬 명령 가정이나, 회사 Pro/원격
# 게이트웨이는 cold 콜 지연이 커 15s 로는 부족.
# 기본을 60 으로 올리고, env override 로 환경별 재조정.
# (LLM 헤드리스 구동의 하네스 프로필 wall 값은 모델 조회엔 과대. --opencode-model 명시 경로의
# 대조-조회가 import UX 를 길게 막지 않도록 fail-soft + 적당한 별도 상한.)
OPENCODE_MODELS_TIMEOUT_SECONDS = 60


# env override: 회사 Pro·느린 원격에서 60s 도 모자라면
#   코드 수정 없이 `PM_OPENCODE_MODELS_TIMEOUT`(초)로 늘린다. 양의 정수만 채택 — 미설정/비숫자/≤0
#   은 기본 OPENCODE_MODELS_TIMEOUT_SECONDS(60) 로 폴백(무해). 빠른 로컬 조회라 무제한은 두지 않는다.
def _opencode_models_timeout() -> int:
    raw = os.environ.get("PM_OPENCODE_MODELS_TIMEOUT")
    if raw is None:
        return OPENCODE_MODELS_TIMEOUT_SECONDS
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        return OPENCODE_MODELS_TIMEOUT_SECONDS
    return val if val > 0 else OPENCODE_MODELS_TIMEOUT_SECONDS

# add_harness어댑터 네임스페이스 = {adapter dir(들), root doc}. 라이브 인스턴스에 두 번째
# harness 를 *비파괴로 추가*할 때 복사 스코프 = 이 네임스페이스 ∪ **guest flavor 가 `@render` 로 선언한
# 경로**(cross-ns 의존물 포함) − **host 실소유**. 그 밖(엔진·wiki dev-state·타 harness·
# 설정·파사드·flavor 미선언)은 plan 에 애초에 안 들어와 clobber 가 불가능(Decision 2·5). cross-ns 예:
# opencode 를 codex host 에 추가하면 opencode 가 네이티브 소비하는 `.claude/skills`(`.opencode` 밖·
# flavor `@render` 선언이라 복사·등재된다. host 가 이미 소유한 경로(dest engine.manifest
# core·`_dest_manifest_core_paths`)는 스코프 안이라도 제외한다(중복 레이다운 방지). 단일 harness
# (claude|opencode|codex) 하나만 추가한다(집합 선택은 최초 import 소관).
#
# 값 shape = **`(adapter_dirs: tuple, root_doc)`**. claude/opencode 는
# 어댑터 dir 가 하나라 단일-원소 튜플(`(".claude",)`)이고, codex 는 네임스페이스가 **둘**로 갈린다 —
# `.codex/`(agents·config·hooks·relay) + `.agents/`(skills·remap 강제) — 이 dual-namespace 가
# 값 shape 일반화를 기계적으로 강제했다(2-튜플 → dirs-튜플). 소비처(_in_adapter_namespace·add_harness
# unpack)는 dirs 를 iterate 한다([[cross-cutting-breaking-blast-radius]] — shape 변경 소비처 선-스코프).
ADD_HARNESS_ADAPTER = {
    "claude": ((".claude",), "CLAUDE.md"),
    "opencode": ((".opencode",), "AGENTS.md"),
    "codex": ((".codex", ".agents"), "AGENTS.md"),
}

# 둘 이상의 하네스가 같은 루트 진입 문서를 자동 로드할 때의 공존 정책.
# 값 = (문서를 공유하는 하네스 집합, harness-neutral 원본을 소유한 하네스). opencode 단독 lite는
# 자족형 AGENTS.lite.md를 계속 쓰지만, opencode+codex 공존에서는 그 opencode 실행 모델(task tool)이
# codex에도 자동 로드되지 않도록 두 트리의 AGENTS.md source를 공통 코어로 통일한다. full도 같은
# 선언을 타므로 두 무게축의 정책이 갈리지 않는다. 하네스별 실행/위임 지침은 각 namespace
# (.opencode/pm-instructions.md, .agents/skills·.codex/agents)에 그대로 분리되어 있다.
NEUTRAL_SHARED_ENTRY_DOCS = {
    Path("AGENTS.md"): (
        frozenset(
            harness
            for harness, (_dirs, root_doc) in ADD_HARNESS_ADAPTER.items()
            if root_doc == "AGENTS.md"
        ),
        "codex",
    ),
}

# 어댑터 네임스페이스에서 **채택자 소유**로 출하되는 파일의 단일 선언(harness → template-relative
# POSIX relpath 집합). 값의 뜻 = "flavor manifest 에 등재하지 않는다"(pm_update 가 안 덮는다) —
# 루트 진입 문서와 사용자 권한·trust·machine-local config 가 여기 든다. 이 선언이 없으면 그 파일은
# 산문 주석으로만 채택자 소유이고, 기계는 그것을 "출하되는데 어느 채널에도 없는 파일"(영구 동결)과
# 구분하지 못한다 — 출하-등재 역방향 가드가 이 선언을 소비해 둘을 가른다.
# 네 번째 하네스가 자기 config 를 들여오면 여기 명시해야 가드를 통과한다.
INSTANCE_OWNED_ADAPTER_FILES = {
    "claude": frozenset({"CLAUDE.md", ".claude/settings.json"}),
    "opencode": frozenset({"AGENTS.md", ".opencode/opencode.jsonc"}),
    "codex": frozenset({"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"}),
}

# add-harness가 절대 merge/clobber하지 않는 adopter-owned adapter 설정의 단일 정책 지점.
# 값은 template-relative POSIX relpath다. 위 소유 선언(INSTANCE_OWNED_ADAPTER_FILES)의 **부분집합**
# 이다 — 소유는 "누구 파일인가", 이 정책은 그 소유에 딸린 "add-harness 복사 때 어떻게 다루나"라
# 관심사가 다르고, 미선언 경로를 여기 넣으면 소유 진실이 둘로 갈린다(가드가 red). 엔진/어댑터
# 코드 전체를 보존하는 broad 예외가 아니라, 권한 경계인 개별 config 파일만 좁게 보호한다.
ADD_HARNESS_CREATE_IF_ABSENT = {
    "claude": frozenset(),
    "opencode": frozenset(),
    "codex": frozenset({"AGENTS.md", ".codex/config.toml", ".codex/hooks.json"}),
}

# instance-owned config 의 **상류 fix 도달 채널** 분류. 키는 위 소유 선언에 있는 경로여야 하고,
# 선언된 경로는 전부 여기 분류가 있어야 한다(양방향을 가드가 강제) — 분류 누락을 허용하면 새 config
# 가 조용히 채널 0 으로 떨어져 지금 닫는 결함이 그대로 재발한다. 값의 뜻:
#   managed : 무편집(dest 해시 == 원장 해시)이면 백업 후 현행 template 으로 자동 갱신. 파일 전체가
#             엔진 동작이고 채택자 노브가 없는 것만 든다.
#   report  : 갱신하지 않고 template 대비 drift 만 표기. 채택자 노브(권한 allowlist·모델·threshold)가
#             실재해 자동 갱신이 그 값을 지울 수 있는 파일.
#   none    : 채널 없음 — 루트 진입 문서(본문 대부분이 채택자 산문이라 기계 대조가 무의미).
ADAPTER_CONFIG_MANAGED = "managed"
ADAPTER_CONFIG_REPORT = "report"
ADAPTER_CONFIG_NO_CHANNEL = "none"
ADAPTER_CONFIG_CHANNEL = {
    "claude": {
        "CLAUDE.md": ADAPTER_CONFIG_NO_CHANNEL,
        ".claude/settings.json": ADAPTER_CONFIG_REPORT,
    },
    "opencode": {
        "AGENTS.md": ADAPTER_CONFIG_NO_CHANNEL,
        ".opencode/opencode.jsonc": ADAPTER_CONFIG_REPORT,
    },
    "codex": {
        "AGENTS.md": ADAPTER_CONFIG_NO_CHANNEL,
        ".codex/config.toml": ADAPTER_CONFIG_REPORT,
        # 초기 롤아웃의 유일한 managed 대상 — 상류 diff 가 100% 엔진 동작(훅 차단→비차단·문구 전면
        #   교체)이고 채택자 노브가 0 임이 실측됐다. 대상 확장은 원장이 누적된 뒤 별도 판정.
        ".codex/hooks.json": ADAPTER_CONFIG_MANAGED,
    },
}

# managed 갱신 뒤 채택자가 해야 하는 후속 행동 — 자동 갱신이 조용한 기능 비활성화가 되지 않게
# 갱신 시점에 함께 낸다(codex 는 훅 정의가 바뀌면 세션에서 다시 승인해야 발화한다).
ADAPTER_CONFIG_REAPPROVAL_NOTE = {
    ".codex/hooks.json": "codex 세션에서 `/hooks` 로 훅을 다시 승인해야 새 정의가 발화한다",
}

# ── 어댑터 훅 세트 (세대 정합 판정의 데이터 축) ─────────────────────────────────
# 훅 세트 = "instance-owned config 가 선언한 훅 호출" + "그 호출이 실행하는 manifest 소유
# 래퍼/드라이버". 두 축은 **갱신 주체가 다르다** — config 는 채택자 소유라 pm_update 가 못 덮고,
# 래퍼/드라이버는 manifest 등재라 pm_update 가 덮는다. 그래서 한쪽만 새 세대인 창이 구조적으로
# 열리고, 그중 **config 가 앞선 조합만** 락아웃이다(v1.7.0 흡수 실측: 신 settings.json 의
# `--git-anchor-hook` 을 구 pm_orch_claude.py 가 argparse rc2 로 거부 → PreToolUse rc2 = 도구 차단
# → Bash 전면 락아웃). 반대 방향(구 config + 신 드라이버)은 훅 미발화라 무해하다
# (tests/test_git_anchor_guard.py 의 unknown-flag rc0 관용).
#
# 판정을 하드코딩 claude 전용으로 두지 않기 위해 검사 대상은 전부 이 표에서만 온다. 등록 하네스
# 전수가 키를 가져야 하며(가드가 강제), 훅 커맨드를 선언하는 config 가 없는 하네스는 빈
# `config_relpath` 로 그 사실을 명시한다 — 미선언과 "없음" 이 구분되지 않으면 네 번째 하네스가
# 조용히 미검사로 떨어진다.

# 훅 커맨드에서 스크립트 경로를 뽑을 때 벗겨내는 접두 토큰(하네스 런타임이 해소하는 루트 변수).
_HOOK_COMMAND_ROOT_TOKENS = ("${CLAUDE_PROJECT_DIR}/", "$CLAUDE_PROJECT_DIR/", "./")


class AdapterHookEntrypoint(NamedTuple):
    """이 엔진 세대가 채택자 config 에 **있어야 한다**고 보는 훅 진입점 하나 (역방향 축).

    `flag_support` 와 방향이 반대다 — 저쪽은 "config 가 요구하는 것을 설치본이 감당하나"(config →
    엔진)이고, 이쪽은 "이 엔진 세대가 기대하는 진입점이 config 에 있나"(엔진 → config)다. 진입점이
    빠진 채택자에서는 **앞으로 등록될** 가드가 발화 자체를 안 한다(옛 직결 배선이 남아 있으면 그
    시점의 기능은 계속 돈다) — 그 상태가 조용한 통과로 남지 않게 한다.

    event      훅 이벤트 이름(config `hooks` 의 키).
    matcher    그 이벤트의 값 공간을 전부 덮는 matcher 리터럴. `None` 은 matcher 키 없음
               (claude 처럼 matcher 를 쓰지 않는 이벤트).
    dispatcher 진입점이 실행하는 manifest 소유 파일(dest 기준 POSIX relpath·`live_files` 안).
               판정은 훅 커맨드 문자열이 이 relpath 를 담는가다 — 출하 커맨드가 인터프리터
               해소를 포함한 셸 한 줄이라 argv 분해로는 스크립트를 뽑을 수 없다.
    flag       그 디스패처가 지원해야 하는 플래그 리터럴(없으면 `None`). config 는 갱신됐는데
               디스패처가 구세대면 훅이 매번 폴백으로 빠진다 — 그 창을 판정하는 축이다.
               플래그가 있으면 판정은 **호출 값**(`<flag> <event>`)까지 내려간다: 같은
               커맨드가 디스패처 경로와 그 이벤트 호출을 함께 담아야 진입점으로 세고, 설치된
               디스패처도 같은 두 값을 감당해야 세대 정합이다. 경로만 보면 실행하지 않는
               문자열이나 다른 이벤트를 부르는 커맨드가 false-green 으로 통과한다.
    """
    event: str
    matcher: str | None
    dispatcher: str
    flag: str | None = None


class AdapterHookSetSpec(NamedTuple):
    """한 하네스의 훅 세트 선언 — 판정에 필요한 데이터 전부.

    config_relpath : 훅 호출을 선언하는 instance-owned config(dest 루트 기준 POSIX relpath).
    live_files     : 실행 중 하네스가 읽는 manifest 소유 훅 파일. `/` 로 끝나면 디렉토리 접두
                     (manifest 의 파일/디렉토리 semantics 와 같다). 판정 범위이자 동기 시
                     원자 write 대상이다 — 채택자 자작 훅 스크립트는 여기 없으므로 검사하지
                     않는다(엔진 소관 밖·거짓 처방 금지).
    flag_support   : 훅 커맨드 플래그 → 그 플래그를 **실제로 지원해야 하는** dest 파일들.
                     래퍼가 다른 파일로 dispatch 하면(claude: ctx_stop_hook.sh → pm_orch_
                     claude.py) 그 체인 전부를 적는다. 지원 판정은 파일 본문의 리터럴 보유다
                     (실행 없이 판정·훅 실행은 그 자체가 부작용).
    coupled_groups : **한 세대로 함께 옮겨야** 하는 묶음(로드 시점 결합). 부분 전파가 묶음의
                     일부만 갱신하면 그 자리에서 세대가 갈린다. `live_files` 와 관심사가 다르다 —
                     저쪽은 "원자 write 대상"(파일 단위 torn read)이고 이쪽은 "함께 움직여야
                     하는 단위"다. 결합이 없는 파일(독립 relay 드라이버·위임 채널 가드)은 어느
                     묶음에도 들지 않아 단건 전파가 정당하다.
    entrypoints    : 이 엔진 세대가 config 에 기대하는 **범용 진입점**(`AdapterHookEntrypoint`).
                     역방향 축이라 판정은 loud advisory 다 — config 는 채택자 소유이고 그 파일엔
                     실 노브가 있어, 무편집을 강제하는 red 는 소유 원칙과 충돌한다.
    """
    config_relpath: str
    live_files: tuple[str, ...]
    flag_support: dict[str, tuple[str, ...]]
    coupled_groups: tuple[tuple[str, ...], ...] = ()
    entrypoints: tuple[AdapterHookEntrypoint, ...] = ()


ADAPTER_HOOK_SET = {
    "claude": AdapterHookSetSpec(
        config_relpath=".claude/settings.json",
        live_files=(
            ".claude/ctx_guard.py",
            ".claude/ctx_stop_hook.py",
            ".claude/ctx_stop_hook.sh",
            ".claude/ctx_statusline.py",
            ".claude/ctx_statusline.sh",
            ".claude/delegate_channel_guard_hook.sh",
            ".claude/precompact_capture_hook.sh",
            ".claude/pm_orch_claude.py",
        ),
        flag_support={
            # T-0587 이 도입한 세대 결합 — settings.json 의 Bash 매처가 이 플래그를 넘기고,
            #   래퍼가 pm_orch_claude.py 로 dispatch 한다. 둘 중 하나라도 구 세대면 락아웃.
            "--git-anchor-hook": (
                ".claude/ctx_stop_hook.sh", ".claude/pm_orch_claude.py"),
        },
        coupled_groups=(
            # 훅 체인 + 공유 코어: 래퍼가 플래그로 두 파이썬 진입 중 하나를 고르고, 그 둘이
            #   ctx_guard 를 import 한다. 일부만 옮기면 미지원 플래그·import 불일치가 난다.
            (".claude/ctx_stop_hook.sh", ".claude/precompact_capture_hook.sh",
             ".claude/ctx_stop_hook.py",
             ".claude/pm_orch_claude.py", ".claude/ctx_guard.py"),
            # statusline 래퍼/구현 쌍(같은 근거·독립 축).
            (".claude/ctx_statusline.sh", ".claude/ctx_statusline.py"),
        ),
        entrypoints=(
            # claude 는 범용 진입점을 이미 갖고 있다 — 이 선언은 값을 바꾸지 않고 선언만
            #   한다(진입점이 사라진 채택자에서 ctx 가드가 조용히 무발화하는 상태를 표면화).
            #   래퍼가 인자로 분기하므로 플래그 축은 `flag_support` 가 이미 본다(중복 선언 0).
            AdapterHookEntrypoint("PreToolUse", "*", ".claude/ctx_stop_hook.sh"),
        ),
    ),
    "codex": AdapterHookSetSpec(
        # `.codex/hooks.json` 의 훅 커맨드는 인터프리터 해소를 포함한 **셸 한 줄**이라 argv 분해로
        #   스크립트를 못 뽑는다(`_hook_script_and_arguments` 의 첫 토큰이 `if`) — 그래서
        #   `flag_support` 는 여기서 구조적으로 공허하고, 같은 세대 결합은 아래 `entrypoints` 의
        #   `flag` 축이 커맨드 파싱 없이 판정한다(공허한 선언을 두지 않는다).
        config_relpath=".codex/hooks.json",
        live_files=(".codex/pm_orch_codex.py",),
        flag_support={},
        entrypoints=tuple(
            # 이벤트당 진입점 하나(`matcher .*`)가 manifest 등재 디스패처를 부르고,
            #   "어떤 가드를 돌릴지" 는 그 코드 안에서 갈린다. 그래서 가드 **기능** 추가는 이제
            #   엔진 코드 변경뿐이고 채택자 config·`/hooks` 재승인을 다시 요구하지 않는다.
            #   이 집합은 릴리즈 간 불변이다 — 늘리려면 채택자 config 재승인을 동반한 1회
            #   마이그레이션이다. 그래서 codex 가 발화시키는 **모든** 이벤트를 한 번에 담는다:
            #   일부만 담으면 남은 이벤트가 다음 기능에서 재승인을 다시 부른다.
            AdapterHookEntrypoint(event, ".*", ".codex/pm_orch_codex.py", "--hook-dispatch")
            for event in ("PreToolUse", "UserPromptSubmit", "PostToolUse",
                          "SubagentStart", "PreCompact", "PostCompact")
        ),
    ),
    # opencode 는 훅 커맨드를 선언하는 config 가 없다 — 플러그인은 `.opencode/plugins/`
    #   autoload 라 config 가 호출 형태를 담지 않는다. 판정 대상은 없지만 실행 중 하네스가 읽는
    #   파일이라 원자 write 대상에는 든다. `lib/` 도 함께다 — 플러그인 3종이 로드 시점에
    #   `../lib/*-core.cjs` 를 즉시 import 하므로(plugins/ctx-guard.js·git-anchor.js·
    #   safe-write.js 실측) 코어가 부분 파일이면 플러그인 로드 자체가 깨진다. claude 축이 공유
    #   코어 `ctx_guard.py` 를 넣은 것과 같은 이유다(하네스 간 비대칭 금지).
    "opencode": AdapterHookSetSpec(
        config_relpath="",
        live_files=(".opencode/lib/", ".opencode/plugins/",
                    ".opencode/pm_orch_opencode.py"),
        flag_support={},
        # 진입점 선언도 비어 있다 — 배선이 `plugins/` **디렉토리 스캔**이라 파일을 더하는 것이
        #   곧 배선이고 대조할 config 항목 자체가 없다(codex 진입점의 참고 모델).
        entrypoints=(),
        # 플러그인이 로드 시점에 코어를 import 한다 — 한쪽만 옮기면 그 자리에서 세대가 갈린다.
        #   relay 드라이버는 결합이 없어 묶음 밖이다(단건 전파 정당).
        coupled_groups=((".opencode/plugins/", ".opencode/lib/"),),
    ),
}

# 위 정적 경로와 같은 instance-owned 정책 섹션의 조건부 보호 규칙 — 선언한 최상위 TOML 키를
# adopter 가 갖고 있으면 그 *파일 전체*를 byte 보존한다(자동 TOML merge 대신). pattern/필드
# 추가는 이 선언만 고친다.
#
# **agent 카드의 `model`/`model_reasoning_effort` 는 여기 없다**: 카드의 모델·추론은 local.conf
# `delegate.<role>[.<tier>].{model,reasoning}` 의 렌더 파생물이라 단일 진실이 conf 다. 그 두 필드를
# 여기서 보존하면 add-harness 는 손편집을 지키고 다음 pm-update 는 conf 로 되돌려, 두 표면이 같은
# 필드에 반대 규칙을 선언하게 된다. 모델을 바꾸는 자리는 카드가 아니라 local.conf 다.
ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS = {
    "claude": {},
    "opencode": {},
    "codex": {},
}

# sed 치환 대상 operational placeholder (`docs/placeholders.md` 표). 자유서술 3종은 여기 없음(보존).
OPERATIONAL_TOKENS = (
    "{{PROJECT_NAME}}",
    "{{PROJECT_TAGLINE}}",
    "{{PROJECT_ROOT}}",
    "{{PY}}",
    "{{TEST_CMD}}",
    "{{DATE}}",
)

# 치환 대상 판정은 **제외 사유 기반**이다 — 옛 확장자 allowlist(`(".md", ".json", ".sh", ".py")`)는
# allowlist 는 "규칙이 적용될 지점을 사람이 열거" 하는 형상이라, 새 하니스가 새 파일
# 형식을 들여오면 조용히 미커버로 남는다 — codex(세 번째 하니스)의 `.codex/agents/*.toml` 이 정확히
# 그렇게 `{{PROJECT_NAME}}` 을 리터럴로 출하했다. 이제 판정은 뒤집혀 "제외 사유가 있는가" 만 본다
# (`_should_substitute`) → `.yaml`·`.jsonc` 같은 네 번째 하니스의 새 형식도 자동 편입된다.
#
# 제외 사유는 **닫힌 집합**(프레임워크가 소유한 엔진 자산)이라 열거해도 안전하다 — 열거가 위험했던 건
# *열린* 쪽(채택자/하니스가 계속 늘리는 파일 형식)을 열거했기 때문이다:
#   엔진 소스 `.project_manager/tools/**` (`_is_engine_source` — 주석의 토큰은 *설명*·verbatim)
#   엔진 메타데이터 `.project_manager/engine.manifest` (`_is_engine_metadata` — 아래)
#   manifest 파생 방법론 문서 제외집합 (`_dest_sed_exclude` — pm_role·pm_playbook)
#   텍스트로 못 읽는 파일 — 각 치환 루프의 `read_text` UnicodeDecodeError 가 걸러낸다.

# 엔진 메타데이터. engine.manifest 는 채택자에게 *개인화되어* 출하되는
# 산출물이 아니라 엔진이 읽는 기계 설정이고, 그 주석은 placeholder 메커니즘을 *설명*하며 토큰을 담는다
# (예 codex manifest 의 "`.codex/agents` 는 developer_instructions 에 {{PROJECT_NAME}} 토큰 보유 →
# @render"). 치환하면 설명이 concrete 값으로 변질된다 — `.project_manager/tools/**` 주석과 같은 클래스
# (_is_engine_source docstring 참조)이나, tools/ prefix 밖이라 별도 판정이 필요하다.
ENGINE_METADATA_RELPATHS = frozenset({
    ".project_manager/engine.manifest",
})

# ── 토큰 소유권: 파일 × 토큰 단위로 치환 주체를 하나로 ───────────────────────
# operational 토큰의 치환 주체는 셋 중 하나다:
#   설치-시 치환 — pm_import(기본값·아래 `_should_substitute` 가 제외 사유만 본다)
#   소비-시 치환 — 그 템플릿이 *산출물을 만드는 시점*(pm_state 생성·domain 페이지 생성)
#   상시 토큰    — 엔진 소유 문서가 토큰을 *설명*으로 담는다(엔진 소스·manifest·방법론 문서)
# 같은 파일을 설치-시와 소비-시가 반대 방향으로 소유하면 **매 sync 마다 진동한다**: 설치가 값으로
# 굳히고, manifest bare 등재의 byte-copy(`pm_update`)가 다음 흡수에서 토큰-form 으로 되돌린다
# (채택자 실측). 아래 선언은 소비-시 소유를 명시해 **그 파일의 그 토큰에서만** 설치-시 치환을
# 끈다 — 같은 파일의 다른 토큰(`{{PROJECT_NAME}}` 등)은 종전대로 설치 시 치환된다
# (치환 예외 *파일* 목록이 아니다).
# 신규 bare 등재 파일이 선언 없이 토큰을 담으면 `tests/test_manifest_render_token_guard.py` 가 fail.
CONSUMPTION_TIME_TOKENS: dict[str, frozenset] = {
    # 소비처: `worktree_pool.ensure_task_pm_state`(task pm_state 렌더)·`board.cmd_init`
    #   (per-clone `wiki/pm_state.md` seed) — 둘 다 생성 시각으로 채운다.
    ".project_manager/wiki/pm_state.template.md": frozenset({"{{DATE}}"}),
    # 소비처: 사람이 스캐폴드를 복사해 domain 페이지를 만드는 시점. 엔진 생성 경로
    #   (`domain.write_draft_page`)는 스캐폴드 frontmatter 를 복사하지 않고 자체 today 로 쓴다.
    ".project_manager/wiki/domain/_template.md": frozenset({"{{DATE}}"}),
}

# operational placeholder 치환에서 *제외*하는 방법론 문서 (repo 기준 relpath) — engine.manifest 파생.
# 하드코딩 목록(과거 pm_role.md·pm_playbook.md 리터럴 frozenset) 대신 manifest 의 `.project_manager/wiki/`
# 직속 `pm_*.md` 비-템플릿(= 방법론 문서 절)에서 결정적으로 유도한다. 이 문서들은 `{{PROJECT_NAME}}`·
# `{{DATE}}` 토큰을 *메커니즘 설명*으로 담아(placeholder 아님·local.conf 가 런타임 해소·`docs/placeholders.md`)
# 치환하면 문서가 concrete 값으로 변질되므로 제외한다. 파생이라 신규 방법론 .md 가 manifest 에 추가되면
# 자동 편입 — "목록 수동 추가 잊음 → 조용한 placeholder 오치환" 클래스 종결.
#
# ⚠️ 파생 기준 manifest 는 **모듈-import 시점(실행 checkout)이 아니라 치환 시점의 dest 인스턴스**
# (`dest_root/.project_manager/engine.manifest`)다. pm_import 는
# `--from <다른 framework checkout>` 에서 복사할 수 있어(구버전 로컬 도구가 신버전 upstream 을 흡수하는
# 실 운영 경로), *실제 복사되는 쪽* manifest 가 기준이어야 upstream 진화(신규 직속 방법론 문서)가 자동
# 편입된다 — 모듈-시점 상수는 실행 checkout 의 manifest 에 묶여 이 보장을 잃는다. 치환은 복사 *이후*
# 단계라 dest manifest 는 그 시점에 실재한다(manifest = self-prop 복사 대상). 각 치환 call site 는 아래
# `_dest_sed_exclude(dest_root)` 로 dest 기준 제외 집합을 산출해 `_should_substitute` 에 넘긴다.

# manifest 부재·로드 실패(broken-manifest)에서도 기존 제외를 조용히 잃지 않게 하는 리터럴 floor
# (should-fix). 정상 경로(manifest 파싱 성공)는 파생 결과를, 실패 경로만 이 floor 를 반환한다.
SED_EXCLUDE_FLOOR = frozenset({
    ".project_manager/wiki/pm_role.md",
    ".project_manager/wiki/pm_playbook.md",
})


def _is_template_scaffold(filename: str) -> bool:
    """파일명이 템플릿 스캐폴드 관례(`*.template.md`·`_template.md`·`*_template.md`)인가.

    템플릿(예: pm_state.template.md)은 `{{DATE}}` 등 토큰을 *렌더 대상*(도구·skill 이 새 산출물
    생성 시 채움)으로 담아, 토큰을 *설명*으로 담는 방법론 문서(pm_role·pm_playbook)와 성격이 다르다.
    치환-제외 집합엔 넣지 않는다(현행 동작 보존 — 템플릿의 operational 토큰은 import 치환 대상)."""
    stem = filename[:-len(".md")] if filename.endswith(".md") else filename
    return stem.endswith(".template") or stem == "_template" or stem.endswith("_template")


def _derive_sed_exclude_relpaths(manifest_path: Path) -> frozenset:
    """치환-제외 방법론 문서(repo 기준 relpath·POSIX) 집합을 engine.manifest 에서 파생한다.

    규칙 = manifest 엔트리 중 `.project_manager/wiki/` **직속** `pm_*.md` 파일 중
    *템플릿이 아닌* 것. README 같은 출하 색인은 render/update 채널에 들어와도 operational
    placeholder는 실제 프로젝트 값으로 치환돼야 하므로 방법론 리터럴 제외 집합에는 들지 않는다.
    서브디렉토리(`tickets/_template.md`·`raw/spikes/_template.md`·`domain/_template.md`)는 "직속"
    조건으로, 직속 템플릿(`pm_state.template.md`)은 `_is_template_scaffold` 로 제외된다 — 현재
    산출은 정확히 {pm_role.md, pm_playbook.md}.

    manifest 파싱은 pm_update.read_manifest 재사용(새 파서 신설 없음·_render_managed_relpaths 동형).
    manifest 부재·로드 실패 → 리터럴 floor SED_EXCLUDE_FLOOR (fail-soft — 빈 집합이면 broken-manifest
    엣지에서 pm_role·pm_playbook 이 조용히 오치환되므로 기존 제외를 floor 로 보장·should-fix). 정상
    파싱은 파생 결과를 그대로 반환한다(manifest 가 방법론 문서를 명시적으로 뺐다면 그 판단을 존중)."""
    wiki_prefix = ".project_manager/wiki/"
    if not manifest_path.is_file():
        return SED_EXCLUDE_FLOOR
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    try:
        mod = _load_module_from_path(
            pm_update_py, "pm_update.py", allow_unverified=True,
        )
        entries = mod.read_manifest(manifest_path)
    except Exception:  # noqa: BLE001 — 로드/파싱 실패는 floor 로 폴백(기존 제외 보존·should-fix).
        return SED_EXCLUDE_FLOOR
    out: set = set()
    for entry in entries:
        rel = str(entry).replace("\\", "/")
        if not rel.startswith(wiki_prefix):
            continue
        remainder = rel[len(wiki_prefix):]
        if "/" in remainder:  # 서브디렉토리 — 방법론 문서 절이 아님(직속만)
            continue
        if not remainder.endswith(".md"):
            continue
        if not remainder.startswith("pm_"):
            continue
        if _is_template_scaffold(remainder):  # 템플릿 스캐폴드 — 치환 대상(제외 아님)
            continue
        out.add(rel)
    return frozenset(out)


def _dest_sed_exclude(dest_root: Path) -> frozenset:
    """dest 인스턴스의 engine.manifest 기준 치환-제외 집합 (치환 시점 산출·codex must-fix).

    모듈-import 시점(실행 checkout)이 아니라 *복사가 끝난 dest* 의 manifest 에서 파생한다 — pm_import
    는 `--from <다른 checkout>` 에서 복사할 수 있어, 실제 dest 에 실린 manifest 가 기준이어야 upstream
    진화(신규 직속 방법론 문서)가 자동 편입된다. manifest 는 self-prop 복사 대상이라 치환(복사 이후)
    단계엔 dest 에 실재한다. 부재/실패는 SED_EXCLUDE_FLOOR(fail-soft)."""
    return _derive_sed_exclude_relpaths(dest_root / ".project_manager" / "engine.manifest")

# tracked-only 출하 목록 위에서도 복사 정책과 fill 스캔의 공통 방어를 보존하려고 유지한다.
# 복사/스캔 제외 디렉토리명 (무겁고 재설치 대상 / stale 산출물 / VCS 메타).
#   node_modules — opencode 의존성(재설치 대상). __pycache__ — stale 바이트코드(.pyc).
#   .git — VCS 메타(템플릿엔 없어 복사목록엔 안 끼지만, fill 폴백 전체 스캔이 대형 repo
#          .git 을 텍스트 read 하지 않도록 명시 제외 — 낭비 방지·결정론).
COPY_EXCLUDE_DIR_NAMES = frozenset({"node_modules", "__pycache__", ".git"})

# 복사 제외 파일 (정확 dst relpath) — adopter 에게 출하하지 않을 프레임워크-repo 내부 문서.
#   README.md(최상위) — 템플릿 트리의 "어댑터 타깃" 설명서다(프레임워크 상대링크 `../../README.md`·
#   `../opencode/README.md` 를 담아 adopter 트리에선 dangling·오해 소지). 채택자는 자기 프로젝트
#   README 를 쓴다 → 프레임워크-내부 doc 를 adopter README 로 박제하지 않는다(다중 선택 충돌도 소거).
#   하위 `.project_manager/wiki/*/README.md`(wiki 구조 안내)는 유지 — 정확 relpath `README.md` 만 제외.
COPY_EXCLUDE_RELPATHS = frozenset({"README.md"})

# --into 백업 중앙화 디렉토리. 충돌 파일별 형제 `*.backup.<DATE>` 를 트리 전역에
# 흩뿌리는 대신, 무백업 덮기 불가(미추적·dirty·비-git)인 파일만 단일 디렉토리
# `<dest>/.pm_import_backups/<DATE>/` 에 relpath 미러링으로 모은다. git 이 추적 중이고
# 미변경인 파일은 git 이 내용을 보존하므로 백업 없이 덮는다(git-safe skip).
BACKUP_DIR_NAME = ".pm_import_backups"

# `git` 호출 seam — argv(list) → (returncode, stdout). 테스트가 stub 주입(라이브 git 미실행).
# git_safe_relpaths 가 work tree 판별·추적집합 조회에 사용한다(_real_models_runner 류 결정적 seam).
GitRunner = Callable[[list], "tuple[int, str]"]

# git 호출 타임아웃 (초) — ls-files/status 는 빠른 로컬 명령이라 짧게(과대 대기 방지·fail-soft 상한).
GIT_SAFE_TIMEOUT_SECONDS = 15

# upstream git 호출(ls-remote·remote get-url·rev-parse) 타임아웃 (초). ls-remote 는
# 네트워크라 ls-files 보다 넉넉히 — pm_config.GIT_TIMEOUT_SECONDS(clone·600)보단 짧게(도달성
# 체크는 clone 만큼 길 필요 없음·과대 대기 방지).
UPSTREAM_GIT_TIMEOUT_SECONDS = 60

# upstream(네트워크-facing) git 호출의 config 격리 키 (codex MF4·worktree_pool GIT_CONFIG_*
# 선례). untrusted URL 의 ls-remote/rev-parse 에 사용자/global git config 의 `insteadOf`
# rewrite·credential helper 가 끼어드는 것을 막는다(defense-in-depth). GIT_CONFIG_GLOBAL/
# SYSTEM=/dev/null 로 global·system config 를 통째 무력화하고, GIT_CONFIG_COUNT 패턴으로
# protocol allowlist(https/ssh/file 만 always·나머지 never) + credential.helper=(빈값·helper
# 미경유)를 강제한다. (분류 검증[validate]은 *형태* 안전, 이 env 는 *실행* 격리 — 이중 방어.)
_UPSTREAM_GIT_CONFIG_KV = (
    ("credential.helper", ""),          # credential helper 미경유(자격증명 자동주입 차단).
    ("protocol.allow", "never"),        # 기본 거부 — 아래 명시 protocol 만 허용(allowlist).
    ("protocol.https.allow", "always"),
    ("protocol.ssh.allow", "always"),
    ("protocol.file.allow", "always"),
    ("http.followRedirects", "false"),  # redirect 추적 차단(잔여 SSRF 표면).
)

# 인터프리터 탐지는 board.py 의 _detect_py() 가 단일 진실. pm_import 가 자체
# 탐지를 신설하지 않고 board.py 를 재사용한다 — 플랫폼별 python3/python 해석을 한 곳에 둔다.
# board.py import 가 실패하면(예: yaml 부재) 리눅스 현행과 동치인 "python3" 로 폴백.
_DEFAULT_PY_FALLBACK = "python3"


def _detected_py() -> str:
    """{{PY}} 치환·local.conf runtime.py= 기본값으로 쓸 인터프리터 명령을 board.py 에서 탐지한다.

    board.py 의 _detect_py() 를 import 해 재사용(단일 진실). board.py 와 같은 디렉토리에
    있으므로 spec_from_file_location 으로 직접 로드 — sys.path 오염 없이 호출 가능.
    어떤 이유로든 로드/호출이 실패하면 "python3" 폴백(리눅스 현행 동치).
    """
    # 같은 canonical tools 디렉토리의 board.py라도 부분/수동 복사로 rev가 어긋날 수 있으므로
    # 로드 직후 verify한다. 일반 로드 실패는 폴백하되 marked skew만 아래에서 재-raise한다.
    board_py = Path(__file__).resolve().parent / "board.py"
    try:
        board_mod = _load_module_from_path(
            board_py, "board.py", verifier=_verify_engine_rev,
        )
        return board_mod._detect_py()
    except Exception as exc:  # noqa: BLE001 — 탐지 실패는 폴백, skew만 fail-loud.
        if _is_engine_rev_skew(exc):
            raise
        return _DEFAULT_PY_FALLBACK


def _default_test_cmd() -> str:
    """기본 test_cmd — 탐지된 인터프리터로 pytest 실행 (Windows 에선 `python`, POSIX 는 python3).

    상수 하드코딩(`python3 -m pytest`)은 Windows 에서 깨진다(`python3`=비기능 shim 또는
    엉뚱한 Store Python). `_detected_py()` 를 경유해 board.py `_detect_py()` 의 실행검증된
    인터프리터를 쓴다 — local.conf `runtime.py=` 와 동일 소스라 일관.
    """
    return f"{_detected_py()} -m pytest tests/ -q"


DEFAULT_TAGLINE = "한 줄 프로젝트 설명"


# pm_playbook.local.md 스텁 본문.
# 단일 소스 = 이 인라인 상수. 루트 pm_playbook.local.md는 manifest 밖이라 템플릿
# 트리에 안 끼어 *복사로 안 따라온다* → pm_import 가 import 시 직접 *생성*한다. 별도 `_template`
# 파일을 두지 않는 이유: 그 파일 자체가 manifest/복사 경로에 다시 얽혀 .local 분리 취지와
# 충돌한다 — stdlib-only 인라인 상수가 가장 단순·일관(루트 스텁 형식과 정합: 프런트매터
# type: playbook-local + 인스턴스 소유·manifest 밖 안내 + [[pm_playbook]] 역참조 + TODO 절).
PM_PLAYBOOK_LOCAL_STUB = """\
---
title: PM Playbook — 프로젝트별 누적 학습 (instance)
type: playbook-local
---

# PM Playbook — 프로젝트 누적 학습·도메인 사례

> [[pm_playbook]] (엔진 · `pm_update` 가 자동 갱신하는 **순수 방법론**)의 **프로젝트별 칸**.
> 이 파일은 **인스턴스 소유** — 프레임워크 갱신이 안 건드린다(manifest 밖·tracked).
> 이 프로젝트의 **누적 wave 학습·도메인 사례**를 여기 적는다 (방법론 일반론은 [[pm_playbook]]).

## 누적 wave 학습 (이 프로젝트 고유)

<!-- TODO: 이 프로젝트에서 정착한 wave 운영 학습·도메인 특수 패턴을 누적한다.
  실시간 학습 trail 은 log/current.md entry 가 매체 — 여기엔 *정착된* 패턴만 흡수. -->

## 도메인 사례

<!-- TODO: 이 프로젝트 도메인에 특화된 ticket/wave 사례. 없으면 절 삭제. -->
"""

# 스텁 대상 경로 (dest_root 기준 relpath). 루트 seam과 동일 위치.
PM_PLAYBOOK_LOCAL_RELPATH = Path(".project_manager") / "wiki" / "pm_playbook.local.md"


# ── git-safe 판정 (LLM 아님·결정적) ──────────────────────────────
# --into 백업 노이즈를 줄이려, git 이 *추적 중이고 미변경*인 파일은 백업 없이 덮는다(git 이
# 내용을 갖고 있어 복원 가능). 그 외(미추적·dirty·비-git)만 중앙 디렉토리에 백업한다.
# git 호출은 LLM 아님 — git_runner 주입으로 테스트 결정적(_real_models_runner 류 seam 철학).


def _real_git_runner(dest_root: Path) -> GitRunner:
    """실 git 을 dest_root 컨텍스트로 호출하는 GitRunner 를 만든다(fail-soft).

    반환 callable: argv(list) → (returncode, stdout). git 바이너리 부재(shutil.which) 또는
    어떤 예외든 (1, "") 로 감싼다 — git_safe_relpaths 가 이를 None(전부 백업) 폴백으로 본다.
    `git -C <dest> <argv...>` 형태로 항상 dest_root work tree 에 묶는다(_real_models_runner 선례).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple:
        if git_binary is None:
            return 1, ""
        try:
            result = subprocess.run(
                [git_binary, "-C", str(dest_root), *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_SAFE_TIMEOUT_SECONDS,
            )
            return result.returncode, result.stdout or ""
        except Exception:  # noqa: BLE001 — fail-soft: 어떤 예외도 import 를 깨지 않는다.
            return 1, ""

    return runner


# ── upstream URL 안전 계약 + self-describing 분류 ──────────
# upstream 값은 git URL *또는* 로컬 경로다 — self-describing(모양으로 분기). git 을
# 호출하는 모든 경로(ls-remote·remote get-url·rev-parse)가 이 계약을 지킨다:
#   - argv-list(no shell·_real_git_runner 가 항상 list 전달) · leading-dash 거부(옵션 오인)
#   - protocol allowlist(https/ssh/file 명시) · credential-in-URL 거부(SSRF·자격증명 누출)
#   - scp-style(user@host:path)/Windows(C:\)/상대/모호 colon 분기로 URL↔경로 정확 판별
# 비대화 auth(GIT_TERMINAL_PROMPT=0)·timeout 은 git 호출 runner(_real_upstream_git_runner)가
# 강제한다. 이 검증 자체는 *순수 함수*(네트워크 0) — 도달성은 ls-remote 호출부가 따로 본다.

# URL scheme allowlist — https/ssh/file *만*. http(평문)·git://(비인증
# 평문·MITM 취약)·ftp·ext::<cmd>(임의명령)·임의 transport 는 전부 거부(SSRF·중간자·원격 코드
# 실행 회피). git:// 는 ssh 위 전송이 아니라 *비인증 평문*이라 allowlist 에서 뺀다(codex MF2).
_UPSTREAM_URL_SCHEMES = ("https://", "ssh://", "file://")

# scp-style URL(`user@host:path`·`host:path`) 판별 — colon 앞에 슬래시가 없고(경로 아님)
# Windows 드라이브(`C:`)가 아니어야 한다. git 의 scp 문법은 이 모양을 SSH 로 해석한다.
_SCP_LIKE_RE = re.compile(r"^[^/\\:]+@[^/\\:]+:.+$|^[A-Za-z][A-Za-z0-9_.-]*:.+$")

# Windows 드라이브 경로(`C:\...`·`C:/...`) — 단일 알파벳 + colon + 슬래시. scp-style 과
# 구분해 *경로*로 취급(콜론 모호성 해소). 단일문자 호스트의 scp 와 충돌하지 않게 슬래시 요구.
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def classify_upstream(value: str) -> str:
    """upstream 값을 self-describing 으로 분류한다 — 'url' | 'path'.

    스킬층이 freshness 분기(URL→clone/fetch · 경로→pull)에 쓰는 것과 *동일 규칙*을 엔진이
    공유한다(값 모양만 본다·네트워크 0). 판정 순서:
      1. 허용 scheme(`https://`·`ssh://`·`file://`) prefix → 'url'.
      2. Windows 드라이브(`C:\\`·`C:/`) → 'path'(콜론 모호성 우선 해소).
      3. scp-style(`user@host:path`·`host:path`) → 'url'(git SSH 문법).
      4. 그 외 → 'path'(상대/절대 로컬 경로).
    빈 문자열은 'path'(호출부 검증이 별도로 거른다 — 여기선 모양 분류만). scheme allowlist
    밖(git://·http:// 등)은 prefix 매칭이 안 돼 scp-style 분기로 빠질 수 있으나, 안전 거부는
    `validate_upstream_value`(`://` allowlist-밖 명시 reject)가 전담한다(분류 ≠ 허가).
    """
    if any(value.startswith(s) for s in _UPSTREAM_URL_SCHEMES):
        return "url"
    if _WINDOWS_DRIVE_RE.match(value):
        return "path"
    if _SCP_LIKE_RE.match(value):
        return "url"
    return "path"


def validate_upstream_value(value: str) -> tuple[bool, str]:
    """upstream 값의 *순수* 안전 검증 (네트워크 0·도달성은 별도). (ok, reason).

    git 을 호출하기 *전* 입구 가드 — fail-closed(나쁜 값은 거부, silently 기록 금지
    동형). 검사:
      - 빈/공백 거부.
      - leading-dash 거부 — `--upload-pack=...` 류 옵션 오인(argv 첫 위치라도 안전).
      - URL 이면 scheme allowlist(https/ssh/file·git://·http:// 거부) 강제 + credential-in-URL
        (`user:pass@`) 거부(자격증명 누출·SSRF) + **authority(host[:port]) leading-dash 거부**
        — `ssh://-oProxyCommand=...`·`ssh://git@-oProxyCommand=...` 류 ssh 옵션 주입 차단
        (codex MF3·defense-in-depth·git 자체 방어에만 의존하지 않음). scp-style(`user@host:
        path`)의 user 부는 허용(비밀 아님)이나 `user:pass@host` 의 password·transport helper
        (`ext::cmd`)는 거부.
    도달성(ls-remote)·경로 존재는 호출부(검증 후속)가 본다 — 여기선 *형태 안전*만.
    """
    if not value or not value.strip():
        return False, "upstream 값이 비어 있다."
    if value.startswith("-"):
        return False, f"upstream 값이 '-' 로 시작한다(옵션 오인·argv 안전): {value!r}"
    # scheme-form(`X://...`)인데 allowlist 밖이면 명시 거부 — http(평문)·git://(비인증 평문·MITM)·
    # ftp·ext::cmd(임의명령)·임의 transport 차단(SSRF·중간자·원격 코드 실행). scp-style(콜론만·
    # `://` 없음)은 아래에서 분류.
    if "://" in value and not value.startswith(_UPSTREAM_URL_SCHEMES):
        scheme = value.split("://", 1)[0]
        return False, (
            f"upstream URL scheme {scheme!r} 비허용 — 허용: https/ssh/file "
            "(http·git:// 평문·ftp·ext::cmd 등 거부·SSRF/중간자/MITM 방지)."
        )
    kind = classify_upstream(value)
    if kind == "url":
        if value.startswith(_UPSTREAM_URL_SCHEMES) and not value.startswith("file://"):
            # scheme-form URL — authority(userinfo@host[:port]) 부를 분리해 검사.
            after_scheme = value.split("://", 1)[1] if "://" in value else value
            authority = after_scheme.split("/", 1)[0]
            userinfo = authority.split("@", 1)[0] if "@" in authority else ""
            host = authority.split("@", 1)[1] if "@" in authority else authority
            # credential-in-URL(`user:pass@`) 거부(자격증명 누출).
            if userinfo and ":" in userinfo:
                return False, (
                    "upstream URL 에 자격증명(user:pass@)이 박혀 있다 — 거부 "
                    "(누출 위험·credential helper/SSH 키를 쓰라)."
                )
            # MF3: host(또는 userinfo)가 `-` 로 시작하면 ssh 옵션 주입(`-oProxyCommand=...`)으로
            # 해석될 수 있다 — defense-in-depth 로 거부(git 자체 방어에만 의존하지 않음).
            if host.startswith("-") or userinfo.startswith("-"):
                return False, (
                    f"upstream URL 의 host/userinfo 가 '-' 로 시작한다 — 거부 "
                    f"(ssh 옵션 주입·-oProxyCommand 류 차단): {value!r}"
                )
        elif not value.startswith("file://"):
            # scp-style(`user@host:path`) — git transport helper(`ext::cmd`·`fd::N` 등 double-
            # colon)는 임의명령 실행이라 거부(`://` 없이 `::` 가 있는 형태). 정상 scp 는 single
            # colon(`host:path`)뿐이다.
            if "::" in value:
                return False, (
                    f"upstream 값에 git transport helper(`::`)가 있다 — 거부 "
                    f"(ext::cmd 등 임의명령 실행 회피): {value!r}"
                )
            # scp authority 분리 (codex round-2 정정) — git scp 문법은 `[user@]host:path`.
            # **먼저 첫 `:` 로 lhs(`[user@]host`) ↔ path 를 나눈다.** path(첫 `:` 뒤)는 자유 —
            # `@`·`:` 포함 정상(`host:path@v1.git`·`host:sub/dir@ref`)이라 거기서 credential/
            # leading-dash 를 보면 false-reject 한다. authority 해석은 **lhs 안에서만** 한다.
            #
            scp_lhs = value.split(":", 1)[0]
            if "@" in scp_lhs:
                scp_userinfo, scp_host = scp_lhs.split("@", 1)
            else:
                scp_userinfo, scp_host = "", scp_lhs
            # `user:pass@host` 형태의 password 박힘 거부(자격증명 누출) — **lhs 의 userinfo 한정**.
            # scp 문법은 password 를 지원하지 않으므로(그건 scheme URL `https://user:pass@` 형식)
            # path 의 `:`·`@`(첫 `:` 뒤)는 자유다 — `host:path@with:colon`·`user:pass@host:path`
            # (git 은 host=`user`·path=`pass@host:path` 로 본다)는 정상 scp 로 통과한다(codex 알고리즘:
            # authority 해석은 lhs[첫 `:` 앞] 안에서만). 진짜 credential 박힘은 scheme-form 에서 거부.
            if ":" in scp_userinfo:
                return False, (
                    "upstream URL 에 자격증명(user:pass@)이 박혀 있다 — 거부 "
                    "(누출 위험·SSH 키를 쓰라)."
                )
            # MF3: scp host/userinfo leading-dash 거부(ssh 옵션 주입 차단·defense-in-depth).
            if scp_host.startswith("-") or scp_userinfo.startswith("-"):
                return False, (
                    f"upstream URL 의 host/userinfo 가 '-' 로 시작한다 — 거부 "
                    f"(ssh 옵션 주입 차단): {value!r}"
                )
        return True, ""
    # 경로 — 형태 안전(leading-dash 는 위에서 이미 거름). 존재 검증은 호출부.
    return True, ""


def _real_upstream_git_runner() -> GitRunner:
    """upstream git 호출(ls-remote·remote get-url·rev-parse)용 GitRunner (fail-soft).

    `_real_git_runner` 와 달리 `-C <dest>` 로 고정하지 않는다 — 호출부가 `-C <checkout>`·
    `ls-remote <url>` 등 컨텍스트를 argv 로 직접 준다. URL 안전 계약 강제:
      - argv-list(no shell) — subprocess 가 리스트로 받아 셸 해석 0.
      - `GIT_TERMINAL_PROMPT=0` — 비대화 auth(자격증명 프롬프트로 멈추지 않게·CI 안전).
      - **config 격리(MF4)** — `GIT_CONFIG_GLOBAL/SYSTEM=os.devnull` 로 global·system config 를
        무력화(사용자 `insteadOf` rewrite·credential helper 가 untrusted URL 호출에 끼는 것 차단)
        + `GIT_CONFIG_COUNT` 패턴으로 protocol allowlist(https/ssh/file 만)·credential.helper=
        (빈값) 강제. `_real_git_runner`(로컬 추적집합 조회)와 달리 *네트워크-facing* 이라 격리한다.
      - timeout — ls-remote 는 네트워크라 GIT_TIMEOUT 류 상한.
    git 바이너리 부재·예외는 (1, stderr) 로 감싼다(호출부가 rc 로 판정).
    """
    git_binary = shutil.which("git")

    def runner(argv: list) -> tuple:
        if git_binary is None:
            return 1, "git 바이너리를 찾을 수 없음 (PATH)."
        env = dict(os.environ)
        env["GIT_TERMINAL_PROMPT"] = "0"  # 비대화 auth — 자격증명 프롬프트로 멈추지 않는다.
        # MF4: global/system git config 무력화 — insteadOf rewrite·credential helper 가
        # untrusted URL 의 ls-remote/rev-parse 에 끼는 것을 막는다(os.devnull = Win/POSIX 공통).
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        # hardening(codex): 상속된 protocol 우회 env 제거(방어층) — GIT_ALLOW_PROTOCOL 은
        # protocol.allow allowlist 를 우회하고, GIT_PROTOCOL_FROM_USER 는 user-given protocol
        # 게이트를 푼다. 둘 다 pop 해 우리 GIT_CONFIG allowlist 가 단일 권위가 되게 한다.
        env.pop("GIT_ALLOW_PROTOCOL", None)
        env.pop("GIT_PROTOCOL_FROM_USER", None)
        # GIT_CONFIG_COUNT 패턴으로 protocol allowlist·credential.helper=(빈값) 주입(`-c` 동치·
        # worktree_pool 선례). 기존 env 의 GIT_CONFIG_* 잔여는 우리 카운트로 덮어 결정론 보장
        # (env 격리는 sub-process[ssh transport]까지 전파돼 argv `-c` 보다 안전 — env 단일 채널).
        for idx, (key, val) in enumerate(_UPSTREAM_GIT_CONFIG_KV):
            env[f"GIT_CONFIG_KEY_{idx}"] = key
            env[f"GIT_CONFIG_VALUE_{idx}"] = val
        env["GIT_CONFIG_COUNT"] = str(len(_UPSTREAM_GIT_CONFIG_KV))
        try:
            result = subprocess.run(
                [git_binary, *argv],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=UPSTREAM_GIT_TIMEOUT_SECONDS,
                env=env,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부 위임.
            return 1, str(exc)

    return runner


def derive_origin_url(checkout_root: Path, *, git_runner: GitRunner | None = None) -> str | None:
    """로컬 git checkout 의 `git remote get-url origin` 을 읽어 URL 을 도출한다.

    로컬 clone 을 `--from` 으로 받았을 때, future update 기록(`--upstream` 생략 시)을 *그
    checkout 경로* 대신 origin URL 로 자동도출하는 데 쓴다(릴리스 추적 기본).
    rev-parse 와 동일 안전 계약(argv-list·timeout·GIT_TERMINAL_PROMPT=0)을 `_real_upstream_
    git_runner` 가 강제한다. git repo 아님·origin 부재·도출 URL 이 검증 실패 → None(graceful·
    호출부가 경로 fallback). 도출 URL 도 `validate_upstream_value` 로 fail-closed 검증.
    """
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    rc, out = runner(["-C", str(checkout_root), "remote", "get-url", "origin"])
    if rc != 0:
        return None
    url = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not url:
        return None
    ok, _reason = validate_upstream_value(url)
    if not ok:
        return None
    return url


def read_upstream_rev(checkout_root: Path, *, git_runner: GitRunner | None = None) -> str | None:
    """로컬 git checkout 의 `git rev-parse HEAD` 를 읽는다 — drift baseline.

    `upstream.rev=<commit>` baseline 기록의 입력이다. checkout_root 가 가리키는
    로컬 git work tree 의 현재 HEAD commit 을 읽는다 — git repo 아님·HEAD 해소 실패는 None
    (graceful·기록 생략). URL upstream(로컬 checkout 없음)은 baseline 을 못 읽으므로 호출부가
    경로 upstream 에 한해 호출한다(스킬층이 URL 의 seen-rev 를 별도 기록·`upstream.seen_rev`).
    안전 계약은 `_real_upstream_git_runner`(argv-list·timeout·GIT_TERMINAL_PROMPT=0).
    """
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    rc, out = runner(["-C", str(checkout_root), "rev-parse", "HEAD"])
    if rc != 0:
        return None
    rev = out.strip().splitlines()[0].strip() if out.strip() else ""
    return rev or None


def _parse_status_dirty(porcelain_z: str) -> set:
    """`git status --porcelain -z` 출력 → dirty·untracked relpath(posix) 집합.

    NUL(`\\0`) 구분. 각 엔트리는 `XY <path>`(2-char 상태 + 공백 + 경로). rename(R/C)은
    `<new>\\0<old>` 로 *두 NUL 필드*를 쓰므로(상태 2칸이 R/C 로 시작), old-path 필드를 건너뛴다.
    untracked(`??`)·수정·staged 전부 "git-safe 아님"으로 본다(보수적 — 중앙 백업 대상).
    """
    dirty: set = set()
    parts = porcelain_z.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        # 엔트리 형식: 2-char XY 상태 + 공백 + 경로. 경로 추출(상태 3칸 이후).
        status = entry[:2]
        path = entry[3:] if len(entry) > 3 else ""
        if path:
            dirty.add(path)
        # rename/copy 는 다음 필드가 old-path — 건너뛴다(경로로 오해 방지).
        if status and status[0] in ("R", "C"):
            i += 2
        else:
            i += 1
    return dirty


def git_safe_relpaths(
    dest_root: Path,
    *,
    git_runner: GitRunner | None = None,
) -> set | None:
    """dest_root 가 git work tree 면 '추적 중 & 미변경' relpath(posix) 집합 반환.

    git 아님 / 바이너리 부재 / 오류 → None (= 전부 백업·보수적 fail-soft·안전).

    구현: `git rev-parse --is-inside-work-tree`(work tree 판별) → `ls-files -z`(추적 집합)
    − `status --porcelain -z`(dirty·untracked). 차집합 = 추적&미변경. git_runner 주입으로
    테스트 결정적(`shutil.which("opencode")`·`_real_models_runner` 동일 seam 철학).

    ⚠️ 경로 기준 정규화: `git -C <dest> ls-files` 는 **cwd(=dest_root) 상대**,
    `status --porcelain` 은 **repo-root 상대** 경로를 낸다. dest_root 가 repo 루트가 아닌 *하위
    디렉토리*면 두 기준이 달라 dirty 가 `tracked − dirty` 에서 안 빠진다 → dirty 를 git-safe 로
    오판해 무백업 덮을 위험. `rev-parse --show-prefix` 로 dirty(repo-root 상대)를 dest_root 상대로
    환산(prefix 하위만·prefix 제거)해 tracked(dest_root 상대)와 기준을 맞춘다. 반환 relpath 는
    plan_copy 의 `rel.as_posix()` 비교 기준(dest_root 상대 posix)과 일치한다.
    """
    runner = git_runner if git_runner is not None else _real_git_runner(dest_root)

    rc, out = runner(["rev-parse", "--is-inside-work-tree"])
    if rc != 0 or out.strip() != "true":
        return None  # git work tree 아님(또는 호출 실패) — 전부 백업.

    rc_pref, prefix_out = runner(["rev-parse", "--show-prefix"])
    if rc_pref != 0:
        return None  # prefix 조회 실패 — 기준 정규화 불가 → 보수적으로 전부 백업.
    prefix = prefix_out.strip()  # 하위 디렉토리면 'sub/dir/'(posix·trailing slash), repo 루트면 ''.

    rc_tracked, tracked_out = runner(["ls-files", "-z"])
    if rc_tracked != 0:
        return None  # 추적 집합 조회 실패 — 보수적으로 전부 백업.
    tracked = {p for p in tracked_out.split("\0") if p}  # cwd(=dest_root) 상대.

    rc_status, status_out = runner(["status", "--porcelain", "-z"])
    if rc_status != 0:
        return None
    dirty_repo = _parse_status_dirty(status_out)  # repo-root 상대.
    # repo-root 상대 dirty → dest_root 상대(prefix 하위만 남기고 prefix 제거). prefix='' 면 그대로.
    dirty = ({p[len(prefix):] for p in dirty_repo if p.startswith(prefix)}
             if prefix else dirty_repo)

    # 추적 중 & 미변경 = 추적 집합 − dirty/untracked. git 이 이 내용을 복원할 수 있다.
    return tracked - dirty


# ── plan 액션 ──────────────────────────────────────────────────────────────
# 결정적 단계(복사·백업·치환)를 액션 리스트로 모은 뒤 apply 에서 실행한다.
# plan/apply 분리 = dry-run 결정론(파일시스템 미변경) + 테스트 용이성 (pm_update 패턴).

class CopyAction:
    """src 파일을 dst 로 복사. 기존 dst 가 있으면 중앙 디렉토리에 백업 후 덮음(--into 비파괴).

    backup:
      - None  = 백업 안 함 — 신규 파일이거나 git-safe(추적&미변경, git 이 복원 가능).
      - Path  = `<dest>/.pm_import_backups/<DATE>/<relpath>` (중앙화·relpath 미러링).
                대상 디렉토리는 run() 이 mkdir(parents) 로 만든다.
    """

    def __init__(self, src: Path, dst: Path, backup: Path | None,
                 dest_root: Path | None = None, *,
                 existed: bool | None = None, is_symlink: bool | None = None):
        self.src = src
        self.dst = dst
        self.backup = backup  # None = 백업 안 함(신규 또는 git-safe)
        # dest 루트 바인딩 — `run()` 이 dest 상대 fd 순회로 쓰기 위해 필요하다(plan_copy 가 넣는다).
        #   계획 전용으로 손-구성한 액션(dry-run 미리보기 등)은 None 이고 `run()` 이 fail-loud 한다.
        self.dest_root = dest_root
        # **계획 시점 관측 상태**를 액션에 박아 둔다 — `run()` 이 적용 시점에 다시 보면 그 사이의
        #   생성·삭제가 조용히 다른 동작으로 바뀐다(계획은 "신규" 라 백업이 없는데 그 사이 생긴
        #   사용자 파일을 무백업으로 덮거나, 계획은 "기존" 인데 그 사이 지워진 파일을 되살린다).
        #   인자 생략(손-구성 액션)이면 **생성 시점**을 계획 시점으로 보고 여기서 관측한다.
        self.planned_symlink = dst.is_symlink() if is_symlink is None else is_symlink
        self.planned_existed = (
            (self.planned_symlink or dst.exists()) if existed is None else existed)

    def describe(self) -> str:
        rel = self.dst
        # lite 배치: src 가 `X.lite.md` 인데 dst 가 `X.md` 면 이름이 치환됐다 —
        #   어느 변종이 어디로 가는지 보이게 한다("CLAUDE.lite.md → CLAUDE.md (lite)").
        lite_note = ""
        if self.src.name.endswith(LITE_SUFFIX) and self.src.name != self.dst.name:
            lite_note = f"  ({self.src.name} → {self.dst.name}, lite)"
        # 3분기: 신규([copy]) · 충돌 git-safe(백업 생략 [copy · git-safe]) ·
        #   충돌 비-safe([backup+copy] → 중앙 디렉토리 상대경로 `.pm_import_backups/<DATE>/<rel>`).
        if self.backup is not None:
            tail = self.backup.as_posix()
            idx = tail.find(BACKUP_DIR_NAME)
            shown = tail[idx:] if idx != -1 else self.backup.name
            return f"  [backup+copy] {rel}  (→ {shown}){lite_note}"
        if self._git_safe_skip:
            return f"  [copy · git-safe] {rel}{lite_note}"
        return f"  [copy] {rel}{lite_note}"

    # describe() 가 신규([copy])와 git-safe skip([copy · git-safe])을 구분하려면 충돌 여부를
    # 알아야 한다 — plan_copy 가 충돌&git-safe 인 액션에 표시한다(기본 False = 신규).
    _git_safe_skip: bool = False

    def run(self, root_identity: tuple | None = None) -> None:
        """복사를 적용한다 — 목적지 쓰기는 **dest 상대 fd 순회**(symlink 미추종)로만 한다.

        옛 코드는 `dst.parent.mkdir(parents=True)` + `shutil.copy2(src, dst)` 로 경로를 열어,
        계획과 복사 사이에 조상·목적지·**dest 루트 자체**가 symlink 로 바뀌면 저장소 밖 트리에
        먼저 썼다(재렌더 채널만 닫아서는 남는 구멍). 이제 디렉토리 생성과 파일 쓰기가 모두
        `_open_dest_relative_nofollow`(컴포넌트별 `O_NOFOLLOW` + 루트 신원 대조)를 탄다.
        원본(src)은 신뢰 소스(템플릿)라 경로로 읽는다."""
        if self.dest_root is None:
            raise ValueError(
                "CopyAction.run 은 dest_root 바인딩이 필요합니다 — 실행 대상은 plan_copy 산출뿐"
                "입니다(손-구성 액션은 계획 미리보기 전용).")
        rel = self.dst.relative_to(self.dest_root)
        # MF1: 기존 dst 가 symlink 이면 shutil.copy2 가 링크를 *따라가* 링크 대상(프로젝트
        #      밖일 수 있음) 파일을 백업/덮어쓴다 — 비파괴 보장 위반 + 외부 파일 변조 위험.
        #      따라서 symlink 는 *링크 자체*를 처리한다: 링크를 그대로 백업(follow 안 함) →
        #      링크 unlink → 일반 파일로 src 복사. 링크 대상 파일은 절대 건드리지 않는다.
        # 계획 뒤 상태가 달라졌으면 **그 파일은 건드리지 않는다** — 적용 시점 관측으로 동작을
        #   갈아타면 (a) 계획엔 없던 새 파일(백업 계획도 없음)을 무백업으로 덮고 (b) 그 사이
        #   사용자가 지운 파일을 되살린다. 둘 다 비파괴 위반이라 호출부가 loud 로 제외한다.
        dst_is_symlink = self.dst.is_symlink()
        dst_existed = dst_is_symlink or self.dst.exists()
        if (dst_existed, dst_is_symlink) != (self.planned_existed, self.planned_symlink):
            was = "있었" if self.planned_existed else "없었"
            now = "생겼" if dst_existed else "사라졌"
            raise PlanStateChangedError(
                f"계획 시점과 상태가 다릅니다({rel.as_posix()}: 계획 때는 {was}고 지금은 "
                f"{now}습니다) — 계획에 없던 처리를 하지 않기 위해 이 파일을 건너뜁니다.")
        # 링크 교체 축(아래 unlink)에서 쓸 leaf 신원 — 상태 확인과 **같은 시점** 값이어야 한다.
        leaf_identity = (
            _dest_leaf_identity(self.dest_root, rel, root_identity=root_identity)
            if dst_is_symlink else None)
        # 백업·삭제는 **dest 만 만지는** 단계다 — 상태 확인 뒤에도 삭제 경쟁이 남으므로
        #   `FileNotFoundError` 를 상태 변화 클래스로 정규화한다(그대로 새면 적용 루프가 죽는다).
        #   소스 쪽 오류는 쓰기 단계가 별도로 가른다(진짜 누락은 그대로 올린다).
        try:
            if dst_existed and not dst_is_symlink:
                # **기존 일반 파일: fd 하나로 백업 읽기와 덮어쓰기를 모두 한다.** 옛 흐름은 백업과
                #   쓰기가 각각 leaf 를 다시 열어, 그 사이 같은 자리가 다른 파일로 바뀌면 백업 없는
                #   새 파일을 잘랐다. 열린 fd 는 계획이 본 inode 에 묶이므로 그 창 자체가 없다
                #   (재검사로 좁히는 게 아니라 구조적 폐쇄 — 루트 신원 고정과 같은 원리).
                _refresh_existing_dest_file(
                    self.dest_root, rel, self.src,
                    backup_base_rel=(
                        self.backup.relative_to(self.dest_root)
                        if self.backup is not None else None),
                    root_identity=root_identity)
                return
            if self.backup is not None and dst_existed:
                # SF1: 백업 경로가 이미 존재하면(같은 날 재실행 등) 덮지 말고 순번 부여 —
                #      가장 오래된 원본(=진짜 사용자 파일)이 살아남게 한다(`_free_backup_path`).
                # 링크 *자체* 백업은 fd 스트리밍으로 표현할 수 없다(내용이 아니라 대상 문자열을
                #   보존해야 한다). 그래서 양쪽 **부모 디렉토리 fd** 안에서 `readlink`/`symlink`
                #   만 한다 — 옛 `copy2(follow_symlinks=False)` 는 원본·백업 경로를 다시 해소해,
                #   부모가 그 사이 교체되면 밖의 링크를 읽거나 밖에 링크를 만들었다.
                _backup_symlink_nofollow(
                    self.dest_root, rel, self.backup.relative_to(self.dest_root),
                    root_identity=root_identity)
            if dst_is_symlink:
                # 링크는 fd 로 붙들 수 없으므로(열면 대상이 열린다) **백업 시점 신원(lstat)** 을
                #   재대조한 뒤 지운다 — 백업한 그 링크가 아니면 건드리지 않는다. 재대조와 unlink
                #   사이 한 syscall 창은 남는다(구조적 폐쇄가 아님·명시).
                _unlink_dest_leaf_if_unchanged(
                    self.dest_root, rel, leaf_identity, root_identity=root_identity)
        except (FileExistsError, FileNotFoundError) as exc:
            raise PlanStateChangedError(
                f"계획 뒤 대상 상태가 달라져 백업·정리를 멈춥니다({rel.as_posix()}: {exc}) — "
                "이 파일을 건너뜁니다(원본·백업 모두 불변).") from exc
        _write_dest_file_from_source_nofollow(
            self.dest_root, rel, self.src, overwrite=False,
            root_identity=root_identity)


class FileVsDirConflict(Exception):
    """dst 위치에 기존 *디렉토리* 가 있어 파일 복사가 불가능.

    src 는 파일인데 dst 가 디렉토리면 shutil.copy2 가 IsADirectoryError 로 터지고, 백업도
    안 된다(디렉토리는 copy2 대상 아님). 비파괴 보장상 사용자 디렉토리를 자동 삭제할 수 없으니
    plan 단계에서 명시적으로 거부한다(apply 부분 복사 전 차단).
    """


class AncestorConflict(Exception):
    """dst 의 *조상* 경로(dest_root 하위)에 symlink·비-디렉토리 파일이 있어
    안전하게 디렉토리를 만들 수 없다.

    위험 둘:
      - 조상이 symlink(프로젝트 밖 가리킴)면 `dst.parent.mkdir(exist_ok=True)`+`shutil.copy2`
        가 링크를 따라가 **프로젝트 밖**에 쓴다 — 비파괴 위반.
      - 조상이 일반 파일이면 plan 은 통과한 뒤 apply 중 `mkdir` 가 터져 **부분 복사** 잔존.
    CopyAction.run 의 dst-자체 symlink 처리(MF1)는 조상은 못 막으므로 plan 단계에서 조상
    컴포넌트를 따로 거부한다(dry-run·apply 모두 안전). dest_root 자신은 --into/--new 가드가
    처리하므로 그 *하위* 조상에만 집중한다.
    """


class DestRootSwappedError(RuntimeError):
    """계획 시점에 고정한 **dest 루트 자체**가 다른 디렉토리로 바뀌었다.

    파일 단위 경로 교체(`UnsafeDestPathError`)와 **의도적으로 다른 클래스**다 — 그쪽은 그 파일만
    빼고 진행하지만, 루트가 바뀐 뒤의 계속 진행은 남은 단계(치환·fill·board init 등 우리가 fd 로
    감싸지 못하는 외부 단계 포함)가 **교체된 트리에 쓰는** 것을 뜻한다. 그래서 즉시 전체 중단이다.

    이 시점의 중단은 "부분 적용을 남긴다"가 아니라 **오염 차단**이다: 대상 트리는 이미 공격자가
    바꿔치기한 것이라 거기서 무엇을 더 하든 원래 인스턴스를 완성하지 못한다. 남은 작업을 계속해
    저장소 밖에 쓰는 것보다, 멈추고 사람에게 알리는 쪽이 항상 낫다."""


class PlanStateChangedError(RuntimeError):
    """계획 시점에 관측한 대상 상태(존재/부재·symlink 여부)가 적용 시점에 달라졌다.

    적용 시점 재관측으로 동작을 갈아타면 두 방향 모두 비파괴를 깬다 — 계획이 "신규"(백업 없음)인
    자리에 그 사이 생긴 사용자 파일을 무백업으로 덮거나, 계획이 "기존"인 자리에서 그 사이 지워진
    파일을 되살린다. **파일 단위 제외 + loud** 클래스다(루트 교체와 달리 전체 중단 아님)."""


class UnsafeDestPathError(RuntimeError):
    """dest 안이어야 할 경로가 symlink·조상 symlink·`..` 로 저장소 밖을 가리켜 거부됐다.

    읽기/쓰기 전에 `_is_safe_dest_path` 로 판정하고 **작업 시작 전** 던진다(부분 적용 0·외부
    파일 불변). RuntimeError 를 상속해 옛 호출부 처리를 유지하면서, CLI 경계
    (`add_harness_cli`)가 이 타입만 골라 친화 메시지 + rc 1 로 번역한다(traceback 0)."""


def _free_backup_path(backup: Path) -> Path:
    """backup 경로가 비었으면 그대로, 점유됐으면 .1·.2… 순번을 붙여 빈 경로 반환.

    점유 판정은 **링크를 따라가지 않는다**(`os.path.lexists`) — `exists()` 는 **깨진 symlink**
    에 False 를 주므로, 그 자리를 "비었다"고 보고 그대로 쓰면 뒤이은 `shutil.copy2` 가 링크를
    따라가 **저장소 밖에 파일을 만든다**. 링크 자체가 있으면 점유로 보고 다음 순번을 고른다
    (원본 보존 + 외부 쓰기 차단)."""
    if not os.path.lexists(backup):
        return backup
    n = 1
    while True:
        candidate = backup.with_name(f"{backup.name}.{n}")
        if not os.path.lexists(candidate):
            return candidate
        n += 1


# lite 진입 파일 관례: `X.lite.md` 는 진입 `X.md` 의 lite 변종이다.
# (예: CLAUDE.lite.md → CLAUDE.md, AGENTS.lite.md → AGENTS.md.) 임의의 `*.lite.md` 에 일반화.
LITE_SUFFIX = ".lite.md"


class SkippedTemplateShippingEntryWarning(RuntimeWarning):
    """pm-import가 byte-copy할 수 없는 템플릿 엔트리를 명시적으로 제외했다는 신호."""


class EmptyTemplateShippingInventoryError(RuntimeError):
    """존재하는 출하 템플릿의 tracked 인벤토리가 0건인 결함."""

    def __init__(
            self, checkout: Path, subtree: str = ".",
            *, filesystem_fallback: bool = False) -> None:
        self.checkout = Path(checkout)
        self.subtree = subtree
        self.filesystem_fallback = filesystem_fallback
        diagnosis = (
            "filesystem 강등 상태이므로 소스 디렉토리가 비었는지와 checkout 루트가 "
            "올바른지 확인하라"
            if filesystem_fallback
            else "checkout 루트가 올바른지와 git index에 출하 파일이 등재됐는지 확인하라"
        )
        super().__init__(
            "pm-import 출하 인벤토리가 0건임 "
            f"(checkout={self.checkout}, subtree={subtree!r}); "
            + diagnosis
        )


def _full_relpath_for_lite(rel: Path) -> Path:
    """lite 변종 relpath `X.lite.md` → full 진입 relpath `X.md` (이름 치환).

    `X.lite.md` 만 매핑한다(이름이 정확히 `.lite.md` 로 끝나는 경우). 반환 relpath 는
    같은 디렉토리의 `X.md` 다 — lite 배치 시 dst 가 full 진입 이름으로 들어가게 한다.
    """
    base = rel.name[: -len(LITE_SUFFIX)]  # 'X.lite.md' → 'X'
    return rel.with_name(base + ".md")


def _shippable_template_files(repo_files, template_root: Path) -> list[Path]:
    """repo 출하 엔트리를 링크를 추종하지 않는 일반 파일 목록으로 좁힌다.

    git 인벤토리는 index mode가 진실이므로 일반 파일 mode만 허용한다. 비-git 폴백은 mode를
    얻을 수 없어 그 경로에서만 lstat()으로 등가 판정한다. 두 판정을 한 엔트리에 함께 적용하면
    index와 working tree 중 어느 쪽이 출하 계약인지 갈리므로 의도적으로 섞지 않는다.

    0건 판정은 복사 계획 수가 아니라 이 원시 열거 수에 둔다. ``plan_copy``는 목적지 byte를
    비교하지 않아 동일 트리 재-import도 모든 출하 파일을 다시 계획하므로 정당한 zero-action
    no-op이 없고, 빈 원시 인벤토리만 출하 결함이다.
    """
    accepted: list[Path] = []
    skipped: dict[str, list[str]] = {
        "symlink(index mode 120000, 링크 대상 내용을 복사하지 않음)": [],
        "gitlink(index mode 160000, 파일 byte-copy 대상 아님)": [],
        "지원하지 않는 git index mode": [],
        "filesystem 폴백 symlink(lstat, 링크 대상 내용을 복사하지 않음)": [],
        "filesystem 폴백 일반 파일이 아닌 엔트리(lstat)": [],
        "filesystem 폴백 lstat 실패": [],
    }
    entries = repo_files.list_repo_owned_entries(
        template_root, ".", mode=repo_files.TRACKED_ONLY
    )
    if not entries:
        runner = repo_files._real_git_runner(template_root)
        probe_rc, inside = runner(["rev-parse", "--is-inside-work-tree"])
        filesystem_fallback = not (
            probe_rc == 0 and inside.strip() == "true")
        # seam의 빈 결과는 부분 subtree 질의에는 유효할 수 있다. 그러나 선택된 템플릿은
        # 프레임워크 출하 단위이므로 소비 지점에서 직접 막아 warning의 조건부 발화에 기대지 않는다.
        raise EmptyTemplateShippingInventoryError(
            template_root, ".", filesystem_fallback=filesystem_fallback)
    for entry in entries:
        rel = entry.path
        source = template_root / rel
        index_mode = entry.index_mode
        if index_mode in {"100644", "100755"}:
            accepted.append(source)
            continue
        if index_mode == "120000":
            skipped["symlink(index mode 120000, 링크 대상 내용을 복사하지 않음)"].append(
                rel.as_posix()
            )
            continue
        if index_mode == "160000":
            skipped["gitlink(index mode 160000, 파일 byte-copy 대상 아님)"].append(
                rel.as_posix()
            )
            continue
        if index_mode is not None:
            skipped["지원하지 않는 git index mode"].append(
                f"{rel.as_posix()} ({index_mode})"
            )
            continue

        # filesystem 폴백에는 index mode가 없으므로 여기서만 링크 비추종 lstat 판정을 쓴다.
        try:
            fallback_mode = source.lstat().st_mode
        except OSError as exc:
            skipped["filesystem 폴백 lstat 실패"].append(
                f"{rel.as_posix()} ({exc})"
            )
            continue
        if stat.S_ISREG(fallback_mode):
            accepted.append(source)
        elif stat.S_ISLNK(fallback_mode):
            skipped[
                "filesystem 폴백 symlink(lstat, 링크 대상 내용을 복사하지 않음)"
            ].append(rel.as_posix())
        else:
            skipped["filesystem 폴백 일반 파일이 아닌 엔트리(lstat)"].append(
                rel.as_posix()
            )

    for reason, paths in skipped.items():
        if paths:
            warnings.warn(
                f"pm-import 템플릿 출하 엔트리 {len(paths)}건 제외 — {reason}: "
                + ", ".join(paths),
                SkippedTemplateShippingEntryWarning,
                stacklevel=2,
            )
    return accepted


def _iter_source_files(template_root: Path, weight: str = "full"):
    """template_root의 repo 추적 파일을 (dst relpath, 절대경로)로. 정책 경로 추가 제외.

    weight 관례 (`*.lite.md` = `*.md` 의 lite 변종):
      - full(기본): 모든 `*.lite.md` 를 복사 대상에서 *제외*한다(lite 변종이 full 배포에
        끼면 안 됨). full `X.md` 는 그대로 복사.
      - lite: 각 `X.lite.md` 를 dst relpath `X.md` 로 복사(이름 치환). 동시에 (a) 같은
        트리의 full `X.md` 는 복사 제외(lite 가 그 자리를 차지), (b) 원본 이름 `X.lite.md`
        도 그대로는 복사 제외(dst 에 `*.lite.md` 잔존 금지).
    yield 하는 relpath 는 *dst* 기준이므로 lite 모드에선 `X.md` 로 치환돼 나간다 —
    placeholder 치환(copied_relpaths 기준)·다중-tree 충돌 판정이 이 dst relpath 위에서 돈다.

    출하 인벤토리는 공용 repo_owned_files seam의 TRACKED_ONLY 의미를 쓴다. 따라서 유지보수자
    checkout의 미추적·machine-local 파일은 복사 후보가 아니며, 비-git 소스에서 filesystem
    폴백할 때는 seam이 RepoFilesFallbackWarning으로 추적 보장 소실을 loud하게 알린다.
    COPY_EXCLUDE_*는 추적 여부와 별개인 출하 정책(node_modules·stale bytecode·내부 README)과
    fill 스캔 공통 방어를 위해 그 위에 계속 적용한다.
    """
    repo_files = _load_repo_owned_files()
    files = [
        path
        for path in _shippable_template_files(repo_files, template_root)
        if not any(
            part in COPY_EXCLUDE_DIR_NAMES
            for part in path.relative_to(template_root).parts
        )
        # 프레임워크-내부 doc(최상위 README.md 등)은 adopter 로 출하하지 않는다.
        and path.relative_to(template_root).as_posix() not in COPY_EXCLUDE_RELPATHS
    ]
    # lite 모드: 이 트리에서 lite 가 대체할 full 진입 relpath 집합을 먼저 모은다.
    lite_overridden: set[Path] = set()
    if weight == "lite":
        for path in files:
            rel = path.relative_to(template_root)
            if rel.name.endswith(LITE_SUFFIX):
                lite_overridden.add(_full_relpath_for_lite(rel))

    for path in files:
        rel = path.relative_to(template_root)
        is_lite = rel.name.endswith(LITE_SUFFIX)
        if weight == "lite":
            if is_lite:
                # X.lite.md → dst X.md (이름 치환). 원본 lite 이름은 dst 에 안 남는다.
                yield _full_relpath_for_lite(rel), path
                continue
            if rel in lite_overridden:
                # full X.md 는 같은 트리의 lite 변종이 그 자리를 차지하므로 제외.
                continue
            yield rel, path
        else:  # full
            if is_lite:
                # lite 변종은 full 배포에 끼면 안 됨 — 제외.
                continue
            yield rel, path


def _check_ancestor_safe(dest_root: Path, dst: Path, checked: set[Path]) -> None:
    """dest_root 와 dst 사이의 조상 경로 컴포넌트가 안전하게 디렉토리화 가능한지 검증.

    이미 존재하는 조상 컴포넌트가 symlink 이거나 비-디렉토리 파일이면
    AncestorConflict 로 거부한다(plan 단계 — apply 부분 복사·외부 쓰기 전 차단). dest_root
    자신은 상위 가드가 처리하므로 *하위* 조상만 본다. checked 는 이미 검증한 조상 캐시
    (같은 디렉토리를 매 파일마다 재검사하지 않게 — 결정론·성능).
    """
    for ancestor in reversed(dst.parents):
        # dest_root 자신과 그 상위는 --into/--new 가드 소관 — 하위 조상만 검사.
        if ancestor == dest_root or dest_root not in ancestor.parents:
            continue
        if ancestor in checked:
            continue
        if ancestor.is_symlink():
            raise AncestorConflict(
                f"dst 조상 경로가 symlink 입니다: {ancestor}. 링크를 따라가면 프로젝트 밖에 "
                f"쓸 수 있어 거부합니다(비파괴). 해당 링크를 직접 옮기거나 제거한 뒤 다시 "
                f"시도하세요."
            )
        if ancestor.exists() and not ancestor.is_dir():
            raise AncestorConflict(
                f"dst 조상 경로에 디렉토리가 아닌 파일이 있습니다: {ancestor}. 그 안에 "
                f"디렉토리를 만들 수 없어 거부합니다(부분 복사 방지). 해당 파일을 직접 "
                f"옮기거나 제거한 뒤 다시 시도하세요(비파괴 — 자동 삭제하지 않습니다)."
            )
        checked.add(ancestor)


def plan_copy(
    template_roots: list[Path],
    dest_root: Path,
    backup_root: Path | None,
    weight: str = "full",
    *,
    git_safe: set | None = None,
    skip_existing_relpaths: set[Path] | None = None,
    include_relpath: Callable[[Path], bool] | None = None,
    source_overrides: dict[tuple[Path, Path], Path] | None = None,
) -> list[CopyAction]:
    """선택된 어댑터 트리들 → dest 복사 액션(여러 트리는 relpath 기준 병합).

    backup_root: None 이면 백업 안 함(--new — 빈 디렉토리 보장). 비-None 이면 기존 충돌
    파일을 *중앙 디렉토리* `backup_root/<relpath>` 로 백업(--into). 형제 `*.backup.<DATE>`(트리
    전역 분산) 대신 단일 디렉토리로 모은다.

    git_safe: git_safe_relpaths 의 반환 — '추적 중 & 미변경' relpath(posix) 집합 또는
    None. None 이면 git 판정 불가(비-git·오류) → 모든 충돌을 백업(보수적). 집합이면 그 안의
    relpath 는 git 이 복원 가능하므로 백업 없이 덮는다(git-safe skip — 액션 _git_safe_skip 표시).

    weight: 'full'(기본) 이면 `*.lite.md` 를 제외, 'lite' 면 `X.lite.md` 를
    dst `X.md` 로 rename 복사(같은 트리 full `X.md` 제외). _iter_source_files 가 이 관례를
    적용해 dst relpath 를 산출하므로, 아래 다중-tree 중복 판정·치환 범위는 모두 *dst relpath*
    위에서 일관되게 돈다(lite 모드에선 `X.md` 가 dst — 선택 트리들이 각자 lite 변종을 깐다).

    MF3: 여러 선택 트리에서 같은 relpath 가 중복되면(예: 공유 엔진), **내용이 같을
    때만** 조용히 skip 한다. 서로 다른 하네스가 같은 dest relpath 에 다른 bytes 를 공급하면
    첫 트리(registry 정규 순서)를 우선하되 stderr 경고를 남긴다. CLI 입력 순서는 집합 정규화에서
    소거되므로 충돌 결과에 영향을 주지 않는다. 이 우선순위는 결정적
    충돌 해소 정책이지 첫 트리가 나머지의 상위집합이라는 전제를 두지 않는다. 단,
    engine.manifest는 복사 뒤 ``_install_selected_manifest_union``이 선택 트리 선언의 합집합으로
    다시 쓰므로 이 첫-tree 충돌 경고 대상이 아니다. 공유 자동-load 진입문서의 하네스 공존은
    ``NEUTRAL_SHARED_ENTRY_DOCS`` 선언이 중립 source로 통일한다.
    """
    seen: dict[Path, tuple[Path, str]] = {}  # relpath → (채택된 src, 채택 트리명)
    skip_existing_relpaths = skip_existing_relpaths or set()
    source_overrides = source_overrides or {}
    actions: list[CopyAction] = []
    checked_ancestors: set[Path] = set()  # 검증 완료 조상 캐시(중복 검사 회피).
    for template_root in template_roots:
        for rel, src in _iter_source_files(template_root, weight):
            # 같은 자동-load 진입 doc을 공유하는 하네스 조합은 선언된 중립 source로 병합한다.
            # key에 template_root까지 포함해, 미래의 제4 하네스가 같은 relpath를 쓰더라도 이
            # 그룹 밖 source를 무관하게 덮지 않는다.
            src = source_overrides.get((template_root, rel), src)
            # add_harness처럼 전체 template 중 일부 namespace만 배포할 때는, 범위 밖 파일의
            # conflict/backup/ancestor safety 검증도 만들지 않는다. 최종 action 필터보다 앞에 둬
            # 무관한 engine 파일이 backup root 검증을 발화시키지 않게 한다.
            if include_relpath is not None and not include_relpath(rel):
                continue
            if rel in seen:
                prev_src, prev_tree = seen[rel]
                if _same_bytes(prev_src, src):
                    # 공유 엔진 등 byte-identical — 한 번만 복사(정상).
                    continue
                if rel.as_posix() == ".project_manager/engine.manifest":
                    # 직후 선택 flavor manifest 합집합으로 교체된다. 첫 tree가 최종 결과라는
                    # 일반 충돌 문구를 내면 사용자에게 거짓을 말하므로 여기서는 경고하지 않는다.
                    continue
                # 내용이 다른 중복 — 첫 트리(우선) 채택을 명시적으로 경고.
                print(
                    f"경고: 선택 트리 중복 relpath 내용 불일치 — '{rel.as_posix()}' 는 "
                    f"registry 정규 순서상 첫 트리 '{prev_tree}' 것을 우선함. "
                    f"후순위 트리: '{template_root.name}'.",
                    file=sys.stderr,
                )
                continue
            seen[rel] = (src, template_root.name)
            dst = dest_root / rel
            # caller가 create-if-absent로 선언한 기존 파일은 일반 backup/action/ancestor safety
            # 계산 전에 action 자체를 만들지 않는다. instance-owned config는 backup이 있어도
            # overwrite 권한이 생기지 않으며, 손상된 backup root가 그 보존 분기까지 막아서도 안 된다.
            if rel in skip_existing_relpaths and (dst.exists() or dst.is_symlink()):
                continue
            # dst 조상(dest_root 하위)이 symlink·비-디렉토리 파일이면 거부 —
            #      링크 follow 로 프로젝트 밖 쓰기 / apply 중 mkdir 실패 부분복사 방지.
            _check_ancestor_safe(dest_root, dst, checked_ancestors)
            # dst 가 (symlink 아닌) 디렉토리면 파일 복사·백업 불가 — plan 에서
            #      거부(apply 부분 복사 전 차단). symlink 는 run() 이 링크 자체로 처리하므로 제외.
            if dst.is_dir() and not dst.is_symlink():
                raise FileVsDirConflict(
                    f"dst 위치에 기존 디렉토리가 있어 파일을 쓸 수 없습니다: {dst}. "
                    f"충돌하는 디렉토리를 직접 옮기거나 제거한 뒤 다시 시도하세요 "
                    f"(비파괴 — 사용자 디렉토리를 자동 삭제하지 않습니다)."
                )
            backup: Path | None = None
            git_safe_skip = False
            # MF1: symlink 도 충돌이다(깨진 링크면 dst.exists() 가 False 라 별도 검사). 충돌
            #      symlink 는 run() 이 링크 자체를 백업하고 일반 파일로 교체한다(대상 불변).
            is_conflict = dst.exists() or dst.is_symlink()
            if backup_root is not None and is_conflict:
                # git 이 추적 중이고 미변경인 파일은 git 이 복원 가능 → 백업 생략(덮기만).
                #   그 외(미추적·dirty·비-git·git 판정불가=git_safe None)는 중앙 디렉토리에 백업.
                #   ⚠️ symlink 충돌은 git_safe 와 무관하게 항상 백업한다 — ls-files 가 symlink 를
                #   추적 중이어도 run() 이 백업하는 것은 *링크 자체*(대상 파일 복제 아님)이고,
                #   git-safe skip 으로 무백업 덮으면 사용자 symlink 구성이 무흔적 손실되기 때문.
                if (git_safe is not None
                        and not dst.is_symlink()
                        and rel.as_posix() in git_safe):
                    git_safe_skip = True  # 백업 None — git 이 복원.
                else:
                    backup = backup_root / rel  # 중앙 디렉토리·relpath 미러링.
                    # 백업 target 의 *전체* 조상 체인
                    #   (`.pm_import_backups/<DATE>/<rel-parents>`) 이 안전한지 plan 단계 검증 —
                    #   일부 조상이 일반 파일/symlink 면 apply 중 mkdir 실패로 부분 복사가 잔존한다.
                    #   dst 조상 가드와 동일 helper·캐시 재사용(중앙 백업 자리 점유까지 한 번에 포착).
                    _check_ancestor_safe(dest_root, backup, checked_ancestors)
            # 계획 시점 관측(`is_conflict`·symlink 여부)을 액션에 넘긴다 — 적용 시점 재관측은
            #   계획 뒤 생성·삭제를 조용히 다른 동작으로 바꾼다(`CopyAction.run` 참조).
            action = CopyAction(src, dst, backup, dest_root=dest_root,
                                existed=is_conflict, is_symlink=dst.is_symlink())
            action._git_safe_skip = git_safe_skip
            actions.append(action)
    return actions


def _same_bytes(a: Path, b: Path) -> bool:
    """두 파일의 바이트 내용이 동일한가 (중복 relpath 충돌 판정용)."""
    try:
        return _read_bytes_shared(a) == _read_bytes_shared(b)
    except OSError:
        return False


def _prepare_selected_manifest_union(template_roots: list[Path]) -> dict | None:
    """선택 template tree의 manifest 합집합을 읽기 전용으로 사전 검증한다.

    ``plan_copy``와 동일한 ``template_roots`` 순서를 단일 선택 트리로 소비한다. 경로/하네스별
    예외는 없고 각 트리의 manifest 선언을 합치므로, 향후 임의 조합도 같은 집합 의미를 그대로
    재사용할 수 있다. 복사 apply 전에 호출해 후순위 manifest 부재/파싱·병합 실패가 부분 설치를
    남기지 않게 한다. 단일 트리는 합집합 write가 불필요하므로 None을 반환한다.
    """
    manifest_paths = [
        root / ".project_manager" / "engine.manifest"
        for root in template_roots
    ]
    if len(manifest_paths) < 2:
        return None
    pu = _load_pm_update()
    if pu is None:
        raise RuntimeError("pm_update.py를 로드할 수 없어 선택 manifest 합집합을 만들 수 없습니다.")
    return pu.merge_manifest_sources(manifest_paths)


def _warn_selected_manifest_union_conflicts(merged: dict | None) -> None:
    """manifest 합집합의 실제 marker 충돌만 stderr에 표면화한다.

    서로 다른 경로를 선언한 정상 합집합은 ``conflicts``가 비어 완전 무소음이다. 같은 관리 경로의
    @render/@target-owned/@source 의미가 다를 때만 merge 결과가 넣은 경로를 소비한다.
    """
    conflicts = [] if merged is None else merged.get("conflicts", [])
    if not conflicts:
        return
    print(
        "경고: 선택 manifest 중복 경로의 marker/@source 불일치 — registry 정규 순서상 "
        f"첫 flavor를 우선함 ({len(conflicts)}건): {', '.join(conflicts)}",
        file=sys.stderr,
    )


def _selected_entry_doc_source_overrides(
    source_root: Path,
    selected_harnesses: tuple[str, ...] | list[str],
    weight: str = "full",
) -> dict[tuple[Path, Path], Path]:
    """공유 자동-load 진입 doc의 조합별 중립 source override를 만든다.

    정책은 ``NEUTRAL_SHARED_ENTRY_DOCS`` 선언만 소비한다. 그룹 전원이 선택됐을 때만 발화하므로
    단일 하네스의 full/lite 선택은 기존 동작을 보존한다.
    """
    selected = set(selected_harnesses)
    overrides: dict[tuple[Path, Path], Path] = {}
    for rel, (members, neutral_harness) in NEUTRAL_SHARED_ENTRY_DOCS.items():
        if not members <= selected:
            continue
        neutral_dirs = HARNESS_TEMPLATE_DIRS[neutral_harness]
        if len(neutral_dirs) != 1:
            raise RuntimeError(
                f"중립 진입문서 소유 하네스 {neutral_harness!r}가 단일 template tree가 아닙니다."
            )
        neutral_root = source_root / "templates" / neutral_dirs[0]
        neutral_source = neutral_root / rel
        if weight == "lite":
            lite_source = neutral_source.with_name(
                f"{neutral_source.stem}.lite{neutral_source.suffix}"
            )
            # lite 변종 부재는 선언된 full 호환 폴백이다. 현행 중립 트리에 변종이 없어 정상
            # 조합마다 경고하면 기존 무소음 계약을 깨므로, 실제 선택 source로만 관측 가능하게 둔다.
            if lite_source.is_file() and not lite_source.is_symlink():
                neutral_source = lite_source
        if not neutral_source.is_file() or neutral_source.is_symlink():
            raise FileNotFoundError(
                f"공존용 중립 진입문서 없음/비정상 파일: {neutral_source}"
            )
        for member in members:
            for dirname in HARNESS_TEMPLATE_DIRS[member]:
                overrides[(source_root / "templates" / dirname, rel)] = neutral_source
    return overrides


def _install_selected_manifest_union(merged: dict | None, dest_root: Path) -> int:
    """사전 검증된 선택 manifest 합집합을 설치한다(여기서는 source를 다시 읽지 않는다)."""
    if merged is None:
        return 0
    target = Path(dest_root) / ".project_manager" / "engine.manifest"
    target.write_text(merged["text"], encoding="utf-8", newline="\n")
    return len(merged["entries"])


# ── placeholder 치환 ───────────────────────────────────────────────────────

def _substitution_map(project_name: str, project_root: Path, today: str) -> dict[str, str]:
    return {
        "{{PROJECT_NAME}}": project_name,
        "{{PROJECT_TAGLINE}}": DEFAULT_TAGLINE,
        "{{PROJECT_ROOT}}": str(project_root),
        "{{PY}}": _detected_py(),
        "{{TEST_CMD}}": _default_test_cmd(),
        "{{DATE}}": today,
    }


def _is_engine_source(rel: Path) -> bool:
    """엔진 소스 코드(`.project_manager/tools/`)인가 — placeholder 처리에서 전면 제외 대상.

    엔진 도구(.py)는 verbatim canonical 사본이다: 코드는 런타임에 local.conf 에서 `project.name`·
    `runtime.py`·`test.cmd` 를 읽지, baked placeholder 를 쓰지 않는다. 그런데 그 *주석·docstring·예시 문자열*
    엔 `{{PROJECT_NAME}}`·`{{OPENCODE_PRO_MODEL}}`·`{{PROJECT_CONSTRAINTS}}` 같은 토큰이 문서로
    등장한다(엔진이 placeholder 메커니즘을 설명하므로). 이 문자열들은 *placeholder 가 아니라
    문서*다 — substitute/fill/token-scan 이 건드리면 (a) 주석이 concrete 값으로 변질 (b) free-form
    토큰에 `<!-- TODO -->` 주입 (c) `{{OPENCODE_PRO_MODEL}}` 이 claude 트리의 모델-해소 게이트를
    오발(_token_present True → resolve active). 따라서 엔진 소스는 placeholder 처리 전 범위에서
    제외해 verbatim 으로 둔다 (pm_render/pm_update 가 토큰을 문서화하며 표면화).

    rel 은 dest-rel(`​.project_manager/tools/board.py`) 또는 절대/소스 경로(dry-run plan 의
    `action.dst`/`action.src`) 둘 다 올 수 있어 substring 매칭으로 통일한다(`tools/` 트레일링
    슬래시로 `tools_backup` 등 오탐 방지)."""
    p = rel.as_posix()
    return p.startswith(".project_manager/tools/") or "/.project_manager/tools/" in p


def _is_engine_metadata(rel: Path) -> bool:
    """엔진 메타데이터(`.project_manager/engine.manifest`)인가 — 치환 제외 대상.

    _is_engine_source 와 같은 클래스(엔진 소유·토큰이 *설명*)이나 `.project_manager/tools/` prefix
    밖이라 별도 판정이다. rel 은 dest-rel 또는 절대/소스 경로 둘 다 올 수 있어 `_is_engine_source`
    와 같은 suffix 매칭으로 통일한다(경로 구분자 정규화)."""
    p = rel.as_posix()
    return any(p == m or p.endswith("/" + m) for m in ENGINE_METADATA_RELPATHS)


def _consumption_time_tokens(rel: Path) -> frozenset:
    """이 파일에서 **설치 시 치환을 하지 않을** 토큰 집합 (소비 시점 소유·`CONSUMPTION_TIME_TOKENS`).

    파일 전체를 제외하는 `_should_substitute` 와 축이 다르다 — 여기서 걸러도 같은 파일의 다른
    operational 토큰은 종전대로 설치 시 치환된다. rel 은 dest-rel 또는 절대/소스 경로 둘 다 올 수
    있어 `_is_engine_metadata` 와 같은 suffix 매칭으로 통일한다."""
    p = rel.as_posix()
    for owned_rel, tokens in CONSUMPTION_TIME_TOKENS.items():
        if p == owned_rel or p.endswith("/" + owned_rel):
            return tokens
    return frozenset()


def _should_substitute(rel: Path, exclude_relpaths: frozenset) -> bool:
    """이 파일이 operational placeholder 치환 대상인가 — **제외 사유가 없으면 대상**.

    옛 판정은 확장자 allowlist(`SUBSTITUTE_SUFFIXES`) gate 였다: 열린 집합(하니스가 계속 늘리는
    파일 형식)을 사람이 열거한 형상이라, codex 가 들여온 `.codex/agents/*.toml` 이 어느 채널도
    못 타고 `{{PROJECT_NAME}}` 을 리터럴로 출하했다. 판정을 뒤집어 **닫힌 집합(엔진 소유 자산)의
    제외 사유**만 본다 → 네 번째 하니스의 새 형식(`.yaml`·`.jsonc`)도 자동 편입된다.

    제외 사유(모듈 상단 주석과 동기):
      방법론 문서 — exclude_relpaths = dest 인스턴스 manifest 파생 치환-제외 집합
         (`_dest_sed_exclude`). 호출부가 치환 시점에 dest 기준으로 산출해 넘긴다(모듈-시점 상수
         아님).
      엔진 소스(`.project_manager/tools/**`) — verbatim.
      엔진 메타데이터(`engine.manifest`) — 주석이 토큰 메커니즘을 *설명*.
      텍스트로 못 읽는 파일 — 이 판정이 아니라 호출부 `read_text` 의 UnicodeDecodeError 가
         걸러낸다(파일 내용 없이는 판정 불가·현 구조 유지).

    범위(어떤 파일이 이 판정에 오는가)는 넓히지 않는다 — 호출부는 전부 `copied_relpaths`
    (이번 run 이 실제 복사한 파일)로 한정된다(`substitute_placeholders` docstring MF1 비파괴)."""
    if rel.as_posix() in exclude_relpaths:
        return False
    if _is_engine_source(rel):  # 엔진 소스(.py)는 verbatim — 주석의 토큰-문서는 placeholder 아님
        return False
    if _is_engine_metadata(rel):  # engine.manifest 주석의 토큰-문서도 placeholder 아님
        return False
    return True


def substitute_placeholders(
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
    root_identity: tuple | None = None,
) -> int:
    """**이번 run 이 복사한 파일만** 대상으로 operational placeholder 치환. 변경 파일 수 반환.

    apply 단계 전용 — 복사가 끝난 dest 트리를 in-place 수정한다.

    MF1: dest 트리 전체를 rglob 하면 이번 import 가 복사하지 *않은* 기존 사용자 파일까지
    무백업 치환되어 --into 비파괴 보장을 위반한다. 따라서 범위를 copied_relpaths(plan_copy
    가 만든 actions 의 dst relpath)로 엄격히 한정한다. 복사된 파일은 충돌 시 이미 백업됐으므로
    치환해도 안전하고, 복사 안 한 사용자 파일은 절대 건드리지 않는다.

    값이 빈 문자열(`""`/`None`)인 subs 는 치환하지 않는다 — `replace(token, "")` 로 토큰을
    silent 로 비우면(예: 빈 `project.name` → " 프로젝트") 미해소 탐지 신호가 사라진다(잔여 토큰보다
    나쁨). 토큰을 남기면 @render path 는 이후 render_managed_files 의 _assert_no_leak 가 leak 으로
    잡고(같은 subs 를 render 채널에도 넘겨 빈값 힌트까지 표면화), 비-@render path 는 리터럴 토큰이
    가시적으로 남아(침묵 비움 아님) 사람이 즉시 알아챈다. 이 함수는 render *이전* 단계라
    빈값이 render 가드 도달 전에 이미 지워졌다(codex must-fix — 최초 import 경로 사각).
    """
    changed = 0
    swapped: list[str] = []
    vanished: list[str] = []
    sed_exclude = _dest_sed_exclude(dest_root)  # 치환 시점·dest manifest 기준(codex must-fix)
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not _should_substitute(rel, sed_exclude):
            continue
        # 복사 직후라도 교체 창은 실재한다 — 선검사는 lstat, 실 IO 는 nofollow fd 로 한다.
        #   **선검사 탈락도 loud** 다: 이 범위는 방금 복사한 파일이라 부재·형상 변화가 곧 사고다.
        anomaly = _copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            swapped.append(rel.as_posix())
            continue
        try:
            text = read_dest_text(dest_root, rel, root_identity=root_identity)
        except UnsafeDestPathError:
            swapped.append(rel.as_posix())
            continue
        except FileNotFoundError:
            # 선검사↔읽기 사이 삭제 경쟁 — 쓰기 쪽과 **같은 loud 제외**로 흡수한다. 일반 OSError
            #   로 삼키면 요약에 안 실려 "적용 단계 제외는 stderr 요약이 신호" 라는 규칙이 이
            #   경로에서만 깨진다(제외는 하되 반드시 알린다).
            vanished.append(rel.as_posix())
            continue
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        # 이 파일에서 소비 시점이 소유한 토큰은 설치가 건드리지 않는다 — 굳히면
        #   manifest bare 등재 byte-copy 가 다음 sync 에 토큰-form 을 되돌려 진동한다.
        consumption_owned = _consumption_time_tokens(rel)
        for token, value in subs.items():
            if token in consumption_owned:
                continue
            if value is None or value == "":
                # 빈값 subs 는 치환하지 않는다 — 토큰을 그대로 남겨 이후 render 단계
                # (render_managed_files)의 _assert_no_leak 가 leak 으로 잡게 한다(silent-empty =
                # leak 클래스). `replace(token, "")` 는 미해소를 *침묵 비움*(예:
                # `{{PROJECT_NAME}}`→"" → description 이 " 프로젝트")으로 박제해 탐지 신호 자체를
                # 없앤다 — 잔여 토큰보다 더 나쁘다. subs 는 이후
                # render_managed_files 에도 그대로 전달돼 render 채널이 같은 빈값을 힌트로 표면화한다.
                continue
            if token in new_text:
                new_text = new_text.replace(token, value)
        if new_text != text:
            try:
                write_dest_text(dest_root, rel, new_text, root_identity=root_identity)
            except UnsafeDestPathError:
                swapped.append(rel.as_posix())
                continue
            except FileNotFoundError:
                vanished.append(rel.as_posix())
                continue
            changed += 1
    _report_copied_scope_anomalies("placeholder 치환", swapped, vanished)
    return changed


# ── render 단계 ─────────────────────────────────
# @render manifest path 의 어댑터 파일은 import 도 pm_update 와 같은 render 경로를 탄다 —
# 복사 후 operational 토큰(local.conf — PROJECT_NAME·TEST_CMD 등)을 render_adapter 로 치환한다
# (pm_render 공유).
# free-form 은 canonical home(root doc·
# pm_role.local.md)의 FILL 채널이 전담. substitute(operational) *이후* 에 둬 일관 처리.

def _load_pm_render_module():
    """pm_render 모듈을 같은 tools/ 디렉토리에서 로드 (board.py 로더 패턴 동형·sys.path 무오염).

    실패 시 None 반환 — **호출부마다 처리가 다르다**: `_mark_model_todos`(모델 토큰 중화는
    안전 출하의 load-bearing 계약)는 중화 불가를 조용히 넘기지 않고 **fail-loud**(raise) 하고,
    `render_managed_files`/@render 처리는 skip(검사 대상 0·무동작·render path 토큰 잔존 시 board
    render-leak lint 가 backstop). pm_render 는 co-located 엔진이라 정상 설치에선 항상 로드된다 —
    로드 실패는 broken install 신호다."""
    render_py = Path(__file__).resolve().parent / "pm_render.py"
    try:
        mod = _load_module_from_path(
            render_py, "pm_render.py", verifier=_verify_engine_rev,
        )
        return mod
    except Exception as exc:  # noqa: BLE001 — 일반 로드 실패만 render 단계 skip(무동작).
        if _is_engine_rev_skew(exc):
            raise
        return None


def unregistered_skill_notation_template_dirs(
    template_dirs: tuple[str, ...] | list[str],
) -> list[str]:
    """pm_render 스킬 표기 registry 에 값이 없는 template dir 목록(설치 전 게이트용).

    렌더러는 호출 토큰이 있는 문서를 미등록 하네스 context 로 만나면 `RenderLeakError` 로
    중단한다(알 수 없는 하네스를 조용히 `/` 로 복사하지 않는 fail-loud). 그런데 그 판정이
    **복사 뒤 렌더 단계**라, 새 하네스를 registry 에 등록하고 표기 값을 안 넣은 채 설치하면
    파일이 다 깔린 뒤 uncaught 로 터져 **부분 설치가 남았다**. 이 함수로 *복사 전에* 같은
    조건을 판정해 `--fill auto` 미매핑 하네스 게이트와 같은 성질(파일 설치 전 중단·traceback 0·
    설치 파일 0)을 `--fill manual` 경로에도 준다.

    pm_render 로드 실패는 빈 목록 — 렌더 단계 자체가 skip 되므로 게이트할 대상이 없다
    (`_load_pm_render_module` 의 render-skip 동작과 같은 경계).
    """
    render_mod = _load_pm_render_module()
    if render_mod is None:
        return []
    prefixes = getattr(render_mod, "SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR", {})
    labels = getattr(render_mod, "_HARNESS_LABEL_BY_TEMPLATE_DIR", {})
    return [
        dirname
        for dirname in dict.fromkeys(template_dirs)
        if dirname not in prefixes or dirname not in labels
    ]


def _unregistered_skill_notation_message(unregistered: list[str]) -> str:
    """미등록 표기 하네스 안내 — main/add-harness 가 같은 문구를 쓴다(단일 진실)."""
    return (
        f"스킬 호출 표기 값이 미등록인 template 디렉터리: {', '.join(unregistered)} — "
        "파일 설치 전 중단합니다. pm_render.SKILL_ENTRY_PREFIX_BY_TEMPLATE_DIR 와 "
        "_HARNESS_LABEL_BY_TEMPLATE_DIR 에 그 하네스의 실제 호출 표기를 등록한 뒤 다시 "
        "시도하세요(부분 설치를 남기지 않습니다)."
    )


def _render_managed_relpaths(dest_root: Path) -> set[str]:
    """복사된 트리의 engine.manifest 에서 `@render` path(repo 기준 relpath·POSIX) 집합.

    pm_update.read_manifest 를 재사용해 `.render` True 항목만 모은다. manifest 부재·로드 실패
    → 빈 set(render 대상 0·무동작). 디렉토리 path 는 하위 어댑터 산출물의 prefix 매칭에 쓴다."""
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    try:
        mod = _load_module_from_path(
            pm_update_py, "pm_update.py", allow_unverified=True,
        )
        return {
            str(e).replace("\\", "/")
            for e in mod.read_manifest(manifest)
            if getattr(e, "render", False)
        }
    except Exception:  # noqa: BLE001 — 로드/파싱 실패는 render 대상 0(무동작).
        return set()


def _is_render_managed(rel_posix: str, managed: set[str]) -> bool:
    """rel_posix 가 @render manifest path(파일 정확일치 OR 디렉토리 prefix) 하위인지."""
    for m in managed:
        if rel_posix == m or rel_posix.startswith(m.rstrip("/") + "/"):
            return True
    return False


def _engine_render_relpaths(root: Path) -> set[str]:
    """트리의 engine.manifest 에서 add_harness 가 *복사 제외*할 native-@render 엔진 리소스 relpath 집합.

    add_harness 스코프 제외용. `@render` 만 있고 `@target-owned`·
    `@source` 가 *둘 다 없는* path = 루트 upstream 에 manifest-경로 그대로 실재하는 native 엔진 리소스
    (예 `.claude/agents`·`.claude/skills`) — pm_update 소관이라 어댑터 추가 시 오적재하면 안 된다.
    반대로 어댑터-소유/전파 경로는 add_harness 의 *복사 대상*이라 이 집합에서 뺀다:
      - `@target-owned`(예 진짜 target-owned) — pm_update ManifestEntry.target_owned 판별자.
      - `@source=<path>`(예 `.opencode/agents`·`.opencode/command` — framework-owned·guest 하네스·
        canonical 소스가 `templates/opencode/.opencode/*`) — pm_update 는
        source-remap 으로 전파하나 add_harness 는 이 guest 어댑터 파일을 *레이다운*해야 한다
        manifest-경로 = 어댑터 네임스페이스라
        add_harness 소스(templates/opencode)에 실재 → 복사.
    manifest 부재·로드 실패 → 빈 set(무동작).
    """
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    manifest = root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    try:
        mod = _load_module_from_path(
            pm_update_py, "pm_update.py", allow_unverified=True,
        )
        return {
            str(e).replace("\\", "/")
            for e in mod.read_manifest(manifest)
            if getattr(e, "render", False)
            and not getattr(e, "target_owned", False)
            and not getattr(e, "source_rel", None)
        }
    except Exception:  # noqa: BLE001 — 로드/파싱 실패는 제외집합 0(무동작·전부 복사 대상).
        return set()


# manifest 미소유 출하 텍스트 중 **설치 하네스 전체**가 함께 읽는 표면 = 인스턴스 wiki.
#   engine.manifest 는 *upstream 이 관리하는* 경로만 담는다 — status·log·architecture·raw/README
#   같은 wiki seed 는 인스턴스 상태라 등재하면 pm_update 가 채택자 상태를 덮는다(엔진/상태 분리).
#   그래서 이 부류는 "등재 강제"가 아니라 **설치 하네스 전체 집합 폴백**으로 표기를 해소한다.
#   루트 진입 문서(AGENTS.md)는 독자가 하네스 부분집합이라 이 접두사에 넣지 않는다 — 호출부가
#   NEUTRAL_SHARED_ENTRY_DOCS 멤버십으로 좁힌 context 를 명시 전달한다.
INSTANCE_SHARED_WIKI_PREFIX = ".project_manager/wiki/"


def read_text_keeping_newlines(path: Path) -> str:
    """줄끝을 **번역하지 않고** 읽는다(`\\r\\n` 은 `\\r\\n` 그대로).

    기본 텍스트 모드는 universal-newlines 라 `read_text`→`write_text` 왕복만으로 CRLF 가 LF 로
    바뀐다. 렌더 범위가 *인스턴스 소유* 문서까지 넓어진 뒤에는 그게 표기와 무관한 바이트 변경
    (Windows 채택자 트리 전체 줄끝 뒤집기·git diff 오염)이므로 원본 줄끝을 보존한다."""
    with _open_shared(path, binary=False, encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_keeping_newlines(path: Path, text: str) -> None:
    """줄끝을 **번역하지 않고** 쓴다 — `read_text_keeping_newlines` 와 쌍(왕복 byte 보존)."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


# ── 개행 표기 보존 왕복 ───────────────────────────────────────────────────────
# 엔진이 자기 소유 파일을 **읽어서 다시 쓰는** 경로(engine.manifest guest 절 동기 등)는 본문을
# LF 로 다뤄야 판정이 단순한데, 그대로 되쓰면 CRLF 체크아웃 채택자의 파일 전체가 LF 로 뒤집힌다
# (engine.manifest 의 append-only byte 불변식 위반·손대지 않은 줄까지 전면 diff).
# 그래서 **판정은 LF 정규화 후, 쓰기는 원본 표기 복원**으로 두 축을 분리한다.
# 표기의 진실은 **그 파일의 현재 내용**이다 — 플랫폼(os.linesep)이 아니다. "Windows 면 CRLF" 로
# 분기하면 `core.autocrlf=false` 로 LF 체크아웃한 Windows 채택자를 반대로 깨뜨린다.
DEFAULT_TEXT_NEWLINE = "\n"


def dominant_newline(text: str, default: str = DEFAULT_TEXT_NEWLINE) -> str:
    """번역 없이 읽은 본문의 지배 개행 (`"\\r\\n"` 또는 `"\\n"`).

    다수결로 정하고 동수면 **첫 등장** 표기를 쓴다. 개행이 하나도 없으면 `default`.
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf == 0 and lf == 0:
        return default
    if crlf != lf:
        return "\r\n" if crlf > lf else "\n"
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def read_text_preserving_newline(path: Path) -> tuple[str, str]:
    """`(LF 정규화 본문, 지배 개행)` — 번역 없이 읽고 판정용 본문만 LF 로 접는다.

    쌍인 `write_text_preserving_newline` 에 그 지배 개행을 그대로 넘기면 우리가 바꾸지 않은
    줄의 bytes 가 보존된다(왕복 무변경)."""
    raw = read_text_keeping_newlines(path)
    return raw.replace("\r\n", "\n"), dominant_newline(raw)


def write_text_preserving_newline(path: Path, text: str, newline: str) -> None:
    """LF 본문을 `newline` 표기로 되돌려 쓴다 (파일 부재 등 미해소 표기는 LF).

    `text` 는 LF 정규화 본문이어야 한다 — 삽입하는 새 블록도 같은 표기로 렌더돼 혼재가 생기지
    않는다(호출부가 블록을 LF 로 만들어 붙이고 여기서 한 번에 변환)."""
    newline = newline or DEFAULT_TEXT_NEWLINE
    write_text_keeping_newlines(
        path, text if newline == "\n" else text.replace("\n", newline))


# ── 계획-적용 사이 TOCTOU 창 ─────────────────────────────────────────────────
# 경로 안전은 `_is_safe_dest_path` 가 **계획 시점**에 판정한다(symlink·조상 symlink·`..` 거부).
# 그 판정과 실제 쓰기 사이에 대상 경로가 symlink 로 교체되면 판정이 무력해지고, 경로로 다시 여는
# 쓰기가 링크를 따라 **저장소 밖 파일**을 고친다. 그래서 아래 **여섯 채널**의 실 IO 는 경로가 아니라
# **fd** 로 한다 — dest 루트 fd 에서 각 컴포넌트를 `O_NOFOLLOW` 로 열고 마지막 컴포넌트도
# `O_NOFOLLOW` 로 연다. 열린 fd 는 그 시점 inode 에 묶이므로 이후 경로가 바뀌어도 우리가 읽고 쓰는
# 대상은 변하지 않는다(창을 좁히는 게 아니라 없앤다).
#   (1) 복사(`CopyAction.run` — 디렉토리 생성 포함) (2) 중앙 백업 (3) 공유 문서 재렌더
#   (4) placeholder 치환 (5) opencode 모델 해소 (6) 자유서술 fill(TODO 표시·토큰 판정).
#   원본(template)은 신뢰 소스라 경로로 읽는다.
#   ⚠ **여기 없는 dest 쓰기는 여전히 경로 기반이다** — engine.manifest 병합/guest 절 갱신 ·
#     `.gitignore` 보강 · local.conf 채널(백업·키 기록) · pm_playbook.local 스텁 생성 ·
#     board submodule scaffold · `--new` 최초 `mkdir`. 이들은 **정적 형상에서 안전**하다(계획 대상이
#     아니거나·인스턴스 소유라 계획-적용 창 밖이거나·존재하면 덮지 않는다). 그러나 **동적 교체 창
#     자체는 남는다** — 이 티켓의 범위가 계획-적용 창(위 6채널)이라 그렇게 한정했고, 확장은 별도
#     판단거리다(조용한 축소가 아니라 명시된 경계).
#   ⚠ dest 루트 **자체** 교체는 파일 단위가 아니라 전체 중단 클래스다(`DestRootSwappedError`) —
#     고정한 루트 신원(`dest_root_identity`)을 매 열기에서 fd 로 대조한다.
#   ⚠ 보장하는 축은 **symlink** 다 — 저장소 안에 미리 만들어 둔 **하드링크**는 같은 inode 라 fd 열기로
#     구분되지 않으므로 "저장소 밖 파일 불변" 문구는 symlink·경로 탈출 축에 한정된다(범위 밖).
_DEST_FD_WALK_SUPPORTED = (
    hasattr(os, "O_NOFOLLOW")
    and hasattr(os, "O_DIRECTORY")
    and os.open in getattr(os, "supports_dir_fd", frozenset())
)
# 거부로 승격하는 errno 와 **그 errno 가 실제로 말하는 것** — ENOTDIR 을 symlink 교체로 단정하지
# 않는다(일반 파일 컴포넌트·구조 변경일 수도 있다). 판정은 같아도(거부) 진단 문구는 갈라 쓴다.
_DEST_PATH_REFUSAL_REASONS = {
    getattr(errno, name): reason
    for name, reason in (
        ("ELOOP", "symlink 교체 의심"),
        ("EMLINK", "symlink 교체 의심(일부 BSD 의 O_NOFOLLOW errno)"),
        ("ENOTDIR", "경로 컴포넌트가 디렉토리가 아님 — 교체·구조 변경 의심"),
        ("ENXIO", "FIFO·디바이스 등 비-일반 파일 — 교체 의심"),
    )
    if hasattr(errno, name)
}

# 비-일반 파일(FIFO·디바이스)에서 **열기 자체가 멈추는** 창을 닫는 플래그. `O_NOFOLLOW` 는 symlink
# 만 거른다 — 계획 통과 뒤 대상이 FIFO 로 바뀌면 `O_RDONLY` 열기가 writer 를 기다리며 무기한 블록해
# 설치가 그 자리에 선다(교체 축의 남은 파생: 유출이 아니라 정지). 일반 파일에는 읽기·쓰기 의미가
# 없으므로(POSIX) 내용 IO 채널에 상시 얹고, 연 fd 를 `fstat` 해 일반 파일이 아니면 거부한다 —
# 검사 대상이 **연 fd 자신**이라 재검사 창이 없다. 쓰기 쪽은 열기가 ENXIO 로 먼저 실패한다(위 표).
_DEST_NONBLOCK_FLAG = getattr(os, "O_NONBLOCK", 0)


def _raise_dest_path_refusal(rel: Path, exc: OSError) -> NoReturn:
    """열기 거부로 볼 OSError 는 `UnsafeDestPathError` 로 승격해 던지고, 그 밖은 원본 재-raise.

    호출부는 승격된 예외만 "계획 뒤 교체" 로 다루고(해당 파일 제외 + loud), 나머지 OSError 는
    기존 처리(권한·부재 등)를 그대로 탄다 — 진단을 뭉개지 않는다."""
    reason = _DEST_PATH_REFUSAL_REASONS.get(exc.errno)
    if reason is None:
        raise exc
    raise UnsafeDestPathError(
        f"계획 검증 뒤 경로를 안전하게 열 수 없습니다({reason}): {Path(rel).as_posix()} — "
        "저장소 밖 파일을 고치지 않기 위해 이 파일을 건너뜁니다.") from exc


def dest_root_identity(dest_root: Path) -> tuple:
    """dest 루트를 **한 번** 해소해 그 디렉토리의 `(st_dev, st_ino)` 신원을 고정한다.

    계획 시점(복사 전)에 잡아 적용까지 넘긴다. 이후 매 IO 는 자기가 **연 fd** 를 `fstat` 해 이
    신원과 대조하므로(검사 대상 = 사용할 fd 자신), 계획 뒤 dest 루트 자체가 다른 디렉토리·저장소
    밖 symlink 로 교체돼도 그 트리를 따라가지 않고 거부한다 — 루트 재해소가 조용히 다른 inode 를
    고르던 창이 닫힌다.

    획득 실패는 **fail-loud** 다(옛 None 폴백 폐기): None 을 돌려주면 루트 검사가 통째로 꺼지므로,
    "획득 순간만 해소 불가로 만들고 그 뒤 교체" 라는 우회가 성립한다. 루트를 못 잡으면 무엇도
    안전하게 열 수 없으니 **적용 전에** 중단한다(호출부가 rc1 로 번역)."""
    try:
        st = os.stat(Path(dest_root).resolve(strict=True))
    except OSError as exc:
        raise UnsafeDestPathError(
            f"dest 루트 신원을 확정할 수 없습니다: {dest_root} ({exc}) — 루트 교체 검사를 끄고 "
            "진행하지 않고 적용 전에 중단합니다.") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeDestPathError(f"dest 루트가 디렉토리가 아닙니다: {dest_root}")
    return (st.st_dev, st.st_ino)


def _open_dest_root_fd(dest_root: Path, root_identity: tuple | None) -> int:
    """dest 루트를 열고 **그 fd 자신**으로 고정 신원을 대조한다 — 실패는 전부 루트 교체 클래스.

    루트 해소·열기 실패(삭제·깨진 링크·일반 파일로 교체)를 `UnsafeDestPathError` 나 raw `OSError`
    로 흘리면 **파일 단위** 핸들러가 그것을 흡수해 실행이 계속되고, 남은 단계가 교체된 트리에
    쓴다(루트 교체는 파일 하나를 빼는 상황이 아니라 대상 트리가 통째로 바뀐 상황이다). 그래서
    고정 신원이 있는 실행에서는 그 계열을 전부 `DestRootSwappedError`(즉시 전체 중단)로 번역한다.
    고정이 없는 호출(질의성 읽기 등)은 대조 기준이 없으므로 옛 경로 오류를 유지한다."""
    try:
        fd = os.open(str(Path(dest_root).resolve(strict=True)),
                     os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if root_identity is not None:
            raise DestRootSwappedError(
                f"계획 검증 뒤 dest 루트를 열 수 없습니다: {dest_root} ({exc}) — 삭제·깨진 링크·"
                "비-디렉토리로 바뀐 형상이므로 즉시 전체 중단합니다.") from exc
        raise UnsafeDestPathError(f"dest 루트를 해소할 수 없습니다: {dest_root}") from exc
    if root_identity is None:
        return fd
    try:
        # 검사 대상이 **지금 연 fd 자신**이라 검사-사용 사이 창이 없다(경로 재검사와 다른 점).
        root_stat = os.fstat(fd)
        if (root_stat.st_dev, root_stat.st_ino) != root_identity:
            raise DestRootSwappedError(
                f"계획 검증 뒤 dest 루트가 다른 디렉토리로 바뀌었습니다: {dest_root} — "
                "저장소 밖 트리를 건드리지 않기 위해 즉시 중단합니다.")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dest_dir_nofollow(dest_root: Path, rel_dir: Path,
                            root_identity: tuple | None = None) -> int:
    """dest 하위 디렉토리를 **컴포넌트마다 symlink 를 거부하며** 열어 dir fd 를 반환한다.

    빈 rel(= dest 루트 자신)은 루트를 직접 연다. "부모 디렉토리 fd 를 잡고 그 안에서만 조작"
    해야 하는 채널(링크 자체 백업·링크 삭제)이 쓴다 — 그쪽은 내용 IO 가 아니라 디렉토리 엔트리
    조작이라 경로로 다시 열면 부모 교체 창이 그대로 남는다."""
    parts = Path(rel_dir).parts
    if not parts:
        return _open_dest_root_fd(dest_root, root_identity)
    return _open_dest_relative_nofollow(
        dest_root, Path(*parts), os.O_RDONLY | os.O_DIRECTORY,
        root_identity=root_identity)


def _open_dest_relative_nofollow(
        dest_root: Path, rel: Path, flags: int, mode: int = 0o666,
        create_parents: bool = False, root_identity: tuple | None = None,
        create_leaf_dir: bool = False, regular_only: bool = False) -> int:
    """`dest_root/rel` 을 **컴포넌트마다 symlink 를 거부하며** 열어 fd 를 반환한다.

    `create_parents` 는 부재 중간 디렉토리를 dir_fd 상대로 만든다(백업 target 용) — 경로 문자열
    `mkdir(parents=True)` 는 조상 symlink 를 따라가 저장소 밖에 쓰기 때문이다.

    `root_identity`(= `dest_root_identity()` 산출)를 주면 **루트 자체 교체**도 거부한다. 컴포넌트
    순회는 rel 안의 교체만 막으므로, 루트가 통째로 바뀌면 안전한 순회가 엉뚱한 트리에서 일어난다.

    `regular_only` 는 **파일 내용 IO 채널** 전용이다: `O_NONBLOCK` 을 얹어 FIFO·디바이스에서 열기가
    무기한 멈추는 것을 막고, 연 fd 를 `fstat` 해 일반 파일이 아니면 거부한다(디렉토리를 여는 호출은
    이 옵션을 쓰지 않는다). 판정 대상이 fd 자신이라 경로 재검사와 달리 검사-사용 창이 없다.

    dir_fd/`O_NOFOLLOW` 미지원 플랫폼(주로 Windows)은 **열기 직전 `_is_safe_dest_path` 재검사**로
    폴백한다 — 창을 syscall 하나로 좁히지만 구조적 폐쇄는 아니다. 그 플랫폼은 symlink 생성 자체가
    관리자/개발자 모드 권한이라 노출면이 다르며, 폴백임을 여기 명시해 둔다."""
    rel = Path(rel)
    parts = rel.parts
    # 절대경로·드라이브·anchor 는 `dir_fd` 를 **무시**하고 그대로 열린다
    #   (`os.open("/etc/hostname", dir_fd=…)` 실측) — 컴포넌트 순회를 통째로 우회하므로 입구에서
    #   거부한다. 현 호출부는 relpath 만 넘기지만 containment 는 호출부 규율이 아니라 여기서
    #   성립해야 한다. `drive`/`root` 는 Windows `C:x`·UNC·POSIX anchor 까지 한 식으로 덮는다.
    if (rel.is_absolute() or rel.drive or rel.root or not parts
            or any(part in ("", ".", "..") for part in parts)):
        raise UnsafeDestPathError(f"dest 상대경로가 안전하지 않습니다: {rel.as_posix()}")
    binary = getattr(os, "O_BINARY", 0)  # Windows 텍스트 모드 줄끝 번역 차단(줄끝 보존).
    if regular_only:
        flags |= _DEST_NONBLOCK_FLAG

    def _regular_or_refuse(fd: int) -> int:
        """연 fd 가 일반 파일인가 — 아니면 fd 를 닫고 거부한다(내용 IO 채널 공통 출구)."""
        if not regular_only:
            return fd
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafeDestPathError(
                    f"경로가 일반 파일이 아닙니다(FIFO·디바이스·교체 의심): {rel.as_posix()}")
        except BaseException:
            os.close(fd)
            raise
        return fd

    if not _DEST_FD_WALK_SUPPORTED:
        if not _is_safe_dest_path(dest_root, rel):
            raise UnsafeDestPathError(
                f"경로가 안전하지 않습니다(symlink·조상 symlink·repo 밖): {rel.as_posix()}")
        # **루트 관문은 고정 신원 유무와 무관하다.** fd 순회 플랫폼은 `_open_dest_root_fd` 가 매
        #   호출 루트를 strict 해소하므로(부재·깨진 링크·비-디렉토리 = `UnsafeDestPathError`),
        #   이 폴백만 관문을 건너뛰면 같은 상황에서 raw `FileNotFoundError` 가 새 나가 호출부의
        #   흡수 클래스가 플랫폼마다 갈린다(Windows 실측·`_is_safe_dest_path` 는 부재 경로를
        #   lexical 로 통과시킨다). 신원이 있으면 그 계열을 루트 교체 클래스로 승격하고, 없으면
        #   옛 경로 오류 클래스를 그대로 유지한다(질의성 호출 규약).
        try:
            current_identity = dest_root_identity(dest_root)
        except UnsafeDestPathError as exc:  # 루트 자체 이상.
            if root_identity is None:
                raise
            raise DestRootSwappedError(str(exc)) from exc
        if root_identity is not None and current_identity != root_identity:
            raise DestRootSwappedError(
                f"계획 검증 뒤 dest 루트가 다른 디렉토리로 바뀌었습니다: {dest_root}")
        target = Path(dest_root) / rel
        if create_parents:
            target.parent.mkdir(parents=True, exist_ok=True)
        return _regular_or_refuse(os.open(str(target), flags | binary, mode))
    # 루트 열기 + 신원 대조는 한 지점(`_open_dest_root_fd`)이 전담한다 — 해소 실패·비-디렉토리·
    #   불일치를 전부 **전체 중단 클래스**로 번역해, 파일 단위 핸들러가 루트 교체를 흡수하는 창을
    #   남기지 않는다.
    dir_flags = os.O_RDONLY | os.O_DIRECTORY
    current = _open_dest_root_fd(dest_root, root_identity)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, dir_flags | os.O_NOFOLLOW, dir_fd=current)
            except OSError as exc:
                if not (create_parents and isinstance(exc, FileNotFoundError)):
                    _raise_dest_path_refusal(rel, exc)
                try:
                    os.mkdir(part, dir_fd=current)
                except FileExistsError:
                    pass  # 경쟁 생성 — 아래 재-open 이 판정한다(symlink 로 선점됐으면 거부).
                try:
                    nxt = os.open(part, dir_flags | os.O_NOFOLLOW, dir_fd=current)
                except OSError as retry_exc:
                    # 부모 생성 경쟁도 같은 정규화를 탄다 — raw OSError 로 새 나가지 않는다.
                    _raise_dest_path_refusal(rel, retry_exc)
            os.close(current)
            current = nxt
        try:
            return _regular_or_refuse(
                os.open(parts[-1], flags | os.O_NOFOLLOW | binary, mode, dir_fd=current))
        except OSError as exc:
            if not (create_leaf_dir and isinstance(exc, FileNotFoundError)):
                _raise_dest_path_refusal(rel, exc)
            try:
                os.mkdir(parts[-1], dir_fd=current)
            except FileExistsError:
                pass  # 경쟁 생성 — 아래 재-open 이 판정한다(symlink 로 선점됐으면 거부).
            try:
                return _regular_or_refuse(os.open(
                    parts[-1], flags | os.O_NOFOLLOW | binary, mode, dir_fd=current))
            except OSError as retry_exc:
                _raise_dest_path_refusal(rel, retry_exc)
    finally:
        os.close(current)


def _fdopen_text(fd: int, mode: str, newline: str | None = ""):
    """fd 소유권을 넘긴 텍스트 핸들 — 실패 시 fd 를 흘리지 않는다.

    `newline=""` = 줄끝 미번역(재렌더 채널·왕복 byte 보존), `newline=None` = 읽기 전용
    universal newline(`Path.read_text` 와 동일 의미)."""
    try:
        return os.fdopen(fd, mode, encoding="utf-8", newline=newline)
    except BaseException:
        os.close(fd)
        raise


def _fdopen_binary(fd: int, mode: str):
    """fd 소유권을 넘긴 바이너리 핸들 — 실패 시 fd 를 흘리지 않는다(백업 스트리밍용)."""
    try:
        return os.fdopen(fd, mode)
    except BaseException:
        os.close(fd)
        raise


def read_dest_text_keeping_newlines(
        dest_root: Path, rel: Path, root_identity: tuple | None = None) -> str:
    """`read_text_keeping_newlines` 의 TOCTOU 안전 짝 — symlink 미추종 fd 로 읽는다."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_RDONLY, root_identity=root_identity,
                regular_only=True), "r") as handle:
        return handle.read()


def write_dest_text_keeping_newlines(
        dest_root: Path, rel: Path, text: str,
        root_identity: tuple | None = None) -> None:
    """`write_text_keeping_newlines` 의 TOCTOU 안전 짝 — symlink 미추종 fd 로 제자리 덮어쓴다.

    `O_CREAT` 를 주지 않는다: 제자리 편집 전용이라 대상이 사라졌으면(경쟁 삭제) 만들지 않고
    실패해야 한다."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_WRONLY | os.O_TRUNC,
                root_identity=root_identity, regular_only=True), "w", newline="") as handle:
        handle.write(text)


def read_dest_text(dest_root: Path, rel: Path, root_identity: tuple | None = None) -> str:
    """`Path.read_text(encoding="utf-8")` 의 TOCTOU 안전 짝 — symlink 미추종 fd + universal newline.

    복사분 채널(치환·모델 해소·fill)용이다. 줄끝 의미를 옛 경로 IO 와 **동일하게** 두고 여는
    방식만 바꾼다 — 여기서 줄끝 보존으로 바꾸면 `_mark_todos` 의 줄 단위 마커 삽입이 CRLF 를
    깨뜨린다(의도치 않은 동작 변경)."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_RDONLY, root_identity=root_identity,
                regular_only=True),
            "r", newline=None) as handle:
        return handle.read()


def write_dest_text(dest_root: Path, rel: Path, text: str,
                    root_identity: tuple | None = None) -> None:
    """LF 고정 텍스트 쓰기의 TOCTOU 안전 짝 — 제자리 덮어쓰기 전용(생성 안 함)."""
    with _fdopen_text(
            _open_dest_relative_nofollow(
                dest_root, rel, os.O_WRONLY | os.O_TRUNC, root_identity=root_identity,
                regular_only=True),
            "w", newline="\n") as handle:
        handle.write(text)


def _ensure_dest_dir_nofollow(dest_root: Path, rel_dir: Path,
                              root_identity: tuple | None = None) -> None:
    """`dest_root/rel_dir` 디렉토리 체인을 **컴포넌트별 `O_NOFOLLOW` 로** 만든다(경로 mkdir 금지).

    `mkdir(parents=True)` 는 조상 symlink 를 따라가 저장소 밖에 디렉토리를 만든다 — 복사 목적지와
    백업 자리 양쪽에서 그게 첫 외부 쓰기였다. 빈 rel(=dest 루트)은 무동작."""
    parts = Path(rel_dir).parts
    if not parts:
        return
    rel_path = Path(*parts)
    if not _DEST_FD_WALK_SUPPORTED:
        # 폴백(dir_fd 미지원·주로 Windows): 만들기 직전 재검사 — 창을 좁힐 뿐 구조적 폐쇄가 아니다.
        if not _is_safe_dest_path(dest_root, rel_path):
            raise UnsafeDestPathError(
                f"디렉토리 경로가 안전하지 않습니다(symlink·조상 symlink·repo 밖): "
                f"{rel_path.as_posix()}")
        assert_dest_root_unchanged(dest_root, root_identity)
        (Path(dest_root) / rel_path).mkdir(parents=True, exist_ok=True)
        return
    fd = _open_dest_relative_nofollow(
        dest_root, rel_path, os.O_RDONLY | os.O_DIRECTORY,
        create_parents=True, root_identity=root_identity, create_leaf_dir=True)
    os.close(fd)


def _stream_fd_into(src_fd: int, dst_handle) -> None:
    """열린 fd 의 내용을 처음부터 스트리밍해 핸들에 쓴다(경로 재열기 0·메모리 적재 0)."""
    os.lseek(src_fd, 0, os.SEEK_SET)
    chunk_size = 1 << 20
    while True:
        data = os.read(src_fd, chunk_size)
        if not data:
            return
        dst_handle.write(data)


def _dest_leaf_identity(dest_root: Path, rel: Path,
                        root_identity: tuple | None = None) -> tuple:
    """dest leaf 의 `(st_dev, st_ino, 파일 종류)` — **부모 fd 안 `lstat`** 이라 링크도 링크 자신을 잰다.

    링크는 fd 로 붙들 수 없다(열면 대상이 열린다). 그래서 백업 시점 신원을 재 두고 삭제 직전에
    재대조한다 — 그 사이 같은 자리가 다른 파일로 바뀌면 백업 없는 남의 파일을 지우게 되므로.

    **파일 종류(`S_IFMT`)를 신원에 포함**한다: 지우고 곧바로 만들면 커널이 같은 inode 번호를
    재사용할 수 있어 `(dev, ino)` 만으로는 교체를 못 가른다(실측). 종류가 바뀌는 교체(링크→일반
    파일 등)는 이걸로 확실히 걸린다. 같은 종류로 교체 + inode 재사용은 남는 잔여인데, 그 경우
    지워지는 것도 링크뿐이라 사용자 데이터 손실 클래스가 아니다(명시된 경계)."""
    rel = Path(rel)
    if not _DEST_FD_WALK_SUPPORTED:
        st = os.lstat(Path(dest_root) / rel)  # 폴백 — 창을 좁힐 뿐(다른 폴백과 동형).
        return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))
    dir_fd = _open_dest_dir_nofollow(dest_root, rel.parent, root_identity=root_identity)
    try:
        st = os.lstat(rel.name, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode))


def _unlink_dest_leaf_if_unchanged(dest_root: Path, rel: Path, leaf_identity: tuple | None,
                                   root_identity: tuple | None = None) -> None:
    """백업한 그 leaf 가 맞을 때만 지운다 — 신원이 다르면 파일 단위 제외로 올린다."""
    if leaf_identity is not None:
        current = _dest_leaf_identity(dest_root, rel, root_identity=root_identity)
        if current != leaf_identity:
            raise PlanStateChangedError(
                f"백업 뒤 대상이 다른 파일로 바뀌었습니다({Path(rel).as_posix()}) — 백업하지 "
                "않은 파일을 지우지 않기 위해 이 파일을 건너뜁니다.")
    _unlink_dest_relative_nofollow(dest_root, rel, root_identity=root_identity)


def _refresh_existing_dest_file(
        dest_root: Path, rel: Path, src: Path, *, backup_base_rel: Path | None,
        root_identity: tuple | None = None) -> None:
    """기존 일반 파일을 **fd 하나로** 백업하고 그 자리에서 덮어쓴다(백업↔쓰기 창 폐쇄).

    옛 흐름은 백업과 쓰기가 leaf 를 각각 다시 열어, 그 사이 같은 자리가 다른 파일로 교체되면
    **백업 없는 새 파일을 truncate** 했다. 여기서는 `O_RDWR` fd 하나를 계획이 본 inode 에 묶어
    백업 읽기와 덮어쓰기를 모두 처리하므로 그 창이 존재하지 않는다(재검사가 아니라 구조적 폐쇄).

    소스는 **dest 를 건드리기 전에** 연다 — 소스 열기가 실패하면 dest 는 자르지도 않은 원본 그대로다.
    dest 경쟁(ENOENT)은 `PlanStateChangedError` 로 정규화한다(파일 단위 제외)."""
    with _open_shared(src, binary=True) as src_handle:  # 소스 먼저 — 실패 시 dest 불변.
        src_stat = os.fstat(src_handle.fileno())
        try:
            leaf_fd = _open_dest_relative_nofollow(
                dest_root, rel, os.O_RDWR, root_identity=root_identity, regular_only=True)
        except (FileExistsError, FileNotFoundError) as exc:
            raise PlanStateChangedError(
                f"계획 뒤 대상 상태가 달라져 복사를 멈춥니다({Path(rel).as_posix()}: {exc}) — "
                "계획에 없던 처리를 하지 않기 위해 이 파일을 건너뜁니다.") from exc
        try:
            if backup_base_rel is not None:
                _backup_open_fd_nofollow(
                    dest_root, leaf_fd, backup_base_rel, root_identity=root_identity)
            os.ftruncate(leaf_fd, 0)
            os.lseek(leaf_fd, 0, os.SEEK_SET)
            while True:
                data = src_handle.read(1 << 20)
                if not data:
                    break
                os.write(leaf_fd, data)
            if hasattr(os, "fchmod"):
                os.fchmod(leaf_fd, stat.S_IMODE(src_stat.st_mode))
            if os.utime in getattr(os, "supports_fd", frozenset()):
                os.utime(leaf_fd, ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
            # 쓰기 자체는 fd(=계획이 본 inode)로 안전하게 끝났다. 다만 그 사이 **경로**가 다른
            #   파일로 바뀌었다면 우리가 쓴 내용은 그 자리에 보이지 않는다 — 성공으로 보고하면
            #   사람이 없는 결과를 믿는다. 손상은 0이지만 제외로 올려 요약에 싣는다.
            fd_stat = os.fstat(leaf_fd)
            current = _dest_leaf_identity(dest_root, rel, root_identity=root_identity)
            if current != (fd_stat.st_dev, fd_stat.st_ino, stat.S_IFMT(fd_stat.st_mode)):
                raise PlanStateChangedError(
                    f"쓰기 중 대상 경로가 다른 파일로 바뀌었습니다({Path(rel).as_posix()}) — "
                    "저장한 내용이 그 자리에 보이지 않으므로 이 파일을 제외로 보고합니다"
                    "(교체된 파일은 건드리지 않았습니다).")
        finally:
            os.close(leaf_fd)


def _write_dest_file_from_source_nofollow(
        dest_root: Path, rel: Path, src: Path, *, overwrite: bool,
        root_identity: tuple | None = None) -> None:
    """신뢰 소스 `src` 를 `dest_root/rel` 로 **fd 스트리밍** 복사한다(모드·타임스탬프 보존).

    `overwrite=False`(신규)는 `O_CREAT|O_EXCL` 로 만들어 계획 뒤 생긴 남의 파일을 덮지 않고,
    `True`(refresh)는 `O_WRONLY|O_TRUNC` 로 기존 파일만 덮는다(기존 일반 파일 갱신은 백업까지 한
    fd 로 묶는 `_refresh_existing_dest_file` 이 쓰므로, 이 경로의 `True` 는 직접 호출·테스트용이다).
    어느 쪽이든 `O_NOFOLLOW` 라 그 자리에 심어진 symlink 는 거부된다. `shutil.copy2` 가 보존하던
    실행 비트(출하 `.sh`)와 mtime 은 fd 기반으로 유지한다.

    **소스를 먼저 연다** — dest 를 `O_TRUNC` 로 연 뒤 소스 열기가 실패하면 dest 는 비워진 채 남고
    그 fd 는 주인이 없다(누수). 소스가 읽히는 것을 확인한 뒤에만 dest 를 건드린다.

    두 플래그 조합이 **계획 뒤 dest 경쟁**을 그대로 표면화한다: 신규인데 그 사이 생겼으면 EEXIST,
    기존인데 그 사이 지워졌으면 ENOENT 다. 둘 다 `PlanStateChangedError`(파일 단위 제외)로
    정규화한다 — 그대로 새면 적용 루프가 통째로 죽어 rc 정책이 깨진다. **소스(template) 쪽**
    오류는 정규화하지 않는다(진짜 누락 신호라 그대로 올린다·`src.open` 은 try 밖)."""
    flags = os.O_WRONLY | (os.O_TRUNC if overwrite else os.O_CREAT | os.O_EXCL)
    with _open_shared(src, binary=True) as src_handle:  # 소스 먼저 — 실패 시 dest 미접촉·fd 누수 0.
        src_stat = os.fstat(src_handle.fileno())
        try:
            # 디렉토리 체인 생성도 dest 조작이라 같은 정규화 안에 둔다 — 부모가 그 사이 생겼다
            #   사라지면 재-open 이 ENOENT 로 새어 적용 루프를 죽인다(인접 창).
            _ensure_dest_dir_nofollow(dest_root, Path(rel).parent, root_identity=root_identity)
            dst_fd = _open_dest_relative_nofollow(
                dest_root, rel, flags, stat.S_IMODE(src_stat.st_mode),
                root_identity=root_identity, regular_only=True)
        except (FileExistsError, FileNotFoundError) as exc:
            raise PlanStateChangedError(
                f"계획 뒤 대상 상태가 달라져 복사를 멈춥니다({Path(rel).as_posix()}: {exc}) — "
                "계획에 없던 처리를 하지 않기 위해 이 파일을 건너뜁니다.") from exc
        with _fdopen_binary(dst_fd, "wb") as dst_handle:
            shutil.copyfileobj(src_handle, dst_handle)
            dst_handle.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(dst_handle.fileno(), stat.S_IMODE(src_stat.st_mode))
            if os.utime in getattr(os, "supports_fd", frozenset()):
                os.utime(dst_handle.fileno(),
                         ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))


class CopyApplyOutcome(NamedTuple):
    """복사 plan 적용 결과 — 실제로 쓴 것과 **건드리지 않은 것**을 사유별로 가른다."""

    copied: list[Path]    # 실제로 복사된 dest relpath(후속 채널의 범위 = 이것뿐).
    refused: list[str]    # 계획 뒤 경로 교체(symlink·비-디렉토리 컴포넌트).
    changed: list[str]    # 계획 뒤 대상 상태 변화(신규 생성·삭제).


def apply_copy_plan(plan: list, dest_root: Path,
                    root_identity: tuple | None = None) -> CopyApplyOutcome:
    """복사 plan 을 적용하고 **파일 단위 제외**를 사유별로 돌려준다.

    적용 단계의 파일 단위 위반(경로 교체·계획 뒤 상태 변화)은 그 파일만 빼고 계속한다 — 모듈
    docstring 이 명문화한 rc 정책(적용 단계 제외 = rc 0 + stderr 요약)과 같은 규칙이다. 여기서
    전체를 중단하면 이미 복사된 파일이 남아 오히려 부분 설치가 된다.

    반환 `copied` 는 **성공한 액션만** 담는다 — 후속 채널(치환·모델 해소·fill·설치 기록)의 범위가
    곧 이 집합이라, 제외된 파일이 섞이면 없는 파일을 처리 대상으로 세거나 교체된 경로를 범위로
    들인다. dest 루트 교체(`DestRootSwappedError`)만 예외로 그대로 올라간다(전체 중단 클래스)."""
    copied: list[Path] = []
    refused: list[str] = []
    changed: list[str] = []
    for action in plan:
        rel = action.dst.relative_to(dest_root)
        try:
            action.run(root_identity=root_identity)
        except UnsafeDestPathError:
            refused.append(rel.as_posix())
            continue
        except PlanStateChangedError:
            changed.append(rel.as_posix())
            continue
        copied.append(rel)
    return CopyApplyOutcome(copied, refused, changed)


def report_copy_apply_anomalies(outcome: CopyApplyOutcome) -> None:
    """복사 단계 파일 단위 제외를 loud 로 알린다(조용한 degrade 금지)."""
    if outcome.refused:
        print(f"  ⚠️ 복사 대상 {len(outcome.refused)}건: 계획 검증 뒤 경로를 안전하게 열 수 없어"
              f"(symlink 교체·비-디렉토리 컴포넌트) 복사를 건너뜁니다(저장소 밖 파일 불변): "
              f"{', '.join(outcome.refused)}", file=sys.stderr)
    if outcome.changed:
        print(f"  ⚠️ 복사 대상 {len(outcome.changed)}건: 계획 뒤 대상 상태가 달라져(그 사이 생성·"
              f"삭제) 복사를 건너뜁니다(무백업 덮기·삭제분 재생성 금지): "
              f"{', '.join(outcome.changed)}", file=sys.stderr)


def _copied_scope_anomaly(dest_root: Path, rel: Path) -> str | None:
    """복사분 채널의 선검사 탈락 사유 — `'vanished'`·`'swapped'`·`None`(정상 대상).

    이 채널들의 범위는 **이번 run 이 방금 복사한 파일**이라, 대상이 없다는 것은 "원래 대상이 아님"
    이 아니라 복사 뒤 삭제다. 조용히 건너뛰면 그 사실이 어디에도 안 남는다(적용 단계의 유일한
    신호가 stderr 요약이므로). 부재는 `vanished`, 실재하는데 편집 대상이 아니면(디렉토리·FIFO 등
    형상 변화) `swapped` 로 갈라 호출부가 같은 요약 채널에 싣는다."""
    if _is_inplace_edit_candidate(dest_root, rel):
        return None
    return "swapped" if os.path.lexists(Path(dest_root) / rel) else "vanished"


def _report_copied_scope_anomalies(
        channel: str, swapped: list[str], vanished: list[str]) -> None:
    """복사분 채널의 파일 단위 제외를 loud 로 알린다(조용한 degrade 금지·채널명 포함).

    루트 교체(`DestRootSwappedError`)는 여기 오지 않는다 — 그건 흡수 대상이 아니라 전체 중단이다."""
    if swapped:
        print(f"  ⚠️ {channel} 대상 {len(swapped)}건: 복사 뒤 경로를 안전하게 열 수 없어(symlink "
              f"교체·비-디렉토리 컴포넌트) 처리를 건너뜁니다(저장소 밖 파일 불변): "
              f"{', '.join(swapped)}", file=sys.stderr)
    if vanished:
        print(f"  ⚠️ {channel} 대상 {len(vanished)}건: 복사 뒤 대상이 사라져(경쟁 삭제) 처리를 "
              f"건너뜁니다(새로 만들지 않습니다): {', '.join(vanished)}", file=sys.stderr)


def assert_dest_root_unchanged(dest_root: Path, root_identity: tuple | None) -> None:
    """고정한 루트 신원을 **외부 단계 직전에** 재확인한다(board init·실 하니스 fill 등).

    그 단계들은 subprocess/외부 프로세스라 우리 fd 를 물려줄 수 없다 — 구조적 폐쇄가 불가능한
    구간이므로 직전 재확인으로 창을 좁힌다(검사-사용 사이 gap 은 남는다·명시). 불일치는 파일
    단위가 아니라 **전체 중단** 클래스다."""
    if root_identity is None:
        return
    try:
        current = dest_root_identity(dest_root)
    except UnsafeDestPathError as exc:
        raise DestRootSwappedError(str(exc)) from exc
    if current != root_identity:
        raise DestRootSwappedError(
            f"dest 루트가 실행 중에 다른 디렉토리로 바뀌었습니다: {dest_root} — 남은 단계가 "
            "교체된 트리에 쓰지 않도록 즉시 중단합니다.")


def _is_inplace_edit_candidate(dest_root: Path, rel: Path) -> bool:
    """제자리 편집(백업·재렌더·복사분 채널) 선검사 — **링크를 따라가지 않는다**(`lstat`).

    옛 `is_file()` 선검사는 링크를 따라가므로 **깨진 symlink** 로 교체된 대상을 False 로 보고
    조용히 건너뛰었다 — 요구된 "파일 단위 loud 제외" 가 그 경로에서만 침묵으로 바뀐다. 그래서
    여기서는 symlink 를 **후보로 통과**시키고, 실제 판정(거부 + loud)은 fd 가드 한 지점에 맡긴다.

    `lstat` 실패도 errno 로 가른다 — 삼키면 같은 침묵이 조상 축으로 되살아난다:
      - `ENOENT` + **조상에 symlink 없음** = 진짜 부재 → 조용한 제외(원래 대상이 아니다).
      - `ENOENT` + **조상이 symlink**(깨진 링크로 교체) 또는 `ELOOP`/`ENOTDIR` 류 = 안전 거부
        대상 → 후보로 통과시켜 fd 가드가 loud 로 거른다.
    조상 순회는 dest 루트 **하위**로 한정한다(루트 자신이 symlink 를 거쳐 있는 정상 형상 —
    예 `/tmp`→`/private/tmp` — 을 매 부재 파일마다 거부로 오판하지 않기 위해)."""
    try:
        st = os.lstat(Path(dest_root) / rel)
    except OSError as exc:
        if exc.errno in _DEST_PATH_REFUSAL_REASONS:
            return True
        return isinstance(exc, FileNotFoundError) and _has_symlink_ancestor(dest_root, rel)
    return stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)


def _has_symlink_ancestor(dest_root: Path, rel: Path) -> bool:
    """`rel` 의 조상 컴포넌트(dest 루트 하위)에 symlink 가 있는가 — 부재 사유 판별용."""
    current = Path(dest_root)
    for part in Path(rel).parts[:-1]:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except OSError:
            return False  # 조상 자체가 부재 — 진짜 부재 경로다.
    return False


def _is_notation_fallback_scope(rel_posix: str) -> bool:
    """manifest 소유자가 없을 때 **설치 하네스 전체 표기**로 폴백해도 되는 relpath 인가.

    폴백 대상 = 인스턴스 wiki 하위 전부(설치 하네스 전원이 같은 물리 문서를 읽는 표면).
    엔진 backbone(`tools/**`)·어댑터 네임스페이스·루트 진입 문서는 독자가 특정 하네스라
    여기 들어오지 않는다(전체 집합으로 렌더하면 오히려 오표기가 된다)."""
    return rel_posix.startswith(INSTANCE_SHARED_WIKI_PREFIX)


def render_managed_files(
    dest_root: Path,
    subs: dict[str, str],
    copied_relpaths: set[Path],
    entry_notation_templates: dict[str, tuple[str, ...]] | None = None,
    installed_notation_context: tuple[str, ...] | None = None,
    root_identity: tuple | None = None,
) -> int:
    """이번 run 이 복사한 @render path 파일을 render_adapter 산출물로 다시 쓴다. 변경 수 반환.

    범위 = copied_relpaths(비파괴·substitute_placeholders 와 동일 보장). 대상 판정은 **@render
    manifest 선언(`_is_render_managed`) 단독**이다 — 옛 `.md` 확장자 하드 필터는 제거했다.
    확장자 조건은 manifest 선언을 덮는 중복·모순 판정이었고, `.codex/agents @render`(TOML)처럼
    선언은 맞는데 코드가 안 따라가는 형상을 만들었다. 텍스트로 못 읽는 파일은 아래 read_text 의
    UnicodeDecodeError 가 걸러낸다(byte-copy 그대로 남음).

    operational 은 이번 import 의 subs(이미 substitute 가 리터럴로 박았으므로 보통 no-op).
    free-form 은 pm_import 의 FILL 채널이 canonical home 에서 전담하므로 render-overlay 가
    관여하지 않는다.

    installed_notation_context: **이 인스턴스를 읽는 하네스 전체**의 template dir(registry
    순서·dest 실설치 ∪ 이번 선택). manifest 어느 엔트리도 소유하지 않는 인스턴스 wiki 문서
    (`_is_notation_fallback_scope`)의 표기를 이 집합으로 폴백 렌더한다 — 옛 동작은 소유자 부재를
    `continue` 로 **조용히 건너뛰어** 다중 하네스 설치에서 canonical slash 가 그대로 출하됐다
    (예 `wiki/raw/README.md` 의 `/spike-new`). 이번 run 의 선택만 넘기면 `--into` 로 하네스를
    덧붙일 때 기존 독자가 빠져 반대 방향 오표기가 되므로 호출부가 `installed_harnesses` 로 합집합을
    만든다. 폴백이 실제로 문서를 바꾸면 stderr 로 표기한다(조용한 degrade 금지). None 이면 폴백
    없음(호출자 미배선).

    subs(중괄호 포함 token→value)를 pm_render 의 bare-key operational dict 로 변환해 넘긴다."""
    managed = _render_managed_relpaths(dest_root)
    notation_contexts = entry_notation_templates or {}
    fallback_context = tuple(installed_notation_context or ())
    if not managed and not notation_contexts and not fallback_context:
        return 0
    render_mod = _load_pm_render_module()
    if render_mod is None:
        return 0
    # subs 는 `{{KEY}}`→value — pm_render 는 bare KEY 를 기대하므로 변환.
    operational = {
        token.strip("{}"): value for token, value in subs.items()
    }
    changed = 0

    def notation_context(rel_posix: str) -> tuple[str, ...] | None:
        matches = [
            (len(owner.rstrip("/")), context)
            for owner, context in notation_contexts.items()
            if rel_posix == owner.rstrip("/")
            or rel_posix.startswith(owner.rstrip("/") + "/")
        ]
        return max(matches, default=(0, None), key=lambda item: item[0])[1]

    fallback_rendered: list[str] = []
    # 계획 검증 뒤 경로가 교체된 대상 — 그 파일만 빼고 크게 알린다(아래 loud).
    swapped: list[str] = []
    # 읽은 뒤 쓰기 전에 대상이 사라진 경우(경쟁 삭제) — 같은 파일 단위 제외이되 사유가 다르다.
    vanished: list[str] = []
    for rel in sorted(copied_relpaths):
        rel_posix = rel.as_posix()
        file_render = _is_render_managed(rel_posix, managed)
        context = notation_context(rel_posix)
        notation_managed = (
            file_render
            or rel_posix.startswith(INSTANCE_SHARED_WIKI_PREFIX)
            or rel_posix == "AGENTS.md"
        )
        # manifest 소유자 부재 = 인스턴스 소유 wiki seed. 조용히 건너뛰지 않고 설치 하네스
        #   전체 집합으로 폴백한다(단일 하네스면 그 하네스 표기·다중이면 병기).
        used_fallback = False
        if (
            context is None
            and not file_render
            and fallback_context
            and _is_notation_fallback_scope(rel_posix)
        ):
            context = fallback_context
            used_fallback = True
        if not file_render and not (context and notation_managed):
            continue
        # 선검사는 `lstat` 이다 — `is_file()` 은 링크를 따라가 **깨진 symlink** 로 교체된 대상을
        #   조용히 건너뛰었다(loud 제외가 그 경로에서만 침묵으로 바뀜). 이 범위(복사분 + 계획이
        #   실재를 확인한 재렌더 대상)에서 부재·형상 변화는 사고이므로 요약에 싣는다.
        anomaly = _copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel_posix)
            continue
        if anomaly == "swapped":
            swapped.append(rel_posix)
            continue
        # 읽기·쓰기 모두 **경로가 아니라 fd** 로 한다 — 계획(`_is_safe_dest_path`)과 이 쓰기
        #   사이에 대상이 symlink 로 교체되면 경로 재열기는 링크를 따라 저장소 밖을 고친다.
        try:
            text = read_dest_text_keeping_newlines(
                dest_root, rel, root_identity=root_identity)
        except UnsafeDestPathError:
            swapped.append(rel_posix)
            continue
        except FileNotFoundError:
            vanished.append(rel_posix)  # 읽기 전 삭제도 loud 제외(아래 쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        rendered = (
            render_mod.render_adapter(
                text,
                operational=operational,
                template_dir=context,
            )
            if file_render
            else render_mod.render_skill_entry_notation(text, context)
        )
        if rendered != text:
            try:
                write_dest_text_keeping_newlines(
                    dest_root, rel, rendered, root_identity=root_identity)
            except UnsafeDestPathError:
                swapped.append(rel_posix)
                continue
            except FileNotFoundError:
                # 읽기와 쓰기 사이 삭제 경쟁. `O_CREAT` 를 안 주므로 되살리지 않고, 적용 단계라
                #   전체 중단도 하지 않는다 — 교체와 같은 파일 단위 loud 제외로 흡수한다
                #   (uncaught 로 터지면 "복사 전에만 던진다 → 부분 적용 0" 불변식이 깨진다).
                vanished.append(rel_posix)
                continue
            changed += 1
            if used_fallback:
                fallback_rendered.append(rel_posix)
    if fallback_rendered:
        print(
            f"  ⚠️ manifest 미소유 인스턴스 wiki {len(fallback_rendered)}건 — 설치 하네스 전체"
            f"({', '.join(fallback_context)}) 표기로 폴백 렌더: {', '.join(fallback_rendered)}",
            file=sys.stderr,
        )
    if swapped:
        print(
            f"  ⚠️ 렌더 대상 {len(swapped)}건: 계획 검증 뒤 경로를 안전하게 열 수 없어(symlink "
            f"교체·비-디렉토리 컴포넌트) 렌더를 건너뜁니다(저장소 밖 파일 불변·비파괴): "
            f"{', '.join(swapped)}. 경로를 정리한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
    if vanished:
        print(
            f"  ⚠️ 렌더 대상 {len(vanished)}건: 계획 검증 뒤 대상이 사라져(경쟁 삭제) 렌더를 "
            f"건너뜁니다(새로 만들지 않습니다): {', '.join(vanished)}.",
            file=sys.stderr,
        )
    return changed


# ── board.py init 호출 ─────────────────────────────────────────────────────

def run_board_init(dest_root: Path) -> int:
    """복사된 트리의 셋업(`pm-config init`)을 인자 없이 호출 — areas repo 행·local.conf·
    pm_state·pre-push 훅 + 이 홈의 첫 슬롯 행 등록.

    인자 0 호출은 그대로 성립한다 — init 은 `--prefix` 없이도 이 clone 의 repo 행을 등록하고
    (prefix 칼럼은 빈 채 = 무prefix `T-NNNN` 카테고리), 그 등록에는 `--area`·사용자 승인이
    필요 없다(신규 카테고리 신설이 아니다).

    같은 인터프리터(sys.executable)로 호출 — board.py 는 pyyaml 의존이라 venv 보존 필요.
    비대화형(stdin 비-tty)이면 external_review opt-in 은 board.py 가 안전쪽(OFF)으로 건너뛴다.
    stdin=DEVNULL 의 isatty() 가 Windows 서 신뢰불가라, env 로
    `PM_NONINTERACTIVE=1` 을 명시 전달해 결정적으로 skip 시킨다 (isatty 폴백 보조).

    `PYTHONDONTWRITEBYTECODE=1` 도 함께 전달한다— board.py 는 이제 같은
    tools/ 디렉토리의 `identity_args.py` 를 `importlib.util.spec_from_file_location` 으로
    동적 로드하는데, 이 실행이 갓 복사된 새 프로젝트 트리 안(`dest_root`)에서 일어나므로 표준
    바이트코드 캐싱이 `dest_root/.project_manager/tools/__pycache__/` 를 새로 만든다 — fresh
    import 직후 빌드 산출물이 섞이는 것을 막는다(`test_import_excludes_pycache`).
    """
    tools = dest_root / ".project_manager" / "tools"
    entry = tools / "pm_config.py"
    if not entry.exists():
        # 셋업 진입은 pm_config 가 소유한다(board init 위임 + 홈 슬롯 등록). 그 파일이 없으면
        # 복사가 반쪽이라는 뜻이라 여기서 멈춘다 — board 로 우회하면 등록이 조용히 빠진다.
        print(f"경고: pm_config.py 없음 ({entry}) — init 건너뜀.", file=sys.stderr)
        return 1
    result = subprocess.run(
        [sys.executable, str(entry), "init"],
        cwd=str(dest_root),
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PM_NONINTERACTIVE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode


def _set_conf_keys(text: str, updates: dict[str, str]) -> str:
    """local.conf 텍스트에서 지정 키를 유일한 한 줄로 정규화한다.

    첫 등장 자리에 `key=value`를 기록하고 뒤의 활성 중복은 제거한다. 없으면 끝에
    추가하며, 갱신하지 않는 줄·주석은 그대로 보존한다. reader들이 모두 last-wins이므로
    갱신 뒤 옛 중복이 실효값을 되돌리지 않게 하는 일반 set-or-replace 규칙이다.
    """
    remaining = dict(updates)
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                if key in remaining:
                    newline = "\n" if line.endswith("\n") else ""
                    out.append(f"{key}={remaining.pop(key)}{newline}")
                continue
        out.append(line)
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] = out[-1] + "\n"
        for key, value in updates.items():
            if key in remaining:
                out.append(f"{key}={value}\n")
    return "".join(out)


def _local_conf_has_blocking_legacy(dest_root: Path) -> bool:
    """dest 의 local.conf 에 **값 공급을 잃는** 구표기 키가 있는가 (안내 전용 키는 제외).

    판정 기준은 공용 로더 하나다(`local_conf.blocking_legacy`) — import 가 자기 목록을 들면
    소비 지점과 갈린다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return False
    module = _load_local_conf()
    return bool(module.blocking_legacy(module.load(local_conf).legacy))


def print_conf_migration_notice(dest_root: Path) -> None:
    """local.conf 표기 통일 교체 안내 — import 도 채택자가 이 사실을 만나는 진입이다.

    안내만 낸다(자동 이관 없음·엔진은 채택자 conf 를 대신 고쳐 쓰지 않는다). 문구의 단일 진실은
    공용 로더(`local_conf.migration_notice`)이고 pm_update 사본과 같은 문장을 쓴다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return
    module = _load_local_conf()
    for line in module.migration_notice(module.load(local_conf)):
        print(f"  {line}")


def sync_local_conf(dest_root: Path, project_name: str) -> bool:
    """board.py init 직후 local.conf 의 operational 해소값을 pm_import 치환값과 일치시킨다.

    board.py init 은 `project.name` 빈값·`test.cmd=pytest -q` 를 하드코딩하므로(seam
    불완전), 엔진 문서(local.conf 해소)와 CLAUDE.md(sed 치환)가 *다른 값*을 보게 된다.
    `project.name`·`test.cmd`·`runtime.py` 3개 키만 키 단위 갱신해 정렬한다. 나머지 키
    (ctx 예산·additional_reviewer 등)와 주석은 보존. clobber 금지. 파일 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — operational 값 동기화 건너뜀.",
              file=sys.stderr)
        return False
    updates = {
        "project.name": project_name,
        "test.cmd": _default_test_cmd(),
        "runtime.py": _detected_py(),
    }
    return _write_conf_keys(local_conf, updates)


def _load_local_conf():
    """공용 local.conf 로더(`local_conf.py`)를 같은 tools/ 에서 경로 로드한다 (board 사본 동형)."""
    return _load_module_from_path(
        Path(__file__).resolve().parent / "local_conf.py", "local_conf.py",
        verifier=_verify_engine_rev, cache=True,
        cache_key=f"_project_manager_local_conf:{Path(__file__).resolve().parent}",
    )


def _parse_conf_keys(text: str) -> dict[str, str]:
    """local.conf 텍스트 → key=value dict (공용 로더 `local_conf.parse` · **판정 없음**).

    pm_import 는 도입/복구 채널이라 **구표기 conf 도 읽어야** 이주 안내를 낼 수 있다 — 판정은
    값을 소비하는 지점(`local_conf.assert_values_no_legacy`)이 명시한다.
    """
    return _load_local_conf().parse(text)


def _load_file_lock():
    """공용 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 로드 (pm_update 사본과 동형).

    conf 키 writer 의 임계 구간을 여는 데만 쓴다. import 시점에 바인딩하지 않는 이유는 pm_import
    가 **복구/도입 채널**이기 때문이다 — 엔진 사본이 부분적으로 깨진 트리에서도 `pm-config
    upstream set` 같은 키 갱신은 떠야 하고, 형제 로드 실패로 죽으면 자기 자신을 못 고친다.
    로드 실패는 호출부가 fail-soft 로 받는다(무락 진행 — 프로세스 간 배타성만 잃는다).
    """
    lock_py = Path(__file__).resolve().parent / "file_lock.py"
    return _load_module_from_path(
        lock_py, "file_lock.py", verifier=_verify_engine_rev, cache=True,
        cache_key=f"_project_manager_file_lock:{lock_py}",
    )


# ── 공유 읽기 (등재 예외 · 복구/도입 채널의 판독) ───────────────────────────
# 원자 교체 대상을 읽는 지점은 공용 seam 을 지난다([[T-0729]]) — 일반 `open` 리더가 하나라도
# 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막는다. 다만 pm_import 는 **복구/도입 채널**
# 이라 형제 사본이 구세대·손상인 트리에서도 conf 판독과 키 기록이 성립해야 한다
# (`_atomic_replace_conf` 와 **같은 등재 항목의 판독 쪽**이다 — `_write_conf_keys_locked` 의
# 읽기→교체→검증이 한 구간이라 둘을 다르게 다루면 그 구간이 반쪽만 강등된다).
#
# 다만 **skew 정책은 쓰기와 다르고, 그 차이가 의도적이다**. 쓰기(`_atomic_replace_conf`)는 rev 가
# 갈린 사본이 상태를 *커밋*하는 것이라 marked skew 를 재동기 안내로 올린다. 판독은 아무것도
# 커밋하지 않고 종전 읽기와 **바이트가 같다** — 여기서 올리면 업그레이드의 정상 형상(구세대
# pm_import 사본을 pm_update 가 형제로 로드해 읽는 경로)에서 판독마다 skew 가 터져 동기 자신이
# 막힌다. 그래서 판독은 등록 사유로 흡수하고 원인을 문구로 구분해 남긴다.

# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "shared_read_seam": (
        "판독은 아무것도 커밋하지 않고 종전 읽기와 바이트가 같다 — 여기서 marked skew 를 올리면 "
        "구세대 pm_import 사본을 형제로 로드해 읽는 업그레이드 정상 경로에서 판독마다 터져 "
        "pm-update 자신이 막힌다(쓰기 `_atomic_replace_conf` 는 상태를 커밋하므로 종전대로 "
        "올린다). 흡수하되 조용하지 않게 사유를 stderr 로 남기고 종전 읽기로 진행한다"
    ),
}


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """판독 경계가 marked skew 를 의도적으로 흡수했음을 표시한다 (사유 등록 필수).

    반환값으로 일반 실패와 사본 불일치를 구분해 호출부가 진단 문구를 달리한다 — 흡수는 하되
    조용하지는 않다."""
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    return _is_engine_rev_skew(exc)


# 강등 사유는 프로세스당 한 번만 알린다 — 판독마다 찍으면 진단이 자기 소음에 묻힌다.
_shared_read_degraded = False


def _warn_shared_read_degraded(cause: str) -> None:
    """강등 사유를 **프로세스당 한 번** 알린다 (판독마다 찍으면 진단이 자기 소음에 묻힌다)."""
    global _shared_read_degraded
    if _shared_read_degraded:
        return
    _shared_read_degraded = True
    print(
        f"경고: 공유 읽기 seam 을 쓸 수 없어 일반 읽기로 진행합니다 ({cause}) — Windows "
        "에서는 이 판독이 열려 있는 동안 원자 교체가 실패할 수 있습니다. `pm-update` 로 "
        ".project_manager/tools/ 전체를 재동기하십시오.",
        file=sys.stderr,
    )


def _shared_read_api(name: str):
    """공유 읽기 seam 의 함수 하나 — 없거나 못 쓰면 `None` (등재 예외의 강등 분기·loud).

    **부재/손상 로드**와 **구세대 사본**(로드는 되는데 그 함수가 없는 형상)을 함께 본다 —
    `_atomic_replace_conf` 가 `getattr(..., "atomic_replace", None)` 로 두 형상을 같이 받는 것과
    같은 규칙이다(무락 복구 계약). skew 정책만 쓰기와 다르다(위 절 주석).
    """
    try:
        seam = _load_file_lock()
    except Exception as exc:  # noqa: BLE001 — 부재/손상/혼합은 이 복구 채널의 정상 입력이다.
        skew = _absorb_engine_rev_skew_for_recovery(exc, "shared_read_seam")
        cause = f"엔진 사본 불일치 — {exc}" if skew else f"{type(exc).__name__}: {exc}"
        _warn_shared_read_degraded(cause)
        return None
    api = getattr(seam, name, None)
    if api is None:
        _warn_shared_read_degraded(f"구세대 file_lock 사본에 {name} 이(가) 없음")
    return api


def _read_text_shared(path, *, encoding=None, errors=None, newline=None) -> str:
    """`file_lock.read_text_shared` — seam 을 못 쓰면 같은 의미의 종전 읽기로 강등한다."""
    api = _shared_read_api("read_text_shared")
    if api is not None:
        return api(path, encoding=encoding, errors=errors, newline=newline)
    with open(path, "r", encoding=encoding, errors=errors, newline=newline) as handle:
        return handle.read()


def _read_bytes_shared(path) -> bytes:
    """`file_lock.read_bytes_shared` — seam 을 못 쓰면 같은 의미의 종전 읽기로 강등한다."""
    api = _shared_read_api("read_bytes_shared")
    if api is not None:
        return api(path)
    with open(path, "rb") as handle:
        return handle.read()


def _open_shared(path, *, binary, encoding=None, errors=None, newline=None):
    """`file_lock.open_shared` — seam 을 못 쓰면 같은 의미의 종전 열기로 강등한다."""
    api = _shared_read_api("open_shared")
    if api is not None:
        return api(path, binary=binary, encoding=encoding, errors=errors, newline=newline)
    if binary:
        return open(path, "rb")
    return open(path, "r", encoding=encoding, errors=errors, newline=newline)


def _force_rmtree(path: Path) -> None:
    """트리를 **실제로** 지운다 — 공용 seam(`file_lock.force_rmtree`) 우선·실패는 올린다.

    `shutil.rmtree(..., ignore_errors=True)` 를 쓰지 않는다: git object·packfile 은 read-only 라
    Windows 에서 삭제가 거부되는데, 그 실패를 삼키면 부분 board·임시 clone 이 채택자 트리에 남고도
    rc=0 이 된다(`.git/modules` 잔재 실측). 부재는 성공이다(정리의 목적은 "없다").

    형제 로드 실패/구세대 사본은 **맨 `shutil.rmtree`** 로 물러난다 — pm_import 는 복구/도입
    채널이라 형제 부재로 죽으면 자기 자신을 못 고친다(`_local_conf_lock_path` 와 같은 폴백 규율).
    폴백도 실패를 삼키지 않는다(재시도가 없을 뿐).
    """
    target = Path(path)
    if not os.path.lexists(target):
        return
    seam = None
    with contextlib.suppress(Exception):
        seam = getattr(_load_file_lock(), "force_rmtree", None)
    if seam is not None:
        seam(target)
        return
    shutil.rmtree(target)


def _local_conf_lock_path(conf_path: Path) -> Path:
    """conf writer 직렬화 락 경로 — 공용 seam 의 유도 규칙을 그대로 쓴다(pm_update 사본과 동형).

    같은 conf 를 건드리는 모든 writer 가 같은 파일에 도달해야 배타가 성립하므로 규칙은
    `file_lock.conf_lock_path` 한 곳이 소유한다. 그 함수가 없는 **구세대 file_lock 사본**에서는
    같은 규칙의 인라인 폴백으로 계산한다(`conf_lock_path`·`local_conf_write_lock` 는 rev 중간에
    들어왔고 `ENGINE_REV` 는 릴리스 단위로 찍히므로, 같은 rev 안에서도 사본의 API 형상이 갈릴 수
    있다). marked rev skew 는 여기서도 삼키지 않는다 — 부분 사본 진단을 잃지 않는다.
    """
    try:
        return _load_file_lock().conf_lock_path(conf_path)
    except Exception as exc:  # noqa: BLE001 — 일반 형제 손상만 인라인 유도로 물러난다.
        if _is_engine_rev_skew(exc):
            raise
        return Path(conf_path).parent / ".local" / "local-conf.lock"


def _conf_lock_section(lock, conf_path: Path):
    """로드된 `file_lock` 사본의 API 형상에 맞는 conf 락 구간을 만든다 (부분 업그레이드 호환).

    새 `local_conf_write_lock` 이 있으면 그것을 쓴다. 없고 구 `exclusive_file_lock` 만 있는 사본
    (같은 rev 로 찍혔지만 새 seam 이전 파일)에서는 **같은 락 파일**을 구 API 로 잡는다 —
    AttributeError 로 죽으면 복구 채널이 자기 자신을 못 고치고, 다른 파일을 잡으면 배타가 조용히
    사라진다. 둘 다 없으면 None = 종전 복구 계약의 무락 진행(프리미티브 *부재* 에만 허용).
    """
    section = getattr(lock, "local_conf_write_lock", None)
    if callable(section):
        return section(conf_path)
    legacy = getattr(lock, "exclusive_file_lock", None)
    if callable(legacy):
        return legacy(_local_conf_lock_path(conf_path))
    return None


@contextlib.contextmanager
def _local_conf_write_lock(conf_path: Path):
    """conf 를 쓰는 구간의 배타락 — 락 seam 을 못 읽으면 무락으로 진행한다(fail-soft).

    경로 유도는 `file_lock.conf_lock_path` 한 곳이 소유한다(board init·pm_update 온보딩과 같은
    파일에 도달해야 배타가 성립). 무락 폴백은 `file_lock` 자신의 규약과 같은 선택이다
    (프리미티브 *부재* 에만 허용 — 여기서 부재는 형제 모듈을 못 읽는 손상 사본이거나 락
    프리미티브가 없는 구세대 사본이다·`_conf_lock_section`).

    구간의 단위는 write 가 아니라 **"이 conf 를 읽고 판단하고 쓰는" 전체**다 — 현재 상태를 락
    밖에서 읽어 계획을 세우면 그 계획이 커밋 시점엔 이미 낡아(stale plan) 그사이 들어온 결정을
    덮는다. 호출부는 존재 판정·현재 텍스트 읽기·계획·쓰기·postcondition 을 이 안에 둔다.
    """
    try:
        lock = _load_file_lock()
    except Exception as exc:  # noqa: BLE001 — 일반 형제 손상만 무락 복구한다.
        if _is_engine_rev_skew(exc):
            raise
        lock = None
    section = None if lock is None else _conf_lock_section(lock, conf_path)
    if section is None:
        yield lock
        return
    with section:
        yield lock


def _write_conf_keys(path: Path, updates: dict[str, str]) -> bool:
    """local.conf 키들을 중복 없이 atomic 기록하고 실효값을 검증한다.

    `_set_conf_keys`로 모든 갱신 키를 유일화한 뒤 같은 디렉터리의 임시파일을
    원자 교체하여 부분 쓰기를 막는다. 변경이 없어도 reader와 같은 last-wins 파서로
    postcondition을 확인하며, 불일치는 성공으로 흡수하지 않고 fail-loud한다.

    읽기→교체→검증 전체가 **전 writer 공용 배타락** 안이다. 이 writer 는 커밋 전 내용을 읽고
    나중에 통째로 갈아끼우므로, 그 사이에 다른 진입이 원자적으로 append 한 결정(추가 리뷰어·
    위임 opt-in)이 교체본에 없어 사라진다 — 원자성만으로는 못 막고 같은 락을 공유해야 막힌다.
    실효값 검증도 같은 구간에 둔다(락 밖에서 다시 읽으면 남의 정상 append 를 불일치로 오판한다).

    **재진입 금지** — 호출부는 이 함수를 conf 락을 쥔 채 부르지 않는다(현 호출부는 전부 락 밖:
    pm_import 의 sync_local_conf/record_*, pm_config upstream set). 이미 락을 쥔 구간
    (pm_import.reapply_preserved_conf_keys·pm_update.record_upstream_revs)은
    `_write_conf_keys_locked` 를 직접 부른다.
    """
    with _local_conf_write_lock(path):
        return _write_conf_keys_locked(path, updates)


def _atomic_replace_conf(tmp: Path, path: Path) -> None:
    """conf 교체 — 공용 seam(`file_lock.atomic_replace`) 우선, 못 쓰면 **loud** 강등한다.

    pm_import 는 **복구 채널**이라 형제 사본이 구세대·손상인 트리에서도 conf 기록이 성립해야
    한다(무락 복구 계약과 같은 이유 · `test_local_conf_writer_serialization`). 교체 수단을 형제에
    걸어 두면 그 계약이 깨져 채택자가 자기 트리를 고치는 데 필요한 키를 못 쓴다 — 그래서 이 한
    지점만 등재된 예외다([[T-0729]] §결정 · 가드 등재부에 사유 필수).

    강등은 조용하지 않다(사유를 stderr 로 남긴다). **marked skew 는 흡수하지 않는다** — 같은 rev
    안의 API 형상 차이는 물러날 근거지만, rev 자체가 다른 사본은 조용한 오작동이 아니라 재동기
    안내로 표출해야 한다(락 경로 유도와 같은 규칙).
    """
    degrade_reason = ""
    try:
        replace = getattr(_load_file_lock(), "atomic_replace", None)
        if replace is None:
            degrade_reason = "구세대 file_lock 사본에 atomic_replace 가 없음"
    except Exception as exc:  # noqa: BLE001 — 부재/손상 사본은 이 복구 채널의 정상 입력이다.
        if _is_engine_rev_skew(exc):
            raise
        replace, degrade_reason = None, f"{type(exc).__name__}: {exc}"
    if replace is not None:
        replace(tmp, path)
        return
    print(
        f"경고: 원자 교체 공용 seam 을 쓸 수 없어 os.replace 로 진행합니다 ({degrade_reason}) — "
        f"Windows 에서는 대상이 열려 있으면 이 교체가 실패할 수 있습니다: {path}",
        file=sys.stderr,
    )
    os.replace(tmp, path)


def _write_conf_keys_locked(path: Path, updates: dict[str, str]) -> bool:
    """`_write_conf_keys` 의 임계 구간 본문 — 락을 **이미 쥔** 호출부 전용."""
    text = _read_text_shared(path, encoding="utf-8")
    new_text = _set_conf_keys(text, updates)
    changed = new_text != text
    if changed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(new_text, encoding="utf-8", newline="\n")
        _atomic_replace_conf(tmp, path)
    effective = _parse_conf_keys(_read_text_shared(path, encoding="utf-8"))
    mismatches = {
        key: (value, effective.get(key))
        for key, value in updates.items()
        if effective.get(key) != value.strip()
    }
    if mismatches:
        detail = ", ".join(
            f"{key}: 요청={requested!r}, 실효={actual!r}"
            for key, (requested, actual) in mismatches.items()
        )
        raise RuntimeError(f"local.conf 기록 후 실효값 불일치 — {detail}; 파일={path}")
    return changed


def backup_existing_local_conf(dest_root: Path, backup_root: Path | None) -> str | None:
    """--into 재-import 전, 기존 local.conf 가 있으면 백업하고 원본 텍스트를 반환한다.

    MF1: board.py init 은 local.conf 를 무조건 write_text 로 덮으므로, 이미 프레임워크를
    쓰던 프로젝트(재-import/업그레이드)면 기존 per-clone 설정(additional_reviewer.enabled·
    추가 리뷰어 프로필 `additional_reviewer.*` 등)이
    무백업 손실된다. local.conf 는 pm_import 의
    copy/backup 대상 트리 밖이라 CopyAction 의 백업 로직을 안 탄다 — init 호출 전 여기서
    명시적으로 백업한다.

    백업은 형제 `*.backup.<DATE>` 가 아니라 중앙 디렉토리
    `backup_root/.project_manager/local.conf` 로 라우팅한다(한 곳 원칙). local.conf 는
    보통 git-ignored(미추적)라 git-safe 아님 — 중앙 백업 유지(내용을 git 이 복원 못 함).
    backup_root=None(--new)이면 빈 디렉토리 보장이라 기존 local.conf 가 없으므로 호출되지 않음.
    중앙 경로 충돌은 _free_backup_path 로 순번 부여(원본 보존). 반환값(원본 텍스트)은
    reapply_preserved_conf_keys 가 사용자 키 재병합에 쓴다. None = 기존 local.conf 없음.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        return None
    original_text = _read_text_shared(local_conf, encoding="utf-8")
    if backup_root is None:
        return original_text  # --new 빈 디렉토리 — 보존만(백업 위치 없음·실질 도달 안 함).
    backup = _free_backup_path(backup_root / local_conf.relative_to(dest_root))
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(local_conf, backup)
    rel = backup.relative_to(dest_root).as_posix()
    print(f"✓ 기존 local.conf 백업: {rel}")
    return original_text


def reapply_preserved_conf_keys(dest_root: Path, original_text: str) -> bool:
    """board.py init 이 새로 쓴 local.conf 위에, 기존 파일의 사용자 키를 재병합한다.

    MF1: board.py init 은 local.conf 를 통째로 덮으므로, init 이 *안 쓴* 사용자 키
    (additional_reviewer.enabled·추가 리뷰어 프로필 `additional_reviewer.*`
    등)는 init 후 사라진다. 따라서 init 산출 local.conf 에 *없는* 기존 키만
    _set_conf_keys 로 다시 얹는다. init 이 쓴 키(`runtime.py`·`test.cmd`·`project.name`·ctx 예산)는
    init/operational sync 값을 우선해 덮지 않는다. 결과: import 후 local.conf = board init 기본 + operational sync
    + 사용자 기존 설정 보존. 재병합으로 변경 시 True.

    보존은 **키 이름을 열거하지 않는 일반 규칙**이다 — `_parse_conf_keys` 가 `key=value` 를
    문자열로만 다루므로 점 표기(`additional_reviewer.harness`)든 채택자가 만든 커스텀
    `additional_reviewer.<임의>` 든 그대로 왕복한다. 구표기 키를 신표기로
    **자동 마이그레이션하지 않고**, 사용자가 이미 쓴 튜플 값도 덮지 않는다(원문 보존).

    보존 대상 계산은 **쓰기와 같은 락 구간 안**이다. 현재 conf 를 락 밖에서 읽어 계획을 세우면
    그사이 다른 진입(추가 리뷰어·위임 opt-in)이 기록한 새 결정이 "현재 conf 에 없는 키" 로 남아,
    백업에 있던 **옛 값이 새 결정을 덮는다**(예: 백업 `additional_reviewer.enabled=false` 가 방금
    기록된 `true` 를 되돌린다). 백업 텍스트 파싱은 대상 conf 와 경쟁하지 않으므로 락 밖이다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        # conf 자체가 없는 형상에 락 파일을 만들지 않는 값싼 단축(pm_update.record_upstream_revs
        # 와 같은 규약) — "쓰지 않는다" 만 결정하고 어떤 write 계획의 입력도 아니다. 권위 판정은
        # 락 안에서 다시 한다.
        return False
    original_keys = _parse_conf_keys(original_text)  # 대상 conf 와 무경쟁 — 락 밖.
    with _local_conf_write_lock(local_conf):
        if not local_conf.is_file():
            return False
        current_keys = _parse_conf_keys(_read_text_shared(local_conf, encoding="utf-8"))
        # board init 이 새로 쓴 local.conf 에 *없는* 기존 사용자 키만 복원(init 값 우선).
        preserved = {
            key: value
            for key, value in original_keys.items()
            if key not in current_keys
        }
        if not preserved:
            return False
        # 락을 이미 쥐었으므로 임계 구간 본문을 직접 부른다(`_write_conf_keys` 재호출 = 재진입).
        changed = _write_conf_keys_locked(local_conf, preserved)
    if changed:
        kept = "·".join(sorted(preserved))
        print(f"✓ 기존 local.conf 사용자 키 보존: {kept}")
        return True
    return False


def record_upstream(dest_root: Path, upstream_value) -> bool:
    """upstream 값(URL 또는 로컬 경로)을 dest local.conf 에 `upstream.path=` 로 기록한다.

    `--upstream`(future update 기록·URL 선호)↔`--from`(이번 import 파일 소스) 디커플:
    이 함수는 *기록할 upstream 값*을 받아 그대로 박는다. `--upstream` 생략 시 호출부가
    `--from`(=source_root)을 넘겨 **기존 동작(경로 기록)을 회귀 보존**한다 — `Path` 를 받으면
    `str()` 로 직렬화하므로 옛 `record_upstream(dest_root, source_root)` 호출 형태도 그대로 동작.

    이후 pm_update 가 --from 생략 시 이 값을 기본 upstream 으로 쓴다. 공용 writer의 키 단위
    중복 정규화·atomic replace·실효값 검증을 거친다 — 따라서 재-import(--into)
    는 reapply_preserved_conf_keys 가 백업의 *stale upstream 을 되살려도*(board init 은 upstream 을
    쓰지 않으므로 preserve 가 옛 값을 복원한다) 마지막에 *현재 값* 으로 제자리 확정 갱신된다(stale
    보존 아님). 바로 그 때문에 board init·conf sync·preserve 단계 *이후* 에 호출해야 갱신이 보장된다. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream 기록 건너뜀.", file=sys.stderr)
        return False
    return _write_conf_keys(local_conf, {"upstream.path": str(upstream_value)})


def record_upstream_rev(dest_root: Path, rev: str) -> bool:
    """upstream baseline revision 을 dest local.conf 에 `upstream.rev=<commit>` 로 기록한다.

    drift-lint의 baseline 입력 — "마지막 동기 이후 upstream 변경분"을 재는 기준점이다
    import 시(이 함수)와 pm_update 매 sync 시 갱신된다. `upstream.seen_rev`(현재
    관찰값·pm-update 스킬 기록)는 **별개 키** — 한 키 2역 금지(race/자기비교 회피). rev 가
    빈 값(git repo 아님·HEAD 해소 실패)이면 호출부가 이 함수를 부르지 않는다(기록 생략·graceful).
    공용 writer의 중복 정규화·atomic replace·실효값 검증을 거치며 다른 키·주석은 보존한다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — upstream.rev 기록 건너뜀.", file=sys.stderr)
        return False
    return _write_conf_keys(local_conf, {"upstream.rev": rev})


def record_opencode_model(dest_root: Path, model: str) -> bool:
    """해소된 opencode 모델을 dest local.conf 에 `harness.opencode.pro_model=` 로 기록한다.

    {{OPENCODE_PRO_MODEL}} 가 import 때 파일에 직접 치환되지만, local.conf 엔 안 들어가
    pm_update 의 @render 가 그 토큰을 local.conf 에서 재유도할 때(`harness.opencode.pro_model` →
    OPENCODE_PRO_MODEL · pm_update._LOCAL_CONF_TO_OPERATIONAL) 키 부재로 leak assertion 에
    걸려 채택자 렌더가 crash 한다. 따라서 *실제로 모델이 해소된* 경로(flag·interactive)에서만
    그 값을 local.conf 에 박아 둔다 — todo(미해소)는 토큰이 YAML 주석으로 남아 렌더 leak 이
    없으므로 기록하지 않는다(호출부 게이트). 공용 writer의 중복 정규화·atomic replace·실효값
    검증을 거치며 다른 키·주석은 보존한다. local.conf 부재면 graceful skip. 변경 시 True.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.is_file():
        print(f"경고: local.conf 없음 ({local_conf}) — opencode 모델 기록 건너뜀.",
              file=sys.stderr)
        return False
    return _write_conf_keys(local_conf, {"harness.opencode.pro_model": model})


# ── opencode 모델 결정적 해소 단계 (LLM 아님) ──────────────────────
# board init·conf sync 직후·fill *이전* 의 결정적 단계(sync_local_conf 와 같은 결). opencode
# 어댑터 token({{OPENCODE_PRO_MODEL}})이 이번 복사본에 잔존할 때만 동작한다.
# 해소 순서: 1) --opencode-model 명시 → 치환  2) 없고 stdin tty → `opencode models` 번호목록·선택
# → 치환  3) 없고 비-tty 또는 opencode 부재 → 치환 안 함·TODO 마커(가용목록 인라인)+stderr 경고.
# `opencode models` 가 실제 가용 모델의 단일 진실 — LLM 추측(fill) 대신 결정적 조회를 쓴다.

# `opencode models` 조회 seam — `()` → (성공 여부, provider/model 목록). 테스트가 stub 주입.
ModelsRunner = Callable[[], "tuple[bool, list[str]]"]


class ModelResolveResult:
    """opencode 모델 해소 산출 — 어느 경로로 갔는지·결정된 값·치환 파일 수.

    필드:
      active    : 해소 단계가 동작했는가 (opencode 토큰 잔존 시만 True; claude-only 면 False).
      path      : 'flag' | 'interactive' | 'todo' | 'inactive' — 해소 경로.
      model     : 치환에 쓴 모델 ID (PROVIDER/MODEL) 또는 None(미치환).
      changed   : {{OPENCODE_PRO_MODEL}} 을 치환한 파일 수 (0 = 미치환·TODO 폴백).
      available : `opencode models` 조회 성공 시 가용 모델 목록 (실패/미조회 시 빈 리스트).
      tty       : 해소 시점 stdin 이 tty 였는가 (대화형 가능 여부).
      todos     : TODO 폴백에서 마커를 추가한 토큰 목록.
      note      : 사람 대상 메모 (경고·경로 사유).

    plain class (dataclass 아님): FillResult 와 같은 이유 — 테스트가 spec_from_file_location
    동적 로드 시 dataclass 의 문자열 annotation 해석이 깨진다.
    """

    def __init__(
        self,
        active: bool = False,
        path: str = "inactive",
        model: str | None = None,
        changed: int = 0,
        available: list | None = None,
        tty: bool = False,
        todos: list | None = None,
        note: str = "",
    ):
        self.active = active
        self.path = path
        self.model = model
        self.changed = changed
        self.available = available if available is not None else []
        self.tty = tty
        self.todos = todos if todos is not None else []
        self.note = note

    def __repr__(self) -> str:
        return (f"ModelResolveResult(active={self.active!r}, path={self.path!r}, "
                f"model={self.model!r}, changed={self.changed!r})")


def _real_models_runner() -> tuple[bool, list[str]]:
    """실 `opencode models` 를 subprocess 로 조회(fail-soft). 반환: (성공 여부, 모델 목록).

    _real_harness_runner 선례 — 예외를 raise 하지 않고 (False, []) 로 감싼다. opencode 바이너리가
    PATH 에 없으면(shutil.which 부재) subprocess 도 안 띄우고 즉시 (False, []) — fail-soft.
    stdout 은 _parse_opencode_models 로 줄단위 provider/model 파싱한다.

    fail-soft 는 유지(import 안 깸)하되 *침묵*은 제거한다 — 각 실패 분기에서 stderr 로
    사유 1줄을 surface 해 사용자가 다음 실행 때 왜 자동해소가 실패했는지(PATH/rc/timeout/parse)를
    본다. 타임아웃은 _opencode_models_timeout()
    (env PM_OPENCODE_MODELS_TIMEOUT > 기본 60)으로 해소한다.
    """
    if shutil.which("opencode") is None:
        print("opencode 바이너리 PATH 부재 — 모델 자동해소 skip", file=sys.stderr)
        return False, []
    timeout = _opencode_models_timeout()
    try:
        result = subprocess.run(
            list(OPENCODE_MODELS_CMD),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        if result.returncode != 0:
            print(
                f"opencode models 실패 rc={result.returncode}: "
                f"{(result.stderr or '').strip()[:200]}",
                file=sys.stderr,
            )
            return False, []
        models = _parse_opencode_models(result.stdout or "")
        if not models:
            print("opencode models 출력에서 모델 0개 파싱 — 형식 확인", file=sys.stderr)
        return True, models
    except subprocess.TimeoutExpired:
        print(
            f"opencode models {timeout}s timeout 초과 — "
            "PM_OPENCODE_MODELS_TIMEOUT 로 늘리세요",
            file=sys.stderr,
        )
        return False, []
    except FileNotFoundError as exc:
        print(f"opencode models 예외: {exc}", file=sys.stderr)
        return False, []
    except Exception as exc:  # noqa: BLE001 — fail-soft: 어떤 예외도 import 를 깨지 않는다.
        print(f"opencode models 예외: {exc}", file=sys.stderr)
        return False, []


def _parse_opencode_models(output: str) -> list[str]:
    """`opencode models` stdout → provider/model 목록. 빈 줄·배너 제외.

    실측 형식: 줄당 `provider/model`(예 'ollama/gemma4:26b'·'opencode/big-pickle'). 슬래시가
    있는 줄만 모델로 본다(배너·헤더 줄 제외). 앞뒤 공백 strip, 순서·중복은 입력대로 보존.
    """
    models: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped or "/" not in stripped:
            continue
        models.append(stripped)
    return models


def _substitute_model_token(
    dest_root: Path,
    model: str,
    copied_relpaths: set[Path],
    root_identity: tuple | None = None,
) -> int:
    """{{OPENCODE_PRO_MODEL}} 을 복사 파일 전역에서 결정적 치환. 변경 파일 수 반환.

    substitute_placeholders 와 동일한 copied_relpaths 비파괴 범위·동일 _should_substitute
    제외-판정(새 파일 형식이 자동 편입된다).
    이번 import 가 복사한 파일만 — 복사 안 한 사용자 파일은 절대 안 건드린다.
    대상 = `.opencode/agents/*.md` 의 `model:` 필드·AGENTS.md 잔존분.
    """
    changed = 0
    swapped: list[str] = []
    vanished: list[str] = []
    sed_exclude = _dest_sed_exclude(dest_root)  # 치환 시점·dest manifest 기준(codex must-fix)
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if not _should_substitute(rel, sed_exclude):
            continue
        anomaly = _copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            swapped.append(rel.as_posix())
            continue
        try:
            text = read_dest_text(dest_root, rel, root_identity=root_identity)
        except UnsafeDestPathError:
            swapped.append(rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(rel.as_posix())  # 읽기 전 삭제도 loud 제외(쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        if OPENCODE_MODEL_TOKEN not in text:
            continue
        try:
            write_dest_text(dest_root, rel, text.replace(OPENCODE_MODEL_TOKEN, model),
                            root_identity=root_identity)
        except UnsafeDestPathError:
            swapped.append(rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(rel.as_posix())
            continue
        changed += 1
    _report_copied_scope_anomalies("모델 토큰 치환", swapped, vanished)
    return changed


def _prompt_model_choice(models: list[str], stdin) -> str | None:
    """`opencode models` 번호 목록을 출력하고 stdin 에서 선택을 읽어 모델 ID 를 반환.

    1-based 번호 목록을 stdout 에 출력하고 stdin 한 줄을 읽는다. 유효한 번호면 해당 모델,
    빈 입력·범위 밖·비숫자·EOF 면 None(미선택 → 호출자가 TODO 폴백). 테스트는 io.StringIO 를
    stdin 으로 주입해 결정적으로 검증한다(라이브 입력 없음).
    """
    if not models:
        return None
    print("opencode 가용 모델 ({{OPENCODE_PRO_MODEL}} 에 쓸 모델 선택):")
    for i, m in enumerate(models, start=1):
        print(f"  {i}) {m}")
    print(f"  번호 입력 (1-{len(models)}, 빈 입력 = 건너뜀): ", end="")
    try:
        line = stdin.readline()
    except Exception:  # noqa: BLE001 — 입력 실패는 미선택 폴백.
        return None
    if not line:  # EOF
        return None
    choice = line.strip()
    if not choice:
        return None
    try:
        idx = int(choice)
    except ValueError:
        return None
    if 1 <= idx <= len(models):
        return models[idx - 1]
    return None


def _mark_model_todos(
    dest_root: Path,
    copied_relpaths: set[Path],
    available: list[str],
    root_identity: tuple | None = None,
) -> list[str]:
    """비-tty/opencode 부재 폴백: `model:` 줄을 주석화하고 그 안의 모델 토큰을 중화한다.

    _mark_todos 폴백을 흡수— 모델 토큰만 대상. 조회 성공 시 가용 모델 목록을 마커에
    인라인해 사람이 바로 고를 수 있게 한다. 비파괴 규칙(이미 TODO/주석인 줄은 건너뜀·
    copied_relpaths 범위 한정). 마크한 토큰([OPENCODE_MODEL_TOKEN] 또는 [])을 반환.

    미해소 시 `model:` 값을 활성으로 남기면(`model: "…"  # TODO`) opencode 가
    "configured model … is not valid" 로 agent 자체를 거부한다(실 파일럿 블로커). → 줄 *전체*를
    주석화해 YAML frontmatter 에서 `model` 키를 *부재*시킨다 → opencode 가 기본 모델로 agent 를
    구동(graceful degradation).

    미해소 폴백이 주석 줄에 리터럴 `{{OPENCODE_PRO_MODEL}}` 을
    남기면 render `_assert_no_leak` 가 hard-fail 한다(@render path 산출물에 토큰 0 이어야 함). 그래서
    주석화하면서 토큰을 **형식 힌트 `<provider/model>` 로 중화**한다 → `# model: "<provider/model>"
    # TODO: …`. 채택자는 주석을 해제하고 provider/model 을 채우거나 `--opencode-model` 재import.

    실 줄-중화 로직은 `pm_render.neutralize_model_todo`
    로 옮겨 import(여기·render *이전* 폴백)와 self-update(pm_update 의 @render 재렌더·render_adapter
    가 같은 함수 호출) 둘 다 **같은 산출**을 내게 한다(byte-동일·drift 0). 옛 opencode `@source`
    비대칭(update 가 미해소 토큰을 leak 으로 rc-fail)의 근본 fix. 렌더러 로드 실패 시엔 **fail-loud**
    (raise) — 이 폴백의 계약은 "미해소 `model:` 줄을 *반드시* 중화해 안전 출하"이므로, 중화
    못 하면 조용히 활성 `{{OPENCODE_PRO_MODEL}}` 을 출하(opencode 가 agent 거부)하는 대신 크게
    터뜨린다. pm_render 는 co-located 엔진이라 정상 설치에선 항상 로드된다(미발화) — 로드 실패는
    broken install 신호이므로 loud 가 옳다(silent-degrade 근절·robustness 값-연결 assert).
    """
    render_mod = _load_pm_render_module()
    if render_mod is None:
        # fail-loud: 렌더러(공유 중화 단일 진실)를 못 실으면 중화 불가 — 조용히 활성 토큰을 출하하는
        # 대신 raise. 정상 설치에선 미발화(co-located 엔진). 로드 실패 = broken install → loud.
        raise RuntimeError(
            "pm_render 모듈 로드 실패 — opencode 모델 토큰 {{OPENCODE_PRO_MODEL}} 을 중화할 수 "
            "없습니다. 활성 토큰을 출하하면 opencode 가 agent 를 거부합니다. 엔진 설치를 "
            "확인하세요(.project_manager/tools/pm_render.py)."
        )
    marked = False
    swapped: list[str] = []
    vanished: list[str] = []
    for _rel, _path in _iter_copied_files(
            dest_root, copied_relpaths, swapped=swapped, vanished=vanished):
        try:
            text = read_dest_text(dest_root, _rel, root_identity=root_identity)
        except UnsafeDestPathError:
            swapped.append(_rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(_rel.as_posix())  # 읽기 전 삭제도 loud 제외(쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        # 공유 중화(pm_render): agent frontmatter 의 `model:` 필드 줄만 주석화·토큰 중화한다 —
        # 산문/헤더(README 의 `{{OPENCODE_PRO_MODEL}}` 예시 등)는 건드리지 않는다(모듈 함수가
        # `model:` 시작 줄로 한정·YAML 주석 안전). 비파괴 멱등도 그쪽이 보장.
        new_text, changed = render_mod.neutralize_model_todo(text, available)
        if changed and new_text != text:
            try:
                write_dest_text(dest_root, _rel, new_text, root_identity=root_identity)
            except UnsafeDestPathError:
                swapped.append(_rel.as_posix())
                continue
            except FileNotFoundError:
                vanished.append(_rel.as_posix())
                continue
            marked = True
    _report_copied_scope_anomalies("모델 TODO 표시", swapped, vanished)
    return [OPENCODE_MODEL_TOKEN] if marked else []


def resolve_opencode_model(
    dest_root: Path,
    copied_relpaths: set[Path],
    *,
    model_arg: str | None,
    models_runner: ModelsRunner | None = None,
    stdin=None,
    root_identity: tuple | None = None,
) -> ModelResolveResult:
    """{{OPENCODE_PRO_MODEL}} 을 결정적으로 해소. board init·conf sync 직후·fill 이전.

    opencode 어댑터 token 이 이번 복사본(copied_relpaths)에 잔존할 때만 동작 — 없으면 inactive.
    해소 순서:
      1) model_arg 명시 → 치환. (조회 가능하면 목록 대조해 *경고만*; 목록에 없어도 사용자 의도
         존중·치환 — 회사 사설 모델 등.)
      2) 없고 stdin tty → `opencode models` 번호목록 출력·선택 입력 → 치환. (선택 안 하면 TODO 폴백.)
      3) 없고 비-tty(CI·파이프) 또는 opencode 바이너리 부재 → 치환 안 함·TODO 마커(조회 성공 시
         가용목록 인라인)+stderr 경고.

    models_runner: `opencode models` 조회 seam — 테스트 stub 주입(라이브 CLI 미실행). None 이면
                   실 _real_models_runner. stdin: 대화형 선택 입력 seam — None 이면 sys.stdin.
    치환은 substitute_placeholders 와 동일한 copied_relpaths 비파괴 범위·_should_substitute 규칙.
    """
    # opencode 토큰이 이번 복사본에 없으면 단계 자체가 무의미(claude-only) — inactive.
    model_scan_swapped: list[str] = []
    model_scan_vanished: list[str] = []
    token_present = _token_present(
        dest_root, OPENCODE_MODEL_TOKEN, copied_relpaths,
        root_identity=root_identity, swapped=model_scan_swapped,
        vanished=model_scan_vanished)
    # 교체됐거나 사라져 못 읽은 파일은 "토큰 없음"이 아니다 — 판정에서 빼되 조용히 넘기지 않는다.
    _report_copied_scope_anomalies(
        "모델 토큰 판정", model_scan_swapped, model_scan_vanished)
    if not token_present:
        return ModelResolveResult(active=False, path="inactive",
                                  note="opencode 모델 토큰 미잔존 — 해소 단계 비활성(claude-only).")

    runner = models_runner if models_runner is not None else _real_models_runner
    stream = stdin if stdin is not None else sys.stdin
    is_tty = bool(getattr(stream, "isatty", lambda: False)())

    # 1) --opencode-model 명시 → 치환(사용자 의도 우선). 조회 가능하면 목록 대조 경고만.
    if model_arg:
        # 명시값을 **먼저 확정**(치환) — 외부 `opencode models` 조회가 명시-플래그 경로의 import
        # 를 막지 않게(codex suggestion). 목록 대조는 그 *뒤* best-effort 경고만(짧은 timeout·
        # fail-soft — 조회 실패/지연이 치환 결과를 바꾸지 않는다).
        changed = _substitute_model_token(dest_root, model_arg, copied_relpaths,
                                          root_identity=root_identity)
        ok, available = runner()
        if ok and available and model_arg not in available:
            print(
                f"경고: --opencode-model '{model_arg}' 가 `opencode models` 가용 목록에 없습니다 "
                f"(사용자 의도 존중·그대로 치환됨). 가용: {', '.join(available)}.",
                file=sys.stderr,
            )
        return ModelResolveResult(
            active=True, path="flag", model=model_arg, changed=changed,
            available=available if ok else [], tty=is_tty,
            note=f"--opencode-model 명시값으로 치환({changed} 파일).",
        )

    # 플래그 없음 → `opencode models` 조회(2·3 공통 전제).
    ok, available = runner()

    # 2) stdin tty + 조회 성공 → 번호목록·대화형 선택 → 치환.
    if is_tty and ok and available:
        choice = _prompt_model_choice(available, stream)
        if choice:
            changed = _substitute_model_token(dest_root, choice, copied_relpaths,
                                              root_identity=root_identity)
            return ModelResolveResult(
                active=True, path="interactive", model=choice, changed=changed,
                available=available, tty=True,
                note=f"대화형 선택 '{choice}' 로 치환({changed} 파일).",
            )
        # 선택 안 함(빈 입력·범위 밖) → TODO 폴백.
        todos = _mark_model_todos(dest_root, copied_relpaths, available,
                                  root_identity=root_identity)
        print("경고: opencode 모델 미선택 — {{OPENCODE_PRO_MODEL}} 을 TODO 로 표시(손으로 채우세요).",
              file=sys.stderr)
        return ModelResolveResult(
            active=True, path="todo", model=None, changed=0,
            available=available, tty=True, todos=todos,
            note="대화형 선택 건너뜀 — TODO 폴백.",
        )

    # 3) 비-tty / opencode 부재·조회 실패 → 치환 안 함·TODO 마커(가용목록 인라인 시도)+경고.
    todos = _mark_model_todos(dest_root, copied_relpaths, available if ok else [],
                              root_identity=root_identity)
    if not ok:
        reason = "opencode 바이너리 부재 또는 `opencode models` 조회 실패"
    elif not is_tty:
        reason = "비대화형(CI·파이프) — 블로킹 프롬프트 회피"
    else:
        reason = "가용 모델 없음"
    print(
        f"경고: {{{{OPENCODE_PRO_MODEL}}}} 미치환({reason}) — TODO 로 표시했습니다. "
        f"--opencode-model PROVIDER/MODEL 로 명시하거나 손으로 채우세요.",
        file=sys.stderr,
    )
    return ModelResolveResult(
        active=True, path="todo", model=None, changed=0,
        available=available if ok else [], tty=is_tty, todos=todos,
        note=f"치환 안 함({reason}) — TODO 폴백.",
    )


# ── fill 단계 (자유서술 placeholder · 하니스 구동 · opt-in) ──────────────────
# board init·local.conf 동기화 직후의 hook 지점. sed 로 못 채우는 자유서술 placeholder 를
# 대상 하니스를 헤드리스 구동해 *제안* 한다(auto) / TODO 로 표시한다(manual). 실구동은 토큰·
# 외부모델 비용이므로 opt-in 게이트(LIVE_HARNESS_ENV + --fill auto) 뒤로 격리한다.

# 하니스 호출 seam — (argv, prompt) → (성공 여부, stdout). 테스트가 stub 주입(토큰 0).
HarnessRunner = Callable[[list[str], str], "tuple[bool, str]"]


class FillResult:
    """fill 단계 산출 — 자유서술 placeholder 값 + 초안 제안 (확정 아님, 사람 리뷰 전제).

    필드:
      mode        : 'auto' (하니스 구동) | 'manual' (TODO 표시).
      harness     : 실제 구동 하니스 ('claude' | 'opencode' | None=manual).
      live        : 실 하니스를 호출했는가 (opt-in 게이트 통과 시만 True).
      values      : placeholder token → 채운 값 (auto·stub 모두 채움 — manual 은 빈 dict).
      drafts      : 라벨 → 초안 텍스트 제안 (CLAUDE.md·pm_role.local.md·harness-output 등).
      todos       : manual 에서 TODO 로 남긴 placeholder token 목록.
      runner_calls: 하니스에 보낸 argv 리스트 (명령 조립 검증·로깅용 — stub 도 기록).
      note        : 사람 대상 메모 (게이트 차단 이유 등).

    plain class (dataclass 아님): 테스트가 spec_from_file_location 로 동적 로드하는데,
    그 경로에선 모듈이 sys.modules 에 없어 dataclass 의 문자열 annotation 해석이 깨진다.
    """

    def __init__(
        self,
        mode: str,
        harness: str | None = None,
        live: bool = False,
        values: dict | None = None,
        drafts: dict | None = None,
        todos: list | None = None,
        runner_calls: list | None = None,
        note: str = "",
    ):
        self.mode = mode
        self.harness = harness
        self.live = live
        self.values = values if values is not None else {}
        self.drafts = drafts if drafts is not None else {}
        self.todos = todos if todos is not None else []
        self.runner_calls = runner_calls if runner_calls is not None else []
        self.note = note

    def __repr__(self) -> str:
        return (f"FillResult(mode={self.mode!r}, harness={self.harness!r}, live={self.live!r}, "
                f"values={list(self.values)!r}, todos={self.todos!r})")


def _load_watchdog():
    """엔진 pm_relay 의 공용 워치독을 지연 로드한다 (deep-import seam·순환 회피).

    pm_import 와 pm_relay 는 형제(`.project_manager/tools/`) — importlib 로 직접 로드해
    PYTHONPATH 의존 없이(테스트 spec_from_file_location 경로 포함) run_with_first_event_watchdog·
    하네스 프로필·timeout sentinel 을 빌려 쓴다(board._load_domain_module 선례 동형)."""
    engine_path = Path(__file__).resolve().parent / "pm_relay.py"
    return _load_module_from_path(
        engine_path, "pm_relay.py", verifier=_verify_engine_rev,
    )


def _fill_driver(argv: list[str]) -> tuple[str, bool, str | None]:
    """실제 fill argv를 (하네스·증분 신호·stdin) 선언으로 해소한다."""
    for command, driver in FILL_DRIVER_BY_CMD.items():
        if tuple(argv[:len(command)]) != command:
            continue
        # 실행 중 이벤트를 내는 **실제 출력 형식**까지 argv에서 확인한다. 바이너리 접두사만
        # 맞고 JSON 플래그가 빠진 주입 호출은 평문/종료시 blob일 수 있으므로 증분 신호 축으로
        # 올리지 않는다(모르는 형식 = idle 판정 없음·가장 관대한 wall).
        if command == OPENCODE_FILL_CMD:
            has_json_format = any(
                token == "--format=json"
                or (token == "--format" and index + 1 < len(argv)
                    and argv[index + 1] == "json")
                for index, token in enumerate(argv)
            )
            if not has_json_format:
                break
        elif command == CODEX_FILL_CMD and "--json" not in argv[len(command):]:
            # 출력이 평문이면 증분 신호/프로필은 보수적으로 미지 축에 두되, codex의 stdin EOF
            # 정책은 출력 형식과 무관하다. 빈 PIPE를 닫지 않으면 추가 입력 대기로 돌아간다.
            return "", False, driver[2]
        return driver
    # 내부 빌더 밖의 주입 호출은 미지 프로필로 보수 처리한다: 신호·stdin을 추정하지 않고
    # 가장 관대한 wall만 적용한다. basename이 알려진 이름이어도 argv 계약이 다르면 미지 축이다.
    return "", False, None


def _fill_local_config(cwd: Path | str | None) -> dict[str, str]:
    """fill 대상 repo의 배포별 하네스 timeout override를 읽는다(fail-soft)."""
    if cwd is None:
        return {}
    path = Path(cwd) / ".project_manager" / "local.conf"
    try:
        return _parse_conf_keys(_read_text_shared(path, encoding="utf-8")) if path.is_file() else {}
    except (OSError, UnicodeError) as exc:
        print(f"경고: fill timeout 설정을 읽을 수 없음({path}): {exc} — 프로필 기본값 사용.",
              file=sys.stderr)
        return {}


def fill_harness_cap_advisory(harness: str, cwd: Path | str | None) -> str | None:
    """fill도 위임·리뷰와 같은 호출층 상한 판정을 소비한다(never-block advisory)."""
    relay = _load_watchdog()
    profile = relay.resolve_harness_profile(harness, _fill_local_config(cwd))
    first_event_timeout = (
        relay.first_event_timeout_default() if profile.startup_watchdog else None
    )
    retries = relay.stall_retries_default() if profile.startup_watchdog else 0
    execution_budget = relay.watchdog_execution_budget(
        profile.wall_timeout,
        first_event_timeout=first_event_timeout,
        retries=retries,
    )
    return relay.harness_cap_advisory(
        os.environ, execution_budget=execution_budget,
        session_markers=relay.HARNESS_SESSION_MARKERS,
        cap_env=relay.HARNESS_CAP_ENV,
        render_missing=lambda _harness, cap_key, required: (
            f"[fill auto] 경고: 호출층 상한 {cap_key} 미해소 — "
            f"실행+정리+부분 산출물 보존 여유 {required}s 이상을 설정하세요."
        ),
        render_invalid=lambda _harness, cap_key, raw, required: (
            f"[fill auto] 경고: 호출층 상한 {cap_key}={raw!r} 해석 불가 — "
            f"실행+정리+부분 산출물 보존 여유 {required}s 이상을 설정하세요."
        ),
        render_low=lambda _harness, cap_key, cap_seconds, required: (
            f"[fill auto] 경고: 호출층 상한 {cap_key}={cap_seconds:g}s < "
            f"실행+정리+부분 산출물 보존 여유 {required}s — 엔진 진단/부분 산출물 보존 전에 "
            f"하네스가 kill할 수 있습니다. {cap_key}를 상향하세요."
        ),
    )


def _fill_failure_with_partial(head: str, exc: BaseException) -> str:
    """워치독 kill/정리 실패의 stdout·stderr를 fill 실패 진단에 보존한다."""
    try:
        return _load_watchdog().format_partial_output(head, exc)
    except Exception as formatter_exc:  # noqa: BLE001 — 일반 formatter/load 실패는 fail-soft.
        if _is_engine_rev_skew(formatter_exc):
            raise
        return head


def _save_fill_failure_output(dest_root: Path, harness: str, output: str) -> Path:
    """fill 실패 원문을 repo 안 private raw 파일로 이식 가능하게 박제한다."""
    dest_root = Path(dest_root)
    repo_root = dest_root.resolve(strict=True)
    if not repo_root.is_dir():
        raise NotADirectoryError(f"fill raw 대상 repo가 디렉터리가 아님: {dest_root}")
    raw_parts = (".project_manager", ".local", "fill")
    raw_dir = repo_root.joinpath(*raw_parts)

    def _assert_raw_dir_contained() -> None:
        try:
            raw_dir.resolve(strict=False).relative_to(repo_root)
        except ValueError as exc:
            raise OSError(
                f"fill raw 경로가 대상 repo 밖을 가리킴: {raw_dir}"
            ) from exc

    _assert_raw_dir_contained()

    os.makedirs(raw_dir, mode=0o700, exist_ok=True)
    _assert_raw_dir_contained()
    raw_dir_stat = os.lstat(raw_dir)
    if stat.S_ISLNK(raw_dir_stat.st_mode):
        raise OSError(f"fill raw 부모가 symlink라 거부: {raw_dir}")
    if not stat.S_ISDIR(raw_dir_stat.st_mode):
        raise NotADirectoryError(f"fill raw 부모가 디렉터리가 아님: {raw_dir}")
    os.chmod(raw_dir, 0o700)

    prefix = f"pm_import_fill_{harness}_{datetime.datetime.now():%Y%m%d_%H%M%S}_"
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = None
    raw_basename = ""
    for _attempt in range(100):
        raw_basename = f"{prefix}{secrets.token_hex(4)}.txt"
        try:
            fd = os.open(raw_dir / raw_basename, file_flags, 0o600)
            break
        except FileExistsError:
            continue
    if fd is None:
        raise FileExistsError("fill raw 고유 파일명 생성 100회 충돌")
    raw_path = raw_dir / raw_basename
    try:
        _assert_raw_dir_contained()
    except BaseException:
        os.close(fd)
        try:
            os.unlink(raw_path)
        except OSError:
            pass
        raise
    with os.fdopen(
            fd, "w", encoding="utf-8", errors="replace", newline="\n") as handle:
        handle.write(output)
    return dest_root.joinpath(*raw_parts, raw_basename)


def _real_harness_runner(
    argv: list[str], prompt: str, cwd: Path | str | None = None
) -> tuple[bool, str]:
    """실 하니스 바이너리를 subprocess 로 구동(fail-soft). 반환: (성공 여부, stdout).

    external_review.run_reviewer 선례 — 예외를 raise 하지 않고 (False, 에러텍스트)로 감싼다.
    프롬프트는 argv 마지막 인자로 전달한다(claude -p "<prompt>" / opencode run "<prompt>" ...).

    SF: cwd 가 주어지면 *대상 repo* 에서 구동한다(run_fill 이 dest_root 를 바인딩). 호출자
    cwd 가 아니라 import 대상에서 돌아야 하니스의 작업 디렉토리·파일 접근이 분석 대상과 맞는다.

    세 하네스 모두 pm_relay 공용 워치독을 경유한다. 하네스별 차이는 코드 분기가 아니라
    HARNESS_PROFILES 선언(startup 감시 여부·진행 신호·idle/wall 상한)이며 대상 repo local.conf
    override도 같은 해소기를 탄다. startup stall만 선언된 유한 재시도를 하고, idle/wall kill은
    중복 과금·외부 전송을 피하려 자동 재시도하지 않은 채 부분 산출물과 함께 fail-soft 한다.

    codex 경로(`codex exec …`)는 **빈 stdin PIPE를 즉시 닫아 EOF를 전달**한다 — stdin 미닫힘 시
    "Reading additional input from stdin..." 로 무기한 대기. stdin·진행 신호 정책은 실제 argv와
    함께 FILL_DRIVER_BY_CMD에 선언돼 하네스 판정 코드와 분리된다."""
    try:
        engine = _load_watchdog()
    except Exception as exc:  # noqa: BLE001 — 일반 로드 실패만 fill fail-soft.
        if _is_engine_rev_skew(exc):
            raise
        return False, _fill_failure_with_partial(f"[하니스 워치독 로드 오류: {exc}]", exc)

    try:
        harness, emits_progress, input_text = _fill_driver(argv)
        profile = engine.resolve_harness_profile(harness, _fill_local_config(cwd))
        progress_signal = (
            profile.progress_signal if emits_progress else engine.PROGRESS_SIGNAL_NONE
        )
        idle_timeout = engine.idle_timeout_for_signal(
            progress_signal, profile.idle_timeout)
        first_event_timeout = (
            engine.first_event_timeout_default() if profile.startup_watchdog else None
        )
        retries = engine.stall_retries_default() if profile.startup_watchdog else 0
        idle_label = f"{idle_timeout:.0f}초" if idle_timeout is not None else "비활성(증분 신호 없음)"
        print(
            f"[fill auto] 실행 상한: idle={idle_label} · wall={profile.wall_timeout:.0f}초 "
            "(중단: Ctrl-C)",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — 일반 프로필 준비 실패만 fill fail-soft.
        if _is_engine_rev_skew(exc):
            raise
        return False, _fill_failure_with_partial(f"[하니스 프로필 준비 오류: {exc}]", exc)

    try:
        result = engine.run_with_first_event_watchdog(
            argv,
            first_event_timeout=first_event_timeout,
            overall_timeout=profile.wall_timeout,
            retries=retries,
            idle_timeout=idle_timeout,
            cwd=str(cwd) if cwd is not None else None,
            input_text=input_text,
        )
    except subprocess.TimeoutExpired as exc:
        wall_axis = getattr(engine, "TIMEOUT_AXIS_WALL", "wall")
        idle_axis = getattr(engine, "TIMEOUT_AXIS_IDLE", "idle")
        axis = getattr(exc, "timeout_axis", wall_axis)
        threshold = float(getattr(exc, "threshold_seconds", exc.timeout))
        silence = getattr(exc, "silence_seconds", None)
        silence_label = f" · 실측 침묵 {silence:.0f}초" if silence is not None else ""
        kind = "무진행 임계" if axis == idle_axis else "벽시계 백스톱"
        return False, _fill_failure_with_partial(
            f"[하니스 타임아웃 — {kind} {threshold:.0f}초 발화{silence_label}; "
            "자동 재시도 안 함 — 출력 확인 후 수동 재시도]",
            exc,
        )
    except FileNotFoundError:
        return False, f"[하니스 명령 '{argv[0] if argv else '?'}' 를 찾을 수 없음 — 설치/PATH 확인]"
    except Exception as exc:  # noqa: BLE001 — fail-soft: 어떤 예외도 import 를 깨지 않는다.
        if _is_engine_rev_skew(exc):
            raise
        if isinstance(exc, engine.StallWatchdogError):
            axis = getattr(exc, "timeout_axis", None)
            threshold = getattr(exc, "threshold_seconds", None)
            threshold_label = f" {float(threshold):.0f}초" if threshold is not None else ""
            first_event_axis = getattr(engine, "TIMEOUT_AXIS_FIRST_EVENT", "first-event")
            idle_axis = getattr(engine, "TIMEOUT_AXIS_IDLE", "idle")
            if axis == first_event_axis:
                kind = f"첫-이벤트 stall{threshold_label}"
            elif axis == idle_axis:
                kind = f"무진행 임계{threshold_label}"
            else:
                kind = f"벽시계 백스톱{threshold_label}"
            return False, _fill_failure_with_partial(
                f"[하니스 {kind} — 재시도 소진(fail-soft): {exc}]", exc)
        return False, _fill_failure_with_partial(f"[하니스 실행 오류: {exc}]", exc)

    try:
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return result.returncode == 0, output
    except Exception as exc:  # noqa: BLE001 — 결과 정규화 실패도 fill fail-soft.
        if _is_engine_rev_skew(exc):
            raise
        return False, _fill_failure_with_partial(f"[하니스 결과 처리 오류: {exc}]", exc)


def _build_fill_prompt(dest_root: Path, tokens: list[str]) -> str:
    """대상 repo 분석 → 자유서술 placeholder 추출 프롬프트(하니스 구동용).

    프롬프트 품질이 산출을 좌우하나 초안일 뿐(확정 아님). 토큰별로 무엇을 채울지 지시한다.
    """
    token_lines = "\n".join(f"  - {t}" for t in tokens)
    return (
        f"이 저장소({dest_root})를 분석해 PM 프레임워크 자유서술 placeholder 를 채울 초안을 제안하라.\n"
        f"확정이 아니라 사람이 검토할 *초안*이다. 다음 placeholder 각각에 대해 한국어로 제안하라:\n"
        f"{token_lines}\n\n"
        f"  - {{{{PROJECT_CONSTRAINTS}}}}: 이 프로젝트의 아키텍처 불변식·금지(핵심 결정 경계 등).\n"
        f"  - {{{{PROTECTED_PATHS}}}}: code author + ADR 없이 건드리면 안 되는 파일/디렉토리.\n"
        f"  - {{{{USER_GATE_ITEMS}}}}: PM 자율 결정 밖 — 사용자 사전 동의가 필요한 행위.\n"
        f"불확실하면 빈 항목으로 두고 사람이 채우도록 TODO 를 남겨라."
    )


def _build_runner_argv(
    harness: str, prompt: str, dest_root: Path | None = None
) -> list[str]:
    """하니스별 헤드리스 구동 명령 조립(runner 매핑·명시 등록).

      claude   → `claude -p "<p>"`
      opencode → `opencode run "<p>" --format json` (token/cost 파싱 위해 json 출력)
      codex    → `codex exec --json -s workspace-write --skip-git-repo-check [-C <dest>] "<p>"`
                 (프롬프트=마지막 positional·빈 stdin PIPE EOF는 _real_harness_runner가 부여)

    미지원 harness 는 **fail-loud**(ValueError). 과거 codex 가 이 매핑에 없어 조용히 `claude -p`
    로 폴백해 *잘못된 바이너리*를 호출하고 출력만 harness=codex 로 오표기하던 클래스를 닫는다 —
    모르는 harness 는 명시 등록을 강제(silent 폴백 금지)."""
    if harness == "claude":
        return [*CLAUDE_FILL_CMD, prompt]
    if harness == "opencode":
        return [*OPENCODE_FILL_CMD, prompt, "--format", "json"]
    if harness == "codex":
        argv = [*CODEX_FILL_CMD, "--json", "-s", "workspace-write", "--skip-git-repo-check"]
        if dest_root is not None:
            argv += ["-C", str(dest_root)]     # workdir 핀(codex 는 -C 로 작업 디렉토리 고정)
        argv.append(prompt)                    # 프롬프트 = 마지막 positional
        return argv
    supported = "·".join(FILL_CAPABLE_HARNESSES)
    raise ValueError(
        f"_build_runner_argv: 미지원 fill harness {harness!r} — 지원: {supported}. "
        f"silent 폴백 금지(잘못된 바이너리 호출·오표기 방지·runner 매핑에 명시 등록 필요)."
    )


def _parse_opencode_json(output: str) -> str:
    """opencode `--format json` 출력에서 결과 텍스트를 추출(token/cost 파싱은 부수적).

    opencode 출력 형태는 버전에 따라 다를 수 있어 보수적으로 흔한 모양을 훑는다:
      - 최상위 'result'/'text'/'output' 문자열
      - 메시지 parts 배열의 text part 들 (parts[].text)
    파싱 실패하면 원문을 그대로 반환(fail-soft — 사람이 읽을 수 있게)."""
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, ValueError):
        return output
    if isinstance(data, dict):
        for key in ("result", "text", "output", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        parts = data.get("parts")
        if isinstance(parts, list):
            texts = [p["text"] for p in parts
                     if isinstance(p, dict) and isinstance(p.get("text"), str)]
            if texts:
                return "\n".join(texts)
    return output


def _parse_codex_json(output: str) -> str:
    """codex `exec --json` JSONL 출력에서 최종 응답 텍스트 추출(fail-soft·_parse_opencode_json 동형).

    codex 는 줄당 JSON 이벤트(thread.started·turn.started·item.*·turn.completed·error)를 낸다.
    응답 = `item.type == "agent_message"` 인 item 의 `.text`(마지막 것 = 최종 응답·엔진
    pm_relay.parse_codex_json 규칙과 정합). JSON 파싱 불가·구조 상이·미발견이면 원문 그대로
    반환(사람이 읽게 — _parse_opencode_json 과 같은 fail-soft). pm_import 자족(형제 파서 미러·
    _parse_opencode_json 선례 + 시그니처/목적 상이 — pm_relay 쪽은 orchestration 용 3-튜플
    (thread_id·reply·usage)·여긴 fill 초안용 str·fail-soft. 순환 import 는 어느 쪽이든 없음)."""
    reply = ""
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(evt, dict):
            continue
        item = evt.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                reply = text  # 최종(마지막) agent_message 로 갱신.
    return reply if reply else output


def _live_harness_allowed(mode: str) -> bool:
    """opt-in 게이트: 실 하니스 호출은 PM_IMPORT_LIVE_HARNESS=1 AND --fill auto 동시 충족 시만.

    둘 중 하나라도 없으면 False → run_fill 이 실 runner 를 호출하지 않고 stub/manual 로 폴백.
    """
    return mode == "auto" and os.environ.get(LIVE_HARNESS_ENV, "").strip() in ("1", "true", "yes", "on")


def _load_repo_owned_files():
    """공용 repo 소유 파일 열거 seam을 검증 소비자로 로드한다."""
    try:
        helper_path = Path(__file__).resolve().with_name("engine_rev.py")
        helper = _load_module_from_path(
            helper_path, "engine_rev.py", allow_unverified=True,
        )
        return helper.load_repo_owned_files(
            Path(__file__).resolve().with_name("repo_owned_files.py"),
            verifier=_verify_engine_rev,
        )
    except Exception as exc:
        if _is_engine_rev_skew(exc):
            raise
        raise RuntimeError(
            "repo_owned_files.py를 로드할 수 없음 — 엔진 사본을 pm-update로 재동기화하라"
        ) from exc


def _resolve_fill_scope(dest_root: Path, copied_relpaths: set[Path] | None) -> set[Path]:
    """fill 스캔 대상 relpath set 을 결정한다.

    copied_relpaths 가 주어지면(main 경로) 그걸 그대로 쓴다 — 이번 import 가 복사한 파일만
    스캔(비파괴). None 이면(run_fill/_run_manual_fill 직접 호출 — 테스트·디버그) dest 트리
    전체를 폴백 스캔한다. 단, --into main 경로는 *항상* copied_relpaths 를 넘기므로 사용자
    파일 오염은 발생하지 않는다(이 폴백은 직접 호출자 편의용). 폴백도 COPY_EXCLUDE_DIR_NAMES
    (node_modules·__pycache__·.git)는 제외한다.
    """
    if copied_relpaths is not None:
        return copied_relpaths
    # 실 import 경로는 항상 copied_relpaths를 넘기므로 여기의 dest 전수 열거는 직접 호출용 폴백에
    # 한정된다. adopter가 아직 git repo가 아니면 seam이 filesystem 강등 경고를 1회 표면화한다.
    # 정상 import에는 노이즈가 없고, 직접 호출자는 추적/ignore 보장 소실을 놓치지 않는다.
    # 이 소비점의 0건은 "채울 파일 없음"이라는 정당 결과이므로 출하용 빈 인벤토리 예외에서 제외한다.
    repo_files = _load_repo_owned_files()
    scope: set[Path] = set()
    for rel in repo_files.list_repo_owned_files(
            dest_root, ".", mode=repo_files.OWNED):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        # dest fallback은 텍스트 읽기/수정 소비처다. seam의 gap domain은 symlink/gitlink를
        # 보존하지만 여기서는 링크 추종(특히 repo 밖)과 디렉토리/gitlink·삭제 엔트리를 제외한다.
        path = dest_root / rel
        if path.is_symlink() or not path.is_file():
            continue
        scope.add(rel)
    return scope


def _iter_copied_files(dest_root: Path, copied_relpaths: set[Path],
                       swapped: list[str] | None = None,
                       vanished: list[str] | None = None):
    """이번 import 가 복사한 파일들만 (relpath, 절대경로)로 순회한다.

    MF(비파괴): fill 단계가 dest_root.rglob 로 *대상 프로젝트 전체* 를 훑으면, --into 에서
    이번 import 가 복사하지 *않은* 기존 사용자 파일(우연히 sentinel 포함)에도 TODO 마커가
    주입되어 비파괴 보장(substitute_placeholders 가 copied_relpaths 로 한정)과
    충돌한다. 따라서 fill 도 substitute_placeholders 와 *동일한* copied_relpaths set 만
    대상으로 한다 — 복사 안 한 사용자 파일은 절대 스캔/수정하지 않는다. node_modules·
    __pycache__·.git 등은 애초에 복사 목록에 없어 자연히 제외된다.
    """
    for rel in sorted(copied_relpaths):
        if any(part in COPY_EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        # 선검사는 `lstat` — symlink 를 후보로 통과시켜 소비처의 nofollow fd 가드가 판정하게 한다
        #   (`is_file()` 은 깨진 링크를 조용히 제외하고 정상 링크는 따라간다). 탈락 사유(복사 뒤
        #   삭제·형상 변화)는 호출부 목록에 실어 요약으로 나가게 한다(조용한 skip 0).
        anomaly = _copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            if vanished is not None and rel.as_posix() not in vanished:
                vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            if swapped is not None and rel.as_posix() not in swapped:
                swapped.append(rel.as_posix())
            continue
        yield rel, dest_root / rel


def _fill_targets(dest_root: Path, copied_relpaths: set[Path] | None = None,
                  root_identity: tuple | None = None) -> list[str]:
    """이번 import 가 복사한 파일에 실제로 남아있는 자유서술 placeholder 토큰 목록.

    잔존 grep 으로 판정 — 트리에 없는 토큰은 채울 필요 없음. 스캔 범위는 copied_relpaths
    (이번 run 복사 파일)로 한정 — 사용자 파일 불가침(비파괴). None 이면 dest 트리 전체 폴백
    (직접 호출용 — COPY_EXCLUDE_DIR_NAMES 제외).

    {{OPENCODE_PRO_MODEL}} 는 LLM fill 후보가 *아니다* — resolve_opencode_model 의
    결정적 `opencode models` 조회가 전담한다(환각·미가용 모델 추측 제거). 여기서는 자유서술
    3종(FREE_FORM_TOKENS)만 본다.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    present: list[str] = []
    swapped: list[str] = []
    vanished: list[str] = []
    for token in FREE_FORM_TOKENS:
        if _token_present(dest_root, token, scan, root_identity=root_identity,
                          swapped=swapped, vanished=vanished):
            present.append(token)
    _report_copied_scope_anomalies("자유서술 토큰 판정", swapped, vanished)
    return present


def _plan_fill_targets(actions: list[CopyAction]) -> list[str]:
    """dry-run 계획용: 복사될 *소스* 파일들에 남아있는 자유서술 토큰 목록.

    MF2: --dry-run 은 파일을 복사하지 않으므로 dest 트리에 토큰이 없다. 그래서 복사 *예정*인
    src 파일(actions[].src)을 직접 읽어 무엇을 채우게 될지 계획을 만든다. 실 fill(_fill_targets)
    이 copied dest 파일에서 보는 것과 동일한 후보 토큰 집합을 source 측에서 미리보기한다.

    {{OPENCODE_PRO_MODEL}} 는 fill 후보에서 분리(결정적 resolve_opencode_model 전담)
    되므로 여기서도 자유서술 3종만 본다. 모델 토큰 계획은 _plan_opencode_model_targets 가 별도.
    """
    present: list[str] = []
    for token in FREE_FORM_TOKENS:
        for action in actions:
            if _is_engine_source(action.dst):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님
                continue
            try:
                if token in _read_text_shared(action.src, encoding="utf-8"):
                    present.append(token)
                    break
            except (UnicodeDecodeError, OSError):
                continue
    return present


def _plan_opencode_model_targets(actions: list[CopyAction]) -> bool:
    """dry-run 계획용: 복사 *예정* src 에 {{OPENCODE_PRO_MODEL}} 토큰이 잔존하는가(opencode 트리).

    실 단계(resolve_opencode_model)가 dest 복사본에서 토큰 잔존을 보고 동작 여부를 정하는데,
    dry-run 은 복사를 안 하므로 src 측에서 미리 본다(_plan_fill_targets 와 같은 결).
    """
    for action in actions:
        if _is_engine_source(action.dst):  # 엔진 소스(.py) 주석의 모델-토큰 문서는 placeholder 아님
            continue
        try:
            if OPENCODE_MODEL_TOKEN in _read_text_shared(action.src, encoding="utf-8"):
                return True
        except (UnicodeDecodeError, OSError):
            continue
    return False


def _token_present(
    dest_root: Path,
    token: str,
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
    swapped: list[str] | None = None,
    vanished: list[str] | None = None,
) -> bool:
    """이번 import 가 복사한 파일에 token 이 한 파일이라도 남아있는가(비파괴 범위 한정).

    copied_relpaths=None 이면 dest 트리 전체 폴백(COPY_EXCLUDE_DIR_NAMES 제외) — 직접 호출용.

    swapped/vanished: 계획 뒤 교체됐거나 사라져 **읽지 못한** relpath 를 담을 리스트(호출부가
    loud 로 보고). 삼키면 "토큰 없음"으로 오판정해 모델 해소가 inactive 로 빠지거나 fill 대상이
    조용히 준다 — 판정 채널이라고 침묵하면 안 되는 이유다(제외는 하되 반드시 알린다). 삭제 경쟁을
    일반 OSError 로 묶으면 그 축만 무요약이 되므로 교체와 같은 격으로 가른다."""
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    for _rel, _path in _iter_copied_files(
            dest_root, scan, swapped=swapped, vanished=vanished):
        if _is_engine_source(_rel):  # 엔진 소스 주석의 토큰-문서는 placeholder 아님
            continue
        try:
            if token in read_dest_text(dest_root, _rel, root_identity=root_identity):
                return True
        except UnsafeDestPathError:
            if swapped is not None and _rel.as_posix() not in swapped:
                swapped.append(_rel.as_posix())
            continue
        except FileNotFoundError:
            if vanished is not None and _rel.as_posix() not in vanished:
                vanished.append(_rel.as_posix())
            continue
        except (UnicodeDecodeError, OSError):
            continue
    return False


def _mark_todos(
    dest_root: Path,
    tokens: list[str],
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
) -> list[str]:
    """manual 모드: 자유서술 placeholder 옆에 `<!-- TODO -->` 가 없으면 표시한다.

    템플릿은 대개 이미 placeholder 아래에 TODO 주석을 둔다. 여기서는 토큰을
    `<!-- TODO: 손으로 채우세요 -->` 인라인으로 *치환*하지 않고, 토큰 줄에 TODO 마커가 없을
    때만 토큰 뒤에 인라인 마커를 덧붙여(비파괴) 채택자에게 손작업 지점을 명시한다.
    실제로 마커를 추가한 토큰 목록을 반환한다.

    스캔 범위는 copied_relpaths(이번 run 복사 파일)로 한정 — 복사하지 않은 사용자 파일에는
    절대 마커를 주입하지 않는다(비파괴 보장). None 이면 dest 트리 전체 폴백(직접 호출용).
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    marked: set[str] = set()
    swapped: list[str] = []
    vanished: list[str] = []
    marker = " <!-- TODO: 손으로 채우세요 -->"
    for _rel, _path in _iter_copied_files(
            dest_root, scan, swapped=swapped, vanished=vanished):
        if _is_engine_source(_rel):  # 엔진 소스(.py)에 TODO 마커 주입 금지 — verbatim
            continue
        try:
            text = read_dest_text(dest_root, _rel, root_identity=root_identity)
        except UnsafeDestPathError:
            swapped.append(_rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(_rel.as_posix())  # 읽기 전 삭제도 loud 제외(쓰기 쪽과 동형).
            continue
        except (UnicodeDecodeError, OSError):
            continue
        new_text = text
        changed = False
        # 이 파일에서 마킹한 토큰은 **쓰기 성공 뒤에** 반영한다 — 계획 시점에 더하면 교체·삭제로
        #   제외된 파일의 토큰이 "표시됨"으로 보고돼 사람이 없는 처리 결과를 믿는다.
        pending: set[str] = set()
        for line in text.splitlines(keepends=True):
            for token in tokens:
                if token in line and "TODO" not in line:
                    replacement = line.replace("\n", "") + marker + ("\n" if line.endswith("\n") else "")
                    new_text = new_text.replace(line, replacement, 1)
                    pending.add(token)
                    changed = True
        if changed and new_text != text:
            try:
                write_dest_text(dest_root, _rel, new_text, root_identity=root_identity)
            except UnsafeDestPathError:
                swapped.append(_rel.as_posix())
                continue
            except FileNotFoundError:
                vanished.append(_rel.as_posix())
                continue
            marked.update(pending)
    _report_copied_scope_anomalies("자유서술 TODO 표시", swapped, vanished)
    return sorted(marked)


def run_fill(
    dest_root: Path,
    harness: str,
    *,
    live: bool,
    runner: HarnessRunner | None = None,
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
) -> FillResult:
    """자유서술 placeholder 채움 단계. board init 직후 hook.

    dest_root: import 된 대상 트리. harness: 구동 하니스('claude'|'opencode').
    live=True  → 실 하니스 호출 시도(opt-in 게이트 통과 — main 이 _live_harness_allowed 로 판정).
    live=False → runner 미호출(stub 경로). runner 가 주입되면(테스트 stub) live=True 와 무관하게
                 그 runner 로 명령을 조립·호출해 *명령 조립* 만 검증한다(토큰 0).
    copied_relpaths: 이번 import 가 복사한 파일 relpath set — fill 스캔 범위를 이 파일들로
                 한정한다(비파괴 보장). None 이면 dest 트리 전체를 스캔(직접 호출용
                 폴백) — main 은 항상 substitute_placeholders 와 동일 set 을 전달한다.

    규격(ticket §인터페이스): live=False 면 runner 를 호출하지 않고 stub/manual 경로로 간다.
    여기서 '하니스 미구동'은 *실 바이너리* 미구동을 뜻한다 — 주입 runner(stub)는 항상 안전.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan, root_identity=root_identity)

    # manual(또는 채울 토큰 없음): 하니스 미구동 — TODO 표시만.
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")

    prompt = _build_fill_prompt(dest_root, tokens)
    argv = _build_runner_argv(harness, prompt, dest_root)   # codex 는 -C <dest> workdir 핀 필요

    # 실 runner 결정: 주입 stub 이 있으면 그걸(테스트). 없고 live 면 실 바이너리. 아니면 미구동.
    # SF: 실 바이너리는 대상 repo(dest_root)에서 구동되도록 cwd 를 바인딩한다(호출자 cwd 아님).
    effective_runner: HarnessRunner | None
    if runner is not None:
        effective_runner = runner
    elif live:
        effective_runner = functools.partial(_real_harness_runner, cwd=dest_root)
    else:
        effective_runner = None

    if effective_runner is None:
        # stub/실호출 모두 없음 → 자유서술 placeholder 값을 채우지 않고 제안만 비움.
        # (main 은 manual 또는 게이트 미통과 시 이 경로 대신 _run_manual_fill 을 부른다.)
        return FillResult(
            mode="auto",
            harness=harness,
            live=False,
            note="하니스 미구동(게이트 미통과·stub 없음) — 제안 없음. manual 폴백 권장.",
        )

    ok, output = effective_runner(argv, prompt)
    result = FillResult(mode="auto", harness=harness, live=live)
    result.runner_calls.append(list(argv))
    if not ok:
        try:
            raw_path = _save_fill_failure_output(dest_root, harness, output)
            shown_path = raw_path.relative_to(dest_root)
            result.note = (
                "하니스 구동 실패(fail-soft) — 제안 없음. "
                f"부분/오류 출력 원문 보존: {shown_path}"
            )
        except Exception as exc:  # noqa: BLE001 — raw 박제 실패도 import를 깨지 않는 fail-soft 경계.
            # 박제가 실패해도 import는 깨지 않되 진단까지 잃지는 않는다. 이 경로는 의도적으로
            # preview 절단을 하지 않아 stdout/stderr 전문이 사람에게 도달한다.
            result.note = (
                f"하니스 구동 실패(fail-soft) — raw 저장 실패({exc}); 제안 없음. "
                f"출력 전문:\n{output}"
            )
        return result

    # 하니스별 응답 파싱: opencode=--format json·codex=exec --json JSONL(agent_message)·claude=평문.
    if harness == "opencode":
        text = _parse_opencode_json(output)
    elif harness == "codex":
        text = _parse_codex_json(output)
    else:
        text = output

    # 산출 텍스트 = 사람이 검토할 placeholder 값 제안. 각 토큰에 동일 출력을 후보로 매핑한다
    # (정밀 파싱은 모델 출력 형식에 의존 — 초안 전제라 통째로 제안하고 사람이 분배·편집).
    for token in tokens:
        result.values[token] = text
    result.drafts["(harness-output)"] = text
    result.note = "하니스 구동 제안 — 사람 검토 후 손으로 반영(자동 확정 아님)."
    return result


def _run_manual_fill(
    dest_root: Path,
    copied_relpaths: set[Path] | None = None,
    root_identity: tuple | None = None,
) -> FillResult:
    """manual 모드(기본): 하니스 미구동. 자유서술 placeholder 에 TODO 마커 표시만.

    copied_relpaths: 이번 import 가 복사한 파일 relpath set — TODO 마킹 범위를 이 파일들로
    한정한다(비파괴). None 이면 dest 트리 전체 폴백(직접 호출용). main 은 항상 전달한다.
    """
    scan = _resolve_fill_scope(dest_root, copied_relpaths)
    tokens = _fill_targets(dest_root, scan, root_identity=root_identity)
    if not tokens:
        return FillResult(mode="manual", note="자유서술 placeholder 가 트리에 없음 — 처리 불필요.")
    marked = _mark_todos(dest_root, tokens, scan, root_identity=root_identity)
    return FillResult(
        mode="manual",
        todos=marked,
        note="자유서술 placeholder 를 TODO 로 표시 — 채택자가 손으로 채운다(하니스 미구동).",
    )


def ensure_pm_playbook_local_stub(dest_root: Path, backup_root: Path | None,
                                  root_identity: tuple | None = None) -> str:
    """pm_playbook.local.md 스텁을 dest 에 생성한다.

    backup_root: 중앙 백업 디렉토리(또는 None=--new). 이 함수는 기존 .local 을
    *덮지 않고 보존(skip)* 하므로 백업할 원본 변경이 없다 — backup_root 는 시그니처 일관성을
    위해 받지만 실제로 사용하지 않는다(미생성 = 백업 불요).

    fill 단계와 같은 자리(board init·conf sync 직후)에서 호출 — pm_role.local 초안 처리와
    같은 결의 인스턴스-소유 문서다. 루트 .local은 manifest 밖이라 템플릿 복사로 안 오니
    여기서 PM_PLAYBOOK_LOCAL_STUB(단일 소스 인라인 상수)로 *생성*한다.

    비파괴(재-import): 기존 pm_playbook.local.md 가 있으면 덮지 않는다 — 인스턴스가 누적한
    wave 학습이 손실되면 안 된다(local.conf 백업 철학 MF1·_backup_existing_local_conf 와 동일).
    구현은 *skip* — 기존 .local 은 manifest 밖·인스턴스 소유라 import 산출 백업과 별개로
    그대로 보존한다(누적 학습 trail 은 사용자 VCS 가 이력 관리). 백업하지 않는 이유: 백업은
    "import 가 덮는 파일"을 위한 것인데 여기선 애초에 덮지 않으므로 백업할 원본 변경이 없다.

    쓰기는 **dest 상대 fd 순회**로 한다 — 이 스텁 자리의 조상(`.project_manager/wiki`)이 저장소 밖
    지향 링크로 바뀌어 있으면 경로 `mkdir`+`write_text` 가 링크를 따라 밖에 파일을 만든다. 복사
    단계가 파일 단위 제외로 **계속 진행**하게 된 뒤로는 그 형상에서도 이 지점에 도달하므로, 여기도
    같은 fd 규율을 탄다(생성 실패는 loud skip — 스텁은 설치 성공 조건이 아니다).

    반환값(사람 대상 상태):
      "created" — 새 스텁 생성.
      "preserved" — 기존 .local 발견·비파괴 보존(미생성).
      "unsafe-skip" — 경로가 안전하지 않아 생성하지 않음(저장소 밖 쓰기 차단).
    """
    rel = Path(PM_PLAYBOOK_LOCAL_RELPATH)
    if not _is_safe_dest_path(dest_root, rel.parent):
        # 조상이 저장소 밖을 향하면 그 아래를 **보지도 만들지도 않는다** — 밖의 동명 파일을
        #   "기존 보존" 으로 오보고하지도, 밖에 스텁을 만들지도 않는다.
        print(f"  ⚠️ pm_playbook.local.md 스텁 자리의 조상 경로가 안전하지 않아 생략합니다 "
              f"({rel.as_posix()}) — 저장소 밖 쓰기를 피합니다.", file=sys.stderr)
        return "unsafe-skip"
    if os.path.lexists(dest_root / rel):
        # 비파괴: 인스턴스 소유 누적 학습 보존(덮지 않음·skip). 링크로 바뀌어 있어도 그 자리를
        #   새로 만들지 않는다(`lexists` — 깨진 링크도 점유로 본다).
        return "preserved"
    try:
        _ensure_dest_dir_nofollow(dest_root, rel.parent, root_identity=root_identity)
        with _fdopen_text(
                _open_dest_relative_nofollow(
                    dest_root, rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    root_identity=root_identity, regular_only=True),
                "w", newline="\n") as handle:
            handle.write(PM_PLAYBOOK_LOCAL_STUB)
    except (OSError, UnsafeDestPathError) as exc:
        print(f"  ⚠️ pm_playbook.local.md 스텁을 만들지 못했습니다 "
              f"({rel.as_posix()}: {exc}) — 저장소 밖 쓰기를 피해 생략합니다.", file=sys.stderr)
        return "unsafe-skip"
    return "created"


def _harness_binary_available(harness: str) -> bool:
    """등록 fill 하니스와 동명의 실 바이너리가 PATH 에 있는가.

    shutil.which 로 탐지한다. 테스트는 monkeypatch(pm_import.shutil.which) 또는 PATH 조작으로
    부재를 stub 한다. registry에 없는 이름은 새 하네스의 등록 누락/호출부 오류이므로 조용히
    첫 하네스로 폴백하지 않고 fail-loud 한다. 등록됐지만 바이너리가 없는 것은 정상적인
    환경 가용성 판정(False)이며 ``_resolve_fill_harness``가 다음 등록 후보를 탐색한다.
    """
    if harness not in HARNESS_TEMPLATE_DIRS:
        raise ValueError(
            f"미등록 fill harness의 바이너리를 판정할 수 없습니다: {harness!r} — "
            f"지원: {', '.join(HARNESS_TEMPLATE_DIRS)}"
        )
    return shutil.which(harness) is not None


def _resolve_fill_harness(
    fill_harness_arg: str | None,
    harness: str | tuple[str, ...] | list[str],
) -> str:
    """fill 구동 하네스 결정. 명시값 우선, 없으면 선택 집합의 첫 가용 하네스를 쓴다.

    registry 정규 순서가 결정적 우선순위다(현재 claude→opencode→codex). 선택된 바이너리가 모두
    없으면 첫 하네스를 그대로 반환해 상위 opt-in 게이트/manual 폴백이 명확히 진단하게 한다.
    """
    if fill_harness_arg:
        return fill_harness_arg
    selected = (
        parse_harness_selection(harness)
        if isinstance(harness, str)
        else tuple(harness)
    )
    if len(selected) == 1:
        return selected[0]
    for candidate in selected:
        if _harness_binary_available(candidate):
            return candidate
    return selected[0]


def _print_fill_result(result: FillResult, dry_run: bool) -> None:
    """fill 결과를 사람 대상으로 출력. auto 제안은 *적용 안 함* — 사람 검토 전제."""
    if result.mode == "manual":
        if result.todos:
            print(f"✓ 자유서술 placeholder TODO 표시: {'·'.join(result.todos)} "
                  f"(채택자가 손으로 채웁니다).")
        else:
            print(f"  fill(manual): {result.note}")
        return
    # auto
    print(f"[fill auto] harness={result.harness}  live={result.live}")
    if result.runner_calls:
        for call in result.runner_calls:
            print(f"  구동 명령: {shlex.join(call) if hasattr(shlex, 'join') else ' '.join(call)}")
    if result.values:
        print("  제안된 자유서술 placeholder 값 (검토 후 손으로 반영 — 자동 확정 아님):")
        for token, value in result.values.items():
            preview = value.strip().splitlines()[0][:80] if value.strip() else "(빈 제안)"
            print(f"    {token} → {preview}")
        if dry_run:
            print("  [dry-run] 제안만 출력 — 파일 미변경.")
    if result.note:
        print(f"  메모: {result.note}")


# ── 모드 준비 (--new / --into) ─────────────────────────────────────────────

def resolve_template_roots(
    source_root: Path,
    harness: str | tuple[str, ...] | list[str],
) -> list[Path]:
    """선택 집합에 대응하는 ``templates/<harness>/`` 경로들. 없으면 fail-loud."""
    selected = (
        parse_harness_selection(harness)
        if isinstance(harness, str)
        else tuple(harness)
    )
    roots: list[Path] = []
    for harness_name in selected:
        for name in HARNESS_TEMPLATE_DIRS[harness_name]:
            root = source_root / "templates" / name
            if not root.is_dir():
                raise FileNotFoundError(
                    f"소스 어댑터 트리 없음: {root}. "
                    f"--from 이 올바른 프레임워크 checkout 인지 확인하라 "
                    f"(templates/{name}/ 필요). 또는 --harness claude 처럼 설치할 하네스를 "
                    f"좁혀라."
                )
            roots.append(root)
    return roots


def _has_harness_templates(root: Path, harness: str) -> bool:
    """root 가 harness 의 어댑터 소스 트리(templates/<dir>/…)를 전부 보유하는가."""
    return all(
        (root / "templates" / name).is_dir()
        for name in HARNESS_TEMPLATE_DIRS[harness]
    )


def _resolve_add_harness_source(
    dest_root: Path, harness: str, explicit: Path | None,
) -> Path:
    """add_harness 의 어댑터 소스 checkout 을 해소한다.

    imported 인스턴스(scoped-core 사본·`templates/` 부재)는 dest 안에 어댑터
    소스 트리가 없다 — 소스는 그 인스턴스의 **upstream 프레임워크 checkout** 이다(add-harness 를
    *라이브 인스턴스*에 걸면 소스는 항상 그 인스턴스의 upstream 이다). 해소 우선순위:
      1. explicit(`--from`)      → 그대로 (기존 계약·override).
      2. dest local.conf upstream.path → classify_upstream=path 이고 그 경로에 templates/<harness>/
                                    가 있으면 소스.
      3. dest 자신              → dest 에 templates/ 가 있으면 dest (framework-checkout 자기전환·
                                    REPO 하드 기본이 맞던 유일 케이스·현행 회귀 보존).
      4. 전부 실패              → 친화 FileNotFoundError (actionable).

    URL upstream 은 이번 스코프 밖 — 엔진은 로컬 파일만 복사(git clone/fetch 안 함)
    하므로 path upstream 만 자동 해소하고 URL 은 skip 해 말단(dest 자기전환 또는 친화 에러)으로
    유도한다(명시 `--from` 요구). classify_upstream 으로 분기.
    """
    if explicit is not None:
        return Path(explicit).resolve()
    dest_root = Path(dest_root).resolve()

    # dest 는 엔진 사본(templates 부재) — 소스는 upstream 프레임워크 checkout.
    local_conf = dest_root / ".project_manager" / "local.conf"
    if local_conf.is_file():
        try:
            upstream = _parse_conf_keys(
                _read_text_shared(local_conf, encoding="utf-8")).get("upstream.path", "").strip()
        except (UnicodeDecodeError, OSError):
            upstream = ""
        # path upstream 만 자동 해소 — URL 은 로컬 파일 소스가 아니므로 skip(--from 요구).
        if upstream and classify_upstream(upstream) == "path":
            # 상대 경로는 인스턴스 루트(dest) 기준·절대면 그대로(pathlib: 절대 우변 승).
            candidate = (dest_root / upstream).resolve()
            if _has_harness_templates(candidate, harness):
                return candidate

    # framework-checkout 자기전환(dest 에 templates/ 보유) — 현행 REPO 하드 기본의 회귀 보존.
    if _has_harness_templates(dest_root, harness):
        return dest_root

    raise FileNotFoundError(
        f"add_harness 소스 미해소: {dest_root} 에 templates/ 가 없고, local.conf upstream.path 도 "
        f"templates/<harness> 를 가진 로컬 프레임워크 경로가 아니다. "
        f"`--from <프레임워크 checkout>` 를 주거나 local.conf 의 upstream.path= 을 로컬 프레임워크 "
        f"경로로 두라(URL upstream 은 자동 해소하지 않는다)."
    )


# ── add_harness (라이브 인스턴스에 두 번째 harness 어댑터 비파괴 추가) ──────
# raw 다중-harness `--into` 재-import 는 full 재-laydown 으로 라이브 wiki dev-state/엔진을
# 템플릿 starter 로 덮는다. add_harness 는 복사 스코프를 *추가되는 harness 의
# 어댑터 네임스페이스*(ADD_HARNESS_ADAPTER)로 제한해 그 파괴를 구조적으로 차단한다 — 기존 copy/
# render/backup 머신(plan_copy·substitute·resolve_opencode_model·_run_manual_fill)만 재사용한다(신규
# 복사 머신 0). 운영 진입(pm_config add-harness)이 이 core 로 verbatim 위임한다(Decision 3).


# codex 어댑터(`​.codex/agents/*.toml`·`config.toml`·hooks)는 **trusted project + hook trust 승인
# 후에만** 발화한다. import/add-harness 직후 신선 인스턴스는 이 2단계가
# 미승인 상태 — 조용히 두면 위임 subagent 스폰·PreCompact ctx checkpoint 안내가 안 뜬다. `-c projects.<path>.
# trust_level` CLI override 는 **안 먹으므로** 대화형 승인이 유일 경로다.
def _print_codex_trust_guidance() -> None:
    """codex 어댑터 laydown 후 loud 2단계 trust 안내.

    import(`--harness codex`)·add-harness(기존 인스턴스에 codex 추가) 완료 출력 끝에 붙는다 —
    채택자가 첫 부트스트랩 전에 밟아야 할 trust 2단계 + 검증 커맨드를 눈에 띄게(loud) 안내한다.
    """
    print("")
    print("⚠️  codex 어댑터 활성화 전 2단계 trust 승인 필요 (미승인 시 위임/훅 미발화):")
    print("  1) 이 디렉토리에서 대화형 `codex` 를 1회 열어 프로젝트 trust 를 수락한다")
    print("     (`.codex/agents/*.toml`·`config.toml` 은 trusted project 한정 로드).")
    print("  2) codex 안에서 `/hooks` 로 hook trust 를 승인한다 (PreCompact ctx checkpoint 안내 발화 전제).")
    print("  검증: 대화형 codex 에서 위임 4축(architect/developer/code-reviewer/researcher)이 "
          "스폰 목록에 뜨는지 확인한다.")
    print("  ⚠️ `-c projects.<path>.trust_level=trusted` CLI override 는 안 먹는다(실측) — "
          "위 1) 대화형 승인이 필수다.")


def _print_claude_trust_guidance() -> None:
    """claude 어댑터 laydown 후 permissions.allow trust 선행 조건을 loud 안내."""
    print("")
    print("⚠️  Claude Code permissions.allow 적용 조건 안내:")
    print("  이 디렉토리를 아직 trust 승인하지 않았다면 출하 `.claude/settings.json`의")
    print("  `permissions.allow`가 적용되지 않으며 전역 설정에 의존한다.")
    print('  콘솔의 "Ignoring N permissions.allow entries" 경고가 이 상태의 실측 신호다.')
    print("  첫 대화형 `claude` 세션에서 trust 다이얼로그를 수락하면 적용된다.")


def _in_adapter_namespace(
    rel: Path, adapter_dirs: tuple, root_doc: str, owned_paths: set[str],
    guest_render_paths: set[str],
) -> bool:
    """rel(dst relpath)이 추가 harness 의 복사 스코프 안인가.

    스코프 = ({adapter dir(들) 하위, root doc} ∪ **flavor `@render` 선언**(`guest_render_paths`·cross-ns
    의존물 포함)) − **host 실소유 경로**(`owned_paths`). adapter_dirs 중 하나의 하위/root doc 정확일치/
    flavor `@render` 선언(그 자체·하위) 중 하나여야 하고, 그 중 host(dest)가 이미 소유(pm_update 관리)하는
    경로 하위는 제외한다(경로-포함·`_is_render_managed`). **cross-ns 확장**: opencode 의
    `.claude/skills @render`(네이티브 소비)는 `.opencode` namespace 밖이나 flavor 가 선언한
    의존물이라 codex host(미소유)엔 복사해야 한다 — 옛 namespace-only 스코프는 이를 놓쳐 PM 스킬이
    host 실소유 차감은 그 위에 얹혀 opencode host 의 `.claude/skills`
    (host 소유)처럼 **host 가 이미 가진 것만** 정확히 뺀다(claude host + opencode 는 여전히 미복사). 옛엔
    flavor-native `@render`(guest flavor 관점)로 판정해 claude-as-guest 의 `.claude/agents`·`.claude/skills`
    adapter_dirs 는 튜플 —
    claude/opencode 는 단일, codex 는 이중(`.codex`+`.agents`). 이 밖(엔진·wiki·타 harness·
    설정·파사드)은 전부 False → plan 에 애초에 안 들어온다(구조적 안전·flavor 미선언 경로 유입 0).
    """
    rel_posix = rel.as_posix()
    # `rel == d` 포함(등재 경계와 동일 판정): namespace 자체 relpath 도 안으로 본다(파일 복사는
    # 하위만 나오지만 두 경계를 문자적으로 일치시켜). flavor `@render` 선언(cross-ns 의존물)은
    # `_is_render_managed`(경로-포함)로 그 하위 파일까지 스코프에 넣는다.
    in_scope = (rel_posix == root_doc
                or any(rel_posix == d or rel_posix.startswith(d + "/") for d in adapter_dirs)
                or _is_render_managed(rel_posix, guest_render_paths))
    if not in_scope:
        return False
    # host(dest)가 이미 소유(pm_update 관리)하는 경로는 스코프 안이라도 제외(중복 레이다운 방지).
    if _is_render_managed(rel_posix, owned_paths):
        return False
    return True


def _existing_create_if_absent_relpaths(
    template_root: Path,
    dest_root: Path,
    relpaths: frozenset[str],
    toml_override_fields: dict[str, frozenset[str]],
) -> tuple[set[Path], list[str]]:
    """기존 instance-owned config의 copy 제외 집합과 loud 안내 대상을 계산한다.

    이 helper는 add_harness가 ``plan_copy``를 호출하기 *전* 쓴다. 따라서 보호 파일은 일반
    CopyAction/backup/ancestor validation 경로에 전혀 들어가지 않는다. src가 없는 정책 항목은
    무시하지 않고 ValueError로 fail-loud — 선언과 template drift를 숨기지 않는다.
    """
    skip: set[Path] = set()
    different: list[str] = []
    for rel_str in sorted(relpaths):
        rel = Path(rel_str)
        src = template_root / rel
        if not src.is_file():
            raise ValueError(
                f"add-harness create-if-absent 정책 경로가 template에 없습니다: {rel_str}"
            )
        dst = dest_root / rel
        if not (dst.exists() or dst.is_symlink()):
            continue
        skip.add(rel)
        if not _same_bytes(src, dst):
            different.append(rel_str)
    for pattern, fields in sorted(toml_override_fields.items()):
        for src in sorted(template_root.glob(pattern)):
            rel = src.relative_to(template_root)
            dst = dest_root / rel
            if not (dst.exists() or dst.is_symlink()) or rel in skip:
                continue
            try:
                text = _read_text_shared(dst, encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # 읽을 수 없는 agent 파일은 engine-managed refresh의 기존 안전 경로를 탄다.
            # TOML 문법으로 해석한 최상위 키만 본다. nested table·문자열·주석의 동일한
            # 단어는 instance override가 아니며, quoted key도 정상적으로 보호한다.
            try:
                parsed = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue  # 깨진 TOML은 기존 engine-managed refresh 경로를 탄다.
            if not any(field in parsed for field in fields):
                continue
            skip.add(rel)
            if not _same_bytes(src, dst):
                different.append(rel.as_posix())
    return skip, different


def _load_pm_update():
    """pm_update 모듈 지연 로드 (read_manifest + guest 절 마커/헬퍼 재사용·pm_import 은 pm_update 를
    top-level import 하지 않는다). `_engine_render_relpaths` 등이 쓰는 그 spec 로드 패턴. 실패 시 None."""
    pm_update_py = Path(__file__).resolve().parent / "pm_update.py"
    try:
        return _load_module_from_path(
            pm_update_py, "pm_update.py", allow_unverified=True,
        )
    except Exception:  # noqa: BLE001 — 로드 실패는 None(호출부가 무동작으로 흡수).
        return None


def _pm_update_read_manifest(manifest_path: Path) -> list:
    """pm_update.read_manifest 재사용 (부재·로드/파싱 실패는 빈 리스트·무동작). 새 파서 신설 없음."""
    if not manifest_path.is_file():
        return []
    mod = _load_pm_update()
    if mod is None:
        return []
    try:
        return list(mod.read_manifest(manifest_path))
    except Exception:  # noqa: BLE001 — 파싱 실패는 빈 리스트(무동작).
        return []


def _dest_manifest_core_paths(dest_root: Path) -> set[str]:
    """dest engine.manifest 의 **core**(guest 절 제외) 경로 집합 — add-harness 복사/등재 차감 기준
    host 가 pm_update 로 이미 관리(소유)하는 경로다.

    옛 flavor-native 판정(`_engine_render_relpaths`)은 *guest flavor* 관점이라 **claude-as-guest** 를
    놓쳤다 — dest 실소유로 대체한다. guest 절(add-harness 자기 산출·refresh 재복사 대상)은 stripped 라
    제외 → refresh 가 guest 를 재복사한다. manifest 부재·pm_update 로드 실패는 빈 set(무차감)."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    pu = _load_pm_update()
    if pu is None:
        return set()
    return pu._core_manifest_paths(_read_text_shared(manifest, encoding="utf-8"))


def _flavor_render_relpaths(template_root: Path) -> set[str]:
    """guest flavor manifest 의 `@render` 선언 경로 **전부** (namespace 무관·host 실소유 미차감·손-열거 0).

    add-harness 복사/등재 후보의 **단일 출처**: flavor 가 `@render` 로 선언한 경로가 곧
    그 하네스가 관리하는 footprint 다. 여기엔 **cross-ns 의존물**도 포함된다 — opencode flavor 의
    `.claude/skills @render`(PM 스킬 채널·네이티브 소비)는 `.opencode` namespace 밖이지만
    opencode 어댑터가 반드시 소비한다. host 실소유 차감은 이 위에 downstream 으로 얹힌다(복사=
    `_in_adapter_namespace`·등재=`_guest_render_sync_plan`·둘 다 dest 실소유 `_path_owned_by` 기준). manifest
    부재·pm_update 로드/파싱 실패는 빈 set(무동작)."""
    manifest = template_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return set()
    return {str(e).replace("\\", "/") for e in _pm_update_read_manifest(manifest)
            if getattr(e, "render", False)}


def _guest_manifest_lines(
        template_root: Path, adapter_dirs: tuple, root_doc: str,
        dest_owned: set[str]) -> list[str]:
    """add-harness 가 레이다운하는 guest 어댑터의 manifest **후보** 라인 (dest 등재용·손-열거 0).

    한 절에 두 종류가 모이고 **한 줄의 `@render` 유무가 소유 채널을 가른다**(새 어휘 0):

      - **어댑터 렌더물**(`@render @target-owned [@source=…]`) — 재렌더 전파(update 채널이 conf 로
        다시 렌더한다·손편집은 되돌아간다).
        후보 = guest flavor manifest 의 `@render` **선언 전부**(`_flavor_render_relpaths` 와 같은
        판정). flavor `@render` 선언 자체가 이미 경계다 — flavor 는 자기가 관리하는 경로만 `@render`
        로 선언하므로("flavor 미선언 경로 유입 0" 불변식의 구조적 보장) namespace cap 없이 **cross-ns
        의존물**(opencode 의 `.claude/skills @render` — 네이티브 소비이나 `.opencode` 밖)도 후보다.
      - **엔진 파일**(`@source=… @target-owned`) — pm_update 소유(byte-copy 전파).
        후보 = flavor manifest 의 **비-`@render`** 엔트리 중 **복사 술어**(`_in_adapter_namespace`)를
        통과하는 것. 복사와 같은 술어를 쓰므로 "등재 ⊆ 복사" 가 구조적으로 보장된다 — 별도 판정을
        두면 claude-as-guest 처럼 복사하지 않은 경로를 등재해 pm_update 가 없던 파일을 만든다.
        이 행이 없으면 `.codex/pm_orch_codex.py`·`.opencode/lib`·claude ctx 가드처럼 `pm_relay`
        코어와 짝인 engine-mirror 가 설치 시점 사본으로 영구 동결된다(코어↔드라이버 skew).

    host 가 이미 소유한 것은 downstream 차감(`_guest_render_sync_plan` 의 `_path_owned_by`·기준
    `_core_manifest_paths`)이 dest 기준으로 정확히 뺀다(엔진 행은 `_in_adapter_namespace` 가 같은
    `dest_owned` 로 여기서 이미 뺀다). 두 종류 모두 host 인스턴스 소유라 `@target-owned` 를 붙인다 —
    렌더물은 재렌더 clobber 계약(MF-2), 엔진 행은 upstream flavor 부재 시 loud `[skip]` + rc0 이다.
    직렬화는 `pm_update._manifest_entry_line` 재사용(마커 순서 결정적·손 f-string 금지)."""
    pu = _load_pm_update()
    if pu is None:
        return []
    entries = _pm_update_read_manifest(
        template_root / ".project_manager" / "engine.manifest")
    render_paths = {str(e).replace("\\", "/")
                    for e in entries if getattr(e, "render", False)}
    lines: list[str] = []
    for entry in entries:
        rel = str(entry).replace("\\", "/")
        render = bool(getattr(entry, "render", False))
        if not render and not _in_adapter_namespace(
                Path(rel), adapter_dirs, root_doc, dest_owned, render_paths):
            continue
        lines.append(pu._manifest_entry_line(pu.ManifestEntry(
            rel, render, True, getattr(entry, "source_rel", None))))
    return sorted(lines, key=lambda line: line.split()[0])


def _is_safe_dest_path(dest_root: Path, rel: Path) -> bool:
    """`dest_root/rel` 이 dest 루트 하위이고 rel 경로·조상에 symlink 가 없는가.

    조작된 경로가 링크 follow·`..` 탈출로 repo 밖을 순회/치환하는 것을 막는다(순회·처리 양쪽):
      - `..`/빈 컴포넌트 거부.
      - rel 각 컴포넌트(`dest_root/…`)가 symlink 면 거부(**symlink 및 symlink 조상**).
      - 최종 resolve 후 dest_root 하위 containment(밖이면 거부).
    위반 시 False → 호출부가 skip(비파괴)."""
    try:
        droot = dest_root.resolve()
    except OSError:
        return False
    cur = dest_root
    for part in rel.parts:
        if part in ("..", "", "."):
            return False
        cur = cur / part
        try:
            if cur.is_symlink():
                return False
        except OSError:
            return False
    try:
        cur.resolve().relative_to(droot)
    except (ValueError, OSError):
        return False
    return True


def _byte_identical_skipped(
        template_root: Path, dest_root: Path, copied_relpaths: set,
        adapter_dirs: tuple, root_identity: tuple | None = None) -> set:
    """이번 하네스 template 과 **byte-identical 이라 복사만 생략된** dest 파일 relpath 집합
    .

    이게 token-form 미렌더 잔존 문제의 **유일한 대상**이다 — 타 guest·adopter 자체 생성 파일(내용 상이·
    copied)
    조건: (a) 이 하네스 adapter namespace(`adapter_dirs`) 안, (b) 미-copied(copy plan 이 안 실음),
    (c) dest 실존 + template 과 byte-identical, (d) 경로 안전(`_is_safe_dest_path`). 경로는
    **template**(신뢰)에서 오고 manifest(조작 가능)에서 오지 않는다."""
    out: set = set()
    for rel, src in _iter_source_files(template_root, "full"):
        rel_posix = rel.as_posix()
        if not any(rel_posix == d.rstrip("/") or rel_posix.startswith(d.rstrip("/") + "/")
                   for d in adapter_dirs):
            continue  # 이 하네스 namespace 밖.
        if rel in copied_relpaths:
            continue  # 이미 copy plan 이 실음 → 렌더 대상(중복 방지).
        if not _is_safe_dest_path(dest_root, rel):
            continue  # 조작 경로·symlink → skip(repo 밖 순회/치환 방지).
        # 비교는 **nofollow 로 한 번 연 fd** 에서 한다 — lstat 으로 걸러도 그 뒤 `_same_bytes` 가
        #   경로를 *다시 열면* 그 사이 교체된 링크의 대상을 읽어 byte-identical 로 보고, 그 경로가
        #   치환·렌더 대상 집합에 들어간다(경로 기반 소비처가 저장소 밖을 고칠 입구). 재열기 제거.
        try:
            # `regular_only` 가 일반 파일 판정(FIFO·디바이스 거부)까지 연 fd 에서 처리한다 —
            #   거부는 아래 UnsafeDestPathError 핸들러가 범위 밖으로 흘린다(옛 별도 S_ISREG 검사와
            #   같은 결과·판정 지점 하나).
            with _fdopen_binary(
                    _open_dest_relative_nofollow(
                        dest_root, rel, os.O_RDONLY,
                        root_identity=root_identity, regular_only=True), "rb") as dst_handle:
                if _same_bytes_fd(src, dst_handle):
                    out.add(rel)
        except UnsafeDestPathError:
            continue  # 교체된 경로는 범위 밖(위 `_is_safe_dest_path` 거부와 같은 결과).
        except OSError:
            continue
    return out


def _same_bytes_fd(src: Path, dst_handle) -> bool:
    """template 원본과 **이미 연 dest 핸들**의 내용이 같은가(dest 재열기 없음·스트리밍 비교)."""
    chunk_size = 1 << 20
    try:
        with _open_shared(src, binary=True) as src_handle:
            while True:
                src_chunk = src_handle.read(chunk_size)
                dst_chunk = dst_handle.read(chunk_size)
                if src_chunk != dst_chunk:
                    return False
                if not src_chunk:
                    return True
    except OSError:
        return False


# PM 어댑터 고유 판별자로 인정하는 이름 관례 — 파일명/디렉토리에 이 접두사가 있으면 PM 이
# 깔아 준 자산이다(`pm_orch_claude.py`·`pm-instructions.md`·`skills/pm-bootstrap/`). 어댑터
# 네임스페이스만 보면 일반 프로젝트가 자기 용도로 만든 `.codex/`·`AGENTS.md` 를 PM 설치로
# 오인한다 — 그 오판은 표기 독자 집합을 부풀려 없는 하네스의 호출법을 출하한다.
_PM_ASSET_NAME_PREFIXES = ("pm-", "pm_")


def _pm_install_evidence(source_root: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """`(하네스 → PM 설치 증거 후보, 하네스 → 전체 출하 relpath)`.

    증거 후보 = 그 하네스 **어댑터 네임스페이스 안**의 PM 관례 자산(경로 컴포넌트가
    `_PM_ASSET_NAME_PREFIXES` 로 시작 — `skills/pm-bootstrap/`·`pm_orch_claude.py`·
    `pm-instructions.md`). 네임스페이스 밖(공유 wiki·엔진 backbone)은 하네스를 못 가르므로 제외.

    두 번째 dict(전체 출하)는 **귀속 판정**용이다 — 어떤 증거가 다른 하네스에서도 나오는지
    (예 `.claude/skills/pm-*` 는 claude 네임스페이스지만 opencode template 도 출하) 알아야
    거짓 양성을 막는다. 소스 열거 실패는 그 하네스 빈 집합(호출부가 구조 판정으로 강등)."""
    evidence: dict[str, set[str]] = {}
    shipped_all: dict[str, set[str]] = {}
    for name in REGISTERED_HARNESSES:
        adapter_dirs = ADD_HARNESS_ADAPTER.get(name, ((), ""))[0]
        rels: set[str] = set()
        every: set[str] = set()
        for dirname in HARNESS_TEMPLATE_DIRS.get(name, ()):
            root = Path(source_root) / "templates" / dirname
            if not root.is_dir():
                continue
            try:
                shipped = list(_iter_source_files(root, "full"))
            except Exception as exc:  # noqa: BLE001 — 열거 실패는 판별자 0(구조 판정으로 강등).
                if _is_engine_rev_skew(exc):
                    raise
                continue
            for rel, _src in shipped:
                rel_posix = rel.as_posix()
                every.add(rel_posix)
                if not any(rel_posix == d.rstrip("/") or rel_posix.startswith(d.rstrip("/") + "/")
                           for d in adapter_dirs):
                    continue
                if any(part.startswith(_PM_ASSET_NAME_PREFIXES)
                       for part in rel_posix.split("/")):
                    rels.add(rel_posix)
        evidence[name] = rels
        shipped_all[name] = every
    return evidence, shipped_all


# ── 영속 설치 기록(install receipt) ──────────────────────────────────────────
# 이 인스턴스에 PM 어댑터를 **실제로 설치한** 하네스 목록을 인스턴스 메타에 박제한다. 증거 추론
# (`_pm_install_evidence`)은 구조상 두 방향으로 틀릴 수 있다 — 증거가 둘뿐인 하네스는 그 파일이
# 함께 사라지면 미검출(표기 유실)이고, 채택자 자작 진입문서는 거짓 양성(표기 소음)이다. 기록이
# 있으면 그 추론을 아예 안 탄다.
#
# 위치를 local.conf 키가 아니라 **별도 소파일**로 둔 근거:
#   - git 추적: local.conf 는 per-clone 이라 엔진 `.gitignore` 가 무시한다. 그런데 기록이 서술하는
#     대상(어댑터 파일 `.claude/**`·`.codex/**`)은 추적물이라 clone 마다 실재한다 — 기록만 빠지면
#     그 clone 은 다시 추론으로 내려가 이 기록이 닫으려는 한계를 그대로 맞는다. 소파일은 기본
#     추적이라 기록과 대상이 같은 채널로 함께 이동한다(다중 사용자 clone 공유).
#   - clobber 면역: manifest 미등재라 pm_update 동기 대상이 아니고, `board.py init` 이 통째로 다시
#     쓰는 local.conf 의 백업·재병합 왕복도 타지 않는다(재-import 마다 값이 되살아나는 창 없음).
# 형식은 JSON 한 객체 — `harnesses`와 실제로 레이다운한 instance-owned template 좌표만 의미를
# 갖는다. 좌표는 dst relpath → `{weight, source}`이고, source는 checkout 기준 POSIX 경로다. 특히
# lite의 `X.lite.md → X.md` rename을 나중 관측이 full `X.md`로 되짚지 않게 설치 순간 선택을 박제한다.
# 날짜 등 변하는 값은 넣지 않아 같은 설치 형상이면 매 실행 byte 동일이다(re-import churn 0).
INSTALL_RECEIPT_RELPATH = Path(".project_manager") / "install.json"
INSTALL_RECEIPT_SCHEMA = 2
INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY = "instance_owned_templates"


def _install_receipt_fix_hint(dest_root: Path) -> str:
    """기록을 사람이 고칠 위치를 실값 경로로 알려 주는 꼬리말(수정·복구 채널 노출).

    기록은 관측(증거 추론)을 덮으므로, 잘못 박힌 기록은 사람이 고치지 않으면 계속 그 판정을
    강제한다 — 제거·수정 채널이 없는 채로 두면 유령 하네스가 영구히 독자로 남는다."""
    return (f"수정 경로: {Path(dest_root) / INSTALL_RECEIPT_RELPATH} 의 "
            "`harnesses` 배열을 직접 편집하세요")


class InstallReceiptDocument(NamedTuple):
    """기록 파일 판독 결과 — 문서와 **왜 못 읽었는지**를 함께 나른다.

    `status`: `ok`(문서 유효) · `absent`(파일 없음) · `unreadable`(경로 교체·권한 등 열기 실패) ·
    `corrupt`(열렸는데 JSON·형식이 깨짐). 부재와 손상을 같은 `None` 으로 접으면 기록 갱신이
    손상 파일을 그냥 덮어써 원본이 영구 소실된다 — 그래서 사유를 남긴다."""

    document: dict | None
    status: str


def _load_install_receipt_document(dest_root: Path, *,
                                   quiet: bool = False) -> InstallReceiptDocument:
    """기록 파일을 파싱한 **원본 문서 + 판독 사유**.

    schema 판정을 읽기(해석)와 쓰기(덮어쓰기) 양쪽이 봐야 해서 문서 단계를 따로 둔다. `quiet` 는
    같은 실행에서 두 번 읽을 때(판정 → 기록) 경고가 겹치지 않게 하는 스위치다."""
    dest_root = Path(dest_root)
    if not dest_root.is_dir():
        # 아직 만들어지지 않은 dest(--new 계획 단계) — 기록 없음이 정상.
        return InstallReceiptDocument(None, "absent")
    try:
        text = read_dest_text(dest_root, INSTALL_RECEIPT_RELPATH)
    except FileNotFoundError:
        # 기록 미도입 인스턴스(구 설치·수기 설치) — 호출부가 추론으로 폴백.
        return InstallReceiptDocument(None, "absent")
    except (OSError, UnsafeDestPathError, UnicodeDecodeError) as exc:
        if not quiet:
            print(f"경고: 설치 기록을 읽을 수 없어 증거 추론으로 판정합니다 "
                  f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}). "
                  f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return InstallReceiptDocument(None, "unreadable")
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError("기록 최상위는 객체여야 합니다")
    except (ValueError, TypeError) as exc:
        if not quiet:
            print(f"경고: 설치 기록 형식이 올바르지 않아 증거 추론으로 판정합니다 "
                  f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}). "
                  f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return InstallReceiptDocument(None, "corrupt")
    return InstallReceiptDocument(data, "ok")


def _install_receipt_is_newer_schema(document: dict | None) -> bool:
    """이 엔진이 해석할 수 없는 **상위 schema** 기록인가(읽기 거부·쓰기 거부 공통 판정)."""
    schema = (document or {}).get("schema")
    return isinstance(schema, int) and schema > INSTALL_RECEIPT_SCHEMA


def _read_install_receipt_raw(dest_root: Path) -> list | None:
    """기록의 `harnesses` 값을 **거르지 않고** 반환 — 부재·해독 불가·형식 오류면 `None`.

    등록 필터는 소비 지점(`read_install_receipt`)이 하고, 기록 갱신(`record_install_receipt`)은
    이 원본을 봐야 한다: 신 엔진이 남긴 미등록 하네스 이름을 구 엔진의 재기록이 영구 삭제하지
    않으려면 원본 목록이 필요하다. 경고는 여기서 한 번만 낸다(중복 출력 방지)."""
    document = _load_install_receipt_document(dest_root).document
    if document is None:
        return None
    # 전방 호환 가드: 상위 schema 기록은 이 엔진이 모르는 의미(예 항목별 조건·제외 규칙)를 담을 수
    #   있다 — 그걸 목록으로만 읽으면 신 엔진의 의도를 조용히 왜곡한다. 알리고 추론으로 내려간다.
    if _install_receipt_is_newer_schema(document):
        print(f"경고: 설치 기록 schema {document.get('schema')} 는 이 엔진(지원 상한 "
              f"{INSTALL_RECEIPT_SCHEMA})보다 새롭습니다 — 해석하지 않고 증거 추론으로 판정합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). 엔진을 갱신하세요.",
              file=sys.stderr)
        return None
    recorded = document.get("harnesses")
    if not isinstance(recorded, list):
        print(f"경고: 설치 기록 형식이 올바르지 않아 증거 추론으로 판정합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: harnesses 는 목록이어야 합니다). "
              f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return None
    return recorded


def read_install_receipt(dest_root: Path) -> list[str] | None:
    """인스턴스에 기록된 설치 하네스(registry 정규 순서) — 기록 부재·해독 불가면 `None`.

    `None` 은 "기록 없음"이고 빈 목록과 다르다: 호출부는 `None` 일 때만 증거 추론으로 내려간다.
    깨진 기록(JSON 오류·`harnesses` 부재·상위 schema·등록 하네스 0)도 `None` 로 강등하되 **조용히
    하지 않는다** — 기록을 진실로 쓰던 판정이 침묵으로 추론으로 바뀌면 그 전환을 아무도 못 본다.
    다만 dest 가 아직 없거나(`--new` 계획 단계) `.project_manager/` 가 없는 트리는 정상이라 무음이다.

    읽기는 symlink 미추종 fd 경로다 — 기록 경로가 저장소 밖 지향 링크로 바뀌어도 따라가지 않는다."""
    dest_root = Path(dest_root)
    recorded = _read_install_receipt_raw(dest_root)
    if recorded is None:
        return None
    unknown = [str(name) for name in recorded if name not in REGISTERED_HARNESSES]
    if unknown:
        # 신 엔진이 쓴 미래 하네스일 수 있다 — 판정에서 빼되 알린다(등록 밖 이름은 registry 조회
        #   에서 터지므로 통과시킬 수 없고, 조용히 버리면 표기 독자가 말없이 준다). 기록 자체에는
        #   보존된다(`record_install_receipt`) — 구 엔진이 신 엔진의 값을 지우지 않게.
        print(f"경고: 설치 기록에 미등록 하네스가 있어 판정에서 제외합니다: "
              f"{', '.join(unknown)} "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}).", file=sys.stderr)
    known = [name for name in REGISTERED_HARNESSES if name in recorded]
    if not known:
        # 유효 항목 0 = 기록 없음과 같게 다루되(표기 유실보다 소음이 낫다) **조용히 하지 않는다** —
        #   빈 `harnesses` 는 정상 상태가 아니라 잘린·손상된 기록이다.
        print(f"경고: 설치 기록에 유효한 하네스가 없어 증거 추론으로 판정합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). "
              f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return None
    return known


def established_harnesses(dest_root: Path, candidates, preexisting) -> tuple[list, list]:
    """기록에 올릴 자격이 있는 하네스 — `(성립분, 제외분)`.

    "이번 run 이 선택했다" 와 "그 어댑터가 실제로 트리에 섰다" 는 다르다: 적용 단계에서 그 하네스
    파일이 전부 제외되면(경로 교체·계획 뒤 상태 변화) 어댑터는 서지 않는데 기록만 "설치됨" 이 된다
    — 그 순간 기록은 유령을 진실로 만든다(기록이 관측을 덮으므로 스스로 교정되지도 않는다).

    판정은 구조 증거(`_has_adapter_shape`)다. 단 **이미 설치돼 있던 하네스**(`preexisting`)는 그대로
    통과시킨다 — 기록은 설치 사실이라 사용자가 파일을 지웠다고 이번 run 이 임의로 철회하지 않는다
    (철회는 명시 편집 채널)."""
    preexisting_set = set(preexisting)
    established: list = []
    dropped: list = []
    for name in candidates:
        if name in preexisting_set or _has_adapter_shape(dest_root, name):
            established.append(name)
        else:
            dropped.append(name)
    return established, dropped


INSTALL_RECEIPT_CORRUPT_SUFFIX = ".corrupt"


def _preserve_corrupt_install_receipt(dest_root: Path,
                                      root_identity: tuple | None = None) -> bool:
    """손상된 기록을 `install.json.corrupt`(순번)로 **백업**하고 재기록을 허용한다 — 성공 시 True.

    손상을 부재와 같게 다루면 갱신이 그 파일을 `O_TRUNC` 로 덮어 원본이 영구 소실된다(사람이 나중에
    무엇이 깨졌는지 볼 수 없다). 그렇다고 기록을 영영 거부하면 그 인스턴스는 계속 추론 판정에
    머문다 — 그래서 **백업 후 재기록**을 택한다(엔진의 "백업하고 바꾼다" 규율과 같은 결). 백업에
    실패하면 재기록도 하지 않는다(백업 못 하는 파일은 고치지 않는다)."""
    try:
        target = _copy_dest_file_nofollow(
            dest_root, INSTALL_RECEIPT_RELPATH,
            INSTALL_RECEIPT_RELPATH.with_name(
                INSTALL_RECEIPT_RELPATH.name + INSTALL_RECEIPT_CORRUPT_SUFFIX),
            root_identity=root_identity)
    except (OSError, UnsafeDestPathError) as exc:
        print(f"경고: 손상된 설치 기록을 백업하지 못해 갱신하지 않습니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}) — 원본을 보존합니다.",
              file=sys.stderr)
        return False
    print(f"  ⚠️ 손상된 설치 기록을 백업하고 다시 씁니다: {target.relative_to(dest_root).as_posix()}",
          file=sys.stderr)
    return True


def record_install_receipt(dest_root: Path, harnesses,
                           root_identity: tuple | None = None, *,
                           template_coordinates: dict | None = None) -> bool:
    """설치 하네스를 인스턴스 설치 기록에 박제한다 — 내용이 바뀌었으면 True.

    기록 시점은 **실제 설치·추가가 일어난 run**(`--new`·`--into`·add-harness 의 적용 경로)뿐이다.
    판정 함수(`installed_harnesses`)는 읽기만 한다 — 거기서 쓰면 dry-run·read-only 갱신 확인 같은
    무변경 명령이 인스턴스를 고치고, 비-PM 트리에도 기록을 남긴다. 기록이 없던 구 인스턴스는 그래서
    다음 설치 행위에서 backfill 된다(그 시점의 추론 산출 ∪ 이번 선택을 그대로 박제 — 폴백을 계속
    쌓는 대신 한 번 원천으로 옮긴다). 엔진 동기(pm_update)는 기록 채널이 아니다: 그쪽 dest 는 채택자
    인스턴스일 수도, 출하 템플릿 트리(`--target`)나 프레임워크 checkout 자신일 수도 있어 "설치했다"
    는 사실을 만들 자격이 없다(설치 행위를 한 쪽만 기록한다).

    `template_coordinates`는 이번 복사에서 실제 성공한 instance-owned dst만 받는다. 기존 좌표와
    합쳐 쓰므로 add-harness가 create-if-absent로 보존한 진입문서나 파일 단위 적용 제외분의 과거
    좌표를 현재 full 좌표로 바꾸지 않는다. 인자를 생략한 기존 호출도 이미 기록된 좌표를 보존한다.

    기존 기록의 **미등록 이름은 보존한다**: 그 값은 이 엔진보다 새로운 엔진이 남긴 하네스일 수
    있어, 구 엔진의 재기록이 지우면 신 엔진의 설치 사실이 영구 소실된다(판정에서 빼는 것과 기록에서
    지우는 것은 다른 일이다). 보존분은 registry 순서 뒤에 원래 순서로 붙는다.

    쓰기 실패(경로 교체·권한·`.project_manager/` 부재)는 경고 후 False — 기록은 판정을 더 정확하게
    할 뿐 설치의 성공 조건이 아니므로 설치 전체를 되돌리지 않는다. dest 루트 교체(전체 중단
    클래스)만 예외로 그대로 올라간다."""
    ordered = [name for name in REGISTERED_HARNESSES if name in set(harnesses)]
    if not ordered:
        return False
    # **상위 schema 기록은 덮지 않는다** — 해석할 수 없다고 판정에서 뺀 문서를 이 엔진 형식으로
    #   다시 쓰면 신 엔진의 기록이 통째로 파괴된다(읽기 거부와 쓰기 거부는 짝이어야 한다).
    existing = _load_install_receipt_document(dest_root, quiet=True)
    if _install_receipt_is_newer_schema(existing.document):
        print(f"경고: 설치 기록 schema {existing.document.get('schema')} 가 이 엔진(지원 상한 "
              f"{INSTALL_RECEIPT_SCHEMA})보다 새로워 갱신하지 않습니다 — 기존 기록을 보존합니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). 엔진을 갱신한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return False
    if existing.status == "unreadable":
        # 경로 자체를 안전하게 열 수 없다(교체·권한) — 새로 쓰면 그 자리를 건드리는 셈이라 멈춘다.
        print(f"경고: 설치 기록 경로를 읽을 수 없어 갱신하지 않습니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}). "
              f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return False
    if existing.status == "corrupt" and not _preserve_corrupt_install_receipt(
            dest_root, root_identity=root_identity):
        return False
    # 보존 대상은 **이미 읽은 문서**에서 뽑는다 — 여기서 다시 읽으면 판정용 경고("증거 추론으로
    #   판정합니다")가 기록 갱신 실행에서 오도성으로 한 번 더 나간다(이 실행은 판정이 아니다).
    existing_names = (existing.document or {}).get("harnesses")
    preserved = [
        str(name) for name in (existing_names if isinstance(existing_names, list) else [])
        if name not in REGISTERED_HARNESSES and str(name) not in ordered
    ]
    seen_preserved: list[str] = []
    for name in preserved:  # 중복 접기(원래 순서 유지·byte 안정).
        if name not in seen_preserved:
            seen_preserved.append(name)
    existing_coordinates = (existing.document or {}).get(
        INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY)
    coordinates = dict(existing_coordinates) if isinstance(existing_coordinates, dict) else {}
    if template_coordinates:
        for relpath, coordinate in template_coordinates.items():
            if not isinstance(relpath, str) or not isinstance(coordinate, dict):
                continue
            weight = coordinate.get("weight")
            source = coordinate.get("source")
            if weight not in WEIGHT_CHOICES or not isinstance(source, str):
                continue
            coordinates[relpath] = {"weight": weight, "source": source}
    payload = {
        "schema": INSTALL_RECEIPT_SCHEMA,
        "harnesses": ordered + seen_preserved,
    }
    if coordinates:
        payload[INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY] = coordinates
    text = json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if read_dest_text(dest_root, INSTALL_RECEIPT_RELPATH,
                          root_identity=root_identity) == text:
            return False  # 같은 집합 재기록 — 무변경(멱등·byte churn 0).
    except (OSError, UnsafeDestPathError, UnicodeDecodeError):
        pass  # 부재·해독 불가는 아래에서 새로 쓴다.
    try:
        with _fdopen_text(
                _open_dest_relative_nofollow(
                    dest_root, INSTALL_RECEIPT_RELPATH,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    root_identity=root_identity, regular_only=True),
                "w", newline="\n") as handle:
            handle.write(text)
    except (OSError, UnsafeDestPathError) as exc:
        print(f"경고: 설치 기록을 남기지 못했습니다 "
              f"({Path(dest_root) / INSTALL_RECEIPT_RELPATH}: {exc}) — 판정이 증거 추론으로 "
              f"내려갑니다. {_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return False
    return True


def _copied_instance_owned_template_coordinates(
        plan: list[CopyAction], dest_root: Path, source_root: Path,
        copied_relpaths, weight: str) -> dict[str, dict[str, str]]:
    """실제 복사 성공분에서 instance-owned template 좌표를 추출한다.

    계획 전체나 설치 하네스 선언으로 재구성하지 않고 ``CopyAction.src``를 직접 읽는다. 그래야
    lite rename과 공존 중립 source override가 기록에 그대로 남고, 적용 단계에서 제외된 파일이나
    create-if-absent로 보존한 기존 파일은 현재 실행 좌표로 오염되지 않는다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    copied = {Path(rel) for rel in copied_relpaths}
    instance_owned = {
        relpath
        for relpaths in INSTANCE_OWNED_ADAPTER_FILES.values()
        for relpath in relpaths
    }
    coordinates: dict[str, dict[str, str]] = {}
    for action in plan:
        try:
            dest_rel = action.dst.relative_to(dest_root)
            source_rel = action.src.relative_to(source_root)
        except ValueError:
            continue
        relpath = dest_rel.as_posix()
        if dest_rel not in copied or relpath not in instance_owned:
            continue
        coordinates[relpath] = {
            "weight": weight,
            "source": source_rel.as_posix(),
        }
    return coordinates


def _has_adapter_shape(dest_root: Path, harness: str) -> bool:
    """그 하네스 어댑터의 **구조 증거**(네임스페이스 디렉토리 전부 + 루트 진입문서)가 있는가.

    추론 판정의 1단이자 기록 보유 인스턴스의 유령 형상(기록엔 있는데 트리엔 없음) 판별에 함께
    쓴다 — 두 곳이 같은 판정을 봐야 "무엇을 설치로 보는가"가 갈리지 않는다."""
    adapter_dirs, root_doc = ADD_HARNESS_ADAPTER[harness]
    return (all((Path(dest_root) / dirname).is_dir() for dirname in adapter_dirs)
            and (Path(dest_root) / root_doc).is_file())


def _installed_harnesses_with_authority(
        dest_root: Path, source_root: Path | None = None) -> tuple[list[str], str]:
    """dest 트리에 **PM 어댑터가 실제로 설치된** 하네스. registry 정규 순서.

    표기 렌더의 독자 집합은 "이번 run 이 고른 하네스"가 아니라 **그 인스턴스를 읽는 하네스
    전부**다 — codex 인스턴스에 `--into claude` 로 claude 를 얹으면 공유 wiki 는 두 하네스가
    함께 읽는다.

    판정 순서는 **기록 우선**이다: 영속 설치 기록(`read_install_receipt`)이 있으면 그것이 진실이고
    아래 증거 추론은 아예 타지 않는다 — 판별자 파일이 전부 지워져도 독자 집합이 유실되지 않는다.
    기록이 없는 인스턴스(기록 도입 전 설치·수기 설치)만 추론으로 폴백하며, 그 인스턴스는 다음
    설치 행위(`--into` 재-import·add-harness)가 기록을 backfill 한다.

    ⚠ 기록은 **설치 사실**이라 어댑터 파일을 지웠다고 자동 철회되지 않는다 — 제거는 기록을 고치는
    명시 행위여야 한다(현재 제거 채널 없음).

    폴백 판정 2단: (a) 어댑터 네임스페이스 + 루트 진입문서 실재(opencode 가 `.claude/skills` 를 함께
    깔기 때문에 claude 는 `CLAUDE.md` 까지 요구하고 codex 는 두 네임스페이스를 모두 요구한다)
    (b) **PM 자산 실재**(`_pm_install_evidence` 의 증거 집합). (b)가 없으면 일반 프로젝트가 자기
    용도로 가진 `.codex/`·`.agents/`·`AGENTS.md` 를 codex PM 설치로 오인해, PM 카드가 없는
    인스턴스에 codex 호출 표기(`$pm-bootstrap`)를 출하한다.

    (b)는 **전용·공유 증거를 가리지 않는다** — 구조 증거가 있는 하네스는 공유 자산(예 opencode
    도 함께 까는 `.claude/skills/pm-*`)만으로도 설치로 본다. 공유 증거를 다른 하네스에 귀속해
    빼면 공존 인스턴스에서 전용 판별자 하나가 빠졌을 때 그 하네스를 통째로 놓쳐 표기가 유실된다.
    반대 방향 오차(채택자 자작 `CLAUDE.md` + opencode 설치 → claude 도 독자로 셈)는 병기 표기가
    하나 늘 뿐이라 **유실보다 안전하다**(비대칭 판단·거짓 양성 허용).

    ⚠ 폴백의 한계: 증거가 적은 하네스는 그 파일들이 다 지워지면 미검출된다 — opencode 는 증거가
    `.opencode/pm-instructions.md`·`pm_orch_opencode.py`·`command/` 뿐이라 그게 다 사라지면 구조 증거가 있어도
    설치로 안 본다. 이 한계는 **기록이 있는 인스턴스에는 없다**(기록이 추론을 대체한다).

    source_root 미지정/증거 파생 실패는 (a)만으로 판정한다(옛 동작·호출부는 항상 소스를 준다)."""
    dest_root = Path(dest_root)
    recorded = read_install_receipt(dest_root)
    if recorded is not None:
        # 기록이 관측을 덮으므로 **유령 형상**(기록엔 있는데 어댑터 자체가 트리에 없음)은 알린다 —
        #   기록은 진실로 쓰되(판정 불변), 사람이 고칠 위치를 함께 준다. 안 알리면 잘못 박힌 기록이
        #   영구히 그 하네스를 독자로 붙든다(제거 채널이 편집뿐이라 더더욱 보여야 한다).
        ghosts = [name for name in recorded if not _has_adapter_shape(dest_root, name)]
        if ghosts:
            print(f"경고: 설치 기록의 {', '.join(ghosts)} 어댑터가 트리에 없습니다 — 기록을 진실로 "
                  f"쓰므로 표기 독자에는 그대로 남습니다. 제거하려면 "
                  f"{_install_receipt_fix_hint(dest_root)}.", file=sys.stderr)
        return recorded, "receipt"
    evidence, _shipped_all = (
        _pm_install_evidence(source_root) if source_root is not None else ({}, {}))
    structural = [
        candidate for candidate in ADD_HARNESS_ADAPTER
        if _has_adapter_shape(dest_root, candidate)
    ]

    def _present(rels) -> bool:
        return any((dest_root / rel).is_file() for rel in rels)

    # 구조 증거(어댑터 네임스페이스 + 루트 진입문서)가 있는 하네스는 **공유 증거만으로도**
    #   인정한다 — 공유 자산을 다른 하네스에 귀속해 빼면 공존 인스턴스에서 전용 판별자 하나가
    #   사라졌을 때 그 하네스가 통째로 미검출된다(claude+opencode 공존에서 `pm_orch_claude.py`
    #   만 없어도 `.claude/skills/pm-*` 가 전부 opencode 몫이 돼 claude 유실·실측). 거짓 양성은
    #   표기 소음이고 거짓 음성은 표기 유실이라 보수적으로 포함한다(비대칭).
    #   판별자를 못 만든 하네스(소스 부재 등)는 구조 판정만으로 인정한다(옛 동작).
    found = [
        name for name in structural
        if not evidence.get(name) or _present(evidence[name])
    ]
    return [name for name in REGISTERED_HARNESSES if name in found], "inferred"


def installed_harnesses(dest_root: Path, source_root: Path | None = None) -> list[str]:
    """dest 트리에 설치된 PM 하네스 목록 — 상세 판정은 authority helper의 단일 경로."""
    return _installed_harnesses_with_authority(dest_root, source_root)[0]


# ── instance-owned 어댑터 config 의 상류 도달 채널 (3-way 원장) ───────────────────
# 닫는 결함 클래스: 이 파일들은 어느 manifest 에도 없어 pm_update 가 안 덮고, add-harness 재실행도
# 기존 값이 template 과 다르면 byte 보존한다 — 상류의 *동작* fix(훅 차단→비차단 같은)가 기존
# 채택자에 도달할 채널이 0 이다(채택자 실측: 두 릴리스 전 차단판을 들고 운영).
#
# 해법은 3-way 대조다. 설치가 내려놓은 template 해시를 원장에 남기면 다음 동기에서 "채택자가
# 손댔는가" 를 판정할 수 있다 — dest 해시 == 원장 해시면 무편집이므로 백업 후 갱신해도 잃을 값이
# 없고, 다르거나 원장이 없으면 무조건 보존한다(**하한선: 채택자 커스텀은 절대 안 덮는다**).
#
# 원장을 install.json 에 넣지 않는 이유: 그 기록은 문서를 통째 재작성해 구 엔진이 새 키를 지우고,
# schema bump 는 구 엔진의 설치 판정을 추론으로 강등시킨다. 별도 소파일이 두 위험을 다 피한다.
# 원장은 flavor manifest 에 **등재하지 않는다** — 등재하면 byte-copy 가 채택자 원장을 상류 값으로
# 덮어 판정 전체가 거짓이 된다(인스턴스 상태 파일·출하 템플릿 트리에도 없다).
ADAPTER_BASELINE_RELPATH = Path(".project_manager") / "adapter_baseline.json"
ADAPTER_BASELINE_SCHEMA = 1


class AdapterConfigJudgment(NamedTuple):
    """instance-owned config 한 개의 현재 판정 (읽기 전용·write 경로가 이걸 소비한다).

    status:
      `in-sync`    dest == template (할 일 없음·원장만 backfill 대상)
      `unedited`   dest != template 인데 원장이 지금 dest 를 기록한 값 → 상류가 바뀐 것(무편집)
      `edited`     dest != template 이고 원장이 다른 내용을 기록 → 채택자 편집(보존)
      `unrecorded` dest != template 이고 원장 항목 없음 → 판정 불가(보존·안전 기본값)

    `==`/`!=` 와 두 해시 필드는 전부 **내용**(`content_sha256` — 개행 정규화 후) 축이다. 체크아웃이
    개행만 바꾼 파일은 같은 내용이므로 `in-sync` 다. 원장 대조만 예외로 구 축(raw bytes)을 함께
    인정한다(`_baseline_attests_dest` — 무편집 증거가 더 강한 축이고, 기록은 현재 축으로 수렴).
    """
    relpath: str
    harness: str
    mode: str
    status: str
    template: Path
    dest_sha256: str
    baseline_sha256: str | None


class AdapterConfigChannelUnavailable(RuntimeError):
    """기존 managed dest를 판정할 source/dest byte 기준이 없어 완료를 증명할 수 없음."""


def _adapter_config_path_exists(path: Path) -> bool:
    """경로 엔트리가 존재하는가 — broken symlink/비-regular도 ``True``.

    managed config의 완료 게이트는 "읽을 수 있는 regular file인가"와 "그 이름이 트리에
    존재하는가"를 분리해야 한다. ``Path.is_file``/``exists``는 디렉터리와 broken symlink를
    모두 부재로 접어 판정 대상 0 false-green을 만든다. 존재 여부 자체를 확인할 수 없는 IO 오류도
    완료를 증명하지 못하므로 unavailable로 보수 처리한다.
    """
    try:
        Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AdapterConfigChannelUnavailable(
            f"managed 어댑터 config 경로 존재 여부 unavailable: {path} ({exc})") from exc
    return True


def _existing_managed_adapter_harnesses(dest_root: Path) -> list[str]:
    """설치 추론과 별개로 기존 managed 경로가 지목하는 하네스(정규 registry 순서)."""
    found = set()
    for harness, channel in ADAPTER_CONFIG_CHANNEL.items():
        for relpath, mode in channel.items():
            if (mode == ADAPTER_CONFIG_MANAGED
                    and _adapter_config_path_exists(Path(dest_root) / relpath)):
                found.add(harness)
                break
    return [name for name in REGISTERED_HARNESSES if name in found]


def file_sha256(path: Path) -> str | None:
    """파일 **raw bytes** 의 sha256 hex — 읽을 수 없으면 None(fail-soft).

    bytes 결속(판정한 그 bytes 를 쓰기 직전에 재확인)·쓰기 검증 전용이다. **내용 동일성 판정은
    `content_sha256`** 을 쓴다 — 이 다이제스트는 개행 표기까지 내용으로 세기 때문이다."""
    digest = hashlib.sha256()
    try:
        with _open_shared(Path(path), binary=True) as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


# ── 내용 동일성 판정 다이제스트 ───────────────────────────────────────────────
# 어댑터 config 원장은 "엔진이 이 **내용**을 깔았다" 를 기록한다. 그 판정을 raw bytes 해시로 하면
# `core.autocrlf=true` 체크아웃이 개행만 바꾼 파일을 채택자 손편집으로 오판하고, Windows 채택자는
# 어댑터 config 전량이 `edited` 로 분류돼 자동 갱신 궤도에서 이탈한다(`--check` 영구 red).
# 그래서 **판정은 개행 정규화 후**, **쓰기는 bytes verbatim**(`_atomic_write_dest_bytes`)으로 축을
# 나눈다 — 위 "개행 표기 보존 왕복" 절의 판정/쓰기 축 분리와 같은 원칙이다.
def content_sha256(path_or_bytes) -> str | None:
    """내용 동일성 판정용 sha256 hex — 텍스트는 개행을 LF 로 접은 뒤 해시한다.

    인자는 경로 또는 이미 읽은 bytes 다(설치할 payload 를 그대로 넘길 수 있다). 경로를 읽을 수
    없으면 None(`file_sha256` 과 같은 fail-soft). UTF-8 로 디코딩되지 않는 바이너리는 정규화
    대상이 아니므로 raw bytes 를 해시한다(`\\r\\n` 이 개행이 아니라 데이터일 수 있다).

    LF 파일에서는 값이 `file_sha256` 과 같다(유효 UTF-8 은 디코드/인코드 왕복이 bytes 보존).
    구 원장 항목이 그대로 유효한 이유이자, 달라진 항목이 무변경 backfill 로 재기록되는 근거다.
    대상은 config 크기 파일이라 통째로 읽는다(스트리밍이 필요한 큰 파일은 `file_sha256`)."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        raw = bytes(path_or_bytes)
    else:
        try:
            raw = _read_bytes_shared(Path(path_or_bytes))
        except OSError:
            return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return hashlib.sha256(raw).hexdigest()
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _baseline_attests_dest(recorded, dest_path: Path,
                           dest_content_sha256: str) -> bool:
    """원장 값이 **지금 dest 그대로**를 기록한 것인가 (= 기록 시점 이후 무편집).

    인정하는 축은 둘이고 둘 다 "편집이 없었다" 의 증거다:
      · **현재 축**(`content_sha256`) — 개행 표기와 무관한 내용 동일.
      · **구 축**(`file_sha256` — raw bytes) — 판정을 내용 축으로 옮기기 전 엔진이 기록한 값.
        저장 값이 dest **raw** 해시와 같다는 건 그 파일의 bytes 가 기록 시점 그대로라는 뜻이라,
        내용 동일성보다 오히려 강한 증거다(채택자가 손댔다면 raw 해시부터 달라진다).

    구 축이 살아 있는 형상은 **상류 체크아웃까지 CRLF 인 Windows manager 로 설치한 채택자**다 —
    그 원장에는 CRLF raw 다이제스트가 들어갔고, 그 파일의 template 이 그 뒤 바뀌면 dest 가
    template 과 달라 in-sync backfill 이 닿지 않는다. 이 판정이 없으면 설치 이후 아무것도 안 한
    채택자가 영구히 `edited`(채택자 편집 — 보존)로 오판돼 상류 동작 fix 를 못 받는다.

    `record_adapter_baseline` 이 **같은 판정**으로 그 항목을 현재 축으로 재기록한다(무변경
    마이그레이션·판정 사본 0). 판정만 두 축을 보고 기록은 한 축으로 수렴하므로 구 축은 원장에
    누적되지 않는다."""
    if not isinstance(recorded, str):
        return False
    if recorded == dest_content_sha256:
        return True
    return recorded == file_sha256(dest_path)


class AdapterBaselineDocument(NamedTuple):
    """원장 파싱 결과 + 판독 사유 — 읽기(해석)와 쓰기(덮어쓰기)가 **같은 판정**을 본다.

    설치 기록(`InstallReceiptDocument`)과 같은 형상이다: schema 판정을 문서 단계로 올려야
    "해석 못 함" 과 "덮어써도 됨" 이 갈리지 않는다(해석 불가를 빈 값으로 접고 이 엔진 형식으로
    다시 쓰면 미래 형식 데이터가 파괴된다)."""
    document: dict | None
    status: str  # ok | absent | unreadable | corrupt | newer-schema


def _load_adapter_baseline_document(dest_root: Path) -> AdapterBaselineDocument:
    """원장 파일을 파싱한 **원본 문서 + 판독 사유** (부작용 0)."""
    try:
        text = read_dest_text(Path(dest_root), ADAPTER_BASELINE_RELPATH)
    except FileNotFoundError:
        return AdapterBaselineDocument(None, "absent")
    except (OSError, UnsafeDestPathError, UnicodeDecodeError):
        return AdapterBaselineDocument(None, "unreadable")
    try:
        data = json.loads(text)
    except ValueError:
        return AdapterBaselineDocument(None, "corrupt")
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return AdapterBaselineDocument(None, "corrupt")
    schema = data.get("schema")
    if isinstance(schema, int) and schema > ADAPTER_BASELINE_SCHEMA:
        return AdapterBaselineDocument(data, "newer-schema")
    return AdapterBaselineDocument(data, "ok")


def read_adapter_baseline(dest_root: Path) -> dict:
    """판정용 원장 — 해석할 수 없는 상태(부재·손상·상위 schema)는 빈 원장으로 본다.

    fail-soft 가 안전한 이유: 원장은 *갱신해도 되는가* 를 여는 열쇠라, 못 읽으면 아무것도 갱신하지
    않는 쪽(보존 + 보고)으로 떨어진다. ⚠ **빈 원장은 '덮어써도 된다' 는 뜻이 아니다** — 쓰기 판정은
    `_load_adapter_baseline_document` 의 사유를 직접 본다(상위 schema 는 쓰기 거부)."""
    loaded = _load_adapter_baseline_document(dest_root)
    if loaded.status != "ok" or loaded.document is None:
        return {"schema": ADAPTER_BASELINE_SCHEMA, "files": {}}
    return loaded.document


class InstanceOwnedTemplateTarget(NamedTuple):
    """설치 인스턴스가 소유하는 한 파일과 설치 때 선택된 template 좌표."""
    relpath: str
    harness: str
    mode: str
    weight: str | None
    template: Path | None
    template_git_relpath: str | None


def _recorded_instance_owned_template_coordinates(dest_root: Path) -> dict:
    """설치 영수증의 파일별 template 좌표 — 구/손상 기록은 빈 좌표다."""
    loaded = _load_install_receipt_document(Path(dest_root), quiet=True)
    if loaded.status != "ok" or loaded.document is None:
        return {}
    if _install_receipt_is_newer_schema(loaded.document):
        return {}
    coordinates = loaded.document.get(INSTALL_RECEIPT_TEMPLATE_COORDINATES_KEY)
    return coordinates if isinstance(coordinates, dict) else {}


def _resolve_recorded_template_coordinate(
        source_root: Path, dest_relpath: str, coordinate) -> tuple[str, Path, str] | None:
    """영수증 좌표를 안전한 checkout 내부 source로 해소한다.

    install.json은 채택자가 편집할 수 있으므로 source를 그대로 현재 파일 read 경로로 쓰지 않는다.
    `templates/<등록 tree>/...` 아래이며 lite rename을 적용한 dst가 관측 대상과 정확히 같을 때만
    받아들인다. lite weight의 full source는 해당 tree에 lite 변종이 없을 때의 선언된 폴백이라
    허용하지만, full weight가 lite source를 가리키는 역조합은 거부한다.
    """
    if not isinstance(coordinate, dict):
        return None
    weight = coordinate.get("weight")
    source = coordinate.get("source")
    if weight not in WEIGHT_CHOICES or not isinstance(source, str) or "\\" in source:
        return None
    parts = source.split("/")
    template_dirs = {
        dirname
        for dirnames in HARNESS_TEMPLATE_DIRS.values()
        for dirname in dirnames
    }
    if (len(parts) < 3 or parts[0] != "templates" or parts[1] not in template_dirs
            or any(part in {"", ".", ".."} for part in parts)):
        return None
    source_template_rel = Path(*parts[2:])
    is_lite = source_template_rel.name.endswith(LITE_SUFFIX)
    mapped_dest = (
        _full_relpath_for_lite(source_template_rel) if is_lite else source_template_rel)
    if mapped_dest.as_posix() != dest_relpath or (weight == "full" and is_lite):
        return None
    git_relpath = "/".join(parts)
    return weight, Path(source_root).joinpath(*parts), git_relpath


def instance_owned_template_targets(
        dest_root: Path, source_root: Path) -> list[InstanceOwnedTemplateTarget]:
    """설치 하네스의 instance-owned 파일 전수(진입문서 ``none`` 포함).

    ``adapter_config_targets``는 실제 동기 채널이라 ``none``을 의도적으로 제외한다. 이 함수는
    반대로 *관측* 인벤토리이므로 ``INSTANCE_OWNED_ADAPTER_FILES`` 전량을 소비한다. 공존
    opencode+codex의 공유 ``AGENTS.md``는 최초 import와 같은 중립 소유 template(codex) 하나로
    접어 파일당 한 줄 계약을 지킨다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    coordinates = _recorded_instance_owned_template_coordinates(dest_root)
    names = installed_harnesses(dest_root, source_root)
    installed = set(names)
    neutral_owner_by_rel = {
        rel.as_posix(): owner
        for rel, (members, owner) in NEUTRAL_SHARED_ENTRY_DOCS.items()
        if members <= installed
    }
    out: list[InstanceOwnedTemplateTarget] = []
    seen: set[str] = set()
    for harness in names:
        for relpath in sorted(INSTANCE_OWNED_ADAPTER_FILES.get(harness, ())):
            owner = neutral_owner_by_rel.get(relpath, harness)
            if owner != harness:
                continue
            if relpath in seen:
                continue
            resolved = _resolve_recorded_template_coordinate(
                source_root, relpath, coordinates.get(relpath))
            weight, template, template_git_relpath = (
                resolved if resolved is not None else (None, None, None))
            out.append(InstanceOwnedTemplateTarget(
                relpath=relpath,
                harness=owner,
                mode=ADAPTER_CONFIG_CHANNEL[owner][relpath],
                weight=weight,
                template=template,
                template_git_relpath=template_git_relpath,
            ))
            seen.add(relpath)
    return out


def _instance_owned_fallback_rev(dest_root: Path) -> str | None:
    """진입문서 등 파일별 원장 rev가 없는 경로의 직전 동기 세대."""
    try:
        text = read_dest_text(
            Path(dest_root), Path(".project_manager") / "local.conf")
    except (OSError, UnsafeDestPathError, UnicodeDecodeError):
        return None
    value = _parse_conf_keys(text).get("upstream.rev", "").strip()
    return value or None


def _template_generation_size(old_lines: list[str], current_lines: list[str]) -> tuple[int, int, int]:
    """template 두 세대의 (+줄, -줄, 첫 차이 current 줄 번호)."""
    matcher = difflib.SequenceMatcher(a=old_lines, b=current_lines, autojunk=False)
    added = 0
    removed = 0
    first_diff: int | None = None
    for tag, old_start, old_end, current_start, current_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if first_diff is None:
            first_diff = current_start + 1
        removed += old_end - old_start
        added += current_end - current_start
    return added, removed, first_diff or 1


def instance_owned_template_delta_lines(
        dest_root: Path, source_root: Path, *,
        git_runner: GitRunner | None = None) -> list[str]:
    """instance-owned 파일의 baseline template 세대 ↔ 현행 세대 델타 요약.

    파일별 ``adapter_baseline.json.template_rev``를 우선하고, 항목이 없는 진입문서는 직전
    ``local.conf upstream.rev``로 폴백한다. 판정은 source checkout의 로컬 object DB만 읽고
    파일을 쓰거나 자동 병합하지 않는다. 기준이 없거나 도달하지 않으면 그것을 변경 없음으로
    접지 않고 전량 확인 경고 한 줄로 축약한다.

    노출 수명은 경로별 기준 원장을 따른다. 진입 문서처럼 ``upstream.rev``를 폴백하는
    경로는 성공한 동기 후 기준이 전진해 이후 요약에서 사라질 수 있다. 반면 report-drift·edited
    managed 파일은 보존된 ``adapter_baseline.json.template_rev``가 기준이다. pm-update가
    무편집 managed 파일을 자동 갱신·원장화한 뒤 이 판정을 호출하면 그 항목은 사라지고, 원장이
    전진하지 않은 report-drift·edited managed만 명시적 ``--accept`` 전까지 반복된다. 어느 쪽도
    지난 세대를 복구하는 durable backstop은 아니며, 그 역할은 백업/git이 담당한다.
    """
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    targets = instance_owned_template_targets(dest_root, source_root)
    if not targets:
        return []
    recorded = read_adapter_baseline(dest_root).get("files", {})
    fallback_rev = _instance_owned_fallback_rev(dest_root)
    # pm-update는 이 함수를 adapter 채널의 쓰기 후 최종 판정에서 호출한다. dirty source처럼
    # template_rev만으로 현행 bytes를 정확히 이름 붙일 수 없는 경우에도, managed 파일과 원장이
    # 모두 현행 template에 수렴했다면 같은 파일에 --accept를 다시 처방하지 않는다. 동기 직전의
    # unedited/edited·원장 미기록은 converged가 아니므로 기존 처방을 그대로 유지한다.
    managed_converged: set[str] = set()
    try:
        managed_converged = {
            judgment.relpath
            for judgment in judge_adapter_configs(dest_root, source_root)
            if (judgment.mode == ADAPTER_CONFIG_MANAGED
                and adapter_config_convergence_status(judgment) == "converged")
        }
    except (OSError, ValueError, AdapterConfigChannelUnavailable):
        # 세대 관측이 config 판정 실패를 숨기면 안 된다. 수렴을 증명하지 못한 경우는 아무 항목도
        # 자동 완료로 접지 않고 아래의 원장 기준 경고/처방을 보존한다.
        managed_converged = set()
    runner = git_runner if git_runner is not None else _real_upstream_git_runner()
    git_rc, git_out = runner([
        "-C", str(source_root), "rev-parse", "--is-inside-work-tree",
    ])
    if git_rc != 0 and "not a git repository" in git_out.lower():
        return ["ℹ️  이 소스는 git 이 아니라 세대 판정 비대상."]
    if git_rc == 0 and git_out.strip().lower() != "true":
        return ["ℹ️  이 소스는 git 이 아니라 세대 판정 비대상."]
    prefix_rc, prefix_out = runner([
        "-C", str(source_root), "rev-parse", "--show-prefix",
    ])
    # 영수증의 template 좌표는 source_root 상대지만 show/ls-tree의 path는 저장소 최상위
    # 상대다. source_root가 상위 저장소의 하위 디렉터리면 prefix를 붙이지 않은 조회는 모든
    # 과거 파일을 "없음"으로 오판한다. prefix 자체를 해소하지 못하거나 비정상 형상이면
    # 전량 추가로 접지 않고 기준 해소 불가로 내린다.
    source_prefix = prefix_out.rstrip("\r\n") if prefix_rc == 0 else ""
    prefix_path = PurePosixPath(source_prefix)
    if (prefix_rc != 0 or "\n" in source_prefix or "\r" in source_prefix
            or prefix_path.is_absolute()
            or any(part in {".", ".."} for part in prefix_path.parts)):
        return [
            "⚠️  인스턴스 소유 템플릿 기준 해소 불가 — "
            "판정 불가(변경 없음 아님)·전량 확인 권장."
        ]
    reachability: dict[str, bool] = {}
    findings: list[str] = []
    coordinate_unknown = False
    baseline_unknown = False
    baseline_unreachable = False

    for target in targets:
        if target.template is None or target.template_git_relpath is None:
            coordinate_unknown = True
            continue
        entry = recorded.get(target.relpath)
        entry_rev = entry.get("template_rev") if isinstance(entry, dict) else None
        baseline_rev = entry_rev.strip() if isinstance(entry_rev, str) else ""
        baseline_rev = baseline_rev or fallback_rev
        if not baseline_rev:
            baseline_unknown = True
            continue

        if baseline_rev not in reachability:
            rc, _out = runner([
                "-C", str(source_root), "cat-file", "-e",
                baseline_rev + "^{commit}",
            ])
            reachability[baseline_rev] = rc == 0
        if not reachability[baseline_rev]:
            baseline_unreachable = True
            continue

        try:
            # dirty source checkout도 현행 배달 후보인 on-disk bytes와 대조하는 것이 의도다.
            current_lines = _read_text_shared(target.template, encoding="utf-8", errors="replace").splitlines(keepends=True)
        except OSError:
            baseline_unreachable = True
            continue

        git_relpath = (prefix_path / target.template_git_relpath).as_posix()

        rc, old_text = runner([
            "-C", str(source_root), "show",
            f"{baseline_rev}:{git_relpath}",
        ])
        if rc == 0:
            old_lines = old_text.splitlines(keepends=True)
        else:
            # `git show rev:path` 비0 하나만으로는 과거 경로 부재와 git I/O·timeout·저장소
            # 손상을 구분할 수 없다. 같은 commit tree를 별도 조회해 **부재가 증명될 때만**
            # 신규 파일(기존 0줄)로 본다. tree 조회 실패 또는 경로가 있는데 blob show만 실패한
            # 경우는 기준 해소 불가다 — 전량 추가라는 거짓 요약을 만들지 않는다.
            tree_rc, tree_out = runner([
                "-C", str(source_root), "ls-tree", "--name-only", "-z",
                baseline_rev, "--", git_relpath,
            ])
            tree_paths = {
                line
                for nul_chunk in tree_out.split("\0")
                for line in nul_chunk.splitlines()
                if line
            }
            if tree_rc != 0 or git_relpath in tree_paths:
                baseline_unreachable = True
                continue
            old_lines = []
        if old_lines == current_lines:
            continue
        if target.mode == ADAPTER_CONFIG_MANAGED and target.relpath in managed_converged:
            # pm-update의 config 채널이 이미 "이번 동기에서 갱신"을 별도 한 줄로 보고한다.
            # 여기서는 최종 상태와 모순되는 --accept 재처방을 만들지 않는다.
            continue
        added, removed, first_diff = _template_generation_size(
            old_lines, current_lines)
        if target.mode == ADAPTER_CONFIG_NO_CHANNEL:
            remedy = (
                "현재 파일을 백업한 뒤 수동 병합 또는 ADOPT.md 절차로 재-import")
        else:
            remedy = f"pm-config sync-adapter-config --accept {target.relpath}"
        findings.append(
            f"⚠️  인스턴스 소유 템플릿 변경 — {target.relpath} · "
            f"+{added}/-{removed}줄 · 첫 차이 {first_diff}줄 · "
            f"처방(세대 변경; 기존 config 채널 보고는 내용 drift): {remedy}"
        )

    prefix: list[str] = []
    if coordinate_unknown:
        prefix.append(
            "⚠️  인스턴스 소유 템플릿 좌표 미기록·수동 확인 — "
            "자동 비교 생략(변경 없음 아님).")
    if baseline_unknown:
        prefix.append(
            "⚠️  인스턴스 소유 템플릿 기준 미기록 — 판정 불가(변경 없음 아님)·전량 확인 권장.")
    if baseline_unreachable:
        prefix.append(
            "⚠️  인스턴스 소유 템플릿 기준 해소 불가 — 판정 불가(변경 없음 아님)·전량 확인 권장.")
    return [*prefix, *findings]


def _write_adapter_baseline(dest_root: Path, document: dict,
                            root_identity: tuple | None = None) -> bool:
    """원장을 dest 안전 쓰기로 기록 — 내용이 바뀌었으면 True.

    **상위 schema 기록은 덮지 않는다**(설치 기록과 동형): 해석할 수 없다고 판정에서 뺀 문서를 이
    엔진 형식으로 다시 쓰면 신 엔진의 원장이 통째로 파괴된다(읽기 거부와 쓰기 거부는 짝이어야
    한다). 그 밖의 기록 실패는 경고 후 False 다 — 호출부가 성공을 주장하지 않도록 결과를 확인한다."""
    if _load_adapter_baseline_document(dest_root).status == "newer-schema":
        print(f"경고: 어댑터 config 원장 schema 가 이 엔진(지원 상한 {ADAPTER_BASELINE_SCHEMA})보다 "
              f"새로워 갱신하지 않습니다 — 기존 원장을 보존합니다 "
              f"({Path(dest_root) / ADAPTER_BASELINE_RELPATH}). 엔진을 갱신한 뒤 다시 실행하세요.",
              file=sys.stderr)
        return False
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        if read_dest_text(Path(dest_root), ADAPTER_BASELINE_RELPATH,
                          root_identity=root_identity) == text:
            return False  # 멱등 — 같은 내용 재기록은 byte churn 0.
    except (OSError, UnsafeDestPathError, UnicodeDecodeError):
        pass  # 부재·해독 불가는 아래에서 새로 쓴다.
    try:
        with _fdopen_text(
                _open_dest_relative_nofollow(
                    Path(dest_root), ADAPTER_BASELINE_RELPATH,
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    root_identity=root_identity, regular_only=True),
                "w", newline="\n") as handle:
            handle.write(text)
    except (OSError, UnsafeDestPathError) as exc:
        print(f"경고: 어댑터 config 원장을 남기지 못했습니다 "
              f"({Path(dest_root) / ADAPTER_BASELINE_RELPATH}: {exc}) — 다음 동기가 보고 모드로 "
              f"내려갑니다(파일은 그대로).", file=sys.stderr)
        return False
    return True


def adapter_config_targets(dest_root: Path, source_root: Path,
                           harnesses=None) -> list[tuple[str, str, str, Path]]:
    """(relpath, harness, mode, template 경로) — 채널을 가진 instance-owned config 전수.

    대상은 **설치 하네스**(`installed_harnesses`)와 **기존 managed 경로가 지목한 하네스**의
    합집합에서 파생하고 분류(`ADAPTER_CONFIG_CHANNEL`)가 mode 를 준다. 후자는 설치 영수증과 완전
    adapter shape가 없는 partial recovery에서도 managed 파일 하나를 빈 판정으로 접지 않는
    보수 경계다. report-only 경로만으로는 하네스를 추론하지 않는다.

    `none` 분류와 **report-only** template 부재는 대상에서 빠진다. 반면 existing managed dest의
    template 부재/읽기 실패는 판정 불능이므로 ``AdapterConfigChannelUnavailable``로 fail-loud한다
    (빈 judgments false-green 차단)."""
    dest_root = Path(dest_root)
    source_root = Path(source_root)
    if harnesses is not None:
        names = list(harnesses)
    else:
        installed, authority = _installed_harnesses_with_authority(dest_root, source_root)
        inferred = set(installed)
        # 유효 receipt는 설치 사실의 명시 단일 진실이다. 그 목록이 부정한 하네스를 우연히 함께
        # 놓인 foreign managed 파일 하나로 되살리면 `--accept`가 외래 파일 overwrite를 권한다.
        # 경로 보강은 receipt 부재/손상으로 증거 추론에 내려간 partial recovery에서만 허용한다.
        if authority == "inferred":
            inferred.update(_existing_managed_adapter_harnesses(dest_root))
        names = [name for name in REGISTERED_HARNESSES if name in inferred]
    out: list[tuple[str, str, str, Path]] = []
    for harness in names:
        channel = ADAPTER_CONFIG_CHANNEL.get(harness, {})
        template_dir = HARNESS_TEMPLATE_DIRS.get(harness, (None,))[0]
        if template_dir is None:
            continue
        template_root = source_root / "templates" / template_dir
        for relpath in sorted(channel):
            mode = channel[relpath]
            if mode == ADAPTER_CONFIG_NO_CHANNEL:
                continue
            template = template_root / relpath
            dest_path = dest_root / relpath
            # 여기 `file_sha256` 은 내용 판정이 아니라 **가독성 probe** 다 — 값을 비교하지 않고
            #   None(읽기 실패)만 본다. 정규화 여부와 무관하므로 raw 스트리밍 해시를 그대로 쓴다.
            if not template.is_file() or file_sha256(template) is None:
                if (mode == ADAPTER_CONFIG_MANAGED
                        and _adapter_config_path_exists(dest_path)):
                    raise AdapterConfigChannelUnavailable(
                        f"managed 어댑터 config 비교 기준 unavailable: {relpath}의 source template을 "
                        f"읽을 수 없거나 찾을 수 없다 ({template})")
                continue  # report-only 또는 dest 후보 자체 없음 — 완료 게이트 비차단.
            out.append((relpath, harness, mode, template))
    return out


def judge_adapter_configs(dest_root: Path, source_root: Path,
                          harnesses=None) -> list[AdapterConfigJudgment]:
    """instance-owned config 전수의 현재 판정 (읽기 전용·부작용 0).

    pm_update 의 동기 채널과 `sync-adapter-config --list` 가 같은 판정을 본다(판정 사본 0).

    세 축(dest·template·원장) 모두 `content_sha256` 으로 본다 — 개행 표기만 다른 파일은 같은
    내용이다(체크아웃 변환을 채택자 편집으로 오판하지 않는다). 원장 대조는 구 축(raw bytes)으로
    기록된 항목까지 `_baseline_attests_dest` 로 함께 인정한다(마이그레이션은 기록 쪽 담당)."""
    dest_root = Path(dest_root)
    recorded = read_adapter_baseline(dest_root).get("files", {})
    out: list[AdapterConfigJudgment] = []
    for relpath, harness, mode, template in adapter_config_targets(
            dest_root, source_root, harnesses):
        dest_path = dest_root / relpath
        dest_hash = content_sha256(dest_path)
        if dest_hash is None:
            if (mode == ADAPTER_CONFIG_MANAGED
                    and _adapter_config_path_exists(dest_path)):
                raise AdapterConfigChannelUnavailable(
                    f"managed 어댑터 config 비교 기준 unavailable: dest를 읽을 수 없다 "
                    f"({dest_path})")
            continue  # dest 에 없거나 못 읽음 — 이 채널의 관심사가 아니다.
        entry = recorded.get(relpath)
        baseline = entry.get("sha256") if isinstance(entry, dict) else None
        if content_sha256(template) == dest_hash:
            status = "in-sync"
        elif baseline is None:
            status = "unrecorded"
        elif _baseline_attests_dest(baseline, dest_path, dest_hash):
            status = "unedited"
        else:
            status = "edited"
        out.append(AdapterConfigJudgment(
            relpath, harness, mode, status, template, dest_hash, baseline))
    return out


def adapter_config_convergence_status(judgment: AdapterConfigJudgment) -> str:
    """managed config 한 건의 완료 판정 — ``converged`` 또는 실행 가능한 red 상태.

    내용이 template 과 같다는 사실만으로는 미래 자동 갱신 궤도에 들어간 게 아니다. 원장에도
    **그 dest 해시**가 있어야 완료다. ``judge_adapter_configs`` 의 원시 판정은 표시/3-way 갱신에
    그대로 쓰고, 완료 게이트만 이 helper가 정규화한다(판정 사본 0).

    report-only 대상은 채택자 노브라 완료 게이트를 막지 않는다. 호출부는 필요하면 원시
    ``judgment.status`` 를 계속 출력한다.
    """
    if judgment.mode != ADAPTER_CONFIG_MANAGED:
        return "report-only"
    if judgment.status != "in-sync":
        return judgment.status
    if (not judgment.dest_sha256
            or judgment.baseline_sha256 != judgment.dest_sha256):
        # 수동 byte 교체 또는 원장 기록 실패. 내용은 같아도 durable 증거가 없으므로 red다.
        return "unrecorded"
    return "converged"


def unconverged_managed_adapter_configs(
        judgments: list[AdapterConfigJudgment]) -> list[tuple[AdapterConfigJudgment, str]]:
    """완료 게이트를 막는 managed 판정만 ``(judgment, normalized_status)`` 로 반환."""
    out = []
    for judgment in judgments:
        status = adapter_config_convergence_status(judgment)
        if status not in ("converged", "report-only"):
            out.append((judgment, status))
    return out


# ── 훅 세트 세대 정합 (판정 단일 진실 — pm_update/pm_config 는 소비만) ────────────
# 판정 결과의 두 축: 무엇이 어긋났나(kind)와 어느 쪽이 뒤처졌나(remedy). 처방이 갈리는 건
# 후자 하나뿐이라 상수로 못박는다 — 문자열을 호출부가 지어내면 처방이 두 벌이 된다.
HOOK_SET_MISSING_SCRIPT = "missing-hook-script"
HOOK_SET_UNSUPPORTED_FLAG = "unsupported-hook-flag"
HOOK_SET_REMEDY_ENGINE_STALE = "engine-stale"     # dest 엔진 파일이 config 보다 구세대
HOOK_SET_REMEDY_CONFIG_AHEAD = "config-ahead"     # config 가 이 엔진 세대 자체보다 앞섬
HOOK_SET_REMEDY_UNKNOWN = "unknown"               # 비교 기준(상류 template) 미해소


class AdapterHookSetFinding(NamedTuple):
    """훅 세트 세대 불일치 1건. 빈 목록 = 정합(락아웃 위험 없음)."""
    harness: str
    config_relpath: str
    kind: str
    subject: str                  # 문제의 스크립트 relpath 또는 플래그
    unmet_paths: tuple[str, ...]  # 그 세대로 설치돼 있지 않은 dest 파일
    remedy: str
    detail: str


# ── 세대 선언 해소 — **모든 소비자의 단일 지점** ──────────────────────────────
# 훅 세트 선언(`ADAPTER_HOOK_SET`)은 pm_import 안에 산다. 그래서 *실행 중인* 엔진 옆 사본은
# 업그레이드에서 정의상 한 세대 뒤다 — 상류가 새 플래그·새 결합 묶음을 도입하는 **첫 전파**에서
# 게이트가 그 세대를 모른 채 통과한다(T-0606 라운드에 같은 클래스가 3회 반복 발견). 불변식:
#
#   훅 세트 세대 선언의 **게이트 소비자는 항상 상류 세대 선언으로 판정한다.**
#
# 소비자(원자 write 판정자·수용 게이트·부분 전파 가드·동기 채널 판정)는 전부 이 함수 하나로
# 선언을 받는다. 판정 코드는 실행 중 엔진 것을 그대로 쓰고 **선언 데이터만** 상류에서 온다 —
# 상류 판정 함수를 호출하면 그쪽 버그·시그니처 변화까지 실행 경로에 들어온다.
HOOK_SET_ORIGIN_UPSTREAM = "상류"
HOOK_SET_ORIGIN_LOCAL = "로컬"
HOOK_SET_ORIGIN_UNRESOLVED = "미해소"


class HookSetGeneration(NamedTuple):
    """세대 선언 해소 결과. `declarations` 가 None 이면 판정할 근거가 없다(게이트는 멈춘다).

    `source_sha256` 은 **선언을 읽어 온 파일의 그 시점 bytes 해시**다 — 게이트가 판정에 쓴 세대
    자체를 쓰기 직전에 재검증하는 결속 축이다(template 해시만 묶으면 "구 선언 판정 + 신 template"
    조합이 통과한다)."""
    declarations: dict | None
    origin: str
    reasons: tuple[str, ...] = ()
    source_sha256: str | None = None


def _hook_set_declaration_source(source_root) -> Path:
    """선언을 읽어 오는 파일 — 상류 트리의 pm_import(없으면 실행 중인 이 사본)."""
    return Path(source_root) / ".project_manager" / "tools" / "pm_import.py"


def _upstream_hook_set_declarations(
        source_root) -> tuple[dict | None, str | None, str | None]:
    """상류 트리 pm_import 의 선언 테이블 — (테이블, 실패 사유, 그 파일 bytes 해시).

    **해시와 실행되는 선언 코드는 한 벌이어야 한다.** 상류 파일을 제자리에서 import 하면 그 결속이
    바이트코드 캐시 한 겹으로 끊긴다: 엔진 전파는 `copy2`(mtime 보존)로 파일을 내려놓으므로 크기까지
    같은 사본이 앞 세대의 **timestamp 유효한 `.pyc`** 와 짝지어지는 창이 실재하고, 그러면 "최신 파일
    해시 + 구 선언 코드" 가 조용히 성립한다(게이트가 결속했다고 믿는 스냅샷이 거짓이 된다). 그래서
    읽어서 해시한 그 bytes 를 **캐시가 없는 새 경로**에 내려놓고 거기서 로드한다 — 실행되는 선언은
    정의상 우리가 해시한 그 bytes 다. (file-location import 는 엔진 단일 경계인 중앙 로더로만 한다 —
    `exec(compile(...))` 직접 실행은 그 경계를 우회하므로 쓰지 않는다.)

    **상류가 실행 중인 바로 그 파일이어도 예외가 없다.** "자기 자신이면 메모리 적재 선언을 그대로
    쓴다" 는 단축은 정확히 같은 결속 붕괴를 남긴다: 이 프로세스가 적재한 선언은 *import 시점*의
    코드인데(그 자체가 stale `.pyc` 였을 수 있다) 해시는 *지금* 디스크 bytes 의 것이다. 자기 갱신
    실행은 그 파일을 실행 도중 덮으므로(pm-update 가 하는 일이 그것이다) 두 시점이 갈리는 창이
    상시 열려 있다 — 빠른 경로가 곧 "신 해시 + 구 선언" 통과다."""
    path = _hook_set_declaration_source(source_root)
    if not path.is_file():
        return None, f"상류 pm_import 부재({path})", None
    # 해시는 **파일 bytes 를 그때그때** 읽어 만든다 — 모듈 캐시를 태우면 상류가 바뀌어도 같은
    #   값이 나와 결속이 무력해진다. 그 bytes 를 그대로 들고 있다가 로드에도 쓴다.
    try:
        payload = _read_bytes_shared(path)
    except OSError as exc:
        return None, f"상류 pm_import 읽기 실패({type(exc).__name__}: {exc})", None
    digest = hashlib.sha256(payload).hexdigest()
    cache_key = f"_pm_hook_set_upstream:{os.path.realpath(path)}:{digest}"
    module = sys.modules.get(cache_key)   # 같은 bytes 는 실행당 한 번만 로드한다.
    if module is None:
        # 스테이징 **정리는 로드 성공/실패와 분리**한다. `with TemporaryDirectory` 로 묶으면 삭제
        #   실패(핸들 잠금·AV 스캔 — Windows 실 클래스)가 `__exit__` 에서 올라와 "상류 로드 실패" 로
        #   분류되고, 그 사유 한 줄이 mutation 게이트를 근거 없이 fail-closed 로 떨어뜨린다. 선언은
        #   이미 읽혔다 — 뒷정리 실패가 그 사실을 뒤집지 않는다(best-effort).
        staging = tempfile.mkdtemp(prefix=".pm_hook_set_gen.")
        try:
            staged = Path(staging) / "pm_import.py"
            staged.write_bytes(payload)
            module = _load_module_from_path(
                staged, "pm_import.py", allow_unverified=True, cache=True,
                cache_key=cache_key)
        except Exception as exc:  # noqa: BLE001 — 상류 사본 손상은 사유로 내리고 호출부가 판정한다.
            return None, f"상류 pm_import 로드 실패({type(exc).__name__}: {exc})", None
        finally:
            try:
                _force_rmtree(Path(staging))
            except OSError as exc:
                # 정리 실패를 로드 실패로 승격하지 않되(위 주석), 침묵하지도 않는다 —
                #   남은 스테이징은 사용자가 지울 수 있게 자리를 알린다.
                print(f"경고: 훅 세트 스테이징 정리 실패 — 임시 디렉토리가 남았습니다: "
                      f"{staging} ({type(exc).__name__}: {exc})", file=sys.stderr)
    table = getattr(module, "ADAPTER_HOOK_SET", None)
    if not isinstance(table, dict) or not table:
        return None, "상류 pm_import 에 훅 세트 선언 부재(그 세대는 이 개념이 없다)", None
    return table, None, digest


def hook_set_declarations(source_root=None, *, required: bool = False) -> HookSetGeneration:
    """훅 세트 세대 선언을 해소한다 — 상류 우선, 폴백은 호출부 성격이 정한다.

    `required=False`(조회·보고): 상류를 못 읽으면 로컬 선언으로 내려간다. 판정을 아예 잃는 것보다
    한 세대 뒤 선언으로라도 보는 편이 낫고, 사유는 `reasons` 로 올라가 호출부가 알린다.

    `required=True`(**mutation 게이트**): 상류를 못 읽으면 로컬로 내려가지 않는다(`declarations`
    None). 확인되지 않은 세대 위에서 파일을 바꾸는 것이 정확히 이 게이트가 막으려는 상태다 —
    모르면 멈춘다(fail-closed).
    """
    reasons: list[str] = []
    if source_root is not None:
        table, reason, digest = _upstream_hook_set_declarations(source_root)
        if table is not None:
            return HookSetGeneration(table, HOOK_SET_ORIGIN_UPSTREAM, (), digest)
        if reason:
            reasons.append(reason)
    else:
        reasons.append("상류 좌표 미지정")
    if required:
        return HookSetGeneration(None, HOOK_SET_ORIGIN_UNRESOLVED, tuple(reasons))
    if ADAPTER_HOOK_SET:
        return HookSetGeneration(ADAPTER_HOOK_SET, HOOK_SET_ORIGIN_LOCAL, tuple(reasons))
    return HookSetGeneration(None, HOOK_SET_ORIGIN_UNRESOLVED, tuple(reasons))


def hook_set_query_fallback_lines(generation) -> list[str]:
    """조회 축 강등 사유 안내 줄 — 상류 선언을 못 읽고 내려간 판정이면 1줄, 아니면 빈 목록.

    문구는 처방(`hook_set_remedy_lines`)과 같은 규약으로 **여기 단일 진실**이다 — pm-update 와
    `pm-config --check` 가 같은 상태를 서로 다른 문장으로 말하면 채택자가 두 게이트를 다른 것으로
    읽는다. 조회 축은 관대 계약이라 이 줄은 차단이 아니라 침묵 제거다(mutation 축은 fail-closed)."""
    reasons = getattr(generation, "reasons", ())
    if not reasons:
        return []
    return ["[경고] 상류 훅 세트 세대 선언을 읽지 못해 설치본 선언으로 판정한다 — 이번 상류가 "
            "새로 들여오는 플래그·결합 묶음은 이 판정에 보이지 않는다(green 이어도 무판정 구간이 "
            f"있다). {' / '.join(reasons)}"]


def hook_set_namespaces(declarations=None) -> tuple[str, ...]:
    """훅 세트가 사는 **어댑터 네임스페이스 접두** — 선언 데이터에서 파생(하드코딩 0).

    상류 세대를 확인할 수 없을 때 fail-closed 대상을 정하는 축이다. 그 상태에서는 로컬 선언의
    결합 묶음 membership 으로 좁힐 수 없다 — 상류에만 있는 묶음은 정의상 로컬이 모르므로, 좁히면
    바로 그 케이스가 빠져나간다. 그래서 판정 단위를 "이 하네스의 훅이 사는 영역" 으로 올린다."""
    out: set[str] = set()
    for spec in _hook_set_table(declarations).values():
        if spec is None:
            continue
        for declared in spec.live_files:
            head = declared.strip("/").split("/", 1)[0]
            if head:
                out.add(head)
    return tuple(sorted(out))


def _hook_set_table(declarations) -> dict:
    """판정이 쓸 선언 테이블 — 미지정이면 이 엔진 선언(조회 기본값·게이트는 명시 전달)."""
    return ADAPTER_HOOK_SET if declarations is None else declarations


# 훅 항목이 커맨드를 싣는 키 — 플랫폼별로 갈린다. codex `hooks.json` 은 POSIX 용 `command` 와
# Windows 용 `commandWindows` 를 **함께** 싣고, 그 플랫폼에서 실제로 실행되는 건 후자다. 한쪽만
# 스캔하면 Windows 채택자의 세대 불일치가 통째로 판정 밖이 된다(같은 락아웃, 다른 플랫폼).
_HOOK_COMMAND_KEYS = ("command", "commandWindows")


def _hook_commands(document) -> list[str]:
    """config document 의 훅 커맨드 전수 — `hooks.<event>[].hooks[].{command,commandWindows}`.

    claude `settings.json` 과 codex `hooks.json` 이 같은 형상이라 추출 규칙이 하나다. 형상이
    어긋난 항목은 조용히 건너뛴다(채택자 소유 파일이라 임의 구조가 정상 범위)."""
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        return []
    out: list[str] = []
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                for key in _HOOK_COMMAND_KEYS:
                    command = entry.get(key)
                    if isinstance(command, str) and command.strip():
                        out.append(command)
    return out


def _split_hook_command(command: str) -> list[str]:
    """훅 커맨드 → 토큰. 따옴표가 깨져 shlex 가 못 읽으면 공백 분할로 내려간다(판정 포기 0)."""
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _hook_script_relpath(token: str) -> str | None:
    """훅 커맨드 토큰 → dest 기준 POSIX relpath. 해소 불가(절대경로·미지 변수)면 None."""
    text = token.strip().replace("\\", "/")
    for prefix in _HOOK_COMMAND_ROOT_TOKENS:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    if not text or text.startswith("/") or "$" in text or text.startswith(".."):
        return None
    return text


# 인터프리터를 앞세우는 훅 커맨드(`bash <path> --flag`·`py -3 <path>`)의 첫 토큰. 목록은 유한하고
# basename 소문자로 대조한다(`/usr/bin/bash`·`bash.exe` 포함) — 표기가 달라도 **실행되는 스크립트**가
# 판정 단위라, 이 형태를 못 읽으면 같은 훅이 표기 하나로 판정에서 빠진다.
# 의도된 경계 2가지(보수적 miss — 거짓 red 없음·판정 누락만): 하나는 이 목록 밖 래퍼 선행(`env`·
# `command`·`winpty` 등)으로, 그 토큰이 live_files 와 안 맞아 판정 대상 밖으로 떨어진다. 다른 하나는 분해가
# POSIX shlex 라 인용 없는 백슬래시 경로(`...\x.sh`)는 이스케이프로 먹혀 miss 다 — 출하 config 는
# 전부 forward slash/인라인이라 실영향 0 이며, 확장은 실수요(백슬래시 훅 커맨드 출하) 때 한다.
_HOOK_COMMAND_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "dash", "python", "python3", "py", "node", "pwsh", "powershell",
})


def _hook_script_and_arguments(tokens: list[str]) -> tuple[str | None, list[str]]:
    """훅 커맨드 토큰 → (스크립트 relpath, 그 뒤 인자). 해소 불가면 (None, []).

    직접 실행(`<path> --flag`)과 인터프리터 선행(`bash <path> --flag`)이 **같은 판정**을 타야 한다 —
    실행되는 스크립트와 그 스크립트가 받는 플래그는 표기와 무관하게 같기 때문이다. 인터프리터 뒤의
    옵션(`py -3.12 <path>`)은 건너뛴다(스크립트는 첫 비-옵션 토큰이다)."""
    if not tokens:
        return None, []
    head = tokens[0].strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head.endswith(".exe"):
        head = head[:-len(".exe")]
    if head not in _HOOK_COMMAND_INTERPRETERS:
        return _hook_script_relpath(tokens[0]), tokens[1:]
    for index in range(1, len(tokens)):
        if tokens[index].startswith("-"):
            continue                    # 인터프리터 옵션 — 스크립트는 아직 뒤다.
        return _hook_script_relpath(tokens[index]), tokens[index + 1:]
    return None, []                     # 인터프리터만 있고 스크립트가 없다(판정 대상 아님).


def _is_hook_set_file(relpath: str, spec: AdapterHookSetSpec) -> bool:
    """그 relpath 가 이 하네스의 훅 세트 파일인가 (파일 동일 또는 디렉토리 접두)."""
    for declared in spec.live_files:
        if declared.endswith("/"):
            if relpath.startswith(declared):
                return True
        elif relpath == declared:
            return True
    return False


def _hook_set_reference_prefix(relpath: str, spec: AdapterHookSetSpec) -> str | None:
    """훅 커맨드가 가리킨 경로 → 그 훅 세트가 놓인 **디렉토리 접두**(선언 좌표면 빈 문자열).

    선언 좌표(`.claude/…`)로 부르는 게 채택자 형상이지만, 같은 엔진 파일을 **다른 좌표**로 부르는
    토폴로지가 실재한다 — 제품 루트의 settings.json 은 자기 `.claude/` 에 훅이 없어
    `templates/claude_code/.claude/ctx_stop_hook.sh` 를 부른다. 좌표가 선언과 다르다는 이유로 그
    커맨드를 검사에서 빼면 그 트리는 세대 검사가 통째로 꺼진다(stale 드라이버가 green). 접두를
    뽑아 두면 플래그 요구 파일까지 같은 접두로 판정할 수 있다(체인 전체가 같은 자리에 산다).

    훅 세트 밖(관련 없는 스크립트·트리 밖 좌표)이면 None — 채택자 자작 훅은 판정 대상이 아니다."""
    if _is_hook_set_file(relpath, spec):
        return ""
    for declared in spec.live_files:
        marker = "/" + declared.rstrip("/")
        if declared.endswith("/"):
            index = relpath.find(marker + "/")
            if index > 0:
                return relpath[:index]
        elif relpath.endswith(marker) and len(relpath) > len(marker):
            return relpath[:-len(marker)]
    return None


def _with_hook_set_prefix(prefix: str, relpath: str) -> str:
    """선언 relpath 를 그 훅 세트가 실제로 놓인 좌표로 옮긴다(접두 없으면 그대로)."""
    return f"{prefix}/{relpath}" if prefix else relpath


def _hook_file_declares(path: Path, literal: str, *, unknown_supported: bool) -> bool:
    """설치된 훅 파일이 그 리터럴(플래그)을 담고 있는가 — 실행 없이 하는 세대 판정.

    훅을 실제로 실행해 보는 판정은 그 자체가 부작용(락아웃 재현)이라 쓸 수 없다. 파일 부재는
    항상 미지원이고, **판정 불가**(권한·바이너리·해독 실패)는 호출부가 방향을 고른다:

      `unknown_supported=True`  조회(report) — 판정 불가를 red 로 올리면 거짓 처방이 나간다.
      `unknown_supported=False` 수용(mutation) — 확인되지 않은 파일을 근거로 config 를 새 세대로
                                앞세우면 그게 곧 락아웃이다. 모르면 진행하지 않는다(fail-closed).
    """
    try:
        return literal in _read_text_shared(path, encoding="utf-8")
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError):
        return unknown_supported


def _hook_set_demands(document, spec: AdapterHookSetSpec, root: Path, *,
                      unknown_supported: bool = True
                      ) -> dict[tuple[str, str], tuple[str, ...]]:
    """그 config 가 요구하는 것 중 `root` 트리가 충족하지 못한 항목 — {(kind, subject): 미충족}.

    `root` 는 판정 대상 트리다: 채택자 dest 를 주면 "지금 설치된 세대가 이 config 를 감당하나",
    상류 template 루트를 주면 "이 엔진 세대가 애초에 그걸 줄 수 있나" 가 된다. 처방 분기(어느
    쪽이 뒤처졌나)를 같은 코어 하나로 판정하려고 트리를 파라미터로 뺐다.

    `unknown_supported` 는 판정 불가 파일의 방향이다(`_hook_file_declares` 참조) — 기본은 조회용
    관대 판정이고, 수용 게이트만 fail-closed 로 뒤집는다."""
    out: dict[tuple[str, str], tuple[str, ...]] = {}

    def _record(kind: str, subject: str, unmet: tuple[str, ...]) -> None:
        # 같은 플래그를 여러 좌표에서 부르면(선언 좌표 + template 하위 참조) 미충족을 합친다 —
        #   뒤 커맨드가 앞 판정을 덮으면 한쪽 좌표의 구세대가 조용히 사라진다.
        merged = dict.fromkeys(out.get((kind, subject), ()) + unmet)
        out[(kind, subject)] = tuple(merged)

    for command in _hook_commands(document):
        tokens = _split_hook_command(command)
        if not tokens:
            continue
        script_rel, arguments = _hook_script_and_arguments(tokens)
        if script_rel is None:
            continue
        prefix = _hook_set_reference_prefix(script_rel, spec)
        if prefix is None:
            continue  # 엔진 소유 훅 세트 밖 — 채택자 자작 훅은 판정 대상이 아니다.
        if not (Path(root) / script_rel).is_file():
            _record(HOOK_SET_MISSING_SCRIPT, script_rel, (script_rel,))
            continue  # 스크립트가 없으면 플래그 지원 여부는 물을 것도 없다.
        for token in arguments:
            required = spec.flag_support.get(token)
            if not required:
                continue  # 선언 밖 플래그 — 지원 여부를 기계가 알 수 없다(판정 유보).
            unmet = tuple(
                actual for actual in
                (_with_hook_set_prefix(prefix, rel) for rel in required)
                if not _hook_file_declares(Path(root) / actual, token,
                                           unknown_supported=unknown_supported))
            if unmet:
                _record(HOOK_SET_UNSUPPORTED_FLAG, token, unmet)
    return out


def _hook_set_template_root(source_root: Path | None, harness: str) -> Path | None:
    """그 하네스의 상류 template 루트 — 처방 분기의 비교 기준(해소 불가면 None)."""
    if source_root is None:
        return None
    template_dir = HARNESS_TEMPLATE_DIRS.get(harness, (None,))[0]
    if template_dir is None:
        return None
    root = Path(source_root) / "templates" / template_dir
    return root if root.is_dir() else None


def _read_hook_set_config(path: Path):
    """훅 세트 config 파싱 — 부재/파손이면 None(채택자 소유 파일이라 임의 상태가 정상 범위)."""
    try:
        return json.loads(_read_text_shared(path, encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _hook_set_finding(harness: str, spec: AdapterHookSetSpec, kind: str, subject: str,
                      unmet: tuple[str, ...], remedy: str) -> AdapterHookSetFinding:
    joined = ", ".join(unmet)
    if kind == HOOK_SET_MISSING_SCRIPT:
        detail = (f"{spec.config_relpath} 이 실행하는 훅 스크립트가 설치돼 있지 않다 "
                  f"({subject}) — 이 훅은 매 발화마다 실패한다")
    else:
        detail = (f"{spec.config_relpath} 이 넘기는 `{subject}` 를 설치된 훅 세트가 "
                  f"지원하지 않는다 ({joined}) — 구 드라이버는 미지원 플래그를 rc2 로 거부하고 "
                  f"그 rc 는 도구 차단으로 번역된다(락아웃)")
    return AdapterHookSetFinding(
        harness, spec.config_relpath, kind, subject, unmet, remedy, detail)


def judge_adapter_hook_sets(dest_root: Path, source_root: Path | None = None,
                            harnesses=None, *,
                            declarations=None) -> list[AdapterHookSetFinding]:
    """설치된 훅 세트가 채택자 config 가 요구하는 세대인지 판정 (읽기 전용·부작용 0).

    pm_update 동기 경로와 `sync-adapter-config` 가 같은 판정을 본다(판정 사본 0). `source_root`
    를 주면 처방까지 분기한다 — 상류 template 이 그 요구를 충족하면 dest 엔진 파일이 뒤처진
    것이고(pm-update 로 자가 수리), 상류도 못 주면 config 가 이 엔진 세대보다 앞선 것이다
    (채택자 소유라 엔진이 못 덮으므로 `sync-adapter-config --accept` 가 유일 채널).

    `declarations` 는 세대 선언 테이블이다(`hook_set_declarations`) — 게이트 호출부는 상류
    세대를 명시로 넘긴다. 미지정이면 이 엔진 선언(설치본)으로 판정한다."""
    dest_root = Path(dest_root)
    table = _hook_set_table(declarations)
    names = (list(harnesses) if harnesses is not None
             else installed_harnesses(dest_root, source_root))
    out: list[AdapterHookSetFinding] = []
    for harness in names:
        spec = table.get(harness)
        if spec is None or not spec.config_relpath:
            continue
        document = _read_hook_set_config(dest_root / spec.config_relpath)
        if document is None:
            continue
        dest_unmet = _hook_set_demands(document, spec, dest_root)
        if not dest_unmet:
            continue
        template_root = _hook_set_template_root(source_root, harness)
        template_unmet = ({} if template_root is None
                          else _hook_set_demands(document, spec, template_root))
        for (kind, subject), unmet in sorted(dest_unmet.items()):
            if template_root is None:
                remedy = HOOK_SET_REMEDY_UNKNOWN
            elif (kind, subject) in template_unmet:
                remedy = HOOK_SET_REMEDY_CONFIG_AHEAD
            else:
                remedy = HOOK_SET_REMEDY_ENGINE_STALE
            out.append(_hook_set_finding(harness, spec, kind, subject, unmet, remedy))
    return out


# ── 역방향 축: 이 엔진 세대가 기대하는 진입점이 config 에 있나 ───────────────────
# 위 `judge_adapter_hook_sets` 는 **config → 엔진** 한 방향만 본다(config 가 요구하는 플래그를
# 설치본이 감당하나). 그 방향만으로는 "진입점이 아예 없어서 앞으로 등록될 가드가 발화하지 않는" 상태가
# 판정 밖이다 — config 가 아무것도 요구하지 않으면 미충족도 0 이라 green 이다. 이 절이 반대
# 방향을 채운다.
#
# 판정은 **loud advisory** 다(차단 아님). config 는 채택자 소유이고 그 파일엔 실 노브가 있어,
# 진입점 부재를 red 로 올리면 의도적으로 훅을 끈 채택자의 흡수가 영구히 막힌다. 소견은 별도
# 채널로 올라가고 완료 게이트(`_adapter_hook_set_gate_failed`)는 이 축을 소비하지 않는다.
HOOK_ENTRYPOINT_MISSING = "missing-hook-entrypoint"
HOOK_ENTRYPOINT_STALE_DISPATCHER = "stale-hook-dispatcher"


class AdapterHookEntrypointFinding(NamedTuple):
    """진입점 역방향 소견 하나 — 무엇이 어디서 빠졌는지와 사람이 읽을 사유."""
    harness: str
    config_relpath: str
    kind: str
    event: str
    matcher: str | None
    dispatcher: str
    detail: str


def _entrypoint_groups(document, event: str) -> list:
    """config 의 그 이벤트 matcher-group 목록 (형상이 어긋나면 빈 목록)."""
    hooks = document.get("hooks") if isinstance(document, dict) else None
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return [group for group in groups if isinstance(group, dict)] if isinstance(groups, list) else []


def _group_commands(group) -> list[str]:
    """그 matcher-group 이 실행하는 커맨드 전수 (POSIX·Windows 양쪽)."""
    entries = group.get("hooks")
    if not isinstance(entries, list):
        return []
    return [entry[key] for entry in entries if isinstance(entry, dict)
            for key in _HOOK_COMMAND_KEYS
            if isinstance(entry.get(key), str) and entry[key].strip()]


def entrypoint_invocation(entrypoint: AdapterHookEntrypoint) -> str | None:
    """그 진입점이 커맨드에서 **호출해야 하는 값**(`--hook-dispatch PreToolUse`).

    플래그가 없는 진입점(claude 처럼 이벤트마다 커맨드 자체가 갈리는 형상)은 호출 값이 없다 —
    그 축의 세대 결합은 `flag_support` 가 따로 본다."""
    return None if not entrypoint.flag else f"{entrypoint.flag} {entrypoint.event}"


def _command_invokes_entrypoint(command: str, entrypoint: AdapterHookEntrypoint) -> bool:
    """**한 커맨드 문자열**이 디스패처를 그 이벤트로 실제 호출하는가.

    디스패처 경로만 보면 실행하지 않는 문자열(`printf .codex/pm_orch_codex.py`)이나 다른
    이벤트를 부르는 커맨드까지 진입점으로 인정된다 — 발화하지 않는 config 가 소견 0 으로
    통과하므로, 경로와 호출 값을 **같은 문자열 안에서 함께** 요구한다. 경로 판정이 포함
    여부인 것은 출하 커맨드가 인터프리터 해소를 담은 셸 한 줄(`if command -v python3 …`)이라
    argv 분해로는 스크립트가 안 나오고, 하네스가 해소하는 루트 토큰(`${CLAUDE_PROJECT_DIR}/`)
    같은 접두가 붙어도 같은 파일을 가리키기 때문이다."""
    normalized = command.replace("\\", "/")
    if entrypoint.dispatcher not in normalized:
        return False
    if not entrypoint.flag:
        return True
    # 이벤트 이름은 **경계까지** 대조한다 — 접미가 붙은 다른 이벤트(`PreToolUseLegacy`)를
    #   같은 값으로 세면 값 대조가 다시 포함 판정으로 되돌아간다.
    return re.search(
        rf"{re.escape(entrypoint.flag)}[\s=]+['\"]?"
        rf"{re.escape(entrypoint.event)}['\"]?(?![\w.-])",
        normalized) is not None


def _entrypoint_is_present(document, entrypoint: AdapterHookEntrypoint) -> bool:
    """그 진입점이 config 에 **값으로** 있는가 — matcher 일치 + 그 이벤트로의 디스패처 호출."""
    for group in _entrypoint_groups(document, entrypoint.event):
        if group.get("matcher") != entrypoint.matcher:
            continue
        if any(_command_invokes_entrypoint(command, entrypoint)
               for command in _group_commands(group)):
            return True
    return False


def _entrypoint_dispatcher_gap(dest_root: Path,
                               entrypoint: AdapterHookEntrypoint) -> str | None:
    """설치된 디스패처가 진입점이 넘기는 **값**을 감당하는가 — 못 하면 빠진 리터럴을 돌려준다.

    기준을 플래그 하나가 아니라 `--hook-dispatch <이벤트>` 두 값으로 두는 이유는, 플래그는
    아는데 그 이벤트는 모르는 세대가 실재하기 때문이다(진입점 집합이 늘어난 릴리즈의 흡수
    창). 그 조합에서 훅은 매 발화마다 폴백으로 빠지므로 config 판정과 같은 값으로 본다."""
    if not entrypoint.flag:
        return None
    path = dest_root / entrypoint.dispatcher
    for literal in (entrypoint.flag, entrypoint.event):
        if not _hook_file_declares(path, literal, unknown_supported=True):
            return literal
    return None


def judge_adapter_hook_entrypoints(dest_root: Path, source_root: Path | None = None,
                                   harnesses=None, *,
                                   declarations=None) -> list[AdapterHookEntrypointFinding]:
    """설치된 채택자 config 가 이 엔진 세대의 진입점을 갖고 있는지 판정 (읽기 전용·advisory).

    `source_root` 는 형제 API 와 시그니처를 맞추기 위한 자리다 — 이 축의 기준은 상류 트리가 아니라
    **선언**(`declarations`)이라 판정에 쓰지 않는다. config 부재·파손은 소견을 내지 않는다: 그건
    어댑터 config 채널이 이미 자기 문구로 말하는 상태이고, 여기서 겹쳐 말하면 처방이 두 벌이 된다.
    """
    dest_root = Path(dest_root)
    table = _hook_set_table(declarations)
    names = (list(harnesses) if harnesses is not None
             else installed_harnesses(dest_root, source_root))
    out: list[AdapterHookEntrypointFinding] = []
    for harness in names:
        spec = table.get(harness)
        # 구세대 선언 사본에는 이 필드가 없다 — 그 세대엔 판정할 진입점 개념이 아예 없다.
        entrypoints = getattr(spec, "entrypoints", ()) if spec is not None else ()
        if spec is None or not spec.config_relpath or not entrypoints:
            continue
        document = _read_hook_set_config(dest_root / spec.config_relpath)
        if document is None:
            continue
        for entrypoint in entrypoints:
            if not _entrypoint_is_present(document, entrypoint):
                out.append(AdapterHookEntrypointFinding(
                    harness, spec.config_relpath, HOOK_ENTRYPOINT_MISSING,
                    entrypoint.event, entrypoint.matcher, entrypoint.dispatcher,
                    f"{spec.config_relpath} 에 {entrypoint.event} 범용 진입점"
                    f"(matcher {entrypoint.matcher!r} → {entrypoint.dispatcher})이 없다 — "
                    f"이 이벤트에 앞으로 등록될 가드는 발화하지 않는다"
                    f"(옛 직결 배선이 남아 있으면 그 기능 자체는 계속 돈다)"))
                continue
            gap = _entrypoint_dispatcher_gap(dest_root, entrypoint)
            if gap is not None:
                out.append(AdapterHookEntrypointFinding(
                    harness, spec.config_relpath, HOOK_ENTRYPOINT_STALE_DISPATCHER,
                    entrypoint.event, entrypoint.matcher, entrypoint.dispatcher,
                    f"설치된 {entrypoint.dispatcher} 가 {entrypoint.event} 진입점이 넘기는 "
                    f"`{entrypoint_invocation(entrypoint)}` 를 감당하지 않는다"
                    f"(`{gap}` 미보유) — 훅이 매 발화마다 폴백으로 빠져 가드가 무음 통과한다"))
    return out


def hook_entrypoint_advisory_lines(
        finding: AdapterHookEntrypointFinding) -> list[str]:
    """진입점 소견의 처방 줄 — pm-update 와 형제 소비자가 같은 문구를 낸다(처방 사본 0)."""
    if finding.kind == HOOK_ENTRYPOINT_STALE_DISPATCHER:
        return [f"pm-update 로 {finding.dispatcher} 를 먼저 받아라"]
    return [f"pm-config sync-adapter-config --accept {finding.config_relpath} "
            f"(이 엔진 세대의 진입점을 담은 config 로 되돌린다)",
            "채택자가 그 이벤트의 훅을 의도적으로 끈 상태면 이 줄은 무시해도 된다(차단 아님)"]


def hook_set_remedy_lines(finding: AdapterHookSetFinding) -> list[str]:
    """처방 줄 — pm_update 와 pm_config 가 같은 문구를 낸다(처방 사본 0).

    manifest 소유 파일(래퍼·드라이버)은 엔진이 직접 고칠 수 있고, 채택자 소유 config 는 수용
    커맨드가 유일 채널이다. 순서는 항상 **엔진 파일 먼저** — 반대 순서가 지금 닫는 락아웃이다."""
    accept = (f"pm-config sync-adapter-config --accept {finding.config_relpath} "
              f"(백업 후 이 엔진 세대의 config 로 되돌린다)")
    # 지목할 파일이 없는 소견도 있다 — 상류 세대 자체를 못 읽어 **무엇이 미충족인지 열거할 수
    #   없는** blocker(`unmet_paths=()`)가 그렇다. 빈 괄호 `()` 를 붙이면 처방이 깨진 문장으로
    #   보이므로 괄호째 생략한다(사유는 `detail` 이 이미 말한다).
    engine_first = "pm-update 로 훅 세트 엔진 파일을 먼저 받아라"
    if finding.unmet_paths:
        engine_first += f" ({', '.join(finding.unmet_paths)})"
    if finding.remedy == HOOK_SET_REMEDY_ENGINE_STALE:
        return [engine_first]
    if finding.remedy == HOOK_SET_REMEDY_CONFIG_AHEAD:
        return [f"{finding.config_relpath} 가 이 엔진 세대보다 앞선 요구를 담고 있다 — {accept}"]
    return [engine_first, f"그래도 남으면 {accept}"]


class HookSetAcceptDecision(NamedTuple):
    """수용 전 세대 판정 결과 — 막는 사유와 **판정에 쓴 template bytes 의 해시**를 함께 낸다.

    blockers          비어 있지 않으면 호출부는 파일을 건드리지 않고 거부한다.
    template_sha256   판정 시점 template 내용의 해시(없으면 None).
    generation_sha256 판정에 쓴 **선언 소스**(상류 pm_import) 그 시점 bytes 해시.
    generation        판정에 쓴 선언의 출처(`HOOK_SET_ORIGIN_*`).
    reasons           상류 선언을 못 읽었을 때의 사유(호출부가 loud 로 낸다).

    두 해시는 **하나의 스냅샷**이다 — 호출부가 그대로 `accept_adapter_config` 에 넘겨 쓰기 직전에
    함께 재확인한다. template 만 묶으면 "선언 해소 뒤 상류가 통째로 갱신됐는데 그 config 파일만
    우연히 같은" 조합이 구 선언 판정으로 통과한다(어느 쪽이 변했든 중단해야 한다).
    """
    blockers: list
    template_sha256: str | None
    generation: str
    reasons: tuple[str, ...] = ()
    generation_sha256: str | None = None


def hook_set_accept_decision(dest_root: Path, source_root: Path, relpath: str, *,
                             declarations=None) -> HookSetAcceptDecision:
    """그 config 를 상류 값으로 수용해도 되는지 — 순서 게이트 판정 + 판정한 bytes 결속.

    판정 대상은 **들어올 config**(상류 template)의 요구다 — 수용은 dest config 를 그 세대로
    앞세우는 행위이므로, 그 세대의 래퍼·드라이버가 dest 에 이미 있어야 한다. 이 검사가 순서
    (엔진 파일 선행 · config 후행)를 기계가 강제하는 지점이다.

    두 축이 모두 fail-closed 다:
      - 읽을 수 없는 훅 파일은 "지원함" 으로 넘기지 않는다(`unknown_supported=False`).
      - 세대 선언은 **상류**여야 한다(`declarations` 필수) — 설치본 선언은 상류가 이번에 들여오는
        새 플래그를 모르므로, 그 위에서 config 를 앞세우면 게이트가 있으나 마나다. 상류를 못 읽어
        `declarations` 가 None 이면 판정 불가를 blocker 로 낸다(모르면 멈춘다).
    """
    dest_root = Path(dest_root)
    out: list[AdapterHookSetFinding] = []
    if not (dest_root / relpath).is_file():
        # 그 config 가 dest 에 없으면 수용 자체가 성립하지 않는다 — 순서 게이트가 아니라
        #   채널 판정이 낼 오류다(여기서 가로채면 엉뚱한 처방이 나간다).
        return HookSetAcceptDecision([], None, HOOK_SET_ORIGIN_UNRESOLVED, ())
    generation = (declarations if isinstance(declarations, HookSetGeneration)
                  else HookSetGeneration(declarations, HOOK_SET_ORIGIN_LOCAL, ()))
    template_sha256: str | None = None
    targets = {rel: template for rel, _h, _m, template
               in adapter_config_targets(dest_root, source_root)}
    template_path = targets.get(relpath)
    if template_path is not None:
        # 여기 `file_sha256` 은 내용 판정이 아니라 **설치할 상류 bytes 의 스냅샷 결속**이다 —
        #   수용 직전 재대조(`expected_template_sha256`)와 같은 raw 축이어야 결속이 성립한다.
        template_sha256 = file_sha256(template_path)
    if generation.declarations is None:
        blocker = AdapterHookSetFinding(
            "-", relpath, HOOK_SET_MISSING_SCRIPT, relpath, (), HOOK_SET_REMEDY_UNKNOWN,
            f"상류 세대 선언을 읽지 못해 {relpath} 가 요구하는 훅 세대를 검증할 수 없다 "
            f"({'; '.join(generation.reasons) or '사유 미상'})")
        return HookSetAcceptDecision(
            [blocker], template_sha256, generation.origin, generation.reasons,
            generation.source_sha256)
    # 스냅샷 해시를 못 만들면 결속이 조용히 생략된다(호출부의 `is not None` 가드) — mutation 축은
    #   모르면 멈춘다. **판정한 세대를 재확인할 수 없는 상태**를 blocker 로 올린다.
    #   상류 선언으로 판정한 경로에서만 본다 — 결속을 약속하는 게 그 경로이고, 구세대 강등이
    #   의도적으로 넘기는 None(설치본 선언 판정)은 계산 실패가 아니라 애초에 결속이 없는 계약이다.
    unreadable = [] if generation.origin != HOOK_SET_ORIGIN_UPSTREAM else [
        name for name, value in
        (("상류 config template", template_sha256),
         ("상류 세대 선언(pm_import)", generation.source_sha256))
        if value is None]
    if unreadable:
        blocker = AdapterHookSetFinding(
            "-", relpath, HOOK_SET_MISSING_SCRIPT, relpath, (), HOOK_SET_REMEDY_UNKNOWN,
            f"{', '.join(unreadable)} 의 해시를 만들지 못해 판정한 스냅샷을 쓰기 직전에 재확인할 "
            f"수 없다 — 검증 없이 {relpath} 를 교체하지 않는다")
        return HookSetAcceptDecision(
            [blocker], template_sha256, generation.origin, generation.reasons,
            generation.source_sha256)
    for harness, spec in generation.declarations.items():
        if spec is None or spec.config_relpath != relpath:
            continue
        template_root = _hook_set_template_root(source_root, harness)
        if template_root is None:
            continue
        document = _read_hook_set_config(template_root / spec.config_relpath)
        if document is None:
            continue
        for (kind, subject), unmet in sorted(
                _hook_set_demands(document, spec, dest_root,
                                  unknown_supported=False).items()):
            out.append(_hook_set_finding(
                harness, spec, kind, subject, unmet, HOOK_SET_REMEDY_ENGINE_STALE))
    return HookSetAcceptDecision(
        out, template_sha256, generation.origin, generation.reasons,
        generation.source_sha256)


def _entry_holds_path(declared: str, path: str) -> bool:
    """그 dest 경로가 선언 항목에 속하는가 (파일 항목은 동일·디렉토리 항목은 접두)."""
    norm = declared.rstrip("/")
    return path == norm or path.startswith(norm + "/")


def _entries_hit_by(group: tuple[str, ...], paths: set[str]) -> set[str]:
    """그 경로 집합이 건드리는 묶음 항목."""
    return {declared for declared in group
            if any(_entry_holds_path(declared, path) for path in paths)}


def hook_set_partial_update(updated_paths, pending_paths, *,
                            declarations=None) -> list[tuple[str, tuple, tuple]]:
    """이번 전파가 결합 묶음을 **반쪽만** 갱신하는가 — (harness, 남겨진 항목, 함께 옮길 항목).

    경로 스코프 전파(`--paths`)는 어댑터 채널을 끄므로 세대 검사가 전무하다. 그 상태에서 래퍼만
    옮기면 "신 래퍼 + 구 드라이버" 를 손수 만들어 놓고 rc0 로 끝난다(락아웃 생성). 판정 단위는
    **결합 묶음**(`coupled_groups`)이다 — 결합이 없는 파일(독립 relay 드라이버 등)까지 묶으면
    정당한 단건 전파를 막는다.

    입력은 **해소된 계획의 dest 좌표**다:
      `updated_paths` 이번 실행이 갱신할 경로 · `pending_paths` 스코프가 없었다면 갱신됐을 경로.
    원문 스코프 표기를 보면 `@source` 상류 좌표 요청이 선언과 교집합 0 으로 빠져 검사가 무발화하고,
    "묶음 전량이 스코프에 있나" 만 보면 **이미 최신인 형제**까지 요구해 거짓 거부가 된다. 그래서
    조건은 하나다: 이 묶음에서 뭔가 옮기는데(`updated`) **옮겨야 할 것이 남으면**(`pending`) 거부.

    `declarations` 는 세대 선언 테이블이다(`hook_set_declarations`) — 상류가 이번에 들여오는 새
    결합 묶음은 설치본 선언에 없으므로, 게이트 호출부는 상류 세대를 명시로 넘긴다."""
    table = _hook_set_table(declarations)
    updated = {str(path).replace("\\", "/").strip("/")
               for path in (updated_paths or []) if str(path).strip()}
    pending = {str(path).replace("\\", "/").strip("/")
               for path in (pending_paths or []) if str(path).strip()}
    if not updated:
        return []
    out: list[tuple[str, tuple, tuple]] = []
    for harness, spec in table.items():
        if spec is None:
            continue
        for group in spec.coupled_groups:
            moving = _entries_hit_by(group, updated)
            if not moving:
                continue
            left_behind = _entries_hit_by(group, pending) - moving
            if left_behind:
                order = {declared: index for index, declared in enumerate(group)}
                out.append((
                    harness,
                    tuple(sorted(left_behind, key=order.get)),
                    tuple(sorted(moving | left_behind, key=order.get)),
                ))
    return out


def coupled_hook_set_paths(paths, *, declarations=None) -> tuple[str, ...]:
    """그 경로들 중 **결합 묶음에 속한** 것 — 상류 세대를 못 읽었을 때 fail-closed 대상 판별.

    결합이 없는 훅 파일(독립 relay 드라이버 등)은 반쪽 갱신이라는 개념 자체가 없으므로, 세대를
    검증하지 못해도 단건 전파를 막지 않는다(가드 과잉 금지).

    `declarations` 는 형제 API 전부와 같은 **kw-only** 다(`judge_adapter_hook_sets`·
    `hook_set_partial_update`·`is_live_hook_set_path` 계열) — 세대 선언을 위치인자로 받는 지점이
    하나라도 있으면 호출부가 표기를 헷갈리고, 인자 하나가 밀려도 조용히 통과한다."""
    table = _hook_set_table(declarations)
    out: list[str] = []
    for path in paths:
        norm = str(path).replace("\\", "/").strip("/")
        for spec in table.values():
            if spec is None:
                continue
            if any(_entries_hit_by(group, {norm}) for group in spec.coupled_groups):
                out.append(norm)
                break
    return tuple(out)


def live_hook_set_paths(declarations=None) -> tuple[str, ...]:
    """실행 중 하네스가 읽는 훅 세트 파일 전수(하네스 합집합·`/` 끝 = 디렉토리 접두)."""
    out: set[str] = set()
    for spec in _hook_set_table(declarations).values():
        if spec is not None:
            out.update(spec.live_files)
    return tuple(sorted(out))


def is_live_hook_set_path(relpath: str, declarations=None) -> bool:
    """동기가 그 dest 경로를 **원자 교체**해야 하는가 (하네스가 실행 중 읽는 파일인가).

    copy2 는 dest 를 truncate 한 뒤 채우므로 그 창에 하네스가 읽으면 부분 파일을 실행한다.
    훅 세트만 원자 write 로 올린다 — 전 파일 확대는 이 클래스가 요구하지 않는다. `declarations`
    미지정이면 이 엔진 선언(설치본)으로 판정한다(호출부가 상류 세대를 명시로 넘긴다)."""
    text = str(relpath).replace("\\", "/")
    for declared in live_hook_set_paths(declarations):
        if declared.endswith("/"):
            if text.startswith(declared):
                return True
        elif text == declared:
            return True
    return False


def record_adapter_baseline(dest_root: Path, source_root: Path, harnesses=None, *,
                            root_identity: tuple | None = None) -> list[str]:
    """template 과 **일치가 확인된** config 의 해시를 원장에 기록 — 기록한 relpath 목록 반환.

    기록 조건이 곧 판정의 전제다: dest 가 template 과 **내용 일치**(개행 정규화 후)일 때만 "이
    내용이 상류가 준 그대로" 라고 말할 수 있다. 편집분·보존분은 기록하지 않는다(원장 부재 =
    보고 모드 = 안전 기본값). 설치·add-harness 의 레이다운 시점과, 동기 시점의 in-sync backfill 이
    같은 규칙을 쓴다 — backfill 이 없으면 원장 도입 전 채택자는 손댄 적이 없어도 영구히 보고
    모드에 갇힌다.

    구 원장(raw bytes 다이제스트)이 정규화 다이제스트와 다른 항목도 이 backfill 이 재기록한다.
    **파일은 건드리지 않는다** — 바뀌는 건 같은 사실의 표현(원장 값)뿐이다.

    그 재기록은 dest 가 template 과 다른 항목에도 닿는다(무변경 마이그레이션). in-sync 조건만
    걸면 상류가 바뀐 뒤의 구 축 항목은 backfill 이 영영 닿지 않아, 설치 이후 아무것도 안 한
    채택자가 `edited` 오판에 갇힌 채 `--accept` 를 강요받는다. 마이그레이션 항목은 `sha256` 만
    현재 축으로 바꾸고 `recorded_at`·`template_rev` 는 **보존**한다 — 이번 실행이 관측한 건
    "그때 기록한 그 bytes 그대로" 라는 사실 하나뿐이고, 세대 스탬프를 현재로 밀면
    `instance_owned_template_delta_lines` 가 실제 세대 델타를 침묵시킨다."""
    dest_root = Path(dest_root)
    document = read_adapter_baseline(dest_root)
    files = dict(document.get("files") or {})
    template_rev = read_upstream_rev(Path(source_root))
    recorded_at = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    changed: list[str] = []
    for relpath, _harness, _mode, template in adapter_config_targets(
            dest_root, source_root, harnesses):
        dest_path = dest_root / relpath
        if not dest_path.is_file():
            continue
        digest = content_sha256(dest_path)
        if digest is None:
            continue
        existing = files.get(relpath)
        recorded = existing.get("sha256") if isinstance(existing, dict) else None
        if content_sha256(template) != digest:
            # template 과 다른 파일은 in-sync backfill 대상이 아니다. 단 **구 축(raw bytes)으로
            #   기록된 같은 파일**이면 표현만 현재 축으로 옮긴다 — 파일 bytes 도, 레이다운 시점
            #   메타(`recorded_at`·`template_rev`)도 그대로다.
            if (recorded is not None and recorded != digest
                    and _baseline_attests_dest(recorded, dest_path, digest)):
                files[relpath] = {**existing, "sha256": digest}
                changed.append(relpath)
            continue
        if recorded == digest:
            continue  # 같은 해시 재기록 — 타임스탬프만 흔들지 않는다(byte 안정).
        files[relpath] = {
            "sha256": digest,
            "recorded_at": recorded_at,
            "template_rev": template_rev,
        }
        changed.append(relpath)
    if not changed:
        return []
    document = {"schema": ADAPTER_BASELINE_SCHEMA, "files": files}
    if not _write_adapter_baseline(dest_root, document, root_identity=root_identity):
        return []
    return sorted(changed)


def resolve_adapter_config_source(dest_root: Path, explicit=None) -> Path:
    """instance-owned config 채널의 어댑터 소스 checkout — add-harness 와 **같은 해소 규칙**.

    설치 하네스를 돌며 `_resolve_add_harness_source` 를 태운다(해소 규칙 사본 0). 어느 하네스로도
    소스를 못 찾으면 add-harness 와 같은 친화 FileNotFoundError 를 그대로 올린다."""
    if explicit is not None:
        return Path(explicit).resolve()
    dest_root = Path(dest_root).resolve()
    last: FileNotFoundError | None = None
    for harness in installed_harnesses(dest_root):
        try:
            return _resolve_add_harness_source(dest_root, harness, None)
        except FileNotFoundError as exc:
            last = exc
    if last is not None:
        raise last
    raise FileNotFoundError(
        f"어댑터 config 소스 미해소: {dest_root} 에 설치된 PM 어댑터가 없다 "
        f"(`--from <프레임워크 checkout>` 를 명시하라).")


class AdapterConfigAcceptResult(NamedTuple):
    """수용 시도의 결과 — 성공은 `accepted` 하나뿐이고 나머지는 전부 비정상이다.

    status:
      `accepted`       백업 + 원자 교체 + 원장 기록까지 검증 완료(이 파일은 자동 갱신 궤도)
      `raced`          판정 시점 해시와 다름(그 사이 편집) → 아무것도 덮지 않고 중단
      `ledger-blocked` 원장을 쓸 수 없는 상태(상위 schema) → **파일도 건드리지 않는다**
      `write-failed`   교체 실패(원본 보존·백업은 남는다)
      `ledger-failed`  파일은 갱신됐으나 원장 기록이 확인되지 않음(자동 갱신 궤도 미진입)
    """
    status: str
    relpath: str
    backup: Path | None
    sha256: str | None
    detail: str = ""


def accept_adapter_config(dest_root: Path, source_root: Path, relpath: str, *,
                          expected_sha256: str | None = None,
                          expected_template_sha256: str | None = None,
                          expected_generation_sha256: str | None = None,
                          root_identity: tuple | None = None) -> AdapterConfigAcceptResult:
    """config 한 개를 **백업 후** 현행 template 으로 원자 교체하고 원장 기록까지 확인한다.

    managed 자동 갱신과 `--accept` 수용이 같은 write 경로를 탄다(백업 규칙·경쟁 판정·원장 기록이
    갈리지 않는다). 순서가 곧 안전 계약이다:
      1) 원장 쓰기 가능 여부 선확인 — 기록할 수 없으면 파일을 아예 안 건드린다(갱신해 놓고 판정
         기준을 못 남기면 다음 실행이 영구 보고 모드로 본다).
      2) `expected_sha256` 재검증 — 판정과 쓰기 사이의 동시 편집을 검증 없이 덮지 않는다.
      2') `expected_template_sha256`·`expected_generation_sha256` 재검증 — **게이트가 검사한 상류
         스냅샷과 지금 설치할 것을 결속**한다. 세대 게이트는 두 가지를 읽고 판정한다: 상류 config
         template 내용과 **선언 소스**(상류 pm_import). 둘 중 하나라도 판정 뒤에 바뀌면 "검사한
         세대" 가 아닌 상태가 설치되므로 중단한다(config 만 묶으면 선언 갱신이 그대로 통과한다).
      3) 중앙 백업(`.pm_import_backups/<날짜>/`) 후 **백업 내용까지** 같은 해시인지 확인 — 덮는
         내용이 백업에 담겼음이 확인된 뒤에만 교체한다.
      4) 같은 디렉토리 임시 파일에 write→flush→fsync→원자 교체 — 디스크 오류가
         빈/부분 파일을 남기고 호출부가 "원본 보존" 으로 오보고하는 창을 없앤다.
      5) 교체 결과 해시 + 원장 항목을 다시 읽어 확인 — 확인 못 하면 비정상 상태로 반환한다.
    대상이 채널 밖이거나 dest 파일이 없으면 ValueError(호출 오류·조용한 무동작 금지)."""
    dest_root = Path(dest_root)
    targets = {rel: template for rel, _h, _m, template in adapter_config_targets(
        dest_root, source_root)}
    template = targets.get(relpath)
    if template is None:
        raise ValueError(
            f"어댑터 config 채널 대상이 아니다: {relpath} "
            f"(대상: {', '.join(sorted(targets)) or '없음'})")
    dest_path = dest_root / relpath
    if not dest_path.is_file():
        raise ValueError(f"채택자 트리에 그 파일이 없다: {dest_path}")

    if _load_adapter_baseline_document(dest_root).status == "newer-schema":
        return AdapterConfigAcceptResult(
            "ledger-blocked", relpath, None, None,
            f"원장 schema 가 이 엔진(지원 상한 {ADAPTER_BASELINE_SCHEMA})보다 새로워 기록할 수 "
            "없다 — 파일을 바꾸지 않았다(엔진을 갱신한 뒤 다시 실행하라)")

    current = content_sha256(dest_path)
    if expected_sha256 is not None and current != expected_sha256:
        return AdapterConfigAcceptResult(
            "raced", relpath, None, None,
            "판정 뒤 파일이 바뀌었다(동시 편집) — 검증 없이 덮지 않는다. 다시 실행해 새 판정을 받아라")
    backup = _copy_dest_file_nofollow(
        dest_root, Path(relpath),
        Path(BACKUP_DIR_NAME) / datetime.date.today().isoformat() / relpath,
        root_identity=root_identity)
    if content_sha256(backup) != current:
        return AdapterConfigAcceptResult(
            "raced", relpath, backup, None,
            "백업 시점 내용이 판정 내용과 달랐다(동시 편집) — 덮지 않았다")

    if expected_generation_sha256 is not None:
        # 선언 소스는 **파일 bytes 를 다시 읽어** 대조한다(모듈 캐시를 태우면 결속이 무력해진다).
        #   여기 `file_sha256` 은 내용 판정이 아니라 **bytes 결속**이다 — 게이트가 판정에 쓴 그
        #   파일이 그대로인지만 보므로 정규화하면 안 된다(같은 축을 만든 게이트도 raw 다).
        current_generation = file_sha256(_hook_set_declaration_source(source_root))
        if current_generation != expected_generation_sha256:
            return AdapterConfigAcceptResult(
                "raced", relpath, backup, None,
                "판정 뒤 상류 세대 선언(pm_import)이 바뀌었다 — 구 선언으로 낸 판정 위에 새 세대를 "
                "설치하지 않는다. 다시 실행해 새 판정을 받아라")
    payload = _read_bytes_shared(template)
    # 두 다이제스트의 축이 다르다. `digest` 는 **설치할 raw bytes** — 게이트가 검사한 template 과의
    #   결속·교체 후 write 검증용이다. `ledger_digest` 는 **내용** — 원장이 기록하는 값이고
    #   판정(`judge_adapter_configs`)이 대조하는 값이다. 섞으면 CRLF template 을 깐 트리에서
    #   원장 확인이 영구 실패한다.
    digest = hashlib.sha256(payload).hexdigest()
    ledger_digest = content_sha256(payload)
    if expected_template_sha256 is not None and digest != expected_template_sha256:
        return AdapterConfigAcceptResult(
            "raced", relpath, backup, None,
            "판정 뒤 상류 template 이 바뀌었다 — 게이트가 검사한 bytes 와 다른 내용을 설치하지 "
            "않는다. 다시 실행해 새 판정을 받아라")
    if not _atomic_write_dest_bytes(dest_root, Path(relpath), payload,
                                    root_identity=root_identity):
        return AdapterConfigAcceptResult(
            "write-failed", relpath, backup, None,
            "원자 교체에 실패했다 — 기존 내용은 그대로다(백업도 남아 있다)")
    # write 검증은 **raw** 다 — 심은 bytes 가 그대로 착지했는지를 보는 자리라 정규화하면 부분
    #   write·개행 변조를 통과시킨다(판정이 아니라 쓰기 결과 확인).
    if file_sha256(dest_path) != digest:
        return AdapterConfigAcceptResult(
            "write-failed", relpath, backup, None,
            "교체 후 내용이 template 과 다르다 — 백업에서 복원할 수 있다")

    record_adapter_baseline(dest_root, source_root, root_identity=root_identity)
    entry = read_adapter_baseline(dest_root).get("files", {}).get(relpath)
    if not isinstance(entry, dict) or entry.get("sha256") != ledger_digest:
        return AdapterConfigAcceptResult(
            "ledger-failed", relpath, backup, ledger_digest,
            "파일은 갱신됐으나 원장 기록을 확인하지 못했다 — 다음 동기는 이 파일을 보고 모드로 "
            "본다(원장 경로의 쓰기 권한을 확인하라)")
    return AdapterConfigAcceptResult("accepted", relpath, backup, ledger_digest)


def _atomic_write_dest_bytes(dest_root: Path, rel: Path, payload: bytes, *,
                             root_identity: tuple | None = None) -> bool:
    """같은 디렉토리 임시 파일 → fsync → `file_lock.atomic_replace` 로 dest 파일을 원자 교체한다.

    임시 파일도 **symlink 미추종 fd**(`_open_dest_relative_nofollow`)로 열어 조상 검증을 그대로
    받는다. 교체는 이름 바꾸기라 대상이 symlink 여도 그 링크 자체를 대체한다(링크 너머 쓰기 없음).
    실패하면 임시 파일을 치우고 False — 원본은 손대지 않은 상태 그대로다."""
    tmp_rel = rel.with_name(rel.name + ".pm-accept.tmp")
    tmp_path = Path(dest_root) / tmp_rel
    try:
        with _fdopen_binary(
                _open_dest_relative_nofollow(
                    Path(dest_root), tmp_rel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    root_identity=root_identity, regular_only=True),
                "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _load_file_lock().atomic_replace(tmp_path, Path(dest_root) / rel)
    except (OSError, UnsafeDestPathError) as exc:
        print(f"경고: 어댑터 config 교체 실패 ({rel.as_posix()}: {exc}) — 기존 내용을 보존합니다.",
              file=sys.stderr)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def adapter_config_drift_summary(judgment: AdapterConfigJudgment,
                                 dest_root: Path) -> str:
    """template 대비 차이 한 줄 요약 — 크기·줄 수·첫 차이 줄 번호.

    무엇이 다른지 사람이 판단할 최소 좌표만 준다(전체 diff 는 채택자가 직접 볼 일이고, 동기
    출력이 파일당 한 줄을 넘기면 아무도 안 읽는다)."""
    dest_path = Path(dest_root) / judgment.relpath
    try:
        dest_lines = _read_text_shared(dest_path, encoding="utf-8", errors="replace").splitlines()
        template_lines = _read_text_shared(judgment.template, encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "차이 요약 불가(읽기 실패)"
    first_diff = next(
        (index + 1 for index, (left, right) in enumerate(
            zip(dest_lines, template_lines)) if left != right),
        min(len(dest_lines), len(template_lines)) + 1,
    )
    return (f"채택자 {len(dest_lines)}줄 ↔ template {len(template_lines)}줄 · "
            f"첫 차이 {first_diff}줄")


def _unowned_shipped_wiki_relpaths(
        source_root: Path, harnesses, notation_contexts: dict) -> list[str]:
    """선택 하네스 template 이 **출하하는** wiki relpath 중 manifest 소유자가 없는 것.

    표기 폴백이 해소하는 그 부류(인스턴스 상태 seed — `status.md`·`log/current.md`·
    `raw/README.md` …)의 기계 파생이다. 손-열거 0: 출하 인벤토리(`_iter_source_files`·tracked-only)
    에서 wiki 하위를 모으고 manifest 선언(`notation_contexts` 키)이 소유하는 경로를 뺀다.

    add-harness 가 **기존 dest 파일**을 재렌더할 대상을 이 집합으로 한정한다 — 채택자가 직접
    쓴 wiki 문서(티켓·ADR·spike·domain 페이지)는 여기 없으므로 제자리 재작성 대상이 되지
    않는다(출하 seed 만·blast radius 한정). 소스 트리 부재/열거 실패는 빈 목록(무동작)."""
    owners = {rel.rstrip("/") for rel in notation_contexts}
    out: set[str] = set()
    for name in harnesses:
        for dirname in HARNESS_TEMPLATE_DIRS.get(name, ()):  # noqa: B905
            root = Path(source_root) / "templates" / dirname
            if not root.is_dir():
                continue
            try:
                shipped = list(_iter_source_files(root, "full"))
            except Exception as exc:  # noqa: BLE001 — 열거 실패는 후보 0(무동작).
                # 엔진 사본 skew 는 삼키지 않는다 — 구 사본 부분 도달을 조용한 후보 0 으로
                #   덮으면 재렌더가 통째로 사라진다(엔진 관례: fail-soft 하되 skew 만 재-raise).
                if _is_engine_rev_skew(exc):
                    raise
                continue
            for rel, _src in shipped:
                rel_posix = rel.as_posix()
                if not _is_notation_fallback_scope(rel_posix):
                    continue
                if any(rel_posix == owner or rel_posix.startswith(owner + "/")
                       for owner in owners):
                    continue
                out.add(rel_posix)
    return sorted(out)


_WIKI_TEMPLATE_SUFFIX = ".template.md"


def _generated_template_sibling(rel_posix: str) -> str | None:
    """`wiki/pm_state.template.md` → `wiki/pm_state.md` (템플릿이 만들어 내는 인스턴스 파일).

    `board.py init` 이 이 산출물을 만들고 그 시점 표기가 박제된다 — 나중에 하네스를 추가하면
    템플릿은 재렌더되는데 산출물은 아무도 안 건드려 옛 표기로 남는다. 손-열거 대신 `*.template.md`
    관례에서 파생한다(`_template.md` 스캐폴드는 1:1 산출물이 아니라 대상 아님)."""
    if not rel_posix.endswith(_WIKI_TEMPLATE_SUFFIX):
        return None
    return rel_posix[: -len(_WIKI_TEMPLATE_SUFFIX)] + ".md"


def _notation_rerender_contexts(
        entry_notation_templates: dict, unowned_wiki_relpaths, installed_context: tuple,
) -> dict:
    """기존 dest 파일 재렌더 후보 relpath → 표기 context.

    세 부류를 합친다:
      - **manifest 소유 공유 문서** — 둘 이상 하네스가 같은 물리 경로를 읽는 것만(단일이면 그
        하네스 template 이 이미 native 로 깔았다).
      - **manifest 미소유 출하 wiki seed** — 소유자가 없어 `entry_notation_templates` 에 안
        나타난다. 설치 하네스 전체를 독자로 본다(render 단계 폴백과 같은 의미·같은 값).
      - **템플릿이 만들어 낸 인스턴스 파일**(`pm_state.template.md` → `pm_state.md`) — 생성
        시점 표기가 박제돼 하네스 추가 후 옛 표기로 남는 유일한 파생 산출물이다.
    실제로 바뀌는지(무변경 no-op 제외)는 `_shared_notation_rerender_plan` 이 산출 미리보기로
    판정하므로 여기서는 후보만 넓게 모은다.
    """
    contexts = {
        rel: context
        for rel, context in entry_notation_templates.items()
        if len(context) > 1
    }
    for rel, context in entry_notation_templates.items():
        if not _is_notation_fallback_scope(rel):
            continue
        sibling = _generated_template_sibling(rel)
        if sibling is not None:
            contexts.setdefault(sibling, tuple(installed_context) or tuple(context))
    for rel in unowned_wiki_relpaths:
        contexts.setdefault(rel, tuple(installed_context))
    return contexts


def _shared_notation_rerender_plan(
        dest_root: Path, entry_notation_templates: dict, backup_root: Path | None,
        git_safe: set | None, root_identity: tuple | None = None,
) -> tuple[list[Path], list[str]]:
    """같은 물리 문서를 둘 이상의 설치 하네스가 읽어 **기존 파일도 병기 렌더**해야 하는 relpath.

    반환 `(재렌더 대상, 백업 불가로 제외한 relpath)`.

    manifest 가 선언한 공유 wiki·공유 루트 doc 중 dest 에 이미 있는 파일만 좁게 연다(사용자
    상태 전체 재복사 아님). **복사 시작 전** 계획으로 산출해 (a) dry-run 이 이 변경을 표시하고
    (b) 적용 시 백업 범위에 들어가게 한다 — 옛 코드는 복사·적용이 끝난 뒤에야 이 집합을 계산해
    dry-run 계획에도 백업에도 없었다(비파괴 보장 구멍).

    경로 안전(`_is_safe_dest_path`)은 **여기서** 판정한다 — 옛 `is_file()` 단독 검사는 symlink 를
    따라가고 `..` 도 안 걸러, 이후 치환·렌더가 저장소 밖 파일을 고칠 수 있었다. 위험 경로는
    조용히 skip 하지 않고 **복사 시작 전 fail-loud**(engine.manifest 가드와 같은 성질 — 부분 적용
    0·외부 파일 불변).

    백업 대상 조상도 여기서 검증한다(`plan_copy` 와 동형 — git 추적&미변경은 git 이 복원하므로
    백업 불요). 백업 자리가 막혀 있으면(예 `.pm_import_backups` 가 일반 파일) **그 파일만 재렌더
    대상에서 빼고 호출부가 loud 하게 알린다** — 백업 못 할 파일은 고치지 않는다(비파괴). 여기서
    전체를 중단하지 않는 이유는 이 재렌더가 *피할 수 있는* 변경이기 때문이다(local.conf 백업은
    board init 이 무조건 덮어 회피 불가라 fail-loud 인 것과 대비).
    """
    render_mod = _load_pm_render_module()
    unsafe: list[str] = []
    backup_blocked: list[str] = []
    out: list[Path] = []
    checked_ancestors: set[Path] = set()
    for rel_posix, context in sorted(entry_notation_templates.items()):
        if not context:
            continue
        if not (rel_posix.startswith(INSTANCE_SHARED_WIKI_PREFIX)
                or rel_posix == "AGENTS.md"):
            continue
        rel = Path(rel_posix)
        if not _is_safe_dest_path(dest_root, rel):
            # 존재하지 않는 경로는 애초에 대상이 아니다 — 실존하는데 불안전할 때만 거부한다.
            if (dest_root / rel).is_symlink() or (dest_root / rel).exists():
                unsafe.append(rel_posix)
            continue
        # 선검사는 `lstat` 이다 — `is_file()` 은 링크를 따라가 **깨진 symlink** 로 교체된 대상을
        #   False 로 보고 조용히 건너뛰었다(fd 가드에 닿지도 않는 침묵 제외). symlink 는 후보로
        #   통과시키고 판정은 아래 nofollow 열기 한 지점에 맡긴다(계획 단계라 그 결과는 fail-loud).
        #   ⚠ 여기서만은 **부재가 정상**이다 — 이 단계의 후보는 "있으면 재렌더할 경로" 라서 없는
        #   것은 사고가 아니다(적용 단계 채널의 loud 부재 규칙과 의도적으로 다른 지점).
        if not _is_inplace_edit_candidate(dest_root, rel):
            continue
        try:
            changes = _notation_rerender_changes_file(
                dest_root, rel, context, render_mod, root_identity=root_identity)
        except UnsafeDestPathError:
            # `_is_safe_dest_path` 판정과 이 미리보기 읽기 사이에 경로가 교체됐다 — 계획 단계라
            #   전체 fail-loud 로 보낸다(복사 시작 전 중단·부분 적용 0).
            unsafe.append(rel_posix)
            continue
        if not changes:
            continue  # 산출 무변경 — 계획·백업·처리 어디에도 넣지 않는다(멱등 재실행 소음 0).
        if backup_root is not None and not (
                git_safe is not None and rel_posix in git_safe):
            try:
                _check_ancestor_safe(dest_root, backup_root / rel, checked_ancestors)
            except AncestorConflict:
                backup_blocked.append(rel_posix)
                continue
        out.append(rel)
    if unsafe:
        raise UnsafeDestPathError(
            "add-harness 거부: 공유 문서 재렌더 대상 경로가 안전하지 않습니다 "
            f"({', '.join(unsafe)}) — symlink·조상 symlink·repo 밖. 링크를 직접 옮기거나 "
            "제거한 뒤 다시 시도하세요(복사 시작 전 중단 — 외부 파일을 건드리지 않습니다).")
    return out, backup_blocked


def _notation_rerender_changes_file(
        dest_root: Path, rel: Path, context: tuple, render_mod,
        root_identity: tuple | None = None) -> bool:
    """이 파일이 재렌더로 **실제로 바뀌는가** (표기 산출 미리보기 + 미해소 토큰 잔존).

    렌더러는 순수 함수라 계획 단계에서 산출을 미리 만들어 볼 수 있다. 무변경 파일을 계획·백업·
    처리 범위에서 빼면 (a) `[rerender]` 계획이 실제 변경만 말하고 (b) 비-git 인스턴스에서
    add-harness 를 재실행해도 이 경로로는 백업이 쌓이지 않는다(멱등).

    판정 축은 **표기 산출**뿐이다 — 잔존 `{{...}}` 를 변경 후보로 세면 토큰을 *설명*으로 담는
    방법론 문서(pm_role·pm_playbook)와 free-form 홈(pm_role.local.md)이 매 실행 재대상이 돼
    멱등이 깨진다(실측: 재실행마다 3건 재계획·백업 누적). 그 문서들의 operational 치환은
    최초 import 가 이미 끝냈고 sed 제외 규칙이 리터럴 유지를 의도한다.

    렌더 모듈 로드 실패는 True — 판정 불가 시 보수적으로 대상에 넣는다(백업 우선).

    읽기는 symlink 미추종 fd 경로다 — `_is_safe_dest_path` 판정 직후라도 이 미리보기 읽기 전에
    경로가 교체될 수 있어서, 그때는 `UnsafeDestPathError` 를 그대로 올려 호출부가 **복사 전
    fail-loud** 로 처리하게 한다(저장소 밖 파일을 읽지도 않는다). 그래서 이 읽기는 렌더 모듈
    로드 여부보다 **먼저** 한다 — 로드 실패를 이유로 안전 판정을 건너뛰면 계획된 대상 중 일부가
    nofollow 열기를 통과하지 않은 채 백업·렌더 범위로 들어간다."""
    try:
        # 실제 렌더와 **같은 읽기**(줄끝 미번역)여야 계획과 적용이 어긋나지 않는다.
        text = read_dest_text_keeping_newlines(dest_root, rel, root_identity=root_identity)
    except (UnicodeDecodeError, OSError):
        return False  # 텍스트가 아니면 렌더 대상이 아니다(byte-copy 그대로).
    if render_mod is None:
        return True
    try:
        return render_mod.render_skill_entry_notation(
            text, context, source=str(dest_root / rel)) != text
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 보수적으로 대상 포함(백업 우선).
        if _is_engine_rev_skew(exc):
            raise  # 엔진 사본 skew 는 삼키지 않는다(로드 경계 진단 보존).
        return True


def _copy_dest_file_nofollow(
        dest_root: Path, src_rel: Path, target_base_rel: Path,
        root_identity: tuple | None = None) -> Path:
    """dest 안 파일을 dest 안 다른 경로로 **fd↔fd 스트리밍** 복사하고 실제 경로 반환.

    옛 `shutil.copy2(src, target, follow_symlinks=False)` 는 두 창을 열어 뒀다:
      - 원본 경로가 검증 뒤 symlink 로 바뀌면 **링크 자체**를 백업해 백업이 원본 내용을 안
        담는다(그 상태로 재렌더가 진행되면 복원 불가).
      - target 의 조상이 symlink 로 바뀌면 `mkdir(parents=True, exist_ok=True)` 가 링크를 따라가
        **저장소 밖에 사용자 파일 내용을 쓴다**(외부 쓰기·내용 유출).
    양쪽 다 fd 열기로 닫는다. 원본이 일반 파일이 아니면(교체 의심) 거부한다. target 이 점유돼
    있으면 `_free_backup_path` 순번으로 비켜 간다(원본 보존 우선). `copy2` 가 보존하던 모드·
    타임스탬프는 fd 기반(`fchmod`/`utime`)으로 유지하고, 내용은 스트리밍이라 메모리 적재가 없다."""
    # 원본이 일반 파일이 아니면(symlink 교체 뒤 FIFO·디바이스 포함) `regular_only` 가 연 fd 에서
    #   거부한다. 연 fd 를 그대로 백업 함수에 넘긴다 — 열기와 읽기가 같은 inode 다.
    src_fd = _open_dest_relative_nofollow(
        dest_root, src_rel, os.O_RDONLY, root_identity=root_identity, regular_only=True)
    try:
        return _backup_open_fd_nofollow(
            dest_root, src_fd, target_base_rel, root_identity=root_identity,
            label=Path(src_rel).as_posix())
    finally:
        os.close(src_fd)


def _backup_open_fd_nofollow(
        dest_root: Path, src_fd: int, target_base_rel: Path,
        root_identity: tuple | None = None, label: str = "") -> Path:
    """**이미 연 dest fd** 의 내용을 dest 안 백업 자리로 스트리밍 복사하고 실제 경로 반환.

    호출부가 fd 를 쥔 채 넘기므로 백업 대상은 그 fd 의 inode 로 고정된다 — 백업과 그 뒤의 덮어쓰기가
    **같은 파일**임이 구조적으로 보장된다(경로 재열기 창 없음). target 이 점유돼 있으면
    `_free_backup_path` 순번으로 비켜 가고, 모드·타임스탬프는 fd 기반으로 유지한다."""
    src_stat = os.fstat(src_fd)
    for _attempt in range(100):
        target = _free_backup_path(Path(dest_root) / target_base_rel)
        try:
            target_rel = target.relative_to(dest_root)
        except ValueError as exc:
            raise UnsafeDestPathError(f"백업 위치가 dest 밖입니다: {target}") from exc
        try:
            dst_fd = _open_dest_relative_nofollow(
                dest_root, target_rel, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                stat.S_IMODE(src_stat.st_mode), create_parents=True,
                root_identity=root_identity)
        except FileExistsError:
            continue  # 순번을 고른 사이 선점됨 — 다음 순번(원본 보존 우선).
        with _fdopen_binary(dst_fd, "wb") as dst:
            _stream_fd_into(src_fd, dst)  # 순번 재시도 시 처음부터 다시 흘린다.
            dst.flush()
            if hasattr(os, "fchmod"):
                os.fchmod(dst.fileno(), stat.S_IMODE(src_stat.st_mode))
            if os.utime in getattr(os, "supports_fd", frozenset()):
                os.utime(dst.fileno(),
                         ns=(src_stat.st_atime_ns, src_stat.st_mtime_ns))
        return target
    raise OSError(f"백업 경로 순번 100회 충돌: {label or target_base_rel}")


# ── 경로 표기 정규화 (Windows 확장 길이 prefix) ───────────────────────────────
# Windows 의 `os.readlink`/`Path.resolve()` 는 확장 길이 경로 prefix `\\?\`(UNC 는 `\\?\UNC\`)를
# 붙여 돌려준다 — 커널이 symlink 대상을 `\??\C:\…` 로 저장하고 CPython 이 그것을 `\\?\C:\…` 로
# 번역하기 때문이다(`Path.resolve()` 는 입력에 그 prefix 가 있으면 산출에도 유지한다). 그 표기가
# **값**에 실리면 두 자리에서 샌다:
#   기록값 — 백업이 다시 심는 링크 대상. 원본 링크의 사용자-가시 대상은 `C:\…` 인데 백업만
#            `\\?\C:\…` 를 갖게 되어 채택자 백업 트리에 플랫폼 표기가 노출된다.
#   비교값 — "링크 대상 자체를 보존했는가" 판정. 같은 파일을 가리키는 두 경로가 문자열로 갈린다.
# 그래서 규칙을 **여기 한 곳**에 두고 두 자리가 같은 함수를 부른다 — 한쪽만 벗기면 같은 결함이
# 자리만 옮긴다.
#
# 정규화 대상은 **표기뿐**이다. 파일시스템 호출에 넘기는 경로(dest 루트·조상 순회·열기)는 `Path`
# 그대로 둔다 — MAX_PATH 를 넘는 경로에서 `\\?\` 는 표기가 아니라 *기능*이라 벗기면 열리지 않는다.
_EXTENDED_PATH_PREFIX = "\\\\?\\"          # 실제 문자열: \\?\
_EXTENDED_UNC_PREFIX = "\\\\?\\UNC\\"      # 실제 문자열: \\?\UNC\
# 같은 prefix 의 슬래시 표기 — `PurePath.as_posix()` 산출과 `//?/…` 로 주어진 입력이 이 형태다.
_EXTENDED_PATH_PREFIX_SLASH = "//?/"
_EXTENDED_UNC_PREFIX_SLASH = "//?/UNC/"
# 확장 prefix 없이 다룰 수 있는 경로 길이 상한(Windows MAX_PATH).
_WINDOWS_MAX_PATH_CHARS = 260
# 확장 prefix 제거는 **Windows 표기 축**이다 — POSIX 에서는 `\` 가 정당한 파일명 문자라 같은
# 문자열을 벗기면 실재하는 파일명을 망가뜨린다(링크 대상이라면 백업이 다른 곳을 가리킨다).
# 플랫폼 판정을 모듈 상수로 두어 테스트가 Windows 표기를 다른 플랫폼에서 **주입해** 규칙을 실제로
# 태울 수 있게 한다(능력 플래그 `_DEST_FD_WALK_SUPPORTED` 와 같은 주입 seam).
_WINDOWS_PATH_NOTATION = os.name == "nt"


def _strip_extended_path_prefix(text: str) -> str:
    """확장 길이 prefix 를 벗긴 경로 표기를 돌려준다(순수 문자열 규칙·플랫폼 판정 없음).

    UNC 변형은 루트 표기까지 되돌린다 — `\\\\?\\UNC\\server\\share` 에서 prefix 만 잘라내면
    `UNC\\server\\share` 라는 **다른 경로**가 되므로 `\\\\server\\share` 로 복원한다. 슬래시
    표기(`//?/…`)도 같은 규칙으로 받는다(`as_posix()` 산출이 그 형태다).
    """
    for extended, root in (
            (_EXTENDED_UNC_PREFIX, "\\\\"),
            (_EXTENDED_UNC_PREFIX_SLASH, "//"),
            (_EXTENDED_PATH_PREFIX, ""),
            (_EXTENDED_PATH_PREFIX_SLASH, "")):
        if text.startswith(extended):
            return root + text[len(extended):]
    return text


def _path_notation_text(path) -> str:
    """경로를 **비교·기록 표기**로 직렬화한다 — Windows 확장 길이 prefix 를 남기지 않는다.

    Windows 표기 축이 아닌 플랫폼에서는 입력 표기를 그대로 둔다(위 `_WINDOWS_PATH_NOTATION`).
    구분자는 건드리지 않는다 — 이 함수의 축은 prefix 하나이고, POSIX 단일 직렬화가 필요한 자리는
    `as_posix()` 를 함께 쓴다(장부 표기 축은 pm_handoff·pm_bootstrap 소관).
    """
    text = path if isinstance(path, str) else os.fspath(path)
    if not _WINDOWS_PATH_NOTATION:
        return text
    return _strip_extended_path_prefix(text)


def _preserved_link_target(link_target: str) -> str:
    """백업 링크가 **기록**할 대상 표기 — 표기 정규화 + 긴 경로 안전 가드.

    `os.readlink` 가 돌려준 `\\\\?\\C:\\…` 를 그대로 다시 심으면 백업 링크만 원본에 없던 표기를
    갖는다. 다만 MAX_PATH 를 넘는 대상에서는 그 prefix 가 있어야 링크 생성·해소가 되므로 그때는
    **벗기지 않는다** — 표기 통일보다 링크가 실제로 대상을 가리키는 것이 우선이다.
    """
    normalized = _path_notation_text(link_target)
    if normalized != link_target and len(normalized) >= _WINDOWS_MAX_PATH_CHARS:
        return link_target
    return normalized


def _backup_symlink_nofollow(
        dest_root: Path, src_rel: Path, target_base_rel: Path,
        root_identity: tuple | None = None) -> Path:
    """dest 안 **symlink 자체**를 dest 안 백업 자리에 재생성한다(경로 재해소 0).

    링크는 내용이 아니라 *대상 문자열*이 자산이라 fd 스트리밍 복사로 표현할 수 없다. 그렇다고
    `copy2(follow_symlinks=False)` 로 두면 원본·백업 경로를 다시 해소하므로, 그 사이 부모가 저장소
    밖 지향 링크로 바뀌면 밖의 링크를 읽거나 밖에 링크를 만든다. 그래서 양쪽 **부모 디렉토리 fd**
    를 symlink 미추종으로 열고 그 안에서 `readlink`/`symlink` 만 한다 — 우리가 쓰는 디렉토리는 연
    fd 에 묶여 이후 경로 교체와 무관하다. 점유 자리는 `_free_backup_path` 순번으로 비켜 간다.

    dir_fd 미지원 플랫폼은 다른 채널과 같은 폴백을 쓴다(직전 재검사 + 경로 조작·창을 좁힐 뿐)."""
    src_rel = Path(src_rel)
    if not _DEST_FD_WALK_SUPPORTED:
        # 검사 대상은 **조상**뿐이다 — 이 채널의 원본은 정의상 symlink 라(그 링크를 보존하러 왔다)
        #   leaf 까지 거부하면 정상 형상이 통째로 막힌다. 조상 링크만 밖을 향하게 만들 수 있다.
        if not _is_safe_dest_path(dest_root, src_rel.parent):
            raise UnsafeDestPathError(
                f"백업 원본 조상 경로가 안전하지 않습니다: {src_rel.as_posix()}")
        assert_dest_root_unchanged(dest_root, root_identity)
        target = _free_backup_path(Path(dest_root) / target_base_rel)
        _ensure_dest_dir_nofollow(
            dest_root, target.relative_to(dest_root).parent, root_identity=root_identity)
        # 옛 `shutil.copy2(follow_symlinks=False)` 와 같은 동작(대상 문자열 재심기 + copystat)을
        #   펼쳐 쓴다 — 그 안의 `os.readlink` 산출이 곧 기록값이라, 표기 정규화를 끼울 자리가
        #   copy2 안에는 없다(이 폴백 분기가 곧 Windows 경로다).
        source = Path(dest_root) / src_rel
        os.symlink(_preserved_link_target(os.readlink(source)), target)
        shutil.copystat(source, target, follow_symlinks=False)
        return target
    src_dir_fd = _open_dest_dir_nofollow(
        dest_root, src_rel.parent, root_identity=root_identity)
    try:
        try:
            raw_link_target = os.readlink(src_rel.name, dir_fd=src_dir_fd)
        except OSError as exc:
            _raise_dest_path_refusal(src_rel, exc)
        # 아래 `os.symlink` 가 심는 값이 곧 채택자가 보는 백업 링크의 대상 표기다 — 기록 직전에
        #   확장 길이 prefix 를 벗긴다(비교 표기와 같은 규칙·`_path_notation_text`).
        link_target = _preserved_link_target(raw_link_target)
        for _attempt in range(100):
            target = _free_backup_path(Path(dest_root) / target_base_rel)
            try:
                target_rel = target.relative_to(dest_root)
            except ValueError as exc:
                raise UnsafeDestPathError(f"백업 위치가 dest 밖입니다: {target}") from exc
            _ensure_dest_dir_nofollow(
                dest_root, target_rel.parent, root_identity=root_identity)
            dst_dir_fd = _open_dest_dir_nofollow(
                dest_root, target_rel.parent, root_identity=root_identity)
            try:
                os.symlink(link_target, target_rel.name, dir_fd=dst_dir_fd)
            except FileExistsError:
                continue  # 순번을 고른 사이 선점됨 — 다음 순번(원본 보존 우선).
            except OSError as exc:
                _raise_dest_path_refusal(target_rel, exc)
            finally:
                os.close(dst_dir_fd)
            return target
        raise OSError(f"백업 경로 순번 100회 충돌: {src_rel.as_posix()}")
    finally:
        os.close(src_dir_fd)


def _unlink_dest_relative_nofollow(
        dest_root: Path, rel: Path, root_identity: tuple | None = None) -> None:
    """dest 안 파일/링크를 **부모 디렉토리 fd 안에서** 지운다(경로 재해소 0).

    경로 `os.unlink` 은 삭제 직전 조상이 교체되면 저장소 밖 파일을 지운다 — 링크를 걷어내고 그
    자리에 일반 파일을 놓는 복사 경로가 그 창을 갖고 있었다."""
    rel = Path(rel)
    if not _DEST_FD_WALK_SUPPORTED:
        # 조상만 검사한다 — 삭제 대상 자신이 symlink 인 것이 이 채널의 정상 형상이고(`os.unlink` 은
        #   링크를 따라가지 않는다), 밖을 가리키게 만드는 건 조상 링크뿐이다.
        if not _is_safe_dest_path(dest_root, rel.parent):
            raise UnsafeDestPathError(
                f"삭제 대상 조상 경로가 안전하지 않습니다: {rel.as_posix()}")
        assert_dest_root_unchanged(dest_root, root_identity)
        os.unlink(Path(dest_root) / rel)
        return
    dir_fd = _open_dest_dir_nofollow(dest_root, rel.parent, root_identity=root_identity)
    try:
        try:
            os.unlink(rel.name, dir_fd=dir_fd)
        except OSError as exc:
            _raise_dest_path_refusal(rel, exc)
    finally:
        os.close(dir_fd)


def _backup_to_central_dir_nofollow(
        dest_root: Path, rel: Path, backup_root: Path,
        root_identity: tuple | None = None) -> Path:
    """`dest_root/rel` 을 `backup_root/<rel>` 로 symlink 미추종 복사(제자리 편집 백업 채널)."""
    try:
        target_base_rel = (Path(backup_root) / rel).relative_to(dest_root)
    except ValueError as exc:
        raise UnsafeDestPathError(
            f"백업 위치가 dest 밖입니다: {Path(backup_root) / rel}") from exc
    return _copy_dest_file_nofollow(
        dest_root, rel, target_base_rel, root_identity=root_identity)


class InplaceBackupOutcome(NamedTuple):
    """제자리 편집 백업 결과 — 성공분과 **고치면 안 되는 분**을 사유별로 가른다."""

    backed_up: list[str]
    refused: list[str]   # 계획 뒤 경로를 안전하게 열 수 없음(교체 의심).
    vanished: list[str]  # 선검사 뒤 대상이 사라짐(삭제 경쟁).


def _backup_before_inplace_edit(
        dest_root: Path, relpaths: list[Path], backup_root: Path | None,
        git_safe: set | None,
        root_identity: tuple | None = None) -> InplaceBackupOutcome:
    """복사 대상이 아닌 기존 파일을 **변경 직전에** 중앙 백업한다.

    `CopyAction` 백업 규칙과 동형: git 추적&미변경(git_safe)은 git 이 복원 가능하므로 생략하고,
    그 밖은 `backup_root/<relpath>`(경로 점유 시 `_free_backup_path` 순번). backup_root=None 이면
    백업 위치가 없어 무동작. 계획 시점의 symlink 는 `_shared_notation_rerender_plan` 이 이미
    거부했고, **계획 뒤 교체·삭제**는 여기서 각각의 목록으로 돌려준다 — 호출부가 재렌더 대상에서
    빼고 loud 하게 알린다(백업 못 하는 파일은 고치지 않는다).

    삭제 경쟁(`FileNotFoundError`)을 예외로 흘리지 않는 이유: 이 시점엔 복사·manifest 변경이 이미
    끝나 있어 예외가 곧 **부분 적용 잔존**이다. 렌더 쓰기의 같은 경쟁 처리와 동형으로 흡수한다."""
    if backup_root is None:
        return InplaceBackupOutcome([], [], [])
    done: list[str] = []
    refused: list[str] = []
    vanished: list[str] = []
    for rel in relpaths:
        # 이 목록은 계획이 **실재를 확인한** 대상이라, 부재·형상 변화는 계획 뒤 사고다 — 조용한
        #   skip 대신 사유별 목록에 실어 호출부가 재렌더에서 빼고 loud 로 알리게 한다.
        anomaly = _copied_scope_anomaly(dest_root, rel)
        if anomaly == "vanished":
            vanished.append(rel.as_posix())
            continue
        if anomaly == "swapped":
            refused.append(rel.as_posix())
            continue
        if git_safe is not None and rel.as_posix() in git_safe:
            continue  # git 이 복원 가능 — 백업 생략(plan_copy git-safe skip 과 동형).
        try:
            _backup_to_central_dir_nofollow(
                dest_root, rel, backup_root, root_identity=root_identity)
        except UnsafeDestPathError:
            refused.append(rel.as_posix())
            continue
        except FileNotFoundError:
            vanished.append(rel.as_posix())
            continue
        done.append(rel.as_posix())
    return InplaceBackupOutcome(done, refused, vanished)


def _guest_line_key(line: str) -> tuple:
    """guest manifest 라인의 **마커-무관 비교 키** = (path, frozenset(markers)).

    공백/마커 순서에 불변이라 `.opencode/agents    @render @target-owned` 와
    `.opencode/agents @target-owned @render` 를 같게 본다. 경로 집합만 비교하면 같은 경로의 **마커
    교정**(`@render` → `@render @target-owned`)을 놓쳐 pm_update 가 non-target-owned 누락으로 rc=2
    실패할 수 있다 — sync 의 changed 판정이 이 키 집합을 비교한다."""
    toks = line.split()
    return (toks[0], frozenset(toks[1:])) if toks else ("", frozenset())


def _guest_render_sync_plan(
        dest_root: Path, guest_lines: list[str], adapter_dirs: tuple) -> dict:
    """refresh 시 **이 하네스 namespace 의 guest 절 항목을 현재 flavor 와 동기화**한 계획.

    반환 `{"added": [라인], "removed": [경로], "new_block": str|None, "changed": bool}`.
    - **이 하네스 namespace(`adapter_dirs`) 항목만** 현행 목표로 교체 — 신규 추가 **+ upstream flavor 에서
      폐기/`@render` 해제된 stale 제거**. 목표(`guest_lines`)는 렌더물과 엔진 행 **둘 다**라
      (`_guest_manifest_lines`) 이 동기가 두 채널을 함께 최신화한다.
    - **타 하네스 guest 항목은 불변**(다른 namespace — 순차 add 로 한 절에 공존).
    - 목표 = `guest_lines`(flavor·이미 namespace-limited) **−** host 실소유(경로-포함
      `_path_owned_by`·기준 `_core_manifest_paths`). add·refresh·dry-run preview 가 이 단일 계획을 공유
      (판정 사본 0). manifest 부재·pm_update 로드 실패는 무동작(changed=False)."""
    empty = {"added": [], "removed": [], "new_block": None, "changed": False}
    manifest = dest_root / ".project_manager" / "engine.manifest"
    if not manifest.is_file():
        return empty
    pu = _load_pm_update()
    if pu is None:
        return empty
    text = _read_text_shared(manifest, encoding="utf-8")
    block = pu._extract_guest_manifest_block(text)
    existing_lines = [
        ln.rstrip() for ln in (block.splitlines() if block else [])
        if ln.strip() and not ln.strip().startswith("#")]

    # 이 하네스가 관리하는 footprint = adapter namespace ∪ **flavor `@render` 선언**(cross-ns 포함).
    #   guest_lines 는 flavor 후보 전부(`_guest_manifest_lines`·렌더물 + 엔진 행) — 그 경로 집합이 cross-ns
    #   의존물(opencode 의 `.claude/skills`)까지 이 하네스 소유로 판정하게 한다(경로-포함
    #   `_path_owned_by`). namespace-only 판정이면 cross-ns 항목이 target 엔 있는데 existing_this 엔 없어
    #   idempotent refresh 가 매번 changed=True(등재 churn)·타 하네스로 오분류된다(멱등 위반).
    this_flavor_paths = {ln.split()[0] for ln in guest_lines}

    def _this_ns(path: str) -> bool:
        return pu._path_owned_by(path, this_flavor_paths) or any(
            path == d.rstrip("/") or path.startswith(d.rstrip("/") + "/")
            for d in adapter_dirs)

    # 목표 = flavor 후보(namespace 무관·cross-ns 포함) − host 실소유. guest_lines 는 이미 flavor-declared.
    core_owned = pu._core_manifest_paths(text)
    target = sorted(
        ln for ln in guest_lines if not pu._path_owned_by(ln.split()[0], core_owned))
    target_paths = {ln.split()[0] for ln in target}
    existing_this_lines = [ln for ln in existing_lines if _this_ns(ln.split()[0])]
    existing_this = {ln.split()[0] for ln in existing_this_lines}
    other_ns = [ln for ln in existing_lines if not _this_ns(ln.split()[0])]  # 타 하네스 — 불변.
    added = [ln for ln in target if ln.split()[0] not in existing_this]
    removed = sorted(existing_this - target_paths)  # upstream 에서 폐기된 this-ns 경로.
    merged = sorted(set(other_ns + target), key=lambda ln: ln.split()[0])
    new_block = (pu._GUEST_MANIFEST_BEGIN + "\n" + "\n".join(merged) + "\n"
                 + pu._GUEST_MANIFEST_END) if merged else None
    # changed = 경로 추가/제거 **OR 마커 교정**(같은 경로·마커 상이) — 경로 집합만 보면 기존
    #   `.opencode/agents @render` → 목표 `@render @target-owned` 교정을 놓쳐 pm_update rc=2
    #   ). this-ns 기존 라인 ↔ 목표를 마커-무관 키 집합으로 비교한다(merged 는 이미 목표 마커 반영).
    changed = ({_guest_line_key(ln) for ln in existing_this_lines}
               != {_guest_line_key(ln) for ln in target})
    return {"added": added, "removed": removed, "new_block": new_block, "changed": changed}


def _append_guest_render_to_manifest(
        dest_root: Path, guest_lines: list[str], adapter_dirs: tuple) -> dict:
    """dest engine.manifest 의 guest 절을 이 하네스 namespace 에 대해 현재 flavor 와 **동기화**한다
    (신규 등재 **+ 폐기 제거**·타 하네스 불변). 반환 `{"added": [라인], "removed": [경로]}`.

    **단일 guest 절**(마커 하나) 아래 모든 하네스 라인이 모이고, pm_update 가 engine.manifest overwrite
    시 재부착한다(MF-1). refresh 가 add-only 였으면 upstream flavor 에서 사라진 경로가 영구 render/lint
    관리로 남았다. write 후 `read_manifest` **왕복 검증**(조용한 미등재 금지·RuntimeError). dest
    manifest 부재·무변경(멱등)은 graceful skip. 계획은 `_guest_render_sync_plan`(preview 공유·판정 사본 0)."""
    manifest = dest_root / ".project_manager" / "engine.manifest"
    # 경로 안전: manifest(또는 조상)가 repo-밖 지향 symlink 면 아래 read/write 가 링크를
    #   따라가 외부 파일을 노출/덮는다 — **fail-loud**(조용한 skip 아님). 부분 적용 방지는 add_harness 의
    #   복사 시작 전 조기 가드가 맡고, 여기선 직접 호출·TOCTOU 백스톱.
    if not _is_safe_dest_path(dest_root, Path(".project_manager") / "engine.manifest"):
        raise UnsafeDestPathError(
            f"add-harness: engine.manifest 경로가 안전하지 않아 guest 등재를 거부합니다 ({manifest}) "
            "— symlink·조상 symlink·repo 밖. 링크를 옮기거나 제거한 뒤 다시 시도하세요(외부 파일 불변).")
    if not manifest.is_file():
        # 등재를 조용히 생략하지 않는다 — 복사됐지만 render/lint 관리 밖임을 명시.
        print("  ⚠️ engine.manifest 부재 — guest 어댑터가 복사됐으나 render/lint 관리 밖입니다 "
              "(manifest-파생 등재 채널 없음).", file=sys.stderr)
        return {"added": [], "removed": []}
    pu = _load_pm_update()
    if pu is None:
        print("  ⚠️ pm_update 로드 실패 — guest 어댑터가 복사됐으나 render/lint 관리 밖입니다 "
              "(guest 절 등재 생략).", file=sys.stderr)
        return {"added": [], "removed": []}
    plan = _guest_render_sync_plan(dest_root, guest_lines, adapter_dirs)
    if not plan["changed"]:
        return {"added": [], "removed": []}  # 멱등 — 이미 동기(재실행 refresh)
    # 판정(절 스트립·블록 조립)은 LF 본문으로, 쓰기는 **이 manifest 의 현재 표기**로 한다 —
    #   CRLF 체크아웃(Windows `core.autocrlf=true`)에서 LF 로 되쓰면 우리가 append 한 guest 절
    #   말고도 파일 전체가 뒤집혀 append-only byte 불변식이 깨진다.
    text, newline = read_text_preserving_newline(manifest)
    stripped = pu._strip_guest_manifest_block(text)
    if plan["new_block"]:
        if stripped and not stripped.endswith("\n"):
            stripped += "\n"
        write_text_preserving_newline(
            manifest, stripped + "\n" + plan["new_block"] + "\n", newline)
    else:
        # this-ns guest 전량 폐기·타 하네스도 0 → 절 제거.
        write_text_preserving_newline(manifest, stripped, newline)
    # read_manifest 왕복 검증 (fail-loud·추가분 반영). 대조는 **등재 경로 전량**이다 —
    #   `@render` 로 좁히면 guest 절의 엔진 행(비-render·update 채널)이 매번 미반영으로 오판된다.
    after = {str(e).replace("\\", "/") for e in pu.read_manifest(manifest)}
    missing = [ln.split()[0] for ln in plan["added"] if ln.split()[0] not in after]
    if missing:
        raise RuntimeError(
            f"add-harness: guest 절 등재가 read_manifest 왕복에 미반영: {missing}")
    return {"added": plan["added"], "removed": plan["removed"]}


def add_harness(
    dest_root: Path,
    harness: str,
    *,
    dry_run: bool,
    source_root: Path | None = None,
) -> list[CopyAction]:
    """라이브 인스턴스에 두 번째 harness 어댑터를 비파괴로 추가한다.

    스코프 = *추가되는 harness 의 어댑터 네임스페이스 ∪ guest flavor `@render` 선언*(ADD_HARNESS_ADAPTER·
    host 실소유: opencode=`.opencode/**`+`AGENTS.md`(+codex host 엔
    cross-ns `.claude/skills` — opencode 네이티브 소비), claude=`.claude/**`(host-소유 제외)+
    `CLAUDE.md`. **제외**(plan 에 애초에 없음→clobber 불가): `.project_manager/**`(엔진+wiki dev-state)·
    `engine.manifest`·`.gitignore`·`.gitattributes`·`.github/**`·루트 파사드·다른 harness·flavor 미선언.

    구현: `plan_copy` 로 전체 어댑터 트리 plan 을 만든 뒤 `_in_adapter_namespace` predicate(네임스페이스
    ∪ flavor `@render` − host 실소유)로 **구조적으로 좁힌다** — 반환·적용 plan 에 flavor 미선언 relpath 가
    0개다(Decision 5 불변식·cross-ns 의존물은 flavor 선언이라 허용).
    첫 add=신규 복사(무손실)·재실행=refresh(엔진 관리 어댑터 파일은 중앙 백업 후 덮음·
    instance-owned config는 create-if-absent 보존·`--into` 백업 철학). fill(LLM) 불요 — operational 토큰 치환(substitute_placeholders)·opencode
    모델 결정적 해소(resolve_opencode_model)·자유서술 TODO 표시(_run_manual_fill·비-LLM)만.

    복사 대상은 아니지만 이번 add 로 **기존 공유 문서**(둘 이상 하네스가 읽는 wiki·`AGENTS.md`)가
    병기 표기로 재렌더된다 — 그 계획은 `_shared_notation_rerender_plan` 이 **복사 시작 전**
    산출·안전 검증하고, 출력/dry-run 계획에 `[rerender]` 로 표시되며, 적용 시 변경 직전에 중앙
    백업을 거친다(비파괴: 계획 제시 + 백업 후 변경).

    dry_run=True 면 plan 만 산출·출력(파일시스템 미변경). 반환값 = 스코프 제한된 CopyAction plan.
    source_root 생략 시 _resolve_add_harness_source 로 소스를 정한다: dest local.conf
    upstream(path·templates 보유) > dest 자신(templates 보유·framework-checkout 자기전환) > 친화
    에러. imported 인스턴스(templates 부재)도 upstream 에서 어댑터 소스를 해소한다.
    harness 는 등록된 단일 하네스 — 집합/미지원 값은 ValueError. dest 미존재는 FileNotFoundError.
    """
    if harness not in ADD_HARNESS_ADAPTER:
        raise ValueError(
            f"add_harness: harness 는 {tuple(ADD_HARNESS_ADAPTER)} 중 하나여야 한다 "
            f"(단일 harness 추가·집합 선택은 최초 import 소관): {harness!r}"
        )
    dest_root = Path(dest_root).resolve()
    if not dest_root.is_dir():
        raise FileNotFoundError(
            f"add_harness: dest 가 존재하는 라이브 인스턴스 디렉토리가 아니다: {dest_root}"
        )
    # engine.manifest 경로 안전 검증 (**복사 시작 전** fail-loud): manifest(또는 조상)가
    #   repo-밖 지향 symlink 면 이후 read(`_dest_manifest_core_paths`)·write(`_append_guest_render_to_
    #   manifest`)가 링크를 따라가 외부 파일을 노출/덮는다. 불안전이면 어떤 복사·등재도 시작하지 않는다
    #   (부분 적용 0). 읽기·쓰기 지점이 같은 경로라 이 단일 가드가 양쪽을 덮는다.
    if not _is_safe_dest_path(dest_root, Path(".project_manager") / "engine.manifest"):
        raise UnsafeDestPathError(
            f"add-harness 거부: engine.manifest 경로가 안전하지 않습니다 "
            f"({dest_root / '.project_manager' / 'engine.manifest'}) — symlink·조상 symlink·repo 밖. "
            "링크를 직접 옮기거나 제거한 뒤 다시 시도하세요(비파괴 — 외부 파일을 건드리지 않습니다).")
    src_root = _resolve_add_harness_source(dest_root, harness, source_root)
    template_root = resolve_template_roots(src_root, harness)[0]

    # 표기 독자 = dest 실설치 하네스 ∪ 이번에 추가하는 하네스(공용 판정 helper).
    # 기존 설치분은 따로 붙들어 둔다 — 설치 기록에 올릴 자격 판정(`established_harnesses`)이
    #   "이번에 선 어댑터" 와 "원래 있던 것" 을 갈라야 하기 때문(원래 것은 이번 실행이 철회 안 함).
    preexisting_harnesses = installed_harnesses(dest_root, src_root)
    selected_harnesses = tuple(
        candidate
        for candidate in REGISTERED_HARNESSES
        if candidate in {*preexisting_harnesses, harness}
    )
    # 미등록 표기 하네스는 **복사 시작 전** 거부한다 — 렌더 단계에서 터지면 부분 설치가 남는다
    #   (main import 게이트와 같은 성질·같은 문구). ValueError 라 add_harness_cli 가 친화
    #   메시지 + rc1 로 번역한다(traceback 0).
    unregistered_notation = unregistered_skill_notation_template_dirs(
        [HARNESS_TEMPLATE_DIRS[name][0] for name in selected_harnesses])
    if unregistered_notation:
        raise ValueError(
            f"add-harness: {_unregistered_skill_notation_message(unregistered_notation)}")
    pm_update_mod = _load_pm_update()
    if pm_update_mod is None:
        raise RuntimeError(
            "add-harness: pm_update.py를 로드할 수 없어 공유 경로 표기 context를 만들 수 없습니다."
        )
    # 설치 하네스의 표기 manifest 는 **전원 확보**돼야 한다 — 하나라도 없으면 그 독자를 뺀 채
    #   공유 문서를 재렌더하게 되므로 복사 전에 중단한다(main import 와 같은 성질). 타입은
    #   FileNotFoundError — `add_harness_cli` 가 소스 템플릿 부재와 같은 친화 메시지 + rc 1 로
    #   번역해 traceback 을 남기지 않는다(양 진입 대칭).
    notation_manifests = []
    missing_notation_manifests = []
    for name in selected_harnesses:
        manifest = (src_root / "templates" / HARNESS_TEMPLATE_DIRS[name][0]
                    / ".project_manager" / "engine.manifest")
        (notation_manifests if manifest.is_file() else missing_notation_manifests).append(
            manifest if manifest.is_file() else f"{name}({manifest})")
    if missing_notation_manifests:
        raise FileNotFoundError(
            "add-harness: 설치 하네스의 표기 manifest 를 소스에서 찾을 수 없습니다: "
            f"{', '.join(missing_notation_manifests)}. 그 하네스를 독자에서 조용히 빼면 공유 "
            "문서가 잘못된 단독 표기로 재렌더되므로 복사 전에 중단합니다 — `--from` 이 그 flavor "
            "template 을 가진 checkout 인지 확인하세요."
        )
    entry_notation_templates = pm_update_mod._entry_notation_templates_from_manifests(
        notation_manifests, src_root)
    shared_agents_members = NEUTRAL_SHARED_ENTRY_DOCS[Path("AGENTS.md")][0]
    selected_shared_agents = tuple(
        HARNESS_TEMPLATE_DIRS[name][0]
        for name in selected_harnesses
        if name in shared_agents_members
    )
    # main import 와 같은 규칙 — AGENTS.md 는 manifest 미등재라 소유자 매칭으로 context 가 안
    #   나오고, 독자는 이 진입문서를 읽는 하네스 부분집합이다. 단일 멤버여도 명시 전달한다
    #   (조용한 skip 0·단일이면 그 하네스 표기라 출하 형상에선 no-op).
    if selected_shared_agents:
        entry_notation_templates["AGENTS.md"] = selected_shared_agents

    adapter_dirs, root_doc = ADD_HARNESS_ADAPTER[harness]
    # 스코프 표시 문자열 — dirs 튜플을 `d/**` 로 합친다(codex=`.codex/** + .agents/**`·단일은 그대로).
    adapter_scope = " + ".join(f"{d}/**" for d in adapter_dirs)
    # 복사/등재 차감 기준 = **dest(host) 실소유 경로** (guest 절 제외).
    # 판정(`_engine_render_relpaths`)은 *guest flavor* 관점이라 bare `@render` 를 전부 native 로 봐
    # **claude-as-guest**(codex/opencode host 에 claude 추가)를 놓쳤다 — claude flavor 의 `.claude/agents`·
    # `.claude/skills` bare @render 가 native 로 차감돼 복사·등재 0 → pm_update 영구 관리 불능. host 가
    # *실제로* 소유(pm_update 관리)하는 경로만 빼고 나머지는 guest 로 레이다운/등재한다(경로-포함
    # `_path_owned_by`/`_is_render_managed`). opencode host 의 `.claude/skills`(native 소비)처럼
    # host 가 이미 가진 것만 정확히 빠진다.
    dest_owned = _dest_manifest_core_paths(dest_root)
    # guest flavor 가 `@render` 로 선언한 경로 전부 (cross-ns 의존물 포함) — 복사 스코프가
    # namespace 밖이라도 이걸 포함해야 opencode 의 `.claude/skills`(codex host 미소유)가 복사·
    # 렌더된다(그래야 등재된 guest @render 를 render_managed_files 가 실제 파일에 적용). host 실소유
    # (dest_owned)는 아래 `_in_adapter_namespace` 가 그 위에서 차감한다(claude host 는 미복사 유지).
    guest_render_paths = _flavor_render_relpaths(template_root)

    today = datetime.date.today().isoformat()
    # refresh(재실행)는 네임스페이스 안 기존 어댑터를 중앙 디렉토리에 백업 후 덮는다(--into 동형).
    # 첫 add 는 신규라 백업 없음. git 추적&미변경은 백업 생략(git 복원 가능).
    backup_root = dest_root / BACKUP_DIR_NAME / today
    git_safe = git_safe_relpaths(dest_root)

    # 기존 공유 문서 재렌더 계획 — **복사 시작 전** 산출해 경로 안전을 검증하고(위험 경로면 여기서
    #   fail-loud·부분 적용 0) dry-run 표시·백업 범위에 싣는다. 후보는 두 부류다:
    #   manifest 소유 공유 문서(다중 하네스) + **manifest 미소유 출하 wiki seed**. 후자를 빼면
    #   claude 인스턴스에 codex 를 얹었을 때 `wiki/raw/README.md` 가 canonical slash 로 남아
    #   최초 설치 경로에서 닫은 조용한 degrade 가 이 경로로 되살아난다(add-harness 축 실측).
    installed_notation_context = tuple(
        HARNESS_TEMPLATE_DIRS[name][0] for name in selected_harnesses)
    rerender_contexts = _notation_rerender_contexts(
        entry_notation_templates,
        _unowned_shipped_wiki_relpaths(
            src_root, selected_harnesses, entry_notation_templates),
        installed_notation_context,
    )
    # dest 루트 신원을 **계획 전에** 고정해 적용까지 넘긴다 — 계획 뒤 루트 자체가 저장소 밖
    #   symlink·다른 디렉토리로 교체되면 컴포넌트 순회가 엉뚱한 트리에서 안전하게 일어난다.
    #   획득 실패는 여기서 fail-loud(`add_harness_cli` 가 rc1 번역·복사 시작 전).
    root_identity = dest_root_identity(dest_root)
    shared_rerender_relpaths, shared_backup_blocked = _shared_notation_rerender_plan(
        dest_root, rerender_contexts, backup_root, git_safe, root_identity=root_identity)

    # 전체 어댑터 트리 plan → (네임스페이스 ∪ flavor `@render` − host 실소유)로 구조적 제한(Decision 2·5·
    # ). 그 밖(엔진·wiki·타 harness·설정·파사드·flavor 미선언)은 필터로 제거돼 plan 에 0개다(불변식).
    create_if_absent = ADD_HARNESS_CREATE_IF_ABSENT[harness]
    skipped_existing, preserved_different = _existing_create_if_absent_relpaths(
        template_root, dest_root, create_if_absent,
        ADD_HARNESS_PRESERVE_EXISTING_TOML_FIELDS[harness],
    )
    plan = plan_copy(
        [template_root], dest_root, backup_root, "full", git_safe=git_safe,
        skip_existing_relpaths=skipped_existing,
        include_relpath=lambda rel: _in_adapter_namespace(
            rel, adapter_dirs, root_doc, dest_owned, guest_render_paths),
    )

    # 복사 plan 이 이미 실을 경로는 재렌더 목록에서 뺀다 — 그쪽이 CopyAction 백업 + render 를
    #   모두 하므로 남겨 두면 같은 파일을 두 번 백업하고 계획에 두 번 나온다(예 공유 `AGENTS.md`).
    planned_copy_relpaths = {a.dst.relative_to(dest_root) for a in plan}
    shared_rerender_relpaths = [
        rel for rel in shared_rerender_relpaths if rel not in planned_copy_relpaths]

    n_new = sum(1 for a in plan if a.backup is None and not a._git_safe_skip)
    n_refresh = len(plan) - n_new
    print(f"[pm_import add-harness] {harness} → {dest_root}")
    print(f"  소스: {src_root}/templates/{HARNESS_TEMPLATE_DIRS[harness][0]}")
    print(f"  스코프: 어댑터 네임스페이스 + flavor @render ({adapter_scope} + {root_doc} · "
          f"host 실소유 경로 제외)")
    for a in plan:
        print(a.describe())
    for rel in preserved_different:
        print(f"  ⚠️ instance-owned {rel}: 기존 값이 template과 다름 — byte 보존. "
              "template 변경은 수동 반영하세요.")
    print(f"  → {len(plan)} 파일 ({n_new} 신규 · {n_refresh} refresh)")
    # 복사 대상은 아니지만 이번 add 로 *내용이 바뀌는* 기존 공유 문서 — 계획에 명시한다
    #   (dry-run·적용 공통. 적용 경로는 아래에서 백업 후 렌더한다).
    if shared_rerender_relpaths:
        print(f"  공유 문서 재렌더 ({len(shared_rerender_relpaths)}건 · 병기 표기 · 백업 후 변경):")
        for rel in shared_rerender_relpaths:
            print(f"  [rerender] {rel.as_posix()}")
    for rel_posix in shared_backup_blocked:
        print(f"  ⚠️ 공유 문서 {rel_posix}: 백업 자리({BACKUP_DIR_NAME}/)가 막혀 있어 재렌더를 "
              "생략합니다 — 백업 못 하는 파일은 고치지 않습니다(비파괴). 해당 경로를 정리한 뒤 "
              "다시 실행하세요.", file=sys.stderr)

    if dry_run:
        # engine.manifest guest `@render` 동기 미리보기 — 추가/제거 예정 둘 다(실제 sync 와 같은 계획·
        #   `_guest_render_sync_plan` 공유·멱등이면 0건).
        gsync = _guest_render_sync_plan(
            dest_root,
            _guest_manifest_lines(template_root, adapter_dirs, root_doc, dest_owned),
            adapter_dirs)
        if gsync["added"]:
            print(f"  engine.manifest guest 절 등재 예정 ({len(gsync['added'])}건):")
            for gl in gsync["added"]:
                print(f"    + {gl}")
        if gsync["removed"]:
            print(f"  engine.manifest guest 절 제거 예정 ({len(gsync['removed'])}건·폐기):")
            for gp in gsync["removed"]:
                print(f"    - {gp}")
        print("[dry-run] 적용 안 함 (파일시스템 미변경).")
        return plan

    # ── 적용 ── 스코프(네임스페이스 ∪ flavor `@render` − host 실소유) 안 파일만 복사·토큰 처리
    #   (스코프 밖은 plan 에 없어 불가침).
    # 복사 **직전** 루트 재확인 — 뒤에 두면 첫 mkdir/쓰기가 이미 교체된 트리에 들어간다.
    #   fd 가드는 *열 파일이 있을 때만* 발화하므로 단계 경계 검사가 false-green(대응 경로 없는
    #   교체 트리에서 아무 일도 안 하고 rc0)까지 막는다.
    assert_dest_root_unchanged(dest_root, root_identity)
    # 파일 단위 위반(경로 교체·계획 뒤 상태 변화)은 그 파일만 빼고 loud 로 알린다 — 적용 단계
    #   rc 정책과 같은 규칙이고, 후속 채널 범위는 **성공분만** 이다(제외분 미접촉).
    copy_outcome = apply_copy_plan(plan, dest_root, root_identity=root_identity)
    report_copy_apply_anomalies(copy_outcome)
    copied_relpaths = set(copy_outcome.copied)
    # guest 어댑터를 dest engine.manifest 에 멱등 등재(렌더물 `@render` + **엔진 행**) —
    # 인스턴스 manifest 가 "이 인스턴스에서 framework-managed 인 것"의 단일 진실이 되어, 아래
    # render_managed_files 와 manifest-파생 overlay 스캔이 guest 를 자연 커버하고, 엔진 행은
    # pm_update 전파 채널을 얻는다(등재 없으면 설치 시점 사본으로 영구 동결).
    # **render 전에** 등재해야 이번 run 의 render_managed_files 가 guest 를 집는다.
    guest_sync = _append_guest_render_to_manifest(
        dest_root,
        _guest_manifest_lines(template_root, adapter_dirs, root_doc, dest_owned),
        adapter_dirs)
    if guest_sync["added"]:
        print(f"  ✓ engine.manifest guest 절 {len(guest_sync['added'])}건 등재: "
              f"{', '.join(ln.split()[0] for ln in guest_sync['added'])}")
    if guest_sync["removed"]:
        print(f"  ✓ engine.manifest guest 절 {len(guest_sync['removed'])}건 제거(폐기 동기): "
              f"{', '.join(guest_sync['removed'])}")
    # 이번 하네스 template 과 **byte-identical 이라 복사만 생략된**
    #   파일만 처리 대상에 추가한다 — token-form 그대로라 미렌더(토큰 잔존) 잔존의 유일 대상. 경로는
    #   template(신뢰)에서 오고 안전 검증(`_is_safe_dest_path`)을 거치며, 타 guest·adopter 자체
    #   생성 파일(내용 상이·copied)은 제외된다(과확장 봉쇄). 기존 렌더 파이프 재사용.
    proc_relpaths = copied_relpaths | _byte_identical_skipped(
        template_root, dest_root, copied_relpaths, adapter_dirs,
        root_identity=root_identity)
    # 같은 물리 문서를 둘 이상의 설치 하네스가 읽으면 기존 파일도 병기 렌더 대상이다(계획은
    #   복사 전 `_shared_notation_rerender_plan` 이 안전 검증까지 마쳤다). 이 파일들은 복사
    #   액션이 아니라 **제자리 변경**이므로 백업이 CopyAction 을 안 탄다 — 변경 직전에 같은
    #   중앙 백업 규칙으로 직접 백업한다(비파괴: 적용 전 계획 제시 + 백업 후 변경).
    backup_outcome = _backup_before_inplace_edit(
        dest_root, shared_rerender_relpaths, backup_root, git_safe,
        root_identity=root_identity)
    shared_backed_up = backup_outcome.backed_up
    if shared_backed_up:
        print(f"  ✓ 공유 문서 {len(shared_backed_up)}건 백업: {', '.join(shared_backed_up)}")
    # 백업 못 한 파일은 고치지 않는다(비파괴) — 교체·삭제 둘 다 재렌더 대상에서 뺀다.
    if backup_outcome.refused:
        print(f"  ⚠️ 공유 문서 {len(backup_outcome.refused)}건: 계획 검증 뒤 경로를 안전하게 열 수 "
              f"없어(symlink 교체·비-디렉토리 컴포넌트) 백업할 수 없습니다 — 재렌더에서 "
              f"제외합니다(원본·저장소 밖 파일 불변): {', '.join(backup_outcome.refused)}",
              file=sys.stderr)
    if backup_outcome.vanished:
        print(f"  ⚠️ 공유 문서 {len(backup_outcome.vanished)}건: 계획 검증 뒤 대상이 사라져(경쟁 "
              f"삭제) 백업할 수 없습니다 — 재렌더에서 제외합니다(새로 만들지 않습니다): "
              f"{', '.join(backup_outcome.vanished)}", file=sys.stderr)
    _backup_dropped = set(backup_outcome.refused) | set(backup_outcome.vanished)
    if _backup_dropped:
        shared_rerender_relpaths = [
            rel for rel in shared_rerender_relpaths
            if rel.as_posix() not in _backup_dropped]
    # ⚠ 기존 문서는 **표기 렌더 채널에만** 싣는다 — placeholder 치환·자유서술 fill 범위
    #   (`proc_relpaths`)에 넣으면 채택자가 쓴 `{{PROJECT_NAME}}` 리터럴이 이름으로 바뀌고
    #   자유서술 토큰에 TODO 마커가 붙는다(콘텐츠 훼손·실측). 이번 run 이 *복사한* 파일만
    #   치환·fill 대상이라는 비파괴 범위(MF1)는 그대로 둔다.
    render_relpaths = proc_relpaths | set(shared_rerender_relpaths)
    # 라이브 인스턴스의 project_name 은 기존 local.conf 를 존중(없으면 디렉토리명 폴백).
    project_name = _instance_project_name(dest_root)
    subs = _substitution_map(project_name, dest_root, today)
    n_subst = substitute_placeholders(dest_root, subs, proc_relpaths,
                                      root_identity=root_identity)
    # opencode 모델 토큰 결정적 해소(claude-only 는 inactive) — main 흐름과 동일.
    resolve_opencode_model(dest_root, proc_relpaths, model_arg=None,
                           root_identity=root_identity)
    # render_managed_files 는 dest 인스턴스 engine.manifest 의 @render path 만 렌더한다 — 위에서 guest
    # `@render` 를 dest manifest 에 등재했으므로 guest 어댑터도 렌더된다.
    render_managed_files(
        dest_root,
        subs,
        render_relpaths,
        root_identity=root_identity,
        entry_notation_templates=entry_notation_templates,
        # manifest 미소유 wiki seed 는 소유자 매칭으로 context 가 안 나온다 — 설치 하네스 전체를
        #   폴백 컨텍스트로 넘겨야 위 재렌더 대상이 실제로 병기 표기를 받는다(최초 설치와 같은 채널).
        installed_notation_context=installed_notation_context,
    )
    # 자유서술 placeholder 는 TODO 표시(비-LLM·main manual 흐름과 동일).
    assert_dest_root_unchanged(dest_root, root_identity)  # 단계 경계 재확인(위와 같은 이유).
    _run_manual_fill(dest_root, proc_relpaths, root_identity=root_identity)
    # main import(:3289)와 **대칭** — 이번 add 가 실제로 중앙 백업을 만들었으면
    #   (backup_root 생성) .gitignore 가 `.pm_import_backups/` 를 무시하게 보장한다(채택자
    #   git status 오염 방지). 같은 헬퍼 재사용(신규 로직 0)·발화 조건은 git repo(git_safe
    #   판정 가능) + 백업 실생성 시에만 — 무백업 add 는 gitignore 무변(최소 변경). add-harness
    #   는 항상 라이브 인스턴스라 main 의 is_new 분기는 없다(backup_root 도 상시 non-None).
    if git_safe is not None and backup_root.exists():
        gi_status = ensure_backup_dir_gitignored(dest_root, git_safe, copied_relpaths)
        if gi_status in ("added", "created"):
            print(f"✓ .gitignore 에 {BACKUP_DIR_NAME}/ 추가 (백업이 git status 오염 방지)")
        elif gi_status == "unsafe-skip":
            print(f"  ⚠️ .gitignore 가 미추적/변경 상태 — 비파괴 위해 자동 추가 생략. "
                  f"수동으로 `{BACKUP_DIR_NAME}/` 한 줄을 추가하세요.")
    # 설치 기록 — 이번에 추가한 하네스 ∪ 이미 설치돼 있던 하네스(`selected_harnesses`) 중 **실제로
    #   어댑터가 선 것만** 박제한다(적용 단계에서 전부 제외됐으면 기록이 유령을 만든다). 이후 판정은
    #   이 기록을 진실로 쓰므로 판별자 파일이 사라져도 독자 집합이 유실되지 않고, 기록이 없던 구
    #   인스턴스는 여기서 backfill 된다(그 시점 추론 산출이 합집합에 이미 들어 있다).
    recordable, unestablished = established_harnesses(
        dest_root, selected_harnesses, preexisting_harnesses)
    if unestablished:
        print(f"  ⚠️ {', '.join(unestablished)} 어댑터가 이번 실행에서 서지 않아(복사 제외) 설치 "
              f"기록에 올리지 않습니다 — 위 제외 사유를 해소한 뒤 다시 실행하세요.", file=sys.stderr)
    template_coordinates = _copied_instance_owned_template_coordinates(
        plan, dest_root, src_root, copied_relpaths, "full")
    if record_install_receipt(
            dest_root, recordable, root_identity=root_identity,
            template_coordinates=template_coordinates):
        print(f"  ✓ 설치 기록 갱신 ({INSTALL_RECEIPT_RELPATH.as_posix()}): "
              f"{', '.join(recordable)}")
    # instance-owned config 원장 — **레이다운/보존 판정 직후**가 기록 시점이다. 방금 내려놓은
    #   (또는 기존 값이 template 과 같음을 확인한) 파일만 들어가고, 보존된 편집분은 안 들어간다
    #   (원장 부재 = 다음 동기가 보고 모드 = 안전 기본값).
    baselined = record_adapter_baseline(
        dest_root, src_root, recordable, root_identity=root_identity)
    if baselined:
        print(f"  ✓ 어댑터 config 원장 기록 ({ADAPTER_BASELINE_RELPATH.as_posix()}): "
              f"{', '.join(baselined)}")
    # 실복사 수를 보고한다(계획 수 아님) — 제외가 있었으면 위 요약이 사유를 이미 말한다.
    print(f"✓ add-harness 완료: {harness} 어댑터 {len(copied_relpaths)} 파일 복사 · "
          f"{n_subst} 파일 토큰 치환 (스코프: {adapter_scope} + {root_doc})")
    # claude 는 프로젝트 trust 수락 전 settings.json permissions.allow 를 조용히 무시한다.
    if harness == "claude":
        _print_claude_trust_guidance()
    # codex 는 laydown 만으로 활성화되지 않는다 — trusted project + hook trust 2단계 안내.
    if harness == "codex":
        _print_codex_trust_guidance()
    return plan


def add_harness_cli(
    dest_root: Path,
    harness: str,
    *,
    dry_run: bool,
    source_root: Path | None = None,
) -> int:
    """add_harness 의 main-style CLI 진입 — 인터페이스 예외를 친화 메시지 + rc 로 번역한다.

    운영 진입(`pm_config add-harness`·Decision 3)이 verbatim 위임하는 얇은 래퍼다. add_harness
    자체(확정 시그니처/로직)는 건드리지 않고, 그것이 던지는 인터페이스 예외만 CLI 경계에서
    잡아 `main()` 과 *동일하게* 처리한다(에러 처리의 단일 진실 = CLI contract owner = pm_import):
      - ValueError            : 미지원/집합 harness·미등록 표기 하네스(add_harness 입구 검증).
      - FileNotFoundError     : dest 부재/비-디렉토리·소스 템플릿 부재(resolve_template_roots).
      - FileVsDirConflict     : 어댑터 dst 위치에 기존 디렉토리(plan_copy·비파괴 거부).
      - AncestorConflict      : dst 조상에 symlink/비-디렉토리 파일(plan_copy·비파괴 거부).
      - UnsafeDestPathError   : engine.manifest·공유 문서 경로가 repo 밖 지향(복사 전 거부).
      - DestRootSwappedError  : dest 루트 자체가 계획 뒤 교체됨(**적용 중에도** 즉시 전체 중단).
    앞 넷(ValueError·FileNotFoundError·FileVsDirConflict·AncestorConflict)과
    `EmptyTemplateShippingInventoryError` 는 add_harness 가 *복사 전*(plan_copy·
    resolve_template_roots·입구 검증)에 던지므로 부분 적용 없이 깨끗한 `오류: …`(stderr) + rc 1 로
    끝난다(traceback 0·main 동형). **`UnsafeDestPathError` 는 복사 전(계획 경로 안전 가드)뿐 아니라
    복사 *중*(`CopyAction.run` 의 fd 가드 — 계획 뒤 조상·목적지 교체)에도 나올 수 있다**: 그때는
    그 시점까지의 복사가 남지만 저장소 밖 쓰기는 0이고, 사람이 정리한 뒤 재실행하면 된다.
    `DestRootSwappedError` 도 적용 중에 나올 수 있으며 그 중단은 부분 적용이 아니라 **오염
    차단**이다(대상 트리가 이미 바꿔치기됐다). 성공은 add_harness 가 자체 plan/summary 를 출력하고
    여기선 rc 0 만 돌려준다(위임 verbatim·중복 출력 0).

    dry_run/source_root 는 add_harness 로 그대로 전달한다(투명 위임). 반환: 0(성공)·1(인터페이스 예외).
    """
    try:
        add_harness(dest_root, harness, dry_run=dry_run, source_root=source_root)
    except (
        ValueError,
        FileNotFoundError,
        FileVsDirConflict,
        AncestorConflict,
        UnsafeDestPathError,
        DestRootSwappedError,
        EmptyTemplateShippingInventoryError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    return 0


def _instance_project_name(dest_root: Path) -> str:
    """라이브 인스턴스의 프로젝트 이름을 local.conf `project.name` 에서 읽는다(없으면 디렉토리명 폴백).

    add_harness 의 operational 토큰 치환이 인스턴스의 실제 이름을 존중하도록 —
    최초 import 가 local.conf 에 박아 둔 값을 재사용한다(_parse_conf_keys). local.conf 부재·
    `project.name` 미설정이면 dest 디렉토리명(main 의 --name 기본값과 동형).
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if local_conf.is_file():
        try:
            conf = _parse_conf_keys(_read_text_shared(local_conf, encoding="utf-8"))
        except (UnicodeDecodeError, OSError):
            conf = {}
        name = conf.get("project.name", "").strip()
        if name:
            return name
    return dest_root.name


# ── board submodule 셋업 ─────────────────────────────────
# `--new --board-submodule --board-remote <url>` 은 board(tickets+areas)를 superproject inline 이
# 아니라 **별도 git submodule**(`.project_manager/board`)로 세운다(board 전용 git·공유
# remote·`ignore=all`). inline 기본(플래그 없음)은 이 경로를 전혀 타지 않아 완전 무변경(현행 --new
# 회귀). 공유 remote 의 호스팅/권한 생성은 사용자 게이트 — 엔진은 URL 을 받아 배선만 한다.

_BOARD_SUBMODULE_PATH = ".project_manager/board"  # 표준 `git submodule add` 은 name == path.
_BOARD_SETUP_GIT_TIMEOUT_SECONDS = 600  # remote clone/submodule add (네트워크·큰 board 여유).

# board submodule areas.md 스캐폴드 canonical 칼럼 — board._AREAS_COLUMNS 를 미러한다(pm_import 는
# stdlib-only·board import 회피). drift 는 test 가 board 를 로드해 대조한다(가드).
_BOARD_AREAS_COLUMNS = ("repo", "prefix", "git", "test_cmd", "owner", "base",
                        "protected", "area_owner")

# board submodule 의 `.gitattributes` seed — areas.md 동시 등록 안전(양쪽 행 보존)의 배포처.
# board 는 **별도 git** 이라 superproject 루트 `.gitattributes` 의 `.project_manager/areas.md
# merge=union` 선언이 닿지 않는다(`check-attr` = unspecified·실측). 신규 board 는 여기서 seed 하고,
# 이미 만들어진 board 는 board.py `_ensure_board_gitattributes` 가 멱등 backfill 한다(seed 재실행 없음).
# 내용은 board._BOARD_GITATTRIBUTES_BLOCK 을 미러한다 — drift 는 test 가 board 를 로드해 대조한다.
_BOARD_GITATTRIBUTES_SCAFFOLD = (
    "# areas.md = 멀티-PM prefix 레지스트리 — 동시 등록(행 append)이 merge 에서 충돌하지 않도록\n"
    "# git 내장 union merge 드라이버로 양쪽 행을 모두 보존한다.\n"
    "# board 는 별도 git 이라 superproject 루트의 같은 선언이 닿지 않는다 — 여기가 그 배포처다.\n"
    "areas.md merge=union\n"
    "# Windows checkout에서도 엔진-소유 텍스트의 논리 개행을 LF로 유지한다(라운드 회수 bytes·\n"
    "# 재작성 byte 판정). 엔진의 표기 보존이 근본이고 이 선언은 그 위의 방어층이다.\n"
    "*.md text eol=lf\n"
    "*.json text eol=lf\n"
    "*.txt text eol=lf\n"
    "*.jsonl text eol=lf\n"
)


def _board_setup_git(argv: list[str], cwd: Path | None) -> tuple[int, str]:
    """board submodule 셋업용 git 실행 → (rc, stdout+stderr). fail-soft(예외→(1,msg)).

    `-c protocol.file.allow=always` 를 상시 얹어 로컬/`file://` remote(hermetic 테스트·self-split
    로컬 board) 의 submodule 전송을 허용한다 — ssh/https remote 는 그 transport 만 제어하므로 무영향
    (CVE-2022-39253 이후 file transport 는 submodule 재귀에서 기본 차단). git identity·credential 은
    사용자 환경 상속(공유 board=사용자-신뢰·credential helper/ssh-agent 그대로 작동). URL 은 호출부가
    `validate_upstream_value` 로 형태-안전 검증(credential-in-url·leading-dash·비허용 scheme 거부)한다.
    """
    git_binary = shutil.which("git")
    if git_binary is None:
        return 1, "git 바이너리를 찾을 수 없음 (PATH)."
    try:
        result = subprocess.run(
            [git_binary, "-c", "protocol.file.allow=always", *argv],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_BOARD_SETUP_GIT_TIMEOUT_SECONDS)
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except Exception as exc:  # noqa: BLE001 — fail-soft: rc!=0 로 호출부 위임.
        return 1, str(exc)


def _board_areas_scaffold() -> str:
    """신규 공유 board 의 초기 areas.md (canonical 8칼럼 헤더만·데이터 행 0).

    board.py `_areas_header_line()`/`_areas_separator_line()` 와 같은 형식이다. 등록 repo 0 →
    `registered_prefixes()` 빈 set → 무prefix `T-NNNN`(합류/멀티-repo 등록 전). 이후 `board.py init
    --prefix` / `pm-config repo add` 가 데이터 행을 append 한다(append-only 레지스트리).
    """
    header = "| " + " | ".join(_BOARD_AREAS_COLUMNS) + " |"
    separator = "|" + "|".join("---" for _ in _BOARD_AREAS_COLUMNS) + "|"
    return (
        "# Area Registry\n\n"
        "> per-repo 레지스트리. `board.py init --prefix` / "
        "`pm-config repo add` 가 등록.\n\n"
        f"{header}\n{separator}\n"
    )


def _cleanup_partial_board(dest_root: Path) -> None:
    """submodule add 실패 후 부분 board 잔재 정리 (best-effort).

    board working tree·staged gitlink·`.git/modules` 캐시·(우리가 만든) `.gitmodules` 를 최대한
    원복한다(실패는 삼킴 — 이미 fail-loud 중). --new 는 빈 dest 보장이라 이번 run 이 만든 것뿐 →
    통째 되돌려도 사용자 자산 위험 0. 남은 트리 복사분(wiki/tickets 등)은 유지한다.

    **재시도는 fresh/비운 dest 로** — `setup_board_submodule` 첫 게이트가 `board/` 존재 시 중단하고
    `main` 의 `--new` 가드가 비-빈 dest 를 거부하므로, *같은* dest 재실행은 막힌다. 남은 트리를
    수동 정리(또는 새 경로)한 뒤 재-import 한다.
    """
    board_dir = dest_root / ".project_manager" / "board"
    _board_setup_git(["rm", "-f", "--cached", _BOARD_SUBMODULE_PATH], cwd=dest_root)
    _board_setup_git(["submodule", "deinit", "-f", _BOARD_SUBMODULE_PATH], cwd=dest_root)
    # `.git/modules` 캐시는 git object·packfile 이라 read-only 다 — 맨 rmtree 는 Windows 에서
    #   실패하고, 그 실패를 삼키면 잔재 때문에 fresh dest 재시도까지 막힌다(실측). 공용 seam 이
    #   속성을 풀고 재시도하며, 그래도 남으면 자리를 알린다(정리 실패로 죽지는 않는다 —
    #   호출부는 이미 fail-loud 중이다).
    for leftover in (board_dir,
                     dest_root / ".git" / "modules" / ".project_manager" / "board"):
        try:
            _force_rmtree(leftover)
        except OSError as exc:
            print(f"경고: 부분 board 정리 실패 — 직접 지우세요: {leftover} "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)
    gitmodules = dest_root / ".gitmodules"
    if gitmodules.exists():
        gitmodules.unlink()


def setup_board_submodule(dest_root: Path, remote_url: str) -> int:
    """--new --board-submodule: board(tickets+areas)를 `.project_manager/board` submodule 로 셋업.

    (board 전용 git·공유 remote·`ignore=all`) 반환 rc(0=성공·비0=fail-loud).

    순서(부작용 원자성·부분 홈 최소화):
      1. remote 를 temp 로 clone. **비었으면**(신규 공유 board) 복사된 wiki/tickets 스캐폴드(치환
         완료·open/claimed/blocked/done + _template + README) + canonical areas.md +
         `.gitattributes`(areas.md merge=union) 를 seed → commit → push. **내용 있으면**
         (2번째 유저 합류) skip(기존 board 재사용 — `.gitattributes` 는 board.py 가 backfill).
         (빈 remote 를 *직접* `submodule add` 하면 git 이 checkout 할 커밋이 없어 rc128 로 거부한다 —
          실측. 그래서 add *전* 에 remote 를 non-empty 로 만들어 두 경로를 수렴시킨다.)
      2. `git submodule add <url> .project_manager/board` — remote 가 non-empty 라 checkout 성공.
      3. `.gitmodules` 에 `ignore = all`(committed·공유 default) 설정 — board PM-commit 이 design(코드)
         git status 를 오염시키지 않게(누출 0). board.py init 이 추가로 `.git/config`
         ignore=all 도 설정한다(`_configure_board_submodule` 재사용·per-clone).
      4. 복사된 dormant `wiki/tickets` 제거 — board 는 이제 submodule 에 산다(`board_root()` 가
         `board/tickets` 존재로 board/ 로 해소하므로 wiki/tickets 는 잉여).
    """
    board_dir = dest_root / ".project_manager" / "board"
    copied_tickets = dest_root / ".project_manager" / "wiki" / "tickets"
    if board_dir.exists():
        print(f"오류: {_BOARD_SUBMODULE_PATH} 가 이미 존재 — board submodule 셋업 중단.",
              file=sys.stderr)
        return 1
    tmp_clone = Path(tempfile.mkdtemp(prefix="pm_board_seed_"))
    try:
        # ── 1. clone remote → temp; 비었으면 스캐폴드 seed ──
        rc, out = _board_setup_git(["clone", remote_url, str(tmp_clone)], cwd=None)
        if rc != 0:
            print(f"오류: board remote clone 실패 (rc={rc}) — {remote_url}\n{out.strip()}",
                  file=sys.stderr)
            return rc or 1
        rc_head, _ = _board_setup_git(["rev-parse", "--verify", "HEAD"], cwd=tmp_clone)
        seeded = rc_head != 0  # HEAD 없음 = 커밋 0 = 빈 remote(신규 공유 board).
        if seeded:
            if not copied_tickets.is_dir():
                print("오류: 복사된 wiki/tickets 부재 — board 스캐폴드 불가(트리 복사 확인).",
                      file=sys.stderr)
                return 1
            shutil.copytree(copied_tickets, tmp_clone / "tickets")
            # board.STATUS_DIRS 와 같은 집합 — 처분 종결 `discarded` 포함. 신규 공유
            # board 는 빈 상태 디렉토리도 추적돼야 다른 clone 이 checkout 에서 받는다.
            for status in ("open", "claimed", "blocked", "done", "discarded"):
                sd = tmp_clone / "tickets" / status
                sd.mkdir(parents=True, exist_ok=True)
                (sd / ".gitkeep").touch(exist_ok=True)  # 빈 status dir git 추적(합류 유저 checkout).
            (tmp_clone / "areas.md").write_text(
                _board_areas_scaffold(), encoding="utf-8", newline="\n")
            # areas.md merge=union 은 **이 git**(board)에 선언돼야 유효하다 — 루트 선언은 다른
            # git 이라 닿지 않는다. 신규 clone 이라 기존 파일 없음(비파괴 판단 불요).
            (tmp_clone / ".gitattributes").write_text(
                _BOARD_GITATTRIBUTES_SCAFFOLD, encoding="utf-8", newline="\n")
            for step in (["add", "-A"],
                         ["commit", "-m", "board scaffold (pm-import --new --board-submodule)"],
                         ["push", "origin", "HEAD"]):
                rc, out = _board_setup_git(step, cwd=tmp_clone)
                if rc != 0:
                    print(f"오류: board seed `git {step[0]}` 실패 (rc={rc})\n{out.strip()}",
                          file=sys.stderr)
                    return rc or 1
        else:
            # 내용 있는 remote(합류) — **실제 board 인지 검증**(codex+reviewer must-fix). 오타/비-board
            #   URL 을 주면 clone 은 성공하고 seeded=False 로 흘러 아래(try 밖)에서 submodule add + step 4
            #   가 dormant wiki/tickets 를 무조건 제거 → board/tickets 부재인데 폴백처(wiki/tickets)도
            #   삭제 = 깨진 dual-inert board 를 rc0 "완료"로 낸다. add·dest mutation *전*(부작용 0·finally
            #   가 temp 정리)에 tickets/·areas.md 존재로 board 형태를 확인해 명확 실패시킨다.
            if not (tmp_clone / "tickets").is_dir() or not (tmp_clone / "areas.md").exists():
                print(
                    f"오류: --board-remote {remote_url!r} 은 유효한 board repo 가 아닙니다 "
                    f"(tickets/·areas.md 부재). 신규면 *빈* remote 를, 합류면 기존 board remote 를 "
                    f"주세요 (오타·비-board URL 확인).",
                    file=sys.stderr)
                return 1
    finally:
        try:
            _force_rmtree(tmp_clone)
        except OSError as exc:
            # clone 사본(read-only git object 포함)이 tempdir 에 남는다 — 셋업 결과를 뒤집지
            #   않지만 침묵하지 않는다.
            print(f"경고: board seed 임시 clone 정리 실패 — 직접 지우세요: {tmp_clone} "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)

    # ── 2. submodule add (remote 는 이제 non-empty — checkout 성공) ──
    rc, out = _board_setup_git(
        ["submodule", "add", remote_url, _BOARD_SUBMODULE_PATH], cwd=dest_root)
    if rc != 0:
        print(f"오류: git submodule add 실패 (rc={rc}) — {remote_url}\n{out.strip()}",
              file=sys.stderr)
        if seeded:
            # 방금 우리가 remote 를 scaffold+push 했으므로 remote 는 이제 non-empty 다 — fresh dest 로
            #   재시도하면 합류(재사용) 경로를 탄다(빈 remote 초기화 아님·중복 scaffold 없음).
            print("  (remote 는 이미 scaffold 됨 — fresh dest 로 재시도 시 기존 board 합류 경로).",
                  file=sys.stderr)
        _cleanup_partial_board(dest_root)
        return rc or 1

    # ── 3. .gitmodules ignore=all (committed·공유 default) — 표준 submodule add 는 name==path ──
    rc, out = _board_setup_git(
        ["config", "-f", str(dest_root / ".gitmodules"),
         f"submodule.{_BOARD_SUBMODULE_PATH}.ignore", "all"], cwd=dest_root)
    if rc != 0:
        # 비차단 — board.py init 의 _configure_board_submodule 이 .git/config 로 보완한다.
        print(f"경고: .gitmodules ignore=all 설정 실패 (rc={rc}) — "
              f".git/config 로 보완(board.py init).\n{out.strip()}", file=sys.stderr)

    # ── 4. dormant wiki/tickets 제거 (board 는 submodule 에 산다) ──
    if copied_tickets.is_dir():
        try:
            _force_rmtree(copied_tickets)
        except OSError as exc:
            # 남으면 board 는 submodule 로 정상 동작하지만 잉여 트리가 채택자를 헷갈리게 한다 —
            #   셋업을 실패로 뒤집지는 않되 자리를 알린다.
            print(f"경고: dormant wiki/tickets 제거 실패 — 직접 지우세요: {copied_tickets} "
                  f"({type(exc).__name__}: {exc})", file=sys.stderr)

    label = "신규 스캐폴드 seed+push" if seeded else "기존 board 재사용(합류)"
    print(f"✓ board submodule 셋업: {_BOARD_SUBMODULE_PATH} "
          f"(remote={remote_url} · {label} · ignore=all)")
    return 0


def git_init(dest_root: Path) -> int:
    """--new 대상에 git init. 이미 .git 있으면 skip(0). returncode 를 반환한다.

    MF2: git init 실패를 무시하면 git repo 없는 불완전 import 가 성공으로 끝난다 —
    board.py init 의 pre-push 훅이 git repo 에 의존하므로 명세상 치명적이다. returncode 를
    그대로 돌려주고, main 이 비0 이면 import 미완으로 판정한다.
    """
    if (dest_root / ".git").exists():
        return 0
    result = subprocess.run(
        ["git", "init", str(dest_root)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode


def ensure_backup_dir_gitignored(
    dest_root: Path, git_safe: set | None, copied_relpaths: set,
) -> str:
    """dest .gitignore 에 `.pm_import_backups/` 패턴을 보장한다 (git 위생).

    반환 상태: "added"(기존에 append) · "created"(신규 .gitignore 생성) ·
      "present"(이미 무시 중·멱등 skip) · "unsafe-skip"(사전 존재 unbacked 사용자 .gitignore —
      비파괴 위해 미변경) · "noop"(읽기 실패).

    **비파괴:** 기존 .gitignore 를 append 하려면 둘 중 하나여야 한다 —
      (a) **git-safe**(추적 중 & 미변경 → git 이 복원 가능), 또는
      (b) **이번 import 가 복사·관리한 파일**(`.gitignore` ∈ copied_relpaths) — 이 경우 사용자
          원본이 있었다면 CopyAction 이 이미 중앙 백업했으므로 append 가 안전하다.
    둘 다 아니면 *사전 존재하는 unbacked 사용자 파일*이므로(미추적/dirty·import 가 안 건드림)
    무백업 변조를 피해 "unsafe-skip" 으로 수동 추가를 안내한다(이 append 는 CopyAction 백업
    경로를 타지 않으므로 별도 가드 필요). .gitignore 가 없으면 새로 만든다(비파괴·신규 파일).
    """
    pattern = f"{BACKUP_DIR_NAME}/"
    gitignore = dest_root / ".gitignore"
    # .gitignore 가 symlink 면 write_text 가 링크 대상(프로젝트 밖 가능)을
    #   따라가 변조한다 — git-safe 여도 링크 대상은 git 복원 대상이 아니다. CopyAction 의 symlink
    #   비파괴 정책(follow_symlinks=False)과 일관되게 자동 append 를 거부한다.
    if gitignore.is_symlink():
        return "unsafe-skip"
    if gitignore.is_file():
        try:
            text = _read_text_shared(gitignore, encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            return "noop"
        # 이미 무시 중이면(정확한 패턴 줄·앞뒤 공백 무시) skip — 멱등.
        existing_lines = {line.strip() for line in text.splitlines()}
        if pattern in existing_lines or BACKUP_DIR_NAME in existing_lines:
            return "present"
        git_safe_ok = git_safe is not None and ".gitignore" in git_safe
        import_owned = Path(".gitignore") in copied_relpaths
        if not (git_safe_ok or import_owned):
            return "unsafe-skip"  # 사전 존재 unbacked 사용자 파일 — 무백업 변경 금지.
        prefix = "" if text.endswith("\n") or text == "" else "\n"
        new_text, status = f"{text}{prefix}{pattern}\n", "added"
    else:
        new_text, status = f"{pattern}\n", "created"
    # 방어: 위생 write 실패(권한 등)가 *복사·치환이 끝난* import 말미를
    #   깨뜨리지 않게 한다 — gitignore 위생은 should 부가단계라 실패해도 import 자체는 성공으로 둔다.
    try:
        gitignore.write_text(new_text, encoding="utf-8", newline="\n")
    except OSError:
        return "noop"
    return status


# ── main ───────────────────────────────────────────────────────────────────

def _translate_dest_safety_errors(func: Callable) -> Callable:
    """경로 안전 예외를 CLI 경계에서 rc 1 + 친화 메시지로 번역하는 데코레이터(traceback 0).

    두 클래스를 모두 잡는다:
      - `DestRootSwappedError` — dest 루트 자체 교체. 파일 단위 교체는 각 단계가 흡수하지만
        (그 파일만 제외 + loud) **루트 교체는 어느 단계도 흡수하면 안 된다**(흡수하면 남은
        단계가 교체된 트리에 계속 쓴다). 실행 전체를 감싸 한 번만 잡고 즉시 끝낸다.
      - `UnsafeDestPathError` — 계획 단계는 각 호출부가 이미 rc1 로 번역하지만, **복사 단계**의
        fd 가드(`CopyAction.run`)도 이 예외를 던질 수 있다. 백스톱이 없으면 그 경로만 traceback
        으로 끝나 `add_harness_cli` 와 비대칭이 된다.

    래핑 함수를 쪼개지 않고 데코레이터로 두는 이유: `main` 본문을 별도 함수로 옮기면 엔진 관용구
    가드(진입 `main()` 의 console helper 선행·하네스 이름 분기 면제)가 이름 기준이라 함께 깨진다."""
    @functools.wraps(func)
    def wrapper(argv: list[str] | None = None) -> int:
        try:
            return func(argv)
        except (DestRootSwappedError, UnsafeDestPathError) as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
    return wrapper


@_translate_dest_safety_errors
def main(argv: list[str] | None = None) -> int:
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    ap = argparse.ArgumentParser(
        prog="pm_import.py",
        description=_cli_description(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "온보딩(fresh 채택자): manager(project_manager) 경로/URL 만 있으면 자율 import — "
            "harness=기본 all(등록 어댑터 전체: " + "|".join(HARNESS_TEMPLATE_DIRS)
            + "; 단일/콤마 조합 명시 가능), --new(빈 PM 홈)/--into(기존 프로젝트 임베드) "
            "맥락 판단. 상세 가이드 = manager 루트 ADOPT.md. import 후 다음 단계: "
            + _runtime_skill_entry("pm-bootstrap") + " → "
            + _runtime_skill_entry("pm-env") + ".\n\n"
            "upstream 기록: --from 은 *파일 소스*, --upstream 은 *future update 기록*으로 "
            "디커플된다. local.conf 에 `upstream.path=`(pm_update 가 --from 생략 시 사용) + "
            "`upstream.rev=<commit>`(drift baseline·--from 이 로컬 git checkout 일 때)이 기록된다. "
            "--upstream 생략 시 --from 으로 폴백하되, --from 이 로컬 clone 이면 origin URL 을 자동도출한다 "
            "(릴리스 추적 기본). 재-import 시 현재 값으로 갱신."
        ),
    )
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--into", metavar="PATH", help="기존 프로젝트에 임베드 import(비파괴·백업·특정 케이스)")
    mode.add_argument("--new", metavar="PATH", help="PM 홈 생성 + git init (코드 없는 홈·표준 채택)")
    ap.add_argument(
        "--harness",
        type=_parse_harness_arg,
        default="all",
        metavar="HARNESS[,HARNESS...]|all",
        help=(
            "어댑터 집합 (등록: " + ", ".join(REGISTERED_HARNESSES)
            + "; 전체: all; default: all)"
        ),
    )
    ap.add_argument("--weight", choices=WEIGHT_CHOICES, default="full",
                    help="무게축 (default: full)")
    ap.add_argument("--from", dest="source", default=str(REPO),
                    help="이번 import 의 *파일 소스* checkout 경로 (default: 이 repo 루트). "
                         "엔진/어댑터 파일을 여기서 복사한다.")
    ap.add_argument("--upstream", dest="upstream", default=None,
                    help="future update 기록값(URL 선호) — pm_update 가 --from 생략 시 쓸 upstream. "
                         "URL|경로 self-describing. 생략 시 --from 으로 폴백하되, --from 이 로컬 "
                         "git clone 이면 `git remote get-url origin` 으로 URL 자동도출(릴리스 추적). "
                         "값은 안전 검증(scheme allowlist·credential 거부·leading-dash)을 통과해야 한다.")
    ap.add_argument("--name", help="{{PROJECT_NAME}} 값 (default: 대상 디렉토리명)")
    ap.add_argument("--fill", choices=FILL_CHOICES, default="manual",
                    help="자유서술 placeholder 채움 — auto: 하니스 구동 제안(opt-in), "
                         "manual: TODO 표시(default)")
    ap.add_argument(
        "--fill-harness",
        choices=FILL_HARNESS_CHOICES,
        default=None,
        help="fill 구동 하네스 (default: 선택 집합 중 등록 순서상 첫 가용 바이너리)",
    )
    ap.add_argument("--opencode-model", dest="opencode_model", metavar="PROVIDER/MODEL",
                    default=None,
                    help="{{OPENCODE_PRO_MODEL}} 결정적 치환값 (비대화/CI). 예 'ollama/glm-5.2:cloud'. "
                         "opencode 어댑터 미포함이면 무시(claude-only)")
    ap.add_argument("--dry-run", action="store_true",
                    help="적용 없이 fill 계획만 출력 (실 하니스 미호출·파일시스템 미변경)")
    ap.add_argument("--board-submodule", action="store_true",
                    help="board(tickets+areas)를 별도 git submodule(.project_manager/board)로 셋업 "
                         "(공유 board). --new + --board-remote <url> 필수. "
                         "미지정(기본)=board 를 superproject inline(무변경).")
    ap.add_argument("--board-remote", dest="board_remote", metavar="URL", default=None,
                    help="공유 board 의 remote URL (--board-submodule 필수). 빈 remote 면 tickets "
                         "구조+areas.md 로 초기화 후 push, 내용 있으면 재사용(합류). URL 안전검증 "
                         "(credential-in-url·leading-dash·비허용 scheme 거부).")
    args = ap.parse_args(argv)

    # --upstream 명시값은 *부작용 전* 입구에서 fail-closed 검증. 나쁜 값
    # (빈/leading-dash/credential-in-URL/비허용 scheme)을 silently 기록하지 않게 입구에서 거른다.
    if args.upstream is not None:
        ok, reason = validate_upstream_value(args.upstream)
        if not ok:
            print(f"오류: --upstream 값 거부 — {reason}", file=sys.stderr)
            return 1

    is_new = args.new is not None
    dest_root = Path(args.into or args.new).resolve()
    source_root = Path(args.source).resolve()
    project_name = args.name or dest_root.name
    today = datetime.date.today().isoformat()

    # --board-submodule: board 를 별도 git submodule 로 셋업. --new + --board-remote
    #   <url> 필수(공유 board 의 본질=remote·미제공 fail-loud). --new 없이 단독은 거부(범위=신규 홈
    #   셋업). *부작용 전* 입구에서 fail-closed 검증(--upstream·MF2 git_init 규율 정합). inline 기본
    #   (플래그 없음)은 이 게이트를 통과해 완전 무변경.
    if args.board_submodule:
        if not is_new:
            print("오류: --board-submodule 은 --new 와 함께만 씁니다 (신규 PM 홈 셋업 범위). "
                  "기존 프로젝트엔 수동 `git submodule add` 를 쓰세요.", file=sys.stderr)
            return 1
        if not args.board_remote:
            print("오류: --board-submodule 은 --board-remote <url> 이 필수입니다 "
                  "(공유 board 의 본질은 remote). 예: --board-remote git@github.com:you/board.git",
                  file=sys.stderr)
            return 1
        ok, reason = validate_upstream_value(args.board_remote)
        if not ok:
            print(f"오류: --board-remote 값 거부 — {reason}", file=sys.stderr)
            return 1
    elif args.board_remote:
        print("오류: --board-remote 는 --board-submodule 과 함께 씁니다 "
              "(board submodule 셋업 없이는 무의미).", file=sys.stderr)
        return 1

    try:
        template_roots = resolve_template_roots(source_root, args.harness)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # --new 대상이 기존 *파일* 이면 아래 iterdir() 가 NotADirectoryError 로
    #      터진다 — 디렉토리 여부를 먼저 검사해 친화적 비0 오류로 거부한다.
    if is_new and dest_root.exists() and not dest_root.is_dir():
        print(
            f"오류: --new 대상이 디렉토리가 아닌 기존 파일입니다: {dest_root}. "
            f"다른 경로를 지정하거나 해당 파일을 직접 옮긴 뒤 다시 시도하세요.",
            file=sys.stderr,
        )
        return 1

    # MF2: --new 는 백업 없이 복사하므로(아래 backup_root=None), 대상이 비어있지 않으면
    #      기존 파일을 무백업 덮을 위험 → 명세(비0=대상 비어있지 않은데 백업 불가)대로 거부.
    #      dry-run 에서도 동일 판정(계획 전 게이트).
    if is_new and dest_root.is_dir() and any(dest_root.iterdir()):
        print(
            f"오류: --new 대상이 비어있지 않습니다: {dest_root}. "
            f"기존 파일이 있는 디렉토리에는 비파괴 백업이 되는 --into 를 사용하세요.",
            file=sys.stderr,
        )
        return 1

    # MF2: --into 는 *기존 프로젝트* 가정이다. 미존재/비-디렉토리 경로면, 복사가 디렉토리를
    #      새로 만들고 git init 없이 board.py init 이 성공해 pre-push 훅 없는 불완전 import 가
    #      "완료"된다 → 거부. 새 프로젝트는 git init·디렉토리 생성을 하는 --new 로 안내.
    #      dry-run 에서도 동일 판정(계획 전 게이트 — --new 가드와 대칭).
    if not is_new and not dest_root.is_dir():
        print(
            f"오류: --into 대상이 존재하는 디렉토리가 아닙니다: {dest_root}. "
            f"--into 는 기존 프로젝트 전용입니다 — 새 프로젝트는 --new 를 사용하세요 "
            f"(디렉토리 생성 + git init).",
            file=sys.stderr,
        )
        return 1

    # 다중 tree import의 manifest 합집합은 **어떤 복사보다 먼저** 읽고 병합 가능성을 검증한다.
    # 후순위 manifest 부재/병합 실패를 복사 뒤 발견하면 --new/--into 모두 부분 설치가 남는다.
    try:
        prepared_manifest_union = _prepare_selected_manifest_union(template_roots)
        _warn_selected_manifest_union_conflicts(prepared_manifest_union)
        entry_doc_source_overrides = _selected_entry_doc_source_overrides(
            source_root, args.harness, args.weight)
    except (OSError, ValueError, RuntimeError) as exc:
        print(
            "오류: 선택된 어댑터 manifest 합집합/진입문서 병합을 만들 수 없어 복사 전에 "
            "중단합니다 — "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    # --into 백업을 중앙 디렉토리 `<dest>/.pm_import_backups/<DATE>/` 로 모은다.
    #   --new 는 빈 디렉토리 보장이라 백업 없음(backup_root=None). git_safe = '추적&미변경'
    #   relpath 집합(또는 None=비-git·판정불가). git 호출 실패는 None→전부 백업(보수적 폴백).
    backup_root = None if is_new else dest_root / BACKUP_DIR_NAME / today
    git_safe = None if is_new else git_safe_relpaths(dest_root)
    # dest 루트 신원을 **계획(plan_copy) 앞에서** 고정한다 — 계획 자체가 dest 를 읽으므로, 고정이
    #   계획 뒤면 "계획은 원래 트리에서 세우고 적용은 교체된 트리에서" 라는 어긋남이 남는다.
    #   `--new` 는 아직 트리가 없어 생성 직후에 잡는다. 획득 실패는 fail-loud(복사 전 rc1) —
    #   None 폴백은 루트 검사를 통째로 끄는 우회가 된다.
    root_identity = None
    if not is_new:
        try:
            root_identity = dest_root_identity(dest_root)
        except UnsafeDestPathError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1
    try:
        actions = plan_copy(
            template_roots, dest_root, backup_root, args.weight,
            git_safe=git_safe, source_overrides=entry_doc_source_overrides,
        )
        # local.conf 백업 target 의 조상도 plan 단계에서 검증한다. 이 백업은
        #   plan_copy actions 밖(apply 후 backup_existing_local_conf)에서 일어나므로, 그 조상이
        #   일반 파일/symlink 면 *복사가 일부 끝난 뒤* mkdir 실패로 부분 적용이 남는다 → 사전 차단.
        if backup_root is not None and not is_new:
            _local_conf = dest_root / ".project_manager" / "local.conf"
            if _local_conf.is_file() or _local_conf.is_symlink():
                _check_ancestor_safe(
                    dest_root, backup_root / ".project_manager" / "local.conf", set())
    except (
        FileVsDirConflict,
        AncestorConflict,
        EmptyTemplateShippingInventoryError,
    ) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    try:
        pm_update_mod = _load_pm_update()
        if pm_update_mod is None:
            raise RuntimeError(
                "pm_update.py를 로드할 수 없어 하네스별 스킬 표기 context를 만들 수 없습니다."
            )
        # 표기 독자 = **dest 실설치 하네스 ∪ 이번 선택**. `--into` 로 codex 인스턴스에 claude 를
        #   얹으면 공유 wiki 는 두 하네스가 함께 읽는다 — 이번 run 의 집합만 쓰면 새로 깔린
        #   문서가 claude 단독 표기로 나가 기존 codex 독자에게 틀린 표기가 된다(실측).
        #   `--new` 는 빈 dest 라 실설치 집합이 비어 선택과 같다(현행 회귀 불변).
        #   기존 설치분은 따로 붙들어 둔다 — 설치 기록 자격 판정이 "이번에 선 어댑터" 와 "원래
        #   있던 것" 을 갈라야 한다(원래 것은 이번 실행이 임의로 철회하지 않는다).
        preexisting_harnesses = installed_harnesses(dest_root, source_root)
        notation_harnesses = tuple(
            name for name in HARNESS_TEMPLATE_DIRS
            if name in {*preexisting_harnesses, *args.harness}
        )
        # context 파생 manifest 도 그 독자 집합에서 뽑는다(선택 트리만 보면 기존 하네스가 빠져
        #   공유 문서가 단독 표기로 재렌더된다). 소스에서 한 하네스의 manifest 라도 못 얻으면
        #   **조용히 빼지 않고 복사 전에 중단한다** — 조용한 제외는 그 독자가 없는 것처럼
        #   재렌더해(기존 codex 인스턴스에 claude 단독 표기) 이 티켓이 닫은 클래스를 되살린다.
        notation_manifests = []
        missing_notation_manifests = []
        for name in notation_harnesses:
            for dirname in HARNESS_TEMPLATE_DIRS[name]:
                manifest = (source_root / "templates" / dirname
                            / ".project_manager" / "engine.manifest")
                if manifest.is_file():
                    notation_manifests.append(manifest)
                else:
                    missing_notation_manifests.append(f"{name}({manifest})")
        if missing_notation_manifests:
            raise RuntimeError(
                "설치 하네스의 표기 manifest 를 소스에서 찾을 수 없습니다: "
                f"{', '.join(missing_notation_manifests)}. 그 하네스를 독자에서 조용히 빼면 "
                "공유 문서가 잘못된 단독 표기로 재렌더되므로 중단합니다 — `--from` 이 그 flavor "
                "template 을 가진 checkout 인지 확인하세요."
            )
        entry_notation_templates = pm_update_mod._entry_notation_templates_from_manifests(
            notation_manifests
            or [root / ".project_manager" / "engine.manifest" for root in template_roots],
            source_root,
        )
        shared_agents_members = NEUTRAL_SHARED_ENTRY_DOCS[Path("AGENTS.md")][0]
        selected_shared_agents = tuple(
            HARNESS_TEMPLATE_DIRS[harness][0]
            for harness in notation_harnesses
            if harness in shared_agents_members
        )
        # AGENTS.md 는 manifest 미등재(instance-owned 루트 doc)라 소유자 매칭으로는 context 가
        #   안 나온다. 독자가 설치 하네스 전체가 아니라 이 진입문서를 읽는 부분집합이므로
        #   wiki 폴백에 맡기지 않고 그 멤버십을 명시 전달한다 — 단일 하네스도 마찬가지다
        #   (조용한 skip 0·단일이면 그 하네스 표기라 출하 형상에선 no-op).
        if selected_shared_agents:
            entry_notation_templates["AGENTS.md"] = selected_shared_agents
    except (OSError, ValueError, RuntimeError) as exc:
        # 엔진 사본 skew 는 삼키지 않는다 — 이 블록은 설치 하네스 판별(`installed_harnesses` →
        #   출하 인벤토리 열거)까지 품어 skew 가 도달할 수 있고, 그걸 "context 실패 rc1" 로
        #   덮으면 로드 경계 진단이 사라진다(엔진 관례: fail-soft 하되 skew 만 재-raise).
        if _is_engine_rev_skew(exc):
            raise
        print(
            "오류: 선택된 어댑터의 스킬 표기 context를 만들 수 없어 복사 전에 중단합니다 — "
            f"{exc}",
            file=sys.stderr,
        )
        return 1

    template_dir_names = [
        template_dir
        for harness_name in args.harness
        for template_dir in HARNESS_TEMPLATE_DIRS[harness_name]
    ]
    # 폴백 표기 context = 이 인스턴스를 읽는 하네스 전체(기존 설치 + 이번 선택).
    notation_context_dirs = tuple(
        template_dir
        for harness_name in notation_harnesses
        for template_dir in HARNESS_TEMPLATE_DIRS[harness_name]
    )
    # 미등록 표기 하네스는 **복사 전** 중단한다 — 렌더 단계에서 터지면 부분 설치가 남는다.
    #   판정 대상은 실제 렌더 context 전체다(기존 설치 하네스가 미등록이어도 복사 뒤에 터진다).
    unregistered_notation = unregistered_skill_notation_template_dirs(
        notation_context_dirs)
    if unregistered_notation:
        print(
            f"오류: {_unregistered_skill_notation_message(unregistered_notation)}",
            file=sys.stderr,
        )
        return 1

    # 복사 대상이 아닌 **기존 dest 파일**의 재렌더 계획(`--into` 축·`--new` 는 빈 dest 라 0건).
    #   복사가 닿지 않는 두 부류가 여기서 걸린다: (a) 이번 선택 하네스가 출하하지 않는 wiki seed
    #   (기존 하네스가 깔아 둔 것) (b) 템플릿에서 *생성된* 인스턴스 파일(`pm_state.md`). 계획·백업·
    #   경로 안전을 add-harness 와 같은 경로로 태운다(비파괴 보장 동형).
    copied_dst_relpaths = {a.dst.relative_to(dest_root) for a in actions}
    try:
        existing_rerender_relpaths, existing_backup_blocked = (
            _shared_notation_rerender_plan(
                dest_root,
                _notation_rerender_contexts(
                    entry_notation_templates,
                    _unowned_shipped_wiki_relpaths(
                        source_root, notation_harnesses, entry_notation_templates),
                    notation_context_dirs,
                ),
                backup_root,
                git_safe,
                root_identity=root_identity,
            )
        )
    except UnsafeDestPathError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    # 이번 run 이 복사하는 파일은 이미 render 범위 안이라 중복 처리하지 않는다.
    existing_rerender_relpaths = [
        rel for rel in existing_rerender_relpaths if rel not in copied_dst_relpaths]

    # ── 계획 출력 ──
    mode_label = f"--new {dest_root}" if is_new else f"--into {dest_root}"
    harness_label = ",".join(args.harness)
    print(f"[pm_import] {mode_label}  harness={harness_label}  weight={args.weight}")
    print(f"  소스: {source_root}/templates/{'+'.join(template_dir_names)}")
    n_copy = len(actions)
    n_backup = sum(1 for a in actions if a.backup is not None)
    n_git_safe = sum(1 for a in actions if a._git_safe_skip)
    for a in actions:
        print(a.describe())
    # 백업이 중앙 디렉토리이고, git-safe(추적&미변경)는 백업 생략임을 한 줄로 요약한다.
    if not is_new:
        git_note = (
            f"git work tree (추적&미변경 {n_git_safe} 백업 생략)"
            if git_safe is not None
            else "비-git/판정불가 (충돌 전부 백업)"
        )
        print(f"  백업 위치: {BACKUP_DIR_NAME}/{today}/  · {git_note}")
    print(f"  → {n_copy} 파일 복사 ({n_backup} 백업), placeholder 치환, board.py init")
    # 복사 대상은 아니지만 표기 병기로 *내용이 바뀌는* 기존 파일 — 계획에 명시한다(dry-run 공통).
    if existing_rerender_relpaths:
        print(f"  기존 문서 재렌더 ({len(existing_rerender_relpaths)}건 · 병기 표기 · "
              "백업 후 변경):")
        for rel in existing_rerender_relpaths:
            print(f"  [rerender] {rel.as_posix()}")
    for rel_posix in existing_backup_blocked:
        print(f"  ⚠️ 기존 문서 {rel_posix}: 백업 자리({BACKUP_DIR_NAME}/)가 막혀 있어 재렌더를 "
              "생략합니다 — 백업 못 하는 파일은 고치지 않습니다(비파괴).", file=sys.stderr)
    if len(template_roots) > 1:
        print(
            f"  engine.manifest: 선택된 {len(template_roots)}개 트리 선언의 합집합 "
            "(중복 경로는 registry 정규 순서상 첫 트리 우선)"
        )
    if args.board_submodule:
        print(f"  board submodule: {args.board_remote} → {_BOARD_SUBMODULE_PATH} "
              f"(빈 remote 면 구조 init+push·ignore=all)")

    # fill 단계 계획/게이트 미리보기 (dry-run·실행 공통). 실 하니스 호출 여부는 opt-in 게이트
    # (PM_IMPORT_LIVE_HARNESS=1 AND --fill auto)로 결정한다 — 여기서는 의도만 출력한다.
    fill_harness = _resolve_fill_harness(args.fill_harness, args.harness)
    if args.fill == "auto" and fill_harness not in FILL_CAPABLE_HARNESSES:
        print(
            f"오류: fill harness {fill_harness!r}는 runner 매핑이 없습니다 — "
            f"지원: {', '.join(FILL_CAPABLE_HARNESSES)}. 파일 설치 전 중단합니다.",
            file=sys.stderr,
        )
        return 1
    live_allowed = _live_harness_allowed(args.fill)
    if args.fill == "auto":
        try:
            cap_warning = fill_harness_cap_advisory(fill_harness, dest_root)
        except Exception as exc:  # noqa: BLE001 — advisory는 import를 막지 않는다.
            if _is_engine_rev_skew(exc):
                raise
            print(f"[fill auto] 경고: 하네스 상한 판정 실패({exc})", file=sys.stderr)
        else:
            if cap_warning is not None:
                print(cap_warning, file=sys.stderr)
    if args.fill == "auto":
        gate = "실구동(게이트 통과)" if live_allowed else "stub/미구동(게이트 미통과 — 안전)"
        print(f"  fill=auto  harness={fill_harness}  → {gate}")
    else:
        print("  fill=manual  → 자유서술 placeholder 를 TODO 로 표시(하니스 미구동).")

    if args.dry_run:
        # opencode 모델 결정적 해소 계획(LLM 아님·fill 이전 단계). 복사 *예정* src 에
        #         {{OPENCODE_PRO_MODEL}} 가 잔존하면(opencode 트리) 어느 경로로 갈지·플래그값·
        #         tty 여부만 출력한다 — 프롬프트·파일변경·`opencode models` 실호출 0.
        if _plan_opencode_model_targets(actions):
            stdin_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
            print("  [dry-run] opencode 모델 해소 계획 (결정적·LLM 아님·파일 미변경):")
            print(f"    stdin tty: {stdin_tty}")
            if args.opencode_model:
                print(f"    경로: flag → --opencode-model '{args.opencode_model}' 로 치환.")
            elif stdin_tty:
                print("    경로: interactive → `opencode models` 번호목록·대화형 선택 후 치환.")
            else:
                print("    경로: todo → 비-tty(또는 opencode 부재) — 치환 안 함·TODO 마커 폴백.")
        # MF2: --dry-run = fill 계획 미리보기. 실 하니스 미호출·파일 미변경. auto 면 무엇을
        #      채울지(대상 토큰)·결정된 fill harness·게이트 상태(실구동/manual 폴백)를 출력한다.
        #      복사를 안 하므로 dest 가 아닌 복사 *예정* src(actions)에서 잔존 토큰을 스캔한다.
        if args.fill == "auto":
            plan_tokens = _plan_fill_targets(actions)
            print("  [dry-run] fill=auto 계획 (실 하니스 미호출·파일 미변경):")
            if plan_tokens:
                print(f"    채울 대상 토큰: {'·'.join(plan_tokens)}")
            else:
                print("    채울 대상 토큰: (트리에 자유서술 placeholder 없음 — 처리 불필요)")
            print(f"    fill harness: {fill_harness}")
            if live_allowed:
                print("    게이트: 통과 → 적용 시 실 하니스 구동(제안 — 사람 검토 전제).")
            else:
                print("    게이트: 미통과(PM_IMPORT_LIVE_HARNESS 미설정) → 적용 시 manual 폴백"
                      "(TODO 표시·하니스 미구동).")
        print("[dry-run] 적용 안 함 (파일시스템 미변경).")
        return 0

    # ── 적용 ──
    if is_new:
        dest_root.mkdir(parents=True, exist_ok=True)
        # MF2: git init 실패 시 git repo 없는 불완전 import — board.py init 의 pre-push 훅이
        #      git repo 에 의존하므로 비0 전파(복사 전에 중단).
        git_rc = git_init(dest_root)
        if git_rc != 0:
            print(
                f"오류: git init 비0 종료({git_rc}) — import 미완(git repo 없이는 "
                f"board.py init 의 pre-push 훅이 동작하지 않습니다).",
                file=sys.stderr,
            )
            return git_rc
        # 트리를 이제 만들었으므로 여기서 신원을 고정한다(이후 렌더까지 재사용). 여기도 fail-loud
        #   — 아직 파일을 하나도 복사하지 않은 지점이라 중단이 곧 "부분 적용 0" 이다.
        try:
            root_identity = dest_root_identity(dest_root)
        except UnsafeDestPathError as exc:
            print(f"오류: {exc}", file=sys.stderr)
            return 1

    # 복사 **직전** 루트 재확인(위 add-harness 와 동형) — 뒤에 두면 첫 쓰기가 교체 트리에 간다.
    assert_dest_root_unchanged(dest_root, root_identity)
    # 파일 단위 위반은 그 파일만 빼고 loud(적용 단계 rc 정책) — 루트 교체만 전체 중단.
    copy_outcome = apply_copy_plan(actions, dest_root, root_identity=root_identity)
    report_copy_apply_anomalies(copy_outcome)

    merged_manifest_entries = _install_selected_manifest_union(
        prepared_manifest_union, dest_root)
    if merged_manifest_entries:
        print(
            f"✓ engine.manifest 선택 트리 합집합 설치 "
            f"({len(template_roots)}개 flavor · {merged_manifest_entries}개 관리 경로)"
        )

    # MF1: 치환 범위 = 이번 run 이 **실제로 복사한** 파일만(복사 안 한 사용자 파일 불가침·
    #      적용 단계에서 제외된 파일도 범위 밖).
    copied_relpaths = set(copy_outcome.copied)
    subs = _substitution_map(project_name, dest_root, today)
    n_subst = substitute_placeholders(dest_root, subs, copied_relpaths,
                                      root_identity=root_identity)
    print(f"✓ {len(copied_relpaths)} 파일 복사 · {n_subst} 파일 placeholder 치환")

    # ── opencode 모델 결정적 해소: substitute *직후*·render *이전*. @render 활성화
    #    로 .opencode/agents 가 render 대상이 됐으므로, render_managed_files 가 model: 줄의
    #    {{OPENCODE_PRO_MODEL}} 을 만나기 *전* 에 해소해야 한다 — flag/interactive=토큰 치환,
    #    todo(미해소)=줄 주석화 + 토큰 중화(<provider/model>) → 어느 경로든 render 시점엔 토큰 0.
    #    (활성화가 그 순서
    #    가정을 깸·옛 "todo 토큰은 YAML 주석으로 남아 leak 없음" 전제 무효.) local.conf 기록
    #    (record_opencode_model)은 board init 이 local.conf 를 만든 *뒤* 로 분리(아래).
    #    LLM 추측(fill)이 아니라 `opencode models` 결정적 조회로 해소(환각·미가용 모델 제거).
    #    범위 = substitute_placeholders 와 동일한 copied_relpaths(비파괴). claude-only=inactive.
    model_result = resolve_opencode_model(
        dest_root, copied_relpaths, model_arg=args.opencode_model,
        root_identity=root_identity)
    if model_result.active:
        if model_result.path == "flag":
            print(f"✓ {OPENCODE_MODEL_TOKEN} 치환(--opencode-model "
                  f"'{model_result.model}', {model_result.changed} 파일)")
        elif model_result.path == "interactive":
            print(f"✓ {OPENCODE_MODEL_TOKEN} 치환(대화형 선택 "
                  f"'{model_result.model}', {model_result.changed} 파일)")
        elif model_result.path == "todo":
            print(f"  {OPENCODE_MODEL_TOKEN} TODO 표시 — {model_result.note}")

    # render 단계: @render manifest path 의 복사본을 render_adapter
    # 산출물로 다시 쓴다 — operational 토큰(subs·이미 sed) 치환. free-form value-fill 은 
    # 로 제거(FILL 채널이 canonical home 전담). substitute·모델해소 *직후*. 범위 = copied_relpaths(비파괴).
    # 복사 밖 기존 문서(위 계획)는 **변경 직전 백업 후** 같은 render 채널에 실어 병기 표기를
    #   받는다. 치환·fill 범위(copied_relpaths·MF1)는 넓히지 않는다 — 표기 렌더만 태운다.
    existing_backup = _backup_before_inplace_edit(
        dest_root, existing_rerender_relpaths, backup_root, git_safe,
        root_identity=root_identity)
    if existing_backup.backed_up:
        print(f"✓ 기존 문서 {len(existing_backup.backed_up)}건 백업: "
              f"{', '.join(existing_backup.backed_up)}")
    # 백업 못 한 파일은 고치지 않는다(비파괴) — 교체·삭제 둘 다 재렌더 대상에서 뺀다.
    if existing_backup.refused:
        print(f"  ⚠️ 기존 문서 {len(existing_backup.refused)}건: 계획 검증 뒤 경로를 안전하게 열 수 "
              f"없어(symlink 교체·비-디렉토리 컴포넌트) 백업할 수 없습니다 — 재렌더에서 "
              f"제외합니다(원본·저장소 밖 파일 불변): {', '.join(existing_backup.refused)}",
              file=sys.stderr)
    if existing_backup.vanished:
        print(f"  ⚠️ 기존 문서 {len(existing_backup.vanished)}건: 계획 검증 뒤 대상이 사라져(경쟁 "
              f"삭제) 백업할 수 없습니다 — 재렌더에서 제외합니다(새로 만들지 않습니다): "
              f"{', '.join(existing_backup.vanished)}", file=sys.stderr)
    _existing_dropped = set(existing_backup.refused) | set(existing_backup.vanished)
    if _existing_dropped:
        existing_rerender_relpaths = [
            rel for rel in existing_rerender_relpaths
            if rel.as_posix() not in _existing_dropped]
    n_render = render_managed_files(
        dest_root,
        subs,
        copied_relpaths | set(existing_rerender_relpaths),
        entry_notation_templates=entry_notation_templates,
        installed_notation_context=notation_context_dirs,
        root_identity=root_identity,
    )
    if n_render:
        print(f"✓ {n_render} 파일 render (operational 토큰 치환)")

    # ── board submodule 셋업: --new --board-submodule 일 때만. board.py init
    #    (아래 run_board_init) *이전* 에 둬야 (a) board_root() 가 board/ 로 해소되고 (b) board.py
    #    init 의 _configure_board_submodule() 이 .git/config ignore=all 을 설정한다. substitute·
    #    render *이후* — 복사·치환이 끝난 wiki/tickets 를 스캐폴드로 board 에 seed 하기 때문. inline
    #    기본(플래그 없음)은 이 블록을 건너뛰어 완전 무변경(현행 --new 회귀). 실패 시 fail-loud.
    if is_new and args.board_submodule:
        board_rc = setup_board_submodule(dest_root, args.board_remote)
        if board_rc != 0:
            print("오류: board submodule 셋업 실패 — import 미완(board 미배선·부분 홈).",
                  file=sys.stderr)
            return board_rc

    # MF1: board.py init 은 local.conf 를 무조건 덮으므로(local.conf 는 복사/백업 대상 트리
    #      밖), --into 재-import 면 기존 per-clone 설정(additional_reviewer.enabled·
    #      additional_reviewer.* 등)이 무백업 손실된다.
    #      init *호출 전*에 백업하고 원본 텍스트를 받아둔다(--new 는
    #      빈 디렉토리 보장이라 None — 보존할 것 없음).
    # 구표기 conf 는 백업 전에 멈춘다 — 바로 뒤 `board.py init` 이 그 conf 를 읽어 fail-loud 하므로
    # 원인이 traceback 으로만 보이면 채택자가 무엇을 바꿔야 하는지 알 수 없다. 자동 이관은 하지
    # 않는다(엔진은 채택자 소유 파일을 대신 고쳐 쓰지 않는다) — 키 단위 처방만 낸다.
    if not is_new and _local_conf_has_blocking_legacy(dest_root):
        print("오류: local.conf 에 구표기 키가 남아 있어 board.py init 을 실행하지 않습니다 "
              "— 엔진 파일은 이미 갱신됐으니 아래대로 키를 바꾸고 같은 명령을 다시 실행하세요.",
              file=sys.stderr)
        print_conf_migration_notice(dest_root)
        return 1

    preserved_conf_text = backup_existing_local_conf(dest_root, backup_root) if not is_new else None

    # SF2: board.py init 비0 이면 local.conf·pm_state 미생성 = import 미완 → 비0 전파.
    # board init 은 subprocess 라 우리 fd 를 물려줄 수 없다 — 직전에 루트를 재확인해 창을 좁힌다
    #   (검사-사용 gap 은 남는다·구조적 폐쇄 아님). 불일치는 전체 중단 클래스다.
    assert_dest_root_unchanged(dest_root, root_identity)
    rc = run_board_init(dest_root)
    if rc != 0:
        print(f"오류: board.py init 비0 종료({rc}) — import 미완(local.conf·pm_state 확인 필요).",
              file=sys.stderr)
        return rc

    # board.py init 은 `project.name` 빈값·`test.cmd=pytest -q` 를 하드코딩한다.
    # init 성공 직후 local.conf 의 operational 해소값(project.name·test.cmd·runtime.py)을 sed
    # 치환값과 정렬해 엔진 문서(local.conf 해소)와 CLAUDE.md(치환)가 같은 값을 보게 한다.
    if sync_local_conf(dest_root, project_name):
        print("✓ local.conf operational 값 동기화 (project.name·test.cmd·runtime.py)")

    # MF1: init 이 덮은 local.conf 위에, 백업해 둔 기존 사용자 키 중 init 이 *안 쓴* 것
    #      (additional_reviewer.enabled·additional_reviewer.* 등)을
    #      재병합. init/operational sync 값은 우선.
    if preserved_conf_text is not None:
        reapply_preserved_conf_keys(dest_root, preserved_conf_text)

    # upstream 값을 local.conf 에 upstream= 으로 기록한다. board init·conf
    #   sync·preserve 단계 *이후* 에 둬야 한다 — 그래야 재-import 에서도 preserve 가 stale 값을
    #   붙들지 않고 *현재 값* 으로 확정된다(_set_conf_keys 제자리 갱신). 이후 pm_update 가
    #   --from 생략 시 이 값을 기본 upstream 으로 쓴다(--new·--into 공통).
    #
    #   --from(파일 소스)↔--upstream(update 기록) 디커플:
    #     --upstream 명시      → 그 값(URL|경로·이미 입구에서 검증됨).
    #     생략 + --from 이 로컬 git clone → origin URL 자동도출(릴리스 추적 기본).
    #     생략 + 도출 실패(git repo 아님·origin 부재) → --from 경로(기존 동작 회귀 보존).
    upstream_value = args.upstream
    if upstream_value is None:
        derived = derive_origin_url(source_root)
        upstream_value = derived if derived is not None else str(source_root)
    if record_upstream(dest_root, upstream_value):
        print(f"✓ local.conf upstream.path 기록 (pm_update --from 기본값): {upstream_value}")

    # upstream.rev baseline 기록 — --from 이 로컬 git checkout
    # 이면 그 HEAD commit 을 baseline 으로 박는다("마지막 동기 이후 변경" 의 기준점). git repo
    # 아님·HEAD 해소 실패면 graceful 생략(URL upstream 은 로컬 checkout 이 없어 baseline 없음 —
    # 스킬층이 fetch 후 upstream.seen_rev 를 별도 기록·별개 키).
    baseline_rev = read_upstream_rev(source_root)
    if baseline_rev and record_upstream_rev(dest_root, baseline_rev):
        print(f"✓ local.conf upstream.rev baseline 기록 (drift-lint 기준점): {baseline_rev}")

    # ── opencode 모델 local.conf 기록: board init·conf sync 가 local.conf 를 만든 *뒤*.
    #    실제 모델을 해소한 경로(flag·interactive)만 기록 — 이후 pm_update @render 가
    #    {{OPENCODE_PRO_MODEL}} 을 local.conf 에서 재유도할 때 키 부재로 leak assertion crash 하는
    #    걸 막는다. todo(미해소)는 위 resolve 가 토큰을 주석화+중화(<provider/model>)했으니 기록
    #    안 함(키 없어도 어댑터에 토큰 0 → leak 없음). claude import 는 active=False 라 자연 skip.
    #    (resolve_opencode_model 자체는 render 이전으로 이동·위 substitute 직후 블록 참조.)
    if model_result.active and model_result.path in ("flag", "interactive") \
            and model_result.model:
        if record_opencode_model(dest_root, model_result.model):
            print(f"✓ local.conf harness.opencode.pro_model 기록 ({model_result.model})")

    # ── 설치 기록(install receipt): 이번 선택 ∪ dest 에 이미 설치돼 있던 하네스(=
    #    `notation_harnesses`·복사 전에 산출한 그 독자 집합)를 인스턴스 메타에 박제한다. 이후
    #    `installed_harnesses`(pm_import 판정·pm_update 표기 독자)는 증거 추론 대신 이 기록을 읽으므로
    #    판별자 파일이 지워져도 독자가 유실되지 않는다. 기록이 없던 구 인스턴스는 `--into` 재-import
    #    가 이 지점에서 backfill 한다(추론 산출을 한 번 원천으로 옮긴다). 복사·render 이후에 두는
    #    이유는 `.project_manager/` 가 그때 확실히 존재하기 때문이다(--new 포함).
    #    **실제로 어댑터가 선 하네스만** 올린다 — 적용 단계에서 그 하네스 파일이 전부 제외되면
    #    기록이 유령을 진실로 만든다(기록이 관측을 덮어 스스로 교정되지도 않는다).
    recordable, unestablished = established_harnesses(
        dest_root, notation_harnesses, preexisting_harnesses)
    if unestablished:
        print(f"  ⚠️ {', '.join(unestablished)} 어댑터가 이번 실행에서 서지 않아(복사 제외) 설치 "
              f"기록에 올리지 않습니다 — 위 제외 사유를 해소한 뒤 다시 실행하세요.", file=sys.stderr)
    template_coordinates = _copied_instance_owned_template_coordinates(
        actions, dest_root, source_root, copied_relpaths, args.weight)
    if record_install_receipt(
            dest_root, recordable, root_identity=root_identity,
            template_coordinates=template_coordinates):
        print(f"✓ 설치 기록 갱신 ({INSTALL_RECEIPT_RELPATH.as_posix()}): "
              f"{', '.join(recordable)}")

    # instance-owned config 원장 — add-harness 와 같은 기록 시점·같은 규칙(레이다운/일치 확인분만).
    #   이게 있어야 다음 pm_update 가 "채택자가 손댔는가" 를 판정해 상류 동작 fix 를 안전히 나른다.
    baselined = record_adapter_baseline(
        dest_root, source_root, recordable, root_identity=root_identity)
    if baselined:
        print(f"✓ 어댑터 config 원장 기록 ({ADAPTER_BASELINE_RELPATH.as_posix()}): "
              f"{', '.join(baselined)}")

    # ── fill 단계: board init·conf sync 직후 hook. 자유서술 placeholder 처리.
    #    auto + opt-in 게이트 통과 → 하니스 구동 *제안*(파일 미변경, 사람 검토 전제).
    #    그 외(manual 또는 게이트 미통과) → TODO 표시(채택자 손작업 지점 명시).
    #    MF(비파괴): fill 스캔 범위 = substitute_placeholders 와 동일한 copied_relpaths —
    #    이번 import 가 복사한 파일만. --into 에서 복사 안 한 사용자 파일은 절대 스캔/수정 안 함.
    if args.fill == "auto" and live_allowed:
        # 실 하니스는 subprocess 라 우리 fd 를 못 물려준다 — 직전에 루트를 재확인해 창을 좁힌다.
        assert_dest_root_unchanged(dest_root, root_identity)
        fill_result = run_fill(dest_root, fill_harness, live=True,
                               copied_relpaths=copied_relpaths,
                               root_identity=root_identity)
        if not fill_result.values:
            # 하니스 미구동/실패 → manual 폴백(자유서술이 빈 채로 남지 않게 TODO 표시).
            print(f"  fill=auto 제안 없음({fill_result.note}) — manual 폴백.")
            fill_result = _run_manual_fill(dest_root, copied_relpaths,
                                           root_identity=root_identity)
    else:
        # --fill auto 라도 게이트 미통과면 실호출 차단 → manual 강제(안전·토큰 0).
        fill_result = _run_manual_fill(dest_root, copied_relpaths,
                                       root_identity=root_identity)
    _print_fill_result(fill_result, dry_run=False)

    # pm_playbook.local 스텁 생성: fill 과 같은 자리 — 인스턴스 소유
    # 누적 학습 칸. 루트 .local 은 manifest 밖이라 복사로 안 오니 여기서 생성한다. 재-import
    # 에서 기존 .local 은 비파괴 보존(누적 학습 손실 방지·local.conf 백업 철학과 같은 결).
    playbook_status = ensure_pm_playbook_local_stub(
        dest_root, backup_root, root_identity=root_identity)
    if playbook_status == "created":
        print(f"✓ pm_playbook.local.md 스텁 생성 ({PM_PLAYBOOK_LOCAL_RELPATH})")
    elif playbook_status == "preserved":
        print("  pm_playbook.local.md 기존 파일 비파괴 보존 (인스턴스 소유 — 덮지 않음).")

    # dest 가 git repo(git_safe is not None)이고 이번에 중앙 백업 디렉토리가
    #   실제로 만들어졌으면, .gitignore 가 `.pm_import_backups/` 를 무시하지 않을 때 1줄 append
    #   — 백업이 git status 를 오염시키지 않게 한다. 비-git/미생성/이미 무시 중이면 skip(멱등).
    if not is_new and git_safe is not None and backup_root is not None and backup_root.exists():
        gi_status = ensure_backup_dir_gitignored(dest_root, git_safe, copied_relpaths)
        if gi_status in ("added", "created"):
            print(f"✓ .gitignore 에 {BACKUP_DIR_NAME}/ 추가 (백업이 git status 오염 방지)")
        elif gi_status == "unsafe-skip":
            print(f"  ⚠️ .gitignore 가 미추적/변경 상태 — 비파괴 위해 자동 추가 생략. "
                  f"수동으로 `{BACKUP_DIR_NAME}/` 한 줄을 추가하세요.")

    print_conf_migration_notice(dest_root)
    print(f"✓ import 완료: {dest_root}")
    print("  다음: 자유서술 placeholder 제안 검토·반영(--fill auto 했으면) + 첫 ticket 발행.")
    # claude 는 프로젝트 trust 수락 전 settings.json permissions.allow 를 조용히 무시한다.
    if "claude" in args.harness:
        _print_claude_trust_guidance()
    # codex 어댑터는 laydown 후 trusted project + hook trust 2단계 승인이 있어야 발화.
    if "codex" in args.harness:
        _print_codex_trust_guidance()
    return 0


if __name__ == "__main__":
    sys.exit(main())
