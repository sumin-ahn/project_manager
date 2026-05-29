#!/usr/bin/env python3
"""PM 핸드오프 7단계 자동화 헬퍼 — PM 세션 종료 시 기계 측정·편집 부분을 한 명령으로 묶는다.

사용:
    venv/bin/python .project_manager/tools/pm_handoff.py \\
      --session-num <N차> \\
      --wave-summary "<wave 1~3 한 줄 요약>" \\
      [--dry-run] [--no-pytest]

동작 순서 (하나라도 실패하면 이후 단계 중단):
  1. 회귀 측정 — pytest tests/ -q. red 면 즉시 중단·핸드오프 불가.
  2. log/current.md handoff entry skeleton append — 표준 형식 (+ "다음 세션 읽기 범위" 줄).
  3. pm_state.md 세션 식별 표 sliding window 정리 — 신규 entry 추가 + 가장 오래된 entry 제거.
  4. pm_state.md 길이 검증 — wc -l 기준 700 라인 초과 시 warning.
  5. 인계 프롬프트 stdout 출력 — pm_playbook.md §"다음 PM 세션 부트스트랩 프롬프트 (템플릿)"
     의 고정부 채워 stdout. <핵심 인계 사항> 절은 PM 손.
  6. git status dump — git status -s 출력 + 변경 파일 카운트.
  7. 잔여 PM 수동 작업 출력 — checklist.

결정:
  - subprocess DI: pytest/git subprocess 는 주입 가능한 함수로 감싼다.
  - fail-soft 가 아니다 — 명시적 실패 (비-0 종료 + 명확 메시지).
  - 편집은 정규식 앵커 치환·멱등 — ticket_finish.py 와 동일.
  - LLM 미호출 — stdlib 만.
  - 인계 프롬프트는 stdout 만 — 파일 저장 안 함.
  - pm_state.md 슬라이딩 윈도우 = 3 차 (프로젝트별 조정 — SLIDING_WINDOW_SIZE).
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[2]
LOG_FILE = REPO / ".project_manager" / "wiki" / "log" / "current.md"
PM_PLAYBOOK_FILE = REPO / ".project_manager" / "wiki" / "pm_playbook.md"  # 정적 — 인계 프롬프트 템플릿 추출용
PM_STATE_FILE = REPO / ".project_manager" / "wiki" / "pm_state.md"       # 동적 — 세션 식별 sliding window 편집 대상
VENV_PYTHON = REPO / "venv" / "bin" / "python"

# ── 상수 ─────────────────────────────────────────────────────────────────────

# pm_state.md 길이 경고 임계값 (핸드오프 절차 7단계 — 세션 정리 누락 신호)
PM_STATE_LINE_WARNING_THRESHOLD = 700

# log/current.md entry 누적 경고 임계값 — 초과 시 pm_log.py archive 권장 (차단 아님).
LOG_ARCHIVE_SUGGEST_THRESHOLD = 40

# log entry 시작 줄 ("## [YYYY-MM-DD] ...") — 누적 카운트용 (pm_log.split_entries 와 동일 형식).
_LOG_ENTRY_RE = re.compile(r"^## \[\d{4}-\d{2}-\d{2}\]", re.MULTILINE)

# 슬라이딩 윈도우 크기 — 최근 N 차 만 short inline 유지. 프로젝트별 조정 가능.
SLIDING_WINDOW_SIZE = 3

# ── log/current.md handoff entry skeleton ────────────────────────────────────────────

HANDOFF_LOG_SKELETON_TEMPLATE = """\
## [{date}] handoff | PM {session_num}차 → 다음 PM 세션

