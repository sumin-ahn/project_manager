#!/usr/bin/env python3
"""외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI.

사용:
    python3 .project_manager/tools/external_review.py [옵션]

동작:
  git diff <base> -- <paths> 추출 (시크릿 denylist 경로 자동 제외)
  (프로젝트 맥락 헤더 +) diff 결합 → 표준 프롬프트 생성
  외부 리뷰어 실행 (reviewer_cmd, stdin 으로 프롬프트 주입, read-only 권장)
  출력에서 판정(통과/반려)·must-fix 파싱
  결과 요약 stdout + 원문 파일 저장 (기본 = **소유 PM 홈**의 `.project_manager/.local/review/`
    + 공유 장부 `raw_outputs.json` · --output-dir 로 격리)

기본 비활성:
  - 코드 diff 가 *외부로 전송*되므로 기본 OFF. local.conf `external_review_enabled=true`
    또는 `board.py init` / `pm_update` 시 opt-in 으로 켠다. 비활성 시 actual 호출은
    no-op(exit 0)이고 `--dry-run` 은 항상 허용(로컬 미리보기·미전송), `--force` 로 1회 강제.
    단 빈/공백 diff 는 dry-run·비활성 포함 무조건 exit 1 (false-green 원천 차단).

종료 코드/신호:
  - 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + stdout 에 FALLBACK_INTERNAL
    (= 내부 code-reviewer 서브에이전트로 폴백하라는 신호)
    중단 판정: 주 = **무진행**(마지막 진행 출력 이후 침묵), 백스톱 = 벽시계. 값은 별도 상수가
    아니라 **reviewer_cmd 의 하네스 프로필**(pm_relay·위임 채널과 동일 테이블)에서 오고, 배포별
    조정은 local.conf `harness.<reviewer>.idle_timeout`/`.wall_timeout`(legacy
    `external_review_idle_timeout`/`external_review_timeout` 도 계속 유효)·일회성은
    `--idle-timeout`/`--timeout`. 어느 쪽으로 중단되든 그 시점까지 받은 출력은 원문 파일에
    보존한다(전량 폐기 금지).
  - must-fix 감지 → exit 1
  - 통과 → exit 0
  - 라운드 상한 초과(--gate 별) → exit 4 (실행 전 거부·전용 rc). 같은 게이트로 승인 없이
    판정 4회(local.conf external_review_round_limit) 또는 미완 2회
    (external_review_incomplete_round_limit)를 채우면 이후 실행을 기계 차단하고 "사용자 보고·대기"
    loud 안내를 낸다 — 사용자 승인 후 `--ack-rounds` 로 각 한도만큼 재개.

설계:
  - 어댑터 seam: 외부 도구를 `reviewer_cmd`(local.conf) 뒤로 격리 → codex 외 교체 가능.
    기본 `codex exec --sandbox read-only --skip-git-repo-check` (stdin 으로 프롬프트).
  - 도메인 외부화: 프로젝트 맥락은 `.project_manager/review_context.local.md`(인스턴스 소유)
    가 있으면 주입, 없으면 generic 헤더. 엔진 도구엔 도메인 콘텐츠 0.
  - subprocess DI (run_fn 매개변수) — 테스트에서 mock 주입 가능.
  - 외부 호출은 코드를 수정하지 않는다 (read-only 인자 사용 권장).
  - 리뷰어 가시 범위 격리: 리뷰어 프로세스는 PM 세션의 cwd/env/홈을 물려받지 않고, 저장소 밖에
    만든 **tracked 파일 거울**(PM 로컬 산출물·전사 부재)과 **세션·이력 없는 임시 홈**(선언된 인증/
    설정 파일만 복제)을 받는다. 판정 본문에 옛 리뷰 raw·세션 전사가 echo 되면 오염 진단을 loud 로
    남기고 판정을 불명확 처리한다. 격리 실패는 기본 차단이고 `--allow-unisolated-reviewer` 가
    유일한 탈출구다. 배포별 조정 키(local.conf):
      · `reviewer_env_keep_extra` — 리뷰어에게 물려줄 env 이름 추가(기본 allowlist 밖 인증 변수).
      · `reviewer_home_artifacts_extra` — 임시 홈에 복제할 인증/설정 파일 추가(홈 상대경로).
  - 시크릿 denylist (.env·*secret*·*credential*·*.key·*token*·*.pem 등) 파일은 diff 에서
    자동 제외한다. 제외 사실은 판정에 반영 — --paths 명시 지정분 제외는 차단(exit 1),
    --ticket/기본 암묵 수집분 제외는 종합 판정 라인에 병기. review_denylist_extra 로 추가 가능.
  - 라운드 상한 기계 차단: 외부 리뷰(과금·전송)가 무한 반복되지 않게 `--gate <T-NNNN>`
    별 라운드 장부(`.project_manager/.local/review_rounds.json`·per-clone·git-ignored)에 실 전송을
    count 하고, 승인 없이 limit(기본 4)회를 넘기면 실행 *전에* 거부(exit 4)한다. PM 자의 판단을
    기계 판정으로 대체 — 사용자 승인 후 `--ack-rounds` 로만 재개한다([[mechanize-dont-instruct-llm]]).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import io
import inspect
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, NamedTuple, Sequence

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

# ── 엔진 사본 rev 스탬프 (형제 사본 skew fail-loud) ──────────────────────
# baked 리터럴 — engine_rev.py --bump가 전 stamped 모듈과 함께 재작성한다.
ENGINE_REV = "v1.6.1"


def _verify_engine_rev(sibling_module, sibling_filename):
    """로드한 형제의 baked ENGINE_REV를 이 사본과 대조한다(skew만 fail-loud)."""
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
    """fail-soft 소비 지점에서 rev skew만 재-raise하기 위한 구조화 판정."""
    return getattr(exc, "_engine_rev_skew", False)


def _require_engine_sibling(path: Path, filename: str) -> None:
    """load-bearing 형제 모듈의 **부재**를 stale 사본과 같은 진단으로 번역한다 (fail-loud).

    부재는 raw `FileNotFoundError` 로 터져 복구 방법(pm-update 재동기)을 알려주지 않는다 —
    원인이 부분/수동 복사라는 점은 stale 사본과 같으므로 같은 marked skew 로 표출한다
    (board.py `_require_engine_sibling` 동형·self-contained 복제)."""
    if path.exists():
        return
    err = RuntimeError(
        f"엔진 사본 불완전 — 로더 {Path(__file__).name}(rev={ENGINE_REV!r})가 형제 "
        f"{filename} 을(를) 찾지 못했다: {path} (부분/수동 복사). `pm-update`(또는 "
        "pm_update.py)로 .project_manager/tools/ 전체를 재동기하라."
    )
    err._engine_rev_skew = True
    raise err


# ── REPO 앵커 (상향 탐색·board_root() graceful 탐지 동형) ──────────
# 하드코딩 `parents[2]` 는 tools 가 `<root>/.project_manager/tools/` 정확히 2단 깊이에 있다고
# 가정한다 — 채택자 형상(PM 홈/worktree 구조 상이·다른 깊이)에선 어긋난다.
# 스크립트 위치에서 부모 체인을 상향 탐색해 `.project_manager` 를 품은 첫(최근접) 조상을 REPO 로
# 삼아 견고화한다 — board.py `board_root()` 의 "존재할 때만 갈리고 없으면 현 위치 100% 폴백"
# 패턴과 동형(additive·회귀 0). REPO 는 module-level 상수로 유지해 hermetic 테스트가 monkeypatch
# 할 수 있게 한다(각 파일 self-contained — 공유 import 미도입·ticket_finish 와 동형 복제).

def _find_repo_root() -> Path:
    """스크립트 위치에서 부모 체인을 상향 탐색해 `.project_manager` 를 품은 첫 조상을 반환한다.

    `Path(__file__).resolve()` 부모 체인을 최근접부터 훑어 `.project_manager` 디렉토리를 자식으로
    가진 첫 조상을 REPO 로 반환한다(worktree/PM 홈 등 다른 깊이여도 마커로 견고 해소). 마커를
    못 찾으면 현행 `parents[2]` 로 폴백한다 — board_root() 동형의 graceful 폴백(회귀 0·additive).
    """
    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / ".project_manager").is_dir():
            return ancestor
    return here.parents[2]


REPO = _find_repo_root()
TICKETS_DIR = REPO / ".project_manager" / "wiki" / "tickets"  # legacy 별칭 (아래 _tickets_dir 가 board_root 추종)
LOCAL_CONF = REPO / ".project_manager" / "local.conf"  # per-clone (git-ignored)
REVIEW_CONTEXT_FILE = REPO / ".project_manager" / "review_context.local.md"  # 인스턴스 소유 overlay
STATUS_DIRS: tuple[str, ...] = ("open", "claimed", "blocked", "done")

# raw 산출/공유 장부의 앵커 = **소유 PM 홈**(diff 슬롯이 아니다). `_main` 이 명시 selector 로 해소한
# PM 홈을 한 호출 동안만 주입하고, 미주입(라이브러리 직접 호출)은 엔진 자기 앵커 REPO 로 폴백한다 —
# pm_delegate `_CONFIG_REPO_OVERRIDE or REPO` 와 같은 규칙이다. 기록과 조회가 같은 앵커를 타야
# PM 홈 장부를 읽는 `pm_delegate raw` 통합 조회가 게이트 raw 를 본다.
_PM_HOME_OVERRIDE: Path | None = None


class AnchorResolutionError(RuntimeError):
    """명시 입력에서 diff/board/config 앵커를 유일하게 해소하지 못한 오류."""


class PmHomeDemotion(NamedTuple):
    """소유 PM 홈 해소에 실패해 anchor 자신이 이 실행의 config 앵커가 된 근거.

    `candidates` 는 이 anchor 를 자기 worktree 로 등록한다고 주장한 PM 홈들이다 — 장부 손상 등으로
    소유 판정까지 가지 못했을 뿐 config 소유자 후보로는 남아 있다. 강등 실행의 필터 승계
    (`_conf_with_owner_filters`)가 이 후보에서 소유 PM 홈의 보호 선언을 되찾는다.
    """

    anchor: Path
    reason: str
    candidates: tuple[Path, ...]


def _load_board():
    """형제 board 엔진을 위치로 로드해 worktree lease 재앵커 판정을 승계한다."""
    path = Path(__file__).resolve().with_name("board.py")
    return _load_module_from_path(path, "board.py", verifier=_verify_engine_rev)


def resolve_pm_home_for_repo(
    anchor: Path, *, required: bool = False, warning_sink: list[str] | None = None,
    demotion_sink: list[PmHomeDemotion] | None = None,
) -> Path:
    """repo/worktree가 소속된 PM 홈을 lease 장부로 해소한다.

    실 board를 가진 repo는 자기 자신, 등록 linked worktree는 정확히 한 lease 소유 홈을
    반환한다. 일반 standalone repo는 자기 local.conf를 쓰도록 자기 자신을 반환한다.
    board가 필수면 장부 부재·손상·중복은 fail-loud다. board 불필요 실행은 한 줄 경고 후
    자기 repo를 standalone 앵커로 사용한다.

    `demotion_sink` 를 주면 그 폴백(강등)의 근거를 `PmHomeDemotion` 으로 담는다 — 호출부가
    소유 PM 홈 필터 승계/차단을 판단하는 입력이다. standalone·실 board 소유처럼 폴백이 아닌
    정상 해소는 아무것도 담지 않는다.
    """
    anchor = anchor.resolve()
    board = _load_board()

    # PM 홈 자기 checkout과 plain clone은 lease가 없는 정상 standalone 형상이다. linked
    # worktree만 소유자 재해소가 필요하다. board._resolve_read_board()는 티켓이 실재하는 홈만
    # 후보로 삼으므로, 아직 티켓이 하나도 없는 등록 슬롯의 config 소유자 판정에는 쓸 수 없다.
    if board._has_real_board(anchor / ".project_manager"):
        return anchor
    if not board._is_linked_worktree(anchor):
        return anchor

    search: list[Path] = list(anchor.parents)
    common = board._git_rev_parse(anchor, "--git-common-dir")
    if common is not None:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (anchor / common_path).resolve()
        search.extend((common_path, *common_path.parents))

    # config 소유자는 아직 ticket이 없는 PM 홈도 lease로 해소해야 한다. 다만 board.py보다
    # 넓게 보이는 tools-only checkout은 오류 후보가 아니다: 유효 lease match는 인정하되,
    # 장부 오류는 실 board 소유자에게서만 load-bearing으로 취급한다. board._resolve_read_board()가
    # 어떤 장부 오류든 유일 match보다 우선하는 것과 의도적으로 다르다: 이 복구 경로는 정상
    # 소유자를 제3자의 손상된 장부로 자기잠금하지 않고, 실제 중복 match만 모호성으로 차단한다.
    candidates: list[Path] = []
    seen: set[Path] = set()
    for path in search:
        home = path.resolve()
        if home in seen:
            continue
        seen.add(home)
        if not (home / ".project_manager").is_dir():
            continue
        if board._registers_worktree(home, anchor):
            candidates.append(home)

    matches: list[Path] = []
    errors: list[str] = []
    for home in candidates:
        matched, error = board._ledger_registration(home, anchor)
        if matched:
            matches.append(home)
        elif error is not None and board._has_real_board(home / ".project_manager"):
            errors.append(error)

    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        homes = ", ".join(str(home) for home in unique)
        resolution_error = (
            "이 앵커가 여러 PM 홈의 worktree lease 장부에 등록되어 소유자가 "
            f"모호합니다: {homes}"
        )
    elif errors:
        detail = "; ".join(errors)
        resolution_error = (
            "이 앵커의 소유 PM 홈 worktree lease 장부를 확정할 수 없습니다: "
            f"{detail}"
        )
    else:
        resolution_error = (
            "worktree lease 장부에서 소유 PM 홈을 찾지 못했습니다."
        )

    if required:
        raise AnchorResolutionError(f"{anchor}: {resolution_error}")
    warning = (
        "경고: PM 홈 해소 실패 — board가 필요 없는 실행이라 repo 자기 앵커를 사용합니다: "
        f"{anchor} ({resolution_error})"
    )
    if warning_sink is None:
        print(warning, file=sys.stderr)
    else:
        warning_sink.append(warning)
    if demotion_sink is not None:
        demotion_sink.append(
            PmHomeDemotion(anchor, resolution_error, tuple(candidates))
        )
    return anchor


# ── 강등 실행의 소유 PM 홈 필터 승계 (외부 송신 방향) ──────────────────────
# PM 홈 해소가 실패해 config 소유자가 anchor 자신으로 강등되면, 소유 PM 홈이 선언한
# `review_denylist_extra`/`review_paths` 가 빠진 채 diff 가 외부로 나간다 — 경고와 provenance 는
# loud 하지만 **필터가 좁아지는 방향**이라 비정상 상태의 송신이 무필터에 가까워진다. 그래서
# 강등을 감지하면 (1) 소유 PM 홈을 하나로 되찾을 수 있으면 그 **유효 필터**를 승계하고,
# (2) 되찾을 수 없으면 외부 송신 전에 차단한다.
#
# **검사 대상 = 이번 실행이 실제로 쓰는 conf 소유자 하나**다. 명시 앵커(--paths/--ticket) 실행은
# diff_root·소유 PM 홈을 해소한 *뒤에* 그 소유자만 검사한다 — 초기 엔진 컨텍스트가 강등·손상이어도
# 다른 repo 를 가리키는 절대 --paths 는 그 손상과 무관하므로, 먼저 검사하면 복구 채널이 자기잠긴다.
# 인자 없는 실행만 범위 파생 전에 검사하며, 그 실행의 선택 소유자는 정의상 최초 PM 홈이다(다르면
# 교차 소유 가드가 이미 차단).
#
# 탈출구 매트릭스 (선택된 소유자의 후보 수 × 그 conf 상태 × 명시 --paths):
#   유일 후보 · conf 정상(선언/미선언 무관) → 승계 후 진행 (--paths 유무 무관)
#   유일 후보 · conf 읽기 실패             → **항상 차단** — `--paths` 는 *범위*만 명시할 뿐
#                                            확인하지 못한 `review_denylist_extra` 를 대신하지 못한다
#   후보 0 또는 2+                          → --paths 없으면 차단 / 있으면 loud 경고 후 진행
# 마지막 줄만 탈출구인 이유: 어느 PM 홈도 이 실행을 지배한다고 확정할 수 없는 상태에서 사용자가
# 범위를 직접 지정한 것이 남은 유일한 복구 채널이다(자기잠김 금지).

# 승계 대상 = 값이 좁아지면 외부로 더 많이 나가는 축뿐이다(리뷰어/타임아웃 등 실행 프로필은 제외).
_OWNER_FILTER_KEYS: tuple[str, ...] = ("review_paths", "review_denylist_extra")


class OwnerFilterConfError(AnchorResolutionError):
    """강등 실행이 승계할 소유 PM 홈 conf 를 안전하게 읽지 못해 송신을 중단해야 함.

    후보 모호성과 달리 이 오류는 `--paths` 로 우회되지 않는다 — 보호 선언을 *확인하지 못한*
    상태이고, 검토 범위 지정은 시크릿 필터의 대체물이 아니다.
    """


def _owner_filter_conf(demotion: PmHomeDemotion) -> tuple[Path, dict[str, str]]:
    """강등 실행이 승계할 소유 PM 홈과 그 필터 선언을 유일 후보에서 읽는다.

    후보가 0개거나 2개 이상이면 어느 선언이 이번 송신을 지배하는지 확정할 수 없어 fail-loud 다.
    conf 파일 부재는 '추가 선언이 없음'을 확인한 상태라 빈 선언으로 승계하고, 읽기 실패는 보호
    선언을 확인할 수 없으므로 fail-closed 다(`_cross_repo_target_conf` 와 같은 규칙).
    """
    if len(demotion.candidates) != 1:
        listed = ", ".join(str(home) for home in demotion.candidates) or "(후보 0)"
        raise AnchorResolutionError(
            f"소유 PM 홈 후보를 하나로 좁히지 못했습니다: {listed}"
        )
    owner = demotion.candidates[0]
    conf_path = owner / ".project_manager" / "local.conf"
    owner_conf: dict[str, str] = {}
    try:
        # lstat 은 dangling symlink 도 '존재'로 구분한다 — 진짜 부재만 '선언 없음'이고, 그 밖의
        # stat/read/UTF-8 실패는 보호 선언을 확인할 수 없어 fail-closed 다.
        declared = True
        try:
            conf_path.lstat()
        except FileNotFoundError:
            declared = False
        if declared:
            owner_conf = _read_local_config(conf_path)
    except (OSError, UnicodeError) as exc:
        raise OwnerFilterConfError(
            "강등 실행이 승계할 소유 PM 홈 conf 를 읽지 못했습니다 — 확인하지 못한 "
            "review_denylist_extra 는 명시 --paths 로 대체되지 않으므로(범위 지정은 시크릿 필터가 "
            "아니다) 어떤 인자에서도 외부 송신 전에 중단합니다: "
            f"{conf_path} ({type(exc).__name__}: {exc})"
        ) from exc
    return owner, {
        key: owner_conf[key] for key in _OWNER_FILTER_KEYS if key in owner_conf
    }


def _merged_owner_filters(
    conf: dict[str, str], owner_filters: dict[str, str],
) -> dict[str, str]:
    """강등 실행의 유효 conf — denylist 는 합집합, review_paths 는 소유 **유효 범위**로 교체.

    denylist 합집합의 근거는 양쪽 보호 선언을 모두 존중하는 monotone 안전성이다
    (`resolve_review_content_conf` 와 같은 규칙). review_paths 는 포함 방향 어느 쪽도 일률적으로
    안전하지 않아 합치지 않고, 소유 PM 홈의 유효 범위를 통째로 승계한다 — 소유 홈이 선언하지
    않았으면 **엔진 고정 기본 경로가 그 유효 범위**이므로 그것으로 덮는다. 슬롯 선언을 남겨두면
    (예: 슬롯 `review_paths=.`) lease 손상만으로 송신 범위가 소유 선언보다 넓어진다.
    """
    merged = dict(conf)
    merged["review_paths"] = " ".join(_configured_paths(owner_filters))
    extras = tuple(dict.fromkeys(
        (*_denylist_extras(conf), *_denylist_extras(owner_filters))
    ))
    if extras:
        merged["review_denylist_extra"] = " ".join(extras)
    return merged


def _conf_with_owner_filters(
    conf: dict[str, str],
    demotions: Sequence[PmHomeDemotion],
    *,
    explicit_paths: bool,
) -> dict[str, str]:
    """강등 실행이면 소유 PM 홈 필터를 승계한 conf 를 반환하고, 승계 불가면 차단한다.

    `demotions` 는 한 번의 `resolve_pm_home_for_repo` 가 채운 sink 라 강등 근거는 최대 1건이고,
    비어 있으면(정상 해소) conf 를 그대로 돌려준다. 탈출구 규칙은 위 §탈출구 매트릭스를 따른다 —
    소유 conf 읽기 실패는 `--paths` 로도 통과하지 못한다.
    """
    if not demotions:
        return conf
    demotion = demotions[0]
    try:
        owner, owner_filters = _owner_filter_conf(demotion)
    except OwnerFilterConfError:
        # 보호 선언을 확인하지 못한 실행 — 범위 명시로 대체 불가라 인자와 무관하게 올린다.
        raise
    except AnchorResolutionError as exc:
        if not explicit_paths:
            raise AnchorResolutionError(
                "PM 홈 해소 실패로 config 소유자가 이 repo 로 강등됐고, 소유 PM 홈의 필터 선언"
                "(review_denylist_extra/review_paths)을 승계할 수 없습니다 — 시크릿 필터가 좁아진 "
                "채로 외부에 송신하지 않도록 전송 전에 중단합니다: "
                f"anchor={demotion.anchor} · 강등 사유={demotion.reason} · 승계 실패={exc}\n"
                "  · `--paths <경로>` 로 이번 검토 범위를 직접 지정하면 이 실행은 통과합니다 "
                "(범위를 명시하는 행위 자체가 '알고 있고 의도했다'는 표현).\n"
                "  · 근본 해소는 worktree lease 장부를 고쳐 소유 PM 홈을 하나로 확정하는 것입니다."
            ) from exc
        print(
            "경고: PM 홈 강등 실행 — 소유 PM 홈 필터를 승계하지 못했습니다 "
            f"({exc}). 명시 --paths 범위로 계속하며, 이번 송신에는 "
            f"{_local_conf_path(demotion.anchor)} 의 선언만 적용됩니다.",
            file=sys.stderr,
        )
        return conf
    merged = _merged_owner_filters(conf, owner_filters)
    print(
        "경고: PM 홈 강등 실행 — 소유 PM 홈 유효 필터를 승계했습니다: "
        f"owner={owner} · conf={_local_conf_path(owner)} · "
        f"review_paths={merged.get('review_paths', '') or '(기본)'!r} · "
        f"review_denylist_extra={merged.get('review_denylist_extra', '') or '(없음)'!r}",
        file=sys.stderr,
    )
    return merged


def _registered_worktrees(pm_home: Path) -> tuple[Path, ...]:
    """PM 홈의 실재 worktree 후보를 lease 장부에서 파생한다(하네스 목록/슬롯 추측 없음)."""
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    try:
        data = json.loads(ledger.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnchorResolutionError(f"worktree lease 장부 읽기 실패: {ledger} ({exc})") from exc
    rows = data.get("leases") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise AnchorResolutionError(f"worktree lease 장부 형식 오류: {ledger}")
    found: set[Path] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("slot"), str):
            raise AnchorResolutionError(f"worktree lease 장부 leases[{index}] 형식 오류: {ledger}")
        candidate = (pm_home / row["slot"]).resolve()
        if candidate.is_dir() and (candidate / ".git").exists():
            found.add(candidate)
    return tuple(sorted(found))


def _path_repo_root(path: Path) -> Path | None:
    """존재 경로(파일이면 부모)에서 가장 가까운 git repo 루트를 반환한다."""
    start = path if path.is_dir() else path.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _ticket_worktree_candidates(
    pm_home: Path, touches: Sequence[str], candidates: Sequence[Path],
) -> tuple[Path, ...]:
    """PM-home 좌표 touches가 실제로 가리키는 worktree 후보만 고른다."""
    selected: set[Path] = set()
    for raw in touches:
        path = Path(raw)
        absolute = path.resolve() if path.is_absolute() else (pm_home / path).resolve()
        for candidate in candidates:
            try:
                absolute.relative_to(candidate)
            except ValueError:
                continue
            selected.add(candidate)
    return tuple(sorted(selected))


def _candidate_has_diff(root: Path, base: str, paths: Sequence[str]) -> bool:
    """등록 슬롯 후보가 **한 diff 단계**에서 변경을 갖는지 read-only 판정.

    단계 문자열과 실행은 추출(`extract_diff`)과 같은 코드 경로(`_stage_diff_text`)를 타므로 폭
    해석이 두 벌로 갈리지 않는다. 어느 단계를 근거로 삼을지는 호출부(`_resolve_diff_root`)가
    `_slot_selection_bases` 로 정한다. git 실행 자체가 불가능한 후보는 변경 없음으로 본다
    (fail-soft·선택 대상 밖).
    """
    try:
        return bool(_stage_diff_text(root, base, paths).strip())
    except OSError:
        return False


def _resolve_diff_root(
    engine_repo: Path,
    *,
    pm_home: Path,
    paths: Sequence[str],
    base: str,
    ticket_selected: bool,
) -> Path:
    """명시 paths/touches와 엔진-홈 관계에서 diff worktree를 유일하게 파생한다."""
    absolute_roots = {
        root
        for raw in paths
        if Path(raw).is_absolute()
        and (root := _path_repo_root(Path(raw).resolve())) is not None
    }
    if len(absolute_roots) > 1:
        raise AnchorResolutionError(
            "--paths가 여러 git repo를 가리킵니다 — 외부 리뷰 1회는 diff 앵커 하나만 허용합니다."
        )
    if absolute_roots:
        selected_root = next(iter(absolute_roots)).resolve()
        # 비-ticket 절대 경로는 이미 repo 하나를 확정했다. 복구 채널이 손상된 PM-home
        # lease 장부 때문에 자기잠김하지 않도록 장부를 읽기 전에 반환한다.
        if not ticket_selected:
            return selected_root
        candidates = _registered_worktrees(pm_home)
        if (
            ticket_selected
            and engine_repo.resolve() != pm_home.resolve()
            and selected_root in candidates
            and selected_root != engine_repo.resolve()
        ):
            raise AnchorResolutionError(
                "ticket touches가 실행 엔진 worktree와 다른 등록 worktree를 가리킵니다: "
                f"engine={engine_repo.resolve()} touches={selected_root}"
            )
        return selected_root

    candidates = _registered_worktrees(pm_home)

    if ticket_selected and candidates:
        selected = _ticket_worktree_candidates(pm_home, paths, candidates)
        if len(selected) == 1:
            if (
                engine_repo.resolve() != pm_home.resolve()
                and selected[0] != engine_repo.resolve()
            ):
                raise AnchorResolutionError(
                    "ticket touches가 실행 엔진 worktree와 다른 등록 worktree를 가리킵니다: "
                    f"engine={engine_repo.resolve()} touches={selected[0]}"
                )
            return selected[0]
        if len(selected) > 1:
            raise AnchorResolutionError(
                "ticket touches가 여러 등록 worktree를 가리킵니다 — diff 앵커가 모호합니다."
            )

    if engine_repo.resolve() != pm_home.resolve():
        return engine_repo.resolve()

    if not candidates:
        # standalone/solo repo에는 lease 장부가 없다. 이 경우 명시 paths의 유일한 repo는
        # 엔진 자기 repo이며, PM-home 다중 슬롯 오해소와 구분된다.
        return engine_repo.resolve()
    # 슬롯 소유 근거가 되는 단계(`_slot_selection_bases`)만 순서대로 본다 — 암묵 폴백 단계는
    # 놀고 있는 슬롯까지 후보로 만들기 때문에 여기서 제외된다. 선택된 슬롯에서 추출이 어느
    # 단계를 쓰든, 슬롯을 *고른* 근거는 항상 사용자가 지정한 base 자신이다.
    changed: tuple[Path, ...] = ()
    for stage_base in _slot_selection_bases(base):
        changed = tuple(
            candidate for candidate in candidates
            if _candidate_has_diff(candidate, stage_base, paths)
        )
        if changed:
            break
    if len(changed) == 1:
        return changed[0]
    if len(candidates) == 1:
        return candidates[0]
    detail = ", ".join(str(path) for path in (changed or candidates)) or "(후보 0)"
    raise AnchorResolutionError(
        "명시 경로에서 diff worktree를 하나로 해소할 수 없습니다. "
        f"ticket touches에 등록 슬롯 경로를 포함하거나 절대 --paths를 사용하세요: {detail}\n"
        "  · 변경을 이미 커밋했다면 `--base <공통 base ref>`로 앵커를 명시하세요 — 직전 커밋 한 "
        "칸(HEAD~1..HEAD)은 아무 것도 안 한 슬롯도 공유 base 의 마지막 커밋으로 걸리므로 슬롯 "
        "소유 근거로 쓰지 않습니다."
    )


def _normalize_review_paths(
    paths: Sequence[str], *, diff_root: Path, pm_home: Path, ticket_selected: bool,
) -> tuple[str, ...]:
    """PM-home/절대 좌표를 선택된 diff repo의 pathspec으로 변환한다."""
    normalized: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            absolute = path.resolve()
            try:
                value = absolute.relative_to(diff_root).as_posix()
            except ValueError as exc:
                raise AnchorResolutionError(
                    f"검토 경로가 해소된 diff worktree 밖입니다: {raw} (diff={diff_root})"
                ) from exc
        elif ticket_selected:
            absolute = (pm_home / path).resolve()
            try:
                value = absolute.relative_to(diff_root).as_posix()
            except ValueError:
                # solo/legacy ticket은 이미 repo-relative touches를 저장한다.
                value = path.as_posix()
        else:
            value = path.as_posix()
        if value:
            normalized.append(value)
    if not normalized:
        raise AnchorResolutionError("검토 경로가 0개로 해소됐습니다.")
    return tuple(dict.fromkeys(normalized))


def _scope_from_initial_pm_home(*, ticket_selected: bool, explicit_paths: bool) -> bool:
    """이번 검토 범위가 **최초 PM 홈**에서 파생됐는가 (diff 소유자 대조 대상).

    명시 `--paths` 나 유효 ticket touches 가 범위를 완전 지정하지 않은 실행은 최초 PM 홈 config
    를 읽어 범위를 얻는다. 그 config 가 `review_paths` 를 선언했든 엔진 고정 기본 경로
    (`DEFAULT_PATHS`)로 떨어졌든 **범위의 출처는 최초 PM 홈 하나**라, 표시 conf 와 실제 전송
    범위가 갈리는 위험은 같다 — 그래서 선언 유무를 판정 입력으로 쓰지 않는다.
    """
    return not ticket_selected and not explicit_paths


# ── board root 추종 (board/ 분리) ───────────────────────
# board(tickets)는 `.project_manager/board/`(submodule)로 분리될 수 있다. 그러면
# ticket touches 해소(`parse_ticket_touches`)가 wiki/ legacy 위치를 보면 *stale*(ticket 미발견
# → 빈 touches)이다. external_review 는 board.py 를 import 하지 않으므로(YAML frontmatter 직접
# 파싱), board.py 의 graceful 탐지를 *동형*으로 최소 복제한다 — board/tickets 가 실 디렉토리면
# board/ 루트, 아니면 wiki/(legacy). 솔로/미분리면 현 위치 100% 폴백(회귀 0). 상수 TICKETS_DIR
# 는 hermetic 테스트 seam·legacy 기본값으로 유지.

def _tickets_dir() -> Path:
    """ticket 디렉토리 — board/ 분리 시 `<REPO>/.project_manager/board/tickets`, 아니면
    legacy `<REPO>/.project_manager/wiki/tickets` (board.py `tickets_dir` 동형·import 없이 복제)."""
    base = REPO / ".project_manager"
    if (base / "board" / "tickets").is_dir():
        return base / "board" / "tickets"
    return base / "wiki" / "tickets"


# 기본 검토 경로 (--paths·local.conf review_paths 미지정 시)
DEFAULT_PATHS: list[str] = ["src/", "tests/", "scripts/", ".project_manager/tools/"]


# ── PM 홈 앵커 재지정 감지 (adopter#0 false-green 게이트) ────────────────
# external_review 의 import 사본은 PM 홈에 있어 REPO 가 PM 홈으로
# 해소된다 — 실 코드 변경은 canonical worktree에 있으므로 `git diff` 가 비어 codex 가 "변경
# 없음"을 통과로 판정하는 false-green 이 난다.
# board.py `_pm_home_worktree_misanchor`의 *역방향*: 거긴 worktree 에서 실행된 board 조작을
# 잡고, 여긴 PM 홈에서 실행된 외부 리뷰를 잡아 worktree 로 재지정한다. 순수 filesystem 판정(subprocess
# 불요)이라 hermetic — REPO 를 module-level 로 두어 테스트가 monkeypatch 하고, 헬퍼는 anchor/conf 를
# 명시 인자로 받아 DI seam 이 된다(board `_has_real_board` 를 import 없이 동형 복제·각 파일 self-contained).

def _owns_real_board(pm_dir: Path) -> bool:
    """`.project_manager` 디렉토리(`pm_dir`)가 실 티켓(`T-*.md`)을 가진 board 를 소유하는가.

    board/ 분리면 `board/tickets`, legacy 면 `wiki/tickets` 상태 디렉토리에 실 티켓이
    하나라도 있으면 True. 빈 scaffold(README/_template 만 — worktree 출하 형상)는 False (worktree
    자신을 PM 홈으로 오인해 가드 오탐 내지 않게·board.py `_has_real_board` 동형)."""
    for base in (pm_dir / "board" / "tickets", pm_dir / "wiki" / "tickets"):
        if not base.is_dir():
            continue
        for status in STATUS_DIRS:
            status_dir = base / status
            if status_dir.is_dir() and any(status_dir.glob("T-*.md")):
                return True
    return False


def _canonical_worktree(anchor: Path) -> Path | None:
    """adopter#0 PM 홈 `anchor` 의 canonical 코드 worktree(재지정 대상) 경로 — 없으면 None.

    `<anchor>/work/*` 스캔 중 엔진 사본(`.project_manager/tools/external_review.py`)을 가진 첫
    디렉토리를 반환한다(board.py `_registers_worktree` (a) `work/<name>` 등록 관례와 동형). 없으면
    None(무관 형상·재지정 대상 없음).

    local.conf `upstream` 은 재지정 대상 결정에 **쓰지 않는다** — upstream 은 URL 이거나 무관한
    로컬 checkout(`pm_import --from <로컬>`)일 수 있어, 실 board 를
    소유한 정규 채택자에서 stale/무관 checkout 으로 오안내하며 정상 리뷰를 hard-block 한다(빈-diff
    백스톱도 실 diff 가 non-empty 면 무력). `work/` 슬롯 스캔만으로 adopter#0재지정을
    완전 커버하고, upstream 분기는 잉여+오탐만 더한다 — 제거가 동등 커버리지·최소·오탐 0(codex/reviewer
    이중 게이트 수렴 must-fix)."""
    work_dir = anchor / "work"
    if work_dir.is_dir():
        for sub in sorted(work_dir.iterdir()):
            if (sub / ".project_manager" / "tools" / "external_review.py").is_file():
                return sub
    return None


def _pm_home_reanchor(anchor: Path) -> Path | None:
    """`anchor`(REPO=도구 자기-앵커)가 adopter#0 PM 홈이면 재지정 대상 worktree 를, 아니면 None.

    2중 conjunction (오탐 0 지향·fail-soft): (1) anchor 가 실 board 소유(PM 홈) — worktree(코드
    전용·board 미소유)에서 실행하면 여기서 탈락해 None(정상·재지정 불요), (2) anchor 아래 canonical
    코드 worktree(`work/<name>`) 존재. 솔로/일반 채택자(로컬 upstream 포함)는 (1) 또는 (2) 미충족으로
    None(무영향)."""
    # external_review.main은 명시 selector 기반 diff_root 해소로 전환되어 이 헬퍼를 호출하지 않는다.
    # pm_delegate.check_write_target_reanchor가 adopter write-target 보호에 계속 사용하므로 유지한다.
    if not _owns_real_board(anchor / ".project_manager"):
        return None
    return _canonical_worktree(anchor)

# 외부 리뷰어 기본 명령 (local.conf reviewer_cmd 로 교체 가능)
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"

# 외부 호출의 시간 예산(무진행 상한 + 벽시계 백스톱)은 **리뷰어 커맨드의 하네스 프로필**이 소유한다
# (`pm_relay.HARNESS_PROFILES` — 기본 reviewer_cmd 가 `codex exec` 이니 클라우드 축 값). 이 모듈에
# 별도 타임아웃 상수를 두지 않는 이유가 이 티켓의 편입 이유와 같다: **값이 두 군데면 규칙이 둘이
# 된다.** 평범한 diff 153~294초·13파일 대형 227초 — 구 기본 180초는 평상 대역 *안*이라 상시 타임아웃
# 구조였고, 900 으로 올려도 같은 구조였다(실측: **입력·모델이 완전히 동일한 리뷰 2회 중 하나는
# 900초 초과 kill·다른 하나는 `--timeout 1500` 으로 성공**). 그래서 값을 고르는 게임을 끝내고
# 판정 기준을 무진행으로 교체했고, 벽시계는 "감지기 자체가 고장난 경우"의 유한 상한으로 강등된다.
# 조정: 일회성 `--timeout`/`--idle-timeout` > local.conf `harness.<reviewer>.wall_timeout`/
# `.idle_timeout` > 아래 표면-flat legacy 키 > 프로필 선언.
EXTERNAL_TIMEOUT_KEY = "external_review_timeout"
EXTERNAL_IDLE_TIMEOUT_KEY = "external_review_idle_timeout"
EXTERNAL_PROGRESS_SIGNAL_KEY = "external_review_progress_signal"

# 알려진 reviewer CLI의 **실행 파일 + 옵션 계약**. 함수 밖 선언이라 새 CLI/형식 추가가 판정 코드
# 분기로 번지지 않는다. attr 값은 동적 로드한 pm_relay의 공개 상수명이다.
_REVIEWER_PROGRESS_CONTRACTS = {
    "codex": {
        "default": "PROGRESS_SIGNAL_PLAINTEXT",
        "flags": {"--json": "PROGRESS_SIGNAL_EVENT_STREAM"},
        "options": {},
    },
    "claude": {
        "default": "PROGRESS_SIGNAL_NONE",
        "flags": {},
        "options": {
            ("--output-format", "stream-json"): "PROGRESS_SIGNAL_EVENT_STREAM",
        },
    },
    "opencode": {
        "default": "PROGRESS_SIGNAL_NONE",
        "flags": {},
        "options": {
            ("--format", "json"): "PROGRESS_SIGNAL_EVENT_STREAM",
        },
    },
}

# 라운드 상한 — 같은 --gate 로 승인 없이 이 횟수를 넘겨 실 전송하면 이후 실행을 거부한다.
# 기본 4 는 사용자 전역 규율(외부 리뷰 ">3~4 라운드면 수렴 판단")의 기계화. local.conf
# external_review_round_limit 로 조정 가능.
DEFAULT_ROUND_LIMIT = 4
DEFAULT_INCOMPLETE_ROUND_LIMIT = 2

# 라운드 상한 초과 전용 종료 코드 (기존 0=통과·1=반려/실패/오류·2=argparse·3=예약 과 구분).
# 실행 전 거부라 리뷰어는 호출되지 않는다(외부 전송·과금 없음).
EXIT_ROUND_LIMIT_EXCEEDED = 4

# 시크릿 denylist — 이 패턴에 매칭되는 파일은 diff 에서 강제 제외하고 stderr 에 경고.
# 보수적으로 유지: 오탐 허용 (누락 금지). 프로젝트 고유 경로는 local.conf review_denylist_extra 로.
_SECRET_DENYLIST_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*secret*",
    "*credential*",
    "*.key",
    "*token*",
    "*.pem",
    "*.p12",
    "*.pfx",
)

# must-fix / 반려 판정 토큰 (보수적: 하나라도 감지 → 비-통과)
_REJECT_TOKENS: tuple[str, ...] = (
    "must-fix", "must fix", "MUST-FIX", "MUST FIX", "반려", "REJECT", "reject",
)
_PASS_TOKENS: tuple[str, ...] = (
    "통과", "PASS", "pass", "승인", "APPROVE", "approve", "lgtm", "LGTM",
)
# 판정 라인 자체의 인식 규칙 — 판정 파서와 echo 오염 검출이 **같은 함수**를 봐야 "검출기 ⊇ 파서"가
# 성립한다(파서가 집어 든 라인을 검출기가 못 보면 오염된 판정이 조용히 통과한다).
_VERDICT_LINE_RE = re.compile(r"^[ \t]*[*_#\s-]*판정[ \t]*:[ \t]*(\S+)")
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_QUOTE_LINE_RE = re.compile(r"^\s*>")

# 판정값 허용 토큰 — **정확일치**다(부분 문자열 금지). 부분 문자열이면 프롬프트 템플릿 echo
# `판정: [통과 | 반려]`·부정형 `판정: 비통과`·선택지 나열 `판정: PASS/REJECT` 가 전부 '통과'로 읽혀
# false-green 이 난다(실측). 허용 목록에 없는 판정값은 통과도 반려도 아닌 **불명확**이다 —
# 리뷰어가 무엇을 말했는지 기계가 모르면 통과로 접지 않는다.
_PASS_VERDICT_TOKENS = frozenset({
    "통과", "PASS", "pass", "승인", "APPROVE", "approve", "LGTM", "lgtm",
})
_REJECT_VERDICT_TOKENS = frozenset({"반려", "REJECT", "reject"})
# 정확일치 전에 벗겨내는 것은 **강조/문장부호뿐**이다(`**통과**`·`통과.`). 괄호·슬래시는 남긴다 —
# 그게 템플릿 echo(`[통과`)와 선택지 나열(`PASS/REJECT`)을 불명확으로 세우는 신호다.
_VERDICT_WORD_TRIM = "*_`.,!;:"

VERDICT_PASS = "pass"
VERDICT_REJECT = "reject"
VERDICT_UNKNOWN = "unknown"


def verdict_kind(word: str) -> str:
    """판정값 낱말 → `pass`/`reject`/`unknown` (정확일치·파서와 검출기 공용)."""
    normalized = word.strip().strip(_VERDICT_WORD_TRIM)
    if normalized in _PASS_VERDICT_TOKENS:
        return VERDICT_PASS
    if normalized in _REJECT_VERDICT_TOKENS:
        return VERDICT_REJECT
    return VERDICT_UNKNOWN


def verdict_words(text: str) -> tuple[str, ...]:
    """판정 라인이 선언한 낱말들 — **행 선두 선언만**, 코드펜스 안·인용행은 제외한다.

    좁히는 규칙을 검출기에만 걸면 파서가 보는 라인을 검출기가 못 보게 되어 오염 판정이 새므로,
    파서와 검출기가 이 함수 하나를 공유한다. 인용/코드펜스 안의 판정 문구는 리뷰어 자신의 선언이
    아니라 인용물이라 어느 쪽에서도 판정으로 세지 않는다(리뷰 대상 diff에 든 판정 문안이 이 축의
    상시 오탐 원천이다).
    """
    words: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if _CODE_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence or _QUOTE_LINE_RE.match(line):
            continue
        match = _VERDICT_LINE_RE.match(line)
        if match:
            words.append(match.group(1).strip())
    return tuple(words)

# 프롬프트 형식에서 must-fix 섹션 헤더를 인식하는 정규식
_MUST_FIX_SECTION_RE = re.compile(
    r"\*{0,2}must[- ]fix\*{0,2}(?:\s*\([^)]*\))?\s*:", re.IGNORECASE,
)
# must-fix 섹션 내 "없음/N/A/none" 항목 패턴
_NONE_ITEM_RE = re.compile(r"^(?:없음|n/?a|none)\s*$", re.IGNORECASE)

# generic 맥락 헤더 (review_context.local.md 부재 시)
_DEFAULT_CONTEXT_HEADER = """\
## 리뷰 맥락

