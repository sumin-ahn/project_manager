#!/usr/bin/env python3
"""PM 세션 시작 부트스트랩 헬퍼 — 기계 측정 부분을 한 명령으로 dump 한다.

사용:
    venv/bin/python .project_manager/tools/pm_bootstrap.py [--json] [--with-pytest]

동작:
  board list → 상태별 카운트 + open ticket 목록 (claim 가능).
  board lint → clean | N warnings.
  pytest tests/ -q → 회귀 A / B 통과 (--with-pytest opt-in — default skip).
  git log / git status → 브랜치·최근 commit·working tree 상태.
  log/current.md 마지막 entry → date / type / title.

회귀 측정 default skip:
  직전 handoff entry 가 회귀 숫자를 기록한다면 부트스트랩 단계 pytest 재측정은
  중복 안전망에 가깝다. default skip 으로 부트스트랩 ~5초. baseline 의심 시
  --with-pytest 명시. 프로젝트가 별도 QA skill 을 두지 않는다면 default 를
  True 로 바꿔도 된다.

출력:
  기본: markdown (PM 의 첫 turn 보고에 그대로 붙여넣기 가능).
  --json: JSON (slash command skill wrapper 소비용).

결정:
  - fail-soft 가 아니다 — git/board/pytest subprocess 실패 시 즉시 중단 (비-0 종료).
  - subprocess DI — ticket_finish.py 와 동일 패턴 (테스트 결정론).
  - LLM 호출 없음 — stdlib + board.py import 만.
  - 첫 turn 권장 액션의 기계 부분만 자동화.
    직전 세션 요약·"무엇부터 갈까요" 옵션 제시는 PM 손.
  - 타임스탬프 = datetime.datetime.now(tz=ZoneInfo("Asia/Seoul")) (KST).
    프로젝트 timezone 이 다르면 KST 상수만 교체.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
TOOLS_DIR = REPO / ".project_manager" / "tools"
AREAS_FILE = REPO / ".project_manager" / "areas.md"   # per-repo 레지스트리 (등록영역 surface·ADR-0014)


# ── worktree_pool import seam (multi-PM 모드·ADR-0013) ───────────────────────────
# multi-PM 인자(--repo)를 받았을 때만 alloc 경로에 진입한다. 솔로 무인자 경로는 이
# 모듈을 전혀 쓰지 않으므로 import 실패가 무해(fail-soft) — 단 --repo 를 줬는데
# worktree_pool 이 없으면 **명시 에러**(침묵 무력화 금지·ADR-0013).
def _load_worktree_pool():
    """worktree_pool 모듈을 동적 로드한다. 부재/로드 실패 시 None (fail-soft).

    REPO/tools 경로 기준 `spec_from_file_location` — board.py·pm_*.py 와 같은
    스크립트-위치 앵커 규약. 솔로(multi-PM 미사용)에선 호출 안 되거나 None 이어도
    무인자 경로가 이 모듈을 안 쓰므로 무해. --repo 경로만 None 을 명시 에러로 처리.
    """
    import importlib.util

    wp_path = TOOLS_DIR / "worktree_pool.py"
    if not wp_path.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("worktree_pool", wp_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 로드 실패는 솔로 경로를 깨지 않는다.
        return None


def _load_board():
    """board 모듈을 동적 로드한다 — 보호 브랜치 surface(`_repo_protected`)용 (T-0076·fail-soft).

    `_load_worktree_pool` 과 동형 — `spec_from_file_location`(스크립트-위치 앵커). board 를
    *직접 import* 하지 않는 이유(touches 격리·병렬충돌 회피)는 동적 로드로 보존된다. 보호
    브랜치 경고는 *소프트*(추가 인지)라 board 부재/로드 실패는 None(경고 생략·정체성 선언
    자체는 깨지 않음). --repo 경로(multi-PM lean identity)에서만 호출된다.
    """
    import importlib.util

    if not BOARD_PY.exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("board", BOARD_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:  # noqa: BLE001 — fail-soft: 보호 경고는 소프트(로드 실패=경고 생략).
        return None


def _registered_repos(areas_file: Path = AREAS_FILE) -> list[str]:
    """areas.md 레지스트리에서 등록된 repo 이름 목록 (identity surface '등록영역' 표면용).

    board.py 를 import 하지 않는다(touches 격리·병렬충돌 회피) — areas.md 의 `repo`/`prefix`
    칼럼을 stdlib 로 가볍게 읽는다. 파일 부재/스키마 불일치 → 빈 목록(fail-soft·솔로 무해).
    """
    if not areas_file.exists():
        return []
    rows: list[str] = []
    header: list[str] | None = None
    sep_pattern = re.compile(r"^\|[\s:|-]+\|?$")
    for line in areas_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or sep_pattern.match(stripped):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if not any(cells):
            continue
        row = dict(zip(header, cells))
        name = row.get("repo") or row.get("prefix")
        if name:
            rows.append(name)
    return rows


def _default_python() -> str:
    """플랫폼-인지 venv 인터프리터 경로 (없으면 sys.executable 폴백).

    Windows 는 venv/Scripts/python.exe, POSIX 는 venv/bin/python. venv 가 없으면
    현재 인터프리터로 폴백한다. 이 머신은 시스템 python3 에 pytest 가 없고 venv 에만
    있으므로, venv 가 있으면 무조건 venv 를 우선해 회귀 측정 인터프리터를 보존한다.
    """
    cand = REPO / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return str(cand) if cand.exists() else sys.executable

# 프로젝트 timezone — 부트스트랩 타임스탬프 표기용. 필요 시 교체.
# zoneinfo 부재(3.8-) 또는 시스템 tz DB·tzdata 부재(Windows) 시 고정 오프셋 폴백.
# 한국은 1988 이후 서머타임 없음 → 고정 UTC+9 가 항상 정확 (타임스탬프 의미 동일).
try:
    from zoneinfo import ZoneInfo

    KST = ZoneInfo("Asia/Seoul")
except Exception:  # ImportError(zoneinfo 부재) + ZoneInfoNotFoundError(tzdata 부재) 모두 포섭
    KST = datetime.timezone(datetime.timedelta(hours=9))


# ── board 카운트 파서 ────────────────────────────────────────────────────

def parse_board_counts(board_output: str) -> dict[str, int]:
    """board list 출력에서 status 별 카운트를 파싱한다.

    board list 출력 행 형식:
      "  [open   ] T-NNNN  title...  claimed_by  tags"
    status 필드는 7자 width 로 패딩된다.

    반환: {"done": N, "open": M, "claimed": K, "blocked": L}
    """
    counts: dict[str, int] = {"done": 0, "open": 0, "claimed": 0, "blocked": 0}
    line_pattern = re.compile(r"^\s+\[(\w+)\s*\]")
    for line in board_output.splitlines():
        match = line_pattern.match(line)
        if match:
            status = match.group(1).strip()
            if status in counts:
                counts[status] += 1
    return counts


def parse_open_tickets(board_output: str) -> list[str]:
    """board list 출력에서 open status 의 ticket ID 목록을 반환한다.

    claim 가능한 open ticket 만 추출한다 (claimed/blocked/done 제외).
    """
    tickets: list[str] = []
    line_pattern = re.compile(r"^\s+\[open\s*\]\s+(T-\d+)")
    for line in board_output.splitlines():
        match = line_pattern.match(line)
        if match:
            tickets.append(match.group(1))
    return tickets


def parse_lint_result(lint_output: str) -> str:
    """board lint 출력에서 결과 요약을 반환한다.

    "✓ no lint issues" 이면 "clean" 반환.
    경고가 있으면 해당 줄 수를 "N warnings" 형식으로 반환.

    `--gate` 출력은 헤더 `⚠️  N lint issue(s) (M blocking 차단):` 다음에 각 issue
    줄(`✗`/공백 마크 + `[kind] …`)이 온다. 헤더 줄은 issue 가 아니므로 카운트에서
    제외한다 — 그러지 않으면 off-by-one 으로 1 더 세어진다(T-0038).
    """
    if "no lint issues" in lint_output:
        return "clean"
    # issue 라인 수를 세어 반환 — 요약 헤더("lint issue(s)" 줄)·clean 마크(✓)는 제외.
    warning_lines = [
        line for line in lint_output.splitlines()
        if line.strip()
        and not line.startswith("✓")
        and "lint issue(s)" not in line
    ]
    count = len(warning_lines)
    if count == 0:
        return "clean"
    return f"{count} warnings"


# ── pytest 파서 ──────────────────────────────────────────────────────────

def parse_pytest_counts(pytest_output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, total) 을 파싱한다.

    반환: (passed, total) — total = passed + failed.
    파싱 실패 시 None.
    """
    passed_match = re.search(r"(\d+) passed", pytest_output)
    if passed_match is None:
        return None
    passed = int(passed_match.group(1))
    failed_match = re.search(r"(\d+) failed", pytest_output)
    failed = int(failed_match.group(1)) if failed_match else 0
    total = passed + failed
    return passed, total