- 다음 세션 읽기 범위: <PM 손 — 이 entry부터 / 또는 인용할 과거 entry·ADR 명시. 라인수 아님>
- <PM 손 — wave summary·다음 우선순위·주의 incident>
"""


def build_handoff_log_skeleton(
    session_num: int | str,
    date: str | None = None,
) -> str:
    """log/current.md 에 append 할 handoff entry skeleton 을 반환한다."""
    if date is None:
        date = datetime.date.today().isoformat()
    return HANDOFF_LOG_SKELETON_TEMPLATE.format(
        date=date,
        session_num=session_num,
    )


# ── pm_role.md sliding window 편집 ───────────────────────────────────────────

# 세션 식별 절 앵커: pm_state.md 의 "## 세션 식별 (현재까지 사용된 이름)" 로 시작하는 섹션
_SESSION_SECTION_ANCHOR = "## 세션 식별 (현재까지 사용된 이름)"

# pm 세션 entry 줄: "  - **N차** (..." 형식
# 각 줄은 반드시 두 칸 들여쓰기 + "- **N차**" 로 시작한다.
_PM_SESSION_ANCHOR_RE = re.compile(
    r"^  - \*\*(\d+차)\*\*[^\n]*$",
    re.MULTILINE,
)

# "이전 차" 포인터 줄
_PREV_SESSIONS_POINTER = "  - 이전 차"


def _extract_session_section(pm_role_text: str) -> tuple[str, int, int] | None:
    """pm_role.md 에서 세션 식별 절 텍스트와 그 시작·끝 위치를 반환한다.

    반환: (section_text, start_offset, end_offset) 또는 None (앵커 불일치).
    end_offset 는 다음 ## 또는 ### 헤더 직전 위치 (혹은 파일 끝).
    """
    anchor_idx = pm_role_text.find(_SESSION_SECTION_ANCHOR)
    if anchor_idx == -1:
        return None

    # 앵커 이후에서 다음 헤더(## 또는 ###)를 찾는다
    after_anchor = pm_role_text[anchor_idx + len(_SESSION_SECTION_ANCHOR):]
    next_header = re.search(r"^###? ", after_anchor, re.MULTILINE)
    if next_header is None:
        end_offset = len(pm_role_text)
    else:
        end_offset = anchor_idx + len(_SESSION_SECTION_ANCHOR) + next_header.start()

    section_text = pm_role_text[anchor_idx:end_offset]
    return section_text, anchor_idx, end_offset


def _find_pm_session_entries(section_text: str) -> list[re.Match]:
    """세션 식별 절에서 개별 pm 세션 entry 줄 (- **N차** ...) 의 match 목록을 반환한다.

    차수 순으로 정렬해 반환한다.
    """
    matches = list(_PM_SESSION_ANCHOR_RE.finditer(section_text))
    # 차수를 숫자로 변환해 정렬
    def _session_num(m: re.Match) -> int:
        return int(m.group(1).replace("차", ""))
    return sorted(matches, key=_session_num)


def _build_new_session_entry(
    session_num: int | str,
    date_str: str,
    wave_summary: str,
) -> str:
    """새 pm 세션 entry 줄을 빌드한다 (줄바꿈 포함).

    형식: "  - **N차** (YYYY-MM-DD · <wave_summary>): <wave_summary>."
    """
    return (
        f"  - **{session_num}차** ({date_str} · {wave_summary}): {wave_summary}.\n"
    )


def update_session_window(
    pm_role_text: str,
    session_num: int | str,
    date_str: str,
    wave_summary: str,
) -> str:
    """pm_state.md 의 세션 식별 절에 sliding window 를 적용한 새 텍스트를 반환한다.

    (인자명 pm_role_text 는 역사적 — 이제 pm_state.md 텍스트를 받는다.)

    - 신규 세션 entry 추가
    - 가장 오래된 세션 entry 제거 (3 차 sliding window)
    - "이전 차 (PM N차~M차)" 포인터 줄 갱신

    앵커 불일치 시 ValueError (추측 편집 금지).
    """
    result = _extract_session_section(pm_role_text)
    if result is None:
        raise ValueError(
            f"앵커 불일치: '{_SESSION_SECTION_ANCHOR}' 가 pm_state.md 에서 발견되지 않았다."
        )
    section_text, start_offset, end_offset = result

    existing_entries = _find_pm_session_entries(section_text)

    if len(existing_entries) == 0:
        raise ValueError(
            "앵커 불일치: 세션 식별 절에 기존 pm 세션 entry (- **N차** ...) 가 없다."
        )

    # 멱등성 검사 — 이미 해당 session_num entry 가 존재하면 no-op 으로 early-return.
    # 동일 session_num 재실행 시 entry 중복 추가 + 오래된 entry 의 이중 제거를 방지.
    target_num = int(str(session_num).replace("차", ""))
    existing_nums = [int(m.group(1).replace("차", "")) for m in existing_entries]
    if target_num in existing_nums:
        return pm_role_text

    # 가장 오래된 entry (최소 차수) 를 제거 대상으로 선정
    oldest_entry = existing_entries[0]
    oldest_num = int(oldest_entry.group(1).replace("차", ""))

    # "이전 차 (PM N차~M차)" 포인터 줄 탐색
    prev_pointer_match = re.search(
        r"^  - 이전 차 \(PM (.+?)\) = `.+?`[^\n]*$",
        section_text,
        re.MULTILINE,
    )

    new_section = section_text

    # 1. 가장 오래된 entry 줄 제거
    # 줄 전체를 제거 (줄바꿈 포함)
    oldest_line_start = oldest_entry.start()
    oldest_line_end = oldest_entry.end()
    # 줄바꿈까지 포함
    if oldest_line_end < len(new_section) and new_section[oldest_line_end] == "\n":
        oldest_line_end += 1
    new_section = new_section[:oldest_line_start] + new_section[oldest_line_end:]

    # 2. 이전 차 포인터 줄 갱신 — 제거한 차수를 포함하도록 범위 확장
    if prev_pointer_match is not None:
        old_range_str = prev_pointer_match.group(1)  # 예: "11차~24차"
        # 기존 범위에서 끝 차수 파싱
        range_match = re.match(r"(\d+차)~(\d+차)", old_range_str)
        if range_match:
            old_end_str = range_match.group(2)  # 예: "24차"
            # 새 포인터: 범위는 그대로, 끝은 제거된 오래된 entry 차수로
            new_end_str = f"{oldest_num}차"
            new_range_str = f"{range_match.group(1)}~{new_end_str}"
        else:
            # 단순 케이스: 범위가 하나의 숫자인 경우
            new_range_str = f"{old_range_str}·{oldest_num}차"

        # 포인터 줄 치환 (이전 범위 → 새 범위)
        # 재탐색 필요 (section 이 변경됐으므로)
        new_pointer_match = re.search(
            r"^  - 이전 차 \(PM .+?\) = `.+?`[^\n]*$",
            new_section,
            re.MULTILINE,
        )
        if new_pointer_match is not None:
            old_pointer_line = new_pointer_match.group(0)
            # 괄호 전체 `(PM N차~M차)` 를 매치해 치환 — 괄호 없이 "PM .+? = " 를 쓰면
            # 닫힘 괄호까지 삼켜 `) ` 가 사라지는 버그가 생긴다.
            new_pointer_line = re.sub(
                r"\(PM .+?\)", f"(PM {new_range_str})", old_pointer_line
            )
            new_section = (
                new_section[:new_pointer_match.start()]
                + new_pointer_line
                + new_section[new_pointer_match.end():]
            )
    else:
        # 포인터 줄이 없는 경우 — 제거된 entry 가 있는 자리에 포인터 추가
        # "이전 차 (PM N차~N차) = log/current.md handoff entry 단일 진실." 형식으로 추가
        pointer_line = (
            f"  - 이전 차 (PM {oldest_num}차~{oldest_num}차) = "
            f"`log/current.md` handoff entry 단일 진실.\n"
        )
        # 섹션의 기존 entry 목록 마지막 위치 뒤에 추가
        new_section += pointer_line

    # 3. 신규 세션 entry 추가 — 기존 entry 목록의 마지막 줄 이후에 삽입
    new_entry_line = _build_new_session_entry(session_num, date_str, wave_summary)

    # 현재 entry 목록 마지막 위치 찾기 (재탐색)
    updated_entries = list(_PM_SESSION_ANCHOR_RE.finditer(new_section))
    if updated_entries:
        last_entry = max(updated_entries, key=lambda m: int(m.group(1).replace("차", "")))
        insert_pos = last_entry.end()
        # 줄바꿈 뒤에 삽입
        if insert_pos < len(new_section) and new_section[insert_pos] == "\n":
            insert_pos += 1
        new_section = new_section[:insert_pos] + new_entry_line + new_section[insert_pos:]
    else:
        # 기존 entry 가 모두 제거된 경우 (새 entry 만 있는 경우) — 포인터 줄 앞에 추가
        pointer_search = re.search(
            r"^  - 이전 차 ", new_section, re.MULTILINE
        )
        if pointer_search:
            new_section = (
                new_section[:pointer_search.start()]
                + new_entry_line
                + new_section[pointer_search.start():]
            )
        else:
            new_section += new_entry_line

    # 섹션을 pm_role.md 에 교체
    new_pm_role_text = (
        pm_role_text[:start_offset]
        + new_section
        + pm_role_text[end_offset:]
    )
    return new_pm_role_text


# ── pm_role.md 인계 프롬프트 추출 ────────────────────────────────────────────

_HANDOFF_PROMPT_SECTION_ANCHOR = "## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)"

# 코드블록 추출
_CODE_BLOCK_RE = re.compile(r"```\n(.+?)```", re.DOTALL)


def extract_handoff_prompt_template(pm_role_text: str) -> str | None:
    """pm_role.md 에서 인계 프롬프트 템플릿 코드블록을 추출한다.

    반환: 코드블록 내용 문자열 또는 None (앵커 불일치).
    """
    anchor_idx = pm_role_text.find(_HANDOFF_PROMPT_SECTION_ANCHOR)
    if anchor_idx == -1:
        return None

    # 섹션 이후에서 다음 ## 헤더 전까지
    after_anchor = pm_role_text[anchor_idx + len(_HANDOFF_PROMPT_SECTION_ANCHOR):]
    next_header = re.search(r"^## ", after_anchor, re.MULTILINE)
    if next_header:
        section_text = after_anchor[:next_header.start()]
    else:
        section_text = after_anchor

    match = _CODE_BLOCK_RE.search(section_text)
    if match is None:
        return None
    return match.group(1)


def build_handoff_prompt_output(
    pm_role_text: str,
    session_num: int | str,
    wave_summary: str,
    date_str: str,
) -> str:
    """인계 프롬프트 stdout 출력 문자열을 빌드한다.

    pm_playbook.md 의 고정부를 그대로 포함하고 <핵심 인계 사항> 절은 PM 손임을 명시.
    (인자명 pm_role_text 는 역사적 — 이제 pm_playbook.md 텍스트를 받는다.)
    """
    template = extract_handoff_prompt_template(pm_role_text)
    if template is None:
        return (
            "[경고] pm_playbook.md 에서 인계 프롬프트 템플릿을 찾지 못했다. "
            f"앵커: '{_HANDOFF_PROMPT_SECTION_ANCHOR}'\n"
            "pm_playbook.md §'다음 PM 세션 부트스트랩 프롬프트 (템플릿)' 을 직접 복사하라."
        )

    header = (
        f"=== 인계 프롬프트 (PM {session_num}차 → 다음 PM 세션) ===\n"
        f"--- 아래를 복사해 다음 PM 세션에 붙여넣기 ---\n"
        f"(⚠️  '<핵심 인계 사항>' 절은 PM 손 — 직접 채워 넣을 것)\n\n"
    )
    footer = (
        f"\n--- 복사 끝 ---\n"
        f"[참고] 날짜: {date_str}, wave summary: {wave_summary}\n"
    )
    return header + "```\n" + template + "```" + footer


# ── pytest 출력 파서 ─────────────────────────────────────────────────────────

def is_pytest_green(output: str, returncode: int = 0) -> bool:
    """pytest -q 출력이 green (passed 존재, failed 없음) 이면 True."""
    if returncode != 0:
        return False
    if re.search(r"\d+ failed", output):
        return False
    if re.search(r"\d+ passed", output):
        return True
    return False


def parse_pytest_summary(output: str) -> str:
    """pytest -q 출력에서 요약 라인을 추출한다. 없으면 빈 문자열."""
    match = re.search(r"(\d+ passed.*)", output)
    if match:
        return match.group(1).strip()
    return output.strip()[-200:] if output.strip() else ""


# ── PmHandoff 핵심 클래스 ─────────────────────────────────────────────────────

class PmHandoff:
    """PM 핸드오프 7단계 자동화 핵심 로직.

    subprocess 함수를 DI 해 테스트에서 실제 실행 없이 결정론적으로 검증한다.
    ticket_finish.py 의 TicketFinisher DI 패턴과 동일.
    """

    def __init__(
        self,
        *,
        run_pytest_fn: Callable[[], tuple[int, str]] | None = None,
        run_git_fn: Callable[[list[str]], tuple[int, str]] | None = None,
        log_file: Path = LOG_FILE,
        pm_playbook_file: Path = PM_PLAYBOOK_FILE,
        pm_state_file: Path = PM_STATE_FILE,
        venv_python: Path = VENV_PYTHON,
    ) -> None:
        self._log_file = log_file
        self._pm_playbook_file = pm_playbook_file
        self._pm_state_file = pm_state_file
        self._venv_python = venv_python

        self._run_pytest_fn = run_pytest_fn or self._default_run_pytest
        self._run_git_fn = run_git_fn or self._default_run_git

    # ── 기본 subprocess 구현 (실제 실행) ──────────────────────────────────────

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

    # ── 메인 흐름 ─────────────────────────────────────────────────────────────

    def run(
        self,
        session_num: int | str,
        wave_summary: str,
        dry_run: bool,
        skip_pytest: bool,
    ) -> int:
        """PM 핸드오프 7단계 자동화 전체 흐름을 실행한다.

        반환: 0=성공, 1=실패 (중단).
        """
        date_str = datetime.date.today().isoformat()
        print(
            f"[pm_handoff] PM {session_num}차 핸드오프 시작 "
            f"(dry_run={dry_run}, skip_pytest={skip_pytest})"
        )

        # ── 1. 회귀 측정 ───────────────────────────────────────────────────────
        print("\n[1/7] 회귀 측정...")
        if skip_pytest:
            print("  [--no-pytest] 회귀 측정 skip.")
            pytest_summary = "(skip)"
        else:
            if dry_run:
                print("  [dry-run] pytest tests/ -q 실행 중 (파일 편집만 생략)...")
            returncode, output = self._run_pytest_fn()
            print(output.rstrip())
            if not is_pytest_green(output, returncode):
                print(
                    "\n[중단] 회귀 red — 핸드오프 불가. log/current.md·pm_role.md 어떤 것도 건드리지 않는다.",
                    file=sys.stderr,
                )
                return 1
            pytest_summary = parse_pytest_summary(output)
            print(f"  ✓ green: {pytest_summary}")

        # ── 2. log/current.md handoff entry skeleton append ────────────────────────────
        print("\n[2/7] log/current.md handoff entry skeleton append...")
        skeleton = build_handoff_log_skeleton(session_num=session_num, date=date_str)

        if dry_run:
            print("  [dry-run] log/current.md 에 append 할 skeleton:")
            print("  " + skeleton.replace("\n", "\n  "))
        else:
            log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
            self._log_file.write_text(log_text + "\n" + skeleton, encoding="utf-8")
            print(f"  ✓ log/current.md handoff entry skeleton append (PM {session_num}차)")

        # log/current.md entry 누적 점검 — 임계 초과 시 archive 권장 (차단 아님).
        cur_log_text = self._log_file.read_text(encoding="utf-8") if self._log_file.exists() else ""
        entry_count = len(_LOG_ENTRY_RE.findall(cur_log_text))
        if entry_count > LOG_ARCHIVE_SUGGEST_THRESHOLD:
            print(
                f"  ⚠ log/current.md entry {entry_count}개 > {LOG_ARCHIVE_SUGGEST_THRESHOLD} — "
                f"`pm_log.py archive --before <날짜>` 로 오래된 entry 봉인 권장 "
                f"(부트스트랩 읽기 비용 ↓).",
                file=sys.stdout,
            )

        # ── 3. pm_state.md sliding window 정리 ─────────────────────────────────
        print("\n[3/7] pm_state.md 세션 식별 sliding window 정리...")
        state_text = self._pm_state_file.read_text(encoding="utf-8")

        try:
            new_state_text = update_session_window(
                pm_role_text=state_text,
                session_num=session_num,
                date_str=date_str,
                wave_summary=wave_summary,
            )
        except ValueError as exc:
            print(f"\n[중단] {exc}", file=sys.stderr)
            return 1

        if dry_run:
            # diff 미리보기: 변경된 줄 출력
            old_lines = state_text.splitlines()
            new_lines = new_state_text.splitlines()
            added = [l for l in new_lines if l not in set(old_lines)]
            removed = [l for l in old_lines if l not in set(new_lines)]
            print("  [dry-run] pm_state.md 세션 식별 절 변경 예고:")
            for line in removed[:5]:
                print(f"  - {line}")
            for line in added[:5]:
                print(f"  + {line}")
        else:
            self._pm_state_file.write_text(new_state_text, encoding="utf-8")
            print(f"  ✓ pm_state.md 세션 식별 sliding window 정리 완료 (PM {session_num}차 추가·최고령 entry 제거)")

        # ── 4. pm_state.md 길이 검증 ────────────────────────────────────────────
        print("\n[4/7] pm_state.md 길이 검증...")
        text_to_check = new_state_text if not dry_run else state_text
        line_count = len(text_to_check.splitlines())
        if line_count > PM_STATE_LINE_WARNING_THRESHOLD:
            print(
                f"  ⚠ pm_state.md 길이 {line_count} 라인 > {PM_STATE_LINE_WARNING_THRESHOLD} 라인 임계값.",
                file=sys.stdout,
            )
            print(
                f"  ⚠ 과거 세션 정리 누락 신호 — §세션 식별 sliding window 정리를 점검하라.",
                file=sys.stdout,
            )
        else:
            print(f"  ✓ pm_state.md {line_count} 라인 (임계값 {PM_STATE_LINE_WARNING_THRESHOLD} 이하).")

        # ── 5. 인계 프롬프트 stdout 출력 ───────────────────────────────────────
        # 템플릿(정적)은 pm_playbook.md 에서 추출한다 — sliding window 편집 대상(pm_state.md)과 분리.
        print("\n[5/7] 인계 프롬프트 출력...")
        playbook_text = self._pm_playbook_file.read_text(encoding="utf-8")
        prompt_output = build_handoff_prompt_output(
            pm_role_text=playbook_text,
            session_num=session_num,
            wave_summary=wave_summary,
            date_str=date_str,
        )
        print(prompt_output)

        # ── 6. git status dump ─────────────────────────────────────────────────
        print("\n[6/7] git status dump...")
        git_rc, git_out = self._run_git_fn(["status", "-s"])
        if git_rc != 0:
            print(f"  ⚠ git status 실패 (rc={git_rc}): {git_out.rstrip()}")
        else:
            changed_files = [l for l in git_out.splitlines() if l.strip()]
            print(f"  변경 파일 수: {len(changed_files)}")
            if changed_files:
                print("  git status -s 출력:")
                for line in changed_files:
                    print(f"    {line}")
            else:
                print("  (변경 없음)")

        # ── 7. 잔여 PM 수동 작업 출력 ──────────────────────────────────────────
        print("\n[7/7] PM 이 손으로 할 잔여 작업:")
        print("  [ ] log/current.md handoff entry 본문 채우기 (<PM 손> 자리를 실제 내용으로)")
        print("  [ ] pm_role.md '진행 중인 의사결정' 표 갱신")
        print("  [ ] pm_role.md '남은 작업 전체 그림' / '권장 액션 template' 갱신")
        print("  [ ] 인계 프롬프트의 '<핵심 인계 사항>' 채우기 (위 [5/7] 출력 참조)")
        print("  [ ] git commit (Co-Authored-By: Claude 트레일러 포함)")

        if dry_run:
            print("\n[dry-run] 완료 — 실제 파일 편집은 실행하지 않았다.")
        else:
            print(f"\n[완료] PM {session_num}차 핸드오프 자동화 완료.")

        return 0


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pm_handoff.py",
        description="PM 핸드오프 7단계 자동화 헬퍼.",
    )
    parser.add_argument(
        "--session-num",
        required=True,
        metavar="N",
        help="떠나는 PM 세션 차수 (예: 28).",
    )
    parser.add_argument(
        "--wave-summary",
        required=True,
        metavar="요약",
        help="떠나는 PM 세션의 wave 종합 1~2 줄 요약 (사람 작성).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="파일 편집 없이 변경 미리보기.",
    )
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="회귀 측정 skip (기본 측정).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handoff = PmHandoff()
    return handoff.run(
        session_num=args.session_num,
        wave_summary=args.wave_summary,
        dry_run=args.dry_run,
        skip_pytest=args.no_pytest,
    )


if __name__ == "__main__":
    sys.exit(main())
