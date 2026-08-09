"""pm_handoff.py 핵심 함수 직접 단위테스트 (T-0042).

PM 세션 라이프사이클 자동화의 다른 한 축 — pm_state.md 세션 식별 절의 sliding window
편집(`update_session_window`)과 그 하부 절 추출(`_extract_session_section`)·인계 프롬프트
템플릿 추출(`extract_handoff_prompt_template`)을 직접 검증한다.

`test_handoff_trigger.py` 는 `infer_next_session_num`·대화형 skeleton·실 pm_playbook lean
검증을 덮는다 — 여기선 함수를 직접 호출하는
*절 경계·윈도 경계·앵커 불일치 ValueError·멱등·프롬프트 부재→None* edge 에 집중한다
([[T-0026]] 규율 — non-vacuous·실제 동작 단언). 모두 텍스트 인자라 실 wiki 미접촉.

T-0041 rename 반영 — `_extract_session_section(pm_state_text)` ·
`extract_handoff_prompt_template(pm_playbook_text)`.

도구는 패키지가 아니므로 importlib 동적 로드 (test_handoff_trigger 관용구).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from _pytest_summary import pytest_summary

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_HANDOFF_PY = TOOLS / "pm_handoff.py"


def _load_module(name: str = "pm_handoff"):
    spec = importlib.util.spec_from_file_location(name, PM_HANDOFF_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_tool(name: str):
    """producer-consumer 계약 테스트용 실제 tools 모듈 로더."""
    path = TOOLS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"test_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hf():
    return _load_module()


class _RegisteredTaskPool:
    """task membership만 양성으로 답하는 최소 handoff 테스트 seam."""

    def __init__(self, *, pid=12345):
        self.pid = pid

    def find_task(self, name):
        return type("_T", (), {"name": name, "pid": self.pid})()

    def slots_for_task(self, name):
        return []


# ── pm_state.md 세션 식별 절 fixture (실 형식 — 앵커·entry·포인터 유지) ────────
#
# 실 pm_state.md 의 "## 세션 식별 (현재까지 사용된 이름)" 절 형식을 그대로 모사:
#   entry 줄: "  - **N차** (YYYY-MM-DD · ...): ..."
#   포인터:   "  - 이전 차 (PM N차~M차) = `log/current.md` handoff entry 단일 진실."
#   다음 헤더: "## 진행 중인 의사결정"

_PREAMBLE = "# PM State\n\n"
_NEXT_HEADER = "## 진행 중인 의사결정\n\n표 내용.\n"


def _state(*entries: str, pointer: str = "", trailing_header: bool = True) -> str:
    """세션 식별 절을 가진 pm_state.md 텍스트를 빌드한다."""
    section = "## 세션 식별 (현재까지 사용된 이름)\n\n최근 N 차 (sliding window, 기본 3 차):\n"
    for e in entries:
        section += e
    if pointer:
        section += pointer
    doc = _PREAMBLE + section
    if trailing_header:
        doc += "\n" + _NEXT_HEADER
    return doc


def _entry(num: int, summary: str = "wave 요약") -> str:
    return f"  - **{num}차** (2026-06-1{num % 10} · {summary}): {summary}.\n"


_POINTER_1_3 = "  - 이전 차 (PM 1차~3차) = `log/current.md` handoff entry 단일 진실.\n"


# ── _extract_session_section: 앵커 존재→(text,start,end)·부재→None·경계 ───────

def test_extract_session_section_returns_text_and_offsets(hf):
    """앵커 존재 시 (section_text, start, end) 반환 — section_text 가 앵커로 시작."""
    doc = _state(_entry(4), pointer=_POINTER_1_3)
    result = hf._extract_session_section(doc)
    assert result is not None
    section_text, start, end = result
    assert section_text.startswith("## 세션 식별 (현재까지 사용된 이름)")
    # offset 정합 — doc[start:end] 가 곧 반환된 section_text.
    assert doc[start:end] == section_text
    # start 는 앵커 위치.
    assert doc.find("## 세션 식별 (현재까지 사용된 이름)") == start


def test_extract_session_section_absent_returns_none(hf):
    """앵커가 없으면 None (추측 편집 금지 — 호출 측이 ValueError 로 승격)."""
    assert hf._extract_session_section("# no session anchor here\n") is None


def test_extract_session_section_stops_at_next_header(hf):
    """절 경계는 다음 ## (또는 ###) 헤더 직전까지 — 후속 헤더 내용은 포함 안 함."""
    doc = _state(_entry(4), pointer=_POINTER_1_3)
    section_text, _, _ = hf._extract_session_section(doc)
    # 다음 헤더(진행 중인 의사결정)는 절에 포함되지 않는다.
    assert "진행 중인 의사결정" not in section_text
    # 세션 entry·포인터는 포함.
    assert "**4차**" in section_text
    assert "이전 차 (PM 1차~3차)" in section_text


def test_extract_session_section_extends_to_eof_when_no_next_header(hf):
    """다음 헤더가 없으면 절은 파일 끝까지 확장된다 (말미 케이스)."""
    doc = _state(_entry(4), pointer=_POINTER_1_3, trailing_header=False)
    section_text, _, end = hf._extract_session_section(doc)
    assert end == len(doc)
    assert "**4차**" in section_text


# ── update_session_window: N차 추가 + 가장 오래된 제거 (윈도 경계) ───────────

def test_update_session_window_adds_new_removes_oldest(hf):
    """3차 윈도: 신규 entry 추가 + 가장 오래된(최소 차수) entry 제거."""
    doc = _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3)
    out = hf.update_session_window(
        doc, session_num=7, date_str="2026-06-15", wave_summary="새 wave"
    )
    # 신규 7차 추가.
    assert "**7차**" in out
    # 가장 오래된 4차 제거 (윈도 경계 — 추가하면 1개 밀려난다).
    assert "**4차**" not in out
    # 중간 entry 는 보존.
    assert "**5차**" in out and "**6차**" in out


def test_update_session_window_advances_prev_pointer(hf):
    """오래된 entry 제거 시 '이전 차 (PM N차~M차)' 포인터 끝 범위가 제거된 차수로 확장된다."""
    doc = _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3)
    out = hf.update_session_window(
        doc, session_num=7, date_str="2026-06-15", wave_summary="새 wave"
    )
    # 제거된 4차가 포인터 범위 끝으로 흡수 (1차~3차 → 1차~4차).
    assert "이전 차 (PM 1차~4차)" in out
    assert "이전 차 (PM 1차~3차)" not in out


def test_update_session_window_idempotent_on_same_session(hf):
    """이미 존재하는 session_num 으로 재실행하면 no-op (이중 추가·이중 제거 방지)."""
    doc = _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3)
    out = hf.update_session_window(
        doc, session_num=6, date_str="2026-06-15", wave_summary="중복"
    )
    # 6차가 이미 있으므로 원문 그대로 (entry 추가·제거 없음).
    assert out == doc


def test_update_session_window_missing_anchor_raises(hf):
    """세션 식별 절 앵커가 없으면 ValueError (추측 편집 금지)."""
    with pytest.raises(ValueError):
        hf.update_session_window(
            "# no anchor\n", session_num=7, date_str="2026-06-15", wave_summary="x"
        )


def test_update_session_window_no_existing_entry_raises(hf):
    """marker 없는 임의 state에서 기존 entry가 0개면 종전처럼 ValueError."""
    doc = _state(pointer=_POINTER_1_3)  # entry 없음, 포인터만.
    with pytest.raises(ValueError):
        hf.update_session_window(
            doc, session_num=7, date_str="2026-06-15", wave_summary="x"
        )


def test_update_session_window_initializes_new_task_marker(hf):
    """task 생성 state의 0-session marker는 첫 handoff 때 실제 1차 entry로 교체된다."""
    doc = _state().replace(
        "최근 N 차 (sliding window, 기본 3 차):\n",
        "최근 N 차 (sliding window, 기본 3 차):\n"
        f"{hf.TASK_PM_STATE_EMPTY_MARKER}\n",
    )
    out = hf.update_session_window(
        doc, session_num=1, date_str="2026-07-23", wave_summary="첫 task 세션"
    )
    assert hf.TASK_PM_STATE_EMPTY_MARKER not in out
    assert "**1차** (2026-07-23 · 첫 task 세션)" in out


def test_update_session_window_fills_three_before_evicting_oldest(hf):
    """task 1→2→3차는 세 entry가 공존하고 4차에서만 1차를 제거한다."""
    state = _state().replace(
        "최근 N 차 (sliding window, 기본 3 차):\n",
        "최근 N 차 (sliding window, 기본 3 차):\n"
        f"{hf.TASK_PM_STATE_EMPTY_MARKER}\n",
    )
    observed: list[list[int]] = []
    for session_num in range(1, 5):
        state = hf.update_session_window(
            state,
            session_num=session_num,
            date_str=f"2026-07-2{session_num}",
            wave_summary=f"wave-{session_num}",
        )
        section, _, _ = hf._extract_session_section(state)
        observed.append([
            int(match.group(1).replace("차", ""))
            for match in hf._find_pm_session_entries(section)
        ])

    assert observed == [[1], [1, 2], [1, 2, 3], [2, 3, 4]]


def test_update_session_window_legacy_solo_pointer_is_not_left_in_window(hf):
    """legacy 1차+`이전 차 1차~1차` 템플릿에 2차를 더하면 포인터가 현재 창과 겹치지 않는다.

    이 템플릿은 1차가 window entry 이면서 이전 차 포인터에도 동시에 남아 있었다. eviction
    전에는 이전 차가 없으므로 stale 포인터를 지우고, 4차 eviction 뒤의 종전 포인터 생성은
    그대로 유지한다.
    """
    legacy = _state(
        _entry(1),
        pointer="  - 이전 차 (PM 1차~1차) = `log/current.md` handoff entry 단일 진실.\n",
    )
    second = hf.update_session_window(
        legacy, session_num=2, date_str="2026-07-24", wave_summary="2차 handoff"
    )
    section, _, _ = hf._extract_session_section(second)
    assert [m.group(1) for m in hf._find_pm_session_entries(section)] == ["1차", "2차"]
    assert "이전 차 (PM" not in section

    # B: 3차 충전과 4차 eviction의 기존 sliding-window 동작은 그대로다.
    third = hf.update_session_window(
        second, session_num=3, date_str="2026-07-25", wave_summary="3차 handoff"
    )
    fourth = hf.update_session_window(
        third, session_num=4, date_str="2026-07-26", wave_summary="4차 handoff"
    )
    section, _, _ = hf._extract_session_section(fourth)
    assert [m.group(1) for m in hf._find_pm_session_entries(section)] == ["2차", "3차", "4차"]
    assert "이전 차 (PM 1차~1차)" in section


def test_update_session_window_preserves_outside_section(hf):
    """절 밖 텍스트(preamble·다음 헤더)는 sliding window 편집에 영향받지 않는다."""
    doc = _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3)
    out = hf.update_session_window(
        doc, session_num=7, date_str="2026-06-15", wave_summary="새 wave"
    )
    assert out.startswith(_PREAMBLE)
    assert "진행 중인 의사결정" in out


# ── T-0243: 앵커 fuzzy 매칭 — 공백/괄호/여백 변형 흡수 (finance_dev D3) ─────────
#
# 채택자 pm_state 의 세션 식별 h2 헤더가 canonical 앵커와 미세하게 다르면(괄호 내용·2칸 공백·
# h2 뒤 trailing space) 정확 str.find 이 실패해 ValueError→핸드오프가 9세션 연속 죽었다.
# h2 헤더 정규화 부분일치('#'·공백·괄호 제거 후 '세션식별' 포함)로 이 변형들을 흡수한다.

# 변형 앵커 3종 — 정규화하면 모두 '세션식별…' 로 시작해 부분일치한다. canonical 문자열과
# byte-불일치라 str.find 은 실패하는 형상.
_ANCHOR_VARIANTS = [
    "## 세션 식별 (이번 세션까지 쓴 이름)",    # ① 뒤 괄호절 내용 다름
    "##  세션  식별  (현재까지 사용된 이름)",  # ② 공백 2칸
    "## 세션 식별 (현재까지 사용된 이름)   ",  # ③ h2 뒤 trailing space
]


def _state_with_anchor(anchor_line: str, *entries: str, pointer: str = "") -> str:
    """임의의 세션 식별 h2 헤더 줄로 pm_state 텍스트를 빌드한다 (변형 앵커 테스트용)."""
    section = anchor_line + "\n\n최근 N 차 (sliding window, 기본 3 차):\n"
    for e in entries:
        section += e
    if pointer:
        section += pointer
    return _PREAMBLE + section + "\n" + _NEXT_HEADER


@pytest.mark.parametrize("anchor_line", _ANCHOR_VARIANTS)
def test_extract_session_section_matches_anchor_variants(hf, anchor_line):
    """공백/괄호/여백 변형 앵커도 정규화 부분일치로 절을 추출한다."""
    doc = _state_with_anchor(anchor_line, _entry(4), pointer=_POINTER_1_3)
    result = hf._extract_session_section(doc)
    assert result is not None
    section_text, start, end = result
    # 절은 변형 헤더 줄로 시작하고(offset 정합), 다음 헤더는 포함하지 않는다.
    assert section_text.startswith(anchor_line)
    assert doc[start:end] == section_text
    assert "진행 중인 의사결정" not in section_text
    assert "**4차**" in section_text


@pytest.mark.parametrize("anchor_line", _ANCHOR_VARIANTS)
def test_update_session_window_applies_on_anchor_variants(hf, anchor_line):
    """변형 앵커에서도 sliding window 가 정상 적용된다 (신규 추가·최고령 제거·포인터 전진)."""
    doc = _state_with_anchor(
        anchor_line, _entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3
    )
    out = hf.update_session_window(
        doc, session_num=7, date_str="2026-06-15", wave_summary="새 wave"
    )
    assert "**7차**" in out                 # 신규 추가
    assert "**4차**" not in out              # 최고령 제거
    assert "**5차**" in out and "**6차**" in out
    assert "이전 차 (PM 1차~4차)" in out      # 포인터 전진
    # 변형 앵커 헤더 줄 자체는 편집 대상 아님 — 그대로 보존.
    assert anchor_line in out
    # 절 밖(preamble·다음 헤더)은 무영향.
    assert out.startswith(_PREAMBLE)
    assert "진행 중인 의사결정" in out


# finance_dev 실 pm_state 형상 재현 — 세션 식별 h2 가 canonical 앵커와 미세 변형(2칸 공백 +
# 괄호절 다름)이고 앞뒤로 다른 절들이 있는 실제 채택자 문서 모양. 정확 str.find 은 여기서
# 실패해 9세션 연속 ValueError→핸드오프 사망이었다.
_FINANCE_DEV_PM_STATE = (
    "---\n"
    "title: PM State\n"
    "---\n\n"
    "# PM State — finance_dev\n\n"
    "## 현재 세션 요약\n\n"
    "이번 세션 작업 내용.\n\n"
    "##  세션 식별  (지금까지 쓴 PM 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - **7차** (2026-07-01 · 결제 파이프라인): 결제 파이프라인 정리.\n"
    "  - **8차** (2026-07-03 · 리팩터): 리팩터 wave.\n"
    "  - **9차** (2026-07-05 · 버그픽스): 버그픽스 wave.\n"
    "  - 이전 차 (PM 4차~6차) = `log/current.md` handoff entry 단일 진실.\n\n"
    "## 진행 중인 의사결정\n\n"
    "표 내용.\n"
)


def test_extract_session_section_finance_dev_shape(hf):
    """finance_dev 실 형상(2칸 공백 + 괄호절 다름 h2)에서도 절을 추출한다 (str.find 실패 형상)."""
    assert hf._SESSION_SECTION_ANCHOR not in _FINANCE_DEV_PM_STATE  # 정확 매칭이면 실패했을 형상.
    result = hf._extract_session_section(_FINANCE_DEV_PM_STATE)
    assert result is not None
    section_text, _, _ = result
    assert "**9차**" in section_text
    # 앞 절(현재 세션 요약)·다음 절(진행 중인 의사결정)은 세션 식별 절에 포함되지 않는다.
    assert "현재 세션 요약" not in section_text
    assert "진행 중인 의사결정" not in section_text


def test_update_session_window_finance_dev_shape(hf):
    """finance_dev 실 형상에서 window 정상 적용 — 9세션 연속 수동 우회 회귀를 재현·해소."""
    out = hf.update_session_window(
        _FINANCE_DEV_PM_STATE, session_num=10, date_str="2026-07-06", wave_summary="핸드오프 복구"
    )
    assert "**10차**" in out                  # 신규
    assert "**7차**" not in out                # 최고령 제거
    assert "**8차**" in out and "**9차**" in out
    assert "이전 차 (PM 4차~7차)" in out        # 포인터 전진(6차→7차 흡수)


def test_extract_session_section_prefers_entry_bearing_among_multiple_matches(hf):
    """fuzzy 후보 h2 가 여럿이면 entry(`- **N차**`) 가진 절을 우선한다 (T-0243 reviewer should-fix).

    '## 세션 식별 규칙'(설명-절·entry 없음)이 실제 window 절보다 앞에 있어도 빈 절을
    오선택하지 않는다 — 오선택 시 window 미갱신(fail-soft 스킵)/오염 재발.
    """
    text = (
        "# PM State\n\n"
        "## 세션 식별 규칙\n\n"
        "이름은 <repo>_<N> 로 짓는다 — 설명만 있는 절.\n\n"
        "## 세션 식별 (현재까지 사용된 이름)\n\n"
        "  - **3차** (2026-07-01 · wave): wave.\n\n"
        "## 다음 절\n\n내용.\n"
    )
    result = hf._extract_session_section(text)
    assert result is not None
    section_text, _, _ = result
    assert "**3차**" in section_text            # entry 보유 절을 선택.
    assert "설명만 있는 절" not in section_text   # 앞의 빈 설명-절이 아님.
    # 폴백: 전부 entry 없으면 첫 후보(종전 동작) — None 이 아니어야 함.
    text_no_entry = "## 세션 식별 규칙\n\n설명.\n\n## 딴 절\n\n내용.\n"
    fallback = hf._extract_session_section(text_no_entry)
    assert fallback is not None and "설명." in fallback[0]


# ── extract_handoff_prompt_template: 앵커 추출·부재→None ─────────────────────

_PROMPT_ANCHOR = "## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)"


def test_extract_handoff_prompt_template_extracts_code_block(hf):
    """앵커 절의 코드블록 내용을 추출한다."""
    playbook = (
        "# pm_playbook\n\n"
        f"{_PROMPT_ANCHOR}\n\n"
        "설명 문단.\n\n"
        "```\n프롬프트 본문 줄1\n프롬프트 본문 줄2\n```\n\n"
        "## 다른 절\n다른 내용.\n"
    )
    out = hf.extract_handoff_prompt_template(playbook)
    assert out is not None
    assert "프롬프트 본문 줄1" in out
    assert "프롬프트 본문 줄2" in out
    # 다음 절(다른 절) 내용은 추출 범위 밖.
    assert "다른 내용" not in out


def test_extract_handoff_prompt_template_anchor_absent_returns_none(hf):
    """앵커가 없으면 None."""
    assert hf.extract_handoff_prompt_template("# no anchor\n```\nx\n```\n") is None


