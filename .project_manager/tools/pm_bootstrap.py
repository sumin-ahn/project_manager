#!/usr/bin/env python3
"""PM 세션 시작 부트스트랩 헬퍼 — 기계 측정 부분을 한 명령으로 dump 한다.

사용:
    venv/bin/python .project_manager/tools/pm_bootstrap.py [--json] [--with-pytest]

동작:
  board list → 상태별 카운트 + open ticket 목록 (claim 가능).
  board lint → clean | N warnings.
  pytest tests/ -q → 회귀 A / B 통과 (--with-pytest opt-in — default skip).
  git log / git status → 브랜치·최근 commit·working tree 상태.
  log.md 마지막 entry → date / type / title.

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
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
LOG_FILE = REPO / ".project_manager" / "wiki" / "log.md"
BOARD_PY = REPO / ".project_manager" / "tools" / "board.py"
VENV_PYTHON = REPO / "venv" / "bin" / "python"

# 프로젝트 timezone — 부트스트랩 타임스탬프 표기용. 필요 시 교체.
KST = ZoneInfo("Asia/Seoul")


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
    """
    if "no lint issues" in lint_output:
        return "clean"
    # 경고 라인 수를 세어 반환
    warning_lines = [
        line for line in lint_output.splitlines()
        if line.strip() and not line.startswith("✓")
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


# ── log.md 파서 ──────────────────────────────────────────────────────────

def parse_log_last_entry(log_text: str) -> dict[str, str] | None:
    """log.md 에서 마지막 ## 항목의 date/type/title 을 파싱한다.

    log.md 포맷: `## [YYYY-MM-DD] type | title`

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
        venv_python: Path = VENV_PYTHON,
    ) -> None:
        self._log_file = log_file
        self._board_py = board_py
        self._venv_python = venv_python

        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git

    # ── 기본 subprocess 구현 ─────────────────────────────────────────────

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_run_pytest(self) -> tuple[int, str]:
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        return result.returncode, result.stdout + result.stderr

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
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

        lint_rc, lint_output = self._run_board_fn(["lint"])
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
        """git 브랜치·최근 3 commit·working tree 상태를 수집한다. 실패 시 sys.exit(1)."""
        branch_rc, branch_out = self._run_git_fn(["rev-parse", "--abbrev-ref", "HEAD"])
        if branch_rc != 0:
            print(f"[중단] git rev-parse 실패 (rc={branch_rc}):\n{branch_out}", file=sys.stderr)
            sys.exit(1)
        branch = parse_git_branch(branch_out)

        log_rc, log_out = self._run_git_fn(["log", "--oneline", "-5"])
        if log_rc != 0:
            print(f"[중단] git log 실패 (rc={log_rc}):\n{log_out}", file=sys.stderr)
            sys.exit(1)
        commits = parse_git_log(log_out)

        status_rc, status_out = self._run_git_fn(["status", "--short"])
        if status_rc != 0:
            print(f"[중단] git status 실패 (rc={status_rc}):\n{status_out}", file=sys.stderr)
            sys.exit(1)
        working_tree = parse_git_status(status_out)

        return {
            "branch": branch,
            "commits": commits,
            "working_tree": working_tree,
        }

    def _collect_log_entry(self) -> dict | None:
        """log.md 의 마지막 entry 를 파싱해 반환한다."""
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
        if git["commits"]:
            head_sha, head_subject = git["commits"][0]
            lines.append(f"- commit: {head_sha} {head_subject}")
            lines.append("- 마지막 3 commit:")
            for sha, subject in git["commits"][:3]:
                lines.append(f"  - {sha} {subject}")
        lines.append(f"- working tree: {git['working_tree']}")
        lines.append("")

        # log.md 섹션
        lines.append("### log.md 마지막 entry")
        if log_entry:
            lines.append(f"- date: {log_entry['date']}")
            lines.append(f"- type: {log_entry['type']}")
            lines.append(f"- title: {log_entry['title']}")
        else:
            lines.append("- (log.md 없음 또는 entry 파싱 실패)")
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
            "- (직전 세션 요약은 PM 손 — pm_role.md \"세션 식별\" 절 참조)"
        )
        lines.append(
            "- 무엇부터 갈까요? (PM 손 — pm_role.md \"남은 작업 전체 그림\" 절 + open ticket"
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
    ) -> int:
        """부트스트랩 정보를 수집해 출력한다.

        with_pytest: True 면 pytest 회귀 실행, False (default) 면 skip.
                     default skip 인 이유는 모듈 docstring 참조.

        반환: 0=성공, 1=실패 (sys.exit 로 중단할 수도 있음).
        """
        now = datetime.datetime.now(tz=KST)
        timestamp = now.strftime("%Y-%m-%d %H:%M KST")

        board = self._collect_board()
        pytest_result = self._collect_pytest() if with_pytest else None
        git = self._collect_git()
        log_entry = self._collect_log_entry()

        if output_json:
            data = self._build_json(board, pytest_result, git, log_entry, timestamp)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            markdown = self._build_markdown(board, pytest_result, git, log_entry, timestamp)
            print(markdown)

        return 0


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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    bootstrap = PmBootstrap()
    return bootstrap.run(
        output_json=args.output_json,
        with_pytest=args.with_pytest,
    )


if __name__ == "__main__":
    sys.exit(main())