아래 diff 를 코드리뷰하라. 프로젝트 고유 맥락(`.project_manager/review_context.local.md`)이
설정돼 있으면 그 기준을 우선한다.
"""

# 출력 형식 블록 (parse_verdict 가 의존 — 리뷰어 무관 공통)
_OUTPUT_FORMAT_BLOCK = """\
### 출력 형식 (필수)
아래 형식으로 응답하라:

판정: [통과 | 반려]

**must-fix** (반드시 수정):
- (없으면 "없음"으로 표기)

**suggestion** (권장):
- (없으면 "없음"으로 표기)

"""

# 빈-diff fail-loud 안내.
# 검토 경로에 tracked 변경이 없어 diff 가 비면 codex 는 "변경 없음"을 통과로 판정해 가짜
# 통과(false-green)를 낸다. codex 호출 전에 이 메시지로 fail-loud 한다 (우회 플래그 없음).
_EMPTY_DIFF_GUIDANCE = (
    "오류: 리뷰할 diff 가 없습니다 (검토 경로에 tracked 변경 없음).\n"
    "  빈 diff 를 리뷰하면 외부 리뷰어가 '변경 없음'을 통과로 판정해 가짜 통과(false-green)가\n"
    "  발생합니다 — 외부 리뷰어를 호출하지 않고 중단합니다.\n"
    "  · 첫 provenance의 `diff_root`가 실 변경 worktree인지 확인하고, 필요하면 그 repo를 가리키는\n"
    "    절대 `--paths <경로>`로 다시 실행하세요. 프로세스 cwd는 diff 판정 입력이 아닙니다.\n"
    "  · 신규 파일만 변경했다면 먼저 `git add` 후 재실행하세요 (diff 는 tracked 변경만 봅니다)."
)


def _empty_diff_guidance(paths: Sequence[str], *, root: Path) -> str:
    """콤마 nargs 오용이 실제로 의심될 때만 기존 빈-diff 안내에 형식 진단을 보탠다."""
    suspicious: list[str] = []
    for raw in paths:
        if "," not in raw or (root / raw).exists():
            continue
        suspicious.append(raw)
    if not suspicious:
        return _EMPTY_DIFF_GUIDANCE
    rendered = ", ".join(repr(item) for item in suspicious)
    return (
        _EMPTY_DIFF_GUIDANCE
        + "\n  · --paths는 공백 구분 인자입니다. 콤마가 든 미존재 경로를 감지했습니다: "
        + rendered
        + "\n    예: --paths a.py b.py (자동 교정하지 않음 — 콤마가 파일명인 경우를 보존)."
    )

# 라운드 상한 초과 fail-loud 안내. 같은 게이트로
# 승인 없이 limit 회를 넘겨 실 전송이 시도되면 diff 추출·리뷰어 호출 전에 이 안내로 차단한다
# (과금·외부 전송 게이트라 초과분은 기계가 멈춘다·자의 우회 불가). 유일한 재개 경로는 사용자
# 승인 후 `--ack-rounds` — 환경 문제 우회 플래그가 아니다.
_ROUND_LIMIT_GUIDANCE = (
    "오류: 외부 리뷰 라운드 상한 초과 — 게이트 {gate} · "
    "count={unacked}(판정 {verdicts} · 미완 {incomplete})\n"
    "  (판정 상한 {limit} · 미완 재시도 상한 {incomplete_limit}). 외부 전송·과금 게이트라 "
    "초과분은 기계가 멈춥니다 — 자의 우회 불가.\n"
    "  · **미완**은 판정이 없던 전송입니다 — 타임아웃·중단뿐 아니라 **오염 진단으로 무효화된 "
    "판정**도 이 축에 들어갑니다(판정 표면과 같은 규칙이라 두 표면이 갈리지 않습니다).\n"
    "  · 지금까지의 리뷰 라운드 수렴 상황을 **사용자에게 보고하고 대기**하세요.\n"
    "  · 사용자 승인을 받은 뒤에만 `--ack-rounds` 로 재개하세요 "
    "(판정 +{limit} · 미완 +{incomplete_limit}):\n"
    "      python3 .project_manager/tools/external_review.py --gate {gate} --ack-rounds [기존 옵션]\n"
    "  · 상한 조정은 local.conf `external_review_round_limit`(판정)과 "
    "`external_review_incomplete_round_limit`(미완).\n"
    "  (장부: {ledger} · count={count} acked_through={acked})"
)


# ── 설정 ──────────────────────────────────────────────────────────────────


def local_config(repo: Path | None = None) -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    conf: dict[str, str] = {}
    path = (repo / ".project_manager" / "local.conf") if repo is not None else LOCAL_CONF
    if not path.exists():
        return conf
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


def _local_config_for_repo(repo: Path) -> dict[str, str]:
    """해소된 PM 홈의 config만 읽는 명시적 seam."""
    return local_config(repo)


def _is_enabled(conf: dict[str, str]) -> bool:
    return conf.get("external_review_enabled", "false").strip().lower() in ("true", "1", "yes", "on")


def _reviewer_cmd(conf: dict[str, str]) -> str:
    return conf.get("reviewer_cmd", "").strip() or DEFAULT_REVIEWER_CMD


def _normalized_reviewer_key(argv: list[str]) -> str:
    """리뷰어 실행파일을 공유 프로필 키로 정규화한다.

    Windows 실행파일 접미사/대소문자/경로 구분자를 여기서 한 번만 처리한다. 진행신호 계약과 시간
    프로필이 이 키를 함께 써야 `codex.exe`의 진행은 codex로 보면서 시간만 미지 GPU 축으로 잡는
    비대칭이 생기지 않는다.
    """
    if not argv:
        return "reviewer"
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if executable.endswith(suffix):
            executable = executable[:-len(suffix)]
            break
    return re.sub(r"[^a-z0-9_.-]", "_", executable) or "reviewer"


def _reviewer_progress_signal(
    reviewer_cmd: str, conf: dict[str, str], relay
) -> str:
    """실행 파일 basename과 그 CLI의 **옵션 계약** 또는 명시 설정으로 진행 신호를 판정한다.

    이름만 같거나 스트리밍처럼 보이는 플래그만 있다는 이유로 추론하지 않는다. 알려진 실행 파일의
    알려진 옵션 조합만 계약으로 인정한다. 미지 CLI/형식은 신호 없음으로 두며, 자유 문자열 커맨드는
    local.conf 명시 키로만 선언할 수 있다.
    """
    explicit = (conf.get(EXTERNAL_PROGRESS_SIGNAL_KEY) or "").strip()
    if explicit:
        if explicit in relay.PROGRESS_SIGNAL_KINDS:
            return explicit
        print(
            f"경고: local.conf {EXTERNAL_PROGRESS_SIGNAL_KEY}={explicit!r} 은 "
            f"{sorted(relay.PROGRESS_SIGNAL_KINDS)} 중 하나가 아님 — argv 형식 판정 사용.",
            file=sys.stderr,
        )
    try:
        argv = shlex.split(reviewer_cmd)
    except ValueError:
        return relay.PROGRESS_SIGNAL_NONE
    if not argv:
        return relay.PROGRESS_SIGNAL_NONE

    executable = _normalized_reviewer_key(argv)
    contract = _REVIEWER_PROGRESS_CONTRACTS.get(executable)
    if contract is None:
        return relay.PROGRESS_SIGNAL_NONE

    def option_value(name: str) -> str | None:
        for index, token in enumerate(argv[1:], start=1):
            if token == name and index + 1 < len(argv):
                return argv[index + 1].strip().lower()
            prefix = name + "="
            if token.startswith(prefix):
                return token[len(prefix):].strip().lower()
        return None

    for flag, signal_attr in contract["flags"].items():
        if flag in argv[1:]:
            return getattr(relay, signal_attr)
    for (option, expected), signal_attr in contract["options"].items():
        if option_value(option) == expected:
            return getattr(relay, signal_attr)
    return getattr(relay, contract["default"])


def reviewer_profile(reviewer_cmd: str, conf: dict[str, str] | None = None):
    """리뷰어 커맨드 → 시간값 프로필 + 출력형식 기반 진행신호 프로필.

    시간값은 실행 파일 축의 공유 테이블을 쓰되, 진행 신호는 전체 argv/명시 설정으로 독립 판정한다.
    conf 미지정 facade도 local.conf를 읽어 `pm_delegate.harness_profile`과 대칭이다.
    """
    relay = _load_relay()
    if conf is None:
        try:
            conf = local_config()
        except OSError:
            conf = {}
    profile = relay.resolve_harness_profile(
        reviewer_name(reviewer_cmd), conf,
        fallback=relay.REVIEWER_FALLBACK_PROFILE,
        legacy_idle_key=EXTERNAL_IDLE_TIMEOUT_KEY,
        legacy_wall_key=EXTERNAL_TIMEOUT_KEY,
    )
    return profile._replace(
        progress_signal=_reviewer_progress_signal(reviewer_cmd, conf, relay)
    )


def _resolve_timeout(args: argparse.Namespace, conf: dict[str, str],
                     reviewer_cmd: str = DEFAULT_REVIEWER_CMD) -> int:
    """외부 리뷰 **벽시계 백스톱**(초)을 `--timeout` > 리뷰어 프로필 순서로 해소한다.

    CLI 양수값은 argparse 검증을 통과한 명시 override다. 그 아래(legacy flat conf →
    `harness.<reviewer>.wall_timeout` → 선언 기본)는 `pm_relay.resolve_harness_profile` 이 소유한다
    — 깨진 conf 값은 거기서 stderr 경고 후 fail-soft. pm_delegate._resolve_timeout 과 같은 계약이다.
    """
    if args.timeout is not None:
        return args.timeout
    return int(reviewer_profile(reviewer_cmd, conf).wall_timeout)


def _resolve_idle_timeout(args: argparse.Namespace, conf: dict[str, str],
                          reviewer_cmd: str = DEFAULT_REVIEWER_CMD) -> float:
    """무진행 상한(초)을 `--idle-timeout` > 리뷰어 프로필 순서로 해소한다(벽시계 축과 대칭).

    값의 출처가 하나이므로(프로필 테이블) 리뷰어 축과 위임 축의 규칙이 갈리지 않는다."""
    if getattr(args, "idle_timeout", None) is not None:
        return float(args.idle_timeout)  # main() 이 양수 검증(usage error)
    return float(reviewer_profile(reviewer_cmd, conf).idle_timeout)


def _timeout_seconds_arg(raw: str) -> int:
    """CLI timeout 을 pm_relay 의 단일 정규화 경계로 파싱한다."""
    value = _load_relay().normalize_timeout_seconds(raw)
    if value is None:
        raise argparse.ArgumentTypeError("유한한 정수 초(최소 1)여야 합니다")
    return value


def _configured_paths(conf: dict[str, str]) -> list[str]:
    raw = conf.get("review_paths", "").strip()
    return [p for p in re.split(r"[,\s]+", raw) if p] if raw else list(DEFAULT_PATHS)


def _denylist_extras(conf: dict[str, str]) -> tuple[str, ...]:
    """conf 가 추가 선언한 denylist 패턴만 — 엔진 고정분 없이(승계 합집합의 입력)."""
    extra = conf.get("review_denylist_extra", "").strip()
    return tuple(p for p in re.split(r"[,\s]+", extra) if p) if extra else ()


def _denylist_patterns(conf: dict[str, str]) -> tuple[str, ...]:
    return _SECRET_DENYLIST_PATTERNS + _denylist_extras(conf)


def _round_limit(conf: dict[str, str]) -> int:
    """라운드 상한 (local.conf external_review_round_limit·기본 `DEFAULT_ROUND_LIMIT`).

    비정수·음수는 기본값으로 fail-soft — 장부/노브 값이 깨졌다고 게이트를 벽돌로 만들지 않는다
    (음수 상한은 첫 라운드부터 무조건 차단이라 무의미)."""
    raw = conf.get("external_review_round_limit", "").strip()
    if not raw:
        return DEFAULT_ROUND_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ROUND_LIMIT
    return value if value >= 0 else DEFAULT_ROUND_LIMIT


def _incomplete_round_limit(conf: dict[str, str]) -> int:
    """판정 없는 전송의 별도 재시도 상한(기본 2)."""
    raw = conf.get("external_review_incomplete_round_limit", "").strip()
    if not raw:
        return DEFAULT_INCOMPLETE_ROUND_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INCOMPLETE_ROUND_LIMIT
    return value if value >= 0 else DEFAULT_INCOMPLETE_ROUND_LIMIT


# ── 라운드 상한 장부 ─────────────
# 외부 리뷰는 과금·전송 게이트라 라운드가 무한정 이어지면 비용이 쌓인다(PM 10차 실측: 한 게이트
# 클러스터 25라운드). PM 자의 "수렴 판단"을 기계 판정으로 대체한다([[mechanize-dont-instruct-llm]]):
# `--gate <T-NNNN>` 별로 실 전송 횟수(count)와 사용자 승인 수위(acked_through)를 per-clone·git-ignored
# 장부에 기록하고, 승인 없이 limit 을 넘기면 실행 전에 거부한다. 장부는 세션/클론 로컬 현상이라
# `.project_manager/.local/`(regression/livegate sidecar 와 동위·board 상태 아님)에 둔다. 경로는
# 호출 시점 REPO(module-level·monkeypatch 가능)에서 파생해 hermetic 테스트가 tmp 로 격리할 수 있게
# 한다(_tickets_dir 동형). 손상 장부는 빈 장부로 fail-soft(회귀해소·regression flag 동형).


def _round_ledger_path() -> Path:
    """라운드 장부 경로 — 해소된 **소유 PM 홈**의 `.local/review_rounds.json`.

    앵커 규칙은 raw 장부(`_raw_storage`)와 같다 — `_PM_HOME_OVERRIDE`(같은 실행이 해소한 소유
    PM 홈) 우선, 미주입(라이브러리 직접 호출)·해소 실패는 엔진 자기 앵커 `REPO` 폴백이다
    (`_main` 이 해소 실패 시 loud 경고와 함께 diff_root 로 이미 강등해 둔 값 — 복구 채널
    자기잠김 금지).

    diff 앵커(diff_root)에 매달면 **같은 게이트의 과금 상한이 diff_root 가 바뀔 때마다 조용히
    리셋된다** — 게이트 스냅샷 worktree 나 새로 판 슬롯에서 같은 `--gate T-NNNN` 을 돌리면
    count 가 0 부터 다시 세어져 상한이 무력화된다. 라운드는 이미 `--gate` 키로 분리되므로 슬롯별
    장부 분리가 주는 추가 격리는 없다(옛 "슬롯별 게이트 수명 분리" 근거 대체).
    """
    return (
        (_PM_HOME_OVERRIDE or REPO) / ".project_manager" / ".local" / "review_rounds.json"
    )


def _legacy_round_ledger_path() -> Path:
    """옛 규칙(diff 앵커)의 라운드 장부 경로 — 1회 승계(backfill)의 입력.

    앵커를 소유 PM 홈으로 옮기기 전의 실행들은 이 경로에 카운트를 쌓아 뒀다. 그 중에는 상한을
    이미 넘겨 **차단 중인 게이트**(rc 4·사용자 승인 대기)가 있고, 새 앵커에서 0 부터 다시 세면
    그 승인 게이트가 무통보로 열린다.
    """
    return REPO / ".project_manager" / ".local" / "review_rounds.json"


def _read_round_ledger_at(path: Path) -> dict:
    """지정 경로의 라운드 장부를 읽는다 — 없거나 손상 시 빈 dict(fail-soft)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_round_ledger() -> dict:
    """라운드 장부(gate→{count, acked_through})를 읽는다 — 없거나 손상 시 빈 dict(fail-soft)."""
    return _read_round_ledger_at(_round_ledger_path())