def test_extract_handoff_prompt_template_no_code_block_returns_none(hf):
    """앵커는 있으나 코드블록이 없으면 None."""
    playbook = f"# pm_playbook\n\n{_PROMPT_ANCHOR}\n\n코드블록 없는 설명만.\n"
    assert hf.extract_handoff_prompt_template(playbook) is None


# ── build_handoff_prompt_output: 트리거로 축소 (T-0180) ────────────────────────
# 프롬프트는 역할 framing + /pm-bootstrap 트리거만 — 인계 본문(읽기 범위·메타 학습·다음
# intent·회귀/incident) 손-채움은 폐기(부트스트랩이 log entry 에서 dump·T-0179 짝).

# 트리거 형태 fixture — 실 pm_playbook §부트스트랩 프롬프트의 축소 형태와 동형.
_TRIGGER_PLAYBOOK = (
    "# pm_playbook\n\n"
    f"{_PROMPT_ANCHOR}\n\n"
    "설명 문단.\n\n"
    "```\n"
    "당신은 이 프로젝트의 PM 세션입니다.\n"
    "지금 /pm-bootstrap 을 실행하세요.\n"
    "```\n\n"
    "## 다른 절\n"
)


def test_build_handoff_prompt_output_emits_trigger(hf):
    """추출한 트리거 코드블록을 헤더/푸터로 감싸 emit 한다 (PM 차수·날짜·wave 표기)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=42,
        wave_summary="요약",
        date_str="2026-06-28",
    )
    assert "=== 인계 프롬프트 (PM 42차 → 다음 PM 세션) ===" in out
    assert "/pm-bootstrap" in out
    assert "당신은 이 프로젝트의 PM 세션입니다." in out
    assert "2026-06-28" in out and "요약" in out


def test_build_handoff_prompt_output_no_handfill_block(hf):
    """트리거 emit 에는 폐기된 `<핵심 인계 사항>` 손-채움 블록이 없다 (T-0180·중복 제거)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=42,
        wave_summary="요약",
        date_str="2026-06-28",
    )
    # 손-채움 인계 블록과 그 안내 헤더 둘 다 사라졌다 (본문은 log entry/부트스트랩이 이월).
    assert "<핵심 인계 사항>" not in out
    assert "채워 넣을 것" not in out


def test_build_handoff_prompt_output_template_absent_warns(hf):
    """앵커/코드블록 부재 시 fail-soft 경고 문자열 (추측 emit 금지)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text="# no anchor here\n",
        session_num=42,
        wave_summary="요약",
        date_str="2026-06-28",
    )
    assert "[경고]" in out and "직접 복사하라" in out


# ── 멀티-PM 슬롯 주입 (T-0185) — 복사 블록의 bare /pm-bootstrap 에 slot 주입 ─────

def test_build_handoff_prompt_output_injects_worktree_slot(hf):
    """worktree_slot=work/<repo>_<N> 이면 복사 블록에 slot-qualified 트리거 주입 (T-0185).

    멀티-PM 다음 세션이 슬롯을 몰라 fail-loud 하던 갭 보완 — bare `/pm-bootstrap` 부재·
    `/pm-bootstrap <repo> --slot <N>` 존재를 단언한다.
    """
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=42,
        wave_summary="요약",
        date_str="2026-06-28",
        worktree_slot="work/repoA_2",
    )
    assert "/pm-bootstrap repoA --slot 2" in out
    # 복사 블록(템플릿) 내 bare 트리거는 남지 않는다 (뒤 공백/줄바꿈으로 slot-qualified 와 구별).
    template = hf.extract_handoff_prompt_template(_TRIGGER_PLAYBOOK)
    injected = hf._inject_slot_into_template(template, "work/repoA_2")
    assert "/pm-bootstrap " not in injected.replace("/pm-bootstrap repoA", "")
    assert "/pm-bootstrap\n" not in injected


def test_build_handoff_prompt_output_none_slot_keeps_bare(hf):
    """worktree_slot=None(solo) 이면 bare `/pm-bootstrap` 유지 (현행·회귀·T-0185)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=42,
        wave_summary="요약",
        date_str="2026-06-28",
        worktree_slot=None,
    )
    assert "/pm-bootstrap" in out
    assert "--slot" not in out


@pytest.mark.parametrize("bad_slot", ["weird", "work/nounderscoreslot", "work/repo_x", ""])
def test_build_handoff_prompt_output_malformed_slot_falls_back_bare(hf, bad_slot):
    """비정형 slot(prefix 없음·underscore 없음·N 비정수·빈문자)이면 bare 폴백·크래시 없음 (T-0185)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=42,
        wave_summary="요약",
        date_str="2026-06-28",
        worktree_slot=bad_slot,
    )
    assert "/pm-bootstrap" in out
    assert "--slot" not in out


# ── task 모드 트리거 (T-0394) — 4경로 트리거 문면 (task-only / task+slot / slot / solo) ──
# task 세션 연속성 앵커는 task(로그 태그 `task:<이름>`·per-task pm_state)라 인계 트리거가
# `--task <이름>` 을 실어야 다음 세션이 task resume 으로 재개한다. 슬롯 재부착만 안내하면(구버전)
# 슬롯 모드로 재개돼 차수 추론·pm_state 포인터·clean resume 링크가 끊긴다(PM 78 실측).


def test_build_handoff_prompt_output_task_only_emits_task_trigger(hf):
    """task-only(슬롯 미동반)면 `/pm-bootstrap --task <이름>` 트리거 (T-0394).

    ADR-0068 W3(T-0399)가 진입 시 보유 슬롯 집합을 자동 수령하므로 트리거는 슬롯을 열거하지
    않는다 — task 앵커만 실어 clean task resume 을 보존한다.
    """
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=78,
        wave_summary="요약",
        date_str="2026-07-20",
        worktree_slot=None,
        task="mytask",
    )
    assert "/pm-bootstrap --task mytask" in out
    # 슬롯 열거 없음(task-only) · bare 잔존 없음.
    assert "--slot" not in out
    template = hf.extract_handoff_prompt_template(_TRIGGER_PLAYBOOK)
    injected = hf._inject_task_into_template(template, "mytask")
    assert "/pm-bootstrap\n" not in injected
    assert "/pm-bootstrap " not in injected.replace("/pm-bootstrap --task", "")


def test_build_handoff_prompt_output_task_with_slot_stays_task_only(hf):
    """task+slot 동반이어도 트리거는 task-only(`--task <이름>`) (T-0394·task 우선·직교 앵커).

    task 는 slot 과 직교하고 진입 시 보유 슬롯이 자동 수령되므로, 슬롯이 동반돼도 트리거엔
    슬롯을 싣지 않는다(트리거 = 재개 명령 1:1·ADR-0035). slot-qualified 재부착 안내가 task 를
    덮지 않게 가드.
    """
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=78,
        wave_summary="요약",
        date_str="2026-07-20",
        worktree_slot="work/repoA_2",
        task="mytask",
    )
    assert "/pm-bootstrap --task mytask" in out
    # slot 이 동반돼도 슬롯 재부착 문면(`repoA --slot 2`)은 트리거에 안 나온다.
    assert "--slot" not in out
    assert "repoA" not in out


def test_build_handoff_prompt_output_slot_only_unchanged_by_task_feature(hf):
    """슬롯-only(task None)는 T-0394 도입 후에도 slot-qualified 트리거 100% 불변 (회귀)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=78,
        wave_summary="요약",
        date_str="2026-07-20",
        worktree_slot="work/repoA_2",
        task=None,
    )
    assert "/pm-bootstrap repoA --slot 2" in out
    assert "--task" not in out


def test_build_handoff_prompt_output_solo_unchanged_by_task_feature(hf):
    """솔로(slot None·task None)는 T-0394 도입 후에도 bare `/pm-bootstrap` 100% 불변 (회귀)."""
    out = hf.build_handoff_prompt_output(
        pm_playbook_text=_TRIGGER_PLAYBOOK,
        session_num=78,
        wave_summary="요약",
        date_str="2026-07-20",
        worktree_slot=None,
        task=None,
    )
    assert "/pm-bootstrap" in out
    assert "--slot" not in out
    assert "--task" not in out


def test_run_task_mode_emits_task_trigger(hf, tmp_path, capsys, monkeypatch):
    """run(task=...) 이 [5/7] 에 task-only 트리거를 emit 한다 (T-0394·호출부 배선).

    명시 pm_state 주입(hermetic) 없이 --task 를 run 에 주면 build_handoff_prompt_output 에
    task 가 전달돼 `/pm-bootstrap --task <이름>` 이 나온다 — task_mode 배선을 가드.
    """
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")
    log_file = tmp_path / "current.md"
    monkeypatch.setattr(hf, "REPO", tmp_path)
    task_state = hf._task_pm_state_file("mytask")
    task_state.parent.mkdir(parents=True, exist_ok=True)
    task_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3),
        encoding="utf-8",
    )

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        worktree_pool=_RegisteredTaskPool(),
        # pm_state_file 미주입 — task 모드가 per-task pm_state 경로를 자체 해소(task_mode=True).
    )
    rc = handoff.run(
        session_num=78,
        wave_summary="요약",
        dry_run=True,
        skip_pytest=True,
        task="mytask",
    )
    assert rc == 0
    out = capsys.readouterr().out
    # task-only 트리거가 [5/7] 복사 블록에 나온다 — task 배선 가드. (전체 run 출력엔 무관한
    # `/pm-bootstrap`·`--slot` 산문 언급이 있어 정밀 slot-부재는 pure-function 테스트가 담당.)
    assert "/pm-bootstrap --task mytask" in out


@pytest.mark.parametrize("bad_task", ["my task", "foo)bar", "../evil", "a/b"])
def test_build_handoff_prompt_output_rejects_invalid_task(hf, bad_task):
    """build_handoff_prompt_output() 직접 호출도 invalid task 를 삽입 전 거부한다 (T-0394 codex R2).

    main() CLI 를 우회한 prompt builder 직접 호출에서 whitespace(`my task`)·괄호(`foo)bar`)·
    traversal(`../evil`)·separator(`a/b`) 가 트리거에 삽입돼 파싱 파손·경로 이탈을 내던 갭 —
    엔진층 단일 validator(validate_task_name_engine)가 formatter 경계에도 걸렸는지 가드한다.
    """
    with pytest.raises(Exception) as excinfo:  # noqa: PT011 — worktree_pool.InvalidTaskName(동적 로드)
        hf.build_handoff_prompt_output(
            pm_playbook_text=_TRIGGER_PLAYBOOK,
            session_num=78,
            wave_summary="요약",
            date_str="2026-07-20",
            task=bad_task,
        )
    assert type(excinfo.value).__name__ == "InvalidTaskName"


def test_validate_task_name_engine_rejects_and_accepts(hf):
    """공유 엔진 validator 가 invalid 거부·valid 통과 (T-0394 codex R2·단일 choke 함수)."""
    import pytest as _pytest

    with _pytest.raises(Exception) as excinfo:  # noqa: PT011
        hf.validate_task_name_engine("my task")
    assert type(excinfo.value).__name__ == "InvalidTaskName"
    # valid 단일 토큰은 통과(예외 없음).
    hf.validate_task_name_engine("mytask")


def test_run_rejects_invalid_task_before_side_effects(hf, tmp_path, capsys):
    """run(task=...) 직접 호출도 invalid task 면 부작용 0 로 중단(1) (T-0394 codex R2).

    run() 진입 검증이 pm_state/log 어떤 것도 건드리기 전 fail-loud — traversal `../evil` 이
    작업트리 밖 디렉토리를 만들거나 트리거를 파손하지 못하게 가드한다.
    """
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
    )
    rc = handoff.run(
        session_num=78,
        wave_summary="요약",
        dry_run=True,
        skip_pytest=True,
        task="../evil",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "부적합" in err
    # 부작용 0 — log 파일 미생성.
    assert not log_file.exists()


def test_run_task_mode_explicit_pm_state_emits_task_trigger(hf, tmp_path, capsys):
    """명시 pm_state_file(hermetic) + task 조합에서도 [5/7] 이 task-only 트리거를 emit 한다 (T-0394).

    정체성 판정 축은 run() 전체에서 `task is not None` 단일 — 명시 pm_state 주입 경로도 로그
    헤더 태그(`task:<이름>`)·dashboard 섹션이 task 로 처리한다. 트리거만 `task_mode`
    (`not _pm_state_file_explicit` 포함)로 갈라지면 로그/대시보드↔트리거 불일치(codex must-fix)라,
    같은 축으로 통일됐는지 명시 pm_state 경로로 가드한다.
    """
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,  # 명시 주입 → _pm_state_file_explicit True (task_mode False)
        worktree_pool=_RegisteredTaskPool(),
    )
    rc = handoff.run(
        session_num=78,
        wave_summary="요약",
        dry_run=True,
        skip_pytest=True,
        task="mytask",
    )
    assert rc == 0
    out = capsys.readouterr().out
    # 명시 pm_state 여도 트리거는 task-only (task is not None 축으로 통일).
    assert "/pm-bootstrap --task mytask" in out


def test_run_passes_worktree_slot_to_prompt(hf, tmp_path, capsys):
    """run() 이 self._worktree_slot 을 build_handoff_prompt_output 에 전달한다 (T-0185·호출부).

    명시 --worktree-slot=work/repoA_2 를 run 에 주면 [5/7] 복사 블록에 slot-qualified 트리거가
    나온다 — run() 호출부 배선을 가드.
    """
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
    )
    rc = handoff.run(
        session_num=7,
        wave_summary="요약",
        dry_run=True,
        skip_pytest=False,
        worktree_slot="work/repoA_2",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "/pm-bootstrap repoA --slot 2" in out


# ── 출하 pm_playbook 정합: 프롬프트가 트리거로 축소됐다 (T-0180·feature-ship 가드) ──

def test_shipped_pm_playbook_prompt_is_trigger_only():
    """실 출하 pm_playbook.md §부트스트랩 프롬프트가 트리거(역할 framing + /pm-bootstrap)다.

    손-채움 `<핵심 인계 사항>` 블록이 폐기됐는지 출하 파일 자체로 가드한다 — 프롬프트·log
    entry 양쪽에 같은 인계를 적던 중복이 재발하지 않게(부트스트랩이 dump·T-0179).
    """
    hf = _load_module()
    playbook_text = (
        REPO / ".project_manager" / "wiki" / "pm_playbook.md"
    ).read_text(encoding="utf-8")
    template = hf.extract_handoff_prompt_template(playbook_text)
    assert template is not None, "출하 pm_playbook 에서 프롬프트 템플릿 추출 실패"
    # 공유 canonical은 실제 Claude/OpenCode 입력값인 slash 표기를 유지한다.
    assert "/pm-bootstrap" in template
    assert "당신은" not in template, "2인칭 역할문구 잔존 — bare(T-0193)와 모순"
    assert "위임" not in template, "역할 framing 잔존 — pm_role/CLAUDE.md 로 이관됐어야 함"
    # 폐기: 손-채움 인계 블록(읽기 범위 손-기입 슬롯).
    assert "<핵심 인계 사항>" not in template
    assert "<- 읽기 범위" not in template
    # 폐기: 부트스트랩이 이미 하는 일(차수 announce·dump·인계 surface)과 4단계 보고 지침의
    # *재기술 사족*. 이건 /pm-bootstrap CLI(T-0179 dump)·pm-bootstrap skill·pm_role §첫-turn
    # 권장 액션이 단일 진실로 담당한다 — 프롬프트에 중복 기술하면 다음 PM 이 부트스트랩 실패
    # (예: board lint abort)에도 그 서술대로 수동 재구성·과잉 보고하게 유도한다(트리거만·T-0180).
    assert "차수 announce" not in template
    assert "자동 surface" not in template
    assert "단계로 보고" not in template


@pytest.mark.parametrize("prefix", ["/", "$", ""])
def test_trigger_injection_is_notation_independent(prefix):
    hf = _load_module()
    template = f"{prefix}pm-bootstrap\n"
    assert hf._inject_slot_into_template(template, "work/repoA_2") == (
        f"{prefix}pm-bootstrap repoA --slot 2\n"
    )
    assert hf._inject_task_into_template(template, "mytask") == (
        f"{prefix}pm-bootstrap --task mytask\n"
    )


def test_trigger_consumer_sensitivity_old_literal_matching_breaks_codex_copy():
    rendered = "$pm-bootstrap\n"
    old_result = rendered.replace("/pm-bootstrap", "/pm-bootstrap --task mytask")
    assert old_result == rendered, "구 literal 소비처가 codex 파손을 재현하지 못함"
    hf = _load_module()
    assert hf._inject_task_into_template(rendered, "mytask") == (
        "$pm-bootstrap --task mytask\n"
    )


@pytest.mark.parametrize(
    "non_call",
    (
        "/pm-bootstrap.md",
        "/pm-bootstrap/path",
        "/pm-bootstrap-extra",
        "prefix-/pm-bootstrap",
    ),
)
def test_trigger_injection_excludes_extensions_paths_and_identifiers(non_call):
    hf = _load_module()
    assert hf._inject_task_into_template(non_call, "mytask") == non_call


def test_multi_harness_shared_prompt_emits_one_runtime_notation(monkeypatch):
    hf = _load_module()
    playbook = """## 다음 PM 세션 부트스트랩 프롬프트 (템플릿)
```
`$pm-bootstrap`(codex) / `/pm-bootstrap`(opencode)
```
"""
    monkeypatch.setenv("CODEX_CI", "1")
    codex = hf.build_handoff_prompt_output(
        playbook, 3, "ok", "2026-08-01", task="mytask"
    )
    assert "$pm-bootstrap --task mytask" in codex
    assert "/pm-bootstrap" not in codex

    monkeypatch.delenv("CODEX_CI")
    opencode = hf.build_handoff_prompt_output(
        playbook, 3, "ok", "2026-08-01", task="mytask"
    )
    assert "/pm-bootstrap --task mytask" in opencode
    assert "$pm-bootstrap" not in opencode


@pytest.mark.parametrize(
    ("relative", "prefix"),
    (
        (".project_manager/wiki/pm_playbook.md", "/"),
        ("templates/claude_code/.project_manager/wiki/pm_playbook.md", "/"),
        ("templates/codex/.project_manager/wiki/pm_playbook.md", "/"),
        ("templates/opencode/.project_manager/wiki/pm_playbook.md", "/"),
    ),
)
def test_shipped_playbook_real_files_keep_trigger_injection(relative, prefix):
    """출하 실파일을 직접 태워 slot/task 주입과 하네스 표기 보존을 함께 검증한다."""
    hf = _load_module()
    text = (REPO / relative).read_text(encoding="utf-8")
    template = hf.extract_handoff_prompt_template(text)
    assert template is not None
    assert f"{prefix}pm-bootstrap\n" in template
    assert f"{prefix}pm-bootstrap repoA --slot 2" in hf._inject_slot_into_template(
        template, "work/repoA_2"
    )
    assert f"{prefix}pm-bootstrap --task mytask" in hf._inject_task_into_template(
        template, "mytask"
    )


def test_shipped_handoff_procedure_docs_have_no_handfill_instruction():
    """핸드오프 절차 문서(pm_role·claude SKILL·opencode 스킬 미러)가 폐기된 `<핵심 인계 사항>`
    손-채움을 *살아있는 단계*로 지시하지 않는다.

    프롬프트 emit(`build_handoff_prompt_output`)은 트리거화됐는데(T-0180) 절차 미러 문서가
    "그 절을 채우라"고 stale 로 남으면 다음 PM 이 *없는 슬롯*을 찾는다 — code-mirror 갱신 ↔
    doc-mirror stale 비대칭은 반복 클래스라([[feature-ship-needs-fresh-adopter-gate]]) 기계로 박는다.
    출하 파일 자체를 가드(canonical = 사본 byte-identical 은 parity 가드가 별도 강제). ADR-0065
    (단일 소비·T-0364): opencode 표면은 `.opencode/command` 은퇴 후 `.claude/skills` 미러다.
    """
    procedure_docs = [
        REPO / ".project_manager" / "wiki" / "pm_role.md",
        REPO / ".claude" / "skills" / "pm-handoff" / "SKILL.md",
        REPO / "templates" / "opencode" / ".claude" / "skills" / "pm-handoff" / "SKILL.md",
    ]
    import re
    # 줄바꿈/blockquote `>`/공백으로 쪼개져도 잡는 bounded loose match — 헤더 blockquote 가
    # "§핵심\n> 인계 사항" 으로 분할돼 단순 substring(`"핵심 인계 사항" in text`)을 빠져나갔던
    # 구멍을 막는다(1.0 문서 감사 P2). `.{0,6}`(DOTALL)로 인접만 매칭해 far-apart 오탐 방지.
    _handfill = re.compile(r"핵심.{0,6}인계.{0,6}사항", re.DOTALL)
    for doc in procedure_docs:
        text = doc.read_text(encoding="utf-8")
        # 폐기된 인계-블록 절 이름이 절차 지시에 잔존하면 안 된다 (트리거화 후 손-채움 슬롯 부재).
        assert not _handfill.search(text), (
            f"{doc} 에 폐기된 `<핵심 인계 사항>` 손-채움 지시 잔존 — 트리거(T-0180)와 모순"
        )


# ── run() 잔여 PM 손작업 checklist — domain capture 리마인더 (T-0084) ─────────


def _playbook_with_prompt() -> str:
    """인계 프롬프트 앵커+코드블록을 가진 최소 pm_playbook 텍스트(run 5단계 통과용)."""
    return (
        "# pm_playbook\n\n"
        f"{_PROMPT_ANCHOR}\n\n"
        "```\n인계 프롬프트 본문\n```\n"
    )


def test_run_checklist_includes_domain_capture_reminder(hf, tmp_path, capsys):
    """핸드오프 7단계 후 잔여 손작업 checklist 에 domain capture 검토 리마인더가 1줄 있다."""
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
    )
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=True, skip_pytest=False
    )
    assert rc == 0
    out = capsys.readouterr().out
    # capture 리마인더 — domain capture 명령과 채록 의도가 checklist 에 보인다.
    assert "domain capture" in out
    assert "capture --tickets" in out
    # 실형식 예시 명시 — 모호한 placeholder(`<이 세션 done…>`)가 아니라 콤마분리 실값을
    # 보여준다("출하 가이드=실값 명시성"·T-0373). placeholder 잔존을 회귀로 막는다.
    assert '--tickets "T-0001,T-0002"' in out
    assert "<이 세션 done" not in out
    # checklist 항목으로 ([ ]) 렌더 — 단순 산문이 아니라 잔여 작업 항목.
    assert any(
        line.strip().startswith("[ ]") and "domain capture" in line
        for line in out.splitlines()
    )


