#!/usr/bin/env python3
"""외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI (ADR-0004).

사용:
    python3 .project_manager/tools/external_review.py [옵션]

동작:
  ① git diff <base> -- <paths> 추출 (시크릿 denylist 경로 자동 제외)
  ② (프로젝트 맥락 헤더 +) diff 결합 → 표준 프롬프트 생성
  ③ 외부 리뷰어 실행 (reviewer_cmd, stdin 으로 프롬프트 주입, read-only 권장)
  ④ 출력에서 판정(통과/반려)·must-fix 파싱
  ⑤ 결과 요약 stdout + 원문 파일 저장 (/tmp 또는 --output-dir)

기본 비활성 (ADR-0004):
  - 코드 diff 가 *외부로 전송*되므로 기본 OFF. local.conf `external_review_enabled=true`
    또는 `board.py init` / `pm_update` 시 opt-in 으로 켠다. 비활성 시 actual 호출은
    no-op(exit 0)이고 `--dry-run` 은 항상 허용(로컬 미리보기·미전송), `--force` 로 1회 강제.
    단 빈/공백 diff 는 dry-run·비활성 포함 무조건 exit 1 (false-green 원천 차단·T-0326).

종료 코드/신호:
  - 리뷰어 실패(인증/한도/네트워크/타임아웃) → exit 1 + stdout 에 FALLBACK_INTERNAL
    (= 내부 code-reviewer 서브에이전트로 폴백하라는 신호)
    타임아웃 상한: 기본 900s · local.conf `external_review_timeout` · 일회성 `--timeout` (T-0467)
  - must-fix 감지 → exit 1
  - 통과 → exit 0
  - 라운드 상한 초과(--gate 별) → exit 4 (실행 전 거부·전용 rc·T-0457). 같은 게이트로 승인 없이
    limit 회(local.conf external_review_round_limit·기본 4) 실 전송하면 이후 실행을 기계 차단하고
    "사용자 보고·대기" loud 안내를 낸다 — 사용자 승인 후 `--ack-rounds` 로 +limit 재개.

설계 (ADR-0004):
  - 어댑터 seam: 외부 도구를 `reviewer_cmd`(local.conf) 뒤로 격리 → codex 외 교체 가능.
    기본 `codex exec --sandbox read-only --skip-git-repo-check` (stdin 으로 프롬프트).
  - 도메인 외부화: 프로젝트 맥락은 `.project_manager/review_context.local.md`(인스턴스 소유)
    가 있으면 주입, 없으면 generic 헤더. 엔진 도구엔 도메인 콘텐츠 0.
  - subprocess DI (run_fn 매개변수) — 테스트에서 mock 주입 가능.
  - 외부 호출은 코드를 수정하지 않는다 (read-only 인자 사용 권장).
  - 시크릿 denylist (.env·*secret*·*credential*·*.key·*token*·*.pem 등) 파일은 diff 에서
    자동 제외한다. 제외 사실은 판정에 반영 (T-0428) — --paths 명시 지정분 제외는 차단(exit 1),
    --ticket/기본 암묵 수집분 제외는 종합 판정 라인에 병기. review_denylist_extra 로 추가 가능.
  - 라운드 상한 기계 차단 (T-0457): 외부 리뷰(과금·전송)가 무한 반복되지 않게 `--gate <T-NNNN>`
    별 라운드 장부(`.project_manager/.local/review_rounds.json`·per-clone·git-ignored)에 실 전송을
    count 하고, 승인 없이 limit(기본 4)회를 넘기면 실행 *전에* 거부(exit 4)한다. PM 자의 판단을
    기계 판정으로 대체 — 사용자 승인 후 `--ack-rounds` 로만 재개한다([[mechanize-dont-instruct-llm]]).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fnmatch
import json
import os
import re
import importlib.util
import shlex
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Iterator

# ── REPO 앵커 (상향 탐색·board_root() graceful 탐지 동형·ADR-0033 ①) ──────────
# 하드코딩 `parents[2]` 는 tools 가 `<root>/.project_manager/tools/` 정확히 2단 깊이에 있다고
# 가정한다 — 채택자 형상(PM 홈/worktree 구조 상이·다른 깊이)에선 어긋난다(finance_dev 제보 D2).
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


# ── board root 추종 (board/ 분리·ADR-0033 ①·T-0162 A6) ───────────────────────
# board(tickets)는 `.project_manager/board/`(submodule)로 분리될 수 있다(ADR-0033 ①). 그러면
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


# ── PM 홈 앵커 재지정 감지 (T-0367·adopter#0 false-green 게이트) ────────────────
# adopter#0(ADR-0027)에서 external_review 의 import 사본은 PM 홈(②)에 있어 REPO 가 PM 홈으로
# 해소된다 — 실 코드 변경은 canonical worktree(①)에 있으므로 `git diff` 가 비어 codex 가 "변경
# 없음"을 통과로 판정하는 false-green 이 난다([[adopter0-gates-use-worktree-canonical]]·PM 65).
# board.py `_pm_home_worktree_misanchor`(T-0345)의 *역방향*: 거긴 worktree 에서 실행된 board 조작을
# 잡고, 여긴 PM 홈에서 실행된 외부 리뷰를 잡아 worktree 로 재지정한다. 순수 filesystem 판정(subprocess
# 불요)이라 hermetic — REPO 를 module-level 로 두어 테스트가 monkeypatch 하고, 헬퍼는 anchor/conf 를
# 명시 인자로 받아 DI seam 이 된다(board `_has_real_board` 를 import 없이 동형 복제·각 파일 self-contained).

def _owns_real_board(pm_dir: Path) -> bool:
    """`.project_manager` 디렉토리(`pm_dir`)가 실 티켓(`T-*.md`)을 가진 board 를 소유하는가.

    board/ 분리(ADR-0033 ①)면 `board/tickets`, legacy 면 `wiki/tickets` 상태 디렉토리에 실 티켓이
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
    로컬 checkout(`pm_import --from <로컬>` 정규 채택자·T-0053 자동 기록)일 수 있어, 실 board 를
    소유한 정규 채택자에서 stale/무관 checkout 으로 오안내하며 정상 리뷰를 hard-block 한다(빈-diff
    백스톱도 실 diff 가 non-empty 면 무력). `work/` 슬롯 스캔만으로 adopter#0(ADR-0027) 재지정을
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

    2중 conjunction (오탐 0 지향·fail-soft): (1) anchor 가 실 board 소유(PM 홈) — worktree(①·코드
    전용·board 미소유)에서 실행하면 여기서 탈락해 None(정상·재지정 불요), (2) anchor 아래 canonical
    코드 worktree(`work/<name>`) 존재. 솔로/일반 채택자(로컬 upstream 포함)는 (1) 또는 (2) 미충족으로
    None(무영향)."""
    if not _owns_real_board(anchor / ".project_manager"):
        return None
    return _canonical_worktree(anchor)

# 외부 리뷰어 기본 명령 (local.conf reviewer_cmd 로 교체 가능)
DEFAULT_REVIEWER_CMD = "codex exec --sandbox read-only --skip-git-repo-check"

# 외부 호출 타임아웃 (초). 실 게이트 실측(T-0467·2026-07-26 세션 게이트 5건+대형 1건):
# 평범한 diff 153~294초·13파일 대형 227초 — 구 기본 180초는 평상 대역 *안*이라 상시 타임아웃
# 구조였다. 900 = 평상 최대(294s)의 ~3배 여유. 여전히 유한 상한이며 clone별 조정은
# local.conf `external_review_timeout`, 일회성 조정은 `--timeout`으로 한다.
EXTERNAL_TIMEOUT_SECONDS = 900

# 라운드 상한 (T-0457) — 같은 --gate 로 승인 없이 이 횟수를 넘겨 실 전송하면 이후 실행을 거부한다.
# 기본 4 는 사용자 전역 규율(외부 리뷰 ">3~4 라운드면 수렴 판단")의 기계화. local.conf
# external_review_round_limit 로 조정 가능.
DEFAULT_ROUND_LIMIT = 4

# 라운드 상한 초과 전용 종료 코드 (기존 0=통과·1=반려/실패/오류·2=argparse·3=예약 과 구분·T-0457).
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

# 빈-diff fail-loud 안내 (T-0326 — adopter#0 false-green 원천 차단).
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

# PM 홈 앵커 재지정 안내 (T-0367 — adopter#0 false-green 게이트 승격). 위 빈-diff 안내(:166)를
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

# 라운드 상한 초과 fail-loud 안내 (T-0457 — codex 게이트 무한 라운드 기계 차단). 같은 게이트로
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


def _resolve_timeout(args: argparse.Namespace, conf: dict[str, str]) -> int:
    """외부 리뷰 timeout 을 `--timeout` > local.conf > 기본 순서로 해소한다.

    CLI 양수값은 argparse 검증을 통과한 명시 override다. local.conf 는 사용자 설정이므로
    깨진 값(비정수/0/음수)은 실행을 막지 않고 stderr 경고와 함께 기본값으로 fail-soft 한다.
    pm_delegate._resolve_timeout 과 같은 계약이다.
    """
    if args.timeout is not None:
        return args.timeout
    raw = (conf.get("external_review_timeout") or "").strip()
    if not raw:
        return EXTERNAL_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        print(f"경고: local.conf external_review_timeout={raw!r} 비정수 — 기본 "
              f"{EXTERNAL_TIMEOUT_SECONDS}s 사용.", file=sys.stderr)
        return EXTERNAL_TIMEOUT_SECONDS
    if value <= 0:
        print(f"경고: local.conf external_review_timeout={value} ≤0 — 기본 "
              f"{EXTERNAL_TIMEOUT_SECONDS}s 사용.", file=sys.stderr)
        return EXTERNAL_TIMEOUT_SECONDS
    return value


def _configured_paths(conf: dict[str, str]) -> list[str]:
    raw = conf.get("review_paths", "").strip()
    return [p for p in re.split(r"[,\s]+", raw) if p] if raw else list(DEFAULT_PATHS)


def _denylist_patterns(conf: dict[str, str]) -> tuple[str, ...]:
    extra = conf.get("review_denylist_extra", "").strip()
    extras = tuple(p for p in re.split(r"[,\s]+", extra) if p) if extra else ()
    return _SECRET_DENYLIST_PATTERNS + extras


def _round_limit(conf: dict[str, str]) -> int:
    """라운드 상한 (local.conf external_review_round_limit·기본 `DEFAULT_ROUND_LIMIT`·T-0457).

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