# ── git 파서 ─────────────────────────────────────────────────────────────

def parse_git_log(log_output: str) -> list[tuple[str, str]]:
    """git log --oneline 출력에서 (sha, subject) 목록을 반환한다.

    반환: [(sha, subject), ...]
    """
    commits: list[tuple[str, str]] = []
    for line in log_output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
        else:
            commits.append((parts[0], ""))
    return commits


def parse_git_branch(branch_output: str) -> str:
    """git rev-parse --abbrev-ref HEAD 출력에서 브랜치명을 반환한다."""
    return branch_output.strip()


def parse_git_status(status_output: str) -> str:
    """git status --short 출력에서 working tree 상태를 반환한다.

    변경 없으면 "clean", 변경 있으면 "N files modified" 형식 반환.
    """
    lines = [line for line in status_output.splitlines() if line.strip()]
    if not lines:
        return "clean"
    return f"{len(lines)} files modified"


# ── log/current.md 파서 ──────────────────────────────────────────────────────────

def parse_log_last_entry(log_text: str) -> dict[str, str] | None:
    """log/current.md 에서 마지막 ## 항목의 date/type/title 을 파싱한다.

    log/current.md 포맷: `## [YYYY-MM-DD] type | title`

    반환: {"date": "...", "type": "...", "title": "..."} 또는 None.
    """
    pattern = re.compile(
        r"^## \[(\d{4}-\d{2}-\d{2})\]\s+(\S+)\s+\|\s+(.+)$",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(log_text))
    if not matches:
        return None
    last = matches[-1]
    return {
        "date": last.group(1),
        "type": last.group(2),
        "title": last.group(3).strip(),
    }