# ── session_num 이중 '차' 부착 방지 (T-0100·PM 9차 deferred·재발) ──────────────

@pytest.mark.parametrize("raw", ["19", "19차", "19차차", 19, " 19차 "])
def test_normalize_session_num_idempotent(hf, raw):
    # 숫자·'N차'·'N차차'·int·공백 모두 bare 숫자로 — 템플릿이 '차' 를 붙이므로 이중부착 방지.
    assert hf._normalize_session_num(raw) == "19"


@pytest.mark.parametrize("raw", ["19", "19차", "19차차", 19])
def test_handoff_skeleton_no_double_cha(hf, raw):
    # 어느 입력이든 헤더는 정확히 'PM 19차' (19차차 회귀 차단).
    head = hf.build_handoff_log_skeleton(raw, date="2026-06-19").splitlines()[0]
    assert head == "## [2026-06-19] handoff | PM 19차 → 다음 PM 세션"
    assert "차차" not in head


# ── T-0549 handoff 박제 entry 자동 목록 ───────────────────────────────────────

_MIXED_SESSION_LOG = """\
## [2026-08-01] handoff | PM 3차 (task:alpha) → 다음 PM 세션

## [2026-08-01] complete | T-0001 — alpha 완료 (task:alpha)

- alpha 본문은 수집하지 않는다.
## [2026-08-01] complete | T-0002 — 다른 task 완료 (task:beta)

## [2026-08-01] complete | T-0003 — legacy 무태그 완료

## [2026-08-02] checkpoint | (task:alpha) — compaction

- checkpoint 본문도 수집하지 않는다.
## [2026-08-02] checkpoint | (task:beta) — manual

## [2026-08-03] handoff | PM 4차 (task:alpha) → 다음 PM 세션

## [2026-08-03] complete | T-0004 — 이번 세션 완료 (task:alpha)

## [2026-08-03] complete | T-0005 — alpha 표기가 제목뿐 (task:beta)

## [2026-08-04] checkpoint | (task:alpha) — manual

## [2026-08-04] verify | T-0004 — 임의 action 완료 (task:alpha)
"""


def test_collect_session_entries_filters_mixed_task_log(hf):
    assert hf.collect_session_entries(_MIXED_SESSION_LOG, "alpha") == [
        "## [2026-08-03] complete | T-0004 — 이번 세션 완료 (task:alpha)",
        "## [2026-08-04] checkpoint | (task:alpha) — manual",
        "## [2026-08-04] verify | T-0004 — 임의 action 완료 (task:alpha)",
    ]


def test_collect_session_entries_round_trip_prefixed_id_from_real_producers(hf):
    """ticket_finish의 prefixed ID 헤더와 pm_log 헤더를 생산→소비 round-trip한다."""
    ticket_finish = _load_tool("ticket_finish")
    pm_log = _load_tool("pm_log")
    task = "orch-dev-T0549-r3"
    finish = ticket_finish.build_log_skeleton(
        "T-service-a-001", "수집기 실 round-trip", 42, 1, 2,
        entry_type="feat", date="2026-08-06", task=task,
    )
    checkpoint = pm_log.build_checkpoint_entry(
        task, "compaction", date="2026-08-06",
    )
    log_text = (
        f"## [2026-08-05] handoff | PM 2차 (task:{task}) → 다음 PM 세션\n\n"
        + finish + "\n" + checkpoint
    )

    assert hf.collect_session_entries(log_text, task) == [
        finish.splitlines()[0],
        checkpoint.splitlines()[0],
    ]


def test_slot_session_uses_own_previous_handoff_as_boundary(hf):
    """slot handoff가 반복돼도 같은 slot의 직전 경계 이전 entry를 재수집하지 않는다."""
    log_text = """\
## [2026-08-01] handoff | PM 1차 (project_manager_1) → 다음 PM 세션
## [2026-08-01] feat | T-0001 — 이전 slot 세션 완료 (project_manager_1)
## [2026-08-02] handoff | PM 1차 (project_manager_2) → 다음 PM 세션
## [2026-08-02] fix | T-0002 — 다른 slot 완료 (project_manager_2)
## [2026-08-03] handoff | PM 2차 (project_manager_1) → 다음 PM 세션
## [2026-08-03] verify | T-0003 — 현재 slot 세션 완료 (project_manager_1)
"""
    skeleton = hf.build_handoff_log_skeleton(
        3, date="2026-08-04", session="project_manager_1", log_text=log_text,
    )

    assert "verify | T-0003 — 현재 slot 세션 완료" in skeleton
    assert "feat | T-0001 — 이전 slot 세션 완료" not in skeleton
    assert "fix | T-0002 — 다른 slot 완료" not in skeleton


def test_slot_session_collects_only_own_tag_across_concurrent_slot_order(hf):
    """slot 태그가 귀속 권위라 interleave된 타 slot·legacy 무태그 complete를 흡수하지 않는다."""
    log_text = """\
## [2026-08-01] handoff | PM 1차 (project_manager_1) → 다음 PM 세션
## [2026-08-02] feat | T-0001 — slot1 완료 (project_manager_1)
## [2026-08-03] handoff | PM 1차 (project_manager_2) → 다음 PM 세션
## [2026-08-04] fix | T-0002 — slot2 완료 (project_manager_2)
## [2026-08-05] verify | T-0003 — legacy 무태그 혼입 후보
## [2026-08-06] complete | T-0004 — slot1 후속 완료 (project_manager_1)
"""

    assert hf.collect_session_entries(log_text, None, "project_manager_1") == [
        "## [2026-08-02] feat | T-0001 — slot1 완료 (project_manager_1)",
        "## [2026-08-06] complete | T-0004 — slot1 후속 완료 (project_manager_1)",
    ]


def test_task_first_handoff_collects_all_own_tagged_entries(hf):
    """자기 handoff가 아직 없는 task는 무관 handoff로 자르지 않고 태그 기준 전체를 수집한다."""
    log_text = """\
## [2026-08-01] feat | T-0001 — 첫 task 완료 (task:alpha)
## [2026-08-02] handoff | PM 9차 (project_manager_2) → 다음 PM 세션
## [2026-08-03] fix | T-0002 — 두 번째 task 완료 (task:alpha)
"""

    assert hf.collect_session_entries(log_text, "alpha") == [
        "## [2026-08-01] feat | T-0001 — 첫 task 완료 (task:alpha)",
        "## [2026-08-03] fix | T-0002 — 두 번째 task 완료 (task:alpha)",
    ]


def test_collect_session_entries_solo_uses_last_untagged_handoff(hf):
    log_text = _MIXED_SESSION_LOG + """\
## [2026-08-05] handoff | PM 9차 → 다음 PM 세션

## [2026-08-05] complete | T-0006 — solo legacy 완료

## [2026-08-05] checkpoint | (task:beta) — compaction
"""
    assert hf.collect_session_entries(log_text, None) == [
        "## [2026-08-05] complete | T-0006 — solo legacy 완료",
    ]


def test_collect_session_entries_unresolved_boundary_caps_old_red_104(hf):
    """자기/임의 handoff가 전혀 없는 adopter#0 로그도 최근 10건만 박제한다."""
    log_text = "\n".join(
        f"## [2026-08-01] feat | T-{index:04d} — adopter entry {index}"
        for index in range(1, 105)
    )

    entries = hf.collect_session_entries(log_text, None)
    skeleton = hf.build_handoff_log_skeleton(1, log_text=log_text)

    assert len(entries) == 10
    assert entries[0].endswith("T-0095 — adopter entry 95")
    assert entries[-1].endswith("T-0104 — adopter entry 104")
    assert "(경계 미해소 — 최근 10건)" in skeleton
    assert "T-0094 — adopter entry 94" not in skeleton


def test_slot_missing_own_identity_does_not_absorb_legacy_untagged_entries(hf):
    log_text = """\
## [2026-08-01] feat | T-0001 — 오래된 완료
## [2026-08-02] handoff | PM 3차 (other_1) → 다음 PM 세션
## [2026-08-03] fix | T-0002 — 최근 완료
"""
    skeleton = hf.build_handoff_log_skeleton(
        1, session="new_1", log_text=log_text,
    )

    assert "T-0001 — 오래된 완료" not in skeleton
    assert "T-0002 — 최근 완료" not in skeleton
    assert "이 세션 박제 entry 없음" in skeleton


def test_slot_and_solo_exclude_other_task_tagged_entries(hf):
    log_text = """\
## [2026-08-01] handoff | PM 1차 (slot_1) → 다음 PM 세션
## [2026-08-02] feat | T-0001 — slot 완료 (slot_1)
## [2026-08-02] fix | T-0002 — 타 task 완료 (task:beta)
## [2026-08-02] checkpoint | (task:beta) — compaction
"""
    assert hf.collect_session_entries(log_text, None, "slot_1") == [
        "## [2026-08-02] feat | T-0001 — slot 완료 (slot_1)",
    ]
    assert hf.collect_session_entries(log_text, None) == []


def test_collect_session_entries_matches_placeholder_type_and_wide_ticket_id(hf):
    log_text = """\
## [2026-08-01] handoff | PM 1차 (task:alpha) → 다음 PM 세션
## [2026-08-02] <!-- feat/fix/verify/… --> | T-12345 — 미채움 (task:alpha)
"""
    assert hf.collect_session_entries(log_text, "alpha") == [
        "## [2026-08-02] <!-- feat/fix/verify/… --> | T-12345 — 미채움 (task:alpha)",
    ]


def test_handoff_skeleton_lists_only_collected_entry_headers(hf):
    skeleton = hf.build_handoff_log_skeleton(
        5,
        date="2026-08-05",
        session="task:alpha",
        log_text=_MIXED_SESSION_LOG,
        task="alpha",
    )
    assert "- 이 세션 박제 entries:\n" in skeleton
    assert "  - ## [2026-08-03] complete | T-0004 — 이번 세션 완료 (task:alpha)" in skeleton
    assert "  - ## [2026-08-04] checkpoint | (task:alpha) — manual" in skeleton
    assert "alpha 본문은 수집하지 않는다" not in skeleton
    assert "읽기 범위" not in skeleton
    assert "대화 thread-tail" not in skeleton


def test_handoff_skeleton_marks_empty_session_entries(hf):
    skeleton = hf.build_handoff_log_skeleton(1, date="2026-08-05")
    # 0건이면 경계 표기 없이 없음 문구만 — 빈 목록의 "최근 N건" 서술은 무의미.
    assert "- 이 세션 박제 entries: (이 세션 박제 entry 없음)" in skeleton
    assert "경계 미해소" not in skeleton


def test_thread_tail_interface_removed(hf):
    import inspect

    assert "thread_tail" not in inspect.signature(hf.build_handoff_log_skeleton).parameters
    assert "--thread-tail" not in hf.build_parser()._option_string_actions
    assert not hasattr(hf, "_flatten_thread_tail")
    assert not hasattr(hf, "_next_intent_lines")
    assert not hasattr(hf, "THREAD_TAIL_PLACEHOLDER")
    assert not hasattr(hf, "THREAD_TAIL_MAX_CHARS")


@pytest.mark.parametrize("raw", ["20", "20차", "20차차"])
def test_session_window_entry_no_double_cha(hf, raw):
    # sliding-window entry 줄도 '**20차**' (정규식 `\\d+차` 매칭 보존 — 19차차면 매칭 깨짐).
    entry = hf._build_new_session_entry(raw, "2026-06-19", "wave")
    assert entry.startswith("  - **20차** ")
    assert "차차" not in entry


# ── _regression_cwd — 회귀 cwd worktree 자동해소 (T-0124) ─────────────────────
# `_regression_cwd(worktree_slot=, areas_file=, leases_file=)` 는 파일 seam 을 인자로
# 노출하므로 실 장부/areas 를 안 건드린다(hermetic·pm_bootstrap._auto_slot 재사용). REPO 는
# 절대경로 비교 대신 반환 문자열의 suffix(슬롯 식별자)로 검증한다 — REPO monkeypatch 불요.

import json as _rcwd_json  # noqa: E402 — T-0124 테스트 전용 로컬 import


def _write_areas(path: Path, repos: list[str]) -> None:
    """areas.md (신 스키마·파이프 테이블) — repo 행을 repos 개수만큼. 빈 리스트면 헤더만."""
    lines = [
        "| repo | prefix | git | test_cmd | owner | base | protected |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in repos:
        lines.append(f"| {r} | {r} |  |  | alice |  |  |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_leases(path: Path, entries: list[dict]) -> None:
    """worktree-leases.json — {"leases": [...]} 스키마 (worktree_pool.Lease.to_dict 동형)."""
    path.write_text(_rcwd_json.dumps({"leases": entries}), encoding="utf-8")


def test_regression_cwd_single_self_host_resolves_slot(hf, tmp_path):
    # 단일 self-host: areas 1 repo + 그 repo 슬롯 정확히 1개 → work/<repo>_<N> 로 끝남.
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    result = hf._regression_cwd(areas_file=areas, leases_file=leases)
    assert result.replace(os.sep, "/").endswith("work/project_manager_1")


def test_regression_cwd_explicit_slot_overrides_auto(hf, tmp_path):
    # 명시 worktree_slot 우선 — auto 판정을 무시하고 그 슬롯 경로 반환. 디렉토리가 실존해야
    # L1(stale→REPO 폴백) 이 발동하지 않는다 — repo_root 를 tmp_path 로 둬 실존을 보장한다.
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    (tmp_path / "work" / "foo_2").mkdir(parents=True)
    result = hf._regression_cwd(
        "work/foo_2", areas_file=areas, leases_file=leases, repo_root=tmp_path
    )
    assert result.replace(os.sep, "/").endswith("work/foo_2")


def test_regression_cwd_zero_repos_falls_back_to_repo(hf, tmp_path):
    # 등록 repo 0개 → str(REPO) 폴백 (work/ 슬롯 suffix 아님).
    areas = tmp_path / "areas.md"   # 미생성 → 부재
    leases = tmp_path / "worktree-leases.json"
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    assert hf._regression_cwd(areas_file=areas, leases_file=leases) == str(hf.REPO)


def test_regression_cwd_two_repos_falls_back_to_repo(hf, tmp_path):
    # 등록 repo 2개(진짜 multi-PM·모호) → str(REPO) 폴백.
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["A", "B"])
    _write_leases(leases, [
        {"slot": "work/A_1", "repo": "A", "session": "A_1", "state": "leased"},
    ])
    assert hf._regression_cwd(areas_file=areas, leases_file=leases) == str(hf.REPO)


def test_regression_cwd_two_slots_ambiguous_falls_back(hf, tmp_path):
    # 등록 repo 1개지만 그 repo 슬롯 2개(모호) → str(REPO) 폴백.
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
        {"slot": "work/project_manager_2", "repo": "project_manager",
         "session": "project_manager_2", "state": "leased"},
    ])
    assert hf._regression_cwd(areas_file=areas, leases_file=leases) == str(hf.REPO)


def test_regression_cwd_missing_leases_falls_back(hf, tmp_path):
    # lease 장부 부재 → str(REPO) 폴백 (fail-soft).
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"  # 미생성 → 부재
    _write_areas(areas, ["project_manager"])
    assert hf._regression_cwd(areas_file=areas, leases_file=leases) == str(hf.REPO)


def test_regression_cwd_corrupt_leases_falls_back(hf, tmp_path):
    # 깨진 JSON 장부 → str(REPO) 폴백 (fail-soft·크래시 안 함).
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    leases.write_text("{not valid json", encoding="utf-8")
    assert hf._regression_cwd(areas_file=areas, leases_file=leases) == str(hf.REPO)


def test_regression_cwd_bootstrap_absent_falls_back(hf, tmp_path, monkeypatch):
    # pm_bootstrap 동적로드 실패(None) → str(REPO) 폴백 (자동해소 없이 안전).
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    monkeypatch.setattr(hf, "_load_pm_bootstrap", lambda: None)
    assert hf._regression_cwd(areas_file=areas, leases_file=leases) == str(hf.REPO)


