"""pm_handoff.py 핵심 함수 직접 단위테스트 (T-0042).

PM 세션 라이프사이클 자동화의 다른 한 축 — pm_state.md 세션 식별 절의 sliding window
편집(`update_session_window`)과 그 하부 절 추출(`_extract_session_section`)·인계 프롬프트
템플릿 추출(`extract_handoff_prompt_template`)을 직접 검증한다.

기존 `test_handoff_trigger.py` 는 run_trigger 경로에서 이들을 *간접* 호출하고
`infer_next_session_num`·실 pm_playbook lean 검증을 덮는다 — 여기선 함수를 직접 호출하는
*절 경계·윈도 경계·앵커 불일치 ValueError·멱등·프롬프트 부재→None* edge 에 집중한다
([[T-0026]] 규율 — non-vacuous·실제 동작 단언). 모두 텍스트 인자라 실 wiki 미접촉.

T-0041 rename 반영 — `_extract_session_section(pm_state_text)` ·
`extract_handoff_prompt_template(pm_playbook_text)`.

도구는 패키지가 아니므로 importlib 동적 로드 (test_handoff_trigger 관용구).
"""
from __future__ import annotations

import importlib.util
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


@pytest.mark.parametrize("raw", ["19", "19차", "19차차"])
def test_trigger_skeleton_no_double_cha(hf, raw):
    head = hf.build_trigger_handoff_log_skeleton(
        raw, reason="ctx", ctx_pct=8, date="2026-06-19").splitlines()[0]
    assert "PM 19차 →" in head and "차차" not in head


@pytest.mark.parametrize("raw", ["20", "20차", "20차차"])
def test_session_window_entry_no_double_cha(hf, raw):
    # sliding-window entry 줄도 '**20차**' (정규식 `\\d+차` 매칭 보존 — 19차차면 매칭 깨짐).
    entry = hf._build_new_session_entry(raw, "2026-06-19", "wave")
    assert entry.startswith("  - **20차** ")
    assert "차차" not in entry
