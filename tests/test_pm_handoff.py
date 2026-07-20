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
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"
PM_HANDOFF_PY = TOOLS / "pm_handoff.py"


def _load_module(name: str = "pm_handoff"):
    spec = importlib.util.spec_from_file_location(name, PM_HANDOFF_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hf():
    return _load_module()


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
    """앵커는 있으나 기존 pm 세션 entry 가 0개면 ValueError."""
    doc = _state(pointer=_POINTER_1_3)  # entry 없음, 포인터만.
    with pytest.raises(ValueError):
        hf.update_session_window(
            doc, session_num=7, date_str="2026-06-15", wave_summary="x"
        )


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


def test_run_task_mode_emits_task_trigger(hf, tmp_path, capsys):
    """run(task=...) 이 [5/7] 에 task-only 트리거를 emit 한다 (T-0394·호출부 배선).

    명시 pm_state 주입(hermetic) 없이 --task 를 run 에 주면 build_handoff_prompt_output 에
    task 가 전달돼 `/pm-bootstrap --task <이름>` 이 나온다 — task_mode 배선을 가드.
    """
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")
    log_file = tmp_path / "current.md"

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
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
    # bare(T-0193): /pm-bootstrap 커맨드만. 역할문구(2인칭 "당신은")·위임 framing 은 폐기 —
    # pm_role.md(bootstrap 필독)·CLAUDE.md 가 auto-load 로 담당. who-pastes 혼동의 근원 제거.
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


# --- `_regression_cwd` L1(라이더) — 명시 슬롯 stale → REPO 폴백 경계 ---

def test_regression_cwd_explicit_slot_stale_falls_back_to_repo(hf, tmp_path, capsys):
    # L1 — 명시 worktree_slot 이 리스 조인(M3)은 통과했더라도 디스크에 실제 디렉토리가 없으면
    # (장부-파일시스템 out-of-sync) FileNotFoundError 로 죽는 대신 REPO 로 soft 폴백·경고 1줄.
    result = hf._regression_cwd("work/foo_2", repo_root=tmp_path)
    assert result == str(tmp_path)
    err = capsys.readouterr().err
    assert "work/foo_2" in err and "L1" in err


def test_regression_cwd_explicit_slot_existing_dir_not_stale(hf, tmp_path):
    # L1 경계 반대편 — 디렉토리가 실제 존재하면 그대로(stale 아님·폴백 미발동).
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
# 세션 종료(핸드오프)의 연속성 앵커를 slot→task 로 이동한다. `--task <name>` 이 pm_state 를
# `.local/tasks/<name>/` 에 기록(첫 핸드오프가 template seed)·dashboard 자기 섹션 `## <name>`·
# log 헤더 태그 `(task:<name>)`. lease 는 유지(세션 종료 ≠ task 종료·F4). log 태그는 서술형 괄호·
# 슬롯 태그와 구분되게 sentinel `task:` 로 박는다.


def test_main_task_forwarded_to_run(hf, captured_run):
    """main() 이 `--task <name>` 을 run(task=...) 로 forward 한다 (ingress 배선·T-0356)."""
    assert hf.main(
        ["--session-seq", "7", "--wave-summary", "x", "--no-pytest", "--task", "mytask"]
    ) == 0
    assert captured_run["task"] == "mytask"


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
            ["--session-seq", "7", "--wave-summary", "x", "--no-pytest", "--task", bad]
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
            ["--session-seq", "7", "--wave-summary", "x", "--no-pytest",
             "--task", "project_manager_1"]
        )
    assert captured_run == {}
    # 미등록 repo 형태(자유 포맷)는 통과 — run 도달(예약 판별이 실 슬롯과만 충돌 방지·오탐 0).
    assert hf.main(
        ["--session-seq", "7", "--wave-summary", "x", "--no-pytest", "--task", "sikdan_2"]
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
    )
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=True, skip_pytest=False, task="mytask"
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