# ── ADR-0057 세션 정체성 canonical: --repo/--slot(decomposed) + --session-seq(차수) ──
#
# ADR-0043 의 단일문자열 `--session`/구형 alias(`--worktree-slot`·`--session-num`)를 뒤집고
# 전 CLI 를 분해형 `--repo <name> [--slot <N>]` 로 통일한다(alias 0·즉시삭제·BREAKING).
# `--session-seq`(차수·비정체성)는 유지. main() ingress→run() 전달 kwargs 를 캡처해 해소·
# M3(세션↔repo 조인 검증)·fail-loud 를 durable 하게 못박는다(T-0246 관용구 계승·비-vacuous).


@pytest.fixture
def captured_run(hf, monkeypatch):
    """PmHandoff.run 을 가로채 kwargs 를 캡처(실 회귀/파일편집 없이 ingress 만 검증).

    main() 이 identity 해소·필수 검증을 마치고 run() 에 넘기는 kwargs 를 그대로 포착한다 —
    fail-loud(parser.error·SystemExit) 케이스에선 run 미도달이라 dict 가 빈 채 남는다.
    """
    calls: dict = {}

    def _fake_run(self, **kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(hf.PmHandoff, "run", _fake_run)
    return calls


# --- 차수 인자: --session-seq(정체성과 무관·유지) ---

def test_session_seq_canonical_accepted(hf, captured_run):
    assert hf.main(["--session-seq", "42", "--wave-summary", "x", "--no-pytest"]) == 0
    assert captured_run["session_num"] == "42"


def test_session_seq_missing_rejected(hf, captured_run):
    # 미지정 → 필수 누락 거부(대화형 경로).
    with pytest.raises(SystemExit):
        hf.main(["--wave-summary", "x", "--no-pytest"])
    assert captured_run == {}


# --- 정체성 인자: --repo/--slot(decomposed canonical·ADR-0057) — hermetic (REPO monkeypatch) ---
#
# `_resolve_explicit_identity_slot` 의 leases_file 기본값은 *호출 시점* 모듈 `REPO` 에서 재구성
# 되므로(monkeypatch 추종), `hf.REPO` 를 tmp_path 로 돌려 실 장부(이 워크트리의 진짜
# worktree-leases.json)를 절대 건드리지 않는 결정론적 CLI 테스트를 만든다.

def test_repo_and_slot_derive_work_prefix(hf, tmp_path, captured_run, monkeypatch):
    # --repo X --slot N → work/<repo>_<N>. 리스 장부 부재(tmp_path)라 M3 는 검증불가·fail-soft(신뢰).
    monkeypatch.setattr(hf, "REPO", tmp_path)
    assert hf.main(
        ["--repo", "project_manager", "--slot", "1", "--session-seq", "7",
         "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/project_manager_1"


def test_repo_alone_resolves_single_active_slot(hf, tmp_path, captured_run, monkeypatch):
    # --repo 단독 + 그 repo 활성(leased) 슬롯 정확히 1개 → actor 자동해소(ADR-0057 결정 3).
    monkeypatch.setattr(hf, "REPO", tmp_path)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True)
    _write_leases(leases, [
        {"slot": "work/myrepo_3", "repo": "myrepo", "session": "myrepo_3", "state": "leased"},
    ])
    assert hf.main(
        ["--repo", "myrepo", "--session-seq", "7", "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/myrepo_3"


def test_repo_alone_zero_active_slots_fails_loud(hf, tmp_path, captured_run, monkeypatch):
    # --repo 단독인데 그 repo 활성 슬롯 0개 → M3(explicit 요청 조인 불가) fail-loud.
    monkeypatch.setattr(hf, "REPO", tmp_path)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True)
    _write_leases(leases, [])
    with pytest.raises(SystemExit):
        hf.main(["--repo", "myrepo", "--session-seq", "7", "--wave-summary", "x", "--no-pytest"])
    assert captured_run == {}


def test_repo_alone_ambiguous_slots_fails_loud(hf, tmp_path, captured_run, monkeypatch):
    # --repo 단독인데 그 repo 활성 슬롯 ≥2 → SlotResolutionError(identity_args) fail-loud.
    monkeypatch.setattr(hf, "REPO", tmp_path)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True)
    _write_leases(leases, [
        {"slot": "work/myrepo_1", "repo": "myrepo", "session": "myrepo_1", "state": "leased"},
        {"slot": "work/myrepo_2", "repo": "myrepo", "session": "myrepo_2", "state": "leased"},
    ])
    with pytest.raises(SystemExit):
        hf.main(["--repo", "myrepo", "--session-seq", "7", "--wave-summary", "x", "--no-pytest"])
    assert captured_run == {}


def test_repo_and_slot_mismatch_fails_loud_m3(hf, tmp_path, captured_run, monkeypatch):
    # M3(라이더) — 명시 --repo/--slot 이 실제 활성 리스와 조인 불일치 → fail-loud(조용히 안 넘김).
    monkeypatch.setattr(hf, "REPO", tmp_path)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True)
    _write_leases(leases, [
        {"slot": "work/myrepo_1", "repo": "myrepo", "session": "myrepo_1", "state": "leased"},
    ])
    with pytest.raises(SystemExit):
        hf.main(
            ["--repo", "myrepo", "--slot", "99", "--session-seq", "7",
             "--wave-summary", "x", "--no-pytest"]
        )
    assert captured_run == {}


def test_slot_without_repo_rejected(hf, captured_run):
    # --slot 단독(--repo 없음) — identity_args.parse_identity 가 ValueError → parser.error(fail-loud).
    with pytest.raises(SystemExit):
        hf.main(["--slot", "4", "--session-seq", "7", "--wave-summary", "x", "--no-pytest"])
    assert captured_run == {}


def test_solo_unspecified_worktree_slot_none(hf, captured_run):
    # 솔로(정체성 미지정) 현행 경로 무변경 — worktree_slot None 로 run 진입.
    assert hf.main(["--session-seq", "7", "--wave-summary", "x", "--no-pytest"]) == 0
    assert captured_run["worktree_slot"] is None


# --- `_resolve_explicit_identity_slot` 직접 단위 (M3 라이더 핵심 로직) ---

def test_resolve_explicit_identity_slot_none_when_no_repo(hf, tmp_path):
    assert hf._resolve_explicit_identity_slot(None, None, tmp_path / "absent.json") == (None, None)


def test_resolve_explicit_identity_slot_missing_ledger_trusts_explicit(hf, tmp_path):
    # 장부 부재(판정불가) → fail-soft: repo/slot 명시를 그대로 신뢰해 조립.
    result = hf._resolve_explicit_identity_slot("myrepo", 5, tmp_path / "absent.json")
    assert result == ("work/myrepo_5", None)


def test_resolve_explicit_identity_slot_m3_matches_active_lease(hf, tmp_path):
    leases = tmp_path / "leases.json"
    _write_leases(leases, [
        {"slot": "work/myrepo_1", "repo": "myrepo", "session": "myrepo_1", "state": "leased"},
    ])
    assert hf._resolve_explicit_identity_slot("myrepo", 1, leases) == ("work/myrepo_1", None)


def test_resolve_explicit_identity_slot_m3_rejects_mismatch(hf, tmp_path):
    leases = tmp_path / "leases.json"
    _write_leases(leases, [
        {"slot": "work/myrepo_1", "repo": "myrepo", "session": "myrepo_1", "state": "leased"},
    ])
    slot, err = hf._resolve_explicit_identity_slot("myrepo", 99, leases)
    assert slot is None
    assert err is not None and "조인" in err and "M3" in err


def test_resolve_explicit_identity_slot_repo_alone_resolves(hf, tmp_path):
    leases = tmp_path / "leases.json"
    _write_leases(leases, [
        {"slot": "work/myrepo_2", "repo": "myrepo", "session": "myrepo_2", "state": "leased"},
    ])
    assert hf._resolve_explicit_identity_slot("myrepo", None, leases) == ("work/myrepo_2", None)


def test_resolve_explicit_identity_slot_repo_alone_zero_active(hf, tmp_path):
    leases = tmp_path / "leases.json"
    _write_leases(leases, [])
    slot, err = hf._resolve_explicit_identity_slot("myrepo", None, leases)
    assert slot is None
    assert err is not None and "M3" in err


def test_resolve_explicit_identity_slot_repo_alone_ambiguous(hf, tmp_path):
    leases = tmp_path / "leases.json"
    _write_leases(leases, [
        {"slot": "work/myrepo_1", "repo": "myrepo", "session": "myrepo_1", "state": "leased"},
        {"slot": "work/myrepo_2", "repo": "myrepo", "session": "myrepo_2", "state": "leased"},
    ])
    slot, err = hf._resolve_explicit_identity_slot("myrepo", None, leases)
    assert slot is None
    assert err is not None and "--slot" in err


# --- `_regression_cwd` — 명시 슬롯 stale → REPO 폴백 경계 ---

def test_regression_cwd_explicit_slot_stale_falls_back_to_repo(hf, tmp_path, capsys):
    # 명시 worktree_slot 이 장부 해소는 통과했더라도 디스크에 실제 디렉토리가 없으면
    # (장부-파일시스템 out-of-sync) FileNotFoundError 로 죽는 대신 REPO 로 soft 폴백·경고 1줄.
    result = hf._regression_cwd("work/foo_2", repo_root=tmp_path)
    assert result == str(tmp_path)
    err = capsys.readouterr().err
    assert "work/foo_2" in err and "REPO 로 폴백" in err


def test_regression_cwd_explicit_slot_existing_dir_not_stale(hf, tmp_path):
    # 경계 반대편 — 디렉토리가 실제 존재하면 그대로(stale 아님·폴백 미발동).
    (tmp_path / "work" / "foo_2").mkdir(parents=True)
    result = hf._regression_cwd("work/foo_2", repo_root=tmp_path)
    assert result == str(tmp_path / "work" / "foo_2")


# ── ADR-0044 handoff 헤더 세션 정체성 태그 (T-0252) ──────────────────────────────
#
# 멀티(정체성 해소) 헤더에 `PM {N}차 ({session}) →` 태그를 박고, 솔로(미해소)는 태그를 생략해
# 현행 헤더와 byte-호환을 유지한다. 태그 값은 canonical `<repo>_<N>`(`work/` 프리픽스 아님).
# 태그는 이벤트 감사 메타데이터일 뿐 상태 저장이 아니다(ADR-0040 무충돌).


def test_session_tag_helper_present_and_absent(hf):
    # 정체성 있으면 선행 공백 포함 ` ({session})`·없으면(None/빈문자/공백) 빈 문자열.
    assert hf._session_tag("project_manager_1") == " (project_manager_1)"
    assert hf._session_tag(None) == ""
    assert hf._session_tag("") == ""


def test_handoff_skeleton_solo_omits_session_tag_byte_compat(hf):
    # 솔로(session 미지정) — 헤더는 현행 스키마와 정확히 byte-호환(태그·괄호 없음).
    head = hf.build_handoff_log_skeleton(7, date="2026-07-10").splitlines()[0]
    assert head == "## [2026-07-10] handoff | PM 7차 → 다음 PM 세션"
    assert "(" not in head


def test_handoff_skeleton_multi_inserts_session_tag(hf):
    # 멀티(session 해소) — 차수 뒤에 정체성 태그 삽입, canonical `<repo>_<N>`(work/ 프리픽스 없음).
    head = hf.build_handoff_log_skeleton(
        7, date="2026-07-10", session="project_manager_1"
    ).splitlines()[0]
    assert head == "## [2026-07-10] handoff | PM 7차 (project_manager_1) → 다음 PM 세션"
    assert "work/" not in head


def test_run_writes_session_tag_from_worktree_slot(hf, tmp_path, capsys):
    """run() 이 해소된 슬롯(`work/<repo>_<N>`)에서 canonical `<repo>_<N>` 를 유도해 헤더 태그에
    박는다 (write 경로 배선·ADR-0044). 태그 값엔 `work/` 프리픽스가 없다.
    """
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
    )
    rc = handoff.run(
        session_num=7,
        wave_summary="요약",
        dry_run=True,
        skip_pytest=False,
        worktree_slot="work/repoA_2",
    )
    assert rc == 0
    out = capsys.readouterr().out
    # 헤더 skeleton 에 canonical 정체성 태그(work/ 프리픽스 제거)가 박혔다.
    assert "PM 7차 (repoA_2) → 다음 PM 세션" in out
    # 태그 자리엔 슬롯 원형(`work/repoA_2`)이 아니라 canonical `<repo>_<N>` 만.
    assert "(work/repoA_2)" not in out


def test_run_solo_omits_session_tag_byte_compat(hf, tmp_path, capsys):
    """솔로(worktree_slot 미지정·명시 pm_state 로 자동해소 skip) — 헤더 태그 생략·byte-호환."""
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
    )
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=True, skip_pytest=False
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "PM 7차 → 다음 PM 세션" in out  # 태그·괄호 없는 현행 헤더.


