#!/usr/bin/env python3
"""PM 부기 자동화 헬퍼 — ticket 완료 시 기계적 부기를 한 명령으로 묶는다.

사용:
    venv/bin/python tools/ticket_finish.py T-NNNN [--section "<섹션명>"] [--dry-run]

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 실행 — pytest tests/ -q. red 면 즉시 중단.
  2. status.md 스칼라 갱신 — 전체 테스트 수 / 합계 행 / 섹션 행(--section 시) / 회귀 실측 라인.
  3. log.md 스켈레톤 append — 표준 형식 entry 골격.
  4. board.py complete 호출 — 회귀를 이미 통과했으므로 --tests-pass.
  5. git add -A — 스테이징. commit 은 PM 이 한다.
  6. 잔여 PM 수동 작업 출력.

결정 (T-0064):
  - subprocess DI: pytest/git/board.py subprocess 는 주입 가능한 함수로 감싼다.
  - red 면 중단: status.md / log.md / board / git 어떤 것도 건드리지 않는다.
  - 편집은 정규식 앵커 치환, 멱등. 앵커 불일치 시 명시적 에러 (추측 편집 금지).
  - 모듈 행·서술·commit 은 자동화하지 않는다 (v1 축소판 — §배경).
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확한 메시지).
  - LLM 미호출 — stdlib + board.py import 만.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
STATUS_FILE = REPO / "project_wiki" / "status.md"
LOG_FILE = REPO / "project_wiki" / "log.md"
BOARD_PY = REPO / "tools" / "board.py"
VENV_PYTHON = REPO / "venv" / "bin" / "python"

# ── 앵커 정규식 ────────────────────────────────────────────────────────
# 각 패턴은 status.md 의 실제 라인 형식에 정확히 맞춰야 한다.

# "**전체 테스트: N / N 통과** (통합 NN개는…)"
_RE_HEADER = re.compile(
    r"(\*\*전체 테스트: )(\d+)( / )(\d+)( 통과\*\* \(통합 )(\d+)(개는)"
)

# "| **합계** | **N** |"
_RE_TOTAL_ROW = re.compile(
    r"(\| \*\*합계\*\* \| \*\*)(\d+)(\*\* \|)"
)

# "회귀 실측 `pytest tests/ -q` = **N / N** 와 일치."
_RE_REGRESSION = re.compile(
    r"(회귀 실측 `pytest tests/ -q` = \*\*)(\d+)( / )(\d+)(\*\* 와 일치\.)"
)

# 섹션 행: "| <섹션명> | N |" — 섹션명은 동적으로 빌드한다.
def _build_section_re(section: str) -> re.Pattern[str]:
    """합계표의 섹션 행 정규식을 빌드한다."""
    escaped = re.escape(section)
    return re.compile(
        r"(\| " + escaped + r" \| )(\d+)( \|)"
    )


# ── pytest 출력 파서 ────────────────────────────────────────────────────

def parse_pytest_output(output: str) -> tuple[int, int] | None:
    """pytest -q 출력에서 (passed, deselected) 를 파싱한다.

    반환: (passed, deselected) — 파싱 실패 시 None.

    pytest -q 요약 라인 형식 예:
      "1472 passed, 24 deselected in 12.34s"
      "1472 passed in 12.34s"
      "5 failed, 1467 passed, 24 deselected in 10.00s"

    red (failed > 0) 여부 판단은 호출 측이 한다 (failed 수 포함 파싱은 하지 않음).
    반환값 (passed, deselected) 만 추출한다.
    """
    passed_match = re.search(r"(\d+) passed", output)
    deselected_match = re.search(r"(\d+) deselected", output)

    if passed_match is None:
        return None

    passed = int(passed_match.group(1))
    deselected = int(deselected_match.group(1)) if deselected_match else 0
    return passed, deselected


def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True.

    returncode 도 함께 검사한다 — returncode != 0 이면(인터럽트·부분 출력 등)
    명확한 'N passed' 가 있어도 green 으로 오판하지 않는다.
    """
    if returncode != 0:
        return False
    if re.search(r"\d+ failed", output):
        return False
    if re.search(r"\d+ passed", output):
        return True
    return False