# ── 라운드 상한 장부 (T-0457 — codex 게이트 무한 라운드 기계 차단) ─────────────
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
    (T-0457 MF-B). os.replace 는 원자 rename — 독자는 옛 파일 또는 새 파일만 본다(부분기록 없음).
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
    """라운드 장부 read-modify-write 를 직렬화하는 OS 파일락 (T-0457 MF-B).

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
    돌려줘 제외 보고(차단 안내·판정 병기)에 근거를 실을 수 있게 한다(T-0428)."""
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
    """`--paths` 명시 지정분이 denylist 로 제외됐을 때의 차단 안내 (T-0428).

    사용자가 `--paths` 로 직접 지목한 경로가 시크릿 denylist 에 걸려 diff 에서 빠지면, 그 상태로
    리뷰를 진행해 '통과'를 내면 게이트가 실제보다 넓게 검증한 것처럼 보인다(false-confidence). 빈-diff
    가드(T-0326)보다 앞서 이 안내로 차단해 *denylist 가 원인*임을 정확히 알린다 — 단일 파일이 통째로
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
    호출자(main)가 차단/판정 병기에 반영할 수 있게 한다(T-0428). 이전엔 stderr 경고만 내고 목록을
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
    # 소유한다(T-0428). stderr 경고도 main 으로 이관해 제외 메시징을 한곳에서 관장한다.
    return filter_secret_hunks(raw_diff, denylist)


# ── ticket touches 파싱 ───────────────────────────────────────────────────


def parse_ticket_touches(ticket_id: str) -> list[str]:
    """board ticket frontmatter 의 touches 필드를 파싱해 경로 목록을 반환한다.

    YAML frontmatter 직접 파싱 (board.py 를 import 하지 않음). 못 찾으면 빈 목록.

    ticket 디렉토리는 `_tickets_dir()`(board_root 추종·T-0162 A6)로 *호출 시점* 해소한다 —
    board/ 분리(ADR-0033 ①) 후 wiki/ legacy 위치(stale·ticket 미발견)를 안 보게.
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


