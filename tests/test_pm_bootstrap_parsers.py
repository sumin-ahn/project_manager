"""pm_bootstrap 순수 파서 8종 직접 단위테스트 (T-0026).

지금까지 이 파서들은 `_collect_git()`/`_build_markdown()` 경유 *간접* 으로만 닿았다.
여기서는 8종 전부를 입력 문자열 → 기대 출력으로 **직접** 호출한다 (부작용 0). 각 함수:
happy-path + 최소 1 edge(빈 문자열·malformed·한글/로캘).

예외 하나 — `parse_lint_result` 의 출력-계약 테스트는 **실 `board.py lint` 를 subprocess 로
실행**한다(읽기 전용·board 미변경). 손으로 쓴 문자열만 쓰면 board 쪽 출력 형식이 바뀌어도
파서 테스트가 통과해버려, 정작 프로덕션에서 카운트가 어긋나는 클래스를 못 잡는다(T-0465).

도구는 패키지가 아니므로 importlib 동적 로드 — test_pm_bootstrap_tz / _failsoft 의
`_load_module` 관용구를 그대로 재사용한다.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOOTSTRAP_PY = TOOLS / "pm_bootstrap.py"
BOARD_PY = TOOLS / "board.py"
HANDOFF_PY = TOOLS / "pm_handoff.py"


def _load_module(name: str = "pm_bootstrap"):
    """pm_bootstrap 를 경로 로드한다 (도구는 패키지가 아니므로 importlib).

    로드 직후 0단계 엔진 앵커 검사(T-0351)를 hermetic 무력화한다 — 엔진 테스트는 worktree ①(엔진
    canonical)에서 로드돼 실 REPO 가 PM 홈 등록 worktree 사본으로 보여 anchor 가 거부하기 때문(모듈이
    per-test 라 autouse 대신 로더에서 처리·다른 부트스트랩 테스트의 autouse 픽스처와 동형)."""
    spec = importlib.util.spec_from_file_location(name, BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if name == "pm_bootstrap":
        real_board = mod._load_board()
        if real_board is not None:
            real_board._pm_home_worktree_misanchor = lambda anchor, **_kw: None
        mod._load_board = lambda: real_board
    return mod


def _load_board(name: str = "board"):
    """board 를 경로 로드한다 — grammar 정합 가드용 (`_ticket_prefix` 비교)."""
    spec = importlib.util.spec_from_file_location(name, BOARD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_pm_handoff(name: str = "pm_handoff"):
    """pm_handoff 를 경로 로드한다 — rewriter↔CLI 정합 가드용(rewriter 산출 재사용)."""
    spec = importlib.util.spec_from_file_location(name, HANDOFF_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# board list 출력 샘플 — status 필드는 7자 width 패딩.
_BOARD_OUTPUT = """\
보드 목록 (T-NNNN)

  [done   ] T-0001  엔진 초기화                    pm    engine
  [done   ] T-0002  보드 lint                       pm    engine
  [open   ] T-0010  lite 어댑터                     -     adapter
  [open   ] T-0026  엔진 코어 테스트 확충 — pm_log  -     test,engine
  [claimed] T-0030  진행 중 작업                    pm    wip
  [blocked] T-0040  의존 대기                       -     blocked