def test_run_task_seeds_and_records_pm_state(hf, tmp_path, capsys, monkeypatch):
    """첫 task 핸드오프 — 부재 pm_state 를 template 에서 seed 후 `.local/tasks/<name>/` 에 기록.

    실 경로 해소(명시 주입 없음·REPO monkeypatch)로 F7 앵커 이동을 못박는다: task pm_state 가
    task 서술 공간에 생성되고(첫 핸드오프·T-0353 surface 약속) 세션 window 가 이 차수를 담는다.
    dashboard 자기 섹션 `## <name>`·log 태그 `(task:<name>)`. lease 는 건드리지 않는다(done=False —
    worktree_pool release 미호출·세션 종료 ≠ task 종료·F4)."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    # tracked pm_state template(seed 원천) — board.py init 과 동일 skeleton(세션 식별 앵커 보유).
    template = tmp_path / ".project_manager" / "wiki" / "pm_state.template.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    log_file = tmp_path / "current.md"
    dashboard_file = tmp_path / "dashboard.md"

    # lease 유지 증거 — release 가 불리면 fail 하는 sentinel worktree_pool(done=False 라 미호출 기대).
    class _NoReleasePool:
        def release(self, *a, **k):  # noqa: ANN002 ANN003
            raise AssertionError("task 세션 종료가 lease 를 release 하면 안 된다(F7·세션종료≠task종료)")

    task_pm_state = (
        tmp_path / ".project_manager" / ".local" / "tasks" / "mytask" / "pm_state.md"
    )
    assert not task_pm_state.exists()  # 첫 핸드오프 전엔 부재.

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file,
        pm_playbook_file=playbook,
        dashboard_file=dashboard_file,
        worktree_pool=_NoReleasePool(),
    )
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    # task pm_state 가 task 서술 공간에 생성·기록(sliding window 가 7차 반영).
    assert task_pm_state.exists()
    state_text = task_pm_state.read_text(encoding="utf-8")
    assert "7차" in state_text
    # dashboard 자기 섹션 `## mytask`.
    assert "## mytask" in dashboard_file.read_text(encoding="utf-8")
    # log 헤더 태그 = sentinel `(task:mytask)`.
    assert "PM 7차 (task:mytask)" in log_file.read_text(encoding="utf-8")


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


def _hermetic_handoff(hf, tmp_path, pool):
    """명시 pm_state/log/dashboard 주입 + mock worktree_pool 로 hermetic PmHandoff 구성."""
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text(
        _state(_entry(4), _entry(5), _entry(6), pointer=_POINTER_1_3), encoding="utf-8"
    )
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_playbook_with_prompt(), encoding="utf-8")
    return hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed in 0.01s\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=pm_state,
        dashboard_file=tmp_path / "dashboard.md",
        worktree_pool=pool,
    )


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


# ── 핸드오프 완료 task 정상-종료 pid=0 기록 ("여기 두고 간다"의 task 판·T-0392) ─────────
#
# task 장부 pid = dump 후 즉사하는 bootstrap subprocess pid(㉑·T-0353)라, pm_handoff 가 종료를 안
# 기록하면 정상 인계 후 재개도 dead-pid → bind_task `reclaimed`("재개(회수·이전 세션 crash)" +
# "⚠️ 회수 진입") 로 상시 오탐한다(PM 78 실측). task 모드 완료 단계에서 pid=0(미점유)으로 비워
# 차기 부트스트랩이 clean resumed 로 재개하게 한다 — 슬롯 재스냅(T-0388)과 동형 배치·fail-soft·
# dry_run 예고·slot/솔로 모드(--task 없음)는 무영향.


class _TaskPidPool(_SnapPool):
    """_SnapPool + release_task_pid 호출 기록 (T-0392 task pid 배선 검증).

    `omit_task_pid` 면 release_task_pid 속성 미노출(구버전 풀·getattr 가드 fail-soft 모델).
    `task_pid_none` 면 release_task_pid 가 None(task 장부 부재) 반환(fail-soft loud 모델)."""

    def __init__(self, *, omit_task_pid=False, task_pid_none=False, **kwargs):
        super().__init__(**kwargs)
        self.task_pid_calls: list[str] = []
        self._task_pid_none = task_pid_none
        if not omit_task_pid:
            self.release_task_pid = self._do_release_task_pid

    def _do_release_task_pid(self, name):
        self.task_pid_calls.append(name)
        if self._task_pid_none:
            return None
        return type("_T", (), {"name": name, "pid": 0})()


def _task_mode_handoff(hf, tmp_path, monkeypatch, pool):
    """task_mode(=명시 pm_state 미주입·--task) 진입 hermetic PmHandoff (T-0392).

    task_mode = task not None AND pm_state 미명시 → REPO monkeypatch + template seed 로 실 앵커
    해소를 태우되 mock worktree_pool 로 release_task_pid 배선만 격리 검증한다(task-only=슬롯 미해소)."""
    monkeypatch.setattr(hf, "REPO", tmp_path)
    template = tmp_path / ".project_manager" / "wiki" / "pm_state.template.md"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.write_text(
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


def test_run_task_mode_releases_task_pid_after_bookkeeping(hf, tmp_path, monkeypatch, capsys):
    """task 모드 핸드오프가 부기 완료 후 release_task_pid 를 task 명으로 1회 호출 — 정상-종료 pid=0 기록(T-0392)."""
    pool = _TaskPidPool()
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.task_pid_calls == ["mytask"]
    out = capsys.readouterr().out
    # 완료 단계(부기 후) 배치 — [3/7] pm_state 부기 뒤.
    assert out.index("[task] 정상-종료 task pid 기록") > out.index("[3/7]")
    assert "task 정상-종료 기록: mytask → pid=0" in out


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


def test_run_task_mode_failsoft_when_task_absent(hf, tmp_path, monkeypatch, capsys):
    """release_task_pid 가 None(task 장부 부재)이면 무해 skip·핸드오프 완주 (fail-soft loud)."""
    pool = _TaskPidPool(task_pid_none=True)
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0                               # fail-soft — 기록 실패가 핸드오프를 막지 않는다.
    assert pool.task_pid_calls == ["mytask"]
    assert "장부에 없음" in capsys.readouterr().err


def test_run_task_mode_failsoft_when_pool_lacks_primitive(hf, tmp_path, monkeypatch, capsys):
    """구버전 풀(release_task_pid 부재)이면 무해 skip·핸드오프 완주 (getattr 가드·fail-soft)."""
    pool = _TaskPidPool(omit_task_pid=True)
    handoff = _task_mode_handoff(hf, tmp_path, monkeypatch, pool)
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.task_pid_calls == []
    assert "구버전" in capsys.readouterr().err


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
    assert "보유 슬롯 0개" in capsys.readouterr().out


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


def test_task_regression_explicit_slot_priority_unchanged(hf, tmp_path, capsys):
    """명시 --repo/--slot 동반 시 우선순위 불변 — task 다중슬롯 경로 안 타고 그 슬롯 단일 회귀(T-0393)."""
    box = [None]
    pytest_fn, cwds = _recording_pytest(box)
    # 명시 슬롯이 있으면 task 회귀 모드 진입 안 함 → slots_for_task 를 회귀 판정에 안 씀(2슬롯 무시).
    pool = _TaskSetPool(["work/a_1", "work/b_1"],
                        states={"work/a_1": {"dirty": True}, "work/b_1": {"dirty": True}})
    handoff = _task_reg_handoff(hf, tmp_path, pool, pytest_fn)
    box[0] = handoff
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=False,
        task="mytask", worktree_slot="work/project_manager_1",
    )
    assert rc == 0
    # 명시 슬롯 단일 cwd 로 1회 — task 다중슬롯 루프(▷) 미진입.
    assert cwds == ["work/project_manager_1"]
    out = capsys.readouterr().out
    assert "▷" not in out
    assert "✓ green: 1 passed" in out


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
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, "1 passed\n"))
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    # 보유 전 슬롯 각각 record_git_snapshot(base 미전달·T-0388 프리미티브 재사용).
    assert pool.snap_calls == [("work/a_1", {}), ("work/b_1", {})]
    assert "[재스냅] task 보유 슬롯" in capsys.readouterr().out


def test_task_resnap_zero_held_slots_skips(hf, tmp_path, capsys):
    """task 보유 0개 — 재스냅 대상 없음 명시 skip(무해·T-0393)."""
    pool = _TaskSetPool([])
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, "1 passed\n"))
    rc = handoff.run(
        session_num=7, wave_summary="요약", dry_run=False, skip_pytest=True, task="mytask"
    )
    assert rc == 0
    assert pool.snap_calls == []
    assert "재스냅 대상 없음" in capsys.readouterr().out


def test_task_resnap_dry_run_previews_all_slots(hf, tmp_path, capsys):
    """dry_run task 재스냅 — 보유 슬롯별 예고만·write 프리미티브 미호출(미리보기·T-0393)."""
    pool = _TaskSetPool(["work/a_1", "work/b_1"])
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, "1 passed\n"))
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
    handoff = _task_reg_handoff(hf, tmp_path, pool, lambda: (0, "1 passed\n"))
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
