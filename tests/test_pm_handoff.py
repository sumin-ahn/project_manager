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
    """핸드오프 절차 문서(pm_role·claude SKILL·opencode command)가 폐기된 `<핵심 인계 사항>`
    손-채움을 *살아있는 단계*로 지시하지 않는다.

    프롬프트 emit(`build_handoff_prompt_output`)은 트리거화됐는데(T-0180) 절차 미러 문서가
    "그 절을 채우라"고 stale 로 남으면 다음 PM 이 *없는 슬롯*을 찾는다 — code-mirror 갱신 ↔
    doc-mirror stale 비대칭은 반복 클래스라([[feature-ship-needs-fresh-adopter-gate]]) 기계로 박는다.
    출하 파일 자체를 가드(canonical = 사본 byte-identical 은 parity 가드가 별도 강제).
    """
    procedure_docs = [
        REPO / ".project_manager" / "wiki" / "pm_role.md",
        REPO / ".claude" / "skills" / "pm-handoff" / "SKILL.md",
        REPO / "templates" / "opencode" / ".opencode" / "command" / "pm-handoff.md",
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
    # 명시 worktree_slot 우선 — auto 판정을 무시하고 그 슬롯 경로 반환.
    areas = tmp_path / "areas.md"
    leases = tmp_path / "worktree-leases.json"
    _write_areas(areas, ["project_manager"])
    _write_leases(leases, [
        {"slot": "work/project_manager_1", "repo": "project_manager",
         "session": "project_manager_1", "state": "leased"},
    ])
    result = hf._regression_cwd("work/foo_2", areas_file=areas, leases_file=leases)
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


# ── ADR-0043 세션 정체성 canonical: --session/--session-seq + 구형 alias 무기한 ──────
#
# canonical(--session `<repo>_<N>` · --session-seq N)을 신설하고 구형(--worktree-slot·
# --session-num)을 deprecated alias 로 무기한 수용한다. 둘 다 주고 값 불일치 → fail-loud
# (추측 금지). 솔로(미지정) 현행 경로 무변경. main() ingress→run() 전달 kwargs 를 캡처해
# 병합·work/ 프리픽스 유도·bare 거부·fail-loud 를 durable 하게 못박는다(T-0246·비-vacuous).


@pytest.fixture
def captured_run(hf, monkeypatch):
    """PmHandoff.run 을 가로채 kwargs 를 캡처(실 회귀/파일편집 없이 ingress 만 검증).

    main() 이 alias 병합·canonical 화·필수 검증을 마치고 run() 에 넘기는 kwargs 를 그대로
    포착한다 — fail-loud(parser.error·SystemExit) 케이스에선 run 미도달이라 dict 가 빈 채 남는다.
    """
    calls: dict = {}

    def _fake_run(self, **kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(hf.PmHandoff, "run", _fake_run)
    return calls


# --- 차수 인자: --session-seq(canonical) / --session-num(deprecated alias) ---

def test_session_seq_canonical_accepted(hf, captured_run):
    assert hf.main(["--session-seq", "42", "--wave-summary", "x", "--no-pytest"]) == 0
    assert captured_run["session_num"] == "42"


def test_session_num_deprecated_alias_accepted(hf, captured_run):
    # 구형 --session-num 무기한 수용 — 값 동일하게 run(session_num=) 으로 전달.
    assert hf.main(["--session-num", "42", "--wave-summary", "x", "--no-pytest"]) == 0
    assert captured_run["session_num"] == "42"


def test_session_seq_and_num_agree_accepted(hf, captured_run):
    assert hf.main(
        ["--session-seq", "42", "--session-num", "42", "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["session_num"] == "42"


def test_session_seq_and_num_mismatch_fails_loud(hf, captured_run):
    with pytest.raises(SystemExit):
        hf.main(
            ["--session-seq", "42", "--session-num", "43", "--wave-summary", "x", "--no-pytest"]
        )
    assert captured_run == {}  # run 미도달 — 추측 없이 거부.


def test_session_seq_missing_rejected(hf, captured_run):
    # canonical/alias 둘 다 미지정 → 필수 누락 거부(대화형 경로).
    with pytest.raises(SystemExit):
        hf.main(["--wave-summary", "x", "--no-pytest"])
    assert captured_run == {}


# --- 정체성 인자: --session(canonical) / --worktree-slot(deprecated alias) ---

def test_session_canonical_derives_work_prefix(hf, captured_run):
    # --session <repo>_<N> → 내부에서 work/ 프리픽스 유도 → worktree_slot=work/<repo>_<N>.
    assert hf.main(
        ["--session", "project_manager_1", "--session-seq", "7", "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/project_manager_1"


def test_worktree_slot_deprecated_alias_accepted(hf, captured_run):
    # 구형 --worktree-slot(qualified) 무기한 수용 — canonical 그대로 유지.
    assert hf.main(
        ["--worktree-slot", "work/project_manager_1", "--session-seq", "7",
         "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/project_manager_1"


def test_session_and_worktree_slot_agree_accepted(hf, captured_run):
    # --session <repo>_<N> 과 --worktree-slot work/<repo>_<N> 은 같은 슬롯 → 일치·canonical 채택.
    assert hf.main(
        ["--session", "project_manager_1", "--worktree-slot", "work/project_manager_1",
         "--session-seq", "7", "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/project_manager_1"


def test_session_and_worktree_slot_mismatch_fails_loud(hf, captured_run):
    with pytest.raises(SystemExit):
        hf.main(
            ["--session", "project_manager_1", "--worktree-slot", "work/finance_2",
             "--session-seq", "7", "--wave-summary", "x", "--no-pytest"]
        )
    assert captured_run == {}  # run 미도달 — 서로 다른 슬롯이면 추측 없이 거부.


def test_session_bare_number_rejected(hf, captured_run):
    # --session 4 는 bare 슬롯 번호 — repo 별 모호라 ingress 가 거부(ADR-0013·아래로 안 샘).
    with pytest.raises(SystemExit):
        hf.main(["--session", "4", "--session-seq", "7", "--wave-summary", "x", "--no-pytest"])
    assert captured_run == {}


def test_solo_unspecified_worktree_slot_none(hf, captured_run):
    # 솔로(정체성 미지정) 현행 경로 무변경 — worktree_slot None 로 run 진입.
    assert hf.main(["--session-seq", "7", "--wave-summary", "x", "--no-pytest"]) == 0
    assert captured_run["worktree_slot"] is None


# --- 병합 헬퍼 직접 단위 (parser.error seam 포함) ---

def test_reconcile_session_seq_prefers_canonical(hf):
    parser = hf.build_parser()
    assert hf._reconcile_session_seq("42", "42", parser) == "42"
    assert hf._reconcile_session_seq("42", None, parser) == "42"
    assert hf._reconcile_session_seq(None, "42", parser) == "42"
    assert hf._reconcile_session_seq(None, None, parser) is None


def test_reconcile_session_slot_canonical_equivalence(hf):
    parser = hf.build_parser()
    # 무접두 <repo>_<N> 과 work/ 접두는 같은 슬롯 → 일치(fail 안 함)·canonical(--session) 채택.
    assert hf._reconcile_session_slot(
        "project_manager_1", "work/project_manager_1", parser
    ) == "project_manager_1"
    # alias 만 → alias 값(이후 main 파이프라인이 canonical 화).
    assert hf._reconcile_session_slot(None, "work/project_manager_1", parser) == "work/project_manager_1"
    # 빈/공백 → None (미지정 동형·T-0201 하드닝 정합).
    assert hf._reconcile_session_slot("  ", None, parser) is None
    assert hf._reconcile_session_slot(None, None, parser) is None


def test_reconcile_helpers_mismatch_raise(hf):
    parser = hf.build_parser()
    with pytest.raises(SystemExit):
        hf._reconcile_session_seq("42", "43", parser)
    with pytest.raises(SystemExit):
        hf._reconcile_session_slot("project_manager_1", "work/finance_2", parser)


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


def test_session_done_canonical_reaches_release(hf, captured_run):
    """codex 게이트(2026-07-10 wave A) — canonical `--session <repo>_<N> --done` 이 release 대상
    (worktree_slot=`work/<repo>_<N>`·done=True)으로 run 에 도달한다. alias(`--worktree-slot`)로만
    release 되던 경로가 canonical `--session` 으로도 확실히 이어지는지 직접 못박아 전환 회귀 차단.
    """
    assert hf.main(
        ["--session", "project_manager_1", "--done", "--session-seq", "7",
         "--wave-summary", "x", "--no-pytest"]
    ) == 0
    assert captured_run["worktree_slot"] == "work/project_manager_1"
    assert captured_run["done"] is True