"""


# ── parse_board_counts ──────────────────────────────────────────────────────

def test_parse_board_counts_happy():
    mod = _load_module()
    counts = mod.parse_board_counts(_BOARD_OUTPUT)
    assert counts == {"done": 2, "open": 2, "claimed": 1, "blocked": 1}


def test_parse_board_counts_empty():
    """빈 입력 → 모든 status 0 (KeyError 없이 dict 골격 유지)."""
    mod = _load_module()
    assert mod.parse_board_counts("") == {"done": 0, "open": 0, "claimed": 0, "blocked": 0}


def test_parse_board_counts_ignores_unknown_status():
    """dict 에 없는 status 토큰(예: archived)은 무시된다 — 골격 키만 카운트."""
    mod = _load_module()
    out = "  [archived] T-0099  옛날 ticket  pm  old\n  [open   ] T-0100  새 ticket  -  new\n"
    counts = mod.parse_board_counts(out)
    assert counts == {"done": 0, "open": 1, "claimed": 0, "blocked": 0}


# ── parse_open_tickets ──────────────────────────────────────────────────────

def test_parse_open_tickets_happy():
    mod = _load_module()
    assert mod.parse_open_tickets(_BOARD_OUTPUT) == ["T-0010", "T-0026"]


def test_parse_open_tickets_none_open():
    """open 이 하나도 없으면 빈 목록 (claimed/blocked/done 은 제외)."""
    mod = _load_module()
    out = "  [done   ] T-0001  x  pm  t\n  [claimed] T-0002  y  pm  t\n"
    assert mod.parse_open_tickets(out) == []


def test_parse_open_tickets_prefixed_ids():
    """prefixed multi-repo ID(`T-PAY-001`·`T-service-a-001`·`T-P0-001`)도 잡는다 (T-0164).

    board list --mine 가 multi-repo 보드를 surface 하면 정상 open 티켓은 prefixed ID 다.
    `T-\\d+` 만 매칭하면 prefixed 가 전부 누락된다 — board.py `_TICKET_PREFIX_RE` 와 같은
    grammar(`[A-Za-z0-9_-]+`)로 prefixed(숫자/하이픈/언더스코어 포함) + legacy 를 다 파싱.
    """
    mod = _load_module()
    out = (
        "  [open   ] T-PAY-001       결제 모듈      -  pay\n"
        "  [open   ] T-service-a-001 서비스 A       -  svc\n"
        "  [open   ] T-P0-001        숫자포함 prefix -  p0\n"
        "  [open   ] T-123-001       순수숫자 prefix -  num\n"
        "  [open   ] T-0164          legacy 4자리   -  legacy\n"
        "  [claimed] T-PAY-002       진행 중        pm  pay\n"
    )
    assert mod.parse_open_tickets(out) == [
        "T-PAY-001",
        "T-service-a-001",
        "T-P0-001",
        "T-123-001",
        "T-0164",
    ]


def test_parse_open_tickets_grammar_matches_board():
    """parse_open_tickets grammar 가 board.py `_TICKET_PREFIX_RE` 와 정합인지 (drift 가드).

    board.py 가 발행/검증하는 ID grammar 와 부트스트랩 소비측이 어긋나면 한쪽이 잡는 ID 를
    다른 쪽이 놓친다(T-0164 round-3 클래스). 같은 prefix 집합에서 대칭임을 못박는다.
    """
    board = _load_board()
    mod = _load_module()
    # board 가 prefixed 로 인정하는 ID 면 부트스트랩도 open 목록으로 잡아야 한다.
    # `123` = 순수 숫자 prefix(등록 grammar `[A-Za-z0-9][A-Za-z0-9_-]*` 가 허용·round-3 must-fix).
    for prefix in ("PAY", "service-a", "P0", "x_y", "123"):
        tid = f"T-{prefix}-001"
        assert board._ticket_prefix(tid) == prefix  # board grammar 가 prefix 로 인정
        out = f"  [open   ] {tid}  t  -  tag\n"
        assert mod.parse_open_tickets(out) == [tid]
    # legacy(prefix 없음)도 양쪽에서 일관 — board 는 None, 부트스트랩은 open 으로 잡음.
    # legacy 4자리(`T-0164`·하이픈 1개) vs 숫자 prefix(`T-123-001`·하이픈 2개) 구조적 비충돌.
    assert board._ticket_prefix("T-0164") is None
    assert mod.parse_open_tickets("  [open   ] T-0164  t  -  tag\n") == ["T-0164"]


# ── parse_lint_result ───────────────────────────────────────────────────────

def test_parse_lint_result_clean():
    mod = _load_module()
    assert mod.parse_lint_result("✓ no lint issues") == "clean"


def test_parse_lint_result_warnings():
    """issue 형식(`[kind]`) 줄만 세어 'N warnings' 반환."""
    mod = _load_module()
    out = (
        "  [missing-depends] T-0001: depends_on 누락\n"
        "  [thin-ticket] T-0002: thin ticket\n"
        "  [dangling-wikilink] T-0003: wikilink 깨짐\n"
    )
    assert mod.parse_lint_result(out) == "3 warnings"


def test_parse_lint_result_empty_is_clean():
    """빈 입력 → 경고 0 → 'clean' (현재 구현 동작)."""
    mod = _load_module()
    assert mod.parse_lint_result("") == "clean"


def test_parse_lint_result_gate_header_excluded():
    """`--gate` 출력의 요약 헤더(⚠️ … lint issue(s) … 차단:)는 카운트에서 제외.

    헤더 1 줄 + issue 3 줄(advisory 2·차단 1) → 헤더를 세면 off-by-one(4) 이지만
    실제 issue 줄만 세어 '3 warnings' 여야 한다(T-0038 회귀 방지).
    """
    mod = _load_module()
    out = (
        "⚠️  3 lint issue(s) (1 blocking 차단):\n"
        "    [unstable-ref-advice] T-0001: 슬러그 참조 권고\n"
        "    [status-done-accum] T-0002: status drift\n"
        "  ✗ [dangling-wikilink] T-0003: 깨진 링크\n"
    )
    assert mod.parse_lint_result(out) == "3 warnings"


def test_parse_lint_result_clean_marker_is_line_anchored():
    """clean 판정은 줄 단위 양성 매칭 — 앵커 줄의 저장소 경로가 마커 문자열을 포함해도 오판 없음.

    T-0465 가 read 출력 첫 줄에 **임의의 사용자 경로**를 주입하면서, 그 전까지 board 생성
    텍스트만 보던 substring 검사가 unsound 해졌다(경로에 `no lint issues` 가 섞이면 실 이슈가
    있어도 clean). codex 게이트 지적.
    """
    mod = _load_module()
    poisoned = (
        "repo 앵커: /home/user/no lint issues/repo (worktree)\n"
        "\u26a0\ufe0f  3 lint issue(s):\n"
        "  [render-leak] a.md: x\n"
        "  [render-leak] b.md: y\n"
        "  [render-leak] c.md: z\n"
    )
    assert mod.parse_lint_result(poisoned) == "3 warnings"
    # 진짜 clean 은 앵커 줄이 있든 없든 clean.
    assert mod.parse_lint_result("repo 앵커: /home/u/repo (PM 홈)\n\u2713 no lint issues\n") == "clean"
    assert mod.parse_lint_result("\u2713 no lint issues\n") == "clean"


def test_parse_lint_result_gate_advisory_only():
    """advisory-only 게이트 출력(차단 0) — 헤더 제외하고 advisory 2 줄만 세어 '2 warnings'."""
    mod = _load_module()
    out = (
        "⚠️  2 lint issue(s) (0 blocking 차단):\n"
        "    [unstable-ref-advice] T-0001: 슬러그 참조 권고\n"
        "    [status-done-accum] T-0002: status drift\n"
    )
    assert mod.parse_lint_result(out) == "2 warnings"


@pytest.mark.parametrize("lint_argv", [["lint"], ["lint", "--gate"]])
def test_parse_lint_result_matches_actual_board_lint_output(lint_argv):
    """실제 `board.py lint` 합성 출력의 자체 요약 N과 파서 결과가 일치한다.

    T-0465 앵커 같은 새 표시 줄은 lint issue 형식이 아니므로 세지 않아야 한다.
    손으로 쓴 문자열만 쓰면 이 출력-계약 drift를 놓치므로 실제 CLI를 실행한다.

    **두 모드 다 돈다** — 프로덕션 호출부(`pm_bootstrap._collect_board`)는 `--gate` 를 쓰는데
    무-gate 만 실측하면 gate 쪽 issue 줄 형식(`  ✗ [kind]`)이 drift 할 때 *과소* 카운트로
    조용히 뒤집힌다(무-gate 는 `  [kind]` 로 형식이 다르다).
    """
    mod = _load_module()
    # encoding 명시 필수 — 자식은 UTF-8 을 내보내는데 text=True 만 주면 로캘 코덱으로 디코딩해
    # CP949 콘솔(Windows)에서 한글 lint 메시지가 UnicodeDecodeError 를 낸다(엔진 관례와 동일).
    result = subprocess.run(
        [sys.executable, str(BOARD_PY), *lint_argv], cwd=REPO,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    output = result.stdout + result.stderr  # pm_bootstrap._default_run_board 와 같은 합성 방식
    summary = re.search(r"⚠️\s+(\d+) lint issue\(s\)", output)
    # tautology 방지 — summary 도 clean 마커도 없으면 CLI 가 죽었거나 출력 형식이 바뀐 것이다.
    # 그걸 "clean 기대"로 흘려보내면 이 테스트가 조용히 무의미해진다(codex 게이트 지적).
    # clean 마커도 줄 단위로 본다 — substring 이면 저장소 경로에 그 문구가 있을 때 CLI 가
    # 앵커만 찍고 실패해도 이 단언이 통과해 false-green 이 된다(codex 게이트 지적).
    has_clean_marker = any(
        re.match(r"^\s*✓\s*no lint issues\s*$", line) for line in output.splitlines()
    )
    assert summary is not None or has_clean_marker, (
        f"board.py {' '.join(lint_argv)} 출력에 요약도 clean 마커도 없다 — "
        f"rc={result.returncode} 출력={output[:400]!r}"
    )
    expected = "clean" if summary is None else f"{summary.group(1)} warnings"
    assert mod.parse_lint_result(output) == expected


# ── parse_pytest_counts ─────────────────────────────────────────────────────

def test_parse_pytest_counts_passed_only():
    mod = _load_module()
    assert mod.parse_pytest_counts("279 passed in 6.55s") == (279, 279)


def test_parse_pytest_counts_with_failures():
    """passed + failed → total = passed + failed."""
    mod = _load_module()
    assert mod.parse_pytest_counts("3 failed, 276 passed in 6.55s") == (276, 279)


def test_parse_pytest_counts_no_passed_is_none():
    """'passed' 토큰이 없으면 None (예: collection error·빈 입력)."""
    mod = _load_module()
    assert mod.parse_pytest_counts("ERROR: no tests collected") is None
    assert mod.parse_pytest_counts("") is None


# ── parse_git_log ───────────────────────────────────────────────────────────

def test_parse_git_log_happy():
    mod = _load_module()
    out = "abc1234 first commit\ndef5678 두 번째 커밋 — 한글\n"
    assert mod.parse_git_log(out) == [
        ("abc1234", "first commit"),
        ("def5678", "두 번째 커밋 — 한글"),
    ]


def test_parse_git_log_sha_only_line():
    """subject 없는 (공백 없는) 줄은 ('sha', '') 로 — empty/malformed edge."""
    mod = _load_module()
    assert mod.parse_git_log("deadbee\n") == [("deadbee", "")]
    assert mod.parse_git_log("") == []


# ── parse_git_branch ────────────────────────────────────────────────────────

def test_parse_git_branch_happy():
    mod = _load_module()
    assert mod.parse_git_branch("main\n") == "main"


def test_parse_git_branch_empty():
    """빈/공백 출력 → 빈 문자열 (detached HEAD 등 edge)."""
    mod = _load_module()
    assert mod.parse_git_branch("   \n") == ""


# ── parse_git_status ────────────────────────────────────────────────────────

def test_parse_git_status_clean():
    mod = _load_module()
    assert mod.parse_git_status("") == "clean"
    assert mod.parse_git_status("\n  \n") == "clean"


def test_parse_git_status_modified():
    """비-빈 줄 수 → 'N files modified'."""
    mod = _load_module()
    out = " M tools/board.py\n?? tests/test_new.py\n"
    assert mod.parse_git_status(out) == "2 files modified"


# ── parse_git_ahead_behind (T-0195·board submodule freshness) ────────────────

def test_parse_git_ahead_behind_happy():
    mod = _load_module()
    assert mod.parse_git_ahead_behind("2\t1\n") == (2, 1)


def test_parse_git_ahead_behind_zero():
    mod = _load_module()
    assert mod.parse_git_ahead_behind("0\t0\n") == (0, 0)


def test_parse_git_ahead_behind_malformed_is_none():
    """빈 문자열·형식 불일치(탭 없음·비-숫자) → None(upstream 미설정과 동형 graceful)."""
    mod = _load_module()
    assert mod.parse_git_ahead_behind("") is None
    assert mod.parse_git_ahead_behind("no-upstream\n") is None
    assert mod.parse_git_ahead_behind("a\tb\n") is None


# ── parse_log_last_entry ────────────────────────────────────────────────────

def test_parse_log_last_entry_happy():
    """여러 entry 중 마지막의 date/type/title 추출 (한글 title 포함)."""
    mod = _load_module()
    text = (
        "# Project Log\n\n"
        "## [2026-06-13] ticket | T-0010 lite 어댑터\n본문1\n\n"
        "## [2026-06-14] handoff | PM 7차 인계 — 다음 우선순위\n본문2\n"
    )
    assert mod.parse_log_last_entry(text) == {
        "date": "2026-06-14",
        "type": "handoff",
        "title": "PM 7차 인계 — 다음 우선순위",
    }


def test_parse_log_last_entry_no_entries_is_none():
    """`## [date] type | title` 패턴이 없으면 None (헤더만·빈 입력)."""
    mod = _load_module()
    assert mod.parse_log_last_entry("# Project Log\n\n> 설명만 있고 entry 없음\n") is None
    assert mod.parse_log_last_entry("") is None


def test_parse_log_last_entry_requires_pipe_separator():
    """`|` 구분자가 없는 `## [date] ...` 줄은 매칭 안 됨 → None (malformed edge)."""
    mod = _load_module()
    text = "## [2026-06-14] handoff PM 인계 (pipe 없음)\n본문\n"
    assert mod.parse_log_last_entry(text) is None


# ── extract_last_log_entry_body (T-0179·인계 dump·pm_log.split_entries 재사용) ──

_LOG_TEXT = (
    "# Project Log\n\n"
    "## [2026-06-13] ticket | T-0010 lite 어댑터\n"
    "- 첫 entry 본문\n\n"
    "## [2026-06-14] handoff | PM 7차 인계 — 다음 우선순위\n"
    "- 인계 사항 A\n"
    "- 인계 사항 B\n"
)


# handoff entry 가 없는 log — 차수 log-폴백 대상 부재(placeholder 유지·T-0208 회귀 가드).
_LOG_TEXT_NO_HANDOFF = (
    "# Project Log\n\n"
    "## [2026-06-14] note | 진행 메모\n"
    "- 인계 사항 A\n"
    "- 인계 사항 B\n"
)


def test_extract_last_log_entry_body_returns_full_body():
    """마지막 entry 의 본문 전체(`## [..]` 줄 + 하위 라인)를 반환한다 (제목만 아님)."""
    mod = _load_module()
    body = mod.extract_last_log_entry_body(_LOG_TEXT)
    assert body is not None
    # 마지막 entry 의 헤더 줄과 본문 라인이 전부 들어간다.
    assert "## [2026-06-14] handoff | PM 7차 인계 — 다음 우선순위" in body
    assert "- 인계 사항 A" in body
    assert "- 인계 사항 B" in body
    # 이전 entry 본문은 섞이지 않는다.
    assert "첫 entry 본문" not in body


def test_extract_last_log_entry_body_matches_pm_log_tail():
    """단일 진실 = `pm_log.split_entries` — tail 의 `entries[-1][1]` 과 동일 결과 (DRY 가드)."""
    mod = _load_module()
    # pm_log 를 직접 로드해 split_entries 의 마지막 entry 와 동형인지 대조한다(tail 재사용 핀).
    import importlib.util
    spec = importlib.util.spec_from_file_location("pm_log", TOOLS / "pm_log.py")
    real_pm_log = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real_pm_log)
    _pre, entries = real_pm_log.split_entries(_LOG_TEXT)
    assert mod.extract_last_log_entry_body(_LOG_TEXT) == entries[-1][1].rstrip()


def test_extract_last_log_entry_body_no_entries_is_none():
    """entry 가 없으면 None (제목만 표시하던 현행으로 폴백 신호)."""
    mod = _load_module()
    assert mod.extract_last_log_entry_body("# Project Log\n\n> 설명만\n") is None
    assert mod.extract_last_log_entry_body("") is None


# ── infer_session_num (T-0179·차수 추론·pm_handoff.infer_next_session_num 재사용) ──

_PM_STATE_TEXT = (
    "---\ntitle: PM State\n---\n\n"
    "## 세션 식별 (현재까지 사용된 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - **40차** (2026-06-27 · 요약): 요약.\n"
    "  - **41차** (2026-06-27 · 요약): 요약.\n"
    "  - 이전 차 (PM 1차~39차) = log.\n\n"
    "## 진행 중인 의사결정\n\n"
    "| 항목 | 상태 |\n|---|---|\n| x | y |\n\n"
    "## 남은 작업 전체 그림\n\n"
    "> PM 41 종결 — actionable = 0.\n\n"
    "### 🔴 다음 세션 — 사용자 발의\n"
    "- sikdan pm 업데이트.\n\n"
    "### 🟡 DEFER (명시 트리거 전엔 안 함)\n"
    "- log archive.\n\n"
    "## 다른 절\n"
    "- 이건 안 들어가야 한다.\n"
)


def test_infer_session_num_returns_next_number():
    """세션 식별 절 최고차(41) + 1 = 42 를 반환한다 (pm_handoff.infer_next_session_num 재사용)."""
    mod = _load_module()
    assert mod.infer_session_num(_PM_STATE_TEXT) == 42


def test_infer_session_num_no_section_returns_placeholder():
    """세션 식별 절/entry 가 없으면 `?` placeholder (pm_handoff 계약 전달)."""
    mod = _load_module()
    assert mod.infer_session_num("## 다른 절\n- 내용\n") == "?"


# ── extract_remaining_work_section (T-0179·남은 작업/사용자발의 절 surface) ────

def test_extract_remaining_work_section_includes_subsections():
    """`## 남은 작업 전체 그림` 절을 다음 `## ` 직전까지 통째로(🔴/🟡 하위절 포함) 반환한다."""
    mod = _load_module()
    section = mod.extract_remaining_work_section(_PM_STATE_TEXT)
    assert section is not None
    assert section.startswith("## 남은 작업 전체 그림")
    assert "🔴 다음 세션 — 사용자 발의" in section
    assert "sikdan pm 업데이트" in section
    assert "🟡 DEFER" in section
    # 다음 `## ` 절은 범위 밖.
    assert "이건 안 들어가야 한다" not in section


def test_extract_remaining_work_section_to_eof():
    """다음 `## ` 헤더가 없으면 파일 끝까지가 절 범위다."""
    mod = _load_module()
    text = "## 남은 작업 전체 그림\n\n### 🔴 다음 세션 — 사용자 발의\n- 끝까지\n"
    section = mod.extract_remaining_work_section(text)
    assert section is not None
    assert "끝까지" in section


def test_extract_remaining_work_section_anchor_absent_is_none():
    """앵커(`## 남은 작업 전체 그림`)가 없으면 None (명시 포인터 폴백 신호)."""
    mod = _load_module()
    assert mod.extract_remaining_work_section("## 다른 절\n- 내용\n") is None


# ── _format_session_label (T-0179·차수 announce 머리표·crash 금지) ─────────────

def test_format_session_label_with_int():
    """session_num 이 정수면 `PM <N>차`."""
    mod = _load_module()
    assert mod._format_session_label({"session_num": 42}) == "PM 42차"


def test_format_session_label_placeholder_cases():
    """`?`(entry 부재)·None·handoff_ctx 부재는 전부 placeholder (graceful·crash 금지)."""
    mod = _load_module()
    assert mod._format_session_label({"session_num": "?"}) == mod._SESSION_LABEL_PLACEHOLDER
    assert mod._format_session_label({"session_num": None}) == mod._SESSION_LABEL_PLACEHOLDER
    assert mod._format_session_label(None) == mod._SESSION_LABEL_PLACEHOLDER


# ── 차수 log-폴백 + stale 교차검증 (T-0208·ADR-0035) ──────────────────────────

_LOG_HANDOFF_TEXT = (
    "# Project Log\n\n"
    "## [2026-07-01] complete | T-0100 뭔가 완료\n"
    "- 본문.\n\n"
    "## [2026-07-02] handoff | PM 47차 → 다음 PM 세션\n"
    "- 인계.\n"
)

# handoff 뒤에 note entry 가 붙어도(chronological 최신이 handoff 아님) handoff 를 잡아야 한다.
_LOG_HANDOFF_THEN_NOTE = (
    "# Project Log\n\n"
    "## [2026-07-02] handoff | PM 48차 → 다음 PM 세션\n"
    "- 인계.\n\n"
    "## [2026-07-03] note | 사용자 액션 메모\n"
    "- 메모.\n"
)


def test_parse_last_handoff_session_num_returns_num():
    """handoff entry 제목 `PM 47차` → 47 (다음 차수 유도 전의 raw N)."""
    mod = _load_module()
    assert mod.parse_last_handoff_session_num(_LOG_HANDOFF_TEXT) == 47


def test_parse_last_handoff_session_num_ignores_trailing_note():
    """handoff 뒤 note entry 가 있어도 마지막 *handoff* 의 N(48)을 잡는다 (note 오파싱 방지)."""
    mod = _load_module()
    assert mod.parse_last_handoff_session_num(_LOG_HANDOFF_THEN_NOTE) == 48


def test_parse_last_handoff_session_num_takes_latest_handoff():
    """여러 handoff 면 마지막(최신·최고차)을 취한다."""
    mod = _load_module()
    text = (
        "## [2026-06-14] handoff | PM 7차 인계 — 다음 우선순위\n- a.\n\n"
        "## [2026-07-02] handoff | PM 47차 → 다음 PM 세션\n- b.\n"
    )
    assert mod.parse_last_handoff_session_num(text) == 47


def test_parse_last_handoff_session_num_none_when_no_handoff():
    """handoff type entry 가 없으면(complete/note 만) None (폴백 없음·현행 유지)."""
    mod = _load_module()
    assert mod.parse_last_handoff_session_num(_LOG_TEXT_NO_HANDOFF) is None
    assert mod.parse_last_handoff_session_num("") is None
    assert mod.parse_last_handoff_session_num(None) is None


def test_last_handoff_header_line_returns_full_line():
    """pickaxe needle — 마지막 handoff entry 헤더 줄 전체(날짜·type·제목)를 반환한다."""
    mod = _load_module()
    line = mod.last_handoff_header_line(_LOG_HANDOFF_TEXT)
    assert line == "## [2026-07-02] handoff | PM 47차 → 다음 PM 세션"


def test_last_handoff_header_line_none_when_absent():
    mod = _load_module()
    assert mod.last_handoff_header_line(_LOG_TEXT_NO_HANDOFF) is None
    assert mod.last_handoff_header_line(None) is None


def test_reconcile_session_num_state_wins_when_ahead():
    """pm_state 해소값이 log-derived 보다 크면 pm_state 우선·stale 아님 (현행 무변경·회귀 0)."""
    mod = _load_module()
    assert mod.reconcile_session_num(42, 8) == (42, False)
    # 동률(log_next == state)도 pm_state 유지·stale 아님(엄격 `>` 비교).
    assert mod.reconcile_session_num(48, 48) == (48, False)


def test_reconcile_session_num_log_wins_when_stale():
    """pm_state 가 해소돼도 log-derived 가 더 크면 log 우선(max) + stale True (머신 간 미동기)."""
    mod = _load_module()
    assert mod.reconcile_session_num(48, 49) == (49, True)


def test_reconcile_session_num_falls_back_to_log():
    """pm_state 미해소(`?`/None)면 log-derived 로 폴백(stale 아님·폴백 층)."""
    mod = _load_module()
    assert mod.reconcile_session_num("?", 48) == (48, False)
    assert mod.reconcile_session_num(None, 48) == (48, False)


def test_reconcile_session_num_state_only_and_both_unresolved():
    """log 폴백 없음(None)이면 pm_state 그대로; 둘 다 미해소면 placeholder 그대로."""
    mod = _load_module()
    assert mod.reconcile_session_num(42, None) == (42, False)
    assert mod.reconcile_session_num("?", None) == ("?", False)
    assert mod.reconcile_session_num(None, None) == (None, False)


def test_format_stale_warning_shows_both_numbers():
    """stale=True 면 log 우선 차수 + 뒤처진 pm_state 차수를 함께 담은 경고 1줄."""
    mod = _load_module()
    line = mod._format_stale_warning(
        {"session_num": 49, "session_stale": True, "state_session_num": 48}
    )
    assert line is not None
    assert "pm_state stale" in line
    assert "PM 49차" in line
    assert "PM 48차" in line


def test_format_stale_warning_none_when_not_stale():
    """stale 아님·handoff_ctx 부재면 None(줄 생략)."""
    mod = _load_module()
    assert mod._format_stale_warning({"session_num": 42, "session_stale": False}) is None
    assert mod._format_stale_warning(None) is None


# ── _format_board_counts_line (T-0194·`--mine`(scoped) 라벨 명확화) ────────────

def test_format_board_counts_line_labels_mine():
    """각 status 카운트 뒤에 `(mine)` 이 붙어 --mine 스코프임이 드러난다 — open 도 세션 스코프(ADR-0067)."""
    mod = _load_module()
    counts = {"done": 25, "open": 6, "claimed": 2, "blocked": 0}
    line = mod._format_board_counts_line(counts)
    assert "done: 25 (mine)" in line
    assert "open: 6 (mine)" in line   # ADR-0067: open 도 세션 스코프(옛 backlog 라벨 폐기)
    assert "claimed: 2 (mine)" in line
    assert "blocked: 0 (mine)" in line


def test_format_board_counts_line_does_not_imply_total():
    """라벨이 "전체" 를 암시하지 않는다 — 실측 오해 재발 방지(done 25 vs 184)."""
    mod = _load_module()
    counts = {"done": 25, "open": 6, "claimed": 2, "blocked": 0}
    line = mod._format_board_counts_line(counts)
    assert "total" not in line


# ── _format_board_git_freshness (T-0195·board submodule freshness 한 줄) ──────

def test_format_board_git_freshness_clean_no_upstream():
    """dirty=False·ahead/behind 미상(None) → HEAD + clean 만(구간 생략)."""
    mod = _load_module()
    line = mod._format_board_git_freshness(
        {"head": "abc1234", "dirty": False, "ahead": None, "behind": None}
    )
    assert line == "HEAD abc1234 · clean"


def test_format_board_git_freshness_dirty_with_ahead_behind():
    mod = _load_module()
    line = mod._format_board_git_freshness(
        {"head": "def5678", "dirty": True, "ahead": 2, "behind": 1}
    )
    assert line == "HEAD def5678 · dirty · 2 ahead / 1 behind"


# ── run() 통합: 차수 announce + log 본문 dump + 남은작업 surface + 미해소 graceful ──

def _make_hermetic_bootstrap(
    mod, tmp_path, *, log_text: str, pm_state_text: str | None, board_git_present: bool = False,
):
    """board/git/pytest 는 stub, log/pm_state 는 tmp 파일로 격리한 PmBootstrap (실 fs 미접촉).

    pm_state_text=None → pm_state 파일을 두지 않아 *미해소*(graceful placeholder) 경로를 탄다.
    board_git_present=True(T-0195) → `board_dir` 을 실 디렉토리로 만들어 `_collect_board_git`
    이 stub 응답(HEAD abc1234·dirty·1 ahead/0 behind)을 수집하는 경로를 탄다. False(기본)면
    board_dir 미생성 → graceful skip(현행 T-0194 테스트들과 회귀 0).
    """
    log_file = tmp_path / "current.md"
    log_file.write_text(log_text, encoding="utf-8")
    areas_file = tmp_path / "areas.md"  # 빈(미생성) → 솔로
    board_dir = tmp_path / "board"
    if board_git_present:
        board_dir.mkdir()

    def _git_fn(args: list[str]) -> tuple[int, str]:
        if args[:2] == ["-C", str(board_dir)]:
            sub = args[2:]
            # T-0217 board 서브모듈 rider(fetch + branch 유지 pull). rev-list 가 behind 0 을
            # 주므로 이 fixture 에선 checkout/pull 은 안 불리지만 안전하게 handler 를 둔다.
            if sub == ["fetch", "origin"]:
                return (0, "")
            if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
                return (0, "refs/heads/main\n")
            if sub == ["rev-parse", "--short", "HEAD"]:
                return (0, "abc1234\n")
            if sub == ["status", "-s"]:
                return (0, " M areas.md\n")
            if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
                return (0, "1\t0\n")
            if sub[:1] == ["checkout"] or sub[:1] == ["pull"]:
                return (0, "")
            raise AssertionError(f"예상치 못한 board git 호출: {args}")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "main\n")
        if args[:2] == ["log", "--oneline"]:
            return (0, "abc123 subj\n")
        return (0, "")

    kwargs = dict(
        run_board_fn=lambda args: (0, "✓ no lint issues\n") if args[:1] == ["lint"]
        else (0, "  [open   ] T-0001  x  pm  tag\n"),
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=_git_fn,
        log_file=log_file,
        areas_file=areas_file,
        board_dir=board_dir,
    )
    if pm_state_text is not None:
        pm_state_file = tmp_path / "pm_state.md"
        pm_state_file.write_text(pm_state_text, encoding="utf-8")
        kwargs["pm_state_file"] = pm_state_file
    else:
        # 미해소 graceful — 존재하지 않는 경로를 주입해 _collect_handoff_context 가 None 으로 폴백.
        kwargs["pm_state_file"] = tmp_path / "absent_pm_state.md"
    return mod.PmBootstrap(**kwargs)


# ── _collect_board_git (T-0195·board submodule freshness) ────────────────────

def test_collect_board_git_absent_dir_is_none(tmp_path):
    """`.project_manager/board` 가 실 디렉토리가 아니면(솔로/미분리) None (graceful skip)."""
    mod = _load_module()
    board_dir = tmp_path / "board"  # 미생성
    inst = mod.PmBootstrap(
        run_git_fn=lambda args: (_ for _ in ()).throw(AssertionError("git 미호출 기대")),
        board_dir=board_dir,
    )
    assert inst._collect_board_git() is None


def test_collect_board_git_present_reports_head_dirty_ahead_behind(tmp_path):
    """board 디렉토리가 실재하면 HEAD·dirty·ahead/behind 를 수집한다."""
    mod = _load_module()
    board_dir = tmp_path / "board"
    board_dir.mkdir()

    def _git_fn(args: list[str]) -> tuple[int, str]:
        assert args[:2] == ["-C", str(board_dir)]
        sub = args[2:]
        if sub == ["rev-parse", "--short", "HEAD"]:
            return (0, "abc1234\n")
        if sub == ["status", "-s"]:
            return (0, " M areas.md\n")
        if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return (0, "2\t1\n")
        raise AssertionError(f"예상치 못한 board git 호출: {args}")

    inst = mod.PmBootstrap(run_git_fn=_git_fn, board_dir=board_dir)
    result = inst._collect_board_git()
    assert result == {"head": "abc1234", "dirty": True, "ahead": 2, "behind": 1}


def test_collect_board_git_no_upstream_ahead_behind_none(tmp_path):
    """ahead/behind rev-list 실패(upstream 미설정) → ahead/behind=None(부분 degrade)."""
    mod = _load_module()
    board_dir = tmp_path / "board"
    board_dir.mkdir()

    def _git_fn(args: list[str]) -> tuple[int, str]:
        sub = args[2:]
        if sub == ["rev-parse", "--short", "HEAD"]:
            return (0, "abc1234\n")
        if sub == ["status", "-s"]:
            return (0, "")
        if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return (1, "fatal: no upstream configured\n")
        raise AssertionError(f"예상치 못한 board git 호출: {args}")

    inst = mod.PmBootstrap(run_git_fn=_git_fn, board_dir=board_dir)
    result = inst._collect_board_git()
    assert result == {"head": "abc1234", "dirty": False, "ahead": None, "behind": None}


def test_collect_board_git_head_failure_is_none(tmp_path):
    """HEAD 조회 자체가 실패하면(손상된 board) 전체 None(fail-soft)."""
    mod = _load_module()
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    inst = mod.PmBootstrap(
        run_git_fn=lambda args: (128, "fatal: not a git repository\n"),
        board_dir=board_dir,
    )
    assert inst._collect_board_git() is None


# ── run() 통합: 차수 announce + log 본문 dump + 남은작업 surface + 미해소 graceful ──


def test_run_announces_session_num_in_header(tmp_path, capsys):
    """bound slot pm_state 차수로 헤더에 `PM <N>차` announce."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "## PM 42차 부트스트랩" in out