# ── status.md 편집 ─────────────────────────────────────────────────────

def read_status(status_file: Path) -> str:
    return status_file.read_text(encoding="utf-8")


def write_status(status_file: Path, content: str) -> None:
    status_file.write_text(content, encoding="utf-8")


def _replace_once(
    text: str,
    pattern: re.Pattern[str],
    replacement_fn: Callable[[re.Match[str]], str],
    anchor_description: str,
) -> str:
    """pattern 이 정확히 1번 매치되면 치환, 아니면 ValueError.

    anchor_description — 에러 메시지에 쓸 앵커 설명 (사람이 읽을 수 있게).
    멱등: 치환 결과가 이미 들어있으면 동일한 치환 결과를 반환한다 (패턴이 여전히 매치되므로).
    """
    matches = list(pattern.finditer(text))
    if len(matches) == 0:
        raise ValueError(
            f"앵커 불일치: '{anchor_description}' 패턴이 status.md 에서 발견되지 않았다. "
            "status.md 형식을 확인하라 (추측 편집 금지 — 이 라인을 수동으로 갱신하라)."
        )
    if len(matches) > 1:
        raise ValueError(
            f"앵커 불일치: '{anchor_description}' 패턴이 status.md 에서 {len(matches)}번 매치됐다 "
            "(정확히 1번이어야 한다). status.md 형식을 확인하라."
        )
    return pattern.sub(replacement_fn, text, count=1)


def update_status(
    status_text: str,
    new_total: int,
    old_total: int,
    deselected: int,
    section: str | None,
) -> str:
    """status.md 텍스트에서 스칼라 값을 갱신한 새 텍스트를 반환한다.

    갱신 대상:
      - 헤더 라인 ("전체 테스트: N / N 통과" + 통합 NN개)
      - 합계 행 ("| **합계** | **N** |")
      - 회귀 실측 라인
      - --section 이 지정된 경우 그 섹션 행 (델타 = new_total - old_total)

    앵커 불일치 시 ValueError (추측 편집 금지).
    """
    delta = new_total - old_total

    # 1. 헤더 라인
    def replace_header(m: re.Match[str]) -> str:
        # group(2)=passed, group(4)=total, group(6)=deselected
        return (
            m.group(1) + str(new_total)
            + m.group(3) + str(new_total)
            + m.group(5) + str(deselected) + m.group(7)
        )

    status_text = _replace_once(
        status_text, _RE_HEADER, replace_header, "전체 테스트: N / N 통과"
    )

    # 2. 합계 행
    def replace_total_row(m: re.Match[str]) -> str:
        return m.group(1) + str(new_total) + m.group(3)

    status_text = _replace_once(
        status_text, _RE_TOTAL_ROW, replace_total_row, "| **합계** | **N** |"
    )

    # 3. 회귀 실측 라인
    def replace_regression(m: re.Match[str]) -> str:
        return (
            m.group(1) + str(new_total)
            + m.group(3) + str(new_total)
            + m.group(5)
        )

    status_text = _replace_once(
        status_text, _RE_REGRESSION, replace_regression,
        "회귀 실측 `pytest tests/ -q` = **N / N** 와 일치."
    )

    # 4. 섹션 행 (--section 지정 시)
    if section is not None:
        section_re = _build_section_re(section)
        matches = list(section_re.finditer(status_text))
        if len(matches) == 0:
            raise ValueError(
                f"앵커 불일치: 섹션 행 '| {section} | N |' 이 합계표에서 발견되지 않았다. "
                f"--section 인자가 합계표의 섹션명과 정확히 일치해야 한다."
            )
        if len(matches) > 1:
            raise ValueError(
                f"앵커 불일치: 섹션 행 '| {section} |' 이 {len(matches)}번 매치됐다 "
                f"(정확히 1번이어야 한다)."
            )
        old_section_count = int(matches[0].group(2))
        new_section_count = old_section_count + delta

        def replace_section(m: re.Match[str]) -> str:
            return m.group(1) + str(new_section_count) + m.group(3)

        status_text = section_re.sub(replace_section, status_text, count=1)

    return status_text


