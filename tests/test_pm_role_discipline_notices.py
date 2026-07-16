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
    """T-0197: list 스코핑(--mine/--repo/--slot) vs claim/mutation 행위자 --repo/--slot 구분 안내 (ADR-0057 표기 통일)."""
    text = _text()
    assert "list" in text and "--repo" in text and "--slot" in text


def test_release_procedure_github_release_step_is_required():
    """T-0290: 릴리즈 절차의 GitHub Release 단계가 필수(soft '(gh 인증 시)' 재약화 차단)·완결 확인 有.

    PM 60 v1.1.0 은 이 단계가 soft-optional("(gh 인증 시)…생략")이라 skip 돼 태그만 push·GitHub
    Release 누락됐다. 필수 마커 문구와 완결-확인(`gh release view`) 단계 존재를 못박아 재발을 막는다
    (문서 회귀 가드 — 내용 옳고 그름은 사람 리뷰).
    """
    text = _text()
    assert "gh release create" in text, "릴리즈 절차에 GitHub Release 생성 단계가 없다"
    # 필수 마커(이 단계 고유 문구) — 재약화되면 이 문구가 사라져 fail.
    assert "태그만으론 릴리즈 아님" in text, \
        "GitHub Release 단계가 필수로 명시되지 않았다(soft-optional 재약화 위험·PM 60 재발)"
    # 완결 확인 단계 — 태그 push ≠ 릴리즈, PM 이 Release 객체 존재를 확인해 릴리즈를 닫는다.
    assert "gh release view" in text, "릴리즈 완결 확인(gh release view) 단계가 없다"