def test_run_dumps_log_entry_body(tmp_path, capsys):
    """log 마지막 entry 의 제목 + **본문 전체**를 dump (제목만 아님·인계 컨텍스트)."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "title: PM 7차 인계 — 다음 우선순위" in out
    # 본문 라인이 표면화된다(그간 제목만 표시).
    assert "- 인계 사항 A" in out
    assert "- 인계 사항 B" in out


def test_run_surfaces_remaining_work_section(tmp_path, capsys):
    """pm_state '남은 작업/사용자발의' 절을 surface."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "### pm_state — 남은 작업 / 사용자 발의" in out
    assert "🔴 다음 세션 — 사용자 발의" in out
    assert "sikdan pm 업데이트" in out


def test_run_graceful_when_pm_state_unresolved(tmp_path, capsys):
    """pm_state 미해소 + log handoff 부재면 차수 placeholder + 명시 포인터 — crash 없이 진행.

    (log 에 handoff entry 가 있으면 차수 log-폴백[T-0208]이 placeholder 를 대체하므로, 진짜
    placeholder 경로를 고정하려면 log 도 handoff-없음이어야 한다 — 별 fixture `_LOG_TEXT_NO_HANDOFF`.)
    """
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT_NO_HANDOFF, pm_state_text=None)
    assert inst.run() == 0
    out = capsys.readouterr().out
    # 차수 placeholder(crash 금지) + 남은작업 명시 포인터 폴백.
    assert "## PM <?>차 부트스트랩" in out
    assert "남은 작업 전체 그림" in out  # 포인터 안내 문구.
    # 본문 dump 는 log 가 있으니 여전히 나온다(차수 미해소와 독립).
    assert "- 인계 사항 A" in out