# ── board.py 연동 ───────────────────────────────────────────────────────

def count_board_done(board_py: Path) -> int:
    """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

    board.py 를 직접 import 해 find_ticket / STATUS_DIRS 를 활용한다.
    실패 시 -1 반환.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper", board_py)
    if spec is None:
        return -1
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        done_dir = mod.TICKETS_DIR / "done"
        return len(list(done_dir.glob("T-*.md")))
    except Exception:
        return -1


def get_ticket_title(board_py: Path, ticket_id: str) -> str:
    """ticket_id 의 title 을 board.py 를 import 해서 읽어온다.

    실패 시 빈 문자열 반환.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("_board_helper2", board_py)
    if spec is None:
        return ""
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        _status, path = mod.find_ticket(ticket_id)
        fm, _body = mod.load_ticket(path)
        return fm.get("title") or ""
    except Exception:
        return ""


# ── 로그 스켈레톤 ───────────────────────────────────────────────────────

LOG_SKELETON_TEMPLATE = """\
## [{date}] {entry_type} | {ticket_id} — {title}

- <!-- PM: 무엇을·왜 서술 -->
- 테스트: +{delta}. 회귀 {old_total}→{new_total} / {new_total}.
- board: done {board_before}→{board_after}.
"""


def build_log_skeleton(
    ticket_id: str,
    title: str,
    old_total: int,
    new_total: int,
    board_before: int,
    board_after: int,
    entry_type: str = "<!-- feat/fix/verify/… -->",
    date: str | None = None,
) -> str:
    if date is None:
        date = datetime.date.today().isoformat()
    delta = new_total - old_total
    return LOG_SKELETON_TEMPLATE.format(
        date=date,
        entry_type=entry_type,
        ticket_id=ticket_id,
        title=title,
        delta=delta,
        old_total=old_total,
        new_total=new_total,
        board_before=board_before,
        board_after=board_after,
    )


# ── 핵심 흐름 ──────────────────────────────────────────────────────────