def _run_reviewer_ex(
    prompt: str,
    reviewer_cmd: str,
    timeout: int,
    run_fn: Callable[..., subprocess.CompletedProcess] | None,
) -> tuple[bool, str, bool]:
    """run_reviewer 본체 + 외부 프로세스 스폰 여부(started) 신호 (T-0457 MF-A).

    반환: (성공 여부, 출력 텍스트, started). started=False = 외부 프로세스가 *확실히 시작되지
    않음*(전송 0·과금 0) — 빈 reviewer_cmd·실행 파일 부재(FileNotFoundError). started=True =
    스폰됨(프롬프트가 전송·과금됐을 수 있음) — 정상 종료(비-0 rc 포함)·타임아웃·기타 실행 오류.
    타임아웃/기타는 시작 여부가 불확실하거나 이미 전송됐으므로 보수적으로 started=True — 라운드
    환불은 started=False 일 때만 해(반복 타임아웃으로 상한을 무한 우회하지 못하게). 확실히 전송
    전인 경우만 환불한다."""
    _run = run_fn or subprocess.run
    argv = shlex.split(reviewer_cmd)
    if not argv:
        return False, "[reviewer_cmd 가 비어 있음 — local.conf 확인]", False
    try:
        result = _run(argv, input=prompt, capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=timeout)
        output = result.stdout or ""
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return result.returncode == 0, output, True
    except subprocess.TimeoutExpired:
        # 프로세스가 시작돼 실행 중 타임아웃 — 프롬프트가 이미 전송·과금됐을 수 있다 → started=True.
        return False, (
            f"[리뷰어 타임아웃 — {timeout}초 초과] "
            "재시도: `--timeout <초>` 또는 local.conf "
            "`external_review_timeout=<초>` (양의 정수)."
        ), True
    except FileNotFoundError:
        # 실행 파일 자체가 없어 exec 실패 — 아무것도 전송되지 않았다 → started=False (환불 대상).
        return False, f"[리뷰어 명령 '{argv[0]}' 를 찾을 수 없음 — 설치 또는 PATH 확인]", False
    except Exception as exc:  # noqa: BLE001
        # 시작 여부 불확실 — 보수적으로 started=True (상한 우회 방지 > 과잉 카운트).
        return False, f"[리뷰어 실행 오류: {exc}]", True