def test_run_json_includes_session_num_and_handoff_context(tmp_path, capsys):
    """--json 출력에 session_num + handoff_context(remaining_work·log body) 포함."""
    import json as _json
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    assert inst.run(output_json=True) == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["session_num"] == 42
    assert data["handoff_context"]["session_num"] == 42
    assert "sikdan pm 업데이트" in data["handoff_context"]["remaining_work"]
    assert "- 인계 사항 A" in data["log_last_entry"]["body"]


# ── 차수 log-폴백 + stale 교차검증 run() 통합 (T-0208) ─────────────────────────

# pm_state 세션 식별 절은 있으나 entry 가 없는 template → infer_session_num == "?"(미해소).
_PM_STATE_TEMPLATE = (
    "---\ntitle: PM State\n---\n\n"
    "## 세션 식별 (현재까지 사용된 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - (아직 없음)\n\n"
    "## 남은 작업 전체 그림\n\n"
    "> 초기.\n"
)

# pm_state 가 48차를 해소하나 log 가 PM 48차 handoff(→ 다음 49) 라 log 가 앞선다(stale).
_PM_STATE_48 = (
    "---\ntitle: PM State\n---\n\n"
    "## 세션 식별 (현재까지 사용된 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - **47차** (2026-07-01 · 요약): 요약.\n\n"
)
_LOG_TEXT_47_HANDOFF = (
    "# Project Log\n\n"
    "## [2026-07-02] handoff | PM 47차 → 다음 PM 세션\n"
    "- 인계.\n"
)
_LOG_TEXT_48_HANDOFF = (
    "# Project Log\n\n"
    "## [2026-07-02] handoff | PM 48차 → 다음 PM 세션\n"
    "- 인계.\n"
)


def test_run_falls_back_to_log_session_num_when_pm_state_template(tmp_path, capsys):
    """DoD ①: fresh pm_state(template·`?`) + log handoff(`PM 47차`) → 헤더 `PM 48차`(N+1 폴백)."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT_47_HANDOFF, pm_state_text=_PM_STATE_TEMPLATE,
    )
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "## PM 48차 부트스트랩" in out
    # 폴백(미해소→log)은 stale 경고 아님(pm_state 가 뒤처진 게 아니라 아예 미해소).
    assert "pm_state stale" not in out


def test_run_session_num_stale_cross_check_prefers_log(tmp_path, capsys):
    """DoD 인터페이스: pm_state 해소(48) < log-derived(49) → log 우선(max) + stale 경고 1줄."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT_48_HANDOFF, pm_state_text=_PM_STATE_48,
    )
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "## PM 49차 부트스트랩" in out
    assert "⚠ pm_state stale (머신 간 미동기)" in out
    assert "PM 48차" in out  # 뒤처진 pm_state 값 진단.


