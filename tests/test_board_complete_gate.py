"""board complete-gate 테스트 (T-0104 · ADR-0023 · T-0596).

두 축을 담는다.

**축 A — 옛 status-mention 경고 제거 박제 (T-0104 · ADR-0023)**: status.md 는
judgment-only(모듈 *판정*)로 재정의돼 ticket ID 를 담지 않으므로(ADR-0023), "does not
mention {tid} — affected module row" 경고는 거의 모든 complete 에서 무의미하게 발화하던
노이즈였다. 이 축은 두 계약을 동시에 단언한다:

  (a) status.md 가 ticket id 를 언급하지 *않아도* 그 경고 문구가 stderr 에 안 나옴
      (제거 회귀 박제 — 경고를 되살리면 fail).
  (b) §1 log/current.md mention gate 는 **여전히 동작**한다 — log 에 ticket id 가 없으면
      blocking problem 을 돌려준다(§1·§2 불변 회귀 보호).

**축 B — DoD 부기 게이트 (T-0596)**: 지금의 §3 은 `## 완료 조건` 체크박스 검사다(옛 §3
status-mention 경고와 무관 — 그건 제거됐고 번호만 비었다). 미체크 `- [ ]` 가 남은 채 done 이
되면 그 항목은 보드에서 증발하므로(실사고: 라이브 probe DoD 소멸), 통과 형식을 둘로 못박는다:
`- [x] <원문>`(했다) 또는 `- [>] <원문> (이월: <사유·귀속>)`(안 했고 사유·귀속을 남겼다).
소급 검사는 없다 — 이미 done 인 티켓은 보지 않는다.

**hermetic**: board.py 모듈 전역(STATUS_FILE·LOG_FILE·REPO 등)은 import 시점에 실 repo
절대경로로 굳는다 — tmp 프로젝트로 재지정해 실 루트를 절대 건드리지 않는다
(test_board_concurrency.py 의 `_load_board_bound` 패턴 동류).
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 제거된 옛 status-mention 경고의 시그니처 문구 — stderr 에 나오면 경고가 살아있다는 뜻.
WARNING_FRAGMENTS = ("does not mention", "affected module row")


def _load_board_bound(proj: Path):
    """board.py 를 새로 로드하고 status/log 경로 전역을 `proj` tmp 프로젝트로 재바인딩한다.

    import 시점에 굳은 실 REPO 경로를 tmp 로 덮어써 complete-gate 가 tmp status.md/
    log/current.md 만 보도록 한다(실 루트 불간섭).
    """
    spec = importlib.util.spec_from_file_location("board_cgate", TOOLS / "board.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    wiki = proj / ".project_manager" / "wiki"
    mod.STATUS_FILE = wiki / "status.md"
    mod.LOG_FILE = wiki / "log" / "current.md"
    return mod


class _Args:
    """argparse.Namespace 대용 — _complete_gate 인자 컨테이너."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _gate_args(**overrides):
    """기본 통과 조합 — log gate·regression gate 모두 만족. 케이스별로 덮어쓴다."""
    defaults = dict(allow_missing_log=True, tests_pass=True, allow_untested=False)
    defaults.update(overrides)
    return _Args(**defaults)


@pytest.fixture
def proj(tmp_path):
    """tmp 프로젝트 골격 — wiki/ + wiki/log/."""
    p = tmp_path / "proj"
    (p / ".project_manager" / "wiki" / "log").mkdir(parents=True)
    return p


@pytest.fixture
def board(proj):
    return _load_board_bound(proj)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════
# (a) §3 status-mention 경고 제거 — status.md 에 ticket 없어도 경고 무발화
# ════════════════════════════════════════════════════════════════════════

def test_status_mention_warning_gone_when_status_lacks_ticket(board, capsys):
    """status.md 가 ticket id 를 언급하지 않아도 §3 경고가 stderr 에 안 나온다.

    judgment-only status.md(ADR-0023)는 ticket id 를 담지 않는다 — 옛 §3 경고를 되살리면
    이 단언이 fail 한다(제거 회귀 박제).
    """
    # judgment-only status.md — ticket id 를 전혀 담지 않는 모듈 판정 표.
    _write(board.STATUS_FILE, "# Status\n\n| 모듈 | 판정 |\n|---|---|\n| core | OK |\n")

    tid = "T-0104"
    problems = board._complete_gate(tid, _gate_args())

    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, (
            f"제거됐어야 할 §3 status-mention 경고 문구가 stderr 에 발화함: {fragment!r}\n{err!r}")
    # §3 만 제거 — 기본 통과 인자에선 blocking problem 도 없어야 한다(§1·§2 만족).
    assert problems == [], f"기대 외 blocking problem: {problems}"