def run_reviewer(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int = EXTERNAL_TIMEOUT_SECONDS,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> tuple[bool, str]:
    """reviewer_cmd 를 stdin(=프롬프트)으로 실행한다. 반환: (성공 여부, 출력 텍스트).

    2-튜플 공개 facade — 스폰 여부(started)까지 필요한 내부 호출은 `_run_reviewer_ex` 를 쓴다."""
    ok, output, _started = _run_reviewer_ex(prompt, reviewer_cmd, timeout, run_fn)
    return ok, output


def reviewer_name(reviewer_cmd: str) -> str:
    """reviewer_cmd 의 첫 토큰을 리뷰어 라벨로 (파일명/요약용)."""
    argv = shlex.split(reviewer_cmd)
    name = argv[0] if argv else "reviewer"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", Path(name).name) or "reviewer"


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


def save_output(reviewer: str, content: str, output_dir: Path | None = None) -> Path:
    """리뷰어 출력 원문을 파일로 저장하고 경로를 반환한다."""
    base_dir = output_dir or Path(tempfile.gettempdir())
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = base_dir / f"external_review_{reviewer}_{ts}.txt"
    dest.write_text(content, encoding="utf-8")
    return dest


# ── 실행 + 수합 ────────────────────────────────────────────────────────────


def run_review(
    prompt: str,
    reviewer_cmd: str = DEFAULT_REVIEWER_CMD,
    timeout: int = EXTERNAL_TIMEOUT_SECONDS,
    output_dir: Path | None = None,
    run_fn: Callable[..., subprocess.CompletedProcess] | None = None,
) -> dict:
    """외부 리뷰어를 실행하고 결과를 수합한다.

    반환 dict: reviewer / ok / output / verdict / file / failed / started / any_must_fix / all_pass.
    `started` (T-0457 MF-A) = 외부 프로세스가 스폰됐는가(전송·과금 가능성) — 라운드 카운트 환불
    판정에 쓴다(False = 확실히 전송 전 실패 → 예약 환불).
    """
    name = reviewer_name(reviewer_cmd)
    ok, output, started = _run_reviewer_ex(prompt, reviewer_cmd, timeout, run_fn)
    verdict = parse_verdict(output)
    out_file: Path | None = None
    if ok or output:
        out_file = save_output(name, output, output_dir)
    return {
        "reviewer": name,
        "ok": ok,
        "output": output,
        "verdict": verdict,
        "file": out_file,
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
    """종합 판정 라인에 붙일 시크릿 제외 병기 접미사 (T-0428).

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
    라인에 제외 건수·경로를 병기한다(T-0428·false-confidence 차단). 0건이면 종전과 동일 출력."""
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
        # 본문이 원문 파일에만 남으면 PM 이 못 본다(T-0428 원칙: 판정 라인은 반드시 읽힌다·T-0467).
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
        description="외부 코드리뷰 래퍼 — 외부 리뷰어 어댑터 CLI (ADR-0004, 기본 OFF)",
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
                        help="게이트 ticket 표식 (로깅 + 라운드 상한 장부 키·T-0457)")
    parser.add_argument("--ack-rounds", action="store_true",
                        help="라운드 상한 승인 재개 — 현 count 를 acked_through 로 기록 후 +limit "
                             "재개 (--gate 필수·사용자 승인 후에만·T-0457)")
    parser.add_argument("--dry-run", action="store_true",
                        help="diff·프롬프트만 출력, 외부 호출/전송 안 함 (비활성이어도 허용·빈 diff 면 exit 1)")
    parser.add_argument("--force", action="store_true",
                        help="external_review_enabled=false 여도 1회 강제 실행 (외부 전송 발생)")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="리뷰 원문 저장 디렉토리 (기본: /tmp)")
    parser.add_argument("--timeout", type=int, default=None,
                        metavar="SEC", help=f"외부 호출 타임아웃(초) (기본: {EXTERNAL_TIMEOUT_SECONDS}; "
                        "local.conf external_review_timeout 로 조정 가능)")
    parser.add_argument("--adr", nargs="+", default=None, metavar="ADR-NNNN",
                        help="관련 ADR 목록 (프롬프트에 포함)")
    return parser