def test_run_session_num_state_wins_no_stale_warning(tmp_path, capsys):
    """DoD ③: pm_state 해소(42) > log-derived(8) → pm_state 우선·stale 경고 없음(회귀 무변경)."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "## PM 42차 부트스트랩" in out
    assert "pm_state stale" not in out


# ── user 연속성 1줄 (T-0208·ADR-0033 ③·author 일치/불일치/미해소 3분기) ─────────

def _user_continuity_bootstrap(mod, *, author_email, user, log_file=None):
    """`_collect_user_continuity` 직접 검증용 PmBootstrap — pickaxe git·board.user_name 을 mock.

    author_email=None → pickaxe 가 빈 출력(author 미해소). user=None → board.user_name 미해소.
    """
    def _git_fn(args: list[str]) -> tuple[int, str]:
        # pickaxe 호출: ["-C", <REPO>, "log", "-1", "--format=%ae", "-S<header>", "--", <log>]
        if args[2:5] == ["log", "-1", "--format=%ae"]:
            return (0, f"{author_email}\n") if author_email else (0, "")
        raise AssertionError(f"예상치 못한 git 호출: {args}")

    kwargs = dict(
        run_git_fn=_git_fn,
        board=SimpleNamespace(user_name=lambda: user),
    )
    if log_file is not None:
        kwargs["log_file"] = log_file
    return mod.PmBootstrap(**kwargs)


def test_collect_user_continuity_match():
    """직전 handoff author == 현재 user → `사용자: … (동일 — 연속)`."""
    mod = _load_module()
    inst = _user_continuity_bootstrap(mod, author_email="alice@x.com", user="alice@x.com")
    line = inst._collect_user_continuity(_LOG_TEXT_47_HANDOFF)
    assert line == "사용자: alice@x.com (직전 handoff 작성자와 동일 — 연속)"


def test_collect_user_continuity_mismatch():
    """직전 handoff author != 현재 user → ⚠ 다른 사용자(author) 경고."""
    mod = _load_module()
    inst = _user_continuity_bootstrap(mod, author_email="bob@x.com", user="alice@x.com")
    line = inst._collect_user_continuity(_LOG_TEXT_47_HANDOFF)
    assert line == (
        "⚠ 직전 handoff 는 다른 사용자(bob@x.com) — pending intent 는 프로젝트 상태로 취급"
    )


def test_collect_user_continuity_unresolved_is_none():
    """author 조회불가·user 미상·handoff 부재 → None(줄 생략·fail-soft)."""
    mod = _load_module()
    # author 미해소(pickaxe 빈 출력).
    inst_no_author = _user_continuity_bootstrap(mod, author_email=None, user="alice@x.com")
    assert inst_no_author._collect_user_continuity(_LOG_TEXT_47_HANDOFF) is None
    # user 미상(board.user_name None).
    inst_no_user = _user_continuity_bootstrap(mod, author_email="alice@x.com", user=None)
    assert inst_no_user._collect_user_continuity(_LOG_TEXT_47_HANDOFF) is None
    # handoff entry 부재 → git 호출 자체를 안 함(needle 없음)·None.
    inst_ok = _user_continuity_bootstrap(mod, author_email="alice@x.com", user="alice@x.com")
    assert inst_ok._collect_user_continuity(_LOG_TEXT_NO_HANDOFF) is None


def test_run_surfaces_user_continuity_line(tmp_path, capsys):
    """run() markdown 헤더 직후 user 연속성 1줄이 표면화된다(일치 케이스)."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT,
    )
    # 기본 fixture 는 pickaxe 를 (0,"") 로 흘려 author 미해소 → 줄 생략. author/user 를 주입해
    # 일치 케이스를 만든다(board mock + pickaxe author 반환).
    inst._board = SimpleNamespace(user_name=lambda: "alice@x.com")
    _orig_git = inst._run_git_fn

    def _git_fn(args):
        if args[2:5] == ["log", "-1", "--format=%ae"]:
            return (0, "alice@x.com\n")
        return _orig_git(args)

    inst._run_git_fn = _git_fn
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "사용자: alice@x.com (직전 handoff 작성자와 동일 — 연속)" in out


# ── run() 통합: board 카운트 `--mine` 라벨 명확화 (T-0194) ─────────────────────

def test_run_markdown_board_counts_labeled_mine(tmp_path, capsys):
    """markdown Board 섹션 + 권장 첫 turn 요약 둘 다 네 status 모두 `(mine)` 스코프 라벨(ADR-0067):
    open 도 세션 스코프(내 세션 생성분)라 옛 open 전용 backlog 라벨(T-0331·ADR-0066)은 폐기됐다."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    assert inst.run() == 0
    out = capsys.readouterr().out
    # 네 status 모두 mine 스코프 — Board 섹션(`open: N`)·요약(`open N`) 양쪽.
    assert "done: 0 (mine)" in out
    assert "done 0 (mine)" in out
    assert "open: 1 (mine)" in out
    assert "open 1 (mine)" in out
    assert "backlog·기본 접힘" not in out   # 옛 open 전용 라벨 폐기(ADR-0067)


def test_run_json_board_counts_include_mine_alias(tmp_path, capsys):
    """solo JSON은 기존 mine 별칭을 보존하고 task-only 키를 방출하지 않는다."""
    import json as _json
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT)
    inst._run_pytest_fn = lambda: (0, "1 passed in 0.01s\n")
    assert inst.run(output_json=True, with_pytest=True) == 0
    data = _json.loads(capsys.readouterr().out)
    board = data["board"]
    assert board["counts_scope"] == "mine"
    assert board["counts_mine"] == {
        "done": board["done"],
        "open": board["open"],
        "claimed": board["claimed"],
        "blocked": board["blocked"],
    }
    assert "counts_task" not in board
    assert data["pytest"] == {"passed": 1, "total": 1}
    assert "scopes" not in data["pytest"]
    assert "task_cwd_slot" not in data["git"]
    assert "task_workspace_count" not in data["git"]


# ── run() 통합: board submodule freshness surface (T-0195) ───────────────────

def test_run_markdown_board_git_absent_is_skipped(tmp_path, capsys):
    """board 미분리(솔로) → Git 섹션에 board freshness 줄 자체가 생략된다(graceful skip)."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT, board_git_present=False,
    )
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "- board: HEAD" not in out


def test_run_markdown_board_git_present_surfaces_freshness(tmp_path, capsys):
    """board 분리(submodule) → Git 섹션에 HEAD·dirty·ahead/behind 1줄이 추가된다."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT, board_git_present=True,
    )
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "- board: HEAD abc1234 · dirty · 1 ahead / 0 behind" in out


def test_run_json_board_git_present_in_git_section(tmp_path, capsys):
    """--json `git.board_git` 에 HEAD/dirty/ahead/behind 가 실린다(분리 시)."""
    import json as _json
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT, board_git_present=True,
    )
    assert inst.run(output_json=True) == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["git"]["board_git"] == {
        "head": "abc1234", "dirty": True, "ahead": 1, "behind": 0,
    }


def test_run_json_board_git_absent_is_none(tmp_path, capsys):
    """--json `git.board_git` 이 솔로(미분리)면 None(graceful skip)."""
    import json as _json
    mod = _load_module()
    inst = _make_hermetic_bootstrap(
        mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=_PM_STATE_TEXT, board_git_present=False,
    )
    assert inst.run(output_json=True) == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["git"]["board_git"] is None


# ── _collect_board dump-then-warn (T-0195·abort-before-dump 제거) ────────────

_BLOCKING_GATE_OUT = (
    "⚠️  1 lint issue(s) (1 blocking 차단):\n"
    "  ✗ [dangling-wikilink] T-0003: 깨진 링크\n"
)


def _make_blocking_lint_board_fn():
    """`lint --gate` 가 blocking(rc=1) 을 내는 fake run_board_fn (`list` 는 정상 rc=0)."""
    def _fn(args: list[str]) -> tuple[int, str]:
        if args[0] == "list":
            return (0, "  [open   ] T-0010  x  -  adapter\n")
        if args[0] == "lint":
            return (1, _BLOCKING_GATE_OUT)
        raise AssertionError(f"예상치 못한 board 호출: {args}")
    return _fn


def test_collect_board_blocking_lint_does_not_abort():
    """T-0195: blocking lint(rc≠0) 여도 `_collect_board` 는 더 이상 sys.exit 하지 않는다.

    (T-0038 이래 회귀 가드였던 `test_pm_bootstrap_failsoft.py::
    test_collect_board_blocking_lint_aborts` 는 이 티켓이 뒤집는 옛 동작(abort-before-
    dump)을 고정하고 있어 T-0195 목표와 직접 충돌한다 — touches 범위 밖이라 갱신 불가.
    이 테스트가 새 동작(dump-then-warn)의 회귀 가드다.)
    """
    mod = _load_module()
    bootstrap = mod.PmBootstrap(run_board_fn=_make_blocking_lint_board_fn())

    board = bootstrap._collect_board()  # SystemExit 를 던지면 이 테스트가 실패한다.

    assert board["lint_blocking"] is True
    assert board["lint_gate_output"] == _BLOCKING_GATE_OUT
    assert board["open_tickets"] == ["T-0010"]


def test_collect_board_advisory_only_lint_blocking_false():
    """advisory-only(rc=0) → `lint_blocking=False`(정상 통과 신호 보존)."""
    mod = _load_module()

    def _fn(args: list[str]) -> tuple[int, str]:
        if args[0] == "list":
            return (0, "  [open   ] T-0010  x  -  adapter\n")
        return (0, "✓ no lint issues\n")

    bootstrap = mod.PmBootstrap(run_board_fn=_fn)
    board = bootstrap._collect_board()
    assert board["lint_blocking"] is False


def test_run_dump_then_warn_prints_dump_before_warning_and_exits_nonzero(tmp_path, capsys):
    """run() — blocking lint 여도 markdown dump(board/git/log) 가 먼저 출력되고, 이후
    경고 + 비-0 반환(T-0195 — abort-before-dump 제거·mid-wave 세션 진입 매끄럽게).
    """
    mod = _load_module()
    log_file = tmp_path / "current.md"
    log_file.write_text(_LOG_TEXT, encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text(_PM_STATE_TEXT, encoding="utf-8")
    areas_file = tmp_path / "areas.md"

    inst = mod.PmBootstrap(
        run_board_fn=_make_blocking_lint_board_fn(),
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=lambda args: (0, "main\n") if args[:2] == ["rev-parse", "--abbrev-ref"]
        else ((0, "abc123 subj\n") if args[:2] == ["log", "--oneline"] else (0, "")),
        log_file=log_file,
        areas_file=areas_file,
        pm_state_file=pm_state_file,
        board_dir=tmp_path / "board",  # 미생성 → board freshness graceful skip
    )

    rc = inst.run()
    out = capsys.readouterr()

    assert rc == 1  # 비-0 종료(경고).
    # 기계 dump(board/git/log/pm_state)가 stdout 에 정상 출력됨 — dump 0 아님.
    assert "## PM 42차 부트스트랩" in out.out
    assert "### Git" in out.out
    assert "- 인계 사항 A" in out.out
    # 경고는 stderr 로.
    assert "차단(blocking)" in out.err


# ── T-0217: git freshness (fetch + behind 표면화 + clean·ff 자동 pull) ─────────

# ── freshness_decision (순수 판정 함수) ──────────────────────────────────────

def test_freshness_decision_latest_no_pull():
    """behind 0 · ahead 0 → '최신' · pull 안 함."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=False, ahead=0, behind=0) == ("최신", False)