def test_status_mention_warning_gone_even_without_status_file(board, capsys):
    """status.md 가 *아예 없어도* §3 경고가 안 나온다(파일 부재 경로도 제거 확인)."""
    assert not board.STATUS_FILE.exists()

    problems = board._complete_gate("T-0104", _gate_args())

    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, f"§3 경고가 status.md 부재 시에도 발화함: {fragment!r}\n{err!r}"
    assert problems == []


# ════════════════════════════════════════════════════════════════════════
# (b) §1 log/current.md mention gate — 여전히 동작(회귀 보호·§1 불변)
# ════════════════════════════════════════════════════════════════════════

def test_log_mention_gate_blocks_when_log_lacks_ticket(board):
    """§1: log/current.md 가 ticket id 를 안 담으면 blocking problem 을 돌려준다(여전히 동작).

    §3 제거가 §1 을 건드리지 않았음을 박제한다.
    """
    _write(board.LOG_FILE, "# Log\n\n다른 작업만 기록됨 — 이 ticket 미언급.\n")

    problems = board._complete_gate(
        "T-0104", _gate_args(allow_missing_log=False))

    assert any("T-0104" in p and "log/current.md" in p for p in problems), (
        f"§1 log-mention gate 가 동작 안 함 — blocking problem 없음: {problems}")


def test_log_mention_gate_passes_when_log_mentions_ticket(board, capsys):
    """§1: log/current.md 가 ticket id 를 담으면 통과(blocking 없음). 옛 경고도 여전히 무발화."""
    _write(board.LOG_FILE, "# Log\n\n- T-0104 완료: 게이트 정합.\n")
    # status.md 는 ticket id 미언급(judgment-only) — 옛 경고가 살아있으면 여기서 발화했을 것.
    _write(board.STATUS_FILE, "# Status\n\n| 모듈 | 판정 |\n|---|---|\n| core | OK |\n")

    problems = board._complete_gate(
        "T-0104", _gate_args(allow_missing_log=False))

    assert problems == [], f"log 가 ticket 을 담는데도 blocking: {problems}"
    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, f"옛 status-mention 경고 재발: {fragment!r}\n{err!r}"


# ════════════════════════════════════════════════════════════════════════
# 축 B — §3 DoD 부기 게이트 (T-0596)
# ════════════════════════════════════════════════════════════════════════

def _dod_body(*items: str, section: str = "## 완료 조건 (Definition of Done)") -> str:
    """DoD 절에 주어진 줄들을 담은 최소 티켓 본문."""
    lines = "\n".join(items)
    return (
        "# T-0596 — 게이트 픽스처\n\n"
        "## 목표\n게이트 판정 입력.\n\n"
        f"{section}\n{lines}\n\n"
        "## 참고\n- 없음\n\n## 메모\n"
    )


def test_dod_unchecked_item_blocks(board):
    """미체크 `- [ ]` 잔존 → blocking problem (false-complete 폐쇄)."""
    body = _dod_body("- [x] 코드", "- [ ] 라이브 probe 실측")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p and "라이브 probe 실측" in p for p in problems), (
        f"미체크 DoD 가 차단되지 않음: {problems}")


def test_dod_all_checked_passes(board):
    """전 항목 `- [x]` → 통과(blocking 0)."""
    body = _dod_body("- [x] 코드", "- [X] 테스트", "  - [x] 들여쓴 하위 항목")

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_deferred_with_reason_passes(board):
    """`- [>] <원문> (이월: <사유·귀속>)` → 통과 (명시 이월만 미체크를 대체한다)."""
    body = _dod_body(
        "- [x] 코드",
        "- [>] 라이브 probe 실측 (이월: 하네스 한도 소진·T-0600 귀속)",
    )

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_deferred_without_reason_blocks(board):
    """사유 없는 `- [>]` → 차단 (이월의 근거가 남지 않으면 이월이 아니다)."""
    body = _dod_body("- [x] 코드", "- [>] 라이브 probe 실측")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("이월 사유 누락" in p for p in problems), (
        f"사유 없는 이월이 통과함: {problems}")


def test_dod_deferred_with_empty_reason_blocks(board):
    """`(이월: )` 처럼 사유가 빈 표기도 차단 — 문법만 흉내낸 통과를 막는다."""
    body = _dod_body("- [>] 라이브 probe 실측 (이월: )")

    assert board._complete_gate("T-0596", _gate_args(), body), "빈 사유 이월이 통과함"


def test_dod_unknown_mark_blocks(board):
    """`- [~]` 같은 알 수 없는 마커도 차단 — 통과 형식은 `[x]`·`[>]` 둘뿐이다(오타로 게이트가 꺼지지 않게)."""
    body = _dod_body("- [~] 애매한 상태")

    problems = board._complete_gate("T-0596", _gate_args(), body)

    assert any("DoD 미체크" in p for p in problems), f"미지 마커가 통과함: {problems}"


