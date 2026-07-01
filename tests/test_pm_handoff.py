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
    # 손-채움 인계 블록과 그 안내 헤더 둘 다 사라졌다 (본문은 log entry/부트스트랩이 carry).
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
    나온다 — 두 호출부(run·run_trigger 중 run) 배선을 통합으로 가드.
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


def test_run_trigger_passes_worktree_slot_to_prompt(hf, tmp_path):
    """_build_trigger_handoff_prompt_block(run_trigger 경로)이 self._worktree_slot 을 전달한다 (T-0185).

    trigger 는 정지되어 stdout 이 휘발하므로 프롬프트를 log entry 에 박제한다 — 그 박제 블록
    빌더가 slot 을 프롬프트에 넘기는지 단위로 가드.
    """
    playbook = tmp_path / "pm_playbook.md"
    playbook.write_text(_TRIGGER_PLAYBOOK, encoding="utf-8")

    handoff = hf.PmHandoff(
        run_pytest_fn=lambda: (0, "1 passed\n"),
        run_git_fn=lambda args: (0, ""),
        log_file=tmp_path / "current.md",
        pm_playbook_file=playbook,
        pm_state_file=tmp_path / "pm_state.md",
    )
    handoff._worktree_slot = "work/repoA_2"
    block = handoff._build_trigger_handoff_prompt_block(
        session_num=7, wave_summary="요약", date_str="2026-06-28"
    )
    assert "/pm-bootstrap repoA --slot 2" in block


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
    # 트리거 유지: 역할 framing + /pm-bootstrap.
    assert "PM" in template and "/pm-bootstrap" in template
    # 폐기: 손-채움 인계 블록(읽기 범위 손-기입 슬롯).
    assert "<핵심 인계 사항>" not in template
    assert "<- 읽기 범위" not in template


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
    assert result.endswith("work/project_manager_1")


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
    assert result.endswith("work/foo_2")


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