def test_freshness_decision_behind_clean_ff_pulls():
    """behind>0 · ahead 0 · clean · fetch 성공 → '동기' · pull True (안전조건 전부 충족)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=False, ahead=0, behind=3) == ("동기", True)


def test_freshness_decision_fetch_failed_no_pull():
    """behind>0 · clean · ff 여도 **fetch 실패면 자동 pull 금지** (codex must-fix ① — stale 원격)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=False, detached=False, dirty=False, ahead=0, behind=3) == (
        "수동 동기 필요", False,
    )


def test_freshness_decision_status_unknown_no_pull():
    """behind>0 · ff 여도 **status 미확인(dirty=None)이면 자동 pull 금지** (codex must-fix ② — clean 미증명)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=None, ahead=0, behind=3) == (
        "수동 동기 필요", False,
    )


def test_freshness_decision_ahead_unknown_no_pull():
    """behind>0 · fetch 성공 · clean 이어도 **ahead=None(미확인)이면 자동 pull 금지**
    (codex round-2 must-fix — `ahead or 0` 폴백이 미확인을 ff-확정으로 위장하던 구멍)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=False, ahead=None, behind=2) == (
        "수동 동기 필요", False,
    )


def test_behind_warning_ahead_unknown_reason():
    """_behind_warning 이 ahead=None 을 'ahead 미확인' 사유로 표면화한다."""
    mod = _load_module()
    warning = mod._behind_warning(
        {"behind": 2, "fetched": True, "dirty": False, "ahead": None})
    assert "ahead 미확인" in warning


def test_freshness_decision_behind_dirty_no_pull():
    """behind>0 인데 dirty → '수동 동기 필요' · pull False (표면화만)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=True, ahead=0, behind=2) == (
        "수동 동기 필요", False,
    )


def test_freshness_decision_diverged_no_pull():
    """behind>0 · ahead>0 (diverged) → '수동 동기 필요' · pull False."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=False, ahead=1, behind=2) == (
        "수동 동기 필요", False,
    )


def test_freshness_decision_detached_no_pull():
    """detached HEAD → 'detached' · pull False (재부착은 PM 판단)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=True, dirty=False, ahead=0, behind=5) == (
        "detached", False,
    )


def test_freshness_decision_no_upstream_no_pull():
    """behind None (upstream 미설정/조회불가) → 'upstream 없음' · pull False."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=False, ahead=None, behind=None) == (
        "upstream 없음", False,
    )


def test_freshness_decision_ahead_only_no_pull():
    """behind 0 · ahead>0 → 'ahead-only' · pull False (로컬만 앞섬·push 대기)."""
    mod = _load_module()
    assert mod.freshness_decision(
        fetched=True, detached=False, dirty=False, ahead=2, behind=0) == (
        "ahead-only", False,
    )


# ── _format_freshness (표면화 문자열) ────────────────────────────────────────

def test_format_freshness_behind_ahead():
    mod = _load_module()
    scope = {"fetched": True, "detached": False, "behind": 3, "ahead": 0, "note": None}
    assert mod._format_freshness(scope) == "behind 3 / ahead 0"


def test_format_freshness_latest_with_note():
    mod = _load_module()
    scope = {"fetched": True, "detached": False, "behind": 0, "ahead": 0,
             "note": "ff-pull 동기 완료 (behind 3→0)"}
    assert mod._format_freshness(scope) == "최신 · ff-pull 동기 완료 (behind 3→0)"


def test_format_freshness_fetch_failure_prefixed():
    """fetch 실패면 '⚠ fetch 실패' 접두 + stale local 측정 병기(fail-soft 표면화)."""
    mod = _load_module()
    scope = {"fetched": False, "detached": False, "behind": 2, "ahead": 0, "note": None}
    line = mod._format_freshness(scope)
    assert line.startswith("⚠ fetch 실패")
    assert "behind 2 / ahead 0" in line


def test_format_freshness_detached():
    mod = _load_module()
    scope = {"fetched": True, "detached": True, "behind": None, "ahead": None, "note": None}
    assert mod._format_freshness(scope) == "detached HEAD"


# ── parse_handoff_worktree_branch / reattach_warning ─────────────────────────

def test_parse_handoff_worktree_branch_happy():
    """handoff entry 본문의 worktree 줄에서 branch 추출 (부가 주석이 붙어도)."""
    mod = _load_module()
    body = (
        "## [2026-07-02] handoff | PM 48차 인계\n"
        "- worktree: slot=`work/project_manager_1` · branch=`main` "
        "(릴리즈 ff 후 상태 · 회전 재부착 단서·ADR-0013)\n"
    )
    assert mod.parse_handoff_worktree_branch(body) == "main"


def test_parse_handoff_worktree_branch_unset_is_none():
    """`(미지정)` placeholder 또는 줄 부재 → None (비교 생략)."""
    mod = _load_module()
    body = "- worktree: slot=`work/A_1` · branch=`(미지정)` (회전 재부착 단서·ADR-0013)\n"
    assert mod.parse_handoff_worktree_branch(body) is None
    assert mod.parse_handoff_worktree_branch("본문에 worktree 줄 없음\n") is None
    assert mod.parse_handoff_worktree_branch(None) is None


def test_reattach_warning_mismatch_warns():
    """현 브랜치 ≠ 직전 handoff worktree 브랜치 → 경고 문자열 (자동 checkout 안 함)."""
    mod = _load_module()
    body = "- worktree: slot=`work/pm_1` · branch=`release/v1.0.2` (회전 재부착 단서·ADR-0013)\n"
    warn = mod.reattach_warning("main", body)
    assert warn is not None
    assert "release/v1.0.2" in warn and "main" in warn
    # 자동 checkout 은 하지 않는다는 안내를 담는다(재부착은 PM 판단).
    assert "자동 checkout" in warn


def test_reattach_warning_match_or_missing_is_none():
    """브랜치 일치·직전 브랜치 미상·현 브랜치 미상 → None (경고 생략)."""
    mod = _load_module()
    body = "- worktree: slot=`work/pm_1` · branch=`main` (회전 재부착 단서·ADR-0013)\n"
    assert mod.reattach_warning("main", body) is None            # 일치
    assert mod.reattach_warning("main", "worktree 줄 없음") is None  # 직전 미상
    assert mod.reattach_warning(None, body) is None              # 현 미상


# ── _sync_scope (fetch + 판정 + clean·ff pull) ───────────────────────────────

def _scope_git_fn(dir_str, *, fetch_rc=0, branch="main", detached=False, dirty=False,
                  status_rc=0, ahead=0, behind=0, pull_rc=0, calls=None):
    """단일 scope_dir 를 대상으로 fetch/symbolic-ref/status/rev-list/pull/checkout 를
    canned 응답하는 fake run_git_fn (모든 호출은 `-C <dir_str>` 명시·실 git 미접촉).

    `status_rc != 0` 로 `git status -s` 실패(clean 미확인)를 흉내낼 수 있다(codex must-fix ②).
    """
    def _fn(args: list[str]) -> tuple[int, str]:
        if calls is not None:
            calls.append(args)
        assert args[:2] == ["-C", dir_str], f"예상치 못한 git 호출: {args}"
        sub = args[2:]
        if sub == ["fetch", "origin"]:
            return (fetch_rc, "" if fetch_rc == 0 else "fatal: could not read from remote\n")
        if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
            return (1, "") if detached else (0, f"refs/heads/{branch}\n")
        if sub == ["status", "-s"]:
            if status_rc != 0:
                return (status_rc, "fatal: not a git repository\n")
            return (0, " M x\n" if dirty else "")
        if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return (0, f"{ahead}\t{behind}\n")
        if sub[:1] == ["pull"]:
            return (pull_rc, "")
        if sub[:1] == ["checkout"]:
            return (0, "")
        raise AssertionError(f"예상치 못한 git 호출: {args}")
    return _fn


def test_sync_scope_fetch_failure_behind_no_pull(tmp_path):
    """codex must-fix ①: fetch 실패 + behind>0(clean·ff) → **pull 미실행** · 경고 표면화.

    fetch 실패면 behind/ahead 는 stale 원격 데이터라 그 위에서 자동 pull 하면 안 된다.
    """
    mod = _load_module()
    d = tmp_path / "repo"
    calls: list[list[str]] = []
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), fetch_rc=1, behind=3, ahead=0, dirty=False, calls=calls))
    scope = inst._sync_scope("② PM 홈", d)
    assert scope["fetched"] is False
    assert scope["state"] == "수동 동기 필요"
    assert scope["pulled"] is False
    # pull 은 절대 호출되지 않는다(stale 원격 위 자동 동기 금지).
    assert not any(a[2:3] == ["pull"] for a in calls)
    # 경고 표면화 — behind 유지 + 차단 사유(fetch 실패).
    assert scope["behind"] == 3
    assert scope["note"] and "fetch 실패" in scope["note"]


