#!/usr/bin/env python3
"""외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI.

사용:
    python3 .project_manager/tools/external_review.py [옵션]

동작:
  git diff <base> -- <paths> 추출 (시크릿 denylist 경로 자동 제외)
  (프로젝트 맥락 헤더 +) diff 결합 → 표준 프롬프트 생성
  외부 리뷰어 실행 (reviewer_cmd, stdin 으로 프롬프트 주입, read-only 권장)
  출력에서 판정(통과/반려)·must-fix 파싱
  결과 요약 stdout + 원문 파일 저장 (기본 `.project_manager/.local/review/` · --output-dir 로 격리)

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
    limit 회(local.conf external_review_round_limit·기본 4) 실 전송하면 이후 실행을 기계 차단하고
    "사용자 보고·대기" loud 안내를 낸다 — 사용자 승인 후 `--ack-rounds` 로 +limit 재개.

설계:
  - 어댑터 seam: 외부 도구를 `reviewer_cmd`(local.conf) 뒤로 격리 → codex 외 교체 가능.
    기본 `codex exec --sandbox read-only --skip-git-repo-check` (stdin 으로 프롬프트).
  - 도메인 외부화: 프로젝트 맥락은 `.project_manager/review_context.local.md`(인스턴스 소유)
    가 있으면 주입, 없으면 generic 헤더. 엔진 도구엔 도메인 콘텐츠 0.
  - subprocess DI (run_fn 매개변수) — 테스트에서 mock 주입 가능.
  - 외부 호출은 코드를 수정하지 않는다 (read-only 인자 사용 권장).
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
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, NamedTuple

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
ENGINE_REV = "v1.5.1"


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

# PM 하네스 런타임 마커. 이 독립 CLI에서 pm_delegate를 deep-import하면 새 엔진 rev 검증 경계가
# 필요하므로 작은 선언을 복제한다. tests/test_external_review.py가 pm_delegate의 동명 선언과
# 동일성을 기계 단언해 두 판정이 서로 갈라지지 않게 한다.
_HARNESS_SESSION_MARKERS: dict[str, tuple[str, ...]] = {
    "codex": ("CODEX_THREAD_ID", "CODEX_CI"),
    "claude": ("CLAUDECODE",),
    "opencode": ("OPENCODE", "OPENCODE_PID"),
}

# Bash 명시호출 최대상한 env 선언은 리뷰 진단의 고유 책임이다. 양쪽 카드가 timeout을 명시하므로
# DEFAULT가 아니라 MAX가 실제 제약이다. Codex는 repo가 읽을 공개 Bash 상한 env가 없다.
_REVIEW_HARNESS_CAP_ENV: dict[str, str | None] = {
    "codex": None,
    "claude": "BASH_MAX_TIMEOUT_MS",
    "opencode": "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
}

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
    "  · adopter#0/worktree 형상: 실 변경이 있는 worktree cwd 의 canonical 사본에서\n"
    "    `--paths <경로>` 로 실행하세요 (REPO 앵커가 PM 홈을 가리키면 diff 가 빕니다).\n"
    "  · 신규 파일만 변경했다면 먼저 `git add` 후 재실행하세요 (diff 는 tracked 변경만 봅니다)."
)

# PM 홈 앵커 재지정 안내. 위 빈-diff 안내(:166)를
# *능동 게이트*로 승격한다: REPO 앵커가 adopter#0 PM 홈(import 사본)을 가리키면, 빈 diff 로 실패할
# 때까지 기다리지 않고 diff 추출 전에 canonical 코드 worktree 재지정을 안내하며 fail-loud 한다.
_PM_HOME_ANCHOR_GUIDANCE = (
    "오류: 외부 리뷰를 adopter#0 PM 홈(import 사본)에서 실행했습니다 — 실 코드 변경은 worktree 에\n"
    "  있어 여기서는 diff 가 비어 가짜 통과(false-green)가 납니다 (REPO 앵커가 PM 홈을 가리킴).\n"
    "  · canonical 코드 worktree 에서 재실행하세요:  cd {worktree}\n"
    "    리뷰 경로는 `--ticket T-NNNN`(touches 로 자동) 또는 `--paths <경로>`(직접 지정)로 핀하세요.\n"
    "  · 이 앵커에서 의도적으로 실행하려면 `--paths <경로>` 로 명시하세요 — override 는 `--paths` 만\n"
    "    받습니다(`--ticket` 은 여전히 차단·touches 상대경로라 PM 홈 git 기준 빈 diff false-green).\n"
    "  (현재 앵커: {anchor})"
)

# 라운드 상한 초과 fail-loud 안내. 같은 게이트로
# 승인 없이 limit 회를 넘겨 실 전송이 시도되면 diff 추출·리뷰어 호출 전에 이 안내로 차단한다
# (과금·외부 전송 게이트라 초과분은 기계가 멈춘다·자의 우회 불가). 유일한 재개 경로는 사용자
# 승인 후 `--ack-rounds` — 환경 문제 우회 플래그가 아니다.
_ROUND_LIMIT_GUIDANCE = (
    "오류: 외부 리뷰 라운드 상한 초과 — 게이트 {gate} 에서 승인 없이 {unacked}회 실 전송했습니다\n"
    "  (상한 {limit}). 외부 전송·과금 게이트라 초과분은 기계가 멈춥니다 — 자의 우회 불가.\n"
    "  · 지금까지의 리뷰 라운드 수렴 상황을 **사용자에게 보고하고 대기**하세요.\n"
    "  · 사용자 승인을 받은 뒤에만 `--ack-rounds` 로 재개하세요 (승인분 +{limit}라운드):\n"
    "      python3 .project_manager/tools/external_review.py --gate {gate} --ack-rounds [기존 옵션]\n"
    "  · 상한 자체 조정은 local.conf `external_review_round_limit` (기본 4).\n"
    "  (장부: .project_manager/.local/review_rounds.json · count={count} acked_through={acked})"
)


# ── 설정 ──────────────────────────────────────────────────────────────────


def local_config() -> dict[str, str]:
    """per-clone local.conf 를 KEY=value 로 읽는다 (없으면 빈 dict). board.py 와 동일 포맷."""
    conf: dict[str, str] = {}
    if not LOCAL_CONF.exists():
        return conf
    for line in LOCAL_CONF.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        conf[key.strip()] = value.strip()
    return conf


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


def _denylist_patterns(conf: dict[str, str]) -> tuple[str, ...]:
    extra = conf.get("review_denylist_extra", "").strip()
    extras = tuple(p for p in re.split(r"[,\s]+", extra) if p) if extra else ()
    return _SECRET_DENYLIST_PATTERNS + extras


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


# ── 라운드 상한 장부 ─────────────
# 외부 리뷰는 과금·전송 게이트라 라운드가 무한정 이어지면 비용이 쌓인다(PM 10차 실측: 한 게이트
# 클러스터 25라운드). PM 자의 "수렴 판단"을 기계 판정으로 대체한다([[mechanize-dont-instruct-llm]]):
# `--gate <T-NNNN>` 별로 실 전송 횟수(count)와 사용자 승인 수위(acked_through)를 per-clone·git-ignored
# 장부에 기록하고, 승인 없이 limit 을 넘기면 실행 전에 거부한다. 장부는 세션/클론 로컬 현상이라
# `.project_manager/.local/`(regression/livegate sidecar 와 동위·board 상태 아님)에 둔다. 경로는
# 호출 시점 REPO(module-level·monkeypatch 가능)에서 파생해 hermetic 테스트가 tmp 로 격리할 수 있게
# 한다(_tickets_dir 동형). 손상 장부는 빈 장부로 fail-soft(회귀해소·regression flag 동형).


def _round_ledger_path() -> Path:
    """라운드 장부 경로 — `<REPO>/.project_manager/.local/review_rounds.json` (호출 시점 REPO 파생)."""
    return REPO / ".project_manager" / ".local" / "review_rounds.json"


def _load_round_ledger() -> dict:
    """라운드 장부(gate→{count, acked_through})를 읽는다 — 없거나 손상 시 빈 dict(fail-soft)."""
    try:
        data = json.loads(_round_ledger_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def _round_ledger_lock_path() -> Path:
    """라운드 장부 배타락 파일 — `<REPO>/.project_manager/.local/review_rounds.lock` (호출 시점 REPO)."""
    return REPO / ".project_manager" / ".local" / "review_rounds.lock"


def _flock_acquire(fd: int) -> None:
    """OS 배타락 획득 (블로킹). POSIX=fcntl.flock·Windows=msvcrt.locking·둘 다 없으면 무락 폴백.

    stdlib 만 사용한다(외부 filelock 의존 금지·board.py `_flock_acquire` 동형·self-contained 복제).
    임포트 안 되는 희귀 환경은 단일-머신 전제로 무락 진행(락 파일 자체는 존재)."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    except (ImportError, OSError):
        pass  # best-effort — 락 프리미티브 없음/실패 시 무락 진행