def _inherit_legacy_round_entry(ledger: dict, gate: str) -> dict | None:
    """diff 앵커 legacy 장부의 게이트 항목을 PM 홈 장부로 **1회 승계**한다 (없으면 None).

    앵커 이동(diff_root → 소유 PM 홈)의 마이그레이션이다 — 런타임 폴백(두 장부를 계속 합산)이
    아니라 원천 이관이라, 승계 뒤에는 PM 홈 장부가 유일한 진실이다(게이트당 1회·호출부가
    즉시 저장한다). 승계 대상은 **PM 홈에 아직 그 게이트가 없을 때**뿐이라 이미 이관된 게이트의
    카운트를 옛 값으로 되돌리지 않는다. 두 앵커가 같은 실행(솔로·강등 폴백)은 대상이 아니다.
    """
    if gate in ledger:
        return None
    legacy_path = _legacy_round_ledger_path()
    if legacy_path == _round_ledger_path():
        return None
    legacy = _read_round_ledger_at(legacy_path)
    if gate not in legacy:
        return None
    ledger[gate] = _gate_entry(legacy, gate)   # 손상 필드는 저장과 같은 규칙으로 정규화
    return ledger[gate]


def _save_round_ledger(ledger: dict) -> None:
    """라운드 장부를 원자적으로 기록한다 (unique tmp + os.replace·부분기록/crash 잔재 방지).

    tmp 이름에 pid+uuid 를 실어 동시 실행 간 고정 `.tmp` 충돌(카운트 유실·write 예외)을 없앤다
    os.replace 는 원자 rename — 독자는 옛 파일 또는 새 파일만 본다(부분기록 없음).
    확인·예약·저장의 원자성은 호출자가 `_round_ledger_lock()` 임계 구역으로 보장한다."""
    path = _round_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(path))
    finally:
        with contextlib.suppress(OSError):
            if tmp.exists():
                tmp.unlink()  # replace 성공 시 no-op·실패 시 잔재 제거


def _load_file_lock():
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    `_load_relay`·`_load_board` 와 같은 경로-앵커 로더이고, 그 둘처럼 **쓰는 경로에서만** 지연
    로드한다 — 라운드 락을 잡는 건 `--gate` 실행의 예약/마감 두 구간뿐이라 나머지 경로(진단·
    denylist·재앵커를 deep-import 로 재사용하는 pm_delegate 포함)를 seam 부재로 무너뜨리지
    않는다. 로드 실패는 흡수하지 않고(fail-loud) 캐시하되, 중앙 loader 가 소비 때마다 baked rev
    를 재검증하므로 사본 skew 는 계속 표출된다.

    (지연/import-시점 선택의 근거는 **기능 축**이다 — "seam 없이도 살아야 하는 경로가 있나".
    board·worktree_pool 은 모든 변경 경로가 락을 지나 import 바인딩이 맞고, 여기는 아니다.
    fail-soft 경계 ratchet 은 그 선택의 *결과*를 계량할 뿐 근거가 아니다.)
    """
    lock_path = Path(__file__).resolve().parent / "file_lock.py"
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev, cache=True,
    )


def _round_ledger_lock_path() -> Path:
    """라운드 장부 배타락 파일 — 장부 **옆** `review_rounds.lock`(장부 경로에서 파생).

    앵커를 장부에서 파생한다 — 락과 장부가 각자 앵커를 계산하면 앵커 규칙이 갈릴 때 서로 다른
    파일을 잠근 두 실행이 같은 장부를 동시에 read-modify-write 한다(상한 우회 창).
    """
    return _round_ledger_path().with_name("review_rounds.lock")


@contextlib.contextmanager
def _round_ledger_lock() -> Iterator[None]:
    """라운드 장부 read-modify-write 를 직렬화하는 OS 파일락.

    확인(상한 대조)→예약(count+1)→저장을 하나의 임계 구역으로 묶어, 동시 실행 2개가 같은 잔여
    슬롯을 통과해 상한을 우회하는 것을 막는다. 프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock
    없음). **재진입 금지**(flock 관례·board_lock 동형) — 예약과 환불은 *각자 독립* 락 구간이다
    (중첩 아님).

    플랫폼 분기(POSIX flock·Windows msvcrt·무락 폴백)는 공용 `file_lock` seam 이 소유하고 이
    도구는 *어느 파일에 거는지*(경로 규약)만 정한다.
    """
    with _load_file_lock().exclusive_file_lock(_round_ledger_lock_path()):
        yield


def _as_int(value: object) -> int:
    """장부 필드를 int 로 강제 (손상/누락 → 0·fail-soft)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _gate_entry(ledger: dict, gate: str) -> dict:
    """게이트 항목을 전송 레코드를 포함한 현 스키마로 정규화한다.

    항목이 없거나 손상(비-dict·비정수 필드)이면 0/0 으로 정규화해 저장한다 — 반환 dict 를 그 자리에서
    수정한 뒤 `_save_round_ledger(ledger)` 하면 깨끗한 값이 기록된다(read→normalize→mutate→write)."""
    entry = ledger.get(gate)
    if not isinstance(entry, dict):
        entry = {}
    records = entry.get("records")
    if not isinstance(records, list):
        records = []
    records = [row for row in records if isinstance(row, dict)]
    normalized = {
        "count": _as_int(entry.get("count")),
        "acked_through": _as_int(entry.get("acked_through")),
        "sequence": max(
            _as_int(entry.get("sequence")),
            *(_as_int(row.get("sequence", row.get("number"))) for row in records),
            0,
        ),
        "records": records,
    }
    ledger[gate] = normalized
    return normalized


def _unacked_round_counts(entry: dict) -> tuple[int, int, int]:
    """승인 이후 (전체, 판정, 미완) 수를 구한다; 구 장부 count는 판정으로 보수 승계한다."""
    count = max(0, _as_int(entry.get("count")))
    acked = min(count, max(0, _as_int(entry.get("acked_through"))))
    records = [row for row in entry.get("records", []) if isinstance(row, dict)]
    # sequence/number는 identity일 뿐 count 좌표가 아니다. 환불로 sequence에 gap이 생겨도
    # 장부의 순서와 count만으로 승인 이전 레코드를 잘라 조기 차단/예산 우회를 모두 막는다.
    logical_records = records[-min(len(records), count):] if count else []
    legacy_count = max(0, count - len(logical_records))
    legacy_verdicts = max(0, legacy_count - acked)
    acked_records = max(0, acked - legacy_count)
    current = logical_records[acked_records:]
    verdicts = legacy_verdicts + sum(bool(row.get("verdict")) for row in current)
    total = count - acked
    return total, verdicts, max(0, total - verdicts)


def _round_has_verdict(result: dict) -> bool:
    """리뷰어 결과에 통과 또는 must-fix/반려 판정이 실제로 있었는지 판별한다.

    오염 진단이 붙은 출력은 판정으로 세지 않는다 — 판정 표면(all_pass·any_must_fix)에서 무효화한
    것을 라운드 장부에서만 판정으로 세면 두 표면이 갈린다."""
    if not result.get("ok") or result.get("contamination"):
        return False
    verdict = result.get("verdict")
    if not isinstance(verdict, dict):
        return False
    return bool(verdict.get("has_pass") or verdict.get("has_must_fix"))


