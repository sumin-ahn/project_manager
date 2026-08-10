#!/usr/bin/env python3
"""engine.manifest 기반 배포 sync — upstream 엔진 경로만 덮어쓴다.

엔진/상태 분리의 managed-manifest 배포. 인스턴스 상태(tickets·status·log·
decisions/*.md·areas.md…)와 per-clone 로컬(board.md·pm_state·local.conf·.local)은
manifest 밖이라 절대 건드리지 않으므로, upstream 갱신이 인스턴스와 *구조적으로*
충돌하지 않는다 (수동 MERGE 백포트의 기계화).

사용:
    # 인스턴스/타깃 내부에서 실행 (self-location):
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> [--dry-run]
    # --from 생략 시 dest local.conf 의 upstream= 을 기본으로 쓴다(pm_import 가 자동 기록):
    python3 .project_manager/tools/pm_update.py [--dry-run]

    # 루트(upstream)에서 특정 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --target <name> [--dry-run]
    # 예: --target opencode  →  templates/opencode/ 에 동기화

    # 루트(upstream)에서 존재하는 모든 templates 타깃으로 동기화:
    python3 .project_manager/tools/pm_update.py --from <upstream-checkout> --all-targets [--dry-run]

    # 명시 경로만 전파 (opt-in 부분 전파 · 단독/--target/--all-targets 와 조합):
    python3 .project_manager/tools/pm_update.py --all-targets --paths <path> [<path>...]

    # 받은 baseline ↔ upstream HEAD 변경점만 read-only 확인 (실 sync 안 함):
    python3 .project_manager/tools/pm_update.py --changes [--from <checkout>] [--count-only] [--log]

동작:
  engine.manifest 의 각 경로를 <upstream>/<path> → <dest-root>/<path> 로 복사(overwrite).
  디렉토리는 재귀. manifest 에 없는 경로는 무시. --dry-run = 변경 예정만 출력(미적용).
  --paths 지정 시 그 경로(파일 또는 디렉토리) 아래만 전파한다 — manifest 등재분에 한정하고
  미등재 경로는 rc1 로 거부한다. 부분 전파이므로 upstream_rev baseline 기록·진입 doc
  마이그레이션·보호 훅 재설치·동기 후 프롬프트는 발화하지 않는다(전량 흡수로 오인 방지).
  --target 지정 시 dest-root = REPO/templates/<target>/ (타깃 자신의 manifest 우선).
  sync 적용 후에는 등록 repo 전수 **보호 훅 재설치**— 훅은 엔진 코드에서 생성되는
  런타임 산출물이라 파일 복사만으론 새 훅이 배포되지 않는다(--target 은 비발화).

결정:
  - merge 아니라 overwrite (엔진은 upstream 단일 진실). 커스터마이즈 가능 문서는 manifest 에서
    제외 — 채택자 customization 은 local.conf(operational)·canonical home(free-form FILL)이 보존.
  - 어떤 경로를 엔진으로 볼지는 *dest-root 의* engine.manifest 가 정한다(없으면 source 의 것).
  - stdlib 만. plan/apply 분리로 테스트 결정론.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import filecmp
import inspect
import os
import re
import shutil
import stat
import sys
import tempfile
import warnings
import zlib
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import TYPE_CHECKING

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
                    added = not sys.path or sys.path[0] != parent
                    if added:
                        sys.path.insert(0, parent)
                    try:
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

if TYPE_CHECKING:
    from repo_owned_files import RepoOwnedEntry

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / ".project_manager" / "engine.manifest"

# 추가 리뷰어(additional reviewer) 첫 opt-in 이 원자적으로 심는 기본 프로필 —
#   board.ADDITIONAL_REVIEWER_DEFAULTS 와 **같은 값**이어야 한다(두 온보딩 진입·동일 프로필).
#   board 를 import 하지 않는 이유는 의존 방향(pm_update 는 stdlib-only 로 돈다)이고, 실행
#   해소를 하지 않는 이유는 무거운 external_review 코어를 업데이트 경로로 끌어오지 않기
#   위해서다 — 여기서는 값만 시드하고 드리프트는 테스트가 잡는다.
#   `reviewer_cmd` 는 신규 온보딩에서 만들지 않는다(레거시 채택자 전용 키).
#   게이트 키는 `additional_reviewer_enabled` 로 개칭됐다 — 신규 기록은 신키만 쓰고,
#   구키 `external_review_enabled` 는 "이미 결정됨" 판정에서만 1릴리즈 더 인정한다(+안내 1줄).
ADDITIONAL_REVIEWER_ENABLED_KEY = "additional_reviewer_enabled"
LEGACY_EXTERNAL_REVIEW_ENABLED_KEY = "external_review_enabled"

ADDITIONAL_REVIEWER_DEFAULTS: tuple[tuple[str, str], ...] = (
    (ADDITIONAL_REVIEWER_ENABLED_KEY, "true"),
    ("additional_reviewer.harness", "codex"),
    ("additional_reviewer.model", "gpt-5.6-sol"),
    ("additional_reviewer.reasoning", "max"),
)


def additional_reviewer_decision_key(conf: dict[str, str]) -> str | None:
    """이미 기록된 opt-in 결정을 공급하는 키 — 신키 우선·구키 1릴리즈 fallback (없으면 None).

    board·external_review 사본과 같은 판정·같은 순서다. 구키만 있는 채택자를 "미결정"으로 보면
    업데이트가 다시 물어 두 키가 공존하는 conf 를 만든다.
    """
    if ADDITIONAL_REVIEWER_ENABLED_KEY in conf:
        return ADDITIONAL_REVIEWER_ENABLED_KEY
    if LEGACY_EXTERNAL_REVIEW_ENABLED_KEY in conf:
        return LEGACY_EXTERNAL_REVIEW_ENABLED_KEY
    return None


# 구키 deprecation 안내 1줄 — external_review 사본과 **같은 문구**(드리프트는 회귀가 잡는다).
LEGACY_ENABLED_KEY_DEPRECATION = (
    f"⚠ local.conf `{LEGACY_EXTERNAL_REVIEW_ENABLED_KEY}` 는 구키다 — "
    f"`{ADDITIONAL_REVIEWER_ENABLED_KEY}` 로 바꾸세요(다음 릴리즈에서 구키 제거)."
)

ADDITIONAL_REVIEWER_OPTIN_BLOCK = (
    "# 추가 리뷰어(additional reviewer) — ON.\n"
    "# additional_reviewer_enabled=true 는 설정된 외부 전송과 통상 과금에 대한 지속 동의다\n"
    "# (리뷰마다·라운드 상한마다 비용을 다시 묻지 않는다). 프로필은 아래 3키로 교체한다.\n"
    + "".join(f"{key}={value}\n" for key, value in ADDITIONAL_REVIEWER_DEFAULTS)
)

# 이미 **유효한 대상**이 있는 conf 의 "예" 가 쓰는 블록 — 활성 플래그만 심고 대상은 손대지 않는다
#   (board.ADDITIONAL_REVIEWER_ENABLE_ONLY_BLOCK 과 같은 값). 기본 4키를 그냥 덧붙이면 구조화
#   튜플은 last-wins 로 갈아치워지고, 레거시 `reviewer_cmd` 와는 엔진이 거부하는 이중 대상이 된다.
ADDITIONAL_REVIEWER_ENABLE_ONLY_BLOCK = (
    "# 추가 리뷰어(additional reviewer) — ON (이미 설정된 대상 그대로).\n"
    "# additional_reviewer_enabled=true 는 설정된 외부 전송과 통상 과금에 대한 지속 동의다\n"
    "# (리뷰마다·라운드 상한마다 비용을 다시 묻지 않는다).\n"
    "additional_reviewer_enabled=true\n"
)

ADDITIONAL_REVIEWER_ENABLE_HINT = (
    "local.conf 에 additional_reviewer_enabled=true + "
    "additional_reviewer.harness/model/reasoning"
)

# **이미 대상이 있는** conf 의 안내 — 활성 플래그 한 줄만 말한다(board 사본과 같은 값·같은 이유).
#   기본 문장을 그대로 쓰면 구조화 3키를 *더* 적으라는 말이 돼, 레거시 `reviewer_cmd` 위에서는
#   엔진이 거부하는 이중 대상이 되고 구조화 튜플 위에서는 last-wins 로 자기 선언이 덮인다.
ADDITIONAL_REVIEWER_ENABLE_ONLY_HINT = "local.conf 에 additional_reviewer_enabled=true"

# 거절이 기록하는 블록 — 결정 자체는 대상과 무관하므로 한 벌뿐이다(board 사본과 같은 값).
ADDITIONAL_REVIEWER_DECLINE_BLOCK = (
    "# 추가 리뷰어 — 기본 OFF. 켜려면 true 로.\n"
    "additional_reviewer_enabled=false\n"
)

# opt-in 커밋 결과 — 락 안에서 **다시 판정한** 사실이다(질문 시점의 판정이 아니다·board 동형).
OPTIN_COMMIT_ALREADY = "already"          # 질문하는 사이 활성 키가 생김 → byte 보존 no-op
OPTIN_COMMIT_BROKEN = "broken"            # 질문하는 사이 대상이 깨짐 → loud no-write
OPTIN_COMMIT_DEFAULTS = "defaults"        # 대상 없음 + 수락 → 기본 4키
OPTIN_COMMIT_ENABLE_ONLY = "enable_only"  # 대상 있음 + 수락 → 활성 플래그 한 줄
OPTIN_COMMIT_DECLINED = "declined"        # 거절 → false 실키

# ── 기존 대상 판정 (온보딩 전용 · 실행 해소 없음 · board 와 같은 규칙) ────────
# 실행 해소(하네스→실 명령·값 검증)는 external_review 코어가 소유하고, 온보딩이 알아야 하는 것은
# "덧쓰면 안 되는 선언이 이미 있는가" 하나다. board 를 import 하지 않는 이유는 의존 방향
# (pm_update 는 stdlib-only), external_review 를 import 하지 않는 이유는 무거운 실행 코어를
# 업데이트 경로로 끌어오지 않기 위해서다. 세 사본의 키/판정 일치는 테스트가 잡는다.
ADDITIONAL_REVIEWER_PREFIX = "additional_reviewer"
ADDITIONAL_REVIEWER_HARNESS_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.harness"
ADDITIONAL_REVIEWER_MODEL_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.model"
ADDITIONAL_REVIEWER_REASONING_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.reasoning"
ADDITIONAL_REVIEWER_KEYS: tuple[str, ...] = (
    ADDITIONAL_REVIEWER_HARNESS_KEY,
    ADDITIONAL_REVIEWER_MODEL_KEY,
    ADDITIONAL_REVIEWER_REASONING_KEY,
)
LEGACY_REVIEWER_CMD_KEY = "reviewer_cmd"

# 판정 결과 — 대상 없음 / 레거시 자유 커맨드 / 구조화 튜플.
REVIEWER_TARGET_NONE = "none"
REVIEWER_TARGET_LEGACY = "legacy"
REVIEWER_TARGET_STRUCTURED = "structured"


class AdditionalReviewerTargetError(RuntimeError):
    """기존 대상이 그 자체로 깨져 있어 온보딩이 결정을 쓸 수 없는 형상(부분 튜플·이중 대상)."""


def classify_additional_reviewer_target(conf: dict[str, str]) -> str:
    """활성 플래그만 없는 conf 에 **이미 어떤 대상이 있는가**를 판정한다
    (board.classify_additional_reviewer_target 와 같은 계약·같은 판정).

    · 구조화 키가 하나도 없고 비어있지 않은 `reviewer_cmd` 도 없으면 `none`(대상 없음).
    · 구조화 키 없이 비어있지 않은 `reviewer_cmd` 만 있으면 `legacy`.
    · 구조화 키가 하나라도 **있으면**(값이 비어 있어도 선언이다) harness/model 동반 필수이고,
      그 둘이 온전하면 `structured`. 판정 기준을 값의 truthiness 로 하면 비운 채 선언한 부분
      튜플이 '대상 없음'으로 떨어져, 온보딩이 기본 4키를 덧써 사용자의 선언을 갈아치운다.
    · 부분 튜플·구조화+레거시 이중 대상은 `AdditionalReviewerTargetError` 다 — 어느 쪽이 이기는지
      추측해 쓰지 않는다(external_review 의 해소 규칙과 같은 판정·같은 이유).
    """
    present = tuple(key for key in ADDITIONAL_REVIEWER_KEYS if key in conf)
    legacy_cmd = (conf.get(LEGACY_REVIEWER_CMD_KEY) or "").strip()
    if not present:
        return REVIEWER_TARGET_LEGACY if legacy_cmd else REVIEWER_TARGET_NONE
    if legacy_cmd:
        raise AdditionalReviewerTargetError(
            f"대상이 둘입니다 — 구조화 프로필({', '.join(present)})과 legacy "
            f"`{LEGACY_REVIEWER_CMD_KEY}={legacy_cmd}` 가 같은 local.conf 에 있습니다. "
            f"하나만 남기세요(권장: `{LEGACY_REVIEWER_CMD_KEY}` 를 지우고 "
            f"{ADDITIONAL_REVIEWER_PREFIX}.* 유지)."
        )
    missing = ", ".join(
        key for key in (ADDITIONAL_REVIEWER_HARNESS_KEY, ADDITIONAL_REVIEWER_MODEL_KEY)
        if not (conf.get(key) or "").strip()
    )
    if missing:
        raise AdditionalReviewerTargetError(
            f"구조화 프로필이 불완전합니다({missing} 부재/빈 값) — harness/model 은 동반 필수인 "
            f"원자 tuple 입니다. 두 키를 함께 채우거나 {ADDITIONAL_REVIEWER_PREFIX}.* 줄을 "
            "지우세요."
        )
    return REVIEWER_TARGET_STRUCTURED


def _is_engine_rev_skew(exc) -> bool:
    """stamped sibling 로더가 표시한 사본 불일치인가."""
    return getattr(exc, "_engine_rev_skew", False)


# ── 동기 실행 중 사본 rev 혼합 = 정상 과도 상태 ────────────────────────────
# `apply` 는 per-file 순차 write 라(원자 교체는 파일 단위·트리 단위가 아니다) 실행 중 목적지
# 트리에는 구/신 `ENGINE_REV` 가 공존한다. 그 사이에 일어나는 **중첩 로드**(pm_update 가 형제
# pm_import·목적지 pm_config 를 불러오고, 그 형제가 다시 자기 형제를 verifier 로 로드하는 계층)는
# 이 혼합을 marked skew 로 올린다. 동기 채널은 skew 를 *고치는* 유일한 복구 경로이므로 자기
# 실행 중 그걸 사유로 죽으면 채택자는 복구 수단을 잃는다(engine_rev.EXEMPT_FROM_STAMP 의
# `pm_update.py` 면제와 같은 논거 · v1.7.0 흡수 실행에서 실측 1회).
#
# **불변식**: 동기 실행 경로의 어떤 중첩 로드도 rev 혼합을 사유로 동기를 중단하지 않는다. 대신
# 각 경계는 자기 fail-soft 로 내려가고(사유는 아래 장부에 등재), 실행 **종료 시** 한 번
# 수렴을 검증한다(`_verify_engine_rev_convergence`) — 흡수가 침묵으로 전락하지 않게 하는 짝이다.
#
# **범위**: 여기서 흡수하는 것은 pm_update 안의 경계뿐이다. 동기 실행 밖(board.py 등 일반 CLI 의
# 형제 로드)에서 skew 는 여전히 실결함 신호라 fail-loud 를 유지한다.
#
# 이번 실행이 흡수한 경계 목록 — 종료 시 수렴 검증이 소비한다(실행마다 초기화).
_ABSORBED_ENGINE_REV_SKEW: list[str] = []
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "reinstall_protected_hooks": (
        "동기화가 끝난 뒤의 훅 재설치가 부분 동기 목적지 엔진 때문에 실패해도 "
        "다음 pm-update 재실행 경로를 열어 둔다"
    ),
    "resolve_hook_set_generation": (
        "훅 세트 세대 선언은 상류/형제 pm_import 사본에서 읽는다 — 그 사본의 rev 가 실행 중 "
        "엔진과 갈리는 것이 업그레이드의 정상 경로이므로, 여기서 skew 를 올리면 엔진이 반쯤 적용된 "
        "채 죽는다. 해소 실패는 '미해소' 로 내려가 소비자별 정책(조회=loud 폴백·게이트=fail-closed)이 "
        "판정한다(사본 불일치 자체는 동기 본류의 skew 표면이 진단한다)"
    ),
    "resolve_hook_set_predicate": (
        "원자 write 판정자는 선언 해소가 실패한 뒤에도 **구세대 형제가 아는 만큼**(설치본 선언 "
        "기준 훅 경로 판정)을 살리려 그 사본의 판정 API 를 조회한다 — 손상/혼합 사본은 로드가 "
        "아니라 속성 접근에서 발화하므로, 그 조회가 혼합 트리에서 터지면 판정자만 무판정으로 "
        "내려가고(훅 파일이 일반 복사로 떨어지는 사실은 loud) 동기 자체는 완주한다"
    ),
    "installed_entry_notation_manifests": (
        "설치 하네스 판별은 형제 pm_import 가 상류 출하물을 열거하려 다시 형제 repo_owned_files 를 "
        "verifier 로 로드하는 계층이라 **계획 수립 전** 혼합 트리에서 바로 발화한다"
        "(v1.7.0 흡수 실측 지점) — 표기 context 는 core manifest 만으로도 성립하므로 guest 보탬을 "
        "포기하고 동기를 계속한다"
    ),
    "sync_adapter_configs.judge": (
        "어댑터 config 판정 채널이 형제 pm_import 를 통해 revved 형제까지 들어간다 — 판정 불가는 "
        "`unavailable` 로 내려가 완료 게이트가 rc1 로 재실행을 요구하고(엔진 적용은 보존), "
        "여기서 올리면 이미 착지한 엔진 파일 위에서 동기가 traceback 으로 죽는다"
    ),
    "sync_adapter_configs.accept": (
        "한 파일 수용은 백업·원자 교체·원장 기록이라 형제 락 seam 까지 들어간다 — 한 파일의 "
        "사본 불일치가 나머지 파일과 이미 끝난 엔진 동기를 되돌리지 않게 보존으로 내린다"
    ),
    "sync_adapter_configs.record_baseline": (
        "원장 기록 실패는 파일 적용을 되돌리지 않는다 — `degraded` 로 내려가 쓰기 후 재판정이 "
        "같은 red 를 올리고, 다음 실행이 원장을 수렴시킨다"
    ),
    "check_adapter_hook_sets": (
        "훅 세트 세대 검사는 형제 pm_import 판정을 빌려 쓰는 **가드**다 — 판정 채널이 혼합 트리 "
        "때문에 안 열리면 검사를 unavailable 로 접고(경고는 loud) 동기 자체는 완주시킨다"
    ),
    "refuse_partial_hook_set_scope": (
        "경로 스코프 반쪽 갱신 가드도 같은 판정 채널을 쓴다 — 복구 전파(부분 동기 트리에서 "
        "돌리는 pm-update)가 자기 가드의 로드 실패로 자기잠금하면 안 된다"
    ),
    "record_upstream_revs.write": (
        "baseline 기록은 형제 pm_import 의 conf writer(다시 형제 file_lock 을 verifier 로 로드)를 "
        "재사용한다 — best-effort 단계라 혼합 트리에서는 기록을 건너뛰고(다음 실행이 기록) 이미 "
        "적용된 엔진 파일을 traceback 으로 덮지 않는다"
    ),
}


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """동기 실행 중 경계가 marked skew를 의도적으로 흡수했음을 표시한다 (True=흡수).

    실행 중 목적지 트리의 rev 혼합은 정상 과도 상태이므로(위 불변식), 등록된 경계는 이 판정으로
    자기 fail-soft 경로에 내려간다. 호출부는 반환값으로 일반 실패와 사본 불일치를 구분해 loud
    진단하되 update 성공을 되돌리지 않는다. 사유가 등재되지 않은 경계는 fail-loud(ValueError) —
    흡수는 장부에 남은 판단이어야 감사 가능하다.
    """
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    absorbed = _is_engine_rev_skew(exc)
    if absorbed:
        _ABSORBED_ENGINE_REV_SKEW.append(boundary)
    return absorbed


# 종료 시 수렴을 검증할 실행 스코프 `(dest, source, write)`. `_main` 이 좌표를 해소하는 즉시
# 기록하고 `main` 의 종료 경로가 소비한다 — 중간 return(게이트 rc1·오류 rc2·예외)이 몇 개든
# 검증이 빠질 자리가 없게 종료 지점을 하나로 묶는다. 부분 전파(`--paths`)는 혼합이 *정상 결과*라
# 기록하지 않는다(그 경로의 rev 혼재 경고는 스코프 분기가 이미 낸다).
_SYNC_RUN_SCOPE: tuple[Path, Path, bool] | None = None
# 부분 전파(`--paths`) 실행의 **흡수 장부 보고** 대상. 수렴 검증은 하지 않는다(혼합이 정상 결과라
# 미수렴 보고가 거짓 경보가 된다) — 그렇다고 장부까지 버리면 계획-전 형제 로드
# (`_installed_entry_notation_manifests` 등)가 삼킨 skew 가 비엔진 경로만 지목한 rc0 실행에서
# 조용히 사라진다. 그래서 이 실행은 **report-only** 다: 흡수 사실만 남기고 rc 는 건드리지 않는다.
_PARTIAL_RUN_SCOPE: tuple[Path, Path] | None = None
# 실행당 수렴 판정 1회 — baseline 억제와 종료 rc 가 **같은 판정**을 쓰고 보고는 한 번만 나간다.
_ENGINE_REV_CONVERGENCE: bool | None = None
# 미수렴 종료 rc — 파일 적용 자체는 서지만 실행은 성공이 아니다(게이트 rc1 관례와 같은 값).
_UNCONVERGED_RC = 1
# baked 리터럴 스캐너 — engine_rev.read_literal 과 같은 패턴이지만 **형제를 로드하지 않는다**.
# 혼합 트리를 진단하는 쪽이 그 혼합 때문에 로드로 죽으면 검증 자체가 성립하지 않는다.
_ENGINE_REV_LITERAL_RE = re.compile(r'^ENGINE_REV = "([^"]*)"', re.MULTILINE)


def _load_engine_rev(tools_dir: Path):
    """`engine_rev.py`(스탬프 단일 진실)를 로드한다 — 활성 inventory·기대 rev 의 출처.

    복구 채널 면제 로드다(`allow_unverified=True`·engine_rev 는 `EXEMPT_FROM_STAMP`): 혼합 트리를
    *진단하는* 쪽이 그 혼합 때문에 로드로 죽으면 검증 자체가 성립하지 않는다. 부재·손상은 None
    (호출부가 판정 불가로 강등)."""
    path = Path(tools_dir) / "engine_rev.py"
    if not path.is_file():
        return None
    try:
        return _load_module_from_path(path, "engine_rev.py", allow_unverified=True)
    except Exception:  # noqa: BLE001 — 진단 보조 로드 실패는 판정 불가로 강등(동기는 계속).
        return None


def engine_rev_expectation(source_root: Path) -> tuple[tuple[str, ...], str | None]:
    """`(활성 stamped inventory, 상류 기대 rev)` — 수렴 판정의 두 입력.

    **inventory 는 `engine_rev.STAMPED_MODULES` 다**(디렉토리 glob 아님). glob 은 두 방향으로 틀린다:
    스탬프 리터럴이 없는 **구형 활성 모듈**을 판정 대상에서 빼 미해소 skew 를 침묵시키고, 동기가
    지우지 않는 **폐기 모듈**까지 세어 영구 오경고를 낸다. 활성 목록은 상류가 소유하므로 상류에서 읽고,
    못 읽으면 실행 중 엔진의 사본으로 물러난다(둘 다 없으면 판정 불가).

    기대 rev 는 **상류 값만** 인정한다 — 목적지 다수결은 중단 초기(구 rev 다수) 형상에서 방금
    착지한 새 파일을 straggler 로 오지목한다. 상류 미해소면 None 이고 보고가 rev별 그룹으로
    강등된다."""
    upstream = _load_engine_rev(Path(source_root) / ".project_manager" / "tools")
    inventory = tuple(getattr(upstream, "STAMPED_MODULES", ()) or ())
    if not inventory:
        running = _load_engine_rev(Path(__file__).resolve().parent)
        inventory = tuple(getattr(running, "STAMPED_MODULES", ()) or ())
    return inventory, getattr(upstream, "ENGINE_REV", None)


def baked_engine_revs(dest_root: Path, inventory) -> tuple[dict[str, str], list[str]]:
    """`(파일 → baked rev, 리터럴 없는 파일)` — **활성 inventory 안에 실재하는** 사본만 본다.

    리터럴 부재는 "스탬프 이전 세대의 활성 모듈" 이라 verifier 가 skew 로 판정하는 상태다 →
    별도 버킷으로 올려 미수렴으로 센다. 부재 *파일*은 여기 관심사가 아니다(부분 전파·manifest
    누락은 다른 축이 진단한다)."""
    revs: dict[str, str] = {}
    unstamped: list[str] = []
    tools = Path(dest_root) / ".project_manager" / "tools"
    for name in inventory:
        path = tools / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue                   # 부재·읽기 실패 — 다른 축 소관.
        match = _ENGINE_REV_LITERAL_RE.search(text)
        if match:
            revs[name] = match.group(1)
        else:
            unstamped.append(name)
    return revs, sorted(unstamped)


def _engine_rev_divergence_report(revs: dict[str, str], unstamped: list[str],
                                  expected: str | None) -> str:
    """미수렴 내역 1줄 — 기대 rev 를 알면 어긋난 사본을, 모르면 rev별 그룹을 지목한다."""
    if expected is not None:
        off = sorted(f"{name}({rev})" for name, rev in revs.items() if rev != expected)
        off += [f"{name}(스탬프 없음)" for name in unstamped]
        return f"상류 기대 rev {expected} 와 어긋난 사본 {len(off)}건: {', '.join(off)}"
    groups = []
    for rev in sorted(set(revs.values())):
        names = sorted(name for name, value in revs.items() if value == rev)
        groups.append(f"{rev}: {', '.join(names)}")
    if unstamped:
        groups.append(f"스탬프 없음: {', '.join(unstamped)}")
    return "상류 기대 rev 미해소 — rev별 사본 " + " / ".join(groups)


def _verify_engine_rev_convergence(dest_root: Path, source_root: Path) -> bool:
    """동기 종료 시 목적지 엔진 사본이 상류 rev 로 수렴했는지 본다 (True=수렴·실행당 1회 판정).

    실행 중 흡수의 **짝**이다. 흡수가 옳은 이유는 "혼합은 실행 중의 과도 상태" 라는 전제인데,
    **끝난 뒤에도** 혼합이면 그 전제가 깨진 것(미해소 skew)이므로 침묵하면 안 된다. 수렴이면 완전히
    조용하고(steady-state 무출력), 아니면 stderr 한 줄 + 처방을 낸다.

    미수렴은 baseline 억제(`converge_upstream_revs`)와 비영 rc 로 이어진다 — 혼합 `--from` 트리를
    그대로 복사한 실행을 성공으로 보고하면 baseline 이 "여기까지 흡수함" 으로 박혀 drift-lint 가
    침묵하고, 소스 자체가 혼합이면 재실행도 영영 못 고치는 침묵 루프가 된다. 판정 불가(활성
    inventory 를 어디서도 못 읽음)는 미수렴이 아니라 **무판정**이다(엔진 트리가 아닌 형상)."""
    global _ENGINE_REV_CONVERGENCE
    if _ENGINE_REV_CONVERGENCE is not None:
        return _ENGINE_REV_CONVERGENCE     # 같은 실행에서 두 번 보고하지 않는다.
    inventory, expected = engine_rev_expectation(source_root)
    revs, unstamped = baked_engine_revs(dest_root, inventory)
    if not inventory or (not revs and not unstamped):
        # 활성 모듈 사본이 **하나도 없는** 트리는 판정 대상이 아니다(스캐폴드·부분 픽스처) —
        #   미수렴이 아니라 무판정이다. 사본이 있는데 전부 리터럴 없는 구형이면 그건 무판정이
        #   아니라 **미수렴**이다(verifier 가 곧 skew 로 판정할 상태) — 아래로 내려간다.
        _ENGINE_REV_CONVERGENCE = True
        return True
    observed = set(revs.values())
    converged = not unstamped and (
        observed == {expected} if expected is not None else len(observed) <= 1)
    _ENGINE_REV_CONVERGENCE = converged
    if converged:
        return True
    absorbed = len(_ABSORBED_ENGINE_REV_SKEW)
    print(
        "[경고] 동기 종료 시 엔진 사본 rev 가 수렴하지 않았다 — "
        + _engine_rev_divergence_report(revs, unstamped, expected) + "."
        + (f" 이번 실행이 중첩 로드 skew {absorbed}건을 흡수했으나 아직 해소되지 않았다."
           if absorbed else "")
        + "\n  → pm-update 를 한 번 더 돌려라. 그래도 남으면 `--from` 트리 자신이 혼합이다 "
        "(그 checkout 을 먼저 수렴시켜라 — 여기서 재실행해도 같은 혼합이 복사된다).",
        file=sys.stderr,
    )
    return False


def _report_partial_run_absorption() -> None:
    """부분 전파(`--paths`) 실행이 흡수한 skew 를 종료 시 보고한다 (report-only·rc 불변).

    `--paths` 는 수렴 검증 대상이 아니다 — 요청 경로만 옮기므로 혼합이 *정상 결과*이고, 거기에
    미수렴 rc 를 세우면 정당한 부분 전파가 전부 실패로 보인다. 그렇다고 흡수 장부를 그대로 버리면
    관측이 사라진다: 계획-전 형제 로드(`_installed_entry_notation_manifests` 등)는 스코프와 무관하게
    돌아 marked skew 를 흡수하는데, 비엔진 경로만 지목한 실행은 그 뒤 훅/어댑터 채널을 전부 건너뛰고
    rc0 으로 끝나므로 흡수 사실을 알릴 자리가 어디에도 없다. 사실만 남기고 rc 는 호출부가 정한
    값 그대로 둔다."""
    if not _ABSORBED_ENGINE_REV_SKEW:
        return
    boundaries = ", ".join(sorted(set(_ABSORBED_ENGINE_REV_SKEW)))
    print(
        f"[알림] 경로 스코프 실행이 엔진 사본 rev 혼합 skew {len(_ABSORBED_ENGINE_REV_SKEW)}건을 "
        f"흡수했다({boundaries}) — 부분 전파는 혼합이 정상 결과라 수렴을 검증하지 않으므로 이 실행의 "
        "rc 는 그대로다.\n  → 스코프 없이 pm-update 를 한 번 돌려 사본을 수렴시켜라(그 실행이 "
        "수렴을 검증한다).",
        file=sys.stderr,
    )

# manifest 의 render 태그 () — path 행 끝 `  @render` 면 byte-copy 대신 render_adapter.
RENDER_TAG = "@render"
# manifest 의 target-owned 태그 — path 행 끝 `  @target-owned` 면 그 경로는 타깃 자신만
# 보유하는 어댑터다(엔진 upstream/루트에 source 부재가 정상). source-부재 skip 의 *명시* 판별자.
# `@render` 와 독립 — `.claude/agents @render`(루트 upstream 에 존재해야 하는 엔진 리소스)는
# render=True 이지만 target_owned=False 라, 잘못된 --from 에서 빠지면 skip 이 아니라 rc2 가 된다.
TARGET_OWNED_TAG = "@target-owned"
# manifest 의 source-remap 태그 — path 행 끝 `  @source=<relpath>` 면 그 경로는
# source_root 아래 canonical 소스(`<source_root>/<relpath>`)에서 읽되 dest 에는 manifest 경로로
# 기록한다(_remap_to_dest). opencode 어댑터(`.opencode/*`)가 프레임워크 루트의
# `templates/opencode/.opencode/*` 에 살지만 채택자 dest 엔 `.opencode/*` 로 전파돼야 하는 비대칭을
# 잇는다(framework-owned·claude `.claude/*` 대칭). `@target-owned`(source-부재 정상·skip)와 대비:
# @source 는 source 가 *실재*(templates/ 아래)하므로 부재면 rc2(엔진/템플릿 누락 은폐 금지).
SOURCE_TAG_PREFIX = "@source="
# read_manifest 가 path 행 끝에서 떼어낼 수 있는 boolean 마커들(복수·순서 무관). `@source=<path>` 는
# 값 운반 마커라 prefix 검사로 별도 처리(이 튜플 밖).
_MANIFEST_MARKERS = (RENDER_TAG, TARGET_OWNED_TAG)
_CENTRAL_LOADER_REL = ".project_manager/tools/repo_owned_files.py"


class ManifestEntry(str):
    """manifest 한 경로 — `str` 서브클래스라 기존 `in`/`.startswith`/`==` 가 그대로 동작한다.

    추가 속성:
    - `render`(bool): path 행 끝에 `@render` 태그가 있으면 True(byte-copy 대신 render_adapter
      로 채운다). 미주석=False → 오늘과 정확히 동일(순수 copy2·후방호환).
    - `target_owned`(bool): path 행 끝에 `@target-owned` 태그가 있으면 True — 타깃 자신만 보유
      하는 어댑터라 엔진 upstream 에 source-부재가 정상(전파 대상 아님). source-부재 skip 의
      명시 판별자. `@render` 와 독립이며, 두 마커는 한 행에 같이 올 수 있다(순서 무관).
    - `source_rel`(str|None): path 행 끝에 `@source=<relpath>` 태그가 있으면 그 canonical 소스
      상대경로— source_root 아래 그 경로에서 읽되 dest 엔 manifest 경로(=`str(self)`)
      로 기록한다(_source_root_rel·_remap_to_dest). 미주석=None → source 읽기 경로 = manifest 경로
      (오늘 동작·후방호환). @render 와 공존 가능(토큰-form 소스 읽어 렌더).

    str 을 상속함으로써 read_manifest 의 반환이 path+플래그 의미를 가지면서도 `entry in entries`·
    `e.startswith(...)` 같은 기존 호출부/테스트를 한 줄도 깨지 않는다.
    """

    render: bool
    target_owned: bool
    source_rel: str | None

    def __new__(
        cls,
        path: str,
        render: bool = False,
        target_owned: bool = False,
        source_rel: str | None = None,
    ) -> "ManifestEntry":
        obj = super().__new__(cls, path)
        obj.render = render
        obj.target_owned = target_owned
        obj.source_rel = source_rel
        return obj


class _RenderDst:
    """change tuple 의 dst — 내부 Path 에 위임하되 `.render` 플래그를 운반하는 thin 래퍼.

    plan 이 dst 에 render 여부를 실어 apply 가 byte-copy vs render 를 분기하게 한다. change
    tuple 을 4-요소로 유지(`(rel, src, dst, kind)`)해 기존 unpack 호출부/테스트를 깨지 않으면서
    render 정보를 운반한다. Path 직접 서브클래싱(버전별 `_flavour` 함정·하위 호환 약화)을 피하고
    `__fspath__`/`__eq__`/`__getattr__` 위임으로 테스트가 쓰는 표면(`dst.exists()`·`dst.parent`·
    `dst == Path(...)`·`str(dst)`·`Path(dst)`)을 모두 지원한다. 평문 Path dst(레거시 apply
    직접 호출)는 이 래퍼가 아니므로 `getattr(dst, "render", False)` 가 False → copy2(후방호환).
    """

    __slots__ = ("_path", "render", "entry_notation_template", "guest_backfill_lines")

    def __init__(
        self,
        path: Path,
        render: bool = False,
        entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
        guest_backfill_lines: list[str] | None = None,
    ) -> None:
        self._path = Path(path)
        self.render = render
        self.entry_notation_template = entry_notation_template
        # engine.manifest self-prop 전용 — apply 의 guest 절 재부착에 함께 기록할 파생 엔진 행.
        #   change tuple 에 실어 나르므로 `apply` 시그니처가 바뀌지 않는다(`entry_notation_template`
        #   과 같은 운반 방식).
        self.guest_backfill_lines = guest_backfill_lines

    def __fspath__(self) -> str:
        return str(self._path)

    def __getattr__(self, name):
        # _path 의 메서드/속성(exists·parent·read_text 등)으로 위임. __slots__ 정의 속성은
        # 이 메서드 진입 전 처리되므로 무한재귀 없음.
        return getattr(self._path, name)

    def __eq__(self, other) -> bool:
        if isinstance(other, _RenderDst):
            return self._path == other._path
        return self._path == other

    def __hash__(self) -> int:
        return hash(self._path)

    def __str__(self) -> str:
        return str(self._path)

    def __repr__(self) -> str:
        return (
            f"_RenderDst({self._path!r}, render={self.render}, "
            f"entry_notation_template={self.entry_notation_template!r}, "
            f"guest_backfill_lines={self.guest_backfill_lines!r})"
        )


class _ManifestTextSource:
    """선택된 flavor manifest 합집합 텍스트를 change tuple에 싣는 인메모리 source.

    합집합은 upstream checkout 안의 단일 파일로 존재하지 않는다. 임시 파일을 만들지 않고도
    plan/apply 4-tuple 계약을 유지하도록 ``read_text``만 제공한다. engine.manifest 전용이며
    apply의 self-prop 분기가 이 객체를 직접 소비한다.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.text


def _templates_dir() -> Path:
    """REPO/templates/ 경로. 없어도 안전하게 반환 (존재 여부는 호출부가 판단)."""
    return REPO / "templates"


def _is_noninteractive() -> bool:
    """`PM_NONINTERACTIVE` env 가 truthy 면 True — 비대화 결정 신호.

    Windows DEVNULL stdin 의 `isatty()` 가 신뢰불가한 cross-OS 함정을 회피. truthy 판정은
    `"1"`/`"true"`/`"yes"`/`"on"`(대소문자 무관) — board._is_noninteractive 와 동일 동작
    (stdlib-only·board 미import 결합 회피). 빈/`"0"`/`"false"` 등은 미설정 취급(isatty 폴백).
    """
    return os.environ.get("PM_NONINTERACTIVE", "").strip().lower() in (
        "1", "true", "yes", "on"
    )


def _additional_reviewer_enable_hint(target: str) -> str:
    """대상 유무에 맞는 "나중에 켜는 법" 1줄 (비대화형·거절 경로 공용·board 사본과 동형).

    대상이 없으면(`none`) 종전 문장 그대로 — 그 conf 는 활성 플래그와 대상을 **둘 다** 받아야
    한다. 대상이 이미 있으면 활성 플래그만 안내한다.
    """
    return (ADDITIONAL_REVIEWER_ENABLE_HINT if target == REVIEWER_TARGET_NONE
            else ADDITIONAL_REVIEWER_ENABLE_ONLY_HINT)


def _load_file_lock():
    """공용 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 로드 (`_load_pm_render` 패턴 동형).

    onboarding 응답의 conf write 를 배타락 + 단일 O_APPEND 로 닫는 데만 쓴다. board 처럼 import
    시점에 바인딩하지 않는 이유는 pm_update 가 **복구 채널**이기 때문이다 — 엔진 사본이 깨진
    채택자에게도 이 도구는 떠야 하고, 동기가 이미 끝난 뒤의 온보딩 질문이 형제 로드 실패로 죽으면
    자기 자신을 못 고친다. 그래서 로드 실패는 호출부가 fail-soft 로 받는다(락 없는 단일 추가로
    물러나되 재읽기·재판정은 유지 — 프로세스 간 배타성만 잃는다).
    """
    lock_py = Path(__file__).resolve().parent / "file_lock.py"
    return _load_module_from_path(
        lock_py, "file_lock.py", allow_unverified=True, cache=True,
        cache_key=f"_project_manager_file_lock:{lock_py}",
    )


def _local_conf_lock_path(conf_path: Path) -> Path:
    """local.conf writer 직렬화 락 경로 — 공용 seam 의 유도 규칙을 그대로 쓴다.

    같은 conf 를 건드리는 **모든** writer(board init 의 전체/병합 write·두 진입의 opt-in append·
    pm_import 의 키 writer)가 같은 파일에 도달해야 배타가 성립한다. 규칙을 도구마다 복제하면 한
    사본만 어긋나도 직렬화가 조용히 사라지므로 `file_lock.conf_lock_path` 한 곳이 소유한다.
    `conf_lock_path` 를 못 읽는 사본(형제를 아예 못 읽는 손상 사본·그 함수 이전의 구세대 사본)에서는
    같은 규칙의 인라인 폴백으로 계산한다. 손상 사본에서는 락 자체가 없어(아래
    `_local_conf_write_lock`) 이 경로가 진단용이지만, 구 `exclusive_file_lock` 만 있는 사본에서는
    **실제로 잡는 경로**다(`_conf_lock_section`) — 그래서 폴백이 공용 seam 과 글자 단위로 같아야 한다.
    """
    try:
        return _load_file_lock().conf_lock_path(conf_path)
    except Exception:  # noqa: BLE001 — 복구 채널은 형제 손상으로 죽지 않는다.
        return Path(conf_path).parent / ".local" / "local-conf.lock"


def _conf_lock_section(lock, conf_path: Path):
    """로드된 `file_lock` 사본의 API 형상에 맞는 conf 락 구간을 만든다 (부분 업그레이드 호환).

    새 `local_conf_write_lock` 이 있으면 그것을 쓴다. 없고 구 `exclusive_file_lock` 만 있는 사본
    (같은 rev 로 찍혔지만 새 seam 이전 파일 — `ENGINE_REV` 는 릴리스 단위라 같은 rev 안에서도 API
    형상이 갈릴 수 있고 이 로더는 rev 를 확인하지 않는다)에서는 **같은 락 파일**을 구 API 로
    잡는다: AttributeError 로 죽으면 복구 채널이 자기 자신을 못 고치고, 다른 파일을 잡으면 배타가
    조용히 사라진다. 둘 다 없으면 None = 종전 복구 계약의 무락 진행(프리미티브 *부재* 에만 허용).
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
    """conf 커밋 구간의 배타락 — 락 seam 을 못 읽으면 무락으로 진행한다(fail-soft).

    무락 폴백은 `file_lock` 자신의 규약과 같은 선택이다(프리미티브 *부재* 에만 허용). 여기서
    부재는 형제 모듈을 못 읽는 손상 사본이거나 락 프리미티브가 없는 구세대 사본
    (`_conf_lock_section`)이고, 그때도 재읽기→재판정→단일 추가라는 좁은 구간은 그대로 남는다.

    구간의 단위는 write 가 아니라 **"이 conf 를 읽고 판단하고 쓰는" 전체**다 — 현재 상태를 락
    밖에서 읽어 계획을 세우면 커밋 시점엔 이미 낡은 계획(stale plan)이라 그사이 들어온 결정을
    잘못된 분기로 덮거나 누락한다.
    """
    try:
        lock = _load_file_lock()
    except Exception:  # noqa: BLE001 — 복구 채널은 형제 손상으로 죽지 않는다.
        lock = None
    section = None if lock is None else _conf_lock_section(lock, conf_path)
    if section is None:
        yield lock
        return
    with section:
        yield lock


def _append_local_conf_atomic(conf_path: Path, block: str, lock) -> None:
    """conf 끝에 블록을 **한 번의 원자 추가**로 붙인다 (필요한 선행 개행 포함·board 동형).

    개행 보장과 블록 추가를 두 write 로 나누면 그 사이가 또 하나의 창이다 — 선행 개행을 같은
    문자열에 실어 O_APPEND 단일 write 로 붙인다. 끝 개행 판정은 **바이트**로 한다(디코딩 불가한
    conf 에서도 마지막 줄을 변질시키지 않게). `lock` 이 None(seam 부재 폴백)이면 같은 의미의
    stdlib append 로 물러난다.
    """
    try:
        raw = Path(conf_path).read_bytes()
    except OSError:
        raw = b""
    text = ("\n" if raw and not raw.endswith(b"\n") else "") + block
    if lock is not None:
        lock.append_atomic(conf_path, text)
        return
    with Path(conf_path).open("a", encoding="utf-8") as f:
        f.write(text)


def _commit_additional_reviewer_optin(
        local_conf: Path, accepted: bool) -> tuple[str, str]:
    """opt-in 응답을 대상 local.conf 에 확정한다 — 락 안에서 **다시 읽고 다시 판정**한다.

    board.`_commit_additional_reviewer_optin` 과 같은 계약이다. 질문은 사람이 답할 때까지 열려
    있고, 그동안 다른 행위자가 같은 conf 를 바꿀 수 있다 — 활성 키를 켜거나, 레거시
    `reviewer_cmd`/구조화 튜플을 새로 적거나, 부분 튜플로 깨뜨린다. 질문 **전** 판정으로 쓰면 그
    사이 생긴 대상 위에 기본 4키가 얹혀 이중 대상·last-wins 손상이 재현된다. 재읽기→재판정→append
    를 배타락 + 단일 O_APPEND write 로 닫아 그 사이에 새 창을 만들지 않는다.

    반환 `(결과, 상세)` — 결과는 `OPTIN_COMMIT_*`, 상세는 broken 이면 진단 사유, 그 밖에는 커밋
    시점의 대상 종류. 사용자 표면 문구는 호출부가 낸다(락 밖).
    """
    with _local_conf_write_lock(local_conf) as lock:
        conf = _read_local_conf(local_conf)
        if additional_reviewer_decision_key(conf) is not None:
            # 질문하는 사이 결정이 생겼다 — 그 결정이 이긴다(이 응답은 버린다·byte 보존).
            return OPTIN_COMMIT_ALREADY, ""
        try:
            target = classify_additional_reviewer_target(conf)
        except AdditionalReviewerTargetError as exc:
            # 질문 전에는 온전했던 대상이 그사이 깨졌다 — 어느 쪽이 이기는지 추측해 쓰지 않는다.
            return OPTIN_COMMIT_BROKEN, str(exc)
        if not accepted:
            block, outcome = ADDITIONAL_REVIEWER_DECLINE_BLOCK, OPTIN_COMMIT_DECLINED
        elif target == REVIEWER_TARGET_NONE:
            block, outcome = ADDITIONAL_REVIEWER_OPTIN_BLOCK, OPTIN_COMMIT_DEFAULTS
        else:
            # 이미 있는 대상은 한 글자도 건드리지 않는다 — 활성 플래그만 덧붙인다.
            block, outcome = (ADDITIONAL_REVIEWER_ENABLE_ONLY_BLOCK,
                              OPTIN_COMMIT_ENABLE_ONLY)
        _append_local_conf_atomic(local_conf, block, lock)
        return outcome, target


def maybe_prompt_external_review(dest_root: Path) -> None:
    """업데이트 후 추가 리뷰어(additional reviewer) opt-in — 아직 미설정이면 **1회** 묻는다.

    코드 diff 외부 *전송*이라 기본 OFF. `additional_reviewer_enabled`(또는 1릴리즈 더 인정하는
    구키 `external_review_enabled`) **실키**가 이미 있으면 (true/false 무관) 묻지 않고 기존
    프로필·레거시 `reviewer_cmd` 를 그대로 둔다(자동 마이그레이션 없음 — 구키는 안내 1줄로만
    처방한다). 비대화형은 안전쪽으로 건너뛰되 나중에 켜는 법을 1줄로 남긴다.
    board.prompt_external_review_optin 과 같은 계약이다 — "예" 는 기존 대상이 없을 때만
    ADDITIONAL_REVIEWER_DEFAULTS 4키를 원자 기록하고, 이미 유효한 대상(레거시 `reviewer_cmd`·
    구조화 튜플)이 있으면 **활성 플래그 한 줄만** 덧붙여 그 대상을 byte 그대로 둔다. 어느
    경로에서도 `reviewer_cmd` 는 만들지 않는다. 기존 대상이 깨져 있으면(부분 튜플·이중 대상)
    질문도 기록도 하지 않고 진단만 낸다.

    질문 전 판정은 **질문 문구**의 입력일 뿐이다. 기록의 입력은 `_commit_additional_reviewer_optin`
    이 커밋 시점에 배타락 안에서 다시 읽어 다시 판정한다 — 사람이 답하는 동안 conf 가 바뀌면 옛
    판정으로 쓴 기본 4키가 그사이 생긴 대상을 이중 대상/last-wins 로 망가뜨린다.

    dest_root: 동기화 대상 루트 (루트 또는 타깃). local.conf 는 이 경로 기준으로 읽고 쓴다.
    --target 모드에서 루트 local.conf 를 오염시키지 않기 위해 반드시 effective_dest 를 전달한다.
    """
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.exists():
        return  # init 전 — board.py init 에서 묻는다
    conf = _read_local_conf(local_conf)
    decision_key = additional_reviewer_decision_key(conf)
    if decision_key is not None:
        # 실키로 이미 결정됨(신키/구키·true/false 무관·기존 프로필/레거시 키 불변). 판정은 파싱된
        # 키 존재로 한다 — raw 텍스트 substring 으로 보면 주석(`# additional_reviewer_enabled=false`)
        # 이나 안내 문장이 결정을 가로채, 켜려던 채택자가 질문도 안내도 못 받는다.
        # maybe_prompt_delegate_optin·board.prompt_external_review_optin 과 같은 seam.
        if decision_key == LEGACY_EXTERNAL_REVIEW_ENABLED_KEY:
            # 업데이트는 채택자가 구키를 실제로 만나는 채널이다 — 여기서 처방 1줄을 준다.
            # 엔진이 대신 고쳐 쓰지 않는 이유는 위와 같다(채택자 conf 는 인스턴스 소유).
            print(f"[pm_update] {LEGACY_ENABLED_KEY_DEPRECATION}")
        return
    try:
        target = classify_additional_reviewer_target(conf)
    except AdditionalReviewerTargetError as exc:
        # 쓰기 **전에** 멈춘다 — 이 conf 는 어떤 답을 받아도 정직하게 기록할 수 없다(board 동형).
        print(f"[pm_update] ⚠ 추가 리뷰어 설정이 이미 깨져 있어 opt-in 을 묻지 않습니다: {exc} "
              "(local.conf 를 고친 뒤 다시 실행하세요 — 지금은 아무것도 기록하지 않았습니다.)")
        return
    # 명시적 비대화 신호 우선: Windows DEVNULL isatty() 신뢰불가 함정 회피.
    # PM_NONINTERACTIVE truthy 면 묻지 않고 안전쪽 skip. isatty 는 보조 폴백(env 없을 때).
    if _is_noninteractive() or not sys.stdin.isatty():
        print("[pm_update] 추가 리뷰어 OFF 유지(비대화형). 켜려면 "
              f"{_additional_reviewer_enable_hint(target)}")
        return
    print("\n[pm_update] 추가 리뷰어(additional reviewer)를 켤까요? 코드 diff 가 "
          "설정된 리뷰 하네스로 *전송*되고 그 하네스에 *과금*됩니다 — 내부 code-reviewer 와 상보적.")
    if target == REVIEWER_TARGET_NONE:
        print("  예 = 기본 프로필(codex · gpt-5.6-sol · reasoning max)을 한 번에 기록합니다 "
              "— 이후 리뷰마다 비용을 다시 묻지 않습니다.")
    else:
        print("  예 = local.conf 에 이미 설정된 대상을 그대로 쓰고 활성 플래그만 기록합니다 "
              "— 이후 리뷰마다 비용을 다시 묻지 않습니다.")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        # stdin EOF = 비대화/파이프 종료 신호이지 **거절이 아니다** — TTY 오판정으로 들어온
        # 질문이라 결정을 박제하면 안 된다(board.prompt_external_review_optin 과 같은 계약).
        # 아무것도 쓰지 않고 반환한다: 다음 실행이 제대로 된 표면에서 다시 묻는다.
        return
    # 질문 전 판정(target)은 **질문 문구**까지의 입력이다. 기록의 입력(대상 판정·개행 가드)은
    # 커밋 시점에 배타락 안에서 다시 읽는다 — 질문하는 동안 바뀐 실제 끝 상태가 기준이다.
    outcome, detail = _commit_additional_reviewer_optin(
        local_conf, answer in ("y", "yes"))
    if outcome == OPTIN_COMMIT_ALREADY:
        print("[pm_update] 질문하는 사이 local.conf 에 additional_reviewer_enabled 결정이 생겨 "
              "그 결정을 그대로 둡니다 — 이 응답은 기록하지 않았습니다.")
    elif outcome == OPTIN_COMMIT_BROKEN:
        print(f"[pm_update] ⚠ 추가 리뷰어 설정이 이미 깨져 있어 opt-in 을 기록하지 않습니다: "
              f"{detail} (local.conf 를 고친 뒤 다시 실행하세요 — 지금은 아무것도 기록하지 "
              "않았습니다.)")
    elif outcome == OPTIN_COMMIT_DEFAULTS:
        print("  ✓ 추가 리뷰어 ON (codex · gpt-5.6-sol · reasoning max — "
              "local.conf additional_reviewer.* 로 교체 가능)")
    elif outcome == OPTIN_COMMIT_ENABLE_ONLY:
        print(f"  ✓ 추가 리뷰어 ON (기존 {detail} 대상 유지 — local.conf 의 설정 그대로)")
    else:
        print("  → 추가 리뷰어 OFF (나중에 "
              f"{_additional_reviewer_enable_hint(detail)} 로 켤 수 있음).")


def maybe_prompt_delegate_optin(dest_root: Path) -> None:
    """동기 후 cross-harness 위임(pm_delegate) opt-in — 아직 실키 미결정이면 (
    maybe_prompt_external_review 동형).

    delegate_enabled **실키**(주석 예시가 아니라 `_read_local_conf` 가 파싱하는 활성 키)가 이미
    있으면 결정됨 → no-op. **TTY** 면 1회 질문 — y=true·그 외/무입력=false 실키를 대상 local.conf 에
    기록한다(질문 응답 기록이 pm_update 의 **유일한 conf write 예외**·그 외 설정 write 는 board.py
    init 단일 채널). **비-TTY(CI/스크립트)** 면 질문·write 없이 도입 advisory 1줄만 표면화(기본 OFF
    유지). conf 부재(init 전)면 무발화. effective_dest 기준(--target 루트 오염 방지)."""
    local_conf = dest_root / ".project_manager" / "local.conf"
    if not local_conf.exists():
        return  # init 전 — board.py init 이 시드/질문한다
    if "delegate_enabled" in _read_local_conf(local_conf):
        return  # 실키로 이미 결정됨(주석 예시는 _read_local_conf 파싱 제외 — 미결정 취급)
    if _is_noninteractive() or not sys.stdin.isatty():
        # 비-TTY — 질문·write 없이 도입 안내만(기본 OFF 유지·write 는 질문 응답 경로 한정).
        print("[pm_update] pm_delegate cross-harness 위임 채널이 도입됐습니다(기본 OFF) — "
              "`board.py init` 재실행으로 local.conf 에 `delegate_*` 주석 시드/opt-in 질문을 받거나 "
              "수동 참조하세요(켜면 프롬프트/코드가 외부 하네스로 전송·과금).")
        return
    print("\n[pm_update] cross-harness 위임(pm_delegate)을 켤까요? 켜면 위임 프롬프트/코드가 외부 "
          "하네스로 *전송*되고 그 하네스에 *과금*됩니다.")
    try:
        answer = input("  켜기 [y/N]: ").strip().lower()
    except EOFError:
        # stdin EOF(Ctrl-D) = 기본 거절 → false 실키를 **기록**(매번 재질문 방지·opt-in 결정 박제).
        answer = ""
    # 기록은 추가 리뷰어 opt-in 과 같은 규약이다 — 전 writer 공용 배타락 안에서 재읽기·재판정 뒤
    # **단일 원자 추가**(선행 개행 포함). 락 밖에서 붙이면 같은 conf 를 통째 교체하는
    # writer(board init 병합·`pm_import._write_conf_keys`)가 이 결정을 덮는다.
    accepted = answer in ("y", "yes")
    block = ("# cross-harness 위임 — ON.\ndelegate_enabled=true\n" if accepted else
             "# cross-harness 위임 — 기본 OFF. 켜려면 true 로.\ndelegate_enabled=false\n")
    with _local_conf_write_lock(local_conf) as lock:
        if "delegate_enabled" in _read_local_conf(local_conf):
            # 질문하는 사이 결정이 생겼다 — 그 결정이 이긴다(이 응답은 버린다·byte 보존).
            print("  (질문하는 사이 local.conf 에 delegate_enabled 결정이 생겨 그대로 둡니다 "
                  "— 이 응답은 기록하지 않았습니다.)")
            return
        _append_local_conf_atomic(local_conf, block, lock)
    if accepted:
        print("  ✓ cross-harness 위임 ON (delegate_enabled=true·외부 송신·과금 수용).")
    else:
        print("  → cross-harness 위임 OFF (나중에 local.conf delegate_enabled=true 로 켤 수 있음).")


def read_manifest(path: Path) -> list[ManifestEntry]:
    """manifest 파일 → ManifestEntry 리스트 ('#' 주석·빈 줄 제외·마커 파싱).

    각 항목은 `str` 서브클래스 ManifestEntry — 값은 path 문자열이고 `.render`·`.target_owned`·
    `.source_rel` 속성이 그 path 의 마커 여부/값을 운반한다. path 행 끝의 마커(`@render`·
    `@target-owned`·`@source=<path>`)는 복수·순서 무관으로 인식해 전부 떼어내고 순수 경로만
    ManifestEntry 값으로 남긴다.
      - `@render`→ render=True (byte-copy 대신 render_adapter)
      - `@target-owned`→ target_owned=True (엔진 upstream source-부재가 정상·skip 판별)
      - `@source=<path>`→ source_rel=<path> (source_root 아래 canonical 소스에서 읽고
                                     dest 엔 manifest 경로로 기록·source-remap)
    예: `.opencode/agents  @render @source=templates/opencode/.opencode/agents`
        → path=`.opencode/agents`, render=True, source_rel=`templates/opencode/.opencode/agents`.
    미주석=render/target_owned False·source_rel None → 오늘과 동일(순수 copy2·전파 대상·후방호환).
    """
    out: list[ManifestEntry] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = _parse_manifest_line(line)
        if entry is not None:
            out.append(entry)
    # 구형·flavor manifest의 물리 행 순서가 남아 있어도 새 updater는 중앙 loader를 먼저
    # 배포한다. self-update가 pm_update 직후 중단돼도 재실행 시 이 정규화가 복구 창을 닫는다.
    return sorted(out, key=lambda entry: str(entry) != _CENTRAL_LOADER_REL)


def _parse_manifest_line(line: str) -> ManifestEntry | None:
    """manifest 한 줄 → ManifestEntry — 주석·빈 줄은 None.

    `read_manifest`(파일 전체)와 guest 절 파싱(`_dest_guest_manifest_entries`·텍스트 조각)이
    **같은 한 함수**를 쓴다. 마커 인식이 갈리면 guest 절의 `@render`/엔진 행 구분이 파일 파싱과
    어긋나 소유 채널 판정이 뒤집힌다(판정 사본 금지)."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    # 행 끝의 마커들(복수·순서 무관)을 떼어낸다 — path 와 마커, 마커끼리는 공백 구분.
    parts = line.split()
    render = False
    target_owned = False
    source_rel: str | None = None
    while parts and (
        parts[-1] in _MANIFEST_MARKERS or parts[-1].startswith(SOURCE_TAG_PREFIX)
    ):
        marker = parts.pop()
        if marker == RENDER_TAG:
            render = True
        elif marker == TARGET_OWNED_TAG:
            target_owned = True
        elif marker.startswith(SOURCE_TAG_PREFIX):
            # `@source=<path>` — 값 운반 마커. 빈 값(`@source=`)은 무의미하므로 None 취급
            #   (source 읽기 경로 = manifest 경로·후방호환).
            source_rel = marker[len(SOURCE_TAG_PREFIX):] or None
    if not parts:
        return None
    return ManifestEntry(" ".join(parts), render, target_owned, source_rel)


def _manifest_entry_line(entry) -> str:
    """ManifestEntry를 손실 없이 한 manifest 행으로 직렬화한다(마커 순서 결정적)."""
    markers: list[str] = []
    if _entry_render_flag(entry):
        markers.append(RENDER_TAG)
    if _entry_target_owned_flag(entry):
        markers.append(TARGET_OWNED_TAG)
    source_rel = getattr(entry, "source_rel", None)
    if source_rel:
        markers.append(f"{SOURCE_TAG_PREFIX}{source_rel}")
    return "    ".join((str(entry), *markers))


def merge_manifest_sources(manifest_paths: list[Path]) -> dict:
    """선택된 template flavor manifest들을 선언 순서대로 합집합한다.

    경로가 처음 등장한 선언을 채택한다. 같은 경로가 뒤 flavor에도 있고 마커가 다르면 첫 선언의
    마커를 유지한다. 이는 ``plan_copy``의 MF3(선택 트리 순서가 결정적 우선순위)와 같은 정책이며,
    첫 flavor가 다른 flavor의 상위집합이라는 전제는 두지 않는다. 후순위 flavor의 주석/레이아웃은
    의도적으로 합치지 않고 새 관리 경로만 결정적 생성 절에 직렬화한다.

    첫 manifest의 주석/레이아웃은 그대로 보존하고, 뒤 manifest에서 새로 추가되는 경로만 생성 절로
    붙인다. 단일 manifest면 원문을 byte-identical 반환한다.
    """
    if not manifest_paths:
        raise ValueError("합칠 engine.manifest가 없습니다.")
    paths = [Path(p) for p in manifest_paths]
    first_text = paths[0].read_text(encoding="utf-8")
    merged: list[ManifestEntry] = []
    seen: dict[str, ManifestEntry] = {}
    additions: list[tuple[str, list[ManifestEntry]]] = []
    conflicts: list[str] = []
    for index, path in enumerate(paths):
        current = read_manifest(path)
        added_here: list[ManifestEntry] = []
        for entry in current:
            key = str(entry).replace("\\", "/")
            if key in seen:
                first_markers = _manifest_marker_key(seen[key])
                next_markers = _manifest_marker_key(entry)
                if (
                    first_markers != next_markers
                    and not (
                        key == _MANIFEST_SELF_REL
                        and first_markers[:2] == next_markers[:2]
                    )
                ):
                    conflicts.append(key)
                continue
            seen[key] = entry
            merged.append(entry)
            if index:
                added_here.append(entry)
        if index and added_here:
            try:
                flavor = path.parents[1].name
            except IndexError:
                flavor = path.parent.name
            additions.append((flavor, added_here))
    if len(paths) == 1:
        text = first_text
    else:
        chunks = [first_text.rstrip("\n")]
        for flavor, entries in additions:
            chunks.extend([
                "",
                f"# ── 선택 flavor 합집합: {flavor} (pm_import/pm_update 생성) ──",
                *(_manifest_entry_line(entry) for entry in entries),
            ])
        text = "\n".join(chunks) + "\n"
    return {
        "entries": merged,
        "text": text,
        "conflicts": sorted(set(conflicts)),
        "paths": paths,
    }


def _entry_render_flag(entry) -> bool:
    """manifest 항목의 render 플래그 — ManifestEntry 면 `.render`, 평문 str(레거시 호출)면 False.

    plan() 이 `list[str]`(기존 테스트·외부 호출)과 `list[ManifestEntry]`(read_manifest) 둘 다
    받게 정규화한다 — 후방호환(평문 str 항목은 render 비대상).
    """
    return bool(getattr(entry, "render", False))


def _entry_target_owned_flag(entry) -> bool:
    """manifest 항목의 target_owned 플래그 — ManifestEntry 면 `.target_owned`, 평문 str 면 False.

    source-부재 skip 판별자. 평문 str 항목(레거시 호출)은 target-owned 가 아니므로
    source-부재 시 엔진 누락으로 보고 rc2(후방호환·is_owned skip 은 명시 마커 한정).
    """
    return bool(getattr(entry, "target_owned", False))


def _read_local_conf(path: Path) -> dict[str, str]:
    """local.conf → key=value dict. board.local_config 파싱 규칙 미러.

    `KEY=value` 줄만 채택. `#` 주석·빈 줄·`=` 없는 줄은 무시. 미존재 → {}. stdlib only —
    board 를 import 하지 않는다(pm_update 는 stdlib-only·결합 회피). 같은 키 중복 시 마지막 값.
    """
    conf: dict[str, str] = {}
    if not path.exists():
        return conf
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        conf[key.strip()] = val.strip()
    return conf


def _load_repo_owned_files():
    """공용 seam을 복구채널 명시 면제로 로드한다.

    중앙 로더는 stdlib-only이며 engine_rev를 import하지 않는다. 따라서 손상·구형 engine_rev와
    무관하게 복구 채널이 열리고, path-key 캐시는 stamped 소비 시점에 다시 검증된다.
    """
    path = Path(__file__).resolve().with_name("repo_owned_files.py").resolve()
    module = _load_module_from_path(
        path,
        "repo_owned_files.py",
        allow_unverified=True,
        cache=True,
        cache_key=f"_project_manager_repo_owned_files:{path}",
    )
    missing = _missing_repo_owned_files_api(module)
    if missing:
        raise RuntimeError(
            "repo_owned_files.py 복구 API가 불완전함 "
            f"({', '.join(missing)} 누락); pm-update로 .project_manager/tools 전체를 "
            "재동기화하라."
        )
    return module


# ``main``도 이 seam의 예외 타입으로 실패를 분류한다. 분류 지점마다 방어하지 않고 예외 타입을
# 필수 API 계약에 포함해, 불완전한 seam은 attribute 조회 전에 선복구 대상으로 일관되게 거부한다.
_REPO_OWNED_FILES_REQUIRED = (
    "load_module",
    "RepoFilesGitError",
    "_real_git_runner",
    "list_repo_owned_entries",
    "list_repo_owned_files",
    "TRACKED_ONLY",
    "OWNED",
)


def _missing_repo_owned_files_api(module) -> list[str]:
    """공용 seam 소비에 필요한 API 누락 — 로드와 선복구가 공유하는 단일 판정."""
    return [
        name for name in _REPO_OWNED_FILES_REQUIRED
        if not hasattr(module, name)
    ]


def _atomic_copy2(source: Path, dest: Path) -> None:
    """같은 디렉토리의 완성된 임시 사본을 원자적으로 dest에 교체한다.

    dest 가 symlink 면 `os.replace` 는 **링크 자체를 대체**한다(일반 copy2 는 링크 너머 타깃에
    쓴다) — 의도된 동작이다. 엔진이 관리하는 훅 세트 파일은 manifest 소유 실파일이고, 그 자리를
    링크로 바꿔 둔 트리에 링크 너머로 쓰면 엔진이 자기 관리 밖 경로를 갱신하게 된다."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent,
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(source, tmp)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _resolve_hook_set_generation_and_sibling(source_root=None, *, required: bool = False):
    """`(세대 선언, 그 해소에 쓴 형제 모듈)` — 형제 로드는 **이 한 지점**이다.

    `_load_pm_import` 는 캐시가 없어 부를 때마다 사본을 다시 exec 한다. 해소와 소비(원자 write
    판정자·강등 사다리·세대 검사)가 각자 부르면 한 실행에서 같은 사본을 두 번 적재하고, 두 번째
    로드는 첫 로드가 등록 경계로 흡수한 바로 그 skew 를 **경계 밖에서** 다시 올릴 수 있다. 한 번
    받아 함께 돌려주면 그 창이 없어진다(모듈은 `None` 일 수 있다 — 로드 자체가 실패한 경우).

    로드는 됐는데 해소만 실패한 경우 모듈은 그대로 돌려준다 — 그 사본이 구세대라 해소 지점이 없는
    것이 정확히 강등 사다리가 살려야 하는 상태다."""
    pm_import = None
    try:
        pm_import = _load_pm_import()
        resolver = getattr(pm_import, "hook_set_declarations", None) if pm_import else None
        if resolver is not None:
            return resolver(source_root, required=required), pm_import
        reason = "형제 pm_import 에 세대 선언 해소 지점 부재(구세대 사본)"
    except Exception as exc:  # noqa: BLE001 — 구형/손상 사본에서도 동기는 계속된다.
        # marked skew 도 여기서는 의도적으로 흡수한다(등록된 복구 경계) — apply 도중
        #   형제 사본이 갈리는 건 업그레이드의 정상 경로다.
        _absorb_engine_rev_skew_for_recovery(exc, "resolve_hook_set_generation")
        reason = f"형제 pm_import 로드 실패({type(exc).__name__}: {exc})"
    return (SimpleNamespace(declarations=None, origin="미해소", reasons=(reason,)),
            pm_import)


def resolve_hook_set_generation(source_root=None, *, required: bool = False):
    """훅 세트 세대 선언을 해소한다 — pm_update 안 **모든 소비자의 단일 진입**.

    해소 규칙 자체는 pm_import 소유(`hook_set_declarations`)다: 상류 우선, 조회는 로컬 폴백,
    mutation 게이트는 fail-closed. 여기서는 형제 로드 실패를 흡수해 그 규칙에 태우기만 한다 —
    사본이 구형/손상이어도 동기 자체는 계속돼야 한다(등록된 복구 경계).

    반환은 pm_import 의 `HookSetGeneration`(declarations·origin·reasons) 형상이고, declarations 가
    None 이면 판정 근거가 없다는 뜻이다(게이트는 멈추고, 조회는 loud 후 무판정). 형제 모듈까지
    함께 쓰는 소비자는 `_resolve_hook_set_generation_and_sibling` 로 **한 번만** 로드한다."""
    return _resolve_hook_set_generation_and_sibling(source_root, required=required)[0]


def _sibling_accepts_kwarg(func, name: str) -> bool:
    """그 형제 API 가 이 키워드를 받는가 — **엔진 사본 세대 차** 흡수 지점.

    복구 채널·부분 전파 창에서는 형제 pm_import 가 직전 세대일 수 있다(그 창을 열어 두는 게 복구
    exemption 의 목적이다). 새 키워드를 무조건 넘기면 그 상태에서 판정이 TypeError 로 죽고, 그걸
    fail-soft 로 받으면 **그 세대가 이미 제공하던 보호까지** 함께 버린다. 있으면 쓰고 없으면 구
    시그니처로 강등하되 강등은 loud 다(pm_config `_accepts_kwarg` 와 같은 규약)."""
    if func is None:
        return False
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # 시그니처를 못 읽는 호출가능 객체 — 보수적으로 구형 취급.
        return False


def _warn_hook_set_downgrade(what: str, effect: str) -> None:
    """구세대 형제 사본 때문에 훅 세트 보호를 낮춰 실행한다는 사실을 알린다(무음 강등 금지)."""
    print(f"[경고] 형제 pm_import 가 구세대라 {what} 없이 진행한다 — {effect}. "
          "pm-update 로 엔진 사본을 맞춘 뒤 다시 실행하면 정상 판정을 받는다.", file=sys.stderr)


def _print_hook_set_query_fallback(lines_fn, generation) -> None:
    """조회 축 강등 사유 표면화 — 상류 선언을 못 읽어 설치본 세대로 판정했다는 사실을 남긴다.

    조회 축은 관대 계약이라 여기서 차단하지 않는다(판정을 통째로 잃는 것보다 한 세대 뒤 선언으로라도
    보는 편이 낫다). 다만 사유까지 버리면 `--check`·변경 0 실행이 **상류 전용 플래그를 못 본 채**
    green 으로 끝나고, 채택자는 강등된 판정을 정상 판정으로 읽는다 — 침묵만 제거한다.

    **문구는 pm_import 단일 진실**(`hook_set_query_fallback_lines`)이다 — pm-config `--check` 와
    같은 문장을 써야 두 게이트가 같은 상태를 같은 말로 보고한다. 그 함수가 없는 세대 사본이면
    조용히 건너뛴다(그 세대엔 이 안내 자체가 없고, 강등 사실은 사다리가 따로 알린다)."""
    if lines_fn is None:
        return
    for line in lines_fn(generation):
        print(line, file=sys.stderr)


def resolve_hook_set_predicate(source_root=None):
    """훅 세트 원자-write 판정자 — 위 단일 진입이 준 **상류 세대 선언**으로 만든다.

    선언은 pm_import 단일 진실이다(여기 사본을 두면 "검사는 하는데 원자 write 는 안 하는" 파일이
    조용히 생긴다). 해소는 실행당 1회이고 결과를 `apply` 에 넘긴다 — 파일마다 부르면 사본 수만큼
    모듈을 다시 로드한다(`_load_pm_import` 는 캐시 없음).

    조회 성격이라 로컬 폴백까지 관대하되, **전부 실패하면** 훅 파일이 통째로 비원자 copy2 로
    떨어지므로 조용히 넘어가지 않는다 — torn read 창이 다시 열린 사실을 stderr 한 줄로 남긴다.

    강등은 3단이고 각 단은 loud 다(pm_config `_adapter_accept_decision` 과 같은 사다리):
      선언 해소됨          상류(또는 로컬) 선언으로 훅 경로를 판정 → 그 파일만 원자 교체.
      선언 미해소 + 구 API  형제가 선언 주입 이전 세대면 `is_live_hook_set_path` 단일 인자로
                           강등한다 — **그 세대가 이미 제공하던 보호**(설치본 선언 기준 판정)를
                           버리지 않는다.
      둘 다 부재           무판정(전 파일 일반 복사) — 그 사실을 알린다."""
    # 해소와 소비가 **같은 형제 적재**를 쓴다 — 여기서 따로 로드하면 한 실행에 사본을 두 번
    #   exec 하고, 두 번째 로드가 첫 로드의 흡수 밖에서 skew 를 올린다.
    generation, pm_import = _resolve_hook_set_generation_and_sibling(source_root)
    declarations = getattr(generation, "declarations", None)
    reasons = " / ".join(getattr(generation, "reasons", ())) or "사유 미상"
    try:
        # 구 API 탐지도 경계 안이다 — 손상/혼합 사본은 로드가 아니라 **속성 접근**에서 발화하므로
        #   (형제 verifier 계층), 탐지를 밖에 두면 그 예외가 apply 직전에 그대로 올라간다.
        legacy_predicate = (getattr(pm_import, "is_live_hook_set_path", None)
                            if pm_import is not None else None)
    except Exception as exc:  # noqa: BLE001 — 구형/손상 사본에서도 동기는 계속된다.
        _absorb_engine_rev_skew_for_recovery(exc, "resolve_hook_set_predicate")
        pm_import = None
        legacy_predicate = None
        reasons = f"{reasons} / 형제 판정 API 조회 실패({type(exc).__name__}: {exc})"
    if declarations is not None and pm_import is not None:
        if source_root is not None and getattr(generation, "reasons", ()):
            # 상류를 주고도 그 선언을 못 읽어 **설치본 세대로 강등**됐다 — 이번에 새로 등재되는 훅
            #   경로는 이 판정자가 모르므로 그 파일만 비원자 복사로 떨어진다. 조용히 넘기지 않는다.
            print(
                "[경고] 상류 훅 세트 선언을 읽지 못해 설치본 세대로 판정한다 — 이번 상류가 새로 "
                f"등재하는 훅 파일은 원자 교체 대상에서 빠진다. {reasons}",
                file=sys.stderr,
            )
        return lambda rel, _t=declarations: bool(
            pm_import.is_live_hook_set_path(str(rel).replace("\\", "/"), _t))
    if legacy_predicate is not None:
        # 선언 해소는 실패했지만 판정 함수 자체는 있다 — 그 사본의 설치본 선언으로 판정한다.
        #   여기서 무판정으로 내려가면 혼합 세대 복구 중에 훅 파일이 통째로 비원자 복사가 된다.
        _warn_hook_set_downgrade(
            "훅 세트 상류 세대 선언",
            f"설치본 선언으로 원자 write 대상을 정한다(이번 상류가 새로 등재하는 훅 파일은 "
            f"그 대상에서 빠진다). {reasons}")
        return lambda rel, _p=legacy_predicate: bool(
            _p(str(rel).replace("\\", "/")))
    print(
        "[경고] 훅 세트 원자 write 판정자를 해소하지 못했다 — 이번 동기는 훅 파일도 일반 복사로 "
        f"쓴다(실행 중 하네스가 부분 파일을 읽을 창이 열린다). {reasons}",
        file=sys.stderr,
    )
    return lambda _rel: False


def central_loader_needs_recovery(dest_root: Path) -> bool:
    """dest 의 중앙 로더 seam이 부재·손상·구형인가(선복구가 실제로 write 할 상황인가).

    경로 스코프 실행이 "이 경로만 전파" 계약을 지키려면 선복구를 건너뛰어야 하는데, 그때 조용히
    넘기면 seam이 깨진 인스턴스는 다음 실행도 같은 자리에서 막힌다 — 판정만 따로 떼어 호출부가
    **알리고** 건너뛸 수 있게 한다."""
    try:
        current = _load_module_from_path(
            dest_root / _CENTRAL_LOADER_REL, "repo_owned_files.py", allow_unverified=True,
        )
    except Exception:  # noqa: BLE001 — 로드 실패도 복구 대상(아래 판정과 같은 결론).
        current = None
    return bool(_missing_repo_owned_files_api(current))


def _predeploy_central_loader(source_root: Path, dest_root: Path) -> None:
    """손상/구형 seam을 검증된 tracked 일반 파일로 원자 선복구한다."""
    global _TOOLS_BOOTSTRAP_MODULE
    source = source_root / _CENTRAL_LOADER_REL
    dest = dest_root / _CENTRAL_LOADER_REL
    if not central_loader_needs_recovery(dest_root):
        return
    try:
        source.lstat()
    except FileNotFoundError:
        # seam을 manifest에 싣지 않은 레거시/부분 fixture는 기존 계획 경로를 보존한다.
        # 실제 신 엔진 source는 manifest 순서 가드가 이 파일의 존재를 별도로 강제한다.
        return
    except OSError as exc:
        raise RuntimeError(f"중앙 로더 source 상태를 확인할 수 없음: {source}: {exc}") from exc
    relative = Path(_CENTRAL_LOADER_REL)
    if not _is_shippable_regular_file(source_root, relative, index_mode=None):
        raise RuntimeError(f"중앙 로더 source가 일반 파일이 아님: {source}")
    try:
        source_module = _load_module_from_path(
            source, "repo_owned_files.py", allow_unverified=True,
        )
    except Exception as exc:  # noqa: BLE001 — source seam 자체의 손상을 복구 실패로 표면화.
        raise RuntimeError(f"중앙 로더 source를 불러올 수 없음: {source}") from exc
    missing = _missing_repo_owned_files_api(source_module)
    if missing:
        raise RuntimeError(
            "중앙 로더 source 복구 API가 불완전함 "
            f"({', '.join(missing)} 누락): {source}"
        )
    tracked = _shipping_inventory(source_module, source_root, _CENTRAL_LOADER_REL)
    accepted = _shippable_tracked_entries(source_root, tracked)
    if accepted != [(relative, source)]:
        raise RuntimeError(f"중앙 로더 source가 tracked 일반 파일이 아님: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    _atomic_copy2(source, dest)
    _TOOLS_BOOTSTRAP_MODULE = None
    sys.modules.pop(_TOOLS_BOOTSTRAP_KEY, None)
    sys.modules.pop(f"_project_manager_repo_owned_files:{dest.resolve()}", None)
    print(f"  [recovery-first] {_CENTRAL_LOADER_REL}")


class SkippedRepoShippingEntryWarning(RuntimeWarning):
    """pm-update가 byte-copy할 수 없는 tracked 엔트리를 명시적으로 제외했다는 신호."""


class EmptyShippingInventoryError(RuntimeError):
    """존재하는 manifest 엔트리의 tracked 출하 인벤토리가 0건인 결함."""

    def __init__(
            self, checkout: Path, subtree: str,
            *, filesystem_fallback: bool = False) -> None:
        self.checkout = Path(checkout)
        self.subtree = subtree
        self.filesystem_fallback = filesystem_fallback
        diagnosis = (
            "filesystem 강등 상태이므로 소스 디렉토리가 비었는지와 checkout 루트가 "
            "올바른지 확인하라"
            if filesystem_fallback
            else "checkout 루트가 올바른지와 git index에 이 경로가 등재됐는지 확인하라"
        )
        super().__init__(
            "pm-update 출하 인벤토리가 0건임 "
            f"(checkout={self.checkout}, subtree={subtree!r}); "
            + diagnosis
        )


def _shipping_inventory(repo_files, root: Path, rel: str) -> list:
    """tracked 출하 목록과 미추적 제외 신호를 만든다.

    ``pm-update`` 출하는 manifest 디렉토리의 byte-copy 채널이다. 따라서 공용 seam의 넓은
    domain(gap 검출에 필요한 symlink/gitlink 포함)은 유지하되, 이 소비처에서만 실제 일반
    파일로 좁힌다. git checkout일 때 OWNED와의 차집합 중 같은 일반 파일 판정을 통과한
    미추적 파일 수만 한 줄로 알려 ``git add`` 뒤에만 출하된다는 계약을 숨기지 않는다.
    """
    runner = repo_files._real_git_runner(root)
    tracked = repo_files.list_repo_owned_entries(
        root,
        rel,
        mode=repo_files.TRACKED_ONLY,
        git_runner=runner,
    )
    rc, inside = runner(["rev-parse", "--is-inside-work-tree"])
    filesystem_fallback = not (rc == 0 and inside.strip() == "true")
    if rc == 0 and inside.strip() == "true":
        owned = repo_files.list_repo_owned_files(
            root, rel, mode=repo_files.OWNED, git_runner=runner)
        tracked_paths = {entry.path for entry in tracked}
        untracked_count = sum(
            _is_shippable_regular_file(root, relative, index_mode=None)
            for relative in set(owned) - tracked_paths
        )
        if untracked_count:
            print(
                f"pm-update: untracked {untracked_count}건 제외 — git add 후 전파됨 "
                f"(subtree={rel})",
                file=sys.stderr,
            )
        if not tracked and (root / rel).is_file():
            ignored_rc, _ignored_detail = runner([
                "check-ignore",
                "--quiet",
                "--",
                rel,
            ])
            if ignored_rc == 0:
                print(
                    "pm-update: manifest 선언 단일 파일이 source에서 gitignore되어 "
                    f"출하되지 않음: {rel} — ignore 규칙을 제거하고 git add 하라",
                    file=sys.stderr,
                )
    if not tracked:
        # seam은 coverage·부분 subtree 질의도 쓰므로 빈 결과 자체를 예외로 만들지 않는다.
        # manifest 경로가 존재해 이 소비점에 도달한 경우만 출하 결함으로 직접 승격한다.
        # 위의 OWNED/check-ignore 진단을 먼저 내 원인 판별 정보도 잃지 않는다.
        raise EmptyShippingInventoryError(
            root, rel, filesystem_fallback=filesystem_fallback)
    return tracked


def _shippable_tracked_entries(
    root: Path,
    entries: list["RepoOwnedEntry"],
) -> list[tuple[Path, Path]]:
    """tracked 엔트리를 안전한 byte-copy source로 좁히고 제외 이유를 loud하게 합친다."""
    accepted: list[tuple[Path, Path]] = []
    skipped: dict[str, list[str]] = {
        "working tree에서 삭제됨": [],
        "symlink(링크 의미를 byte-copy 출하하지 않음)": [],
        "디렉토리/gitlink(파일 byte-copy 대상 아님)": [],
        "일반 파일이 아닌 엔트리": [],
    }
    for entry in entries:
        relative = entry.path
        index_mode = entry.index_mode
        source = root / relative
        if index_mode == "120000":
            skipped["symlink(링크 의미를 byte-copy 출하하지 않음)"].append(
                relative.as_posix())
            continue
        if index_mode == "160000":
            skipped["디렉토리/gitlink(파일 byte-copy 대상 아님)"].append(
                relative.as_posix())
            continue
        if index_mode is not None and index_mode not in {"100644", "100755"}:
            skipped["일반 파일이 아닌 엔트리"].append(
                f"{relative.as_posix()} (git index mode {index_mode})")
            continue
        try:
            mode = source.lstat().st_mode
        except FileNotFoundError:
            skipped["working tree에서 삭제됨"].append(relative.as_posix())
            continue
        except OSError as exc:
            skipped["일반 파일이 아닌 엔트리"].append(
                f"{relative.as_posix()} ({exc})")
            continue
        if stat.S_ISREG(mode):
            accepted.append((relative, source))
        elif stat.S_ISLNK(mode):
            skipped["symlink(링크 의미를 byte-copy 출하하지 않음)"].append(
                relative.as_posix())
        elif stat.S_ISDIR(mode):
            skipped["디렉토리/gitlink(파일 byte-copy 대상 아님)"].append(
                relative.as_posix())
        else:
            skipped["일반 파일이 아닌 엔트리"].append(relative.as_posix())

    for reason, paths in skipped.items():
        if paths:
            warnings.warn(
                f"pm-update 출하 tracked 엔트리 {len(paths)}건 제외 — {reason}: "
                + ", ".join(paths),
                SkippedRepoShippingEntryWarning,
                stacklevel=2,
            )
    return accepted


def _is_shippable_regular_file(
    root: Path,
    relative: Path,
    *,
    index_mode: str | None,
) -> bool:
    """출하 가능한 일반 파일인가 — index mode와 working-tree lstat의 공통 판정."""
    if index_mode is not None and index_mode not in {"100644", "100755"}:
        return False
    try:
        return stat.S_ISREG((root / relative).lstat().st_mode)
    except OSError:
        return False


def _iter_files(root: Path, rel: str):
    """manifest 엔트리(파일/디렉토리) → (repo 기준 relpath, src 절대경로) 들.

    디렉토리는 repo-owned seam으로 협착하고 symlink/gitlink 등 제외를 loud하게 표면화한다.
    relpath 는 **항상 posix(슬래시) 정규화**한다(`as_posix()`) — 모듈 전체의 슬래시 관례
    (`_path_under_manifest`·`_dest_relpath_for` 는 `.replace("\\","/")` 로 슬래시 전제)과 통일.
    `str(Path.relative_to)` 는 OS-네이티브 구분자라 Windows 에선 역슬래시(`.claude\\agents\\x.md`)
    를 산출해 plan change 튜플 key 가 소비자/테스트(슬래시)와 어긋났다(pm_render 4건 red).
    POSIX 에선 `str(p.relative_to(root)) == p.relative_to(root).as_posix()` 라 동작 무변경.
    """
    src = root / rel
    if src.is_symlink():
        warnings.warn(
            f"pm-update 출하 manifest 엔트리 제외 — symlink 의미를 byte-copy 출하하지 않음: {rel}",
            SkippedRepoShippingEntryWarning,
            stacklevel=2,
        )
    elif src.is_dir():
        repo_files = _load_repo_owned_files()
        tracked = _shipping_inventory(repo_files, root, rel)
        for relative, source in _shippable_tracked_entries(root, tracked):
            yield relative.as_posix(), source
    elif src.is_file():
        repo_files = _load_repo_owned_files()
        tracked = _shipping_inventory(repo_files, root, rel)
        for accepted_relative, source in _shippable_tracked_entries(root, tracked):
            yield accepted_relative.as_posix(), source
    # missing → 아무것도 yield 안 함 (호출부가 missing 으로 보고)


# ── board-분리 인지 dest 리매핑 ───────────────────────────
# manifest 는 ticket 본문 템플릿을 `wiki/tickets/_template.md` 로 들고 있다(canonical·
# legacy adopter 의 실 위치). 그러나 board(tickets+areas)가 `.project_manager/board/`
# (submodule)로 분리된 adopter(board.py board_root)에선 `_template.md` 가
# `board/tickets/_template.md` 에 산다(board_root() 추종·B 마이그레이션이 거기로 옮김).
# manifest 항목은 legacy-correct 로 두고(자체 drift 회피), *동기 시 dest 경로만*
# board_root 로 해소한다 — board-분리 dest 면 board/tickets/_template.md 로, legacy dest 면
# 종전 wiki/tickets/_template.md 로(무변경). 이로써 board-분리 adopter 의 매 sync 가
# wiki/tickets/_template.md 를 부활시키지 않는다(drift-0·실 발생 버그 reconcile).
#
# board.py board_root() 의 *실측* 판별(`<dest>/.project_manager/board/tickets` 가 dir 인가)을
# 동형 복제한다 — pm_update 는 stdlib-only(board 미import 결합 회피·_resolve_dest_source 와
# 동형)이고, 판별은 단일 is_dir() probe 라 board.py line 71/95 와 정확히 같다. 어떤 manifest
# 항목이 board-분리 시 board/ 로 옮겨가는지는 아래 `_TEMPLATE_REL`→`_BOARD_TEMPLATE_REL`
# 매핑(`_dest_relpath_for`)이 단일 진실.
_TEMPLATE_REL = ".project_manager/wiki/tickets/_template.md"
_BOARD_TEMPLATE_REL = ".project_manager/board/tickets/_template.md"


def _is_board_separated(dest_root: Path) -> bool:
    """dest 가 board-분리 형상인가 — `<dest>/.project_manager/board/tickets` 가 실 dir.

    board.py board_root() 의 판별과 동형(line 71/95) — pm_update 가 board 를 import 하지 않고
    같은 *실측* probe 로 dest 레이아웃을 가른다. board/tickets 가 없으면 legacy(False·무변경).
    """
    return (Path(dest_root) / ".project_manager" / "board" / "tickets").is_dir()


def _dest_relpath_for(rel: str, dest_root: Path) -> str:
    """manifest source relpath → dest 기록 relpath (board-분리 인지 리매핑).

    `wiki/tickets/_template.md` 항목은 board-분리 dest 에서 `board/tickets/_template.md` 로
    리매핑한다(board_root() 추종) — source 는 upstream 의 wiki/ 에서 그대로 읽되 dest 만 옮긴다.
    legacy dest(board/ 미분리)거나 다른 모든 항목은 입력 그대로(무변경·후방호환). 경로 비교는
    OS-무관하게 posix-normalize 한다(_iter_files 가 str(Path) 로 yield 해 Windows 에선 `\\` 가
    섞일 수 있다)."""
    rel_norm = rel.replace("\\", "/")
    if rel_norm == _TEMPLATE_REL and _is_board_separated(dest_root):
        return _BOARD_TEMPLATE_REL
    return rel


# ── @source source-remap (_dest_relpath_for dest-remap 의 대칭 source 쌍) ──
# manifest 항목이 `@source=<relpath>` 를 달면 source_root 아래 그 canonical 경로에서 읽되(source_rel),
# dest 엔 manifest 경로(str(entry))로 기록한다. opencode 어댑터(`.opencode/agents`·`command`)는
# 채택자 dest 엔 `.opencode/*` 로 살지만 프레임워크 루트의 canonical 소스는 `templates/opencode/
# .opencode/*` 에 있다(루트=claude·`.opencode/` 부재). 이 비대칭을 잇는 read-side remap.
def _source_root_rel(entry) -> str:
    """manifest 항목의 source-root 상대 *읽기* 경로 — @source= 있으면 source_rel, 없으면 str(entry).

    기본(마커 부재·source_rel None)은 dest relpath 를 그대로 source-root 상대 읽기 경로로 쓴다
    (오늘 동작·후방호환). `@source=<path>`가 있으면 source_root 아래 그 canonical 경로에서
    읽는다 — dest 기록 경로는 manifest 경로 유지(_remap_to_dest 가 치환). 평문 str 항목(레거시 호출)은
    source_rel 속성 부재 → str(entry)(getattr 폴백).
    """
    return getattr(entry, "source_rel", None) or str(entry)


def _remap_to_dest(rel: str, source_rel: str, manifest_path: str) -> str:
    """source-root relpath → manifest(dest) 기록 relpath (@source source-remap).

    _iter_files 가 source_rel(canonical 소스) 아래에서 yield 한 relpath 의 source_rel prefix 를
    manifest_path(dest)로 치환한다 — `_dest_relpath_for`(dest-remap)의 대칭 source 쌍. source_rel ==
    manifest_path(마커 부재·기본)면 무변경(후방호환). 파일 항목(단일)은 yield 가 source_rel 자체라
    manifest_path 로 통째 치환, 디렉토리 항목은 `source_rel/…` 하위를 `manifest_path/…` 로 옮긴다.
    경로 비교는 OS-무관 posix-normalize(_iter_files 가 Windows 에서 `\\` 섞을 수 있음·_dest_relpath_for
    동형)."""
    if source_rel == manifest_path:
        return rel
    rel_norm = rel.replace("\\", "/")
    src_norm = source_rel.replace("\\", "/")
    if rel_norm == src_norm:
        return manifest_path
    prefix = src_norm + "/"
    if rel_norm.startswith(prefix):
        return manifest_path + "/" + rel_norm[len(prefix):]
    return rel


def _manifest_owner_index(manifest: list, rel: str, dest_root: Path) -> int | None:
    """``rel``을 공급할 가장 구체적인 manifest 항목의 index.

    디렉터리 remap 위에 단일 파일 remap을 선언하면 더 긴 destination 경로가 override다. 이
    우선순위가 없으면 상위 디렉터리와 파일 항목이 같은 destination을 각각 plan하고, plan 시점의
    기존 파일만 비교한 뒤 apply 순서에 따라 override가 사라질 수 있다. 동일 경로 중복은 뒤 항목을
    택해 manifest의 인접 override가 결정적이게 한다.
    """
    rel_norm = rel.replace("\\", "/").strip("/")
    owners: list[tuple[int, int, int]] = []
    for index, candidate in enumerate(manifest):
        dest_rel = _dest_relpath_for(str(candidate), dest_root)
        dest_norm = dest_rel.replace("\\", "/").strip("/")
        if rel_norm == dest_norm or rel_norm.startswith(dest_norm + "/"):
            owners.append((len(Path(dest_norm).parts), index, index))
    return max(owners)[2] if owners else None


def manifest_entry_shipping_inventory(
    source_root: Path,
    manifest: list,
    entry_index: int,
    dest_root: Path | None = None,
) -> tuple[list[tuple[str, Path]], bool, bool]:
    """manifest 한 항목의 실제 byte-copy 출하 inventory와 누락 성격을 반환한다.

    반환은 ``(files, missing, target_owned)``다. ``files``의 각 항목은
    ``(dest repo 상대경로, source 절대경로)``이며, 디렉토리의 tracked-only 열거·일반 파일
    협착·``@source`` 리매핑·가장 구체적인 manifest 소유권·dest 레이아웃 리매핑을 모두
    ``plan``과 같은 경로로 적용한다. source가 없으면 files는 비고 ``missing=True``이며,
    호출자가 ``target_owned``로 정상적인 전파 제외와 엔진 누락 오류를 구분한다.

    출하 목록을 관찰하는 다른 엔진 기능은 이 seam을 소비해야 한다. manifest 경로를 직접
    ``iterdir``/``rglob``로 전개하면 git ignore, index mode, source remap과 override 의미가
    실제 update 계획에서 다시 갈라진다.
    """
    source_root = Path(source_root)
    effective_dest = Path(dest_root) if dest_root is not None else REPO
    entry = manifest[entry_index]
    rel = str(entry)
    source_rel = _source_root_rel(entry)
    target_owned = _entry_target_owned_flag(entry)
    if not (source_root / source_rel).exists():
        return [], True, target_owned

    files: list[tuple[str, Path]] = []
    for shipped_rel, source in _iter_files(source_root, source_rel):
        shipped_rel = _remap_to_dest(shipped_rel, source_rel, rel)
        shipped_rel = _dest_relpath_for(shipped_rel, effective_dest)
        if _manifest_owner_index(manifest, shipped_rel, effective_dest) != entry_index:
            continue
        files.append((shipped_rel, source))
    return files, False, target_owned


def _load_pm_render():
    """pm_render 모듈을 같은 tools/ 디렉토리에서 직접 로드 (sys.path 오염 없이·stdlib seam).

    pm_import._detected_py 가 board.py 를 로드하는 패턴과 동형 — pm_update 는 stdlib-only
    철학이나 render 분기는 pm_render(같은 엔진 동기 대상)에 위임한다. import 실패는 호출부가
    안전쪽으로 처리하게 예외를 전파(render path 인데 렌더러 없음 = 명확한 에러가 옳다).
    """
    render_py = Path(__file__).resolve().parent / "pm_render.py"
    return _load_module_from_path(
        render_py, "pm_render.py", allow_unverified=True,
    )


def _load_pm_import():
    """pm_import 모듈을 같은 tools/ 에서 직접 로드 (_load_pm_render 패턴 동형).

    upstream_rev baseline 기록(매 sync)에 pm_import 의 URL 안전 git 호출
    (read_upstream_rev — argv-list·timeout·GIT_TERMINAL_PROMPT=0)과 local.conf set-or-replace
    (`_set_conf_keys` — record_upstream_rev 와 동일 백엔드)를 *재사용*한다 — pm_update 가 자체
    git/conf-write 를 중복 구현하지 않게(엔진 stdlib-only 철학 안에서 검증된 안전 계약을 상속).
    로드 실패는 호출부가 fail-soft
    (baseline 기록은 best-effort·sync 자체를 깨지 않는다).
    """
    import_py = Path(__file__).resolve().parent / "pm_import.py"
    return _load_module_from_path(
        import_py, "pm_import.py", allow_unverified=True,
    )


# ── upstream baseline↔HEAD 변경점 요약 (read-only) ─────────
# git `name-status` 코드(첫 글자) → 표시용. R(rename)·C(copy)는 첫 글자만 본다(접두).
_NAME_STATUS_LABELS = {"M": "M", "A": "A", "D": "D", "R": "R", "C": "C", "T": "T"}


def _path_under_manifest(
        rel_path: str, manifest: list, dest_root: Path | None = None) -> bool:
    """changed relpath 가 manifest 항목(파일=동일·디렉토리=prefix)에 속하는지 — 엔진 영향 판정.

    manifest 한 줄은 파일 또는 디렉토리(repo 루트 기준·재귀). changed 파일이 manifest 의
    파일 항목과 정확히 같거나, manifest 디렉토리 항목 *아래*(posix prefix + `/`)면 이번 동기가
    덮어쓰는 엔진 경로다. _iter_files 의 디렉토리 재귀 의미와 동형(파일은 `==`·디렉토리는
    `startswith(d + "/")`). manifest 의 @render/@target-owned 마커는 ManifestEntry 가 이미
    떼어내 path-only 값이라 `str(entry)` 로 순수 경로만 비교한다.

    판정 좌표는 **엔트리마다 하나**, 그 엔트리의 *읽기* 좌표다(`_source_root_rel` 공유) — `@source=`
    매핑 엔트리는 상류 경로(예 `.claude/agents @source=templates/claude_code/.claude/agents`),
    bare 엔트리는 dest 경로(둘이 같다). 상류 변경은 읽기 좌표로 오므로 dest 축만 보면 `@source`
    엔트리 전부가 "manifest 밖(동기 안 받음)" 으로 오분류되고, 거꾸로 매핑 엔트리에 **dest 좌표까지**
    인정하면 이번 계획이 읽지도 않는 경로(`.codex/agents/x`)를 엔진 영향으로 과분류한다. 이 함수의
    의미가 "이 상류 변경이 이번 동기에 영향을 주는가" 이므로 판정은 읽기 좌표 단일이다.

    **좌표가 맞는 것만으로는 부족하다 — 소유권까지 해소한다.** 디렉토리 항목 위에 더 구체적인 파일
    항목이 다른 source 를 공급하면(codex `.agents/skills @source=.claude/skills` 위의
    `.agents/skills/<skill>/SKILL.md @source=templates/codex/…`) 그 파일의 실제 source 는 override
    쪽이라, 상위 항목의 source 변경은 그 파일에 도달하지 않는다. plan 이 쓰는 좌표 변환·우선순위
    seam(`_remap_to_dest`→`_dest_relpath_for`→`_manifest_owner_index` — `manifest_entry_shipping_
    inventory` 와 같은 순서·판정 사본 없음)을 그대로 태워, 변경된 경로가 **그 항목의 산출로 실제
    선택될 때만** 엔진으로 분류한다. dest 레이아웃 기준은 그 seam 의 폴백과 동일(REPO).
    """
    # dest 레이아웃 리매핑 기준 — 미지정은 REPO(self-location·`_dest_relpath_for` 폴백과 동일).
    effective_dest = Path(dest_root) if dest_root is not None else REPO
    rel_norm = rel_path.replace("\\", "/").strip("/")
    for index, entry in enumerate(manifest):
        dest_declared = str(entry).replace("\\", "/").strip("/")
        # 이 엔트리의 **읽기 좌표** 하나만 본다 — bare 는 dest 와 같고, 매핑 엔트리는 상류 경로다.
        coordinate = _source_root_rel(entry).replace("\\", "/").strip("/")
        if not coordinate:
            continue
        if not (rel_norm == coordinate or rel_norm.startswith(coordinate + "/")):
            continue
        shipped_rel = _dest_relpath_for(
            _remap_to_dest(rel_norm, coordinate, dest_declared), effective_dest)
        if _manifest_owner_index(manifest, shipped_rel, effective_dest) == index:
            return True
    return False


# 엔진 도구 접두사 — 이 아래 파일은 rev 스탬프(ENGINE_REV)를 들고 서로를 로드하므로, 부분 전파가
# 트리 안 rev 를 섞을 수 있다(스코프 경고의 판정 기준).
_ENGINE_TOOLS_PREFIX = ".project_manager/tools/"


# ── `--paths` 경로 스코프 (opt-in 부분 전파) ────────────────────────────────
# 기본 sync 는 manifest 전량이라 all-or-nothing 이다 — 병렬 작업 중 한 파일만 내보내려면 엔진 밖
# 수동 복사로 우회하게 된다(그 우회가 안전 판정·render 를 통째로 건너뛴다). 이 옵션은 그 필요를
# 엔진 안으로 들여 **명시한 경로만** 전파한다. 스코프 판정은 manifest 등재분에 한정하고(미등재는
# 거부), 부분 전파이므로 "전량 흡수" 를 전제하는 후속 단계(baseline 기록·마이그레이션·훅 재설치)는
# 발화하지 않는다 — 그것들이 돌면 drift-lint 가 거짓 '최신' 을 말한다.

def _normalize_scope_paths(raw_paths) -> tuple[list[str], list[str]]:
    """`--paths` 입력을 repo 기준 posix relpath 로 정규화 — (정규화 목록, 거부 사유 목록).

    거부: 빈 값(`.` 자기참조 포함)·절대경로/드라이브·`..` 컴포넌트(스코프가 저장소 밖을 가리키면
    안 된다). **저장하는 값은 정규화 결과**다 — 원본을 저장하면 `./a/b`·`a//b` 같은 동치 표기가
    매칭에서 빗나가 "등재는 통과했는데 아무것도 전파 안 되는" 조용한 rc0 이 된다. 정규화 뒤에
    중복을 입력 순서대로 접는다(`a/b` 와 `./a/b` 는 한 항목)."""
    normalized: list[str] = []
    rejected: list[str] = []
    for raw in raw_paths:
        value = str(raw).strip().replace("\\", "/").rstrip("/")
        if not value:
            rejected.append(f"{raw!r}: 빈 경로")
            continue
        candidate = PurePosixPath(value)
        # posix anchor(`/`·UNC `//host/share`)와 Windows 드라이브(`C:x`)를 한 식으로 거른다 —
        #   백슬래시는 위에서 이미 `/` 로 통일했다.
        if candidate.is_absolute() or PureWindowsPath(value).drive:
            rejected.append(f"{value!r}: 절대경로(스코프는 repo 루트 상대여야 한다)")
            continue
        if any(part == ".." for part in candidate.parts):
            rejected.append(f"{value!r}: `..` 컴포넌트(저장소 밖 지향)")
            continue
        # `PurePosixPath` 가 `.`·중복 `/` 를 접으므로 그 산출이 곧 정규형이다(`.` 만 준 경우는
        #   parts 가 비어 빈 경로로 거부된다).
        canonical = candidate.as_posix()
        if not candidate.parts:
            rejected.append(f"{value!r}: 빈 경로(자기 참조 `.`)")
            continue
        if canonical not in normalized:
            normalized.append(canonical)
    return normalized, rejected


def _paths_overlap(one: str, other: str) -> bool:
    """두 경로가 겹치는가 — 같거나 한쪽이 다른 쪽의 상위 디렉토리(양방향)."""
    return one == other or one.startswith(other + "/") or other.startswith(one + "/")


def _unregistered_scope_paths(manifest: list, scope: list[str],
                              effective_dest: Path | None = None) -> list[str]:
    """manifest 어느 항목과도 겹치지 않는 요청 경로 — 거부 대상(조용한 무전파 금지).

    겹침은 양방향이다: 요청이 manifest 항목 *아래*(디렉토리 항목의 파일 하나)여도, 요청이 여러
    manifest 항목을 *담는* 상위 디렉토리여도 유효하다. 판정 대상은 세 좌표다 — manifest 의 dest
    경로, `@source` 읽기 경로, 그리고 **dest 레이아웃 리매핑 결과**(`_dest_relpath_for`). 마지막을
    빼면 board 분리 인스턴스에서 실제 관리 파일인 `board/tickets/_template.md` 가 미등재로 거부된다
    (계획은 그 좌표로 착지시키는데 게이트만 옛 좌표를 본다)."""
    declared: set[str] = set()
    for entry in manifest:
        declared.add(str(entry).replace("\\", "/").strip("/"))
        declared.add(_source_root_rel(entry).replace("\\", "/").strip("/"))
        if effective_dest is not None:
            declared.add(
                _dest_relpath_for(str(entry), effective_dest).replace("\\", "/").strip("/"))
    declared.discard("")
    return [
        path for path in scope
        if not any(_paths_overlap(path, item) for item in declared)
    ]


def _in_scope_paths(rel: str, scope: list[str]) -> bool:
    """이 relpath 가 요청 스코프 안인가 — 요청 경로 자신이거나 요청 디렉토리 아래.

    전파 대상 판정은 **단방향**이다: 요청보다 상위 경로(요청을 담는 디렉토리)는 스코프 밖 파일까지
    끌고 오므로 대상이 아니다. 반대로 source 부재 보고는 항목 단위(디렉토리)라 양방향으로 본다
    (`_paths_overlap`) — 요청 파일을 담은 디렉토리 등재가 통째로 없으면 그 사실이 신호여야 한다."""
    rel_norm = str(rel).replace("\\", "/").strip("/")
    return any(rel_norm == path or rel_norm.startswith(path + "/") for path in scope)


def _dest_guest_manifest_entries(effective_dest: Path) -> list[ManifestEntry]:
    """dest engine.manifest 의 add-harness guest 절 행 — 읽기 실패·절 부재는 빈 목록.

    절 안의 한 줄이 **소유 채널**을 스스로 말한다: `@render` 행은 어댑터 렌더물(add-harness
    refresh 소유·update plan 제외), 비-`@render` 행은 엔진 파일(update 채널 소유·byte-copy 전파).
    그 구분을 하려면 경로만이 아니라 마커까지 필요하므로 파싱해서 돌려준다(파서는
    `_parse_manifest_line` 공유)."""
    manifest_file = Path(effective_dest) / ".project_manager" / "engine.manifest"
    try:
        block = _extract_guest_manifest_block(manifest_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []
    if not block:
        return []
    entries = [_parse_manifest_line(line) for line in block.splitlines()]
    return [entry for entry in entries if entry is not None]


def _dest_guest_manifest_paths(effective_dest: Path) -> set[str]:
    """dest guest 절의 **`@render` 행** 경로 집합 — 읽기 실패·절 부재는 빈 집합.

    이 집합의 유일한 소비자는 `--paths` 스코프 거부 라벨링이다: 렌더물은 update plan 에서 빠지는
    **다른 채널**(add-harness refresh 전용)이라, 스코프 거부 진단이 그것을 "미등재(오타)" 로 뭉치면
    사람이 없는 오타를 찾게 된다. 반대로 **엔진 행은 update 채널의 정상 전파 대상**이므로 여기 넣으면
    안 된다 — 넣으면 그 경로를 `--paths` 로 지정한 요청이 "guest 절 소속" 으로 오거부된다."""
    return {
        str(entry).replace("\\", "/").strip("/")
        for entry in _dest_guest_manifest_entries(effective_dest)
        if _entry_render_flag(entry)
    }


# ── guest 절 엔진 행 파생 백필 ────────────────────────────────────────────────
# add-harness 가 등재하는 guest 엔진 행은 `@source=templates/<flavor>/…` 로 자기 출처를 들고 있다.
# 그 채널이 없던 세대에 설치된 채택자의 절엔 렌더물 행뿐이라 provenance 가 없고, 그 코호트의
# 엔진 파일(`pm_relay` 코어와 짝인 드라이버·ctx 가드 등)은 add-harness 를 다시 돌리지 않는 한 설치
# 시점 사본으로 영구 동결된다. 계획 직전에 flavor 를 해소해 upstream flavor manifest 에서 엔진 행을
# **매 실행 재파생**하면 옛 채택자도 다음 sync 한 번으로 채널을 얻고, upstream 이 flavor 에 엔진
# 파일을 새로 추가해도 재동결되지 않는다. 파생은 읽기 전용이고 기록은 apply 의 절 재부착 한 곳뿐이다.
_FLAVOR_SOURCE_PREFIX = "templates/"


def _guest_declared_flavors(guest_entries: list) -> list[str]:
    """guest 절 행의 `@source=templates/<flavor>/…` provenance — flavor 이름(최초 등장 순서).

    기록된 출처는 추론보다 항상 정확하므로 **선언된 flavor 는 추론 대상에서 뺀다**. 다만 절 전체를
    "선언됨" 으로 보지는 않는다 — 한 절에 새 세대(provenance 有)와 구 세대(렌더물 행만)가 공존하는
    **혼재 코호트**가 실재하기 때문이다(하네스를 순차로 얹고 하나만 refresh 한 인스턴스)."""
    flavors: list[str] = []
    for entry in guest_entries:
        source_rel = (getattr(entry, "source_rel", None) or "").replace("\\", "/")
        if not source_rel.startswith(_FLAVOR_SOURCE_PREFIX):
            continue
        parts = PurePosixPath(source_rel).parts
        if len(parts) < 3 or parts[1] in {"", ".", ".."}:
            continue
        if parts[1] not in flavors:
            flavors.append(parts[1])
    return flavors


def _flavor_manifest_entries_by_name(source_root: Path) -> dict[str, list]:
    """`templates/*/…/engine.manifest` → {flavor 디렉토리명: entries} (읽기 실패는 제외·fail-soft)."""
    out: dict[str, list] = {}
    for candidate in sorted(
        Path(source_root).glob("templates/*/.project_manager/engine.manifest"),
        key=lambda path: path.as_posix(),
    ):
        try:
            out[candidate.parents[1].name] = read_manifest(candidate)
        except (OSError, UnicodeError, ValueError):
            continue
    return out


def _infer_guest_flavors(source_root: Path, guest_paths: set[str]) -> list[str]:
    """guest 절이 담은 경로에서 flavor 를 **배타 경로 증거**로 추론한다(provenance 미해소분용).

    증거 = 그 flavor 에만 있는 경로(`_flavor_exclusive_paths`·frozen 판정과 같은 헬퍼)를 guest 절이
    실제로 담고 있는가. 단순 namespace 매칭으로 대체하면 여러 flavor 가 함께 선언하는 cross-ns 행
    (codex host + opencode guest 의 `.claude/skills`)이 claude 로 오인돼 **없던 인스턴스에 파일을
    만든다** — 파생 결과가 곧 파일 생성 권한이므로 거짓양성이 비파괴 계약을 깬다. 같은 이유로
    install receipt(존재-추론)도 쓰지 않는다."""
    entries_by_flavor = _flavor_manifest_entries_by_name(source_root)
    if not entries_by_flavor:
        return []
    paths_by_flavor = {
        flavor: {str(entry).replace("\\", "/") for entry in entries}
        for flavor, entries in entries_by_flavor.items()
    }
    inferred: list[str] = []
    for flavor, entries in entries_by_flavor.items():
        other_paths: set[str] = set()
        for other, paths in paths_by_flavor.items():
            if other != flavor:
                other_paths |= paths
        if guest_paths & set(_flavor_exclusive_paths(entries, other_paths)):
            inferred.append(flavor)
    return inferred


def _guest_engine_backfill_entries(
        effective_dest: Path, source_root: Path,
        guest_entries: list) -> tuple[list, list[str]]:
    """dest guest 절이 담아야 할 **엔진 행**을 upstream flavor 에서 재파생 — (엔트리, 해소 flavor).

    파생은 pm_import 의 단일 생성기(`_guest_manifest_lines` — 복사 술어 `_in_adapter_namespace` 를
    그대로 태운다)를 호출해 얻고 비-`@render` 행만 남긴다. 렌더물은 add-harness refresh 소유라
    update 채널이 파생하지 않으며, 생성기를 공유하므로 "등재 ⊆ 복사" 가 여기서도 상속된다.
    guest 절이 없거나(비-add-harness) flavor 를 해소할 수 없으면 빈 목록(무동작·현행 거동).

    flavor 해소 = **선언 ∪ 미선언분 추론**이다. 한 절에 두 하네스가 공존하고 그 중 하나만 새 세대로
    refresh 된 **혼재 코호트**에서, 선언이 하나라도 있으면 추론을 통째로 끄는 판정은 구 세대 쪽 엔진
    행을 영구 미등재로 남긴다(정확히 이 티켓이 닫으려는 동결). 추론은 선언되지 않은 flavor 에 대해
    서만 돌고 판정 자체는 배타 경로 증거 그대로라, cross-ns 오탐 가드는 손대지 않는다."""
    if not guest_entries:
        return [], []
    guest_paths = {str(entry).replace("\\", "/") for entry in guest_entries}
    declared = _guest_declared_flavors(guest_entries)
    flavors = [*declared, *(
        flavor for flavor in _infer_guest_flavors(source_root, guest_paths)
        if flavor not in declared
    )]
    if not flavors:
        return [], []
    dest_manifest = Path(effective_dest) / ".project_manager" / "engine.manifest"
    try:
        dest_owned = _core_manifest_paths(dest_manifest.read_text(encoding="utf-8"))
        pm_import = _load_pm_import()
        harness_by_flavor = {
            template_dir: harness
            for harness, template_dirs in pm_import.HARNESS_TEMPLATE_DIRS.items()
            for template_dir in template_dirs
        }
    except Exception as exc:  # noqa: BLE001 — 파생 실패는 현행 거동 유지(sync 자체는 계속).
        # 조용히 접지 않는다 — 파생이 꺼지면 guest 엔진 파일이 그 실행에서 다시 동결되는데,
        #   출력이 없으면 "원래 갱신 대상이 아니다" 와 구분되지 않는다.
        print(f"note: guest 엔진 행 파생을 건너뛴다(fail-soft·엔진 동기는 계속): {exc}",
              file=sys.stderr)
        return [], []
    entries: list = []
    seen: set[str] = set()
    resolved: list[str] = []
    for flavor in flavors:
        harness = harness_by_flavor.get(flavor)
        template_root = Path(source_root) / "templates" / flavor
        if harness not in pm_import.ADD_HARNESS_ADAPTER:
            continue
        if not (template_root / ".project_manager" / "engine.manifest").is_file():
            continue
        adapter_dirs, root_doc = pm_import.ADD_HARNESS_ADAPTER[harness]
        try:
            lines = pm_import._guest_manifest_lines(
                template_root, adapter_dirs, root_doc, dest_owned)
        except Exception as exc:  # noqa: BLE001 — 한 flavor 실패가 나머지를 막지 않는다.
            print(f"note: guest flavor {flavor} 의 엔진 행 파생 실패(그 flavor 만 건너뛴다): {exc}",
                  file=sys.stderr)
            continue
        resolved.append(flavor)
        for line in lines:
            entry = _parse_manifest_line(line)
            if entry is None or _entry_render_flag(entry):
                continue
            path = str(entry).replace("\\", "/")
            if path in seen:
                continue
            seen.add(path)
            entries.append(entry)
    return entries, resolved


def _refuse_unregistered_scope_paths(
        manifest: list, scope: list[str], guest_paths: set[str],
        effective_dest: Path | None = None) -> int:
    """스코프 요청 중 manifest 미등재분을 사유별로 loud 거부 — 있으면 rc1, 없으면 0.

    두 사유를 가른다: **guest 절**(add-harness 채널이라 update 가 원래 안 건드린다)과 **미등재**
    (오타·인스턴스 소유·아직 manifest 에 안 올린 신규 파일). 같은 문구로 뭉치면 전자는 manifest 를
    뒤지게 만들고 후자는 채널을 오해하게 만든다."""
    unregistered = _unregistered_scope_paths(manifest, scope, effective_dest)
    if not unregistered:
        return 0
    guest_hits = [
        path for path in unregistered
        if any(_paths_overlap(path, guest) for guest in guest_paths)
    ]
    for path in unregistered:
        if path in guest_hits:
            print(f"  [guest 절] {path} — add-harness 가 등재한 guest `@render` 경로다. "
                  "update 채널의 전파 대상이 아니므로(refresh 는 add-harness 소관) 스코프로 "
                  "지정할 수 없다.", file=sys.stderr)
        else:
            print(f"  [미등재] {path}", file=sys.stderr)
    plain = [path for path in unregistered if path not in guest_hits]
    if plain:
        print(
            f"오류: --paths 경로 {len(plain)}개가 engine.manifest 등재분이 아니다 — pm-update 는 "
            "manifest 소유 경로만 전파한다(인스턴스 소유 파일은 대상 아님). 경로 오타이거나, 그 "
            "파일이 아직 manifest 에 등재되지 않았는지 확인하라.", file=sys.stderr)
    if guest_hits:
        print(
            f"오류: --paths 경로 {len(guest_hits)}개가 add-harness guest 절 소속이다 — "
            "`pm_config add-harness <harness>` 로 갱신하라(update 는 guest 를 건드리지 않는다).",
            file=sys.stderr)
    return 1


def _scope_validation_manifest(effective_dest: Path, source_root: Path) -> list | None:
    """스코프 **소유권 선검증**용 manifest 합 — dest 것과 source 것의 합집합(best-effort).

    실 계획 manifest 는 중앙 로더 선복구(dest 쓰기) 뒤에 해소된다. 미등재 요청이 rc1 로 끝나는데
    그 전에 쓰기가 일어나면 "거부인데 파일이 바뀐다" 가 되므로, 쓰기 전에 읽기만으로 한 번 거른다.
    이 시점 판정은 **관대해야** 한다(합집합): self-heal 이 upstream 등재분을 계획 기준으로 올리는
    경우가 있어 dest manifest 만 보면 유효 요청을 오거부한다. 좁은 판정은 계획 확정 뒤 한 번 더
    돈다. 양쪽 다 못 읽으면 None(선검증 생략 — 정규 경로가 같은 오류를 더 정확히 낸다)."""
    entries: list = []
    seen: set[str] = set()
    candidates = [
        Path(effective_dest) / ".project_manager" / "engine.manifest",
        Path(source_root) / ".project_manager" / "engine.manifest",
    ]
    found = False
    for manifest_path in candidates:
        if not manifest_path.is_file():
            continue
        try:
            parsed = read_manifest(manifest_path)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        found = True
        for entry in parsed:
            key = str(entry).replace("\\", "/").strip("/")
            if key not in seen:
                seen.add(key)
                entries.append(entry)
    return entries if found else None


def _scope_change_candidates(rel: str, source_path, source_root: Path) -> list[str]:
    """한 변경을 스코프에 대볼 후보 경로 — dest relpath + (가능하면) source relpath.

    `@source` 리매핑 항목은 dest 와 upstream 경로가 다르다(opencode 어댑터). 둘 다 후보로 두면
    "내가 고친 파일 경로" 로 지정해도, "채택자 트리에서 보이는 경로" 로 지정해도 걸린다. 인메모리
    source(manifest 합집합)는 파일 경로가 없어 dest 후보만 남는다."""
    candidates = [str(rel)]
    if isinstance(source_path, Path):
        try:
            candidates.append(source_path.relative_to(source_root).as_posix())
        except ValueError:
            pass
    return candidates


def summarize_upstream_changes(
    checkout: Path,
    baseline: str,
    manifest: list,
    *,
    git_runner=None,
    dest_root: Path | None = None,
) -> dict:
    """upstream 로컬 checkout 의 baseline..HEAD 변경점을 read-only 로 요약한다 ().

    채택자가 받은 baseline(`upstream_rev`) ↔ 그 이후 upstream HEAD 에 쌓인 변경을 *이미 로컬에
    있는* checkout 에서 `git log`/`diff --name-status` 로 집계한다 — **fetch/clone 안 함**
    (네트워크 0). git 안전 계약(argv-list·timeout·GIT_TERMINAL_PROMPT=0·config
    격리)은 pm_import._real_upstream_git_runner 를 재사용한다(git_runner 미주입 시). 테스트는
    git_runner 를 주입해 라이브 git 0 으로 결정론을 얻는다(DI seam).

    `manifest` 는 "무엇이 엔진인가"의 판별 집합 — 호출부가 **sync 와 동일한**
    `_resolve_planning_manifest(effective_dest, source, selfheal)` 로 해소해 넘긴다(self-heal
    승격분 우선·없으면 dest 우선 로컬 해소·guest 절 채널 분리 포함). 이 함수가 자체 로드하지 않는
    이유: 엔진 영향(이번 동기가 받는 것) 분류는 실 sync 가 쓰는 manifest 와 *반드시* 일치해야 하기
    때문(source 단독은 dest 커스터마이즈/--target 에서 어긋난다). 빈 manifest → 전부 'other'
    (graceful·엔진 영향 0 보수 표시).

    `dest_root` 는 dest 레이아웃 리매핑(board 분리 인스턴스) 기준 — 미지정이면 REPO(self-location)
    다. 분류가 좌표 변환을 태우므로 미리보기 대상 인스턴스와 같은 기준을 넘겨야 어긋나지 않는다.

    반환 dict:
      - `status`: 'ok' | 'baseline_unreachable' | 'up_to_date' | 'summary_failed'
      - `head`: HEAD commit(rev-parse HEAD) 또는 '' (실패 시)
      - `count`: baseline..HEAD commit 수 (int)
      - `engine`: [(code, path)] — manifest 항목에 속하는 변경(이번 동기가 받는 것)
      - `removed_upstream`: [(code, path)] — manifest 항목의 상류 삭제(`D`)와 rename 의 낡은
        경로(`R` 의 old path). 동기는 **지우지 않는다**(dest 잔존) — "받는 것" 과 반대 사실이라
        버킷을 가른다. rename 의 *새* 경로는 상류가 실제로 공급하므로 engine 에 남는다.
      - `other`: [(code, path)] — manifest 밖 변경(동기 안 받음)
      - `log`: [(sha, subject)] — `git log --oneline baseline..HEAD` (--log 옵션용)

    호출부(main --changes 분기)가 baseline 부재·URL upstream·HEAD==baseline 등 *상위* 게이트를
    이미 처리한 뒤 진입한다. 여기선 baseline rev 가 checkout 에서 도달 가능한지(rc)만 본다 —
    도달불가(force-push·shallow)면 status='baseline_unreachable'(호출부가 재clone 권고). log/diff
    가 rc≠0(도달가능한데도 git 호출 실패·예외)면 status='summary_failed' — 빈 결과를 "변경 0"으로
    오판하지 않게 surface 한다(codex suggestion 1·advisory 오판 금지).
    """
    runner = git_runner if git_runner is not None else _load_pm_import()._real_upstream_git_runner()
    result: dict = {
        "status": "ok",
        "head": "",
        "count": 0,
        "engine": [],
        "removed_upstream": [],
        "other": [],
        "log": [],
    }

    # HEAD 해소 (rev-parse) — checkout 의 현재 HEAD commit.
    rc, out = runner(["-C", str(checkout), "rev-parse", "HEAD"])
    if rc == 0 and out.strip():
        result["head"] = out.strip().splitlines()[0].strip()

    # baseline 도달성 검사 — baseline commit object 가 이 checkout 에 있는지(force-push·shallow
    # 시 없을 수 있다). `cat-file -e <rev>^{commit}` rc 로 판정(네트워크 0·로컬 object DB 만).
    rc, _out = runner(["-C", str(checkout), "cat-file", "-e", baseline + "^{commit}"])
    if rc != 0:
        result["status"] = "baseline_unreachable"
        return result

    # baseline == HEAD 면 변경 0 — log/diff 모두 빈 출력이라 자연히 up_to_date 로 떨어지지만,
    # 호출부가 보통 상위에서 거른다(별도 키 비교). 여기선 log 집계로 count 를 낸다.
    # rc≠0(도달가능한데도 git 실패)은 summary_failed 로 surface(빈 결과 오판 금지·suggestion 1).
    rc, out = runner(["-C", str(checkout), "log", "--oneline", f"{baseline}..HEAD"])
    if rc != 0:
        result["status"] = "summary_failed"
        return result
    log_entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        sha, _, subject = line.partition(" ")
        log_entries.append((sha.strip(), subject.strip()))
    result["log"] = log_entries
    result["count"] = len(log_entries)
    if result["count"] == 0:
        result["status"] = "up_to_date"

    # diff --name-status baseline..HEAD — 변경 파일 목록(M/A/D/R…). 첫 토큰=코드, 둘째=경로
    # (R/C 는 `R100\told\tnew` 3필드라 마지막 필드를 새 경로로 본다). rc≠0 면 summary_failed —
    # commit 수는 났지만 파일 분류가 불가능하므로 "엔진 영향 0" 오판을 피해 surface 한다.
    rc, out = runner(["-C", str(checkout), "diff", "--name-status", f"{baseline}..HEAD"])
    if rc != 0:
        result["status"] = "summary_failed"
        return result
    for line in out.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        fields = line.split("\t")
        raw_code = fields[0].strip()
        code = _NAME_STATUS_LABELS.get(raw_code[:1], raw_code[:1] or "?")
        path = fields[-1].strip() if len(fields) > 1 else ""
        if not path:
            continue
        # 상류 삭제(`D`)는 이번 동기가 *받는* 변경이 아니다 — 디렉토리 엔트리 동기는 source 만
        # 열거해(추가·갱신) 삭제를 전파하지 않으므로 dest 파일은 잔존한다. "받는 것" 버킷에
        # 실으면 출력이 사실과 반대로 오보한다.
        if code == "D":
            bucket = (
                "removed_upstream" if _path_under_manifest(path, manifest, dest_root) else "other"
            )
            result[bucket].append((code, path))
            continue
        # rename 은 두 좌표를 낸다 — 새 경로는 상류가 공급(engine)이고, 낡은 경로는 manifest
        # 안이었다면 dest 에 잔존한다(manifest 밖으로 나가는 rename 이 대표 사례). `C`(copy)는
        # 원본이 그대로 남으므로 낡은 좌표가 없다.
        if code == "R" and len(fields) >= 3:
            old_path = fields[1].strip()
            if old_path and _path_under_manifest(old_path, manifest, dest_root):
                result["removed_upstream"].append((code, old_path))
        bucket = "engine" if _path_under_manifest(path, manifest, dest_root) else "other"
        result[bucket].append((code, path))

    return result


def _resolve_dest_source(args) -> tuple:
    """args(--target·--from) → (rc, dest_root, source_root). rc≠0 이면 메시지는 이미 출력됨.

    dest/source 해소는 sync(main)와 read-only --changes가 공유한다 — 둘 다
    같은 우선순위(명시 --from local.conf upstream= 에러)·URL 게이트·stale
    가드를 거쳐야 일관적이다. 추출로 두 진입이 같은 코드 경로를 탄다(중복 0). 성공 시 rc=0 +
    (dest_root[None=self-loc], source_root[디렉토리 검증 통과]). 실패 시 rc≠0(메시지 stderr 출력)
    + (None, None).
    """
    # dest_root: --target 지정 시 REPO/templates/<target>/, 아니면 None(self-location=REPO).
    if args.target:
        try:
            dest_root = resolve_target_root(args.target)
        except (ValueError, FileNotFoundError) as exc:
            print(str(exc), file=sys.stderr)
            return 1, None, None
    else:
        dest_root = None  # 호출부가 REPO fallback 사용

    effective_dest = dest_root if dest_root is not None else REPO

    # ── upstream(source) 해소 — 순서: 명시 --from local.conf upstream= 에러.
    #    침묵 폴백 없음. stale(부재/비-디렉토리) 경로는 자동 진행하지 않고 명확한 에러로 멈춘다.
    if args.source:
        source_root = Path(args.source).resolve()
    else:
        local_conf = effective_dest / ".project_manager" / "local.conf"
        stored = _read_local_conf(local_conf).get("upstream", "").strip()
        if not stored:
            print(
                "오류: upstream 미등록 — --from <checkout> 를 주거나 "
                f"{local_conf} 에 `upstream=` 를 등록하라 "
                "(이 프로젝트를 한 번 pm_import 하면 자동 기록된다).",
                file=sys.stderr,
            )
            return 1, None, None
        # upstream= 이 URL(릴리스 추적 기본값)이면 엔진은
        #   로컬 파일만 복사하므로 `Path(url).resolve()` 했다간 "디렉터리 없음" 류로 침묵 실패한다.
        #   URL 은 디렉토리로 해소하지 말고 *명확·actionable* 에러로 멈춘다 — git freshness 는
        #   스킬층(pm-update: URL→cache clone)이거나 `--from <로컬 checkout>` 명시가 답이다.
        try:
            kind = _load_pm_import().classify_upstream(stored)
        except Exception:  # noqa: BLE001 — 분류 실패는 보수적으로 경로 취급(기존 동작·fail-soft).
            kind = "path"
        if kind == "url":
            print(
                f"오류: upstream 이 URL 이다 ({stored}) — 엔진(pm_update)은 로컬 파일만 복사한다 "
                "(git clone/fetch 안 함). `pm-update` 스킬(URL→cache clone 후 sync)을 "
                "쓰거나, `--from <로컬 checkout>` 으로 로컬 경로를 명시하라.",
                file=sys.stderr,
            )
            return 1, None, None
        source_root = Path(stored).resolve()

    # stale 가드: 해소된 upstream 이 부재/디렉토리 아님 → 자동 진행 금지(명확한 에러). 기존
    # missing-manifest(rc 2)와 구분되는 메시지·rc(=1)로 "upstream 자체가 잘못됐다"를 알린다.
    if not source_root.is_dir():
        origin = "--from" if args.source else f"local.conf upstream= ({effective_dest}/.project_manager/local.conf)"
        print(
            f"오류: upstream 경로가 디렉토리가 아니거나 존재하지 않음: {source_root} "
            f"(출처: {origin}). 체크아웃이 이동/삭제됐다면 --from 으로 올바른 경로를 주거나 "
            "local.conf 의 upstream= 을 갱신하라.",
            file=sys.stderr,
        )
        return 1, None, None

    return 0, dest_root, source_root


def _run_changes(args) -> int:
    """`--changes` read-only 분기 — baseline..HEAD 변경점 요약 출력(실 sync 안 함).

    dest/source 해소는 sync 와 공유(_resolve_dest_source) — URL upstream 은 거기서 명확 에러로
    멈춘다(엔진은 git clone/fetch 안 함). baseline(`upstream_rev`)은 *dest* local.conf
    에서 읽는다(매 sync 시 pm_update 가 기록한 마지막 동기 기준점). 전부 fail-soft·exit 0(graceful
    안내) — baseline 미기록·HEAD==baseline·baseline 도달불가 각각 메시지로 surface 한다.
    """
    rc, dest_root, source_root = _resolve_dest_source(args)
    if rc != 0:
        return rc  # URL upstream·미등록·stale 는 sync 와 동일한 명확 에러(rc≠0).

    effective_dest = dest_root if dest_root is not None else REPO
    baseline = _read_local_conf(
        effective_dest / ".project_manager" / "local.conf").get("upstream_rev", "").strip()

    # baseline 미기록(아직 sync 한 적 없음·구 import) — graceful 안내(exit 0). 다음 sync 후 추적된다.
    if not baseline:
        print(
            "upstream 변경: baseline 미기록 — 아직 동기 baseline(upstream_rev)이 local.conf 에 "
            "없다. 다음 `pm-update`(실 sync) 후 baseline 이 기록되면 변경점이 추적된다."
        )
        return 0

    # 엔진 영향 판별 manifest 는 **sync 와 동일한** `_resolve_planning_manifest` 로 해소한다
    # (self-heal 승격 우선·없으면 dest 우선 로컬·둘 다 부재면 source). 실 sync 가 그 manifest 로
    # "무엇이 엔진인가"를 정하므로 --changes 의 "엔진 영향(이번 동기가 받는 것)"도 같은 기준이어야
    # 한다 — 둘이 반드시 일치해야 한다. self-heal 해소 조건(`--target` 비발화)도 `_main` 과 같다.
    # 둘 다 부재(fresh-adopter)면 빈 manifest → 전부 'other'(graceful·엔진 영향 0 보수 표시·
    # summarize_upstream_changes 가 빈 리스트 허용). **read-only 유지** — self-heal 은 판정만 하고
    # (읽기 전용) 승격 manifest 를 디스크에 쓰지 않는다(쓰기는 실 sync 의 apply 소관).
    try:
        selfheal = (
            None if args.target
            else resolve_manifest_selfheal(effective_dest, source_root)
        )
        if selfheal is not None:
            # 다중-flavor 합집합의 마커 충돌은 미리보기에서도 알린다 — 실 sync 만 알리면 "미리보기는
            #   조용했는데 sync 가 경고" 가 되어, 미리보기로 판단하는 채택자가 충돌을 못 본다.
            _print_manifest_merge_conflicts(selfheal)
        manifest = _resolve_planning_manifest(effective_dest, source_root, selfheal)
    except FileNotFoundError:
        manifest = []

    summary = summarize_upstream_changes(
        source_root, baseline, manifest, dest_root=effective_dest)

    # baseline rev 가 checkout 에서 도달 불가(force-push·shallow clone) — 재clone 권고(exit 0).
    if summary["status"] == "baseline_unreachable":
        print(
            f"upstream 변경: baseline {baseline[:12]} 가 upstream checkout 에서 도달 불가 "
            "(force-push 됐거나 shallow clone). upstream 을 재clone 하거나 `--from <온전한 "
            "checkout>` 으로 다시 확인하라."
        )
        return 0

    # 변경점 집계 실패(log/diff git 호출 rc≠0) — 빈 결과를 "변경 0"으로 오판하지 않게 surface
    # (codex suggestion 1·advisory 오판 금지). exit 0 유지(read-only 안내)하되 명확히 알린다.
    if summary["status"] == "summary_failed":
        print(
            f"upstream 변경: baseline {baseline[:12]} 이후 변경점 집계 실패(요약 불가) — "
            "upstream checkout 의 git log/diff 호출이 실패했다. checkout 이 온전한 git work "
            "tree 인지 확인하거나 `--from <온전한 checkout>` 으로 다시 시도하라.",
            file=sys.stderr,
        )
        return 0

    head = summary["head"]
    count = summary["count"]

    # --count-only: commit 개수 1줄만(advisory/스크립트).
    if args.count_only:
        print(str(count))
        return 0

    # HEAD == baseline(변경 0·최신) — count 0.
    if count == 0:
        print(f"upstream 변경: baseline {baseline[:12]} → HEAD {head[:12]} (변경 0·최신)")
        return 0

    # ── 3블록 요약 (채택자-facing·기본 간결) ──────────────────────────────────
    print(f"upstream 변경: baseline {baseline[:12]} → HEAD {head[:12]} ({count} commits)")

    engine = summary["engine"]
    other = summary["other"]
    removed_upstream = summary.get("removed_upstream") or []
    print(f"엔진 영향 (manifest 경로·이번 동기가 받는 것): {len(engine)} files")
    for code, path in engine:
        print(f"  {code} {path}")
    print(f"그 외 변경 (manifest 밖·동기 안 받음): {len(other)} files")
    # 삭제·rename 은 "받는 것" 두 버킷과 성격이 다르다(동기가 손대지 않는 dest 잔존) — 맨 뒤에
    # 따로 낸다. sync 실행의 은퇴 후보 보고(`_print_retired_manifest_files`)와 같은 사실을 말한다.
    print(
        "상류 삭제·rename (동기가 지우지 않음 — 아래 보고 참조): "
        f"{len(removed_upstream)} files"
    )
    for code, path in removed_upstream:
        print(f"  {code} {path}")
    # 헤더가 가리키는 "아래 보고" — 실 sync 와 같은 은퇴 후보 산출(read-only·파생만·write 0).
    #   여기서 안 부르면 그 포인터가 가리킬 대상이 없다(빈 포인터).
    _print_retired_manifest_files(
        _retired_manifest_files(source_root, manifest, effective_dest, set()))

    # --log: git log --oneline baseline..HEAD 꼬리.
    if args.log:
        print("커밋 (baseline..HEAD):")
        for sha, subject in summary["log"]:
            print(f"  {sha} {subject}")

    return 0


# 경로 upstream 에서 baseline 과 *함께* 기록하는 현재-관찰 키 (board._DRIFT_SEEN_KEY 동명).
_SEEN_REV_KEY = "upstream_seen_rev"


def _upstream_shape(pm_import, dest_root: Path) -> str:
    """dest local.conf 의 `upstream=` 값 모양 — 'url' | 'path' (네트워크 0).

    seen-rev 동시 기록의 분기 입력이다. 미등록(`--from` 직접 지정·구 import)·분류 실패는
    `_resolve_dest_source` 와 동일하게 **보수적으로 'path'** 취급한다(기존 동작·fail-soft).
    """
    stored = _read_local_conf(
        dest_root / ".project_manager" / "local.conf").get("upstream", "").strip()
    if not stored:
        return "path"
    try:
        return pm_import.classify_upstream(stored)
    except Exception:  # noqa: BLE001 — 분류 실패는 보수적으로 경로 취급(fail-soft).
        return "path"


def _upstream_rev_updates(pm_import, dest_root: Path, rev: str) -> dict[str, str]:
    """이번에 기록할 rev 키 계획 — baseline 은 항상, 관찰값은 **경로 형상에서만**.

    형상 입력은 대상 conf 의 현재 `upstream=` 값이라 이 계산은 conf 락 구간 안에서만 유효하다
    (락 밖에서 세운 계획은 커밋 시점에 이미 낡을 수 있다 — 동시 `upstream set` 이 path↔URL 을
    뒤집으면 stale 형상으로 `upstream_seen_rev` 을 잘못 쓰거나 빠뜨린다). 계획을 락 안에서 다시
    세울 수 있게 분리한 조각이다.
    """
    updates = {"upstream_rev": rev}
    if _upstream_shape(pm_import, dest_root) == "path":
        updates[_SEEN_REV_KEY] = rev
    return updates


def _warn_missing_conf_for_rev(local_conf: Path) -> tuple[bool, dict[str, str]]:
    """local.conf 부재 — rev 기록을 graceful 생략하고 `record_upstream_revs` 반환값을 낸다."""
    print(f"경고: local.conf 없음 ({local_conf}) — upstream_rev 기록 건너뜀.", file=sys.stderr)
    return False, {}


def record_upstream_revs(dest_root: Path, source_root: Path) -> tuple[bool, dict[str, str]]:
    """매 sync 후 upstream rev 키들을 dest local.conf 에 **단일 write** 로 기록.

    반환 `(변경 여부, 이번에 엔진이 기록한 {키: rev})` — 호출부가 *실제로 무엇을 썼는지* 를
    보고 안내 문구를 정한다(결과 상태로 역추론 금지: URL 형상은 스킬층이 쓴 seen 이 이미
    baseline 과 같아서 "엔진이 썼다"와 구분되지 않는다). 기록 생략 시 `(False, {})`.

    기록 키:
      - `upstream_rev`      (baseline·항상) — drift-lint의 "마지막 동기 이후" 기준점
        pm_import(import 시)와 여기(매 sync) 둘 다 갱신해야 그 의미가 성립한다.
      - `upstream_seen_rev` (현재 관찰값·**경로 upstream 한정**) — 경로 형상은 fetch 채널이
        따로 없어 *동기 시점의 로컬 checkout rev 가 곧 관찰값*이다('로컬 경로'
        분기와 동일 규정). baseline 만 갱신하면 두 키가 영구히 어긋나 정상 흡수 직후에도 drift
        거짓 경보가 상시 뜬다(실측). URL 형상은 **건드리지 않는다** — 스킬층이 fetch 후
        관찰값을 기록한다(한 키 2역 금지·race/자기비교 회피).

    두 키를 한 번의 공용 atomic writer로 묶는다 — 중간 중단에도 baseline 만 앞선 반쪽
    상태가 생기지 않는다(어긋난 두 키 = 거짓 drift 의 원인이었다). rev 읽기는 pm_import 의
    read_upstream_rev(URL 안전 git 호출), 파일 갱신은 pm_import 의 `_write_conf_keys_locked`(키 중복
    정규화·atomic replace·실효값 검증·record_upstream_rev·pm_config upstream set 과 동일 백엔드의
    임계 구간 본문)를 재사용한다. git repo 아님·HEAD 해소 실패·pm_import 로드 실패·local.conf
    부재는 **graceful 생략**(best-effort — sync 자체는 안 깬다).

    conf 존재 판정·형상 판정(`upstream=` 읽기)·updates 계산·atomic write·실효값 검증은 **한
    락 구간**이다 — 형상을 락 밖에서 읽으면 동시 `pm-config upstream set` 이 path↔URL 을 뒤집는
    사이 stale 형상으로 계획이 굳어, URL 이 된 conf 에 `upstream_seen_rev`(스킬층 소유)을 쓰거나
    path 가 된 conf 에서 그 키를 빠뜨린다. source rev 읽기(git·네트워크)는 대상 conf 와 무관하므로
    락 밖이다 — 사람/네트워크 대기를 임계 구역에 넣지 않는다는 seam 규약 그대로다.
    """
    try:
        pm_import = _load_pm_import()
    except Exception:  # noqa: BLE001 — 로드 실패는 baseline best-effort: sync 를 안 깬다.
        return False, {}
    # 아래는 형제 pm_import 의 rev 조회·conf writer 를 재사용한다 — writer 가 다시 형제 file_lock 을
    #   verifier 로 로드하므로 **동기 실행 중 사본 rev 혼합의 표면**이다. baseline 기록은
    #   best-effort 단계이고 이 지점은 apply 이후라, 여기서 skew 를 올리면 이미 착지한 엔진 파일
    #   위에서 동기가 traceback 으로 죽는다(등록된 경계로 흡수·다음 실행이 기록한다).
    try:
        rev = pm_import.read_upstream_rev(source_root)  # 대상 conf 와 무관(git) — 락 밖.
        if not rev:
            return False, {}  # git repo 아님·HEAD 해소 실패 — graceful 생략(URL upstream 포함).

        local_conf = dest_root / ".project_manager" / "local.conf"
        if not local_conf.is_file():
            # init 전 트리·출하 템플릿처럼 conf 자체가 없는 형상에 락 파일을 만들지 않는 값싼 단축이다
            # (권위 판정은 아래 락 안에서 다시 한다 — 이 단축은 "쓰지 않는다" 만 결정하고 어떤 write
            # 계획의 입력도 아니다). conf 가 그사이 생기면 종전처럼 다음 sync 가 기록한다.
            return _warn_missing_conf_for_rev(local_conf)
        if not callable(getattr(pm_import, "_write_conf_keys_locked", None)):
            # 구세대 pm_import 사본(임계 구간 본문 seam 이전) — 자기-락 writer 로 물러난다. 우리 락을
            # 쥔 채 부르면 같은 락 파일을 두 fd 로 잡아 데드락이므로 락 밖에서 계획·위임한다(종전 동작:
            # 그 사본에서만 stale-plan 창이 남는다·부분 업그레이드 복구가 크래시보다 우선).
            updates = _upstream_rev_updates(pm_import, dest_root, rev)
            return pm_import._write_conf_keys(local_conf, updates), updates

        with _local_conf_write_lock(local_conf):
            if not local_conf.is_file():
                return _warn_missing_conf_for_rev(local_conf)
            updates = _upstream_rev_updates(pm_import, dest_root, rev)
            changed = pm_import._write_conf_keys_locked(local_conf, updates)
        return changed, updates
    except Exception as exc:  # noqa: BLE001 — 사본 skew 만 생략으로 내린다(그 밖은 종전대로 전파).
        if not _absorb_engine_rev_skew_for_recovery(exc, "record_upstream_revs.write"):
            raise
        print(f"경고: 엔진 사본 rev 혼합으로 upstream_rev 기록을 건너뛴다 ({exc}) — "
              "다음 pm-update 가 기록한다(엔진 파일 적용은 유지).", file=sys.stderr)
        return False, {}


def record_upstream_rev_baseline(dest_root: Path, source_root: Path) -> bool:
    """`record_upstream_revs` 의 변경-여부 전용 wrapper (시그니처 보존·기존 호출부/테스트)."""
    return record_upstream_revs(dest_root, source_root)[0]


def converge_upstream_revs(
    dest_root: Path, source_root: Path, skew_status: str, skew_new: list[str]
) -> bool:
    """skew 안전장치를 보존하며 sync 뒤 revision 키를 수렴·안내한다 (반환=이번 실행의 수렴 여부).

    반환값은 **엔진 사본이 상류 rev 로 수렴했는가** 하나다(`main` 종료 경로가 쓰는 그 판정 —
    실행당 1회 캐시). 미수렴이면 이 실행은 종료 rc 가 서는 실패 실행이므로, 호출부는 baseline
    기록(여기서 이미 억제)에 더해 **후속 opt-in 프롬프트도 건너뛴다** — 성공하지 않은 실행이
    사용자에게 새 질문을 던지고 그 답을 local.conf 에 적을 자리가 아니다(수렴한 뒤의 실행이
    묻는다). manifest skew 억제는 rc 축이 아니라 baseline 축이므로 그 판정과 독립이다."""
    if skew_status == "skew":
        print(
            f"→ manifest skew({len(skew_new)}건)로 upstream_rev baseline(+경로 upstream 의 "
            "upstream_seen_rev 관찰값) 갱신을 **억제**한다 — drift-lint 가 계속 이 skew 를 울리게 "
            "둔다. 로컬 engine.manifest 를 reconcile 한 뒤 다시 pm-update 하라(신규 등재분 "
            ")."
        )
        return _verify_engine_rev_convergence(dest_root, source_root)
    if not _verify_engine_rev_convergence(dest_root, source_root):
        # manifest skew 억제와 **같은 패턴**이다 — 사본 rev 가 상류로 수렴하지 않았는데 baseline 을
        #   박으면 "여기까지 흡수함" 이 되어 drift-lint 가 침묵한다(거짓 최신). 위 경고가 이미
        #   어긋난 사본을 지목했으므로 여기서는 억제 사실만 한 줄로 남긴다.
        print("→ 엔진 사본 rev 미수렴으로 upstream_rev baseline(+`upstream_seen_rev`) 갱신을 "
              "**억제**한다 — 수렴한 뒤의 실행이 기록한다.")
        return False

    # 안내 문구는 **엔진이 실제로 기록한 키**(recorded)로 정한다 — 파일의 결과 상태로
    # 역추론하면 URL 형상(스킬층이 쓴 seen 이 이미 baseline 과 같음)에서 "동시 기록" 이
    # 거짓으로 뜬다.
    changed, recorded = record_upstream_revs(dest_root, source_root)
    if changed:
        seen_note = " (+upstream_seen_rev 동시 기록)" if _SEEN_REV_KEY in recorded else ""
        print("✓ local.conf upstream_rev baseline 갱신 (drift-lint 기준점): "
              f"{recorded['upstream_rev']}{seen_note}")
    return True


def detect_manifest_skew(
    local_manifest: list,
    source_root: Path,
    *,
    upstream_manifest: Path | None = None,
    upstream_manifests: list[Path] | None = None,
) -> tuple[str, list[str]]:
    """upstream engine.manifest ↔ 로컬(sync 에 쓰인) manifest 대조 — 신규 등재분 탐지.

    로컬 manifest 가 구형이면 `pm_update` 는 로컬 등재분만 복사해 upstream 이 새로 등재한 엔진
    경로(신규 등재분)가 도달하지 않는데, upstream_rev baseline 은 무조건 최신으로 갱신돼
    drift-lint 가 "최신"으로 침묵한다(구형 identity_args 잔존 →
    pm_handoff AttributeError). 이 함수는 그 skew 를 **탐지만** 한다 — baseline 억제/경고는
    호출부(main)가, 신규 등재분 실제 도달(자기치유)은 이 맡는다(분리: 탐지는 무해).

    `local_manifest` 는 실 sync 가 쓴 manifest(resolve_manifest_for_dest 산출 — dest 우선·없으면
    source). 대조 upstream manifest 는 `upstream_manifests` 전체가 있으면 선언 순서 합집합을,
    아니면 `upstream_manifest` 인자(있으면)를, 둘 다 없으면 source_root 의 root engine.manifest 를
    읽는다. **flavor-correct 통일**selfheal 이 채택자 manifest 선언을 따라 선택 flavor 전체를
    해소하므로, main 은 *그 동일 경로 목록*을 넘겨 두 기전(탐지 / 승격)의 대조 기준을 정합시킨다.
    첫 manifest만 대조하면 diverged 로컬 + 후순위 flavor 신규 경로가 이번 실행에 도달하지 않아도
    in_sync로 오판하고 baseline이 전진한다.

    단일 flavor에서 self-prop `@source` 를 무시하면
    flavor 채택자가 치유 후에도 root-only 경로(`.claude/agents` 등)를 skew 오탐해 baseline 이 억제된다.
    인자 미주입(직접 호출·레거시)은 root 폴백(후방호환). 두 집합의 순수 경로(마커 제외·ManifestEntry
    가 이미 떼어냄·str(e))를 비교해 upstream 에만 있는 경로를 신규 등재분으로 본다 — 로컬에서 제거된
    경로(local−upstream)는 관심 밖(신규 도달 누락만 차단 대상).

    반환 (status, new_entries):
      - ('upstream_missing', []) : upstream engine.manifest 부재/읽기 실패(구 upstream) — fail-soft.
      - ('in_sync', [])          : 신규 등재분 0(정합) — baseline 갱신 진행.
      - ('skew', [<path>…])      : 로컬에 없는 upstream 등재 경로 존재 — baseline 억제 대상(정렬).
    """
    try:
        if upstream_manifests:
            upstream_entries = merge_manifest_sources(
                [Path(path) for path in upstream_manifests]
            )["entries"]
        else:
            if upstream_manifest is None:
                upstream_manifest = Path(source_root) / ".project_manager" / "engine.manifest"
            upstream_entries = read_manifest(upstream_manifest)
    except (FileNotFoundError, OSError, ValueError):
        return "upstream_missing", []
    local_paths = {str(e) for e in local_manifest}
    new_entries = sorted({str(e) for e in upstream_entries} - local_paths)
    return ("skew", new_entries) if new_entries else ("in_sync", [])


def _print_manifest_skew_finding(
    status: str, new_entries: list[str], *, dry_run: bool = False
) -> None:
    """detect_manifest_skew 결과를 사람이 읽을 형태로 출력(loud 경고).

    - 'skew'            : loud 경고 + 신규 등재 경로 목록(reconcile 필요 surface).
    - 'upstream_missing': fail-soft 경고 1줄(구 upstream·부재 — 대조 생략·현행 유지).
    - 'in_sync'         : dry-run 에서만 정합 표시(실 sync 는 조용히 baseline 갱신으로 진행).
    - 'skipped'         : 무출력 — --target(엔진 export) 경로는 skew 대조 비발화(현행 거동).

    baseline 억제/갱신 자체는 호출부(main)가 status 로 결정한다 — 이 함수는 출력만.
    """
    if status == "skew":
        print(
            f"⚠️  manifest skew — upstream engine.manifest 에 등재됐으나 로컬 manifest 에 없는 "
            f"신규 경로 {len(new_entries)}건(이번 sync 로 도달하지 않음·manifest reconcile 필요):"
        )
        for path in new_entries:
            print(f"    + {path}")
        print(
            "    참고: legacy 보존 모드에서는 대조 기준이 표준판이라 .claude/* 같은 무관 flavor "
            "경로가 포함될 수 있다."
        )
    elif status == "upstream_missing":
        print(
            "note: upstream engine.manifest 를 읽을 수 없어(구 upstream·부재) manifest 정합 "
            "대조를 건너뛴다(fail-soft·현행 유지)."
        )
    elif status == "in_sync" and dry_run:
        print("manifest 정합 — upstream 신규 등재분 0(baseline 갱신 진행 예정).")


# manifest self-prop 엔트리(채택자 engine.manifest 가 자기 자신을 전파 대상으로 등재한 행)의
# path — flavor-correct upstream 해소(resolve_manifest_selfheal)와 root 폴백의 단일 기준.
_MANIFEST_SELF_REL = ".project_manager/engine.manifest"


def _manifest_marker_key(entry) -> tuple:
    """ManifestEntry 의 마커 3종(@render/@target-owned/@source)을 비교키 튜플로 — 경로 집합만으론
    못 잡는 flavor 차이(예: `@source=templates/claude_code/...` vs bare)를 selfheal 이 감지하게 한다.

    평문 str 항목(레거시)은 getattr 폴백으로 (False, False, None)(마커 없음·후방호환).
    """
    return (
        bool(getattr(entry, "render", False)),
        bool(getattr(entry, "target_owned", False)),
        getattr(entry, "source_rel", None),
    )


def _selfprop_upstream_rel(local_entries: list) -> str:
    """채택자 로컬 manifest 의 self-prop 엔트리(`.project_manager/engine.manifest`)를 따라 flavor-correct
    upstream manifest 의 source-root 상대 *읽기* 경로를 낸다(codex MF).

    claude_code/opencode 채택자의 self-prop 는 `@source=templates/<harness>/.project_manager/
    engine.manifest` 라, 그 @source(=_source_root_rel)가 같은 flavor upstream manifest 를 가리킨다.
    self-prop 엔트리가 없거나 bare(@source 부재)면 root manifest(`_MANIFEST_SELF_REL`·현행 폴백).
    이로써 flavor↔flavor 비교가 성립해 root(bare) 승격이 flavor manifest 를 클로버하지 않는다.
    """
    for entry in local_entries:
        if str(entry) == _MANIFEST_SELF_REL:
            return _source_root_rel(entry)  # @source 있으면 그 경로·없으면 str(entry)=_MANIFEST_SELF_REL
    return _MANIFEST_SELF_REL


def _print_frozen_flavor_warning(
    flavor: str,
    observed: list[str],
    evidence_paths: list[str],
    *,
    declared_manifest: bool,
) -> None:
    """자동 승격할 수 없는 타 flavor 일부 관측을 동일 migration 절차로 안내한다."""
    cli_flavor = "claude" if flavor == "claude_code" else flavor
    if declared_manifest:
        lead = (
            "⚠️ 미등재 flavor 파일 관측 — @source 선택 선언이 있는 manifest와 "
            f"선언되지 않은 타 flavor({flavor}) 관리 경로 일부가 함께 관측됐다 "
        )
    else:
        lead = (
            "⚠️ 미등재 flavor 파일 관측 — @source 선언이 없는 legacy manifest와 "
            f"타 flavor({flavor}) 관리 경로 일부가 함께 관측됐다 "
        )
    print(
        lead
        + f"({len(observed)}/{len(evidence_paths)}: {', '.join(observed)}). "
        "관측 경로는 **어느 동기 채널도 선언하지 않은 출하 파일**이라 자동 자기치유하지 않는다 "
        "(`add-harness`로 얹은 어댑터는 guest 절이 렌더물·엔진 파일을 모두 등재하므로 이 경고 "
        "대상이 아니다). 남는 경우는 절 없이 손으로 복사된 flavor 트리, 사용자 stray 파일, "
        "아직 어느 flavor도 선언하지 않은 신규 출하 경로다.\n"
        f"    `add-harness {cli_flavor}`는 그 하네스 어댑터만 등재한다(엔진 전량 마이그레이션 아님).\n"
        "    완전 마이그레이션(등록 flavor 전체; 관측 누락에도 안전하나 원치 않는 flavor도 "
        "추가될 수 있으므로 dry-run 검토):\n"
        "      <manager>/pm-import.sh --into <project> --harness all --dry-run\n"
        "      <manager>/pm-import.sh --into <project> --harness all\n"
        "      cd <project> && ./pm-update.sh\n"
        "    재-import가 커스터마이즈된 CLAUDE.md/AGENTS.md를 템플릿 판으로 덮을 수 있으니, "
        "진입 문서 커스텀은 .pm_import_backups/<날짜>/ 백업에서 재병합하라.\n"
        f"    복구: 그 하네스를 정식으로 얹어 동기 채널을 만든다 — "
        f"`./pm-config.sh add-harness {cli_flavor}` (upstream 이 로컬 경로가 아니면 "
        f"`--from <프레임워크 checkout>` 추가; Windows 는 `.\\pm-config.cmd`).\n"
        "    해당 파일이 stray라면 이 경고를 무시해도 된다.",
        file=sys.stderr,
    )


def _flavor_exclusive_paths(entries: list, other_candidate_paths: set[str]) -> list[str]:
    """다른 어떤 후보 flavor 에도 없는 이 flavor **배타 경로** (선언 순서 보존).

    두 소비자가 공유한다(판정 사본 금지): frozen evidence(`_frozen_flavor_evidence`)와 legacy
    guest 절의 flavor provenance 추론(`_infer_guest_flavors`). 배타성이 판정의 핵심이라 단순
    namespace 매칭으로 대체할 수 없다 — 여러 flavor 가 함께 선언하는 cross-ns 경로(opencode 의
    `.claude/skills`)를 배타 증거로 세면 없던 flavor 를 인스턴스에 만들어 낸다."""
    return [
        str(entry).replace("\\", "/")
        for entry in entries
        if str(entry).replace("\\", "/") not in other_candidate_paths
    ]


def _frozen_flavor_evidence(
    entries: list,
    other_candidate_paths: set[str],
    local_core_paths: set[str],
    guest_paths: set[str],
) -> list[str] | None:
    """다른 모든 후보에는 없는 배타적 flavor 경로의 frozen evidence를 계산한다.

    guest 절이 소유한 경로는 add-harness refresh 채널이 전담하므로 evidence에서 **개별로** 뺀다.
    flavor 고유 경로가 전부 guest 소유일 때만 ``None``(그 flavor 전체가 refresh 채널 관할)이고,
    그 밖의 guest 밖 고유 경로는 남겨 frozen 판정에 쓴다. ``add_harness``는 항상 guest 행을
    등재하므로, guest 소유 하나로 flavor 전체를 버리면 frozen 상태에 도달하는 유일한 경로가 곧
    탐지기를 끈다(add-harness 채택자에게 경고가 구조적으로 발화하지 못함).
    """
    unique_paths = _flavor_exclusive_paths(entries, other_candidate_paths)
    unique_paths = [rel for rel in unique_paths if not _path_owned_by(rel, guest_paths)]
    if not unique_paths:
        return None
    return [rel for rel in unique_paths if rel not in local_core_paths]


def _print_legacy_nonmatch_warning(
    local_core_paths: set[str],
    observed_by_flavor: list[tuple[str, list[str], int]],
) -> None:
    """exact-match가 아닌 legacy 형상을 무변경 유지하며 완전 재-import 절차를 loud하게 낸다."""
    if observed_by_flavor:
        shapes = "; ".join(
            f"{flavor} {len(observed)}/{evidence_count}"
            + (f" ({', '.join(observed)})" if observed else "")
            for flavor, observed, evidence_count in observed_by_flavor
        )
    else:
        shapes = "배타적 flavor 경로 관측 0"
    print(
        "⚠️ legacy manifest 형상 — @source 선언이 없는 legacy engine.manifest의 "
        "core 경로 집합이 현행 flavor 후보 중 정확히 하나와 완전 일치하지 않는다. "
        f"관측 형상: 로컬 core {len(local_core_paths)}행; {shapes}.\n"
        "    로컬 manifest는 그대로 사용한다(자동 flavor 승격·행 제거·치유 0).\n"
        "    `add-harness`는 그 하네스 어댑터만 등재한다(엔진 전량 마이그레이션 아님).\n"
        "    완전 마이그레이션(등록 flavor 전체; 관측 0개·누락에도 안전하나 원치 않는 flavor도 "
        "추가될 수 있으므로 dry-run 검토):\n"
        "      <manager>/pm-import.sh --into <project> --harness all --dry-run\n"
        "      <manager>/pm-import.sh --into <project> --harness all\n"
        "      cd <project> && ./pm-update.sh\n"
        "    재-import가 커스터마이즈된 CLAUDE.md/AGENTS.md를 템플릿 판으로 덮을 수 있으니, "
        "진입 문서 커스텀은 .pm_import_backups/<날짜>/ 백업에서 재병합하라.\n"
        "    관측 파일이나 manifest 행이 사용자 stray/커스텀이면 이 경고를 무시해도 된다.",
        file=sys.stderr,
    )


def _selected_upstream_manifests(
    effective_dest: Path,
    source_root: Path,
    local_entries: list,
    local_text: str,
    guest_backfill_paths: set[str] | None = None,
) -> tuple[list[Path], bool]:
    """채택자에 실제 설치된 flavor들의 upstream manifest를 선택 순서대로 해소한다.

    설치 manifest의 ``@source=templates/<flavor>/...`` 선언이 있으면 그 flavor 집합만 신뢰한다.
    파일 존재는 사용자 파일/PM 홈의 어댑터 사본과 구별할 수 없으므로 선언이 하나라도 있으면
    존재-휴리스틱을 전혀 타지 않는다. 선언 순서는 manifest 행의 최초 출현 순서이고, 첫 flavor
    우선 + 후속 선언 순서라는 합집합 우선순위를 그대로 보존한다.

    ``@source`` flavor 선언이 전혀 없는 구 manifest는 로컬 core 경로 집합이 **정확히 한 후보와
    완전 일치**할 때만 그 후보를 primary로 고른다. 부분집합, 존재 경로, 은퇴 행 추정, 최소
    초과집합/tiebreak는 사용하지 않는다. 완전 일치가 아니면 로컬 manifest를 그대로 계획에
    사용하고, frozen/stray를 구분할 수 없다는 진단과 검증된 완전 재-import 절차만 낸다.

    선언이 하나라도 있으면 존재-휴리스틱에 의한 자동 선택은 비발화한다. 선언되지 않은 flavor의
    관리-고유 경로 일부가 보이면 같은 frozen/stray 마이그레이션 경고만 내고 자동 승격하지 않는다.
    선언된 후순위 manifest가 부재해도 선택 목록에서 버리지 않아 호출부가 로컬 union을 유지하며
    경고하고, 해소 불가 선언은 primary만 유지한 채 경고한다.

    add-harness guest 절은 별도 refresh 채널이므로 core 합집합으로 승격하지 않는다. guest가 소유한
    경로만 존재해 후보가 된 flavor는 제외해 기존 add-harness 불가침 계약을 유지한다.

    ``guest_backfill_paths``(이번 실행이 파생한 guest 엔진 행)도 그 guest 소유 집합에 합친다. 절
    텍스트만 보면 legacy 코호트 **첫 실행**에서 같은 run 이 "이 파일들은 어떤 채널도 없다" 고 경고한
    직후 그 파일들을 등재·동기하는 자기모순 출력이 난다 — 파생이 실제로 채널을 만드므로 경고 대상이
    아닌 게 참이다. 파생 경로는 host core 를 차감하고 뽑히므로 core 판정과 겹치지 않는다.
    """
    source_root = Path(source_root)
    primary = source_root / _selfprop_upstream_rel(local_entries)
    candidates = sorted(
        source_root.glob("templates/*/.project_manager/engine.manifest"),
        key=lambda p: p.as_posix(),
    )
    candidate_by_flavor = {
        candidate.parents[1].name: candidate for candidate in candidates
    }
    guest_block = _extract_guest_manifest_block(local_text)
    guest_paths = {
        ln.split()[0].replace("\\", "/")
        for ln in guest_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    } if guest_block else set()
    guest_paths |= {
        str(path).replace("\\", "/") for path in (guest_backfill_paths or ())
    }
    local_core_paths = {
        str(entry).replace("\\", "/")
        for entry in local_entries
        if not _path_owned_by(str(entry).replace("\\", "/"), guest_paths)
    }

    primary_flavor = next(
        (flavor for flavor, candidate in candidate_by_flavor.items() if candidate == primary),
        None,
    )
    source_declarations = [
        getattr(entry, "source_rel", None)
        for entry in local_entries
        if not _path_owned_by(str(entry).replace("\\", "/"), guest_paths)
        and getattr(entry, "source_rel", None)
    ]
    declared_flavors: list[str] = []
    unresolved_declarations: list[str] = []
    for source_rel in source_declarations:
        parts = Path(source_rel.replace("\\", "/")).parts
        if (
            len(parts) >= 3
            and parts[0] == "templates"
            and parts[1] not in {"", ".", ".."}
            and (source_root / "templates" / parts[1]).is_dir()
        ):
            flavor = parts[1]
            if flavor not in declared_flavors:
                declared_flavors.append(flavor)
        elif not (
            # flavor 선택과 무관한 bare source remap은 source가 실제 해소될 때만 유효하다.
            not source_rel.replace("\\", "/").startswith("templates/")
            and (source_root / source_rel).exists()
        ):
            unresolved_declarations.append(source_rel)
    if source_declarations and (unresolved_declarations or not declared_flavors):
        unresolved = ", ".join(dict.fromkeys(
            unresolved_declarations or source_declarations
        ))
        print(
            "경고: engine.manifest에 해소할 수 없는 @source 선언이 있어 legacy 존재-휴리스틱을 "
            f"사용하지 않는다: {unresolved}. 선언된 primary manifest만 유지한다.",
            file=sys.stderr,
        )
        return [primary], False
    candidate_entries: dict[Path, list] = {}
    for candidate in candidates:
        try:
            candidate_entries[candidate] = read_manifest(candidate)
        except (OSError, UnicodeError, ValueError) as exc:
            print(
                f"note: legacy flavor 후보 manifest를 읽을 수 없어 제외한다(fail-soft): "
                f"{candidate} ({exc})",
                file=sys.stderr,
            )
    candidate_paths = {
        candidate: {
            str(entry).replace("\\", "/") for entry in entries
        }
        for candidate, entries in candidate_entries.items()
    }
    if declared_flavors:
        # root manifest의 flavor @source는 선택 provenance가 아니라 canonical remap이다. self-prop가
        # template flavor를 가리키는 설치 manifest에서만 선언 집합을 선택 집합으로 해석한다.
        if primary_flavor is None:
            return [primary], False
        ordered_flavors = [primary_flavor, *(
            flavor for flavor in declared_flavors if flavor != primary_flavor
        )]
        selected_declared: list[Path] = []
        for flavor in ordered_flavors:
            candidate = (
                source_root / "templates" / flavor / ".project_manager" / "engine.manifest"
            )
            selected_declared.append(candidate)
            if not candidate.is_file():
                print(
                    "경고: engine.manifest가 선언한 후순위 flavor의 upstream manifest가 없다 — "
                    f"선언을 버리지 않고 로컬 union을 유지한다: {flavor} ({candidate}). "
                    "누락 source가 있으면 apply 전에 중단된다.",
                    file=sys.stderr,
                )
        declared_set = set(ordered_flavors)
        for candidate, entries in candidate_entries.items():
            flavor = candidate.parents[1].name
            if flavor in declared_set:
                continue
            other_paths = set().union(*(
                paths for other, paths in candidate_paths.items()
                if other != candidate
            ))
            evidence_paths = _frozen_flavor_evidence(
                entries, other_paths, local_core_paths, guest_paths)
            if evidence_paths is None:
                continue
            observed = [
                rel for rel in evidence_paths
                if (Path(effective_dest) / rel).exists()
            ]
            if observed:
                _print_frozen_flavor_warning(
                    flavor,
                    observed,
                    evidence_paths,
                    declared_manifest=True,
                )
        return selected_declared, False

    if not candidates:
        return [primary], False
    if not candidate_entries:
        return [primary], False

    exact_matches = [
        candidate for candidate, paths in candidate_paths.items()
        if paths == local_core_paths
    ]
    legacy_primary = (
        exact_matches[0]
        if (
            len(exact_matches) == 1
            and Path(effective_dest).resolve() != source_root.resolve()
        )
        # framework checkout의 root manifest가 우연히 template과 같은 경로 집합이어도 root가 primary다.
        # template provenance 복원은 source와 분리된 legacy adopter에서만 필요하다.
        else None
    )
    if legacy_primary is not None:
        for candidate, entries in candidate_entries.items():
            if candidate == legacy_primary:
                continue
            other_paths = set().union(*(
                paths for other, paths in candidate_paths.items()
                if other != candidate
            ))
            evidence_paths = _frozen_flavor_evidence(
                entries, other_paths, local_core_paths, guest_paths)
            if evidence_paths is None:
                continue
            observed = [
                rel for rel in evidence_paths
                if (Path(effective_dest) / rel).exists()
            ]
            if observed:
                _print_frozen_flavor_warning(
                    candidate.parents[1].name,
                    observed,
                    evidence_paths,
                    declared_manifest=False,
                )
        return [legacy_primary], False

    if Path(effective_dest).resolve() == source_root.resolve():
        return [primary], False

    observed_by_flavor: list[tuple[str, list[str], int]] = []
    for candidate, entries in candidate_entries.items():
        other_paths = set().union(*(
            paths for other, paths in candidate_paths.items()
            if other != candidate
        ))
        evidence_paths = _frozen_flavor_evidence(
            entries,
            other_paths,
            local_core_paths,
            guest_paths,
        )
        if evidence_paths is None:
            continue
        observed = [
            rel for rel in evidence_paths
            if (Path(effective_dest) / rel).exists()
        ]
        if observed:
            observed_by_flavor.append((
                candidate.parents[1].name, observed, len(evidence_paths)))
    _print_legacy_nonmatch_warning(local_core_paths, observed_by_flavor)
    return [primary], True


def resolve_manifest_selfheal(
        effective_dest: Path, source_root: Path,
        *, guest_backfill_paths: set[str] | None = None) -> dict:
    """self-update manifest 자기치유 (2-pass 단일 실행) — upstream engine.manifest 를
    이번 sync 의 **계획 기준 manifest 로 승격**해 신규 등재분을 한 번의 실행으로 도달시킨다.

    채택자가 bare `pm-update`/CLI 로 흡수할 때, 로컬 engine.manifest 가 구형이면
    resolve_manifest_for_dest 가 그 구형 로컬 manifest 를 집어 plan 이 신규 등재 경로(upstream 이
    새로 등재한 엔진 파일)를 아예 안 실었다 — 다음 sync 전까진 영영 미도달(
    구 manifest·pm_handoff identity_args 미등재 → AttributeError·손 manifest 교체로만 복구). 이
    함수는 upstream manifest 를 plan 기준으로 승격한다. manifest 자신도 self-prop 엔트리(
    upstream 항상 등재)라 같은 plan 안에서 로컬 manifest 파일이 upstream 판으로 apply 된다 —
    별도 write 없이 정상 순서(missing-check 후·실 apply 시·dry-run 무부작용)에서 갱신된다.

    **flavor-correct upstream 해소** (`@source` self-prop): 비교/승격 대상 upstream
    manifest 는 root(`source_root/.project_manager/engine.manifest`·claude-scoped·bare)가 아니라 채택자
    self-prop 엔트리의 `@source` 를 따라간 *같은 flavor* manifest 다. claude_code/opencode 채택자의
    self-prop 는 `.project_manager/engine.manifest @source=templates/<harness>/.project_manager/
    engine.manifest` 라, 이를 무시하고 root 를 승격하면 flavor manifest(@source 마커 보유)를 root(bare)로
    **클로버**해 하네스-특정 remap 구조를 깬다. self-prop 의 @source(=`_source_root_rel`)로 flavor upstream
    을 읽어 flavor↔flavor 로 비교하면 마커가 정합하고 신규 등재분만 승격된다. self-prop 부재/bare 는
    root 로 폴백(현행).

    ("manifest 진화=스킬 reconcile·self-list 아님")의 통제-상실 우려(채택자 로컬 manifest
    커스텀 제외)는 **전체 교체 + diff loud 표시**로 대체한다(자동 병합 안 함·호출부가 표시).
    flavor upstream manifest 부재/읽기 실패면 fail-soft(로컬 유지·plan 무변경)
    baseline 억제가 그 잔여 경로 안전망이다. --target(엔진 export)은 호출하지 않는다(타깃
    manifest 가 루트와 의도적으로 다름·skew 검출과 동일 경계).

    반환 dict:
      - status  : 'upstream_missing'(flavor upstream 부재·fail-soft) | 'no_local'(로컬 manifest 부재·
                  이미 source manifest 기준) | 'in_sync'(로컬==upstream 또는 경로/선언 동일·무변경) |
                  'diverged'(로컬-전용 경로 또는 공통 경로 마커/@source divergence=커스텀 편집·승격
                  안 함·안전망) | 'legacy_preserved'(후보 exact-match 없음·로컬 manifest 불가침) |
                  'heal'(upstream 신규 등재 또는 exact-match legacy provenance 승격)
      - added   : flavor upstream 에만 있는 순수 경로(신규/재-등재·정렬) — 'heal' 이면 이번 sync 로 도달
      - removed : 로컬 manifest 에만 있던 순수 경로('diverged' 판정 근거·정렬)
      - manifest: plan 이 쓸 ManifestEntry 리스트 — 'heal' 이면 flavor upstream_entries, 그 외 None
                  (None 이면 호출부가 resolve_manifest_for_dest 산출 로컬 manifest 를 그대로 쓴다).
      - upstream_manifest: 대조에 쓴 flavor-correct upstream engine.manifest **Path** — 호출부(main)가
                  이 경로를 detect_manifest_skew 에 그대로 넘겨 두 기전의 대조 기준을 flavor 로 정합시킨다
                  (). 로컬 manifest 부재('no_local')는 self-prop 이 없어 root 폴백 경로.
    """
    dest_manifest = Path(effective_dest) / ".project_manager" / "engine.manifest"
    root_manifest = Path(source_root) / ".project_manager" / "engine.manifest"
    if not dest_manifest.exists():
        # 로컬 manifest 부재(fresh/구 import) — resolve_manifest_for_dest 가 이미 source manifest 를
        #   집으므로 plan 이 upstream 기준(신규 등재 포함)으로 돈다. 승격 불요(무변경·현행). self-prop
        #   이 없어 skew 대조는 root(=resolve 산출과 동일) 로 정합.
        return {"status": "no_local", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": root_manifest,
                "upstream_manifests": [root_manifest], "manifest_text": None,
                "merge_conflicts": []}
    local_entries = read_manifest(dest_manifest)
    local_text = dest_manifest.read_text(encoding="utf-8")
    if guest_backfill_paths is None:
        # 호출부가 이미 해소했으면 넘긴다(중복 IO·pm_import 재로드 회피). 안 넘기면 여기서 해소해
        #   진단 판정이 호출 경로마다 갈리지 않게 한다.
        backfill, _flavors = _guest_engine_backfill_entries(
            effective_dest, source_root,
            _dest_guest_manifest_entries(effective_dest))
        guest_backfill_paths = {str(entry).replace("\\", "/") for entry in backfill}
    # 설치된 flavor들의 upstream manifest 합집합. 첫 항목은 self-prop가 지정한 기존 flavor이고,
    # 추가 항목은 설치 manifest의 flavor provenance로 일반화해 발견한다(고정 조합 손-끼워넣기 없음).
    upstream_manifest = Path(source_root) / _selfprop_upstream_rel(local_entries)
    upstream_manifests: list[Path] = [upstream_manifest]
    try:
        upstream_manifests, legacy_preserved = _selected_upstream_manifests(
            effective_dest, source_root, local_entries, local_text,
            guest_backfill_paths)
        upstream_manifest = upstream_manifests[0]
        if legacy_preserved:
            return {
                "status": "legacy_preserved",
                "added": [],
                "removed": [],
                "manifest": None,
                "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": None,
                "merge_conflicts": [],
            }
        merged_upstream = merge_manifest_sources(upstream_manifests)
        upstream_text = merged_upstream["text"]
        upstream_entries = merged_upstream["entries"]
    except (FileNotFoundError, OSError, UnicodeError, ValueError):
        # flavor upstream 읽기 실패 — skew 대조도 같은 경로를 넘겨 upstream_missing 으로 정합(fail-soft).
        return {"status": "upstream_missing", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests, "manifest_text": None,
                "merge_conflicts": []}
    # add-harness guest 절(로컬-전용 `@target-owned` guest)은 **core 비교에서 제외**한다:
    #   섞으면 항상 removed 비어있지 않아 영구 diverged → upstream 신규 항목 자기치유(승격) 불능. 절은
    #   apply 가 재부착하므로(대칭·`_copy_manifest_preserving_guest`) 승격돼도 잔존한다. 판정 사본 없이
    #   추출 헬퍼를 재사용해 in_sync 판정도 core 로(strip==upstream), 경로 집합도 core 로 좁힌다.
    guest_block = _extract_guest_manifest_block(local_text)
    guest_paths = {
        ln.split()[0] for ln in guest_block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")} if guest_block else set()
    if _strip_guest_manifest_block(local_text) == upstream_text:
        return {"status": "in_sync", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    core_local_entries = [
        e for e in local_entries
        if str(e).replace("\\", "/") not in guest_paths
    ]
    # 경로 + 마커(@render/@target-owned/@source) 동시 비교 — 경로 집합만 보면 flavor manifest 의
    #   @source self-prop 을 root bare 로 덮는 클로버를 못 잡는다(codex MF). 다만 @source 자체가
    #   전혀 없던 legacy manifest는 source provenance 추가가 바로 치유 목적이므로 source_rel 차이만
    #   허용한다(render/target-owned 편집은 계속 divergence). 신규 선언 manifest의 공통 경로 마커가
    #   하나라도 갈리면(로컬 커스텀 편집·잘못된 flavor 대조) 승격하지 않는다.
    local_markers = {str(e): _manifest_marker_key(e) for e in core_local_entries}
    upstream_markers = {str(e): _manifest_marker_key(e) for e in upstream_entries}
    added = sorted(set(upstream_markers) - set(local_markers))
    removed = sorted(set(local_markers) - set(upstream_markers))
    legacy_without_source_provenance = not any(
        getattr(entry, "source_rel", None) for entry in core_local_entries
    )
    provenance_divergent = sorted(
        p for p in (set(local_markers) & set(upstream_markers))
        if local_markers[p][2] != upstream_markers[p][2]
    )
    marker_divergent = sorted(
        p for p in (set(local_markers) & set(upstream_markers))
        if (
            local_markers[p][:2] != upstream_markers[p][:2]
            if legacy_without_source_provenance
            else local_markers[p] != upstream_markers[p]
        )
    )
    if removed or marker_divergent:
        # 로컬-전용 경로 또는 공통 경로 마커 divergence = 로컬이 flavor upstream 의 단순 부분집합이
        #   아니다(채택자 커스텀 편집·마커 손질). 전체 교체하면 그 커스텀/구조를 클로버하므로 승격하지
        #   않고 현행 로컬 manifest 를 유지한다. upstream 신규 등재분은 skew 대조가
        #   surface 한다(안전망). "항목 제외" 커스텀(로컬⊂upstream·마커 정합)은 아래 heal 로 전체 교체.
        return {"status": "diverged", "added": added, "removed": removed,
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    if not added and not (
        legacy_without_source_provenance and provenance_divergent
    ):
        # 경로/마커 동일(주석만 차이) — 도달할 신규 등재 경로 0. manifest 자신도 self-prop 엔트리라
        #   plan 이 파일은 갱신한다(승격 불요). in_sync 로 취급(baseline 갱신 진행).
        return {"status": "in_sync", "added": [], "removed": [],
                "manifest": None, "upstream_manifest": upstream_manifest,
                "upstream_manifests": upstream_manifests,
                "manifest_text": upstream_text,
                "merge_conflicts": merged_upstream["conflicts"]}
    # 로컬 ⊂ upstream(removed 0·마커 정합·added>0), 또는 경로는 같지만 @source가 전무한 legacy
    # manifest — flavor upstream 을 계획 기준으로 승격해 신규 경로와 source provenance를 같은
    # sync에서 도달시킨다. provenance-only 승격은 bare opencode 경로를 source root에서 찾는 rc=2도
    # 막는다.
    return {"status": "heal", "added": added, "removed": [],
            "manifest": upstream_entries, "upstream_manifest": upstream_manifest,
            "upstream_manifests": upstream_manifests,
            "manifest_text": upstream_text,
            "merge_conflicts": merged_upstream["conflicts"],
            "multi_flavor_recovery": len(upstream_manifests) > 1,
            "provenance_upgrade": bool(
                legacy_without_source_provenance and provenance_divergent
            ),
            "legacy_manifest": legacy_without_source_provenance}


def _print_manifest_selfheal_finding(selfheal: dict, *, dry_run: bool = False) -> None:
    """resolve_manifest_selfheal 결과를 사람이 읽을 형태로 출력(loud diff).

    - 'heal'            : loud — upstream manifest 를 계획 기준으로 승격(전체 교체·자동 병합 없음).
                          upstream 이 새로/재-등재한 경로(+·이번 sync 로 도달)를 표시한다. 로컬 ⊂
                          upstream 이 승격 조건이라 로컬-전용 제거분은 없다(있으면 'diverged').
    - 'upstream_missing': 무출력 — skew 대조가 이어서 fail-soft note 를 낸다(중복 회피).
    - 'diverged'/'in_sync'/'no_local'/'skipped': 무출력 — 'diverged'(로컬-전용 경로=다른 하네스/커스텀)는
                          승격 안 하고 skew 대조에 맡긴다(중복 회피).

    승격 자체는 호출부(main)가 plan manifest 를 교체해 수행 — 이 함수는 출력만.
    """
    if selfheal.get("status") != "heal":
        return
    added = selfheal["added"]
    verb = "자기치유 예정" if dry_run else "자기치유"
    if selfheal.get("multi_flavor_recovery"):
        flavors = [
            path.parents[1].name
            for path in selfheal.get("upstream_manifests", [])
            if len(path.parents) >= 2
        ]
        print(
            "⚠️ 설치된 다중 하네스의 manifest 누락(frozen adapter) 감지 — 이 adapter 경로들은 "
            "설치 manifest 밖이라 그동안 pm_update 갱신이 정지돼 있었다. "
            f"선택 flavor 합집합으로 {verb}: {', '.join(flavors)}. "
            "복구를 원치 않으면 해당 어댑터 트리를 제거하라."
            + (
                " engine.manifest의 그 flavor @source 선언도 정리하라."
                if not selfheal.get("legacy_manifest")
                else ""
            )
        )
    if selfheal.get("provenance_upgrade") and not added:
        print(
            f"→ engine.manifest {verb} — 관리 경로는 같지만 @source provenance 선언을 "
            "upstream 형식으로 승격한다."
        )
        return
    print(
        f"→ engine.manifest {verb} — upstream manifest 를 계획 기준으로 승격 "
        f"(선택 flavor 합집합·선언 순서 우선): 신규 등재 +{len(added)}"
    )
    for path in added:
        print(f"    + {path}  (upstream 신규/재-등재 — 이번 sync 로 도달)")


def _print_manifest_merge_conflicts(selfheal: dict) -> None:
    """다중 flavor 합집합의 마커 충돌을 pm_import와 대칭으로 stderr에 표면화한다."""
    conflicts = selfheal.get("merge_conflicts", [])
    if not conflicts:
        return
    print(
        "경고: 선택 manifest 중복 경로의 마커/@source 불일치 — 선언 순서상 첫 flavor를 "
        f"우선함 ({len(conflicts)}건): {', '.join(conflicts)}",
        file=sys.stderr,
    )


def _selected_upstream_core_paths(selfheal: dict) -> set[str]:
    """이번 실행에서 실제 승격된 selfheal manifest의 core 경로 집합.

    ``selfheal["manifest"]``는 heal 판정에서만 채워진 선택 flavor 합집합이다. 후보 upstream
    manifest를 직접 다시 읽으면 diverged/in_sync처럼 승격하지 않은 실행에서도 guest 보호를
    해제한다. 실제 plan 기준으로 승격된 엔트리만 반환해 후순위 flavor의 1-run 승격은 유지하면서
    미승격 상태의 guest는 계속 보호한다.
    """
    entries = selfheal.get("manifest") or []
    return {str(entry).replace("\\", "/") for entry in entries}


def _template_dir_from_manifest(
    manifest_path: Path | None,
    source_root: Path,
) -> str | None:
    """flavor manifest 경로에서 `templates/<dir>` context를 파생한다. root manifest면 None."""
    if manifest_path is None:
        return None
    try:
        rel = Path(manifest_path).resolve().relative_to(
            (Path(source_root) / "templates").resolve()
        )
    except (OSError, ValueError):
        return None
    return rel.parts[0] if len(rel.parts) >= 3 else None


def _entry_notation_templates_from_manifests(
    manifest_paths: list[Path],
    source_root: Path,
) -> dict[str, tuple[str, ...]]:
    """선택 manifest의 항목별 flavor 집합을 선언 순서대로 보존한다.

    서로 다른 경로는 각 flavor 하나를 받고, 같은 물리 경로는 그 파일을 실제로 읽는 flavor를
    전부 받는다. 선택된 flavor manifest를 읽지 못하면 context만 조용히 버리지 않고 fail-loud한다.
    로컬 manifest 동기화는 계속하면서 notation context만 유실하면 canonical slash가 codex의 기존
    dollar 산출물을 덮기 때문이다.
    """
    contexts: dict[str, list[str]] = {}
    for manifest_path in manifest_paths:
        template_dir = _template_dir_from_manifest(manifest_path, source_root)
        if template_dir is None:
            continue
        try:
            entries = read_manifest(manifest_path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                "선택된 flavor manifest에서 스킬 표기 context를 해소할 수 없다 — "
                f"기존 렌더 산출물을 보존하기 위해 동기화를 중단한다: {manifest_path} ({exc})"
            ) from exc
        for entry in entries:
            rel = str(entry).replace("\\", "/")
            flavors = contexts.setdefault(rel, [])
            if template_dir not in flavors:
                flavors.append(template_dir)
    return {rel: tuple(flavors) for rel, flavors in contexts.items()}


def _installed_entry_notation_manifests(
    effective_dest: Path,
    source_root: Path,
    upstream_manifests: list[Path],
) -> list[Path]:
    """core 선택에 더해 실제 설치된 add-harness guest를 표기 context에만 포함한다.

    guest는 update plan/manifest self-heal에서는 계속 불가침이다. 다만 공유 wiki를 실제로 읽는
    하네스 집합에서는 빠지면 안 되므로, 설치 하네스를 찾아 그 flavor manifest를 context 입력에만
    보탠다.

    판정은 **pm_import.installed_harnesses 단일 진실**을 쓴다(구조 판정 사본 금지). 어댑터
    디렉터리+root-doc 실재만 보면 일반 프로젝트가 자기 용도로 가진 `.codex/`·`AGENTS.md`를 codex
    PM 설치로 오인해, PM 카드가 없는 인스턴스의 공유 wiki에 codex 표기(`$pm-bootstrap`)를 주입한다
    — pm_import(최초 설치·add-harness)와 pm_update(자기 갱신)가 같은 판정을 써야 표기가 어긋나지
    않는다.
    """
    paths = list(upstream_manifests)
    try:
        pm_import = _load_pm_import()
        harnesses = pm_import.installed_harnesses(effective_dest, source_root)
        template_registry = pm_import.HARNESS_TEMPLATE_DIRS
    except Exception as exc:  # noqa: BLE001 — pm_import 부재·손상은 core sync 우선.
        # 사본 rev 혼합도 등록된 경계로 흡수한다 — 이 판별은 **계획 수립 전**에 형제
        #   pm_import → repo_owned_files(verifier) 계층까지 들어가므로, 올리면 혼합 트리에서 돌린
        #   pm-update 가 자기 복구 경로를 잃는다(v1.7.0 흡수 실측 지점). 표기 context 는 core
        #   manifest 만으로도 성립하므로 guest 보탬만 포기하고 동기를 계속한다.
        _absorb_engine_rev_skew_for_recovery(exc, "installed_entry_notation_manifests")
        return paths
    seen = {Path(path).resolve() for path in paths}
    for harness in harnesses:
        template_dir = template_registry[harness][0]
        manifest = (
            Path(source_root) / "templates" / template_dir
            / ".project_manager" / "engine.manifest"
        )
        if not manifest.is_file():
            continue
        resolved = manifest.resolve()
        if resolved not in seen:
            paths.append(manifest)
            seen.add(resolved)
    return paths


# local.conf key(lowercase) → operational token key(uppercase·pm_render). board.py init 은
# py·test_cmd·project_name 만 기록 — 나머지(project_root·project_tagline·date)는 local.conf
# 에 없으므로 매핑 부재 시 빈값(render 시 그 토큰이 남아있으면 leak assertion 이 잡는다·그러나
# 출하 어댑터의 operational 토큰은 import sed 로 이미 리터럴이라 render 시점엔 보통 부재 → no-op).
_LOCAL_CONF_TO_OPERATIONAL = {
    "project_name": "PROJECT_NAME",
    "project_tagline": "PROJECT_TAGLINE",
    "project_root": "PROJECT_ROOT",
    "py": "PY",
    "test_cmd": "TEST_CMD",
    "date": "DATE",
    # opencode 어댑터 전용 — pm_import 가 import 시 local.conf 에 기록(모델 해소 시만).
    # self-update 의 @source 재렌더가 `.opencode/agents` 를 렌더할 때 이 매핑으로
    # local.conf 재유도. **미해소**(opencode 없이 import 한 채택자·local.conf 에 opencode_pro_model
    # 부재)면 render_adapter 가 leak 으로 rc-fail 하지 않고 intentional-TODO 로 graceful 중화한다
    # (pm_render.neutralize_model_todo·import 대칭) — 한 토큰 미해소가 엔진/타 어댑터 update
    # 전체를 막지 않는다(부분-graceful). claude tree 엔 토큰 부재 → no-op.
    "opencode_pro_model": "OPENCODE_PRO_MODEL",
}


def _operational_from_local_conf(dest_root: Path) -> tuple[dict[str, str], list[str]]:
    """local.conf 의 operational 해소값을 pm_render 의 token-key dict 로 변환.

    local.conf 키(lowercase) → operational token key(uppercase). board.py init 이 안 쓴 키는
    포함하지 않는다(빈값 강제 안 함). 출하 어댑터의 operational 토큰은 import sed 로 이미
    리터럴이라 render 시점엔 보통 부재 — 이 매핑은 재렌더가 그 토큰을 만났을 때 local.conf
    단일 진실로 재유도하기 위한 것().

    **값이 빈 문자열인 키도 dict 에서 제외**한다(부재와 동일 취급) — 빈값을 그대로
    넘기면 렌더가 토큰을 빈 문자열로 silent 치환해(예: `project_name=` 빈값 → description 이
    " 프로젝트") 탐지 신호가 사라진다. 제외하면 토큰이 잔존해
    render 의 _assert_no_leak 가 leak 으로 잡는다(silent-empty = leak 클래스). 제외된 빈값
    token-key 목록을 함께 반환해 render_adapter 가 leak 힌트("값을 채우라")에 싣게 한다.

    반환: (operational dict, 빈값이라 제외된 token-key 목록).
    """
    conf = _read_local_conf(dest_root / ".project_manager" / "local.conf")
    operational: dict[str, str] = {}
    empty_keys: list[str] = []
    for conf_key, token_key in _LOCAL_CONF_TO_OPERATIONAL.items():
        if conf_key not in conf:
            continue
        if conf[conf_key] == "":
            empty_keys.append(token_key)
            continue
        operational[token_key] = conf[conf_key]
    return operational, empty_keys


def _render_text(
    source_path: Path,
    dest_root: Path,
    entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
) -> str:
    """source 템플릿을 채택자 local.conf(operational)로 렌더한 텍스트.

    local.conf 의 operational 값을 plain replace 로 채운다(free-form 은 pm_import FILL 채널이
    canonical home 에서 전담). 결과는 자족(잔여 `{{...}}` 0·assertion).
    호출부(apply/plan)가 dst 와 비교/기록한다.
    """
    render_mod = _load_pm_render()
    operational, empty_keys = _operational_from_local_conf(dest_root)
    text = Path(source_path).read_text(encoding="utf-8")
    return render_mod.render_adapter(
        text,
        operational=operational,
        empty_keys=empty_keys,
        template_dir=entry_notation_template,
        source=str(source_path),
    )


def render_skill_entry_notation(
    text: str,
    template_dir: str | tuple[str, ...] | list[str],
    *,
    source: str | None = None,
) -> str:
    """호출 표기 최소 렌더 public seam — board mirror 판정도 같은 경로를 재사용한다."""
    render_mod = _load_pm_render()
    return render_mod.render_skill_entry_notation(
        text, template_dir, source=source
    )


def _render_skill_entry_text(
    source_path: Path,
    template_dir: str | tuple[str, ...] | list[str],
) -> str:
    """--target export용 파일 wrapper — operational은 건드리지 않고 호출 표기만 치환."""
    text = Path(source_path).read_text(encoding="utf-8")
    return render_skill_entry_notation(
        text, template_dir, source=str(source_path)
    )


def _is_text_source(source_path: Path) -> bool:
    """source 가 UTF-8 텍스트로 읽히는가 — render 대상 판정의 유일한 형식 조건.

    옛 `.md` 확장자 열거를 대체한다: 확장자는 열린 집합(하니스가 새 형식을 들여온다)이라 열거하면
    새 형식이 조용히 미커버로 남는다(codex `.toml`). render 가 실제로 요구하는 건 "텍스트로 읽어
    plain replace 할 수 있는가" 뿐이므로 그것만 본다 — 바이너리 리소스는 False → byte-copy.
    IO 실패도 보수적으로 False(byte-copy·기존 동작).
    """
    try:
        Path(source_path).read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return False
    return True


def _render_eq_dst(
    sp: Path,
    dst: Path,
    dest_root: Path,
    entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
) -> bool:
    """render path 의 '변경 없음' 정직 판정 — 렌더 산출물 == dst 현재 내용 ().

    filecmp.cmp(템플릿, dst) 는 render path 에 *틀림*(템플릿은 렌더 산출물과 byte-equal 일 수
    없어 항상 update 오보). 대신 source 를 dest 의 local.conf(operational)로 렌더해 dst 와 비교한다.
    렌더 실패(렌더러 부재·assertion)는 보수적으로 '다름'(False) 취급 — plan 이 그 path 를
    change 로 띄워 apply 가 실제 렌더에서 명확히 실패하게 한다(침묵 폴백 금지).
    """
    try:
        rendered = _render_text(sp, dest_root, entry_notation_template)
        # newline=None인 read_text는 CRLF를 LF로 정규화해 byte drift를 숨긴다. 산출물 encoding을
        # 명시해 결정적 bytes로 대조하면 LF/CRLF 차이와 비-UTF8 dest를 모두 update로 판정한다.
        return rendered.encode("utf-8") == dst.read_bytes()
    except Exception:  # noqa: BLE001 — 렌더/IO 실패는 '다름'으로 보수 처리.
        return False


def plan(
    source_root: Path,
    manifest: list,
    dest_root: Path | None = None,
    *,
    render_enabled: bool = True,
    entry_notation_template: str | tuple[str, ...] | list[str] | None = None,
    entry_notation_templates: dict[str, tuple[str, ...]] | None = None,
    manifest_source_text: str | None = None,
    inventory_out: set[str] | None = None,
    dest_inventory_out: set[str] | None = None,
    guest_backfill_lines: list[str] | None = None,
) -> tuple[list[tuple], list[str]]:
    """(changes, missing) 반환. changes = [(rel, src, dst, kind)] (kind: new|update).

    dest_root: 동기화 대상 루트. None 이면 REPO(self-location) 사용.

    manifest 항목이 `ManifestEntry`(render 플래그 운반·read_manifest 산출)면 그 path 의 render
    여부를 dst(`_RenderDst` 래퍼)에 실어 apply 가 byte-copy vs render 를 분기하게 한다. 평문
    str 항목(레거시 호출)은 render=False(후방호환·순수 copy2). render path 의 변경검출은
    filecmp 대신 rendered-output 비교(`_render_eq_dst`) — 템플릿≠산출물 오보 회피().

    render_enabled=False 면 operational 전체 렌더는 끈다. 다만 entry notation context가 있으면
    ``@render`` 어댑터와 공유 canonical wiki의 `/pm-*` 호출 토큰만 타깃 표기로 바꾼다. 프로젝트명
    등 나머지 토큰-form은 그대로 보존한다. 채택자 self-update에서도 같은 항목별 하네스 context를
    넘겨 canonical 스킬 표기가 되돌아오지 않게 한다.

    ``inventory_out``을 주면 이번 계획이 훑은 **출하 파일 좌표**(dest relpath + source relpath)를
    거기에 모은다. 변경 0인 파일도 담기므로 "요청 경로가 실제로 존재하는가"를 changes 유무와 독립
    으로 판정할 수 있다(경로 스코프의 오타 검출 — 별도 재열거 없이 같은 열거를 재사용).

    ``dest_inventory_out``은 같은 열거의 **dest 좌표만** 모은다. 은퇴 판정처럼 dest 잔존물과 대조하는
    소비자는 상류 좌표가 섞이면 우연한 문자열 일치로 후보를 놓치므로(false negative) 좌표축을 나눈다.

    ``guest_backfill_lines``는 apply 가 guest 절에 기록할 파생 엔진 행이다. self-prop 판정에만 쓴다 —
    기록 지점이 apply 한 곳뿐이라 그 change 가 계획에 없으면 파생분이 영원히 기록되지 않는다.
    """
    effective_dest = dest_root if dest_root is not None else REPO
    changes: list[tuple] = []
    missing: list[str] = []
    for entry_index, entry in enumerate(manifest):
        rel = str(entry)
        if rel.replace("\\", "/") == _MANIFEST_SELF_REL and manifest_source_text is not None:
            # 선택 flavor 합집합은 upstream에 단일 실파일로 존재하지 않는다. 인메모리 source를
            # change tuple에 실어 self-prop가 합집합 전체를 설치/갱신하게 한다.
            if inventory_out is not None:
                # 이 분기도 **계획이 다루는 좌표**다 — 빼면 `--paths .project_manager/engine.manifest`
                #   가 유효 변경인데도 "대응 없음" 으로 거부된다(합집합 경로에만 생기는 갭).
                inventory_out.add(rel.replace("\\", "/").strip("/"))
            if dest_inventory_out is not None:
                dest_inventory_out.add(rel.replace("\\", "/").strip("/"))
            dst = _RenderDst(
                effective_dest / rel, False,
                guest_backfill_lines=guest_backfill_lines)
            source = _ManifestTextSource(manifest_source_text)
            if not dst.exists():
                changes.append((rel, source, dst, "new"))
            else:
                try:
                    # 판정은 단일 flavor 분기와 **같은 헬퍼**다 — guest 절 차감 core 비교 + 옛 세대
                    #   마커. 여기만 마커 축이 빠지면 다중-harness 형상에서 마커 세대가 영구 잔존한다.
                    if _manifest_self_prop_needs_update(
                            Path(dst).read_text(encoding="utf-8"), manifest_source_text,
                            guest_backfill_lines):
                        changes.append((rel, source, dst, "update"))
                except (OSError, UnicodeDecodeError):
                    changes.append((rel, source, dst, "update"))
            continue
        declared_render = _entry_render_flag(entry)
        item_notation_template = (
            (entry_notation_templates or {}).get(rel.replace("\\", "/"))
            or entry_notation_template
        )
        # 전체 render는 adopter self-update에서만. --target은 아래 호출 표기 최소 렌더만 허용한다.
        render = declared_render if render_enabled else False
        inventory, source_missing, _target_owned = manifest_entry_shipping_inventory(
            source_root,
            manifest,
            entry_index,
            effective_dest,
        )
        if source_missing:
            # 부재 보고는 manifest(dest) 경로(rel) — missing-핸들러가 @target-owned 플래그를
            # str(entry) key 로 조회한다. @source 항목은 non-@target-owned → source 부재면 rc2
            # (템플릿 누락 은폐 금지·안전판).
            missing.append(rel)
            continue
        for r, sp in inventory:
            if inventory_out is not None:
                # 이 계획이 **실제로 다루는** 좌표(dest·source 양쪽) — 변경 유무와 무관하게 모은다.
                inventory_out.add(str(r).replace("\\", "/").strip("/"))
                try:
                    inventory_out.add(Path(sp).relative_to(source_root).as_posix())
                except ValueError:
                    pass
            if dest_inventory_out is not None:
                dest_inventory_out.add(str(r).replace("\\", "/").strip("/"))
            # render 대상 판정 = @render manifest 선언 + **텍스트로 읽히는가**.
            # 옛 `.md` 확장자 하드 필터는 제거했다: 확장자 열거는 manifest 선언을 덮는 중복
            # 판정이라, codex 가 들여온 `.codex/agents/*.toml`(@render 선언 O)이 byte-copy 로
            # 새어 채택자 트리에 `{{PROJECT_NAME}}` 리터럴을 재전파했다(pm_import 와 동형 결함·
            # 두 채널을 함께 닫는다). 텍스트 아님(바이너리 리소스)은 여전히 byte-copy 로 남는다.
            text_source = _is_text_source(sp)
            file_render = render and text_source
            notation_template = None
            rendered_entry: str | None = None
            notation_managed = declared_render or (
                render_enabled
                and str(r).replace("\\", "/").startswith(".project_manager/wiki/")
            )
            if (
                not file_render
                and notation_managed
                and text_source
                and item_notation_template is not None
            ):
                # 변환 지점이 실제로 있는 파일만 생성 산출물로 표시한다. 토큰 부재 파일은 기존
                # copy2 경로를 유지해 metadata/출력 churn을 만들지 않는다. 미등록 template 값은
                # helper가 여기서 fail-loud한다(dry-run도 원문 복사 false-green 금지).
                source_text = Path(sp).read_text(encoding="utf-8")
                rendered_entry = _render_skill_entry_text(sp, item_notation_template)
                if rendered_entry != source_text:
                    notation_template = item_notation_template
            dst = _RenderDst(
                effective_dest / r,
                file_render,
                entry_notation_template=(
                    item_notation_template if file_render else notation_template
                ),
                # guest 절 기록은 self-prop 분기에서만 일어난다 — 전 change 에 실으면 낭비다.
                guest_backfill_lines=(
                    guest_backfill_lines
                    if str(r).replace("\\", "/") == _MANIFEST_SELF_REL
                    else None
                ),
            )
            if not dst.exists():
                changes.append((r, sp, dst, "new"))
            elif file_render:
                # render path: 템플릿이 산출물과 byte-equal 일 수 없으므로 filecmp 는 항상 오보.
                # 렌더한 결과가 dst 와 다를 때만 update(정직 판정).
                if not _render_eq_dst(
                    sp, dst, effective_dest, item_notation_template
                ):
                    changes.append((r, sp, dst, "update"))
            elif notation_template is not None and rendered_entry is not None:
                if rendered_entry.encode("utf-8") != dst.read_bytes():
                    changes.append((r, sp, dst, "update"))
            elif str(r).replace("\\", "/") == _MANIFEST_SELF_REL:
                # engine.manifest self-prop — 판정(guest 절 차감 core 비교 + 옛 세대 마커)은
                #   다중-harness 합집합 분기와 **같은 헬퍼**를 쓴다(사유·근거는 그 docstring).
                try:
                    if _manifest_self_prop_needs_update(
                            Path(dst).read_text(encoding="utf-8"),
                            Path(sp).read_text(encoding="utf-8"),
                            guest_backfill_lines):
                        changes.append((r, sp, dst, "update"))
                except (OSError, UnicodeDecodeError):
                    if not filecmp.cmp(sp, dst, shallow=False):
                        changes.append((r, sp, dst, "update"))
            elif not filecmp.cmp(sp, dst, shallow=False):
                changes.append((r, sp, dst, "update"))
    return changes, missing


# ── 상류 은퇴 파일 보고 (삭제 전파 없음·read-only 진단) ────────────────────────
# manifest 디렉토리 엔트리 동기는 **source 열거**라 추가·갱신만 한다(`_iter_files`) — 상류에서
# 은퇴한 파일은 채택자 dest 에 영구 잔존하는데 어떤 출력도 그 사실을 말하지 않았다. 삭제를
# 전파하지 않는 것 자체는 유지한다: dest 에만 있는 파일이 "은퇴 파일" 인지 "채택자 로컬 자산"
# 인지 가를 데이터가 없고(둘 다 source 부재라는 같은 신호), 구분 수단 없는 삭제는 자산 파괴다.
# 그래서 지우지 않고 **보고**만 하며, 그 구분 불가를 출력 문구에 명시한다.

def _retired_manifest_files(
    source_root: Path,
    manifest: list,
    effective_dest: Path,
    dest_map,
) -> list[str]:
    """manifest 디렉토리 엔트리 아래 dest 파일 중 대응 source 가 upstream 에 없는 relpath 들.

    판정 좌표는 dest→source **역방향** 매핑이다 — `_dest_relpath_for`(board-분리 dest remap)와
    `_remap_to_dest`(`@source` source-remap)를 되짚어 upstream 읽기 경로를 만들고 그 실재만 본다.
    dest 파일 열거는 공용 seam(`repo_owned_files` OWNED = 추적 + 미추적-비ignore)이다. 직접
    rglob 하면 ignore 산출물(`__pycache__` 등)이 후보로 새어 보고가 노이즈에 묻힌다.

    ``dest_map`` 은 이번 계획이 훑은 출하 좌표(`plan(inventory_out=…)`) — 상류가 실제로 공급하는
    파일을 재판정 없이 걸러낸다. 그 밖의 제외:
      - `@target-owned` 엔트리 — upstream 에 source 가 없는 게 정상(타깃 고유 어댑터).
      - 더 구체적인 manifest 항목이 소유한 파일 — 그 항목 기준으로 판정된다(override 의미 보존).
      - **working tree 에 실재하지 않는 경로** — OWNED 열거는 git index 기반이라 이미 지웠지만
        commit 하지 않은 파일이 계속 후보로 남는다. "dest 에 잔존한다" 가 이 보고의 전제이므로
        디스크 실재를 다시 확인한다(없으면 보고할 잔존물 자체가 없다).

    **상류에서 통째로 사라진 등재도 이 채널이 본다**: dest 에 파일로 잔존하면 그 파일이, 디렉토리로
    잔존하면 그 아래 소유 파일 전부가 후보다. 실 sync 는 plan 의 missing 채널이 rc2 로 먼저 멈춰
    여기 도달하지 않지만, `--changes`(read-only)에는 그 채널이 없어 이 보고가 유일한 표면이다 —
    상류가 살아 있는 디렉토리만 스캔하면 "상류 삭제·rename (아래 보고 참조)" 헤더가 빈 포인터가
    되고, 통째 삭제라는 **가장 크게 잔존하는 형상**이 가장 조용해진다.

    ``dest_map`` 은 **dest 좌표만** 담아야 한다 — `@source` 매핑의 상류 좌표가 섞이면 우연히 같은
    문자열인 dest 잔존물이 "상류가 공급함" 으로 접혀 보고에서 사라진다(false negative).

    실패는 fail-soft(빈 목록) — 이 보고는 진단이라 sync 자체를 깨서는 안 된다.
    """
    source_root = Path(source_root)
    effective_dest = Path(effective_dest)
    planned = {
        str(coordinate).replace("\\", "/").strip("/") for coordinate in (dest_map or ())
    }
    try:
        repo_files = _load_repo_owned_files()
        dest_runner = repo_files._real_git_runner(effective_dest)
    except (OSError, RuntimeError, ValueError):
        return []

    retired: set[str] = set()
    for index, entry in enumerate(manifest):
        if _entry_target_owned_flag(entry):
            continue
        source_rel = _source_root_rel(entry).replace("\\", "/").strip("/")
        dest_rel = _dest_relpath_for(
            str(entry), effective_dest).replace("\\", "/").strip("/")
        source_exists = (source_root / source_rel).exists()
        if not (effective_dest / dest_rel).is_dir():
            # 디렉토리가 아니면 하위 열거가 없다 — 상류에서 사라진 **파일 엔트리**만 후보다.
            #   상류가 공급하는 파일 엔트리는 계획이 갱신하므로 은퇴가 아니다.
            if (not source_exists
                    and (effective_dest / dest_rel).is_file()
                    and dest_rel not in planned):
                retired.add(dest_rel)
            continue
        # 상류 디렉토리가 **통째로 사라져도** 하위 열거를 건너뛰지 않는다 — 건너뛰면 그 아래 dest
        #   잔존물 전부가 보고에서 통째 누락된다(가장 크게 잔존하는 형상인데 가장 조용했다). 아래
        #   per-child source 실재 검사가 자연히 전량을 후보로 만든다. 제외 규칙 우선순위는 그대로다:
        #   `@target-owned` 는 이 루프에 들어오기 전에 이미 빠졌고(source 부재가 정상), 계획이
        #   공급하는 좌표(`planned`)와 dest 미실재 경로도 아래에서 계속 걸러진다.
        try:
            # 열거는 **등재 디렉토리 subtree 로 한정**한다 — dest 전체를 훑으면 비-git dest 의
            # filesystem 폴백이 채택자 프로젝트 전부를 순회한다(등재 밖은 애초에 판정 대상도 아님).
            # 비-git dest 의 폴백 경고는 여기서 삼킨다 — 진단 보고 하나 때문에 채택자에게 엔진
            # 내부 seam 경고 원문을 노출할 이유가 없다(열거 자체는 폴백으로 정상 동작한다).
            fallback_warning = getattr(
                repo_files, "RepoFilesFallbackWarning", RuntimeWarning)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", fallback_warning)
                dest_files = [
                    dest_entry.path.as_posix()
                    for dest_entry in repo_files.list_repo_owned_entries(
                        effective_dest,
                        dest_rel,
                        mode=repo_files.OWNED,
                        git_runner=dest_runner,
                    )
                ]
        except (OSError, RuntimeError, ValueError):
            continue
        prefix = dest_rel + "/"
        for rel in dest_files:
            if not rel.startswith(prefix) or rel in planned:
                continue
            if _manifest_owner_index(manifest, rel, effective_dest) != index:
                continue
            if not (effective_dest / rel).is_file():
                continue  # index 에만 남은 삭제분 — dest 에 잔존물이 없으니 보고 대상이 아니다.
            source_candidate = source_rel + "/" + rel[len(prefix):]
            if not (source_root / source_candidate).exists():
                retired.add(rel)
    return sorted(retired)


# 은퇴 후보 나열 상한 — 상류가 디렉토리 하나를 통째로 재편하면 후보가 수백 건이 돼 다른 출력이
# 스크롤 밖으로 밀린다. 건수는 항상 정확히 알리고 나열만 자른다(정보 손실 0·"외 M건" 명시).
_RETIRED_REPORT_LIST_LIMIT = 20


def _print_retired_manifest_files(retired: list[str]) -> None:
    """은퇴 후보 보고 — 0 건이면 침묵(apply·dry-run·변경 0·`--changes` 네 경로 공용)."""
    if not retired:
        return
    print(
        f"⚠️ 상류 부재 파일 {len(retired)}건 — 동기는 지우지 않는다"
        "(채택자 로컬 자산과 구분 불가·수동 정리 판단):"
    )
    for rel in retired[:_RETIRED_REPORT_LIST_LIMIT]:
        print(f"    {rel}")
    remaining = len(retired) - _RETIRED_REPORT_LIST_LIMIT
    if remaining > 0:
        print(f"    … 외 {remaining}건 (전량은 engine.manifest 등재 경로를 직접 확인하라)")


# ── add-harness guest @render 절 (engine.manifest self-prop 보존) ──────────────
# engine.manifest 는 self-prop `@source` 라 apply 가 upstream 사본으로 통째 덮어쓴다(guest 는 로컬-전용
# → selfheal 'diverged' 도 *파일* overwrite 는 못 막는다·plan 에 self-prop change 가 실린다·실측). 그래서
# add_harness 가 등재한 guest `@render` 가 1회 update 만에 사라져 렌더/overlay 스캔 커버리지가 끊기던 것을
# () 이 마커 구획으로 닫는다: apply 가 engine.manifest 를 덮기 **전** dest 의 guest 절을
# 추출 → 덮은 **뒤** 재부착한다. 마커는 read_manifest 가 '#' 주석으로 무시하고, 절 안의 라인은
# `@render @target-owned` 유효 항목이라 파서/스캔/렌더가 그대로 소비한다(판정원 단일 = engine.manifest
# 최종 뷰 하나). 절의 *값* 은 pm_import.add_harness 가 쓴다(같은 리터럴 공유·아래 상수 블록).
# pm:data-literal:begin
# 아래 네 상수는 **채택자 디스크에 기록된 데이터**다 — engine.manifest 안에 실제로 적힌 바이트이지
# 이 소스의 산문이 아니다. 따라서 스트립·문구 정리·리팩터 대상이 아니며 한 글자만 달라져도 이미
# 기록된 채택자 manifest 의 guest 절을 못 읽어 절이 조용히 사라진다. 리터럴을 바꿔야 하면 옛 값을
# 지우지 말고 `_LEGACY` 튜플에 남겨라 — **읽기는 세대 집합 전체를 받고, 쓰기는 항상 현행 하나만**
# 낸다(옛 세대는 `_migrate_legacy_guest_markers` 가 다음 sync 에서 1 회 치환한다).
_GUEST_MANIFEST_BEGIN = "# >>> pm add-harness guest @render (local·pm_update-preserved) >>>"
_GUEST_MANIFEST_END = "# <<< pm add-harness guest @render (local) <<<"
_GUEST_MANIFEST_BEGIN_LEGACY: tuple[str, ...] = (
    "# >>> pm add-harness guest @render (local·pm_update-preserved·T-0456) >>>",
)
# 종료 마커는 아직 세대가 하나뿐이라 비어 있다 — 다음 세대를 대비해 시작 마커와 대칭 구조로 둔다.
_GUEST_MANIFEST_END_LEGACY: tuple[str, ...] = ()
# pm:data-literal:end

# 읽기가 인식하는 세대 집합(현행 + 옛). 쓰기 경로는 이 튜플을 쓰지 않는다(현행 상수 단일).
_GUEST_MANIFEST_BEGINS = (_GUEST_MANIFEST_BEGIN,) + _GUEST_MANIFEST_BEGIN_LEGACY
_GUEST_MANIFEST_ENDS = (_GUEST_MANIFEST_END,) + _GUEST_MANIFEST_END_LEGACY


def _extract_guest_manifest_block(text: str) -> str | None:
    """engine.manifest 텍스트의 add-harness guest 절(마커 경계 포함)을 반환 — 없으면 None.

    마커 비교는 **세대 집합 멤버십**이다(현행 + 옛 리터럴) — 옛 세대로 기록된 채택자 manifest 도
    절을 인식해야 apply 재부착이 그것을 보존한다(단일 리터럴 비교였을 때 guest 절이 경고 없이
    사라졌다)."""
    lines = text.splitlines()
    begin = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s in _GUEST_MANIFEST_BEGINS:
            begin = i
        elif s in _GUEST_MANIFEST_ENDS and begin is not None:
            end = i
            break
    if begin is None or end is None or end < begin:
        return None
    return "\n".join(lines[begin:end + 1])


def _migrate_legacy_guest_markers(text: str) -> tuple[str, bool]:
    """옛 세대 guest 마커 라인을 현행 리터럴로 치환 — (치환된 텍스트, 변경 여부).

    마커가 없거나 이미 현행이면 `(text, False)`(멱등). 쓰기 경로는 이 함수를 통과한 뒤에만 절을
    재부착하므로 디스크에 남는 세대는 항상 하나다(읽기 관용·쓰기 단일)."""
    lines = text.splitlines()
    migrated: list[str] = []
    changed = False
    for line in lines:
        s = line.strip()
        if s in _GUEST_MANIFEST_BEGIN_LEGACY:
            migrated.append(_GUEST_MANIFEST_BEGIN)
            changed = True
        elif s in _GUEST_MANIFEST_END_LEGACY:
            migrated.append(_GUEST_MANIFEST_END)
            changed = True
        else:
            migrated.append(line)
    if not changed:
        return text, False
    out = "\n".join(migrated)
    return (out + "\n" if text.endswith("\n") else out), True


def _strip_guest_manifest_block(text: str) -> str:
    """engine.manifest 텍스트에서 guest 절(마커 포함 + 선행 빈 줄)을 제거 — 마커 부재면 원문 그대로.

    마커 비교는 추출과 같은 세대 집합이다 — 읽기 판정이 한쪽만 관용이면 옛 세대 manifest 에서
    core 경로 집합(`_core_manifest_paths`)이 guest 라인을 core 로 세어 소유권 판정이 어긋난다."""
    lines = text.splitlines()
    begin = end = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s in _GUEST_MANIFEST_BEGINS:
            begin = i
        elif s in _GUEST_MANIFEST_ENDS and begin is not None:
            end = i
            break
    if begin is None or end is None or end < begin:
        return text
    lo = begin
    while lo > 0 and lines[lo - 1].strip() == "":  # 절 앞 빈 줄 구분자까지 회수(누적 방지)
        lo -= 1
    kept = "\n".join(lines[:lo] + lines[end + 1:])
    return kept + "\n" if kept and not kept.endswith("\n") else kept


def _manifest_self_prop_needs_update(
        dst_text: str, upstream_text: str,
        backfill_lines: list[str] | None = None) -> bool:
    """engine.manifest self-prop 을 이번 계획에 실어야 하는가 — 두 self-prop 분기의 공유 판정.

    분기는 둘이다: upstream 실파일 사본(단일 flavor)과 인메모리 합집합 텍스트(다중-harness). 판정이
    갈리면 한쪽 형상에서만 마이그레이션/갱신이 도달하므로 **한 함수**로 둔다(판정 사본 금지).

    사유 세 가지:
    - **core 차이**: dest 는 apply 가 재부착한 guest 절을 갖고 upstream 은 안 가지므로 guest 절을
      차감한 core 로 비교한다(raw 비교면 매 sync 영구 update churn). 트레일링 블랭크는
      `rstrip("\\n")` 로 정규화한다(strip 이 절 앞 빈 줄을 회수하며 생기던 반복 update 를 닫는다).
    - **옛 세대 guest 마커**: core 가 같아도 그 자체가 update 사유다. 마커 세대 치환은 apply
      (`_copy_manifest_preserving_guest`) 한 곳에서만 일어나므로, 이 change 가 계획에 안 실리면
      "다음 sync 한 번으로 세대 수렴" 이 그 형상에서 깨진다.
    - **절에 없는 파생 엔진 행**: 같은 이유다. 기록 지점이 apply 한 곳뿐이라, core 가 이미 정합한
      채택자(대다수)에서 self-prop 이 계획에 안 실리면 파생분이 영원히 기록되지 않고 매 실행 추론을
      반복한다. 병합 결과가 현재 절과 같으면(=이미 기록됨) 사유가 아니다(멱등·churn 0).
    """
    _, legacy_markers = _migrate_legacy_guest_markers(dst_text)
    if legacy_markers:
        return True
    guest_block = _extract_guest_manifest_block(dst_text)
    if _merge_guest_backfill_lines(guest_block, backfill_lines or []) != guest_block:
        return True
    return _strip_guest_manifest_block(dst_text).rstrip("\n") != upstream_text.rstrip("\n")


def _reattach_guest_block(new_text: str, guest_block: str | None) -> str:
    """upstream 사본(new_text) 뒤에 guest 절(guest_block)을 재부착 — block 없으면 new_text 그대로."""
    if not guest_block:
        return new_text
    sep = "" if new_text.endswith("\n") or not new_text else "\n"
    return new_text + sep + "\n" + guest_block + "\n"


def _core_manifest_paths(text: str) -> set[str]:
    """manifest 텍스트의 core 경로 집합 (guest 절·마커·주석·빈 줄 제외·마커 떼고 path 만)."""
    out: set[str] = set()
    for ln in _strip_guest_manifest_block(text).splitlines():
        s = ln.strip()
        if s and not s.startswith("#"):
            out.add(s.split()[0].replace("\\", "/"))
    return out


def _path_owned_by(path: str, owner_paths) -> bool:
    """path 가 owner_paths 중 하나에 소유되는가 — **동일**(`path==c`) OR **상위**(`path` 가 `c/` 하위).

    add-harness 등재 차감(pm_import `_guest_render_to_add`)과 update 재부착 차감
    (`_prune_guest_block_owned_by_core`)이 **공유**하는 소유권 판정(경로-포함·판정
    사본 금지) — core 가 `.opencode`(상위)를 가지면 `.opencode/agents` 도 소유로 본다."""
    p = path.replace("\\", "/")
    return any(
        p == c or p.startswith(c.rstrip("/") + "/")
        for c in (str(o).replace("\\", "/") for o in owner_paths))


def _prune_guest_block_owned_by_core(guest_block: str | None, core_text: str) -> str | None:
    """upstream core 가 소유하게 된 경로(**동일 OR 상위**)를 guest 절에서 차감.

    guest 경로가 추후 upstream core manifest 로 승격되면 apply 재부착이 기존 `@target-owned` guest 를
    그대로 붙여 **같은 경로가 core+guest 이중 등재** → 뒤쪽 guest 가 owner 로 이겨 upstream 소스가 영구
    skip 된다. 재부착 전 core 소유 경로를 `_path_owned_by`(경로-포함·add-측과 공유)로 차감해 닫는다.
    남는 guest 라인 0 이면 None(절 제거)."""
    if not guest_block:
        return None
    core_paths = _core_manifest_paths(core_text)
    kept: list[str] = []
    guest_count = 0
    for ln in guest_block.splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            kept.append(ln)  # 마커/빈 줄 보존.
            continue
        if _path_owned_by(s.split()[0], core_paths):
            continue  # core 가 소유(동일/상위) — 차감.
        kept.append(ln)
        guest_count += 1
    if guest_count == 0:
        return None  # 전량 승격 — 절 제거.
    return "\n".join(kept)


def _merge_guest_backfill_lines(
        guest_block: str | None, backfill_lines: list[str]) -> str | None:
    """guest 절에 파생 엔진 행을 병합 — 같은 경로는 파생 행으로 **덮어쓰고**, 없으면 추가.

    절이 없으면(비-add-harness) 아무것도 만들지 않는다 — 파생 자체가 절의 존재를 전제한다. 본문은
    경로 정렬로 재조립하며(add-harness 등재와 같은 순서·멱등), 마커 경계와 절 안 주석은 보존한다."""
    if not guest_block or not backfill_lines:
        return guest_block
    block_lines = guest_block.splitlines()
    if len(block_lines) < 2:
        return guest_block
    body = block_lines[1:-1]
    comments = [line for line in body
                if not line.strip() or line.strip().startswith("#")]
    rows: dict[str, str] = {}
    for line in body:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        rows[stripped.split()[0].replace("\\", "/")] = line.rstrip()
    for line in backfill_lines:
        rows[line.split()[0].replace("\\", "/")] = line
    return "\n".join([
        block_lines[0], *comments,
        *(rows[path] for path in sorted(rows)),
        block_lines[-1],
    ])


def _copy_manifest_preserving_guest(
        sp, dst: Path, backfill_lines: list[str] | None = None) -> None:
    """engine.manifest 를 upstream(sp)으로 덮되 dest 의 add-harness guest 절을 재부착.

    재부착 전 **upstream core 가 소유하게 된 경로를 guest 절에서 차감**한다(소유권 전환·이중 등재
    방지). guest 절이 없거나(비-add-harness) 전량 승격되면 순수 copy2 와 동일(무영향).

    dest 가 **옛 세대 마커**로 기록돼 있으면 읽기 직전에 현행 리터럴로 1 회 치환한다 — 재부착되는
    절은 항상 현행 리터럴이라 다음 sync 부터는 단일 세대만 남는다(별도 마이그레이션 커맨드 없이
    채택자의 다음 `pm_update` 한 번으로 정상화).

    `backfill_lines`(파생 엔진 행)를 주면 **마커 세대 마이그레이션 뒤** 그 절에 병합해 기록한다 —
    절을 쓰는 지점이 여기 하나뿐이라 파생분 지속화도 같은 write 에 실린다(`--paths` 선검증이 파일만
    읽어도 정합). dry-run 은 apply 를 타지 않으므로 자동으로 무기록이다.

    ⚠ 알려진 한계: self-heal 이 `legacy_preserved` 인 코호트는 self-prop 자체가 계획에서 빠지므로
    (로컬 manifest 불가침) 이 치환이 도달하지 않는다 — 읽기 관용 덕에 동작은 정상이고, 세대 수렴은
    그 채택자가 legacy 상태를 벗어나는 시점으로 미뤄진다."""
    guest_block = None
    try:
        if dst.is_file():
            dst_text, migrated = _migrate_legacy_guest_markers(
                dst.read_text(encoding="utf-8"))
            if migrated:
                print("✓ guest 절 마커 세대 마이그레이션 (구 리터럴 → 현행)")
            guest_block = _extract_guest_manifest_block(dst_text)
    except OSError:
        guest_block = None
    # 세대 마이그레이션 → 파생 백필 순서(치환된 현행 절 위에 기록·조합 시 절 소실 0).
    guest_block = _merge_guest_backfill_lines(guest_block, backfill_lines or [])
    new_text = sp.read_text(encoding="utf-8")
    guest_block = _prune_guest_block_owned_by_core(guest_block, new_text)
    if not guest_block and isinstance(sp, Path):
        shutil.copy2(sp, dst)  # 비-add-harness 또는 전량 승격 — copy2(바이트/메타 무변경).
        return
    dst.write_text(_reattach_guest_block(new_text, guest_block), encoding="utf-8")


def apply(changes: list[tuple], *, is_hook_set_path=None) -> None:
    """change 적용 — render=False(기본)는 순수 copy2, render=True 는 render_adapter 후 기록.

    dst 가 `_RenderDst`(render 플래그 운반·plan 산출)면 그 플래그로 분기한다. 평문 Path dst
    (레거시 직접 호출)는 render 비대상 → copy2(후방호환·현 pm_update 동작 불변).

    engine.manifest self-prop overwrite 는 add-harness guest 절을 재부착한다(위 헬퍼). 그 재부착에
    함께 기록할 파생 엔진 행은 dst(`_RenderDst.guest_backfill_lines`)가 실어 나른다 — `entry_
    notation_template` 과 같은 운반 방식이라 이 함수의 시그니처는 그대로다.

    **훅 세트 파일만 원자 교체**한다 — 하네스가 실행 중에 읽는 파일을 copy2 로 덮으면
    truncate→채움 창에 부분 파일이 실행된다. 그 밖의 파일은 현행 copy2 그대로다(전 파일 확대는
    이 클래스가 요구하지 않는다). `is_hook_set_path` 는 호출부가 **상류 세대**로 해소해 넘긴다
    (`resolve_hook_set_predicate`) — 미지정이면 로컬 세대로 폴백하는데, 그 폴백은 이번 실행이
    새로 추가하는 훅 경로를 모른다(구세대 선언).
    """
    hook_set_path = (is_hook_set_path if is_hook_set_path is not None
                     else resolve_hook_set_predicate())
    render_mod = None  # render path 가 있을 때만 lazy-load.
    for _r, sp, dst, _kind in changes:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if getattr(dst, "render", False):
            dest_root = _dest_root_for(dst, _r)
            if render_mod is None:
                render_mod = _load_pm_render()
            operational, empty_keys = _operational_from_local_conf(dest_root)
            text = Path(sp).read_text(encoding="utf-8")
            rendered = render_mod.render_adapter(
                text,
                operational=operational,
                empty_keys=empty_keys,
                template_dir=getattr(dst, "entry_notation_template", None),
                source=str(sp),
            )
            Path(dst).write_bytes(rendered.encode("utf-8"))
        elif getattr(dst, "entry_notation_template", None) is not None:
            if render_mod is None:
                render_mod = _load_pm_render()
            text = Path(sp).read_text(encoding="utf-8")
            rendered = render_mod.render_skill_entry_notation(
                text,
                dst.entry_notation_template,
                source=str(sp),
            )
            Path(dst).write_bytes(rendered.encode("utf-8"))
        elif str(_r).replace("\\", "/") == _MANIFEST_SELF_REL:
            # engine.manifest self-prop — upstream 사본으로 덮되 guest 절 보존(+파생 행 기록).
            _copy_manifest_preserving_guest(
                sp, Path(dst), getattr(dst, "guest_backfill_lines", None))
        elif hook_set_path(_r):
            _atomic_copy2(Path(sp), Path(dst))
        else:
            shutil.copy2(sp, dst)


def _dest_root_for(dst: Path, rel: str) -> Path:
    """change 의 dst 절대경로와 그 repo-기준 relpath 로 dest_root 를 역산한다.

    dst = dest_root / rel 이므로 dst 에서 rel 컴포넌트 수만큼 거슬러 올라가면 dest_root.
    plan 이 dst 를 effective_dest/r 로 만들었으므로 정확히 복원된다(render path 의 local.conf
    조회 기준).
    """
    parts = Path(rel).parts
    root = Path(dst)
    for _ in parts:
        root = root.parent
    return root


def resolve_target_root(target_name: str) -> Path:
    """타깃 이름 → 동기화 대상 루트 경로 (항상 REPO/templates/<target_name>/).

    source(--from)와 dest는 독립적이다:
    - source_root(--from): 엔진 파일을 읽어오는 곳
    - dest(이 함수 반환값): 이 스크립트가 속한 REPO의 templates/<target>/

    따라서 --from 이 REPO 외의 upstream 이어도 dest 는 항상 이 REPO 를 가리킨다.

    타깃 유효성은 REPO/templates/<name>/ 디렉토리 존재로 판단한다.
    새 타깃 추가가 이 파일 수정을 강제하지 않는다.

    보안: target_name 은 단일 path segment 이어야 한다.
    '/', os.sep, '..', 빈 문자열을 포함하면 path traversal 로 간주해 거부한다.
    이후 resolve() 결과의 parent 가 REPO/templates/ 임을 이중 검증한다.
    """
    # ── 1차: 단일 segment 검증 (빠른 거부) ──────────────────────────────────
    if (
        not target_name
        or "/" in target_name
        or os.sep in target_name
        or target_name == ".."
        or target_name.startswith("../")
        or ".." in target_name.split("/")
    ):
        raise ValueError(
            f"잘못된 타깃 이름: {target_name!r}. "
            "타깃은 단일 path segment 이어야 한다 ('/', '..', 빈 문자열 불허)."
        )

    # ── 2차: resolve() 후 parent 검증 (symlink·우회 방어) ───────────────────
    templates_resolved = (REPO / "templates").resolve()
    candidate = (REPO / "templates" / target_name).resolve()
    if candidate.parent != templates_resolved:
        raise ValueError(
            f"타깃 경로 탈출 시도: {target_name!r} → {candidate}. "
            f"허용 범위: {templates_resolved}/<name>."
        )

    target_root = candidate
    if not target_root.is_dir():
        templates_dir = _templates_dir()
        if templates_dir.is_dir():
            known = sorted(p.name for p in templates_dir.iterdir() if p.is_dir())
        else:
            known = []
        known_hint = ", ".join(known) if known else "(없음)"
        raise FileNotFoundError(
            f"알 수 없는 타깃 또는 디렉토리 없음: {target_name!r}. "
            f"REPO/templates/<name>/ 디렉토리를 먼저 만들어라. "
            f"현재 발견된 타깃: {known_hint}"
        )
    return target_root


def discover_target_names() -> list[str]:
    """`templates/` 직계 디렉토리의 타깃 이름을 정렬해 반환한다.

    타깃 추가 시 CLI의 고정 목록을 갱신하지 않도록 `resolve_target_root`와 같은
    디렉토리-발견 규칙을 사용한다. `--all-targets`의 대상은 이 함수의 반환값이다.
    """
    templates_dir = _templates_dir()
    if not templates_dir.is_dir():
        return []
    # 숨김 디렉토리(.git 류)는 타깃이 아니다 — 비-숨김 직계 디렉토리는 전부 타깃으로 간주한다
    # (templates/ 는 관례상 타깃 전용·문서 열거 ↔ 디렉토리 집합 일치는 enumeration 가드가 강제).
    return sorted(path.name for path in templates_dir.iterdir()
                  if path.is_dir() and not path.name.startswith("."))


def resolve_manifest_for_dest(dest_root: Path, source_root: Path) -> Path:
    """dest_root 의 engine.manifest 우선, 없으면 source_root 의 것."""
    dest_manifest = dest_root / ".project_manager" / "engine.manifest"
    if dest_manifest.exists():
        return dest_manifest
    source_manifest = source_root / ".project_manager" / "engine.manifest"
    if source_manifest.exists():
        return source_manifest
    raise FileNotFoundError("engine.manifest 없음 (dest·source 둘 다).")


def _split_guest_channels(
        entries: list, guest_entries: list, guest_backfill: list,
        selfheal: dict | None) -> list:
    """계획 manifest 에 add-harness guest 절의 **소유 채널 분리**를 적용한다.

    절 한 줄의 `@render` 유무가 채널을 가른다:
      - `@render` 행(어댑터 렌더물) = add-harness refresh 전용·update 불가침 → **계획 제외**.
        `@target-owned` skip 은 *source-부재* 때만 발동해, 프레임워크 root 에 source 가 실재하는
        claude-guest(`.claude/agents`·`.claude/skills`)는 self-update 계획이 그냥 갱신해 채택자의
        guest 로컬 수정을 덮었다. 절은 apply 가 재부착하므로 파일엔 남고 계획에서만 뺀다.
      - 비-`@render` 행(엔진 파일) = update 채널 소유 → **계획 합류**(byte-copy). 이 행까지 빼면
        `pm_relay` 코어와 짝인 드라이버·ctx 가드가 어떤 채널로도 갱신되지 않아 영구 동결된다."""
    if not guest_entries:
        return entries
    refresh_owned = {
        str(entry).replace("\\", "/") for entry in guest_entries
        if _entry_render_flag(entry)
    }
    if refresh_owned:
        # **승격분 제외**: guest 경로가 upstream core 로
        # 승격되면(selfheal 이 그 경로를 담은 upstream 을 계획 기준으로 올림) 이제 core 라 **1차
        # sync 에서 갱신돼야** 한다 — dest guest 절에 있어도 **upstream core 에 실재하면 필터 밖**
        # (안 그러면 첫 실행이 그 파일을 안 갱신·2회 필요). upstream core = selfheal 이 해소한 flavor
        # manifest 경로(사본 0·같은 대조 기준). --target 은 selfheal 미실행이나 guest 절도 없어 무해.
        upstream_core_paths = _selected_upstream_core_paths(selfheal or {})
        if upstream_core_paths:
            refresh_owned -= upstream_core_paths
    # 아래 합류가 append 하므로 **항상 새 리스트**로 뜬다 — 입력이 selfheal 승격분 그 자체일 수
    #   있어(같은 객체) 제자리 변경하면 selfheal 산출이 오염된다.
    planned = [
        entry for entry in entries
        if str(entry).replace("\\", "/") not in refresh_owned
    ]
    # 엔진 행 합류 — 파생분 우선, 그 뒤 절에만 남은 행(상류에서 폐기된 경로는 `@target-owned` 라
    #   loud `[skip]` + rc0). selfheal 이 upstream 을 승격한 run 은 계획 manifest 가 upstream 전용
    #   이라 guest 엔진 행이 통째로 빠지므로, 경로 중복만 접고 계획에 얹는다.
    planned_paths = {str(entry).replace("\\", "/") for entry in planned}
    for entry in [*guest_backfill,
                  *(e for e in guest_entries if not _entry_render_flag(e))]:
        path = str(entry).replace("\\", "/")
        if path in planned_paths:
            continue
        planned_paths.add(path)
        planned.append(entry)
    return planned


def _resolve_planning_manifest(
        effective_dest: Path, source_root: Path, selfheal: dict | None = None,
        *, guest_entries: list | None = None,
        guest_backfill: list | None = None) -> list:
    """이번 실행의 **계획 기준 manifest** — self-heal 승격분 우선, 없으면 dest/source 로컬 해소.

    실 sync(`_main`)와 미리보기(`--changes`)가 이 한 함수를 공유한다. 갈라져 있던 동안 미리보기는
    낡은 로컬 manifest 로 분류하고 실 sync 는 승격된 upstream manifest 로 계획해, 정확히 self-heal
    이 전달하려는 신규 엔진 파일이 "안 받음" 으로 미리보기됐다. 발생 기전이 "self-heal 을 `_main`
    에만 추가" 였으므로 판정 사본을 두지 않는다 — self-heal 이 또 진화해도 여기 한 곳만 바뀐다.

    `selfheal` 은 **이미 해소한** `resolve_manifest_selfheal` 산출이다 — 호출부가 넘긴다(`_main` 은
    skew 대조·merge conflict 표시에도 그 dict 를 쓰므로 두 번 돌릴 이유가 없고, 해소 여부 자체가
    호출부 게이트다: `--target` 엔진 export 는 타깃 manifest 가 루트와 의도적으로 달라 비발화).
    `None` = self-heal 미해소(승격 없음) → 로컬 해소만 한다.
    폴백 경로의 FileNotFoundError 는 호출부가 처리한다 — 진입별로 rc 가 다르다(sync=rc1 에러,
    미리보기=빈 manifest graceful).

    **`legacy_preserved` 제외도 이 판정의 일부**다: exact-match 가 아닌 legacy 는 로컬 manifest
    자체가 불가침이라 self-prop(`_MANIFEST_SELF_REL`)을 계획에서 뺀다(source root manifest 가 파일을
    통째 덮어 커스텀 행을 지우는 것을 막는다). 이 축이 `_main` 에만 있으면 그 코호트에서 미리보기가
    engine.manifest 를 "받는다" 고 오보한다 — 기준 일치가 이 헬퍼의 존재 이유다.

    **add-harness guest 절의 채널 분리도 여기서** 한다(`_split_guest_channels`). `_main` 에만 있던
    동안 미리보기는 guest 렌더물을 "받는다" 고 하고 guest 엔진 행은 "안 받는다" 고 해, 이 헬퍼의
    존재 이유(미리보기 == 계획)가 guest 축에서만 거짓이었다. `guest_entries`/`guest_backfill` 은
    호출부가 이미 해소했으면 넘기고(중복 IO·pm_import 재로드 회피), 없으면 여기서 해소한다.
    """
    if guest_entries is None:
        guest_entries = _dest_guest_manifest_entries(effective_dest)
    if guest_backfill is None:
        guest_backfill, _flavors = _guest_engine_backfill_entries(
            effective_dest, source_root, guest_entries)
    if selfheal is not None and selfheal["manifest"] is not None:
        entries = selfheal["manifest"]
    else:
        entries = _resolve_local_manifest(effective_dest, source_root)
        if selfheal is not None and selfheal["status"] == "legacy_preserved":
            entries = [
                entry for entry in entries
                if str(entry).replace("\\", "/") != _MANIFEST_SELF_REL
            ]
    return _split_guest_channels(entries, guest_entries, guest_backfill, selfheal)


def _resolve_local_manifest(effective_dest: Path, source_root: Path) -> list:
    """로컬 해소 manifest **원문 전체**(dest 우선·없으면 source) — 계획 제외 전 상태.

    계획용(`_resolve_planning_manifest`)과 skew 대조용이 갈린다: 전자는 `legacy_preserved` 에서
    self-prop 을 빼고, 후자는 원문 전체를 봐야 upstream 신규 등재분을 정상 검출한다. 두 소비자가
    같은 해소를 쓰도록 이름 붙인 seam."""
    return read_manifest(resolve_manifest_for_dest(effective_dest, source_root))


# ── 진입 doc 세대 마이그레이션 ────────────────────────────────
# 기존 채택자의 구형 진입 doc(자족 매뉴얼형 opencode `AGENTS.md`·~22KiB)을 신형(harness-neutral
# 공통 코어 + `.opencode/pm-instructions.md` + `opencode.jsonc` `instructions` 배열)으로 수렴시킨다
# — 2세대 영구 공존 차단(사용자 발의 "신형 전환 선택제=관리 분기"). self-update 흡수 경로 한정이며
# `--target` 엔진 export 는 비발화(skew/selfheal 와 동일 경계).
#
# 판정 = **미수정 여부 단 하나**(mechanize·추측 0). @render 치환(operational 토큰)·manual-fill TODO
# 마커가 채택자본을 세대 원본에서 벌려 놓으므로 순수 해시 대조는 불가능하다(ticket 열린 질문). →
# **치환-불변 정규화**로 판정한다: (1) manual-fill 마커(pm_import._mark_todos)를 벗겨 정규화하고,
# (2) 세대 원본에서 operational 토큰을 줄-경계 wildcard 로, free-form 토큰(`{{PROJECT_CONSTRAINTS}}`)
# 을 *리터럴*(미채움=pristine 요구)로 둔 패턴에 re.fullmatch 한다. operational=출하 렌더(전 채택자
# 결정적)라 wildcard(=미수정), free-form=채택자 FILL 영역이라 리터럴 요구(채웠으면 커스텀 흔적→무손
# loud). 이로써 local.conf 가 tagline/date 를 보존하지 않아도(board.py init 은 py·test_cmd·
# project_name 만 기록) 세대 판정이 성립한다 — 재렌더 대조(local.conf 미보유 토큰서 실패)보다 강건.
# 매칭 시 operational 값을 *포획*해 신형 재렌더에 재사용(채택자 tagline 보존).

# pm_import._mark_todos 가 manual-fill 시 free-form placeholder 줄 끝에 덧붙이는 마커 — 정규화로
# 벗겨낸다(세대 원본엔 없음). pm_import 리터럴과 동일해야 한다(단일 진실·거기서 바뀌면 여기도).
_ENTRY_DOC_MANUAL_TODO_MARKER = " <!-- TODO: 손으로 채우세요 -->"

# 구형 opencode AGENTS.md 의 H1 판별자 — 신형 title 은 "PM 어댑터 공통 코어"(이 문자열 부재). 세대
# clean-match 실패 시 "구형이나 수정됨(→loud) vs 신형/무관(→no-op)" 을 가르는 기계 신호.
_ENTRY_DOC_OLD_GEN_MARKER = "# AGENTS.md — opencode PM 어댑터"

# opencode.jsonc `instructions` 배열에 idempotent 추가할 신형 지침 경로(@source 전파).
_ENTRY_DOC_PM_INSTRUCTIONS_REL = ".opencode/pm-instructions.md"

# 중앙 백업 디렉토리 — pm_import 백업 채널 재사용(BACKUP_DIR_NAME 미러·relpath 미러링). 자동 전환
# 시 원본 AGENTS.md·opencode.jsonc 를 `<dest>/.pm_import_backups/<DATE>/<relpath>` 로 보존한다.
_ENTRY_DOC_BACKUP_DIR = ".pm_import_backups"

# 어댑터 `{{TOKEN}}` placeholder 스캔 — 세대 패턴 빌드용(operational=wildcard·그 외=리터럴 분류).
_ENTRY_DOC_TOKEN_RE = re.compile(r"\{\{([A-Z_]+)\}\}")

# 역대 출하 opencode AGENTS.md 세대(구형·자족 매뉴얼형)의 *원본* 텍스트 = fingerprint 자산. 채택자
# 사이트(프레임워크 git history 없음)에서도 자족적으로 세대 판정을 하려면 원본 텍스트가 필요하므로
# (해시만으론 치환-불변 대조 불가·위 근거) 엔진에 임베드한다. zlib+base64 인코딩 — 이스케이프 함정
# (triple-quote·backslash) 회피 + 콤팩트. 원본 = git `0ccc025`(v1.3.5 출하 세대). provenance 는
# tests/test_entry_doc_migration.py 가 git blob 과 기계 대조(무결성 lock). 세대 추가 시 아래 튜플에
# append("역대 출하본" 확장 지점·해시 목록의 텍스트 판).
_OLD_OPENCODE_AGENTS_V1_3_5_B64 = (
    "eNqtXHtzU0eW/9+fohdmZyUFSTavJF7MFgF2ll1eFZjdmWIpXyFdGw2ypJVkCDXMlgGZNdjZ2BMbZLA98sbEJuNUZCOw2JDa"
    "Kn8U/tS9+g77O+d034dsT0hqqATs++g+ffo8fufRd7868avT5y9fSoxk1LuxWVUo2vl0IWOri+eU+7jpTM10qo2enuP+Dbfa"
    "csdr7mJTuZML7tKMM11XztyUcpemnS/mlbO84Hy54EzXnEezyl2tukvjbn06oWIxeqUzNeUuvqWfLp6LXCwVfmenK+pcKp8a"
    "tktR5T5turW77mIN82E4d3nDrd9VzuqK87DpPqbXnMmVRCymTuZSoyDlJNHjvKniDXe5Gab/7NlzEdDifr+uhu2RkZRKqs7T"
    "KffeusK0USYf/zvrLbe64KxO0YyLP7Rf1pX7aAX/4VVmwWJVOa/HOo9auIa/naUX21tCZmdugajpOY5Xnakxd3FGtZtj7ZfV"
    "fuWvVA2EaO3cH2u/uQ9ylXXy7IlfnzoNtlsJ5UyugT/trRbGoYmdzQn3ybdM41zVmZxwFt/GYqE9mFzpPH6onG/WnM9bkWuj"
    "2VxGFUvZkVTptvpAOdUVvNuZbDitKWVVUuUblqoUCjnlLmA7qlH3yTSYutTepE1bx/rdiXrn8QStRkVOnPo03tvbe1RtbxED"
    "PnYba9Genv37VWe2SvysL4INCotX7kq1p+f3v7/46YV/Pn3y8uDlE786e+b86T/8oefY38Tj6vKFUxf6lX/7/IlzuKfajTEw"
    "vek++S9ibWdOxASCBPaOL7iLLXd1TPX950HemaXnCRWPH+fpexN7LF9FQKbzota5P6GvgN44+AkJK5TS1+1ypZSqFCBeA8oq"
    "jlgeoyBz+YqK9NEKabVhNkY6XzSdxkYU4tajFI1HD3aNAVGMWAlDV5JHLCeLI7StNKQ1gsv95mkrmlAlO5e6bZj8MWSJtYlF"
    "s17FREqVR4t26Wa2XChtb8lzB3vBtgXnm/EocY+kUqtgFVRl7IpdGsnms+VKNk0MpW0tF1O38iBO89eIHakPa4nlsbI0mgeL"
    "hRW0MHfhLUmgW1t3GrPO8xaTpOWMFGd7q/PHsfbrKQgAiJ+bANW0p1B53tnGXGdpKkgiVGxhnSWLuaKcly3a2cUxVbmezTMx"
    "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"
)
_OLD_OPENCODE_AGENTS_GENERATIONS = (_OLD_OPENCODE_AGENTS_V1_3_5_B64,)


def _decode_entry_doc_generation(b64: str) -> str:
    """임베드된 세대 원본(zlib+base64) → 텍스트."""
    return zlib.decompress(base64.b64decode(b64)).decode("utf-8")


def _entry_doc_operational_keys() -> frozenset:
    """세대 대조에서 wildcard(=출하 렌더)로 볼 operational 토큰 집합 = pm_render.OPERATIONAL_KEYS.
    그 밖의 `{{...}}`(free-form·`{{PROJECT_CONSTRAINTS}}`)은 리터럴(pristine 요구). pm_render 로드
    실패는 보수 폴백(하드코딩 동일 집합·엔진 co-located 라 정상 설치엔 항상 로드)."""
    try:
        return frozenset(_load_pm_render().OPERATIONAL_KEYS)
    except Exception:  # noqa: BLE001 — 로드 실패 폴백(pm_render 와 동일 집합).
        return frozenset((
            "PROJECT_NAME", "PROJECT_TAGLINE", "PROJECT_ROOT", "PY", "TEST_CMD", "DATE",
        ))


def _build_entry_doc_pattern(generation_text: str, operational_keys) -> tuple[str, dict]:
    """세대 원본 → (re.fullmatch 패턴, group→token 맵). operational 토큰은 줄-경계 wildcard 캡처
    그룹(`[^\\n]*`), 그 외 `{{...}}`(free-form)는 리터럴, 나머지 텍스트는 re.escape.

    operational 값은 출하 렌더라 채택자마다 다르나 미수정 신호(줄 내 값)이므로 `[^\\n]*`. free-form
    토큰은 채택자 FILL 영역이라 리터럴로 둬 미채움(pristine)일 때만 매칭한다(채웠으면 불일치→loud)."""
    parts: list[str] = []
    group_token: dict[str, str] = {}
    last = 0
    gi = 0
    for m in _ENTRY_DOC_TOKEN_RE.finditer(generation_text):
        parts.append(re.escape(generation_text[last:m.start()]))
        tok = m.group(1)
        if tok in operational_keys:
            name = f"op{gi}"
            gi += 1
            parts.append(f"(?P<{name}>[^\\n]*)")
            group_token[name] = tok
        else:
            parts.append(re.escape(m.group(0)))  # free-form/미상 토큰 = 리터럴(pristine 요구)
        last = m.end()
    parts.append(re.escape(generation_text[last:]))
    return "".join(parts), group_token


def _match_entry_doc_generation(
    generation_text: str, adopter_text: str, operational_keys
) -> dict | None:
    """정규화한 채택자 AGENTS.md 가 세대 원본 구조와 byte-match 하면 포획 operational 값 dict, 아니면 None.

    정규화 = manual-fill 마커 제거(pm_import._mark_todos). operational 토큰의 복수 occurrence 는
    같은 값이어야 한다(출하 시 uniform 치환) — 불일치면 손편집 신호로 None(안전·loud 로 낙하)."""
    normalized = adopter_text.replace(_ENTRY_DOC_MANUAL_TODO_MARKER, "")
    pattern, group_token = _build_entry_doc_pattern(generation_text, operational_keys)
    m = re.fullmatch(pattern, normalized)
    if m is None:
        return None
    values: dict[str, str] = {}
    for name, tok in group_token.items():
        v = m.group(name)
        if tok in values:
            if values[tok] != v:
                return None  # 같은 토큰 occurrence 값 불일치 → 비-uniform(손편집)·안전 낙하
        else:
            values[tok] = v
    return values


def _render_new_entry_doc(
    new_template_text: str, operational: dict, operational_keys
) -> str | None:
    """신형 AGENTS.md 템플릿 → operational 치환 산출물(free-form 리터럴 유지). operational leak
    잔존(값 미보유) 시 None — 미완 렌더 파일을 쓰지 않는다(안전·loud 로 낙하).

    render_adapter(assert_no_leak)는 free-form `{{PROJECT_CONSTRAINTS}}` 에서 raise 하므로 쓰지
    않는다 — operational 만 채우고 free-form 은 pristine 유지(신선 import --fill manual 과 동형)."""
    text = new_template_text
    for key, val in operational.items():
        if val:
            text = text.replace("{{" + key + "}}", str(val))
    for key in operational_keys:
        if "{{" + key + "}}" in text:
            return None  # operational 미해소 잔존 — 자족 위반 방지(free-form 은 허용)
    return text


# quoted-string 원소 추출 (escape-aware) — 등록-확인을 substring 이 아니라 *정확 원소* 대조로
# (): `.opencode/pm-instructions.md.bak` 같은 suffix 나 문자열-내
# 부분일치를 "이미 등록"으로 오인하지 않게 한다.
_JSONC_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
# 최상위(depth==1) `"instructions"` 키 + 배열 여는 `[` — brace-depth 스캐너가 이 위치에서 match.
_INSTR_KEY_RE = re.compile(r'"instructions"\s*:\s*\[')


def _mask_jsonc_comments(text: str) -> str:
    """jsonc 주석(`//…`·`/* */`)을 같은 길이 공백(개행 보존)으로 마스킹 — **오프셋 보존**(원본과 1:1).

    문자열 리터럴은 존중한다 — 문자열 안의 `//`(예: `$schema` URL `https://…`)는 주석이 아니므로
    마스킹하지 않는다. 탐지/삽입 위치를 이 마스킹본에서 구하고 실제 write 는 원본에 같은 오프셋으로
    적용해, 주석-아웃된 `"instructions"`/경로를 오탐 없이 걸러내면서 원본 주석·서식을 보존한다."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:  # escape — 다음 문자 그대로(짝으로 소비)
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":  # 라인 주석 → EOL 까지 blank
            j = i
            while j < n and text[j] != "\n":
                out.append(" ")
                j += 1
            i = j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":  # 블록 주석 → `*/` 까지 blank(개행 보존)
            j = i
            while j < n and not (text[j] == "*" and j + 1 < n and text[j + 1] == "/"):
                out.append("\n" if text[j] == "\n" else " ")
                j += 1
            if j < n:  # 닫는 `*/`
                out.append("  ")
                j += 2
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _scan_array_end(masked: str, body_start: int) -> int:
    """배열 `[` 직후 body_start 부터 매칭되는 `]` 위치를 문자열/중첩 존중으로 찾는다(배열 body 끝).

    문자열 리터럴 내 `]`·중첩 `[...]` 은 건너뛴다. 닫는 `]` 부재(비정상)면 끝(len) 반환."""
    i, n = body_start, len(masked)
    depth = 0
    in_str = False
    while i < n:
        c = masked[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return n


def _find_toplevel_instructions(masked: str) -> tuple[int | None, int | None, int | None]:
    """주석-마스킹된 jsonc 에서 **최상위(depth==1)** `"instructions"` 배열을 brace-depth 추적으로 찾는다.

    중첩 객체(agent/provider 블록 등)의 `"instructions"` 는 무시한다() — opencode 가 읽는
    진입 지침 배열은 최상위 키 하나다. 문자열 리터럴 내 brace/bracket 은 세지 않는다(문자열 상태 추적).

    반환 (body_start, body_end, root_end):
      - 최상위 instructions 배열 존재 → (배열 `[` 직후, 닫는 `]` 위치, None): 검사/append 용.
      - 부재 → (None, None, 최상위 여는 `{` 직후 오프셋): 신설 블록 삽입 위치.
      - 최상위 `{` 부재(비정상) → (None, None, None)."""
    i, n = 0, len(masked)
    depth = 0
    root_end: int | None = None
    in_str = False
    while i < n:
        c = masked[i]
        if in_str:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            # 최상위(depth==1)의 "instructions" 키만 후보 — 중첩(depth>1)은 무시.
            if depth == 1:
                m = _INSTR_KEY_RE.match(masked, i)
                if m:
                    body_start = m.end()  # 여는 `[` 직후
                    return body_start, _scan_array_end(masked, body_start), None
            in_str = True
            i += 1
            continue
        if c == "{":
            depth += 1
            if depth == 1 and root_end is None:
                root_end = i + 1  # 최상위 여는 `{` 직후
            i += 1
            continue
        if c == "}":
            depth -= 1
            i += 1
            continue
        i += 1
    return None, None, root_end


def _ensure_jsonc_instructions(jsonc_text: str) -> tuple[str, bool]:
    """opencode.jsonc **최상위** `instructions` 배열에 신형 지침 경로를 idempotent 추가(comment-preserving).

    반환 (new_text, changed). 최상위 배열에 이미 (비-주석) 원소로 있으면 무변경. 최상위 배열이
    있으나 경로가 없으면 배열 앞에 삽입. 최상위 배열이 없으면 최상위 `{` 직후 신설 블록 삽입.
    JSONC(주석)라 json.load 불가 — 주석을 **오프셋 보존 마스킹**한 사본 위에서 **brace-depth 추적**
    으로 위치를 구하고 원본에 같은 오프셋으로 write 한다(비파괴·주석·타 키·provider 보존).

    **최상위(depth==1) 한정** (): 중첩 객체(agent/provider)의 `"instructions"` 가 파일에서
    먼저 나와도 그 중첩 배열에 삽입하지 않는다 — opencode 가 로드하는 진입 지침은 최상위 키다.
    등록-확인은 **quoted-string 원소 정확 대조**(substring 오인 방지·주석-아웃/`.bak` suffix)."""
    rel = _ENTRY_DOC_PM_INSTRUCTIONS_REL
    masked = _mask_jsonc_comments(jsonc_text)  # 주석 blank(오프셋 == 원본)
    body_start, body_end, root_end = _find_toplevel_instructions(masked)
    if body_start is not None:
        elements = _JSONC_STRING_RE.findall(masked[body_start:body_end])  # 주석-아웃 원소 제외
        if rel in elements:
            return jsonc_text, False  # idempotent — 최상위 배열에 이미 등록
        # **빈 배열**에 `"…",` 를 넣으면 뒤에 이을 원소가 없어 후행 쉼표만 남는다
        #   (`[\n    "…",]` — strict JSON parse 실패·실측). 비어 있으면 쉼표 없이 단독 원소 형태로
        #   (닫는 괄호 앞 개행 정렬·신설 블록과 같은 모양) 넣고, 아니면 현행대로 앞머리에 쉼표를 달아
        #   넣는다. 판정은 **마스킹본 배열 본문에 비-공백이 있는가** — 문자열 원소 유무로 좁히면
        #   비-문자열 원소(`[123]`)를 "빈 배열" 로 오인해 같은 결함이 다른 모양으로 재발한다(클래스
        #   폐쇄). 주석은 마스킹으로 공백이 되므로 주석-only 배열은 자연히 "비어 있음" 이다.
        insertion = (
            f'\n    "{rel}",' if masked[body_start:body_end].strip()
            else f'\n    "{rel}"\n  ')
        return jsonc_text[:body_start] + insertion + jsonc_text[body_start:], True
    if root_end is None:
        return jsonc_text, False  # 최상위 `{` 없음 — 비정상 config·무변경(안전)
    block = f'\n  "instructions": [\n    "{rel}"\n  ],'
    return jsonc_text[:root_end] + block + jsonc_text[root_end:], True


def _entry_doc_backup_root(dest_root: Path) -> Path:
    """중앙 백업 루트 `<dest>/.pm_import_backups/<DATE>/` (pm_import 채널 재사용·relpath 미러)."""
    return Path(dest_root) / _ENTRY_DOC_BACKUP_DIR / datetime.date.today().isoformat()


def _entry_doc_backup(dest_root: Path, rel: str, backup_root: Path) -> None:
    """`dest_root/rel` 을 `backup_root/rel` 로 복사(중앙 백업·relpath 미러링). 부재면 무동작."""
    src = Path(dest_root) / rel
    if not src.is_file():
        return
    dst = Path(backup_root) / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def migrate_entry_doc(effective_dest: Path, source_root: Path, *, write: bool) -> dict:
    """진입 doc 세대 마이그레이션 — self-update 흡수 경로 한정(호출부가 --target 게이트).

    구형 미수정 opencode `AGENTS.md`(세대 fingerprint clean-match) → 신형 공통 코어로 자동 교체
    (+백업·`opencode.jsonc` instructions 배열 idempotent 추가). 커스텀 흔적(FILL·손편집) → 무손·
    loud 안내. 이미 신형/재실행 → no-op 멱등(부분 전환 시 jsonc instructions 만 복구). `write=False`
    (dry-run)면 판정만 하고 파일을 쓰지 않는다. 반환 dict 는 finding 출력·테스트 단언 공용.

    status ∈ {'not_opencode','no_agents','no_new_template','migrated','loud_manual','noop','recovered'}.
    """
    dest = Path(effective_dest)
    jsonc_path = dest / ".opencode" / "opencode.jsonc"
    agents_path = dest / "AGENTS.md"
    result: dict = {
        "status": "not_opencode", "agents_replaced": False, "jsonc_updated": False,
        "backup_rel": None, "matched_generation": None,
    }
    # opencode 채택자 게이트 — opencode.jsonc 부재면 비-opencode(claude 등)·비발화.
    if not jsonc_path.is_file():
        return result
    if not agents_path.is_file():
        result["status"] = "no_agents"
        return result

    adopter_agents = agents_path.read_text(encoding="utf-8")
    operational_keys = _entry_doc_operational_keys()

    # 세대 clean-match 탐색 (구형 미수정?).
    captured = None
    matched_gen = None
    for idx, b64 in enumerate(_OLD_OPENCODE_AGENTS_GENERATIONS):
        try:
            gen_text = _decode_entry_doc_generation(b64)
        except Exception:  # noqa: BLE001 — 세대 디코드 실패는 그 세대 skip(다음 세대 시도).
            continue
        captured = _match_entry_doc_generation(gen_text, adopter_agents, operational_keys)
        if captured is not None:
            matched_gen = idx
            break

    if captured is not None:
        # ── 구형 미수정 → 자동 전환 ─────────────────────────────────────────
        new_tmpl_path = source_root / "templates" / "opencode" / "AGENTS.md"
        if not new_tmpl_path.is_file():
            # 신형 목적지(source) 부재 — fail-soft(무손·loud 아님·비정상 source 신호는 타 게이트).
            result["status"] = "no_new_template"
            return result
        # operational: 포획값(tagline 등 local.conf 미보유분) + local.conf(현재 진실·py/name/test_cmd 우선).
        local_op, _empty = _operational_from_local_conf(dest)
        operational = {**captured, **local_op}
        new_text = _render_new_entry_doc(
            new_tmpl_path.read_text(encoding="utf-8"), operational, operational_keys)
        if new_text is None:
            # operational 재렌더 미완 — 무손·loud 로 낙하(미완 파일을 쓰지 않는다).
            result["status"] = "loud_manual"
            result["matched_generation"] = matched_gen
            return result
        adopter_jsonc = jsonc_path.read_text(encoding="utf-8")
        new_jsonc, jsonc_changed = _ensure_jsonc_instructions(adopter_jsonc)
        result.update(status="migrated", matched_generation=matched_gen,
                      agents_replaced=True, jsonc_updated=jsonc_changed)
        if write:
            backup_root = _entry_doc_backup_root(dest)
            _entry_doc_backup(dest, "AGENTS.md", backup_root)
            if jsonc_changed:
                _entry_doc_backup(dest, ".opencode/opencode.jsonc", backup_root)
            agents_path.write_text(new_text, encoding="utf-8")
            if jsonc_changed:
                jsonc_path.write_text(new_jsonc, encoding="utf-8")
            result["backup_rel"] = (
                f"{_ENTRY_DOC_BACKUP_DIR}/{datetime.date.today().isoformat()}")
        return result

    # ── clean-match 실패 ────────────────────────────────────────────────────
    if _ENTRY_DOC_OLD_GEN_MARKER in adopter_agents:
        # 구형 세대이나 수정됨(FILL·손편집) → 무손·loud 안내(수동 병합·커스텀 보존).
        result["status"] = "loud_manual"
        return result
    # 신형/무관 — AGENTS.md 미터치. opencode.jsonc instructions 만 idempotent 보장(부분 전환 복구·멱등).
    adopter_jsonc = jsonc_path.read_text(encoding="utf-8")
    new_jsonc, jsonc_changed = _ensure_jsonc_instructions(adopter_jsonc)
    result["jsonc_updated"] = jsonc_changed
    result["status"] = "recovered" if jsonc_changed else "noop"
    if jsonc_changed and write:
        backup_root = _entry_doc_backup_root(dest)
        _entry_doc_backup(dest, ".opencode/opencode.jsonc", backup_root)
        jsonc_path.write_text(new_jsonc, encoding="utf-8")
        result["backup_rel"] = f"{_ENTRY_DOC_BACKUP_DIR}/{datetime.date.today().isoformat()}"
    return result


def _print_entry_doc_migration_finding(result: dict, *, dry_run: bool = False) -> None:
    """migrate_entry_doc 결과를 사람이 읽을 형태로 출력(loud 안내).

    'migrated'/'loud_manual'/'recovered' 만 출력 — 'noop'·'not_opencode'·'no_agents'·
    'no_new_template' 는 조용(정상/무관·노이즈 회피). 전환/복구 자체는 migrate_entry_doc 이 수행."""
    status = result.get("status")
    if status == "migrated":
        verb = "전환 예정" if dry_run else "전환"
        gen = result.get("matched_generation")
        tail = " + opencode.jsonc instructions 배열 추가" if result.get("jsonc_updated") else ""
        print(f"→ 진입 doc 세대 마이그레이션 {verb} — 구형 미수정 opencode AGENTS.md "
              f"(세대 #{gen})를 신형 공통 코어로 교체{tail}.")
        if dry_run:
            print("    (원본은 .pm_import_backups/<DATE>/ 에 백업 예정·적용 안 함)")
        elif result.get("backup_rel"):
            src = "AGENTS.md·opencode.jsonc" if result.get("jsonc_updated") else "AGENTS.md"
            print(f"    백업: {result['backup_rel']}/ (원본 {src})")
    elif status == "loud_manual":
        print("⚠️  진입 doc 세대 마이그레이션 — 구형 opencode AGENTS.md 를 감지했으나 커스텀 흔적"
              "(FILL·손편집)이 있어 자동 전환하지 않는다(무손).")
        print("    신형(공통 코어 + .opencode/pm-instructions.md + opencode.jsonc instructions)으로 "
              "수동 병합하려면:")
        print("      1) templates/opencode/AGENTS.md(신형 공통 코어)로 AGENTS.md 를 교체하고 "
              "커스텀(프로젝트 고유 제약 등)을 프로젝트 고유 제약으로 옮긴다.")
        print("      2) opencode.jsonc 최상위에 "
              '`"instructions": [".opencode/pm-instructions.md"]` 를 추가한다(기존 배열이면 경로 append).')
    elif status == "recovered":
        verb = "추가 예정" if dry_run else "추가"
        print("→ 진입 doc — opencode.jsonc `instructions` 배열에 .opencode/pm-instructions.md "
              f"{verb}(신형 정합·idempotent 복구).")


# ── instance-owned 어댑터 config 채널 (3-way 원장 + drift 보고) ────────────────
# 어댑터 config(`.codex/hooks.json`·`config.toml`·`.claude/settings.json`·`.opencode/opencode.jsonc`)
# 는 어느 manifest 에도 없어 이 sync 가 안 덮고, add-harness 재실행조차 기존 값이 다르면 byte
# 보존한다 — 상류의 *동작* fix 가 기존 채택자에 도달할 채널이 0 이었다(훅 차단→비차단 fix 를 든
# 릴리스가 나간 뒤에도 채택자가 차단판을 그대로 운영한 실측이 이 클래스다).
#
# 채널은 두 갈래이고 판정·분류의 단일 진실은 pm_import 다(소유 선언 → 채널 분류 → 원장 판정):
#   managed  무편집(dest 해시 == 원장 해시)이면 백업 후 현행 template 으로 갱신 + 후속 행동 안내.
#   report   갱신하지 않고 template 대비 drift 만 파일당 한 줄로 표기.
# 어느 갈래든 **편집분·원장 부재는 무조건 보존**한다(하한선) — 수용은 채택자가 명시 커맨드로 한다.
# 채택자가 실제로 칠 커맨드 — Windows 파사드를 병기한다(그 플랫폼엔 `./pm-config.sh` 가 없다·
#   같은 파일 add-harness 안내의 선례와 동형).
_ADAPTER_CONFIG_ACCEPT_HINT = "./pm-config.sh sync-adapter-config --accept"
_ADAPTER_CONFIG_LIST_HINT = (
    "./pm-config.sh sync-adapter-config --list (Windows 는 `.\\pm-config.cmd`)")
_ADAPTER_CONFIG_WINDOWS_HINT = "Windows 는 `.\\pm-config.cmd`"
_ADAPTER_BACKUP_HINT = ".pm_import_backups/<DATE>/"
# pm_import 자체를 아직 못 불러오는 복구 RUN에서도 채널 적용 여부를 판정할 최소 좌표.
# "존재하는 managed 대상"만 완료 게이트 대상이라는 계약 경계다. 파일이 하나도 없는 순수
# 엔진 복구 트리는 adapter 채널 unavailable이 아니라 적용 대상 없음(vacuous green)이다.
_ADAPTER_CONFIG_DEST_CANDIDATES = (
    ".codex/hooks.json", ".codex/config.toml", ".claude/settings.json",
    ".opencode/opencode.jsonc",
)


def _has_adapter_config_candidate(dest_root: Path) -> bool:
    """instance-owned config 경로 엔트리가 하나라도 있는가(읽기/쓰기 0).

    regular file만 세면 directory·broken symlink·권한 오류가 외곽 gate에서 채널 실행 자체를
    건너뛴다. 중앙 판정이 unavailable로 올릴 기회를 보존하도록 "부재가 확인된 경우"만 제외한다.
    """
    for relpath in _ADAPTER_CONFIG_DEST_CANDIDATES:
        try:
            (Path(dest_root) / relpath).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        return True
    return False


def sync_adapter_configs(dest_root: Path, source_root: Path, *, write: bool) -> dict:
    """instance-owned 어댑터 config 채널을 1회 돌린다 — 판정 결과 dict(출력은 호출부).

    `write=False`(dry-run)는 판정만 한다(파일·원장 미변경). pm_import 로드/판정 실패는
    `status="unavailable"` 로 반환한다. 엔진 파일 적용은 되돌리지 않지만 상위 완료 게이트는
    이를 non-green 으로 소비한다."""
    result = {"status": "ok", "updated": [], "preserved": [], "drift": [],
              "backfilled": [], "degraded": [], "blocking": [],
              "managed_converged": False}
    try:
        # **로드도 try 안**이다 — 부분 전파로 pm_import 사본이 없는 트리에서 로더가 던지면
        #   그 예외가 CLI 를 통째로 죽인다(엔진 복구 실행이 바로 그 형상이다).
        pm_import = _load_pm_import()
        if pm_import is None:
            return {**result, "status": "unavailable", "reason": "pm_import 로드 실패"}
        judgments = pm_import.judge_adapter_configs(dest_root, source_root)
    except Exception as exc:  # noqa: BLE001 — 판정 실패는 sync 를 막지 않는다(사본 skew 포함).
        # 사본 rev 혼합은 실행 중의 정상 과도 상태라 등록된 경계로 흡수한다 — 판정 불가는
        #   `unavailable` 로 내려가 완료 게이트가 rc1 로 재실행을 요구하고, 이미 착지한 엔진
        #   파일은 그대로 선다(여기서 올리면 그 위에서 traceback 으로 죽는다).
        _absorb_engine_rev_skew_for_recovery(exc, "sync_adapter_configs.judge")
        return {**result, "status": "unavailable", "reason": str(exc)}

    backfill_candidates = {
        judgment.relpath for judgment in judgments
        if judgment.status == "in-sync"
        and judgment.baseline_sha256 != judgment.dest_sha256
    }
    for judgment in judgments:
        if judgment.status == "in-sync":
            # 이미 상류와 같다 — write 경로가 아래에서 원장을 뒤늦게 채운다. 성공 여부는
            # 재판정으로 확인한 뒤에만 ``backfilled`` 로 보고한다(원장 write 실패 false-green 금지).
            continue
        summary = pm_import.adapter_config_drift_summary(judgment, dest_root)
        managed = judgment.mode == pm_import.ADAPTER_CONFIG_MANAGED
        if managed and judgment.status == "unedited":
            backup_rel = None
            if write:
                try:
                    # 판정 시점 해시를 넘겨 **판정↔쓰기 사이 동시 편집**을 엔진이 재검증하게 한다
                    #   (raced 면 아무것도 안 덮고 돌아온다). 백업·원자 교체·원장 확인도 그 안이다.
                    accepted = pm_import.accept_adapter_config(
                        dest_root, source_root, judgment.relpath,
                        expected_sha256=judgment.dest_sha256)
                except Exception as exc:  # noqa: BLE001 — 아래에서 skew·IO 만 보존으로 내린다.
                    # 한 파일의 실패가 이미 끝난 엔진 sync 를 traceback 으로 덮지 않게 보존 쪽으로
                    #   내린다(다음 실행이 같은 판정으로 재시도). 수용은 백업·원자 교체·원장까지
                    #   가느라 형제 락 seam 을 로드하므로 사본 rev 혼합도 같은 자리에서 보존으로
                    #   내린다(등록된 경계). 경로 안전 거부·루트 교체는 의도적 hard abort 라
                    #   그대로 올라간다.
                    if not _absorb_engine_rev_skew_for_recovery(
                        exc, "sync_adapter_configs.accept",
                    ) and not isinstance(exc, (OSError, ValueError)):
                        raise
                    result["preserved"].append({
                        "relpath": judgment.relpath, "status": "update-failed",
                        "summary": str(exc)})
                    continue
                if accepted.status != "accepted":
                    # `ledger-failed` 만 성질이 다르다 — 파일은 이미 바뀌었으므로 "보존" 으로
                    #   묶으면 거짓 보고다. 별도 버킷으로 낸다(둘 다 loud).
                    bucket = ("degraded" if accepted.status == "ledger-failed"
                              else "preserved")
                    result[bucket].append({
                        "relpath": judgment.relpath, "status": accepted.status,
                        "summary": accepted.detail})
                    continue
                backup_rel = Path(accepted.backup).relative_to(Path(dest_root)).as_posix()
            result["updated"].append({
                "relpath": judgment.relpath,
                "backup_rel": backup_rel,
                "note": pm_import.ADAPTER_CONFIG_REAPPROVAL_NOTE.get(judgment.relpath),
            })
            continue
        bucket = "preserved" if managed else "drift"
        result[bucket].append({
            "relpath": judgment.relpath, "status": judgment.status, "summary": summary})

    if write and (result["updated"] or backfill_candidates):
        # 갱신분·in-sync backfill 을 한 번에 기록한다(record 는 template 일치분만 담는다).
        try:
            pm_import.record_adapter_baseline(dest_root, source_root)
        except Exception as exc:  # noqa: BLE001 — 파일 적용은 보존, 재판정이 rc1로 승격.
            # 사본 rev 혼합도 같은 규칙이다(등록된 경계) — 원장 기록 실패는 아래 재판정이 같은
            #   red 로 올리고, 다음 실행이 원장을 수렴시킨다.
            _absorb_engine_rev_skew_for_recovery(
                exc, "sync_adapter_configs.record_baseline")
            result["degraded"].append({
                "relpath": ", ".join(sorted(backfill_candidates)) or "adapter_baseline.json",
                "status": "ledger-failed",
                "summary": str(exc),
            })

    # 완료 판정은 **쓰기 후 재판정**이 진실이다. accept가 파일을 바꿨으나 원장을 못 남긴 경우,
    # in-sync backfill write가 실패한 경우를 모두 여기서 같은 red로 잡는다. dry-run은 현재 상태를
    # 그대로 판정하므로 write 0 계약을 유지한다.
    try:
        final_judgments = pm_import.judge_adapter_configs(dest_root, source_root)
    except Exception as exc:  # noqa: BLE001 — 엔진 적용 결과는 보존하되 게이트는 red.
        # 첫 판정과 같은 경계·같은 이유로 사본 rev 혼합을 흡수한다(게이트는 여전히 red).
        _absorb_engine_rev_skew_for_recovery(exc, "sync_adapter_configs.judge")
        return {**result, "status": "unavailable", "reason": str(exc)}
    unconverged = pm_import.unconverged_managed_adapter_configs(final_judgments)
    result["blocking"] = [
        {"relpath": judgment.relpath, "status": status,
         "judgment_status": judgment.status}
        for judgment, status in unconverged
    ]
    result["managed_converged"] = not result["blocking"]
    final_by_rel = {judgment.relpath: judgment for judgment in final_judgments}
    result["backfilled"] = sorted(
        relpath for relpath in backfill_candidates
        if relpath in final_by_rel
        and final_by_rel[relpath].baseline_sha256 == final_by_rel[relpath].dest_sha256
    )
    return result


def _adapter_config_gate_failed(result: dict) -> bool:
    """동기 결과의 managed 완료 게이트 — unavailable/미수렴이면 True."""
    return (result.get("status") != "ok"
            or not result.get("managed_converged", False))


def _print_adapter_config_finding(result: dict, *, dry_run: bool = False) -> None:
    """어댑터 config 채널 결과 출력 — 변경/보존은 loud, 보고는 파일당 한 줄.

    조용한 갱신을 금지하는 게 이 출력의 목적이다: managed 갱신은 채택자가 훅을 다시 승인해야
    발화하므로 갱신 사실과 후속 행동이 같은 자리에서 보여야 한다. 소견이 없으면(정상·전부 최신)
    무출력이지만, **`unavailable` 은 조용히 넘기지 않는다**(형제 `_print_protected_hook_reinstall_
    finding` 동형) — 채널이 안 돌았다는 건 상류 동작 fix 가 이 채택자에 안 갔다는 뜻이라, 침묵하면
    그 사실을 알 방법이 아예 없다."""
    status = result.get("status")
    if status == "unavailable":
        print(
            "[경고] 어댑터 config 채널을 건너뛰었다 — "
            f"{result.get('reason')}. instance-owned config(hooks.json 등)의 상류 동작 fix 가 "
            "이번 동기에 반영되지 않았다.\n"
            "  → 먼저 pm-update로 엔진 전체를 동기화한 뒤 재실행하세요.\n"
            f"  → 현재 판정 조회: {_ADAPTER_CONFIG_LIST_HINT}",
            file=sys.stderr,
        )
        return
    if status != "ok":
        return
    for item in result.get("updated", []):
        verb = "갱신 예정" if dry_run else "갱신"
        print(f"→ 어댑터 config {verb} — {item['relpath']} (무편집 확인·상류 동작 fix 반영)")
        if dry_run:
            print(f"    (원본은 {_ADAPTER_BACKUP_HINT} 에 백업 예정·적용 안 함)")
        elif item.get("backup_rel"):
            print(f"    백업: {item['backup_rel']}")
        if item.get("note"):
            print(f"    ⚠️ {item['note']}")
    for item in result.get("preserved", []):
        reason = {
            "edited": "채택자 편집",
            "unrecorded": "원장 부재(구세대 설치)",
            "update-failed": "갱신 실패(다음 실행이 재시도)",
            "raced": "판정 뒤 동시 편집 감지",
            "ledger-blocked": "원장 기록 불가(상위 schema)",
            "write-failed": "원자 교체 실패",
            "ledger-failed": "원장 기록 미확인",
        }.get(item["status"], item["status"])
        print(f"⚠️  어댑터 config {item['relpath']} — {reason}이라 보존한다 "
              f"({item['summary']}).", file=sys.stderr)
        print(f"    상류 값을 받으려면: {_ADAPTER_CONFIG_ACCEPT_HINT} {item['relpath']} "
              f"(백업 후 교체 · {_ADAPTER_CONFIG_WINDOWS_HINT})", file=sys.stderr)
    for item in result.get("degraded", []):
        # 파일은 갱신됐는데 판정 기준(원장)이 안 남은 상태 — 조용히 두면 다음 동기가 이 파일을
        #   영구 보고 모드로 본다. 보존과 섞지 않고 실제 상태 그대로 알린다.
        print(f"⚠️  어댑터 config {item['relpath']} — 갱신은 됐으나 원장 기록을 확인하지 못했다 "
              f"({item['summary']}).", file=sys.stderr)
        print(f"    원장 경로 권한을 고친 뒤 `{_ADAPTER_CONFIG_ACCEPT_HINT} {item['relpath']}` 로 "
              f"기록을 복구하라(내용은 이미 상류 값이다).", file=sys.stderr)
    # in-sync byte지만 원장 backfill 실패 같은 상태는 preserved/degraded 버킷에 아직 없을 수 있다.
    already_reported = {
        item["relpath"] for bucket in ("preserved", "degraded")
        for item in result.get(bucket, [])
    }
    for item in result.get("blocking", []):
        if (item["relpath"] in already_reported
                or item.get("judgment_status") != "in-sync"):
            continue
        print(
            f"⚠️  어댑터 config {item['relpath']} — 내용은 상류와 같지만 원장 기록이 없어 "
            "수렴 완료로 볼 수 없다.",
            file=sys.stderr,
        )
        print(
            ("    다음 실 pm-update가 파일 byte를 바꾸지 않고 원장을 backfill한다. "
             "dry-run 없이 pm-update를 재실행하라."
             if dry_run else
             f"    원장화하려면: {_ADAPTER_CONFIG_ACCEPT_HINT} {item['relpath']} "
             f"(백업 후 확인 · {_ADAPTER_CONFIG_WINDOWS_HINT})"),
            file=sys.stderr,
        )
    drift = result.get("drift", [])
    for item in drift:
        print(f"→ 어댑터 config drift(보고 전용) {item['relpath']} — {item['summary']}")
    if drift:
        print(f"    위 파일은 채택자 소유라 갱신하지 않는다 — 상류 값을 받으려면 "
              f"`{_ADAPTER_CONFIG_ACCEPT_HINT} <경로>` ({_ADAPTER_CONFIG_WINDOWS_HINT}).")


# ── 훅 세트 세대 정합 검사 ───────────────────────────────────
# 어댑터 config 채널의 형제다. 채널이 "config 내용이 상류와 같은가" 를 보는 반면, 이 검사는
# "config 가 요구하는 훅 세대를 **설치된 래퍼/드라이버가 실제로 감당하는가**" 를 본다. 두 축의
# 갱신 주체가 달라(config = 채택자 소유·엔진 불가침 / 래퍼·드라이버 = manifest 등재) 세대 혼합
# 창이 구조적으로 열리고, 그중 config 가 앞선 조합은 PreToolUse rc2 = 도구 전면 차단이다
# (v1.7.0 흡수 실측). 검사는 읽기 전용이고, 판정·처방은 pm_import 단일 진실을 그대로 쓴다.


def check_adapter_hook_sets(dest_root: Path, source_root: Path) -> dict:
    """훅 세트 세대 정합을 1회 판정한다 — 결과 dict(출력은 호출부·write 0).

    판정 실패는 `status="unavailable"` 로 내린다(형제 `sync_adapter_configs` 와 같은 fail-soft
    — 이 검사가 성공한 엔진 동기를 traceback 으로 덮지 않는다). 엔진 사본 skew 만 예외."""
    result: dict = {"status": "ok", "findings": [], "reason": None}
    try:
        # 판정 선언은 **상류 세대**를 우선한다 — 상류가 이번에 들여오는 새 플래그를 설치본 선언은
        #   모르므로, 그 세대로 보면 요구를 "선언 밖 플래그" 로 접어 green 이 된다. 조회 성격이라
        #   상류를 못 읽으면 로컬 선언으로 내려가되(판정을 통째로 잃지 않는다) **사유는 알린다**.
        #   해소와 판정이 같은 형제 적재를 쓴다(사본 1회 exec).
        generation, pm_import = _resolve_hook_set_generation_and_sibling(source_root)
        if pm_import is None:
            return {**result, "status": "unavailable", "reason": "pm_import 로드 실패"}
        judge = pm_import.judge_adapter_hook_sets
        if _sibling_accepts_kwarg(judge, "declarations"):
            _print_hook_set_query_fallback(
                getattr(pm_import, "hook_set_query_fallback_lines", None), generation)
            findings = judge(dest_root, source_root,
                             declarations=generation.declarations)
        else:
            # 구세대 형제(선언 주입 이전 시그니처) — 새 키워드를 넘기면 TypeError 로 판정이 통째로
            #   `unavailable` 이 된다. 그 세대가 제공하던 판정(설치본 선언 기준)은 그대로 살린다.
            _warn_hook_set_downgrade(
                "훅 세트 상류 세대 선언 주입",
                "직전 세대 판정으로 내려간다(이번 상류가 새로 들여오는 플래그·묶음은 판정되지 않지만, "
                "그 세대가 아는 세대 불일치는 그대로 잡는다)")
            findings = judge(dest_root, source_root)
        remedy_lines = pm_import.hook_set_remedy_lines
    except Exception as exc:  # noqa: BLE001 — 판정 실패가 동기를 무효화하지 않는다.
        # 사본 rev 혼합도 등록된 경계로 흡수한다 — 이건 채널이 아니라 **가드**라, 판정 채널이
        #   혼합 트리에서 안 열리면 검사를 unavailable 로 접고(경고는 loud) 동기는 완주시킨다.
        _absorb_engine_rev_skew_for_recovery(exc, "check_adapter_hook_sets")
        return {**result, "status": "unavailable", "reason": str(exc)}
    result["findings"] = [
        {"harness": finding.harness, "config_relpath": finding.config_relpath,
         "kind": finding.kind, "subject": finding.subject,
         "unmet_paths": list(finding.unmet_paths), "remedy": finding.remedy,
         "detail": finding.detail, "remedy_lines": remedy_lines(finding)}
        for finding in findings
    ]
    return result


def _unverified_hook_scope_paths(pm_import, updated) -> tuple[str, ...]:
    """판정 채널을 잃었을 때 **거부해야 할** 스코프 경로 — 훅 네임스페이스 하위 전량.

    "결합 묶음을 검증할 수 없다" 는 인식 상태는 하나이므로 처분도 하나여야 한다(선언 미해소 폴백과
    같은 fail-closed). 네임스페이스 목록조차 못 얻는 형상(판정 채널 자체 부재)은 종전대로 loud
    통과다 — 거기서 거부하면 채널 없는 트리의 부분 복구가 통째로 잠긴다."""
    try:
        namespaces = tuple(f"{name}/" for name in pm_import.hook_set_namespaces(None))
    except Exception:  # noqa: BLE001 — 채널 부재/손상은 위 계약대로 통과(경고는 호출부가 낸다).
        return ()
    return tuple(path for path in updated if path.startswith(namespaces))


def _change_dest_paths(changes) -> list[str]:
    """change 목록 → dest 기준 relpath 목록 (판정 입력 정규화)."""
    return [str(change[0]).replace("\\", "/").strip("/") for change in changes]


def refuse_partial_hook_set_scope(scoped_changes, planned_changes,
                                  source_root=None) -> int:
    """경로 스코프가 결합 묶음을 반쪽만 갱신하면 **쓰기 전에** 거부한다 (rc 1·정상이면 0).

    `--paths` 는 어댑터 채널을 끄므로(요청 밖 write 0) 세대 검사가 전무하다 — 래퍼만 옮기고
    드라이버를 두면 "신 래퍼 + 구 드라이버" 락아웃을 손수 만들고도 rc0 다. 엔진 도구 부분 전파
    경고와는 **다른 축**이다: 저쪽은 rev 혼재를 알리기만 하고, 이쪽은 하네스가 잠기므로 차단한다.

    입력은 **해소된 계획**이다(원문 `--paths` 표기가 아니라 dest 좌표의 change 목록):
      `scoped_changes`  이번 실행이 실제로 갱신할 change
      `planned_changes` 스코프가 없었다면 갱신됐을 change(= 상류와 다른 것 전수)
    원문 표기를 비교하면 `@source` 상류 좌표(`templates/claude_code/.claude/…`)로 지목한 요청이
    dest 좌표(`.claude/…`) 선언과 교집합 0 이 되어 검사가 통째로 무발화한다. 계획을 입력으로 삼으면
    좌표 표기 전반이 한 번에 닫히고, **이미 최신인 형제**는 `planned` 에 없어 거짓 거부도 없다.

    결합 묶음 선언은 **상류 세대**를 쓴다 — 상류가 이번에 들여오는 새 묶음을 설치본 선언은 모르고,
    그 첫 전파가 정확히 반쪽 갱신이 나는 자리다. 상류를 못 읽으면 설치본 선언으로 한 번 더 보되,
    **어댑터 훅 네임스페이스 하위를 건드리는 스코프는 통째로 거부**한다 — 그 세대의 결합을 검증할
    방법이 없고, 로컬 묶음 membership 으로 좁히면 "상류만 아는 묶음" 이 정확히 그 틈으로 빠진다
    (탈출구는 스코프 없는 전량 pm-update). 훅과 무관한 부분 전파는 종전대로 통과한다.

    형제가 **선언 주입 이전 세대**면 구 시그니처로 강등해 그 세대의 결합 묶음 판정을 살린다(loud) —
    새 키워드를 그대로 넘겨 TypeError 로 가드를 통째로 끄면, 그 세대가 이미 막던 반쪽 갱신까지
    rc0 으로 통과한다. 판정 채널을 못 불러오면 가드를 끄되 조용히 넘어가지 않는다(무진단 침묵 금지)."""
    updated = _change_dest_paths(scoped_changes)
    unverified: tuple[str, ...] = ()
    pm_import = None
    try:
        # 해소와 가드 판정이 같은 형제 적재를 쓴다 — 따로 로드하면 한 실행에 사본을 두 번 exec 한다.
        generation, pm_import = _resolve_hook_set_generation_and_sibling(
            source_root, required=True)
        partial_update = (getattr(pm_import, "hook_set_partial_update", None)
                          if pm_import is not None else None)
        if pm_import is None:
            partial = None
            print("[경고] 훅 세트 부분 전파 가드를 건너뛰었다 — 형제 pm_import 로드 실패(판정 "
                  "채널 없음). 이 스코프가 훅 세트를 반쪽만 갱신해도 막지 못한다.",
                  file=sys.stderr)
        elif (partial_update is not None
                and not _sibling_accepts_kwarg(partial_update, "declarations")):
            # 구세대 형제(선언 주입 이전 시그니처) — 새 키워드를 넘기면 TypeError 로 가드가 통째로
            #   꺼지고 반쪽 갱신이 rc0 으로 통과한다. 그 세대의 결합 묶음 판정은 그대로 살린다.
            _warn_hook_set_downgrade(
                "훅 세트 상류 세대 선언 주입",
                "설치본 선언의 결합 묶음으로 반쪽 갱신을 판정한다(상류만 아는 묶음은 못 본다)")
            # **인식 상태는 선언 미해소 폴백과 같다** — 이 사본에는 해소 지점 자체가 없어 상류
            #   세대를 확인할 방법이 없다. 그러니 처분도 같아야 한다: 구세대가 아는 묶음 판정은
            #   유지하되(아래 호출), 훅 네임스페이스 하위를 건드리는 스코프는 fail-closed 로
            #   거부한다. 강등을 이유로 이 검사만 빼면 **상류에만 추가된 결합 묶음**의 반쪽 전파가
            #   정확히 이 분기로 빠져나간다(구 판정자는 그 묶음을 정의상 모른다). 네임스페이스
            #   목록조차 못 얻는 사본은 종전대로 loud 통과다(판정 단위를 얻을 방법이 없고, 거기서
            #   막으면 채널 없는 트리의 부분 복구가 통째로 잠긴다).
            unverified = _unverified_hook_scope_paths(pm_import, updated)
            partial = partial_update(updated, _change_dest_paths(planned_changes))
        else:
            if generation.declarations is None:
                # 조회 폴백(설치본 선언)으로 판정은 계속한다 — 그 세대가 아는 반쪽 갱신은 여전히
                #   잡는다. 다만 fail-closed 대상은 **로컬 결합 묶음으로 좁히지 않는다**: 상류에만
                #   있는 묶음은 정의상 로컬이 모르므로, membership 으로 좁히면 지금 닫으려는
                #   케이스가 그대로 빠져나간다. 훅이 사는 **네임스페이스** 하위를 건드리면 전부
                #   거부하고, 훅과 무관한 경로는 종전대로 통과시킨다.
                generation = resolve_hook_set_generation(source_root)
                namespaces = tuple(
                    f"{name}/" for name in
                    pm_import.hook_set_namespaces(generation.declarations))
                unverified = tuple(
                    path for path in updated if path.startswith(namespaces))
            partial = pm_import.hook_set_partial_update(
                updated, _change_dest_paths(planned_changes),
                declarations=generation.declarations)
    except Exception as exc:  # noqa: BLE001 — 가드 판정 실패가 복구 전파를 자기잠금하면 안 된다.
        # 사본 rev 혼합도 등록된 경계로 흡수한다 — 부분 동기 트리에서 돌리는 복구 전파가 자기
        #   가드의 형제 로드 실패로 잠기면 안 된다는 이 함수의 계약이 그대로 적용된다.
        _absorb_engine_rev_skew_for_recovery(exc, "refuse_partial_hook_set_scope")
        partial = None
        # **인식 상태는 위 폴백과 같다** — 결합 묶음을 검증할 방법이 없다. 그러니 처분도 같아야
        #   한다: 흡수 경로로 들어왔다는 이유만으로 훅 영역 반쪽 갱신이 rc0 으로 통과하면 이 가드는
        #   skew 한 줄로 우회된다. 훅 네임스페이스 하위만 거부하고(무관 경로는 종전대로 통과)
        #   탈출구는 스코프 없는 pm-update 다 — `--paths` 한정이라 복구 채널은 잠기지 않는다.
        unverified = _unverified_hook_scope_paths(pm_import, updated)
        print(f"[경고] 훅 세트 부분 전파 가드를 건너뛰었다 — {exc}.", file=sys.stderr)
    if unverified:
        print(
            "[중단] 상류 훅 세트 세대 선언을 확인할 수 없어 어댑터 훅 영역의 부분 전파를 거부한다 — "
            f"이 스코프가 결합 묶음을 반쪽만 갱신하는지 검증할 방법이 없다: {', '.join(unverified)}\n"
            "  → 스코프 없이 pm-update 를 돌리거나(전량 전파는 이 가드 대상이 아니다) `--from` 이 "
            "온전한 프레임워크 checkout 인지 확인하라.",
            file=sys.stderr,
        )
        return 1
    if not partial:
        return 0
    for harness, left_behind, required in partial:
        print(
            f"[중단] 경로 스코프가 {harness} 훅 세트를 반쪽만 갱신한다 — 갱신이 필요한데 스코프 "
            f"밖인 {len(left_behind)}건: {', '.join(left_behind)}",
            file=sys.stderr,
        )
        print(
            "  훅 세트는 한 세대 단위로만 정합이다(래퍼·드라이버·공유 코어). 반쪽 갱신은 "
            "미지원 플래그 rc2 = 도구 차단을 만든다.\n"
            f"  → 이 묶음을 함께 지목하거나(--paths {' '.join(required)}) 스코프 없이 pm-update 를 "
            "돌려라.",
            file=sys.stderr,
        )
    return 1


def _adapter_hook_set_gate_failed(result: dict) -> bool:
    """훅 세트 검사의 완료 게이트 — 불일치가 하나라도 남으면 True.

    `unavailable` 은 게이트를 막지 않는다(형제 config 채널과 다른 판단): 이 검사는 채널이 아니라
    가드라, 판정 불가를 red 로 올리면 구형/부분 트리에서 복구 실행 자체가 자기잠김한다. 대신
    침묵하지 않고 경고를 낸다."""
    return bool(result.get("findings"))


def _print_adapter_hook_set_finding(result: dict, *, dry_run: bool = False) -> None:
    """훅 세트 검사 결과 — 정합이면 무출력, 불일치는 loud 처방(사유 + 실행 커맨드).

    dry_run 은 문구만 바꾼다(판정 자체가 읽기 전용이라 write 경로와 결과가 같다)."""
    status = result.get("status")
    if status == "unavailable":
        print(
            "[경고] 어댑터 훅 세트 세대 검사를 건너뛰었다 — "
            f"{result.get('reason')}. 설치된 훅 래퍼/드라이버가 채택자 config 가 요구하는 "
            "세대인지 확인되지 않았다.",
            file=sys.stderr,
        )
        return
    for finding in result.get("findings", []):
        print(f"⚠️  어댑터 훅 세트 세대 불일치({finding['harness']}) — {finding['detail']}.",
              file=sys.stderr)
        for line in finding["remedy_lines"]:
            print(f"    → {line}", file=sys.stderr)
    if result.get("findings") and dry_run:
        print("    (dry-run — 판정만 했다. 위 처방을 적용한 뒤 실 pm-update 를 돌려라.)",
              file=sys.stderr)


# ── 보호 훅 전수 재설치 트리거 ────────────────────────────────
# 보호 훅(`.local/repo-hooks/<repo>/pre-push`·`pre-commit`)은 엔진 코드(worktree_pool 의 훅
# 본문 상수)에서 *생성*되는 런타임 산출물이라, 엔진 파일이 갱신돼도 **재설치가 돌아야** 새 훅이
# 디스크에 놓인다. 그런데 기존 설치 트리거는 `repo add`·`worktree add` 둘뿐이었다 — 즉 엔진
# 업그레이드만 한 채택자는 새 훅(예: pre-commit 가드)을 **영영 못 받는다**(값-연결이
# 끊긴 채 green·[[robustness-value-connections-before-ship]]). 그래서 매 sync **실행마다** 등록
# repo 전수 정합 확인 + drift 재설치를 신설한다.
#
# ⚠ **`changes` 유무로 게이트하지 않는다**(내부/외부 게이트 must-fix·격리 실측): 업그레이드
# 경계에서 sync 를 *실행하는 주체는 dest 의 구 엔진*이다 — 이 기능을 배달하는 그 sync 자체는
# 재설치 코드를 갖고 있지도 않다(RUN 1 미발화). 바로 다음 실행은 dest 가 신 엔진이지만
# `changes == 0` 이라, "changes>0 에서만" 으로 좁히면 **다음에 우연히 엔진이 또 바뀔 때까지**
# 훅이 안 깔린다(RUN 2 미발화). 그래서 옆의 `migrate_entry_doc` 와 **동형으로** changes 0 경로
# 에서도 돈다. 노이즈는 트리거를 끄는 대신 **정합이면 조용**(아래 `_protected_hook_in_sync`
# drift 판정)으로 낮춘다 — sidecar reconcile 의 "비교 우선·정합이면 subprocess 0" 과
# 같은 패턴이라 새 개념이 늘지 않는다. 이 판정은 훅 디렉토리가 통째로 지워진 clone 도 덮는다
# (bootstrap reconcile 은 sidecar 파일이 없으면 즉시 return 이라 그 상태를 영구 침묵한다).
#
# 배선은 **기존 계약을 그대로 탄다**(신규 seam 0): dest 의 `pm_config._install_protected_hook_
# reporting` → `_resolve_repo_protected`(areas 권위) →
# `worktree_pool.install_protected_hook`(훅·sidecar·hooksPath). pm_update 는 목록 해소도 훅
# 본문도 재구현하지 않는다.
#
# **dest 의 엔진**을 로드한다(source 아님) — sync 로 방금 갱신된 사본이 새 훅 본문을 들고 있고,
# 등록 repo 레지스트리(areas.md)·훅 디렉토리도 dest(PM 홈) 소유다. `--target`(루트→templates
# 엔진 export)은 비발화 — templates/<name> 은 PM 홈이 아니라 출하 스캐폴드라 등록 repo 가 없다
# (selfheal/skew/진입 doc 마이그레이션과 같은 경계).
def _load_dest_pm_config(dest_root: Path):
    """dest(방금 동기된) `.project_manager/tools/pm_config.py` 를 로드 (_load_pm_import 동형).

    실행 중인 pm_update 프로세스는 **sync 이전** 코드를 메모리에 들고 있으므로, 재설치는 반드시
    디스크의 *새* 사본을 로드해서 돌려야 신 훅 본문이 배포된다. 부재(구형 dest·엔진 미배치)면
    None — 호출부가 fail-soft 로 보고한다. 로드 예외는 전파(호출부가 잡아 loud 보고)."""
    pm_config_py = Path(dest_root) / ".project_manager" / "tools" / "pm_config.py"
    if not pm_config_py.exists():
        return None
    return _load_module_from_path(
        pm_config_py, "pm_config.py", allow_unverified=True,
    )


def _read_hook_artifact(path: Path) -> str | None:
    """설치된 훅 산출물(훅 본문·sidecar) 1개를 읽는다 — 부재/읽기 실패는 **None**.

    `_protected_hook_in_sync` 의 유일한 읽기 창구다. 부재와 **읽기 실패**(non-UTF-8 로 깨진
    본문·권한·IO 오류)를 *같은* None 으로 수렴시키는 게 요점 — 둘 다 "이 파일은 현 엔진 산출물이
    아니다" 라는 같은 결론이고, 따라서 같은 해소(재설치)로 가야 한다. 예외를 밖으로 내면 호출부의
    fail-soft 가 그걸 `unavailable`(=재설치 안 함)로 처리해 **깨진 훅이 영영 복구되지 않는다**."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None            # 부재·권한·IO — 재설치 대상.
    except UnicodeDecodeError:
        return None            # 깨진 본문(non-UTF-8) — 우리 산출물이 아니다 → 재설치 대상.


# 실행 비트 축을 볼 수 있는 플랫폼인가 — POSIX 만. Windows 는 실행 비트 개념이 다르다(NTFS 에
# mode 가 없고 `Path.chmod` 는 read-only 플래그만 만진다·`st_mode` 는 항상 0o666/0o444 계열).
# 거기서 이 축을 보면 **매 sync 거짓 drift → 무한 재설치**가 된다. git-for-windows 는 훅을 sh 로
# 돌리며 실행 비트를 요구하지도 않으므로 축 자체가 무의미하다. 테스트는 이 상수를 뒤집어
# Windows 거동을 hermetic 하게 친다(플랫폼 분기 실행 불요).
_EXEC_BIT_MEANINGFUL = os.name != "nt"


def _hook_artifact_executable(path: Path) -> bool:
    """산출물이 실행 가능한가 — 실행권한 축 (Windows 는 축 비활성이라 항상 True).

    **없으면 git 이 훅을 조용히 건너뛴다** — 본문만 비교하면 `chmod 0644` 된 훅이 `in_sync` 로
    오판돼 보호가 침묵 비활성화된다. stat 실패(부재·권한)는 `False`(drift·재설치)."""
    if not _EXEC_BIT_MEANINGFUL:
        return True
    try:
        return bool(path.stat().st_mode & 0o111)
    except OSError:
        return False


def _protected_hook_in_sync(repo: str, *, pm_config, worktree_pool, board) -> bool:
    """이 repo 의 설치된 보호 훅이 **현재 엔진과 정합**인가 — drift 판정.

    정합이면 재설치를 건너뛴다(매 sync 반복 출력 회피). "비교 우선·정합이면 조용" 은
    sidecar reconcile 과 같은 패턴이다.

    **축을 열거하지 않고 유도한다** — 봐야 할 것은 정의상 "`install_protected_hook` 이 쓰는 것"
    이므로, 그 함수와 **같은 명세**(`worktree_pool.protected_hook_artifacts`)를 읽어 산출물마다
    내용 실행권한(필요한 것만)을 대조하고, 파일이 아닌 bare `core.hooksPath` **배선**은
    `pm_config.protected_hook_wired()`로 본다. 판정이 자체 목록을 들면 설치가
    자랄 때 조용히 갈라진다 — 실제로 그 클래스가 연달아 났다(읽기 실패 축·실행 비트 축).

    **모르면 재설치 쪽으로 기운다**(fail-safe): 파일 부재·**읽기 실패**(깨진 본문·권한·IO)·
    실행권한 상실·명세 부재(구 엔진 사본)·배선 판정 불가(`None`)는 전부 drift 로 수렴한다 —
    재설치는 멱등이라 비용이 낮고, 반대 방향(조용히 stale 유지)은 보호가 꺼진 채 침묵하는
    실패모드다.

    **일반 판정 예외는 밖으로 내지 않는 게 계약**이다(fail-safe False). 단 엔진 사본
    불일치 marker는 계속 실행할 수 없는 상태라 재전파한다. 호출부의
    `unavailable` 은 "dest 엔진/모듈을 못 불렀다"(=판정 자체가 불가능)를 위한 상태지 "파일이
    깨졌다"가 아니다 — 후자를 unavailable 로 흘리면 재설치가 안 돌아 복구 경로가 사라진다."""
    try:
        artifacts_of = getattr(worktree_pool, "protected_hook_artifacts", None)
        if artifacts_of is None:
            return False       # 구 엔진 사본(명세 부재) — 판정 불가 → 재설치.
        # 설치가 받는 것과 **같은 입력**(areas 실효 보호목록)으로 기대 산출물을 유도한다.
        expected_list = list(pm_config._resolve_repo_protected(repo, board=board))
        gate_config = getattr(pm_config, "_protected_push_gate_config", None)
        if gate_config is None:
            return False       # 구 엔진 사본(형상 resolver 부재) — 재설치로 새 계약 배포.
        # read-only drift 판정은 steady-state 무출력 계약을 지킨다. 강등 경고는 실제 설치
        # 깔때기에서 1회 loud하게 나가며, 여기서는 같은 resolver 결과만 소비한다.
        gate_mode, test_cmd = gate_config(repo, board=board, report_downgrade=False)
        for artifact in artifacts_of(
                repo, expected_list, gate_mode=gate_mode, test_cmd=test_cmd):
            if _read_hook_artifact(artifact.path) != artifact.content:
                return False
            if artifact.executable and not _hook_artifact_executable(artifact.path):
                return False
        # 배선 축 — `False`(hooksPath 가 우리 디렉토리를 안 가리킴)면 훅이 아예 발화하지 않는다.
        # `None`(bare 부재·git 실패)은 판정 불가 → 재설치 시도(install 이 결과를 loud 보고).
        return pm_config.protected_hook_wired(repo, worktree_pool=worktree_pool) is True
    except Exception as exc:  # noqa: BLE001 — 일반 판정 실패만 drift로 수렴(단 skew 재전파).
        if _is_engine_rev_skew(exc):
            raise
        return False


def reinstall_protected_hooks(dest_root: Path, *, write: bool) -> dict:
    """등록 repo 전수 보호 훅 정합 확인 + drift 재설치 — 엔진 업그레이드 배포 트리거.

    **매 sync 실행마다** 돈다(changes 유무 무관 — 위 모듈 주석의 RUN1/RUN2 실측 참조).
    `write=False`(dry-run)면 판정만 하고 아무것도 쓰지 않는다(migrate_entry_doc 의 write 플래그
    동형). 반환 dict:
      - `status` — "done" / "no_repos"(등록 repo 0) / "unavailable"(엔진/레지스트리 미해소)
      - `targets` — 판정 대상 repo(= bare 미러 보유) · `in_sync` — 정합이라 건너뛴 repo(조용)
      - `drifted` — 재설치가 필요한 repo(⊆ targets) · `failed` — 그중 설치 실패
      - `no_bare` — 등록됐지만 `.repos/<repo>.git` 이 없어 게이트할 대상이 없는 repo
      - `reason` — unavailable 사유(사람이 읽는 1줄)

    **bare 부재는 실패가 아니다** — 게이트할 미러가 없으면 훅도 무의미하다(install 이 no-op
    False). 매 sync 마다 경고를 울리는 대신 `no_bare` 로 분리해 요약 1줄로만 surface 한다
    (침묵 아님·`_print_protected_hook_reinstall_finding`).

    **fail-soft** — sync 는 이미 성공했다. 일반 엔진 로드 실패(구형 dest)나 레지스트리
    파싱 실패가 update rc 를 바꾸면 안 된다 → 예외를 "unavailable"+사유로 강등하고 호출부가
    경고로 낸다(훅은 추가 가드·`_install_protected_hook` 의 fail-soft 계약과 동형). stamped
    sibling의 marked rev skew도 이 최외곽 복구 경계에서는 명시적으로 흡수한다. 여기서 update를
    중단하면 사본 불일치를 고칠 유일한 채널이 자기잠김하기 때문이다.

    ⚠ **"unavailable" 은 판정 자체가 불가능한 경우만**이다 — dest 엔진/레지스트리를 못 불러
    *어느 repo 도* 손댈 수 없는 상태. **개별 repo 의 훅 파일이 깨진 것은 unavailable 이 아니라
    drift** 다(`_protected_hook_in_sync` 가 읽기 실패를 False 로 수렴). 그 구분이 무너지면
    "깨진 훅을 발견했는데 재설치는 안 하는" 경로가 생겨 복구 채널이 사라진다."""
    result: dict = {"status": "unavailable", "targets": [], "in_sync": [],
                    "drifted": [], "failed": [], "no_bare": [], "reason": None}
    try:
        pm_config = _load_dest_pm_config(dest_root)
        if pm_config is None:
            result["reason"] = (
                f"{dest_root}/.project_manager/tools/pm_config.py 부재 — 로드 불가")
            return result
        board = pm_config._load_module("board", "board.py")
        worktree_pool = pm_config._load_module("worktree_pool", "worktree_pool.py")
        if board is None or worktree_pool is None:
            missing = "board.py" if board is None else "worktree_pool.py"
            result["reason"] = f"dest 엔진 {missing} 부재/로드 실패"
            return result
        repos = sorted(board.registered_repos())
        if not repos:
            result["status"] = "no_repos"
            return result
        for repo in repos:
            # bare 미러가 있어야 `core.hooksPath` 를 걸 대상이 있다(install 과 같은 가드).
            if not worktree_pool.bare_repo_path(repo).exists():
                result["no_bare"].append(repo)
                continue
            result["targets"].append(repo)
            if _protected_hook_in_sync(repo, pm_config=pm_config,
                                       worktree_pool=worktree_pool, board=board):
                result["in_sync"].append(repo)
                continue
            result["drifted"].append(repo)
            if not write:
                continue
            ok = pm_config._install_protected_hook_reporting(
                repo, board=board, worktree_pool=worktree_pool, action="(재)설치")
            if not ok:
                result["failed"].append(repo)
        result["status"] = "done"
        return result
    except Exception as exc:  # noqa: BLE001 — 재설치 실패가 성공한 sync 를 무효화하면 안 됨.
        recovery_skew = _absorb_engine_rev_skew_for_recovery(
            exc, "reinstall_protected_hooks",
        )
        result["status"] = "unavailable"
        result["reason"] = (
            f"엔진 사본 불일치(복구 sync는 유지): {exc}"
            if recovery_skew else f"{type(exc).__name__}: {exc}"
        )
        return result


def _print_protected_hook_reinstall_finding(result: dict, *, dry_run: bool = False) -> None:
    """reinstall_protected_hooks 결과 요약 (per-repo 성공/실패 줄은 pm_config 깔때기 소관).

    **정합이면 완전히 조용**하다(`in_sync` 만 있는 매 sync 의 정상 경로) — 트리거를 끄지 않고
    출력만 낮춘 게 이 함수다. 등록 repo 0(=`no_repos`)도 조용(걸 대상 없음). `unavailable` 은
    훅이 갱신되지 않았다는 뜻이므로 stderr 경고 + 재설치 커맨드를 낸다(침묵 무력화 금지)."""
    status = result.get("status")
    if status == "no_repos":
        return
    if status == "unavailable":
        print(
            "[경고] 보호 브랜치 훅 정합 확인/재설치를 건너뛰었다 — "
            f"{result.get('reason')}. 이 clone 의 훅은 **옛 엔진 본문**으로 남을 수 있다.\n"
            "  → 먼저 pm-update로 엔진 전체를 동기화한 뒤 재실행하세요.\n"
            "  → 그 다음 재설치(멱등): pm-config repo add <repo>",
            file=sys.stderr,
        )
        return
    drifted = result.get("drifted") or []
    if drifted and dry_run:
        print(f"→ 보호 브랜치 훅 (재)설치 예정: {', '.join(drifted)} "
              "(설치된 훅이 현 엔진과 불일치·적용 안 함)")
    no_bare = result.get("no_bare") or []
    if no_bare:
        print(f"→ 보호 훅 대상 아님(bare `.repos/<repo>.git` 부재): {', '.join(no_bare)} "
              "— 미러를 만들면(`pm-config repo add <repo>`) 훅이 걸린다.")


def _main(argv: list[str] | None = None) -> int:
    global _SYNC_RUN_SCOPE          # 종료 시 수렴 검증 대상(아래 dest 해소 지점에서 등록).
    global _PARTIAL_RUN_SCOPE       # 종료 시 흡수 장부 보고 대상(부분 전파 실행·같은 지점).
    ap = argparse.ArgumentParser(
        prog="pm_update.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "--from 생략 시 <dest>/.project_manager/local.conf 의 `upstream=` 값을 기본으로 쓴다 "
            "(pm_import 가 한 번 import 하면 자동 기록·--from 명시로 override 가능). "
            "단 upstream= 이 **URL**(릴리스 추적 기본)이면 엔진은 로컬 파일만 복사하므로 "
            "(git clone/fetch 안 함) 자동 진행하지 않고 명확한 에러로 멈춘다 — "
            "`pm-update` 스킬(URL→cache clone)을 쓰거나 `--from <로컬 checkout>` 을 명시하라. "
            "upstream 미등록이거나 그 경로가 부재/디렉토리 아님이어도 명확한 에러로 멈춘다(침묵 폴백 없음)."
        ),
    )
    ap.add_argument("--from", dest="source", required=False, default=None,
                    help="upstream 프레임워크 checkout 경로 "
                         "(생략 시 local.conf 의 upstream= 사용)")
    ap.add_argument("--dry-run", action="store_true")
    target_group = ap.add_mutually_exclusive_group()
    target_group.add_argument(
        "--target",
        metavar="NAME",
        help=(
            "루트에서 templates/<NAME>/ 타깃으로 동기화. "
            "REPO/templates/<NAME>/ 디렉토리가 존재하면 유효. "
            "생략 시 self-location(스크립트 위치 기준 dest) 사용."
        ),
    )
    target_group.add_argument(
        "--all-targets",
        action="store_true",
        help=(
            "루트에서 templates/ 직계 하위의 존재하는 모든 타깃으로 동기화. "
            "새 타깃도 디렉토리만 있으면 자동 포함한다. --target 및 --changes 와 함께 쓸 수 없다."
        ),
    )
    ap.add_argument(
        "--paths",
        metavar="PATH",
        action="extend",
        nargs="+",
        default=None,
        help=(
            "명시한 경로만 전파한다(opt-in 부분 전파·반복 지정 가능). manifest 등재 경로만 "
            "허용하며 미등재 경로는 거부한다(rc1·조용한 무전파 없음). 경로는 repo 루트 상대 "
            "(파일 또는 디렉토리)다. 전량 흡수가 아니므로 upstream_rev baseline 기록·진입 doc "
            "마이그레이션·보호 훅 재설치·동기 후 프롬프트는 건너뛴다(부분 전파를 '최신' 으로 "
            "박으면 drift-lint 가 거짓 침묵한다). --target·--all-targets 와 조합 가능하고 "
            "--changes 와는 함께 쓸 수 없다."
        ),
    )
    # ── read-only 변경점 확인 (실 sync 안 함) ──────────────
    ap.add_argument(
        "--changes",
        action="store_true",
        help=(
            "받은 upstream baseline(local.conf upstream_rev) ↔ 그 이후 upstream HEAD 변경점을 "
            "read-only 로 요약(실 sync 안 함). 엔진 영향(manifest 경로)/그 외 분리. "
            "upstream 이 로컬 checkout 일 때만(URL 은 명확 에러·git clone/fetch 안 함)."
        ),
    )
    ap.add_argument(
        "--count-only",
        action="store_true",
        help="--changes 와 함께: baseline..HEAD commit 개수 1줄만 출력(advisory/스크립트).",
    )
    ap.add_argument(
        "--log",
        action="store_true",
        help="--changes 와 함께: `git log --oneline baseline..HEAD` 커밋 목록을 꼬리에 출력.",
    )
    args = ap.parse_args(argv)

    # ── --count-only/--log 는 --changes 전용 (codex suggestion 2·CLI 오사용 차단) ──
    #    --changes 없이 주면 일반 sync 가 돌면서 두 옵션이 조용히 무시된다 → 명확 에러로 멈춘다.
    #    --all-targets 분기보다 **앞**이어야 한다 — 뒤면 자식 argv 에 안 실리는 두 옵션이 조용히
    #    무시된 채 실 동기화가 돈다(오사용 검증이 모든 모드에 선행).
    if (args.count_only or args.log) and not args.changes:
        misused = []
        if args.count_only:
            misused.append("--count-only")
        if args.log:
            misused.append("--log")
        print(
            f"오류: {', '.join(misused)} 는 --changes 전용 옵션이다 — --changes 와 함께 쓰라 "
            "(read-only 변경점 확인 모드). 일반 sync 에는 무효.",
            file=sys.stderr,
        )
        return 1

    # ── `--paths` 경로 스코프 입구 검증 (부작용 전·--all-targets 분기보다 앞) ──
    #    나쁜 값(절대경로·`..`)과 모드 충돌은 어떤 복사/기록보다 먼저 거른다. --all-targets 는 이
    #    검증을 통과한 값을 자식 argv 로 그대로 넘긴다(자식이 다시 같은 검증을 탄다).
    scope_paths: list[str] = []
    if args.paths is not None:
        if args.changes:
            print("오류: --paths 는 실 동기화 옵션이며 --changes(read-only 확인)와 함께 쓸 수 없다.",
                  file=sys.stderr)
            return 1
        scope_paths, rejected = _normalize_scope_paths(args.paths)
        if rejected:
            for reason in rejected:
                print(f"  [거부] {reason}", file=sys.stderr)
            print("오류: --paths 값이 repo 루트 상대 경로가 아니다 — 위 항목을 고쳐 다시 실행하라.",
                  file=sys.stderr)
            return 1
        if not scope_paths:
            print("오류: --paths 에 유효한 경로가 없다(빈 스코프는 전파 0 이라 무의미).",
                  file=sys.stderr)
            return 1

    # 전체 export 는 타깃 집합을 디렉토리에서 매번 발견한다. 단일 타깃 실행을 재사용해
    # manifest/안전 가드/출력의 의미를 갈라놓지 않는다. 한 타깃의 실패는 즉시 반환한다.
    if args.all_targets:
        # 자식 실행(`main` 재귀)이 모듈 전역 장부(`_SYNC_RUN_SCOPE`·`_ABSORBED_ENGINE_REV_SKEW`·
        #   수렴 판정 캐시)를 **자기 것으로** 초기화하고 소비한다. 그게 성립하는 전제는 이 분기가
        #   dest 해소보다 **앞**이라 부모가 스코프를 등록하지 않는다는 것 — 순서가 바뀌면 부모의
        #   스코프를 자식이 지워 검증이 조용히 사라지므로 전제를 기계로 못박는다.
        assert _SYNC_RUN_SCOPE is None and _PARTIAL_RUN_SCOPE is None, (
            "--all-targets 분기는 dest 해소(수렴 검증·흡수 보고 스코프 등록)보다 앞이어야 한다")
        if args.changes:
            print("오류: --all-targets 는 실 동기화 옵션이며 --changes 와 함께 쓸 수 없다.", file=sys.stderr)
            return 1
        target_names = discover_target_names()
        if not target_names:
            print("오류: templates/ 아래에 동기화할 타깃 디렉토리가 없다.", file=sys.stderr)
            return 1
        for target_name in target_names:
            child_argv = ["--target", target_name]
            if args.source:
                child_argv = ["--from", args.source, *child_argv]
            if args.dry_run:
                child_argv.append("--dry-run")
            if scope_paths:
                # 경로 스코프는 타깃마다 같은 값이다 — 정규화된 값을 넘겨 자식이 다시 같은 검증을
                #   통과하게 한다(타깃별로 스코프가 달라지는 경로 없음).
                child_argv.extend(["--paths", *scope_paths])
            rc = main(child_argv)
            if rc:
                return rc
        return 0

    # ── read-only 변경점 확인 — main 초입 early-return(실 sync 안 함).
    #    dest/source 해소는 _run_changes 안에서 sync 와 동일 경로(_resolve_dest_source)로 탄다.
    if args.changes:
        return _run_changes(args)

    # dest/source 해소(--target·--from·URL 게이트·stale 가드)는 --changes 와 공유한다.
    rc, dest_root, source_root = _resolve_dest_source(args)
    if rc != 0:
        return rc
    effective_dest = dest_root if dest_root is not None else REPO
    if not scope_paths:
        # 종료 시 수렴 검증 대상 등록(흡수의 짝) — 부분 전파는 혼합이 정상 결과라 제외한다.
        #   dry-run 은 판정·보고만 하고 rc 를 세우지 않는다(무write 실행의 rc 는 "계획이 온전한가"다).
        _SYNC_RUN_SCOPE = (effective_dest, source_root, not args.dry_run)
    else:
        # 부분 전파는 수렴 검증 대상이 아니지만 **흡수 장부는 보고 대상**이다 — 계획-전 형제 로드는
        #   스코프와 무관하게 돌아 skew 를 흡수하는데, 비엔진 경로만 지목한 실행은 그 뒤 채널을
        #   전부 건너뛰고 rc0 으로 끝나 흡수 사실을 알릴 자리가 없다(report-only·rc 불변).
        _PARTIAL_RUN_SCOPE = (effective_dest, source_root)

    # ── guest 절 해소(읽기 전용) — 스코프 선검증·계획·기록이 **같은 산출**을 쓴다. 엔진 행은 upstream
    #    flavor 에서 매 실행 재파생하므로, 절에 아직 기록되지 않은 파생분도 이 시점에 확정된다.
    guest_entries = _dest_guest_manifest_entries(effective_dest)
    guest_backfill, guest_backfill_flavors = _guest_engine_backfill_entries(
        effective_dest, source_root, guest_entries)

    # ── `--paths` 소유권 선검증: **어떤 dest 쓰기보다 먼저**(중앙 로더 선복구 포함). 미등재 요청은
    #    rc1 로 끝나는데 그 전에 seam 복구 write 가 일어나면 "거부인데 파일이 바뀐다" 가 된다.
    #    판정은 읽기만 하고 관대하다(dest∪source manifest 합집합) — 좁은 판정은 계획 확정 뒤 한 번
    #    더 돈다. manifest 를 못 읽으면 선검증을 생략한다(정규 경로가 같은 오류를 더 정확히 낸다).
    if scope_paths:
        early_manifest = _scope_validation_manifest(effective_dest, source_root)
        if early_manifest is not None:
            # 파생 엔진 행은 **영속화 전**(첫 실행)엔 어느 파일에도 없다 — 합집합에 넣지 않으면
            #   이번 실행이 전파할 경로를 "미등재" 로 오거부한다(선검증만 파일을 읽는 비대칭).
            early_paths = {str(entry).replace("\\", "/") for entry in early_manifest}
            early_manifest = early_manifest + [
                entry for entry in guest_backfill
                if str(entry).replace("\\", "/") not in early_paths
            ]
            rc = _refuse_unregistered_scope_paths(
                early_manifest, scope_paths, _dest_guest_manifest_paths(effective_dest),
                effective_dest)
            if rc:
                return rc

    # dry-run은 무변경 계약을 지킨다. 실제 sync에서는 manifest 해석/출하 인벤토리보다 먼저
    # 중앙 seam을 복구해, missing/syntax/empty/old 어느 중단 상태에서도 같은 명령을 재실행한다.
    # **경로 스코프는 예외**다: 그 실행의 계약은 "명시 경로만 전파" 라, 스코프 밖인 이 seam 을
    # 고치면 요청하지 않은 파일이 바뀐다. 스코프에 그 경로가 들어 있으면 정상대로 복구하고,
    # 아니면 건너뛰되 **복구가 필요한 상태면 알린다**(조용한 방치 금지 — 전량 sync 로 안내).
    if not args.dry_run:
        loader_in_scope = not scope_paths or _in_scope_paths(
            _CENTRAL_LOADER_REL, scope_paths)
        if loader_in_scope:
            try:
                _predeploy_central_loader(source_root, effective_dest)
            except (OSError, RuntimeError) as exc:
                print(f"오류: 중앙 로더 선복구 실패 — {exc}", file=sys.stderr)
                return 1
        elif central_loader_needs_recovery(effective_dest):
            print(f"  ⚠️ 중앙 로더({_CENTRAL_LOADER_REL})가 복구 대상이지만 경로 스코프 밖이라 "
                  "건드리지 않는다 — 전량 sync 를 돌리거나 그 경로를 --paths 에 포함하라.",
                  file=sys.stderr)

    # ── manifest 자기치유 (self-update 2-pass) — upstream engine.manifest 를 이번 sync 의
    #    계획 기준으로 승격해, 로컬 manifest 가 구형이어도 신규 등재분이 한 번의 실행으로 plan→apply
    #    에 실린다(회사 실측: bare CLI 흡수가 신규 등재분 미도달). manifest 자신도 self-prop 엔트리
    #라 같은 plan 안에서 로컬 파일이 upstream 판으로 갱신된다(별도 write 불요). upstream
    #    manifest 부재/읽기 실패는 fail-soft(로컬 유지) — baseline 억제가 그 잔여 경로
    #    안전망. --target(엔진 export)은 타깃 manifest 가 루트와 의도적으로 달라 승격하지 않는다
    #    (현행·아래 skew 검출과 동일 경계). 승격 후 skew 는 정의상 0(manifest==upstream).
    selfheal: dict = {
        "status": "skipped", "added": [], "removed": [],
        "manifest": None, "upstream_manifest": None,
        "upstream_manifests": [], "manifest_text": None,
        "merge_conflicts": [],
    }
    skew_manifest = None
    if not args.target:
        # 파생 경로를 함께 넘긴다 — 이번 실행이 등재할 파일을 "채널 없음" 으로 경고하면 같은 run 이
        #   자기모순을 낸다(legacy 코호트 첫 실행). 이미 해소한 산출이라 재파생도 없다.
        selfheal = resolve_manifest_selfheal(
            effective_dest, source_root,
            guest_backfill_paths={
                str(entry).replace("\\", "/") for entry in guest_backfill})
        _print_manifest_merge_conflicts(selfheal)

    # 계획 기준 manifest — 승격분 우선·없으면 dest 우선 로컬 해소. `--changes` 미리보기가 **같은
    #   헬퍼**를 소비한다(판정 사본 0: 미리보기 분류 기준 == 적용 계획 기준).
    # add-harness guest 절의 소유 채널 분리(`@render`=refresh 소유·계획 제외 / 비-`@render`=update
    #   채널·계획 합류 + 파생 백필)도 이 헬퍼 안에서 일어난다 — `--changes` 미리보기가 같은 기준을
    #   자동 상속한다(판정 사본 0).
    try:
        manifest = _resolve_planning_manifest(
            effective_dest, source_root, selfheal,
            guest_entries=guest_entries, guest_backfill=guest_backfill)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if selfheal["status"] == "legacy_preserved":
        # self-prop 계획 제외는 `_resolve_planning_manifest` 가 한다(미리보기와 공유). 여기선 skew
        # 대조 기준만 갈라 둔다 — 대조는 **원문 전체**를 봐야 upstream 신규 등재분을 정상 검출한다.
        skew_manifest = _resolve_local_manifest(effective_dest, source_root)

    # 파생 엔진 행의 **기록** 여부 — 전파는 위에서 이미 계획에 실었고, 여기선 절에 남길지만 정한다.
    #   `legacy_preserved`(로컬 manifest 불가침 선언)는 기록만 생략한다(선언 존중·파일 동기는 유지).
    #   그 밖에는 apply 의 절 재부착에 실어, 다음 실행부터 추론 없이 provenance 로 해소되게 한다.
    persisted_guest_backfill: list[str] | None = None
    if guest_backfill:
        recorded = {str(entry).replace("\\", "/") for entry in guest_entries}
        fresh = sorted(
            str(entry).replace("\\", "/") for entry in guest_backfill
            if str(entry).replace("\\", "/") not in recorded
        )
        if selfheal["status"] == "legacy_preserved":
            print("  ⚠️ guest 엔진 행은 동기하되 guest 절에는 기록하지 않는다 — 로컬 manifest "
                  "불가침(legacy_preserved) 선언을 존중한다. 절 기록은 이 인스턴스가 legacy "
                  "상태를 벗어난 뒤 다음 sync 에서 이뤄진다.", file=sys.stderr)
        else:
            persisted_guest_backfill = [
                _manifest_entry_line(entry) for entry in guest_backfill]
        if fresh:
            print(f"  guest 엔진 행 {len(fresh)}건 파생 "
                  f"(flavor: {', '.join(guest_backfill_flavors)}): {', '.join(fresh)}")

    # --target은 operational 전체 render를 끄되 @render 경로의 호출 표기 토큰만 target 이름으로
    # 조건부 렌더한다. adopter self-update는 선택된 flavor manifest 경로에서 같은 context를 파생한다.
    # root canonical manifest는 flavor template가 아니므로 None(기존 `/` token-form 유지).
    render_enabled = not args.target
    entry_notation_template = args.target if args.target else None
    try:
        notation_manifests = (
            []
            if args.target
            else _installed_entry_notation_manifests(
                effective_dest,
                source_root,
                selfheal.get("upstream_manifests") or [],
            )
        )
        entry_notation_templates = (
            {}
            if args.target
            else _entry_notation_templates_from_manifests(
                notation_manifests, source_root
            )
        )
    except RuntimeError as exc:
        # 엔진 사본 skew 는 삼키지 않는다 — 그걸 "context 실패 rc2" 로 덮으면 로드 경계 진단이
        #   사라진다. 동기 실행 중 발화하는 설치 하네스 판별의 skew 는 **안쪽 경계**
        #   (`_installed_entry_notation_manifests`)가 이미 흡수하므로, 이 지점은 그 밖에서 온
        #   사본 불일치를 최외곽까지 올려 보내는 방어선으로 남는다(무진단 침묵 금지).
        if _is_engine_rev_skew(exc):
            raise
        print(f"오류: {exc}", file=sys.stderr)
        return 2
    # 계획이 훑은 출하 좌표는 두 판정이 함께 쓴다(그래서 스코프 유무와 무관하게 모은다):
    #   - 경로 스코프의 등재 디렉토리 **안의 오타**(`adapterdir/typo.md`) — 등재 검증은 통과하나
    #     대응 파일이 없다. 변경 0 은 "이미 최신" 과 구분되지 않으므로 인벤토리 대응으로 닫는다.
    #   - 은퇴 후보 판정 — 상류가 실제로 공급하는 파일을 재판정 없이 걸러낸다.
    plan_inventory: set[str] = set()
    # 은퇴 판정은 **dest 좌표만** 대조한다 — `--paths` 오타 검출용 합집합에는 상류 좌표가 섞여 있어,
    #   그대로 넘기면 우연한 문자열 일치로 잔존물이 "상류가 공급함" 으로 접힌다(false negative).
    plan_dest_inventory: set[str] = set()
    changes, missing = plan(
        source_root,
        manifest,
        dest_root=dest_root,
        render_enabled=render_enabled,
        entry_notation_template=entry_notation_template,
        entry_notation_templates=entry_notation_templates,
        manifest_source_text=(
            selfheal.get("manifest_text")
            if len(selfheal.get("upstream_manifests", [])) > 1
            else None
        ),
        inventory_out=plan_inventory,
        dest_inventory_out=plan_dest_inventory,
        guest_backfill_lines=persisted_guest_backfill,
    )
    retired_files = _retired_manifest_files(
        source_root, manifest, effective_dest, plan_dest_inventory)

    # ── 경로 스코프 적용: **계획을 좁히는 게 아니라 계획 결과를 거른다.** manifest 항목은 디렉토리
    #    일 수 있어(예 `.claude/skills`) 항목 단위로 자르면 "그 디렉토리 안 파일 하나" 를 못 고른다 —
    #    plan 이 산출한 파일 단위 결과에서 고르면 경로 리매핑·render 판정을 그대로 물려받는다.
    #    등재 검증은 manifest 기준(요청이 어떤 항목과도 안 겹치면 rc1) — 오타가 조용한 무전파로
    #    끝나지 않게 한다. 계획 자체는 여전히 manifest 전량을 훑으므로 스코프 밖 항목의 구조 오류
    #    (출하 인벤토리 0 등)는 그대로 표면화된다 — 스코프는 *적용 범위*를 좁히지 실패 판정을
    #    끄지 않는다.
    if scope_paths:
        # 좁은 판정(계획 확정 manifest 기준) — 선검증은 합집합이라 관대했다. guest 절 소속은
        #   "오타" 가 아니라 채널이 다른 것이라 사유를 갈라 알린다.
        rc = _refuse_unregistered_scope_paths(
            manifest, scope_paths, _dest_guest_manifest_paths(effective_dest),
            effective_dest)
        if rc:
            return rc
        # 등재 디렉토리 **안의 오타**는 위 판정을 통과한다(`adapterdir/typo.md` 는 `adapterdir`
        #   등재와 겹친다) — 실제 계획 인벤토리(변경 0 포함)와 source 부재 보고 어디에도 대응이
        #   없으면 그 요청은 아무 파일도 가리키지 않는다. 조용한 rc0 무전파 대신 거부한다.
        # 부재 보고의 좌표는 dest·source 양쪽이다(`@source` 항목은 둘이 다르다) — 한쪽만 보면
        #   upstream 경로로 지정한 "source 부재" 요청이 오타로 오분류된다(rc2 여야 할 것이 rc1).
        source_rel_by_dest = {
            str(entry).replace("\\", "/").strip("/"):
                _source_root_rel(entry).replace("\\", "/").strip("/")
            for entry in manifest
        }
        missing_coordinates: set[str] = set()
        for rel in missing:
            dest_norm = str(rel).replace("\\", "/").strip("/")
            missing_coordinates.add(dest_norm)
            missing_coordinates.add(source_rel_by_dest.get(dest_norm, dest_norm))
        phantom = [
            path for path in scope_paths
            if not any(_paths_overlap(path, item) for item in plan_inventory)
            and not any(_paths_overlap(path, item) for item in missing_coordinates)
        ]
        if phantom:
            for path in phantom:
                print(f"  [대응 없음] {path}", file=sys.stderr)
            print(
                f"오류: --paths 경로 {len(phantom)}개가 이번 계획의 출하 인벤토리에 대응하지 "
                "않는다(등재 디렉토리 안의 오타·이미 폐기된 경로). 아무것도 전파하지 않고 멈춘다 — "
                "경로 철자와 upstream 에 그 파일이 있는지 확인하라.",
                file=sys.stderr,
            )
            return 1
        # 스코프 적용 **전** 계획을 보존한다 — 훅 세트 반쪽 갱신 판정이 "이번에 옮기는 것" 과
        #   "옮겨야 하는데 스코프 밖인 것" 을 dest 좌표로 대조하는 데 쓴다(아래).
        planned_changes = list(changes)
        changes = [
            change for change in changes
            if any(_in_scope_paths(candidate, scope_paths)
                   for candidate in _scope_change_candidates(
                       change[0], change[1], source_root))
        ]
        # source 부재 보고는 **항목 단위**(디렉토리 등재 하나)이고 `@source` 항목은 dest·upstream
        #   경로가 다르다 — dest 경로만 대조하면 upstream 경로로 지정한 요청의 부재 보고가 접혀
        #   "변경 없음 rc0" 라는 거짓 성공이 된다. 위에서 만든 양쪽 좌표를 그대로 쓴다.
        missing = [
            rel for rel in missing
            if any(
                _paths_overlap(coordinate, path)
                for coordinate in {
                    str(rel).replace("\\", "/").strip("/"),
                    source_rel_by_dest.get(
                        str(rel).replace("\\", "/").strip("/"),
                        str(rel).replace("\\", "/").strip("/")),
                }
                for path in scope_paths
            )
        ]
        print(f"  경로 스코프: {' '.join(scope_paths)} — 이 경로만 전파한다"
              "(baseline 기록·진입 doc 마이그레이션·훅 재설치·동기 후 프롬프트 생략).")
        # 엔진 사본은 rev 스탬프를 들고 있다 — 일부만 옮기면 트리 안에 rev 가 섞여 sibling 로더가
        #   skew 로 볼 수 있다. 부분 전파의 성질이라 차단하지 않고 알리기만 한다(최종 방어는 wave
        #   말 `--all-targets` 전량 전파 + parity 게이트).
        stamped = sorted({
            str(change[0]).replace("\\", "/")
            for change in changes
            if str(change[0]).replace("\\", "/").startswith(_ENGINE_TOOLS_PREFIX)
        })
        if stamped:
            print(f"  ⚠️ 엔진 도구 {len(stamped)}건이 스코프에 포함됐다 — 부분 전파는 트리 안 "
                  f"ENGINE_REV 를 섞을 수 있다(로더 skew 진단 유발 가능). 차단하지 않으니 wave "
                  f"마감에 `--all-targets` 전량 전파 + parity 로 수렴시켜라: {', '.join(stamped)}",
                  file=sys.stderr)
        # 훅 세트 반쪽 갱신은 알리는 데서 그치지 않는다 — 하네스가 잠기므로 **쓰기 전에** 거부.
        #   입력은 **해소된 계획**이다(원문 스코프 표기 아님) — `--paths` 는 dest 좌표와 `@source`
        #   상류 좌표를 모두 받으므로, 원문을 그대로 비교하면 상류 좌표 요청이 교집합 0 으로 빠져
        #   검사가 통째로 무발화한다(래퍼만 갱신하고 rc0).
        rc = refuse_partial_hook_set_scope(changes, planned_changes, source_root)
        if rc:
            return rc

    for r, _sp, _dst, kind in changes:
        # render path 는 byte-copy 가 아니라 재렌더 산출물 — PM 이 구분하게 [render] 로 표기
        # ([update] = byte-copy· dry-run 표기). new 든 update 든 render 면 [render].
        label = (
            "render"
            if (
                getattr(_dst, "render", False)
                or getattr(_dst, "entry_notation_template", None) is not None
            )
            else kind
        )
        print(f"  [{label}] {r}")

    # ── source 부재 항목 처리 (@target-owned skip · 양 모드 공통) ──
    # manifest 의 일부는 *target-owned 어댑터* 일 수 있다 — 엔진 upstream(루트)엔 source 가
    # 없고 타깃 자신만 보유하는 경로(예: opencode `.opencode/*`). 그런 항목은 upstream→dest
    # 전파 대상이 *아니므로* rc2 로 전체를 막는 대신 graceful skip + 안내 로그로 surface 한다
    # (침묵 skip 금지).
    #
    # skip 은 **`@target-owned` 항목 한정**이다(명시 마커). 옛 구현은 `@render` 를
    # 판별자로 썼으나 그건 틀렸다(codex 포착): `.claude/agents @render`·`.claude/skills @render`
    # 처럼 *루트 upstream 에 존재해야 하는 엔진 리소스*도 @render 라, 잘못된 --from/upstream 에서
    # 빠지면 rc2 대신 skip 으로 숨겨 엔진 누락을 은폐했다. `@target-owned` 는 @render 와 독립인
    # 명시 마커로, "upstream 이 안 들고 있어도 정상" 을 정확히 표시한다. non-`@target-owned`
    # 항목이 source-부재면 진짜 누락(오타·잘못된 --from·전파돼야 하는데 빠진 도구·@render 엔진
    # 리소스 포함)이므로 rc2 + 에러를 유지한다(silent skip 금지). 혼합이면 non-@target-owned 가
    # 전체를 막는다.
    #
    # 이 판별은 **양 모드(--target·self-update) 공통**이다. opencode 채택자의 self-update 는
    # manifest 에 `.opencode/* @target-owned` 가 있으나 upstream=프레임워크 루트(.opencode/
    # 부재·root=claude)라 source-부재 → 과거 rc2(전체 update 실패)였다. @target-owned 는 어느
    # 모드든 판별자이므로 self-update 에서도 skip 한다.
    if missing:
        # missing 은 path 문자열만 운반하므로 manifest 에서 각 path 의 @target-owned 플래그를
        # 복원한다(plan 의 render_enabled=False 는 copy/render 동작만 끄고 entry 플래그는 보존).
        target_owned_flag = {str(e): _entry_target_owned_flag(e) for e in manifest}
        owned = [r for r in missing if target_owned_flag.get(r, False)]
        engine_missing = [r for r in missing if not target_owned_flag.get(r, False)]
        for r in owned:
            print(
                f"  [skip] {r} — target-owned: upstream source 부재 "
                "(타깃 고유 @target-owned 어댑터·엔진 upstream 에 없음·전파 대상 아님)"
            )
        if engine_missing:
            for r in engine_missing:
                print(f"  [source 에 없음] {r}", file=sys.stderr)
            print(
                f"오류: 엔진 경로 {len(engine_missing)}개가 source 에 없음(non-@target-owned) — "
                "--from 경로가 올바른 엔진 upstream 인지 확인하라 "
                "(@target-owned 어댑터만 target-owned skip 대상).",
                file=sys.stderr,
            )
            return 2

    # ── manifest skew 탐지 — upstream engine.manifest 와 로컬(sync 에 쓰인) manifest
    #    를 대조해 "로컬에 없는 upstream 신규 등재 경로"(신규 엔진 파일)를 찾는다. 로컬 manifest
    #    가 구형이면 신규 경로가 이번 sync 로 도달하지 않으므로, 아래 baseline 갱신을 억제해
    #    drift-lint 가 계속 skew 를 울리게 한다(false-최신 차단). --dry-run 도 동일 대조 결과 표시.
    #
    #    **self-update(채택자 흡수) 경로 한정** — `--target`(루트→templates/<name> 엔진 export)은
    #    타깃별 manifest(templates/*/engine.manifest)가 루트와 *의도적으로* 다르므로(어댑터
    #    비대칭·@target-owned 등) 대조하면 대량 오탐 + baseline 억제가 된다. --target 은 검출/억제
    #    를 비발화하고 현행 거동(무조건 baseline 갱신)을 유지한다(codex must-fix).
    #
    #    **flavor-correct 대조 기준 통일** (): skew 대조 upstream manifest 는 selfheal 이
    #    해소한 *동일* flavor-correct 경로(`selfheal["upstream_manifest"]`)를 넘긴다 — 안 그러면
    #    flavor 채택자(@source self-prop)가 치유 후에도 root-only 경로(`.claude/agents` 등)를 skew
    #    오탐해 baseline 이 억제된다(승격 기준 == 탐지 기준). 승격되면 manifest==flavor
    #    upstream 이라 skew 는 정의상 0.
    skew_status, skew_new = (
        ("skipped", [])
        if args.target
        else detect_manifest_skew(
            skew_manifest if skew_manifest is not None else manifest,
            source_root,
            upstream_manifest=selfheal["upstream_manifest"],
            upstream_manifests=selfheal.get("upstream_manifests"),
        )
    )

    # ── 진입 doc 세대 마이그레이션 — self-update 흡수 경로 한정 ──
    #    --target(엔진 export)은 비발화(skew/selfheal 동일 경계). 구형 미수정 opencode AGENTS.md
    #    를 신형 공통 코어로 자동 전환(+백업·jsonc idempotent), 수정 흔적 있으면 무손·loud 안내.
    #    AGENTS.md·opencode.jsonc 는 instance-owned(manifest 밖)이라 changes 유무와 독립.
    #
    #    ⚠ 시퀀싱 (비파괴 보장): 실제 전환 write 는 **apply(changes) 성공 이후**에만 한다.
    #    apply 가 render/IO 로 중단되면 신규 등재분(예 `.opencode/pm-instructions.md`)이 lay down
    #    되지 않는데, 그 전에 AGENTS.md 를 신형(위임 공백 공통 코어)으로 갈고 jsonc 가 미-laydown
    #    파일을 참조하면 채택자가 반쪽 상태(위임 방법론 공백)에 갇힌다 — 구형은 인라인 자족이라
    #    전환 전이 더 안전한 역설. 따라서 apply 실패 시 채택자가 *완전한 구형*에 남도록, has-changes
    #    경로는 apply 뒤에서만 전환한다. changes 없음(=엔진 최신·신규 등재분도 이미 laydown)·dry-run
    #    (무write)은 apply 가 없으므로 각 경로에서 직접 처리한다. 각 경로 migrate 1회(write flag 만 상이).
    #    `--paths`(부분 전파)도 비발화다 — 요청 밖 인스턴스 파일을 고치지 않는다는 게 이 모드의
    #    전부이고, 진입 doc 전환은 요청하지 않은 write 다.
    do_migrate = not args.target and not scope_paths

    # ── 보호 훅 정합 확인 + drift 재설치 — migrate 와 **같은 경계·같은
    #    시퀀싱**(--target 비발화 · changes 0 경로에서도 write · dry-run 은 판정만). changes 로
    #    게이트하면 이 기능을 배달하는 sync(구 엔진이 실행)도, 그 다음 실행(changes 0)도 발화
    #    하지 않아 채택자가 가드를 못 받는다(격리 실측 RUN1/RUN2·모듈 주석). 반복 출력은
    #    `_protected_hook_in_sync` 정합 판정이 흡수한다(정합이면 무출력).
    #    `--paths` 는 migrate 와 같은 이유로 비발화(요청 밖 write 0).
    do_reinstall = not args.target and not scope_paths

    # ── instance-owned 어댑터 config 채널 — migrate/reinstall 과 **같은 경계·같은 시퀀싱**.
    #    `--target`(엔진 export)은 dest 가 출하 템플릿 트리라 채택자 config 개념이 없고,
    #    `--paths`(부분 전파)는 요청 밖 write 를 하지 않는다. changes 유무와 독립인 것도 같다 —
    #    이 채널이 나르는 건 manifest 밖 파일이라 엔진 변경 0 인 실행에서도 할 일이 있다.
    do_adapter_config = (
        not args.target and not scope_paths
        and _has_adapter_config_candidate(effective_dest)
    )

    if not changes:
        # 잔존 은퇴 파일은 changes 와 독립이다 — "변경 없음" 이 곧 "dest 가 상류와 같다" 는 아니다.
        _print_retired_manifest_files(retired_files)
        _print_manifest_selfheal_finding(selfheal, dry_run=args.dry_run)
        _print_manifest_skew_finding(skew_status, skew_new, dry_run=args.dry_run)
        if do_migrate:
            # 엔진 변경 0 = 이미 최신(신규 등재분도 laydown 완료) → 전환 write 안전(apply 무관).
            result = migrate_entry_doc(
                effective_dest, source_root, write=not args.dry_run)
            _print_entry_doc_migration_finding(result, dry_run=args.dry_run)
        if do_reinstall:
            # **업그레이드 배달 다음 실행이 여기로 온다**(dest 는 신 엔진·changes 0) — 훅이
            # 실제로 깔리는 지점이므로 migrate 와 동형으로 write 한다(정합이면 무출력).
            hooks = reinstall_protected_hooks(
                effective_dest, write=not args.dry_run)
            _print_protected_hook_reinstall_finding(hooks, dry_run=args.dry_run)
        if do_adapter_config:
            # 엔진 변경 0 인 실행에서도 돈다 — 이 채널의 대상은 manifest 밖이라 `changes` 와
            #   무관하고, 훅 재설치와 같은 이유로 여기가 실제 배달 지점이 되는 실행이 있다.
            configs = sync_adapter_configs(
                effective_dest, source_root, write=not args.dry_run)
            _print_adapter_config_finding(configs, dry_run=args.dry_run)
            # 세대 정합은 config 채널과 **같은 경계·같은 자리**에서 본다(둘 다 manifest 밖
            #   config 를 축으로 하는 판정이라 한쪽만 도는 실행이 있으면 안 된다). 게이트 순서는
            #   심각도순 — 세대 불일치는 하네스가 잠기는 상태고 원장 미수렴은 다음 실행이
            #   수렴시킨다(두 소견 자체는 이미 위에서 각자 출력됐다).
            hook_sets = check_adapter_hook_sets(effective_dest, source_root)
            _print_adapter_hook_set_finding(hook_sets, dry_run=args.dry_run)
            if not args.dry_run and _adapter_hook_set_gate_failed(hook_sets):
                print(
                    "[중단] 설치된 훅 세트가 채택자 config 가 요구하는 세대가 아니다 — 위 처방 후 "
                    "pm-update 를 재실행하라(방치하면 훅이 rc2 로 도구 호출을 막는다).",
                    file=sys.stderr,
                )
                return 1
            if not args.dry_run and _adapter_config_gate_failed(configs):
                # 엔진 파일에는 적용할 변경이 없었다는 사실만 말한다. adapter config 가 red인데
                # "최신/흡수 완료"라고 접으면 release가 false-complete 되므로 baseline 기록도 막는다.
                print(
                    "[중단] 엔진 manifest 변경은 0건이지만 managed 어댑터 config가 "
                    "미수렴이다 — 위 처방 후 pm-update를 재실행하고 "
                    "`pm-config sync-adapter-config --check`를 통과시켜라.",
                    file=sys.stderr,
                )
                return 1
        print("최신 — 변경 없음.")
        # RUN2 수렴 지점: 엔진을 배달한 RUN1은 구 pm_update로 실행될 수 있으므로, 새 엔진의
        # 변경 0 재실행에서도 경로 upstream의 baseline/seen 쌍을 확인한다. dry-run은 기존
        # 계약대로 local.conf를 절대 쓰지 않는다. manifest skew면 has-changes 경로와 동형으로
        # 두 키를 함께 억제해 반쪽 상태/거짓 drift를 만들지 않는다.
        #    `--paths` 는 baseline 을 건드리지 않는다 — 부분 전파를 "여기까지 흡수함" 으로 박으면
        #    나머지 미전파분이 drift-lint 에서 사라진다(거짓 최신).
        if not args.dry_run and not scope_paths:
            # 변경 0 경로에서도 opt-in/안내 — has-changes 경로와 **같은 순서·같은 게이트**.
            #   추가 리뷰어를 배달한 RUN1 은 구 엔진이 실행할 수 있고, 이미 최신인 채택자는
            #   changes 가 영영 0 이라 apply 경로로 오지 않는다. 여기서 묻지 않으면 미결정
            #   채택자가 첫 질문/안내를 한 번도 못 받는다(훅 재설치·migrate 와 같은 논거).
            #   재질문은 두 helper 의 "실키 있으면 no-op" 계약이 흡수한다.
            #   단 **미수렴 실행에서는 묻지 않는다** — 종료 rc 가 서는 실패 실행이라 baseline 과
            #   같은 이유로 그 답을 local.conf 에 박을 자리가 아니다(수렴한 뒤 실행이 묻는다).
            if converge_upstream_revs(
                    effective_dest, source_root, skew_status, skew_new):
                maybe_prompt_external_review(effective_dest)
                maybe_prompt_delegate_optin(effective_dest)
        return 0
    if args.dry_run:
        print(f"[dry-run] {len(changes)} 파일 변경 예정 (적용 안 함).")
        _print_retired_manifest_files(retired_files)  # apply 경로와 같은 보고(무write).
        _print_manifest_selfheal_finding(selfheal, dry_run=True)
        _print_manifest_skew_finding(skew_status, skew_new, dry_run=True)
        if do_migrate:  # 판정만(write=False·무부작용).
            result = migrate_entry_doc(effective_dest, source_root, write=False)
            _print_entry_doc_migration_finding(result, dry_run=True)
        if do_reinstall:  # 대상 해소만(write=False·무부작용).
            hooks = reinstall_protected_hooks(effective_dest, write=False)
            _print_protected_hook_reinstall_finding(hooks, dry_run=True)
        if do_adapter_config:  # 판정만(write=False·파일·원장 미변경).
            configs = sync_adapter_configs(effective_dest, source_root, write=False)
            _print_adapter_config_finding(configs, dry_run=True)
            _print_adapter_hook_set_finding(
                check_adapter_hook_sets(effective_dest, source_root), dry_run=True)
        return 0

    # 훅 세트 판정자는 **상류 세대**로 미리 해소해 넘긴다 — 이번 실행이 pm_import 자체를
    #   갱신하면 dest 사본의 선언은 구세대라, 이번 세대가 추가한 훅 경로가 바로 이 실행에서
    #   비원자 copy2 로 떨어진다(원자 write 가 영영 한 세대 늦게 도착).
    apply(changes,  # ← 실패 시 예외 전파 → 아래 전환 미도달(채택자 완전한 구형 유지).
          is_hook_set_path=resolve_hook_set_predicate(source_root))
    msg = f"✓ {len(changes)} 파일 동기화"
    print(msg)

    _print_retired_manifest_files(retired_files)
    _print_manifest_selfheal_finding(selfheal, dry_run=False)
    _print_manifest_skew_finding(skew_status, skew_new, dry_run=False)
    if do_migrate:
        # 전환 write 는 apply(changes) 성공 이후 — 반쪽 상태 방지().
        result = migrate_entry_doc(effective_dest, source_root, write=True)
        _print_entry_doc_migration_finding(result, dry_run=False)

    if do_reinstall:
        # apply 이후 — 방금 착지한 *새* 엔진 사본에서 훅 본문을 읽어 배포한다. 단
        # 이 경로만으로는 부족하다(배달 sync 는 구 엔진이 실행) — 위 changes 0 경로가 짝이다.
        hooks = reinstall_protected_hooks(effective_dest, write=True)
        _print_protected_hook_reinstall_finding(hooks, dry_run=False)

    if do_adapter_config:
        # apply 이후 — 백업/교체 write 를 엔진 적용 성공 뒤로 미룬다(엔진이 반쯤 적용된 트리에
        #   어댑터 config 만 새 세대로 앞서가지 않게·migrate 와 같은 시퀀싱 논거).
        configs = sync_adapter_configs(effective_dest, source_root, write=True)
        _print_adapter_config_finding(configs, dry_run=False)
        # apply 로 방금 착지한 **새 훅 세트**를 기준으로 판정한다 — 이 순서라야 "엔진 파일이
        #   뒤처져서 난 불일치" 가 이번 실행에서 저절로 해소되고, 그래도 남는 것만 red 다.
        hook_sets = check_adapter_hook_sets(effective_dest, source_root)
        _print_adapter_hook_set_finding(hook_sets, dry_run=False)
        if _adapter_hook_set_gate_failed(hook_sets):
            print(
                f"[중단] 엔진 파일 {len(changes)}건은 적용됐지만 설치된 훅 세트가 채택자 config "
                "가 요구하는 세대가 아니다 — 위 처방 후 pm-update 를 재실행하라"
                "(방치하면 훅이 rc2 로 도구 호출을 막는다).",
                file=sys.stderr,
            )
            return 1
        if _adapter_config_gate_failed(configs):
            # apply는 이미 완료됐다. rollback/성공 은폐 없이 엔진 적용 사실(위 ✓ 출력)을 남기고,
            # 전체 흡수 baseline·후속 성공 프롬프트만 차단한다.
            print(
                f"[중단] 엔진 파일 {len(changes)}건은 적용됐지만 managed 어댑터 config가 "
                "미수렴이다 — 위 처방 후 pm-update를 재실행하고 "
                "`pm-config sync-adapter-config --check`를 통과시켜라.",
                file=sys.stderr,
            )
            return 1

    # upstream_rev baseline 갱신 — 매 sync 마다 source(upstream) HEAD 를
    # local.conf 에 박아 drift-lint의 "마지막 동기 이후" 기준점을 최신화한다. 경로
    # upstream 이면 `upstream_seen_rev`(현재 관찰값)도 같은 rev 로 함께 기록한다(
    # 경로는 동기 시점 checkout rev 가 곧 관찰값·두 키가 어긋난 채 남으면 상시 거짓 drift). 단
    # **manifest skew**(로컬 manifest 구형·신규 등재분 미도달)면 갱신을 억제한다 —
    # baseline 을 최신으로 박으면 drift-lint 가 "최신"으로 침묵해 신규 엔진 파일 누락을 은폐한다
    # (회사 채택자 실측). skew 아님(정합·또는 upstream manifest 부재 fail-soft)이면 현행대로 갱신.
    # source 가 로컬 git checkout 일 때만(URL upstream 은 로컬 checkout 없어 graceful 생략).
    # best-effort — 기록 실패가 동기화 자체를 무효화하지 않는다(파일은 이미 적용됨). --target
    # 모드는 effective_dest(templates/<name>)의 conf 에 기록(루트 오염 방지·maybe_prompt 와 동형).
    # `--paths`(부분 전파)는 baseline·프롬프트를 건너뛴다 — 요청 경로만 옮긴 실행을 "전량 흡수"
    # 로 박으면 나머지 미전파분이 drift-lint 에서 사라진다(거짓 최신).
    if scope_paths:
        print("  (경로 스코프 — upstream_rev baseline 을 갱신하지 않는다: 나머지 경로는 "
              "여전히 미전파다.)")
        return 0
    # 미수렴이면 프롬프트도 건너뛴다 — baseline 억제와 같은 논거다(성공하지 않은 실행이 던진
    #   질문의 답을 local.conf 에 박으면, 그 실행의 rc1 과 기록이 어긋난다).
    if converge_upstream_revs(effective_dest, source_root, skew_status, skew_new):
        maybe_prompt_external_review(effective_dest)
        maybe_prompt_delegate_optin(effective_dest)  # 동기 후 delegate opt-in(TTY 질문·비TTY 안내)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI 경계: repo 출하 seam의 분류 오류만 짧은 실행 오류로 바꾼다.

    apply/render/IO 등 다른 예외는 프로그래밍·시퀀싱 오류이므로 기존처럼 호출자에게 전파한다.
    """
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        allow_unverified=True,
    )
    _console_encoding.configure_console_utf8()
    global _SYNC_RUN_SCOPE, _PARTIAL_RUN_SCOPE, _ENGINE_REV_CONVERGENCE
    _SYNC_RUN_SCOPE = None
    _PARTIAL_RUN_SCOPE = None
    _ENGINE_REV_CONVERGENCE = None
    _ABSORBED_ENGINE_REV_SKEW.clear()
    converged = True
    write_run = False
    try:
        rc = _main(argv)
    except EmptyShippingInventoryError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        rc = 1
    except Exception as exc:
        # 엔진 사본 skew 는 분류 이전에 그대로 올린다 — 아래 분기도 결과적으로 re-raise 하지만,
        #   skew 가 이 경계를 지난다는 사실을 명시해 둔다(_main 이 설치 하네스 판별을 품은 뒤로
        #   실제 도달 경로가 생겼다).
        if _is_engine_rev_skew(exc):
            raise
        # 중앙 seam 복구는 _main이 --from을 해소한 뒤 선행한다. 예외 타입 판별 때문에
        # main import 시점부터 seam을 요구하면 missing/syntax 복구가 다시 자기잠김된다.
        repo_files = None
        try:
            repo_files = _load_repo_owned_files()
        except Exception:  # noqa: BLE001 — 분류 보조 실패가 원 예외를 가리면 안 된다.
            pass
        if repo_files is None or not isinstance(exc, repo_files.RepoFilesGitError):
            raise
        print(
            "오류: source 출하 파일의 git 추적정보를 열거하지 못함 — "
            f"{exc}; 해당 checkout 경로와 git index 상태를 확인·복구한 뒤 다시 실행하라.",
            file=sys.stderr,
        )
        rc = 1
    finally:
        # 실행 중 흡수의 **짝** — 어느 종료 경로(정상 rc·게이트 rc1·오류 rc2·예외 전파)로 나가든
        #   한 번만 본다(판정은 캐시되어 baseline 억제와 같은 결과를 쓴다). 종료 지점이 늘어도
        #   검증이 빠질 자리가 없게 여기 하나로 묶는다.
        scope, _SYNC_RUN_SCOPE = _SYNC_RUN_SCOPE, None
        partial_scope, _PARTIAL_RUN_SCOPE = _PARTIAL_RUN_SCOPE, None
        if scope is not None:
            dest_root, source_root, write_run = scope
            converged = _verify_engine_rev_convergence(dest_root, source_root)
        elif partial_scope is not None:
            # 부분 전파 실행 — 수렴은 판정하지 않고(혼합이 정상 결과) 흡수 사실만 보고한다.
            #   rc 는 `_main` 이 낸 값 그대로다(report-only).
            _report_partial_run_absorption()
    # 미수렴은 성공으로 보고하지 않는다 — 혼합 `--from` 을 그대로 복사한 실행이 rc0 이면 그
    #   침묵이 다음 실행에도 이어진다(baseline 은 위에서 이미 억제됐다). 무write 실행(dry-run)은
    #   보고만 하고 rc 를 세우지 않는다.
    return rc if converged or not write_run else (rc or _UNCONVERGED_RC)


if __name__ == "__main__":
    sys.exit(main())