def test_done_repo_slot_reaches_release(hf, tmp_path, captured_run, monkeypatch):
    """codex 게이트(2026-07-10 wave A) 계승 — `--repo/--slot --done` 이 release 대상
    (worktree_slot=`work/<repo>_<N>`·done=True)으로 run 에 도달한다(ADR-0057 decomposed 화 후에도
    release 배선이 확실히 이어지는지 직접 못박아 전환 회귀 차단).
    """
    monkeypatch.setattr(hf, "REPO", tmp_path)
    assert hf.main(
        ["--repo", "project_manager", "--slot", "1", "--done", "--session-seq", "7",
         "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/project_manager_1"
    assert captured_run["done"] is True


# ── task 모드 귀속 (F7·T-0356) — pm_state task 경로·dashboard 키·log 태그·lease 유지 ──────
#
# 세션 종료(핸드오프)의 연속성 앵커를 slot→task 로 이동한다. task 생성 시 이미 만들어진
# `.local/tasks/<name>/pm_state.md`에 기록·dashboard 자기 섹션 `## <name>`·
# log 헤더 태그 `(task:<name>)`. lease 는 유지(세션 종료 ≠ task 종료·F4). log 태그는 서술형 괄호·
# 슬롯 태그와 구분되게 sentinel `task:` 로 박는다.


def test_main_task_forwarded_to_run_without_session_override(hf, captured_run):
    """main()은 task만 forward하고 차수는 run()의 state 추론에 맡긴다."""
    assert hf.main(["--wave-summary", "x", "--no-pytest", "--task", "mytask"]) == 0
    assert captured_run["task"] == "mytask"
    assert captured_run["session_num"] is None
    assert captured_run["wave_summary"] == "x"  # 명시 콘텐츠는 정당하므로 task에서도 유지.


def test_main_task_rejects_explicit_session_seq_usage_error(hf, captured_run, capsys):
    """task CLI의 명시 차수는 state 추론 우회가 되므로 usage error로 거부한다(MF-a)."""
    with pytest.raises(SystemExit) as exc:
        hf.main(["--task", "mytask", "--session-seq", "999"])
    assert exc.value.code == 2
    assert "--session-seq를 지정할 수 없다" in capsys.readouterr().err
    assert captured_run == {}


def test_main_task_only_infers_session_and_default_summary(
    hf, tmp_path, captured_run, monkeypatch
):
    """`pm_handoff.py --task mytask`만으로 task state에서 차수와 기본 요약을 해소한다."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    state = hf._task_pm_state_file("mytask")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        "## 세션 식별 (현재까지 사용된 이름)\n"
        f"{hf.TASK_PM_STATE_EMPTY_MARKER}\n",
        encoding="utf-8",
    )

    assert hf.main(["--task", "mytask"]) == 0
    assert captured_run["task"] == "mytask"
    assert captured_run["worktree_slot"] is None
    assert captured_run["session_num"] is None
    assert captured_run["wave_summary"] == "task mytask 세션 핸드오프"


@pytest.mark.parametrize(
    "argv",
    [
        ["--task", "mytask", "--repo", "A"],
        ["--task", "mytask", "--repo", "A", "--slot", "2"],
        ["--task", "mytask", "--slot", "2"],
        ["--task", "mytask", "--branch", "feature"],
        ["--task", "mytask", "--done"],
    ],
)
def test_main_task_rejects_slot_identity_mixing(hf, captured_run, argv):
    """task handoff CLI는 repo/slot/branch/done 혼합을 run 전에 거부한다."""
    with pytest.raises(SystemExit) as exc:
        hf.main(argv)
    assert exc.value.code == 2
    assert captured_run == {}


def test_main_task_slot_only_surfaces_task_contract_before_repo_hint(
    hf, captured_run, capsys
):
    """task+bare slot은 `--repo 필수`가 아니라 ADR-0078 task 혼합 거부를 먼저 안내한다(SF)."""
    with pytest.raises(SystemExit) as exc:
        hf.main(["--task", "mytask", "--slot", "2"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--task 는 단독 정체성" in err
    assert "--slot 은 --repo 필수" not in err
    assert captured_run == {}


@pytest.mark.parametrize("bad", [
    "../evil", "a/b", "..", ".hidden", "  ", "x\\y",
    # 문자 도메인 협소화(T-0356 codex 2건) — 공유 validator 상속으로 handoff 도 whitespace/괄호 거부.
    "my task", "a\tb", "foo)bar", "foo(bar",
])
def test_main_task_traversal_rejected(hf, captured_run, bad):
    """--task 검증 — 공유 엔진 validator(`worktree_pool._validate_task_name`)로 부적합 명 fail-loud.

    traversal/절대경로/빈 이름/선행 `.` + **whitespace·괄호**(하류 CLI 인자 경계·log 태그 delimiter
    파손 방지·codex 2건) 를 부작용 이전에 거부한다. handoff 는 bind_task 우회 CLI 라 여기서 shared
    validator 로 닫아 도메인 협소화를 자동 상속한다. 거부 시 run 미도달(captured 비어 있음)."""
    with pytest.raises(SystemExit):
        hf.main(
            ["--wave-summary", "x", "--no-pytest", "--task", bad]
        )
    assert captured_run == {}


def test_main_task_reserved_slot_name_rejected(hf, captured_run, monkeypatch):
    """예약명(`<repo>_<N>`) task 거부 — shared validator 가 registered_repos(board fail-soft)로 판별.

    `--task project_manager_1` 오입력이 dashboard `## project_manager_1` 를 실 slot-1 섹션과 충돌
    시키는 것을 차단한다(reviewer). board.registered_repos 를 monkeypatch 로 hermetic 주입한다."""
    class _FakeBoard:
        @staticmethod
        def registered_repos():
            return ["project_manager"]

    monkeypatch.setattr(hf, "_load_board", lambda: _FakeBoard())
    with pytest.raises(SystemExit):
        hf.main(
            ["--wave-summary", "x", "--no-pytest", "--task", "project_manager_1"]
        )
    assert captured_run == {}
    # 미등록 repo 형태(자유 포맷)는 통과 — run 도달(예약 판별이 실 슬롯과만 충돌 방지·오탐 0).
    assert hf.main(
        ["--wave-summary", "x", "--no-pytest", "--task", "sikdan_2"]
    ) == 0
    assert captured_run["task"] == "sikdan_2"


def test_run_task_writes_task_tag_and_dashboard_key(hf, tmp_path, capsys):
    """task 모드 — log 헤더 태그 `(task:<name>)`(sentinel)·dashboard 자기 섹션 `## <name>`(verbatim).

    hermetic(명시 pm_state 주입) dry_run 으로 태그/키 생성 배선만 본다. 슬롯 태그(`<repo>_<N>`)와
    달리 sentinel `task:` 접두를 붙여 서술형 괄호와 기계 구분한다. dashboard 는 verbatim 이라
    sentinel 없이 `## mytask`(interface 2)."""
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        worktree_pool=_RegisteredTaskPool(),
    )
    rc = handoff.run(
        session_num=999, wave_summary="요약", dry_run=True, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    out = capsys.readouterr().out
    # log 헤더 태그 = sentinel `(task:mytask)`.
    assert "PM 7차 (task:mytask) → 다음 PM 세션" in out
    # bare `(mytask)`(서술 괄호와 오탐 원천)이 아님.
    assert "PM 7차 (mytask)" not in out
    # dashboard 자기 섹션은 verbatim task 명(sentinel 없음).
    assert "## mytask" in out
    assert "## task:mytask" not in out


def test_run_task_recovers_log_only_interrupt_as_same_session_and_ignores_999(
    hf, tmp_path
):
    """log append 후 state 전 중단은 같은 log 차수로 window만 복구한다."""
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(_state(_entry(1), _entry(2)), encoding="utf-8")
    log_file = tmp_path / "current.md"
    log_file.write_text(
        hf.build_handoff_log_skeleton(
            session_num=3,
            date="2026-07-24",
            session="task:mytask",
        ),
        encoding="utf-8",
    )
    dashboard_file = tmp_path / "dashboard.md"
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=dashboard_file,
        # log-only 반쪽 상태는 log보다 먼저 남는 handoff intent(pid=0)가
        # 반드시 있다. 새 bootstrap이 bind한 pid>0이면 log 3 다음 4차가 맞다.
        worktree_pool=_RegisteredTaskPool(pid=0),
    )

    rc = handoff.run(
        session_num=999,
        wave_summary="중단 복구",
        dry_run=False,
        skip_pytest=True,
        task="mytask",
    )

    assert rc == 0
    log_text = log_file.read_text(encoding="utf-8")
    state_text = pm_state.read_text(encoding="utf-8")
    dashboard_text = dashboard_file.read_text(encoding="utf-8")
    assert log_text.count("PM 3차 (task:mytask)") == 1
    assert "PM 4차 (task:mytask)" not in log_text
    assert "**3차**" in state_text
    assert "- 차수: PM 3차" in dashboard_text
    assert "PM 999차" not in log_text + state_text + dashboard_text


def test_run_unregistered_task_fails_without_side_effects(
    hf, tmp_path, monkeypatch, capsys
):
    """handoff는 bootstrap이 만들지 않은 task를 거부하고 state/log/dashboard를 만들지 않는다."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    log_file = tmp_path / "current.md"
    dashboard_file = tmp_path / "dashboard.md"
    pytest_calls: list[bool] = []

    class _MissingTaskPool:
        @staticmethod
        def find_task(name):
            return None

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (pytest_calls.append(True) or (0, pytest_summary())),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=tmp_path / "unused-playbook.md",
        dashboard_file=dashboard_file,
        worktree_pool=_MissingTaskPool(),
    )

    rc = handoff.run(
        session_num=999,
        wave_summary="오타",
        dry_run=False,
        skip_pytest=False,
        task="not-registered",
    )

    assert rc != 0
    assert pytest_calls == []
    assert not log_file.exists()
    assert not dashboard_file.exists()
    assert not (tmp_path / ".project_manager" / ".local" / "tasks" / "not-registered").exists()
    assert "미등록 task" in capsys.readouterr().err


def test_run_task_records_precreated_pm_state(hf, tmp_path, capsys, monkeypatch):
    """첫 task handoff는 task 생성 시 이미 존재하는 pm_state marker를 1차 기록으로 갱신한다."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    log_file = tmp_path / "current.md"
    dashboard_file = tmp_path / "dashboard.md"

    # lease 유지 증거 — release 가 불리면 fail 하는 sentinel worktree_pool(done=False 라 미호출 기대).
    class _NoReleasePool:
        def find_task(self, name):
            return type("_T", (), {"name": name, "pid": 12345})()

        def slots_for_task(self, name):
            return []

        def release_task_pid(self, name):
            return type("_T", (), {"name": name, "pid": 0})()

        def release(self, *a, **k):  # noqa: ANN002 ANN003
            raise AssertionError("task 세션 종료가 lease 를 release 하면 안 된다(F7·세션종료≠task종료)")

    task_pm_state = (
        tmp_path / ".project_manager" / ".local" / "tasks" / "mytask" / "pm_state.md"
    )
    task_pm_state.parent.mkdir(parents=True, exist_ok=True)
    task_pm_state.write_text(
        _state().replace(
            "최근 N 차 (sliding window, 기본 3 차):\n",
            "최근 N 차 (sliding window, 기본 3 차):\n"
            f"{hf.TASK_PM_STATE_EMPTY_MARKER}\n",
        ),
        encoding="utf-8",
    )

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        dashboard_file=dashboard_file,
        worktree_pool=_NoReleasePool(),
    )
    rc = handoff.run(
        session_num=1, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    # task pm_state marker가 첫 실제 세션 entry로 전환.
    assert task_pm_state.exists()
    state_text = task_pm_state.read_text(encoding="utf-8")
    assert "1차" in state_text
    assert hf.TASK_PM_STATE_EMPTY_MARKER not in state_text
    # dashboard 자기 섹션 `## mytask`.
    assert "## mytask" in dashboard_file.read_text(encoding="utf-8")
    # log 헤더 태그 = sentinel `(task:mytask)`.
    assert "PM 1차 (task:mytask)" in log_file.read_text(encoding="utf-8")


# ── 핸드오프 완료 git 재스냅 ("여기 두고 간다"·T-0388) ─────────────────────────────
#
# 세션 중 브랜치/HEAD 가 바뀌면(예: 릴리즈) bind 의 옛 도착 스냅만 남아 차기 부트스트랩 0단계
# record-vs-live 정합이 `diverged` FAIL-LOUD 로 정당한 진행을 외부-개입 오경보로 차단한다(PM 78
# 실측). 핸드오프 부기(log·pm_state) 완료 후 bound 슬롯의 live git 을 lease.git 에 재기록해 이를
# 닫는다. base 미전달(기존 보존)·판정 재구현 없이 T-0350 write 프리미티브(`record_git_snapshot`)만
# 호출·솔로/미바인딩/장부 부재/--done(release→idle)은 fail-soft 로 제외.


class _SnapPool:
    """record_git_snapshot 호출을 기록하는 hermetic mock worktree_pool (T-0388 배선 검증).

    `return_none` 이면 record_git_snapshot 이 None(장부에 슬롯 없음)을 돌려줘 fail-soft 경로를
    모델링한다. release/current_branch 는 --done 경로 공존 검증용(release 시 재스냅 skip 대조).
    """

    def __init__(self, *, lease_git=None, before_git=None, return_none=False):
        self.snap_calls: list[tuple] = []
        self.release_calls: list[tuple] = []
        self._lease_git = lease_git or {"branch": "v1.3.3", "head": "f0cd6cf"}
        # 재스냅 *전* lease.git (실갱신/무변경 판별·T-0391). 기본은 after 와 달라 실갱신으로 읽힌다.
        self._before_git = before_git or {"branch": "v1.3.2", "head": "old0000"}
        self._return_none = return_none

    def read_lease(self, slot):
        return type("_L", (), {"git": self._before_git})()

    def find_task(self, name):
        return type("_T", (), {"name": name})()

    def record_git_snapshot(self, slot, **kwargs):
        self.snap_calls.append((slot, kwargs))
        if self._return_none:
            return None
        return type("_L", (), {"git": self._lease_git})()

    def release(self, slot, **kwargs):
        self.release_calls.append((slot, kwargs))
        return type("_L", (), {"state": "idle", "git": None})()

    def current_branch(self, slot, **kwargs):
        return "v1.3.3"


def _hermetic_handoff(hf, tmp_path, pool, *, run_git_fn=None, raw_ledger_file=None,
                      peer_raw_ledger_files=()):
    """명시 pm_state/log/dashboard 주입 + mock worktree_pool 로 hermetic PmHandoff 구성.

    `raw_ledger_file` 미지정이면 tmp 의 **없는** 경로를 준다 — [6b/7] 미마감 raw sweep 이
    실 PM 홈 장부를 읽지 않게 하는 격리다(sweep 자체 검증은 명시 주입 케이스가 한다).
    `peer_raw_ledger_files` 기본 `()` 도 같은 격리다 — 사본 장부 병기가 실 트리 탐색
    (`pm_delegate._peer_engine_ledgers`)으로 새지 않게 한다.
    """
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    return hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=run_git_fn or (lambda args: (0, "")),
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
        raw_ledger_file=raw_ledger_file or (tmp_path / "absent_raw_outputs.json"),
        peer_raw_ledger_files=peer_raw_ledger_files,
    )


# ── 같은-차수 handoff 재실행 멱등성 (T-0588) ───────────────────────────────

def _run_slot_handoff(handoff, session_num: int, *, dry_run: bool = False) -> int:
    return handoff.run(
        session_num=session_num,
        wave_summary=f"{session_num}차 요약",
        dry_run=dry_run,
        skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )


def test_same_session_rerun_keeps_one_entry_and_shifts_window_once(
    hf, tmp_path, capsys
):
    """같은 차수 2회 실실행은 handoff 1개·window shift 1회이고 갱신 모드를 밝힌다."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())

    assert _run_slot_handoff(handoff, 7) == 0
    after_first_state = handoff._pm_state_file.read_text(encoding="utf-8")
    capsys.readouterr()
    assert _run_slot_handoff(handoff, 7) == 0

    log_text = handoff._log_file.read_text(encoding="utf-8")
    after_second_state = handoff._pm_state_file.read_text(encoding="utf-8")
    assert log_text.count("handoff | PM 7차 (project_manager_1)") == 1
    assert after_second_state == after_first_state
    assert "**4차**" not in after_second_state
    assert all(f"**{num}차**" in after_second_state for num in (5, 6, 7))
    assert "이전 차 (PM 1차~4차)" in after_second_state
    out = capsys.readouterr().out
    assert "[모드] 같은 차수(7) 재실행 — entry 갱신·윈도 shift 생략" in out
    assert "세션 window shift 생략" in out


def test_same_session_rerun_refreshes_entries_added_after_first_handoff(
    hf, tmp_path, capsys
):
    """조기 handoff 뒤 추가된 complete는 재실행 시 기존 기계 목록에 합쳐진다."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())
    handoff._log_file.write_text(
        "## [2026-08-07] complete | T-0587 — 첫 박제 (project_manager_1)\n",
        encoding="utf-8",
    )
    assert _run_slot_handoff(handoff, 7) == 0
    first_log = handoff._log_file.read_text(encoding="utf-8")
    assert "T-0587 — 첫 박제" in first_log

    handoff._log_file.write_text(
        first_log
        + "\n## [2026-08-07] complete | T-0588 — 조기 handoff 뒤 완료 "
          "(project_manager_1)\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert _run_slot_handoff(handoff, 7) == 0

    refreshed = handoff._log_file.read_text(encoding="utf-8")
    assert refreshed.count("handoff | PM 7차 (project_manager_1)") == 1
    assert "  - ## [2026-08-07] complete | T-0587 — 첫 박제" in refreshed
    assert "  - ## [2026-08-07] complete | T-0588 — 조기 handoff 뒤 완료" in refreshed
    # 원본 top-level complete entry도 보존되고 기계 목록에 1회 반영된다.
    assert refreshed.count("T-0588 — 조기 handoff 뒤 완료") == 2


def test_same_session_rerun_preserves_pm_authored_body(hf, tmp_path):
    """PM이 skeleton 본문을 채운 뒤 재실행해도 기계 목록 외 byte는 보존한다."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())
    assert _run_slot_handoff(handoff, 7) == 0
    authored = handoff._log_file.read_text(encoding="utf-8")
    authored = authored.replace(
        "- 메타 학습: <PM 손 — ticket 상태에서 도출 불가한 교훈만. 없으면 \"없음\".>",
        "- 메타 학습: 사람이 채운 비가역 서술",
    ).replace(
        f"- pending user intent: {hf.PENDING_INTENT_PLACEHOLDER}",
        "- pending user intent: 사용자 결정을 기다린다",
    ).replace(
        "- 회귀/incident: <PM 손 — 회귀 \"N passed / 상태\" 1줄(green 도 — baseline) + "
        "비-자명 incident. (회귀는 1줄 load-bearing 이라 항상 적음 — board/git/log 대량 "
        "재열거만 금지.)>",
        "- 회귀/incident: 123 passed / green",
    )
    handoff._log_file.write_text(authored, encoding="utf-8")

    assert _run_slot_handoff(handoff, 7) == 0
    refreshed = handoff._log_file.read_text(encoding="utf-8")
    assert "- 메타 학습: 사람이 채운 비가역 서술" in refreshed
    assert "- pending user intent: 사용자 결정을 기다린다" in refreshed
    assert "- 회귀/incident: 123 passed / green" in refreshed
    # 기계 소유 구획을 sentinel로 치환하면 entry의 나머지 bytes가 동일하다.
    assert hf._SESSION_ENTRIES_BLOCK_RE.sub("<MACHINE>", refreshed) == (
        hf._SESSION_ENTRIES_BLOCK_RE.sub("<MACHINE>", authored)
    )


def test_same_session_dry_run_previews_update_without_writes(hf, tmp_path, capsys):
    """dry-run도 같은 차수 갱신 모드를 미리 보이되 log/state를 쓰지 않는다."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())
    assert _run_slot_handoff(handoff, 7) == 0
    log_before = handoff._log_file.read_bytes()
    state_before = handoff._pm_state_file.read_bytes()
    capsys.readouterr()

    assert _run_slot_handoff(handoff, 7, dry_run=True) == 0

    assert handoff._log_file.read_bytes() == log_before
    assert handoff._pm_state_file.read_bytes() == state_before
    out = capsys.readouterr().out
    assert "[모드] 같은 차수(7) 재실행" in out
    assert "기계 소유 구획 갱신 미리보기" in out


def test_log_plan_and_append_share_one_lock_critical_section(hf, tmp_path):
    """첫 append 판정과 실제 O_APPEND가 같은 shared-lock 구간에 있다(TOCTOU 구조 차단)."""
    current = tmp_path / "current.md"
    events = []

    class FakePmLog:
        locked = False

        @contextmanager
        def log_write_lock(self, path):
            assert Path(path) == current
            assert not self.locked
            self.locked = True
            events.append("lock-enter")
            try:
                yield
            finally:
                events.append("lock-exit")
                self.locked = False

        def append_log_locked(self, path, text):
            assert self.locked, "append가 판정 lock 밖으로 빠짐"
            events.append("append")
            Path(path).write_bytes(text.encode("utf-8"))

        def _replace_atomic(self, path, text):
            assert self.locked, "replace가 판정 lock 밖으로 빠짐"
            events.append("replace")
            Path(path).write_bytes(text.encode("utf-8"))

    fake = FakePmLog()
    first = hf._commit_handoff_log_change(
        current, session_num=7, date="2026-08-07", worktree_slot=None,
        branch=None, session=None, task=None, pm_log_module=fake,
    )
    second = hf._commit_handoff_log_change(
        current, session_num=7, date="2026-08-07", worktree_slot=None,
        branch=None, session=None, task=None, pm_log_module=fake,
    )

    assert first[0] is False and second[0] is True
    assert events == [
        "lock-enter", "append", "lock-exit",
        "lock-enter", "replace", "lock-exit",
    ]
    assert current.read_text(encoding="utf-8").count("handoff | PM 7차") == 1


def test_concurrent_first_log_commits_cross_barrier_leave_one_entry(hf, tmp_path):
    """barrier로 동시에 출발한 첫 handoff 둘도 shared lock 재판정으로 entry 하나다."""
    current = tmp_path / "current.md"
    pm_log = _load_tool("pm_log")
    # worker 둘이 동시에 lazy import를 타면 importlib/sys.modules 경합을
    # log lock 경합으로 오인한다. 실제 임계구역 테스트 전 dependency를 선로드한다.
    assert pm_log._load_file_lock() is not None
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=5)
        return hf._commit_handoff_log_change(
            current, session_num=7, date="2026-08-07", worktree_slot=None,
            branch=None, session=None, task=None, pm_log_module=pm_log,
        )[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: worker(), range(2)))

    assert sorted(results) == [False, True]
    assert current.read_text(encoding="utf-8").count("handoff | PM 7차") == 1


def test_same_session_update_preserves_crlf_mixed_pm_body_bytes(hf, tmp_path):
    """CRLF/mixed log도 machine block 바깥 PM 한글 본문 bytes를 그대로 둔다."""
    current = tmp_path / "current.md"
    original = (
        "## [2026-08-07] handoff | PM 7차 → 다음 PM 세션\r\n"
        "\r\n"
        "- 이 세션 박제 entries: (이 세션 박제 entry 없음)\r\n"
        "- 메타 학습: 사람이 쓴 한글 본문\r\n"
        "- pending user intent: 혼합 개행은 보존\n"
        "- 회귀/incident: 123 passed / green\r\n"
        "\n"
        "## [2026-08-07] complete | T-0588 — 뒤늦은 완료\r\n"
    )
    current.write_bytes(original.encode("utf-8"))
    before_match = hf._SESSION_ENTRIES_BLOCK_RE.search(original)
    assert before_match is not None
    prefix = original[:before_match.start()].encode("utf-8")
    suffix = original[before_match.end():].encode("utf-8")

    same, _preview, warning = hf._commit_handoff_log_change(
        current, session_num=7, date="2026-08-07", worktree_slot=None,
        branch=None, session=None, task=None,
    )

    updated = current.read_bytes()
    assert same is True and warning is None
    assert updated.startswith(prefix) and updated.endswith(suffix)
    assert "사람이 쓴 한글 본문".encode("utf-8") in updated
    assert b"\r\n" in updated and b"\n" in updated


def test_same_session_update_preserves_pure_crlf_machine_boundary(hf, tmp_path):
    """순수 CRLF log의 machine block 마지막 구분 개행도 bare LF로 바뀌지 않는다."""
    current = tmp_path / "current.md"
    original = (
        "## [2026-08-07] handoff | PM 7차 → 다음 PM 세션\r\n"
        "\r\n"
        "- 이 세션 박제 entries: (이 세션 박제 entry 없음)\r\n"
        "- 메타 학습: 순수 CRLF 본문\r\n"
        "- pending user intent: 보존\r\n"
        "- 회귀/incident: green\r\n"
        "\r\n"
        "## [2026-08-07] complete | T-0588 — 뒤늦은 완료\r\n"
    )
    current.write_bytes(original.encode("utf-8"))

    same, _preview, warning = hf._commit_handoff_log_change(
        current, session_num=7, date="2026-08-07", worktree_slot=None,
        branch=None, session=None, task=None,
    )

    updated = current.read_bytes()
    assert same is True and warning is None
    assert "순수 CRLF 본문".encode("utf-8") in updated
    assert all(index > 0 and updated[index - 1] == 0x0D
               for index, byte in enumerate(updated) if byte == 0x0A)


def test_task_session_inference_advances_after_bootstrap_but_reruns_when_released(hf):
    """log N은 bind 상태에서 N+1, handoff intent(pid=0) 상태에서 N 재실행이다."""
    state = (
        "# pm_state\n\n"
        "## 세션 식별 (현재까지 사용된 이름)\n"
        "  - **1차** stale state\n"
    )
    log = (
        "# log\n\n"
        "## [2026-08-07] handoff | PM 3차 (task:job1) → 다음 PM 세션\n"
    )
    assert hf.infer_next_task_session_num(state, log, "job1") == 4
    assert hf.infer_next_task_session_num(
        state, log, "job1", task_released=True
    ) == 3


def test_task_cli_two_process_rerun_is_idempotent_then_bootstrap_advances(tmp_path):
    """실제 adopter CLI: 같은 task handoff 두 프로세스는 1개, 새 bootstrap 뒤엔 N+1."""
    dest = tmp_path / "task-adopter"
    env = {**os.environ, "PM_NONINTERACTIVE": "1"}

    def run(tool, *args):
        return subprocess.run(
            [sys.executable, str(dest / ".project_manager" / "tools" / tool), *args],
            cwd=str(dest), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
        )

    install = subprocess.run(
        [
            sys.executable, str(TOOLS / "pm_import.py"), "--new", str(dest),
            "--harness", "codex", "--name", "handoff-task-e2e", "--fill", "manual",
        ],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    boot1 = run("pm_bootstrap.py", "--task", "mytask", "--json")
    assert boot1.returncode == 0, boot1.stdout + boot1.stderr
    first = run("pm_handoff.py", "--task", "mytask", "--no-pytest")
    second = run("pm_handoff.py", "--task", "mytask", "--no-pytest")
    assert first.returncode == second.returncode == 0, (
        first.stdout + first.stderr + second.stdout + second.stderr
    )
    assert "[모드] 같은 차수(1) 재실행" in second.stdout

    log_file = dest / ".project_manager" / "wiki" / "log" / "current.md"
    task_state = (
        dest / ".project_manager" / ".local" / "tasks" / "mytask" / "pm_state.md"
    )
    assert log_file.read_text(encoding="utf-8").count(
        "handoff | PM 1차 (task:mytask)"
    ) == 1
    once_shifted = task_state.read_bytes()

    # 진짜 다음 세션은 bootstrap bind가 pid를 다시 채운 뒤 N+1로 전진한다.
    boot2 = run("pm_bootstrap.py", "--task", "mytask", "--json")
    assert boot2.returncode == 0, boot2.stdout + boot2.stderr
    third = run("pm_handoff.py", "--task", "mytask", "--no-pytest")
    assert third.returncode == 0, third.stdout + third.stderr
    final_log = log_file.read_text(encoding="utf-8")
    assert final_log.count("handoff | PM 1차 (task:mytask)") == 1
    assert final_log.count("handoff | PM 2차 (task:mytask)") == 1
    assert task_state.read_bytes() != once_shifted


def test_malformed_last_handoff_number_falls_back_to_append_with_loud_warning(
    hf, tmp_path, capsys
):
    """마지막 자기 handoff 차수 파싱 실패는 기존 append 동작과 loud 위험 경고를 유지한다."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())
    handoff._log_file.write_text(
        "## [2026-08-07] handoff | PM seven차 (project_manager_1) → 다음 PM 세션\n\n"
        "- 이 세션 박제 entries: (이 세션 박제 entry 없음)\n"
        "- 메타 학습: 기존 본문\n",
        encoding="utf-8",
    )

    assert _run_slot_handoff(handoff, 7) == 0

    log_text = handoff._log_file.read_text(encoding="utf-8")
    assert "PM seven차 (project_manager_1)" in log_text
    assert "PM 7차 (project_manager_1)" in log_text
    err = capsys.readouterr().err
    assert "마지막 handoff 헤더 차수 파싱 실패" in err
    assert "이중 부기 가능" in err


def test_different_session_number_keeps_append_and_window_behavior(hf, tmp_path):
    """다른 차수는 기존처럼 새 entry append와 다음 window shift를 수행한다."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())
    assert _run_slot_handoff(handoff, 7) == 0
    assert _run_slot_handoff(handoff, 8) == 0

    log_text = handoff._log_file.read_text(encoding="utf-8")
    state_text = handoff._pm_state_file.read_text(encoding="utf-8")
    assert log_text.count("handoff | PM 7차 (project_manager_1)") == 1
    assert log_text.count("handoff | PM 8차 (project_manager_1)") == 1
    assert "**5차**" not in state_text
    assert all(f"**{num}차**" in state_text for num in (6, 7, 8))
    assert "이전 차 (PM 1차~5차)" in state_text


def test_run_prints_pathspec_commit_guidance(hf, tmp_path, capsys):
    """[7/7] 커밋 안내가 **경로 명시형**으로 실제 출력된다 (ADR-0074·T-0425).

    bare `git commit` 지시는 다른 슬롯이 index 에 올려둔 남의 변경까지 함께 싣는다 — 스킬 문서만
    pathspec 형으로 고치고 PM 이 마지막에 읽는 이 화면을 두면 사람 손이 그대로 샌다.
    **런타임 출력으로 묻는다** — 소스 문자열 assert 는 `print` 를 dead-code 로 만들어도 통과해
    가드가 아니다(reviewer probe 실증).
    """
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool())
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "git commit" in out, "커밋 안내가 런타임에 아예 안 나온다"
    guidance = next(line for line in out.splitlines()
                    if "git commit" in line and "[ ]" in line)
    assert "-- " in guidance, f"bare commit 안내(경로 미명시): {guidance!r}"
    assert ".project_manager/wiki/log/current.md" in guidance, \
        f"이 도구가 실제로 쓰는 산출물 경로가 안내에 없다: {guidance!r}"


def test_run_records_slot_snapshot_after_bookkeeping(hf, tmp_path, capsys):
    """핸드오프가 부기 완료 후 bound 슬롯으로 record_git_snapshot 을 1회 호출한다 (base 미전달·arrival 보존)."""
    pool = _SnapPool()
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0
    # bound 슬롯으로 정확히 1회·base kwarg 미전달(기존 base 보존·arrival 동형·판정 재구현 없음).
    assert pool.snap_calls == [("work/project_manager_1", {})]
    out = capsys.readouterr().out
    # 재스냅 시점 = 부기([2/7] log·[3/7] pm_state) 완료 후 — 출력 순서로 확인.
    assert out.index("[재스냅]") > out.index("[7/7]")
    # before(v1.3.2) ≠ after(v1.3.3) → 실갱신 표기(옛 값 성공 위장 아님·T-0391).
    assert "git 재스냅 기록: work/project_manager_1" in out
    assert "실갱신" in out


def test_run_snapshot_no_change_distinguished_from_real_update(hf, tmp_path, capsys):
    """T-0391 ③: 재스냅 전/후 lease.git 동일(스냅 불가·기존 유지)이면 "무변경"으로 구분 출력(옛 값 성공 위장 금지)."""
    same = {"branch": "v1.3.3", "head": "f0cd6cf"}
    pool = _SnapPool(before_git=same, lease_git=same)   # before == after → 스냅 불가·무변경 모델.
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "git 재스냅 무변경" in out
    assert "기존 기록 유지" in out
    assert "실갱신" not in out           # 무변경을 성공(실갱신)으로 오표기하지 않는다.


def test_run_snapshot_failsoft_when_ledger_missing_slot(hf, tmp_path, capsys):
    """record_git_snapshot 이 None(슬롯 미바인딩/장부 부재)이면 무해 skip·핸드오프 완주 (fail-soft)."""
    pool = _SnapPool(return_none=True)
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0                       # fail-soft — 재스냅 실패가 핸드오프를 막지 않는다.
    assert pool.snap_calls == [("work/project_manager_1", {})]
    assert "리스 장부에 없음" in capsys.readouterr().err


def test_run_solo_no_slot_skips_snapshot(hf, tmp_path):
    """솔로(슬롯 미해소·self._worktree_slot None) — 재스냅 자체를 시도하지 않는다(무회귀)."""
    pool = _SnapPool()
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True)
    assert rc == 0
    assert pool.snap_calls == []


def test_run_dry_run_previews_snapshot_without_call(hf, tmp_path, capsys):
    """dry_run — 재스냅 예고만 출력하고 write 프리미티브는 호출하지 않는다(미리보기)."""
    pool = _SnapPool()
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=True, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0
    assert pool.snap_calls == []
    assert "[dry-run] git 재스냅 예고" in capsys.readouterr().out


def test_run_done_release_skips_snapshot(hf, tmp_path):
    """--done(release→idle·git 정리) 경로는 재스냅 대상이 아니다 — idle 슬롯은 활성 git 기대가 없다."""
    pool = _SnapPool()
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1", done=True,
    )
    assert rc == 0
    # release 는 호출·재스냅은 skip(다음 alloc 이 arrival 재스냅으로 덮으므로 무의미+idle git 무결성).
    assert pool.release_calls == [("work/project_manager_1", {"require_clean": False})]
    assert pool.snap_calls == []


def test_snapshot_updates_lease_and_next_phase0_passes(hf, tmp_path):
    """PM 78 재현(real git + real worktree_pool) — 세션 중 브랜치 변경 후 핸드오프 재스냅이 차기
    부트스트랩 0단계(compare_slot_git) 외부-개입 오탐을 닫는다.

    도착 스냅(v1.3.2@c1)만 남은 채 세션 중 v1.3.3@c2 로 브랜치가 바뀌면 재스냅 전 0단계 compare
    는 branch 변경 → `diverged` FAIL-LOUD(버그 재현). 핸드오프 재스냅이 lease.git 을 live(v1.3.3@c2)
    로 갱신(base=v1.3.2@c1 보존)하면 0단계가 `match` 로 통과한다."""
    import importlib.util as _ilu
    import subprocess as _sp

    proj = tmp_path / "proj"
    local = proj / ".project_manager" / ".local"
    local.mkdir(parents=True, exist_ok=True)
    (proj / "work").mkdir(parents=True, exist_ok=True)
    spec = _ilu.spec_from_file_location("wp_h388", TOOLS / "worktree_pool.py")
    wp = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wp)
    for _name, _val in {
        "REPO": proj, "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "WORK_DIR": proj / "work",
    }.items():
        setattr(wp, _name, _val)

    slot_dir = proj / "work" / "project_manager_1"
    slot_dir.mkdir(parents=True)

    def _git(*argv):
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        })
        return _sp.run(["git", "-C", str(slot_dir), *argv], check=True,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=env)

    _git("init", "-q", "-b", "v1.3.2")
    (slot_dir / "f.txt").write_text("a\n", encoding="utf-8")
    _git("add", "f.txt")
    _git("commit", "-q", "-m", "c1")
    c1 = _git("rev-parse", "HEAD").stdout.strip()

    # 리스 seed + arrival 스냅(v1.3.2@c1·base 기록) — 부트스트랩 bind 도착 시점 모사.
    lease = wp.Lease(slot="work/project_manager_1", repo="project_manager",
                     session="project_manager_1", pid=os.getpid(), started="t", state="leased")
    with wp._lease_lock():
        wp._write_ledger([lease])
    wp.record_git_snapshot("work/project_manager_1", base_branch="v1.3.2")

    # 세션 중 브랜치 변경 — v1.3.2 → v1.3.3 (릴리즈 커밋 모사).
    _git("checkout", "-q", "-b", "v1.3.3")
    (slot_dir / "f.txt").write_text("b\n", encoding="utf-8")
    _git("add", "f.txt")
    _git("commit", "-q", "-m", "c2")
    c2 = _git("rev-parse", "HEAD").stdout.strip()

    # 재스냅 전 — 차기 0단계 compare 는 브랜치 변경 → diverged FAIL-LOUD (버그 재현).
    before = wp.compare_slot_git("work/project_manager_1")
    assert not before.branch_match
    assert before.head_relation == wp.HEAD_DIVERGED

    # 핸드오프 — 재스냅 배선(real pool·git_runner 미전달=실 git)이 lease.git 을 live 로 갱신.
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=wp,
    )
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0

    # lease.git 이 live branch/head 로 갱신 + base(v1.3.2@c1)는 보존(base 미전달·arrival 동형).
    updated = wp.list_leases()[0]
    assert updated.git["branch"] == "v1.3.3"
    assert updated.git["head"] == c2
    assert updated.git["base"] == {"branch": "v1.3.2", "commit": c1}

    # 재스냅 후 — 차기 0단계 compare 통과(match·외부-개입 오탐 폐쇄).
    after = wp.compare_slot_git("work/project_manager_1")
    assert after.branch_match
    assert after.head_relation == wp.HEAD_MATCH


# ── task handoff intent pid=0 선행 기록 ("여기 두고 간다"의 task 판·T-0392) ────────────
#
# task 장부 pid = dump 후 즉사하는 bootstrap subprocess pid(㉑·T-0353)라, pm_handoff 가 종료를 안
# 기록하면 정상 인계 후 재개도 dead-pid → bind_task `reclaimed`("재개(회수·이전 세션 crash)" +
# "⚠️ 회수 진입") 로 상시 오탐한다(PM 78 실측). 이제 같은-task 재실행 판정이
# released 상태에 의존하므로 log 쓰기 전 pid=0(미점유)을 먼저 남긴다. 이 intent 기록은
# load-bearing·fail-loud이고, dry_run은 예고만, slot/솔로 모드(--task 없음)는 무영향이다.


class _TaskPidPool(_SnapPool):
    """_SnapPool + release_task_pid 호출 기록 (T-0392 task pid 배선 검증).

    `omit_task_pid` 면 release_task_pid 속성 미노출(구버전 풀·fail-loud 모델).
    `task_pid_none` 면 release_task_pid 가 None(task 장부 부재) 반환(fail-loud 모델)."""

    def __init__(self, *, omit_task_pid=False, task_pid_none=False,
                 task_pid_error=False, **kwargs):
        super().__init__(**kwargs)
        self.task_pid_calls: list[str] = []
        self._task_pid_none = task_pid_none
        self._task_pid_error = task_pid_error
        if not omit_task_pid:
            self.release_task_pid = self._do_release_task_pid

    def _do_release_task_pid(self, name):
        self.task_pid_calls.append(name)
        if self._task_pid_error:
            raise OSError("ledger unavailable")
        if self._task_pid_none:
            return None
        return type("_T", (), {"name": name, "pid": 0})()

    def slots_for_task(self, name):
        return []


def _task_mode_handoff(hf, tmp_path, monkeypatch, pool):
    """task_mode(=명시 pm_state 미주입·--task) 진입 hermetic PmHandoff (T-0392).

    task_mode = task not None AND pm_state 미명시 → REPO monkeypatch + task 생성 시 이미 만들어진
    state로 실 앵커 해소를 태우되 mock pool로 release_task_pid 배선만 격리한다."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    state = hf._task_pm_state_file("mytask")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    return hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
    )