def test_sync_scope_status_failure_behind_no_pull(tmp_path):
    """codex must-fix ②: `git status` 실패 + behind>0(ff) → **pull 미실행** · 경고 표면화.

    status 조회 실패면 clean 을 증명 못 하므로 자동 pull 불가(fail-soft — abort 아님·pull 만 차단).
    """
    mod = _load_module()
    d = tmp_path / "repo"
    calls: list[list[str]] = []
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), status_rc=128, behind=2, ahead=0, calls=calls))
    scope = inst._sync_scope("① worktree", d)
    assert scope["dirty"] is None  # clean 미확인(tri-state).
    assert scope["state"] == "수동 동기 필요"
    assert scope["pulled"] is False
    # pull 은 절대 호출되지 않는다(clean 미증명).
    assert not any(a[2:3] == ["pull"] for a in calls)
    # 경고 표면화 — behind 유지 + 차단 사유(status 미확인).
    assert scope["behind"] == 2
    assert scope["note"] and "status 미확인" in scope["note"]


def test_collect_scope_git_tag_collision_yields_pure_branch(tmp_path):
    """T-0381: 동명 태그 존재(full ref `refs/heads/v1.3.0`) → 표시 브랜치는 순수명 `v1.3.0`.

    `symbolic-ref --short HEAD` 는 브랜치와 같은 이름의 태그가 있으면(릴리즈가 `v1.3.0` 브랜치를
    그대로 `v1.3.0` 태그로 찍은 경우) 모호성 회피로 `heads/v1.3.0` 을 줘 표시를 오염시켰다(PM 76
    실측). full ref(`symbolic-ref HEAD`)는 태그 존재와 무관하게 항상 `refs/heads/<정확한 이름>` 이라
    `refs/heads/` 접두만 정확히 벗기면 순수 브랜치명이 된다 (T-0377 계보·클래스 마감).
    """
    mod = _load_module()
    d = tmp_path / "repo"
    calls: list[list[str]] = []
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), branch="v1.3.0", behind=0, ahead=0, dirty=False, calls=calls))
    scope = inst._sync_scope("① worktree", d)
    # full ref → `refs/heads/` 접두 정확 제거 → 순수명(`heads/v1.3.0` 오염 아님).
    assert scope["branch"] == "v1.3.0"
    assert scope["detached"] is False
    # `--short`(모호성 접두 오염원) 는 절대 호출하지 않는다 — full ref(`symbolic-ref HEAD`)로 전환됐다.
    assert ["-C", str(d), "symbolic-ref", "HEAD"] in calls
    assert not any("--short" in a for a in calls)


# ── _default_run_git 네트워크 timeout → fail-soft (reviewer must-fix ③) ────────

def test_default_run_git_fetch_timeout_is_failsoft(tmp_path, monkeypatch):
    """reviewer must-fix ③: fetch 가 timeout(원격 무응답)이면 rc≠0 로 흡수 — hang·abort 없음.

    present-but-unresponsive 원격(VPN 미접속·captive portal)이 OS TCP 타임아웃(수 분)까지
    세션 시작을 막던 hang 을 GIT_NETWORK_TIMEOUT 로 끊고 fail-soft(`fetched=False`) 경로로 흡수.
    """
    mod = _load_module()
    inst = mod.PmBootstrap()  # 기본 러너(_default_run_git) 사용.

    captured: dict = {}

    def _raise_timeout(*a, **k):
        captured["timeout"] = k.get("timeout")
        raise subprocess.TimeoutExpired(cmd=a[0] if a else k.get("args"), timeout=k.get("timeout"))

    monkeypatch.setattr(mod.subprocess, "run", _raise_timeout)
    rc, out = inst._default_run_git(["-C", str(tmp_path), "fetch", "origin"])
    # 네트워크 계열엔 timeout 이 실제로 전달된다(무-timeout hang 재발 방지).
    assert captured["timeout"] == mod.GIT_NETWORK_TIMEOUT
    # fail-soft — SystemExit/TimeoutExpired 전파 아님·rc≠0 로 흡수.
    assert rc != 0
    assert "timeout" in out.lower()


def test_default_run_git_local_command_no_timeout(monkeypatch):
    """로컬 git(status·log 등)엔 timeout 을 걸지 않는다(=None·현행 무변경·정상 완주 보존)."""
    mod = _load_module()
    inst = mod.PmBootstrap()

    captured: dict = {}

    class _R:
        returncode = 0
        stdout = "main\n"
        stderr = ""

    def _fake_run(*a, **k):
        captured["timeout"] = k.get("timeout")
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)
    rc, _out = inst._default_run_git(["status", "--short"])
    assert rc == 0
    assert captured["timeout"] is None  # 로컬 git 은 timeout 미부여.


def test_sync_scope_behind_clean_ff_performs_pull(tmp_path):
    """② clean+ff → `git pull --ff-only` 수행 · behind→0 · pulled=True (DoD ②)."""
    mod = _load_module()
    d = tmp_path / "repo"
    calls: list[list[str]] = []
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), behind=3, ahead=0, dirty=False, calls=calls))
    scope = inst._sync_scope("① worktree", d)
    assert scope["state"] == "동기"
    assert scope["pulled"] is True
    assert scope["behind"] == 0
    assert scope["note"] and "ff-pull 동기 완료" in scope["note"]
    # pull --ff-only 이 실제 호출됨.
    assert ["-C", str(d), "pull", "--ff-only"] in calls


def test_sync_scope_behind_dirty_surfaces_no_pull(tmp_path):
    """① behind 표면화 + dirty → 경고만 · pull 없음 (DoD ①·③)."""
    mod = _load_module()
    d = tmp_path / "repo"
    calls: list[list[str]] = []
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), behind=2, ahead=0, dirty=True, calls=calls))
    scope = inst._sync_scope("① worktree", d)
    assert scope["state"] == "수동 동기 필요"
    assert scope["pulled"] is False
    assert scope["behind"] == 2  # behind 표면화 유지(0 으로 덮이지 않음).
    assert scope["note"] and "수동 동기 필요" in scope["note"]
    # pull 은 호출되지 않는다.
    assert not any(a[2:3] == ["pull"] for a in calls)


def test_sync_scope_diverged_surfaces_no_pull(tmp_path):
    """diverged(ahead>0 & behind>0) → 경고만 · pull 없음 (DoD ③)."""
    mod = _load_module()
    d = tmp_path / "repo"
    calls: list[list[str]] = []
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), behind=2, ahead=1, dirty=False, calls=calls))
    scope = inst._sync_scope("① worktree", d)
    assert scope["state"] == "수동 동기 필요"
    assert scope["pulled"] is False
    assert "diverged" in scope["note"]
    assert not any(a[2:3] == ["pull"] for a in calls)


def test_sync_scope_fetch_failure_is_failsoft(tmp_path):
    """④ fetch 실패 → fetched=False · abort 없이 scope dict 반환(현행 측정 계속)."""
    mod = _load_module()
    d = tmp_path / "repo"
    inst = mod.PmBootstrap(
        run_git_fn=_scope_git_fn(str(d), fetch_rc=1, behind=0, ahead=0, dirty=False))
    scope = inst._sync_scope("② PM 홈", d)  # SystemExit/예외 던지면 실패.
    assert scope["fetched"] is False
    assert scope["pulled"] is False
    # 로컬 측정은 계속돼 상태가 채워진다(behind 0 → 최신).
    assert scope["state"] == "최신"


# ── _sync_board_submodule (branch 유지 pull·detached 회피) ────────────────────

def test_sync_board_submodule_absent_is_none(tmp_path):
    """board 미분리(디렉토리 부재) → None (skip·git 미호출)."""
    mod = _load_module()
    inst = mod.PmBootstrap(
        run_git_fn=lambda a: (_ for _ in ()).throw(AssertionError("board git 미호출 기대")),
        board_dir=tmp_path / "board")
    assert inst._sync_board_submodule() is None


def test_sync_board_submodule_branch_preserving_pull(tmp_path):
    """clean+ff → `checkout <branch> && pull --ff-only` (branch 유지·`submodule update` 아님)."""
    mod = _load_module()
    board_dir = tmp_path / "board"
    board_dir.mkdir()
    calls: list[list[str]] = []

    def _fn(args: list[str]) -> tuple[int, str]:
        calls.append(args)
        assert args[:2] == ["-C", str(board_dir)]
        sub = args[2:]
        if sub == ["fetch", "origin"]:
            return (0, "")
        if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
            return (0, "refs/heads/main\n")
        if sub == ["status", "-s"]:
            return (0, "")  # clean
        if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
            return (0, "0\t2\n")  # behind 2 · ahead 0 → clean+ff
        if sub == ["checkout", "main"]:
            return (0, "")
        if sub == ["pull", "--ff-only"]:
            return (0, "")
        raise AssertionError(f"예상치 못한 board git 호출: {args}")

    inst = mod.PmBootstrap(run_git_fn=_fn, board_dir=board_dir)
    scope = inst._sync_board_submodule()
    assert scope["pulled"] is True
    assert scope["behind"] == 0
    # branch 유지 동기 — checkout main + pull --ff-only.
    assert ["-C", str(board_dir), "checkout", "main"] in calls
    assert ["-C", str(board_dir), "pull", "--ff-only"] in calls
    # `git submodule update` 는 절대 호출하지 않는다(detached HEAD → T-0203 sentinel 회피).
    # 서브커맨드 토큰(`-C <dir>` 뒤)만 검사한다 — tmp_path 경로에 'submodule' 이 섞여도 오탐 0.
    subcommands = [a[2] for a in calls if len(a) >= 3]
    assert "submodule" not in subcommands


# ── run() 통합: freshness surface + ff-pull + json 필드 + 재부착 경고 ──────────

def _make_freshness_bootstrap(mod, tmp_path, *, git_fn, log_text=_LOG_TEXT):
    """REPO scope freshness 를 git_fn 으로 제어하는 run() 통합 픽스처 (board 미분리·솔로)."""
    log_file = tmp_path / "current.md"
    log_file.write_text(log_text, encoding="utf-8")
    pm_state_file = tmp_path / "pm_state.md"
    pm_state_file.write_text(_PM_STATE_TEXT, encoding="utf-8")
    areas_file = tmp_path / "areas.md"
    board_dir = tmp_path / "board"  # 미생성 → board rider None(솔로)
    return mod.PmBootstrap(
        run_board_fn=lambda a: (0, "✓ no lint issues\n") if a[:1] == ["lint"]
        else (0, "  [open   ] T-0001  x  pm  tag\n"),
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=git_fn,
        log_file=log_file, areas_file=areas_file, board_dir=board_dir,
        pm_state_file=pm_state_file,
    )