def _reserve_round(entry: dict, record_id: str) -> dict:
    """단조 sequence identity로 전송을 예약하고 레코드를 반환한다."""
    entry["sequence"] = max(0, _as_int(entry.get("sequence"))) + 1
    entry["count"] = max(0, _as_int(entry.get("count"))) + 1
    record = {
        "id": record_id,
        "number": entry["sequence"],
        "sequence": entry["sequence"],
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    entry.setdefault("records", []).append(record)
    return record


def _refund_round(entry: dict, record_id: str) -> None:
    """스폰 전 실패 예약만 제거한다; sequence는 재사용하지 않는다."""
    before = len(entry.get("records", []))
    entry["records"] = [
        row for row in entry.get("records", []) if row.get("id") != record_id
    ]
    if len(entry["records"]) != before and entry.get("count", 0) > 0:
        entry["count"] -= 1


# ── 시크릿 필터링 ────────────────────────────────────────────────────────


def _matching_denylist_pattern(
    file_path: str, patterns: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> str | None:
    """파일 경로가 매칭되는 첫 시크릿 denylist 패턴을 반환한다 (매칭 없으면 None).

    `_is_secret_path` 의 매칭 로직 단일 소스 — bool 대신 '어느 패턴에 걸렸는지'(=왜 제외됐는지)를
    돌려줘 제외 보고(차단 안내·판정 병기)에 근거를 실을 수 있게 한다."""
    normalized = file_path.strip()
    for prefix in ("a/", "b/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    basename = Path(normalized).name
    for pattern in patterns:
        if fnmatch.fnmatch(normalized, pattern) or fnmatch.fnmatch(basename, pattern):
            return pattern
        if pattern.endswith("/*") or pattern.endswith("/"):
            dir_pattern = pattern.rstrip("*").rstrip("/")
            if normalized.startswith(dir_pattern + "/") or normalized == dir_pattern:
                return pattern
    return None


def _is_secret_path(file_path: str, patterns: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS) -> bool:
    """파일 경로가 시크릿 denylist 패턴에 매칭되는지 확인한다 (`_matching_denylist_pattern` 위임)."""
    return _matching_denylist_pattern(file_path, patterns) is not None


def filter_secret_hunks(
    diff_text: str, patterns: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> tuple[str, list[str]]:
    """diff 텍스트에서 시크릿 denylist 파일의 hunk 를 제거한다.

    반환: (필터링된 diff 텍스트, 제외된 파일 경로 목록)
    """
    if not diff_text:
        return diff_text, []
    excluded_files: list[str] = []
    output_blocks: list[str] = []
    current_block: list[str] = []
    current_is_secret = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_block and not current_is_secret:
                output_blocks.extend(current_block)
            current_block = [line]
            match = re.match(r"diff --git a/(\S+)\s+b/(\S+)", line)
            if match:
                file_path = match.group(2)
                current_is_secret = _is_secret_path(file_path, patterns)
                if current_is_secret:
                    excluded_files.append(file_path)
            else:
                current_is_secret = False
        else:
            current_block.append(line)
    if current_block and not current_is_secret:
        output_blocks.extend(current_block)
    return "".join(output_blocks), excluded_files


def _format_explicit_exclusion_block(excluded: list[str], patterns: tuple[str, ...]) -> str:
    """`--paths` 명시 지정분이 denylist 로 제외됐을 때의 차단 안내.

    사용자가 `--paths` 로 직접 지목한 경로가 시크릿 denylist 에 걸려 diff 에서 빠지면, 그 상태로
    리뷰를 진행해 '통과'를 내면 게이트가 실제보다 넓게 검증한 것처럼 보인다(false-confidence). 빈-diff
    가드보다 앞서 이 안내로 차단해 *denylist 가 원인*임을 정확히 알린다 — 단일 파일이 통째로
    제외돼 diff 가 비면 빈-diff 안내는 '변경 없음'으로 오도한다. 어느 경로가 어느 패턴에 걸렸는지 병기.
    우회는 새 플래그가 아니라 그 경로를 빼고 재실행(경로를 빼는 행위 자체가 '의도했다'는 표현)."""
    lines = [
        "오류: --paths 로 명시 지정한 경로가 시크릿 denylist 에 걸려 diff 에서 제외됐습니다 —",
        "  검증 안 한 것을 검증한 것처럼 보이는 가짜 통과(false-confidence)를 막기 위해 중단합니다.",
    ]
    for path in excluded:
        pattern = _matching_denylist_pattern(path, patterns)
        lines.append(f"  · {path}  (denylist 패턴 '{pattern}' 매칭)")
    lines.append(
        "  우회는 새 플래그가 아니라 위 경로를 --paths 에서 빼고 재실행하세요 — 경로를 빼는 행위가")
    lines.append(
        "  '알고 있고 의도했다'는 표현이 됩니다. denylist 패턴은 시크릿 유출 방지를 위해 유지됩니다.")
    return "\n".join(lines)


# ── git diff 추출 ─────────────────────────────────────────────────────────
# "이번 base 의 diff 는 어디까지인가"(=폭)를 여기서 한 번만 서술한다. 등록 슬롯 후보 판정
# (`_candidate_has_diff`)과 실제 추출(`extract_diff`)이 같은 단계 표를 소비해야, 후보 판정에서
# '변경 없음'으로 탈락한 슬롯을 추출은 리뷰하는(또는 그 반대) 한 칸 어긋남이 생기지 않는다.

_COMMIT_FALLBACK_BASE = "HEAD~1..HEAD"


def _diff_bases(base: str) -> tuple[str, ...]:
    """`base` 한 폭을 이루는 diff 단계 목록 — 앞 단계가 비었을 때만 다음 단계가 유효하다.

    'HEAD' 는 작업트리(스테이징+언스테이징)가 첫 단계이고, 그게 비면 직전 커밋 한 칸까지가
    같은 폭이다. 명시 base 는 단계가 하나뿐이다.
    """
    return (base, _COMMIT_FALLBACK_BASE) if base == "HEAD" else (base,)


def _slot_selection_bases(base: str) -> tuple[str, ...]:
    """등록 슬롯 후보 판정에 **소유 근거로 쓸 수 있는** 단계 — `_diff_bases` 의 부분집합.

    사용자가 지정한 base 자신만 근거다. 암묵 폴백 단계(`HEAD~1..HEAD`)는 이 슬롯이 무엇을 했는지
    말해주지 않는다 — 슬롯이 아무 것도 안 해도 **공유 base 의 마지막 커밋**이 검토 경로를
    건드렸으면 비어 있지 않아, 놀고 있는 슬롯을 '변경 슬롯'으로 뽑고 그 커밋을 이번 작업물인 양
    외부로 보낸다. 그래서 그 단계는 *이미 고른 repo 안에서 무엇을 리뷰할지*(추출 폭)에만 남기고
    *어느 repo인지*의 근거에서는 뺀다. 커밋만 된 변경으로 슬롯을 고르려면 앵커를 명시해야 한다 —
    `--base <공통 base ref>`(그 자체로 단계 하나) 또는 repo 를 직접 지정하는 절대 `--paths`.
    """
    return tuple(stage for stage in _diff_bases(base) if stage == base)


def _stage_diff_runs(
    root: Path, stage_base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> list[subprocess.CompletedProcess]:
    """한 diff 단계를 이루는 git 실행 결과 — 'HEAD' 단계만 스테이징/언스테이징 2회다."""
    _run = run_fn or subprocess.run
    arg_sets = (("--cached",), ()) if stage_base == "HEAD" else ((stage_base,),)
    return [
        _run(["git", "-C", str(root), "diff", *args, "--", *paths],
             capture_output=True, text=True, encoding="utf-8", errors="replace")
        for args in arg_sets
    ]


def _stage_diff_text(
    root: Path, stage_base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, strict: bool = False,
) -> str:
    """한 diff 단계의 원문(성공한 실행분만 이어붙임).

    strict 면 실패한 git 실행을 RuntimeError 로 올린다 — 명시 base 의 오타/미존재 리비전은
    조용한 빈 diff 가 아니라 오류여야 한다. 폴백 단계와 후보 판정은 실패를 '이 단계에는 변경
    없음'으로 본다(그 단계가 없는 repo 도 정상 형상).
    """
    runs = _stage_diff_runs(root, stage_base, paths, run_fn)
    if strict:
        for result in runs:
            if result.returncode != 0:
                raise RuntimeError(
                    f"git diff 실패 (rc={result.returncode}): {result.stderr.strip()}"
                )
    return "".join(result.stdout for result in runs if result.returncode == 0)


def extract_diff(
    base: str,
    paths: list[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    denylist: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> tuple[str, list[str]]:
    """git diff <base> -- <paths> 를 추출한다. 반환: (필터링된 diff, 제외된 경로 목록).

    시크릿 denylist 매칭 파일은 diff 에서 제외하고 그 경로 목록을 함께 돌려준다 — 제외 사실을
    호출자(main)가 차단/판정 병기에 반영할 수 있게 한다.
    버려, 제외분이 조용히 빠진 채 '통과'가 나던 게이트 false-confidence 를 낳았다. 제외 메시징은
    호출자(main)가 모드별(명시 --paths=차단 / 암묵 --ticket=병기)로 소유한다.

    폭은 `_diff_bases` 단계 표를 따른다(base 가 'HEAD' 면 스테이징+언스테이징, 비면 HEAD~1..HEAD).
    run_fn — subprocess.run 대체 주입 (테스트용).
    """
    raw_diff = ""
    for stage_base in _diff_bases(base):
        raw_diff = _stage_diff_text(
            REPO, stage_base, paths, run_fn, strict=base != "HEAD",
        )
        if raw_diff.strip():
            break

    # 제외 목록을 삼키지 않고 그대로 반환한다 — 호출자(main)가 모드별 제외 보고(차단/병기)를
    # 소유한다. stderr 경고도 main 으로 이관해 제외 메시징을 한곳에서 관장한다.
    return filter_secret_hunks(raw_diff, denylist)


# ── ticket touches 파싱 ───────────────────────────────────────────────────


def parse_ticket_touches(ticket_id: str, *, pm_home: Path | None = None) -> list[str]:
    """board ticket frontmatter 의 touches 필드를 파싱해 경로 목록을 반환한다.

    YAML frontmatter 직접 파싱 (board.py 를 import 하지 않음). 못 찾으면 fail-loud.

    ticket 디렉토리는 `_tickets_dir()`로 *호출 시점* 해소한다 —
    board/ 분리 후 wiki/ legacy 위치(stale·ticket 미발견)를 안 보게.
    """
    if not ticket_id or re.search(r"[\\/*?\[\]]", ticket_id):
        raise AnchorResolutionError(f"ticket id 형식이 안전하지 않습니다: {ticket_id!r}")
    if pm_home is None:
        tickets_dir = _tickets_dir()
    else:
        pm_dir = pm_home / ".project_manager"
        board_tickets = pm_dir / "board" / "tickets"
        tickets_dir = board_tickets if board_tickets.is_dir() else pm_dir / "wiki" / "tickets"
    for status_dir in STATUS_DIRS:
        dir_path = tickets_dir / status_dir
        if not dir_path.exists():
            continue
        for ticket_file in dir_path.glob(f"{ticket_id}-*.md"):
            return _parse_touches_from_file(ticket_file)
        exact = dir_path / f"{ticket_id}.md"
        if exact.exists():
            return _parse_touches_from_file(exact)
    raise AnchorResolutionError(
        f"ticket {ticket_id} 을 해소된 board에서 찾지 못했습니다: {tickets_dir}"
    )


def _parse_touches_from_file(path: Path) -> list[str]:
    """ticket 파일에서 frontmatter touches 를 추출한다."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return []
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        return []
    fm_text = after_open[:end]
    touches: list[str] = []
    in_touches = False
    for line in fm_text.splitlines():
        if re.match(r"^touches\s*:", line):
            empty_inline = re.match(r"^touches\s*:\s*\[\s*\]\s*$", line)
            inline_match = re.match(r"^touches\s*:\s*\[(.+)\]", line)
            if empty_inline:
                touches = []
                in_touches = False
            elif inline_match:
                items = [s.strip().strip("\"'") for s in inline_match.group(1).split(",")]
                touches = [i for i in items if i]
                in_touches = False
            elif re.match(r"^touches\s*:\s*$", line):
                in_touches = True
            else:
                val = re.sub(r"^touches\s*:\s*", "", line).strip().strip("\"'")
                if val:
                    touches = [val]
                in_touches = False
        elif in_touches:
            item_match = re.match(r"^\s*-\s+(.+)$", line)
            if item_match:
                touches.append(item_match.group(1).strip().strip("\"'"))
            elif line and not line[0].isspace():
                in_touches = False
    return touches


# ── 프롬프트 조립 ─────────────────────────────────────────────────────────


def _load_review_context() -> str:
    """review_context.local.md (인스턴스 overlay) 가 있으면 그 내용, 없으면 generic 헤더."""
    if REVIEW_CONTEXT_FILE.exists():
        try:
            return REVIEW_CONTEXT_FILE.read_text(encoding="utf-8").strip() + "\n"
        except OSError:
            pass
    return _DEFAULT_CONTEXT_HEADER


def build_prompt(
    diff: str,
    dod: str | None = None,
    adr_refs: list[str] | None = None,
    gate: str | None = None,
) -> str:
    """맥락 헤더 + 출력 형식 + diff 를 결합해 표준 리뷰 프롬프트를 생성한다."""
    parts: list[str] = [_load_review_context().rstrip() + "\n\n", _OUTPUT_FORMAT_BLOCK]
    if adr_refs:
        parts.append(f"관련 ADR: {', '.join(adr_refs)}\n\n")
    if gate:
        parts.append(f"게이트 ticket: {gate}\n\n")
    if dod:
        parts.append(f"### 완료 조건 (DoD)\n{dod}\n\n")
    parts.append("### 리뷰 대상 diff\n")
    if diff.strip():
        parts.append("```diff\n")
        parts.append(diff)
        parts.append("```\n")
    else:
        parts.append("(변경 diff 없음 — 경로에 해당하는 변경사항이 없거나 base 와 동일)\n")
    return "".join(parts)


# ── 리뷰어 가시 범위 격리 ─────────────────────────────────────────────────
#
# 외부 리뷰어의 판정 입력은 **프롬프트에 실린 diff** 여야 한다. 그런데 스폰된 리뷰어 프로세스는
# 종전에 PM 세션의 cwd 와 env 를 그대로 물려받았고, 그 자리에서 저장소 밖 PM 로컬 산출물이
# 손 닿는 거리에 있었다 — 옛 리뷰 raw(`.project_manager/.local/review/`)와 하네스 세션 전사
# (`~/.claude/projects/<cwd 슬러그>/<세션 id>.jsonl`). 실측된 게이트 raw 에서 리뷰어는 그것들을 읽어
# 같은 세션 내부 reviewer 보고를 verbatim echo 했고, 옛 raw 를 재인용했다. 오염은 이중 게이트의
# 독립성(generate≠evaluate)을 침식하고, echo 된 옛 판정 블록이 파서에 먼저 잡히면 **가짜 판정**이 된다.
# 프롬프트 금지문(텍스트 의도-가드)은 수렴하지 않으므로 기계 격리/검출로만 닫는다
# ([[mechanize-dont-instruct-llm]]).
#
# **방향 기각 근거 박제 (codex-cli 0.146.0 실측):**
#   · *리뷰어 CLI 의 read 스코프 제한* — 기각. `codex exec --help` 에 읽기 범위 옵션이 없다.
#     `--sandbox read-only` 는 쓰기 전용 제한이다: 같은 정책에서
#     `codex sandbox -c sandbox_mode='"read-only"' -- touch /tmp/x` 는 "읽기전용 파일 시스템"으로
#     거부되지만 `-- head <홈>/.codex/config.toml` 은 rc=0 으로 성공한다. 정책 구조체도
#     `writable_roots`(쓰기)만 갖고, 바이너리에 실린 codex 자신의 승인 정책 문구가
#     "The sandbox allows it read access everywhere, and write access in its writable root" 다.
#     `--sandbox-state-readable-root` 는 임의 커맨드용 `codex sandbox` 서브커맨드 전용이라
#     `codex exec` 경로에 없다. → **절대경로 읽기는 어떤 CLI 옵션으로도 못 막는다**(그래서 아래
#     echo 검출이 선택이 아니라 필수 백스톱이다).
#   · *OS 수준 읽기 경계(bwrap/unshare 등) 도입* — 채택하지 않는다. 근거 셋:
#     (a) 리뷰어 CLI 자체에는 read 스코프가 없음이 위 실측으로 확정됐고, 그 공백을 OS 로 메우려면
#         게이트가 커널 기능에 의존하게 된다.
#     (b) 이 축의 완료 기준은 "가시 범위 부재 **또는** 접근 시 검출·loud" 양로다. 채택 설계는
#         **거울**(발견 경로·유인 제거) + **env 정화**(세션/원본 포인터 제거) + **echo 검출**
#         (백스톱)의 심층 방어로 그 기준을 양쪽에서 만족한다.
#     (c) bwrap/unshare 류는 Linux 한정이라 Windows/macOS 게이트가 다른 격리 등급으로 갈린다 —
#         하네스 파리티를 깨는 인프라 확장이고, 파리티가 깨지면 "이 게이트가 무엇을 보장하는가"가
#         플랫폼마다 달라진다.
#     **알려진 한계(잔여 위험)**: 포인터가 없어도 리뷰어가 홈/전사 절대경로를 스스로 재구성해
#     조용히 참조하는 경로는 남는다. 인용하지 않으면 검출도 못 한다 — 이 한계를 감수한 선택이며,
#     감수 못 할 형상이면 OS 경계를 별도 인프라 티켓으로 세워야 한다(이 티켓 범위 밖).
#   · *`gate_snapshot` 격리 worktree 재사용* — 개념만 채택하고 구현은 기각. (a) linked worktree 의
#     `.git` 은 공유 저장소를 가리키는 **절대 gitdir 포인터**라 `cat .git` 한 번이면 원본 트리와 그
#     상위 PM 홈(`.project_manager/.local/review/`)으로 돌아오는 다리가 남는다 — 격리 목적 자체가
#     무효다. (b) `create_snapshot` 은 검토 범위가 완전히 staged 임을 요구해(untracked 신규·unstaged
#     수정에서 SnapshotError) 언스테이징 작업물 게이트를 통째로 막는다. 그래서 "저장소 밖 격리 트리"
#     라는 성질만 가져오고 복제는 **작업 트리 내용의 tracked 파일 거울**로 한다 — 원본으로 돌아가는
#     포인터가 없고, staged 를 요구하지 않아 종전 게이트 입력이 그대로 통과한다.

_REVIEWER_WORKSPACE_PREFIX = "pm_review_workspace_"

# 거울 커밋용 명시 identity — 로컬 git 설정(user.*)이 없어도 거울 생성이 실패하지 않게.
_WORKSPACE_GIT_IDENTITY = (
    "-c", "user.name=external-review",
    "-c", "user.email=external-review@localhost",
)

# 리뷰어에게 물려줄 env **최소 allowlist**(이름 완전일치·대소문자 무시). 제거-list 는 쓰지 않는다 —
# "포인터처럼 생긴 이름을 지운다"는 규칙은 새 이름 하나(`CLAUDE_TRANSCRIPT_PATH`·`CODEX_ROLLOUT_PATH`
# 처럼 인증 예외어를 품은 포인터)마다 구멍이 나고, 그 구멍이 곧 전사 위치를 손에 쥐여 주는 창이다.
# 모르는 이름은 기본 차단이고, 배포별로 더 필요한 이름은 local.conf `reviewer_env_keep_extra` 로 명시
# 추가한다(인증이 조용히 깨지지 않게 남긴 탈출구).
_REVIEWER_ENV_ALLOWLIST = frozenset({
    # 프로세스 기본 실행 환경
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ", "HOSTNAME",
    "TMPDIR", "TEMP", "TMP",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    # Windows 실행 환경
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "OS",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # 로케일·인코딩
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC",
    "LC_TIME", "LC_COLLATE", "PYTHONUTF8", "PYTHONIOENCODING",
    # 네트워크(프록시·인증서) — 사내망 리뷰어가 이걸 잃으면 호출 자체가 죽는다.
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "FTP_PROXY",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "CODEX_CA_CERTIFICATES",
    # 리뷰어 하네스 인증/설정 앵커 (세션 포인터가 아닌 것만)
    "CODEX_HOME", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ORGANIZATION",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
})
_REVIEWER_ENV_KEEP_EXTRA_KEY = "reviewer_env_keep_extra"

# 거울 생성 중 원본 git 환경 하이재킹 차단용(훅 안에서 실행되면 GIT_DIR 이 살아 있다).
_GIT_ENV_PREFIX = "GIT_"

# 임시 홈에 복제할 **인증/설정 파일** 선언 표(사용자 홈 기준 상대경로·파일만). 세션/이력/메모리는
# 여기에 절대 넣지 않는다 — 리뷰어의 홈이 그것들의 부모가 되는 순간 전사 탐색이 다시 열린다.
# 하네스별로 값만 다르고 복제 코드는 하나다. 배포별 추가분은 local.conf `reviewer_home_artifacts_extra`.
# 부재는 정상이다(그 하네스를 안 쓰는 형상) — 진짜 부족분의 증상은 "미인증/다른 모델로 실행"이고,
# 그때의 처방은 실패 진단이 직접 안내한다.
_REVIEWER_HOME_ARTIFACTS: tuple[str, ...] = (
    # codex: 로그인 + 모델/추론 설정. config 가 없으면 게이트가 **다른 모델**로 돈다.
    ".codex/auth.json",
    ".codex/config.toml",
    # claude: 자격증명 + 온보딩/신뢰 상태(`~/.claude.json`). 후자는 세션 흔적을 품고 있어
    # `_REVIEWER_HOME_JSON_SCRUB` 가 그 키를 떼고 복제한다.
    ".claude/.credentials.json",
    ".claude.json",
    # opencode: 설정은 XDG_CONFIG_HOME, 인증은 XDG_DATA_HOME 쪽이다(둘 다 임시 홈으로 재지정됨).
    # 실측(1.18.x): 데이터 디렉터리에 `auth.json` 이 없고 자격증명이 세션 이력과 함께
    # `opencode.db` 한 파일에 들어 있다 — 그 DB 는 **복제하지 않는다**(전사를 통째로 되들이는 셈이고
    # 수백 MB다). 그 형상의 opencode 리뷰어는 codex 와 동형으로 "미인증/다른 모델" 증상을 내고,
    # 처방도 같다: local.conf `reviewer_home_artifacts_extra` 로 필요한 파일만 명시 추가.
    ".local/share/opencode/auth.json",
    ".config/opencode/opencode.json",
    ".config/opencode/opencode.jsonc",
)
_REVIEWER_HOME_EXTRA_KEY = "reviewer_home_artifacts_extra"

# 복제 전에 **떼어낼 JSON 키** 선언 — 인증/온보딩과 세션 흔적이 한 파일에 섞여 있는 경우만.
# 실측(`~/.claude.json`): 최상위는 온보딩/설치 상태지만 `projects` 하위에 원본 저장소 경로 16개와
# `lastSessionId`(전사 파일명)·`lastSessionFirstPrompt`(사용자 프롬프트 원문)가 들어 있다. 통째로
# 복제하면 이 티켓이 닫은 채널이 그대로 다시 열리므로, 그 키만 떼고 나머지를 복제한다.
# 파싱 실패 시에는 복제하지 않는다(정화 못 한 파일을 홈에 두느니 미인증 증상이 낫다).
_REVIEWER_HOME_JSON_SCRUB: dict[str, tuple[str, ...]] = {
    # `projects` = 저장소 절대경로 + 전사 id + 프롬프트 원문 · `githubRepoPaths` = 저장소 경로 목록.
    ".claude.json": ("projects", "githubRepoPaths"),
}
# 같은 이유의 TOML 축 + **기능 테이블 중화**. 실측(`~/.codex/config.toml`): `projects` 는 신뢰한
# 저장소 절대경로 목록이고, `hooks`·`mcp_servers`·`plugins` 는 실 홈의 스크립트/서버 **절대경로**를
# 담는다. 후자는 경로 노출에 그치지 않는다 — 그 선언대로 리뷰어 codex 가 **실 홈의 훅/서버를 실제로
# 실행**하게 되어 격리 취지를 정면으로 뒤집는다(임시 홈에 그 파일들은 없고, 있어도 실행돼선 안 된다).
# 그래서 중화 방법은 무력화가 아니라 제거다. 리뷰어에게 필요한 건 **모델/추론 설정과 인증**뿐이고,
# 나머지 키(tui 등 표시 설정)는 경로를 담지 않으므로 그대로 남긴다.
_REVIEWER_HOME_TOML_SCRUB: dict[str, tuple[str, ...]] = {
    ".codex/config.toml": ("projects", "hooks", "mcp_servers", "plugins"),
}

# 인증 원본 위치는 `$HOME` 고정이 아니라 **기존 env 앵커 우선**이다 — 사용자가 `CODEX_HOME` 등으로
# 다른 경로를 쓰는 형상에서 홈만 보면 인증 파일을 못 찾아 게이트가 미인증으로 죽는다.
_REVIEWER_HOME_SOURCE_ANCHORS: tuple[tuple[str, str], ...] = (
    (".codex", "CODEX_HOME"),
    (".claude", "CLAUDE_CONFIG_DIR"),
    (".config", "XDG_CONFIG_HOME"),
    (".local/share", "XDG_DATA_HOME"),
)

# **라이브 검증 범위 박제**: codex 프로필은 실 게이트로 인증 생존까지 실측했다. claude 프로필은
# 기계 검증(선언·복제·env 재지정)까지만이고 **라이브 미실측**이다 — 복제한 자격증명으로 리뷰어가
# 토큰을 갱신하면 원본 refresh 토큰이 회전·무효화될 수 있어(사용자 계정에 영향) 자율 프로브 대상이
# 아니다. 필요해지면 `reviewer_home_artifacts_extra` 로 보강한 뒤 **사용자 승인 아래 라이브 1회**.

# 임시 홈이 대체하는 env — 실 홈 값을 물려주면 `~/.codex/sessions`·`~/.claude/projects` 의 부모를
# 그대로 쥐여 주는 셈이다. allowlist 를 통과한 뒤 **마지막에** 덮어쓴다.
_REVIEWER_HOME_ENV_OVERRIDES = (
    "HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR",
    "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "APPDATA", "LOCALAPPDATA",
)

# 거울에 절대 싣지 않는 저장소-내 경로(추적돼 있더라도). 리뷰 raw 장부가 사는 자리다.
_MIRROR_DENIED_PREFIXES = (PurePosixPath(".project_manager") / ".local",)


class ReviewerWorkspaceError(RuntimeError):
    """격리 작업 루트를 만들지 못함 — 호출부가 loud 경고 후 미격리 실행을 결정한다."""


class ReviewerWorkspace(NamedTuple):
    """리뷰어에게 줄 격리 컨테이너 — tracked 파일 거울(`tree`) + 세션 없는 임시 홈(`home`).

    `root` 는 둘을 담는 컨테이너다(정리 단위). 홈을 거울 **안**에 두지 않는 이유: 거울은 리뷰어가
    읽는 저장소 사본이자 `git add -A` 대상이라, 홈이 그 안에 있으면 인증 파일이 검토 대상처럼 보인다.
    """

    root: Path
    tree: Path
    home: Path
    files: int
    skipped_unsafe: int
    git_repo: bool
    copied_home_artifacts: tuple[str, ...] = ()
    skipped_secret: int = 0
    home_scrub_failed: tuple[str, ...] = ()


def _project_manager_ancestor(path: Path) -> Path | None:
    """조상 중 PM 인스턴스 루트(`.project_manager/` 보유)를 찾는다 — 없으면 None.

    격리의 기계 불변식이 이것 하나다: **격리 루트의 조상에 `.project_manager` 가 없다.** 그래야
    PM 홈 분리 형상(raw 가 상위 PM 홈)과 standalone 채택자 형상(`pm_home == diff_root == repo` 라
    raw 가 저장소 안)이 같은 검사로 함께 닫힌다.
    """
    for ancestor in path.parents:
        if (ancestor / ".project_manager").is_dir():
            return ancestor
    return None


def _tracked_relative_paths(root: Path, env: dict[str, str] | None = None) -> list[str]:
    """index 에 등재된 경로 목록(`git ls-files -z`). 신규도 stage 돼 있으면 포함된다.

    거울 생성과 **같은 정화 env** 로 돈다 — 훅 안에서 실행돼 `GIT_DIR` 이 살아 있으면 이 열거가
    다른 저장소의 목록을 돌려주고, 거울이 통째로 엉뚱한 트리가 된다.
    """
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env,
    )
    if result.returncode != 0:
        raise ReviewerWorkspaceError(
            f"tracked 파일 목록 조회 실패 (rc={result.returncode}): {result.stderr.strip()}"
        )
    return [path for path in result.stdout.split("\0") if path]


def _is_denied_mirror_path(relative: str) -> bool:
    """거울 복제 금지 경로인가 — gitignore 와 무관하게 이름으로 막는다.

    `.project_manager/.local/**` 이 추적되는 형상(채택자가 실수로 add 한 경우)에서도 옛 리뷰 raw 가
    거울에 실리면 격리가 통째로 무의미해진다. ignore 규칙에 의존하지 않는 두 번째 자물쇠다.
    """
    candidate = PurePosixPath(relative)
    return any(
        candidate == denied or candidate.is_relative_to(denied)
        for denied in _MIRROR_DENIED_PREFIXES
    )


def _contained_real_parent(path: Path, boundary: Path) -> Path | None:
    """`path` 부모 디렉터리의 realpath — 경계 밖이면 None.

    최종 경로만 lstat 하면 **중간 구성요소**가 뚫린다: tracked `dir/file` 의 `dir` 가 저장소 밖을
    가리키는 symlink 로 바뀌어 있으면 lstat 은 정상 파일을 보고하고 복사는 저장소 밖 파일을
    집어온다. realpath 는 모든 구성요소를 해소하므로 이 한 번의 검사가 경로 전체를 덮는다.
    """
    try:
        real_parent = Path(os.path.realpath(path.parent))
    except OSError:
        return None
    return real_parent if real_parent.is_relative_to(boundary) else None


def _mirror_tracked_files(
    root: Path, destination: Path,
    denylist: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> tuple[int, int, int]:
    """tracked 파일을 **작업 트리 내용 그대로** destination 에 복제한다.

    반환: (복제 수, 격리 경계 제외 수, 시크릿 denylist 제외 수).

    index 목록을 쓰되 내용은 작업 트리에서 읽는다 — 리뷰어가 읽는 파일이 프롬프트 diff(스테이징+
    언스테이징)와 어긋나지 않게. git-ignored 산출물(`.project_manager/.local/`)과 untracked 잔재는
    목록에 없으므로 애초에 실리지 않는다.

    **경계 검사**는 읽는 쪽과 쓰는 쪽 양쪽이다. 읽는 경로는 모든 구성요소를 해소한 realpath 가
    저장소 안이어야 하고(중간 디렉터리 symlink 로 저장소 밖 파일을 빨아오는 창 폐쇄), 쓰는 경로도
    거울 안이어야 한다. symlink 자체는 저장소 안을 가리키는 상대 링크만 재현한다 — 절대 링크는
    원본 트리로 돌아가는 다리이고 밖을 가리키는 링크는 격리 우회다. 제외분은 개수로 진단에 남는다.

    **시크릿 denylist 는 프롬프트와 같은 폭으로 적용한다.** diff 에서 빼놓고 거울에는 복제하면
    리뷰어가 그 파일을 그냥 열어 읽는다 — 제외의 의미(외부로 보내지 않음)가 거울 쪽 구멍으로
    무효화된다. 두 경로가 같은 해소값(local.conf 승계 포함)을 받아야 폭이 갈리지 않는다.
    """
    real_root = Path(os.path.realpath(root))
    real_destination = Path(os.path.realpath(destination))
    copied = 0
    skipped_unsafe = 0
    skipped_secret = 0
    for relative in _tracked_relative_paths(root, _workspace_git_env(destination)):
        source = root / relative
        target = destination / relative
        if _is_secret_path(relative, denylist):
            skipped_secret += 1
            continue
        if _is_denied_mirror_path(relative):
            # 추적됐더라도 리뷰 raw 장부 자리는 거울에 싣지 않는다(gitignore 에만 기대지 않는다).
            skipped_unsafe += 1
            continue
        try:
            info = source.lstat()
        except OSError:
            continue  # index 에만 있고 작업 트리에 없음(미반영 삭제) — 거울에도 없는 게 맞다.
        if stat.S_ISDIR(info.st_mode):
            continue  # gitlink(서브모듈) 작업 트리 — 이 거울의 검토 대상이 아니다.
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
            # FIFO·소켓·디바이스 노드: 복사하면 열기에서 블록되거나 의미 없는 노드를 만든다.
            skipped_unsafe += 1
            continue
        if _contained_real_parent(source, real_root) is None:
            skipped_unsafe += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if _contained_real_parent(target, real_destination) is None:
            skipped_unsafe += 1
            continue
        if stat.S_ISLNK(info.st_mode):
            link_target = os.readlink(source)
            resolved = Path(os.path.realpath(source))
            if os.path.isabs(link_target) or not resolved.is_relative_to(real_root):
                skipped_unsafe += 1
                continue
            os.symlink(link_target, target)
            copied += 1
            continue
        shutil.copy2(source, target)
        copied += 1
    return copied, skipped_unsafe, skipped_secret


def _workspace_git_env(destination: Path) -> dict[str, str]:
    """거울 git 실행 전용 환경 — 바깥 git 설정/훅/저장소 포인터를 전부 끊는다.

    `--no-verify` 는 pre-commit/commit-msg 만 막는다. 사용자 global/system config 의
    `core.hooksPath`·`init.templateDir`·`commit.gpgsign` 은 그대로 살아, 거울을 만드는 이 몇 줄이
    바깥에서 주입된 스크립트를 실행하고 거울 내용을 바꿀 수 있는 창이 된다. 그래서 config 자체를
    끊는다(구형 git 이 `GIT_CONFIG_GLOBAL` 을 모르면 HOME/XDG 재지정이 같은 일을 한다).
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(_GIT_ENV_PREFIX)
    }
    scratch = destination / ".git"  # git 이 내용으로 취급하지 않는 유일한 경로.
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "HOME": str(scratch),
        "XDG_CONFIG_HOME": str(scratch),
    })
    return env


def _init_workspace_git(destination: Path) -> bool:
    """거울을 자족 git 저장소로 만든다(원본을 가리키는 포인터 없음). 실패는 fail-soft(False).

    `--skip-git-repo-check` 를 안 붙인 채택자 `reviewer_cmd` 도 종전처럼 동작하게 하는 게 목적이다.
    실행 설정은 전용 env + 명시 `-c` 뿐이라 바깥 git config 가 이 단계에 개입하지 못한다.
    """
    env = _workspace_git_env(destination)
    hooks_path = destination / ".git" / "pm-review-no-hooks"
    isolation = (
        "-c", f"core.hooksPath={hooks_path}",
        "-c", "core.fsmonitor=false",
        "-c", "commit.gpgsign=false",
    )
    steps = (
        ("-c", "init.defaultBranch=main", "-c", "init.templateDir=", "init", "-q"),
        ("add", "-A"),
        (*_WORKSPACE_GIT_IDENTITY, "commit", "-q", "--no-verify",
         "-m", "external review basis"),
    )
    for step in steps:
        result = subprocess.run(
            ["git", "-C", str(destination), *isolation, *step],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        if result.returncode != 0:
            return False
    return True


def reviewer_home_artifacts(conf: dict[str, str] | None = None) -> tuple[str, ...]:
    """임시 홈에 복제할 인증/설정 파일의 홈-상대 경로 — 엔진 선언 + 배포 추가분."""
    extra = (conf or {}).get(_REVIEWER_HOME_EXTRA_KEY, "").strip()
    declared = tuple(p for p in re.split(r"[,\s]+", extra) if p) if extra else ()
    return _REVIEWER_HOME_ARTIFACTS + declared


def _reviewer_home_source(
    relative: PurePosixPath, base: Path, env: dict[str, str],
) -> Path:
    """인증 원본 경로 — 선언된 env 앵커가 있으면 그것을, 없으면 홈 기준 상대경로를 쓴다."""
    for prefix, key in _REVIEWER_HOME_SOURCE_ANCHORS:
        anchor = PurePosixPath(prefix)
        if relative == anchor or relative.is_relative_to(anchor):
            configured = (env.get(key) or "").strip()
            if configured:
                remainder = relative.relative_to(anchor)
                return Path(configured) / Path(*remainder.parts)
            break
    return base / Path(*relative.parts)


class ReviewerHomeBuild(NamedTuple):
    """임시 홈 구성 결과 — 복제분과 **정화 실패로 빠진 분**을 구분해 진단에 싣는다.

    둘을 뭉뚱그리면 "그 하네스를 안 쓰는 형상(부재)"과 "정화 실패로 설정이 빠져 다른 모델로 도는
    형상"이 같은 화면이 되어, 게이트가 왜 이상하게 도는지 PM 이 알 방법이 없다.
    """

    copied: tuple[str, ...]
    scrub_failed: tuple[str, ...]


def _leaks_isolated_paths(payload: str, forbidden: Sequence[str]) -> bool:
    """정화 결과에 격리 대상 절대경로가 남았는가 — 남으면 그 아티팩트는 복제하지 않는다.

    키 열거만으로는 **새로 생기는 경로 키**를 못 막는다(도구가 판올림하며 테이블이 는다). 그래서
    선언표로 아는 것을 지우고, 그러고도 남은 절대경로를 성질로 한 번 더 막는다 — 이쪽이 조용히
    열리는 경로를 닫는 실제 자물쇠다. 걸리면 '정화 실패'로 진단에 뜬다(부재와 구분).
    """
    return any(marker and marker in payload for marker in forbidden)


def _build_reviewer_home(
    home: Path, artifacts: Sequence[str], source_home: Path | None = None,
    env: dict[str, str] | None = None, forbidden_paths: Sequence[str] = (),
) -> ReviewerHomeBuild:
    """세션·이력이 없는 임시 홈을 만들고 선언된 인증/설정 파일만 복제한다.

    실 홈을 물려주면 리뷰어에게 `~/.codex/sessions`·`~/.claude/projects` 의 **부모**를 쥐여 주는
    셈이라, 인용 없이 내용만 반영된 오염은 어떤 검출로도 안 보인다. 그래서 홈 자체를 갈아끼우되
    게이트가 죽지 않도록 로그인/설정 파일만 골라 복제한다(디렉터리는 복제하지 않는다 — 하위에
    이력이 섞여 들어온다). 복제 실패는 조용히 넘긴다: 없는 파일은 그 하네스를 안 쓴다는 뜻이고,
    진짜 인증 파손은 리뷰어 실행이 loud 하게 실패시킨다.
    """
    base = Path(source_home if source_home is not None else Path.home())
    resolved_env = dict(os.environ if env is None else env)
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    copied: list[str] = []
    scrub_failed: list[str] = []
    for relative in artifacts:
        candidate = PurePosixPath(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            continue
        source = _reviewer_home_source(candidate, base, resolved_env)
        if not source.is_file():
            continue
        payload: str | None = None
        json_scrub = _REVIEWER_HOME_JSON_SCRUB.get(relative)
        toml_scrub = _REVIEWER_HOME_TOML_SCRUB.get(relative)
        if json_scrub is not None or toml_scrub is not None:
            payload = (
                _scrubbed_json_text(source, json_scrub) if json_scrub is not None
                else _scrubbed_toml_text(source, toml_scrub)
            )
            if payload is not None and _leaks_isolated_paths(payload, forbidden_paths):
                payload = None  # 선언표가 못 지운 경로 키가 남았다 — 성질 검사가 잡는다.
            if payload is None:
                scrub_failed.append(relative)
                continue  # 정화 실패 — 흔적이 든 채로 홈에 두느니 없는 게 낫다.
        target = home / Path(*candidate.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(target.parent, 0o700)
        if payload is None:
            shutil.copy2(source, target)
        else:
            target.write_text(payload, encoding="utf-8")
        os.chmod(target, 0o600)
        copied.append(relative)
    return ReviewerHomeBuild(tuple(copied), tuple(scrub_failed))


def _scrubbed_json_text(source: Path, drop_keys: Sequence[str]) -> str | None:
    """최상위 선언 키를 떼어낸 JSON 본문 — 읽기/파싱/형식이 어긋나면 None(복제 안 함)."""
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in drop_keys:
        payload.pop(key, None)
    return json.dumps(payload, ensure_ascii=False)


# TOML 테이블 헤더(`[a.b]`·`[[a.b]]`) 인식 — 줄 단위 절단으로 테이블 하나를 통째로 뺀다.
_TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$")


def _scrubbed_toml_text(source: Path, drop_tables: Sequence[str]) -> str | None:
    """선언된 최상위 테이블을 떼어낸 TOML 본문 — 읽기/파싱/잔존 검증에 실패하면 None.

    TOML 을 쓰기(직렬화)하는 표준 모듈이 없어 **줄 단위로 테이블 구간을 뺀 뒤** 결과를 다시
    파싱해 (a) 문법이 살아 있고 (b) 드롭 대상이 실제로 사라졌는지 확인한다. 인라인 선언
    (`projects = {...}`)처럼 줄 절단으로 못 빼는 형태는 이 검증에서 걸려 복제 자체가 취소된다.
    """
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    dropped = set(drop_tables)
    kept: list[str] = []
    dropping = False
    for line in text.splitlines(keepends=True):
        header = _TOML_TABLE_HEADER_RE.match(line)
        if header is not None:
            root_key = header.group(1).strip().split(".", 1)[0].strip().strip("\"'")
            dropping = root_key in dropped
        if dropping:
            continue
        kept.append(line)
    scrubbed = "".join(kept)
    try:
        parsed = tomllib.loads(scrubbed)
    except (tomllib.TOMLDecodeError, ValueError):
        return None
    if any(key in parsed for key in dropped):
        return None
    return scrubbed


def create_reviewer_workspace(
    diff_root: Path, *, base_dir: Path | None = None,
    conf: dict[str, str] | None = None, source_home: Path | None = None,
    denylist: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> ReviewerWorkspace:
    """저장소 밖에 리뷰어 격리 컨테이너(거울 + 임시 홈)를 만들고 정체를 반환한다.

    복사/링크/git 실행에서 오는 `OSError` 계열은 전부 `ReviewerWorkspaceError` 로 정규화한다 —
    호출부의 복구 채널(`--allow-unisolated-reviewer`)이 실제로 타야지, traceback 으로 죽으면 안 된다.
    """
    root = Path(diff_root).resolve()
    base = Path(base_dir).resolve() if base_dir is not None else Path(tempfile.gettempdir())
    try:
        container = Path(tempfile.mkdtemp(prefix=_REVIEWER_WORKSPACE_PREFIX, dir=base))
    except OSError as exc:
        raise ReviewerWorkspaceError(f"격리 작업 루트 생성 실패: {exc}") from exc
    try:
        resolved = container.resolve()
        polluted = _project_manager_ancestor(resolved)
        if polluted is not None:
            raise ReviewerWorkspaceError(
                "격리 작업 루트의 조상이 PM 인스턴스입니다 — 그 자리에서는 로컬 리뷰 raw 가 "
                f"리뷰어 손에 닿습니다: {resolved} (PM 인스턴스: {polluted}). "
                "TMPDIR 을 저장소/PM 홈 밖으로 지정하세요."
            )
        if resolved.is_relative_to(root) or root.is_relative_to(resolved):
            raise ReviewerWorkspaceError(
                f"격리 작업 루트가 검토 저장소와 겹칩니다: {resolved} (저장소: {root})"
            )
        tree = container / "tree"
        home = container / "home"
        tree.mkdir()
        home_build = _build_reviewer_home(
            home, reviewer_home_artifacts(conf), source_home,
            forbidden_paths=(
                str(Path(source_home) if source_home is not None else Path.home()),
                str(root),
            ),
        )
        copied, skipped_unsafe, skipped_secret = _mirror_tracked_files(
            root, tree, denylist,
        )
        if copied == 0:
            raise ReviewerWorkspaceError(
                f"복제할 tracked 파일이 없습니다 (저장소: {root})"
            )
        git_repo = _init_workspace_git(tree)
    except ReviewerWorkspaceError:
        shutil.rmtree(container, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(container, ignore_errors=True)
        raise ReviewerWorkspaceError(f"격리 작업 루트 구성 실패: {exc}") from exc
    except Exception:
        shutil.rmtree(container, ignore_errors=True)
        raise
    return ReviewerWorkspace(
        root=container, tree=tree, home=home, files=copied,
        skipped_unsafe=skipped_unsafe, git_repo=git_repo,
        copied_home_artifacts=home_build.copied, skipped_secret=skipped_secret,
        home_scrub_failed=home_build.scrub_failed,
    )


def reviewer_env_keep_extra(conf: dict[str, str]) -> tuple[str, ...]:
    """배포가 추가로 물려주라고 선언한 env 이름(대문자 정규화) — allowlist 확장 입력."""
    raw = conf.get(_REVIEWER_ENV_KEEP_EXTRA_KEY, "").strip()
    return tuple(name.upper() for name in re.split(r"[,\s]+", raw) if name) if raw else ()


def reviewer_env(
    tree: Path | None,
    home: Path | None = None,
    env: dict[str, str] | None = None,
    *,
    extra_keep: Sequence[str] = (),
) -> dict[str, str] | None:
    """리뷰어에게 넘길 환경 — **allowlist 구성**(모르는 이름은 차단). 미격리면 None(상속 유지).

    `CLAUDE_CODE_SESSION_ID` 하나면 전사 파일이 확정되고 `GIT_DIR`/`CLAUDE_PROJECT_DIR` 하나면
    원본 트리가 확정되므로 cwd 격리만으로는 부족하다. 지울 이름을 세는 규칙은 새 포인터 이름마다
    구멍이 난다(인증 예외어를 품은 `*_TRANSCRIPT_PATH`·`*_ROLLOUT_PATH` 가 실제 반례). 그래서
    **물려줄 이름만** 세고 나머지는 이름이 무엇이든 차단한다.

    relay 의 하네스 세션 마커 선언은 이 구성에서 자동으로 빠진다 — 마커 목록을 여기 다시 적지
    않고, 테스트가 "선언된 마커 전량이 결과 env 에 없다"를 단언해 두 표가 갈릴 여지를 없앤다.

    allowlist 를 통과한 **홈 계열 값은 마지막에 임시 홈으로 덮어쓴다**. 실 홈 값을 남기면 이름을
    아무리 걸러도 `~/.codex/sessions`·`~/.claude/projects` 의 부모를 그대로 넘기는 것과 같다.
    """
    if tree is None:
        return None
    source = dict(os.environ if env is None else env)
    allowed = _REVIEWER_ENV_ALLOWLIST | {name.upper() for name in extra_keep}
    resolved = {key: value for key, value in source.items() if key.upper() in allowed}
    if home is not None:
        for key in list(resolved):
            if key.upper() in _REVIEWER_HOME_ENV_OVERRIDES:
                del resolved[key]
        resolved.update({
            "HOME": str(home),
            "CODEX_HOME": str(home / ".codex"),
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
        })
        if os.name == "nt":
            resolved.update({
                "USERPROFILE": str(home),
                "APPDATA": str(home / "AppData" / "Roaming"),
                "LOCALAPPDATA": str(home / "AppData" / "Local"),
            })
    # cwd 를 무시하고 PWD 로 작업 루트를 해석하는 하네스(opencode 실측)까지 같은 값으로 닫는다.
    resolved["PWD"] = str(tree)
    return resolved


UNISOLATED_REVIEWER_FLAG = "--allow-unisolated-reviewer"

_UNISOLATED_GUIDANCE = (
    "오류: 리뷰어 가시 범위 격리 실패 — 외부 전송 전에 중단합니다: {reason}\n"
    "  격리 없이 실행하면 리뷰어가 PM 세션 cwd 에서 옛 리뷰 raw·세션 전사에 닿습니다. echo 검출은\n"
    "  *인용한 경우*만 잡으므로, 읽고도 인용하지 않은 참조는 아무도 못 봅니다 — 그래서 기본은 차단입니다.\n"
    f"  · 격리 실패 사유를 먼저 해소하세요(TMPDIR 위치·디스크·git 실행 가능 여부).\n"
    f"  · 그래도 이번 1회를 미격리로 보내야 하면 `{UNISOLATED_REVIEWER_FLAG}` 를 명시하세요 "
    "(loud 경고 · 판정 강등 없음)."
)


def _remove_reviewer_workspace(workspace: ReviewerWorkspace) -> None:
    """격리 컨테이너 정리 — 실패를 조용히 삼키지 않는다(저장소 사본이 남는다는 뜻이다)."""
    try:
        shutil.rmtree(workspace.root)
    except OSError as exc:
        print(
            "[external-review] 경고: 리뷰어 격리 컨테이너 정리 실패 — 저장소 사본과 인증 파일 "
            f"사본이 남아 있습니다. 직접 지우세요: {workspace.root} ({exc})",
            file=sys.stderr,
        )


@contextlib.contextmanager
def reviewer_visibility_scope(
    diff_root: Path, *, base_dir: Path | None = None, allow_unisolated: bool = False,
    conf: dict[str, str] | None = None,
    denylist: tuple[str, ...] = _SECRET_DENYLIST_PATTERNS,
) -> Iterator[ReviewerWorkspace | None]:
    """리뷰어 작업 루트를 만들고 실행 뒤 지운다. 실패는 **기본 차단**(예외 전파).

    미격리 폴백을 기본값으로 두지 않는 이유: echo 검출은 리뷰어가 *인용한* 참조만 잡는다. 읽고
    조용히 판정에 반영한 참조는 어떤 검출로도 안 보이므로, 격리가 없으면 오염 여부를 아무도 모른다.
    대신 자기잠김을 막는 명시 탈출구를 하나 남긴다(`allow_unisolated`) — 그 경로는 조용하지 않고
    loud 경고를 남기며, 판정은 건드리지 않는다(격리 부재는 오염의 *증거*가 아니라 가능성이다).
    """
    workspace: ReviewerWorkspace | None = None
    try:
        workspace = create_reviewer_workspace(
            diff_root, base_dir=base_dir, conf=conf, denylist=denylist,
        )
    except ReviewerWorkspaceError:
        if not allow_unisolated:
            raise
        print(
            f"[external-review] 경고: {UNISOLATED_REVIEWER_FLAG} 로 미격리 실행합니다 — "
            "리뷰어가 PM 세션 cwd 에서 옛 리뷰 raw·세션 전사를 탐색할 수 있습니다.",
            file=sys.stderr,
        )
    try:
        yield workspace
    finally:
        if workspace is not None:
            _remove_reviewer_workspace(workspace)


# ── 외부 리뷰어 실행 ──────────────────────────────────────────────────────


def _load_relay():
    """엔진 pm_relay 를 importlib 로 직접 로드 — **무진행 판정 공용 seam** 재사용
    (`run_with_first_event_watchdog`·`idle_timeout_for_signal`·프로세스그룹 kill·부분 산출물 보존).

    pm_delegate 가 이 모듈을 deep-import 해 denylist/재앵커를 재사용하는 선례의 대칭 방향이다
    (형제 `.project_manager/tools/`·PYTHONPATH 무의존). **복붙 구현 금지** — 두 표면(위임·외부
    리뷰)의 판정 규칙이 코드 상 하나여야 규칙이 둘로 갈리지 않는다."""
    path = Path(__file__).resolve().parent / "pm_relay.py"
    return _load_module_from_path(
        path, "pm_relay.py", verifier=_verify_engine_rev,
    )


def _reviewer_idle_timeout(reviewer_cmd: str, idle_timeout: float | None) -> float | None:
    """리뷰어 축의 무진행 상한 해소 — 공용 선언 테이블(`idle_timeout_for_signal`)을 그대로 탄다.

    리뷰어 기본 커맨드(`codex exec --sandbox read-only`)는 `--json` 이 없어 이벤트 스트림이 아니라
    **평문 증분** 축이다. 그래도 진행 신호는 있다 — 실측상 진행 로그(hook/exec/succeeded 라인)가
    stderr 로 촘촘히 흐르고(리뷰 1건 12,233줄) stdout 은 최종 회신뿐이라(498~759 바이트), chunk
    도착 자체를 신호로 보면 파서 없이 같은 판정이 선다. 특례 분기 없이 축 선언만 다르다.
    명시값이 없으면 리뷰어 커맨드의 하네스 프로필 값을 쓴다(위임 축과 같은 테이블)."""
    relay = _load_relay()
    profile = reviewer_profile(reviewer_cmd)
    resolved = idle_timeout if idle_timeout is not None else profile.idle_timeout
    return relay.idle_timeout_for_signal(profile.progress_signal, resolved)


def _reviewer_watchdog_settings(reviewer_cmd: str):
    """해소된 리뷰어 프로필의 startup 선언을 공용 워치독 인자로 변환한다."""
    relay = _load_relay()
    profile = reviewer_profile(reviewer_cmd)
    first_event_timeout = (
        relay.first_event_timeout_default() if profile.startup_watchdog else None
    )
    retries = relay.stall_retries_default() if profile.startup_watchdog else 0
    return relay, first_event_timeout, retries


def _reviewer_execution_budget(reviewer_cmd: str, timeout: int) -> int:
    """리뷰 1회의 실행+재시도별 실제 최악 정리 예산(위임 축과 같은 공용 식)."""
    relay, first_event_timeout, retries = _reviewer_watchdog_settings(reviewer_cmd)
    return int(relay.watchdog_execution_budget(
        timeout,
        first_event_timeout=first_event_timeout,
        retries=retries,
    ))


def _watchdog_reviewer_run(argv, *, input=None, timeout=None, idle_timeout=None,
                           cwd=None, env=None,
                           **_ignored) -> subprocess.CompletedProcess:
    """기본 리뷰어 러너 — pm_relay 공용 워치독 경유(무진행 주 판정 + 벽시계 백스톱).

    `subprocess.run(..., timeout=)` 을 대체한다. 그 단일 호출은 증분 관측이 아예 없어서 (a) 정상
    진행을 벽시계로 죽이고 (b) `TimeoutExpired.stdout` 에 실려 온 부분 산출물을 아무도 안 읽어
    통째로 버렸다(kill 된 리뷰의 raw 가 헤더 138바이트뿐이던 실측). startup 창/재시도는 리뷰어
    이름 특례가 아니라 **해소된 공유 프로필 선언**을 따른다. 따라서 기본 codex(False)는 종전처럼
    꺼져 있고, 알려진 startup stall 축인 opencode(True)만 유한 재시도를 얻는다.

    `cwd`/`env` 는 리뷰어 가시 범위 격리 입력이다 — 시그니처에 명시해 relay 워치독까지 내려보낸다
    (`**_ignored` 가 삼키면 격리가 조용히 사라진다)."""
    reviewer_cmd = shlex.join(argv)
    relay, first_event_timeout, retries = _reviewer_watchdog_settings(reviewer_cmd)
    return relay.run_with_first_event_watchdog(
        argv,
        first_event_timeout=first_event_timeout,
        overall_timeout=float(timeout),
        retries=retries,
        idle_timeout=idle_timeout,
        input_text=input,
        cwd=cwd,
        env=env,
    )  # text 는 워치독 기본(True·utf-8/replace 고정) — 인코딩은 _WatchedPopen 이 소유.


def harness_cap_advisory(
    env: dict[str, str] | None = None, *, execution_budget: int
) -> str | None:
    """리뷰 엔진보다 먼저 끝나는 외부 Bash 명시호출 최대상한을 런타임에 loud 표면화한다.

    문서의 호출층 timeout 계약이 누락된 기존 채택자도 진단을 얻는 백스톱이다. 실행은 차단하지
    않으며 Codex처럼 공개 상한 env가 없는 표면은 판정하지 않는다.
    """
    env = os.environ if env is None else env
    # 설정 경로만 있는 비하네스 쉘에서는 형제 모듈을 읽지 않는다. 이 검사는
    # 판정 표가 아니라 로더 조기 반환용 보수적 후보 필터이며, 실제 판정은 relay 선언만 쓴다.
    if not any(
        value and _is_possible_harness_session_key(key)
        for key, value in env.items()
    ):
        return None
    relay = _load_relay()
    session_markers = relay.HARNESS_SESSION_MARKERS
    cap_env = relay.HARNESS_CAP_ENV
    return relay.harness_cap_advisory(
        env, execution_budget=execution_budget,
        session_markers=session_markers, cap_env=cap_env,
        render_missing=None,
        render_invalid=lambda harness, cap_key, raw, required: (
            f"[external-review] 경고: {harness} 호출층 상한 {cap_key}={raw!r} 해석 불가 — "
            f"리뷰 실행+재시도별 정리+박제 여유 {required}s 이상을 Bash tool timeout으로 명시하세요."
        ),
        render_low=lambda harness, cap_key, cap_seconds, required: (
            f"[external-review] 경고: {harness} 호출층 최대상한 "
            f"{cap_key}={cap_seconds:g}s < "
            f"리뷰 실행+재시도별 정리+박제 여유 {required}s — 엔진 진단/부분 산출물 보존 전에 하네스가 kill할 수 "
            "있습니다. Bash tool 호출에 장시간 timeout을 명시하세요."
        ),
    )


def _is_possible_harness_session_key(key: str) -> bool:
    """relay를 안 읽는 조기 반환용 사설 필터(선언표 coverage는 테스트가 단언)."""
    return key.startswith(("CODEX_", "CLAUDE", "OPENCODE")) and "CONFIG" not in key


def _timeout_output(timeout: int, exc: subprocess.TimeoutExpired) -> str:
    """타임아웃 실패 본문 — 사유(무진행/벽시계) + **kill 시점까지 받은 부분 산출물**.

    부분 산출물을 붙이는 게 핵심이다: 판정 기준을 고쳐도 감지기가 늦게 울리는 실행은
    남으므로, 그때 수백 초어치 리뷰가 0바이트가 되는 현행 동작을 그대로 두면 안 된다."""
    idle_seconds = getattr(exc, "idle_seconds", None)
    silence_seconds = getattr(exc, "silence_seconds", idle_seconds)
    timeout_axis = getattr(
        exc, "timeout_axis", "idle" if idle_seconds is not None else "wall"
    )
    threshold = float(getattr(exc, "threshold_seconds", exc.timeout or timeout))
    if timeout_axis != "idle":
        silence_label = (
            f" · 중단 시 실측 침묵 {silence_seconds:.0f}초"
            if silence_seconds is not None else " · 중단 시 침묵 관측 불가"
        )
        head = (f"[리뷰어 타임아웃 — 벽시계 백스톱 {threshold:.0f}초 초과"
                f"{silence_label}] "
                "재시도: `--timeout <초>` 또는 local.conf "
                "`external_review_timeout=<초>` (양의 정수).")
    else:
        measured = idle_seconds if idle_seconds is not None else silence_seconds
        measured_label = f"{measured:.0f}초" if measured is not None else "관측 불가"
        head = (f"[리뷰어 타임아웃 — 무진행 임계 {threshold:.0f}초 발화"
                f" · 실측 침묵 {measured_label}] "
                f"벽시계 상한 {timeout}초는 미도달. 재시도: `--idle-timeout <초>` 또는 local.conf "
                f"`{EXTERNAL_IDLE_TIMEOUT_KEY}=<초>` (양의 정수).")
    try:
        return _load_relay().format_partial_output(head, exc)
    except Exception as formatter_exc:  # noqa: BLE001 - timeout diagnosis must survive formatter/load failure.
        if _is_engine_rev_skew(formatter_exc):
            raise
        return head


# raw 박제/진단 **표시**에만 쓰는 구분자. 판정 경계는 이 문자열이 아니라 아래 두 필드다 —
# 표시 형식으로 경계를 되찾으려 하면 모델 회신이나 재인용된 옛 raw 에 같은 문자열이 들어오는 순간
# `옛 통과 → 구분자 → 이번 반려` 가 '깨끗한 통과'로 잘린다(구분자 파싱 = 신뢰 경계로 부적격).
_STDERR_SECTION_MARKER = "\n[stderr]\n"


class ReviewerOutput(NamedTuple):
    """리뷰어 실행 산출물 — 두 채널을 **구조로** 분리 보관한다.

    `answer` = 리뷰어 회신(stdout). 판정·오염 검출은 오직 이것만 본다.
    `log`    = 진행 로그(stderr). 프롬프트 전문과 검토 대상 diff 원문이 그대로 실려 오므로 판정
               입력이 될 수 없다(실측). raw 박제와 사람 진단에만 쓴다.
    """

    answer: str
    log: str = ""

    @property
    def combined(self) -> str:
        """사람이 읽는 박제/진단 표시 — 표시 전용이고 다시 파싱하지 않는다."""
        return self.answer + (f"{_STDERR_SECTION_MARKER}{self.log}" if self.log else "")


def _as_reviewer_output(value: object) -> ReviewerOutput:
    """주입 러너/스텁이 돌려준 산출물을 두 채널 구조로 정규화한다(문자열 = 회신만)."""
    return value if isinstance(value, ReviewerOutput) else ReviewerOutput(str(value))


class ReviewerRunSeamError(TypeError):
    """주입 runner 의 호출 계약 불일치 — 리뷰어 프로세스 오류와 구분하는 loud sentinel."""


def _reviewer_run_kwargs(run_fn: Callable, argv: list[str], *,
                         prompt: str, timeout: int, idle_timeout: float | None,
                         cwd: str | None = None,
                         env: dict[str, str] | None = None) -> dict:
    """기존 subprocess.run 호환 seam 을 보존하고 명시 지원 runner 에만 idle_timeout 을 확장한다.

    `**kwargs` 만으로는 새 키를 실제 소비하는지 알 수 없다(`subprocess.run` 은 **kwargs 를 Popen 에
    넘겨 뒤늦게 TypeError). 따라서 시그니처에 `idle_timeout` 이 명시된 runner 에만 전달한다.
    호출 전에 bind 해 seam skew 를 프로세스 실행 오류와 분리한다.

    `cwd`/`env`(리뷰어 가시 범위 격리)는 **값이 있을 때 무조건** 넘긴다 — 조건부로 넘기면 그 키를
    선언하지 않은 runner 에서 격리가 조용히 사라져(silent degrade) 오염 차단이 있다고 착각하게 된다.
    받지 못하는 runner 는 아래 bind 검사가 loud seam 오류로 세운다. 격리 미적용 실행(None)의 kwargs 는
    종전과 바이트 단위로 같다.
    """
    kwargs = {
        "input": prompt,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
    if cwd is not None:
        kwargs["cwd"] = cwd
    if env is not None:
        kwargs["env"] = env
    try:
        signature = inspect.signature(run_fn)
    except (TypeError, ValueError):
        return kwargs  # introspection 불가 callable 은 기존 seam 만 보수적으로 전달.
    parameter = signature.parameters.get("idle_timeout")
    if parameter is not None and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
        kwargs["idle_timeout"] = idle_timeout
    try:
        signature.bind(argv, **kwargs)
    except TypeError as exc:
        raise ReviewerRunSeamError(str(exc)) from exc
    return kwargs


def _run_reviewer_ex(
    prompt: str,
    reviewer_cmd: str,
    timeout: int | None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None,
    idle_timeout: float | None = None,
    metrics: dict[str, object] | None = None,
    *,
    cwd: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[bool, ReviewerOutput, bool]:
    """run_reviewer 본체 + 외부 프로세스 스폰 여부(started) 신호.

    반환: (성공 여부, `ReviewerOutput`(회신/로그 **분리 보관**), started). started=False = 외부 프로세스가 *확실히 시작되지
    않음*(전송 0·과금 0) — 빈 reviewer_cmd·실행 파일 부재(FileNotFoundError). started=True =
    스폰됨(프롬프트가 전송·과금됐을 수 있음) — 정상 종료(비-0 rc 포함)·타임아웃·기타 실행 오류.
    타임아웃/기타는 시작 여부가 불확실하거나 이미 전송됐으므로 보수적으로 started=True — 라운드
    환불은 started=False 일 때만 해(반복 타임아웃으로 상한을 무한 우회하지 못하게). 확실히 전송
    전인 경우만 환불한다.

    **판정 기준**: 기본 러너가 `subprocess.run` 단일 호출에서 pm_relay 공용 워치독으로
    바뀌어, 주 판정이 "시작 후 경과"가 아니라 "마지막 진행 이후 무진행"이다(벽시계는 백스톱).
    `idle_timeout=None` 이면 reviewer_cmd 프로필 선언이 적용된다.

    `run_fn` 주입의 기존 계약은 subprocess.run 호환 키까지다. 새 `idle_timeout` 은 시그니처에 그
    이름을 명시한 runner 에만 조건부 전달한다. 호출 계약 불일치는 일반 "리뷰어 실행 오류"로
    삼키지 않고 seam 오류로 loud 구분한다."""
    if metrics is not None:
        metrics.clear()
        metrics.update({"rc": 1, "silence_sec": None})
    _run = run_fn or _watchdog_reviewer_run
    argv = shlex.split(reviewer_cmd)
    if not argv:
        return False, ReviewerOutput("[reviewer_cmd 가 비어 있음 — local.conf 확인]"), False
    if timeout is None:  # 미지정 호출(공개 facade) — 리뷰어 프로필의 벽시계 백스톱.
        timeout = int(reviewer_profile(reviewer_cmd).wall_timeout)
    try:
        kwargs = _reviewer_run_kwargs(
            _run, argv, prompt=prompt, timeout=timeout,
            idle_timeout=_reviewer_idle_timeout(reviewer_cmd, idle_timeout),
            cwd=None if cwd is None else str(cwd), env=env,
        )
        result = _run(argv, **kwargs)
        if metrics is not None:
            metrics["rc"] = int(result.returncode)
            metrics["silence_sec"] = getattr(result, "silence_sec", None)
        # 두 채널을 합치지 않는다 — 합치면 경계가 문자열이 되고, 그 문자열은 모델 회신이나
        # 재인용된 옛 raw 에 그대로 나타날 수 있다(판정 경계로 부적격).
        return (
            result.returncode == 0,
            ReviewerOutput(result.stdout or "", result.stderr or ""),
            True,
        )
    except subprocess.TimeoutExpired as exc:
        # 프로세스가 시작돼 실행 중 타임아웃 — 프롬프트가 이미 전송·과금됐을 수 있다 → started=True.
        if metrics is not None:
            metrics["silence_sec"] = getattr(
                exc, "silence_seconds", getattr(exc, "idle_seconds", None)
            )
        return False, ReviewerOutput(_timeout_output(timeout, exc)), True
    except FileNotFoundError:
        # 실행 파일 자체가 없어 exec 실패 — 아무것도 전송되지 않았다 → started=False (환불 대상).
        if metrics is not None:
            metrics["rc"] = 127
        return False, ReviewerOutput(
            f"[리뷰어 명령 '{argv[0]}' 를 찾을 수 없음 — 설치 또는 PATH 확인]"), False
    except ReviewerRunSeamError as exc:
        # 호출 전 bind 실패 — 외부 프로세스는 확실히 시작되지 않았다. 라운드 환불 가능.
        return False, ReviewerOutput(
            f"[리뷰어 runner seam 계약 오류 — 호출 전 차단: {exc}]"), False
    except TypeError as exc:
        # runner 내부에서 발생한 키워드/시그니처 skew. 시작 여부는 불명이라 보수적으로 started=True.
        return False, ReviewerOutput(f"[리뷰어 runner seam 호출 오류: {exc}]"), True
    except Exception as exc:  # noqa: BLE001
        # _load_relay는 호출마다 독립 module 객체를 만들 수 있어 클래스 identity 대신 sentinel의
        # 구조화 속성을 본다. 일반 RuntimeError를 정리 실패로 오인하지 않는다.
        if _is_engine_rev_skew(exc):
            raise
        if getattr(exc, "process_cleanup_failed", False) is True:
            output = (
                f"[리뷰어 프로세스 정리 실패: {exc} — 잔존 프로세스 가능성 때문에 자동 재시도/"
                "폴백 금지, 사람 확인 필요. 부분 산출물 보존]"
            )
            if getattr(exc, "output", ""):
                output += f"\n{exc.output}"
            return False, ReviewerOutput(output, getattr(exc, "stderr", "") or ""), True
        # 시작 여부 불확실 — 보수적으로 started=True (상한 우회 방지 > 과잉 카운트).
        return False, ReviewerOutput(f"[리뷰어 실행 오류: {exc}]"), True


def run_reviewer(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    idle_timeout: float | None = None,
) -> tuple[bool, str]:
    """reviewer_cmd 를 stdin(=프롬프트)으로 실행한다. 반환: (성공 여부, 출력 텍스트).

    2-튜플 공개 facade — 스폰 여부(started)와 채널 분리가 필요한 내부 호출은 `_run_reviewer_ex` 를
    쓴다. 이 facade 는 사람이 읽는 표시 문자열(회신+로그)을 돌려준다."""
    ok, output, _started = _run_reviewer_ex(prompt, reviewer_cmd, timeout, run_fn, idle_timeout)
    return ok, output.combined


def reviewer_name(reviewer_cmd: str) -> str:
    """reviewer_cmd 의 공유 정규화 키(시간 프로필·진행신호·파일명/요약 공통)."""
    argv = shlex.split(reviewer_cmd)
    return _normalized_reviewer_key(argv)


def _reviewer_model(reviewer_cmd: str) -> str:
    """reviewer argv의 명시 model을 장부용으로 해소하고, 생략이면 default로 표기한다."""
    argv = shlex.split(reviewer_cmd)
    for flag in ("--model", "-m"):
        if flag in argv:
            index = argv.index(flag)
            if index + 1 < len(argv):
                return argv[index + 1]
    return "default"


# ── 결과 파싱 ─────────────────────────────────────────────────────────────


def _extract_must_fix_items(output: str) -> list[str]:
    """must-fix 섹션 헤더 이후의 항목 라인을 추출한다 (표기 편차 처리)."""
    items: list[str] = []
    in_section = False
    for line in output.splitlines():
        if not in_section:
            if _MUST_FIX_SECTION_RE.search(line):
                in_section = True
                after_header = _MUST_FIX_SECTION_RE.sub("", line, count=1).strip()
                if after_header:
                    items.append(after_header)
        else:
            stripped = line.strip()
            if stripped.startswith("**") and stripped.endswith(":"):
                break
            if stripped.startswith("**") and "**:" in stripped:
                break
            if stripped.startswith("- ") or stripped.startswith("* "):
                items.append(stripped.lstrip("-* ").strip())
            elif stripped and not stripped.startswith("#"):
                items.append(stripped)
    return items


def _is_none_items(items: list[str]) -> bool:
    """항목 목록이 "없음/N/A/none" 만으로 구성됐는지 확인한다."""
    if not items:
        return True
    return all(_NONE_ITEM_RE.match(item) for item in items if item)


def parse_verdict(output: str) -> dict[str, bool]:
    """모델 출력에서 판정(통과/반려)·must-fix 존재를 파싱한다.

    반환: {"has_must_fix": bool, "has_pass": bool}. 보수적: 판정 라인 없이 must-fix/반려
    토큰만 있어도 has_must_fix=True. 예외: must-fix 섹션이 "없음/N/A/none" 만이면 False.

    **통과 인정은 `verdict_words` 가 거른 명시 판정선에서만** 나온다. 본문 아무 곳의 '통과' 토큰을
    통과 근거로 세면 두 가지가 깨진다. (a) 코드펜스에 든 `판정: 통과`(= 검토 대상 diff 나 인용물) +
    본문 `must-fix: 없음` 조합이 파서만 통과시키고 오염 검출은 그 라인을 안 세는 **비대칭**이 생긴다.
    (b) 형식을 아예 안 지킨 산문("회귀 통과 확인. 문제 없음.")이 통과로 접힌다 — 실측된 형상이다.
    판정선이 하나도 없으면 통과가 아니라 '판정 불명확'(보수적 exit 1)이며, 그 성질은 파서 단독
    회귀가 소유한다(별도 폴백 분기 없음 — 실측상 no-op 이라 두지 않는다).
    """
    must_fix_items = _extract_must_fix_items(output)
    section_found = bool(_MUST_FIX_SECTION_RE.search(output))

    if section_found and _is_none_items(must_fix_items):
        has_must_fix = False
    elif section_found and must_fix_items:
        has_must_fix = True
    else:
        has_must_fix = any(token in output for token in _REJECT_TOKENS)

    declared_verdicts = verdict_words(output)
    kind = verdict_kind(declared_verdicts[0]) if declared_verdicts else VERDICT_UNKNOWN
    has_pass = kind == VERDICT_PASS
    if has_pass:
        if not must_fix_items or _is_none_items(must_fix_items):
            has_must_fix = False
    elif kind == VERDICT_REJECT:
        has_must_fix = True

    return {"has_must_fix": has_must_fix, "has_pass": has_pass}


# ── echo 오염 검출 (가시 범위 격리의 백스톱) ──────────────────────────────
#
# 리뷰어 CLI 로는 절대경로 읽기를 못 막으므로(위 기각 근거) 격리는 완전 차단이 아니다. 남는 구멍을
# **판정 본문의 증거**로 닫는다: 리뷰어가 옛 리뷰 raw 나 하네스 세션 전사를 읽었으면 그 파일명/경로
# 형태가 출력에 남고, 옛 판정 블록을 통째로 옮겨 오면 판정 라인이 여러 개가 된다.
# 검사 대상은 **회신 채널만**이다(`ReviewerOutput.answer`) — 진행 로그에는 프롬프트와 diff 원문이
# 그대로 실려 있어 그것까지 세면 정상 실행이 전부 오염으로 잡힌다(라이브 실측).

# 이 엔진이 만드는 raw 산출물 파일명(`_reserve_output`·pm_delegate 동형). 타임스탬프/pid 자리가
# 있어 diff 본문에서 우연히 나오지 않는다 — 인용됐다면 로컬 산출물 디렉터리를 읽었다는 증거다.
# reviewer/harness 이름에는 `_` 가 들어갈 수 있다(`external_review_<이름>_<타임스탬프>_…`) —
# 이름 문자에서 `_` 를 빼면 그런 배포의 인용을 통째로 놓친다.
_RAW_ARTIFACT_RE = re.compile(
    r"\b(?:external_review|pm_delegate)_[A-Za-z0-9._-]+_\d{4,}[A-Za-z0-9_.-]*\.txt\b"
)
# 하네스 세션 전사 저장소(실측 경로만 선언 — 추정 경로는 넣지 않는다). 구분자는 `/`·`\` 양쪽을
# 받는다 — Windows 형상(`C:\Users\u\.claude\projects\…`)을 못 보면 그 플랫폼에서 백스톱이 없다.
_SESSION_TRANSCRIPT_RE = re.compile(
    r"[\w~.:/\\-]*(?:\.claude[/\\]projects|\.codex[/\\]sessions)[/\\][\w.:@/\\-]*"
)


class OutputContamination(NamedTuple):
    """리뷰어 출력에 남은 저장소 밖 탐색 흔적."""

    verdicts: tuple[str, ...]
    raw_artifacts: tuple[str, ...]
    transcripts: tuple[str, ...]

    @property
    def verdict_conflict(self) -> bool:
        """판정 라인이 여러 개면서 서로 다른 판정을 말한다 = 어느 게 이번 판정인지 모른다.

        같은 판정의 재진술은 위험이 없어 잡지 않는다(false-red 억제). 서로 다르면 파서가 고른
        한 줄이 이번 리뷰의 판정이라는 보장이 사라지므로 보수적으로 '불명확'이어야 한다.
        """
        return len(self._verdict_kinds()) > 1

    def _verdict_kinds(self) -> set[str]:
        """판정 라인 낱말을 파서와 **같은 함수**(`verdict_kind`)로 접는다.

        허용 토큰이 아닌 판정선(`[통과`·`PASS/REJECT` 같은 템플릿/선택지 echo)은 `unknown` 이라
        고유한 종류로 센다 — 그런 줄이 진짜 판정선과 섞여 있으면 어느 게 이번 판정인지 모른다.
        """
        return {verdict_kind(word) for word in self.verdicts}

    @property
    def contaminated(self) -> bool:
        """오염 신호가 하나라도 있으면 이 판정은 리뷰어 자신의 것이라는 보장이 없다."""
        return bool(self.markers)

    @property
    def markers(self) -> tuple[str, ...]:
        """PM 이 읽을 오염 진단 — 비어 있으면 오염 신호 없음."""
        markers: list[str] = []
        if self.verdict_conflict:
            markers.append(
                f"판정 라인 {len(self.verdicts)}개가 서로 다름 "
                f"({', '.join(self.verdicts)}) — 옛 판정 블록 echo 가능"
            )
        if self.raw_artifacts:
            markers.append(
                "옛 리뷰/위임 raw 파일명 인용: " + ", ".join(self.raw_artifacts)
            )
        if self.transcripts:
            markers.append(
                "하네스 세션 전사 경로 인용: " + ", ".join(self.transcripts)
            )
        return tuple(markers)


def _unique(values: Sequence[str], limit: int = 5) -> tuple[str, ...]:
    """등장 순서를 유지한 중복 제거 + 진단 상한."""
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
        if len(seen) >= limit:
            break
    return tuple(seen)


def detect_output_contamination(output: str) -> OutputContamination:
    """리뷰어 출력에서 저장소 밖 탐색 흔적을 찾는다(판정 무관·순수 함수).

    판정 라인은 파서와 같은 `verdict_words` 로 센다 — 검출 범위가 파서보다 좁으면 파서가 집어 든
    echo 라인이 검출을 빠져나간다.
    """
    return OutputContamination(
        verdicts=verdict_words(output),
        raw_artifacts=_unique(_RAW_ARTIFACT_RE.findall(output)),
        transcripts=_unique(_SESSION_TRANSCRIPT_RE.findall(output)),
    )


# ── 결과 저장 ─────────────────────────────────────────────────────────────


def _raw_storage(output_dir: Path | None = None) -> tuple[Path, Path]:
    """외부리뷰 raw/공유 장부 위치 — 앵커는 해소된 소유 PM 홈(미해소만 tempdir 폴백).

    diff 앵커(`_main` 이 주입하는 REPO=diff_root)가 아니라 `_PM_HOME_OVERRIDE`(= 같은 실행이
    해소한 소유 PM 홈)를 쓴다. 기록이 슬롯/스냅샷 장부로 갈리면 PM 홈 장부를 읽는
    `pm_delegate raw` 통합 조회가 게이트 raw 를 영구히 못 본다. PM 홈 해소 불가 형상
    (미등록 worktree·lease 손상)에서는 `_main` 의 해소 자체가 loud 경고와 함께 diff_root
    자기 앵커로 폴백하므로 이 앵커도 그것을 따른다 — 복구 채널 자기잠김 금지.

    delegate의 raw 장부는 표현 축이 아닌 저장 경로 축이므로 포맷터 통합에서 제외하고,
    두 소비처 모두 relay.raw_storage_paths를 쓰는 현재 경계를 유지한다.
    """
    return _load_relay().raw_storage_paths(
        _PM_HOME_OVERRIDE or REPO,
        "review",
        output_dir,
        temp_dir=Path(tempfile.gettempdir()),
    )


def _reserve_output(reviewer: str, output_dir: Path | None = None) -> Path:
    """실행 전 장부가 가리킬 외부리뷰 raw를 충돌 없이 선점한다."""
    base_dir, _ledger_path = _raw_storage(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = base_dir / (
        f"external_review_{reviewer}_{ts}_{os.getpid()}_{uuid.uuid4().hex}.txt"
    )
    fd = os.open(str(dest), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    return dest


def _write_reserved_output(
    dest: Path,
    content: str,
    *,
    local_conf_path: Path | None,
    resolved_profile: str | None,
) -> None:
    with dest.open("w", encoding="utf-8") as handle:
        handle.write(_review_raw_content(content, local_conf_path, resolved_profile))


def save_output(reviewer: str, content: str, output_dir: Path | None = None, *, local_conf_path: Path | None = None, resolved_profile: str | None = None) -> Path:
    """리뷰어 출력 원문(+선택적 conf provenance 감사 헤더)을 저장하고 경로를 반환한다."""
    dest = _reserve_output(reviewer, output_dir)
    _write_reserved_output(
        dest,
        content,
        local_conf_path=local_conf_path,
        resolved_profile=resolved_profile,
    )
    return dest


# ── 실행 + 수합 ────────────────────────────────────────────────────────────


def run_review(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int | None = None,
    output_dir: Path | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    idle_timeout: float | None = None, local_conf_path: Path | None = None, resolved_profile: str | None = None,
    cwd: Path | str | None = None, env: dict[str, str] | None = None,
) -> dict:
    """외부 리뷰어를 실행하고 결과를 수합한다.

    반환 dict: reviewer / ok / output / verdict / contamination / file / failed / started /
    any_must_fix / all_pass.
    `started` = 외부 프로세스가 스폰됐는가(전송·과금 가능성) — 라운드 카운트 환불
    판정에 쓴다(False = 확실히 전송 전 실패 → 예약 환불). `idle_timeout` = 무진행 상한(None=공유
    기본) — 타임아웃 시에도 `output` 에 부분 산출물이 실려 `save_output` 이 그대로 박제한다.
    `cwd`/`env` = 리뷰어 가시 범위 격리 입력(None=미격리·종전 상속). 격리 여부는 **실제 실행 인자
    에서 도출**한다(`unisolated = cwd is None`) — 별도 플래그를 받으면 공개 facade 나 직접 호출이
    격리 없이도 '격리됨'으로 기록돼 판정 블록이 거짓말을 한다. 미격리 사실은 결과 dict 와 판정
    블록에 남는다(stderr 경고는 로그를 안 읽으면 사라지지만 판정 블록은 PM 이 반드시 읽는다).

    `contamination` = 저장소 밖 탐색 흔적 진단. **하나라도 잡히면** `all_pass` 를 내려 '판정
    불명확'(보수적 exit 1)로 만든다 — 옛 raw·전사를 읽고 쓴 판정은 그 자체로 리뷰어 자신의 판정이
    아닐 수 있어, 오염된 출력에서 통과가 나가는 false-green 을 기계가 막는다.
    """
    name = reviewer_name(reviewer_cmd)
    raw_path = _reserve_output(name, output_dir)
    _raw_dir, ledger_path = _raw_storage(output_dir)
    relay = _load_relay()
    model = _reviewer_model(reviewer_cmd)
    record_id = relay.start_raw_record(
        ledger_path,
        surface="external-review",
        harness=name,
        model=model,
        role="code-reviewer",
        raw_path=raw_path,
        attempt="primary",
    )
    metrics: dict[str, object] = {"rc": 1, "silence_sec": None}
    started_at = time.monotonic()
    ok, output, started = _run_reviewer_ex(
        prompt, reviewer_cmd, timeout, run_fn, idle_timeout, metrics,
        cwd=cwd, env=env,
    )
    elapsed = time.monotonic() - started_at
    # 판정과 오염 검출은 **회신 채널**만 본다(진행 로그는 프롬프트·diff 원문을 그대로 싣는다).
    # 경계는 구조(필드)라서 표시 문자열에 무엇이 섞여 와도 되찾을 필요가 없다.
    output = _as_reviewer_output(output)
    verdict = parse_verdict(output.answer)
    contamination = detect_output_contamination(output.answer)
    _write_reserved_output(
        raw_path,
        output.combined,
        local_conf_path=local_conf_path,
        resolved_profile=resolved_profile,
    )
    relay.finish_raw_record(
        ledger_path,
        record_id,
        rc=int(metrics["rc"]),
        elapsed_sec=elapsed,
        silence_sec=metrics.get("silence_sec"),
    )
    return {
        "reviewer": name,
        "ok": ok,
        "output": output.combined,
        "answer": output.answer,
        "log": output.log,
        "verdict": verdict,
        "contamination": contamination.markers,
        # 격리는 두 축이 **모두** 있어야 성립한다 — cwd 만 옮기고 env 를 상속하면 세션 포인터가
        # 그대로 넘어가므로 격리로 기록하면 안 된다.
        "unisolated": cwd is None or env is None,
        "file": raw_path,
        "failed": not ok,
        "started": started,
        # 오염된 출력은 통과도 반려도 아니다. 반려만 남겨 두면 옛 반려 블록 echo 가 **이번 리뷰의
        # 반려**로 기록돼(라운드 장부·PM 판단 모두) 리뷰어가 하지 않은 지적을 근거로 일이 돌아간다.
        "any_must_fix": (
            ok and verdict["has_must_fix"] and not contamination.contaminated
        ),
        "all_pass": (
            ok and verdict["has_pass"] and not verdict["has_must_fix"]
            and not contamination.contaminated
        ),
    }


# ── 결과 요약 출력 ────────────────────────────────────────────────────────


def _format_verdict(ok: bool, verdict: dict | None) -> str:
    if not ok:
        return "실패"
    if verdict is None:
        return "미실행"
    if verdict["has_must_fix"]:
        return "성공 → 반려 (must-fix 감지)"
    if verdict["has_pass"]:
        return "성공 → 통과"
    return "성공 → 판정 불명확 (PM 확인 필요)"


def _exclusion_suffix(excluded: list[str] | None) -> str:
    """종합 판정 라인에 붙일 시크릿 제외 병기 접미사.

    암묵 수집분(--ticket/기본)에서 diff 제외가 있었으면 건수·경로를 판정 라인에 남긴다 — stderr
    경고는 로그를 안 읽으면 사라지지만 판정 라인은 PM 이 반드시 본다. 제외 0건이면 빈 문자열이라
    출력은 종전과 완전 동일(기존 통과 경로 무변경)."""
    if not excluded:
        return ""
    return f" (검토 제외 {len(excluded)}건 — {', '.join(excluded)})"


def print_summary(result: dict, gate: str | None = None,
                  excluded: list[str] | None = None) -> None:
    """결과 요약을 stdout 에 출력한다.

    excluded — 시크릿 denylist 로 diff 에서 제외된 암묵 수집분 경로. 비어있지 않으면 종합 판정
    라인에 제외 건수·경로를 병기한다(false-confidence 차단). 0건이면 종전과 동일 출력."""
    sep = "=" * 60
    name = result.get("reviewer", "reviewer")
    suffix = _exclusion_suffix(excluded)
    print(sep)
    print(f"외부 코드리뷰 결과 요약 [{name}]")
    if gate:
        print(f"게이트: {gate}")
    print(sep)
    print(f"\n[{name}] {_format_verdict(result['ok'], result.get('verdict'))}")
    if result.get("file"):
        print(f"  원문: {result['file']}")
    # 오염 진단은 판정 라인과 같은 자리에 둔다 — stderr 경고는 로그를 안 읽으면 사라지지만 PM 은
    # 판정 블록을 반드시 읽는다. 판정 자체를 뒤집지는 않고(엇갈린 판정만 run_review 가 불명확 처리),
    # 이번 판정이 리뷰어 자신의 것인지 PM 이 확인할 근거를 남긴다.
    for marker in result.get("contamination") or ():
        print(f"  ⚠ 오염 의심(저장소 밖 탐색 흔적): {marker}")
    if result.get("unisolated"):
        print("  ⚠ 미격리 실행 — 리뷰어가 PM 세션 cwd 에서 옛 리뷰 raw·세션 전사를 탐색할 수 "
              f"있었습니다({UNISOLATED_REVIEWER_FLAG}).")
    print()
    if result["failed"]:
        # 실패 사유 1줄을 판정 라인에 병기 — 타임아웃 안내(`--timeout`/conf 키) 같은 실패
        # 본문이 원문 파일에만 남으면 PM 이 못 본다(판정 라인은 반드시 읽힌다).
        head = str(result.get("output") or "").strip().splitlines()
        if head:
            print(f"  사유: {head[0][:200]}")
        print(f"종합 판정: {name} 실패{suffix}")
        print("FALLBACK_INTERNAL")  # 내부 code-reviewer 서브에이전트로 폴백 신호
    elif result["any_must_fix"]:
        print(f"종합 판정: 비-통과 (must-fix 감지 — PM 검토 필요){suffix}")
    elif result["all_pass"]:
        print(f"종합 판정: 통과{suffix}")
    else:
        print(f"종합 판정: 판정 불명확 (PM 확인 필요){suffix}")
    print(sep)


# ── 종료 코드 결정 ────────────────────────────────────────────────────────


def determine_exit_code(result: dict) -> int:
    """failed→1(FALLBACK), any_must_fix→1, all_pass→0, 판정불명확→1(보수적)."""
    if result["failed"] or result["any_must_fix"]:
        return 1
    if result["all_pass"]:
        return 0
    return 1


# ── CLI ──────────────────────────────────────────────────────────────────


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI (기본 OFF)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 (HEAD 기준 변경, local.conf review_paths/기본 경로) — 활성화돼 있어야 실제 호출
  python3 .project_manager/tools/external_review.py

  # ticket 의 touches 로 경로 결정
  python3 .project_manager/tools/external_review.py --ticket T-0259

  # 특정 base 와 경로 지정
  python3 .project_manager/tools/external_review.py --base main --paths src/ tests/

  # dry-run (diff·프롬프트만 출력, 외부 호출/전송 안 함 — 비활성이어도 허용)
  python3 .project_manager/tools/external_review.py --dry-run

  # 비활성 상태에서 1회 강제 실행
  python3 .project_manager/tools/external_review.py --force

활성화: local.conf 에 `external_review_enabled=true` (+ 필요 시 `reviewer_cmd`) ·
        또는 `board.py init` / `pm_update` 시 opt-in 프롬프트.
""",
    )
    parser.add_argument("--base", default="HEAD",
                        help=("git diff 기준 ref (기본: HEAD — 작업트리"
                              "(스테이징+언스테이징), 비면 직전 커밋 HEAD~1..HEAD)"))
    parser.add_argument("--paths", nargs="+", default=None,
                        help="검토 대상 경로 (기본: local.conf review_paths / src tests scripts ...)")
    parser.add_argument("--ticket", default=None, metavar="T-NNNN",
                        help="ticket ID — touches 로 검토 경로 결정")
    parser.add_argument("--gate", default=None, metavar="T-NNNN",
                        help="게이트 ticket 표식 (로깅 + 라운드 상한 장부 키)")
    parser.add_argument("--ack-rounds", action="store_true",
                        help="라운드 상한 승인 재개 — 현 count 를 acked_through 로 기록 후 +limit "
                             "재개 (--gate 필수·사용자 승인 후에만)")
    parser.add_argument("--dry-run", action="store_true",
                        help="diff·프롬프트만 출력, 외부 호출/전송 안 함 (비활성이어도 허용·빈 diff 면 exit 1)")
    parser.add_argument("--force", action="store_true",
                        help="external_review_enabled=false 여도 1회 강제 실행 (외부 전송 발생)")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="리뷰 원문 저장 디렉토리"
                             " (기본: .project_manager/.local/review, PM 홈 미해소 시 tempdir)")
    parser.add_argument("--timeout", type=_timeout_seconds_arg, default=None,
                        metavar="SEC",
                        help="외부 호출 벽시계 백스톱(초) — 기본은 reviewer_cmd 의 하네스 프로필. "
                             "local.conf harness.<reviewer>.wall_timeout 또는 "
                             f"{EXTERNAL_TIMEOUT_KEY} 로 조정")
    parser.add_argument("--idle-timeout", type=_timeout_seconds_arg, default=None, metavar="SEC",
                        help="무진행 상한(초) — 마지막 진행 출력 이후 이 시간 침묵하면 중단(주 판정). "
                             "local.conf harness.<reviewer>.idle_timeout 또는 "
                             f"{EXTERNAL_IDLE_TIMEOUT_KEY} 로 조정")
    parser.add_argument("--adr", nargs="+", default=None, metavar="ADR-NNNN",
                        help="관련 ADR 목록 (프롬프트에 포함)")
    parser.add_argument(UNISOLATED_REVIEWER_FLAG, action="store_true",
                        help="리뷰어 가시 범위 격리에 실패해도 중단하지 않고 PM 세션 cwd/env 를 "
                             "그대로 물려준 채 1회 실행 (loud 경고 · 기본은 차단)")
    return parser


# ── local.conf 송신 프로필 provenance/divergence ───────────────────────────
# `_find_repo_root` 보유 도구 기계 inventory: contradiction_lint.py,
# external_review.py, pm_delegate.py, ticket_finish.py. 이 중 local.conf 로 외부 송신 대상을 고르는
# 표면은 external_review(reviewer_cmd)와 pm_delegate(delegate.*)뿐이다. ticket_finish는 local.conf의
# 완료/회귀 노브만 읽고, contradiction_lint는 local.conf 자체를 읽지 않아 이번 송신 분기 범위에서
# 제외한다. tests/test_conf_resolution_provenance.py가 이 전수와 소비 여부를 소스에서 다시 센다.


def _local_conf_path(repo: Path | None = None) -> Path:
    """repo별 local.conf 경로 — 호출 시점 REPO monkeypatch 를 추종하고 절대경로 provenance 입력."""
    return ((repo if repo is not None else REPO) / ".project_manager" / "local.conf").resolve()


def _read_local_config(path: Path) -> dict[str, str]:
    """존재가 확인된 비교 대상 local.conf를 KEY=value 로 읽는다."""
    conf: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


def _review_raw_content(
    content: str, local_conf_path: Path | None, resolved_profile: str | None,
) -> str:
    """pm_delegate와 같은 `# key: value` 감사 헤더를 external_review 원문에 붙인다."""
    if local_conf_path is None and resolved_profile is None:
        return content
    header = ["# external_review raw 출력 (감사)"]
    if local_conf_path is not None:
        header.append(f"# local_conf: {local_conf_path}")
    if resolved_profile is not None:
        header.append(f"# resolved_profile: {resolved_profile}")
    return "\n".join((*header, "", content))


class LocalConfDivergence(NamedTuple):
    """엔진 conf와 호출 대상 repo conf의 실제 송신 프로필 차이."""

    engine_repo: Path
    engine_conf_path: Path
    target_repo: Path
    target_conf_path: Path
    differences: tuple[str, ...]


class ReviewContentResolution(NamedTuple):
    """cross-repo 리뷰의 유효 denylist와 엔진 트리 송신 내용 분기 진단."""

    denylist: tuple[str, ...]
    divergence: LocalConfDivergence | None


class TargetLocalConfReadError(RuntimeError):
    """존재하는 비교 대상 local.conf를 안전하게 읽지 못해 송신을 중단해야 함."""

    def __init__(self, path: Path, cause: BaseException):
        self.path = path
        self.cause = cause
        super().__init__(
            f"대상 local.conf 읽기 실패 — 외부 송신 전에 중단합니다: {path} "
            f"({type(cause).__name__}: {cause})"
        )


def repo_root_from_cwd(cwd: Path | None) -> Path | None:
    """호출 cwd/`--cwd`에서 가장 가까운 제품 repo 루트를 해소한다.

    기존 판정 입력인 cwd를 그대로 쓰되, `--cwd`가 repo 하위 디렉토리여도 루트의 conf를 보도록
    `.git` + `.project_manager` 마커를 상향 탐색한다. cwd 미지정/비-repo면 None이며 divergence
    가드는 조용히 skip한다.
    """
    if cwd is None:
        return None
    resolved = cwd.resolve()
    for candidate in (resolved, *resolved.parents):
        if (candidate / ".git").exists() and (candidate / ".project_manager").is_dir():
            return candidate
    return None


def delegate_profile_config(
    conf: dict[str, str], role: str, tier: str,
) -> dict[str, str]:
    """이번 pm_delegate 실행이 해소하는 role/tier의 명시 프로필 키만 반환한다.

    다른 역할 mapping과 fallback/model_alias 등은 이번 primary tuple의 해소 입력이 아니다. 대상
    per-clone conf에 같은 키가 없다는 사실도 값 충돌이 아니므로, 존재하는 키만 반환해 공용 비교기가
    양쪽 공통 키의 실제 값 차이만 판정하게 한다.
    """
    prefix = f"delegate.{role}" + (".hard" if tier == "hard" else "")
    keys = tuple(f"{prefix}.{field}" for field in ("harness", "model", "reasoning"))
    return {
        key: conf[key].strip()
        for key in keys
        if key in conf
    }


def reviewer_profile_config(conf: dict[str, str]) -> dict[str, str]:
    """external_review의 실제 송신 대상 값. 미지정과 명시 default는 같은 값으로 정규화한다."""
    return {"reviewer_cmd": _reviewer_cmd(conf)}


def _cross_repo_target_conf(
    engine_repo: Path, target_repo: Path | None,
) -> tuple[Path, Path, dict[str, str]] | None:
    """다른 repo conf를 읽되, 진짜 부재만 무소음이고 읽기 실패는 fail-closed로 올린다."""
    if target_repo is None:
        return None
    engine_repo = engine_repo.resolve()
    target_repo = target_repo.resolve()
    if target_repo == engine_repo:
        return None
    # `_local_conf_path()`는 provenance용으로 symlink까지 resolve한다. 존재 판정은 그보다 앞선
    # directory entry 자체를 봐야 dangling symlink를 정상 부재로 오인하지 않는다. target_repo는
    # 위에서 resolve했으므로 이 경로도 절대경로지만 마지막 local.conf symlink는 따라가지 않는다.
    target_conf_path = target_repo / ".project_manager" / "local.conf"
    try:
        # lstat은 dangling symlink도 "존재"로 구분한다. FileNotFoundError만 기존 정상·무소음
        # 대상 conf 부재 축이고, 그 밖의 stat/read/UTF-8 실패는 보호 선언을 확인할 수 없으므로
        # 외부 송신 전에 fail-closed 해야 한다.
        target_conf_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TargetLocalConfReadError(target_conf_path, exc) from exc
    try:
        target_conf = _read_local_config(target_conf_path)
    except (OSError, UnicodeError) as exc:
        raise TargetLocalConfReadError(target_conf_path, exc) from exc
    return target_repo, target_conf_path, target_conf


def _normalized_review_paths(conf: dict[str, str]) -> tuple[str, ...]:
    """동일 Git 경로를 고르는 평범한 표기(`src`, `src/`, `./src`)를 비교용으로 정규화한다.

    Git pathspec magic(`:(...)`)과 루트 표기는 의미를 보존하기 위해 손대지 않는다. 실제 diff에
    전달하는 원문은 바꾸지 않고 cross-repo 경고의 동치 판정에만 쓴다.
    """
    normalized: set[str] = set()
    for item in _configured_paths(conf):
        value = item if item.startswith((":", "/")) else str(PurePosixPath(item))
        normalized.add(value)
    return tuple(sorted(normalized))


def resolve_review_content_conf(
    *,
    engine_repo: Path,
    engine_conf: dict[str, str],
    target_repo: Path | None,
    include_review_paths: bool,
    engine_label: str = "engine",
    target_label: str = "cwd",
) -> ReviewContentResolution:
    """cross-repo 리뷰의 **해소된 유효 내용 값**을 판정하고 denylist를 안전하게 합친다.

    denylist는 순서/중복이 송신 제외 집합을 바꾸지 않으므로 포함 관계로 비교한다. 대상 트리 패턴이
    실행 엔진 패턴의 부분집합이면 엔진 쪽이 같거나 더 보수적이어서 무소음이고, 대상에만 있는 패턴은
    경고하면서 양쪽 유효 denylist의 합집합을 실제 diff 추출에 적용한다. 이 합집합의 근거는 양쪽 보호
    선언을 모두 존중하는 **monotone 안전성**이지 대상 트리 diff 오배송 경로 폐쇄가 아니다.
    `extract_diff`는 항상 `git -C str(REPO)`로 엔진 트리만 추출하므로 대상 트리 diff 오배송 경로는
    애초에 없다. 합집합에 따른 추가 과차단은 명시 경로 차단·암묵 경로 제외 보고·빈 diff fail-loud
    기존 게이트가 가시화한다.

    review_paths는 포함 방향 어느 쪽도 일률적으로 안전하지 않다. 엔진 쪽이 넓으면 대상 선언보다 많은
    내용을 보내고, 좁으면 리뷰 완전성이 줄기 때문에 **유효 경로 집합 동일성**을 비교한다. 단 CLI
    `--paths`나 유효 ticket touches가 선택을 완전히 지정해 conf 값을 쓰지 않는 실행에서는 이 축을
    빼서 미사용 값 경고를 만들지 않는다. 대상 conf 부재·같은 repo·cwd 미해소는 모두 무소음이다.
    """
    engine_denylist = _denylist_patterns(engine_conf)
    loaded = _cross_repo_target_conf(engine_repo, target_repo)
    if loaded is None:
        return ReviewContentResolution(engine_denylist, None)

    resolved_target_repo, target_conf_path, target_conf = loaded
    target_denylist = _denylist_patterns(target_conf)
    # 엔진 순서를 보존해 기존 매칭/보고를 안정화하고 대상에만 있는 패턴만 뒤에 붙인다.
    effective_denylist = tuple(dict.fromkeys((*engine_denylist, *target_denylist)))
    engine_denylist_set = set(engine_denylist)
    target_only = tuple(sorted(set(target_denylist) - engine_denylist_set))
    differences: list[str] = []
    if target_only:
        differences.append(
            "review_denylist_extra: "
            f"{target_label}-only={target_only!r} (양쪽 유효 denylist 합집합 적용)"
        )

    if include_review_paths:
        engine_paths = _normalized_review_paths(engine_conf)
        target_paths = _normalized_review_paths(target_conf)
        if engine_paths != target_paths:
            differences.append(
                f"review_paths: {engine_label}={engine_paths!r}, "
                f"{target_label}={target_paths!r}"
            )

    divergence = None
    if differences:
        divergence = LocalConfDivergence(
            engine_repo=engine_repo.resolve(),
            engine_conf_path=_local_conf_path(engine_repo.resolve()),
            target_repo=resolved_target_repo,
            target_conf_path=target_conf_path,
            differences=tuple(differences),
        )
    return ReviewContentResolution(effective_denylist, divergence)


def local_conf_divergence(
    *,
    engine_repo: Path,
    engine_conf: dict[str, str],
    target_repo: Path | None,
    selector: Callable[[dict[str, str]], dict[str, object]],
    engine_label: str = "engine",
    target_label: str = "cwd",
) -> LocalConfDivergence | None:
    """두 repo가 다르고 대상 conf가 있으며 선택된 실제 값이 다를 때만 차이를 반환한다.

    값 동일·대상 conf 부재·cwd repo 미해소는 None(경고 인플레 금지). 비교는 selector가 허용한
    송신 프로필 키만 하므로 test_cmd 같은 무관 per-clone 차이는 loud 조건이 아니다.
    """
    loaded = _cross_repo_target_conf(engine_repo, target_repo)
    if loaded is None:
        return None
    target_repo, target_conf_path, target_conf = loaded
    engine_repo = engine_repo.resolve()

    engine_values = selector(engine_conf)
    target_values = selector(target_conf)
    # selector는 비교할 **유효 프로필**을 완전한 동일 키 집합으로 정규화한다. 해소 불가능한
    # 대상 프로필은 빈 dict라 skip되고, 정상 프로필의 생략 기본값(None 포함)은 공통 키로 비교된다.
    keys = sorted(set(engine_values) & set(target_values))
    differences = tuple(
        f"{key}: {engine_label}={engine_values[key]!r}, "
        f"{target_label}={target_values[key]!r}"
        for key in keys
        if engine_values[key] != target_values[key]
    )
    if not differences:
        return None
    return LocalConfDivergence(
        engine_repo=engine_repo,
        engine_conf_path=_local_conf_path(engine_repo),
        target_repo=target_repo,
        target_conf_path=target_conf_path,
        differences=differences,
    )


def format_local_conf_divergence(
    divergence: LocalConfDivergence, *, surface: str, cwd_label: str,
    resolution_note: str | None = None, source_label: str = "실행 엔진",
) -> str:
    """두 송신 표면 공용 never-block loud 경고 문구."""
    details = "\n".join(f"  · {item}" for item in divergence.differences)
    resolution = resolution_note or (
        f"이번 실행에서는 {source_label} conf가 이깁니다: {divergence.engine_conf_path}\n"
        f"  차단하지 않고 계속합니다. 같은 프로필을 원하면 {cwd_label} conf의 위 키 값을 "
        f"{source_label} "
        "conf와 맞추거나, 의도한 local.conf를 가진 엔진 사본을 실행하세요. 실행 결과 raw 헤더의 "
        "# local_conf/# resolved_profile에도 승자 provenance를 기록합니다."
    )
    return (
        f"경고: local.conf 프로필 분기 감지 ({surface}) — {cwd_label} repo와 {source_label} "
        "REPO가 다르고 외부 송신 프로필/내용 값이 실제로 갈립니다.\n"
        f"  · {source_label} REPO: {divergence.engine_repo}\n"
        f"  · {source_label} conf: {divergence.engine_conf_path}\n"
        f"  · {cwd_label} repo: {divergence.target_repo}\n"
        f"  · {cwd_label} conf: {divergence.target_conf_path}\n"
        f"{details}\n"
        f"  {resolution}"
    )


def resolved_reviewer_profile(
    reviewer_cmd: str, timeout: int | None, idle_timeout: float | None,
) -> str:
    """stderr/raw가 공유하는 external_review 해소 tuple."""
    return (
        f"(reviewer_cmd={reviewer_cmd}, wall_timeout_sec={timeout}, "
        f"idle_timeout_sec={idle_timeout})"
    )


def _main(argv: list[str] | None = None) -> int:
    global REPO, LOCAL_CONF, REVIEW_CONTEXT_FILE, TICKETS_DIR, _PM_HOME_OVERRIDE
    # main() 재호출 간 raw 앵커가 새지 않게 해소 전에 비운다(해소 전 조기 return 은 raw 를 쓰지 않는다).
    _PM_HOME_OVERRIDE = None
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    engine_repo = REPO.resolve()
    ticket_selected = bool(args.ticket and not args.paths)
    explicit_paths = bool(args.paths)
    engine_demotions: list[PmHomeDemotion] = []
    diff_owner_demotions: list[PmHomeDemotion] = []
    try:
        engine_pm_home = resolve_pm_home_for_repo(
            engine_repo, required=ticket_selected, demotion_sink=engine_demotions,
        )
        scope_from_initial_pm_home = _scope_from_initial_pm_home(
            ticket_selected=ticket_selected, explicit_paths=explicit_paths,
        )
        # **명시 앵커(--paths/--ticket) 실행은 선택 전 config를 읽지 않는다.** 이번 범위가 그
        # config에서 나오지 않아 읽을 이유가 없고, 읽는 행위 자체가(판독 불가 시) diff 대상 해소
        # *전* 중단이 되어 다른 repo를 가리키는 절대 --paths 복구 채널을 자기잠근다. 그 실행의
        # config는 아래에서 **선택된 소유자**로부터 한 번만 읽는다 — 소비처(reviewer_cmd·타임아웃·
        # denylist·opt-in·라운드 상한)는 모두 해소 뒤라 읽는 시점만 옮겨지고 값의 출처는 종전과 같다.
        # config를 읽는 조건은 아래 **분기 자신**이다(판정 헬퍼 결과가 아니라) — 범위 원천 분기와
        # 로드 조건이 따로 놀 수 없게.
        conf: dict[str, str] | None = None
        if ticket_selected:
            raw_paths = tuple(parse_ticket_touches(args.ticket, pm_home=engine_pm_home))
            if not raw_paths:
                raise AnchorResolutionError(
                    f"board anchor {engine_pm_home}: ticket {args.ticket} 의 touches가 비어 있어 "
                    "검토 범위를 확정할 수 없습니다."
                )
        elif args.paths:
            raw_paths = tuple(args.paths)
        else:
            # 인자 없는 실행 — 이 config가 **범위의 원천**이라 파생 전에 읽고 승계까지 마친다
            # (승계한 review_paths가 슬롯 선택과 diff 추출에 함께 반영돼야 표시 conf와 실제 전송
            # 범위가 갈리지 않는다). 이 실행의 선택 소유자는 정의상 최초 PM 홈이다 — 다르면 아래
            # 교차 소유 가드가 차단한다.
            conf = _conf_with_owner_filters(
                _local_config_for_repo(engine_pm_home), engine_demotions,
                explicit_paths=explicit_paths,
            )
            raw_paths = tuple(_configured_paths(conf))
        diff_root = _resolve_diff_root(
            engine_repo,
            pm_home=engine_pm_home,
            paths=raw_paths,
            base=args.base,
            ticket_selected=ticket_selected,
        )
        pm_home = (
            engine_pm_home
            if diff_root.resolve() == engine_repo
            else resolve_pm_home_for_repo(
                diff_root, required=ticket_selected,
                demotion_sink=diff_owner_demotions,
            )
        )
        if ticket_selected and pm_home != engine_pm_home:
            raise AnchorResolutionError(
                "ticket board 소유 PM 홈과 diff worktree의 lease 소유 PM 홈이 다릅니다: "
                f"board={engine_pm_home} diff-owner={pm_home}"
            )
        if scope_from_initial_pm_home and pm_home != engine_pm_home:
            raise AnchorResolutionError(
                "인자 없는 실행의 검토 범위(local.conf review_paths 또는 엔진 고정 기본 경로)를 "
                "최초 PM 홈에서 파생해 diff_root를 선택했지만, 해소된 diff 소유 PM 홈이 다릅니다. "
                "config provenance와 실제 전송 범위를 일치시킬 수 없어 외부 송신 전에 중단합니다: "
                f"initial-pm-home={engine_pm_home} diff-owner-pm-home={pm_home}\n"
                "  · review_paths 선언 유무와 무관한 같은 기준입니다 — 기본 경로도 최초 PM 홈 "
                "config를 읽어 얻은 이번 실행의 범위입니다.\n"
                "  · 절대 `--paths <경로>`로 이번 검토 범위를 직접 지정하거나, 유효한 "
                "`--ticket T-NNNN`(touches 채워진 것)으로 범위를 파생시키세요.\n"
                "  · 두 PM 홈이 같은 슬롯을 등록한 상태라면 lease 장부의 슬롯 등록을 정리하세요."
            )
        selected_paths = _normalize_review_paths(
            raw_paths,
            diff_root=diff_root,
            pm_home=pm_home,
            ticket_selected=ticket_selected,
        )
    except (AnchorResolutionError, OSError, UnicodeError) as exc:
        print(f"오류: 외부 리뷰 앵커 해소 실패 — {exc}", file=sys.stderr)
        return 1

    # 이후 기존 diff/raw/round helper는 module seam을 계속 소비한다. 한 프로세스 실행 안에서만
    # 명시 입력으로 해소된 diff 앵커를 주입하며, board/config는 별도 pm_home 경로를 유지한다.
    REPO = diff_root
    LOCAL_CONF = pm_home / ".project_manager" / "local.conf"
    # 리뷰 context overlay는 선택된 코드 worktree가 아니라 board/config 소유 PM 인스턴스 입력이다.
    # diff_root에 빈/구 사본이 있어도 해소된 local.conf와 같은 소유 경계에서 읽도록 의도적으로 둔다.
    REVIEW_CONTEXT_FILE = pm_home / ".project_manager" / "review_context.local.md"
    TICKETS_DIR = pm_home / ".project_manager" / "wiki" / "tickets"
    # raw 산출/공유 장부도 board/config 와 같은 소유 경계에 등재한다 — 슬롯(diff_root) 장부에
    # 박제하면 PM 홈 장부를 읽는 `pm_delegate raw` 통합 조회가 이 실행의 raw 를 못 본다.
    # 해소 실패 형상에서는 pm_home 자체가 loud 경고 뒤 diff_root 로 폴백해 있다.
    _PM_HOME_OVERRIDE = pm_home
    if conf is None:
        # 명시 앵커 실행의 config는 여기서 **처음이자 유일하게** 읽고, 강등 필터 검사도 여기서
        # 한다 — 대상은 이번 실행이 실제로 쓰는 conf 소유자뿐이다. 초기 엔진 컨텍스트가 강등·손상
        # 이어도 diff 대상이 다른 소유자로 확정됐으면 그 손상은 이 송신과 무관하다(절대 --paths
        # 복구 채널 보존). 인자 없는 실행은 범위 파생 지점에서 이미 읽고 검사했고, 그 실행의 선택
        # 소유자는 정의상 최초 PM 홈이다(다르면 교차 소유 가드가 이미 차단).
        selected_demotions = (
            engine_demotions if pm_home == engine_pm_home else diff_owner_demotions
        )
        try:
            conf = _conf_with_owner_filters(
                _local_config_for_repo(pm_home), selected_demotions,
                explicit_paths=explicit_paths,
            )
        except (OSError, UnicodeError) as exc:
            conf_path = _local_conf_path(pm_home)
            print(
                "오류: 해소된 local.conf 읽기 실패 — 외부 송신 전에 중단합니다: "
                f"{conf_path} ({type(exc).__name__}: {exc})",
                file=sys.stderr,
            )
            return 1
        except AnchorResolutionError as exc:
            print(f"오류: 외부 리뷰 앵커 해소 실패 — {exc}", file=sys.stderr)
            return 1
    # 시간 예산은 **리뷰어 커맨드의 하네스 프로필**을 따른다 — reviewer_cmd 를 먼저 해소해야
    # 어떤 축(클라우드/로컬)의 값을 쓸지 정해진다(기본 `codex exec` → codex 축). 깨진 conf의
    # fail-soft 경고는 해소 중 발생하지만 stderr 첫 줄 provenance 계약을 지키도록 잠시 보류한다.
    reviewer_cmd = _reviewer_cmd(conf)
    resolution_warnings = io.StringIO()
    with contextlib.redirect_stderr(resolution_warnings):
        resolved_time_profile = reviewer_profile(reviewer_cmd, conf)
    timeout = (
        args.timeout
        if args.timeout is not None
        else int(resolved_time_profile.wall_timeout)
    )
    idle_timeout = (
        float(args.idle_timeout)
        if args.idle_timeout is not None
        else float(resolved_time_profile.idle_timeout)
    )
    conf_path = _local_conf_path(pm_home)
    profile = resolved_reviewer_profile(reviewer_cmd, timeout, idle_timeout)

    target_repo = diff_root
    # 정상 실행·dry-run 공용 첫 provenance. 이후 cap/reanchor/diff 진단이 붙더라도 어느 PM-home conf와
    # reviewer tuple을 해소했는지가 stderr에 남는다.
    print(
        f"[external-review] config provenance: local_conf={conf_path} "
        f"· diff_root={diff_root} · pm_home={pm_home} · resolved_profile={profile}",
        file=sys.stderr,
    )
    deferred_warnings = resolution_warnings.getvalue()
    if deferred_warnings:
        print(deferred_warnings, end="", file=sys.stderr)
    try:
        divergence = local_conf_divergence(
            engine_repo=pm_home,
            engine_conf=conf,
            target_repo=target_repo,
            selector=reviewer_profile_config,
            engine_label="pm-home",
            target_label="diff-worktree",
        )
    except TargetLocalConfReadError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if divergence is not None:
        print(
            format_local_conf_divergence(
                divergence,
                surface="external_review",
                cwd_label="diff worktree",
                source_label="해소된 PM 홈",
                resolution_note=(
                    "이번 실행에서는 --paths/--ticket에서 해소된 PM 홈 conf가 적용됩니다: "
                    f"{divergence.engine_conf_path}\n"
                    "  차단하지 않고 계속합니다. 같은 프로필을 원하면 diff worktree conf의 위 "
                    "키 값을 PM 홈 conf와 맞추세요. 다른 PM 홈 conf를 선택하려면 --paths/--ticket이 "
                    "가리키는 등록 worktree와 lease 소유 관계를 확인하세요."
                ),
            ),
            file=sys.stderr,
        )
    cap_warning = harness_cap_advisory(
        execution_budget=_reviewer_execution_budget(reviewer_cmd, timeout)
    )
    if cap_warning is not None:
        print(cap_warning, file=sys.stderr)

    output_dir: Path | None = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    # 슬롯 선택과 diff 추출은 위에서 확정한 동일 path 집합을 쓴다.
    paths = list(selected_paths)
    include_conf_review_paths = not args.paths and not ticket_selected

    try:
        content_resolution = resolve_review_content_conf(
            engine_repo=pm_home,
            engine_conf=conf,
            target_repo=target_repo,
            include_review_paths=include_conf_review_paths,
            engine_label="pm-home",
            target_label="diff-worktree",
        )
    except TargetLocalConfReadError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if content_resolution.divergence is not None:
        review_paths_note = (
            "review_paths의 이번 범위는 해소된 PM 홈 conf가 정했습니다"
            if include_conf_review_paths
            else "review_paths는 --paths/유효 ticket touches 완전지정으로 conf 비교에서 제외했습니다"
        )
        print(
            format_local_conf_divergence(
                content_resolution.divergence,
                surface="external_review 송신 내용",
                cwd_label="diff worktree",
                source_label="해소된 PM 홈",
                resolution_note=(
                    "denylist는 양쪽 해소값의 합집합을 적용하고 "
                    f"{review_paths_note}: {_local_conf_path(pm_home)}\n"
                    "  차단하지 않고 계속합니다. 대상 전용 denylist도 이번 diff 제외에 적용되며, "
                    "review_paths 범위를 직접 정하려면 --paths/유효 ticket touches로 이번 범위를 "
                    "완전히 지정하세요."
                ),
            ),
            file=sys.stderr,
        )

    print(f"검토 경로: {paths}", file=sys.stderr)
    print(f"base: {args.base}", file=sys.stderr)

    # diff 추출 (시크릿 denylist 자동 제외 — 제외 경로 목록도 반환)
    denylist = content_resolution.denylist
    try:
        diff, excluded = extract_diff(args.base, paths, denylist=denylist)
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # 시크릿 denylist 제외 보고. 제외된 경로가 판정에 전혀
    # 안 남으면, 지정분이 조용히 빠진 채 '통과'가 나 게이트가 실제보다 넓게 검증한 것처럼 보인다.
    # 명시(--paths)와 암묵(--ticket/기본) 지정을 구분한다: 명시 지정분이 제외되면 차단(exit 1)하고
    # 어느 경로가 왜 빠졌는지 알린다 — 빈-diff 가드보다 앞서 두어, 단일 파일 전량 제외로 diff 가
    # 비어도 '변경 없음'이 아니라 denylist 가 원인임을 정확히 알린다. 암묵 수집분은 차단하지 않고
    # stderr 경고 + 종합 판정 라인 병기(아래 print_summary)로 남긴다. 제외 0건이면 no-op(종전 무변경).
    if excluded:
        if args.paths:  # 명시 지정 → 차단 (우회는 그 경로를 빼고 재실행·새 플래그 없음)
            print(_format_explicit_exclusion_block(excluded, denylist), file=sys.stderr)
            return 1
        for path in excluded:  # 암묵 수집 → 비차단·stderr 경고 (판정 병기는 print_summary)
            pattern = _matching_denylist_pattern(path, denylist)
            print(f"경고: 시크릿 denylist 경로 '{path}' 를 diff 에서 제외했습니다 "
                  f"(패턴 '{pattern}' 매칭).", file=sys.stderr)

    # 빈-diff fail-loud 가드 (diff 추출 직후·codex invoke 전). 빈 diff(공백-only 포함)를
    # 리뷰하면 가짜 통과(false-green)가 나므로 어떤 형상·모드에서도 무조건 fail 한다 — 우회
    # 플래그 없음. 기존 오류 규약(비-0 = 1)과 정합. dry-run/비활성 no-op 보다 앞서므로 잘못된
    # 형상(worktree 아닌 곳에서 실행 등)은 codex 전송 없이 미리보기 단계에서도 드러난다.
    if not diff.strip():
        print(_empty_diff_guidance(paths, root=diff_root), file=sys.stderr)
        return 1

    prompt = build_prompt(diff=diff, adr_refs=args.adr, gate=args.gate)

    if args.dry_run:
        print("=== [dry-run] 프롬프트 미리보기 (외부 전송 없음) ===")
        print(prompt)
        print("=== [dry-run] 외부 호출 생략 ===")
        return 0

    # 활성화 게이트 (외부 전송이므로 기본 OFF)
    if not _is_enabled(conf) and not args.force:
        print(
            "외부 리뷰 비활성 — 코드 diff 외부 전송이 꺼져 있습니다 "
            "(local.conf external_review_enabled=false).\n"
            "켜기: local.conf 에 `external_review_enabled=true` 추가, 또는 "
            "`board.py init` / `pm_update` 시 opt-in 프롬프트. "
            "미리보기는 `--dry-run`, 1회 강제는 `--force`.",
            file=sys.stderr,
        )
        return 0  # no-op — 실패 아님

    # ── 리뷰어 가시 범위 격리 ──────────
    # 라운드 예약보다 **먼저** 만든다 — 격리 실패로 중단하는 실행은 외부 전송이 없으므로 라운드를
    # 소비하지 않아야 하고(환불 경로를 새로 만들 이유가 없다), 리뷰어는 이 거울 안에서만 돈다.
    isolation = contextlib.ExitStack()
    try:
        workspace = isolation.enter_context(reviewer_visibility_scope(
            diff_root, allow_unisolated=args.allow_unisolated_reviewer, conf=conf,
            # 거울과 프롬프트가 **같은 해소값**으로 시크릿을 제외한다(폭이 갈리면 제외가 무의미).
            denylist=content_resolution.denylist,
        ))
    except ReviewerWorkspaceError as exc:
        isolation.close()
        print(_UNISOLATED_GUIDANCE.format(reason=exc), file=sys.stderr)
        return 1
    # 생성 이후 **모든 경로**(라운드 상한 거부·예외 포함)가 이 finally 를 지나 정리된다.
    try:
        return _run_isolated_review(
            args, workspace, conf=conf, prompt=prompt, reviewer_cmd=reviewer_cmd,
            timeout=timeout, idle_timeout=idle_timeout, output_dir=output_dir,
            conf_path=conf_path, profile=profile, excluded=excluded,
        )
    finally:
        isolation.close()


def _run_isolated_review(
    args, workspace, *, conf, prompt, reviewer_cmd, timeout, idle_timeout,
    output_dir, conf_path, profile, excluded,
) -> int:
    """격리 컨테이너 수명 **안**에서 도는 구간 — 라운드 상한 예약·리뷰 실행·장부 마감.

    `_main` 에서 떼어낸 이유는 수명 관리 하나다: 여기서 어떤 경로로 빠져나가도(조기 return·예외)
    호출부의 단일 finally 가 컨테이너를 지운다. 거울에는 저장소 사본과 인증 파일 사본이 있어
    잔존이 곧 누출이다.
    """
    workspace_tree = workspace.tree if workspace is not None else None
    extra_keep = reviewer_env_keep_extra(conf)
    reviewer_environment = reviewer_env(
        workspace_tree,
        workspace.home if workspace is not None else None,
        extra_keep=extra_keep,
    )
    applied_extra = tuple(
        name for name in extra_keep
        if name in {key.upper() for key in (reviewer_environment or {})}
    )
    if workspace is not None:
        print(
            f"[external-review] 리뷰어 가시 범위: cwd={workspace.tree} "
            f"· HOME={workspace.home} "
            f"(tracked {workspace.files}개 거울 · git 저장소="
            f"{'예' if workspace.git_repo else '아니오'}"
            + (f" · 홈 인증/설정 {len(workspace.copied_home_artifacts)}개 복제"
               if workspace.copied_home_artifacts else " · 홈 인증/설정 복제 0개(부재)")
            + (f" · 홈 정화 실패 {len(workspace.home_scrub_failed)}개 미복제"
               f"({', '.join(workspace.home_scrub_failed)}) — 설정이 빠져 **다른 모델**로 동작할 수 "
               "있습니다" if workspace.home_scrub_failed else "")
            + (f" · 격리 밖 참조 {workspace.skipped_unsafe}개 제외"
               if workspace.skipped_unsafe else "")
            + (f" · 시크릿 denylist {workspace.skipped_secret}개 제외"
               if workspace.skipped_secret else "")
            + (f" · {_REVIEWER_ENV_KEEP_EXTRA_KEY} 통과: {', '.join(applied_extra)}"
               if applied_extra else "")
            + ")",
            file=sys.stderr,
        )

    # ── 라운드 상한 게이트: 호출 전 예약 ──────────
    # 여기까지 왔으면 dry-run·빈-diff·비활성 no-op 을 모두 통과해 *실 외부 전송*이 일어난다 —
    # 그것들은 전송이 없어 라운드가 아니므로(카운트 제외) 이 앞의 조기 return 뒤에 게이트를 둔다.
    # `--gate` 지정 시에만 per-gate 장부를 대조한다("--gate 미지정 실행은 상한 대상 밖").
    #
    # MF-A(예약-후-환불): count 를 *호출 전에* +1 예약한다 — 타임아웃·비정상 종료도 프롬프트가 이미
    # 전송·과금됐을 수 있는데 성공시에만 세면 반복 타임아웃으로 상한을 무한 우회한다. 외부 프로세스가
    # 확실히 시작되지 않은 경우(스폰 실패·started=False)만 아래에서 환불한다. MF-B(원자성): 확인→예약
    # →저장을 `_round_ledger_lock()` 한 임계 구역으로 묶어 동시 실행이 같은 잔여 슬롯을 통과 못 하게 한다.
    # --ack-rounds 는 acked_through 를 현 count 로 올려(엔진은 기록만·승인 판단은 사용자/카드) +limit
    # 창을 열고 그 호출도 실 전송이므로 함께 예약한다. 초과면 리뷰어 호출 전에 거부(전용 rc·과금 없음).
    reserved_gate: str | None = None
    reserved_round_id: str | None = None
    if args.gate:
        limit = _round_limit(conf)
        incomplete_limit = _incomplete_round_limit(conf)
        try:
            with _round_ledger_lock():
                ledger = _load_round_ledger()
                # 앵커 이동 1회 승계 — legacy(diff 앵커) 장부의 차단 상태를 그대로 이관한다.
                # 승계분은 판정 *이전에* 저장한다: 차단으로 조기 return 해도 마이그레이션은
                # 남아야 다음 실행이 다시 승계하지 않는다(게이트당 1회).
                if _inherit_legacy_round_entry(ledger, args.gate) is not None:
                    _, legacy_verdicts, legacy_incomplete = _unacked_round_counts(
                        ledger[args.gate]
                    )
                    print(
                        f"legacy 라운드 장부 승계: {_legacy_round_ledger_path()} → "
                        f"{_round_ledger_path()} · gate={args.gate} "
                        f"verdicts={legacy_verdicts} incomplete={legacy_incomplete}",
                        file=sys.stderr,
                    )
                    _save_round_ledger(ledger)
                entry = _gate_entry(ledger, args.gate)
                count, acked = entry["count"], entry["acked_through"]
                if args.ack_rounds:
                    entry["acked_through"] = count      # 승인 수위 상향 (+limit 창)
                    entry["records"] = []               # 승인된 상세 레코드는 집계에서 영구 불필요
                    print(f"라운드 상한 승인 재개: 게이트 {args.gate} — acked_through={count} "
                          f"(+{limit}라운드).", file=sys.stderr)
                else:
                    unacked, verdicts, incomplete = _unacked_round_counts(entry)
                if not args.ack_rounds and (
                    verdicts >= limit or incomplete >= incomplete_limit
                ):
                    print(_ROUND_LIMIT_GUIDANCE.format(
                        gate=args.gate, unacked=unacked, verdicts=verdicts,
                        incomplete=incomplete, limit=limit,
                        incomplete_limit=incomplete_limit,
                        ledger=_round_ledger_path(),
                        count=count, acked=acked), file=sys.stderr)
                    # 거울 정리는 호출부의 단일 finally 가 한다(전송 없이 되돌아감).
                    return EXIT_ROUND_LIMIT_EXCEEDED    # 예약 없음 (전송 전 거부)
                reserved_round_id = uuid.uuid4().hex
                _reserve_round(entry, reserved_round_id)  # 호출 전 라운드 예약
                _save_round_ledger(ledger)
                reserved_gate = args.gate
        except OSError as exc:
            # 락 획득/장부 write 실패 — 상한을 확인하지 못한 채 전송하면 과금 게이트가 무력화되므로
            # **전송 전에** 멈춘다(과금 0). 가장 흔한 원인은 동시 실행의 락 보유다(Windows
            # `msvcrt.locking` 은 재시도 소진 시 OSError·POSIX 는 블로킹이라 여기 안 온다).
            print(
                f"오류: 라운드 장부 임계 구역 진입 실패 ({type(exc).__name__}: {exc}) — 다른 "
                "게이트 실행이 장부 락을 보유 중일 수 있습니다. 잠시 후 다시 실행하세요 "
                f"(장부: {_round_ledger_path()}).",
                file=sys.stderr,
            )
            return 1
    elif args.ack_rounds:
        print("경고: --ack-rounds 는 --gate 와 함께 써야 합니다 (게이트 단위 장부) — 무시.",
              file=sys.stderr)

    print(f"외부 리뷰어 실행 중: {reviewer_cmd}", file=sys.stderr)
    # 리뷰어는 PM 세션의 cwd/env/홈을 물려받지 않는다 — 거울과 임시 홈, allowlist env 만 받는다.
    result = run_review(
        prompt=prompt, reviewer_cmd=reviewer_cmd,
        timeout=timeout, output_dir=output_dir, idle_timeout=idle_timeout,
        local_conf_path=conf_path, resolved_profile=profile,
        cwd=workspace_tree,
        env=reviewer_environment,
    )
    print_summary(result, gate=args.gate, excluded=excluded)
    if result.get("failed") and workspace is not None:
        # 격리 임시 홈에서 로그인 파일이 빠졌을 때의 증상이 '리뷰어 실패'라, 원인 분리 채널을 준다.
        print(
            "[external-review] 힌트: 격리(임시 홈·allowlist env)에서 인증 입력이 빠져 실패했을 수 "
            f"있습니다 — 홈 인증/설정 복제 {len(workspace.copied_home_artifacts)}개("
            f"{', '.join(workspace.copied_home_artifacts) or '없음'}) · "
            f"{_REVIEWER_ENV_KEEP_EXTRA_KEY} 통과 {len(applied_extra)}개("
            f"{', '.join(applied_extra) or '없음'}).\n"
            + (f"  · 정화 실패로 빠진 설정: {', '.join(workspace.home_scrub_failed)} — 그 파일의 "
               "경로 선언(hooks·mcp_servers·plugins·projects 류)을 정리하거나 필요한 키만 남기세요.\n"
               if workspace.home_scrub_failed else "")
            + f"  · 인증 파일 추가: local.conf `{_REVIEWER_HOME_EXTRA_KEY}=<홈 상대경로 …>`\n"
            f"  · 인증 환경변수 추가: local.conf `{_REVIEWER_ENV_KEEP_EXTRA_KEY}=<이름 …>`\n"
            f"  · 원인 분리: `{UNISOLATED_REVIEWER_FLAG}` 실행과 비교(격리 탓인지 확정).",
            file=sys.stderr,
        )

    # 정상 복귀한 호출은 판정 유무와 별개로 종료 마감한다. 프로세스 kill처럼 이 지점에 도달하지
    # 못한 레코드는 finished_at 없이 남아 다음 호출에서 미완 재시도 예산으로 식별된다.
    if reserved_gate is not None and reserved_round_id is not None:
        try:
            with _round_ledger_lock():
                ledger = _load_round_ledger()
                entry = _gate_entry(ledger, reserved_gate)
                matching = next(
                    (row for row in entry["records"] if row.get("id") == reserved_round_id),
                    None,
                )
                if not result.get("started", True):
                    _refund_round(entry, reserved_round_id)
                elif matching is not None:
                    matching["finished_at"] = datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat()
                    matching["verdict"] = _round_has_verdict(result)
                _save_round_ledger(ledger)
        except OSError as exc:
            # 마감은 이미 *끝난* 전송의 부기다 — 여기서 rc 를 바꾸면 리뷰 판정이 락 사정으로
            # 뒤집힌다. loud 경고만 남기고 판정 종료코드를 그대로 돌려준다. 마감 못 한 레코드는
            # finished_at 없이 남아 다음 실행의 미완 재시도 예산으로 보수적으로 집계된다.
            print(
                f"경고: 라운드 장부 마감 실패 ({type(exc).__name__}: {exc}) — 다른 게이트 "
                "실행이 장부 락을 보유 중일 수 있습니다. 이 라운드는 미완으로 남아 다음 실행의 "
                f"재시도 예산에서 세어집니다 (장부: {_round_ledger_path()}).",
                file=sys.stderr,
            )

    return determine_exit_code(result)


def main(argv: list[str] | None = None) -> int:
    """한 호출 동안만 selector 해소 전역을 주입하고 재진입 전에 원복한다."""
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    global REPO, LOCAL_CONF, REVIEW_CONTEXT_FILE, TICKETS_DIR, _PM_HOME_OVERRIDE
    original = (
        REPO, LOCAL_CONF, REVIEW_CONTEXT_FILE, TICKETS_DIR, _PM_HOME_OVERRIDE,
    )
    try:
        return _main(argv)
    finally:
        (
            REPO, LOCAL_CONF, REVIEW_CONTEXT_FILE, TICKETS_DIR,
            _PM_HOME_OVERRIDE,
        ) = original


if __name__ == "__main__":
    sys.exit(main())