@pytest.mark.parametrize("line", ["- [] 빈 브라켓 항목", "* []빈 브라켓·공백 없음"])
def test_dod_empty_bracket_blocks(board, line):
    """`- []`(빈 브라켓)도 차단 — 마커를 1글자로만 보면 체크박스로 인식조차 못 해 통과했다.

    사람 눈에는 미체크인 줄이라, 조용한 통과는 done 이행에서 그 항목을 증발시킨다(fail-open).
    """
    problems = board._complete_gate("T-0596", _gate_args(), _dod_body("- [x] 코드", line))

    assert any("DoD 미체크" in p and "빈 브라켓" in p for p in problems), (
        f"빈 브라켓 항목이 통과함: {problems}")


def test_dod_gate_ignores_checkboxes_outside_dod_section(board):
    """DoD 절 **밖**(메모·참고)의 미체크 체크박스는 판정 대상이 아니다(오탐 0)."""
    body = _dod_body("- [x] 코드") + "\n## 메모\n- [ ] 다음 세션 후보 아이디어\n"

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_gate_ignores_fenced_code_examples(board):
    """DoD 절 안의 코드펜스 *예시* 체크박스는 판정 대상이 아니다(문법 설명이 차단 사유가 되지 않게)."""
    body = _dod_body(
        "- [x] 코드",
        "",
        "```",
        "- [ ] 예시일 뿐(이 줄은 판정 대상 아님)",
        "```",
    )

    assert board._complete_gate("T-0596", _gate_args(), body) == []


def test_dod_section_anchor_ignores_fenced_heading_quotes(board):
    """코드펜스 안에 인용된 `## 완료 조건`·`## …` 헤딩은 절 경계를 흔들지 못한다(양방향 오판).

    펜스를 먼저 걷지 않으면 ① 앞선 코드블록의 인용 헤딩이 절 시작으로 잡혀 실제 DoD 를 못 보고
    ② DoD 절 안의 인용 헤딩이 절 끝으로 잡혀 그 뒤 미체크 항목이 통째로 빠진다.
    """
    # ① 실제 DoD 절 **앞**에 인용된 헤딩 — 가짜 앵커가 이기면 진짜 미체크를 못 본다.
    quoted_before = (
        "# T-0596 — 픽스처\n\n## 목표\n문서에 문법을 인용한다.\n\n"
        "```markdown\n## 완료 조건 (Definition of Done)\n- [x] 인용된 예시\n```\n\n"
        "## 완료 조건 (Definition of Done)\n- [ ] 진짜 미체크 항목\n\n## 참고\n- 없음\n"
    )
    problems = board._complete_gate("T-0596", _gate_args(), quoted_before)
    assert any("진짜 미체크 항목" in p for p in problems), (
        f"펜스 안 인용 헤딩이 절 앵커를 가로챔 — 실제 미체크를 못 봄: {problems}")

    # ② DoD 절 **안**에 인용된 헤딩 — 가짜 절 끝이 이기면 그 뒤 미체크가 빠진다.
    quoted_inside = _dod_body(
        "- [x] 앞 항목",
        "",
        "```markdown",
        "## 참고",
        "```",
        "",
        "- [ ] 펜스 뒤 미체크 항목",
    )
    problems = board._complete_gate("T-0596", _gate_args(), quoted_inside)
    assert any("펜스 뒤 미체크 항목" in p for p in problems), (
        f"펜스 안 인용 헤딩이 절 끝으로 오판돼 뒤쪽 항목이 검사에서 빠짐: {problems}")


def test_dod_gate_detects_all_markdown_bullet_markers(board):
    """`-`·`*`·`+` 세 불릿 전부에서 미체크를 잡는다 — 하나라도 빠지면 그 표기가 조용히 통과(fail-open)."""
    for bullet in ("-", "*", "+"):
        body = _dod_body(f"{bullet} [ ] {bullet} 표기 미체크 항목")

        problems = board._complete_gate("T-0596", _gate_args(), body)

        assert any("DoD 미체크" in p for p in problems), (
            f"{bullet!r} 불릿 체크박스가 판정에서 빠짐: {problems}")


def test_dod_gate_passes_prose_or_missing_section(board):
    """체크박스 없는 산문 DoD·DoD 절 부재(레거시 본문)는 통과 — 게이트는 체크박스를 *요구* 하지 않는다."""
    prose = _dod_body("완료 조건을 산문으로만 적은 옛 티켓.")
    no_section = "# T-0001 — 옛 티켓\n\n## 목표\n옛 형식.\n\n## 참고\n- 없음\n"

    assert board._complete_gate("T-0596", _gate_args(), prose) == []
    assert board._complete_gate("T-0001", _gate_args(), no_section) == []


