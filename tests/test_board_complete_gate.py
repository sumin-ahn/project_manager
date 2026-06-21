"""board complete-gate 테스트 (T-0104 · ADR-0023).

`_complete_gate` 의 §3 status.md staleness 경고가 **제거**됐는지 박제한다. status.md 는
judgment-only(모듈 *판정*)로 재정의돼 ticket ID 를 담지 않으므로(ADR-0023), "does not
mention {tid} — affected module row" 경고는 거의 모든 complete 에서 무의미하게 발화하던
노이즈였다. 이 테스트는 두 계약을 동시에 단언한다:

  (a) status.md 가 ticket id 를 언급하지 *않아도* 그 §3 경고 문구가 stderr 에 안 나옴
      (제거 회귀 박제 — 경고를 되살리면 fail).
  (b) §1 log/current.md mention gate 는 **여전히 동작**한다 — log 에 ticket id 가 없으면
      blocking problem 을 돌려준다(§1·§2 불변 회귀 보호).

**hermetic**: board.py 모듈 전역(STATUS_FILE·LOG_FILE 등)은 import 시점에 실 repo 절대경로로
굳는다 — tmp 프로젝트로 재지정해 실 루트를 절대 건드리지 않는다(test_board_concurrency.py 의
`_load_board_bound` 패턴 동류).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

# 제거된 §3 경고의 시그니처 문구 — 이 문자열들이 stderr 에 나오면 경고가 살아있다는 뜻.
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
    """§1: log/current.md 가 ticket id 를 담으면 통과(blocking 없음). §3 경고도 여전히 무발화."""
    _write(board.LOG_FILE, "# Log\n\n- T-0104 완료: 게이트 정합.\n")
    # status.md 는 ticket id 미언급(judgment-only) — §3 경고가 살아있으면 여기서 발화했을 것.
    _write(board.STATUS_FILE, "# Status\n\n| 모듈 | 판정 |\n|---|---|\n| core | OK |\n")

    problems = board._complete_gate(
        "T-0104", _gate_args(allow_missing_log=False))

    assert problems == [], f"log 가 ticket 을 담는데도 blocking: {problems}"
    err = capsys.readouterr().err
    for fragment in WARNING_FRAGMENTS:
        assert fragment not in err, f"§3 경고 재발: {fragment!r}\n{err!r}"
