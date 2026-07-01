"""`pm_role.md` 방법론 안내 문구 존재 가드 — T-0196(무티켓 확인) · T-0198(test-less done).

두 티켓 모두 *이미 있는* 기능/규율의 discoverability 갭을 메운다 — 코드가 아니라 방법론
문서(`pm_role.md`)에 안내를 추가하는 게 산출물이므로, "문구가 실제로 있는가"만 기계로
고정한다(내용 자체의 옳고 그름은 사람 리뷰 — 여기선 회귀 성격의 존재 가드).
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PM_ROLE = REPO / ".project_manager" / "wiki" / "pm_role.md"


def _text() -> str:
    return PM_ROLE.read_text(encoding="utf-8")


def test_pm_role_exists():
    assert PM_ROLE.is_file()


def test_unticketed_work_confirmation_notice_present():
    """T-0196: 무티켓 작업 착수 전 사용자 확인 방법론이 §안전가드에 명문화돼 있다."""
    text = _text()
    assert "무티켓 작업 착수 전" in text
    assert "board.py new" in text


def test_allow_untested_test_less_done_notice_present():
    """T-0198: `complete --allow-untested` test-less done 경로가 안내돼 있다."""
    text = _text()
    assert "--allow-untested" in text
    assert "test-less done" in text or "회귀와 무관" in text


def test_list_session_slot_scoping_notice_present():
    """T-0197: list 스코핑(--mine/--session/--slot) vs claim/mutation 행위자 --session 구분 안내."""
    text = _text()
    assert "list" in text and "--session" in text
