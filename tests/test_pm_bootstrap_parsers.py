"""pm_bootstrap 순수 파서 8종 직접 단위테스트 (T-0026).

지금까지 이 파서들은 `_collect_git()`/`_build_markdown()` 경유 *간접* 으로만 닿았다.
여기서는 8종 전부를 입력 문자열 → 기대 출력으로 **직접** 호출한다 (부작용 0·실 git/도구
비의존). 각 함수: happy-path + 최소 1 edge(빈 문자열·malformed·한글/로캘).

도구는 패키지가 아니므로 importlib 동적 로드 — test_pm_bootstrap_tz / _failsoft 의
`_load_module` 관용구를 그대로 재사용한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
BOOTSTRAP_PY = TOOLS / "pm_bootstrap.py"
BOARD_PY = TOOLS / "board.py"


def _load_module(name: str = "pm_bootstrap"):
    """pm_bootstrap 를 경로 로드한다 (도구는 패키지가 아니므로 importlib)."""
    spec = importlib.util.spec_from_file_location(name, BOOTSTRAP_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_board(name: str = "board"):
    """board 를 경로 로드한다 — grammar 정합 가드용 (`_ticket_prefix` 비교)."""
    spec = importlib.util.spec_from_file_location(name, BOARD_PY)
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
    """경고 줄(✓ 로 시작 안 하는 비-빈 줄)을 세어 'N warnings' 반환."""
    mod = _load_module()
    out = "T-0001: depends_on 누락\nT-0002: thin ticket\nT-0003: wikilink 깨짐\n"
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


def test_parse_lint_result_gate_advisory_only():
    """advisory-only 게이트 출력(차단 0) — 헤더 제외하고 advisory 2 줄만 세어 '2 warnings'."""
    mod = _load_module()
    out = (
        "⚠️  2 lint issue(s) (0 blocking 차단):\n"
        "    [unstable-ref-advice] T-0001: 슬러그 참조 권고\n"
        "    [status-done-accum] T-0002: status drift\n"
    )
    assert mod.parse_lint_result(out) == "2 warnings"


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


# ── run() 통합: 차수 announce + log 본문 dump + 남은작업 surface + 미해소 graceful ──

def _make_hermetic_bootstrap(mod, tmp_path, *, log_text: str, pm_state_text: str | None):
    """board/git/pytest 는 stub, log/pm_state 는 tmp 파일로 격리한 PmBootstrap (실 fs 미접촉).

    pm_state_text=None → pm_state 파일을 두지 않아 *미해소*(graceful placeholder) 경로를 탄다.
    """
    log_file = tmp_path / "current.md"
    log_file.write_text(log_text, encoding="utf-8")
    areas_file = tmp_path / "areas.md"  # 빈(미생성) → 솔로

    kwargs = dict(
        run_board_fn=lambda args: (0, "✓ no lint issues\n") if args[:1] == ["lint"]
        else (0, "  [open   ] T-0001  x  pm  tag\n"),
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("pytest 미호출")),
        run_git_fn=lambda args: (0, "main\n") if args[:2] == ["rev-parse", "--abbrev-ref"]
        else ((0, "abc123 subj\n") if args[:2] == ["log", "--oneline"] else (0, "")),
        log_file=log_file,
        areas_file=areas_file,
    )
    if pm_state_text is not None:
        pm_state_file = tmp_path / "pm_state.md"
        pm_state_file.write_text(pm_state_text, encoding="utf-8")
        kwargs["pm_state_file"] = pm_state_file
    else:
        # 미해소 graceful — 존재하지 않는 경로를 주입해 _collect_handoff_context 가 None 으로 폴백.
        kwargs["pm_state_file"] = tmp_path / "absent_pm_state.md"
    return mod.PmBootstrap(**kwargs)


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
    """pm_state 미해소(부재)면 차수 placeholder + 명시 포인터 — crash 없이 진행."""
    mod = _load_module()
    inst = _make_hermetic_bootstrap(mod, tmp_path, log_text=_LOG_TEXT, pm_state_text=None)
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