def main(argv: list[str] | None = None) -> int:
    _console_spec = importlib.util.spec_from_file_location(
        "_console_encoding", Path(__file__).resolve().with_name("console_encoding.py")
    )
    _console_encoding = importlib.util.module_from_spec(_console_spec)
    _console_spec.loader.exec_module(_console_encoding)
    _console_encoding.configure_console_utf8()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.timeout is not None and args.timeout <= 0:
        parser.error("--timeout 은 양의 정수여야 합니다 (0/음수 금지).")
    conf = local_config()
    timeout = _resolve_timeout(args, conf)

    output_dir: Path | None = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

    # PM 홈 앵커 재지정 가드 (T-0367 — adopter#0 false-green 게이트 승격). REPO 앵커가 adopter#0
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
    if args.paths:
        paths = args.paths
    elif args.ticket:
        touches = parse_ticket_touches(args.ticket)
        if not touches:
            print(f"경고: ticket {args.ticket} 의 touches 미발견 — 기본 경로 사용", file=sys.stderr)
            paths = _configured_paths(conf)
        else:
            paths = touches
    else:
        paths = _configured_paths(conf)

    print(f"검토 경로: {paths}", file=sys.stderr)
    print(f"base: {args.base}", file=sys.stderr)

    # diff 추출 (시크릿 denylist 자동 제외 — 제외 경로 목록도 반환)
    denylist = _denylist_patterns(conf)
    try:
        diff, excluded = extract_diff(args.base, paths, denylist=denylist)
    except RuntimeError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    # 시크릿 denylist 제외 보고 (T-0428 — 게이트 false-confidence 차단). 제외된 경로가 판정에 전혀
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

    # 빈-diff fail-loud 가드 (diff 추출 직후·codex invoke 전·T-0326). 빈 diff(공백-only 포함)를
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

    # 활성화 게이트 (ADR-0004 — 외부 전송이므로 기본 OFF)
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

    # ── 라운드 상한 게이트: 호출 전 예약 (T-0457 — 실 외부 전송 확정 지점) ──────────
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

    reviewer_cmd = _reviewer_cmd(conf)
    print(f"외부 리뷰어 실행 중: {reviewer_cmd}", file=sys.stderr)
    result = run_review(
        prompt=prompt, reviewer_cmd=reviewer_cmd,
        timeout=timeout, output_dir=output_dir,
    )
    print_summary(result, gate=args.gate, excluded=excluded)

    # 예약 환불 (T-0457 MF-A) — 외부 프로세스가 *확실히 시작되지 않은* 경우(started=False·스폰 실패·
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