def test_run_task_mode_records_task_pid_before_log_bookkeeping(hf, tmp_path, monkeypatch, capsys):
    """task pid=0 intent는 log보다 먼저 기록돼 이후 중단도 같은 차수로 복구된다."""
    pool = _TaskPidPool()
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.task_pid_calls == ["mytask"]
    out = capsys.readouterr().out
    assert out.index("[task] handoff intent pid=0 기록") < out.index("[2/7]")
    assert "task handoff intent 기록: mytask → pid=0" in out


def test_run_task_mode_dry_run_previews_task_pid_without_call(hf, tmp_path, monkeypatch, capsys):
    """dry_run task 모드 — pid=0 기록 예고만 출력·write 프리미티브 미호출(미리보기)."""
    pool = _TaskPidPool()
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=True, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.task_pid_calls == []
    assert "[dry-run] task pid=0(미점유) 기록 예고: mytask" in capsys.readouterr().out


def test_run_task_mode_blocks_before_log_when_task_intent_record_absent(
    hf, tmp_path, monkeypatch, capsys
):
    """release_task_pid가 None이면 load-bearing intent 부재로 log write 전 차단한다."""
    pool = _TaskPidPool(task_pid_none=True)
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 1
    assert pool.task_pid_calls == ["mytask"]
    assert "장부에 없음" in capsys.readouterr().err
    assert not handoff._log_file.exists()


def test_run_task_mode_blocks_before_log_when_pool_lacks_intent_primitive(
    hf, tmp_path, monkeypatch, capsys
):
    """구버전 풀은 멱등성 intent를 못 남기므로 log write 전 차단한다."""
    pool = _TaskPidPool(omit_task_pid=True)
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 1
    assert pool.task_pid_calls == []
    assert "구버전" in capsys.readouterr().err
    assert not handoff._log_file.exists()


def test_run_task_mode_blocks_before_log_when_intent_write_raises(
    hf, tmp_path, monkeypatch, capsys
):
    pool = _TaskPidPool(task_pid_error=True)
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 1
    assert "ledger unavailable" in capsys.readouterr().err
    assert not handoff._log_file.exists()


def test_run_slot_mode_no_task_does_not_release_task_pid(hf, tmp_path):
    """슬롯 모드(--repo/--slot·--task 없음·task_mode False) — release_task_pid 미호출(무영향·T-0392).

    슬롯 재스냅(T-0388)은 정상 동작하되 task pid 기록은 task 모드 전용이라 건드리지 않는다."""
    pool = _TaskPidPool()
    handoff = _hermetic_handoff(hf, tmp_path, pool)   # 명시 pm_state → task_mode 진입 자체 없음.
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",
    )
    assert rc == 0
    assert pool.snap_calls == [("work/project_manager_1", {})]   # 슬롯 재스냅은 정상 동작
    assert pool.task_pid_calls == []                             # task pid 는 미호출


def test_run_solo_no_task_does_not_release_task_pid(hf, tmp_path):
    """솔로(슬롯 미해소·--task 없음) — release_task_pid 미호출(task_mode False·무영향·T-0392)."""
    pool = _TaskPidPool()
    handoff = _hermetic_handoff(hf, tmp_path, pool)
    rc = handoff.run(session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True)
    assert rc == 0
    assert pool.task_pid_calls == []


# ── task 퇴장: 변경 슬롯 회귀(F6) + 보유 전 슬롯 재스냅 (ADR-0068 W2·T-0393) ─────────────
#
# task 세션은 보유 슬롯 **집합**을 두고 나간다: ① 회귀는 변경 흔적(lease 스냅 대비 head 전진/dirty)
# 있는 보유 슬롯 각각에서(F6 해소·명시 --repo/--slot 우선 유지·0개=skip)·② 재스냅은 보유 전 슬롯
# (현행 1슬롯 한정 폐지). slot/솔로 모드(--task 없음)는 100% 불변.