class TicketFinisher:
    """PM 부기 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    broker/dispatch.py 의 clock_fn/sleep_fn DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_board_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        board_count_fn: Callable[[], int] | None = None,
        ticket_title_fn: Callable[[str], str] | None = None,
        status_file: Path = STATUS_FILE,
        log_file: Path = LOG_FILE,
        board_py: Path = BOARD_PY,
        venv_python: Path = VENV_PYTHON,
    ) -> None:
        self._status_file = status_file
        self._log_file = log_file
        self._board_py = board_py
        self._venv_python = venv_python

        # subprocess DI — 기본값은 실제 subprocess 호출
        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_board_fn = run_board_fn or self._default_run_board
        self._run_git_fn = run_git_fn or self._default_run_git

        # board 조회 DI — 기본값은 실 board.py import 구현
        self._board_count_fn = board_count_fn or self._default_board_count
        self._ticket_title_fn = ticket_title_fn or self._default_ticket_title

    # ── 기본 subprocess 구현 (실제 실행) ─────────────────────────────

    def _default_run_pytest(self) -> tuple[int, str]:
        """pytest tests/ -q 를 실행해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            [str(self._venv_python), "-m", "pytest", "tests/", "-q"],
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_run_board(self, args: list[str]) -> tuple[int, str]:
        """board.py 를 subprocess 로 호출해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            [str(self._venv_python), str(self._board_py)] + args,
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_run_git(self, args: list[str]) -> tuple[int, str]:
        """git 명령을 실행해 (returncode, stdout+stderr) 반환."""
        result = subprocess.run(
            ["git"] + args,
            capture_output=True,
            text=True,
            cwd=str(REPO),
        )
        output = result.stdout + result.stderr
        return result.returncode, output

    def _default_board_count(self) -> int:
        """board.md 의 done 티켓 수를 반환한다 (board.py 를 import 해서).

        실패 시 -1 반환.
        """
        return count_board_done(self._board_py)

    def _default_ticket_title(self, ticket_id: str) -> str:
        """ticket_id 의 title 을 board.py 를 import 해서 읽어온다.

        실패 시 빈 문자열 반환.
        """
        return get_ticket_title(self._board_py, ticket_id)

    # ── 메인 흐름 ────────────────────────────────────────────────────

    def run(
        self,
        ticket_id: str,
        section: str | None,
        dry_run: bool,
    ) -> int:
        """ticket_id 완료 부기 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).
        """
        print(f"[ticket_finish] {ticket_id} 완료 부기 시작 (dry_run={dry_run})")

        # ── 1. 회귀 실행 ──────────────────────────────────────────────
        # dry-run 도 pytest 를 실제 실행한다 — "부작용 없음"이지 "빠름"이 아니다.
        # 파일·board·git 편집만 생략하므로 pytest 실행은 항상 수행.
        print("\n[1/6] 회귀 실행 중...")
        if dry_run:
            print("  [dry-run] pytest tests/ -q 실행 중 (파일·board·git 편집만 생략)...")
        returncode, output = self._run_pytest_fn()
        print(output.rstrip())

        if not is_pytest_green(output, returncode):
            print(
                "\n[중단] 회귀 red — status.md·log.md·board·git 어떤 것도 건드리지 않는다.",
                file=sys.stderr,
            )
            print(
                "원인: pytest 가 실패를 보고했거나 출력 파싱 실패.",
                file=sys.stderr,
            )
            return 1

        parsed = parse_pytest_output(output)
        if parsed is None:
            print(
                "\n[중단] pytest 출력 파싱 실패 — passed 수를 읽지 못했다.",
                file=sys.stderr,
            )
            return 1

        new_total, deselected = parsed
        old_total = self._read_current_totals()[0]
        print(
            f"\n  ✓ green: passed={new_total}, deselected={deselected}, "
            f"delta={new_total - old_total:+d}"
        )

        # ── 2. status.md 스칼라 갱신 ─────────────────────────────────
        print("\n[2/6] status.md 스칼라 갱신...")
        status_text = read_status(self._status_file)

        if dry_run:
            self._preview_status_changes(
                status_text, new_total, old_total, deselected, section
            )
        else:
            edited_before: list[str] = []  # 부분 편집 추적용
            try:
                new_text = update_status(
                    status_text, new_total, old_total, deselected, section
                )
                edited_before.append("status.md")
                write_status(self._status_file, new_text)
                print(f"  ✓ status.md 갱신: {old_total}→{new_total}")
                if section:
                    print(f"  ✓ 섹션 행 '{section}' 갱신.")
                else:
                    print(
                        "  ⚠ --section 미지정 — 섹션 행은 PM 이 수동으로 갱신해야 한다 "
                        "(합계와 섹션 합이 불일치할 수 있음)."
                    )
            except ValueError as exc:
                print(f"\n[중단] {exc}", file=sys.stderr)
                if edited_before:
                    print(
                        f"  부분 편집 완료된 파일: {', '.join(edited_before)}",
                        file=sys.stderr,
                    )
                return 1

        # ── 3. log.md 스켈레톤 append ────────────────────────────────
        print("\n[3/6] log.md 스켈레톤 append...")
        board_before = self._board_count_fn()
        board_after = board_before + 1  # board complete 후 +1

        title = self._ticket_title_fn(ticket_id)
        if not title:
            title = f"<{ticket_id} 제목을 읽지 못했습니다>"

        skeleton = build_log_skeleton(
            ticket_id=ticket_id,
            title=title,
            old_total=old_total,
            new_total=new_total,
            board_before=board_before,
            board_after=board_after,
        )

        if dry_run:
            print("  [dry-run] log.md 에 append 할 스켈레톤:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
            self._log_file.write_text(log_text + "\n" + skeleton, encoding="utf-8")
            print(f"  ✓ log.md 스켈레톤 append ({ticket_id})")

        # ── 4. board.py complete ──────────────────────────────────────
        print("\n[4/6] board.py complete...")
        if dry_run:
            print(f"  [dry-run] board.py complete {ticket_id} --tests-pass")
        else:
            board_rc, board_out = self._run_board_fn(
                ["complete", ticket_id, "--tests-pass"]
            )
            print(f"  {board_out.rstrip()}")
            if board_rc != 0:
                print(
                    f"\n[중단] board.py complete 실패 (rc={board_rc}). "
                    "status.md·log.md 는 이미 편집됐다.",
                    file=sys.stderr,
                )
                return 1
            print(f"  ✓ board: {ticket_id} → done")

        # ── 5. git add -A ─────────────────────────────────────────────
        print("\n[5/6] git stage (git add -A)...")
        if dry_run:
            print("  [dry-run] git add -A (실제 실행 생략)")
        else:
            git_rc, git_out = self._run_git_fn(["add", "-A"])
            if git_rc != 0:
                print(
                    f"\n[중단] git add -A 실패 (rc={git_rc}): {git_out.rstrip()}",
                    file=sys.stderr,
                )
                return 1
            print("  ✓ git add -A 완료 (commit 은 아직 안 했다)")

        # ── 6. 잔여 PM 작업 출력 ─────────────────────────────────────
        print("\n[6/6] PM 이 손으로 할 잔여 작업:")
        print("  ① log.md 서술 불릿 채우기 — <!-- PM: 무엇을·왜 서술 --> 를 실제 내용으로 교체")
        print("  ② status.md 모듈 행(테스트 수 + 비고) — 해당 모듈 행을 PM 이 직접 갱신")
        print("  ③ git commit — 메시지는 PM 이 작성 (Co-Authored-By: Claude 트레일러 포함)")
        if not section:
            print("  ④ status.md 섹션 행 — --section 을 지정하지 않았으므로 섹션 행도 PM 이 갱신")

        if dry_run:
            print("\n[dry-run] 완료 — 실제 편집·board·git 는 실행하지 않았다.")
        else:
            print(f"\n[완료] {ticket_id} 부기 완료.")

        return 0

    # ── 내부 헬퍼 ────────────────────────────────────────────────────

    def _read_current_totals(self) -> tuple[int, int]:
        """현재 status.md 에서 (passed, deselected) 를 읽는다 (old_total).

        읽기 자체 실패 시 (0, 0) 으로 폴백한다 — 이후 update_status() 가
        앵커 불일치를 명시적 ValueError 로 낸다 (§결정 5).
        """
        try:
            text = read_status(self._status_file)
            header_match = _RE_HEADER.search(text)
            if header_match:
                passed = int(header_match.group(2))
                deselected = int(header_match.group(6))
                return passed, deselected
        except Exception:
            pass
        return 0, 0

    def _preview_status_changes(
        self,
        status_text: str,
        new_total: int,
        old_total: int,
        deselected: int,
        section: str | None,
    ) -> None:
        """dry-run 시 변경될 내용을 출력한다."""
        delta = new_total - old_total
        print(f"  [dry-run] 헤더 라인: 전체 테스트 {old_total}→{new_total}")
        print(f"  [dry-run] 합계 행: **합계** | **{old_total}** → **{new_total}**")
        print(f"  [dry-run] 회귀 실측 라인: {old_total} / {old_total} → {new_total} / {new_total}")
        if section:
            section_re = _build_section_re(section)
            m = section_re.search(status_text)
            if m:
                old_sec = int(m.group(2))
                print(
                    f"  [dry-run] 섹션 행 '{section}': {old_sec}→{old_sec + delta}"
                )
            else:
                print(
                    f"  [dry-run] 섹션 행 '{section}': 패턴 불일치 — "
                    "실제 실행 시 에러가 발생한다."
                )
        else:
            print("  [dry-run] --section 미지정 — 섹션 행은 건드리지 않는다.")


# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_finish.py",
        description="ticket 완료 시 PM 부기 자동화 헬퍼 (v1 축소판).",
    )
    parser.add_argument("ticket_id", metavar="T-NNNN", help="완료할 ticket ID")
    parser.add_argument(
        "--section",
        metavar="섹션명",
        default=None,
        help=(
            "status.md 합계표의 섹션명 (예: '개발 도구 (board.py)'). "
            "지정하면 해당 섹션 행도 갱신한다."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="편집·board·git 없이 무엇을 바꿀지만 출력한다.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    finisher = TicketFinisher()
    return finisher.run(
        ticket_id=args.ticket_id,
        section=args.section,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