def _flock_release(fd: int) -> None:
    """OS 배타락 해제 (close 가 자동 해제하지만 명시적으로 풀어 둔다·board.py 동형)."""
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    except ImportError:
        pass
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    except (ImportError, OSError):
        pass


@contextlib.contextmanager
def _round_ledger_lock() -> Iterator[None]:
    """라운드 장부 read-modify-write 를 직렬화하는 OS 파일락.

    확인(상한 대조)→예약(count+1)→저장을 하나의 임계 구역으로 묶어, 동시 실행 2개가 같은 잔여
    슬롯을 통과해 상한을 우회하는 것을 막는다. 프로세스가 죽으면 OS 가 락을 자동 해제(stale-lock
    없음). **재진입 금지**(flock 관례·board_lock 동형) — 예약과 환불은 *각자 독립* 락 구간이다
    (중첩 아님). 락 프리미티브 미지원 환경은 무락 폴백(board.py 와 동일·인터페이스 불변)."""
    lock_path = _round_ledger_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _flock_acquire(fd)
        try:
            yield
        finally:
            _flock_release(fd)
    finally:
        os.close(fd)  # close 만으로도 OS 가 락 해제 (크래시 안전망)


def _as_int(value: object) -> int:
    """장부 필드를 int 로 강제 (손상/누락 → 0·fail-soft)."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _gate_entry(ledger: dict, gate: str) -> dict:
    """게이트의 장부 항목을 정규화된 {count, acked_through} 로 반환하고 ledger 에 심는다.

    항목이 없거나 손상(비-dict·비정수 필드)이면 0/0 으로 정규화해 저장한다 — 반환 dict 를 그 자리에서
    수정한 뒤 `_save_round_ledger(ledger)` 하면 깨끗한 값이 기록된다(read→normalize→mutate→write)."""
    entry = ledger.get(gate)
    if not isinstance(entry, dict):
        entry = {}
    normalized = {
        "count": _as_int(entry.get("count")),
        "acked_through": _as_int(entry.get("acked_through")),
    }
    ledger[gate] = normalized
    return normalized


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

    base 가 'HEAD' 이면 스테이징+언스테이징 변경분(없으면 HEAD~1..HEAD)을 추출한다.
    run_fn — subprocess.run 대체 주입 (테스트용).
    """
    _run = run_fn or subprocess.run
    if base == "HEAD":
        staged = _run(["git", "-C", str(REPO), "diff", "--cached", "--"] + paths,
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
        unstaged = _run(["git", "-C", str(REPO), "diff", "--"] + paths,
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
        combined = (staged.stdout if staged.returncode == 0 else "") + \
                   (unstaged.stdout if unstaged.returncode == 0 else "")
        if not combined.strip():
            commit = _run(["git", "-C", str(REPO), "diff", "HEAD~1..HEAD", "--"] + paths,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
            if commit.returncode == 0:
                combined = commit.stdout
        raw_diff = combined
    else:
        result = _run(["git", "-C", str(REPO), "diff", base, "--"] + paths,
                      capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode != 0:
            raise RuntimeError(f"git diff 실패 (rc={result.returncode}): {result.stderr.strip()}")
        raw_diff = result.stdout

    # 제외 목록을 삼키지 않고 그대로 반환한다 — 호출자(main)가 모드별 제외 보고(차단/병기)를
    # 소유한다. stderr 경고도 main 으로 이관해 제외 메시징을 한곳에서 관장한다.
    return filter_secret_hunks(raw_diff, denylist)


# ── ticket touches 파싱 ───────────────────────────────────────────────────


def parse_ticket_touches(ticket_id: str) -> list[str]:
    """board ticket frontmatter 의 touches 필드를 파싱해 경로 목록을 반환한다.

    YAML frontmatter 직접 파싱 (board.py 를 import 하지 않음). 못 찾으면 빈 목록.

    ticket 디렉토리는 `_tickets_dir()`로 *호출 시점* 해소한다 —
    board/ 분리 후 wiki/ legacy 위치(stale·ticket 미발견)를 안 보게.
    """
    tickets_dir = _tickets_dir()
    for status_dir in STATUS_DIRS:
        dir_path = tickets_dir / status_dir
        if not dir_path.exists():
            continue
        for ticket_file in dir_path.glob(f"{ticket_id}-*.md"):
            return _parse_touches_from_file(ticket_file)
        exact = dir_path / f"{ticket_id}.md"
        if exact.exists():
            return _parse_touches_from_file(exact)
    return []


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
            inline_match = re.match(r"^touches\s*:\s*\[(.+)\]", line)
            if inline_match:
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
                           **_ignored) -> subprocess.CompletedProcess:
    """기본 리뷰어 러너 — pm_relay 공용 워치독 경유(무진행 주 판정 + 벽시계 백스톱).

    `subprocess.run(..., timeout=)` 을 대체한다. 그 단일 호출은 증분 관측이 아예 없어서 (a) 정상
    진행을 벽시계로 죽이고 (b) `TimeoutExpired.stdout` 에 실려 온 부분 산출물을 아무도 안 읽어
    통째로 버렸다(kill 된 리뷰의 raw 가 헤더 138바이트뿐이던 실측). startup 창/재시도는 리뷰어
    이름 특례가 아니라 **해소된 공유 프로필 선언**을 따른다. 따라서 기본 codex(False)는 종전처럼
    꺼져 있고, 알려진 startup stall 축인 opencode(True)만 유한 재시도를 얻는다."""
    reviewer_cmd = shlex.join(argv)
    relay, first_event_timeout, retries = _reviewer_watchdog_settings(reviewer_cmd)
    return relay.run_with_first_event_watchdog(
        argv,
        first_event_timeout=first_event_timeout,
        overall_timeout=float(timeout),
        retries=retries,
        idle_timeout=idle_timeout,
        input_text=input,
    )  # text 는 워치독 기본(True·utf-8/replace 고정) — 인코딩은 _WatchedPopen 이 소유.


def harness_cap_advisory(
    env: dict[str, str] | None = None, *, execution_budget: int
) -> str | None:
    """리뷰 엔진보다 먼저 끝나는 외부 Bash 명시호출 최대상한을 런타임에 loud 표면화한다.

    문서의 호출층 timeout 계약이 누락된 기존 채택자도 진단을 얻는 백스톱이다. 실행은 차단하지
    않으며 Codex처럼 공개 상한 env가 없는 표면은 판정하지 않는다.
    """
    env = os.environ if env is None else env
    matched_caps = tuple(
        (harness, _REVIEW_HARNESS_CAP_ENV.get(harness))
        for harness, markers in _HARNESS_SESSION_MARKERS.items()
        if any(env.get(marker) for marker in markers)
    )
    matched_caps = tuple(
        (harness, cap_key)
        for harness, cap_key in matched_caps
        if cap_key is not None
    )
    if not matched_caps:
        return None
    relay = _load_relay()
    required = int(relay.harness_cap_required_budget(execution_budget))
    warnings = []
    for harness, cap_key in matched_caps:
        raw = env.get(cap_key)
        try:
            cap_seconds = int(raw) / 1000.0
        except (TypeError, ValueError, OverflowError):
            warnings.append(
                f"[external-review] 경고: {harness} 호출층 상한 {cap_key}={raw!r} 해석 불가 — "
                f"리뷰 실행+재시도별 정리+박제 여유 {required}s 이상을 Bash tool timeout으로 명시하세요."
            )
            continue
        if cap_seconds < required:
            warnings.append(
                f"[external-review] 경고: {harness} 호출층 최대상한 "
                f"{cap_key}={cap_seconds:g}s < "
                f"리뷰 실행+재시도별 정리+박제 여유 {required}s — 엔진 진단/부분 산출물 보존 전에 하네스가 kill할 수 "
                "있습니다. Bash tool 호출에 장시간 timeout을 명시하세요."
            )
    return "\n".join(warnings) or None


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
    partial_stdout = exc.output or ""
    partial_stderr = exc.stderr or ""
    parts = [head]
    if partial_stdout:
        parts.append(f"\n[중단 시점까지의 stdout — {len(partial_stdout)}자 보존]\n{partial_stdout}")
    if partial_stderr:
        parts.append(f"\n[중단 시점까지의 stderr — {len(partial_stderr)}자 보존]\n{partial_stderr}")
    return "".join(parts)


class ReviewerRunSeamError(TypeError):
    """주입 runner 의 호출 계약 불일치 — 리뷰어 프로세스 오류와 구분하는 loud sentinel."""


def _reviewer_run_kwargs(run_fn: Callable, argv: list[str], *,
                         prompt: str, timeout: int, idle_timeout: float | None) -> dict:
    """기존 subprocess.run 호환 seam 을 보존하고 명시 지원 runner 에만 idle_timeout 을 확장한다.

    `**kwargs` 만으로는 새 키를 실제 소비하는지 알 수 없다(`subprocess.run` 은 **kwargs 를 Popen 에
    넘겨 뒤늦게 TypeError). 따라서 시그니처에 `idle_timeout` 이 명시된 runner 에만 전달한다.
    호출 전에 bind 해 seam skew 를 프로세스 실행 오류와 분리한다.
    """
    kwargs = {
        "input": prompt,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
    }
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
) -> tuple[bool, str, bool]:
    """run_reviewer 본체 + 외부 프로세스 스폰 여부(started) 신호.

    반환: (성공 여부, 출력 텍스트, started). started=False = 외부 프로세스가 *확실히 시작되지
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
        return False, "[reviewer_cmd 가 비어 있음 — local.conf 확인]", False
    if timeout is None:  # 미지정 호출(공개 facade) — 리뷰어 프로필의 벽시계 백스톱.
        timeout = int(reviewer_profile(reviewer_cmd).wall_timeout)
    try:
        kwargs = _reviewer_run_kwargs(
            _run, argv, prompt=prompt, timeout=timeout,
            idle_timeout=_reviewer_idle_timeout(reviewer_cmd, idle_timeout),
        )
        result = _run(argv, **kwargs)
        if metrics is not None:
            metrics["rc"] = int(result.returncode)
            metrics["silence_sec"] = getattr(result, "silence_sec", None)
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return result.returncode == 0, output, True
    except subprocess.TimeoutExpired as exc:
        # 프로세스가 시작돼 실행 중 타임아웃 — 프롬프트가 이미 전송·과금됐을 수 있다 → started=True.
        if metrics is not None:
            metrics["silence_sec"] = getattr(
                exc, "silence_seconds", getattr(exc, "idle_seconds", None)
            )
        return False, _timeout_output(timeout, exc), True
    except FileNotFoundError:
        # 실행 파일 자체가 없어 exec 실패 — 아무것도 전송되지 않았다 → started=False (환불 대상).
        if metrics is not None:
            metrics["rc"] = 127
        return False, f"[리뷰어 명령 '{argv[0]}' 를 찾을 수 없음 — 설치 또는 PATH 확인]", False
    except ReviewerRunSeamError as exc:
        # 호출 전 bind 실패 — 외부 프로세스는 확실히 시작되지 않았다. 라운드 환불 가능.
        return False, f"[리뷰어 runner seam 계약 오류 — 호출 전 차단: {exc}]", False
    except TypeError as exc:
        # runner 내부에서 발생한 키워드/시그니처 skew. 시작 여부는 불명이라 보수적으로 started=True.
        return False, f"[리뷰어 runner seam 호출 오류: {exc}]", True
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
            if getattr(exc, "stderr", ""):
                output += f"\n[stderr]\n{exc.stderr}"
            return False, output, True
        # 시작 여부 불확실 — 보수적으로 started=True (상한 우회 방지 > 과잉 카운트).
        return False, f"[리뷰어 실행 오류: {exc}]", True


def run_reviewer(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
    idle_timeout: float | None = None,
) -> tuple[bool, str]:
    """reviewer_cmd 를 stdin(=프롬프트)으로 실행한다. 반환: (성공 여부, 출력 텍스트).

    2-튜플 공개 facade — 스폰 여부(started)까지 필요한 내부 호출은 `_run_reviewer_ex` 를 쓴다."""
    ok, output, _started = _run_reviewer_ex(prompt, reviewer_cmd, timeout, run_fn, idle_timeout)
    return ok, output


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
    """
    must_fix_items = _extract_must_fix_items(output)
    section_found = bool(_MUST_FIX_SECTION_RE.search(output))

    if section_found and _is_none_items(must_fix_items):
        has_must_fix = False
    elif section_found and must_fix_items:
        has_must_fix = True
    else:
        has_must_fix = any(token in output for token in _REJECT_TOKENS)

    has_pass = any(token in output for token in _PASS_TOKENS)

    verdict_line_match = re.search(r"판정\s*:\s*(\S+)", output)
    if verdict_line_match:
        verdict_word = verdict_line_match.group(1).strip()
        if any(tok in verdict_word for tok in
               ("통과", "PASS", "pass", "승인", "APPROVE", "approve", "LGTM", "lgtm")):
            if not must_fix_items or _is_none_items(must_fix_items):
                has_must_fix = False
        elif any(tok in verdict_word for tok in ("반려", "REJECT", "reject")):
            has_must_fix = True

    return {"has_must_fix": has_must_fix, "has_pass": has_pass}


# ── 결과 저장 ─────────────────────────────────────────────────────────────


def _raw_storage(output_dir: Path | None = None) -> tuple[Path, Path]:
    """외부리뷰 raw/공유 장부 위치(REPO 미해소만 tempdir 폴백)."""
    return _load_relay().raw_storage_paths(
        REPO, "review", output_dir, temp_dir=Path(tempfile.gettempdir())
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
) -> dict:
    """외부 리뷰어를 실행하고 결과를 수합한다.

    반환 dict: reviewer / ok / output / verdict / file / failed / started / any_must_fix / all_pass.
    `started` = 외부 프로세스가 스폰됐는가(전송·과금 가능성) — 라운드 카운트 환불
    판정에 쓴다(False = 확실히 전송 전 실패 → 예약 환불). `idle_timeout` = 무진행 상한(None=공유
    기본) — 타임아웃 시에도 `output` 에 부분 산출물이 실려 `save_output` 이 그대로 박제한다.
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
        prompt, reviewer_cmd, timeout, run_fn, idle_timeout, metrics
    )
    elapsed = time.monotonic() - started_at
    verdict = parse_verdict(output)
    _write_reserved_output(
        raw_path,
        output,
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
        "output": output,
        "verdict": verdict,
        "file": raw_path,
        "failed": not ok,
        "started": started,
        "any_must_fix": ok and verdict["has_must_fix"],
        "all_pass": ok and verdict["has_pass"] and not verdict["has_must_fix"],
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
                        help="git diff 기준 ref (기본: HEAD — 스테이징+언스테이징)")
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
            f"cwd-only={target_only!r} (양쪽 유효 denylist 합집합 적용)"
        )

    if include_review_paths:
        engine_paths = _normalized_review_paths(engine_conf)
        target_paths = _normalized_review_paths(target_conf)
        if engine_paths != target_paths:
            differences.append(
                f"review_paths: engine={engine_paths!r}, cwd={target_paths!r}"
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
        f"{key}: engine={engine_values[key]!r}, cwd={target_values[key]!r}"
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
    resolution_note: str | None = None,
) -> str:
    """두 송신 표면 공용 never-block loud 경고 문구."""
    details = "\n".join(f"  · {item}" for item in divergence.differences)
    resolution = resolution_note or (
        f"이번 실행에서는 실행 엔진 conf가 이깁니다: {divergence.engine_conf_path}\n"
        f"  차단하지 않고 계속합니다. 같은 프로필을 원하면 {cwd_label} conf의 위 키 값을 engine "
        "conf와 맞추거나, 의도한 local.conf를 가진 엔진 사본을 실행하세요. 실행 결과 raw 헤더의 "
        "# local_conf/# resolved_profile에도 승자 provenance를 기록합니다."
    )
    return (
        f"경고: local.conf 프로필 분기 감지 ({surface}) — {cwd_label} repo와 실행 엔진 "
        "REPO가 다르고 외부 송신 프로필/내용 값이 실제로 갈립니다.\n"
        f"  · engine REPO: {divergence.engine_repo}\n"
        f"  · engine conf: {divergence.engine_conf_path}\n"
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


def main(argv: list[str] | None = None) -> int:
    _console_encoding = _load_module_from_path(
        Path(__file__).resolve().with_name("console_encoding.py"),
        "console_encoding.py",
        verifier=_verify_engine_rev,
    )
    _console_encoding.configure_console_utf8()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    conf = local_config()
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
    conf_path = _local_conf_path(REPO)
    profile = resolved_reviewer_profile(reviewer_cmd, timeout, idle_timeout)

    # 상대 `.project_manager/tools/external_review.py` 선택을 결정한 호출 cwd repo와 실행 엔진
    # 사본 REPO를 같은 입력으로 대조한다. reviewer_cmd의 **실제 해소값**이 갈릴 때만 경고하며,
    # 동일(default 명시 포함)·대상 conf 부재·non-repo cwd는 조용히 통과한다.
    target_repo = repo_root_from_cwd(Path.cwd())
    # 정상 실행·dry-run 공용 첫 provenance. 이후 cap/reanchor/diff 진단이 붙더라도 어느 엔진 conf와
    # reviewer tuple을 해소했는지가 stderr에 남는다.
    print(
        f"[external-review] config provenance: local_conf={conf_path} "
        f"· resolved_profile={profile}",
        file=sys.stderr,
    )
    deferred_warnings = resolution_warnings.getvalue()
    if deferred_warnings:
        print(deferred_warnings, end="", file=sys.stderr)
    try:
        divergence = local_conf_divergence(
            engine_repo=REPO,
            engine_conf=conf,
            target_repo=target_repo,
            selector=reviewer_profile_config,
        )
    except TargetLocalConfReadError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if divergence is not None:
        print(
            format_local_conf_divergence(
                divergence, surface="external_review", cwd_label="호출 cwd",
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

    # PM 홈 앵커 재지정 가드. REPO 앵커가 adopter#0
    # PM 홈(실 board 소유 + canonical 코드 worktree 보유)을 가리키고 `--paths` 로 명시 override 하지
    # 않았다면, import 사본을 리뷰해 빈 diff false-green 이 나기 전에 fail-loud 로 차단하고 canonical
    # worktree 재지정을 안내한다(빈-diff 안내 :166 을 능동 게이트로 승격). diff 추출·dry-run·비활성
    # no-op 보다 앞서므로 잘못된 형상은 codex 전송 없이 미리보기에서도 드러난다. `--paths` 명시 시엔
    # 통과(deliberate override) — 그래도 빈 diff 면 아래 빈-diff 가드가 백스톱. REPO 는 호출 시점 읽어
    # (module global) 테스트 monkeypatch 를 추종한다.
    if not args.paths:
        reanchor_target = _pm_home_reanchor(REPO)
        if reanchor_target is not None:
            print(_PM_HOME_ANCHOR_GUIDANCE.format(worktree=reanchor_target, anchor=REPO),
                  file=sys.stderr)
            return 1

    # 경로 결정: --paths > --ticket touches > local.conf review_paths > DEFAULT_PATHS
    include_conf_review_paths = False
    if args.paths:
        paths = args.paths
    elif args.ticket:
        touches = parse_ticket_touches(args.ticket)
        if not touches:
            print(f"경고: ticket {args.ticket} 의 touches 미발견 — 기본 경로 사용", file=sys.stderr)
            paths = _configured_paths(conf)
            include_conf_review_paths = True
        else:
            paths = touches
    else:
        paths = _configured_paths(conf)
        include_conf_review_paths = True

    try:
        content_resolution = resolve_review_content_conf(
            engine_repo=REPO,
            engine_conf=conf,
            target_repo=target_repo,
            include_review_paths=include_conf_review_paths,
        )
    except TargetLocalConfReadError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1
    if content_resolution.divergence is not None:
        review_paths_note = (
            "review_paths의 이번 범위는 엔진 conf가 정했습니다"
            if include_conf_review_paths
            else "review_paths는 --paths/유효 ticket touches 완전지정으로 conf 비교에서 제외했습니다"
        )
        print(
            format_local_conf_divergence(
                content_resolution.divergence,
                surface="external_review 송신 내용",
                cwd_label="호출 cwd",
                resolution_note=(
                    "denylist는 양쪽 해소값의 합집합을 적용하고 "
                    f"{review_paths_note}: {_local_conf_path(REPO)}\n"
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
        print(_EMPTY_DIFF_GUIDANCE, file=sys.stderr)
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
    if args.gate:
        limit = _round_limit(conf)
        with _round_ledger_lock():
            ledger = _load_round_ledger()
            entry = _gate_entry(ledger, args.gate)
            count, acked = entry["count"], entry["acked_through"]
            if args.ack_rounds:
                entry["acked_through"] = count      # 승인 수위 상향 (+limit 창)
                entry["count"] = count + 1          # 이 호출도 실 전송 → 예약
                _save_round_ledger(ledger)
                reserved_gate = args.gate
                print(f"라운드 상한 승인 재개: 게이트 {args.gate} — acked_through={count} "
                      f"(+{limit}라운드).", file=sys.stderr)
            elif count - acked >= limit:
                print(_ROUND_LIMIT_GUIDANCE.format(
                    gate=args.gate, unacked=count - acked, limit=limit,
                    count=count, acked=acked), file=sys.stderr)
                return EXIT_ROUND_LIMIT_EXCEEDED    # 예약 없음 (전송 전 거부)
            else:
                entry["count"] = count + 1          # 호출 전 라운드 예약
                _save_round_ledger(ledger)
                reserved_gate = args.gate
    elif args.ack_rounds:
        print("경고: --ack-rounds 는 --gate 와 함께 써야 합니다 (게이트 단위 장부) — 무시.",
              file=sys.stderr)

    print(f"외부 리뷰어 실행 중: {reviewer_cmd}", file=sys.stderr)
    result = run_review(
        prompt=prompt, reviewer_cmd=reviewer_cmd,
        timeout=timeout, output_dir=output_dir, idle_timeout=idle_timeout,
        local_conf_path=conf_path, resolved_profile=profile,
    )
    print_summary(result, gate=args.gate, excluded=excluded)

    # 예약 환불 — 외부 프로세스가 *확실히 시작되지 않은* 경우(started=False·스폰 실패·
    # 전송 0)만 되돌린다. 타임아웃·비정상 종료(started=True)는 프롬프트가 이미 전송·과금됐을 수 있어
    # 유지한다(반복 타임아웃 상한 우회 차단). 예약과 같은 lock 아래 재-load→감소→저장(원자·MF-B).
    if reserved_gate is not None and not result.get("started", True):
        with _round_ledger_lock():
            ledger = _load_round_ledger()
            entry = _gate_entry(ledger, reserved_gate)
            if entry["count"] > 0:
                entry["count"] -= 1
            _save_round_ledger(ledger)

    return determine_exit_code(result)


if __name__ == "__main__":
    sys.exit(main())