def _repo_scope_git_fn(repo_dir, *, behind, ahead=0, dirty=False, pull_rc=0):
    """②(REPO) scope freshness + `_collect_git` 응답을 함께 dispatch 하는 run() 용 git_fn."""
    def _fn(args: list[str]) -> tuple[int, str]:
        if args[:2] == ["-C", repo_dir]:
            sub = args[2:]
            if sub == ["fetch", "origin"]:
                return (0, "")
            if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
                return (0, "refs/heads/main\n")
            if sub == ["status", "-s"]:
                return (0, " M x\n" if dirty else "")
            if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
                return (0, f"{ahead}\t{behind}\n")
            if sub == ["pull", "--ff-only"]:
                return (pull_rc, "")
            return (0, "")
        # _collect_git (worktree cwd=REPO in solo) — branch/commits/status.
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "main\n")
        if args[:2] == ["log", "--oneline"]:
            return (0, "abc123 subj\n")
        return (0, "")
    return _fn


def test_run_surfaces_freshness_and_ff_pull(tmp_path, capsys):
    """run() Git 절에 freshness 줄 + clean·ff 자동 ff-pull 결과가 표면화된다 (DoD ①·②)."""
    mod = _load_module()
    git_fn = _repo_scope_git_fn(str(mod.REPO), behind=3, ahead=0, dirty=False)
    inst = _make_freshness_bootstrap(mod, tmp_path, git_fn=git_fn)
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "freshness (② PM 홈)" in out
    assert "ff-pull 동기 완료" in out


def test_run_freshness_json_field(tmp_path, capsys):
    """--json 출력의 git.freshness 에 scope 목록(pulled·label)이 실린다 (DoD --json 필드)."""
    import json as _json
    mod = _load_module()
    git_fn = _repo_scope_git_fn(str(mod.REPO), behind=2, ahead=0, dirty=False)
    inst = _make_freshness_bootstrap(mod, tmp_path, git_fn=git_fn)
    assert inst.run(output_json=True) == 0
    data = _json.loads(capsys.readouterr().out)
    freshness = data["git"]["freshness"]
    assert isinstance(freshness, list) and len(freshness) == 1  # 솔로(②=①) → 1 scope
    assert freshness[0]["label"] == "② PM 홈"
    assert freshness[0]["pulled"] is True
    assert freshness[0]["behind"] == 0


def test_run_freshness_fetch_failure_failsoft_continues(tmp_path, capsys):
    """④ fetch 실패여도 부트스트랩은 현행 dump 를 정상 출력하고 rc 0 (fail-soft)."""
    mod = _load_module()
    repo_dir = str(mod.REPO)

    def _fn(args: list[str]) -> tuple[int, str]:
        if args[:2] == ["-C", repo_dir]:
            sub = args[2:]
            if sub == ["fetch", "origin"]:
                return (1, "fatal: could not read from remote repository\n")
            if sub == ["symbolic-ref", "HEAD"]:  # full ref (T-0377).
                return (0, "refs/heads/main\n")
            if sub == ["status", "-s"]:
                return (0, "")
            if sub == ["rev-list", "--left-right", "--count", "HEAD...@{u}"]:
                return (0, "0\t0\n")
            return (0, "")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return (0, "main\n")
        if args[:2] == ["log", "--oneline"]:
            return (0, "abc123 subj\n")
        return (0, "")

    inst = _make_freshness_bootstrap(mod, tmp_path, git_fn=_fn)
    assert inst.run() == 0  # abort 안 함.
    out = capsys.readouterr().out
    assert "### Git" in out
    assert "⚠ fetch 실패" in out  # 경고 줄은 표면화되되 현행 출력 계속.


def test_run_reattach_warning_on_branch_mismatch(tmp_path, capsys):
    """현 브랜치(main) ≠ 직전 handoff worktree 브랜치(feature-x) → 재부착 경고 줄 (DoD 재부착)."""
    mod = _load_module()
    log_text = (
        "# Project Log\n\n"
        "## [2026-07-02] handoff | PM 48차 인계\n"
        "- worktree: slot=`work/project_manager_1` · branch=`feature-x` "
        "(회전 재부착 단서·ADR-0013)\n"
    )
    git_fn = _repo_scope_git_fn(str(mod.REPO), behind=0, ahead=0, dirty=False)
    inst = _make_freshness_bootstrap(mod, tmp_path, git_fn=git_fn, log_text=log_text)
    assert inst.run() == 0
    out = capsys.readouterr().out
    assert "worktree 브랜치 불일치" in out
    assert "feature-x" in out


# ── positional repo 흡수 — rewriter↔CLI 정합 (T-0247·ADR-0043) ────────────────

# ── resolve_repo_arg (순수 정합 함수) ────────────────────────────────────────

def test_resolve_repo_arg_both_none_is_none():
    """positional·--repo 둘 다 미지정 → None (무인자 자동바인딩 경로 보존·회귀 0)."""
    mod = _load_module()
    assert mod.resolve_repo_arg(None, None) is None


def test_resolve_repo_arg_positional_only():
    """positional 만 주면 그 값 (rewriter 산출 `/pm-bootstrap <repo> --slot N` 경로)."""
    mod = _load_module()
    assert mod.resolve_repo_arg("myrepo", None) == "myrepo"


def test_resolve_repo_arg_flag_only():
    """`--repo` 만 주면 그 값 (기존 옵션 경로 무변경)."""
    mod = _load_module()
    assert mod.resolve_repo_arg(None, "myrepo") == "myrepo"


def test_resolve_repo_arg_both_equal():
    """positional == --repo → 그 값 (일치는 허용·alias 관계)."""
    mod = _load_module()
    assert mod.resolve_repo_arg("myrepo", "myrepo") == "myrepo"


def test_resolve_repo_arg_mismatch_raises():
    """positional != --repo → ValueError(fail-loud·추측 금지). 두 값이 메시지에 드러난다."""
    mod = _load_module()
    with pytest.raises(ValueError) as excinfo:
        mod.resolve_repo_arg("alpha", "beta")
    msg = str(excinfo.value)
    assert "alpha" in msg and "beta" in msg


# ── build_parser positional 흡수 (rewriter 산출 parse_args 수용) ──────────────

def test_parse_args_accepts_positional_repo_with_slot():
    """DoD ①: rewriter 형태 `<repo> --slot N` 이 raw CLI parse_args 를 통과한다."""
    mod = _load_module()
    args = mod.build_parser().parse_args(["myrepo", "--slot", "2"])
    assert args.repo_positional == "myrepo"
    assert args.repo is None  # --repo 미지정 — 정합은 resolve_repo_arg 가 접는다.
    assert args.slot == 2
    # main() 이 부르는 정합 헬퍼로 접으면 단일 repo 값이 된다.
    assert mod.resolve_repo_arg(args.repo_positional, args.repo) == "myrepo"


def test_parse_args_rewriter_output_roundtrips():
    """DoD ①(정합 단언): handoff rewriter 실산출을 그대로 raw CLI 가 파싱한다.

    pm_handoff `_inject_slot_into_template` 가 bare `/pm-bootstrap` 을
    `/pm-bootstrap <repo> --slot N` 로 치환한 산출(pm_handoff.py:912 경로)을 재사용해,
    트리거 토큰을 벗긴 나머지를 pm_bootstrap `build_parser` 가 수용하는지 못박는다
    (rewriter↔CLI drift 가드).
    """
    mod = _load_module()
    handoff = _load_pm_handoff()
    injected = handoff._inject_slot_into_template(
        handoff._BARE_BOOTSTRAP_TRIGGER, "work/myrepo_3"
    )
    # 산출: `/pm-bootstrap myrepo --slot 3` — 트리거 토큰(첫 토큰)을 벗긴 나머지가 raw CLI argv.
    tokens = injected.split()
    assert tokens[0] == handoff._BARE_BOOTSTRAP_TRIGGER
    cli_args = tokens[1:]
    args = mod.build_parser().parse_args(cli_args)
    assert mod.resolve_repo_arg(args.repo_positional, args.repo) == "myrepo"
    assert args.slot == 3


def test_parse_args_flag_repo_path_unchanged():
    """무회귀: 기존 `--repo <repo> --slot N` 경로는 그대로 동작(positional 미지정)."""
    mod = _load_module()
    args = mod.build_parser().parse_args(["--repo", "myrepo", "--slot", "2"])
    assert args.repo_positional is None
    assert args.repo == "myrepo"
    assert args.slot == 2
    assert mod.resolve_repo_arg(args.repo_positional, args.repo) == "myrepo"


def test_parse_args_bare_no_arg_path_unchanged():
    """무회귀: 무인자(솔로) 호출은 positional·--repo·--slot 전부 None."""
    mod = _load_module()
    args = mod.build_parser().parse_args([])
    assert args.repo_positional is None
    assert args.repo is None
    assert args.slot is None
    assert mod.resolve_repo_arg(args.repo_positional, args.repo) is None


def test_parse_args_positional_matches_flag_ok():
    """positional == --repo(같은 값)면 parse 통과·정합도 그 값 (alias 일치 허용)."""
    mod = _load_module()
    args = mod.build_parser().parse_args(["myrepo", "--repo", "myrepo"])
    assert mod.resolve_repo_arg(args.repo_positional, args.repo) == "myrepo"


# ── main() CLI 레벨 fail-loud (positional↔--repo 불일치) ──────────────────────

def test_main_positional_repo_flag_mismatch_fails_loud(capsys):
    """DoD: positional 과 --repo 를 다른 값으로 주면 main() 이 fail-loud(SystemExit·비-0).

    argparse `error()` 는 SystemExit(2) 를 던지고 usage/메시지를 stderr 로 낸다. 정합 실패가
    자동바인딩 fs 접근(_resolve_session_slot) 전에 걸리므로 hermetic 하다(실 fs 미접촉).
    """
    mod = _load_module()
    with pytest.raises(SystemExit) as excinfo:
        mod.main(["alpha", "--repo", "beta"])
    assert excinfo.value.code != 0
    err = capsys.readouterr().err
    assert "alpha" in err and "beta" in err