class _TaskSetPool(_SnapPool):
    """task 다중슬롯 회귀/재스냅 mock — slots_for_task + compare_slot_git + slot_git_status (T-0393).

    `slots` = slots_for_task 가 돌려줄 보유 슬롯 식별자 리스트. `states` = slot→변경 상태 dict
    (`unrecorded`/`branch_match`/`head_relation`/`dirty`·미지정은 head match·clean·recorded=무변경).
    compare_slot_git·slot_git_status 를 mock 해 `_slot_has_changes` 판정을 격리 검증한다. 재스냅
    프리미티브(read_lease·record_git_snapshot)는 _SnapPool 상속(snap_calls 기록)."""

    HEAD_MATCH = "match"

    def __init__(self, slots, states=None, **kwargs):
        super().__init__(**kwargs)
        self._task_slots = slots
        self._states = states or {}

    def slots_for_task(self, name):
        return [type("_L", (), {"slot": s})() for s in self._task_slots]

    def lease_owned_by_task_strict(self, slot, task):
        return slot in self._task_slots and task == "mytask"

    def compare_slot_git(self, slot):
        st = self._states.get(slot, {})
        return type("_C", (), {
            "unrecorded": st.get("unrecorded", False),
            "branch_match": st.get("branch_match", True),
            "head_relation": st.get("head_relation", "match"),
        })()

    def slot_git_status(self, slot):
        return {"dirty": self._states.get(slot, {}).get("dirty", False)}


def _task_reg_handoff(hf, tmp_path, pool, pytest_fn):
    """task 회귀/재스냅 검증용 hermetic PmHandoff — 명시 pm_state(hermetic) + mock pool + 관찰 pytest_fn.

    명시 pm_state 주입이라 task_mode(pid 기록)는 진입 안 하지만, task 회귀 모드(task_regression =
    task not None AND 명시 slot 없음)와 보유 전 슬롯 재스냅은 태운다(그 두 축이 이 파일 검증 대상)."""
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    return hf.PmHandoff(
        run_pytest_fn=pytest_fn,
        run_git_fn=lambda args: (0, ""),
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
    )


def _recording_pytest(handoff_box, result=(0, "1 passed in 0.01s\n")):
    """호출 시점 self._worktree_slot 을 기록하는 pytest_fn 팩토리 (per-slot cwd 관찰용).

    `handoff_box` = [handoff] 1-원소 리스트(핸드오프를 나중에 담아 클로저가 참조). 반환 러너는
    호출마다 현재 _worktree_slot(회귀 cwd 해소 입력)을 cwds 에 append 한다."""
    cwds: list = []

    def _run():
        cwds.append(handoff_box[0]._worktree_slot)
        return result

    return _run, cwds


def test_task_state_ensure_failure_leaves_log_and_dashboard_untouched(
    hf, tmp_path, monkeypatch, capsys
):
    """회귀 통과 후 task state backfill 실패 시 첫 외부 write 전에 중단한다(MF-b).

    기존 log/dashboard bytes를 sentinel로 두고 ensure 실패를 주입해 재시도 중복 원인을 막는다.
    """
    monkeypatch.setattr(hf, "REPO", tmp_path)
    log_file = tmp_path / "current.md"
    dashboard_file = tmp_path / "dashboard.md"
    log_file.write_text("LOG-SENTINEL\n", encoding="utf-8")
    dashboard_file.write_text("DASH-SENTINEL\n", encoding="utf-8")
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")

    class _FailEnsurePool(_TaskSetPool):
        def ensure_task_pm_state(self, name):
            raise OSError("injected ensure failure")

    pool = _FailEnsurePool([])
    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        dashboard_file=dashboard_file,
        worktree_pool=pool,
    )

    rc = handoff.run(
        session_num=999,
        wave_summary="요약",
        dry_run=False,
        skip_pytest=True,
        task="mytask",
    )

    assert rc == 1
    assert log_file.read_text(encoding="utf-8") == "LOG-SENTINEL\n"
    assert dashboard_file.read_text(encoding="utf-8") == "DASH-SENTINEL\n"
    assert "task pm_state 생성 실패" in capsys.readouterr().err


def test_task_regression_runs_in_held_workspace(hf, tmp_path, capsys):
    """task-only 핸드오프가 변경 흔적 있는 보유 슬롯(작업공간)에서 회귀를 돌린다 — F6 해소(T-0393)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool(["work/project_manager_1"],
                        states={"work/project_manager_1": {"dirty": True}})   # dirty=변경.
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    # 회귀가 task 보유 슬롯 worktree(=cwd 해소 입력)에서 1회 — REPO 폴백("no tests ran") 아님.
    assert cwds == ["work/project_manager_1"]
    out = capsys.readouterr().out
    assert "▷ work/project_manager_1 회귀" in out
    assert "dirty" in out


def test_task_regression_runs_each_changed_slot(hf, tmp_path, capsys):
    """보유 슬롯 2+ — 변경 흔적 있는 슬롯 각각에서 회귀(집합 처리·T-0393·ADR-0068 ⓑB)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool(
        ["work/a_1", "work/b_1"],
        states={"work/a_1": {"dirty": True},
                "work/b_1": {"head_relation": "descendant"}},   # 커밋 전진.
    )
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    assert cwds == ["work/a_1", "work/b_1"]   # 두 변경 슬롯 각각에서.


def test_task_regression_skips_unchanged_slots(hf, tmp_path, capsys):
    """무변경 슬롯(head match·clean·recorded)은 회귀 skip — 변경 0개면 명시 skip·회귀 실행 없음(신호 0)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool(["work/a_1"], states={})   # 기본=match·clean·recorded → 무변경.
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    assert cwds == []   # 회귀 자체가 실행되지 않는다.
    out = capsys.readouterr().out
    assert "변경 흔적 없음" in out
    assert "변경 슬롯 없음" in out


def test_task_regression_mixed_only_changed_runs(hf, tmp_path, capsys):
    """변경+무변경 혼재 — 변경 슬롯만 회귀·무변경은 skip 사유 출력(T-0393)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool(
        ["work/a_1", "work/b_1"],
        states={"work/a_1": {"dirty": True}},   # b_1 미지정=무변경.
    )
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    assert cwds == ["work/a_1"]   # 변경 슬롯만.
    out = capsys.readouterr().out
    assert "work/b_1 — 변경 흔적 없음" in out


def test_task_regression_unrecorded_slot_conservatively_included(hf, tmp_path):
    """스냅 미기록(unrecorded) 슬롯은 보수적으로 변경 취급 → 회귀 포함(직전 green 근거 없음·T-0393)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool(["work/a_1"], states={"work/a_1": {"unrecorded": True}})
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    assert cwds == ["work/a_1"]


def test_task_regression_red_slot_blocks_handoff(hf, tmp_path, capsys):
    """변경 슬롯 회귀가 red 면 핸드오프 차단(rc 1)·부기 미접촉(T-0393·회귀 게이트 불변)."""
    box = [None]
    pytest_fn, _cwds = _recording_pytest(box, result=(1, "1 failed in 0.01s\n"))
    pool = _TaskSetPool(["work/a_1"], states={"work/a_1": {"dirty": True}})
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 1
    assert "회귀 red" in capsys.readouterr().err


def test_task_regression_zero_held_slots_skips(hf, tmp_path, capsys):
    """보유 슬롯 0개 — 회귀 대상 없음 명시 skip(REPO 폴백 red 아님)·핸드오프 완주(T-0393)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool([])   # 보유 0개.
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    assert cwds == []
    out = capsys.readouterr().out
    assert "보유 슬롯 0개" in out
    assert "/pm-env alloc <repo> --task <이름>" in out
    assert "`--repo/--slot` 명시" not in out


def test_task_slots_resolution_exception_fails_before_persistent_records(hf, tmp_path, capsys):
    """slots_for_task 예외는 실제 0슬롯이 아니다 — rc1이고 log/dashboard/state/snapshot 기록은 0."""

    class BrokenTaskSetPool(_TaskSetPool):
        def slots_for_task(self, name):
            raise OSError("ledger unreadable")

    pool = BrokenTaskSetPool([])
    handoff = _task_reg_handoff(
        hf,
        tmp_path,
        pool,
        lambda: (_ for _ in ()).throw(AssertionError("해소 실패 뒤 pytest 호출 금지")),
    )
    state_before = handoff._pm_state_file.read_text(encoding="utf-8")
    assert handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    ) == 1
    captured = capsys.readouterr()
    assert "보유 슬롯 장부 해소 실패" in captured.err
    assert "실제 0슬롯으로 간주하지 않습니다" in captured.err
    assert not handoff._log_file.exists()
    assert not handoff._dashboard_file.exists()
    assert handoff._pm_state_file.read_text(encoding="utf-8") == state_before
    assert pool.snap_calls == []


def test_corrupt_real_ledger_blocks_handoff_without_records(hf, tmp_path, capsys):
    """실 strict 파서가 손상 JSON을 task 미등록/0슬롯으로 낮추지 않고 모든 기록 전에 막는다."""
    # worktree_pool은 tmp 장부에 재배선할 독립 모듈로 직접 동적 로드한다.
    spec = importlib.util.spec_from_file_location(
        "wp_handoff_corrupt_real", TOOLS / "worktree_pool.py"
    )
    wp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wp)
    local = tmp_path / "real-local"
    ledger = local / "worktree-leases.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text('{"tasks": [', encoding="utf-8")
    for name, value in {
        "LEASES_FILE": ledger,
        "LEASES_LOCK": local / "worktree-leases.lock",
        "TASKS_DIR": local / "tasks",
        "WORK_DIR": tmp_path / "real-work",
    }.items():
        setattr(wp, name, value)
    before_ledger = ledger.read_bytes()
    handoff = _task_reg_handoff(
        hf,
        tmp_path,
        wp,
        lambda: (_ for _ in ()).throw(AssertionError("손상 장부 뒤 pytest 호출 금지")),
    )
    before_state = handoff._pm_state_file.read_text(encoding="utf-8")

    assert handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    ) == 1

    captured = capsys.readouterr()
    assert "task 장부 membership 조회 실패" in captured.err
    assert ledger.read_bytes() == before_ledger
    assert not handoff._log_file.exists()
    assert not handoff._dashboard_file.exists()
    assert handoff._pm_state_file.read_text(encoding="utf-8") == before_state


def test_task_regression_stale_slot_fails_loud_not_repo_green(hf, tmp_path, capsys):
    """stale 슬롯(장부엔 있으나 worktree dir 부재)이 변경으로 분류돼도 REPO 폴백 green 으로 핸드오프가
    통과하지 않는다 — fail-loud red 차단(vacuous-pass 금지·codex R3·T-0393·T-0220 클래스).

    `slot_path` 가 부재 경로를 돌려주는 pool → `_slot_worktree_missing` True → 회귀 실행 전 fail-loud."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)   # 실행되면 안 됨(REPO 폴백 회귀 금지).

    class _StaleSlotPool(_TaskSetPool):
        def slot_path(self, slot):
            return tmp_path / "gone" / slot   # 항상 부재(stale worktree).

    pool = _StaleSlotPool(["work/a_1"], states={"work/a_1": {"dirty": True}})   # 변경으로 분류.
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 1                       # fail-loud — REPO green 으로 통과 안 됨.
    assert cwds == []                    # 회귀(REPO 폴백조차) 미실행.
    err = capsys.readouterr().err
    assert "stale" in err
    assert "vacuous-pass" in err


def test_task_handoff_run_rejects_explicit_slot(hf, tmp_path, capsys):
    """run() 직접 호출도 task+slot 혼합을 부작용 전에 거부한다."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pool = _TaskSetPool(["work/a_1", "work/b_1"],
                        states={"work/a_1": {"dirty": True}, "work/b_1": {"dirty": True}})
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False,
        task="mytask", worktree_slot="work/project_manager_1",
    )
    assert rc == 1
    assert cwds == []
    captured = capsys.readouterr()
    assert "`--task <이름>` 정체성만 받는다" in captured.err
    assert captured.out == ""


def test_task_shipping_surface_per_changed_slot(hf, tmp_path, capsys, monkeypatch):
    """[1b] 출하 변경 surface 를 회귀가 돈 변경-슬롯 각각에서 돌린다 — 두 번째 슬롯만 출하 경로 변경
    stub → 그 슬롯이 surface 되는지(codex must-fix·집합 1급화 일관·T-0393).

    단일 _regression_cwd(None) 자동해소면 한 트리만 보고 두 번째 슬롯의 SHIPPING_GLOBS 변경을
    놓친다 — 변경-슬롯 집합(회귀와 공유)을 슬롯별로 돌려 각 worktree 의 출하 변경을 표시한다."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    # 회귀·출하 cwd 해소(_regression_cwd(slot)=REPO/slot)가 실 디렉토리로 해소되도록 슬롯 worktree 생성.
    (tmp_path / "work" / "a_1").mkdir(parents=True)
    (tmp_path / "work" / "b_1").mkdir(parents=True)

    def _git(args):
        # `git -C <worktree> diff --name-only HEAD` 만 관찰 — b_1 worktree 에서만 출하 경로(엔진 파일)
        # 변경을 돌려주고(그 슬롯=미검증 출하 변경), a_1·기타 명령은 빈 출력(비-출하).
        joined = " ".join(args)
        if "diff" in args and "--name-only" in args and "HEAD" in args and "..HEAD" not in joined:
            if str(tmp_path / "work" / "b_1") in args:
                return 0, ".project_manager/tools/board.py\n"
            return 0, ""
        return 0, ""   # ls-files·baseline rev-parse 등 → 빈(비-출하·baseline 미해소).

    box = [None]
    pytest_fn, _cwds = _recording_pytest(box)
    pool = _TaskSetPool(["work/a_1", "work/b_1"],
                        states={"work/a_1": {"dirty": True}, "work/b_1": {"dirty": True}})
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    handoff = hf.PmHandoff(
        run_pytest_fn=pytest_fn,
        run_git_fn=_git,
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
    )
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0
    out = capsys.readouterr().out
    # [1b] 두 슬롯 각각 surface 헤더.
    assert "▷ work/a_1 출하 변경:" in out
    assert "▷ work/b_1 출하 변경:" in out
    # b_1 만 미검증 출하 변경(엔진 파일) 발동·a_1 은 비-출하.
    b_idx = out.index("▷ work/b_1 출하 변경:")
    a_idx = out.index("▷ work/a_1 출하 변경:")
    assert "미검증 출하 변경 1파일" in out[b_idx:]           # b_1 발동.
    assert "출하 변경 없음" in out[a_idx:b_idx]              # a_1 은 비-출하.


def test_task_shipping_surface_per_changed_slot_under_no_pytest(hf, tmp_path, capsys, monkeypatch):
    """`--task --no-pytest` — 회귀 skip 이어도 변경-슬롯을 열거해 [1b] 가 슬롯별 surface 를 돈다
    (codex R2 must-fix — skip_pytest 여도 REPO 폴백 단일 검사로 후퇴하지 않음·T-0393).

    2슬롯 다 변경(dirty)·b_1 worktree 만 SHIPPING_GLOBS 변경 stub → b_1 만 발동·a_1 비-출하."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    (tmp_path / "work" / "a_1").mkdir(parents=True)
    (tmp_path / "work" / "b_1").mkdir(parents=True)

    def _git(args):
        joined = " ".join(args)
        if "diff" in args and "--name-only" in args and "HEAD" in args and "..HEAD" not in joined:
            if str(tmp_path / "work" / "b_1") in args:
                return 0, ".project_manager/tools/board.py\n"
            return 0, ""
        return 0, ""

    box = [None]
    pytest_fn, cwds = _recording_pytest(box)   # skip_pytest → 회귀 미실행 확인용.
    pool = _TaskSetPool(["work/a_1", "work/b_1"],
                        states={"work/a_1": {"dirty": True}, "work/b_1": {"dirty": True}})
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    handoff = hf.PmHandoff(
        run_pytest_fn=pytest_fn,
        run_git_fn=_git,
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
    )
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert cwds == []   # --no-pytest — 회귀는 실행되지 않는다(열거만 수행).
    out = capsys.readouterr().out
    assert "[--no-pytest] 회귀 측정 skip." in out
    # 회귀 skip 이어도 [1b] 가 변경-슬롯 각각 surface(REPO 폴백 단일 검사 아님).
    assert "▷ work/a_1 출하 변경:" in out
    assert "▷ work/b_1 출하 변경:" in out
    b_idx = out.index("▷ work/b_1 출하 변경:")
    a_idx = out.index("▷ work/a_1 출하 변경:")
    assert "미검증 출하 변경 1파일" in out[b_idx:]
    assert "출하 변경 없음" in out[a_idx:b_idx]


def test_task_resnap_records_all_held_slots(hf, tmp_path, capsys):
    """재스냅 = 보유 전 슬롯 루프(현행 1슬롯 한정 폐지·T-0393·ADR-0068 퇴장)."""
    pool = _TaskSetPool(["work/a_1", "work/b_1"])
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, pytest_summary()))
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    # 보유 전 슬롯 각각 record_git_snapshot(base 미전달·T-0388 프리미티브 재사용).
    assert pool.snap_calls == [("work/a_1", {}), ("work/b_1", {})]
    assert "[재스냅] task 보유 슬롯" in capsys.readouterr().out


def test_task_resnap_rechecks_owner_before_snapshot(hf, tmp_path, capsys):
    """초기 held-slot 스냅 뒤 realloc되면 새 소유자 lease.git을 덮지 않고 loud rc1이다."""
    class ReallocatedPool(_TaskSetPool):
        def lease_owned_by_task_strict(self, slot, task):
            return False

    pool = ReallocatedPool(["work/a_1"])
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, pytest_summary()))
    assert handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    ) == 1
    assert pool.snap_calls == []
    err = capsys.readouterr().err
    assert "소유권 재검증" in err
    assert "새 소유자의 lease.git" in err


def test_task_resnap_zero_held_slots_skips(hf, tmp_path, capsys):
    """task 보유 0개 — 재스냅 대상 없음 명시 skip(무해·T-0393)."""
    pool = _TaskSetPool([])
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, pytest_summary()))
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.snap_calls == []
    assert "재스냅 대상 없음" in capsys.readouterr().out


def test_task_resnap_dry_run_previews_all_slots(hf, tmp_path, capsys):
    """dry_run task 재스냅 — 보유 슬롯별 예고만·write 프리미티브 미호출(미리보기·T-0393)."""
    pool = _TaskSetPool(["work/a_1", "work/b_1"])
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, pytest_summary()))
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=True, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.snap_calls == []
    out = capsys.readouterr().out
    assert "[dry-run] git 재스냅 예고: work/a_1" in out
    assert "[dry-run] git 재스냅 예고: work/b_1" in out


def test_slot_mode_resnap_single_unchanged_by_task_axis(hf, tmp_path):
    """slot 모드(task None) 재스냅은 단일 bound 슬롯 그대로 — task 축이 slot/솔로를 안 건드린다(T-0393 불변)."""
    pool = _TaskSetPool(["work/a_1", "work/b_1"])   # slots_for_task 는 task 모드에서만 소비.
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, pytest_summary()))
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True,
        worktree_slot="work/project_manager_1",   # task 없음 → slot 모드.
    )
    assert rc == 0
    # slot 모드 = 명시 bound 슬롯 단일 재스냅(보유 집합 열거 미진입).
    assert pool.snap_calls == [("work/project_manager_1", {})]