def test_complete_gate_without_body_skips_dod(board):
    """본문 미제공 호출은 DoD 검사를 건너뛴다(부기 축만 묻는 레거시 호출 무영향)."""
    assert board._complete_gate("T-0596", _gate_args()) == []


# ── cmd_complete 실 경로 — 차단 시 티켓은 claimed/ 에 그대로 남는다 ─────────────
#
# `ticket_finish.py` 는 이 CLI(`board.py complete <id> --tests-pass`)를 그대로 호출하므로
# 마감 도구 경유 경로도 같은 게이트를 탄다(게이트 구현이 cmd_complete 안에 있다).

_TICKET_FRONTMATTER = (
    "---\n"
    "id: {tid}\n"
    "title: 픽스처\n"
    "status: {status}\n"
    "created: '2026-08-08'\n"
    "created_by: t/t_1\n"
    "claimed_by: t/t_1\n"
    "claimed_at: '2026-08-08T00:00:00+00:00'\n"
    "completed_at: null\n"
    "depends_on: []\n"
    "blocks: []\n"
    "touches: []\n"
    "estimate: small\n"
    "tags: []\n"
    "---\n\n"
)


@pytest.fixture
def live_board(tmp_path, monkeypatch):
    """실 파일 이동이 도는 hermetic board — legacy 형상(board-git 비활성·sync no-op)."""
    mod = _load_board_bound(tmp_path / "proj")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(
        mod, "BOARD_LOCK", tmp_path / ".project_manager" / ".local" / "board.lock")
    for status in ("open", "claimed", "blocked", "done"):
        (tmp_path / ".project_manager" / "wiki" / "tickets" / status).mkdir(
            parents=True, exist_ok=True)
    return mod


def _seed_ticket(board, tid: str, body: str, *, status: str = "claimed") -> Path:
    path = (board.tickets_dir() / status / f"{tid}-픽스처.md")
    path.write_text(
        _TICKET_FRONTMATTER.format(tid=tid, status=status) + body, encoding="utf-8")
    return path


def _complete_args(tid: str) -> argparse.Namespace:
    return argparse.Namespace(
        id=tid, tests_pass=True, allow_missing_log=True, allow_untested=False)


def test_cmd_complete_blocks_unchecked_dod_and_keeps_ticket_claimed(
    live_board, capsys
):
    """미체크 DoD → rc=1 이고 티켓은 claimed/ 에 그대로(부분 이동·false-done 없음)."""
    path = _seed_ticket(live_board, "T-9001",
                        _dod_body("- [x] 코드", "- [ ] 라이브 probe 실측"))

    rc = live_board.cmd_complete(_complete_args("T-9001"))

    err = capsys.readouterr().err
    assert rc == 1, "미체크 DoD 인데 complete 가 통과함"
    assert path.exists(), "차단됐는데 티켓이 claimed/ 에서 사라짐"
    assert not list((live_board.tickets_dir() / "done").glob("T-9001*")), \
        "차단됐는데 done/ 으로 이동함"
    assert "sync gate failed" in err and "DoD 미체크" in err


def test_cmd_complete_passes_with_checked_and_deferred_dod(live_board):
    """`[x]` + 사유 붙은 `[>]` 조합 → rc=0 · done/ 이동(정상 마감 경로 불변)."""
    _seed_ticket(live_board, "T-9002", _dod_body(
        "- [x] 코드",
        "- [>] 라이브 probe 실측 (이월: 하네스 한도 소진·T-0600 귀속)",
    ))

    rc = live_board.cmd_complete(_complete_args("T-9002"))

    assert rc == 0
    assert list((live_board.tickets_dir() / "done").glob("T-9002*")), "done/ 이동 안 함"


def test_done_tickets_are_not_retroactively_checked(live_board):
    """기존 done 티켓(미체크 DoD)은 소급 검사 대상이 아니다 — 새 complete 도 그것 때문에 막히지 않는다."""
    legacy = _seed_ticket(live_board, "T-9003",
                          _dod_body("- [ ] 옛 티켓의 미체크 항목"), status="done")
    before = legacy.read_text(encoding="utf-8")
    _seed_ticket(live_board, "T-9004", _dod_body("- [x] 코드"))

    rc = live_board.cmd_complete(_complete_args("T-9004"))

    assert rc == 0, "done/ 의 옛 미체크 DoD 가 무관한 complete 를 막음(소급 검사 발생)"
    assert legacy.exists() and legacy.read_text(encoding="utf-8") == before, \
        "옛 done 티켓이 건드려짐"