# ── 핵심 흐름 ──────────────────────────────────────────────────────────────

class PmBootstrap:
    """PM 세션 부트스트랩 기계 측정 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    ticket_finish.py 의 TicketFinisher DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        log_file: Path = LOG_FILE,
        board_py: Path = BOARD_PY,
        areas_file: Path = AREAS_FILE,
        venv_python: str | Path = _default_python(),
        worktree_pool=None,
        board=None,
    ) -> None:
        self._log_file = log_file
        self._board_py = board_py
        self._areas_file = areas_file
        self._venv_python = venv_python
        # worktree_pool seam — 테스트는 mock 모듈을 주입(hermetic). None 이면 --repo
        # 경로 진입 시에만 동적 로드(multi-PM 모드)·솔로 무인자 경로는 안 건드린다.
        self._worktree_pool = worktree_pool
        # board seam (T-0076) — 보호 브랜치 surface(`_repo_protected`)용. 테스트는 mock
        # 모듈 주입. None 이면 lean identity(--repo --slot) 경로에서만 동적 로드(소프트
        # 경고·board 부재면 경고 생략). board.py *직접 import* 는 안 함(touches 격리).
        self._board = board

        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git

    # ── 기본 subprocess 구현 ─────────────────────────────────────────────

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        # encoding 명시 — board.py 의 한글/이모지 출력을 부모가 cp949 로 디코딩해
        # 크래시하지 않도록 utf-8 고정 (Windows CP949 콘솔 회피).
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_run_pytest(self) -> tuple[int, str]:
        # encoding 명시 — pytest 의 한글 테스트명 출력을 부모가 cp949 로 디코딩해
        # 크래시하지 않도록 utf-8 고정 (Windows CP949 회피).
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        # encoding 명시 — git 의 한글 커밋 메시지/상태 출력을 부모가 cp949 로
        # 디코딩해 크래시하지 않도록 utf-8 고정 (Windows CP949 회피).
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    # ── 데이터 수집 ──────────────────────────────────────────────────────

    def _collect_board(self) -> dict:
        """board list + lint 결과를 수집한다. 실패 시 sys.exit(1)."""
        rc, output = self._run_board_fn(["list"])
        if rc != 0:
            print(f"[중단] board.py list 실패 (rc={rc}):\n{output}", file=sys.stderr)
            sys.exit(1)

        counts = parse_board_counts(output)
        open_tickets = parse_open_tickets(output)

        # `--gate` 로 호출 — 차단 카테고리에만 rc=1, advisory(status drift·
        # unstable-ref-advice)는 rc=0 (board.cmd_lint). advisory-only lint 는
        # 부트스트랩을 막지 않고, 진짜 차단(dangling-wikilink·dep 오류·placeholder
        # /thin)만 abort 한다 — push 게이트와 동일한 차단 기준 재사용(T-0038).
        lint_rc, lint_output = self._run_board_fn(["lint", "--gate"])
        if lint_rc != 0:
            print(f"[중단] board.py lint 실패 (rc={lint_rc}):\n{lint_output}", file=sys.stderr)
            sys.exit(1)
        lint_result = parse_lint_result(lint_output)

        return {
            "counts": counts,
            "open_tickets": open_tickets,
            "lint": lint_result,
        }

    def _collect_pytest(self) -> dict | None:
        """pytest 회귀를 실행하고 결과를 반환한다.

        default 는 skip — 호출자가 with_pytest=True 일 때만 호출한다.
        실패 시 sys.exit(1).
        """
        rc, output = self._run_pytest_fn()
        parsed = parse_pytest_counts(output)
        if parsed is None:
            print(
                f"[중단] pytest 출력 파싱 실패 (rc={rc}):\n{output}",
                file=sys.stderr,
            )
            sys.exit(1)
        passed, total = parsed
        return {"passed": passed, "total": total, "output": output}

    def _collect_git(self) -> dict:
        """git 브랜치·최근 3 commit·working tree 상태를 수집한다.

        빈 repo(커밋 0 — fresh clone `pm_import --new` 직후)는 **정상 케이스**로
        degrade 한다: branch 는 `symbolic-ref` 폴백, commits 는 빈 목록 +
        `no_commits=True`. **진짜 git repo 가 아닐 때만** (symbolic-ref·status 전부
        실패) sys.exit 로 중단한다. 빈-repo 판정은 commits==[] 기준 (로캘 독립 —
        git 메시지 텍스트를 파싱하지 않는다).
        """
        # ── branch: rev-parse 실패 시 symbolic-ref 폴백 (빈 repo서 동작 실측) ──
        branch_rc, branch_out = self._run_git_fn(["rev-parse", "--abbrev-ref", "HEAD"])
        if branch_rc == 0:
            branch = parse_git_branch(branch_out)
        else:
            # 빈 repo(커밋 0)서 rev-parse rc≠0 — symbolic-ref 로 HEAD 가 가리키는
            # 브랜치명을 얻는다 (커밋 없이도 rc 0). 둘 다 실패해야 git repo 아님.
            sym_rc, sym_out = self._run_git_fn(["symbolic-ref", "--short", "HEAD"])
            if sym_rc != 0:
                print(
                    f"[중단] git repo 아님 — rev-parse(rc={branch_rc})·symbolic-ref(rc={sym_rc}) 모두 실패:\n{sym_out}",
                    file=sys.stderr,
                )
                sys.exit(1)
            branch = parse_git_branch(sym_out)

        # ── commits: log 실패(빈 repo "아직 커밋 없음" 포함)는 빈 목록으로 degrade ──
        log_rc, log_out = self._run_git_fn(["log", "--oneline", "-5"])
        # rc≠0 면 빈 repo로 보고 commits=[] — 별도 메시지 파싱 불요(로캘 독립).
        commits = parse_git_log(log_out) if log_rc == 0 else []
        no_commits = commits == []

        # ── status: 빈 repo서도 rc 0 동작 — 실패는 git repo 부재 신호 ──
        status_rc, status_out = self._run_git_fn(["status", "--short"])
        if status_rc != 0:
            print(
                f"[중단] git repo 아님 — git status 실패 (rc={status_rc}):\n{status_out}",
                file=sys.stderr,
            )
            sys.exit(1)
        working_tree = parse_git_status(status_out)

        return {
            "branch": branch,
            "commits": commits,
            "no_commits": no_commits,
            "working_tree": working_tree,
        }

    def _collect_log_entry(self) -> dict | None:
        """log/current.md 의 마지막 entry 를 파싱해 반환한다."""
        if not self._log_file.exists():
            return None
        log_text = self._log_file.read_text(encoding="utf-8")
        return parse_log_last_entry(log_text)

    # ── 출력 빌드 ────────────────────────────────────────────────────────

    def _build_markdown(
        self,
        board: dict,
        pytest_result: dict | None,
        git: dict,
        log_entry: dict | None,
        timestamp: str,
    ) -> str:
        counts = board["counts"]
        open_tickets = board["open_tickets"]
        lint = board["lint"]

        lines: list[str] = []
        lines.append(f"## PM 세션 부트스트랩 ({timestamp})")
        lines.append("")

        # Board 섹션
        lines.append("### Board")
        lines.append(
            f"- done: {counts['done']} / open: {counts['open']} / "
            f"claimed: {counts['claimed']} / blocked: {counts['blocked']}"
        )
        if pytest_result is not None:
            lines.append(
                f"- 회귀: {pytest_result['passed']} / {pytest_result['total']} 통과"
            )
        else:
            lines.append("- 회귀: (skip — handoff entry 참조 · --with-pytest 로 재측정)")
        lines.append(f"- lint: {lint}")
        if open_tickets:
            lines.append(f"- open ticket 목록 (claim 가능): {', '.join(open_tickets)}")
        else:
            lines.append("- open ticket 목록 (claim 가능): (없음)")
        lines.append("")

        # Git 섹션
        lines.append("### Git")
        lines.append(f"- branch: {git['branch']}")
        if git.get("no_commits"):
            lines.append("- commit: (초기 커밋 없음 — fresh clone)")
        elif git["commits"]:
            head_sha, head_subject = git["commits"][0]
            lines.append(f"- commit: {head_sha} {head_subject}")
            lines.append("- 마지막 3 commit:")
            for sha, subject in git["commits"][:3]:
                lines.append(f"  - {sha} {subject}")
        lines.append(f"- working tree: {git['working_tree']}")
        lines.append("")

        # log/current.md 섹션
        lines.append("### log/current.md 마지막 entry")
        if log_entry:
            lines.append(f"- date: {log_entry['date']}")
            lines.append(f"- type: {log_entry['type']}")
            lines.append(f"- title: {log_entry['title']}")
        else:
            lines.append("- (log/current.md 없음 또는 entry 파싱 실패)")
        lines.append("")

        # 권장 첫 turn 섹션
        lines.append("### 권장 첫 turn")
        lines.append("PM 세션 시작합니다.")
        board_summary = (
            f"done {counts['done']} / open {counts['open']} / "
            f"claimed {counts['claimed']} / blocked {counts['blocked']}."
        )
        if pytest_result is not None:
            regression_summary = (
                f" 회귀 {pytest_result['passed']} / {pytest_result['total']}, lint {lint}."
            )
        else:
            regression_summary = f" 회귀 (handoff entry 참조), lint {lint}."
        lines.append(f"- board: {board_summary}{regression_summary}")
        lines.append(
            "- (직전 세션 요약은 PM 손 — pm_state.md \"세션 식별\" 절 + log/current.md 마지막 handoff entry 참조)"
        )
        lines.append(
            "- 무엇부터 갈까요? (PM 손 — pm_state.md \"남은 작업 전체 그림\" 절 + open ticket"
        )
        lines.append("  목록 보고 옵션 제시)")

        return "\n".join(lines)

    def _build_json(
        self,
        board: dict,
        pytest_result: dict | None,
        git: dict,
        log_entry: dict | None,
        timestamp: str,
    ) -> dict:
        counts = board["counts"]
        return {
            "timestamp": timestamp,
            "board": {
                "done": counts["done"],
                "open": counts["open"],
                "claimed": counts["claimed"],
                "blocked": counts["blocked"],
                "open_tickets": board["open_tickets"],
                "lint": board["lint"],
            },
            "pytest": (
                {
                    "passed": pytest_result["passed"],
                    "total": pytest_result["total"],
                }
                if pytest_result is not None
                else None
            ),
            "git": {
                "branch": git["branch"],
                "commits": [
                    {"sha": sha, "subject": subject}
                    for sha, subject in git["commits"]
                ],
                "no_commits": git.get("no_commits", False),
                "working_tree": git["working_tree"],
            },
            "log_last_entry": log_entry,
        }

    # ── 메인 흐름 ────────────────────────────────────────────────────────

    def run(
        self,
        *,
        output_json: bool = False,
        with_pytest: bool = False,
        repo: str | None = None,
        branch: str | None = None,
        resume: str | None = None,
        slot: int | None = None,
    ) -> int:
        """부트스트랩 정보를 수집해 출력한다.

        with_pytest: True 면 pytest 회귀 실행, False (default) 면 skip.
                     default skip 인 이유는 모듈 docstring 참조.
        repo:        multi-PM 모드(ADR-0013) — 주면 worktree 슬롯 alloc/bind + identity surface
                     를 *추가* 출력한다. 무인자(솔로)면 None — 현행 동작 100% 보존
                     (alloc/bind 경로 미진입).
        slot:        multi-PM lean 모드(T-0074) — `--repo` 와 함께 주면 `alloc` 대신
                     `bind_slot("work/<repo>_<N>", repo, "<repo>_<N>")` 로 **직접 바인딩**
                     하고, 다른 활성 PM 현황(상태점검)도 surface 한다. None 이면 기존
                     `--repo` alloc 경로(현행 보존).

        반환: 0=성공, 1=실패 (sys.exit 로 중단할 수도 있음).
        """
        now = datetime.datetime.now(tz=KST)
        timestamp = now.strftime("%Y-%m-%d %H:%M KST")

        board = self._collect_board()
        pytest_result = self._collect_pytest() if with_pytest else None
        git = self._collect_git()
        log_entry = self._collect_log_entry()

        # multi-PM 모드 분기: --slot(lean·직접 bind·T-0074) vs 기존 --repo alloc.
        umbrella_lean = repo is not None and slot is not None
        umbrella_alloc = repo is not None and slot is None

        if output_json:
            data = self._build_json(board, pytest_result, git, log_entry, timestamp)
            if umbrella_lean:
                data["worktree"] = self._bind_and_identity(repo, slot)
            elif umbrella_alloc:
                data["worktree"] = self._alloc_and_identity(repo, branch, resume)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            markdown = self._build_markdown(board, pytest_result, git, log_entry, timestamp)
            print(markdown)
            if umbrella_lean:
                # lean 정체성 선언(T-0074) — bind + identity surface + 다른 활성 PM 상태점검.
                identity = self._bind_and_identity(repo, slot)
                print()
                print(self._build_slot_identity_markdown(identity))
            elif umbrella_alloc:
                # 기존 --repo alloc + identity surface 를 markdown 뒤에 추가 출력.
                identity = self._alloc_and_identity(repo, branch, resume)
                print()
                print(self._build_identity_markdown(identity))

        return 0

    # ── multi-PM 모드: worktree 슬롯 alloc + identity surface (ADR-0013·0011) ────

    def _resolve_worktree_pool(self):
        """worktree_pool 모듈을 해소한다 — 주입분 우선·없으면 동적 로드 (multi-PM 모드 전용).

        --repo 경로에서만 호출된다. 주입(테스트 mock)이 있으면 그걸, 없으면 동적
        로드한다. 둘 다 None 이면 **명시 에러**(SystemExit) — multi-PM 인자를 줬는데
        worktree_pool 이 없으면 침묵 무력화 금지(ADR-0013).
        """
        wp = self._worktree_pool or _load_worktree_pool()
        if wp is None:
            print(
                "[중단] --repo multi-PM 모드인데 worktree_pool 엔진을 찾을 수 없다 "
                f"({TOOLS_DIR / 'worktree_pool.py'} 부재 또는 로드 실패). "
                "multi-PM 셋업(pm-config) 또는 엔진 전파를 확인하라.",
                file=sys.stderr,
            )
            sys.exit(1)
        return wp

    def _alloc_and_identity(
        self, repo: str, branch: str | None, resume: str | None
    ) -> dict:
        """worktree 슬롯을 alloc 하고 identity surface 데이터를 반환한다 (multi-PM 모드).

        - `worktree_pool.alloc(repo, branch=, resume=)` 호출 → Lease.
        - `NeedsCreate` (풀 소진) → 사용자 게이트 안내 후 sys.exit(1). **자동
          `git worktree add` 안 함**(ADR-0013 — fs 행위는 사용자 게이트).
        - 성공 시 cwd=슬롯 경로·branch·등록영역을 dict 로 반환(markdown/JSON 빌더가 소비).
          branch 는 `worktree_pool.current_branch(slot)` live 조회(ADR-0013 amend T-0072 —
          git=진실·장부 저장 폐지). detached/조회불가는 None → surface 가 "(미지정)".
        """
        wp = self._resolve_worktree_pool()
        try:
            lease = wp.alloc(repo, branch=branch, resume=resume)
        except wp.NeedsCreate as exc:
            print(
                f"\n[사용자 게이트] repo {exc.repo!r} worktree 풀 소진 — idle 슬롯이 없다.\n"
                f"  자동 `git worktree add` 는 하지 않는다(ADR-0013 — fs 행위·사용자 게이트).\n"
                f"  새 슬롯이 필요하면 수동으로 추가하라:\n"
                f"    pm-config worktree add {exc.repo}"
                f"{f' --branch {branch}' if branch else ''}\n"
                f"  (또는 진행 중인 다른 슬롯을 작업완료 후 release.)",
                file=sys.stderr,
            )
            sys.exit(1)

        slot_path = wp.slot_path(lease.slot)
        # 브랜치는 슬롯 worktree 의 git HEAD 에서 live 조회(ADR-0013 amend T-0072 —
        # git=진실·장부 저장 폐지). detached/조회불가는 None → identity surface 가 "(미지정)".
        return {
            "repo": repo,
            "slot": lease.slot,
            "slot_path": str(slot_path),
            "branch": wp.current_branch(lease.slot),
            "registered_repos": _registered_repos(self._areas_file),
        }

    def _bind_and_identity(self, repo: str, slot: int) -> dict:
        """슬롯을 직접 bind 하고 lean identity + 상태점검 데이터를 반환한다 (multi-PM lean·T-0074).

        - 세션 = `f"{repo}_{slot}"`·슬롯 식별자 = `f"work/{repo}_{slot}"`.
        - `worktree_pool.bind_slot(slot_id, repo, session)` 호출 → Lease. **pool alloc 아님**
          (직접 바인딩·`NeedsCreate` 게이트 없음·`reclaim_stale` 안 거침·ADR-0013).
        - branch 는 `worktree_pool.current_branch(slot_id)` live 조회(git=진실·ADR-0013 amend
          T-0072). detached/조회불가/슬롯 폴더 부재는 None → surface 가 "(미지정)".
        - **상태점검**: `list_leases()` 에서 *이 세션 제외* 다른 활성(leased) 리스를 모아
          각 줄 `세션 · 슬롯 · 브랜치(live)` 로 반환한다(다른 활성 PM 현황 surface).
        """
        wp = self._resolve_worktree_pool()
        session = f"{repo}_{slot}"
        slot_id = f"work/{repo}_{slot}"
        lease = wp.bind_slot(slot_id, repo, session)

        slot_path = wp.slot_path(lease.slot)
        # 다른 활성 PM 현황 — 이 세션 제외 leased 리스(상태점검 surface).
        others: list[dict] = []
        for other in wp.list_leases():
            if other.state != "leased" or other.session == session:
                continue
            others.append({
                "session": other.session,
                "slot": other.slot,
                "branch": wp.current_branch(other.slot),
            })

        live_branch = wp.current_branch(lease.slot)
        return {
            "repo": repo,
            "session": session,
            "slot": lease.slot,
            "slot_path": str(slot_path),
            "branch": live_branch,
            "others": others,
            # 보호 브랜치 경고 (T-0076·소프트) — 라이브 브랜치가 그 repo 보호목록에 있으면
            # 🚫 경고를 surface 한다. 미보호/조회불가/board 부재면 None(경고 생략).
            "protected_branch": self._protected_warning(repo, live_branch),
        }

    def _protected_warning(self, repo: str, branch: str | None) -> str | None:
        """라이브 브랜치가 그 repo 보호목록(`board._repo_protected`)이면 그 브랜치명 (T-0076·소프트).

        보호목록이 아니거나 브랜치 조회불가(detached)·board 부재/헬퍼 부재면 None(경고 생략).
        board 직접 import 금지(touches 격리) — 주입/동적로드된 board 의 `_repo_protected` 를
        getattr 로 쓴다(DI 보존). 파싱 실패는 fail-soft None(소프트 경고는 깨지지 않는다).
        """
        if not branch:
            return None
        board_mod = self._board or _load_board()
        repo_protected = getattr(board_mod, "_repo_protected", None) if board_mod else None
        if repo_protected is None:
            return None
        try:
            protected = repo_protected(repo)
        except Exception:  # noqa: BLE001 — fail-soft: 파싱 실패는 경고 생략(소프트).
            return None
        return branch if branch in protected else None

    def _build_slot_identity_markdown(self, identity: dict) -> str:
        """lean identity + 상태점검 markdown — 세션명·라이브 브랜치·`--session` 안내·다른 PM 현황.

        라이브 브랜치가 보호목록(T-0076)이면 🚫 경고 줄을 정체성 선언 직후 surface 한다
        (소프트 인지 — 하드 강제는 pre-push 훅).
        """
        repo = identity["repo"]
        session = identity["session"]
        slot = identity["slot"]
        slot_path = identity["slot_path"]
        branch = identity["branch"] or "(미지정)"
        others = identity["others"]
        protected_branch = identity.get("protected_branch")

        lines: list[str] = []
        lines.append("### multi-PM identity surface (lean·T-0074)")
        lines.append(
            f"- 당신은 **{repo} PM** · 세션=`{session}` · 슬롯=`{slot}` · "
            f"브랜치=`{branch}` · 보드=multi-PM 공유."
        )
        lines.append(
            f"- **보드/리스 조작은 `--session {session}` 을 명시**한다 "
            "(정체성 = 에이전트 맥락·도구엔 명시 전달)."
        )
        lines.append(f"- cwd (작업 슬롯): `{slot_path}`")
        # 보호 브랜치 경고 (T-0076·소프트) — 라이브 브랜치가 보호목록이면.
        if protected_branch:
            lines.append(
                f"- 🚫 **보호 브랜치 `{protected_branch}`** — 여기서 커밋/푸시 금지. "
                "feature 브랜치를 checkout 후 작업하고, main 갱신이 필요하면 사용자에게 맡긴다 "
                "(pre-push 훅이 하드 차단·T-0076)."
            )
        lines.append("")
        # 상태점검 — 다른 활성 PM 현황.
        lines.append("### 다른 활성 PM (상태점검)")
        if others:
            for other in others:
                other_branch = other["branch"] or "(미지정)"
                lines.append(
                    f"- `{other['session']}` · `{other['slot']}` · 브랜치=`{other_branch}`"
                )
        else:
            lines.append("- (다른 활성 PM 없음)")
        return "\n".join(lines)

    def _build_identity_markdown(self, identity: dict) -> str:
        """identity surface markdown — "당신은 <repo> PM · worktree=… · branch=… · …"."""
        repo = identity["repo"]
        slot = identity["slot"]
        slot_path = identity["slot_path"]
        branch = identity["branch"] or "(미지정)"
        registered = identity["registered_repos"]
        areas = ", ".join(registered) if registered else f"{repo} (areas.md 미등록)"

        lines: list[str] = []
        lines.append("### multi-PM identity surface (ADR-0013·0011)")
        lines.append(
            f"- 당신은 **{repo} PM** · worktree=`{slot}` · branch=`{branch}` · "
            f"보드=multi-PM 공유 · 등록영역: {areas}"
        )
        lines.append(f"- cwd (작업 슬롯): `{slot_path}`")
        lines.append(
            "- 코드 작업은 이 슬롯 cwd 에서 — 보드/wiki 는 multi-PM 공유 `.project_manager`."
        )
        return "\n".join(lines)


# ── CLI ────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_bootstrap.py",
        description="PM 세션 시작 부트스트랩 헬퍼 — 기계 측정 부분을 한 명령으로 dump 한다.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="기계 파싱용 JSON 출력 (기본: markdown).",
    )
    parser.add_argument(
        "--with-pytest",
        action="store_true",
        help="pytest 회귀 측정 opt-in (default skip — handoff entry 가 숫자를 기록한다고 가정).",
    )
    # ── multi-PM 모드 (ADR-0013·0011) — 무인자(솔로)면 미사용·현행 보존 ──
    parser.add_argument(
        "--repo",
        metavar="이름",
        default=None,
        help=(
            "multi-PM 모드 — repo 워크트리 슬롯을 alloc 하고 identity surface 를 출력한다 "
            "(ADR-0013). 무인자(솔로)면 현행 부트스트랩만 (alloc 경로 미진입)."
        ),
    )
    parser.add_argument(
        "--slot",
        metavar="N",
        type=int,
        default=None,
        help=(
            "multi-PM 모드 — 슬롯 번호를 직접 선언해 바인딩(lean·T-0074). 세션=`<repo>_<N>`·"
            "슬롯=`work/<repo>_<N>` 에 직접 bind(pool alloc 아님). --repo 전용. 주면 alloc "
            "대신 bind 경로로 간다(--branch/--resume 와 배타)."
        ),
    )
    parser.add_argument(
        "--branch",
        metavar="브랜치",
        default=None,
        help="multi-PM 모드 — alloc 할 작업스트림 브랜치 (--repo 전용·idle 슬롯 리스 후 checkout).",
    )
    parser.add_argument(
        "--resume",
        metavar="브랜치",
        default=None,
        help="multi-PM 모드 — 회전 재부착할 이전 작업스트림 브랜치 (--repo 전용·같은 슬롯 연속성).",
    )
    return parser


def _set_console_codepage_utf8() -> None:
    # Windows 한정 — 콘솔 코드페이지를 UTF-8(65001)로 맞춘다. cp949(한국어) 콘솔에서
    # stdout reconfigure(utf-8)만으로는 콘솔이 UTF-8 바이트를 cp949 로 디코드해 한글이
    # mojibake 되므로, 콘솔 입출력 codepage 자체를 65001 로 설정해 정합시킨다 (T-0068).
    # best-effort: 콘솔 핸들 없음·권한·예외 시 조용히 통과(reconfigure 와 동형 try/except).
    # idempotent — 이미 UTF-8 콘솔엔 65001 재설정이 무해. POSIX 는 진입하지 않는다.
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    # 콘솔/파이프 출력을 UTF-8 로 재설정 — cp949 콘솔이나 리다이렉트된 stdout 에서
    # 이모지·em-dash(—) print 가 UnicodeEncodeError 로 죽는 것을 막는다 (T-0017).
    # 먼저 Windows 콘솔 codepage 를 UTF-8 로 맞춘 뒤(T-0068) 스트림을 reconfigure 한다.
    # reconfigure 미지원 스트림(테스트 캡처 등)은 hasattr 가드로 건너뛴다.
    _set_console_codepage_utf8()
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    # --branch/--resume/--slot 은 --repo multi-PM 모드 전용 — repo 없이 주면 오용 신호로 거부.
    if args.repo is None and (
        args.branch is not None or args.resume is not None or args.slot is not None
    ):
        build_parser().error("--branch/--resume/--slot 은 --repo multi-PM 모드 전용이다.")
    # --slot(직접 바인딩·lean)은 --branch/--resume(alloc 경로)과 배타 — 둘은 다른 경로다.
    if args.slot is not None and (args.branch is not None or args.resume is not None):
        build_parser().error("--slot 은 --branch/--resume 과 함께 쓸 수 없다 (bind vs alloc 경로).")
    # 슬롯 번호는 1부터 — `work/<repo>_<N>` 네이밍과 정합. 0/음수는 무의미 슬롯이라 거부(codex 게이트).
    if args.slot is not None and args.slot < 1:
        build_parser().error("--slot 은 1 이상의 슬롯 번호여야 한다 (work/<repo>_<N>).")
    bootstrap = PmBootstrap()
    return bootstrap.run(
        output_json=args.output_json,
        with_pytest=args.with_pytest,
        repo=args.repo,
        branch=args.branch,
        resume=args.resume,
        slot=args.slot,
    )


if __name__ == "__main__":
    sys.exit(main())