def test_task_handoff_real_git_changed_slot_regressed_all_resnapped(hf, tmp_path, monkeypatch):
    """통합(real git worktree 2개 + real worktree_pool) — task-only 핸드오프가 **변경 슬롯만 회귀**하고
    **보유 전 슬롯을 실 장부에 재스냅**한다 (T-0393·reviewer should-fix·shape drift false-green 방지).

    mock 만으로는 slots_for_task/compare_slot_git/slot_git_status 의 실 반환 shape drift 시 false-green
    여지가 있다 — T-0388 `test_snapshot_updates_lease_and_next_phase0_passes` 패턴을 확장해 2슬롯 중
    b_1 만 커밋 전진(head descendant=변경)시키고 a_1 은 arrival 그대로(무변경) 둔 뒤, 실 엔진으로:
      ① 회귀는 b_1(변경 슬롯)에서만 돈다(a_1 은 skip·직전 green 불변).
      ② 재스냅은 a_1·b_1 **둘 다** 실 장부 lease.git 을 live 로 갱신한다(보유 전 슬롯 루프)."""
    import importlib.util as _ilu
    import subprocess as _sp

    proj = tmp_path / "proj"
    local = proj / ".project_manager" / ".local"
    local.mkdir(parents=True, exist_ok=True)
    (proj / "work").mkdir(parents=True, exist_ok=True)
    spec = _ilu.spec_from_file_location("wp_h393", TOOLS / "worktree_pool.py")
    wp = _ilu.module_from_spec(spec)
    spec.loader.exec_module(wp)
    for _name, _val in {
        "REPO": proj, "LOCAL_DIR": local,
        "LEASES_FILE": local / "worktree-leases.json",
        "LEASES_LOCK": local / "worktree-leases.lock",
        "WORK_DIR": proj / "work",
    }.items():
        setattr(wp, _name, _val)
    # _regression_cwd(slot)=REPO/slot 해소가 실 worktree 를 보게 pm_handoff.REPO 도 proj 로.
    monkeypatch.setattr(hf, "REPO", proj)

    def _mkgit(slot_dir):
        def g(*argv):
            env = dict(os.environ)
            env.update({
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            })
            return _sp.run(["git", "-C", str(slot_dir), *argv], check=True,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env)
        return g

    # 두 슬롯 worktree 실 git init + arrival 커밋.
    a_dir = proj / "work" / "a_1"
    b_dir = proj / "work" / "b_1"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    ga, gb = _mkgit(a_dir), _mkgit(b_dir)
    for g, slot_dir, tag in ((ga, a_dir, "a"), (gb, b_dir, "b")):
        g("init", "-q", "-b", "main")
        (slot_dir / "f.txt").write_text(f"{tag}1\n", encoding="utf-8")
        g("add", "f.txt")
        g("commit", "-q", "-m", f"{tag}1")

    # 리스 seed(둘 다 session="mytask"·slots_for_task 대상) + arrival 스냅(도착 시점 모사).
    leases = [
        wp.Lease(slot="work/a_1", repo="a", session="mytask",
                 pid=os.getpid(), started="t", state="leased"),
        wp.Lease(slot="work/b_1", repo="b", session="mytask",
                 pid=os.getpid(), started="t", state="leased"),
    ]
    with wp._lease_lock():
        wp._write_ledger(leases)
        wp._write_tasks([wp.Task("mytask", pid=os.getpid(), started="t")])
    wp.record_git_snapshot("work/a_1", base_branch="main")
    wp.record_git_snapshot("work/b_1", base_branch="main")
    a_head = ga("rev-parse", "HEAD").stdout.strip()   # a_1 은 이후 불변.

    # b_1 만 세션 중 커밋 전진(head descendant = 변경 흔적)·a_1 은 arrival 그대로(무변경).
    (b_dir / "f.txt").write_text("b2\n", encoding="utf-8")
    gb("add", "f.txt")
    gb("commit", "-q", "-m", "b2")
    b_head2 = gb("rev-parse", "HEAD").stdout.strip()

    # 변경 판정 사전 확인(실 엔진) — a_1=match(무변경)·b_1=descendant(변경).
    assert wp.compare_slot_git("work/a_1").head_relation == wp.HEAD_MATCH
    assert wp.compare_slot_git("work/b_1").head_relation == wp.HEAD_DESCENDANT

    # 회귀가 돈 슬롯 cwd 관찰(주입 pytest_fn — 실 pytest 대신 슬롯만 기록·green).
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    handoff = hf.PmHandoff(
        run_pytest_fn=pytest_fn,
        run_git_fn=lambda args: (0, ""),   # 출하 surface·[6/7] git 은 비관찰(비차단).
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=wp,
    )
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False, task="mytask"
    )
    assert rc == 0

    # ① 회귀는 변경 슬롯 b_1 에서만(a_1 은 무변경 skip) — 실 compare_slot_git/slot_git_status 판정.
    assert cwds == ["work/b_1"]

    # ② 재스냅 = 보유 전 슬롯 — a_1·b_1 둘 다 lease.git 이 live head 로(실 장부 재열람).
    updated = {l.slot: l for l in wp.list_leases()}
    assert updated["work/a_1"].git["head"] == a_head       # 무변경이라도 재스냅 대상(live=arrival).
    assert updated["work/b_1"].git["head"] == b_head2      # 변경분 live(c2)로 갱신.
    # base(arrival main)는 재스냅에서 보존(base 미전달·arrival 동형).
    assert updated["work/b_1"].git["base"]["branch"] == "main"


# ── [6/7] 미push ahead 경고 · [6b/7] 미마감 raw sweep · [7/7] push 단계 (T-0596) ──
#
# 실사고 근거: 세션이 커밋만 하고 push 를 안 한 채 넘어가 다음 세션이 원격에 없는 부기를 기준으로
# 판단했고(PM 홈 2커밋 미push), 비정상 종료한 위임/리뷰의 미마감 raw 가 아무도 안 보는 채로
# 누적했다(실측 17건). 둘 다 **비차단 표면**이다 — 핸드오프 rc 를 바꾸지 않는다.


class _GitStub:
    """git seam stub — `rev-list --count` 만 케이스별로 응답하고 나머지는 (0, "")."""

    def __init__(self, *, ahead: str | None = None, rc_when_absent: int = 128):
        self.ahead = ahead                    # None = baseline 해소 실패(미push 수 미해소)
        self.rc_when_absent = rc_when_absent
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> tuple[int, str]:
        self.calls.append(args)
        if args[:2] == ["rev-list", "--count"]:
            if self.ahead is None:
                return self.rc_when_absent, "fatal: no upstream configured"
            return 0, f"{self.ahead}\n"
        return 0, ""


def _raw_record(index: int, *, finished: bool) -> dict:
    """raw 장부 레코드 1행 — 미마감은 finished_at/rc 가 없다(실행 전 예약 상태 그대로)."""
    row = {
        "id": f"rec{index:02d}",
        "surface": "delegate",
        "harness": "codex",
        "model": "gpt-x",
        "role": "developer",
        "attempt": "primary",
        "pid": 4242,
        "started_at": f"2026-08-0{index % 8 + 1}T01:02:03.000000+00:00",
        "raw_path": f"/tmp/raw/pm_delegate_codex_{index}.txt",
    }
    if finished:
        row.update({"finished_at": "2026-08-08T01:03:03.000000+00:00", "rc": 0,
                    "elapsed_sec": 60.0, "silence_sec": None})
    return row


def _write_ledger(tmp_path, *, unfinished: int = 0, finished: int = 0) -> Path:
    ledger = tmp_path / "raw_outputs.json"
    records = ([_raw_record(i, finished=False) for i in range(unfinished)]
               + [_raw_record(100 + i, finished=True) for i in range(finished)])
    ledger.write_text(
        json.dumps({"version": 1, "records": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return ledger


def _run_handoff_out(hf, tmp_path, capsys, **kwargs) -> str:
    """hermetic 핸드오프 1회 실행 후 stdout 반환 (rc=0 단언 포함 — 표면은 비차단)."""
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool(), **kwargs)
    assert _run_slot_handoff(handoff, 7) == 0, "비차단 표면이 핸드오프를 막았다"
    return capsys.readouterr().out


def test_handoff_warns_on_unpushed_ahead_commits(hf, tmp_path, capsys):
    """[6/7]: ahead>0 이면 미push commit 수 경고 1줄 + push 단계 지시(비차단)."""
    out = _run_handoff_out(hf, tmp_path, capsys, run_git_fn=_GitStub(ahead="2"))

    line = next(l for l in out.splitlines() if "미push commit" in l)
    assert "⚠" in line and "2개" in line, f"ahead 경고가 건수를 안 싣는다: {line!r}"
    assert "@{upstream}" in line, f"판정 기준 ref 가 안 보인다: {line!r}"
    assert "[7/7]" in line, f"경고가 push 단계로 안내하지 않는다: {line!r}"


def test_handoff_reports_clean_when_nothing_to_push(hf, tmp_path, capsys):
    """[6/7]: ahead=0 이면 경고 대신 '미push commit 없음'(무경보를 정확히 말한다)."""
    out = _run_handoff_out(hf, tmp_path, capsys, run_git_fn=_GitStub(ahead="0"))

    assert "미push commit 없음" in out
    assert "⚠ 미push commit" not in out


def test_ahead_count_falls_through_to_second_baseline_on_exception(hf, tmp_path):
    """[6/7]: 첫 baseline 후보에서 git 예외가 나도 두 번째 후보를 시도한다(rc 실패와 동일 취급).

    첫 후보 예외로 곧장 미해소로 떨어지면 upstream 미설정 형상(= 첫 후보가 늘 실패)이 곧
    "미push 수 미해소"가 돼 origin/main 폴백이 죽는다.
    """
    tried: list[str] = []

    def runner(args: list[str]) -> tuple[int, str]:
        tried.append(args[-1])
        if args[-1].startswith("@{upstream}"):
            raise OSError("git: no upstream")
        return 0, "3\n"

    assert hf._ahead_commit_count(runner) == (3, "origin/main")
    assert tried == ["@{upstream}..HEAD", "origin/main..HEAD"]


def test_handoff_ahead_count_unresolved_is_failsoft(hf, tmp_path, capsys):
    """[6/7]: baseline 해소 실패(upstream 미설정·detached)는 미해소 1줄 — 0 으로 위장하지 않는다."""
    git = _GitStub(ahead=None)

    out = _run_handoff_out(hf, tmp_path, capsys, run_git_fn=git)

    assert "미push commit 수 미해소" in out
    assert "미push commit 없음" not in out, "미해소를 '없음'(안전)으로 오표기함"
    tried = [args[-1] for args in git.calls if args[:2] == ["rev-list", "--count"]]
    assert tried == ["@{upstream}..HEAD", "origin/main..HEAD"], (
        f"baseline 후보를 순서대로 다 시도하지 않음: {tried}")


def test_handoff_surfaces_unfinished_raw_records(hf, tmp_path, capsys):
    """[6b/7]: 미마감 raw 건수 + 최근 레코드 식별자를 표면화하고, 마감분은 안 센다(비차단)."""
    ledger = _write_ledger(tmp_path, unfinished=2, finished=3)

    out = _run_handoff_out(hf, tmp_path, capsys, raw_ledger_file=ledger)

    assert f"이 장부 기준: {ledger}" in out, (
        f"판정 범위(어느 장부를 봤나) 표기 누락 — '0건'이 전 장부 0건으로 읽힌다:\n{out}")
    assert "⚠ 미마감 raw 2건" in out, f"미마감 건수 미표면화:\n{out}"
    assert "pm_delegate.py raw --unfinished" in out, "전체 조회 경로 안내 누락"
    assert "id=rec00" in out and "id=rec01" in out, "레코드 식별자가 안 실림"
    assert "id=rec100" not in out, "마감된 레코드가 미마감으로 표시됨"


def test_handoff_raw_sweep_caps_listing_but_keeps_count(hf, tmp_path, capsys):
    """[6b/7]: 나열은 상한(N건)까지만·나머지는 잔여 건수로 접는다(터미널 폭주 방지·건수는 보존)."""
    limit = hf.UNFINISHED_RAW_DISPLAY_LIMIT
    ledger = _write_ledger(tmp_path, unfinished=limit + 2)

    out = _run_handoff_out(hf, tmp_path, capsys, raw_ledger_file=ledger)

    assert f"⚠ 미마감 raw {limit + 2}건" in out
    listed = [l for l in out.splitlines() if l.strip().startswith("- 2026-")]
    assert len(listed) == limit, f"나열 상한 {limit} 위반: {len(listed)}줄"
    assert "… 외 2건" in out


def test_handoff_reports_clean_raw_ledger(hf, tmp_path, capsys):
    """[6b/7]: 미마감 0건이면 '없음'을 명시한다(무경보도 말한다)."""
    ledger = _write_ledger(tmp_path, finished=2)

    out = _run_handoff_out(hf, tmp_path, capsys, raw_ledger_file=ledger)

    assert "미마감 raw 없음" in out
    assert f"이 장부 기준: {ledger}" in out, "0건 보고에도 판정 범위 표기가 있어야 한다"
    assert "⚠ 미마감 raw" not in out


def test_handoff_raw_sweep_is_failsoft_on_corrupt_ledger(hf, tmp_path, capsys):
    """[6b/7]: 손상 장부는 사유 1줄 후 skip — 핸드오프를 막지 않는다(비차단 계약)."""
    ledger = tmp_path / "raw_outputs.json"
    ledger.write_text("{not json", encoding="utf-8")

    out = _run_handoff_out(hf, tmp_path, capsys, raw_ledger_file=ledger)

    assert "⚠ 미마감 raw 조회 실패" in out


def test_handoff_raw_sweep_reports_missing_ledger(hf, tmp_path, capsys):
    """[6b/7]: 장부 파일 부재(위임 이력 0)는 사유를 밝히고 0건으로 넘어간다."""
    out = _run_handoff_out(hf, tmp_path, capsys,
                           raw_ledger_file=tmp_path / "없는장부.json")

    assert "raw 장부 없음" in out


def _write_named_ledger(directory: Path, *, unfinished: int) -> Path:
    """사본 장부 픽스처 — 다른 디렉터리에 같은 이름의 장부를 하나 더 만든다."""
    directory.mkdir(parents=True, exist_ok=True)
    return _write_ledger(directory, unfinished=unfinished)


def test_handoff_names_peer_ledgers_with_their_unfinished_count(hf, tmp_path, capsys):
    """[6b/7]: worktree 사본 장부가 있으면 **경로와 건수**를 1줄 병기한다 (T-0600·가시성만).

    결정 공급 장부 하나만 보고 "0건"이라고 말하면 다른 사본에 쌓인 미마감이 통째로 안 보인다.
    통합 조회는 범위 밖이라 이 표면은 존재와 건수만 알린다.
    """
    primary = _write_ledger(tmp_path, unfinished=1)
    peer = _write_named_ledger(tmp_path / "worktree-copy", unfinished=2)

    out = _run_handoff_out(hf, tmp_path, capsys, raw_ledger_file=primary,
                           peer_raw_ledger_files=(peer,))

    line = next(l for l in out.splitlines() if "사본 장부" in l)
    assert str(peer) in line, f"사본 장부 경로가 안 실림: {line!r}"
    assert "미마감 2건" in line, f"사본 장부 건수가 안 실림: {line!r}"
    assert "범위 밖" in line, f"통합 조회가 아님을 밝히지 않음: {line!r}"
    assert f"이 장부 기준: {primary}" in out          # 판정 장부 표기는 그대로


def test_handoff_stays_silent_about_peers_when_there_are_none(hf, tmp_path, capsys):
    """[6b/7]: 사본이 없으면(솔로 형상) 병기 줄 자체가 없다 — 새 상시 소음 금지."""
    out = _run_handoff_out(hf, tmp_path, capsys,
                           raw_ledger_file=_write_ledger(tmp_path, unfinished=1))

    assert "사본 장부" not in out


def test_peer_ledger_line_is_failsoft_on_a_corrupt_copy(hf, tmp_path, capsys):
    """[6b/7]: 손상된 사본 장부는 사유 1줄 후 건너뛴다 — 핸드오프를 막지 않는다."""
    broken = tmp_path / "broken" / "raw_outputs.json"
    broken.parent.mkdir()
    broken.write_text("{not json", encoding="utf-8")

    out = _run_handoff_out(hf, tmp_path, capsys,
                           raw_ledger_file=_write_ledger(tmp_path, unfinished=1),
                           peer_raw_ledger_files=(broken,))

    assert "⚠ 사본 장부 조회 실패" in out


def test_peer_ledger_discovery_reuses_the_delegate_query_rule(hf, tmp_path, monkeypatch):
    """프로덕션(주입 없음) 사본 판정은 위임 조회면 규칙을 그대로 쓰고, 자기 장부는 뺀다.

    같은 판정을 여기서 다시 구현하면 두 표면이 서로 다른 사본 집합을 말한다.
    """
    primary = tmp_path / "primary" / "raw_outputs.json"
    primary.parent.mkdir()
    primary.write_text("{}", encoding="utf-8")
    peer = tmp_path / "peer" / "raw_outputs.json"
    stub = types.SimpleNamespace(
        _peer_engine_ledgers=lambda: (primary.resolve(), peer))
    monkeypatch.setattr(hf, "_load_pm_delegate", lambda: stub)
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool(), peer_raw_ledger_files=None)

    assert handoff._peer_raw_ledgers(primary) == (peer,)


def test_peer_ledger_discovery_is_failsoft_without_the_delegate_engine(
        hf, tmp_path, monkeypatch):
    """pm_delegate 부재/로드 실패면 병기를 조용히 건너뛴다(핸드오프 불변)."""
    monkeypatch.setattr(hf, "_load_pm_delegate", lambda: None)
    handoff = _hermetic_handoff(hf, tmp_path, _SnapPool(), peer_raw_ledger_files=None)

    assert handoff._peer_raw_ledgers(tmp_path / "raw_outputs.json") == ()


def test_handoff_checklist_includes_push_step_after_commit(hf, tmp_path, capsys):
    """[7/7]: 커밋 다음에 **push 단계**가 체크리스트로 실제 출력된다(런타임 출력으로 확인).

    소스 문자열 assert 는 `print` 를 dead-code 로 만들어도 통과해 가드가 아니다
    (`test_run_prints_pathspec_commit_guidance` 와 같은 규율).
    """
    out = _run_handoff_out(hf, tmp_path, capsys, run_git_fn=_GitStub(ahead="2"))

    push_lines = [l for l in out.splitlines() if "[ ]" in l and "push" in l]
    assert push_lines, f"push 단계가 잔여 작업 체크리스트에 없다:\n{out}"
    assert any("PM 홈 push" in l for l in push_lines), f"push 대상이 모호하다: {push_lines}"
    commit_line = next(l for l in out.splitlines() if "[ ]" in l and "git commit" in l)
    assert out.index(push_lines[0]) > out.index(commit_line), \
        "push 단계가 commit 안내보다 앞에 있다(순서 역전)"


def test_raw_ledger_default_resolves_to_pm_home_local_ledger(hf):
    """주입이 없으면 sweep 은 **실 PM 홈 장부**(`.project_manager/.local/raw_outputs.json`)를 본다.

    이 배선이 없으면 hermetic 테스트는 다 green 인데 프로덕션 sweep 만 조용히 죽는다
    (경로 해소만 확인 — 파일을 읽지 않는다).
    """
    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
    )

    resolved = handoff._resolve_raw_ledger()

    assert resolved == hf.REPO / ".project_manager" / ".local" / "raw_outputs.json", (
        f"기본 장부 경로가 PM 홈 규약과 다르다: {resolved}")
