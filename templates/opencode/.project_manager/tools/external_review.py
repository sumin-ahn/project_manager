#!/usr/bin/env python3
"""추가 리뷰어 래퍼 — 추가 리뷰어(외부 하네스) 어댑터 CLI.

사람 역할 이름은 **추가 리뷰어(additional reviewer)** 다 — 팀에 한 명 더 붙는 리뷰어다.
`external` 은 전송/격리/과금 축(코드가 저장소 밖으로 나간다)과 기계 식별자(모듈 파일 이름·
raw 파일 접두)에만 남는다. 설정 키는 `additional_reviewer_enabled`/`additional_reviewer.*` 다.

명칭 이력: 이 모듈의 파일 이름은 개칭 전 이름 `external_review.py` 그대로다(T-0597 판단) —
파일명을 바꾸면 동기가 상류 부재 파일을 지우지 않으므로 채택자 PM 홈에 구 사본이 남아 두 진입점이
공존하고, 이미 기록된 raw 감사물(`external_review_*.txt`)의 접두와도 어긋난다.

사용:
    python3 .project_manager/tools/external_review.py [옵션]

동작:
  git diff <base> -- <paths> 추출 (시크릿 denylist 경로 자동 제외)
  (프로젝트 맥락 헤더 +) diff 결합 → 표준 프롬프트 생성
  추가 리뷰어 실행 (additional_reviewer.* 원자 tuple · 읽기 권위 code-reviewer 고정)
  출력에서 판정(통과/반려)·must-fix 파싱
  결과 요약 stdout + 원문 파일 저장 (기본 = **소유 PM 홈**의 `.project_manager/.local/review/`
    + 공유 장부 `raw_outputs.json` · --output-dir 로 격리)

기본 비활성:
  - 코드 diff 가 *외부로 전송*되므로 기본 OFF. local.conf `additional_reviewer_enabled=true`
    또는 `board.py init` / `pm_update` 시 opt-in 으로 켠다. 비활성 시 actual 호출은
    no-op(exit 0)이고 `--dry-run` 은 항상 허용(로컬 미리보기·미전송), `--force` 로 1회 강제.
    단 빈/공백 diff 는 dry-run·비활성 포함 무조건 exit 1 (false-green 원천 차단).

종료 코드/신호:
  - 리뷰어 실패(인증/한도/네트워크/타임아웃·구조화 wire 에서 최종 회신 미추출) → exit 1 +
    stdout 에 FALLBACK_INTERNAL (= 내부 code-reviewer 서브에이전트로 폴백하라는 신호).
    회신을 못 뽑은 실행은 rc=0 으로 끝났어도 **리뷰를 받지 못한 실행**이라 같은 축으로 내린다 —
    wire 원문은 원문 파일에 그대로 보존되고 raw 장부는 정상 마감된다.
    중단 판정: 주 = **무진행**(마지막 진행 출력 이후 침묵), 백스톱 = 벽시계. 값은 별도 상수가
    아니라 **해소된 추가 리뷰어 대상의 하네스 프로필**(pm_relay·위임 채널과 동일 테이블)에서 오고, 배포별
    조정은 local.conf `harness.<reviewer>.idle_timeout`/`.wall_timeout`(legacy
    `external_review_idle_timeout`/`external_review_timeout` 도 계속 유효)·일회성은
    `--idle-timeout`/`--timeout`. 어느 쪽으로 중단되든 그 시점까지 받은 출력은 원문 파일에
    보존한다(전량 폐기 금지).
  - must-fix 감지 → exit 1
  - 통과 → exit 0
  - 수렴-형상 차단(--gate 별) → exit 4 (실행 전 거부). 라운드 장부의 must_fix 추이로 판정한다:
    라운드 수가 상한(local.conf review_rounds_max·기본 2)에 닿았거나 직전 라운드보다 must_fix 가
    늘었으면(발산·조기 차단) 라운드를 더 쓰지 않는다. 출구는 **재설계·티켓 분할**이고, 직전 지적
    해소만 확인하려면 게이트당 1회 `--confirm-fix`(확인 전용 라운드)를 쓴다.
  - diff 서킷브레이커 → exit 1 (리뷰어 호출 전 거부). 티켓 estimate 별 diff 총량 상한
    (small 300 / medium 1,000 / large 2,500 · local.conf diff_cap.<estimate>)을 넘긴 스코프는
    리뷰 라운드로 닫히지 않으므로 분할·재설계로 보낸다.
  - 라운드 상한 도달(--gate 별) → exit 4 (실행 전 거부·전용 rc). 같은 게이트로
    판정 4회(local.conf additional_reviewer_round_limit) 또는 미완 2회
    (additional_reviewer_incomplete_round_limit)를 채우면 이후 실행을 기계 차단한다. 성격은
    **무한 루프 차단(anti-loop pause)**이다 — 연장 승인(`--ack-rounds`)은 폐지됐고, 호출하면
    아무것도 하지 않고 거부한다.
  - 게이트 스냅샷 또는 미등록 linked worktree 자기 앵커 + 게이트 라운드 → exit 1 (실행 전
    거부). 격리 스냅샷 안 `.local/review_rounds.json` 은 스냅샷 재생성과 함께 사라지므로,
    스냅샷 마커가 있거나 PM 홈을 해소하지 못한 linked worktree에서는 실 전송 라운드를
    기록하지 않는다. dry-run·조회·처분과 명시 `--no-gate` 자문은 이 차단 대상이 아니다.
  - wave 예산 소진 → exit 4 (같은 rc·같은 실행 전 거부). 게이트별 상한과 **별개로** wave 단위
    총 라운드 예산(local.conf additional_reviewer_wave_budget·기본 24)을 두어 티켓 수 × 라운드 상한
    으로 비용이 무한 확장되는 구조를 막는다 — 재개는 같은 규율의 `--ack-wave`(예산 리셋).

설계:
  - 어댑터 seam: canonical `additional_reviewer.{harness,model,reasoning}` tuple 을 pm_relay 의
    3-harness 드라이버로 해소한다. tuple 이 전혀 없을 때만 임의 `reviewer_cmd`/기본 command 를
    legacy `unpinned-model` 경로로 보존한다.
  - 도메인 외부화: 프로젝트 맥락은 `.project_manager/review_context.local.md`(인스턴스 소유)
    가 있으면 주입, 없으면 generic 헤더. 엔진 도구엔 도메인 콘텐츠 0.
  - subprocess DI (run_fn 매개변수) — 테스트에서 mock 주입 가능.
  - 구조화 외부 호출의 권한은 code-reviewer read 축으로 고정하며 설정으로 쓰기 권한을 올릴 수 없다.
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
  - 라운드 상한 기계 차단: 추가 리뷰어 호출(과금·전송)이 무한 반복되지 않게 `--gate <T-NNNN>`
    별 라운드 장부(`.project_manager/.local/review_rounds.json`·per-clone·git-ignored)에 실 전송을
    count 하고, limit(기본 4)회를 넘기면 실행 *전에* 거부(exit 4)한다. "몇 라운드나 돌았나"라는
    PM 자의 집계를 기계 장부로 대체하는 것이고([[mechanize-dont-instruct-llm]]), 수렴 여부 판정도
    같은 장부의 must_fix 추이가 소유한다(사람의 "이번엔 진짜 수렴 중" 판단을 입력으로 쓰지 않는다).
    게이트를 붙이는 것도 기억에 맡기지 않는다 — `--gate` 미지정 `--ticket` 실행은 게이트를 그
    티켓으로 **자동 유도**하고(명시 `--gate` 가 항상 우선), 회계 밖 자문 실행은 명시
    opt-out `--no-gate` 로만 연다(그 실행은 무기록·비회계를 loud 로 표기한다).
  - 라운드별 산출 장부 + wave 예산: 같은 장부에 라운드마다 산출(`rounds` — 판정 rc·must-fix 수)을
    append 하고, 전 게이트 합계 전송을 wave 단위로 센다(`wave` 절). 라운드 수만으로는 "그 라운드가
    실결함을 냈는가"를 기계로 확인할 수 없어 게이트 심도 대비 비용 적정성 판단이 PM 자기보고에
    의존했다. 조회는 `--rounds-report`(외부 전송 없는 읽기 전용 표). 기록은 무조건이고 hard 거부는
    예산 축 하나뿐이다.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import hashlib
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
# baked 리터럴 — engine_rev.py --bump가 전 stamped 모듈과 함께 재작성한다.
ENGINE_REV = "v1.7.6"


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


# 사본 불일치를 **의도적으로 흡수**하는 경계의 등록부 (경계 이름 → 사유). 등록되지 않은 경계는
# 흡수 자격이 없다 — 기본 규율은 여전히 "marked skew 는 재-raise" 다.
_ENGINE_REV_SKEW_RECOVERY_REASONS = {
    "abort_pre_spawn_raw": (
        "스폰 전 중단의 raw 정리는 **주 예외를 덮지 않는 것**이 계약이다 — 정리 중 사본 불일치로 "
        "갈아타면 이 실행이 왜 죽었는지가 사라지고 라운드 환불 판정의 근거도 함께 사라진다. "
        "불일치는 경고 한 줄로 남기고 중단 사유(주 예외)를 그대로 전파한다"
    ),
    "shared_read_seam": (
        "판독은 형제 없이도 떠야 한다 — 이 모듈의 진단·denylist·재앵커 경로는 seam 이 필요한 "
        "`--gate` 구간 밖이고(로더 주석의 기능 축), pm_delegate 가 그 경로를 deep-import 로 "
        "재사용한다. seam 부재로 판독이 죽으면 부분 동기 트리에서 진단 자체가 사라진다. "
        "부재/손상/혼합 사본을 흡수하되 조용하지 않게 사유를 stderr 로 남기고 종전 읽기로 "
        "진행한다(잃는 것은 Windows 에서 이 판독 중의 원자 교체 한 번)"
    ),
    "ticket_harvest": (
        "회수는 이미 **끝나고 과금된** 라운드의 부기다 — 여기서 사본 불일치로 traceback 을 내면 "
        "리뷰 판정과 요약이 이미 출력된 뒤 실행이 죽어, 채택자에게는 '리뷰가 실패했다'로만 보인다. "
        "설계된 회수 실패 처방(재동기 안내 + rc≠0 + raw 경로)으로 접고 산출은 raw 에 남긴다"
    ),
    "partial_container_cleanup": (
        "구성 실패로 남은 부분 격리 컨테이너 정리는 **원래 실패를 덮지 않는 것**이 계약이다 — "
        "여기서 갈아타면 격리가 왜 실패했는지가 사라진다. 삭제 수단이 형제 모듈이라 사본 불일치도 "
        "이 경계에 닿는데, 그것도 정리 실패의 한 형태로 흡수하되 잔존 경로 안내와 함께 원인을 "
        "문구로 구분해 남긴다"
    ),
}


def _absorb_engine_rev_skew_for_recovery(exc, boundary: str) -> bool:
    """정리 경계가 marked skew 를 의도적으로 흡수했음을 표시한다 (사유 등록 필수).

    반환값으로 일반 실패와 사본 불일치를 구분해 호출부가 진단 문구를 달리한다 — 흡수는 하되
    조용하지는 않다."""
    reason = _ENGINE_REV_SKEW_RECOVERY_REASONS.get(boundary, "").strip()
    if not reason:
        raise ValueError(f"등록되지 않았거나 사유가 빈 복구 경계: {boundary!r}")
    return _is_engine_rev_skew(exc)


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


class PmHomeResolutionFacts(NamedTuple):
    """한 번의 PM-home 해소가 가드에 넘기는 자기-앵커 판정 사실.

    라운드 가드는 이 값을 통해서만 마커/git/lease 판정을 소비한다. 따라서 테스트나 채택자가
    `resolve_pm_home_for_repo` seam을 고정하면 가드도 같은 해소 경계를 따르고, 호출 뒤 실제
    checkout 형상을 독자적으로 재조회하지 않는다.
    """

    anchor: Path
    pm_home: Path
    snapshot_marker: Path | None
    unregistered_linked_self_anchor: bool


def _load_board():
    """형제 board 엔진을 위치로 로드해 worktree lease 재앵커 판정을 승계한다."""
    path = Path(__file__).resolve().with_name("board.py")
    return _load_module_from_path(path, "board.py", verifier=_verify_engine_rev)


def _absolute_git_common_dir(board, anchor: Path) -> Path | None:
    """Git이 증명한 ``anchor``의 공용 저장소 디렉터리를 절대경로로 반환한다."""
    common = board._git_rev_parse(
        anchor, "--path-format=absolute", "--git-common-dir",
    )
    if common is None:
        # Git 2.31 미만 호환. 기존 해소 경로가 쓰던 상대경로 정규화와 같은 폴백이다.
        common = board._git_rev_parse(anchor, "--git-common-dir")
    if common is None:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = anchor / common_path
    return common_path.resolve()


def _git_worktree_records(anchor: Path) -> tuple[tuple[Path, bool], ...]:
    """``anchor``와 같은 저장소라고 Git이 열거한 worktree와 bare 여부를 반환한다."""
    try:
        result = subprocess.run(
            [
                "git", "-C", str(anchor), "worktree", "list",
                "--porcelain", "-z",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode != 0:
        return ()

    records: list[tuple[Path, bool]] = []
    for raw_record in result.stdout.split("\0\0"):
        fields = [field for field in raw_record.split("\0") if field]
        if not fields or not fields[0].startswith("worktree "):
            continue
        records.append((
            Path(fields[0][len("worktree "):]).resolve(),
            "bare" in fields[1:],
        ))
    return tuple(records)


def _checkout_pm_home_matches(board, checkout: Path) -> tuple[Path, ...]:
    """같은 저장소 checkout 하나의 기존 board/lease 축에서 소유 PM 홈을 찾는다.

    일반 main worktree가 실 board 자체면 그 checkout이 소유자다. 그 밖에는 checkout의 경로와
    git-common-dir 조상만 후보로 삼고 기존 strict lease point-read로 정확한 등록을 요구한다.
    """
    checkout = checkout.resolve()
    if board._has_real_board(checkout / ".project_manager"):
        return (checkout,)

    search: list[Path] = [checkout, *checkout.parents]
    common_path = _absolute_git_common_dir(board, checkout)
    if common_path is not None:
        search.extend((common_path, *common_path.parents))

    matches: list[Path] = []
    seen: set[Path] = set()
    for path in search:
        home = path.resolve()
        if home in seen:
            continue
        seen.add(home)
        if not (home / ".project_manager").is_dir():
            continue
        matched, _error = board._ledger_registration(home, checkout)
        if matched:
            matches.append(home)
    return tuple(sorted(set(matches)))


def _same_repo_checkout_pm_home_matches(
    board, anchor: Path, common_path: Path,
) -> tuple[Path, ...]:
    """미등록 linked worktree를 같은 Git 저장소의 소유 checkout을 거쳐 재해소한다.

    main worktree를 포함한 모든 non-bare checkout에서 기존 board/lease 소유자를 찾는다. 어느
    후보든 common-dir을 다시 대조하고, 서로 다른 소유자가 나오면 호출부가 모호성으로 거부한다.
    임의 경로/상위 디렉터리로 해소 축을 넓히지 않는다.
    """
    records = _git_worktree_records(anchor)
    if not records:
        return ()
    roots = tuple(path for path, is_bare in records if not is_bare)
    owners: list[Path] = []
    for checkout in roots:
        if checkout == anchor or not checkout.is_dir():
            continue
        if _absolute_git_common_dir(board, checkout) != common_path:
            continue
        owners.extend(_checkout_pm_home_matches(board, checkout))
    return tuple(sorted(set(owners)))


def resolve_pm_home_for_repo(
    anchor: Path, *, required: bool = False, warning_sink: list[str] | None = None,
    demotion_sink: list[PmHomeDemotion] | None = None,
    resolution_sink: list[PmHomeResolutionFacts] | None = None,
) -> Path:
    """repo/worktree가 소속된 PM 홈을 lease 장부로 해소한다.

    실 board를 가진 repo는 자기 자신, 등록 linked worktree는 정확히 한 lease 소유 홈을
    반환한다. lease 미등록 gate snapshot은 Git이 증명한 같은 저장소의 main/등록 checkout을
    거쳐 그 소유 홈을 반환한다. 일반 standalone repo는 자기 local.conf를 쓰도록 자기 자신을
    반환한다. board가 필수면 장부 부재·손상·중복은 fail-loud다. board 불필요 실행은 한 줄
    경고 후 자기 repo를 standalone 앵커로 사용한다.

    `demotion_sink` 를 주면 그 폴백(강등)의 근거를 `PmHomeDemotion` 으로 담는다 — 호출부가
    소유 PM 홈 필터 승계/차단을 판단하는 입력이다. standalone·실 board 소유처럼 폴백이 아닌
    정상 해소는 아무것도 담지 않는다. `resolution_sink` 는 마커/linked/lease를 포함한 가드 판정
    사실을 담아, 호출부가 이 resolver seam을 건너뛰고 실제 checkout을 다시 읽지 않게 한다.
    """
    anchor = anchor.resolve()
    board = _load_board()

    # PM 홈 자기 checkout과 plain clone은 lease가 없는 정상 standalone 형상이다. linked
    # worktree만 소유자 재해소가 필요하다. board._resolve_read_board()는 티켓이 실재하는 홈만
    # 후보로 삼으므로, 아직 티켓이 하나도 없는 등록 슬롯의 config 소유자 판정에는 쓸 수 없다.
    if board._has_real_board(anchor / ".project_manager"):
        _publish_pm_home_resolution(resolution_sink, (), anchor=anchor, pm_home=anchor)
        return anchor
    if not board._is_linked_worktree(anchor):
        _publish_pm_home_resolution(resolution_sink, (), anchor=anchor, pm_home=anchor)
        return anchor

    search: list[Path] = list(anchor.parents)
    common_path = _absolute_git_common_dir(board, anchor)
    if common_path is not None:
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
        pm_home = unique[0]
        _publish_pm_home_resolution(resolution_sink, (), anchor=anchor, pm_home=pm_home)
        return pm_home
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
        # `<pm-home>/work/<slot>`은 lease가 소유하는 정규 슬롯이다. 장부에서 빠진 정규 슬롯까지
        # main-worktree 축으로 되살리면 lease 제거/손상의 fail-loud 계약이 무력화된다. 새 축은 그
        # 관례 밖에 만들어지는 일회용 gate snapshot에만 적용한다.
        managed_slot_without_lease = any(
            anchor.parent.name == "work" and anchor.parent.parent == home
            for home in candidates
        )
        if managed_slot_without_lease:
            resolution_error = (
                "worktree lease 장부에서 소유 PM 홈을 찾지 못했습니다."
            )
        else:
            same_repo_owners = (
                _same_repo_checkout_pm_home_matches(board, anchor, common_path)
                if common_path is not None else ()
            )
            if len(same_repo_owners) == 1:
                pm_home = same_repo_owners[0]
                _publish_pm_home_resolution(
                    resolution_sink, (), anchor=anchor, pm_home=pm_home,
                )
                return pm_home
            if len(same_repo_owners) > 1:
                homes = ", ".join(str(home) for home in same_repo_owners)
                resolution_error = (
                    "같은 Git 저장소 checkout들이 여러 PM 홈의 worktree lease 장부에 "
                    f"등록되어 소유자가 모호합니다: {homes}. 장부를 정리한 뒤 다시 "
                    "실행하세요."
                )
            else:
                resolution_error = (
                    "worktree lease 장부와 같은 Git 저장소의 소유 checkout에서 PM 홈을 "
                    "찾지 못했습니다."
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
    demotion = PmHomeDemotion(anchor, resolution_error, tuple(candidates))
    if demotion_sink is not None:
        demotion_sink.append(demotion)
    _publish_pm_home_resolution(
        resolution_sink, (demotion,), anchor=anchor, pm_home=anchor,
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


_GATE_SNAPSHOT_MARKER = Path(
    ".project_manager/.local/gate-snapshot.json"
)


def _gate_snapshot_marker(anchor: Path) -> Path | None:
    """생성기가 남긴 스냅샷 사실 마커를 반환한다(내용 손상도 차단 쪽으로 fail-closed)."""
    marker = anchor.resolve() / _GATE_SNAPSHOT_MARKER
    try:
        marker.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        # 접근 오류를 '마커 없음'으로 낮추면 손상만으로 가드가 열릴 수 있다.
        return marker
    # 생성기는 regular JSON file을 쓰지만, 이후 손상된 symlink/directory도 '없음'으로
    # 강등하지 않는다. 내용 파싱이나 강등 사유 문자열은 차단 판정 입력이 아니다.
    return marker


def _is_unregistered_linked_self_anchor(
    demotions: Sequence[PmHomeDemotion], *, diff_root: Path, pm_home: Path,
) -> bool:
    """라운드 장부 앵커가 lease 미등록 linked worktree 자신인지 단일 판정한다.

    `resolve_pm_home_for_repo` 의 강등 기록은 판정 입력이 아니다. 스냅샷 안에 실 ticket이 있으면
    resolver가 정상 board 자기 앵커로 조기 반환해 강등 기록이 생기지 않기 때문이다. 대신
    git-dir/common-dir 차이로 linked worktree를 확인하고, 경로 조상과 common-dir 조상에 있는
    PM 홈 lease 장부 중 하나라도 이 앵커를 등록하는지 직접 조회한다.

    main checkout/standalone PM 홈은 linked worktree가 아니므로 False다. 정상 등록 worktree가
    자기 ticket 때문에 self-anchor로 해소돼도 lease match가 있으므로 False다. 소유 PM 홈으로
    재앵커된 정상 worktree도 `pm_home != diff_root`에서 False다. lease 장부가 손상돼
    자기 앵커로 강등됐더라도 해소기가 남긴 단일 소유 후보가 있으면 관리 슬롯 복구 폴백으로
    False다. 후보가 생긴 *이유 문자열*은 판정에 쓰지 않는다.
    """
    anchor = diff_root.resolve()
    if pm_home.resolve() != anchor:
        return False

    board = _load_board()
    if not board._is_linked_worktree(anchor):
        return False

    search: list[Path] = list(anchor.parents)
    common = board._git_rev_parse(anchor, "--git-common-dir")
    if common is not None:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (anchor / common_path).resolve()
        search.extend((common_path, *common_path.parents))

    seen: set[Path] = set()
    for path in search:
        owner = path.resolve()
        if owner in seen:
            continue
        seen.add(owner)
        if not (owner / ".project_manager").is_dir():
            continue
        if not board._registers_worktree(owner, anchor):
            continue
        matched, error = board._ledger_registration(owner, anchor)
        if matched or error is not None:
            return False
    # lease 장부 손상 시 exact match는 읽을 수 없다. 그래도 resolver가 경로/git common-dir
    # 사실으로 단일 관리 후보를 남겼고 **그 후보의 strict point-read가 실제 오류를 반환**하면
    # 종전 복구 폴백을 보존한다. 정상적으로 읽힌 장부의 non-match까지 후보 수만으로 허용하면
    # 유효한 빈 장부 아래 미등록 worktree가 휘발 장부에 라운드를 기록할 수 있다.
    # gate_snapshot 마커는 호출자가 이 술어보다 먼저 검사하므로 후보·경로와 무관하게 차단된다.
    candidate_owners = {
        candidate.resolve()
        for demotion in demotions
        if demotion.anchor.resolve() == anchor
        for candidate in demotion.candidates
    }
    if len(candidate_owners) == 1:
        candidate_owner = next(iter(candidate_owners))
        _matched, error = board._ledger_registration(candidate_owner, anchor)
        if error is not None:
            return False
    return True


def _publish_pm_home_resolution(
    sink: list[PmHomeResolutionFacts] | None,
    demotions: Sequence[PmHomeDemotion],
    *,
    anchor: Path,
    pm_home: Path,
) -> None:
    """resolver가 읽은 자기-앵커 사실을 선택적 sink에 한 번 게시한다."""
    if sink is None:
        return
    resolved_anchor = anchor.resolve()
    resolved_home = pm_home.resolve()
    marker = _gate_snapshot_marker(resolved_anchor)
    sink.append(PmHomeResolutionFacts(
        anchor=resolved_anchor,
        pm_home=resolved_home,
        snapshot_marker=marker,
        unregistered_linked_self_anchor=(
            False
            if marker is not None
            else _is_unregistered_linked_self_anchor(
                demotions, diff_root=resolved_anchor, pm_home=resolved_home,
            )
        ),
    ))


def _self_anchored_round_refusal(
    demotions: Sequence[PmHomeDemotion],
    resolutions: Sequence[PmHomeResolutionFacts],
    *,
    diff_root: Path,
    pm_home: Path,
    gate: str | None,
) -> str | None:
    """스냅샷/미등록 linked worktree의 휘발 장부에 라운드를 기록하면 차단 안내를 반환한다.

    생성 마커는 경로·PM-home 후보·lease 상태보다 먼저 판정한다. 마커 없는 과거/수동 linked
    worktree 판정까지 `resolve_pm_home_for_repo`가 게시한 facts만 소비한다. 이 seam이 고정된
    하네스에서는 가드도 실제 checkout/git/lease/마커를 다시 읽지 않는다. 강등 사유 문자열은
    안내에만 쓰며 판정 입력이 아니다. `gate is None` 인 명시 `--no-gate` 자문은 라운드 장부를
    쓰지 않아 raw 자기 앵커 복구 채널의 기존 계약을 보존한다.

    호출부는 dry-run·조회·처분과 비활성/egress/diff-cap 조기 종료가 끝난 뒤 이 함수를 부른다.
    따라서 여기서 문자열이 나오면 바로 다음 단계가 라운드 예약인 **실 전송 확정 구간**이다.
    """
    if gate is None:
        return None
    resolved_diff = diff_root.resolve()
    resolved_home = pm_home.resolve()
    resolution = next((
        fact for fact in resolutions
        if fact.anchor == resolved_diff and fact.pm_home == resolved_home
    ), None)
    if resolution is None:
        return None
    marker = resolution.snapshot_marker
    if marker is not None:
        ledger = resolved_home / ".project_manager" / ".local" / "review_rounds.json"
        return _GATE_SNAPSHOT_ROUND_GUIDANCE.format(
            anchor=resolved_diff,
            gate=gate,
            ledger=ledger,
            marker=marker,
        )
    if not resolution.unregistered_linked_self_anchor:
        return None
    reason = next(
        (
            demotion.reason
            for demotion in demotions
            if demotion.anchor.resolve() == resolved_diff
        ),
        "linked worktree 자기 앵커가 어떤 PM 홈 lease 장부에도 등록되지 않았습니다.",
    )
    ledger = resolved_diff / ".project_manager" / ".local" / "review_rounds.json"
    return _SELF_ANCHORED_ROUND_GUIDANCE.format(
        anchor=resolved_diff,
        gate=gate,
        ledger=ledger,
        reason=reason,
    )


def _registered_worktrees(pm_home: Path) -> tuple[Path, ...]:
    """PM 홈의 실재 worktree 후보를 lease 장부에서 파생한다(하네스 목록/슬롯 추측 없음)."""
    ledger = pm_home / ".project_manager" / ".local" / "worktree-leases.json"
    try:
        data = json.loads(_read_text_shared(ledger, encoding="utf-8"))
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
            "--paths가 여러 git repo를 가리킵니다 — 추가 리뷰 1회는 diff 앵커 하나만 허용합니다."
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
# 잡고, 여긴 PM 홈에서 실행된 추가 리뷰를 잡아 worktree 로 재지정한다. 순수 filesystem 판정(subprocess
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

# legacy 경로의 기본 명령 (구조화 키 부재 시 · local.conf reviewer_cmd 로 교체 가능·unpinned-model)
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"

# 외부 호출의 시간 예산(무진행 상한 + 벽시계 백스톱)은 **해소된 대상의 하네스 프로필**이 소유한다
# (`pm_relay.HARNESS_PROFILES` — legacy 기본 command가 `codex exec`이면 codex 축 값). 이 모듈에
# 별도 타임아웃 상수를 두지 않는 이유가 이 티켓의 편입 이유와 같다: **값이 두 군데면 규칙이 둘이
# 된다.** 평범한 diff 153~294초·13파일 대형 227초 — 구 기본 180초는 평상 대역 *안*이라 상시 타임아웃
# 구조였고, 900 으로 올려도 같은 구조였다(실측: **입력·모델이 완전히 동일한 리뷰 2회 중 하나는
# 900초 초과 kill·다른 하나는 `--timeout 1500` 으로 성공**). 그래서 값을 고르는 게임을 끝내고
# 판정 기준을 무진행으로 교체했고, 벽시계는 "감지기 자체가 고장난 경우"의 유한 상한으로 강등된다.
# 조정: 일회성 `--timeout`/`--idle-timeout` > local.conf `harness.<reviewer>.wall_timeout`/
# `.idle_timeout` > 아래 표면-flat legacy 키 > 프로필 선언.
# opt-in 게이트 키는 사람 역할 이름과 같이 `additional_reviewer_enabled` 로 개칭됐다.
# 구키 `external_review_enabled` 의 **fallback 읽기는 제거됐다**(개칭 릴리즈가 예고한 유예 종료) —
# 이제 이 키는 값을 공급하지 않는다. 다만 **감지·안내는 남긴다**: 구키만 있는 채택자가 이유 모를
# OFF 를 겪으면 그게 곧 무음 강등이다. 엔진은 채택자 local.conf 를 대신 고쳐 쓰지 않으므로(자동
# 마이그레이션 없음) 처방은 사람에게 준다. 모듈 파일명·raw 파일 접두·아래 표면-flat legacy 타임아웃
# 키는 기계 식별자로 그대로 남는다(기존 raw 감사물과 채택자 PM 홈 사본의 안정 계약).
# **아래 `LEGACY_*_KEY` 4종은 감지 전용이다** — 값 공급 경로는 없고(어느 해소도 이 상수를 읽어
# 값을 꺼내지 않는다) 오직 "이 conf 에 구키가 남아 있는가" 를 판정해 안내 1줄을 만드는 데만 쓴다.
ADDITIONAL_REVIEWER_ENABLED_KEY = "additional_reviewer_enabled"
LEGACY_EXTERNAL_REVIEW_ENABLED_KEY = "external_review_enabled"

# 라운드/wave 예산 노브도 게이트 키와 **같은 규칙**으로 개칭됐고, fallback 도 같은 릴리즈에 함께
# 제거됐다 — 이름만 바뀌고 값 의미·기본값은 그대로다. 게이트 키 하나만 바꾸면 채택자 local.conf
# 안에서 같은 기능의 키가 두 접두로 갈려("어느 게 현재 이름인가") 개칭이 절반만 도착한다. 구키가
# 값을 담고 있으면 그 값은 무시되고 키마다 안내 1줄이 나간다(엔진 기본값으로 간다). 게이트 축과
# 같이 아래 `LEGACY_*_KEY` 3종도 **감지 전용**이다(값 공급 아님).
ADDITIONAL_REVIEWER_ROUND_LIMIT_KEY = "additional_reviewer_round_limit"
LEGACY_EXTERNAL_REVIEW_ROUND_LIMIT_KEY = "external_review_round_limit"
ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY = (
    "additional_reviewer_incomplete_round_limit")
LEGACY_EXTERNAL_REVIEW_INCOMPLETE_ROUND_LIMIT_KEY = (
    "external_review_incomplete_round_limit")
ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY = "additional_reviewer_wave_budget"
LEGACY_EXTERNAL_REVIEW_WAVE_BUDGET_KEY = "external_review_wave_budget"

# 신키 → 구키 매핑. **감지·안내가 이 한 표에서 파생한다**(값 해소는 더 이상 이 표를 타지 않는다) —
# 키마다 분기를 복사하면 새 노브가 안내를 못 갖는 절반 배선이 생긴다.
LEGACY_KNOB_KEYS: dict[str, str] = {
    ADDITIONAL_REVIEWER_ROUND_LIMIT_KEY: LEGACY_EXTERNAL_REVIEW_ROUND_LIMIT_KEY,
    ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY:
        LEGACY_EXTERNAL_REVIEW_INCOMPLETE_ROUND_LIMIT_KEY,
    ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY: LEGACY_EXTERNAL_REVIEW_WAVE_BUDGET_KEY,
}

# Codex egress attestation 플래그 — 판정/문구 단일 소유자는 pm_relay 이고, 여기 리터럴은 argparse
# 선언용 사본이다(드리프트는 회귀 테스트가 막는다).
CODEX_EGRESS_FLAG = "--codex-egress-escalated"

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

# 라운드 상한 — 같은 --gate 로 이 횟수를 넘겨 실 전송하면 이후 실행을 거부한다.
# 기본 4 는 사용자 전역 규율(추가 리뷰 ">3~4 라운드면 수렴 판단")의 기계화. local.conf
# additional_reviewer_round_limit 로 조정 가능.
DEFAULT_ROUND_LIMIT = 4
DEFAULT_INCOMPLETE_ROUND_LIMIT = 2

# 수렴-형상 상한 — 코드 리뷰 라운드 수 상한(기본 2·local.conf `review_rounds_max`).
# 위 판정 상한(4)이 "몇 번 전송했나"만 보는 반면 이 축은 **장부의 must_fix 추이**로 수렴 여부를
# 본다: 상한을 넘겼거나(초과), 직전 라운드보다 must_fix 가 늘었으면(발산) 라운드를 더 쓰지 않는다.
# 실측에서 green 6건 중 5건이 2R 종결이고 관측된 3R 시도 8건은 모두 반려였다.
DEFAULT_REVIEW_ROUNDS_MAX = 2
REVIEW_ROUNDS_MAX_KEY = "review_rounds_max"

# 수렴 차단 사유 — 안내 문구의 라벨이자 판정 반환값(호출부가 사유별로 갈리지 않게 한 축).
CONVERGENCE_DIVERGING = "diverging"
CONVERGENCE_CAP_UNRESOLVED = "cap-unresolved"
CONVERGENCE_CAP_REACHED = "cap-reached"
_CONVERGENCE_REASONS: dict[str, str] = {
    CONVERGENCE_DIVERGING: "직전 라운드 대비 must_fix 증가(발산) — 상한 전 조기 차단",
    CONVERGENCE_CAP_UNRESOLVED: "라운드 상한 도달 · must_fix 미해소",
    CONVERGENCE_CAP_REACHED: "라운드 상한 도달",
}

# diff 서킷브레이커 — 티켓 `estimate` 별 diff 총량(추가+삭제) 상한. 리뷰 라운드가 수렴하지 않는
# 두 번째 원인이 **구현 스코프 팽창**이라(실측 5,018줄 단일 티켓), 리뷰/완료 진입에서 총량을
# 기계로 대조한다. 값은 프로젝트 규약이고 채택자는 local.conf `diff_cap.<estimate>` 로
# 덮어쓴다. estimate 가 없거나 모르는 값이면 가드 off(None) — 엔진이 보편값을 지어내지 않는다.
DEFAULT_DIFF_CAPS: dict[str, int] = {"small": 300, "medium": 1000, "large": 2500}
DIFF_CAP_KEY_PREFIX = "diff_cap."

# 측정 제외 subtree — 기계 생성·설치 산출물을 손작업 diff 와 같은 가중으로 합산하지 않는다.
# `templates/<타깃>/.project_manager/`는 그 대부분이 pm_update 가 내보낸 엔진 사본이고,
# `.opencode/node_modules/`는 패키지 설치 트리다. 이들을 세면 한 티켓의 구현 스코프가 출하 타깃 수나
# 의존성 크기만큼 부풀어 분할이 필요 없는 티켓을 서킷브레이커가 막는다. 제외는 **subtree 단위**라
# templates 안의 manifest 밖 손편집 파일도 함께 빠지지만, 오차의 방향은 **측정 축소 = 가드 약화**라
# 정당한 작업을 오차단하지 않는다. template subtree 자체의 정합은 drift-0 가드가 따로 지키고,
# payload와 리뷰어 거울은 아래의 좁은 manifest 예외만 보존하고 나머지 측정 제외분을 함께 뺀다.
_MACHINE_MIRROR_RE = re.compile(
    r"^templates/[^/]+/\.project_manager/|^\.opencode/node_modules/"
)
# 세 출하 타깃의 manifest는 pm_update 결과물이 아니라 사람이 전파 범위를 선언하는 입력 자산이다.
# 측정 술어의 subtree 정책은 유지하되, 검토 누락이 false-green이 되는 이 세 실파일만 보존한다.
_HAND_EDITED_REVIEW_PATHS = frozenset({
    "templates/claude_code/.project_manager/engine.manifest",
    "templates/codex/.project_manager/engine.manifest",
    "templates/opencode/.project_manager/engine.manifest",
})
# 사람 표면에 측정 의미를 실어 두는 한 줄 — "왜 내 diff 보다 적게 세나"를 안내가 스스로 답한다.
MEASURED_SCOPE_NOTE = "측정=손작업 스코프(기계 mirror 제외)"

# wave(세션) 단위 총 라운드 예산 — 게이트별 상한과 **별개** 축이다. 게이트 상한만 있으면 비용이
# 티켓 수 × 라운드 상한으로 확장되므로, 전 게이트 합계 전송을 이 예산으로 묶는다. 기본 24 는
# 게이트 상한 4 × 동시 진행 6티켓 어림이고 실측 세션당 라운드(~50)보다 낮게 잡아 PM 이 중간에
# `--rounds-report`로 수렴 상태를 점검하는 관측점을 만든다. local.conf
# additional_reviewer_wave_budget 로 조정 가능.
DEFAULT_WAVE_BUDGET = 24

# 라운드 상한 초과 전용 종료 코드 (기존 0=통과·1=반려/실패/오류·2=argparse·3=예약 과 구분).
# 실행 전 거부라 리뷰어는 호출되지 않는다(외부 전송·과금 없음). wave 예산 소진도 같은 축(전송 전
# 예산 거부)이라 같은 rc 를 쓴다 — 호출부가 "예산 때문에 안 나갔다"를 한 코드로 판별한다.
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
_AUTHORITATIVE_TICKET_CONTEXT = (
    "티켓 §결정·§설계·PM 판정 블록은 **권위 있는 확정 사항**이다 — 그 결정을 되돌리라는 지적은 "
    "`design-proposal` 로 분류하고 must-fix 로 내지 마라. must-fix 는 티켓 목표·결정 안에서의 "
    "결함에 한한다."
)

_DEFAULT_CONTEXT_HEADER = f"""\
## 리뷰 맥락

아래 diff 를 코드리뷰하라. 프로젝트 고유 맥락(`.project_manager/review_context.local.md`)이
설정돼 있으면 그 기준을 우선한다.

{_AUTHORITATIVE_TICKET_CONTEXT}
"""

# 출력 형식 블록 (parse_verdict 가 의존 — 리뷰어 무관 공통)
_OUTPUT_FORMAT_BLOCK = """\
### 출력 형식 (필수)
아래 형식으로 응답하라:

판정: [통과 | 반려]

**must-fix** (반드시 수정):
- (없으면 "없음" · 있으면 아래 블록의 finding ID 만 나열한다)

**suggestion** (권장):
- (없으면 "없음" · 있으면 아래 블록의 finding ID 만 나열한다)

"""

# 구조화 블록 요구 — 스키마는 엔진 파서 상수에서만 파생한다(여기서 다시 적지 않는다).
_VERSIONED_BLOCK_HEADER = """\
### 구조화 판정 블록 (필수)
위 산문 뒤에 아래 스키마의 블록을 **정확히 하나** 출력하라. 엔진이 이 회신 전문을 게이트 티켓의
`external-reviewer` 라운드 파일로 회수하고, PM 은 이 블록으로 판정한다 — 증거·권고·심각도는 블록이
단일 진실이므로 산문에 같은 항목을 다시 서술하지 마라.

"""
_VERSIONED_BLOCK_RULES = """\
- `id` 는 이 채널 전용 접두 `{prefix}-` 를 쓰고 **이번 라운드는 `{next_id}` 부터** 매긴다 —
  티켓 전역 유일이라 본문에 이미 있는 ID 를 다시 쓰면 이 라운드는 회수되지 않는다.
- `severity` 가 "반드시 고쳐야 하는가"의 단일 진실이다(가장 높은 값 `{top_severity}` 는 산문
  must-fix 절과 같은 건수여야 한다).
- `authority` 는 티켓 §목표/§결정 또는 `[[T-NNNN]]`·`[[ADR-NNNN]]` 같은 권위 근거를 적는다.
- 직전 라운드 지적의 해소 확인 라운드는 그 `{prefix}-` ID 를 `confirmations` 에 싣는다.
- 위 티켓 본문에 이미 있는 블록(지난 라운드 산출·시드 골격)을 회신에 **재인용하지 마라** —
  이 회신에는 네가 이번에 낸 블록 하나만 있어야 한다.
- 블록이 없거나 스키마를 어기면 이 라운드는 회수되지 않는다(라운드 파일 없음 · 종료코드 ≠ 0).

"""


def _versioned_block_requirement(
    next_finding_id: str | None = None,
    confirmation_ids: Sequence[str] | None = None,
) -> str:
    """추가 리뷰어 채널의 구조화 블록 요구 — 골격·접두·ID 실값을 엔진에서 렌더한다.

    리뷰어 세션은 라운드마다 fresh 라 이전 라운드의 ID 를 모른다. 회수 대상 티켓에서 읽어 온
    **다음 ID 실값**(`_next_external_finding_id`)과 **확인 가능한 ID 목록**
    (`_confirmable_external_finding_ids`)을 골격에 싣는다 — 내부 리뷰 절 시드가 같은 값을 같은
    엔진 함수로 채우는 것과 같은 축이다. 실값이 없으면(회수 대상 없는 실행) 첫 ID·placeholder 로
    돌려준다.
    """
    delegate = _load_pm_delegate()
    role = delegate.EXTERNAL_REVIEW_ROLE
    next_id = next_finding_id or delegate.next_review_finding_id("", role)
    return (
        _VERSIONED_BLOCK_HEADER
        + delegate.render_pm_review_block_skeleton(role, confirmation_ids)
        + "\n"
        + _VERSIONED_BLOCK_RULES.format(
            prefix=delegate.PM_REVIEW_FINDING_ID_PREFIXES[role],
            next_id=next_id,
            top_severity=delegate.PM_REVIEW_SEVERITIES[0],
        )
    )


# 확인 전용 라운드(`--confirm-fix`) 헌장 — 이 라운드의 임무를 프롬프트에서 좁힌다. 좁히지 않으면
# 예외 라운드가 그냥 한 라운드 더가 되어 상한이 무의미해진다(실측 12라운드의 형상).
_CONFIRM_FIX_CHARTER = """\
### 이 라운드의 임무 (확인 전용 · 필수)
이번 라운드는 **직전 라운드 must-fix 의 해소 확인 전용**이다. 리뷰 라운드의 연장이 아니다.

- 직전 must-fix 항목이 실제로 해소됐는지만 판정하라.
- 새로 발견한 사항은 must-fix 로 올리지 말고 **"재설계 신호"** 라는 표기와 함께 suggestion 에
  적어라 — 다음 라운드 거리가 아니라 다음 티켓(재설계·분할)의 입력이다.
- 직전 지적이 해소됐으면 신규 발견 유무와 무관하게 `판정: 통과` 로 마감하라.

"""

# 확인 전용 라운드 근거 블록 — 리뷰어 세션은 라운드마다 fresh 라, 직전 지적을 **프롬프트가**
# 들고 가지 않으면 "무엇을 확인하는지 모르는 확인 라운드"가 된다. 텍스트는 라운드 장부의 예약
# 레코드가 보관한 항목이고(`records[].must_fix_items`), 보관분이 없는 구세대 라운드는 건수만
# 싣고 재구성을 지시한다.
_CONFIRM_FIX_EVIDENCE_HEADER = """\
### 직전 라운드 must-fix (이번 확인의 대상)
아래는 직전 반려 라운드({round})가 지적한 항목이다. 이 항목들의 해소 여부만 판정하라.

"""
_CONFIRM_FIX_EVIDENCE_UNRECORDED = """\
항목 텍스트가 장부에 남아 있지 않다 (그 라운드가 기록한 must-fix 건수: {count}). 리뷰 대상
diff 와 티켓 본문에서 직전 지적을 재구성해 해소 여부를 판정하라 — 새 결함 탐색이 아니다.

"""

# 빈-diff fail-loud 안내.
# 검토 경로에 tracked 변경이 없어 diff 가 비면 codex 는 "변경 없음"을 통과로 판정해 가짜
# 통과(false-green)를 낸다. codex 호출 전에 이 메시지로 fail-loud 한다 (우회 플래그 없음).
_EMPTY_DIFF_GUIDANCE = (
    "오류: 리뷰할 diff 가 없습니다 (검토 경로에 tracked 변경 없음).\n"
    "  빈 diff 를 리뷰하면 추가 리뷰어가 '변경 없음'을 통과로 판정해 가짜 통과(false-green)가\n"
    "  발생합니다 — 추가 리뷰어를 호출하지 않고 중단합니다.\n"
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

# 라운드 상한 초과 fail-loud 안내. 같은 게이트로 limit 회를 넘겨 실 전송이 시도되면
# diff 추출·추가 리뷰어 호출 전에 이 안내로 차단한다.
#
# 이 상한의 성격은 **무한 루프 차단(anti-loop pause)**이다. 연장 승인(`--ack-rounds`)은 폐지됐다 —
# "반례가 진짜니까 계속"을 사람이 승인하는 구조 자체가 비용 누수였다(실측 12라운드). 남은 출구는
# **재설계·티켓 분할**뿐이고, 직전 지적의 해소만 확인하려면 게이트당 1회 `--confirm-fix` 를 쓴다.
_ROUND_LIMIT_GUIDANCE = (
    "오류: 추가 리뷰어 라운드 상한 도달 — 게이트 {gate} · "
    "count={unacked}(판정 {verdicts} · 미완 {incomplete})\n"
    "  (판정 상한 {limit} · 미완 재시도 상한 {incomplete_limit}). 무한 라운드 차단이라 "
    "초과분은 기계가 멈춥니다 — 자의 우회 불가.\n"
    "  · **미완**은 판정이 없던 전송입니다 — 타임아웃·중단뿐 아니라 **오염 진단으로 무효화된 "
    "판정**도 이 축에 들어갑니다(판정 표면과 같은 규칙이라 두 표면이 갈리지 않습니다).\n"
    "  · 먼저 `--rounds-report` 로 라운드별 산출과 수렴 상황을 확인하세요.\n"
    "  · **재설계·티켓 분할이 유일한 출구입니다** — 라운드 연장 승인(`--ack-rounds`)은 "
    "폐지됐고, 확인 전용 라운드(`--confirm-fix`)는 수렴 축의 예외라 이 전송 횟수 상한은 "
    "열지 않습니다.\n"
    "  · 상한 조정은 local.conf `additional_reviewer_round_limit`(판정)과 "
    "`additional_reviewer_incomplete_round_limit`(미완).\n"
    "  (장부: {ledger} · count={count} acked_through={acked})"
)

# 수렴-형상 차단 안내 (라운드 상한 rc 를 그대로 쓴다 — 전송 전 예산 거부라 같은 축).
# 라운드 수는 **완료 산출 + 진행 중 예약**이다 — 상한 판정과 같은 수를 보여야 "상한에 닿았는데
# 왜 막히나"를 문구가 스스로 답한다(동시 실행이 이미 나가 있는 형상).
_CONVERGENCE_GUIDANCE = (
    "오류: 리뷰 수렴 게이트 차단 — 게이트 {gate} · {reason}\n"
    "  라운드 {rounds}(완료 {completed} · 진행 중 예약 {inflight}) / 상한 {limit} · "
    "must_fix 추이 [{series}]\n"
    "  · 라운드를 더 쓰지 않습니다 — **재설계·티켓 분할이 유일한 출구입니다**. 남은 지적은 "
    "다음 티켓의 목표로 옮기세요.\n"
    "  · 직전 must_fix 해소만 확인하려면 게이트당 1회 `--confirm-fix` 로 확인 전용 라운드를 "
    "쓸 수 있습니다 (신규 발견은 '재설계 신호'로 보고 — 라운드 계속이 아닙니다):\n"
    "      python3 .project_manager/tools/external_review.py --gate {gate} --confirm-fix "
    "[기존 옵션]\n"
    "  · 라운드별 산출은 `--rounds-report --gate {gate}` 로 확인하세요.\n"
    "  · 상한 조정은 local.conf `{knob}` (기본 {default}).\n"
    "  (장부: {ledger})"
)

# `--confirm-fix` 소진 안내 — 확인 전용 라운드는 게이트당 1회다(장부가 소유·자의 재사용 불가).
_CONFIRM_FIX_SPENT_GUIDANCE = (
    "오류: `--confirm-fix` 는 게이트당 1회입니다 — 게이트 {gate} 는 이미 확인 전용 라운드를 "
    "썼습니다 (사용 {used}회).\n"
    "  · 남은 지적은 라운드로 닫지 않습니다 — 재설계·티켓 분할로 전환하세요.\n"
    "  · 지금까지의 산출은 `--rounds-report --gate {gate}` 로 확인하세요.\n"
    "  (장부: {ledger})"
)

# `--confirm-fix` 자격 미달 안내 — 확인 전용 라운드는 **확인할 지적이 있을 때만** 뜻이 있다.
# 반려 라운드가 없는 게이트(첫 라운드·전건 통과)에서 열어 주면 그 라운드는 근거 없는 통과 판정
# 채널이 된다(리뷰어 세션은 fresh 라 직전 맥락을 스스로 알지 못한다).
_CONFIRM_FIX_NO_REJECTION_GUIDANCE = (
    "오류: `--confirm-fix` 는 **최신 완료 라운드가 반려인 게이트**에서만 씁니다 — 게이트 {gate} 의 "
    "최신 판정은 {verdict} 입니다 (기록된 산출 {rounds}건).\n"
    "  · 확인 전용 라운드의 임무는 '직전 must_fix 해소 확인'입니다 — 확인할 지적이 없으면 "
    "근거 없는 통과 판정만 남습니다.\n"
    "  · 첫 리뷰는 `--confirm-fix` 없이 그냥 실행하세요 (일반 라운드).\n"
    "  · 통과로 닫힌 게이트는 다시 열지 않습니다 — 옛 반려를 근거로 과금 라운드를 여는 경로입니다. "
    "새 변경은 일반 라운드로 리뷰하세요.\n"
    "  · 기록 상황은 `--rounds-report --gate {gate}` 로 확인하세요.\n"
    "  (장부: {ledger})"
)

# `--confirm-fix` 자격 미달 안내(확인 대상이 표면 밖) — 최신 반려는 실재하지만 그 지적이 판정
# 표면에 없다(회수 거부된 라운드이거나 PM 이 `rejected` 로 판정한 ID). 그대로 확인 대상으로
# 실으면 확인 라운드가 표면 밖 ID 를 `confirmations` 에 담아 회수 게이트에 다시 걸린다(과금만
# 소비하는 왕복). 처방은 일반 라운드인데 그 축은 수렴 상한이 막을 수 있어 노브까지 함께 안내한다.
_CONFIRM_FIX_REFUSED_HARVEST_GUIDANCE = (
    "오류: `--confirm-fix` 의 확인 대상이 판정 표면에 없습니다 — 게이트 {gate} 의 최신 반려 "
    "라운드 지적이 회수 거부됐거나 PM 이 rejected 로 판정했습니다 (표면 밖 finding: {ids}).\n"
    "  · 그 산출은 raw 에만 남고 `review delta` 표면에는 없습니다 — 그 ID 를 "
    "확인해도 회수가 다시 거부됩니다.\n"
    "  · 그 지적은 **일반 라운드**로 다시 받으세요 (`--confirm-fix` 없이 실행).\n"
    "  · 수렴 상한이 일반 라운드를 막으면 상한 조정은 local.conf `{knob}` (기본 {default}).\n"
    "  · 거부 사유는 그 실행의 stderr 와 raw 산출, `--rounds-report --gate {gate}` 로 "
    "확인하세요.\n"
    "  (장부: {ledger})"
)

# 확인 가능한 finding ID 해소 실패 안내의 강등 서술 — **두 소비자**를 함께 적는다. 하나만 적으면
# `--confirm-fix` 운영자가 표면 대조가 꺼진 것을 모른 채 장부 기록만 실린 근거를 받는다.
_CONFIRMABLE_IDS_DEGRADED = (
    "골격은 확인 가능한 finding ID 없이 싣고, `--confirm-fix` 근거는 판정 표면 대조 없이 "
    "장부 기록 그대로 싣습니다."
)

# `--ack-rounds` 폐지 안내 — 플래그는 인자표에 남겨 두고(모르는 인자 오류 대신 처방을 낸다)
# 호출 자체를 거부한다. 연장 승인 경로가 남아 있으면 상한이 상한이 아니게 된다.
_ACK_ROUNDS_REMOVED_GUIDANCE = (
    "오류: `--ack-rounds`(라운드 연장 승인)는 폐지됐습니다 — 이 실행은 아무것도 하지 않았습니다.\n"
    "  · 라운드 상한·수렴 상한에 걸렸다면 출구는 **재설계·티켓 분할**입니다 (연장 승인 없음).\n"
    "  · 직전 must_fix 해소 확인만 필요하면 게이트당 1회 `--confirm-fix` 를 쓰세요.\n"
    "  · wave 예산 재개(`--ack-wave`)는 그대로입니다 — 별개 축입니다.\n"
    "  · 현재 수렴 상황은 `--rounds-report --gate <T-NNNN>` 로 확인하세요."
)

# `--confirm-fix` 게이트 누락 안내 — 확인 전용 라운드는 **게이트당 1회**라 장부 항목이 있어야
# 회계가 성립한다. 게이트 없는 confirm-fix 는 그 1회 제한 밖에서 도는 전송이므로(경고만 내고
# 실행하면 상한 밖 라운드가 무한히 열린다) 전송 전에 거부한다 — `--ack-rounds` 폐지와 같은 자리·
# 같은 규율이다(부작용 0 지점).
_CONFIRM_FIX_REQUIRES_GATE_GUIDANCE = (
    "오류: `--confirm-fix` 는 `--gate <T-NNNN>` 와 함께 써야 합니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · 확인 전용 라운드는 **게이트당 1회**이고 그 회계를 라운드 장부가 소유합니다 — 게이트가 "
    "없으면 1회 제한이 성립하지 않습니다.\n"
    "  · 이번 확인이 어느 티켓의 must_fix 해소인지 게이트로 지정하세요:\n"
    "      python3 .project_manager/tools/external_review.py --gate <T-NNNN> --confirm-fix "
    "[기존 옵션]\n"
    "  · 게이트 없이 그냥 한 번 더 보고 싶은 것이면 `--confirm-fix` 를 빼세요(상한 대상 밖 실행)."
)


# ── 게이트 회계 자동 유도 (`--ticket` → `--gate`) ─────────────────────────────
# `--ticket` 만 준 실행은 리뷰를 정상 전송·과금하면서 라운드 예약·기록·상한 회계를 통째로
# 건너뛰었다(실측: 하루 8 라운드 넘게가 장부에 0건). 그 조용한 무기록은 반려 must-fix 가
# 릴리즈 차단 표면(`board.py livegate record`)에 도달하지 못하게 만드는 구멍이라, 기본값을
# **기록**으로 뒤집는다 — `--ticket` 은 이미 이번 검토가 어느 티켓의 것인지 말하고 있으므로 그
# 값을 게이트로 쓰면 회계를 붙이는 데 사람의 기억이 필요 없다([[mechanize-dont-instruct-llm]]:
# "게이트를 같이 붙여라"는 규율 기억은 실측에서 실패했다). 회계 밖 자문 실행은 명시
# opt-out(`--no-gate`)으로만 연다.
#
# 고지 **출력 자리**는 해소 결과를 말하는 자리다 — 유도 자체는 인자 파싱 직후에 하지만(뒤따르는
# 게이트 검사·프롬프트·예약이 모두 그 값을 쓴다), 문구는 stderr **첫 줄 = config provenance** 계약을
# 깨지 않게 provenance 직후에 낸다. 빈 diff·비활성·egress·diff-cap 으로 아직 끝날 수 있는
# 예약 전 자리이므로, 장부에 붙었다는 확정형이 아니라 전송 조건형으로만 말한다.
_GATE_DERIVED_NOTICE = (
    "게이트 자동 유도: --gate {gate} (--ticket 값에서 유도 — 이 실행이 전송되면 라운드 회계가 "
    "게이트 {gate}에 붙습니다). "
    "회계 밖 자문 실행은 --no-gate 로 명시하세요."
)
# 미리보기는 예약도 기록도 하지 않는다 — 같은 문구를 쓰면 "이번 실행이 기록된다"는 오보가 된다.
_GATE_DERIVED_DRY_RUN_NOTICE = (
    "게이트 자동 유도: --gate {gate} (--ticket 값에서 유도 — 미리보기라 이번 실행은 기록·집계하지 "
    "않고, 실 전송 때 라운드 회계가 이 게이트에 붙습니다)."
)

# 회계 밖 실행의 loud 표기 — 침묵이 이 함정의 본체였으므로 명시 opt-out 실행은 자기
# 상태를 스스로 말한다. 아무 선택도 없는 실 전송은 아래 `_GATE_ACCOUNTING_REQUIRED_GUIDANCE`로
# 거부하므로, 이 조건형 경고는 `--no-gate` 경로에서만 나온다.
# 예약 자리(전송 확정 전)의 문구는 **조건형**이다: 이 뒤로 격리 생성·스폰이 남아 있어 "전송했다"고
# 확정하면 격리 실패로 중단된 실행이 이미 찍힌 고지와 모순된다. 확정 표기는 실행이 끝난 뒤 판정
# 블록(`print_summary`)의 게이트 줄이 낸다.
_UNACCOUNTED_RUN_TAIL = (
    "전송되면 라운드 장부에 기록되지 않고 라운드·wave 예산도 쓰지 않습니다 — 그 라운드에서 나온 "
    "반려 must-fix 는 릴리즈 차단(`board.py livegate record`)이 읽는 장부에 남지 않습니다.\n"
    "  · 회계에 넣으려면 `--ticket <T-NNNN>`(게이트 자동 유도) 또는 `--gate <게이트>` 로 "
    "실행하세요."
)
_UNACCOUNTED_OPT_OUT_NOTICE = (
    "경고: `--no-gate` 명시 opt-out — 이 실행이 " + _UNACCOUNTED_RUN_TAIL
)

# 실 전송은 회계에 넣거나 명시적으로 빠져야 한다. 어느 쪽도 고르지 않은 실행을 경고만
# 하고 보내면 티켓 누락 한 번이 라운드 장부·livegate 누락으로 이어진다. dry-run·조회·처분과
# 비활성/egress/diff-cap 조기 종료는 실제 외부 송신이 없어 이 검사에 도달하지 않는다.
_GATE_ACCOUNTING_REQUIRED_GUIDANCE = (
    "오류: 실 전송에는 게이트 회계 지정 또는 명시 opt-out 이 필요합니다 — "
    "이 실행은 외부로 전송하지 않았습니다.\n"
    "  · 라운드 회계에 넣으려면 `--ticket <T-NNNN>`(게이트 자동 유도) 또는 "
    "`--gate <게이트>` 를 지정하세요.\n"
    "  · 회계 밖 자문 실행이면 `--no-gate` 를 명시하세요."
)

# 게이트 장부는 **휘발하지 않는 PM 홈 또는 lease 등록 worktree**에 있어야 한다. linked worktree가
# 자기 앵커로 해소됐지만 어떤 PM 홈 lease에도 등록되지 않은 실행에서 라운드를 허용하면, 격리
# 스냅샷을 재생성할 때 그 안 `.local/review_rounds.json` 도 함께 사라져 수렴 상한·발산 차단·
# livegate 기록이 조용히 초기화된다. 명시 `--paths` + `--no-gate`는 장부 없는 raw 송신 복구
# 채널로 기존대로 열어 두되, **장부 기록이 생기는 실 전송**만 예약 직전 fail-loud 한다.
_GATE_SNAPSHOT_ROUND_GUIDANCE = (
    "오류: 게이트 스냅샷 마커가 있는 앵커에서는 실 전송 라운드를 기록할 수 없습니다 — "
    "이 앵커는 경로·PM 홈 후보·lease 상태와 무관하게 reviewer 전용 일회용 산출물이라 외부로 "
    "전송하지 않았습니다.\n"
    "  · PM 홈 cwd에서 다시 실행하고, 등록 worktree를 가리키는 `--paths <경로>` 또는 "
    "`--ticket <T-NNNN>`을 명시하세요.\n"
    "  · 현재 스냅샷 앵커: {anchor} · gate={gate} · 장부={ledger} · 마커={marker}"
)

_SELF_ANCHORED_ROUND_GUIDANCE = (
    "오류: 미등록 linked worktree 자기 앵커에서는 실 전송 라운드를 기록할 수 없습니다 — "
    "휘발성 장부가 스냅샷 재생성 때 사라져 수렴 게이트를 우회하므로 외부로 전송하지 "
    "않았습니다.\n"
    "  · PM 홈 cwd에서 다시 실행하고, 등록 worktree를 가리키는 `--paths <경로>` 또는 "
    "`--ticket <T-NNNN>`을 명시하세요.\n"
    "  · worktree lease 장부에 등록되지 않은 linked worktree 전반에서 게이트 라운드를 "
    "실행하지 마세요. 격리 스냅샷은 내부 reviewer 전용입니다.\n"
    "  · 현재 자기 앵커: {anchor} · gate={gate} · 장부={ledger} · 판정 근거={reason}"
)

# 판정 블록의 게이트 줄 — 회계 밖 실행은 **끝난 뒤** 여기서 확정형으로 말한다. stderr 경고는 로그를
# 안 읽으면 사라지지만 PM 은 판정 블록을 반드시 읽는다(오염 진단·실패 사유와 같은 근거).
_SUMMARY_UNACCOUNTED_GATE = "(없음 — 회계 밖·라운드 장부 미기록)"

# `--gate` 와 `--no-gate` 동시 지정 안내 — 한 실행이 "기록한다"와 "기록하지 않는다"를 동시에
# 뜻할 수 없다. 경고 후 한쪽을 골라 실행하면 그 선택이 조용한 자의 판정이 되므로 부작용 0
# 지점에서 거부한다(`--confirm-fix` 게이트 누락·`--ack-rounds` 폐지와 같은 자리·같은 규율).
_GATE_OPT_OUT_CONFLICT_GUIDANCE = (
    "오류: `--no-gate` 와 `--gate {gate}` 는 함께 쓸 수 없습니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · `--gate` 는 이 라운드를 장부에 기록하겠다는 지정이고, `--no-gate` 는 기록하지 않겠다는 "
    "opt-out 입니다.\n"
    "  · 회계에 넣으려면 `--no-gate` 를 빼고, 회계 밖 자문이면 `--gate` 를 빼세요."
)

# diff 서킷브레이커 차단 안내 — 리뷰/완료 진입에서 같은 문구를 쓴다(두 표면이 다른 말을 하지 않게).
_DIFF_CAP_GUIDANCE = (
    "오류: diff 서킷브레이커 차단 — {ticket}(estimate={estimate}) · "
    "diff {total}줄 > 상한 {cap}줄\n"
    "  측정 범위: {scope}\n"
    "  측정 의미: {measured_note}\n"
    "  · 한 티켓의 구현 스코프가 estimate 를 넘겼습니다 — 리뷰 라운드가 수렴하지 않는 원인입니다.\n"
    "  · **티켓 분할·재설계**로 스코프를 줄이세요 (분할 후 각 티켓이 자기 상한 안에서 돕니다).\n"
    "  · estimate 자체가 틀렸다면 티켓 frontmatter `estimate` 를 고치세요 "
    "(상한: small {small} / medium {medium} / large {large}).\n"
    "  · 프로젝트 상한 조정은 local.conf `{key}`."
)

# wave 예산 소진 fail-loud 안내. 게이트별 상한을 통과한 전송이라도 wave 합계가 예산을 채우면
# 같은 자리(추가 리뷰어 호출 전)에서 같은 rc 로 막는다 — 게이트마다 상한을 새로 받는 구조에서는
# 티켓 수만큼 라운드가 늘어나기 때문이다. 성격은 라운드 상한과 같은 anti-loop pause 다.
_WAVE_BUDGET_GUIDANCE = (
    "오류: wave 예산 소진 — `--ack-wave` 로 재개(예산 리셋)\n"
    "  게이트 {gate} · wave spent={spent} (예산 {budget} · wave 시작 {started})\n"
    "  · 게이트별 라운드 상한과 **별개**인 wave 단위 총 라운드 예산입니다 — 티켓 수 × 라운드 "
    "상한으로 라운드가 확장되는 것을 막습니다.\n"
    "  · 먼저 `--rounds-report` 로 라운드별 산출을 확인하세요.\n"
    "  · 같은 범위가 정상 수렴 중이면 PM 이 자율로 `--ack-wave` 를 붙여 재개합니다 "
    "(spent 를 0 으로 리셋):\n"
    "      python3 .project_manager/tools/external_review.py --gate {gate} --ack-wave [기존 옵션]\n"
    "  · 수렴이 안 되고 있으면(같은 지적 반복·범위 재설계) 그때 사용자에게 보고하세요.\n"
    "  · 예산 조정은 local.conf `additional_reviewer_wave_budget`.\n"
    "  (장부: {ledger})"
)


# ── 게이트 처분 선언(`--resolve-gate`) 안내 ────────────────────────────────
# 라운드 상한으로 종결된 게이트의 잔여 must-fix 를 **어떻게 소화했는지** 장부에 남기는 표면이다.
# 선언 자체가 판정이 아니라 *기록*이고, 릴리즈 차단(`board.py livegate record`)은 그 기록 사실만
# 읽는다 — "사소하니 넘어간다"는 자의 판정이 들어갈 자리를 어느 쪽에도 만들지 않는다.
_RESOLVE_GATE_MODE_GUIDANCE = (
    "오류: `--resolve-gate {gate}` 에는 처분을 **하나** 지정해야 합니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · 후속 티켓으로 재설계: `--into <T-NNNN>` (면제가 아닙니다 — 그 티켓이 done 이어야 "
    "릴리즈가 열립니다)\n"
    "  · 코드로 해소: `--fixed <근거 게이트>` (반려 종료 뒤 **시작**해 변경된 diff를 검토하고 "
    "통과한 장부 게이트를 지목하세요 — 확인 라운드나 후속 게이트)\n"
    "  · 두 처분을 같이 쓸 수 없습니다 (한 게이트의 잔여는 한 갈래로 소화됩니다)."
)

_RESOLVE_GATE_REQUIRED_GUIDANCE = (
    "오류: `{flag}` 는 `--resolve-gate <게이트>` 와 함께 써야 합니다 — 처분할 게이트가 없으면 "
    "선언할 사실이 없습니다.\n"
    "      python3 .project_manager/tools/external_review.py --resolve-gate <게이트> {flag} "
    "<T-NNNN>"
)

_RESOLVE_GATE_UNKNOWN_GUIDANCE = (
    "오류: 게이트 {gate} 는 라운드 장부에 기록이 없습니다 — 처분할 잔여가 없습니다.\n"
    "  · 처분 선언의 입력은 장부의 기록 사실뿐입니다(그 게이트로 실제 돈 라운드).\n"
    "  · 게이트 이름을 `--rounds-report` 로 확인하세요 (장부: {ledger})."
)

_RESOLVE_GATE_DRY_RUN_GUIDANCE = (
    "오류: `--resolve-gate {gate}` 는 `--dry-run` 과 함께 쓸 수 없습니다 — 이 실행은 아무것도 "
    "하지 않았습니다.\n"
    "  · 처분 선언은 장부에 사실을 남기는 것이 목적이라 미리보기가 성립하지 않습니다 "
    "(외부 전송은 어차피 없습니다).\n"
    "  · 지금 상태만 보려면 `--rounds-report --gate {gate}` 를 쓰세요."
)

_RESOLVE_GATE_NO_RESIDUAL_GUIDANCE = (
    "오류: 게이트 {gate} 에는 처분할 잔여 must-fix 가 없습니다 (최종 라운드 must_fix={residual}).\n"
    "  · 처분 선언은 **반려로 끝난** 게이트의 잔여를 소화하는 기록입니다 — 잔여가 없으면 릴리즈 "
    "게이트도 이 게이트를 보지 않습니다(무대상).\n"
    "  · 현재 상태는 `--rounds-report --gate {gate}` 로 확인하세요."
)

_RESOLVE_GATE_SELF_INTO_GUIDANCE = (
    "오류: 게이트 {gate} 의 잔여를 **자기 자신**({ticket})으로 재설계할 수 없습니다.\n"
    "  · 재설계는 잔여 must-fix 를 소화할 **다른** 후속 티켓을 지목하는 선언입니다 — 자기 지목은 "
    "그 게이트가 done 이라는 사실만으로 잔여를 지워 릴리즈를 여는 우회입니다.\n"
    "  · 코드로 이미 해소됐다면 `--fixed <근거 게이트>` 를 쓰세요."
)

_RESOLVE_GATE_TICKET_GUIDANCE = (
    "오류: 재설계 대상 티켓 {ticket} 을 보드에서 찾지 못했습니다 — {detail}\n"
    "  · 먼저 후속 티켓을 만들고(`board.py new`) 그 ID 로 다시 선언하세요.\n"
    "  · 재설계 대상은 릴리즈 시점에 **done** 이어야 합니다 (같은 릴리즈 안 소화)."
)

_RESOLVE_GATE_EVIDENCE_GUIDANCE = (
    "오류: 근거 게이트 {evidence} 가 해소를 뒷받침하지 못합니다 — {detail}\n"
    "  · `--fixed` 는 '마지막 반려분이 코드로 해소됐고 그 사실을 **통과 라운드가 보였다**'는 "
    "선언입니다 — 근거는 장부의 기록(마지막 라운드 판정 0 + 반려 종료 뒤 started_at + 서로 다른 "
    "target_rev)이어야 합니다. 구 라운드처럼 결속 필드가 없거나 ts가 ISO 8601 UTC가 아니면 "
    "근거로 인정하지 않습니다.\n"
    "  · 확인 전용 라운드(`--gate {gate} --confirm-fix`)나 후속 게이트를 통과시킨 뒤 그 게이트를 "
    "지목하세요.\n"
    "  · 아직 통과 근거가 없으면 후속 티켓으로 재설계하세요: `--resolve-gate {gate} --into <T-NNNN>`."
)


# ── 설정 ──────────────────────────────────────────────────────────────────


def local_config(repo: Path | None = None) -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    conf: dict[str, str] = {}
    path = (repo / ".project_manager" / "local.conf") if repo is not None else LOCAL_CONF
    if not path.exists():
        return conf
    for line in _read_text_shared(path, encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


def _local_config_for_repo(repo: Path) -> dict[str, str]:
    """해소된 PM 홈의 config만 읽는 명시적 seam."""
    return local_config(repo)


def enabled_decision_key(conf: dict[str, str]) -> str | None:
    """게이트 결정을 공급하는 키 — **신키뿐**. 결정이 없으면 None.

    "결정"의 판정은 **키 존재**다(값의 truthiness 가 아니다) — `false` 도 기록된 결정이라 온보딩이
    다시 묻지 않는다. 구키는 개칭 릴리즈의 유예가 끝나 더 이상 결정을 공급하지 않는다: 구키만 있는
    conf 는 **미결정**이고, 게이트는 기본값(OFF)으로 간다. 그 전환이 무음이 되지 않게 아래
    `legacy_enabled_key_warning` 이 같은 conf 를 감지해 안내 1줄을 낸다(fallback 은 없되 침묵도 없다).
    """
    return (ADDITIONAL_REVIEWER_ENABLED_KEY
            if ADDITIONAL_REVIEWER_ENABLED_KEY in conf else None)


# 구키만 남은 conf 의 안내 1줄 — board/pm_update 사본과 **같은 문구**를 쓴다(드리프트는 회귀가
# 잡는다). 엔진은 채택자 conf 를 대신 고쳐 쓰지 않으므로 처방을 사람에게 준다. 버전 리터럴을 박지
# 않는 이유는 이 문장이 릴리즈마다 stale 해지기 때문이다 — "더 이상 읽지 않는다" 는 사실만 말한다.
LEGACY_ENABLED_KEY_REMOVED = (
    f"⚠ local.conf `{LEGACY_EXTERNAL_REVIEW_ENABLED_KEY}` 는 더 이상 읽지 않는다(구키 제거) — "
    f"`{ADDITIONAL_REVIEWER_ENABLED_KEY}` 로 바꾸세요. 그 전까지 추가 리뷰어는 OFF 입니다."
)


def legacy_enabled_key_warning(conf: dict[str, str]) -> str | None:
    """구키만 있어 결정이 무시되는 conf 면 안내 1줄, 아니면 None.

    조건은 **구키 존재 + 신키 부재**다. 둘 다 있으면 신키가 결정을 공급하므로 구키 줄은 무해한
    잔존이고(이미 이주한 채택자) 안내가 잡음이 된다 — 동작이 바뀐 conf 만 알린다.
    """
    if (LEGACY_EXTERNAL_REVIEW_ENABLED_KEY in conf
            and ADDITIONAL_REVIEWER_ENABLED_KEY not in conf):
        return LEGACY_ENABLED_KEY_REMOVED
    return None


def knob_value_key(conf: dict[str, str], key: str) -> str | None:
    """노브 값을 공급하는 키 — **신키뿐**. 공급이 없으면 None.

    게이트 키와 규칙은 같고(구키 fallback 제거) **공급 판정만 다르다**: 게이트는 키 존재가 곧
    결정이지만 (`false` 도 결정이라 온보딩이 다시 묻지 않는다) 노브는 종전부터 빈 값을 미설정으로
    읽어 기본값으로 fail-soft 했다. 그 의미를 그대로 승계해 "비어 있지 않은 값"만 공급으로 본다.

    공급 판정은 값의 **존재**이지 형식이 아니다 — 신키가 깨진 값(비정수)을 공급하면 해소는
    엔진 기본값으로 간다(구키로 내려가지 않는다).
    """
    return key if conf.get(key, "").strip() else None


def _knob_raw(conf: dict[str, str], key: str) -> str:
    """공급 키의 원문 값 — 공급이 없으면 빈 문자열(호출부가 기본값으로 간다)."""
    supplier = knob_value_key(conf, key)
    return conf.get(supplier, "").strip() if supplier else ""


def legacy_knob_key_deprecation(key: str) -> str:
    """노브 구키 안내 1줄 — 게이트 안내와 같은 형태·같은 처방(값이 무시된다는 사실 포함)."""
    return (
        f"⚠ local.conf `{LEGACY_KNOB_KEYS[key]}` 는 더 이상 읽지 않는다(구키 제거) — "
        f"`{key}` 로 바꾸세요. 그 전까지 엔진 기본값을 씁니다."
    )


def legacy_knob_key_ignored(conf: dict[str, str], key: str) -> bool:
    """그 노브의 구키가 값을 담았는데 신키가 비어 있는가 — 값이 무시되는 상태.

    둘 다 값이 있으면 신키가 이기고 구키 줄은 무해한 잔존이라 알리지 않는다(이미 이주한 채택자에게
    잡음을 내지 않는다) — 게이트 축과 같은 판정 규칙이다.
    """
    return bool(conf.get(LEGACY_KNOB_KEYS[key], "").strip()) and not conf.get(
        key, "").strip()


def legacy_key_warnings(conf: dict[str, str]) -> list[str]:
    """이 conf 가 받아야 할 구키 안내 전부 — 게이트 1줄 + 값이 무시되는 노브마다 1줄.

    호출부가 축마다 따로 찍으면 새 노브가 안내 없이 조용히 무시되는 절반 배선이 생긴다. 깔때기를
    하나로 두어 "구키 때문에 동작이 달라지면 반드시 알린다"가 한 곳의 성질이 된다.
    """
    warnings = []
    enabled_warning = legacy_enabled_key_warning(conf)
    if enabled_warning:
        warnings.append(enabled_warning)
    warnings += [
        legacy_knob_key_deprecation(key)
        for key in LEGACY_KNOB_KEYS
        if legacy_knob_key_ignored(conf, key)
    ]
    return warnings


def disabled_gate_notice(conf: dict[str, str]) -> str:
    """게이트가 꺼져 있을 때의 안내 — 현재 상태를 **결정을 공급한 키 실명**으로 말한다.

    결정이 아예 없으면 키 이름 대신 그 사실을 말한다 — 없는 줄을 인용하지 않는다(고정 표기의 실패
    형상). 구키만 있는 conf 도 이제 "결정 없음" 이다(구키는 값을 공급하지 않는다) — 그 사실 자체는
    `legacy_enabled_key_warning` 이 별도 1줄로 알린다. 처방은 언제나 신키다.
    """
    key = enabled_decision_key(conf)
    state = (f"local.conf {key}={conf.get(key, '').strip()}" if key
             else "local.conf 에 opt-in 결정 없음")
    return (
        f"추가 리뷰어 비활성 — 코드 diff 외부 전송이 꺼져 있습니다 ({state}).\n"
        f"켜기: local.conf 에 `{ADDITIONAL_REVIEWER_ENABLED_KEY}=true` 추가, 또는 "
        "`board.py init` / `pm_update` 시 opt-in 프롬프트. "
        "미리보기는 `--dry-run`, 1회 강제는 `--force`."
    )


def _is_enabled(conf: dict[str, str]) -> bool:
    """설정된 추가 리뷰어로의 외부 전송·통상 과금에 대한 **지속 동의** 여부.

    한 번 켜면 그 프로필의 호출마다 비용을 다시 묻지 않는다 — 반복 질문은 게이트가 아니라 마찰이고,
    실제 상한은 라운드/wave 예산(무한 루프 차단)이 기계로 소유한다."""
    key = enabled_decision_key(conf) or ADDITIONAL_REVIEWER_ENABLED_KEY
    return conf.get(key, "false").strip().lower() in (
        "true", "1", "yes", "on")


def _reviewer_cmd(conf: dict[str, str]) -> str:
    return conf.get("reviewer_cmd", "").strip() or DEFAULT_REVIEWER_CMD


# ── 추가 리뷰어 대상 해소 (원자 tuple) ──────────────────────────────────────
#
# 사람 역할 이름은 **추가 리뷰어(additional reviewer)** 다 — 팀에 한 명 더 붙는 리뷰어라는 뜻이고,
# `external` 은 전송/격리/과금(외부로 나간다)에만 남는다. 그래서 설정 키는 opt-in 게이트
# `additional_reviewer_enabled` + 대상 `additional_reviewer.*` 이고, raw 파일 접두·모듈 파일 이름
# 같은 **이미 기록된 산출물에 박힌 기계 식별자만 external_review 그대로** 유지한다.
#
# 정상 경로의 대상은 위임과 동형의 **원자 tuple**(harness+model 동반 필수·reasoning 선택)이다.
# 모델을 고정하지 않는 자유 문자열(`reviewer_cmd`)은 "어느 모델이 봤는지"를 사후에 알 수 없어
# 라운드 장부·raw 감사가 거짓말을 하게 된다. 하위 호환으로 legacy 경로를 남기되 **unpinned-model**
# 로 크게 라벨링한다.
ADDITIONAL_REVIEWER_PREFIX = "additional_reviewer"
ADDITIONAL_REVIEWER_HARNESS_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.harness"
ADDITIONAL_REVIEWER_MODEL_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.model"
ADDITIONAL_REVIEWER_REASONING_KEY = f"{ADDITIONAL_REVIEWER_PREFIX}.reasoning"
LEGACY_REVIEWER_CMD_KEY = "reviewer_cmd"
ADDITIONAL_REVIEWER_KEYS: tuple[str, ...] = (
    ADDITIONAL_REVIEWER_HARNESS_KEY,
    ADDITIONAL_REVIEWER_MODEL_KEY,
    ADDITIONAL_REVIEWER_REASONING_KEY,
)

REVIEWER_SOURCE_STRUCTURED = "structured"
REVIEWER_SOURCE_LEGACY = "legacy"
# legacy 경로 라벨 — stderr provenance·dry-run·raw 헤더·raw 장부가 같은 낱말을 쓴다.
UNPINNED_MODEL_LABEL = "unpinned-model"
# legacy argv 에 `-m/--model` 표기가 없을 때 `_reviewer_model` 이 돌려주는 자리표시자.
LEGACY_UNSPECIFIED_MODEL = "default"
# 모델 축의 **예약 sentinel** — 엔진이 "이 실행은 모델이 고정되지 않았다"를 뜻하는 데 쓰는 낱말이다.
# 구조화 프로필은 정의상 모델을 고정한 tuple 이므로 이 낱말들을 값으로 받을 수 없다: 받으면 장부·
# raw 헤더·stderr 가 legacy 미고정 실행과 **글자 단위로 구분 불가**해져 "어느 모델이 봤는가"를
# 사후에 확정할 수 없다(고정했다는 선언과 기록이 서로 반대말을 한다).
RESERVED_MODEL_VALUES: frozenset[str] = frozenset(
    {LEGACY_UNSPECIFIED_MODEL, UNPINNED_MODEL_LABEL}
)

# 추가 리뷰어의 권한축은 **불변**이다 — 읽기 권위(code-reviewer)로 고정하고 설정으로 올릴 수 없다.
# 리뷰는 저장소를 고치지 않는다(고치는 것은 위임 developer 축의 일이다).
REVIEWER_ROLE = "code-reviewer"

# 실행 시점에만 정해지는 경로는 세 표면(dry-run·stderr provenance·raw)이 **같은 자리표시자**로
# 표시한다. 값이 실행마다 달라지는 토큰을 정체 문자열에 넣으면 세 표면이 서로 다른 문자열을
# 말하게 되고, 그러면 "같은 대상을 말했는가"를 기계가 대조할 수 없다. 실 argv 에는 그 자리에
# 실제 거울 경로/프롬프트 파일이 들어간다(opencode 만 해당).
REVIEWER_CWD_PLACEHOLDER = "<isolated-cwd>"
REVIEWER_PROMPT_FILE_PLACEHOLDER = "<prompt-file>"


class ReviewerTargetError(RuntimeError):
    """추가 리뷰어 대상 설정이 원자 tuple 계약을 어김 — 외부 송신 전에 중단해야 함."""


class ReviewerTarget(NamedTuple):
    """이번 실행이 실제로 부를 추가 리뷰어 — 세 표면이 공유하는 단일 해소 결과.

    `command` = 세 표면(dry-run·stderr provenance·raw 헤더)이 **같은 문자열로** 말하는 정체다.
    구조화 대상은 argv 를 그대로 렌더한 값이고, legacy 대상은 설정된 자유 문자열 그대로다.
    """

    source: str
    command: str
    harness: str | None = None
    model: str | None = None
    reasoning: str | None = None

    @property
    def structured(self) -> bool:
        return self.source == REVIEWER_SOURCE_STRUCTURED

    @property
    def name(self) -> str:
        """시간 프로필·진행신호·raw 파일명이 공유하는 정규화 키."""
        return reviewer_name(self.command)

    @property
    def ledger_model(self) -> str:
        """이 실행의 **모델 정체** — 네 표면(dry-run·stderr·raw 헤더·raw 장부)이 공유하는 단일 값.

        구조화 대상은 **명시 모델 그대로**다. 구조화 tuple 의 모델을 커맨드 문자열에서 역추론하지
        않는다(그러면 명시 모델이 argv 표기 규칙에 따라 `default` 로 퇴화할 수 있다).

        legacy `reviewer_cmd`는 임의 실행기까지 허용하는 opaque 문자열이라 `-m/--model` 토큰이
        실제 수신 모델을 고정한다는 스키마를 엔진이 보증할 수 없다. 따라서 표기 유무와 무관하게
        항상 `unpinned-model`로 기록한다. exact command는 별도 provenance에 그대로 남으므로 정보는
        사라지지 않으며, 모델 정체를 보증하려면 구조화 tuple을 써야 한다."""
        if self.structured:
            return self.model or ""
        return UNPINNED_MODEL_LABEL

    @property
    def profile_tail(self) -> str:
        """해소 tuple 의 출처/모델 축 — provenance 문자열의 공통 꼬리.

        모델 축은 장부·raw 헤더와 **같은 정체 값**(`ledger_model`)을 쓴다. legacy 라는 사실은
        `source=legacy` 가 이미 말하므로 여기서 모델 이름을 다르게 부를 이유가 없다."""
        if self.structured:
            return (f"source={self.source}, harness={self.harness}, "
                    f"model={self.model}, reasoning={self.reasoning}")
        return f"source={self.source}, model={self.ledger_model}"


def _structured_reviewer_argv(
    harness: str, model: str, reasoning: str | None, *,
    cwd: str, prompt_file: str,
) -> list[str]:
    """구조화 대상의 argv — 공용 드라이버 계약(pm_relay)만으로 조립한다.

    모델·reasoning 은 **항상 argv 에 명시**되고 권한축은 code-reviewer(읽기)로 고정된다. 격리 홈에
    복제된 사용자 config 기본값(모델·effort)이 실제 수신자를 바꿀 수 없는 이유가 이것이다 —
    명시 플래그가 config 기본값을 이긴다.

    codex/claude 는 프로세스 cwd(=격리 거울)를 그대로 쓰므로 실행마다 달라지는 토큰이 argv 에
    없다. opencode 만 subprocess cwd 를 무시해 `--dir` 이 필요하고 프롬프트도 `--file` 첨부라,
    그 두 자리는 자리표시자로 렌더한 뒤 실행 시점에 실 경로로 조립한다.
    """
    relay = _load_relay()
    if harness == "codex":
        return relay.build_codex_argv(model, reasoning, REVIEWER_ROLE)
    if harness == "claude":
        return relay.build_claude_argv(model, reasoning, REVIEWER_ROLE)
    return relay.build_opencode_argv(model, reasoning, REVIEWER_ROLE, cwd, prompt_file)


def legacy_reviewer_target(conf: dict[str, str]) -> ReviewerTarget:
    """구조화 키가 없는 형상의 대상 — 종전 `reviewer_cmd`/엔진 기본 커맨드 그대로."""
    return ReviewerTarget(source=REVIEWER_SOURCE_LEGACY, command=_reviewer_cmd(conf))


def resolve_reviewer_target(conf: dict[str, str]) -> ReviewerTarget:
    """`additional_reviewer.{harness,model,reasoning}` 을 **원자로** 해소한다.

    · 구조화 키가 **하나도 없으면**(키 자체가 conf 에 부재) legacy 경로(하위 호환·unpinned-model).
    · 구조화 키가 하나라도 **있으면**(값이 비어 있어도 선언이다) harness/model 동반 필수 —
      부분 지정은 fail-loud 다(조용한 폴백·절반만 반영된 대상 금지). 선언 판정을 값의 truthiness
      로 하면 `additional_reviewer.harness=` 처럼 **비운 채 선언한 부분 tuple** 이 legacy 로 조용히
      떨어져, 사용자가 지정한 것과 다른 대상으로 나간다. 그래서 기준은 **키 존재**다.
      `reasoning` 만 빈 값이 허용되고(선택 축·플래그 생략), 그것도 harness/model 이 온전할 때다.
    · model 이 **예약 sentinel**(`RESERVED_MODEL_VALUES` — 엔진이 '모델 미고정'을 뜻하는 낱말)이면
      거부한다. 값이 비어있지 않다는 것만으로 통과시키면 `additional_reviewer.model=default` 가
      "고정했다"는 선언과 함께 미고정 라벨을 장부에 박아, 감사가 자기 모순을 기록한다.
    · 같은 conf 에 비어있지 않은 `reviewer_cmd` 가 함께 있으면 대상이 둘이라 fail-loud 다(구조화
      키가 비어 있어도 **선언은 선언**이라 같은 판정을 받는다). 어느 쪽이 이기는지 추측해 외부로
      보내지 않는다.
    · harness/reasoning 값 검증은 공용 드라이버 계약(pm_relay)이 소유한다.

    모든 거부는 **송신·격리·라운드 예약·raw 예약 전**에 성립한다 — 호출부가 이 함수를 그 게이트들
    앞에서 부르고, 이 함수 자체는 부작용이 없다.
    """
    present = tuple(key for key in ADDITIONAL_REVIEWER_KEYS if key in conf)
    declared = {
        key: (conf.get(key) or "").strip()
        for key in ADDITIONAL_REVIEWER_KEYS
    }
    if not present:
        return legacy_reviewer_target(conf)

    legacy_cmd = (conf.get(LEGACY_REVIEWER_CMD_KEY) or "").strip()
    if legacy_cmd:
        given = ", ".join(present)
        raise ReviewerTargetError(
            f"추가 리뷰어 대상이 둘입니다 — 구조화 프로필({given})과 legacy "
            f"`{LEGACY_REVIEWER_CMD_KEY}={legacy_cmd}` 가 같은 local.conf 에 있습니다. "
            "어느 쪽이 이기는지 추측해 외부로 보내지 않습니다 — 하나만 남기세요"
            f"(권장: `{LEGACY_REVIEWER_CMD_KEY}` 를 지우고 {ADDITIONAL_REVIEWER_PREFIX}.* 유지)."
        )

    harness = declared[ADDITIONAL_REVIEWER_HARNESS_KEY]
    model = declared[ADDITIONAL_REVIEWER_MODEL_KEY]
    if not harness or not model:
        missing = ", ".join(
            key for key in (ADDITIONAL_REVIEWER_HARNESS_KEY, ADDITIONAL_REVIEWER_MODEL_KEY)
            if not declared[key]
        )
        raise ReviewerTargetError(
            f"추가 리뷰어 프로필이 불완전합니다({missing} 부재/빈 값) — harness/model 은 동반 "
            "필수인 원자 tuple 입니다. 부분 설정으로 조용한 기본값을 쓰지 않습니다. local.conf 에 "
            f"`{ADDITIONAL_REVIEWER_HARNESS_KEY}`·`{ADDITIONAL_REVIEWER_MODEL_KEY}` 를 함께 "
            f"설정하세요(선택: `{ADDITIONAL_REVIEWER_REASONING_KEY}`). 구조화 프로필을 쓰지 않을 "
            f"거면 {ADDITIONAL_REVIEWER_PREFIX}.* 줄을 지우세요(선언이 남아 있으면 legacy 로 "
            "조용히 떨어지지 않습니다)."
        )
    if model.lower() in RESERVED_MODEL_VALUES:
        raise ReviewerTargetError(
            f"`{ADDITIONAL_REVIEWER_MODEL_KEY}={model}` 은 예약 sentinel 입니다 — 엔진이 '모델 "
            f"미고정'을 표시하는 낱말이라(예약: {', '.join(sorted(RESERVED_MODEL_VALUES))}) "
            "구조화 프로필의 모델 값이 될 수 없습니다. 구조화 프로필은 정의상 모델을 고정한 "
            "tuple 인데 이 값을 받으면 raw 장부·raw 헤더·stderr provenance 가 legacy 미고정 실행과 "
            "구분되지 않아 '어느 모델이 이 판정을 냈는가'를 사후에 확정할 수 없습니다.\n"
            f"  · 실제 모델 이름을 적으세요(예: `{ADDITIONAL_REVIEWER_MODEL_KEY}=gpt-5.6-sol`).\n"
            f"  · 모델을 고정하지 않을 거면 {ADDITIONAL_REVIEWER_PREFIX}.* 줄을 지우고 "
            f"`{LEGACY_REVIEWER_CMD_KEY}` 를 쓰세요(그 경로가 `{UNPINNED_MODEL_LABEL}` 로 "
            "라벨링됩니다)."
        )

    relay = _load_relay()
    try:
        harness = relay.validate_harness(harness)
        reasoning = relay.validate_reasoning(
            harness, declared[ADDITIONAL_REVIEWER_REASONING_KEY] or None)
    except relay.HarnessContractError as exc:
        raise ReviewerTargetError(
            f"추가 리뷰어 프로필 값 오류 — {exc}"
        ) from exc

    command = shlex.join(_structured_reviewer_argv(
        harness, model, reasoning,
        cwd=REVIEWER_CWD_PLACEHOLDER, prompt_file=REVIEWER_PROMPT_FILE_PLACEHOLDER,
    ))
    return ReviewerTarget(
        source=REVIEWER_SOURCE_STRUCTURED, command=command,
        harness=harness, model=model, reasoning=reasoning,
    )


def _running_on_windows() -> bool:
    """실행 플랫폼 판정(진입점 인터프리터 표기·argv 분해 규칙) — 주입 가능한 좁은 seam."""
    return os.name == "nt"


def _split_reviewer_argv(reviewer_cmd: str) -> list[str]:
    """`reviewer_cmd` 를 **실행 플랫폼 규칙**으로 argv 분해한다 (분해 규칙은 board 공용 seam 소유).

    `shlex.split` 를 직접 부르면 POSIX 규칙이라 Windows 실행 경로의 ``\\`` 가 escape 로 소비된다 —
    ``C:\\Users\\pm\\codex.exe`` 가 ``C:Userspmcodex.exe`` 로 뭉개져 확정 기동 실패로 끝나고, 그
    실행은 "리뷰어를 찾을 수 없음"으로 흡수돼 교차검증이 조용히 환불된다. 분해 규칙 사본을 여기
    만들지 않고 `board.split_command_argv` 하나를 쓴다(같은 보호 규칙·같은 복원).
    """
    return _load_board().split_command_argv(
        reviewer_cmd, windows=_running_on_windows(),
    )


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
        argv = _split_reviewer_argv(reviewer_cmd)
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
    """추가 리뷰 **벽시계 백스톱**(초)을 `--timeout` > 리뷰어 프로필 순서로 해소한다.

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
    """라운드 상한 (local.conf additional_reviewer_round_limit·기본 `DEFAULT_ROUND_LIMIT`).

    비정수·음수는 기본값으로 fail-soft — 장부/노브 값이 깨졌다고 게이트를 벽돌로 만들지 않는다
    (음수 상한은 첫 라운드부터 무조건 차단이라 무의미)."""
    raw = _knob_raw(conf, ADDITIONAL_REVIEWER_ROUND_LIMIT_KEY)
    if not raw:
        return DEFAULT_ROUND_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_ROUND_LIMIT
    return value if value >= 0 else DEFAULT_ROUND_LIMIT


def _incomplete_round_limit(conf: dict[str, str]) -> int:
    """판정 없는 전송의 별도 재시도 상한(기본 2)."""
    raw = _knob_raw(conf, ADDITIONAL_REVIEWER_INCOMPLETE_ROUND_LIMIT_KEY)
    if not raw:
        return DEFAULT_INCOMPLETE_ROUND_LIMIT
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INCOMPLETE_ROUND_LIMIT
    return value if value >= 0 else DEFAULT_INCOMPLETE_ROUND_LIMIT


def _wave_budget(conf: dict[str, str]) -> int:
    """wave 총 라운드 예산 (local.conf additional_reviewer_wave_budget·기본 `DEFAULT_WAVE_BUDGET`).

    비정수·음수는 기본값으로 fail-soft — 라운드 상한 노브와 같은 규칙이다(깨진 노브가 게이트를
    벽돌로 만들지 않는다)."""
    raw = _knob_raw(conf, ADDITIONAL_REVIEWER_WAVE_BUDGET_KEY)
    if not raw:
        return DEFAULT_WAVE_BUDGET
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_WAVE_BUDGET
    return value if value >= 0 else DEFAULT_WAVE_BUDGET


def _review_rounds_max(conf: dict[str, str]) -> int:
    """수렴-형상 라운드 상한 (local.conf `review_rounds_max`·기본 2).

    비정수·음수는 기본값으로 fail-soft — 다른 예산 노브와 같은 규칙이다(깨진 노브가 게이트를
    벽돌로 만들지 않는다)."""
    raw = conf.get(REVIEW_ROUNDS_MAX_KEY, "").strip()
    if not raw:
        return DEFAULT_REVIEW_ROUNDS_MAX
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_REVIEW_ROUNDS_MAX
    return value if value >= 0 else DEFAULT_REVIEW_ROUNDS_MAX


def _diff_cap(conf: dict[str, str], estimate: str | None) -> int | None:
    """이 estimate 의 diff 총량 상한 — 모르는/빈 estimate 면 None(가드 off).

    채택자 override 는 `diff_cap.<estimate>` 한 키다(`diff_cap.small=500`). 비정수·음수는 엔진
    기본값으로 fail-soft 하고, **엔진이 모르는 estimate 는 상한을 지어내지 않는다** — 상한은
    스코프 규약의 함수라 선언되지 않은 구간에 보편값을 씌우면 상시 false-block 이 된다."""
    if not estimate:
        return None
    key = estimate.strip().lower()
    default = DEFAULT_DIFF_CAPS.get(key)
    if default is None:
        return None
    raw = conf.get(f"{DIFF_CAP_KEY_PREFIX}{key}", "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


# ── 라운드 상한 장부 ─────────────
# 추가 리뷰어 호출은 과금·전송 게이트라 라운드가 무한정 이어지면 비용이 쌓인다(PM 10차 실측: 한
# 게이트 클러스터 25라운드). PM 자의 라운드 집계를 기계 장부로 대체한다
# ([[mechanize-dont-instruct-llm]]): `--gate <T-NNNN>` 별로 실 전송 횟수(count)와 옛 승인 수위
# (acked_through — 폐지된 `--ack-rounds` 의 잔존 필드·구 장부 해석에만 쓴다)를 per-clone·
# git-ignored 장부에 기록하고, limit 을 넘기면 실행 전에 거부한다. 장부는 세션/클론 로컬 현상이라
# `.project_manager/.local/`(regression/livegate sidecar 와 동위·board 상태 아님)에 둔다. 경로는
# 호출 시점 REPO(module-level·monkeypatch 가능)에서 파생해 hermetic 테스트가 tmp 로 격리할 수 있게
# 한다(_tickets_dir 동형). 손상 장부는 빈 장부로 fail-soft(회귀해소·regression flag 동형).
#
# 장부는 두 축을 함께 싣는다:
#   · 게이트 축 — 최상위 키가 게이트 이름이고 값이 `_gate_entry` 스키마다. 상한 집계용
#     `records`(예약/마감 레코드) 옆에 **라운드별 산출** `rounds`(판정 rc·결함 수)를 append 한다.
#     `rounds` 는 지워지지 않는 이력이라 "그 라운드가 실결함을 냈는가"를 나중에도 기계로 확인할 수
#     있고, 수렴-형상 게이트(`_convergence_refusal`)의 판정 입력이기도 하다. 확인 전용 라운드
#     소비(`confirm_fix`)와 게이트 종결 시점의 **처분 선언**(`resolution` — `--resolve-gate`)도
#     같은 항목에 실린다. 처분 선언은 릴리즈 차단(`board.py livegate record`)이 읽는 절이고,
#     그 값의 해석은 board 의 공용 seam(`gate_resolution`)이 소유한다(여기선 구조만 보존).
#   · wave 축 — 예약 키 `wave`(`WAVE_SECTION_KEY`) 하나에 {started, spent} 를 둔다. 게이트 상한만
#     있으면 비용이 티켓 수 × 상한으로 확장되므로 전 게이트 합계를 이 예산이 묶는다. 두 축이 한
#     dict 를 공유하므로 **예약 키를 게이트 이름으로 쓰는 것은 기계로 막는다**(`_reserved_gate_error`
#     가 `--gate` 를 거르고 `_gate_entry` 가 그 키를 fail-loud 로 거부·집계 순회는 건너뛴다).
# 두 축의 적용 범위는 같다 — 장부를 타는 실행은 게이트가 해소된 실행(명시 `--gate` 또는 `--ticket`
# 자동 유도)뿐이라 wave 도 그 라운드만 센다. 게이트가 없는 실행(selector 에 티켓이 없거나 명시
# `--no-gate` opt-out)은 장부 밖이고 어느 예산도 쓰지 않으며, 그 사실을 loud 로 표기한다.


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
    이미 넘겨 **차단 중인 게이트**(rc 4·ack 대기)가 있고, 새 앵커에서 0 부터 다시 세면
    그 차단이 무통보로 열린다.
    """
    return REPO / ".project_manager" / ".local" / "review_rounds.json"


def _read_round_ledger_at(path: Path) -> dict:
    """지정 경로의 라운드 장부를 읽는다 — 없거나 손상 시 빈 dict(fail-soft)."""
    return _load_review_rounds().read_ledger(path)


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
    """라운드 장부와 shell 소비 잔여 표식을 fail-closed 순서로 원자 기록한다.

    tmp 이름에 pid+uuid 를 실어 동시 실행 간 고정 `.tmp` 충돌(카운트 유실·write 예외)을 없앤다
    os.replace 는 원자 rename — 독자는 옛 파일 또는 새 파일만 본다(부분기록 없음).
    확인·예약·저장의 원자성은 호출자가 `_round_ledger_lock()` 임계 구역으로 보장한다.

    두 파일은 한 rename 으로 묶을 수 없으므로 순서가 안전성이다: 새 장부가 blocked 면 표식을 먼저,
    clear 면 장부를 먼저 쓴다. 어느 사이에서 죽어도 false-clear 대신 false-block 만 남는다. livegate
    record/check 도 같은 락과 board 공용 판정을 써 stale clear overwrite 를 막는다."""
    path = _round_ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    board = _load_board()
    problems = board._unresolved_must_fix_data(ledger, path)
    livegate_flag = path.with_name("livegate.json")
    if problems:
        board._write_release_must_fix_marker(livegate_flag, problems)
    _load_review_rounds().write_ledger(path, ledger)
    if not problems:
        board._write_release_must_fix_marker(livegate_flag, problems)


def _load_file_lock():
    """공용 배타 파일락 seam(`file_lock.py`)을 같은 tools/ 에서 경로 로드한다.

    `_load_relay`·`_load_board` 와 같은 경로-앵커 로더이고, 그 둘처럼 **쓰는 경로에서만** 지연
    로드한다 — 이 seam 이 필요한 구간은 `--gate` 실행의 라운드 장부 예약/마감과 격리 산출물의
    접근 제한·정리뿐이라 나머지 경로(진단·denylist·재앵커를 deep-import 로 재사용하는
    pm_delegate 포함)를 seam 부재로 무너뜨리지 않는다. 로드 실패는 흡수하지 않고(fail-loud)
    캐시하되, 중앙 loader 가 소비 때마다 baked rev 를 재검증하므로 사본 skew 는 계속 표출된다.

    (지연/import-시점 선택의 근거는 **기능 축**이다 — "seam 없이도 살아야 하는 경로가 있나".
    board·worktree_pool 은 모든 변경 경로가 락을 지나 import 바인딩이 맞고, 여기는 아니다.
    fail-soft 경계 ratchet 은 그 선택의 *결과*를 계량할 뿐 근거가 아니다.)
    """
    lock_path = Path(__file__).resolve().parent / "file_lock.py"
    _require_engine_sibling(lock_path, "file_lock.py")
    return _load_module_from_path(
        lock_path, "file_lock.py", verifier=_verify_engine_rev, cache=True,
    )


# ── 공유 읽기 (등재 예외 · 형제 없이도 떠야 하는 판독) ──────────────────────
# 원자 교체 대상을 읽는 지점은 공용 seam 을 지난다([[T-0729]]) — 일반 `open` 리더가 하나라도
# 잡고 있으면 Windows 는 그 교체를 WinError 32 로 막는다. 다만 이 모듈의 판독은 위 로더 주석의
# 기능 축 그대로 **형제 없이도 떠야 한다**(진단·denylist·재앵커는 seam 이 필요한 `--gate` 구간
# 밖이고, pm_delegate 가 그 경로를 deep-import 로 재사용한다). 그래서 여기는 **등재된 예외**다 —
# seam 이 있으면 쓰고, 없거나 로드가 실패하면 사유를 남기고 종전 읽기로 진행한다.

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

    **부재/손상 로드**와 **구세대 사본**(로드는 되는데 그 함수가 없는 형상)을 함께 본다 — 쓰기
    축의 등재 예외가 `getattr(..., "atomic_replace", None)` 로 두 형상을 같이 받는 것과 같다.
    한쪽만 보면 부분 업그레이드 트리에서 AttributeError 로 죽는다.
    """
    global _shared_read_degraded
    try:
        seam = _load_file_lock()
    except Exception as exc:  # noqa: BLE001 — 부재/손상/혼합은 이 판독의 정상 입력이다.
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


def _restrict_to_owner(path: Path) -> None:
    """격리 산출물을 소유자 전용 접근으로 제한한다 (플랫폼 수단은 공용 seam 소유).

    `os.chmod(0o600/0o700)` 을 직접 부르지 않는 이유는 그 호출이 **Windows 에서 아무 제한도
    걸지 않기 때문**이다(실측 `S_IMODE`=0o666). 임시 홈에는 인증 파일 사본이, 샌드박스에는
    검토 대상 diff 원문이 들어가므로 "다른 사용자에게 읽히지 않는다" 는 이 도구의 보안 경계다 —
    수단(퍼미션/ACL)은 `file_lock` 이 소유하고 여기서는 보장만 요구한다.
    """
    _load_file_lock().restrict_to_owner(path)


def _force_rmtree(path: Path) -> None:
    """트리를 실제로 지운다 — 실패는 `OSError` 로 올라온다(공용 seam·조용한 잔재 금지)."""
    _load_file_lock().force_rmtree(path)


def _load_pm_delegate():
    """리뷰 블록 스키마와 회수 내용 규칙의 단일 진실(`pm_delegate.py`)을 경로 로드한다.

    `_load_relay`·`_load_board` 와 같은 경로-앵커 로더다. 스키마를 이 모듈이 다시 적으면 파서와
    프롬프트가 갈리고, 회수 판정 규칙을 여기서 따로 구현하면 리뷰 채널마다 규칙이 갈린다.
    """
    path = Path(__file__).resolve().parent / "pm_delegate.py"
    _require_engine_sibling(path, "pm_delegate.py")
    return _load_module_from_path(
        path, "pm_delegate.py", verifier=_verify_engine_rev, cache=True,
    )


def _load_ticket_rounds():
    """티켓 라운드 사이드카 공용 seam(`ticket_rounds.py`)을 지연 로드한다.

    라운드 파일의 경로 규약·이름 문법·순번 예약·판독·렌더는 그 모듈이 소유한다 — 회수 쓰기와
    리뷰 입력 조립이 각자 규약을 다시 적으면 두 표면이 갈린다.
    """
    path = Path(__file__).resolve().parent / "ticket_rounds.py"
    _require_engine_sibling(path, "ticket_rounds.py")
    return _load_module_from_path(
        path, "ticket_rounds.py", verifier=_verify_engine_rev, cache=True,
    )


def _load_review_rounds():
    """라운드 장부 read/write·예약·수렴 판정 공용 seam을 지연 로드한다."""
    path = Path(__file__).resolve().parent / "review_rounds.py"
    _require_engine_sibling(path, "review_rounds.py")
    return _load_module_from_path(
        path, "review_rounds.py", verifier=_verify_engine_rev, cache=True,
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


def _utc_now_iso() -> str:
    """장부 시각 표기 단일 소스 — UTC ISO-8601 (예약/마감/산출이 같은 형식을 쓴다)."""
    return _load_review_rounds().utc_now_iso()


def _target_rev_fingerprint(diff: str) -> str:
    """리뷰어에게 실제 전송할 diff의 content revision (`sha256:<hex>`).

    git HEAD 는 working tree의 staged/unstaged/untracked 변경을 식별하지 못한다. 프롬프트에 들어가는
    UTF-8 diff 바이트를 직접 해시하면 같은 미수정 diff의 동시 리뷰는 같은 좌표, 수정 뒤 리뷰는 새
    좌표를 얻는다."""
    return "sha256:" + hashlib.sha256(diff.encode("utf-8")).hexdigest()


def _as_int(value: object) -> int:
    """장부 필드를 int 로 강제 (손상/누락 → 0·fail-soft)."""
    return _load_review_rounds().as_int(value)


# ── wave 예산 절 (전 게이트 합계) ───────────────────────────────────────────

WAVE_SECTION_KEY = "wave"

# 장부 최상위에서 wave 절이 차지하는 **예약 키** — 게이트 이름으로 쓸 수 없다. 같은 키를 게이트가
# 쓰면 `_gate_entry` 와 `_wave_state` 가 한 항목을 서로 덮어써 게이트 count 는 저장되지 않고 wave
# 예산은 매 실행 0 으로 되살아난다(두 상한이 조용히 무력화). 게이트 이름은 형식 강제 대상이 아니라
# 자유 문자열이 실사용이므로(장부 실측: `wave4-b1`·`wave4-b3r2`) `T-NNNN` 강제 대신 **예약 키만**
# 거부한다 — 기존 이름은 하나도 깨지지 않고 충돌 가능성만 사라진다.
_RESERVED_LEDGER_KEYS: frozenset[str] = frozenset({WAVE_SECTION_KEY})

# 게이트 항목임을 알아보는 필드 — 예약 키 자리에 이런 항목이 들어 있으면 예약 키 도입 *이전에*
# 그 이름을 게이트로 쓴 장부다(교체 사실을 조용히 넘기지 않고 알린다).
_GATE_ENTRY_MARKERS: tuple[str, ...] = ("count", "acked_through", "records", "rounds")

_RESERVED_GATE_GUIDANCE = (
    "오류: `{flag} {gate}` 는 라운드 장부의 예약 키라 게이트 이름으로 쓸 수 없습니다 "
    "(예약 키: {keys}).\n"
    "  · 그 키는 장부 최상위에서 wave 예산 절이 씁니다 — 같은 이름의 게이트는 게이트 집계와 "
    "wave 예산이 서로 덮어써 라운드 상한·예산이 둘 다 조용히 무력화됩니다.\n"
    "  · 다른 게이트 이름으로 다시 실행하세요 (이름 형식 제약은 없습니다 — 예약 키만 거부)."
)


def _reserved_gate_error(gate: str | None, *, flag: str = "--gate") -> str | None:
    """게이트 이름이 장부 예약 키면 차단 안내를 돌려준다 (아니면 None).

    판정 입력은 이름 하나뿐이라 전송·장부 접근 **전에** 부를 수 있다 — 거부된 실행은 외부 전송도
    장부 변경도 남기지 않는다. 게이트 이름을 받는 표면이 여럿이라(`--gate`·`--resolve-gate`·
    `--fixed` 근거 게이트) 어느 플래그가 걸렸는지는 인자로 받는다 — 판정 규칙은 한 곳이다."""
    if gate is None or gate not in _RESERVED_LEDGER_KEYS:
        return None
    return _RESERVED_GATE_GUIDANCE.format(
        flag=flag, gate=gate, keys=", ".join(sorted(_RESERVED_LEDGER_KEYS)),
    )


def _new_wave_id() -> str:
    """wave 세대 식별자 — 리셋마다 새로 발급한다 (환불이 남의 세대를 깎지 않게)."""
    return uuid.uuid4().hex


def _wave_frame_corruption(raw: object) -> str | None:
    """wave 절 **자체**가 못 쓸 형상인지 (절 부재·정상이면 None).

    비-dict 값이나 예약 키를 차지한 옛 게이트 항목은 id/started 를 포함해 아무 좌표도 신뢰할 수
    없는 경우다 — 소비량은 라운드 이력에서 다시 센다."""
    if raw is None or (isinstance(raw, dict) and not any(
        marker in raw for marker in _GATE_ENTRY_MARKERS
    )):
        return None
    if not isinstance(raw, dict):
        return f"`{WAVE_SECTION_KEY}` 절 형식 오류({type(raw).__name__})"
    return f"예약 키 `{WAVE_SECTION_KEY}` 자리의 옛 게이트 항목"


def _wave_spent_corruption(spent: object) -> str | None:
    """`spent` 값이 신뢰할 수 없는지 — 비정수와 **음수**가 같은 축이다.

    음수를 정상 정수로 받아 `max(0, …)` 로 접으면 `spent: -1` 한 줄 편집이 승인 없이 hard 예산을
    되돌린다(무통보 재개방). 소비량은 셀 수 없는 값이지 0 이 아니므로 손상으로 판정한다."""
    if spent is None:
        return None
    if isinstance(spent, bool) or not isinstance(spent, int):
        return f"`{WAVE_SECTION_KEY}.spent` 값 손상({spent!r})"
    if spent < 0:
        return f"`{WAVE_SECTION_KEY}.spent` 음수({spent})"
    return None


def _wave_corruption_note(raw: object) -> str | None:
    """wave 절 손상 진단 한 곳 — 프레임 축과 spent 축을 합산한다 (정상·부재면 None).

    `_wave_state` 의 복구 판정과 호출부의 "복구값을 저장할까" 판정이 **같은 술어**를 봐야 한다 —
    갈리면 고친 값을 안 쓰거나(경고만 반복) 멀쩡한 장부를 거부 경로에서 덮어쓴다."""
    frame = _wave_frame_corruption(raw)
    if frame is not None:
        return frame
    return _wave_spent_corruption(raw.get("spent") if isinstance(raw, dict) else None)


def _recorded_round_outcomes(ledger: dict, *, since: str | None = None) -> int:
    """장부에 남은 라운드 산출 수 — `since`(UTC ISO 시각) 이후로 좁힐 수 있으면 좁힌다.

    손상된 `spent` 를 0 으로 접지 않기 위한 **데이터 근거 재계산**이다. 산출은 실제 전송된 라운드
    에만 남으므로(스폰 전 실패는 환불·미기록) 소비량의 하한이고, 범위를 좁힐 근거가 없으면
    (wave 시작 시각까지 손상) 전체를 세어 보수적으로 남는다 — 손상이 예산을 여는 방향으로는
    작동하지 않게 한다. 같은 형식의 UTC ISO 문자열은 사전순 비교가 곧 시간순이다."""
    total = 0
    for name in _gate_names(ledger):
        entry = ledger.get(name)
        rounds = entry.get("rounds") if isinstance(entry, dict) else None
        if not isinstance(rounds, list):
            continue
        for row in rounds:
            if not isinstance(row, dict):
                continue
            timestamp = row.get("ts")
            if since is None or (isinstance(timestamp, str) and timestamp >= since):
                total += 1
    return total


def _wave_state(ledger: dict) -> dict:
    """wave 절(`{id, started, spent}`)을 정규화해 돌려주고 장부에 심는다.

    절이 없는 **구세대 장부**나 손상 값은 새 wave(started 미기록·spent 0)로 정규화한다 —
    `_gate_entry` 와 같은 read→normalize→mutate→write 규약이라 호출부가 그 자리에서 고친 뒤
    저장하면 깨끗한 값이 기록된다.

    `id` 는 **세대 식별자**다. `--ack-wave` 리셋은 새 id 를 발급하므로, 리셋 전에 예약한 실행이
    나중에 환불하려 해도 세대가 달라 새 wave 의 예산을 깎지 못한다(예산 우회 차단). id 가 없는
    구세대 절에는 여기서 하나 발급해 심는다 — 이후 모든 예약/환불이 같은 좌표를 쓴다.

    손상(비-dict 값·옛 게이트 항목·비정수/음수 spent)은 **소비량을 0 으로 접지 않는다** — 예산은
    승인(`--ack-wave`)으로만 열리는 축이라, 손상을 리셋으로 대접하면 장부 한 줄 편집이 승인을
    대신하게 된다. 대신 장부에 남은 라운드 산출 이력으로 소비량을 다시 세고(범위는 wave 시작 시각
    이후, 그것마저 못 믿으면 전체) stderr 한 줄로 무엇을 왜 재계산했는지 고지한다."""
    raw = ledger.get(WAVE_SECTION_KEY)
    corruption = _wave_corruption_note(raw)
    state = raw if isinstance(raw, dict) and _wave_frame_corruption(raw) is None else {}
    started = state.get("started")
    started = started if isinstance(started, str) and started else None
    if corruption is None:
        spent = max(0, _as_int(state.get("spent")))
    else:
        spent = _recorded_round_outcomes(ledger, since=started)
        print(
            f"경고: 라운드 장부 {corruption} — wave 소비를 라운드 산출 이력으로 **재계산**했습니다 "
            f"(spent={spent}). 손상은 승인을 대신하지 않습니다 — 새 wave 로 시작하려면 "
            "`--ack-wave`.",
            file=sys.stderr,
        )
    wave_id = state.get("id")
    normalized = {
        "id": wave_id if isinstance(wave_id, str) and wave_id else _new_wave_id(),
        "started": started,
        "spent": spent,
    }
    ledger[WAVE_SECTION_KEY] = normalized
    return normalized


def _gate_names(ledger: dict) -> list[str]:
    """장부의 게이트 키만 정렬해 돌려준다 — 예약 키(`wave`)는 게이트가 아니다."""
    return sorted(key for key in ledger if key != WAVE_SECTION_KEY)


def _spend_wave_round(state: dict) -> None:
    """실 전송 1회를 wave 예산에서 쓴다 — 첫 전송이 wave 시작 시각을 찍는다.

    wave 경계는 명시 리셋(`--ack-wave`)까지 누적한다. 세션 자동 감지는 하지 않는다 — 세션 식별이
    이 도구 밖 개념이라 명시 리셋이 단순하고 예측 가능하다."""
    if not state.get("started"):
        state["started"] = _utc_now_iso()
    state["spent"] = max(0, _as_int(state.get("spent"))) + 1


def _refund_wave_round(state: dict, wave_id: str | None) -> bool:
    """예약과 **같은 세대**의 wave 소비만 되돌린다 (라운드 count 환불과 같은 조건).

    세대 확인이 없으면 `--ack-wave` 로 새로 연 wave 의 예산을 옛 실행의 실패가 깎아 예산이 조용히
    늘어난다(승인 1회로 예산 +N). 세대가 다르면 아무것도 하지 않고 False 를 돌려준다.

    소비가 0 으로 돌아가면 시작 시각도 지운다 — `started` 는 **첫 실 전송** 시각이라, 전송이 하나도
    없는 wave 에 시각만 남으면 조회 표가 있지도 않은 wave 를 진행 중으로 보여주고 다음 첫 전송이
    자기 시각을 못 찍는다(그 wave 의 시작이 남의 실패 시각이 된다)."""
    if wave_id is None or state.get("id") != wave_id:
        return False
    state["spent"] = max(0, _as_int(state.get("spent")) - 1)
    if not state["spent"]:
        state["started"] = None
    return True


def _reset_wave(ledger: dict) -> dict:
    """`--ack-wave` — ack 시점에 예산을 리셋한다(다음 전송이 새 시작 시각을 찍는다).

    새 세대 id 를 발급한다 — 리셋 전에 예약한 실행의 환불이 이 wave 의 예산을 깎지 못하게."""
    ledger[WAVE_SECTION_KEY] = {"id": _new_wave_id(), "started": None, "spent": 0}
    return ledger[WAVE_SECTION_KEY]


def _gate_entry(ledger: dict, gate: str) -> dict:
    """게이트 항목을 전송 레코드를 포함한 현 스키마로 정규화한다.

    항목이 없거나 손상(비-dict·비정수 필드)이면 0/0 으로 정규화해 저장한다 — 반환 dict 를 그 자리에서
    수정한 뒤 `_save_round_ledger(ledger)` 하면 깨끗한 값이 기록된다(read→normalize→mutate→write).

    `rounds`(라운드별 산출 이력)가 없는 **구세대 항목**은 빈 배열로 정규화된다 — 옛 장부를 그대로
    읽어 상한 판정을 이어가고, 새 산출은 그 뒤부터 쌓인다(마이그레이션 불요).

    예약 키(wave 절)를 게이트로 정규화하려는 호출은 **fail-loud** 다 — 그 자리에 게이트 항목을 쓰면
    같은 항목을 wave 예산이 덮어써 상한·예산이 둘 다 조용히 무력화된다. 정상 경로는 `_main` 이
    `--gate` 를 이미 거른 뒤라 여기 오지 않는다(불변식을 주석이 아니라 기계로 지킨다)."""
    return _load_review_rounds().normalize_gate_entry(
        ledger, gate, reserved_keys=tuple(_RESERVED_LEDGER_KEYS),
    )


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


def _reserve_round(
    entry: dict, record_id: str, *, wall_timeout_sec: int | None = None,
    target_rev: str | None = None,
) -> dict:
    """단조 sequence identity로 전송을 예약하고 레코드를 반환한다.

    **이 실행의 벽시계 백스톱을 `deadline` 으로 레코드에 새긴다** — 진행 중 예약이 언제까지
    살아 있을 수 있는지는 그 예약을 만든 실행의 timeout 이 정하는 값이지, 나중에 장부를 읽는
    호출자의 timeout 이 아니다(짧은 timeout 의 후속 호출이 긴 timeout 으로 도는 라운드를 stale
    로 접으면 수렴 상한을 넘겨 예약할 수 있다). 백스톱이 없는 실행은 만료 시각도 없다(null)."""
    return _load_review_rounds().reserve_round(
        entry, record_id, wall_timeout_sec=wall_timeout_sec,
        target_rev=target_rev,
    )


def _reservation_deadline(wall_timeout_sec: int | None) -> str | None:
    """예약이 살아 있을 수 있는 마지막 시각 (백스톱 없으면 None) — 기록·판정 공용 산식."""
    return _load_review_rounds().reservation_deadline(wall_timeout_sec)


def _refund_round(entry: dict, record_id: str) -> bool:
    """스폰 전 실패 예약만 제거한다; sequence는 재사용하지 않는다.

    반환값은 **실제로 환불했는지**다 — 같은 조건으로 wave 예산도 되돌려야 하는데, 되돌릴 예약이
    없는데 spent 만 깎으면 두 축의 소비가 갈린다."""
    return _load_review_rounds().refund_round(entry, record_id)


# ── 라운드별 산출 기록 ──────────────────────────────────────────────────────
# 라운드 count 만으로는 "그 라운드가 실결함을 냈는가"를 기계로 확인할 수 없어, 게이트 심도 대비
# 비용 적정성 판단이 PM 자기보고에 의존했다. 산출은 **새 파서 없이** 기존 판정 결과에서만 파생한다
# (rc 판정 + 이미 있는 must-fix 파서). 기록은 무조건이고, 셀 근거가 없으면 null 로 남긴다 —
# 기록 실패로 리뷰를 막지 않는다(hard 거부는 예산 축 하나뿐).


def _round_must_fix_items(result: dict) -> list[str] | None:
    """이번 라운드가 지적한 must-fix 항목 **텍스트** (셀 근거가 없으면 None · "없음" 표기는 []).

    판정이 무효한 라운드(실패·타임아웃·오염 진단)는 세지 않는다 — 판정 표면에서 무효화한 출력을
    산출 장부에서만 결함으로 세면 두 표면이 갈린다(`_round_has_verdict` 와 같은 규칙). 세는 대상은
    **회신 채널**(`answer`)뿐이다: 진행 로그에는 프롬프트와 diff 원문이 그대로 실려 있어 그것까지
    보면 리뷰 대상 코드의 문구가 결함 수로 둔갑한다.

    **섹션 부재와 "없음" 표기는 다르다.** 형식을 지킨 응답의 `must-fix: 없음` 은 리뷰어가 결함이
    없다고 *말한* 것이라 []이고, must-fix 섹션이 아예 없는 응답은 아무 말도 없던 것이라 None 이다
    (항목 근거 없이 반려만 있는 응답을 '결함 0건 반려'로 박제하면 비용 판단이 거짓이 된다).
    섹션 인식은 판정 파서와 **같은 정규식**(`_MUST_FIX_SECTION_RE`)을 쓰고, 항목 추출은 판정
    파서와 **같은 함수**(`_extract_must_fix_items`)를 쓴다 — 개수와 텍스트가 다른 규칙으로 갈리면
    확인 전용 라운드에 실리는 근거가 장부의 건수와 어긋난다."""
    if not _round_has_verdict(result):
        return None
    answer = result.get("answer")
    if not isinstance(answer, str):
        return None
    if not _MUST_FIX_SECTION_RE.search(answer):
        return None
    items = _extract_must_fix_items(answer)
    return [] if _is_none_items(items) else items


def _must_fix_count(result: dict) -> int | None:
    """이번 라운드가 지적한 must-fix 항목 수 (셀 근거가 없으면 None).

    셈의 입력은 항목 텍스트 추출(`_round_must_fix_items`)과 **같은 한 함수**다 — 건수와 텍스트가
    각자 파싱하면 장부의 `must_fix` 와 확인 전용 라운드 근거가 서로 다른 라운드를 말하게 된다."""
    items = _round_must_fix_items(result)
    return None if items is None else len(items)


def _round_outcome(result: dict, *, record: dict | None = None) -> dict:
    """리뷰 결과 → 라운드 산출(`{ts, started_at, target_rev, id, sequence, ...}`).

    verdict 는 이 실행이 돌려주는 **rc 판정**(0=통과·1=반려)이라 장부와 종료 코드가 갈리지 않는다.
    suggestions 는 응답에 suggestion 판별기가 아직 없어 null 로 시작한다 — 자리를 먼저 두어 파서가
    생기면 스키마 변경 없이 채워진다(파서 확장은 후속).

    `id`/`sequence`/`started_at`/`target_rev` 는 이 산출을 낸 **예약 레코드**의 좌표다. 같은 게이트에서
    여러 실행이 동시에 끝나면 append 순서나 완료 ts 만으로 어느 실행이 반려 뒤 시작했는지 확정할
    수 없어, 예약 identity 와 실제 검토 diff를 그대로 실어 라운드↔결과 연결을 잠근다(예약 레코드를
    못 찾은 장부 밖 직접 호출만 null)."""
    return {
        "ts": _utc_now_iso(),
        "id": (record or {}).get("id"),
        "sequence": (record or {}).get("sequence"),
        "started_at": (record or {}).get("started_at"),
        "target_rev": (record or {}).get("target_rev"),
        "verdict": determine_exit_code(result),
        "must_fix": _must_fix_count(result),
        "suggestions": None,
    }


def _append_round_outcome(entry: dict, outcome: dict) -> dict:
    """이미 만들어 둔 산출 레코드를 게이트 이력에 append 한다 (승인으로 비워지지 않는 축).

    산출 **계산**(응답 파싱·시각)은 호출부가 락 밖에서 끝낸다 — 임계 구역은 장부 read-modify-write
    만 담당한다(파싱이 길어져도 다른 게이트 실행을 붙잡지 않는다)."""
    return _load_review_rounds().append_round_outcome(entry, outcome)


# ── 수렴-형상 게이트 (라운드 장부 위의 판정) ────────────────────────────────
# 라운드 수만 보는 상한은 "라운드를 몇 번 썼나"만 막고 "닫히고 있나"는 묻지 않는다. 실측 두 형상이
# 그 공백이다: must_fix 가 줄지 않은 채 라운드만 늘거나(3→2→2), 라운드마다 새 지적이 늘어난다.
# 판정 입력은 **이미 있는 장부 필드**(`rounds[].must_fix`)뿐이고 LLM 판단은 0 이다.


def _recorded_must_fix_series(entry: dict) -> tuple[int | None, ...]:
    """예약 순번 순 must_fix 추이 — 기록된 라운드 산출만 (셀 근거가 없던 라운드는 None).

    나열 순서는 조회 표와 **같은 정렬**(`_ordered_round_outcomes`)이다 — append 순서는 완료
    순서라 동시 실행이 역순으로 끝나면 추이가 뒤바뀐다."""
    return _load_review_rounds().recorded_must_fix_series(
        entry, order_key=_load_board().round_outcome_order_key,
    )


def _format_must_fix_series(series: Sequence[int | None]) -> str:
    """must_fix 추이 표기 — 셀 근거가 없던 라운드는 '미상'(0 과 구분)."""
    return " → ".join("미상" if value is None else str(value) for value in series) or "없음"


def _inflight_reservations(entry: dict, *, wall_timeout_sec: int | None = None) -> int:
    """마감 기록(`finished_at`)이 없는 예약 수 — **지금 나가 있을 수 있는** 라운드.

    수렴 상한이 완료 산출만 세면 동시 실행 둘이 같은 잔여 슬롯을 함께 통과한다(예: 상한 3 설정에서
    2완료 + 2예약 동시 통과 → 4전송). 예약은 장부 임계 구역 안에서만 만들어지므로, 그 구역에서
    보는 미마감 예약을 상한에 더하면 창이 닫힌다.

    **미완 재시도 상한(`additional_reviewer_incomplete_round_limit`)과 역할이 다르다.** 그쪽은
    "판정을 못 낸 전송을 몇 번까지 다시 시도하나"(전송 횟수 축·승인 창 기준)이고, 이 수는
    "지금 몇 라운드가 이미 나가 있나"(수렴 축의 동시성)다. 같은 레코드를 보지만 묻는 질문이
    달라 한쪽이 다른 쪽을 대신하지 못한다 — 미완 상한(기본 2)은 상한 3 설정을 넘기는 4전송 창을
    막지 못하고, 이 수는 재시도 예산을 세지 않는다.

    전송이 확실히 없던 예약은 환불로 레코드 자체가 사라지므로(`_refund_round`) 여기 남지 않는다.
    남는 위험은 **회수 경로 없는 잠식**이다 — kill·전원차단으로 마감하지 못한 레코드는 영원히
    미마감이라, 연장 승인이 폐지된 수렴 축에서 상한을 영구 잠식한다(상한 3 설정에서 중단 2회면
    라운드가 1회로 줄고 안내는 "3라운드 썼다"고 오도한다). 그래서 만료 시각을 지난 예약은 세지 않는다 —
    그 시각을 지난 라운드는 하네스가 이미 죽였을 것이라 *실행 중일 수 없다*. 대가는 백스톱 직후의
    좁은 창(마감 write 직전 구간)뿐이고, 그건 회수 불능 잠식보다 작다(승인 축과 회수 축을 묶지
    않는다 — `--ack-wave` 는 예산 축이다).

    **만료 기준은 레코드가 소유한다**(`deadline` — 그 예약을 만든 실행의 백스톱). 지금 장부를 읽는
    호출자의 `wall_timeout_sec` 으로 재면, 짧은 timeout 의 후속 호출이 긴 timeout 으로 *실제 돌고
    있는* 라운드를 stale 로 접어 수렴 상한을 넘겨 예약한다. `deadline` 이 없는 구레코드만 종전
    규칙(호출자 백스톱 기준 `started_at` 대조)으로 보수 합산한다 — 시각을 못 읽는 레코드는 계속
    센다(판정 불능은 세는 쪽으로 틀린다)."""
    return _load_review_rounds().inflight_reservations(
        entry, wall_timeout_sec=wall_timeout_sec,
    )


def _convergence_round_usage(
    entry: dict, *, wall_timeout_sec: int | None = None,
) -> tuple[int, int]:
    """(완료 산출 수, 미마감 예약 수) — 상한 판정과 차단 안내가 **같은 수**를 쓴다."""
    return _load_review_rounds().convergence_round_usage(
        entry, wall_timeout_sec=wall_timeout_sec,
        order_key=_load_board().round_outcome_order_key,
    )


def _convergence_refusal(
    entry: dict, limit: int, *, wall_timeout_sec: int | None = None,
) -> str | None:
    """이번 라운드를 거부할 수렴-형상 사유 (통과면 None).

    세 조건을 이 순서로 본다:
      (b) **발산** — 직전 라운드 대비 must_fix 증가. 상한 도달을 기다리지 않는다(조기 차단).
      (a)(c) **상한** — 기록된 라운드 수 + **진행 중 예약**(벽시계 백스톱 안의 것만)이 상한 이상.
          마지막 must_fix 가 0 이 아니면(미해소·'미상' 포함) 사유를 나눠 표기한다. 미상을 해소로
          접지 않는 건 보수 방향이다 — 셀 수 없던 라운드를 '0건'으로 읽으면 발산 형상이 통과한다.
    """
    return _load_review_rounds().convergence_refusal(
        entry, limit, wall_timeout_sec=wall_timeout_sec,
        order_key=_load_board().round_outcome_order_key,
    )


# ── 확인 전용 라운드의 자격·근거 (최신 반려 must_fix) ────────────────────────
# 리뷰어 세션은 라운드마다 fresh 다 — 장부에 "몇 건이었나"만 있으면 확인 전용 라운드는 *무엇을*
# 확인하는지 모른 채 통과를 선언할 수 있다. 이 절이 두 축을 한 함수로 소유한다:
#   (a) **자격** — **최신 완료 라운드의 판정이 반려(rc≠0)** 인 게이트에서만 예외를 연다. 첫 라운드나
#      전건 통과 게이트의 `--confirm-fix` 는 확인할 지적이 없어 근거 없는 통과 판정 채널이 되고,
#      '과거에 반려가 하나라도 있었나'로 물으면 **반려 → 통과로 이미 닫힌 게이트**에도 옛 지적을
#      근거로 과금 라운드가 열린다(수렴 축의 유일한 예외가 상시 예외가 된다).
#   (b) **근거** — 그 최신 반려 라운드의 must_fix 항목 **텍스트**를 프롬프트에 싣는다. 텍스트는 마감
#      시점에 예약 레코드(`records[].must_fix_items`)가 보관하고, 구세대 라운드처럼 보관분이
#      없으면 건수 + 재구성 안내로 떨어진다(무근거 통과보다 낫다).
# 자격과 근거가 **같은 함수**를 쓰는 이유는 갈림 방지다 — 따로 두면 "자격은 있는데 실을 근거가
# 없다"(또는 그 반대)가 조용히 생긴다.


def _latest_round_outcome(entry: dict) -> dict | None:
    """예약 순번 순 **마지막 완료 라운드** 산출 (기록이 없으면 None).

    순서는 조회 표·수렴 추이와 같은 정렬(`_ordered_round_outcomes`)이다 — append 순서는 완료
    순서라 동시 라운드가 역순으로 끝나면 '최신'이 뒤바뀐다."""
    return _load_review_rounds().latest_round_outcome(
        entry, order_key=_load_board().round_outcome_order_key,
    )


def _has_recorded_verdict(entry: dict, outcome: dict) -> bool:
    """그 산출을 낸 **예약 레코드**가 실제 리뷰 판정을 남겼는가 (`records[].verdict` 가 참).

    산출의 `verdict` 는 `determine_exit_code()` 의 rc 라 **timeout·하네스 실패·판정 불명확도
    1** 이다 — 그 값만 보면 리뷰어가 아무 판정도 내지 않은 라운드가 '반려'로 세어져 확인 전용
    라운드(과금 예외)의 자격이 선다. 마감 시점에 예약 레코드가 따로 새기는
    `verdict = _round_has_verdict(result)`(통과 또는 must-fix 선언이 실재했는가)를 함께 본다.

    연결 좌표는 예약 identity(`id`)다(`_recorded_must_fix_texts` 와 같은 규칙). 레코드를 못
    찾거나 그 축이 없는 구세대 기록은 **자격 없음**이다 — 과금 축은 못 세우는 쪽이 보수 방향이다.
    """
    round_id = outcome.get("id")
    if not round_id:
        return False
    for row in entry.get("records") or []:
        if isinstance(row, dict) and row.get("id") == round_id:
            return bool(row.get("verdict"))
    return False


def _is_rejection(entry: dict, outcome: dict) -> bool:
    """그 산출이 **실제 리뷰 판정으로서의** 반려(rc≠0)인가.

    판정 미상(기록 없음·손상)은 반려로 세지 않는다 — 확인 전용 라운드는 과금 라운드라 자격을 못
    세우는 쪽이 보수 방향이다(board 강등 판정이 미상 게이트를 활성으로 세지 않는 것과 같은 방향).
    rc 만으로는 timeout·하네스 실패·판정 불명확이 반려와 구분되지 않으므로 예약 레코드의 실제
    verdict 존재(`_has_recorded_verdict`)를 함께 요구한다."""
    verdict = outcome.get("verdict")
    if not (isinstance(verdict, int) and not isinstance(verdict, bool) and verdict != 0):
        return False
    return _has_recorded_verdict(entry, outcome)


def _latest_verdict_label(entry: dict) -> str:
    """최신 완료 라운드의 판정 표기 — 거부 안내가 '무엇을 보고 막았는지' 그대로 말한다.

    rc 는 비통과인데 실제 리뷰 판정이 없던 라운드(timeout·하네스 실패·판정 불명확)는 그 사실을
    함께 적는다 — 안 적으면 안내가 "최신 판정은 1(비통과)"이라고 말해 놓고 반려 자격은 없다고
    막는 모순으로 읽힌다."""
    outcome = _latest_round_outcome(entry)
    if outcome is None:
        return "라운드 기록 없음"
    label = _format_round_verdict(outcome.get("verdict"))
    verdict = outcome.get("verdict")
    if (isinstance(verdict, int) and not isinstance(verdict, bool) and verdict != 0
            and not _has_recorded_verdict(entry, outcome)):
        return f"{label} · 리뷰 판정 없음(timeout·하네스 실패·판정 불명확)"
    return label


def _recorded_must_fix_texts(entry: dict, outcome: dict) -> list[str]:
    """그 산출을 낸 **예약 레코드**가 보관한 must_fix 항목 텍스트 (미보관·손상이면 []).

    산출(`rounds`)과 예약(`records`)의 연결 좌표는 예약 identity(`id`)다 — 동시 라운드에서도
    어느 텍스트가 어느 라운드의 것인지 확정된다."""
    round_id = outcome.get("id")
    if not round_id:
        return []
    for row in entry.get("records") or []:
        if not isinstance(row, dict) or row.get("id") != round_id:
            continue
        items = row.get("must_fix_items")
        if not isinstance(items, list):
            return []
        return [str(item).strip() for item in items if str(item).strip()]
    return []


def _must_fix_items_on_surface(
    items: Sequence[str], surface_finding_ids: set[str],
) -> list[str]:
    """must-fix 항목 중 **확인 가능한** finding 을 가리키는 것만 남긴다.

    입력 집합은 회수 대상 티켓의 confirmable 목록이다(회수 거부 절 제외 · PM `rejected` 제외 —
    배제 규칙은 리뷰 절 시드와 같은 엔진 함수가 소유한다).

    항목의 ID 표기 해석은 회수 엔진과 같은 함수(`collect_review_finding_ids`)를 쓴다 — 두 자리가
    다른 규칙으로 읽으면 근거에 실린 ID 와 회수 게이트가 받는 ID 가 갈린다. ID 를 하나도 담지
    않은 항목(구세대 자유 산문)은 판정할 근거가 없어 그대로 둔다.
    """
    delegate = _load_pm_delegate()
    kept: list[str] = []
    for item in items:
        ids = delegate.collect_review_finding_ids(
            item, delegate.EXTERNAL_REVIEW_ROLE,
        )
        if not ids or (ids & set(surface_finding_ids)):
            kept.append(item)
    return kept


def _confirm_fix_offsurface_ids(
    entry: dict, surface_finding_ids: set[str] | None,
) -> list[str]:
    """최신 반려 라운드 must_fix 가 가리키는 ID 중 판정 표면 **밖**의 것 (거부 라운드 진단).

    자격 거부 안내가 "확인할 반려가 없다"가 아니라 "그 라운드 산출이 회수 거부돼 판정 표면에
    없다"고 정확히 말하게 하는 입력이다.
    """
    if surface_finding_ids is None:
        return []
    outcome = _latest_round_outcome(entry)
    if outcome is None or not _is_rejection(entry, outcome):
        return []
    delegate = _load_pm_delegate()
    referenced: set[str] = set()
    for item in _recorded_must_fix_texts(entry, outcome):
        referenced |= delegate.collect_review_finding_ids(
            item, delegate.EXTERNAL_REVIEW_ROLE,
        )
    return sorted(referenced - set(surface_finding_ids))


def _confirm_fix_evidence(
    entry: dict, *, surface_finding_ids: set[str] | None = None,
) -> str | None:
    """확인 전용 라운드 프롬프트에 실을 근거 블록 — **최신 완료 라운드가 반려가 아니면
    None(자격 없음)**. 근거는 그 최신 반려 라운드의 must_fix 다(자격과 같은 산출 1건).

    `surface_finding_ids`(회수 대상 티켓이 있는 실행)가 주어지면 **표면 밖 지적을 근거에서
    뺀다** — 회수 거부된 라운드의 ID 와 PM 이 `rejected` 로 판정한 ID 다. 그 ID 를 근거로 실으면
    확인 라운드가 규칙대로 `confirmations` 에 담아 회수 게이트에 다시 걸린다(엔진이 스스로
    함정을 지시하는 경로). 배제 규칙은 리뷰 절 시드와 같은 엔진 함수가 소유한다. 남는 지적이
    없으면 자격 없음이다 — 과금 라운드는 못 세우는 쪽이 보수 방향이다.
    """
    outcome = _latest_round_outcome(entry)
    if outcome is None or not _is_rejection(entry, outcome):
        return None
    sequence = _round_sequence(outcome)
    header = _CONFIRM_FIX_EVIDENCE_HEADER.format(
        round=f"#{sequence}" if sequence is not None else "직전 반려 라운드")
    items = _recorded_must_fix_texts(entry, outcome)
    if items and surface_finding_ids is not None:
        items = _must_fix_items_on_surface(items, surface_finding_ids)
        if not items:
            return None
    if not items:
        return header + _CONFIRM_FIX_EVIDENCE_UNRECORDED.format(
            count=_format_round_field(outcome.get("must_fix")))
    listed = "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1))
    return f"{header}{listed}\n\n"


def _gate_confirm_fix_evidence(
    gate: str, *, surface_finding_ids: set[str] | None = None,
) -> str | None:
    """게이트 장부를 **읽기 전용**으로 열어 확인 전용 라운드 근거 블록을 만든다 (없으면 None).

    프롬프트 조립은 예약(임계 구역)보다 앞이라 여기서 한 번 더 읽는다 — 장부를 고치지 않으므로
    사본(`dict(ledger)`)에 정규화한다(조회 표 `render_rounds_report` 와 같은 규약). 앵커 승계
    규칙도 예약 경로와 같게 본다: PM 홈 장부에 그 게이트가 아직 없으면 legacy(diff 앵커) 장부를
    읽는다 — 예약 시점에 승계될 항목을 프롬프트만 못 보면 "자격은 있는데 근거가 빈" 라운드가 난다.
    """
    ledger = _load_round_ledger()
    if gate not in ledger:
        legacy_path = _legacy_round_ledger_path()
        if legacy_path != _round_ledger_path():
            legacy = _read_round_ledger_at(legacy_path)
            if gate in legacy:
                ledger = legacy
    return _confirm_fix_evidence(
        _gate_entry(dict(ledger), gate), surface_finding_ids=surface_finding_ids,
    )


def _spend_confirm_fix(entry: dict) -> None:
    """확인 전용 라운드 1회를 장부에서 소비한다 (게이트당 1회 예외의 유일한 기록 지점)."""
    entry["confirm_fix"] = max(0, _as_int(entry.get("confirm_fix"))) + 1


def _refund_confirm_fix(entry: dict) -> None:
    """전송이 확실히 없던 실행의 확인 전용 라운드 quota 를 되돌린다 (라운드 count 환불과 동조)."""
    entry["confirm_fix"] = max(0, _as_int(entry.get("confirm_fix")) - 1)


# 승인 고지 — 같은 승인이라도 **재개된 실행**과 **남은 축이 막아 거부된 실행**의 문구가 달라야
# 한다. rc 4 로 끝나는 실행이 "재개"를 말하면 stderr 가 종료 코드와 어긋난 loud 오보가 된다
# (승인 자체는 저장되므로 다음 실행이 이어받는다).
_APPROVAL_RESUMED_VERB = "재개"
_APPROVAL_REFUSED_VERB = "기록(이번 실행은 남은 축이 막아 거부)"


def _approval_notes(
    *, wave_reset: bool, wave_budget: int, resumed: bool,
) -> list[str]:
    """이번 실행이 적용한 승인의 stderr 고지 문구 (적용분이 없으면 빈 목록).

    승인 축은 wave 예산 하나뿐이다 — 라운드 연장 승인(`--ack-rounds`)은 폐지됐다."""
    verb = _APPROVAL_RESUMED_VERB if resumed else _APPROVAL_REFUSED_VERB
    notes: list[str] = []
    if wave_reset:
        notes.append(f"wave 예산 승인 {verb}: spent 리셋 (예산 {wave_budget}).")
    return notes


# ── 예산 게이트: 확인→예약 (격리·전송보다 **앞**) ───────────────────────────
# 두 예산(게이트 라운드 상한·wave)의 확인과 예약은 리뷰어 격리 컨테이너 생성보다 먼저다. 이미
# 상한에 닿은 호출은 리뷰어를 스폰하지 않더라도 격리를 먼저 만들면 저장소 tracked 사본과 홈
# 인증/설정 사본을 디스크에 만들었다 지우는 실 작업을 한 번 수행한다 — "차단된 호출은 아무것도
# 하지 않는다"가 거짓이 된다(**남은 게 없다 ≠ 격리 seam 에 들어간 적 없다**). 정리가 끝까지
# 성공한다는 보장도 없어(정리 실패는 loud 경고로 남는다) 전송도 못 할 실행이 사본을 남길 수 있다.
#
# 순서를 뒤집는 대가는 하나뿐이다: 예약 뒤·스폰 전에 끝나는 구간(격리 생성·리뷰어 환경 준비)이
# 생긴다. 그 구간은 외부 전송이 확실히 없으므로 마감 시점의 `started=False` 환불과 **같은 조건**
# 이고, 같은 기계(`_refund_reserved_round`)를 같은 락 아래에서 재사용해 되돌린다
# (`_release_round_reservation`). 구간 전체의 소유는 `_PreSpawnReservation` 하나가 진다 — 어떤
# 예외로 나가든 한 번만 환불하고, 스폰 직전에 소유권을 마감 경로로 넘긴다.


class RoundBudget(NamedTuple):
    """이번 실행의 예산 판정 결과 — 거부 rc 또는 예약 identity.

    `refused_rc` 가 채워진 결과는 **그 자리에서 끝나는 실행**이다(격리·raw·스폰·과금 없음).
    통과한 실행 중 `--gate` 지정분만 예약 좌표(gate·round_id·sequence·wave_id)를 갖는다 —
    `--gate` 없는 실행은 종전대로 장부 밖이라 세 값이 비어 있는 통과 결과다.

    `sequence` 는 **예약 시점** 순번이다: 마감 때 레코드를 되찾아 읽으면 그 사이 집계 창이
    비워진 실행만 순번을 잃는다. `wave_id` 는 예약 시점 wave 세대라
    환불이 그 세대에만 유효하다(리셋된 새 wave 의 예산을 옛 실패가 깎지 못한다).

    `confirm_fix_spent` 는 이번 예약이 확인 전용 라운드 quota 를 썼는지다 — 환불도 같은 조건이라
    (전송 0 이면 예산을 안 먹는다) 라운드 count·wave 와 **한 축으로** 되돌린다.

    `confirm_fix_evidence` 는 확인 전용 라운드가 실을 직전 must_fix 근거 블록이다 — **자격을
    판정한 그 스냅샷**에서 나온다. 프롬프트가 장부를 따로 읽으면 두 read 사이에 끼어든 라운드
    때문에 "자격은 통과했는데 근거 블록이 빈" 라운드가 난다(반대 방향도 같다). 자격이 열린
    실행에서만 채워진다."""

    refused_rc: int | None = None
    gate: str | None = None
    round_id: str | None = None
    sequence: int | None = None
    started_at: str | None = None
    target_rev: str | None = None
    wave_id: str | None = None
    confirm_fix_spent: bool = False
    confirm_fix_evidence: str | None = None

    @property
    def reserved(self) -> bool:
        """되돌리거나 마감할 예약이 실제로 있는지."""
        return self.gate is not None and self.round_id is not None


class GateDerivation(NamedTuple):
    """게이트 유도 결과 — 거부 안내 / 유도 고지. **출력은 호출부가** 제 자리에서 한다.

    고지를 여기서 찍지 않는 이유는 자리 때문이다: 유도는 인자 파싱 직후여야 하고(뒤따르는 게이트
    검사·프롬프트·예약이 그 값을 쓴다) 고지는 stderr 첫 줄 provenance 뒤여야 한다."""

    refusal: str | None = None
    notice: str | None = None


def _derive_gate_from_ticket(args) -> GateDerivation:
    """`--ticket` 실행의 게이트를 티켓으로 유도한다 (거부 사유가 있으면 그 안내를 돌려준다).

    유도는 **리뷰 실행면에서만** 한다 — 조회(`--rounds-report`)와 기록(`--resolve-gate`) 면은
    같은 `--gate` 를 각각 '한 게이트만 보기' 필터와 '무시 목록'으로 읽어, 유도값이 사용자가 주지
    않은 선택으로 둔갑한다(그 두 면은 전송도 예약도 하지 않아 여기서 닫을 구멍이 없다).

    명시 `--gate` 가 항상 이기고, `--no-gate` 는 유도를 끄는 명시 opt-out 이다. 리뷰
    실행면에서 둘을 함께 주면 한 실행이 "기록한다"와 "기록하지 않는다"를 동시에
    뜻하므로 거부한다. 조회·처분면에서는 둘 다 필터/무시 입력이라 충돌이 아니다.

    유도한 이름도 **예약 키 검사를 지난다** — `_gate_entry` 는 예약 키를 hard 예외로 거부하므로,
    거르지 않으면 자유 문자열 티켓 하나가 예약 임계 구역에서 크래시로 나타난다(불변식을 주석이
    아니라 기계로 지킨다)."""
    opt_out = getattr(args, "no_gate", False)
    # 조회·처분면을 **충돌 판정보다 먼저** 가른다. 이 두 표면은 실 리뷰를
    # 전송·예약하지 않아 `--gate`는 필터/무시 입력, `--no-gate`는 무시 경고 대상이다.
    # 여기서 닫아야 호출부의 각 표면 무시-플래그 처리까지 정상 도달한다.
    if args.rounds_report or args.resolve_gate:
        return GateDerivation()
    if args.gate and opt_out:
        return GateDerivation(refusal=_GATE_OPT_OUT_CONFLICT_GUIDANCE.format(gate=args.gate))
    if args.gate or opt_out or not args.ticket:
        return GateDerivation()
    reserved = _reserved_gate_error(args.ticket, flag="--ticket")
    if reserved is not None:
        return GateDerivation(refusal=reserved)
    args.gate = args.ticket
    template = (
        _GATE_DERIVED_DRY_RUN_NOTICE if getattr(args, "dry_run", False)
        else _GATE_DERIVED_NOTICE
    )
    return GateDerivation(notice=template.format(gate=args.gate))


def _reserve_round_budget(
    args, conf: dict[str, str], *, wall_timeout_sec: int | None = None,
    target_rev: str | None = None, surface_finding_ids: set[str] | None = None,
) -> RoundBudget:
    """라운드 상한·wave 예산을 한 임계 구역에서 확인하고 이번 전송을 예약한다.

    여기까지 왔으면 dry-run·빈-diff·비활성 no-op·egress 차단을 모두 통과해 *실 외부 전송*이
    일어난다 — 그것들은 전송이 없어 라운드가 아니므로(카운트 제외) 이 앞의 조기 return 뒤에
    게이트를 둔다. 게이트가 해소된 실행에서만 per-gate 장부를 대조한다(게이트 없는 실행 = 상한
    대상 밖).

    게이트가 있는지는 이 함수보다 앞에서 정해진다 — `--ticket` 실행은 `_derive_gate_from_ticket`
    이 게이트를 유도하고, selector 에 티켓이 없으면 실 전송 seam 이 명시 `--no-gate`를
    요구한다. 따라서 여기 게이트 없이 오는 실행은 모두 명시 opt-out 이며, loud 표기 뒤
    회계 없이 지난다(조용한 무기록은 반려 must-fix 를 릴리즈 차단 표면에서 지운다).

    MF-A(예약-후-환불): count 를 *호출 전에* +1 예약한다 — 타임아웃·비정상 종료도 프롬프트가 이미
    전송·과금됐을 수 있는데 성공시에만 세면 반복 타임아웃으로 상한을 무한 우회한다. 외부 프로세스가
    확실히 시작되지 않은 경우(스폰 실패·started=False, 그리고 예약 뒤 스폰 전 중단)만 환불한다.
    MF-B(원자성): 확인→예약→저장을 `_round_ledger_lock()` 한 임계 구역으로 묶어 동시 실행이 같은
    잔여 슬롯을 통과 못 하게 한다. 초과면 리뷰어 호출 전에 거부(전용 rc·과금 없음).

    상한 축은 셋이고 이 순서로 본다: **수렴-형상**(장부 must_fix 추이) → 판정/미완 라운드 상한 →
    wave 예산. 수렴 축을 먼저 보는 이유는 그쪽이 더 좁고 처방이 구체적(재설계·분할)이기 때문이다.
    유일한 예외는 `--confirm-fix`(게이트당 1회 확인 전용 라운드)이고, 그 소비도 이 임계 구역이
    기록한다. wave 승인(`--ack-wave`)은 **먼저 적용한 뒤** 남은 축을 다시 본다 — 적용해 놓고 저장
    없이 되돌아가면 PM 이 적용한 승인이 조용히 사라진다.

    `wall_timeout_sec` 는 이 실행이 해소한 하네스 벽시계 백스톱이다 — 진행 중 예약 합산의 회수
    기준으로 그대로 넘긴다(그 시각을 넘긴 미마감 예약은 실행 중일 수 없다). 확인 전용 라운드의
    근거 블록도 **이 임계 구역이 만들어** 결과에 실어 보낸다: 자격을 판정한 스냅샷과 프롬프트에
    실리는 근거가 같은 read 에서 나와야 둘이 갈리지 않는다."""
    if not args.gate:
        # `--confirm-fix` 는 여기 오지 못한다 — main 이 부작용 0 지점에서 rc 거부한다(게이트당
        # 1회 회계가 장부 항목 없이는 성립하지 않는다). wave 승인만 무시 경고로 흡수한다:
        # 그쪽은 "예산 리셋"이라 게이트가 없으면 리셋할 대상 자체가 없고 실행은 정상이다.
        if args.ack_wave:
            print("경고: --ack-wave 는 --gate 와 함께 써야 합니다 (게이트 단위 장부) — 무시.",
                  file=sys.stderr)
        # 전송이 확정된 구간이지만 **아직 스폰 전**이다(격리 생성이 남아 있다) — 그래서 문구는
        # 조건형이고("이 실행이 전송되면"), 확정 표기는 실행이 끝난 뒤 판정 블록의 게이트 줄이
        # 낸다. 그래도 여기서 한 번 말하는 이유는 침묵이 이 함정의 본체였기 때문이다(장부 0건·
        # 릴리즈 차단 미도달) — 전송 전에 멈출 기회를 PM 에게 준다.
        print(_UNACCOUNTED_OPT_OUT_NOTICE, file=sys.stderr)
        return RoundBudget()

    limit = _round_limit(conf)
    incomplete_limit = _incomplete_round_limit(conf)
    wave_budget = _wave_budget(conf)
    rounds_max = _review_rounds_max(conf)
    confirm_fix = bool(getattr(args, "confirm_fix", False))
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
            # 손상 복구(재계산된 spent)는 거부되는 실행에서도 저장한다 — 안 그러면 손상값이
            # 남아 매 실행 같은 경고를 반복하고 장부가 계속 거짓말을 한다. 저장 판정은 정규화와
            # **같은 술어**를 쓴다.
            wave_repaired = _wave_corruption_note(ledger.get(WAVE_SECTION_KEY)) is not None
            wave = _wave_state(ledger)
            # wave 승인 적용 (판정 전). **고지는 아직 하지 않는다** — 재개인지 거부인지는
            # 남은 상한을 다시 본 뒤에야 정해지고, 거부되는 실행이 "재개"를 말하면 rc 4 와
            # 어긋난 loud 오보가 된다.
            wave_reset = False
            if args.ack_wave:
                wave = _reset_wave(ledger)
                wave_reset = True
            approved = wave_reset

            def announce(resumed: bool) -> None:
                for note in _approval_notes(
                    wave_reset=wave_reset, wave_budget=wave_budget, resumed=resumed,
                ):
                    print(note, file=sys.stderr)

            # (1) 수렴-형상 — 장부 must_fix 추이로 "닫히고 있나"를 본다. `--confirm-fix` 는 이 축의
            #     유일한 예외(게이트당 1회)이고, 그 소비는 통과 직전에 기록한다.
            convergence = _convergence_refusal(
                entry, rounds_max, wall_timeout_sec=wall_timeout_sec)
            # 확인 전용 라운드의 자격과 근거를 **이 스냅샷에서 한 번** 읽는다 — 자격은 여기서
            # 보고 근거는 프롬프트가 따로 읽으면, 두 read 사이에 끼어든 라운드가 "자격 통과 ·
            # 근거 없음"(또는 그 반대)을 만든다.
            confirm_fix_evidence = (
                _confirm_fix_evidence(
                    entry, surface_finding_ids=surface_finding_ids,
                ) if confirm_fix else None
            )
            if confirm_fix and entry["confirm_fix"] >= 1:
                if approved or wave_repaired:
                    _save_round_ledger(ledger)
                announce(resumed=False)
                print(_CONFIRM_FIX_SPENT_GUIDANCE.format(
                    gate=args.gate, used=entry["confirm_fix"],
                    ledger=_round_ledger_path()), file=sys.stderr)
                return RoundBudget(refused_rc=EXIT_ROUND_LIMIT_EXCEEDED)
            # 예외의 **자격** — 확인할 지적(최신 완료 라운드의 반려)이 실재해야 한다. 판정 입력은
            # 방금 만든 근거 블록 자체라, 자격이 열린 실행은 반드시 실을 근거를 갖는다.
            if confirm_fix and confirm_fix_evidence is None:
                if approved or wave_repaired:
                    _save_round_ledger(ledger)
                announce(resumed=False)
                # 자격이 없는 사유는 둘이고 처방이 다르다 — 반려 자체가 없거나(첫 라운드·전건
                # 통과), 반려는 있는데 그 산출이 회수 거부돼 판정 표면에 없거나.
                offsurface = _confirm_fix_offsurface_ids(entry, surface_finding_ids)
                if offsurface:
                    print(_CONFIRM_FIX_REFUSED_HARVEST_GUIDANCE.format(
                        gate=args.gate, ids=", ".join(offsurface),
                        knob=REVIEW_ROUNDS_MAX_KEY,
                        default=DEFAULT_REVIEW_ROUNDS_MAX,
                        ledger=_round_ledger_path()), file=sys.stderr)
                else:
                    print(_CONFIRM_FIX_NO_REJECTION_GUIDANCE.format(
                        gate=args.gate, rounds=len(entry["rounds"]),
                        verdict=_latest_verdict_label(entry),
                        ledger=_round_ledger_path()), file=sys.stderr)
                return RoundBudget(refused_rc=EXIT_ROUND_LIMIT_EXCEEDED)
            if convergence is not None and not confirm_fix:
                if approved or wave_repaired:
                    _save_round_ledger(ledger)
                announce(resumed=False)
                series = _recorded_must_fix_series(entry)
                completed, inflight = _convergence_round_usage(
                    entry, wall_timeout_sec=wall_timeout_sec)
                print(_CONVERGENCE_GUIDANCE.format(
                    gate=args.gate, reason=_CONVERGENCE_REASONS[convergence],
                    rounds=completed + inflight, completed=completed, inflight=inflight,
                    limit=rounds_max,
                    series=_format_must_fix_series(series),
                    knob=REVIEW_ROUNDS_MAX_KEY, default=DEFAULT_REVIEW_ROUNDS_MAX,
                    ledger=_round_ledger_path()), file=sys.stderr)
                return RoundBudget(refused_rc=EXIT_ROUND_LIMIT_EXCEEDED)

            # (2) 판정/미완 라운드 상한 (전송 횟수 축). wave 예산은 이와 독립 축이라 한쪽 승인이
            #     다른 쪽을 열지 않는다.
            count, acked = entry["count"], entry["acked_through"]
            unacked, verdicts, incomplete = _unacked_round_counts(entry)
            if verdicts >= limit or incomplete >= incomplete_limit:
                if approved or wave_repaired:
                    _save_round_ledger(ledger)      # 승인·손상 복구는 거부돼도 남긴다
                announce(resumed=False)
                print(_ROUND_LIMIT_GUIDANCE.format(
                    gate=args.gate, unacked=unacked, verdicts=verdicts,
                    incomplete=incomplete, limit=limit,
                    incomplete_limit=incomplete_limit,
                    ledger=_round_ledger_path(),
                    count=count, acked=acked), file=sys.stderr)
                # 예약 없음 (전송 전 거부) — 격리도 아직 없다(이 게이트가 그보다 앞이다).
                return RoundBudget(refused_rc=EXIT_ROUND_LIMIT_EXCEEDED)
            if wave["spent"] >= wave_budget:
                if approved or wave_repaired:
                    _save_round_ledger(ledger)
                announce(resumed=False)
                print(_WAVE_BUDGET_GUIDANCE.format(
                    gate=args.gate, spent=wave["spent"], budget=wave_budget,
                    started=wave["started"] or "미기록",
                    ledger=_round_ledger_path()), file=sys.stderr)
                return RoundBudget(refused_rc=EXIT_ROUND_LIMIT_EXCEEDED)
            announce(resumed=True)                  # 세 축을 모두 통과한 뒤에만 "재개"
            if confirm_fix:
                # 확인 전용 라운드 소비 — 통과가 확정된 뒤에 기록한다(거부된 실행은 예외를
                # 쓰지 않는다). 수렴 축이 막지 않는 상태에서 써도 같은 규칙으로 1회를 쓴다:
                # "플래그를 쓴 실행 = 예외를 쓴 실행"이라 회계가 조건 분기를 갖지 않는다.
                _spend_confirm_fix(entry)
                print(
                    f"확인 전용 라운드(--confirm-fix) 사용: 게이트 {args.gate} — "
                    f"게이트당 1회 (사용 {entry['confirm_fix']}회).",
                    file=sys.stderr,
                )
            round_id = uuid.uuid4().hex
            # 예약 sequence 는 **여기서** 잡는다 — 마감 시점에 레코드를 되찾아 읽으면 그 사이
            # 집계 창이 비워진 실행만 순번을 잃는다. 만료 시각도 같은 자리에서 새긴다 — 이 예약이
            # 언제까지 살아 있을 수 있는지는 **이 실행의** 백스톱이 정한다.
            record = _reserve_round(                                # 호출 전 라운드 예약
                entry, round_id, wall_timeout_sec=wall_timeout_sec,
                target_rev=target_rev,
            )
            _spend_wave_round(wave)                   # 같은 전송을 wave 예산에서도 차감
            _save_round_ledger(ledger)
            return RoundBudget(
                gate=args.gate, round_id=round_id, sequence=record["sequence"],
                started_at=record["started_at"], target_rev=record["target_rev"],
                wave_id=wave["id"],                   # 환불은 이 세대에만 유효
                confirm_fix_spent=confirm_fix,        # 전송 0 이면 quota 도 되돌린다
                confirm_fix_evidence=confirm_fix_evidence,   # 자격을 판정한 그 스냅샷의 근거
            )
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
        return RoundBudget(refused_rc=1)


def _refund_reserved_round(ledger: dict, reservation: RoundBudget) -> bool:
    """예약 하나를 **세 예산 축에서 같은 조건으로** 되돌린다 (전송이 확실히 없던 실행).

    호출부가 이미 `_round_ledger_lock()` 안에서 로드한 장부를 그 자리에서 고치고 저장은 호출부가
    한다 — 마감 경로는 같은 임계 구역에서 산출 기록까지 함께 쓰기 때문이다. 환불 조건이 한 군데라
    "격리 실패로 되돌린 예약"과 "스폰 실패로 되돌린 예약"이 서로 다른 규칙을 갖지 않는다.

    세 축은 라운드 count · wave 예산 · **확인 전용 라운드 quota** 다. quota 를 빼놓으면 스폰 실패
    한 번으로 게이트당 1회뿐인 예외가 소멸해, 전송도 과금도 없던 실행이 유일한 처방을 먹는다
    ("전송 0·과금 0 실행은 예산을 먹지 않는다" 불변식의 세 번째 축).

    wave 는 **예약 시점 세대**만 깎는다: 그 사이 `--ack-wave` 로 새 wave 가 열렸으면 이 실패는 그
    예산과 무관하다(깎으면 승인 1회로 예산이 늘어난다). 라운드 count 를 되돌리지 못했으면(레코드가
    그 사이 접힌 경우) 나머지 축도 건드리지 않는다 — 한쪽만 깎으면 축들의 소비가 갈린다."""
    entry = _gate_entry(ledger, reservation.gate)
    if not _refund_round(entry, reservation.round_id):
        return False
    if reservation.confirm_fix_spent:
        _refund_confirm_fix(entry)
    if not _refund_wave_round(_wave_state(ledger), reservation.wave_id):
        print(
            "경고: 예약 시점 wave 가 이미 리셋돼 wave 예산은 환불하지 않았습니다 "
            "(라운드 count 만 환불).",
            file=sys.stderr,
        )
    return True


def _release_round_reservation(reservation: RoundBudget, *, reason: str) -> bool:
    """전송 **전에** 끝난 실행의 예약을 그 자리에서 원자 환불한다 (락→로드→환불→저장).

    예약 뒤·스폰 전에 중단하는 구간의 소유자(`_PreSpawnReservation`)가 쓴다. 락과 환불 기계는 마감
    경로와 같은 것을 재사용하므로 동시 실행 직렬화와 세대 규칙이 두 경로에서 갈리지 않는다.

    락/저장 실패는 loud 경고만 남기고 삼킨다 — 이 실행의 rc(또는 전파할 예외)는 중단 사유가 소유하고,
    환불하지 못한 예약은 finished_at 없는 미완 레코드로 남아 다음 실행의 재시도 예산에서
    보수적으로 세어진다(장부가 실제보다 헐거워지는 방향으로는 틀리지 않는다).

    반환값은 **이번 호출이 실제로 되돌려 저장까지 했는지**다 — 정상 return 하는 호출부(마감 저장이
    실패한 no-spawn 경로)가 뒤따르는 안내 문구를 그 사실에 맞추는 데 쓴다. 되돌릴 예약이 없거나
    (이미 반영·다른 세대) 락/저장이 실패하면 False 다."""
    if not reservation.reserved:
        return False
    try:
        with _round_ledger_lock():
            ledger = _load_round_ledger()
            refunded = _refund_reserved_round(ledger, reservation)
            _save_round_ledger(ledger)
    except OSError as exc:
        print(
            f"경고: 라운드 예약 환불 실패 ({type(exc).__name__}: {exc}) — {reason}(으)로 전송 "
            "없이 중단한 실행의 예약이 장부에 남았습니다. 다음 실행의 미완 재시도 예산에서 "
            f"세어집니다 (장부: {_round_ledger_path()}).",
            file=sys.stderr,
        )
        return False
    if refunded:
        print(
            f"라운드 예약 환불: 게이트 {reservation.gate} — {reason}(으)로 전송 없이 "
            "중단했습니다(상한·wave 예산 미소진).",
            file=sys.stderr,
        )
    return refunded


class _PreSpawnReservation:
    """예약~스폰 사이 구간의 **단일 소유 seam** — 그 구간에서 끝난 실행의 예약을 한 번만 환불한다.

    예산 게이트가 격리보다 앞에 서면서 "예약은 잡혔는데 리뷰어는 아직 못 떴다"는 구간이 생겼다
    (격리 컨테이너 생성 · 리뷰어 환경/커맨드 준비). 이 구간에서 어떤 이유로 빠져나가든 외부 전송은
    확실히 없으므로 예약은 되돌아가야 한다. 환불 조건을 예외 **종류**로 잡으면(알려진
    `ReviewerWorkspaceError` 만) 나머지 예외가 예약을 finished_at 없는 미완 레코드로 남겨, 전송도
    과금도 없던 실행이 다음 실행의 미완 재시도 예산을 깎는다 —
    `additional_reviewer_incomplete_round_limit=1` 이면 다음 **정상** 호출이 곧바로 차단된다. 그래서
    조건은 종류가 아니라 **구간**이다: 여기서 나가는 모든 `BaseException`
    (`KeyboardInterrupt`·`SystemExit` 포함)이 환불을 부른다.

    환불은 하되 예외는 그대로 다시 던진다 — 예상 못 한 실패를 격리 실패와 같은 rc 로 바꾸면 진단이
    사라진다. 판정 rc 를 갖는 건 이미 정의된 격리 실패 경로뿐이다.

    소유권은 **실 스폰 경계**의 `hand_off()` 로 넘어간다 — 스폰할 *수도* 있는 함수의 입구가
    아니라, 인자·seam 검증과 러너 준비(relay 로드·프로필 해소·워치독 셋업)가 모두 끝나 다음
    문장이 `Popen` 을 부르는 그 한 줄 앞이다(`run_review` → `_run_reviewer_ex` →
    `_watchdog_reviewer_run` → relay 워치독의 `on_spawn_attempt`). 그 뒤의 실패는 전송이 이미
    일어났을 수 있어 조건이 다르고, 마감 경로(`started` 판정)가 본다. `_settled` 는 한 번만
    서므로 여러 층이 같은 예외를 잡아도 이중 환불(상한이 조용히 늘어남)이 되지 않는다.

    이전은 **대칭 반납**을 갖는다(`reclaim_no_spawn()`). 러너가 `started=False` 로 돌아온 실행은
    자식이 확실히 없었다고 **판명**된 것이라(확정 기동 실패·경계 전 준비 실패), 스폰 직전에 넘긴
    소유권이 그 자리에서 되돌아온다 —
    그러지 않으면 그 뒤의 요약 출력·진단·마감이 죽었을 때(닫힌 stdout 파이프의 `BrokenPipeError`
    가 실측 축이다) 예약이 finished_at 없는 미완 레코드로 남아, 전송 0·과금 0 인 실행이
    `incomplete_limit=1` 형상에서 다음 **정상** 호출을 곧바로 막는다. 반납 조건은 판명된
    `started=False` 하나뿐이다 — 타임아웃·불확실 예외(started=True)는 이미 나갔을 수 있어
    그대로 마감 경로가 소비한다."""

    def __init__(self, budget: RoundBudget) -> None:
        self.budget = budget
        self._settled = False

    def __enter__(self) -> "_PreSpawnReservation":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            self.release(reason=f"스폰 전 예외 {exc_type.__name__}")
        return False                                  # 예외는 삼키지 않는다

    def release(self, *, reason: str) -> bool:
        """아직 소유 중인 예약을 환불한다 (이미 환불·이전됐으면 no-op).

        반환값은 **이번 호출이 실제로 되돌렸는지**다 — 예외로 나가는 구간은 쓰지 않지만, 정상
        return 하는 마감 실패 경로는 뒤따르는 안내 문구("미완으로 남는다" vs "되돌렸다")를 그
        사실에 맞춰야 한다. no-op(이미 정산)·환불 실패는 둘 다 False 다."""
        if self._settled:
            return False
        self._settled = True
        return bool(_release_round_reservation(self.budget, reason=reason))

    def hand_off(self) -> None:
        """스폰 구간으로 소유권 이전 — 이후 실패는 이 seam 이 아니라 마감 경로가 판정한다."""
        self._settled = True

    def reclaim_no_spawn(self) -> None:
        """스폰이 **없었다고 판명된** 뒤 소유권을 되찾는다 (`hand_off` 의 대칭).

        호출 자격은 러너가 결론적으로 `started=False` 로 돌아온 직후 한 자리뿐이다 — 그 시점의
        사실은 "자식이 뜰 수도 있었다"가 아니라 "뜬 적 없다"이므로, 스폰 직전에 넘긴 권리가 그대로
        되돌아온다. 되찾은 뒤의 실패(요약·진단·마감)는 다시 이 seam 이 한 번 환불한다."""
        self._settled = False

    def settle_refunded(self) -> None:
        """마감 경로가 이 예약을 이미 환불했음을 기록한다 — 같은 예약을 두 번 되돌리지 않는다.

        `hand_off` 와 상태 변화는 같지만 사실이 다르다: 저쪽은 "스폰 구간이 판정한다", 이쪽은
        "환불이 끝났다"이다. 되돌린 뒤 다시 되돌리는 건 상한이 조용히 늘어나는 방향이라 이름으로
        구분해 둔다."""
        self._settled = True


# ── 장부 조회면 (--rounds-report) ───────────────────────────────────────────
# 비용 적정성 판단(게이트 심도 대비)을 PM 자기보고 대신 장부 근거로 하게 만드는 읽기 전용 표다.
# 외부 전송·diff 추출 없이 장부만 읽는다.

# rc 판정 표기 — 1 은 반려뿐 아니라 실패·불명확도 포함하므로 '비통과'로 읽는다(결함 수 '미상'이
# 그 라운드가 판정을 내지 못했음을 함께 알린다).
_ROUND_VERDICT_LABELS: dict[int, str] = {0: "통과", 1: "비통과"}


def _format_round_field(value: object) -> str:
    """산출 필드(결함 수) 표기 — 셀 근거가 없던 null 은 '미상'으로 구분해 보인다."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "미상"
    return str(value)


def _format_round_verdict(value: object) -> str:
    """판정 rc 표기 — `0(통과)` / `1(비통과)`; 기록이 없거나 손상이면 '미상'."""
    if isinstance(value, bool) or not isinstance(value, int):
        return "미상"
    label = _ROUND_VERDICT_LABELS.get(value)
    return f"{value}({label})" if label else str(value)


def _round_sequence(outcome: dict) -> int | None:
    """산출 레코드가 실은 **예약 순번** (없거나 손상이면 None)."""
    value = outcome.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _ordered_round_outcomes(rounds: list) -> list[dict]:
    """산출을 **예약 순서**로 정렬한다 — append 순서는 *완료* 순서라 라운드 번호가 아니다.

    같은 게이트의 두 라운드가 역순으로 끝나면(느린 1라운드·빠른 2라운드) append 순서는 실제 라운드
    순서를 뒤집는다. 순번이 없는 구세대/미연결 산출은 **앞**(오래된 쪽)에 원래 순서대로 남긴다
    (안정 정렬) — 뒤에 두면 업그레이드 후 새 통과가 쌓여도 옛 반려가 영원히 '최신'이 된다.

    정렬 규칙은 board 의 공용 seam(`round_outcome_order_key`)이 소유한다 — 같은 장부를 읽는
    board 의 강등 판정(`_last_round_verdict`)과 사본으로 갈리면 두 표면이 서로 다른 라운드를
    '최신'이라고 답한다. 방향은 이 파일이 board 를 로드하는 쪽이다(역방향은 순환)."""
    return sorted(rounds, key=_load_board().round_outcome_order_key)


def _format_gate_resolution(entry: dict) -> str:
    """게이트 처분 열 — `미처분` / `재설계→T-NNNN` / `해소(근거 T-NNNN)` / `무대상`.

    판정은 board 의 공용 seam(`gate_resolution`·`gate_residual_must_fix`)이 소유한다 — 릴리즈
    차단이 보는 것과 **같은 사실**을 그대로 보여야 이 표로 차단을 미리 예측할 수 있다.
    `무대상` 은 잔여 must-fix 가 없어 처분이 필요 없는 게이트다(통과·suggestion 만 남은 게이트)."""
    board = _load_board()
    declared = board.gate_resolution(entry)
    if declared is None:
        return "미처분" if board.gate_has_residual(entry) else "무대상"
    return _describe_resolution(declared)


def render_rounds_report(
    ledger: dict,
    *,
    ledger_path: Path | str | None = None,
    gate: str | None = None,
    wave_budget: int = DEFAULT_WAVE_BUDGET,
    title: str = "추가 리뷰 라운드 장부",
    include_wave: bool = True,
) -> str:
    """라운드 장부를 조회 표로 렌더한다 (게이트별 라운드 수 · 라운드별 산출 · wave spent).

    `gate` 를 주면 그 게이트만 본다. 순수 함수라 장부 dict 만 있으면 렌더가 재현된다 — 파일/앵커
    해소는 호출부(`_print_rounds_report`)가 한다.

    라운드 번호와 나열 순서는 **예약 순번**(`sequence`)이다 — 장부의 append 순서는 완료 순서라
    동시 리뷰가 역순으로 끝나면 라운드가 뒤바뀌어 보인다."""
    snapshot = dict(ledger)                # 사본 정규화 — 조회가 장부를 고치지 않는다
    wave = _wave_state(snapshot)
    lines = [
        f"{title}: {ledger_path if ledger_path is not None else '(미해소)'}",
        "범례: 판정 0=통과 · 1=비통과(반려·실패·불명확) · '미상'=판정이 무효했던 라운드",
        "처분: 미처분=잔여 must-fix 선언 없음(릴리즈 차단) · 재설계→티켓(그 티켓 done 이 조건) · "
        "해소(근거 게이트) · pm-fixed=PM 직접 해소(리뷰 통과 아님) · 무대상=잔여 없음",
    ]
    if include_wave:
        lines.insert(
            1,
            f"wave: spent={wave['spent']} / 예산 {wave_budget} · "
            f"시작 {wave['started'] or '미시작'}",
        )
    names = [name for name in _gate_names(snapshot) if gate is None or name == gate]
    if not names:
        lines.append(
            f"게이트 {gate}: 장부에 기록 없음" if gate else "기록된 게이트 없음"
        )
        return "\n".join(lines)
    for name in names:
        entry = _gate_entry(snapshot, name)
        rounds = entry["rounds"]
        lines.append(
            f"게이트 {name}: count={entry['count']} · "
            f"acked_through={entry['acked_through']} · 산출 {len(rounds)}건 · "
            f"처분={_format_gate_resolution(entry)}"
        )
        if not rounds:
            lines.append("  (라운드 산출 기록 없음 — 산출 장부 이전의 전송)")
            continue
        for outcome in _ordered_round_outcomes(rounds):
            sequence = _round_sequence(outcome)
            lines.append(
                f"  #{sequence if sequence is not None else '미상'} "
                f"{outcome.get('ts') or '시각 미상'} "
                f"판정={_format_round_verdict(outcome.get('verdict'))} "
                f"must_fix={_format_round_field(outcome.get('must_fix'))} "
                f"suggestions={_format_round_field(outcome.get('suggestions'))}"
            )
    return "\n".join(lines)


def _print_rounds_report(
    anchor: Path, *, gate: str | None, resolved: bool = False,
) -> int:
    """`--rounds-report` 실행면 — 소유 PM 홈 장부를 해소해 조회 표를 출력한다 (rc 0).

    앵커 규칙은 **기록면과 같은 입력**을 쓴다. `--paths`/`--ticket` 을 주면 호출부가 기록 경로와
    똑같이 diff 소유 PM 홈까지 해소한 뒤 그 값을 넘기고(`resolved=True`), selector 없는 조회만
    엔진 앵커의 소유 PM 홈으로 해소한다(= selector 없는 기록 실행과 같은 규칙). 그래서 같은 인자
    로는 읽는 장부와 쓰는 장부가 갈리지 않는다. board 가 필요 없는 조회라 해소 실패는 loud 경고
    뒤 자기 앵커 폴백이다."""
    global _PM_HOME_OVERRIDE
    _PM_HOME_OVERRIDE = (
        anchor if resolved else resolve_pm_home_for_repo(anchor, required=False)
    )
    try:
        wave_budget = _wave_budget(_local_config_for_repo(_PM_HOME_OVERRIDE))
    except (OSError, UnicodeError) as exc:
        # 조회는 외부 송신이 없어 conf 판독 실패로 막을 이유가 없다(송신 경로의 fail-closed 와
        # 다른 축) — 예산 표기만 기본값으로 낮추고 그 사실을 알린다.
        print(
            f"경고: local.conf 읽기 실패 ({type(exc).__name__}: {exc}) — wave 예산 표기를 "
            f"기본값 {DEFAULT_WAVE_BUDGET} 로 대체합니다.",
            file=sys.stderr,
        )
        wave_budget = DEFAULT_WAVE_BUDGET
    print(render_rounds_report(
        _load_round_ledger(),
        ledger_path=_round_ledger_path(),
        gate=gate,
        wave_budget=wave_budget,
    ))
    return 0


# ── 게이트 처분 선언면 (--resolve-gate) ─────────────────────────────────────
# 라운드 상한으로 종결된 게이트의 **잔여 must-fix 를 어떻게 소화했는지**를 장부에 남기는 기록면이다
# (외부 전송 없음). 릴리즈 차단(`board.py livegate record`)은 이 기록 사실만 읽는다 — 선언은
# 판정이 아니라 기록이고, 두 갈래(재설계·해소) 모두 장부로 확인 가능한 근거를 요구한다:
#   · `--into <T-NNNN>`  — 잔여를 후속 티켓으로 재설계. 면제가 아니라 **그 티켓이 done 이어야**
#     릴리즈가 열린다(같은 릴리즈 안 소화 강제). 자기 자신 지목은 우회라 거부한다.
#   · `--fixed <근거 게이트>` — 코드로 해소. 근거 게이트의 **마지막 라운드 판정이 통과(0)** 라는
#     장부 사실이 조건이다("해소했다"는 주장만으로는 선언되지 않는다).
# 앵커는 selector 없는 조회면(`--rounds-report`)과 같다 — 엔진 repo 의 소유 PM 홈. 기록면이 쓰는
# 그 장부를 보고, 같은 홈의 보드에서 재설계 대상 티켓을 확인한다.

# `--fixed` 를 값 없이 쓴 실행의 표식 — 근거 게이트 지목은 필수라 안내로 거부한다(argparse 의
# 일반 "expected one argument" 대신 왜 필요한지를 말한다).
_FIXED_WITHOUT_EVIDENCE = "\x00-근거-미지목"


def _describe_resolution(declared: dict) -> str:
    """처분 선언 한 줄 표기 — `재설계→T-NNNN` / `해소(근거 T-NNNN)` (조회 표·선언 응답 공용).

    어휘는 board 의 표기 표(`GATE_RESOLUTION_LABELS`)를 쓴다 — 차단 사유와 선언 응답이 같은 처분을
    다른 말로 부르지 않게."""
    board = _load_board()
    label = board.GATE_RESOLUTION_LABELS[declared["kind"]]
    if declared["kind"] == board.GATE_RESOLUTION_INTO:
        return f"{label}→{declared['ticket']}"
    if declared["kind"] == board.GATE_RESOLUTION_PM_FIXED:
        return _load_review_rounds().describe_pm_fixed_resolution(declared)
    return f"{label}(근거 {declared['evidence_gate']})"


def _evidence_gate_problem(ledger: dict, gate: str, entry: dict, evidence: str) -> str | None:
    """근거 게이트가 이 게이트의 해소를 뒷받침하는지 — 아니면 사유 1줄 (뒷받침하면 None).

    입력은 장부 사실뿐이고, 판정은 board 의 공용 seam(`gate_evidence_problem`)이 소유한다 —
    선언 시점(여기)과 릴리즈 재검증이 다른 규칙을 쓰면 "선언은 됐는데 릴리즈에서 막힌다"가 난다.
    자기 자신 지목은 근거가 될 수 없다: 반려로 끝난 그 게이트의 통과 라운드를 자기 안에서 찾는
    셈이라 정의상 성립하지 않는다(명시 거부로 안내를 정확히 한다)."""
    if evidence == gate:
        return "자기 자신은 근거가 될 수 없습니다 — 해소를 보인 다른 게이트를 지목하세요"
    if evidence not in ledger:
        return "라운드 장부에 그 게이트의 기록이 없습니다"
    # 차단 게이트 항목은 **호출부가 정규화한 그 객체**를 그대로 받는다 — 여기서 다시
    # `_gate_entry` 를 부르면 장부의 항목이 새 dict 로 교체돼 호출부가 들고 있던 참조가 고아가
    # 되고, 그 뒤 기록한 처분이 저장되지 않는다(선언은 rc0 인데 장부는 그대로).
    return _load_board().gate_evidence_problem(entry, _gate_entry(ledger, evidence))


def _resolve_gate_ignored_flags(args: argparse.Namespace) -> str:
    """처분 선언 실행이 무시하는 플래그 목록 (없으면 빈 문자열)."""
    return ", ".join(
        flag for flag, given in (
            ("--gate", bool(args.gate)),        # 처분 대상은 `--resolve-gate` 가 지정한다
            # 회계 opt-out 도 선언면에선 뜻이 없다 — 이 실행은 전송도 예약도 하지 않는다.
            ("--no-gate", bool(getattr(args, "no_gate", False))),
            ("--rounds-report", args.rounds_report),
            ("--confirm-fix", args.confirm_fix),
            ("--ack-wave", args.ack_wave),
            ("--force", args.force),
        ) if given
    )


def _resolve_gate_command(args: argparse.Namespace, engine_repo: Path) -> int:
    """`--resolve-gate` 실행면 — 게이트 처분을 장부에 선언한다 (rc 0 = 기록됨).

    거부는 전부 **장부 변경 전**이다 — 선언되지 않은 실행은 장부에 아무 흔적도 남기지 않는다.
    확인→기록은 라운드 예약과 같은 배타락 임계 구역 안에서 한다(동시 실행이 서로의 선언을
    덮어쓰지 않게)."""
    global _PM_HOME_OVERRIDE
    gate = args.resolve_gate
    reserved = _reserved_gate_error(gate, flag="--resolve-gate")
    if reserved is not None:
        print(reserved, file=sys.stderr)
        return 1
    into, fixed = args.into, args.fixed
    if bool(into) == bool(fixed) or fixed == _FIXED_WITHOUT_EVIDENCE:
        # 둘 다 없음 · 둘 다 있음 · 근거 없는 `--fixed` — 셋 다 "처분이 확정되지 않았다"는 한 축.
        print(_RESOLVE_GATE_MODE_GUIDANCE.format(gate=gate), file=sys.stderr)
        return 1
    if fixed:
        reserved = _reserved_gate_error(fixed, flag="--fixed")
        if reserved is not None:
            print(reserved, file=sys.stderr)
            return 1
    if args.dry_run:
        # 선언은 기록이 목적인 실행이라 "미리보기"가 성립하지 않는다 — 경고 후 기록하면
        # `--dry-run` 의 부작용 0 계약이 이 표면에서만 깨진다. 거부가 정직하다.
        print(_RESOLVE_GATE_DRY_RUN_GUIDANCE.format(gate=gate), file=sys.stderr)
        return 1
    ignored = _resolve_gate_ignored_flags(args)
    if ignored:
        print(f"경고: --resolve-gate 는 장부 기록 전용이라 다음 플래그를 무시합니다: {ignored}.",
              file=sys.stderr)
    _PM_HOME_OVERRIDE = resolve_pm_home_for_repo(engine_repo, required=False)
    if into:
        if into == gate:
            print(_RESOLVE_GATE_SELF_INTO_GUIDANCE.format(gate=gate, ticket=into),
                  file=sys.stderr)
            return 1
        try:
            _find_ticket_file(into, pm_home=_PM_HOME_OVERRIDE)
        except AnchorResolutionError as exc:
            print(_RESOLVE_GATE_TICKET_GUIDANCE.format(ticket=into, detail=exc),
                  file=sys.stderr)
            return 1
    board = _load_board()
    with _round_ledger_lock():
        ledger = _load_round_ledger()
        if gate not in ledger:
            print(_RESOLVE_GATE_UNKNOWN_GUIDANCE.format(
                gate=gate, ledger=_round_ledger_path()), file=sys.stderr)
            return 1
        entry = _gate_entry(ledger, gate)
        if not board.gate_has_residual(entry):
            # 잔여 없음만 거부한다 — **미상**(판정 무효 라운드)은 릴리즈가 차단하는 축이라
            # 선언도 받아야 한다(둘이 갈리면 차단은 되는데 처분은 못 하는 데드락).
            print(_RESOLVE_GATE_NO_RESIDUAL_GUIDANCE.format(
                gate=gate, residual=board.gate_residual_label(entry)), file=sys.stderr)
            return 1
        residual = board.gate_residual_must_fix(entry)
        if fixed:
            detail = _evidence_gate_problem(ledger, gate, entry, fixed)
            if detail is not None:
                print(_RESOLVE_GATE_EVIDENCE_GUIDANCE.format(
                    evidence=fixed, gate=gate, detail=detail), file=sys.stderr)
                return 1
        residual_label = board.gate_residual_label(entry)
        previous = board.gate_resolution(entry)
        declared = {
            "kind": board.GATE_RESOLUTION_INTO if into else board.GATE_RESOLUTION_FIXED,
            "ticket" if into else "evidence_gate": into or fixed,
            "ts": _utc_now_iso(),
            # 선언 시점의 잔여 건수 — 나중에 "무엇을 처분했나"를 되짚는 감사 사실이다.
            "must_fix": residual,
            # 처분이 결속하는 라운드 좌표 — 이 뒤에 새 라운드가 오면 그 잔여는 미처분이다
            # (좌표 없는 선언은 board 가 처분으로 인정하지 않는다·`gate_resolution`).
            **board.gate_round_binding(entry),
        }
        entry["resolution"] = declared
        _save_round_ledger(ledger)
    if previous is not None:
        print(f"이전 처분 선언을 교체합니다: {_describe_resolution(previous)}", file=sys.stderr)
    print(f"게이트 처분 선언: {gate} · 잔여 must_fix {residual_label} · "
          f"{_describe_resolution(declared)}")
    print(f"  · 장부: {_round_ledger_path()}")
    if into:
        print(f"  · 재설계는 면제가 아닙니다 — `board.py livegate record` 는 {into} 가 done 일 "
              "때만 릴리즈를 엽니다.")
    return 0


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
    return _filter_diff_hunks(
        diff_text, lambda path: _is_secret_path(path, patterns),
    )


_DIFF_PATH_UNRESOLVED_PREFIX = "[diff 경로 확정 실패 · fail-closed] "
_GIT_PATH_INVALID = object()
_GIT_PATH_MISSING = object()
_GIT_PATH_DEV_NULL = object()


def _git_c_unquote(value: str) -> str:
    """Git의 큰따옴표 C-quote 경로 하나를 UTF-8 문자열로 복원한다.

    Git은 ``core.quotePath`` 기본값에서 비 ASCII 바이트를 ``\\ooo`` 8진 escape로 쓴다.
    따라서 escape를 문자 단위로 치환하면 mojibake가 된다. 먼저 원래 바이트열을 복원한 뒤
    UTF-8로 엄격하게 decode한다. 손상되거나 비 UTF-8인 표기는 호출자가 fail-closed 처리한다.
    """
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        raise ValueError("Git C-quote 경로가 큰따옴표로 닫히지 않았습니다")
    decoded = bytearray()
    named_escapes = {
        "a": 0x07, "b": 0x08, "t": 0x09, "n": 0x0A,
        "v": 0x0B, "f": 0x0C, "r": 0x0D,
    }
    index = 1
    while index < len(value) - 1:
        char = value[index]
        if char != "\\":
            decoded.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(value) - 1:
            raise ValueError("Git C-quote 경로의 escape가 끝나지 않았습니다")
        escaped = value[index]
        if escaped in ('"', "\\"):
            decoded.append(ord(escaped))
            index += 1
        elif escaped in named_escapes:
            decoded.append(named_escapes[escaped])
            index += 1
        elif escaped in "01234567":
            octal = value[index:index + 3]
            if len(octal) != 3 or any(digit not in "01234567" for digit in octal):
                raise ValueError("Git C-quote 경로의 8진 escape가 3자리가 아닙니다")
            decoded.append(int(octal, 8))
            index += 3
        else:
            raise ValueError(f"알 수 없는 Git C-quote escape: \\{escaped}")
    return decoded.decode("utf-8", errors="strict")


def _quoted_git_path_atom(value: str) -> tuple[str, str]:
    """문자열 선두의 C-quoted atom과 나머지를 분리한다."""
    if not value.startswith('"'):
        raise ValueError("C-quoted atom이 아닙니다")
    index = 1
    while index < len(value):
        if value[index] == "\\":
            index += 2
            continue
        if value[index] == '"':
            atom = value[:index + 1]
            return _git_c_unquote(atom), value[index + 1:]
        index += 1
    raise ValueError("Git C-quote 경로가 닫히지 않았습니다")


def _diff_header_paths(line: str) -> tuple[str, str] | None:
    """구조적으로 명확한 ``diff --git`` 헤더의 old/new 경로만 복원한다.

    공백 경로의 ``a/x y b/x y``는 헤더만으로 경계를 정할 수 없으므로 반환하지 않는다. 두 atom이
    각각 공백 없는 값이거나 C-quote로 경계가 명시된 경우만 보조 근거로 쓴다.
    """
    value = line[len("diff --git "):].rstrip("\r\n")
    atoms: list[str] = []
    position = 0
    try:
        for _ in range(2):
            while position < len(value) and value[position].isspace():
                position += 1
            if position >= len(value):
                return None
            if value[position] == '"':
                atom, remainder = _quoted_git_path_atom(value[position:])
                consumed = len(value[position:]) - len(remainder)
                position += consumed
            else:
                end = position
                while end < len(value) and not value[end].isspace():
                    end += 1
                atom = value[position:end]
                position = end
            atoms.append(atom)
        if value[position:].strip():
            return None
    except (UnicodeDecodeError, ValueError):
        return None
    if not atoms[0].startswith("a/") or not atoms[1].startswith("b/"):
        return None
    return atoms[0][2:], atoms[1][2:]


def _metadata_git_path(
    line: str, marker: str, *, side_prefix: str | None = None,
    tab_suffix: bool = False,
) -> str | object:
    """``---``/``+++``/rename 메타데이터 한 줄의 경로를 복원한다."""
    value = line[len(marker):].rstrip("\r\n")
    try:
        if value.startswith('"'):
            path, remainder = _quoted_git_path_atom(value)
            if remainder and not (tab_suffix and remainder.startswith("\t")):
                return _GIT_PATH_INVALID
        else:
            path = value.split("\t", 1)[0] if tab_suffix else value
    except (UnicodeDecodeError, ValueError):
        return _GIT_PATH_INVALID
    if path == "/dev/null":
        return _GIT_PATH_DEV_NULL
    if side_prefix is not None:
        prefix = side_prefix + "/"
        if not path.startswith(prefix):
            return _GIT_PATH_INVALID
        path = path[len(prefix):]
    return path if path else _GIT_PATH_INVALID


def _unique_metadata_path(values: list[str | object]) -> str | object:
    """한쪽 메타데이터가 가리키는 단 하나의 path/dev-null 상태를 확정한다."""
    if _GIT_PATH_INVALID in values:
        return _GIT_PATH_INVALID
    paths = {value for value in values if isinstance(value, str)}
    has_dev_null = _GIT_PATH_DEV_NULL in values
    if len(paths) > 1 or (paths and has_dev_null):
        return _GIT_PATH_INVALID
    if paths:
        return next(iter(paths))
    if has_dev_null:
        return _GIT_PATH_DEV_NULL
    return _GIT_PATH_MISSING


def _diff_block_path(block: list[str]) -> str | None:
    """Git diff block의 판정 경로를 메타데이터 우선으로 유일하게 확정한다.

    destination 경로를 우선하되 신규/삭제의 ``/dev/null``은 경로로 보지 않고 반대편을 쓴다.
    헤더는 공백 없는 두 atom 또는 C-quoted 두 atom일 때, 해당 side의 구조화 메타데이터가 없을
    때만 폴백한다. 공백 헤더를 임의 분할하지 않는다.
    """
    if not block or not block[0].startswith("diff --git "):
        return None
    old_values: list[str | object] = []
    new_values: list[str | object] = []
    for line in block[1:]:
        if line.startswith("@@") or line.startswith("GIT binary patch"):
            break
        if line.startswith("--- "):
            old_values.append(_metadata_git_path(
                line, "--- ", side_prefix="a", tab_suffix=True,
            ))
        elif line.startswith("+++ "):
            new_values.append(_metadata_git_path(
                line, "+++ ", side_prefix="b", tab_suffix=True,
            ))
        elif line.startswith("rename from ") or line.startswith("copy from "):
            marker = "rename from " if line.startswith("rename from ") else "copy from "
            old_values.append(_metadata_git_path(line, marker))
        elif line.startswith("rename to ") or line.startswith("copy to "):
            marker = "rename to " if line.startswith("rename to ") else "copy to "
            new_values.append(_metadata_git_path(line, marker))

    header_paths = _diff_header_paths(block[0])
    if not old_values and header_paths is not None:
        old_values.append(header_paths[0])
    if not new_values and header_paths is not None:
        new_values.append(header_paths[1])
    old_path = _unique_metadata_path(old_values)
    new_path = _unique_metadata_path(new_values)
    if _GIT_PATH_INVALID in (old_path, new_path):
        return None
    if isinstance(new_path, str):
        return new_path
    if new_path is _GIT_PATH_DEV_NULL and isinstance(old_path, str):
        return old_path
    if new_path is _GIT_PATH_MISSING and isinstance(old_path, str):
        return old_path
    return None


def _unresolved_diff_exclusion(block: list[str]) -> str:
    header = block[0].rstrip("\r\n") if block else "(diff 헤더 없음)"
    return f"{_DIFF_PATH_UNRESOLVED_PREFIX}{header}"


def _is_unresolved_diff_exclusion(value: str) -> bool:
    return value.startswith(_DIFF_PATH_UNRESOLVED_PREFIX)


def _filter_diff_hunks(
    diff_text: str, excluded_by: Callable[[str], bool],
) -> tuple[str, list[str]]:
    """파일 경로 판정 하나로 diff block을 제외한다.

    시크릿 denylist와 기계 mirror payload가 이 조립 기계를 공유한다. 정책 판정은 각각 기존
    `_is_secret_path`와 `is_machine_mirror_path`가 소유하며, 여기에는 별도 제외 술어를 두지 않는다.
    단 경로를 유일하게 복원하지 못한 block은 술어와 무관하게 fail-closed 제외하고 사유 표식을
    제외 목록에 남긴다.
    """
    if not diff_text:
        return diff_text, []
    excluded_files: list[str] = []
    prefix: list[str] = []
    blocks: list[list[str]] = []
    current_block: list[str] | None = None
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_block is not None:
                blocks.append(current_block)
            current_block = [line]
        elif current_block is None:
            prefix.append(line)
        else:
            current_block.append(line)
    if current_block is not None:
        blocks.append(current_block)

    output_blocks = list(prefix)
    for block in blocks:
        file_path = _diff_block_path(block)
        if file_path is None:
            excluded_files.append(_unresolved_diff_exclusion(block))
        elif excluded_by(file_path):
            excluded_files.append(file_path)
        else:
            output_blocks.extend(block)
    return "".join(output_blocks), excluded_files


def _format_unresolved_diff_exclusion_block(excluded: list[str]) -> str:
    """경로를 유일하게 확정하지 못해 fail-closed 제외한 diff block 안내."""
    lines = [
        "오류: diff block 경로를 유일하게 확정하지 못해 review payload에서 제외했습니다 ",
        "  (fail-closed — 확정하지 못한 hunk를 조용히 남기지 않습니다).",
    ]
    lines.extend(f"  · {item}" for item in excluded)
    return "\n".join(lines)


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


def _format_machine_mirror_exclusion_block(excluded: list[str]) -> str:
    """`--paths` 명시 지정분이 기계 mirror payload 제외에 걸렸을 때의 차단 안내."""
    lines = [
        "오류: --paths 로 명시 지정한 경로가 기계 mirror 판정에 걸려 review payload 에서 "
        "제외됐습니다 —",
        "  검증 안 한 것을 검증한 것처럼 보이는 가짜 통과(false-confidence)를 막기 위해 중단합니다.",
    ]
    for path in excluded:
        lines.append(f"  · {path}  (is_machine_mirror_path=True 판정)")
    lines.append(
        "  위 경로는 pm_update 엔진 사본 또는 패키지 설치 산출물입니다. 직접 리뷰 대상으로 "
        "지목하지 마세요.")
    lines.append(
        "  우회는 새 플래그가 아니라 위 경로를 --paths 에서 빼고 재실행하는 것입니다.")
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


# untracked 신규 파일의 '새 파일 diff' 는 `--no-index` 로 뽑는다 — **index 를 건드리지 않는
# 방식**이어야 한다. `git add -N`(intent-to-add) 선행이 더 짧지만 그건 read-only 여야 할 리뷰·측정
# 경로가 채택자 index 를 바꾸는 일이고, 위임 스코프 가드가 `git status --porcelain` 지문으로
# 작업트리 상태를 재는 축과 정면으로 부딪힌다(리뷰가 지문을 흔들면 범위 위반으로 오판된다).
# `/dev/null` 은 git 이 **문자열 그대로** 특수 취급하는 이름이라 Windows 에서도 같은 인자다.
_DEV_NULL_DIFF_OPERAND = "/dev/null"
# `--no-index` 는 `--exit-code` 를 내장해 **차이가 있으면 rc 1** 이다. 소비자는 rc 0 만 읽으므로
# (실패한 실행 = 그 단계에 변경 없음) 여기서 '차이 있음'을 rc 0 으로 접는다. rc≥2(진짜 오류)는
# 그대로 두어 종전 폴백 규칙을 탄다.
_NO_INDEX_DIFFERENCES_RC = 1


def _untracked_paths(
    root: Path, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[str, ...]:
    """검토 폭 안의 untracked 신규 파일 경로 (`.gitignore` 제외 · 조회 실패는 빈 튜플).

    pathspec 을 그대로 넘겨 폭이 diff 와 같게 유지한다. 출력 경로는 cwd(=`root`) 기준 상대라
    이어지는 `--no-index` 실행과 numstat 판정(`is_machine_mirror_path`)이 같은 좌표를 본다."""
    _run = run_fn or subprocess.run
    result = _run(
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z",
         "--", *paths],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        return ()
    return tuple(entry for entry in (result.stdout or "").split("\0") if entry)


def _untracked_diff_runs(
    root: Path, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, extra_args: Sequence[str] = (),
) -> list[subprocess.CompletedProcess]:
    """untracked 신규 파일 각각의 '새 파일 diff' 실행 결과 (index 미변경).

    `git diff` 는 index/커밋에 있는 것만 보므로 신규 파일이 통째로 빠진다 — 리뷰는 새 파일을 못
    보고, 서킷브레이커는 그것을 0 줄로 잰다(완료 부기가 재고 나서 stage 하는 순서라 대형 신규
    파일이 상한을 그대로 통과했다)."""
    _run = run_fn or subprocess.run
    runs: list[subprocess.CompletedProcess] = []
    for path in _untracked_paths(root, paths, run_fn):
        result = _run(
            ["git", "-C", str(root), "diff", "--no-index", *extra_args, "--",
             _DEV_NULL_DIFF_OPERAND, path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        if result.returncode == _NO_INDEX_DIFFERENCES_RC:
            result = subprocess.CompletedProcess(
                result.args, 0, result.stdout, result.stderr)
        runs.append(result)
    return runs


def _stage_diff_runs(
    root: Path, stage_base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, extra_args: Sequence[str] = (), untracked: bool | None = None,
) -> list[subprocess.CompletedProcess]:
    """한 diff 단계를 이루는 git 실행 결과 — 'HEAD' 단계만 스테이징/언스테이징+untracked 다.

    `extra_args` 는 같은 폭을 **다른 형식**으로 뽑는 데 쓴다(`--numstat`). 폭 자체(단계 표)는
    한 곳이 정하고 형식만 갈리게 해, 리뷰가 본 diff 와 서킷브레이커가 잰 diff 가 어긋나지 않게 한다.

    untracked 는 기본적으로 **작업트리 단계('HEAD')에만** 붙는다 — 명시 base·폴백 커밋 단계는
    커밋 사이의 폭이라 아직 커밋되지 않은 신규 파일이 그 폭의 구성원이 아니다. `untracked` 를
    명시하면 그 판정을 호출부가 소유한다: claim 앵커 단계(`git diff <claim 시점 rev>`)는 커밋
    이름을 base 로 쓰지만 비교 대상이 **현재 작업트리**라 신규 파일도 그 폭의 구성원이다.
    """
    _run = run_fn or subprocess.run
    arg_sets = (("--cached",), ()) if stage_base == "HEAD" else ((stage_base,),)
    runs = [
        _run(["git", "-C", str(root), "diff", *extra_args, *args, "--", *paths],
             capture_output=True, text=True, encoding="utf-8", errors="replace")
        for args in arg_sets
    ]
    include_untracked = stage_base == "HEAD" if untracked is None else untracked
    if include_untracked:
        runs.extend(
            _untracked_diff_runs(root, paths, run_fn, extra_args=extra_args))
    return runs


def _canonical_measure_path(path: str) -> str:
    """측정 판정 전 표기 변형을 POSIX 한 좌표로 모은다(`repo_coordinates` 정규화와 동형)."""
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _numstat_path(field: str) -> str:
    """`--numstat` 3번째 필드 → 판정 대상 경로 (rename 표기는 **목적지**로 접는다).

    rename 은 `{a => b}/c` 또는 `a => b` 로 나온다. 목적지를 보는 이유는 그게 지금 트리에 있는
    경로이고, mirror 판정도 "지금 어디에 있나"의 물음이기 때문이다."""
    if " => " not in field:
        return field
    expanded = re.sub(r"\{([^{}]*) => ([^{}]*)\}", r"\2", field)
    return expanded.split(" => ")[-1].strip()


def is_machine_mirror_path(path: str) -> bool:
    """diff 서킷브레이커의 **측정 제외 subtree**인가.

    정책은 pm_update 엔진 사본인 `templates/<타깃>/.project_manager/` 아래와 패키지 설치
    산출물인 `.opencode/node_modules/` 아래를 제외한다. template subtree 의 파일 전부는 아니다 —
    manifest 밖 손편집 파일도 있어 통째 제외하면 과소 측정이 생기지만, 오차의 방향이
    **측정 축소 = 가드 약화**라
    정당한 작업을 오차단하지 않는다(과다 차단이 아니라 과소 차단 쪽으로만 틀린다).

    **측정 제외 규칙의 단일 진실** — 리뷰(external_review)와 완료 부기(ticket_finish)가 같은 판정을
    쓴다(사본 0). 판정은 경로 문자열뿐이라 트리 상태에 의존하지 않는다."""
    return _MACHINE_MIRROR_RE.match(_canonical_measure_path(path)) is not None


def _is_review_machine_mirror_path(path: str) -> bool:
    """payload·리뷰어 거울에서 제외할 기계 mirror인가.

    측정 축의 단일 술어는 그대로 재사용하되, 사람이 직접 관리하는 세 출하 manifest만 검토 표면에
    남긴다. 예외를 정확한 실파일 집합으로 고정해 다른 `.project_manager/**` 산출물까지 넓어지지
    않게 한다."""
    canonical = _canonical_measure_path(path)
    return is_machine_mirror_path(canonical) and canonical not in _HAND_EDITED_REVIEW_PATHS


def _sum_numstat(text: str) -> int:
    """`git diff --numstat` 출력의 추가+삭제 합계 (바이너리 `-`/깨진 줄은 제외).

    **기계 mirror 경로는 세지 않는다**(`is_machine_mirror_path`) — 이 합계의 소비자는 diff
    서킷브레이커뿐이고, 그 상한은 사람이 손으로 쓴 스코프에 대한 값이다. 선언 경로가 mirror 를
    포함하는 넓은 접두(`templates/`)여도 여기서 걸러지므로 제외가 선언 형태에 좌우되지 않는다."""
    total = 0
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        if is_machine_mirror_path(_numstat_path(fields[2])):
            continue
        for value in fields[:2]:
            if value.isdigit():
                total += int(value)
    return total


# ── 서킷브레이커 측정 폭 (claim 앵커) ──────────────────────────────────────
# 서킷브레이커가 재야 하는 것은 "이 티켓이 claim 이후 남긴 변경"이다. 작업트리+직전 커밋 한 칸
# 이라는 옛 폭은 dev 브랜치를 `--no-ff` merge 로 흡수하는 형상에서 0 에 수렴한다 — finish 시점
# 트리는 clean 이고 마지막 커밋이 전파/부기 커밋이면 티켓 경로 교집합이 비기 때문이다(실측).
# claim 시점 코드 트리 HEAD 를 앵커로 쓰면 그 사이 커밋(merge 로 들어온 dev 누적 포함)이
# 한 폭에 들어온다.

# 앵커는 git argv 에 그대로 들어가므로 형태를 먼저 좁힌다 — 옵션처럼 보이는 값(`--foo`)이
# base 자리로 새면 git 이 그것을 플래그로 읽는다. board 가 박제하는 값은 40자 sha 다.
_CLAIMED_REV_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")

CLAIMED_REV_ABSENT_NOTE = (
    "claimed_rev 없음 — 폭 과소 측정 가능(옛 폭·작업트리+직전 커밋 한 칸으로만 잰다). "
    "claim 시점 rev 가 박제된 티켓부터 claim 이후 누적(merge 흡수분 포함)을 잰다."
)
CLAIMED_REV_UNRESOLVED_NOTE = (
    "claimed_rev {rev} 를 이 트리에서 해소하지 못함 — 폭 과소 측정 가능(옛 폭으로 잰다). "
    "박제된 rev 가 다른 저장소의 것이거나 히스토리가 다시 쓰였는지 확인하라."
)


def claim_anchor(
    root: Path, claimed_rev: str | None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[str | None, str | None]:
    """측정에 쓸 claim 앵커와 **폭 축소 사유** — `(앵커, None)` 또는 `(None, 사유)`.

    `git diff <rev>` 는 rev 를 못 찾으면 rc≠0 이고 측정 경로는 실패한 실행을 '이 단계에는 변경
    없음'으로 접는다. 그래서 해소되지 않는 앵커를 그대로 넘기면 **조용한 0 줄**(false-green)이
    된다 — 폭을 고르기 전에 존재를 한 번 묻고, 없으면 사유를 돌려 호출부가 옛 폭으로 접되
    그 사실을 시끄럽게 알리게 한다."""
    if not claimed_rev:
        return None, CLAIMED_REV_ABSENT_NOTE
    if not _CLAIMED_REV_RE.match(claimed_rev):
        return None, CLAIMED_REV_UNRESOLVED_NOTE.format(rev=claimed_rev)
    _run = run_fn or subprocess.run
    try:
        result = _run(
            ["git", "-C", str(root), "rev-parse", "--verify", "--quiet",
             f"{claimed_rev}^{{commit}}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return None, CLAIMED_REV_UNRESOLVED_NOTE.format(rev=claimed_rev)
    if result.returncode != 0:
        return None, CLAIMED_REV_UNRESOLVED_NOTE.format(rev=claimed_rev)
    return claimed_rev, None


def _measure_stages(
    base: str, claimed_rev: str | None,
) -> tuple[tuple[str, bool], ...]:
    """서킷브레이커가 잴 단계 표 — `(git diff 기준, untracked 포함 여부)`.

    claim 앵커가 있고 base 가 기본('HEAD')이면 단계는 하나다: `git diff <claimed_rev>` 는 그
    커밋 트리와 **현재 작업트리**를 비교하므로 claim 이후 커밋(merge 로 흡수한 dev 브랜치 누적
    포함)·스테이징·언스테이징이 한 폭에 들어온다. untracked 신규 파일도 작업트리 구성원이라
    함께 센다(stage 전 대형 신규 파일이 0 줄로 통과하던 창을 그대로 닫아 둔다).

    앵커가 없거나(구 티켓) `--base` 가 명시되면 옛 폭 그대로다 — 명시 base 는 사용자가 고른
    폭이라 앵커가 덮어쓰지 않는다."""
    if claimed_rev and base == "HEAD":
        return ((claimed_rev, True),)
    return tuple((stage, stage == "HEAD") for stage in _diff_bases(base))


def measured_numstat_text(
    root: Path, base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, claimed_rev: str | None = None,
) -> str:
    """서킷브레이커 측정 폭의 `--numstat` 원문 — **폭의 단일 정의 지점**.

    총량(`diff_line_total`)과 경로별 귀속(완료 부기의 claimed 합집합 분배)이 같은 함수를 소비해,
    두 소비자가 다른 폭을 재는 어긋남이 생기지 않는다. 실패한 git 실행은 '그 단계에는 변경
    없음'으로 본다(추출 경로의 폴백 규칙과 같다 — 측정 실패가 게이트를 벽돌로 만들지 않는다)."""
    for stage_base, untracked in _measure_stages(base, claimed_rev):
        text = "".join(
            result.stdout
            for result in _stage_diff_runs(
                root, stage_base, paths, run_fn, extra_args=("--numstat",),
                untracked=untracked,
            )
            if result.returncode == 0
        )
        if text.strip():
            return text
    return ""


def diff_line_total(
    root: Path, base: str, paths: Sequence[str],
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    *, claimed_rev: str | None = None,
) -> int:
    """검토 폭의 diff 총량(추가+삭제) — `measured_numstat_text` 폭을 합계로 접는다.

    폭 판정(어느 단계가 이번 측정인가)은 새로 만들지 않고 그 함수를 그대로 재사용하고, 형식만
    `--numstat` 이다. `claimed_rev` 는 claim 앵커(옵션) — 없으면 종전 폭 그대로다."""
    return _sum_numstat(
        measured_numstat_text(root, base, paths, run_fn, claimed_rev=claimed_rev)
    )


def _diff_cap_refusal(
    args, conf: dict[str, str], *, root: Path, paths: Sequence[str],
    pm_home: Path | None = None,
) -> str | None:
    """이번 실행의 diff 서킷브레이커 판정 (통과·가드 off 면 None).

    상한을 고르는 티켓은 `--ticket`(검토 범위를 정한 티켓) 우선, 없으면 `--gate`(게이트 표식)다.
    측정 폭은 **이번 실행이 실제로 리뷰하는 범위**(해소된 검토 경로)이고, 기준점은 그 티켓의
    claim 시점 rev(`claimed_rev`)다 — 완료 부기 서킷브레이커와 같은 폭이라 두 게이트가 다른
    숫자를 보지 않는다. 앵커는 **기본 폭**(미지정 또는 `--base HEAD`)에만 적용된다 — 판정이
    `args.base == "HEAD"` 문자열 비교라 명시 `--base HEAD` 도 기본과 구분되지 않는다(같은 이름의
    폭을 고른 것이라 실효 위험은 낮다). `--base` 로 다른 값을 명시하면 그 폭이 우선한다(앵커
    미적용). 앵커를 못 쓰는 티켓은 옛 폭으로 재되 **경고 1줄**을 남긴다 — 과소 측정이 조용히
    통과하지 않게 한다.
    측정 실패(git 부재·비-repo)는 0 으로 접혀 가드가 조용히 off 된다 — 이 축의 실패로
    리뷰 채널을 막지 않는다(hard 거부는 예산 축이 소유)."""
    ticket = args.ticket or args.gate
    if not ticket:
        return None
    estimate = parse_ticket_estimate(ticket, pm_home=pm_home)
    cap = _diff_cap(conf, estimate)
    if cap is None:
        return None
    claimed_rev: str | None = None
    anchor_note: str | None = None
    if args.base == "HEAD":
        # 기본 폭일 때만 앵커를 적용한다 — 명시 `--base` 는 사용자가 고른 폭의 주인이다.
        claimed_rev, anchor_note = claim_anchor(
            root, parse_ticket_claimed_rev(ticket, pm_home=pm_home),
        )
    if anchor_note is not None:
        print(f"주의: diff 서킷브레이커 측정 폭 — {ticket} {anchor_note}",
              file=sys.stderr)
    try:
        total = diff_line_total(root, args.base, list(paths),
                                claimed_rev=claimed_rev)
    except OSError:
        return None
    return diff_cap_block(
        total, cap, ticket=ticket, estimate=estimate, scope=list(paths),
    )


def diff_cap_block(
    total: int, cap: int | None, *,
    ticket: str, estimate: str | None, scope: Sequence[str],
) -> str | None:
    """diff 총량이 estimate 상한을 넘었으면 차단 안내를, 아니면 None 을 돌려준다.

    두 진입 표면(추가 리뷰 · 티켓 완료)이 같은 문구를 쓰도록 판정과 문구를 한 곳에 둔다.
    안내는 측정 의미(`MEASURED_SCOPE_NOTE`)를 함께 실어, 선언 스코프와 실제 측정 대상이
    다르다는 사실을 사람이 문구만 보고 알 수 있게 한다."""
    if cap is None or total <= cap:
        return None
    key = f"{DIFF_CAP_KEY_PREFIX}{(estimate or '').strip().lower()}"
    return _DIFF_CAP_GUIDANCE.format(
        ticket=ticket, estimate=estimate or "미지정", total=total, cap=cap,
        scope=", ".join(scope) or "(없음)", key=key,
        measured_note=MEASURED_SCOPE_NOTE,
        small=DEFAULT_DIFF_CAPS["small"], medium=DEFAULT_DIFF_CAPS["medium"],
        large=DEFAULT_DIFF_CAPS["large"],
    )


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

    기계 mirror와 시크릿 denylist 매칭 파일은 diff 에서 제외하고 그 경로 목록을 함께 돌려준다 —
    제외 사실을 호출자(main)가 차단/판정 병기에 반영할 수 있게 한다. 기계 mirror 판정은 측정 축의
    기존 `is_machine_mirror_path`를 재사용하되, 손편집 출하 manifest는 검토에 남기는 좁은 예외를
    적용한다. `_sum_numstat`·`diff_line_total`은 payload 조립과 별도 축이라 이 함수가 건드리지
    않는다. 제외분이 조용히 빠진 채 '통과'가 나면 게이트 false-confidence를 낳으므로, 메시징은
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

    # 기계 mirror를 먼저 빼 이유 분류를 보존한다. 두 정책에 동시에 걸리는 경로도 payload에서 한 번만
    # 제외되고 `is_machine_mirror_path` 판정으로 보고된다. 제외 목록은 호출자(main)가 명시/암묵
    # 모드별 차단·병기를 소유하도록 그대로 반환한다.
    filtered, machine_excluded = _filter_diff_hunks(raw_diff, _is_review_machine_mirror_path)
    filtered, secret_excluded = filter_secret_hunks(filtered, denylist)
    return filtered, [*machine_excluded, *secret_excluded]


# ── ticket touches 파싱 ───────────────────────────────────────────────────


def _tickets_dir_for(pm_home: Path | None) -> Path:
    """조회 대상 board 의 tickets 디렉토리 — 명세 파일과 라운드 사이드카가 함께 파생한다.

    `pm_home` 이 없으면 이 실행의 앵커(`_tickets_dir()`), 있으면 그 홈의 board/(없으면 legacy
    wiki/) 다. 본문과 라운드가 **같은 함수**로 해소돼야 두 입력이 다른 보드를 보지 않는다.
    """
    if pm_home is None:
        return _tickets_dir()
    pm_dir = Path(pm_home) / ".project_manager"
    board_tickets = pm_dir / "board" / "tickets"
    return board_tickets if board_tickets.is_dir() else pm_dir / "wiki" / "tickets"


def _find_ticket_file(ticket_id: str, *, pm_home: Path | None = None) -> Path:
    """board 에서 ticket 파일 하나를 찾는다 (못 찾으면 fail-loud).

    ticket 디렉토리는 `_tickets_dir()`로 *호출 시점* 해소한다 —
    board/ 분리 후 wiki/ legacy 위치(stale·ticket 미발견)를 안 보게.

    조회 판정은 board 의 공용 정확-일치 seam(`find_ticket_exact`)이 소유한다 — `{id}-*.md`
    prefix glob 의 **첫 매칭**을 믿으면 `T-NNNN` 과 `T-NNNN-001` 공존 시 **다른 티켓의
    estimate/touches** 로 diff 상한이 정해진다(조용한 오판). 사본 판정을 두지 않는 이유는
    같은 클래스가 표면마다 half-fix 로 재발해서다. 슬러그 없는 파일명(`T-NNNN.md`)은 prefix
    충돌이 불가능한 정확 이름이라 seam 이 비면 그 폴백을 그대로 본다(종전 동작).
    """
    if not ticket_id or re.search(r"[\\/*?\[\]]", ticket_id):
        raise AnchorResolutionError(f"ticket id 형식이 안전하지 않습니다: {ticket_id!r}")
    tickets_dir = _tickets_dir_for(pm_home)
    found = _load_board().find_ticket_exact(
        ticket_id,
        search_dirs=[(status, tickets_dir / status) for status in STATUS_DIRS],
    )
    if found is not None:
        return found[1]
    for status_dir in STATUS_DIRS:
        exact = tickets_dir / status_dir / f"{ticket_id}.md"
        if exact.exists():
            return exact
    raise AnchorResolutionError(
        f"ticket {ticket_id} 을 해소된 board에서 찾지 못했습니다: {tickets_dir}"
    )


class _TicketTouches(list[str]):
    """실 ticket 파일에서 읽은 touches와 그 파일 provenance.

    list 하위형이라 기존 소비자/테스트 seam은 그대로 동작한다. main은 이 provenance가 있을 때
    활성화·egress·빈-diff 게이트를 지난 뒤 같은 파일의 본문을 읽는다. plain list가 주입된 테스트
    seam은 실제 ticket 파일을 약속하지 않은 fixture라 본문 조회 실패를 hard-fail로 승격하지 않는다.
    """

    def __init__(self, values: list[str], source: Path):
        super().__init__(values)
        self.source = source


def parse_ticket_touches(ticket_id: str, *, pm_home: Path | None = None) -> list[str]:
    """board ticket frontmatter 의 touches 필드를 파싱해 경로 목록을 반환한다.

    touches 값 파싱은 이 파일의 YAML 리더가 하고, **어느 파일이 그 티켓인가**는 board 의 정확-일치
    seam 이 정한다(`_find_ticket_file`). 못 찾으면 fail-loud.
    """
    source = _find_ticket_file(ticket_id, pm_home=pm_home)
    return _TicketTouches(_parse_touches_from_file(source), source)


def _split_ticket_frontmatter(text: str, *, source: Path) -> tuple[str | None, str]:
    """frontmatter 원문과 본문을 한 경계에서 분리한다(None이면 frontmatter 없음).

    board.load_ticket을 재사용하지 않는 이유는 본문을 YAML 재직렬화나 strip 없이 byte-for-byte
    보존해야 하고, touches의 경량 파서도 YAML 의존 없이 같은 원문을 소비하기 때문이다. opener가
    있으면 closer도 반드시 있어야 하며 두 소비자 모두 같은 AnchorResolutionError로 fail-loud 한다.
    """
    if not text.startswith("---\n"):
        return None, text
    after_open = text[4:]
    end = after_open.find("\n---\n")
    if end == -1:
        raise AnchorResolutionError(f"ticket frontmatter가 닫히지 않았습니다: {source}")
    return after_open[:end], after_open[end + 5:]


def _load_ticket_text_and_body(path: Path) -> tuple[str, str]:
    """ticket 파일의 전문과 frontmatter만 벗긴 본문 (판독 1회).

    전문이 따로 필요한 이유는 라운드 판독이다 — 라운드의 "산출 없음(시드 그대로)" 판정은 준비가
    시드를 렌더할 때 본 것과 **같은 명세 텍스트**를 입력으로 써야 하고, 준비(`pm_delegate`)는
    파일 전문을 쓴다.
    """
    raw = _read_text_shared(path, encoding="utf-8")
    _frontmatter, body = _split_ticket_frontmatter(raw, source=path)
    return raw, body


def _load_ticket_body_from_file(path: Path) -> str:
    """이미 정확-일치 해소된 ticket 파일의 frontmatter만 벗긴 본문 원문."""
    return _load_ticket_text_and_body(path)[1]


def _load_ticket_rounds_for(
    ticket_id: str, *, pm_home: Path | None, ticket_text: str,
) -> list:
    """티켓의 라운드 목록을 순번 순으로 읽는다 (라운드 없으면 빈 목록).

    라운드 디렉터리 규약 위반은 조용히 건너뛰지 않고 본문 조립 실패와 **같은 깔때기**로 올린다
    — 리뷰 입력이 반쪽이 된 채로 외부에 나가면 안 된다(호출부가 loud 하게 멈춘다).
    """
    rounds_module = _load_ticket_rounds()
    try:
        return rounds_module.load_rounds(
            _tickets_dir_for(pm_home), ticket_id, ticket_text=ticket_text,
        )
    except rounds_module.RoundsError as exc:
        raise AnchorResolutionError(
            f"티켓 라운드 사이드카가 손상돼 리뷰 입력을 조립할 수 없습니다: {exc}"
        ) from exc


def _load_ticket_body(ticket_id: str, *, pm_home: Path | None = None) -> str:
    """정확-일치 ticket 파일에서 frontmatter만 벗긴 본문 원문을 반환한다.

    본문은 7절과 PM 판정 블록까지 모두 리뷰 입력이므로 strip/재직렬화하지 않는다.
    frontmatter opener가 있는데 closer가 없으면 전체 파일을 본문으로 오인해 메타데이터를 외부로
    보내지 않고 fail-loud 한다.
    """
    return _load_ticket_body_from_file(_find_ticket_file(ticket_id, pm_home=pm_home))


class TicketBodySelection(NamedTuple):
    """리뷰 입력 티켓 본문 — 명세 + 선별한 라운드, 그리고 접어 둔 산출 라운드 수."""

    text: str
    omitted_rounds: int


# 생략 표기 한 줄 — 리뷰어가 "이 티켓에 라운드가 더 있다"는 사실만 알면 된다(순번·크기는 board
# 에서 본다). 상한이 없으므로 정보성이다.
_TICKET_BODY_OMISSION_LINE = "(생략한 라운드 {n}개 — 역할별 마지막 산출만 싣습니다)"


def _select_ticket_body_for_review(body: str, rounds: Sequence) -> TicketBodySelection:
    """명세 전문 + **역할별 마지막 산출 라운드**를 순번 순으로 이어 붙인다.

    명세(`body`)는 권위 절과 PM 판정 블록을 그대로 담은 파일이라 전량 싣는다. 라운드는 파일
    하나가 산출 하나라 선별이 파일 선택으로 끝난다 — 역할마다 마지막 것만 싣고, 시드 그대로인
    라운드(산출 없음)는 세지도 싣지도 않는다. 이어 붙이는 표기는 `ticket_rounds.render_rounds_for_show`
    가 소유한다(사람이 `board.py show` 에서 읽는 그 구분선과 같은 규칙 하나).

    라운드가 없으면 명세 원문을 **바이트 그대로** 돌려준다 — 라운드 없는 티켓의 입력이 이 선별
    도입 전후로 동일해야 한다.
    """
    landed = [item for item in rounds if not item.pending]
    last_by_role: dict[str, int] = {}
    for item in landed:
        last_by_role[item.role] = max(item.ordinal, last_by_role.get(item.role, 0))
    selected = sorted(
        (item for item in landed if last_by_role[item.role] == item.ordinal),
        key=lambda item: item.ordinal,
    )
    # 생략 = **접힌 산출**의 수다. 산출 없는 라운드는 애초에 실을 것이 없으므로 세지 않는다 —
    # 세면 접힌 산출이 없는 티켓에서도 "생략" 과 선별 요약 헤더가 붙는다.
    omitted = len(landed) - len(selected)
    if not selected and not omitted:
        return TicketBodySelection(body, 0)

    parts = [body if body.endswith("\n") else body + "\n"]
    if omitted:
        parts.append("\n" + _TICKET_BODY_OMISSION_LINE.format(n=omitted) + "\n")
    if selected:
        parts.append("\n" + _load_ticket_rounds().render_rounds_for_show(selected))
    return TicketBodySelection("".join(parts), omitted)


def _parse_title_from_file(path: Path) -> str | None:
    """ticket 파일에서 frontmatter title 스칼라를 추출한다(선별 헤더 요약 전용).

    board 의 YAML frontmatter 로더(`_parse_estimate_from_file` 과 같은 seam)를 재사용한다 —
    자체 정규식은 estimate 축에서 이미 확인된 결함(YAML 주석 꼬리가 값에 붙는 오독)을 title 에서
    반복할 뿐이라 두지 않는다."""
    board = _load_board()
    fm, _body = board.load_ticket(path)
    title = fm.get("title") if isinstance(fm, dict) else None
    return title.strip() or None if isinstance(title, str) else None


def _ticket_body_selection_header(path: Path, ticket_id: str) -> str:
    """절 선별이 실제로 일어난 티켓 본문 앞에 붙는 frontmatter 요약 1줄(T-0703 §인터페이스 1).

    estimate 는 diff 서킷브레이커와 같은 board YAML 파서(`_parse_estimate_from_file`)를 그대로
    호출한다 — 표시용이라고 값이 갈리는 경량 스캐너를 따로 두지 않는다(F-001)."""
    title = _parse_title_from_file(path)
    touches = _parse_touches_from_file(path)
    estimate = _parse_estimate_from_file(path)
    return (
        f"id={ticket_id} · title={title or '(미상)'} · "
        f"touches=[{', '.join(touches)}] · estimate={estimate or '(미상)'}"
    )


def parse_ticket_estimate(ticket_id: str, *, pm_home: Path | None = None) -> str | None:
    """board ticket frontmatter 의 `estimate` 값 (ticket/필드 부재면 None).

    diff 서킷브레이커의 상한 선택 입력이다. 못 찾으면 **가드 off**(None)다 — `--gate` 는 자유
    문자열이 실사용이라(장부 실측 `wave4-b1`) 티켓이 아닌 이름으로 상한을 지어내면 안 된다.
    손상 frontmatter 도 같은 축이다(fail-soft) — 다만 엔진 사본 skew 는 삼키지 않는다.
    조회 실패(ticket 부재·형식 거부·board seam 로드 실패)와 파싱 실패를 **한 깔때기**에서 받는
    이유는 둘 다 같은 처방(가드 off)이라서다 — 두 try 로 나누면 조회 쪽 새 실패 형상이 그대로
    실행을 죽인다."""
    return _parse_ticket_scalar(ticket_id, "estimate", pm_home=pm_home)


def parse_ticket_claimed_rev(
    ticket_id: str, *, pm_home: Path | None = None,
) -> str | None:
    """board ticket frontmatter 의 `claimed_rev` 값 (ticket/필드 부재면 None).

    서킷브레이커 측정 폭의 claim 앵커 입력이다. 실패 처방은 estimate 축과 같다 — 조회/파싱
    실패는 앵커 없음(호출부가 옛 폭 + 경고)이고, 엔진 사본 skew 만 fail-loud 다. 형태 검증은
    앵커 해소(`claim_anchor`)가 소유한다 — 여기서는 원문 스칼라만 돌려준다."""
    return _parse_ticket_scalar(ticket_id, "claimed_rev", pm_home=pm_home)


def _parse_ticket_scalar(
    ticket_id: str, key: str, *, pm_home: Path | None = None,
) -> str | None:
    """티켓 frontmatter 스칼라 하나를 읽는 **단일 fail-soft 깔때기**(조회 + 파싱).

    서킷브레이커 입력(estimate·claimed_rev)이 같은 실패 처방을 쓰므로 흡수 경계도 하나다 —
    필드마다 try 를 늘리면 같은 규칙의 사본이 늘고 엔진 사본 skew 재전파를 한쪽만 빠뜨린다."""
    try:
        return _parse_frontmatter_scalar(
            _find_ticket_file(ticket_id, pm_home=pm_home), key)
    except Exception as exc:  # noqa: BLE001 — 조회/파싱 실패는 가드 off(ticket_finish 동형).
        if _is_engine_rev_skew(exc):
            raise  # 사본 skew 는 fail-loud(삼키지 않는다).
        return None


def _parse_frontmatter_scalar(path: Path, key: str) -> str | None:
    """ticket frontmatter 의 문자열 스칼라 (없거나 비-문자열이면 None).

    board 의 frontmatter 로더(`load_ticket` = YAML)를 형제 seam 으로 재사용한다. 자체 정규식
    파싱은 YAML 이 주석으로 읽는 꼬리(`estimate: small # reviewed`)를 값에 붙여 `small` 을
    모르는 값으로 만들었다 — 그 결과 리뷰쪽 diff 상한은 조용히 꺼지고(가드 off) 완료쪽
    (`ticket_finish.get_ticket_estimate`·같은 board 로더)은 `small` 로 읽어 **두 게이트가 다른
    값을 보는** 형상이 났다. 같은 값을 두 번 해석하지 않는 것이 유일한 해소다.

    값 정규화도 완료쪽과 같다 — 비-문자열(리스트·숫자)은 값을 지어내지 않고 None 이다."""
    board = _load_board()
    fm, _body = board.load_ticket(path)
    value = fm.get(key) if isinstance(fm, dict) else None
    return value.strip() or None if isinstance(value, str) else None


def _parse_estimate_from_file(path: Path) -> str | None:
    """ticket 파일의 `estimate` 스칼라 — **완료 게이트와 같은 파서**(표시 경로 공유 표면)."""
    return _parse_frontmatter_scalar(path, "estimate")


def _parse_touches_from_file(path: Path) -> list[str]:
    """ticket 파일에서 frontmatter touches 를 추출한다."""
    fm_text, _body = _split_ticket_frontmatter(
        _read_text_shared(path, encoding="utf-8"), source=path,
    )
    if fm_text is None:
        return []
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
    """인스턴스 overlay 또는 기본 헤더에 티켓 결정 권위 규약을 빠짐없이 결합한다."""
    if REVIEW_CONTEXT_FILE.exists():
        try:
            context = _read_text_shared(REVIEW_CONTEXT_FILE, encoding="utf-8").strip() + "\n"
            if _AUTHORITATIVE_TICKET_CONTEXT not in context:
                context += "\n" + _AUTHORITATIVE_TICKET_CONTEXT + "\n"
            return context
        except OSError:
            pass
    return _DEFAULT_CONTEXT_HEADER


def build_prompt(
    diff: str,
    ticket_body: str | None = None,
    adr_refs: list[str] | None = None,
    gate: str | None = None,
    confirm_fix: bool = False,
    confirm_fix_evidence: str | None = None,
    *,
    ticket_id: str | None = None,
    next_finding_id: str | None = None,
    confirmation_ids: Sequence[str] | None = None,
) -> str:
    """맥락 헤더 + 티켓 본문 + diff 를 결합해 표준 리뷰 프롬프트를 생성한다.

    `confirm_fix` 면 확인 전용 라운드 헌장을 앞에 얹는다 — 이 라운드는 라운드의 연장이 아니라
    직전 지적의 해소 확인이고, 새로 발견한 것은 다음 라운드 거리가 아니라 **재설계 신호**다.
    `confirm_fix_evidence`(라운드 장부가 만든 직전 must-fix 근거 블록)가 있으면 헌장 **바로
    뒤**에 싣는다 — 임무 선언과 그 임무의 대상이 붙어 있어야 fresh 세션이 무엇을 확인하는지 안다.
    `next_finding_id` 는 회수 대상 티켓에서 읽은 이 채널의 다음 ID 실값이고,
    `confirmation_ids` 는 그 티켓에서 확인할 수 있는 ID 실값 목록이다(빈 목록 = 확인 대상 없음)."""
    parts: list[str] = [
        _load_review_context().rstrip() + "\n\n",
        _OUTPUT_FORMAT_BLOCK,
        _versioned_block_requirement(next_finding_id, confirmation_ids),
    ]
    if confirm_fix:
        parts.append(_CONFIRM_FIX_CHARTER)
        if confirm_fix_evidence:
            parts.append(confirm_fix_evidence)
    if adr_refs:
        parts.append(f"관련 ADR: {', '.join(adr_refs)}\n\n")
    if gate:
        parts.append(f"게이트 ticket: {gate}\n\n")
    if ticket_body is not None:
        ticket_label_value = ticket_id or gate
        ticket_label = f" ({ticket_label_value})" if ticket_label_value else ""
        parts.append(f"### 게이트 티켓 본문{ticket_label}\n")
        parts.append(ticket_body)
        if not ticket_body.endswith("\n"):
            parts.append("\n")
        parts.append("\n")
    parts.append("### 리뷰 대상 diff\n")
    if diff.strip():
        parts.append("```diff\n")
        parts.append(diff)
        parts.append("```\n")
    else:
        parts.append("(변경 diff 없음 — 경로에 해당하는 변경사항이 없거나 base 와 동일)\n")
    return "".join(parts)


# ── 추가 리뷰어 가시 범위 격리 ────────────────────────────────────────────
#
# 추가 리뷰어의 판정 입력은 **프롬프트에 실린 diff** 여야 한다. 그런데 스폰된 리뷰어 프로세스는
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

# **세션 재사용(resume) 비지원 — 격리 우선**: 이 축은 라운드마다 임시 홈·전사 없는 환경으로 돌아
# 세션 연속성을 의도적으로 끊는다(빈 설정 홈에서 재개는 대화 부재로 실패한다). 그래서 리뷰어는
# 항상 fresh + full payload 이고, 그 완화는 이 격리를 무효화하므로 하지 않는다. 위임 채널의 재사용
# 배선은 그쪽 장부(raw 레코드 행)에만 있고 라운드 장부 스키마는 확장하지 않는다.

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
    거울에 실리면 격리가 통째로 무의미해진다. 기계 mirror도 payload와 같은 검토 제외 판정을 타서
    손편집 출하 manifest만 보존하며, ignore 규칙에 의존하지 않는 두 번째 자물쇠다.
    """
    candidate = PurePosixPath(relative)
    return _is_review_machine_mirror_path(relative) or any(
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
    _restrict_to_owner(home)
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
        _restrict_to_owner(target.parent)
        if payload is None:
            shutil.copy2(source, target)
        else:
            target.write_text(payload, encoding="utf-8", newline="\n")
        _restrict_to_owner(target)
        copied.append(relative)
    return ReviewerHomeBuild(tuple(copied), tuple(scrub_failed))


def _scrubbed_json_text(source: Path, drop_keys: Sequence[str]) -> str | None:
    """최상위 선언 키를 떼어낸 JSON 본문 — 읽기/파싱/형식이 어긋나면 None(복제 안 함)."""
    try:
        payload = json.loads(_read_text_shared(source, encoding="utf-8"))
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
        text = _read_text_shared(source, encoding="utf-8")
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


def _remove_partial_container(container: Path) -> None:
    """구성 실패로 남은 부분 컨테이너를 지운다 — **원래 실패를 덮지 않되 침묵하지도 않는다**.

    이 자리의 정리는 실패 처리 도중이라 예외를 새로 올리면 진짜 원인(격리 실패 사유)이 가려진다.
    그렇다고 `ignore_errors=True` 로 삼키면 검토 대상 저장소 사본과 홈 인증 사본이 디스크에
    남는데도 아무도 모른다(Windows 의 read-only git object 가 실제로 그렇게 만든다). 그래서
    **지우되, 못 지웠으면 경로를 loud 하게 알린다**.
    """
    try:
        _force_rmtree(container)
    except Exception as exc:  # noqa: BLE001 — 정리 실패가 원래 실패를 대체하면 안 된다.
        # 삭제 수단이 형제 모듈(`file_lock.force_rmtree`)이라 사본 불일치도 이 경계에 닿는다.
        # 등록된 사유로 흡수하되 원인을 문구로 구분한다 — 잔존 경로 안내와 재동기 처방이 같은
        # 경고로 뭉개지면 둘 다 실행되지 않는다.
        skew = _absorb_engine_rev_skew_for_recovery(exc, "partial_container_cleanup")
        cause = f"엔진 사본 불일치 — {exc}" if skew else f"{exc}"
        print(
            "[external-review] 경고: 부분 격리 컨테이너 정리 실패 — 저장소 사본과 인증 파일 "
            f"사본이 남아 있을 수 있습니다. 직접 지우세요: {container} ({cause})",
            file=sys.stderr,
        )


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
        _remove_partial_container(container)
        raise
    except OSError as exc:
        _remove_partial_container(container)
        raise ReviewerWorkspaceError(f"격리 작업 루트 구성 실패: {exc}") from exc
    except Exception:
        _remove_partial_container(container)
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
    """격리 컨테이너 정리 — 실패를 조용히 삼키지 않는다(저장소 사본이 남는다는 뜻이다).

    거울에는 `git init` 한 저장소가 들어 있고 git 은 object·packfile 을 read-only 로 만든다 —
    맨 `shutil.rmtree` 는 Windows 에서 그 속성에 막혀 컨테이너를 통째로 남긴다(실측). 공용
    seam 이 속성을 풀고 재시도하며, 그러고도 남으면 아래 경고가 사용자에게 자리를 알린다.
    """
    try:
        _force_rmtree(workspace.root)
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


# ── 추가 리뷰어 실행 ──────────────────────────────────────────────────────


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


def _load_delegate_transport():
    """opencode sandbox 전달 사본 seam을 형제 pm_delegate에서 지연 로드한다.

    위임 축이 확립한 O_EXCL·0600 생성, 자기-은닉 ignore, fd containment와 정리를 리뷰 축에서도
    그대로 호출한다. pm_delegate가 external_review의 다른 판정을 지연 로드하는 반대 방향 seam을
    갖지만, 이 로드는 두 모듈의 import 완료 뒤 opencode 실행 시점에만 일어나 순환 import를 만들지
    않는다.
    """
    path = Path(__file__).resolve().parent / "pm_delegate.py"
    return _load_module_from_path(
        path, "pm_delegate.py", verifier=_verify_engine_rev, cache=True,
    )


def _reviewer_idle_timeout(reviewer_cmd: str, idle_timeout: float | None) -> float | None:
    """리뷰어 축의 무진행 상한 해소 — 공용 선언 테이블(`idle_timeout_for_signal`)을 그대로 탄다.

    리뷰어 기본 커맨드(`codex exec --sandbox read-only`)는 `--json` 이 없어 이벤트 스트림이 아니라
    **평문 증분** 축이다. 그래도 진행 신호는 있다 — 실측상 진행 로그(hook/exec/succeeded 라인)가
    stderr 로 촘촘히 흐르고(리뷰 1건 12,233줄) stdout 은 최종 회신뿐이라(498~759 바이트), chunk
    도착 자체를 신호로 보면 파서 없이 같은 판정이 선다. 특례 분기 없이 축 선언만 다르다.
    명시값이 없으면 해소 command가 가리키는 하네스 프로필 값을 쓴다(위임 축과 같은 테이블)."""
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
                           cwd=None, env=None, on_spawn_attempt=None,
                           **_ignored) -> subprocess.CompletedProcess:
    """기본 리뷰어 러너 — pm_relay 공용 워치독 경유(무진행 주 판정 + 벽시계 백스톱).

    `subprocess.run(..., timeout=)` 을 대체한다. 그 단일 호출은 증분 관측이 아예 없어서 (a) 정상
    진행을 벽시계로 죽이고 (b) `TimeoutExpired.stdout` 에 실려 온 부분 산출물을 아무도 안 읽어
    통째로 버렸다(kill 된 리뷰의 raw 가 헤더 138바이트뿐이던 실측). startup 창/재시도는 리뷰어
    이름 특례가 아니라 **해소된 공유 프로필 선언**을 따른다. 따라서 기본 codex(False)는 종전처럼
    꺼져 있고, 알려진 startup stall 축인 opencode(True)만 유한 재시도를 얻는다.

    `cwd`/`env` 는 리뷰어 가시 범위 격리 입력이다 — 시그니처에 명시해 relay 워치독까지 내려보낸다
    (`**_ignored` 가 삼키면 격리가 조용히 사라진다).

    `on_spawn_attempt` 도 같은 이유로 명시한다 — 이 러너의 **앞부분**(relay 로드·프로필 해소·
    워치독 준비)은 자식이 아직 없는 구간이라, 소유권 이전 콜백을 이 함수 입구에서 태우면 스폰 0·
    전송 0 으로 끝난 준비 실패가 예산을 먹는다. 콜백은 relay 워치독의 실 스폰 경계까지 그대로
    내려간다."""
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
        on_spawn_attempt=on_spawn_attempt,
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


def _declares_keyword(signature: inspect.Signature, name: str) -> bool:
    """runner 가 이 키워드를 **이름으로 선언**했는가(`**kwargs` 흡수는 선언이 아니다)."""
    parameter = signature.parameters.get(name)
    return parameter is not None and parameter.kind in (
        inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _reviewer_run_kwargs(run_fn: Callable, argv: list[str], *,
                         prompt: str, timeout: int, idle_timeout: float | None,
                         cwd: str | None = None,
                         env: dict[str, str] | None = None,
                         on_spawn_attempt: Callable[[], None] | None = None) -> dict:
    """기존 subprocess.run 호환 seam 을 보존하고 명시 지원 runner 에만 확장 키를 전달한다.

    `**kwargs` 만으로는 새 키를 실제 소비하는지 알 수 없다(`subprocess.run` 은 **kwargs 를 Popen 에
    넘겨 뒤늦게 TypeError). 따라서 시그니처에 `idle_timeout`·`on_spawn_attempt` 가 명시된 runner
    에만 전달한다. 호출 전에 bind 해 seam skew 를 프로세스 실행 오류와 분리한다.

    `on_spawn_attempt`(스폰 경계 seam)를 선언하지 않은 기존 주입 러너의 kwargs 는 종전과 바이트
    단위로 같다 — 그 러너의 스폰 경계는 호출부가 알 수 없으므로 콜백은 호출 직전에 태운다.

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
    if _declares_keyword(signature, "idle_timeout"):
        kwargs["idle_timeout"] = idle_timeout
    if on_spawn_attempt is not None and _declares_keyword(signature, "on_spawn_attempt"):
        kwargs["on_spawn_attempt"] = on_spawn_attempt
    try:
        signature.bind(argv, **kwargs)
    except TypeError as exc:
        raise ReviewerRunSeamError(str(exc)) from exc
    return kwargs


# exec 자체가 실패해 자식이 **뜬 적 없는** 확정 기동 실패 — 전송 0·과금 0 이라 예약 환불 대상이다.
#   FileNotFoundError    실행 파일 부재/PATH 미해소
#   PermissionError      실행 권한 없음(비실행 파일·noexec 마운트)
#   NotADirectoryError   경로 중간 요소가 디렉토리가 아님(cwd·실행 파일 경로)
#   IsADirectoryError    실행 대상이 디렉토리
# 종류로 잡는 이유는 주입 러너(`subprocess.run` 호환)가 표식 없이 이 예외들을 그대로 올리기
# 때문이고, relay 표식이 있으면 그쪽이 이긴다(`_launch_failed_definitely`).
_DEFINITE_LAUNCH_FAILURES = (
    FileNotFoundError, PermissionError, NotADirectoryError, IsADirectoryError,
)


def _launch_failed_definitely(exc: BaseException) -> bool:
    """이 예외가 "자식이 뜬 적 없음"을 **확정**하는가.

    1순위는 relay 가 스폰 경계에서 붙인 표식(`spawn_failed`)이다 — 그 층만 `Popen` 반환 전후를
    실제로 본다. 그래서 종류표에 없는 pre-child 거절(argv NUL 의 `ValueError`)도 표식이 True 면
    여기서 True 이고, 반대로 `Popen` 성공 뒤의 실패에 종류표의 예외가 실려 와도 표식이 False 라
    여기서도 False 다(재시도 도중 자식이 한 번이라도 떴던 실행 포함).
    표식이 없는 경로(주입 러너·직접 호출)는 예외 종류만으로 보수적으로 판정한다.
    """
    marked = getattr(exc, "spawn_failed", None)
    if marked is not None:
        return bool(marked)
    return isinstance(exc, _DEFINITE_LAUNCH_FAILURES)


def _started_after(exc: BaseException, seam_reached: bool) -> bool:
    """스폰 표식이 있으면 그것으로, 없으면 seam 위치로 `started` 를 정한다 (단일 우선순위).

    우선순위는 한 줄이다 — `spawn_failed=True` → started=False(자식 없음·환불 대상),
    `spawn_failed=False` → started=True(자식 있었음·환불 금지), **표식 없음** → 종전대로 스폰 경계
    콜백이 돌았는지(`seam_reached`). 표식은 `Popen` 앞뒤를 실제로 본 층(relay)이 붙이므로 경계
    콜백의 위치 추정보다 강하다: 콜백이 아직 안 돌았어도 relay 가 '자식 있었음'을 봤으면 그
    실행은 이미 전송됐을 수 있고, 반대로 콜백이 돈 뒤라도 relay 가 '자식 없음'을 확정했으면 전송
    0 이다. 이 판정을 분기마다 다시 쓰면 한 분기만 표식을 빠뜨려도 예산이 조용히 새므로 여기 한
    곳이 소유한다.
    """
    marked = getattr(exc, "spawn_failed", None)
    if marked is not None:
        return not bool(marked)
    return seam_reached


# 두 사건의 진단 표식 — 흡수되면 안 되는 경계다. "설정상 리뷰어 없음"(채택자가 리뷰어를 아예
# 선언하지 않음)은 정상 형상이지만, "실행 파일 해소 실패"(선언은 있는데 그 실행 파일로 자식을
# 띄우지 못함)는 **교차검증이 돌지 않은 결함**이다. 한 문구로 합치면 후자가 전자로 흡수돼
# 조용히 환불된다 — Windows 실행 경로 분해 결함이 여러 릴리즈 동안 숨어 있던 경로다.
NO_REVIEWER_CONFIGURED_MARKER = "추가 리뷰어 미설정"
REVIEWER_LAUNCH_FAILURE_MARKER = "리뷰어 실행 파일 해소 실패"
NO_REVIEWER_CONFIGURED_OUTPUT = (
    f"[{NO_REVIEWER_CONFIGURED_MARKER} — reviewer_cmd 에 실행할 명령이 없습니다"
    " (local.conf 확인). 외부 전송은 일어나지 않았습니다]"
)


def _launch_failure_output(
    argv: list[str], exc: BaseException, reviewer_cmd: str | None = None,
) -> str:
    """확정 기동 실패의 사람 진단 1줄 — 원인축(해소 실패 vs 실행 불가)을 구분해 말한다.

    "설정상 리뷰어 없음"(`NO_REVIEWER_CONFIGURED_OUTPUT`)과 갈라진 문구를 쓴다 — 이 실행은 리뷰어가
    선언돼 있는데도 교차검증이 돌지 않은 실행이다. 선언 문자열과 실제로 시도한 실행 파일이 다르면
    (분해·인용 사고) 그 대조를 같이 낸다: Windows 채택자가 본 증상이 "설치 또는 PATH 확인"뿐이라,
    엔진이 경로를 뭉갠 실행과 리뷰어를 안 깐 실행을 구분할 수 없었다.
    """
    attempted = argv[0] if argv else ""
    if isinstance(exc, FileNotFoundError):
        return (
            f"[{REVIEWER_LAUNCH_FAILURE_MARKER}: '{attempted}' 로 자식을 띄우지 못했습니다 — "
            f"설치·PATH·경로 표기 확인{_declared_command_mismatch(attempted, reviewer_cmd)}. "
            "추가 리뷰어 교차검증은 실행되지 않았습니다(외부 전송 없음)]"
        )
    return (
        f"[리뷰어 명령 '{attempted}' 를 실행할 수 없음 ({type(exc).__name__}: {exc}) — "
        f"실행 권한·경로 확인{_declared_command_mismatch(attempted, reviewer_cmd)}. "
        "외부 전송은 일어나지 않았습니다]"
    )


def _declared_command_mismatch(attempted: str, reviewer_cmd: str | None) -> str:
    """시도한 실행 파일이 선언 문자열에 없으면 그 대조를 진단에 덧붙인다 (빈 문자열 = 일치).

    분해가 경로를 훼손하면 argv[0] 은 채택자가 local.conf 에 적은 어떤 부분문자열도 아니다 —
    "설치 안 됨"과 "엔진이 커맨드를 잘못 분해함"을 사람이 그 자리에서 가를 수 있는 유일한 사실이다.
    """
    if not reviewer_cmd or not attempted or attempted in reviewer_cmd:
        return ""
    return (
        f" · 선언한 커맨드에 없는 실행 파일로 분해됐습니다(local.conf: '{reviewer_cmd}') — "
        "경로 구분자·인용을 확인하세요"
    )


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
    argv: Sequence[str] | None = None,
    stdin_text: str | None = None,
    on_spawn_attempt: Callable[[], None] | None = None,
) -> tuple[bool, ReviewerOutput, bool]:
    """run_reviewer 본체 + 외부 프로세스 스폰 여부(started) 신호.

    반환: (성공 여부, `ReviewerOutput`(회신/로그 **분리 보관**), started). started=False = 외부 프로세스가 *확실히 시작되지
    않음*(전송 0·과금 0) — 빈 reviewer_cmd·seam 계약 오류·**스폰 경계 전의 준비 실패**·확정 기동
    실패(exec 이 실패한 `FileNotFoundError`·`PermissionError`·`NotADirectoryError`·
    `IsADirectoryError`). started=True = 스폰됨(프롬프트가 전송·과금됐을 수 있음) — 정상 종료
    (비-0 rc 포함)·타임아웃·스폰 뒤의 실행 오류. 타임아웃/불확실은 이미 전송됐을 수 있으므로
    보수적으로 started=True — 라운드 환불은 started=False 일 때만 해(반복 타임아웃으로 상한을
    무한 우회하지 못하게). 확실히 전송 전인 경우만 환불한다.

    **판정 기준**: 기본 러너가 `subprocess.run` 단일 호출에서 pm_relay 공용 워치독으로
    바뀌어, 주 판정이 "시작 후 경과"가 아니라 "마지막 진행 이후 무진행"이다(벽시계는 백스톱).
    `idle_timeout=None` 이면 reviewer_cmd 프로필 선언이 적용된다.

    `run_fn` 주입의 기존 계약은 subprocess.run 호환 키까지다. 새 `idle_timeout` 은 시그니처에 그
    이름을 명시한 runner 에만 조건부 전달한다. 호출 계약 불일치는 일반 "리뷰어 실행 오류"로
    삼키지 않고 seam 오류로 loud 구분한다.

    `argv`/`stdin_text` = 구조화 대상의 wire transport 주입. 미지정이면 종전대로
    `_split_reviewer_argv(reviewer_cmd)`(실행 플랫폼 규칙 분해) + 프롬프트 stdin 이라, legacy 자유
    문자열 커맨드의 실행 형상은 바이트 단위로 동일하다(구조화 플래그/파서로 다시 쓰지 않는다).

    `on_spawn_attempt` = **실제 자식 생성 직전** 1회 호출되는 seam(기본 None = 종전 동작). 라운드
    예약 소유권 이전(`_PreSpawnReservation.hand_off`)이 이 콜백을 탄다. 호출 지점은 러너가 스폰
    경계를 선언했는지에 따라 두 자리다:

    · 선언한 러너(기본 워치독 러너 — `on_spawn_attempt` 키워드를 이름으로 가짐)면 콜백을 **러너
      안까지** 내려보내, relay 워치독이 `Popen` 하기 바로 앞에서 돈다. 러너 입구~`Popen` 사이의
      준비 구간(relay 로드·프로필 해소·워치독 셋업)에서 실패하면 콜백이 아직 돌지 않았으므로
      이 함수는 `started=False` 로 돌아간다 — 스폰 0·전송 0 인 실행이 예산을 먹지 않는다.
    · 선언하지 않은 기존 주입 러너면 종전 그대로 **호출 직전**에 한 번 돈다(호환 보존). 그 러너의
      내부 스폰 경계는 알 수 없으므로 그 뒤의 불확실 실패는 보수적으로 started=True 다.

    콜백은 어느 경로로도 최대 1회만 돈다(멱등 래퍼). 러너가 정상 반환했는데 콜백을 부르지 않은
    형상은 자식이 있었던 것으로 보고 반환 직후 한 번 돌린다 — 그러지 않으면 스폰된 실행의 예산이
    조용히 환불된다.

    콜백이 **돈 뒤에** 돌아온 `started=False`(확정 기동 실패)는 자식이 없었다고 판명된 결과라,
    호출부(`run_review` 의 `on_no_spawn`)가 그 자리에서 소유권을 되찾는다 — 판정 자체는 이 함수의
    반환값 하나가 소유하고, 되돌림 시점만 호출부 계약이다. 확정 기동 실패의 판정 입력은 relay 가
    붙이는 스폰 표식(`spawn_failed`)이 1순위, 없으면 예외 종류다 — `Popen` 성공 **뒤**의 실패에
    같은 예외 종류가 실려 오면 relay 표식이 그것을 post-spawn 으로 못박아 보수적 started=True 가
    유지된다. 그 우선순위(표식 True→False·표식 False→True·표식 없음→스폰 경계 위치)는 확정 기동
    실패 분기만이 아니라 seam 호출 오류(`TypeError`)·일반 실행 오류 분기에도 **같이** 적용된다
    (`_started_after` 한 곳이 소유). 한 분기만 표식을 무시해도 그 자리로 예산이 샌다."""
    if metrics is not None:
        metrics.clear()
        metrics.update({"rc": 1, "silence_sec": None})
    _run = run_fn or _watchdog_reviewer_run
    argv = list(argv) if argv is not None else _split_reviewer_argv(reviewer_cmd)
    if not argv:
        # "설정상 리뷰어 없음" — 실행 파일 해소 실패(`_launch_failure_output`)와 **다른 사건**이다.
        return False, ReviewerOutput(NO_REVIEWER_CONFIGURED_OUTPUT), False
    if timeout is None:  # 미지정 호출(공개 facade) — 리뷰어 프로필의 벽시계 백스톱.
        timeout = int(reviewer_profile(reviewer_cmd).wall_timeout)
    # 스폰 경계를 실제로 지났는가 — 확실치 않은 실패의 started 판정 입력이다. 경계 전이면 자식이
    # 없으므로 전송 0 이고, 경계 뒤면 프롬프트가 이미 나갔을 수 있다.
    spawn_reached = False

    def _spawn_seam() -> None:
        """스폰 경계 1회 통과 — 예약 소유권을 호출부 계약대로 넘긴다(멱등)."""
        nonlocal spawn_reached
        if spawn_reached:
            return
        spawn_reached = True
        if on_spawn_attempt is not None:
            on_spawn_attempt()

    try:
        kwargs = _reviewer_run_kwargs(
            _run, argv,
            prompt=prompt if stdin_text is None else stdin_text,
            timeout=timeout,
            idle_timeout=_reviewer_idle_timeout(reviewer_cmd, idle_timeout),
            cwd=None if cwd is None else str(cwd), env=env,
            on_spawn_attempt=_spawn_seam,
        )
        # ── 스폰 경계 seam ──────────────────────────────────────────────
        # 스폰 경계를 **이름으로 선언한** 러너면 콜백이 그 안(relay 의 `Popen` 직전)에서 돈다 —
        # 러너 입구~Popen 사이의 준비 실패는 스폰 0·전송 0 이라 예약이 환불 대상으로 남는다.
        # 선언하지 않은 기존 주입 러너는 종전 자리(호출 직전)에서 한 번 돈다(호환 보존).
        if "on_spawn_attempt" not in kwargs:
            _spawn_seam()
        result = _run(argv, **kwargs)
        # 러너가 결과를 돌려줬다 = 자식이 떴다. 스폰 경계를 선언해 놓고 부르지 않은 러너가 있어도
        # 여기서 소유권이 넘어간다 — 스폰된 실행의 예산이 조용히 환불되지 않게 하는 백스톱이다.
        _spawn_seam()
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
        # 타임아웃은 정의상 "자식을 기다리다 만료"라 스폰 경계 신호와 무관하게 보수적으로 True 다
        # (반복 타임아웃으로 상한을 무한 우회하는 길을 열지 않는다·MF-A).
        if metrics is not None:
            metrics["silence_sec"] = getattr(
                exc, "silence_seconds", getattr(exc, "idle_seconds", None)
            )
        return False, ReviewerOutput(_timeout_output(timeout, exc)), True
    except _DEFINITE_LAUNCH_FAILURES as exc:
        # exec 자체가 실패해 자식이 뜬 적 없다(파일 부재·실행 권한·경로 형상) → started=False
        # (환불 대상). 단 relay 표식이 "Popen 성공 뒤"라고 못박은 예외는 예외다 — 그건 자식이
        # 있었던 실행이라 보수 규칙으로 내려보낸다.
        if not _launch_failed_definitely(exc):
            return False, ReviewerOutput(f"[리뷰어 실행 오류: {exc}]"), True
        if metrics is not None:
            metrics["rc"] = 127 if isinstance(exc, FileNotFoundError) else 126
        return (False,
                ReviewerOutput(_launch_failure_output(argv, exc, reviewer_cmd)),
                False)
    except ReviewerRunSeamError as exc:
        # 호출 전 bind 실패 — 외부 프로세스는 확실히 시작되지 않았다. 라운드 환불 가능.
        return False, ReviewerOutput(
            f"[리뷰어 runner seam 계약 오류 — 호출 전 차단: {exc}]"), False
    except TypeError as exc:
        # runner 내부에서 발생한 키워드/시그니처 skew. 판정은 다른 분기와 **같은 우선순위**다 —
        # relay 표식이 있으면 그것이 이기고(True→환불·False→환불 금지), 없을 때만 스폰 경계 위치를
        # 본다. 표식을 무시하고 경계 위치만 보면, `Popen` 뒤에 실려 온 TypeError 가 콜백이 아직 안
        # 돈 러너에서 올라올 때 이미 나간 전송이 환불된다.
        return (False, ReviewerOutput(f"[리뷰어 runner seam 호출 오류: {exc}]"),
                _started_after(exc, spawn_reached))
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
        if _launch_failed_definitely(exc):
            # relay 가 스폰 경계에서 "자식 없음"으로 표식한 실패 — 종류는 exec `OSError` 만이
            # 아니다(argv NUL 의 `ValueError` 처럼 `Popen` 이 fork 전에 거절한 형상도 여기 온다).
            # 자식이 없었으므로 전송 0·과금 0 이고 환불 대상이다.
            return (False,
                    ReviewerOutput(_launch_failure_output(argv, exc, reviewer_cmd)),
                    False)
        # 스폰 경계 뒤의 실패는 시작 여부가 불확실하다 — 보수적으로 started=True (상한 우회 방지 >
        # 과잉 카운트). 경계 **전**의 실패는 자식이 아직 없었다는 사실이 확정이라 False 다. 단
        # relay 가 '자식 있었음'(False)으로 못박은 예외는 경계 콜백이 아직 안 돌았어도 True 다 —
        # 위 `_DEFINITE_LAUNCH_FAILURES` 분기와 같은 우선순위(표식 > 위치)를 여기서도 쓴다.
        return (False, ReviewerOutput(f"[리뷰어 실행 오류: {exc}]"),
                _started_after(exc, spawn_reached))


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
    argv = _split_reviewer_argv(reviewer_cmd)
    return _normalized_reviewer_key(argv)


def _reviewer_model(reviewer_cmd: str) -> str:
    """legacy argv의 model처럼 보이는 토큰을 읽는 호환 관측 seam.

    이 값은 **정체가 아니다**. 임의 실행기 문자열의 옵션 의미를 엔진이 보증할 수 없으므로 실제
    provenance는 `ReviewerTarget.ledger_model == unpinned-model`을 사용한다."""
    argv = _split_reviewer_argv(reviewer_cmd)
    for flag in ("--model", "-m"):
        if flag in argv:
            index = argv.index(flag)
            if index + 1 < len(argv):
                return argv[index + 1]
    return LEGACY_UNSPECIFIED_MODEL


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
    """추가 리뷰 raw/공유 장부 위치 — 앵커는 해소된 소유 PM 홈(미해소만 tempdir 폴백).

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
    """실행 전 장부가 가리킬 추가 리뷰 raw를 충돌 없이 선점한다."""
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
    target: ReviewerTarget | None = None,
    codex_egress: str | None = None,
) -> None:
    with dest.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(_review_raw_content(
            content, local_conf_path, resolved_profile, target, codex_egress))


def save_output(reviewer: str, content: str, output_dir: Path | None = None, *, local_conf_path: Path | None = None, resolved_profile: str | None = None, target: ReviewerTarget | None = None, codex_egress: str | None = None) -> Path:
    """추가 리뷰어 출력 원문(+선택적 conf provenance 감사 헤더)을 저장하고 경로를 반환한다."""
    dest = _reserve_output(reviewer, output_dir)
    _write_reserved_output(
        dest,
        content,
        local_conf_path=local_conf_path,
        resolved_profile=resolved_profile,
        target=target,
        codex_egress=codex_egress,
    )
    return dest


# ── 실행 + 수합 ────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _structured_transport(
    target: ReviewerTarget, prompt: str, cwd: Path | str | None,
) -> Iterator[tuple[list[str] | None, str | None]]:
    """구조화 대상의 (argv, stdin) 을 준비하고 임시 자원을 **모든 종료 경로**에서 정리한다.

    codex/claude 는 프롬프트를 stdin 으로 받으므로 준비할 자원이 없다. opencode 만 `--file` 첨부라
    리뷰 sandbox 하위에 0600 프롬프트 파일을 만들고(프롬프트에는 검토 대상 diff 원문이 들어간다 —
    다른 사용자에게 읽히면 안 된다) 실행 성공·실패·예외 무관하게 지운다. 생성·containment·
    자기-은닉·정리는 위임 축 `pm_delegate`의 공용 seam을 그대로 쓴다. legacy 대상은 이 경로를
    타지 않는다."""
    if not target.structured:
        yield None, None
        return
    if target.harness != "opencode":
        yield _structured_reviewer_argv(
            target.harness, target.model, target.reasoning,
            cwd=str(cwd) if cwd is not None else REVIEWER_CWD_PLACEHOLDER,
            prompt_file=REVIEWER_PROMPT_FILE_PLACEHOLDER,
        ), None
        return
    sandbox = Path(cwd) if cwd is not None else Path.cwd()
    delegate = _load_delegate_transport()
    prompt_file = delegate._save_opencode_transport_prompt(sandbox, prompt)
    try:
        # 생성 뒤 argv 조립 직전에도 위임 축과 같은 containment guard를 다시 건다. 생성 seam이
        # fd로 고정한 실 sandbox를 그대로 써 사용자 입력 cwd symlink 재해소 창을 열지 않는다.
        delegate._assert_opencode_transport_path(prompt_file.sandbox, prompt_file)
        # 생성 seam의 `0600`은 **Windows에서 아무 제한도 걸지 않는다**(실측 `S_IMODE`=0o666).
        # 이 파일에는 검토 대상 diff 원문이 들어가므로 플랫폼 등가 수단(ACL)으로 소유자 전용
        # 접근을 실제로 건다 — containment 재확인과 같은 자리에서 접근 경계도 재확인한다.
        transport_dir = Path(prompt_file.path).parent
        for artifact in (transport_dir, Path(prompt_file.path),
                         transport_dir / delegate._OPENCODE_TRANSPORT_IGNORE):
            if artifact.exists():
                _restrict_to_owner(artifact)
        yield _structured_reviewer_argv(
            target.harness, target.model, target.reasoning,
            cwd=str(prompt_file.sandbox),
            prompt_file=str(prompt_file),
        ), ""
    finally:
        delegate._cleanup_attempt_transport(prompt_file)


# 스폰 전 중단으로 마감하는 raw 레코드의 rc — 리뷰를 하나도 받지 못한 실행이라 실패 축(1)이다
# (빈 커맨드·실행 파일 부재 같은 다른 '스폰 없음' 마감과 **같은 축**이라 조회면의 뜻이 갈리지
# 않는다). "왜 실패했나"는 그 레코드가 가리키는 raw 파일의 중단 사유 줄이 소유한다 — 장부 스키마에
# 새 상태 값을 만들지 않는다(마감 API 는 rc/경과/침묵만 받는다).
_PRE_SPAWN_ABORT_RC = 1


def _abort_pre_spawn_raw(
    relay, ledger_path: Path | None, record_id: str | None, raw_path: Path, *,
    reason: str, elapsed: float, local_conf_path: Path | None,
    resolved_profile: str | None, target: ReviewerTarget | None,
    codex_egress: str | None,
) -> None:
    """스폰 **전에** 끊긴 실행이 남긴 raw 선점/미마감 레코드를 그 자리에서 정직하게 닫는다.

    raw 장부의 미마감 레코드는 "떠 있을지 모르는 자식"(고아 프로세스 조회면 `--unfinished` 의
    입력)이라는 뜻이다. 스폰이 확실히 없던 실행이 그 상태로 남으면 조회면이 없는 프로세스를
    가리키고, 0바이트 raw 파일은 "리뷰어가 아무 말도 안 했다"로 읽힌다. 그래서 두 경우로 닫는다:

    · 장부가 이 raw 를 가리키기 전에 끊겼으면(레코드 없음) 선점 파일만 지운다 — 되돌릴 원자 단위가
      그것 하나다.
    · 레코드가 있으면 raw 에 중단 사유를 박제하고 레코드를 실패 축으로 마감한다.

    정리 실패는 loud 경고만 남기고 삼킨다 — 이 구간의 **주 예외**(호출부가 다시 던진다)가 진단을
    소유하고, 정리 예외가 그것을 덮으면 원인이 사라진다."""
    if record_id is None or ledger_path is None:
        try:
            raw_path.unlink()
        except OSError as exc:
            print(
                f"경고: 스폰 전 중단의 raw 선점 정리 실패 ({type(exc).__name__}: {exc}) — "
                f"빈 파일이 남았습니다: {raw_path}",
                file=sys.stderr,
            )
        return
    try:
        _write_reserved_output(
            raw_path,
            f"[스폰 전 중단 — 외부 프로세스 시작 없음(전송 0·과금 0): {reason}]",
            local_conf_path=local_conf_path,
            resolved_profile=resolved_profile,
            target=target,
            codex_egress=codex_egress,
        )
    except Exception as exc:  # noqa: BLE001 — 정리 실패가 주 예외를 덮지 않는다
        print(
            f"경고: 스폰 전 중단 사유의 raw 박제 실패 ({type(exc).__name__}: {exc}) — "
            f"빈 파일이 남았습니다: {raw_path}",
            file=sys.stderr,
        )
    try:
        relay.finish_raw_record(
            ledger_path, record_id,
            rc=_PRE_SPAWN_ABORT_RC, elapsed_sec=elapsed, silence_sec=None,
        )
    except Exception as exc:  # noqa: BLE001 — 정리 실패가 주 예외를 덮지 않는다
        # 사본 불일치(marked skew)도 여기서는 흡수한다 — 등록된 경계 사유가 그 근거다. 다만
        # 원인을 문구로 구분해 "장부 도구가 이 엔진과 다른 사본"이라는 사실이 지워지지 않게 한다.
        skew = _absorb_engine_rev_skew_for_recovery(exc, "abort_pre_spawn_raw")
        cause = f"엔진 사본 불일치 — {exc}" if skew else f"{type(exc).__name__}: {exc}"
        print(
            f"경고: 스폰 전 중단 레코드 마감 실패 ({cause}) — raw 장부에 "
            f"미마감 레코드가 남았습니다(실제로는 스폰 0): {ledger_path}",
            file=sys.stderr,
        )


def run_review(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int | None = None,
    output_dir: Path | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    idle_timeout: float | None = None, local_conf_path: Path | None = None, resolved_profile: str | None = None,
    cwd: Path | str | None = None, env: dict[str, str] | None = None,
    target: ReviewerTarget | None = None, codex_egress: str | None = None,
    on_spawn_attempt: Callable[[], None] | None = None,
    on_no_spawn: Callable[[], None] | None = None,
) -> dict:
    """추가 리뷰어를 실행하고 결과를 수합한다.

    반환 dict: reviewer / ok / output / verdict / contamination / file / failed / started /
    any_must_fix / all_pass.
    `target` = 해소된 추가 리뷰어(미지정이면 `reviewer_cmd` 의 legacy 대상). 구조화 대상은 argv 를
    직접 조립하고 회신을 **공용 wire 파서**로 추출해 판정에 넣는다 — JSON/스트림 원문을 그대로
    판정 파서에 넣으면 이벤트 필드에 실린 문구가 판정으로 오독된다. raw 박제는 추출 전 wire
    (stdout+stderr)를 그대로 보존한다.
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

    `on_spawn_attempt` = 라운드 예약 소유권 이전 seam(기본 None = 종전 동작). 이 함수의 **앞부분**
    (raw 선점·장부 시작 레코드·구조화 transport·argv/kwargs 준비)과 러너 안의 준비 구간
    (relay 로드·프로필 해소·워치독 셋업)은 아직 확실히 전송 전이라,
    거기서 예외로 나가면 콜백은 돌지 않는다 — 호출부의 스폰 전 seam 이 예약을 한 번 환불하고
    예외는 원본 그대로 다시 던져진다. 나가는 길에 이 함수가 남긴 raw 선점/미마감 레코드도
    함께 닫는다(`_abort_pre_spawn_raw`).

    `on_no_spawn` = 그 이전의 **대칭 반납** seam(기본 None = 종전 동작). 러너가 `started=False`
    로 돌아오면 자식은 뜬 적이 없다고 판명된 것이라, 회신 파싱·raw 박제·장부 마감 **어느 것도
    하기 전에** 한 번 호출해 소유권을 호출부로 돌려준다. 그 뒤 이 함수의 수합 구간에서 예외로
    나가면 (1) 호출부의 스폰 전 seam 이 예약을 한 번 환불하고 (2) 여기서 raw 선점/미마감 레코드를
    같은 보상 경로로 닫는다 — 스폰이 없던 실행이 "떠 있을지 모르는 자식"으로 장부에 남지 않는다.
    이미 정상 마감된 레코드는 다시 닫지 않는다(이중 마감 없음). 스폰된 실행(started=True)의 수합
    실패는 종전 보수 규칙 그대로다 — 환불도 보상도 없이 미완으로 남는다.
    """
    target = target or ReviewerTarget(REVIEWER_SOURCE_LEGACY, reviewer_cmd)
    name = target.name
    raw_path = _reserve_output(name, output_dir)
    # ── 여기부터 스폰 시도 seam 까지가 "확실히 전송 전" 구간이다 ──────────
    # raw 선점(mkdir/open)·장부 시작 레코드·구조화 transport·argv/kwargs 준비가 전부 이 안이고,
    # 자식은 아직 없다. 이 구간에서 예외로 나가면 (1) 예약 소유권을 넘기지 않아 호출부의 스폰 전
    # seam 이 한 번 환불하고, (2) 이 함수가 남긴 raw 선점/미마감 레코드를 정직하게 닫는다.
    # 소유권 이전 시점 자체는 relay 워치독이 `Popen` 을 부르기 한 줄 앞이다(그보다 앞이면
    # "스폰할 수도 있는 함수에 들어갔다"는 이유만으로 환불 권리가 사라진다).
    record_id: str | None = None
    ledger_path: Path | None = None
    relay = None
    spawn_attempted = False
    # 러너가 "자식 없음"으로 **판명**해 돌아온 뒤인가 — 스폰 시도 창이 다시 열린 상태다.
    no_spawn_proven = False
    # 장부 레코드를 정상 마감했는가 — 마감 뒤의 실패까지 보상 경로가 다시 닫으면 이중 마감이 된다.
    raw_finalized = False
    started_at = time.monotonic()

    def _spawn_attempt() -> None:
        """실 스폰 경계 1회 — 스폰 전 롤백 창을 닫고 예약 소유권을 호출부 계약대로 넘긴다."""
        nonlocal spawn_attempted
        spawn_attempted = True
        if on_spawn_attempt is not None:
            on_spawn_attempt()

    try:
        _raw_dir, ledger_path = _raw_storage(output_dir)
        relay = _load_relay()
        model = target.ledger_model
        # 장부 레코드는 raw 헤더·stderr provenance·dry-run 과 **같은 해소 축**을 싣는다 — 그래야
        # raw 파일이 지워지거나 stderr 를 못 읽어도 "무엇이 어떤 설정으로 나갔는가"가 장부 하나로
        # 닫힌다. local_conf 는 그 축의 앵커라(어느 conf 가 이 대상을 골랐나) 함께 남긴다.
        record_id = relay.start_raw_record(
            ledger_path,
            surface="external-review",
            harness=name,
            model=model,
            role=REVIEWER_ROLE,
            raw_path=raw_path,
            attempt="primary",
            extra={
                **({"local_conf": str(local_conf_path)}
                   if local_conf_path is not None else {}),
                "reviewer_source": target.source,
                "reasoning": target.reasoning,
                "command": target.command,
                **({"codex_egress": codex_egress} if codex_egress is not None else {}),
            },
        )
        metrics: dict[str, object] = {"rc": 1, "silence_sec": None}
        started_at = time.monotonic()
        run_env = env
        if target.structured:
            # 공용 표가 OpenCode 카드 부재의 runtime role을 주입하고 다른 하네스는 env만 복사한다.
            run_env = relay.with_harness_runtime_role(
                env, target.harness, REVIEWER_ROLE,
            )
        with _structured_transport(target, prompt, cwd) as (argv, stdin_text):
            ok, output, started = _run_reviewer_ex(
                prompt, reviewer_cmd, timeout, run_fn, idle_timeout, metrics,
                cwd=cwd, env=run_env, argv=argv, stdin_text=stdin_text,
                on_spawn_attempt=_spawn_attempt,
            )
            if not started:
                # ── 스폰 없음 확정 seam ──────────────────────────────────
                # 러너가 결론적으로 "자식 없음"을 돌려줬다(빈 커맨드·실행 파일 부재·seam 계약
                # 오류). 스폰 시도 직전에 넘긴 소유권을 **회신 파싱·raw 박제·장부 마감 어느 것도
                # 하기 전에** 되돌려 놓는다 — 그 뒤 어디서 죽든(요약 출력의 BrokenPipeError 가
                # 실측 축이다) 전송 0·과금 0 인 실행이 예약을 미완으로 남기지 않는다.
                no_spawn_proven = True
                if on_no_spawn is not None:
                    on_no_spawn()
        elapsed = time.monotonic() - started_at
        # 판정과 오염 검출은 **회신 채널**만 본다(진행 로그는 프롬프트·diff 원문을 그대로 싣는다).
        # 경계는 구조(필드)라서 표시 문자열에 무엇이 섞여 와도 되찾을 필요가 없다.
        output = _as_reviewer_output(output)
        # 구조화 대상의 회신 채널은 하네스 wire(JSONL/stream)다 — 공용 추출기를 통과한 **최종 회신
        # 텍스트만** 판정·오염 검출에 넣는다. 추출 실패(회신 이벤트 부재·비-문자열·형식 붕괴)는 wire 를
        # 판정에 흘리지 않고 빈 회신으로 둔다 — 타입 계약은 공용 seam 이 소유하므로 어떤 wire 형상도
        # 여기서 예외로 터지지 않는다(터지면 raw 박제·장부 마감 전이라 진단 근거가 함께 사라진다).
        #
        # 그리고 **회신이 없으면 리뷰가 없다** — 프로세스가 rc=0 으로 끝났어도 이 실행은 리뷰어의
        # 판정을 하나도 받지 못했다. 이것을 '성공 → 판정 불명확'으로 두면 rc 는 1 이어도 폴백 신호
        # (FALLBACK_INTERNAL)가 없어, PM 이 내부 code-reviewer 로 갈아탈 근거를 못 받는다. 그래서
        # 실패 축으로 내린다: 요약은 실패 + 사유를 말하고 raw 에 wire 전문이 남는다. 라운드 장부에서의
        # 취급은 종전과 같다(판정 없음 = 미완 라운드 · 전송은 있었으므로 환불 없음).
        answer = output.answer
        reply_extraction_failed = False
        if target.structured and ok:
            extracted = relay.extract_harness_reply(target.harness, output.answer)
            reply_extraction_failed = extracted is None
            answer = extracted or ""
            ok = not reply_extraction_failed
        verdict = parse_verdict(answer)
        contamination = detect_output_contamination(answer)
        _write_reserved_output(
            raw_path,
            output.combined,
            local_conf_path=local_conf_path,
            resolved_profile=resolved_profile,
            target=target,
            codex_egress=codex_egress,
        )
        try:
            relay.finish_raw_record(
                ledger_path,
                record_id,
                rc=int(metrics["rc"]),
                elapsed_sec=elapsed,
                silence_sec=metrics.get("silence_sec"),
            )
        except relay.RawRecordAlreadyFinished as exc:
            # 수동 `raw close`(--force) 가 먼저 마감한 충돌 — 첫 마감을 보존하고 리뷰
            # 결과는 실패로 바꾸지 않는다(회신은 raw 파일에 이미 박제됨).
            print(f"경고: 장부 마감 충돌 — {exc} (수동 마감 보존·회신은 raw 파일 참조)",
                  file=sys.stderr)
        raw_finalized = True
        return {
            "reviewer": name,
            "ok": ok,
            # wire 원문(stdout+stderr)은 그대로 보존한다 — raw 박제·사람 진단 입력.
            "output": output.combined,
            # 판정에 실제로 들어간 회신(구조화 대상은 추출된 최종 텍스트).
            "answer": answer,
            "log": output.log,
            "reply_extraction_failed": reply_extraction_failed,
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
    except BaseException as exc:
        # 보상 조건은 두 가지뿐이다. (1) 스폰 시도 **전** — 자식이 아직 없다. (2) 러너가 "자식
        # 없음"으로 판명해 돌아온 **뒤** — 자식이 뜬 적이 없다. 그 사이(스폰 시도 후·판정 전)의
        # 실패는 종전 보수 규칙 그대로다: 전송됐을 수 있으니 예약도 미마감 레코드도 건드리지
        # 않는다(미마감이 곧 "확인 필요" 진단이다). 정상 마감이 이미 끝난 뒤라면 다시 닫지
        # 않는다 — 닫힌 레코드를 실패 축으로 덮으면 장부가 두 번째 거짓말을 한다.
        if not spawn_attempted or (no_spawn_proven and not raw_finalized):
            _abort_pre_spawn_raw(
                relay, ledger_path, record_id, raw_path,
                reason=f"{type(exc).__name__}: {exc}",
                elapsed=time.monotonic() - started_at,
                local_conf_path=local_conf_path, resolved_profile=resolved_profile,
                target=target, codex_egress=codex_egress,
            )
        raise


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
    """종합 판정 라인에 붙일 payload 제외 병기 접미사.

    암묵 수집분(--ticket/기본)에서 기계 mirror/시크릿 diff 제외가 있었으면 건수·경로를 판정 라인에
    남긴다 — stderr 경고는 로그를 안 읽으면 사라지지만 판정 라인은 PM 이 반드시 본다. 제외 0건이면
    빈 문자열이라 출력은 종전과 완전 동일(기존 통과 경로 무변경)."""
    if not excluded:
        return ""
    return f" (검토 제외 {len(excluded)}건 — {', '.join(excluded)})"


def _first_output_line(result: dict) -> str:
    """실패 사유 1줄(없으면 빈 문자열) — 판정 블록과 환불 경고가 같은 문장을 쓰게 하는 공용 seam."""
    head = str(result.get("output") or "").strip().splitlines()
    return head[0][:200] if head else ""


def print_summary(result: dict, gate: str | None = None,
                  excluded: list[str] | None = None) -> None:
    """결과 요약을 stdout 에 출력한다.

    excluded — 기계 mirror 또는 시크릿 denylist로 diff에서 제외된 암묵 수집분 경로. 비어있지
    않으면 종합 판정 라인에 제외 건수·경로를 병기한다(false-confidence 차단). 0건이면 종전과 동일.

    게이트 줄은 **항상** 나온다 — 게이트 없이 끝난 실행은 그 사실이 회계 상태(장부 미기록)라
    stderr 경고 하나에만 맡기지 않는다(오염 진단·실패 사유와 같은 근거: 판정 블록은 읽힌다).
    이 줄은 실행이 끝난 뒤라 확정형으로 말할 수 있다."""
    sep = "=" * 60
    name = result.get("reviewer", "reviewer")
    suffix = _exclusion_suffix(excluded)
    print(sep)
    print(f"추가 리뷰어 코드리뷰 결과 요약 [{name}]")
    print(f"게이트: {gate if gate else _SUMMARY_UNACCOUNTED_GATE}")
    print(sep)
    print(f"\n[{name}] {_format_verdict(result['ok'], result.get('verdict'))}")
    if result.get("file"):
        print(f"  원문: {result['file']}")
    # 오염 진단은 판정 라인과 같은 자리에 둔다 — stderr 경고는 로그를 안 읽으면 사라지지만 PM 은
    # 판정 블록을 반드시 읽는다. 판정 자체를 뒤집지는 않고(엇갈린 판정만 run_review 가 불명확 처리),
    # 이번 판정이 리뷰어 자신의 것인지 PM 이 확인할 근거를 남긴다.
    for marker in result.get("contamination") or ():
        print(f"  ⚠ 오염 의심(저장소 밖 탐색 흔적): {marker}")
    if result.get("reply_extraction_failed"):
        # 구조화 대상이 rc=0 으로 끝났는데 회신을 못 뽑았다 — wire 를 판정에 흘리지 않았으므로
        # 이 실행은 리뷰를 못 받은 것이고, 실패 축(FALLBACK_INTERNAL)으로 내려간다. 원인(형식
        # 변경·비-문자열 회신·빈 turn)은 raw 원문에서만 보이니 그리로 보낸다.
        print("  ⚠ 회신 추출 실패 — 하네스 wire 에서 최종 회신을 찾지 못했습니다"
              "(리뷰 미수신 처리 · 원문 파일 확인).")
    if result.get("unisolated"):
        print("  ⚠ 미격리 실행 — 리뷰어가 PM 세션 cwd 에서 옛 리뷰 raw·세션 전사를 탐색할 수 "
              f"있었습니다({UNISOLATED_REVIEWER_FLAG}).")
    print()
    if result["failed"]:
        # 실패 사유 1줄을 판정 라인에 병기 — 타임아웃 안내(`--timeout`/conf 키) 같은 실패
        # 본문이 원문 파일에만 남으면 PM 이 못 본다(판정 라인은 반드시 읽힌다).
        reason = _first_output_line(result)
        if result.get("reply_extraction_failed"):
            # 이 실패의 출력은 wire 원문이라 첫 줄(이벤트 JSON)이 사유가 되지 못한다 — 사유를
            # 직접 말한다.
            print("  사유: 하네스 wire 에서 최종 회신 텍스트를 추출하지 못했습니다 "
                  "(회신 이벤트 부재 · 비-문자열 회신 · 형식 붕괴) — 원문 파일에 wire 전문 보존.")
        elif reason:
            print(f"  사유: {reason}")
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
        description="추가 리뷰어 래퍼 — 추가 리뷰어(외부 하네스) 어댑터 CLI (외부 전송·기본 OFF)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예시:
  # 기본 (HEAD 기준 변경, local.conf review_paths/기본 경로) — 회계 밖 실행을 명시
  python3 .project_manager/tools/external_review.py --no-gate

  # ticket 의 touches 로 경로 결정 (--gate 미지정이면 그 티켓으로 게이트 자동 유도·장부 기록)
  python3 .project_manager/tools/external_review.py --ticket T-0259

  # 게이트 회계 밖 자문 실행 (명시 opt-out · 장부 미기록·예산 미소모)
  python3 .project_manager/tools/external_review.py --ticket T-NNNN --no-gate

  # 특정 base 와 경로·게이트 지정
  python3 .project_manager/tools/external_review.py --base main --paths src/ tests/ --gate T-NNNN

  # dry-run (diff·프롬프트만 출력, 외부 호출/전송 안 함 — 비활성이어도 허용)
  python3 .project_manager/tools/external_review.py --dry-run

  # 비활성 상태에서 1회 강제 실행
  python3 .project_manager/tools/external_review.py --force --no-gate

  # 라운드 장부 조회 (외부 전송 없음 — 게이트별 라운드 수·라운드별 산출·처분·wave spent)
  python3 .project_manager/tools/external_review.py --rounds-report --gate T-NNNN

  # 게이트 처분 선언 (외부 전송 없음 — 릴리즈 게이트가 읽는 잔여 must-fix 소화 기록)
  python3 .project_manager/tools/external_review.py --resolve-gate T-NNNN --into T-MMMM
  python3 .project_manager/tools/external_review.py --resolve-gate T-NNNN --fixed T-MMMM
  # --fixed 근거는 마지막 반려 종료 뒤 시작 + 변경된 target_rev + 통과가 모두 필요.
  # --resolve-gate 와 --dry-run 조합은 기록 목적과 모순이라 rc1로 거부.

  # Codex sandbox(network-off) 안에서: 미리보기 → 도구 승격 + 증명 동반 실행
  python3 .project_manager/tools/external_review.py --ticket T-NNNN --dry-run
  python3 .project_manager/tools/external_review.py --ticket T-NNNN --codex-egress-escalated

활성화: local.conf 에 `additional_reviewer_enabled=true` ·
        또는 `board.py init` / `pm_update` 시 opt-in 프롬프트.
추가 리뷰어 대상(원자 tuple · 정상 경로):
        additional_reviewer.harness=codex
        additional_reviewer.model=gpt-5.6-sol
        additional_reviewer.reasoning=max     (선택 · 하네스별 허용집합 검증)
        harness/model 은 동반 필수이고 legacy `reviewer_cmd` 와 함께 쓸 수 없다.
        구조화 키가 하나도 없으면 종전 `reviewer_cmd`/기본 커맨드로 도는 unpinned-model 경로다.
지속 동의: `additional_reviewer_enabled=true` 는 설정된 대상의 외부 전송·통상 과금에 대한 지속
        동의다 — 호출마다 비용을 다시 묻지 않는다. 무한 라운드는 라운드/wave 예산이 기계로 막는다.
""",
    )
    parser.add_argument("--base", default="HEAD",
                        help=("git diff 기준 ref (기본: HEAD — 작업트리"
                              "(스테이징+언스테이징), 비면 직전 커밋 HEAD~1..HEAD)"))
    parser.add_argument("--paths", nargs="+", default=None,
                        help="검토 대상 경로 (기본: local.conf review_paths / src tests scripts ...)")
    parser.add_argument("--ticket", default=None, metavar="T-NNNN",
                        help="ticket ID — touches 로 검토 경로 결정. --gate 미지정이면 이 값이 "
                             "게이트로 자동 유도돼 라운드가 장부에 기록·집계된다")
    parser.add_argument("--gate", default=None, metavar="T-NNNN",
                        help="게이트 ticket 표식 (로깅 + 라운드 상한 장부 키) — 미지정이면 "
                             "--ticket 에서 자동 유도한다(명시 값이 항상 우선)")
    parser.add_argument("--no-gate", action="store_true",
                        help="게이트 회계 opt-out — --ticket 자동 유도를 끄고 라운드 장부 기록·"
                             "라운드/wave 예산 집계 없이 실행한다 (회계 밖 자문 실행·loud 표기 · "
                             "--gate 와 함께 쓸 수 없음)")
    parser.add_argument("--ack-rounds", action="store_true",
                        help="(폐지됨) 라운드 연장 승인 — 호출하면 아무것도 하지 않고 거부한다. "
                             "출구는 재설계·티켓 분할이고, 해소 확인만 필요하면 --confirm-fix.")
    parser.add_argument("--confirm-fix", action="store_true",
                        help="확인 전용 라운드 — 상한 밖에서 게이트당 1회만 허용"
                             "(게이트 지정 필수 — --gate 또는 --ticket 유도). "
                             "직전 must-fix 해소만 확인하고 신규 발견은 '재설계 신호'로 보고하는 "
                             "헌장을 프롬프트에 싣는다 (장부 기록·2회째는 거부)")
    parser.add_argument("--ack-wave", action="store_true",
                        help="wave 예산 재개 — wave spent 를 0 으로 리셋 후 재개 "
                             "(게이트 지정 필수 — --gate 또는 --ticket 유도 · 같은 범위의 "
                             "정상 수렴이면 PM 이 자율 판단)")
    parser.add_argument("--rounds-report", action="store_true",
                        help="라운드 장부 조회 — 게이트별 라운드 수·라운드별 판정/결함 수·처분·wave "
                             "spent 를 출력하고 종료 (외부 전송 없음·--gate 로 한 게이트만)")
    parser.add_argument("--resolve-gate", default=None, metavar="T-NNNN",
                        help="게이트 처분 선언 — 반려로 끝난 게이트의 잔여 must-fix 를 어떻게 "
                             "소화했는지 장부에 남기고 종료 (외부 전송 없음). `--into` 또는 "
                             "`--fixed` 중 하나 필수. 릴리즈 게이트(`board.py livegate record`)가 "
                             "이 선언을 읽어 미처분 잔여를 차단한다")
    parser.add_argument("--into", default=None, metavar="T-NNNN",
                        help="--resolve-gate 처분: 잔여 must-fix 를 이 후속 티켓으로 재설계 선언 "
                             "(면제 아님 — 그 티켓이 done 이어야 릴리즈가 열린다)")
    parser.add_argument("--fixed", nargs="?", const=_FIXED_WITHOUT_EVIDENCE, default=None,
                        metavar="T-NNNN",
                        help="--resolve-gate 처분: 잔여 must-fix 가 코드로 해소됐음을 선언 — 값은 "
                             "그 사실을 보인 **근거 게이트**다. 근거 마지막 라운드는 반려 종료 뒤 "
                             "시작했고(started_at·ISO 8601 UTC), 다른 target_rev를 검토해 통과해야 "
                             "한다. 결속 필드 없는 구 라운드는 거부. 지목은 필수")
    parser.add_argument("--dry-run", action="store_true",
                        help="diff·프롬프트만 출력, 외부 호출/전송 안 함 (비활성이어도 허용·빈 diff 면 "
                             "exit 1). --resolve-gate 는 기록 명령이므로 함께 쓰면 exit 1")
    parser.add_argument("--force", action="store_true",
                        help=f"{ADDITIONAL_REVIEWER_ENABLED_KEY}=false 여도 1회 강제 실행 (외부 전송 발생)")
    parser.add_argument(CODEX_EGRESS_FLAG, action="store_true",
                        help="Codex egress 승격 호출층 증명 — 이 호출을 Codex 도구 "
                             'sandbox_permissions="require_escalated" 로 올렸음을 감사 기록한다. '
                             "CODEX_SANDBOX_NETWORK_DISABLED=true 환경의 실행은 이 증명 없이는 "
                             "격리·라운드·raw·스폰 전에 rc=1 로 중단된다(플래그 자체는 권한을 "
                             "만들지 않는다 · 미리보기는 --dry-run)")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="리뷰 원문 저장 디렉토리"
                             " (기본: .project_manager/.local/review, PM 홈 미해소 시 tempdir)."
                             " 실제 전송이 일어나는 실행에서만 생성된다"
                             " (미리보기·비활성 no-op·게이트 거부는 만들지 않음)")
    parser.add_argument("--timeout", type=_timeout_seconds_arg, default=None,
                        metavar="SEC",
                        help="외부 호출 벽시계 백스톱(초) — 기본은 해소 대상의 하네스 프로필. "
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
    for line in _read_text_shared(path, encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


def _review_raw_content(
    content: str, local_conf_path: Path | None, resolved_profile: str | None,
    target: ReviewerTarget | None = None, codex_egress: str | None = None,
) -> str:
    """pm_delegate와 같은 `# key: value` 감사 헤더를 external_review 원문에 붙인다.

    헤더는 **추가만** 한다(기존 두 줄의 위치·표기 불변). 해소 축(출처·harness·model·reasoning·
    실행 커맨드)과 egress 라벨을 한 줄씩 더해, raw 하나만 봐도 "무엇이 어떤 권한으로 이 판정을
    냈는가"가 닫힌다."""
    if (local_conf_path is None and resolved_profile is None
            and target is None and codex_egress is None):
        return content
    header = ["# external_review raw 출력 (감사)"]
    if local_conf_path is not None:
        header.append(f"# local_conf: {local_conf_path}")
    if resolved_profile is not None:
        header.append(f"# resolved_profile: {resolved_profile}")
    if target is not None:
        header.append(f"# reviewer_source: {target.source}")
        if target.structured:
            header.extend([
                f"# harness: {target.harness}",
                f"# model: {target.model}",
                f"# reasoning: {target.reasoning}",
            ])
        else:
            # legacy 도 장부와 **같은 정체 값**을 쓴다(표기 없으면 `unpinned-model`).
            header.append(f"# model: {target.ledger_model}")
        header.append(f"# command: {target.command}")
    if codex_egress is not None:
        header.append(f"# codex_egress: {codex_egress}")
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
    """external_review의 실제 송신 대상 값. 미지정과 명시 default는 같은 값으로 정규화한다.

    비교 대상은 **해소된 대상**이다 — 한쪽이 구조화 tuple 이고 다른 쪽이 legacy 커맨드면 실제
    수신자가 다르므로 커맨드·출처 축이 모두 갈려 loud 하다. 해소 불가능한 대상 conf 는 값 자리에
    사유를 실어 조용히 같아 보이지 않게 한다(비교기는 절대 예외를 올리지 않는다)."""
    try:
        target = resolve_reviewer_target(conf)
    except ReviewerTargetError as exc:
        return {"reviewer_cmd": f"<해소 실패: {exc}>"}
    values = {"reviewer_cmd": target.command, "reviewer_source": target.source}
    if target.structured:
        values.update({
            "reviewer_harness": str(target.harness),
            "reviewer_model": str(target.model),
            "reviewer_reasoning": str(target.reasoning),
        })
    return values


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
    target: ReviewerTarget | None = None,
) -> str:
    """stderr·dry-run·raw 가 **같은 문자열로** 공유하는 추가 리뷰어 해소 tuple.

    앞머리(커맨드·시간 예산)는 종전과 같고, 뒤에 해소 출처 축이 붙는다 — 구조화면
    `source=structured` + harness/model/reasoning, legacy 면 `source=legacy` +
    `model=unpinned-model`(모델을 고정하지 않은 경로임을 크게 남긴다)."""
    tail = (target or ReviewerTarget(REVIEWER_SOURCE_LEGACY, reviewer_cmd)).profile_tail
    return (
        f"(reviewer_cmd={reviewer_cmd}, wall_timeout_sec={timeout}, "
        f"idle_timeout_sec={idle_timeout}, {tail})"
    )


def _main(argv: list[str] | None = None) -> int:
    global REPO, LOCAL_CONF, REVIEW_CONTEXT_FILE, TICKETS_DIR, _PM_HOME_OVERRIDE
    # main() 재호출 간 raw 앵커가 새지 않게 해소 전에 비운다(해소 전 조기 return 은 raw 를 쓰지 않는다).
    _PM_HOME_OVERRIDE = None
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    engine_repo = REPO.resolve()
    # 게이트 이름 검증이 **가장 먼저**다 — 예약 키 충돌은 전송·장부 접근 뒤에 발견하면 이미
    # 두 상한이 서로를 덮어쓴 뒤다(조회면도 같은 이름을 게이트로 부르지 않게 함께 막는다).
    reserved_gate_error = _reserved_gate_error(args.gate)
    if reserved_gate_error is not None:
        print(reserved_gate_error, file=sys.stderr)
        return 1
    # 폐지된 연장 승인은 **아무 표면에서도** 통하지 않는다 — 조회면 무시 경고로 흡수하면 "쓰긴
    # 썼는데 안 먹었다"가 남아 규율이 흐려진다. 부작용 0 지점(장부 접근·전송 전)에서 거부한다.
    if args.ack_rounds:
        print(_ACK_ROUNDS_REMOVED_GUIDANCE, file=sys.stderr)
        return 1
    # 게이트 회계 자동 유도 — `--ticket` 실행의 기본값은 "기록"이다(조용한 무기록 폐지). 아무것도
    # 하지 않고 끝나는 `--ack-rounds` 거부 **뒤**에 둔다: 그 실행이 유도 고지를 내면 오보다.
    # `--confirm-fix` 게이트 누락 검사보다는 **앞**이라, 유도된 게이트가 확인 전용 라운드의
    # 회계 자리도 그대로 제공한다. **고지 출력은 여기서 하지 않는다** — stderr 첫 줄은 config
    # provenance 라 그 뒤에 낸다(아래 provenance 직후).
    gate_derivation = _derive_gate_from_ticket(args)
    if gate_derivation.refusal is not None:
        print(gate_derivation.refusal, file=sys.stderr)
        return 1
    # 게이트 없는 확인 전용 라운드는 어느 표면에서도 뜻이 없다 — 장부 항목이 없어 1회 제한을
    # 셀 수 없다. `--ack-rounds` 와 같은 부작용 0 지점에서 거부한다(경고-만-실행 폐지).
    if getattr(args, "confirm_fix", False) and not args.gate:
        print(_CONFIRM_FIX_REQUIRES_GATE_GUIDANCE, file=sys.stderr)
        return 1
    # 처분 인자는 `--resolve-gate` 없이는 뜻이 없다 — 선언할 게이트가 없으면 남길 사실도 없다
    # (`--confirm-fix` 게이트 누락과 같은 부작용 0 지점·경고-만-실행 금지).
    for flag, value in (("--into", args.into), ("--fixed", args.fixed)):
        if value and not args.resolve_gate:
            print(_RESOLVE_GATE_REQUIRED_GUIDANCE.format(flag=flag), file=sys.stderr)
            return 1
    if args.resolve_gate:
        # 장부 기록 전용면 — 전송 경로(conf 분기·denylist·diff 추출)보다 앞에서 끝낸다.
        return _resolve_gate_command(args, engine_repo)
    if args.rounds_report:
        ignored = ", ".join(
            flag for flag, given in (
                ("--confirm-fix", args.confirm_fix), ("--ack-wave", args.ack_wave),
                # 조회면은 전송도 예약도 없어 회계 자체가 없다 — opt-out 도 무시 대상이다.
                ("--no-gate", args.no_gate),
                ("--dry-run", args.dry_run), ("--force", args.force),
                (CODEX_EGRESS_FLAG, args.codex_egress_escalated),
            ) if given
        )
        if ignored:
            print(f"경고: --rounds-report 는 조회 전용이라 다음 플래그를 무시합니다: {ignored}.",
                  file=sys.stderr)
        if not (args.paths or args.ticket):
            # selector 없는 조회는 여기서 끝낸다 — 장부만 읽으므로 검토 경로가 없거나 diff 가
            # 비어도(빈-diff 가드) 답을 내야 하고, 그러려면 그 게이트들보다 앞서야 한다.
            # selector 를 준 조회는 기록면과 **같은 해소**를 타야 해서 아래 앵커 해소 뒤로 간다.
            return _print_rounds_report(engine_repo, gate=args.gate)
    ticket_selected = bool(args.ticket and not args.paths)
    explicit_paths = bool(args.paths)
    engine_demotions: list[PmHomeDemotion] = []
    diff_owner_demotions: list[PmHomeDemotion] = []
    engine_resolutions: list[PmHomeResolutionFacts] = []
    diff_owner_resolutions: list[PmHomeResolutionFacts] = []
    ticket_body_source: Path | None = None
    # plain list는 production parser가 만들지 않는다. 기존 회귀 fixture가 touches seam만 주입하고
    # 실 ticket 파일은 제공하지 않은 형상을 명시적으로 식별하는 테스트 전용 호환 축이다.
    ticket_scope_fixture_injected = False
    try:
        engine_pm_home = resolve_pm_home_for_repo(
            engine_repo, required=ticket_selected, demotion_sink=engine_demotions,
            resolution_sink=engine_resolutions,
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
            parsed_touches = parse_ticket_touches(args.ticket, pm_home=engine_pm_home)
            ticket_body_source = getattr(parsed_touches, "source", None)
            ticket_scope_fixture_injected = ticket_body_source is None
            raw_paths = tuple(parsed_touches)
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
                resolution_sink=diff_owner_resolutions,
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
        print(f"오류: 추가 리뷰 앵커 해소 실패 — {exc}", file=sys.stderr)
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
    selected_owner_demotions = (
        engine_demotions if pm_home == engine_pm_home else diff_owner_demotions
    )
    selected_owner_resolutions = (
        engine_resolutions
        if diff_root.resolve() == engine_repo
        else diff_owner_resolutions
    )
    if args.rounds_report:
        # selector 를 준 조회 — 기록면이 쓸 장부(diff 소유 PM 홈)를 그대로 읽는다. 여기서 끝내
        # 전송 경로(conf 분기·denylist·diff 추출)는 타지 않는다.
        return _print_rounds_report(pm_home, gate=args.gate, resolved=True)
    if conf is None:
        # 명시 앵커 실행의 config는 여기서 **처음이자 유일하게** 읽고, 강등 필터 검사도 여기서
        # 한다 — 대상은 이번 실행이 실제로 쓰는 conf 소유자뿐이다. 초기 엔진 컨텍스트가 강등·손상
        # 이어도 diff 대상이 다른 소유자로 확정됐으면 그 손상은 이 송신과 무관하다(절대 --paths
        # 복구 채널 보존). 인자 없는 실행은 범위 파생 지점에서 이미 읽고 검사했고, 그 실행의 선택
        # 소유자는 정의상 최초 PM 홈이다(다르면 교차 소유 가드가 이미 차단).
        try:
            conf = _conf_with_owner_filters(
                _local_config_for_repo(pm_home), selected_owner_demotions,
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
            print(f"오류: 추가 리뷰 앵커 해소 실패 — {exc}", file=sys.stderr)
            return 1
    # 시간 예산은 **해소된 대상의 하네스 프로필**을 따른다 — 대상 command 를 먼저 해소해야
    # 어떤 축(클라우드/로컬)의 값을 쓸지 정해진다. 깨진 conf의
    # fail-soft 경고는 해소 중 발생하지만 stderr 첫 줄 provenance 계약을 지키도록 잠시 보류한다.
    # 대상 해소가 시간 예산보다 **먼저**다 — 잘못된 프로필(부분 tuple·미지원 값·대상 이중 선언)은
    # output-dir 생성·격리 거울·라운드 예약·raw 예약·과금 문구 어느 것도 만들기 전에 끊는다.
    # 여기까지의 앵커 해소는 읽기 전용이고, 이 함수 자체도 부작용이 없다.
    try:
        target = resolve_reviewer_target(conf)
    except ReviewerTargetError as exc:
        print(
            f"오류: 추가 리뷰어 대상 해소 실패 — 외부 송신 전에 중단합니다: {exc}\n"
            f"  · 설정 파일: {_local_conf_path(pm_home)}",
            file=sys.stderr,
        )
        return 1
    reviewer_cmd = target.command
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
    profile = resolved_reviewer_profile(reviewer_cmd, timeout, idle_timeout, target)
    # Codex egress 승격 판정 — dry-run 표시와 실행 게이트가 **같은 입력**을 쓴다. 마커는 "승격이
    # 필요한 형상인가"만 정하고, "이번 호출이 승격됐는가"는 호출층 attestation 이 소유한다.
    relay = _load_relay()
    codex_egress_required = relay.codex_egress_escalation_required()
    codex_egress = relay.codex_egress_label(
        escalation_required=codex_egress_required,
        attested=args.codex_egress_escalated,
    )

    target_repo = diff_root
    # 정상 실행·dry-run 공용 첫 provenance. 이후 cap/reanchor/diff 진단이 붙더라도 어느 PM-home conf와
    # reviewer tuple을 해소했는지가 stderr에 남는다.
    print(
        f"[external-review] config provenance: local_conf={conf_path} "
        f"· diff_root={diff_root} · pm_home={pm_home} · resolved_profile={profile}",
        file=sys.stderr,
    )
    if gate_derivation.notice is not None:
        # 게이트 유도 고지는 **provenance 다음**이다 — 첫 줄 계약(그 자리에서 어느 conf·어느
        # diff_root 로 해소했는지 읽는다)을 이 고지가 밀어내면 안내 문서와 실제 출력이 갈린다.
        print(gate_derivation.notice, file=sys.stderr)
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

    # `--output-dir` 은 **경로 해소만** 여기서 한다(위임 표면과 같은 규칙). 디렉토리 생성은
    # 부작용이므로 실제로 raw 를 예약하는 지점(`_reserve_output`)이 소유한다 — 그래야 미리보기·
    # 비활성 no-op·Codex egress 차단·라운드 상한 거부처럼 **전송 없이 끝나는 실행**이 요청한
    # 디렉토리를 만들지 않는다(빈 디렉토리를 남기면 "아무 일도 없었다"가 파일시스템에서 거짓이
    # 되고, 사람이 그 자리를 산출물 위치로 오독한다).
    output_dir: Path | None = Path(args.output_dir) if args.output_dir else None

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

    # `--paths`가 검토 제외 기계 mirror subtree 자체를 직접 지목한 실행은 diff 유무와 무관하게
    # 차단한다. subtree 루트(`.opencode/node_modules`)는 trailing-slash 좌표로 넣어 같은 판정을
    # 쓰고, 손편집 출하 manifest의 정확 경로는 payload 정책과 같이 허용한다. 넓은 부모
    # (`templates/` 등) 안에서 발견되는 제외분은 아래 실 diff 필터가 보고한다.
    if args.paths:
        directly_selected_machine_paths = [
            path for path in paths
            if _canonical_measure_path(path) not in _HAND_EDITED_REVIEW_PATHS
            and (
                _is_review_machine_mirror_path(path)
                or _is_review_machine_mirror_path(path.rstrip("/") + "/")
            )
        ]
        if directly_selected_machine_paths:
            print(
                _format_machine_mirror_exclusion_block(directly_selected_machine_paths),
                file=sys.stderr,
            )
            return 1

    # diff 추출 (기계 mirror·시크릿 denylist 자동 제외 — 제외 경로 목록도 반환)
    denylist = content_resolution.denylist
    try:
        diff, excluded = extract_diff(args.base, paths, denylist=denylist)
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # payload 제외 보고. 제외된 경로가 판정에 전혀 안 남으면, 지정분이 조용히 빠진 채 '통과'가 나
    # 게이트가 실제보다 넓게 검증한 것처럼 보인다. 기계 mirror와 시크릿 denylist 모두 같은
    # 명시/암묵 규율을 탄다.
    # 명시(--paths)와 암묵(--ticket/기본) 지정을 구분한다: 명시 지정분이 제외되면 차단(exit 1)하고
    # 어느 경로가 왜 빠졌는지 알린다 — 빈-diff 가드보다 앞서 두어, 단일 파일 전량 제외로 diff 가
    # 비어도 '변경 없음'이 아니라 denylist 가 원인임을 정확히 알린다. 암묵 수집분은 차단하지 않고
    # stderr 경고 + 종합 판정 라인 병기(아래 print_summary)로 남긴다. 제외 0건이면 no-op(종전 무변경).
    if excluded:
        unresolved_excluded = [path for path in excluded if _is_unresolved_diff_exclusion(path)]
        machine_excluded = [
            path for path in excluded
            if not _is_unresolved_diff_exclusion(path) and _is_review_machine_mirror_path(path)
        ]
        secret_excluded = [
            path for path in excluded
            if not _is_unresolved_diff_exclusion(path) and not _is_review_machine_mirror_path(path)
        ]
        if args.paths:  # 명시 지정 → 차단 (우회는 그 경로를 빼고 재실행·새 플래그 없음)
            if unresolved_excluded:
                print(
                    _format_unresolved_diff_exclusion_block(unresolved_excluded),
                    file=sys.stderr,
                )
            if machine_excluded:
                print(_format_machine_mirror_exclusion_block(machine_excluded), file=sys.stderr)
            if secret_excluded:
                print(_format_explicit_exclusion_block(secret_excluded, denylist), file=sys.stderr)
            return 1
        # 암묵 수집 → 비차단·stderr 경고 (판정 병기는 아래 print_summary의 전체 excluded 목록).
        for item in unresolved_excluded:
            print(
                f"경고: {item} — 경로를 유일하게 확정하지 못해 diff block을 review payload에서 "
                "제외했습니다 (fail-closed).",
                file=sys.stderr,
            )
        for path in machine_excluded:
            print(
                f"경고: 기계 mirror 경로 '{path}' 를 review payload 에서 제외했습니다 "
                "(is_machine_mirror_path=True 판정).",
                file=sys.stderr,
            )
        for path in secret_excluded:
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

    # 티켓 본문은 실제 프롬프트가 필요한 dry-run 또는 외부 전송 확정 구간에서만 읽는다. 비활성
    # no-op·egress/diff-cap/앵커 차단은 본문 파일이나 byte 상한을 건드리지 않는다. ticket touches를
    # 실제 파일에서 읽은 경우 그 exact source를 재사용하고, 명시 --paths 또는 테스트 fixture의
    # plain-list touches seam은 ticket 파일 부재가 scope 해소를 자기잠그지 않게 본문 없이 진행한다.
    ticket_body: str | None = None
    ticket_body_bytes = 0
    ticket_body_omitted_rounds = 0

    def prepare_ticket_body() -> bool:
        nonlocal ticket_body, ticket_body_bytes, ticket_body_omitted_rounds
        if not args.ticket:
            return True
        try:
            ticket_file = (
                ticket_body_source if ticket_body_source is not None
                else _find_ticket_file(args.ticket, pm_home=pm_home)
            )
            raw_text, raw_body = _load_ticket_text_and_body(ticket_file)
            # 입력 선별 — 명세는 전량, 라운드는 역할별 마지막 산출만(파일 선택).
            selection = _select_ticket_body_for_review(
                raw_body,
                _load_ticket_rounds_for(
                    args.ticket, pm_home=pm_home, ticket_text=raw_text,
                ),
            )
            if selection.omitted_rounds > 0:
                header = _ticket_body_selection_header(ticket_file, args.ticket)
                composed_body = f"{header}\n\n{selection.text}"
            else:
                # 생략된 라운드가 없으면 요약 헤더 없이 원문 그대로 — 회귀 불변(DoD (e)).
                composed_body = selection.text
        except (AnchorResolutionError, OSError, UnicodeError) as exc:
            if args.paths:
                print(
                    "경고: 명시 --paths가 검토 범위를 소유해 "
                    f"{args.ticket} 본문을 읽지 못했지만 본문 없이 계속합니다: {exc}",
                    file=sys.stderr,
                )
                ticket_body = None
                return True
            if ticket_scope_fixture_injected:
                print(
                    "경고: 테스트 fixture가 ticket touches seam만 주입하고 본문 provenance는 "
                    f"제공하지 않아 {args.ticket} 본문 없이 계속합니다: {exc}",
                    file=sys.stderr,
                )
                ticket_body = None
                return True
            print(f"오류: 게이트 티켓 본문 로딩 실패 — {exc}", file=sys.stderr)
            return False
        ticket_body = composed_body
        ticket_body_bytes = len(ticket_body.encode("utf-8"))
        ticket_body_omitted_rounds = selection.omitted_rounds
        return True

    # 확인 가능한 finding ID 실값은 **소비 지점에서** 한 번만 해소한다(회수 대상이 있는 실행만 ·
    # 읽기 전용). 두 소비자(프롬프트 골격 실값 · 확인 전용 라운드 근거의 표면 대조)가 같은 목록을
    # 봐야 리뷰어가 표면 밖 ID(회수 거부 라운드·PM rejected)를 확인 대상으로 받지 않는다.
    # 미리 읽지 않는 이유는 부작용 규율이다 — 비활성 no-op·프롬프트를 만들지 않는 조기 종료는
    # 티켓 파일을 건드리지 않는다.
    resolved_confirmable: dict[str, list[str] | None] = {}

    def confirmable_finding_ids() -> list[str] | None:
        if "ids" not in resolved_confirmable:
            resolved_confirmable["ids"] = _confirmable_external_finding_ids(
                args, pm_home=pm_home, degraded=_CONFIRMABLE_IDS_DEGRADED,
            )
        return resolved_confirmable["ids"]

    def surface_finding_ids() -> set[str] | None:
        ids = confirmable_finding_ids()
        return None if ids is None else set(ids)

    def compose_prompt(confirm_fix_evidence: str | None) -> str:
        """이번 실행의 프롬프트 — 확인 전용 라운드 근거만 호출 시점에 따라 다르다."""
        return build_prompt(
            diff=diff, ticket_body=ticket_body, ticket_id=args.ticket,
            adr_refs=args.adr, gate=args.gate,
            confirm_fix=args.confirm_fix,
            confirm_fix_evidence=confirm_fix_evidence,
            next_finding_id=_next_external_finding_id(args, pm_home=pm_home),
            confirmation_ids=confirmable_finding_ids(),
        )

    # 구키 deprecation — 미리보기·실행 **양쪽**에서 같은 자리에 안내. 게이트 판정 앞이라 꺼져 있는
    # conf 도 안내를 받는다(구키로 `false` 를 적어 둔 채택자가 켜려 할 때 신키를 알아야 한다).
    # 게이트와 노브를 한 깔때기에서 받아 축마다 다른 자리에 찍히지 않게 한다.
    for warning in legacy_key_warnings(conf):
        print(warning, file=sys.stderr)

    if args.dry_run:
        if not prepare_ticket_body():
            return 1
        # dry-run은 예약 스냅샷이 없으므로 읽기 전용 장부 조회 근거로 미리보기를 조립한다.
        prompt = compose_prompt(
            _gate_confirm_fix_evidence(
                args.gate, surface_finding_ids=surface_finding_ids(),
            )
            if getattr(args, "confirm_fix", False) and args.gate else None
        )
        # 미리보기는 **부작용 0**이다(외부 송신·raw 예약·라운드 예약·격리 거울·`--output-dir`
        # 생성 모두 없음). 여기까지의 준비는 전부 읽기 전용이고(conf 해소·denylist·git diff),
        # 그 diff 는 아래 프롬프트 미리보기가 **실제 나갈 내용**을 보여주기 위해 필요하다.
        # 해소 대상 표시는 stderr 첫 provenance·raw 헤더와 **같은 문자열**을 쓴다 — 세 표면이 서로
        # 다른 말을 하면 "미리보기로 확인한 대상"과 "실제 나간 대상"을 대조할 수 없다.
        print("=== [dry-run] 추가 리뷰어 대상 (외부 전송 없음) ===")
        print(f"local_conf: {conf_path}")
        print(f"resolved_profile: {profile}")
        print(f"command: {target.command}")
        if ticket_body is not None:
            print(
                f"ticket_body_bytes: {ticket_body_bytes} · "
                f"생략 라운드: {ticket_body_omitted_rounds}개"
            )
        print(relay.dry_run_codex_egress_line(
            escalation_required=codex_egress_required,
            attested=args.codex_egress_escalated,
            script=relay.EXTERNAL_REVIEW_ENTRYPOINT,
            consent_key=ADDITIONAL_REVIEWER_ENABLED_KEY,
            windows=_running_on_windows(),
        ))
        print("=== [dry-run] 프롬프트 미리보기 (외부 전송 없음) ===")
        print(prompt)
        print("=== [dry-run] 외부 호출 생략 ===")
        return 0

    # 활성화 게이트 (외부 전송이므로 기본 OFF)
    if not _is_enabled(conf) and not args.force:
        print(disabled_gate_notice(conf), file=sys.stderr)
        return 0  # no-op — 실패 아님

    # Codex egress 게이트 — `--output-dir` 생성·격리 거울·라운드 예약·raw 예약·외부 스폰·과금
    # 문구보다 **앞**이다. 증명 없는 network-off 실행은 여기서 끝난다(엔진은 sandbox 를 완화하거나
    # 다른 대상으로 무음 대체하지 않는다). `--force` 는 위의 opt-in 게이트용이라 이 안전 경계를
    # 열지 못한다(이 판정은 force 를 입력으로 쓰지 않는다). 미리보기(dry-run)는 이 게이트 앞에서
    # 이미 반환됐다 — 차단 환경에서도 처방을 낼 수 있어야 하고, 반환 시점까지 부작용이 0 이라
    # 게이트 뒤로 미룰 이유가 없다.
    if codex_egress_required and not args.codex_egress_escalated:
        print(
            relay.codex_egress_block_message(
                list(sys.argv[1:] if argv is None else argv),
                target.name, target.ledger_model,
                script=relay.EXTERNAL_REVIEW_ENTRYPOINT,
                consent_key=ADDITIONAL_REVIEWER_ENABLED_KEY,
                subject="추가 리뷰어 외부 전송",
                windows=_running_on_windows(),
            ),
            file=sys.stderr,
        )
        return 1

    # 실행 provenance — raw 헤더와 같은 라벨을 stderr 에도 남긴다(사후 안전 경계 재구성 입력).
    print(
        "[external-review] 실행 provenance: "
        + relay.codex_egress_provenance(
            escalation_required=codex_egress_required,
            attested=args.codex_egress_escalated,
        ),
        file=sys.stderr,
    )

    # ── diff 서킷브레이커 (전송 경로 진입 검사) ──────────
    # 스코프가 estimate 상한을 넘긴 티켓은 리뷰 라운드로 닫히지 않는다 — 리뷰어를 부르기 전에
    # 멈추고 분할·재설계로 보낸다. 자리는 **전송이 확정된 구간**이라 미리보기(dry-run)·비활성
    # no-op·egress 차단은 이 검사를 지나지 않는다: 그 실행들은 전송도 과금도 없어 이 게이트가
    # 막으려는 비용이 애초에 없고, 미리보기를 막으면 분할 판단에 필요한 diff 확인 채널까지 닫힌다.
    # estimate 를 못 읽으면(자유 문자열 게이트·티켓 부재) 가드는 조용히 off 다.
    cap_block = _diff_cap_refusal(
        args, conf, root=diff_root, paths=paths, pm_home=pm_home,
    )
    if cap_block is not None:
        print(cap_block, file=sys.stderr)
        return 1

    # ── 라운드 상한·wave 예산 게이트: 호출 전 예약 ──────────
    # 격리 컨테이너 생성보다 **먼저**다 — 이미 상한에 닿은 호출은 스폰이 없어도 격리를 먼저 만들면
    # 저장소·인증 사본을 만들었다 지우는 실 작업을 하고, 그건 "전송 없이 끝나는 실행은 부작용 0"
    # 규율(dry-run·비활성 no-op·egress 차단과 같은 축)을 이 rc 에서만 깨는 것이다.
    # dry-run·조회·처분은 이미 반환했고, 비활성 no-op·egress 차단·diff-cap 거부도 송신 없이
    # 종료됐다. 여기까지 온 실행은 이제 외부 송신으로 가므로, `--gate`(또는 `--ticket`
    # 자동 유도) / 명시 `--no-gate` 중 하나가 반드시 있어야 한다. 둘 다 없으면 예약·격리·raw·
    # 리뷰어 스폰 전 부작용 0 지점에서 fail-loud 한다.
    if not args.gate and not args.no_gate:
        print(_GATE_ACCOUNTING_REQUIRED_GUIDANCE, file=sys.stderr)
        return 1

    # lease 미등록 임시 linked worktree의 자기 `.local` 에 라운드를 기록하면 스냅샷 재생성 때
    # 장부도 사라진다. 강등 기록 유무와 무관하게 git/lease 단일 술어로 판정한다. 이 자리는
    # dry-run·조회·처분·비활성 no-op·egress 차단·diff-cap 거부가 모두 반환한 뒤이고, 바로 아래
    # 예약부터 장부 부작용이 시작되는 경계다. `--no-gate` 는 gate=None 이라 기존 raw 자기-앵커
    # 복구 채널을 그대로 통과한다.
    anchor_refusal = _self_anchored_round_refusal(
        selected_owner_demotions,
        selected_owner_resolutions,
        diff_root=diff_root,
        pm_home=pm_home,
        gate=args.gate,
    )
    if anchor_refusal is not None:
        print(anchor_refusal, file=sys.stderr)
        return 1

    # 모든 무전송 조기 종료를 지난 뒤, 라운드 예약(부작용) 전 마지막 입력 경계에서 본문을 읽고
    # 상한을 판정한다. 여기서 실패하면 외부 전송·라운드/wave 소비 모두 0이다.
    if not prepare_ticket_body():
        return 1
    # 예약 전 조회는 미리보기 성격일 뿐 자격 근거가 아니다. confirm-fix 예약이 반환한 eligibility
    # snapshot evidence로 아래에서 반드시 재조립한다(동시 마감 라운드와 두 read가 갈려도 한 스냅샷).
    prompt = compose_prompt(
        _gate_confirm_fix_evidence(
            args.gate, surface_finding_ids=surface_finding_ids(),
        )
        if getattr(args, "confirm_fix", False) and args.gate else None
    )

    budget = _reserve_round_budget(
        args, conf, wall_timeout_sec=timeout,
        target_rev=_target_rev_fingerprint(diff),
        surface_finding_ids=surface_finding_ids(),
    )
    if budget.refused_rc is not None:
        return budget.refused_rc
    if budget.confirm_fix_evidence is not None:
        # 확인 전용 라운드가 열렸다 — 자격을 판정한 스냅샷의 근거로 프롬프트를 다시 조립한다.
        # (열리지 않은 실행은 근거가 없어 재조립도 없다 — 일반 라운드 프롬프트는 그대로다.)
        prompt = compose_prompt(budget.confirm_fix_evidence)

    # ── 추가 리뷰어 가시 범위 격리 ──────────
    # 리뷰어는 이 거울 안에서만 돈다. 스폰 전에 중단하는 실행은 외부 전송이 없으므로 방금 잡은
    # 예약을 그 자리에서 원자 환불한다(마감 시점 `started=False` 환불과 같은 조건·같은 기계).
    # 환불 소유는 이 seam 하나다 — 알려진 격리 실패도, 예상 못 한 예외도 같은 구간 규칙을 탄다.
    with _PreSpawnReservation(budget) as reservation:
        isolation = contextlib.ExitStack()
        try:
            workspace = isolation.enter_context(reviewer_visibility_scope(
                diff_root, allow_unisolated=args.allow_unisolated_reviewer, conf=conf,
                # 거울과 프롬프트가 **같은 해소값**으로 시크릿을 제외한다(폭이 갈리면 제외가 무의미).
                denylist=content_resolution.denylist,
            ))
        except ReviewerWorkspaceError as exc:
            isolation.close()
            # 중단 사유를 먼저 말하고, 그 결과로 장부가 어떻게 됐는지를 뒤에 붙인다.
            print(_UNISOLATED_GUIDANCE.format(reason=exc), file=sys.stderr)
            reservation.release(reason="리뷰어 격리 생성 실패")
            return 1
        # 생성 이후 **모든 경로**(예외 포함)가 이 finally 를 지나 정리된다.
        try:
            return _run_isolated_review(
                args, workspace, conf=conf, prompt=prompt, reviewer_cmd=reviewer_cmd,
                timeout=timeout, idle_timeout=idle_timeout, output_dir=output_dir,
                conf_path=conf_path, profile=profile, excluded=excluded,
                target=target, codex_egress=codex_egress, reservation=reservation,
                pm_home=pm_home,
            )
        finally:
            isolation.close()


# 회수 경계에서 사본 불일치를 접을 때의 단일 문구 — 두 로드/쓰기 지점이 같은 처방을 낸다.
_HARVEST_ENGINE_SKEW_PROBLEM = (
    "엔진 사본 불일치로 회수를 중단했습니다 — {detail} "
    "(`pm-update` 로 재동기한 뒤 같은 티켓으로 라운드를 다시 돌리세요)"
)


def _harvest_target_ticket(
    args, *, pm_home: Path | None,
) -> tuple[str | None, str | None]:
    """회수 대상 티켓과 (대상이 없을 때의) 사유 — `--ticket` 우선, 없으면 ticket 형상 `--gate`.

    문서화된 설계 리뷰 형상은 `--paths … --gate T-NNNN` 이라 `--ticket` 이 비어 있다. 그 실행의
    산출도 그 티켓의 것이므로 같은 회수 규칙을 탄다(자유 문자열 게이트는 대상이 아니다).
    """
    if args.ticket:
        return args.ticket, None
    gate = getattr(args, "gate", None)
    if not gate:
        return None, None
    board = _load_board()
    if not board._is_valid_ticket_id(gate):
        return None, None                     # 자유 문자열 게이트 — 회수 대상 아님(조용).
    try:
        _find_ticket_file(gate, pm_home=pm_home)
    except AnchorResolutionError as exc:
        return None, f"게이트 {gate} 를 보드에서 찾지 못해 회수하지 않았습니다: {exc}"
    return gate, None


class _HarvestTargetState(NamedTuple):
    """회수 대상 티켓의 명세 전문과 라운드 목록 — 프롬프트 실값 두 소비자가 쓰는 한 벌."""

    text: str
    rounds: list


def _harvest_target_ticket_state(
    args, *, pm_home: Path | None, degraded: str,
) -> _HarvestTargetState | None:
    """회수 대상 티켓의 명세 + 라운드 (대상 없거나 읽기 실패면 None · 실패는 loud).

    프롬프트에 실린 티켓 본문에서 파생하면 문서화된 설계 리뷰 형상(`--paths … --gate T-NNNN`)은
    본문이 실리지 않아 티켓을 못 본다. 대상 해소는 회수 경로와 **같은 함수**를 써서 두 표면이
    갈리지 않게 하고, 프롬프트 실값·확인 근거 대조가 **같은 읽기**를 쓴다.

    finding ID 는 라운드 파일에만 선언되므로 명세만 읽어서는 다음 ID·확인 대상을 알 수 없다 —
    두 축을 함께 돌려준다.
    """
    ticket, _problem = _harvest_target_ticket(args, pm_home=pm_home)
    if not ticket:
        return None
    try:
        ticket_text = _load_ticket_text_and_body(
            _find_ticket_file(ticket, pm_home=pm_home)
        )[0]
        return _HarvestTargetState(
            ticket_text,
            _load_ticket_rounds_for(
                ticket, pm_home=pm_home, ticket_text=ticket_text,
            ),
        )
    except (AnchorResolutionError, OSError, UnicodeError) as exc:
        print(
            f"경고: {ticket} 본문을 읽지 못했습니다({exc}) — {degraded}",
            file=sys.stderr,
        )
        return None


def _next_external_finding_id(args, *, pm_home: Path | None) -> str | None:
    """이 채널이 이번 라운드에 쓸 첫 finding ID 실값 (회수 대상 없으면 None).

    라운드마다 fresh 인 리뷰어 세션이 스스로 알 수 없는 값이다 — 안 실으면 2라운드가 같은 ID 를
    재선언해 회수가 거부된다. 시야는 **명세 + 모든 라운드**다(넓은 스캔) — 한 라운드만 보면
    지난 라운드가 쓴 번호를 다시 지시한다.
    """
    state = _harvest_target_ticket_state(
        args, pm_home=pm_home,
        degraded="이 라운드는 첫 finding ID 로 안내합니다.",
    )
    if state is None:
        return None
    delegate = _load_pm_delegate()
    return delegate.next_review_finding_id(
        state.text, delegate.EXTERNAL_REVIEW_ROLE, state.rounds,
    )


def _confirmable_external_finding_ids(
    args, *, pm_home: Path | None, degraded: str,
) -> list[str] | None:
    """확인 라운드가 참조할 수 있는 이 채널 finding ID 실값 목록 (대상 없으면 None).

    입력은 이 채널의 **직전 라운드 파일** 하나다 — 확인 전용 라운드의 임무가 "직전 라운드
    must-fix 의 해소 확인"이라 리뷰 라운드 시드 프리필과 같은 시야여야 한다. 그 "직전 라운드"
    규칙(역할 필터 · 산출 없는 라운드 배제 · 마지막 순번)은 사이드카 seam 이 소유한다 — 여기서
    다시 구현하면 예약해 둔 시드 라운드가 직전 산출 자리를 차지해 확인 대상이 빈 목록이 된다.
    배제(PM 이 `rejected` 로 판정한 ID)는 시드와 **같은 엔진 함수**가 소유한다 — 두 채널이 서로
    다른 목록을 보면 한쪽 리뷰어가 표면이 거부할 ID 를 확인 대상으로 받는다.
    """
    state = _harvest_target_ticket_state(args, pm_home=pm_home, degraded=degraded)
    if state is None:
        return None
    delegate = _load_pm_delegate()
    role = delegate.EXTERNAL_REVIEW_ROLE
    latest = _load_ticket_rounds().latest_round_of_role(state.rounds, role)
    if latest is None:
        return []
    try:
        return delegate.collect_confirmable_finding_ids(state.text, role, [latest])
    except delegate.DelegateError as exc:
        print(
            f"경고: 확인 가능한 finding ID 목록을 해소하지 못했습니다({exc}) — {degraded}",
            file=sys.stderr,
        )
        return None


def _harvest_external_review_section(
    ticket: str, result: dict, *, pm_home: Path | None,
) -> str | None:
    """추가 리뷰어 산출을 게이트 티켓의 새 `external-reviewer` 라운드 파일로 회수한다.

    회수 주체는 **엔진**이다 — 리뷰어에게 티켓/보드 편집 권한을 주지 않는다. 라운드 본문은 첫 줄
    헤더 + 산문 회신 전문(그 안에 versioned 블록 하나)이고, 내용 검증(`pm_delegate` 소유)을
    통과할 때만 파일이 생긴다. 통과하지 못하면 **파일을 만들지 않고** 사유를 돌려준다(호출부가
    rc 로 표면화하고 산출 원문은 raw 에 남는다) — 라운드가 파일 하나라 "거부 표식을 얹어 절에
    남기고 판정 표면에서 빼는" 보정이 필요 없다.

    채번+생성은 준비(`pm_delegate.prepare_ticket_copy`)와 **같은 seam**(`ticket_rounds`)을
    지난다 — 인자가 시드냐 실내용이냐만 다르다.

    쓰기 엔진은 **이 실행의 형제 사본**이고 데이터 좌표만 PM 홈이다 — PM 홈의 import 사본을
    로드하면 stale 엔진이 회수를 쓰게 된다(문서화된 실행 형상은 worktree canonical + PM 홈 데이터).
    """
    reply = result.get("answer") or result.get("output") or ""
    if not reply.strip():
        return "리뷰어 회신이 비어 회수할 산출이 없습니다"
    home = pm_home or REPO
    try:
        delegate = _load_pm_delegate()
        rounds_module = _load_ticket_rounds()
        board = delegate.anchor_board_to_repo(_load_board(), home)
    except RuntimeError as exc:
        if not _absorb_engine_rev_skew_for_recovery(exc, "ticket_harvest"):
            raise
        return _HARVEST_ENGINE_SKEW_PROBLEM.format(detail=exc)
    try:
        return _reserve_external_review_round(
            ticket, reply, delegate=delegate, rounds_module=rounds_module, board=board,
        )
    # 라운드 규약 위반은 `RuntimeError` 하위형이라 **사본 skew 절보다 먼저** 받는다 — 순서를
    # 뒤집으면 예약 충돌·이름 문법 위반이 skew 판정을 지나 traceback 으로 나간다.
    except (delegate.DelegateError, rounds_module.RoundsError,
            OSError, UnicodeError) as exc:
        return f"티켓 라운드 기록 실패: {exc}"
    except RuntimeError as exc:
        if not _absorb_engine_rev_skew_for_recovery(exc, "ticket_harvest"):
            raise
        return _HARVEST_ENGINE_SKEW_PROBLEM.format(detail=exc)


def _reserve_external_review_round(
    ticket: str, reply: str, *, delegate, rounds_module, board,
) -> str | None:
    """내용 검증 → 통과 시 라운드 예약 + board 부분 커밋 (위반 사유 또는 None).

    검증 입력(명세 + 라운드)은 예약 **직전**에 읽는다 — finding ID 재선언·confirmation 대상
    판정이 실제로 쓰이는 상태와 같은 스냅샷을 봐야 한다.
    """
    role = delegate.EXTERNAL_REVIEW_ROLE
    found = board.find_ticket_exact(ticket)
    if found is None:
        raise delegate.DelegateError(f"ticket not found: {ticket}")
    status, ticket_path = found
    if status not in ("open", "claimed"):
        raise delegate.DelegateError(
            f"external-reviewer 라운드 기록은 open/claimed 티켓만 허용: "
            f"{ticket} in {status}/"
        )
    with _load_file_lock().open_shared(
        ticket_path, binary=False, encoding="utf-8", newline="",
    ) as handle:
        ticket_text = handle.read()
    tickets_dir = board.tickets_dir()
    rounds = rounds_module.load_rounds(
        tickets_dir, ticket, ticket_text=ticket_text,
    )
    problem = delegate.external_review_harvest_problem(
        reply, ticket_text=ticket_text, rounds=rounds,
    )
    if problem is not None:
        return problem      # 파일을 만들지 않는다 — 산출 원문은 raw 에만 남는다.

    # 회신 bytes 는 그대로 둔다(개행 표기 포함) — 라운드 파일은 산출 하나의 원문이다.
    content = (
        rounds_module.render_round_header(
            role, today=datetime.date.today().isoformat(),
        )
        + "\n\n" + (reply if reply.endswith("\n") else reply + "\n")
    )
    round_path = rounds_module.reserve_round(
        tickets_dir, ticket, role, content=content, lock=board.board_lock(),
    )
    ordinal, _role = rounds_module.parse_round_filename(round_path.name)
    message = f"external-review {ticket} {role}"
    # board 부분 커밋 seam 은 직접 부른다 — 이름을 더듬어 찾으면 그 이름이 갈렸을 때 라운드
    # 파일만 만들어지고 board 커밋은 조용히 빠진 rc0 이 된다(AttributeError 로 죽는 편이 낫다).
    sync_ready = bool(board._rounds_mutation_sync_paths(message, [round_path]))
    print(
        f"[external-review] 티켓 회수: {ticket} {role}[{ordinal}] → {round_path}"
        + ("" if sync_ready else " (board-git 동기 미준비)"),
        file=sys.stderr,
    )
    return None


def _run_isolated_review(
    args, workspace, *, conf, prompt, reviewer_cmd, timeout, idle_timeout,
    output_dir, conf_path, profile, excluded, target=None, codex_egress=None,
    reservation: "_PreSpawnReservation | None" = None,
    pm_home: Path | None = None,
) -> int:
    """격리 컨테이너 수명 **안**에서 도는 구간 — 리뷰 실행·장부 마감.

    `_main` 에서 떼어낸 이유는 수명 관리 하나다: 여기서 어떤 경로로 빠져나가도(조기 return·예외)
    호출부의 단일 finally 가 컨테이너를 지운다. 거울에는 저장소 사본과 인증 파일 사본이 있어
    잔존이 곧 누출이다.

    예산 확인·예약은 이 구간 **밖**(격리 생성 전)에서 이미 끝났다 — 상한에 닿은 호출이 격리를
    만들지 않게 하려면 그 순서여야 한다. 넘겨받은 예약(`reservation`)의 앞부분(환경 준비, 그리고
    `run_review` 안의 raw 선점·장부 시작 레코드·transport·argv 준비, 그리고 러너 안의 준비
    구간)은 아직 스폰 전이라 호출부의 환불 seam 이 소유하고, 소유권은 **실 스폰 경계**(relay
    워치독의 `Popen` 직전)의 `on_spawn_attempt` 콜백으로 넘어온다 — 그 뒤부터 이 구간이 예약을
    전송 결과로 마감한다.

    이전에는 대칭 반납이 붙는다(`on_no_spawn`). 러너가 `started=False` 로 판명해 돌아오면 소유권이
    스폰 전 seam 으로 되돌아오고, 요약·진단·라운드 환불이 **저장까지 끝난 뒤**에야 반납된다 — 그
    사이의 실패(닫힌 stdout 파이프의 `BrokenPipeError` 가 실측 축이다)는 자식이 없던 실행이므로
    바깥 seam 이 예약을 한 번 되돌려야 한다. 예외로 나가지 **않는** 변종이 하나 있다: 마감 저장이
    `OSError` 로 실패한 경우다. 그 경로는 판정 rc 를 그대로 돌려주며 정상 return 하므로 바깥
    `__exit__` 가 소유를 보지 못해, 되돌림을 그 자리에서 직접 한 번 한다.
    `started=True` 는 종전 그대로 반납하지 않는다.
    """
    if reservation is None:
        reservation = _PreSpawnReservation(RoundBudget())  # 장부 밖 직접 호출
    budget = reservation.budget
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

    print(f"추가 리뷰어 실행 중 (외부 전송·과금): {reviewer_cmd}", file=sys.stderr)
    # 예약 소유는 `run_review` **진입**이 아니라 relay 워치독이 자식을 띄우기 **직전**에 넘어간다
    # (`on_spawn_attempt`). run_review 의 앞부분(raw 선점·장부 시작 레코드·구조화 transport·
    # argv/kwargs 준비)과 러너 안의 준비 구간(relay 로드·프로필 해소·워치독 셋업)은 자식이 아직
    # 없는 구간이라, 진입에서 넘기면 스폰 0·과금 0 으로 끝난 준비 실패(예: `--output-dir` 이
    # 일반 파일이라 raw 선점이 터지는 실행, 실행 권한 없는 리뷰어 바이너리)가 예약을 미완/소진으로
    # 남긴다 — `incomplete_limit=1` 이면 다음 **정상** 호출이 곧바로 rc=4 로 막힌다.
    # 이전 이후의 실패는 프롬프트가 이미 나갔을 수 있으므로 환불하지 않는다: 되돌림 판정은 아래
    # 마감 경로의 `started` 가 소유한다(타임아웃·비정상 종료까지 환불하면 과금된 호출이 상한을
    # 소비하지 않아 무한 우회가 열린다·MF-A).
    #
    # 추가 리뷰어는 PM 세션의 cwd/env/홈을 물려받지 않는다 — 거울과 임시 홈, allowlist env 만 받는다.
    result = run_review(
        prompt=prompt, reviewer_cmd=reviewer_cmd,
        timeout=timeout, output_dir=output_dir, idle_timeout=idle_timeout,
        local_conf_path=conf_path, resolved_profile=profile,
        cwd=workspace_tree,
        env=reviewer_environment,
        target=target, codex_egress=codex_egress,
        on_spawn_attempt=reservation.hand_off,
        on_no_spawn=reservation.reclaim_no_spawn,
    )
    started = bool(result.get("started", True))
    if started:
        # 스폰된 실행의 예약은 아래 마감 경로가 소유한다(소유권은 스폰 시도 콜백이 이미 넘겼다 —
        # 여기 한 줄은 그 불변식을 이 자리에서 못 박는 멱등 재확인이다). 이후 어디서 죽어도
        # 환불하지 않는다: 프롬프트가 이미 나갔을 수 있어 되돌리면 과금된 호출이 상한을 소비하지
        # 않는다(무한 우회·MF-A).
        reservation.hand_off()
    # started=False 는 반대다 — 러너가 "자식 없음"으로 판명해 돌아온 실행이라 `on_no_spawn` 이
    # 소유권을 이 seam 으로 되돌려 놨다. 아래 요약·진단·마감이 **끝날 때까지** 그 상태를 유지한다:
    # 여기서 미리 넘기면 요약 출력이 닫힌 stdout 파이프로 죽는 순간(BrokenPipeError) 전송 0·과금 0
    # 인 실행의 예약이 미완으로 남아 `incomplete_limit=1` 형상에서 다음 정상 호출을 막는다.
    print_summary(result, gate=args.gate, excluded=excluded)
    if (result.get("failed") and workspace is not None
            and not result.get("reply_extraction_failed")):
        # 격리 임시 홈에서 로그인 파일이 빠졌을 때의 증상이 '리뷰어 실패'라, 원인 분리 채널을 준다.
        # 회신 추출 실패는 예외다 — 프로세스가 rc=0 으로 끝났으니 인증은 통과한 실행이고, 여기서
        # 인증 힌트를 내면 원인을 반대로 가리킨다(진단 채널은 요약의 사유 줄과 raw wire 다).
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
    if budget.reserved:
        # 산출 파싱·시각은 락 **밖**에서 끝낸다 — 임계 구역은 장부 read-modify-write 만 잡는다.
        # 예약 identity(id·sequence)는 예약 시점 값이라 마감 시점 장부 상태에 의존하지 않는다.
        outcome = (
            _round_outcome(result, record={
                "id": budget.round_id, "sequence": budget.sequence,
                "started_at": budget.started_at, "target_rev": budget.target_rev,
            })
            if started else None
        )
        # 확인 전용 라운드가 "무엇을 확인하는지" 알려면 **항목 텍스트**가 남아야 한다(건수만으로는
        # fresh 세션이 대상을 모른다). 산출 파싱과 같은 자리·같은 함수라 건수와 텍스트가 갈리지
        # 않고, 저장 지점은 예약 레코드 안이라 정규화(`_gate_entry`)에서 살아남는다.
        must_fix_items = _round_must_fix_items(result) if started else None
        refunded_round = False
        try:
            with _round_ledger_lock():
                ledger = _load_round_ledger()
                if not started:
                    # 전송이 확실히 없던 라운드 — 두 예산(게이트 count·wave spent)을 같은 조건으로
                    # 되돌린다(격리 생성 실패 환불과 **같은 기계**). 산출도 남기지 않는다
                    # (리뷰어가 아무 말도 하지 않았다).
                    refunded_round = _refund_reserved_round(ledger, budget)
                elif outcome is not None:
                    entry = _gate_entry(ledger, budget.gate)
                    matching = next(
                        (row for row in entry["records"]
                         if row.get("id") == budget.round_id),
                        None,
                    )
                    if matching is not None:
                        matching["finished_at"] = _utc_now_iso()
                        matching["verdict"] = _round_has_verdict(result)
                        if must_fix_items is not None:
                            matching["must_fix_items"] = must_fix_items
                    # 산출 기록은 예약 레코드 유무와 무관하다 — 전송된 라운드는 무조건 남긴다
                    # (승인이 집계 창을 비워도 "무엇이 나왔는가"는 이력으로 남아야 한다).
                    # 예약 identity 를 함께 실어 동시 완료에서도 라운드↔산출 연결이 확정된다.
                    _append_round_outcome(entry, outcome)
                _save_round_ledger(ledger)
            if not started:
                # 되돌린 라운드는 **말하고** 되돌린다 — 조용히 환불하면 채택자에게 남는 사실이
                # "장부가 그대로다" 뿐이라, 교차검증이 아예 돌지 않은 실행과 리뷰어를 선언하지
                # 않은 실행이 같은 모양이 된다(Windows 경로 분해 결함이 30여 릴리즈 숨은 경로).
                if refunded_round:
                    print(
                        f"라운드 환불: 게이트 {budget.gate} — 리뷰어 프로세스가 시작되지 않아 "
                        "추가 리뷰어 교차검증이 **실행되지 않았습니다**(전송 0·상한/wave 예산 "
                        f"미소진). 사유: {_first_output_line(result) or '미상'}",
                        file=sys.stderr,
                    )
                # 환불이 **저장까지 끝난** 뒤에야 소유권을 반납한다 — 이 줄 앞에서 죽으면 바깥
                # seam 이 아직 소유자라 한 번 되돌리고, 이 줄 뒤에는 되돌릴 것이 없다. 저장이
                # 실패한 경로(아래 OSError)는 반납하지 않는다: 되돌리지 못한 예약의 소유자는
                # 여전히 스폰 전 seam 이고, 그 경로는 예외 없이 정상 return 하므로(바깥
                # `__exit__` 가 보지 못한다) 거기서 직접 한 번 되돌린다.
                reservation.settle_refunded()
        except OSError as exc:
            # 마감은 이미 *끝난* 전송의 부기다 — 여기서 rc 를 바꾸면 리뷰 판정이 락 사정으로
            # 뒤집힌다. loud 경고만 남기고 판정 종료코드를 그대로 돌려준다. 마감 못 한 레코드는
            # finished_at 없이 남아 다음 실행의 미완 재시도 예산으로 보수적으로 집계된다.
            #
            # 단 `started=False` 는 예외다 — 자식이 뜬 적 없다고 **판명된** 실행이라 예약 소유는
            # 아직 스폰 전 seam 에 있다(`on_no_spawn` 이 되돌려 놨고 반납은 저장 성공 뒤에만
            # 선다). 이 경로는 예외를 삼키고 **정상 return** 하므로 `__exit__` 가 그 소유를 보지
            # 못한다: 그대로 두면 전송 0·과금 0 인 실행이 라운드·wave 예산을 먹은 채 미완으로
            # 남아, `incomplete_limit=1` 형상에서 다음 **정상** 재시도가 곧바로 rc=4 로 막힌다.
            # 그래서 같은 환불 기계로 한 번 더 되돌린다 — 락이 그새 풀리면 예산이 복구되고,
            # 저장은 됐는데 락 해제만 실패한 형상이면 되돌릴 레코드가 없어 `_refund_round` 가
            # False 를 내므로 이중 환불이 되지 않는다. 소유는 여전히 한 seam 이다(`release` 는
            # 한 번만 선다). 스폰된 실행(started=True)은 종전대로 건드리지 않는다 — 프롬프트가
            # 이미 나갔을 수 있어 되돌리면 과금된 호출이 상한을 소비하지 않는다(MF-A).
            refunded = not started and reservation.release(
                reason="스폰 없이 끝난 실행의 장부 마감 실패")
            print(
                f"경고: 라운드 장부 마감 실패 ({type(exc).__name__}: {exc}) — 다른 게이트 "
                "실행이 장부 락을 보유 중일 수 있습니다. "
                + ("전송이 없던 라운드라 예약은 되돌렸습니다(상한·wave 예산 미소진)."
                   if refunded else
                   "이 라운드는 미완으로 남아 다음 실행의 재시도 예산에서 세어집니다.")
                + f" (장부: {_round_ledger_path()})",
                file=sys.stderr,
            )

    rc = determine_exit_code(result)
    # 라운드 장부 마감 **뒤**에 회수한다 — 장부는 종전대로 기록되고, 회수 실패는 판정 rc 를
    # 덮지 않고 rc=0 인 실행만 실패로 올린다. 대상은 `--ticket` 또는 보드에 실재하는 ticket
    # 형상 `--gate` 이고, 대상이 있는데 회수하지 않은 실행은 사유를 반드시 말한다(조용한 누락 금지).
    harvest_ticket, target_problem = _harvest_target_ticket(args, pm_home=pm_home)
    if target_problem is not None:
        print(f"경고: 추가 리뷰어 산출 미회수 — {target_problem}", file=sys.stderr)
    elif harvest_ticket and not (started and not result.get("failed")):
        print(
            f"경고: 추가 리뷰어 산출 미회수 — {harvest_ticket}: 이번 라운드는 "
            "리뷰어 회신을 받지 못했습니다(전송 실패·타임아웃). 산출 없음이라 라운드 파일을 "
            f"만들지 않습니다 · raw={result.get('file')}",
            file=sys.stderr,
        )
    if harvest_ticket and started and not result.get("failed"):
        problem = _harvest_external_review_section(
            harvest_ticket, result, pm_home=pm_home,
        )
        if problem is not None:
            print(
                f"오류: 추가 리뷰어 산출 회수 문제 — {problem}\n"
                f"  · 라운드 파일은 만들지 않았습니다 · 산출 원문은 raw 에 보존됩니다: "
                f"{result.get('file')}\n"
                "  · PM 판정 전에 리뷰어에게 구조화 블록을 갖춘 회신을 다시 받으세요.",
                file=sys.stderr,
            )
            rc = rc or 1
    return rc


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
