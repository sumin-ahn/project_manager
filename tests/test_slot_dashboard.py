"""slot 대시보드(수정형) — handoff 자기 섹션 overwrite + bootstrap 타 PM light dump (T-0260·ADR-0047).

multi-PM 슬롯 간 공유를 *가벼운 수정형 대시보드*로 바꾼다: 핸드오프가 `wiki/log/dashboard.md` 의
**자기 섹션만 overwrite**(3~5줄 상한·타 슬롯 byte 불변·append 아님), 부트스트랩이 자기 섹션 제외한
타 PM 섹션을 light dump(대시보드 부재→현행 lease 폴백).

검증 축:
  - render_dashboard_section: 차수·wave·claimed·다음 3~5줄·개행 평탄화·char cap·max_lines truncate.
  - upsert_dashboard_section: lazy 생성·자기 섹션 overwrite·**타 슬롯 섹션 byte 불변**·멱등·헤딩 정확매치.
  - parse_dashboard_sections: write/read 경계 대칭·preamble 무시·round-trip.
  - _claimed_tickets_for_session: status-subdir/flat 레이아웃·세션 필터·fail-soft.
  - PmHandoff._write_dashboard_section: 파일 write·자기 섹션 overwrite + 타 섹션 byte 불변(통합).
  - PmBootstrap._collect_dashboard_others: 자기 제외 dump·lease 폴백·솔로 None·markdown 렌더.
  - handoff write → bootstrap read 라운드트립(실 DRY parse).

hermetic: 파일/REPO 접촉 테스트는 모듈-레벨 REPO 를 tmp 로 monkeypatch 한 fresh 인스턴스를 쓴다
(test_pm_state_per_slot 동류). 순수 함수는 인자로만 검증(실 wiki 미접촉).
"""
from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"

_DATE = "2026-07-10"


def _load(name: str):
    """도구 모듈을 importlib 경로 로드 (test_pm_state_per_slot 동일 규약)."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hf():
    """pm_handoff — 순수 함수(render/upsert/parse) 검증용(REPO 미접촉)."""
    return _load("pm_handoff")


# ══════════════════════════════════════════════════════════════════════════
# render_dashboard_section — 본문 구성·평탄화·상한
# ══════════════════════════════════════════════════════════════════════════

def test_render_section_heading_and_core_lines(hf):
    """헤딩 `## <session>` + 차수·wave 본문 + 후행 단일 개행."""
    sec = hf.render_dashboard_section(
        "project_manager_1", session_num=52, wave_summary="wave A", date=_DATE
    )
    assert sec.startswith("## project_manager_1\n\n")
    assert "- 차수: PM 52차 (2026-07-10)" in sec
    assert "- wave: wave A" in sec
    assert sec.endswith("\n") and not sec.endswith("\n\n")


def test_render_section_claimed_and_next_lines(hf):
    """claimed_tickets·next_plan 이 있으면 각 줄 추가(comma-join)."""
    sec = hf.render_dashboard_section(
        "project_manager_1",
        session_num=52,
        wave_summary="wave",
        claimed_tickets=["T-0260", "T-0251"],
        next_plan="다음 계획 X",
        date=_DATE,
    )
    assert "- claimed: T-0260, T-0251" in sec
    assert "- 다음: 다음 계획 X" in sec


def test_render_section_omits_empty_claimed_and_next(hf):
    """claimed 빈 목록·next_plan None 이면 해당 줄 생략(차수·wave 2줄만)."""
    sec = hf.render_dashboard_section(
        "project_manager_1", session_num=1, wave_summary="w", claimed_tickets=[], date=_DATE
    )
    body = sec.split("\n\n", 1)[1]
    assert body.strip().count("\n") == 1  # 차수·wave 2줄
    assert "claimed:" not in sec
    assert "다음:" not in sec


def test_render_section_flattens_multiline_wave(hf):
    """다중행 wave_summary 는 한 줄로 평탄화 — 섹션 경계/추가 줄 위조 차단."""
    sec = hf.render_dashboard_section(
        "project_manager_1",
        session_num=1,
        wave_summary="line1\n## injected heading\nline2",
        date=_DATE,
    )
    # 본문에 '## injected' 가 독립 헤딩 줄로 새지 않는다(공백으로 접힘).
    assert "\n## injected heading" not in sec
    assert "- wave: line1 ## injected heading line2" in sec
    # 섹션 헤딩은 자기 것 하나뿐.
    assert sec.count("\n## ") == 0


def test_render_section_char_cap(hf):
    """긴 단일 값은 DASHBOARD_LINE_MAX_CHARS 로 truncate('…' 부착)."""
    long_wave = "x" * 500
    sec = hf.render_dashboard_section(
        "project_manager_1", session_num=1, wave_summary=long_wave, date=_DATE
    )
    wave_line = [l for l in sec.splitlines() if l.startswith("- wave:")][0]
    value = wave_line[len("- wave: "):]
    assert value.endswith("…")
    assert len(value) <= hf.DASHBOARD_LINE_MAX_CHARS


def test_render_section_max_lines_truncate(hf):
    """max_lines 상한 초과 본문줄은 drop(3~5줄 상한 truncate)."""
    sec = hf.render_dashboard_section(
        "project_manager_1",
        session_num=1,
        wave_summary="w",
        claimed_tickets=["T-1"],
        next_plan="plan",
        max_lines=2,
        date=_DATE,
    )
    body = sec.split("\n\n", 1)[1].rstrip("\n")
    assert len(body.splitlines()) == 2  # 차수·wave 만 남음
    assert "claimed:" not in sec
    assert "다음:" not in sec


def test_dashboard_section_max_lines_default_is_three_to_five(hf):
    """기본 상한 상수는 3~5줄 범위(ADR-0047)."""
    assert 3 <= hf.DASHBOARD_SECTION_MAX_LINES <= 5


# ══════════════════════════════════════════════════════════════════════════
# upsert_dashboard_section — lazy 생성·overwrite·타 섹션 byte 불변·멱등
# ══════════════════════════════════════════════════════════════════════════

def _sec(hf, session: str, num: int, wave: str) -> str:
    return hf.render_dashboard_section(
        session, session_num=num, wave_summary=wave, date=_DATE
    )


def test_upsert_lazy_create_on_empty(hf):
    """빈 대시보드 → 헤더 + 자기 섹션 lazy 생성."""
    doc = hf.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "w"))
    assert doc.startswith(hf._DASHBOARD_HEADER)
    assert "## project_manager_1" in doc


def test_upsert_overwrite_own_section_replaces_content(hf):
    """자기 섹션 재 upsert → 새 내용으로 교체(옛 wave 사라짐·멱등적 갱신)."""
    doc = hf.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "OLD"))
    doc2 = hf.upsert_dashboard_section(doc, "project_manager_1", _sec(hf, "project_manager_1", 2, "NEW"))
    assert "NEW" in doc2 and "OLD" not in doc2
    assert doc2.count("## project_manager_1") == 1  # 중복 섹션 안 생김


def test_upsert_idempotent_same_section(hf):
    """같은 section 재 upsert → 결과 동일(멱등)."""
    sec = _sec(hf, "project_manager_1", 1, "w")
    doc = hf.upsert_dashboard_section("", "project_manager_1", sec)
    assert hf.upsert_dashboard_section(doc, "project_manager_1", sec) == doc


def test_upsert_other_section_byte_invariant(hf):
    """자기 섹션 overwrite 시 **타 슬롯 섹션은 byte 불변** (ADR-0047 핵심·못박기)."""
    doc = hf.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "A_old"))
    doc = hf.upsert_dashboard_section(doc, "finance_2", _sec(hf, "finance_2", 5, "B_wave"))

    def finance_region(text: str) -> str:
        idx = text.index("## finance_2")
        return text[idx:]

    before = finance_region(doc)
    doc2 = hf.upsert_dashboard_section(doc, "project_manager_1", _sec(hf, "project_manager_1", 2, "A_new"))
    after = finance_region(doc2)
    assert before == after  # finance_2 섹션 bytes 완전 동일
    assert "A_new" in doc2 and "A_old" not in doc2


def test_upsert_heading_exact_match_no_prefix_clobber(hf):
    """`project_manager_1` overwrite 가 `project_manager_10` 섹션을 오매치·훼손하지 않는다."""
    doc = hf.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "one"))
    doc = hf.upsert_dashboard_section(doc, "project_manager_10", _sec(hf, "project_manager_10", 3, "ten"))
    ten_before = doc[doc.index("## project_manager_10"):]
    doc2 = hf.upsert_dashboard_section(doc, "project_manager_1", _sec(hf, "project_manager_1", 2, "one_new"))
    assert doc2[doc2.index("## project_manager_10"):] == ten_before
    assert "one_new" in doc2


def test_upsert_append_keeps_existing_sections(hf):
    """신규 슬롯 append 시 기존 섹션들 보존(마지막에 추가)."""
    doc = hf.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "a"))
    doc2 = hf.upsert_dashboard_section(doc, "finance_2", _sec(hf, "finance_2", 2, "b"))
    keys = [k for k, _ in hf.parse_dashboard_sections(doc2)]
    assert keys == ["project_manager_1", "finance_2"]


# ══════════════════════════════════════════════════════════════════════════
# parse_dashboard_sections — 경계·round-trip·preamble
# ══════════════════════════════════════════════════════════════════════════

def test_parse_round_trip_two_sections(hf):
    """upsert 로 만든 2섹션 대시보드를 parse → 키·본문 왕복 일치(write/read 대칭)."""
    doc = hf.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "waveA"))
    doc = hf.upsert_dashboard_section(doc, "finance_2", _sec(hf, "finance_2", 2, "waveB"))
    parsed = dict(hf.parse_dashboard_sections(doc))
    assert set(parsed) == {"project_manager_1", "finance_2"}
    assert "waveA" in parsed["project_manager_1"]
    assert "waveB" in parsed["finance_2"]
    # 본문은 헤딩 제외·surrounding 빈 줄 제거.
    assert not parsed["project_manager_1"].startswith("\n")
    assert "## project_manager_1" not in parsed["project_manager_1"]


def test_parse_ignores_preamble(hf):
    """첫 `## ` 앞 preamble(`# 헤더`)은 섹션으로 잡지 않는다."""
    doc = "# slot 대시보드\n\n## project_manager_1\n\n- 차수: PM 1차\n"
    parsed = hf.parse_dashboard_sections(doc)
    assert [k for k, _ in parsed] == ["project_manager_1"]


def test_parse_empty_returns_empty(hf):
    """섹션 없는 텍스트 → 빈 목록."""
    assert hf.parse_dashboard_sections("") == []
    assert hf.parse_dashboard_sections("# preamble only\n") == []


# ══════════════════════════════════════════════════════════════════════════
# _claimed_tickets_for_session — board tickets 스캔(레이아웃·필터·fail-soft)
# ══════════════════════════════════════════════════════════════════════════

def _write_ticket(path: Path, tid: str, status: str, claimed_by: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nid: {tid}\ntitle: t\nstatus: {status}\n"
        f"claimed_by: {claimed_by}\n---\n# body\n",
        encoding="utf-8",
    )


def test_claimed_tickets_status_subdir_layout(hf, tmp_path):
    """status-subdir(`claimed/`) 레이아웃 — 세션 claimed_by 매칭 티켓 id 반환."""
    tickets = tmp_path / "tickets"
    _write_ticket(tickets / "claimed" / "T-0260.md", "T-0260", "claimed", "me@x/project_manager_1")
    _write_ticket(tickets / "claimed" / "T-0299.md", "T-0299", "claimed", "you@x/finance_2")
    got = hf._claimed_tickets_for_session("project_manager_1", tickets_dir=tickets)
    assert got == ["T-0260"]


def test_claimed_tickets_flat_layout_filters_status(hf, tmp_path):
    """flat 레이아웃 — `status: claimed` 만·세션 매칭."""
    tickets = tmp_path / "tickets"
    _write_ticket(tickets / "T-0260.md", "T-0260", "claimed", "me@x/project_manager_1")
    _write_ticket(tickets / "T-0100.md", "T-0100", "done", "me@x/project_manager_1")
    got = hf._claimed_tickets_for_session("project_manager_1", tickets_dir=tickets)
    assert got == ["T-0260"]


def test_claimed_tickets_absent_dir_failsoft(hf, tmp_path):
    """티켓 디렉토리 부재 → 빈 목록(fail-soft·대시보드 claimed 줄 생략)."""
    assert hf._claimed_tickets_for_session("project_manager_1", tickets_dir=tmp_path / "nope") == []


# ══════════════════════════════════════════════════════════════════════════
# PmHandoff._write_dashboard_section — 파일 write 통합(overwrite·타 섹션 불변)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def hf_repo(tmp_path, monkeypatch):
    """pm_handoff — 모듈 REPO 를 tmp 로 monkeypatch(파일 write 격리)."""
    mod = _load("pm_handoff")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def _dash_path(mod) -> Path:
    return mod._tmp / ".project_manager" / "wiki" / "log" / "dashboard.md"


def test_write_dashboard_section_creates_file(hf_repo):
    """_write_dashboard_section — 대시보드 파일 lazy 생성·차수·wave·claimed 반영."""
    # 세션이 claim 한 티켓 1개 배치(claimed 줄 검증).
    _write_ticket(
        hf_repo._tmp / ".project_manager" / "board" / "tickets" / "claimed" / "T-0260.md",
        "T-0260", "claimed", "me@x/project_manager_1",
    )
    inst = hf_repo.PmHandoff()
    out = inst._write_dashboard_section("project_manager_1", 52, "wave 요약 문장", _DATE)
    text = out.read_text(encoding="utf-8")
    assert out == _dash_path(hf_repo)
    assert "## project_manager_1" in text
    assert "- 차수: PM 52차 (2026-07-10)" in text
    assert "- wave: wave 요약 문장" in text
    assert "- claimed: T-0260" in text


def test_write_dashboard_section_overwrites_own_keeps_other(hf_repo):
    """_write_dashboard_section 재실행 → 자기 섹션 overwrite·타 슬롯 섹션 byte 불변(통합)."""
    inst = hf_repo.PmHandoff()
    inst._write_dashboard_section("project_manager_1", 51, "waveOld", _DATE)
    # 타 슬롯 섹션을 파일에 주입.
    dash = _dash_path(hf_repo)
    injected = hf_repo.upsert_dashboard_section(
        dash.read_text(encoding="utf-8"),
        "finance_2",
        hf_repo.render_dashboard_section("finance_2", session_num=9, wave_summary="waveFin", date=_DATE),
    )
    dash.write_text(injected, encoding="utf-8")
    fin_before = injected[injected.index("## finance_2"):]

    inst._write_dashboard_section("project_manager_1", 52, "waveNew", _DATE)
    after = dash.read_text(encoding="utf-8")
    assert "waveNew" in after and "waveOld" not in after
    assert after[after.index("## finance_2"):] == fin_before  # 타 슬롯 byte 불변


# ══════════════════════════════════════════════════════════════════════════
# PmBootstrap._collect_dashboard_others — 자기 제외 dump·lease 폴백·솔로 None
# ══════════════════════════════════════════════════════════════════════════

def _make_bs(tmp_path, monkeypatch, *, load_tool_hf=None):
    """pm_bootstrap — REPO tmp monkeypatch. load_tool_hf 주면 _load_tool 을 실 모듈로 대체.

    `_load_tool` 은 pm_handoff(대시보드 parse·차수)·pm_log(log entry split)을 동적로드하는데,
    REPO 를 tmp 로 monkeypatch 하면 TOOLS_DIR 이 없어 None 이 된다 — 실 canonical 도구를 이름으로
    되돌려 log_entry/차수 경로도 hermetic 하게 동작하게 한다.
    """
    bs = _load("pm_bootstrap")
    monkeypatch.setattr(bs, "REPO", tmp_path)
    if load_tool_hf is not None:
        def _fake_load_tool(name):
            if name == "pm_handoff":
                return load_tool_hf
            if name == "pm_log":
                return _load("pm_log")
            return None
        monkeypatch.setattr(bs, "_load_tool", _fake_load_tool)
    return bs


def _write_dashboard(tmp_path, hf, *sections: tuple[str, int, str]) -> Path:
    doc = ""
    for session, num, wave in sections:
        doc = hf.upsert_dashboard_section(
            doc, session, hf.render_dashboard_section(session, session_num=num, wave_summary=wave, date=_DATE)
        )
    path = tmp_path / ".project_manager" / "wiki" / "log" / "dashboard.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


def test_collect_dashboard_others_excludes_own(tmp_path, monkeypatch):
    """대시보드 존재 → 자기 세션 제외 타 슬롯만 dump(자기 섹션 미표시·ADR-0047)."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(tmp_path, hf, ("project_manager_1", 52, "mineWave"), ("finance_2", 7, "finWave"))
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    result = inst._collect_dashboard_others()
    assert result is not None and result["mode"] == "dashboard"
    sessions = [o["session"] for o in result["others"]]
    assert "finance_2" in sessions
    assert "project_manager_1" not in sessions  # 자기 섹션 제외
    fin = next(o for o in result["others"] if o["session"] == "finance_2")
    assert "finWave" in fin["body"] and "mineWave" not in fin["body"]


def test_collect_dashboard_others_only_own_returns_none(tmp_path, monkeypatch):
    """대시보드에 자기 섹션뿐 → None(타 PM 없음·절 생략)."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(tmp_path, hf, ("project_manager_1", 52, "mineWave"))
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    assert inst._collect_dashboard_others() is None


def test_collect_dashboard_others_task_mode_excludes_own_task(tmp_path, monkeypatch):
    """task dashboard 키는 task 이름이므로 자기 task를 '다른 활성 PM'으로 다시 표시하지 않는다."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(
        tmp_path, hf,
        ("mytask", 3, "mineTaskWave"),
        ("other-task", 2, "otherTaskWave"),
    )
    inst = bs.PmBootstrap()
    inst._task_name = "mytask"
    result = inst._collect_dashboard_others()
    assert result is not None and result["mode"] == "dashboard"
    assert [o["session"] for o in result["others"]] == ["other-task"]
    assert "mineTaskWave" not in result["others"][0]["body"]


def test_collect_dashboard_others_lease_fallback(tmp_path, monkeypatch):
    """대시보드 부재 → lease 장부 폴백(자기 제외 leased 슬롯)."""
    bs = _make_bs(tmp_path, monkeypatch)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    leases.write_text(
        '{"leases":['
        '{"slot":"work/project_manager_1","repo":"project_manager","session":"project_manager_1","state":"leased"},'
        '{"slot":"work/finance_2","repo":"finance","session":"finance_2","state":"leased"}]}',
        encoding="utf-8",
    )
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    result = inst._collect_dashboard_others()
    assert result["mode"] == "lease"
    assert [o["session"] for o in result["others"]] == ["finance_2"]


def test_collect_dashboard_others_task_lease_fallback_excludes_own_task(tmp_path, monkeypatch):
    """대시보드 부재 lease 폴백도 task 이름을 자기 키로 제외한다."""
    bs = _make_bs(tmp_path, monkeypatch)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    leases.write_text(
        '{"leases":['
        '{"slot":"work/project_manager_1","repo":"project_manager","session":"mytask","state":"leased"},'
        '{"slot":"work/finance_2","repo":"finance","session":"other-task","state":"leased"}]}',
        encoding="utf-8",
    )
    inst = bs.PmBootstrap()
    inst._task_name = "mytask"
    result = inst._collect_dashboard_others()
    assert result["mode"] == "lease"
    assert [o["session"] for o in result["others"]] == ["other-task"]


def test_collect_dashboard_others_solo_none(tmp_path, monkeypatch):
    """대시보드 부재 + 자기 슬롯만 leased → None(솔로·절 생략)."""
    bs = _make_bs(tmp_path, monkeypatch)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    leases.write_text(
        '{"leases":[{"slot":"work/project_manager_1","repo":"project_manager",'
        '"session":"project_manager_1","state":"leased"}]}',
        encoding="utf-8",
    )
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    assert inst._collect_dashboard_others() is None


def test_lease_fallback_skips_idle(tmp_path, monkeypatch):
    """lease 폴백은 idle(반납) 슬롯 제외 — 활성(leased)만."""
    bs = _make_bs(tmp_path, monkeypatch)
    leases = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    leases.parent.mkdir(parents=True, exist_ok=True)
    leases.write_text(
        '{"leases":['
        '{"slot":"work/project_manager_1","repo":"project_manager","session":"project_manager_1","state":"leased"},'
        '{"slot":"work/finance_2","repo":"finance","session":"finance_2","state":"idle"}]}',
        encoding="utf-8",
    )
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    assert inst._collect_dashboard_others() is None  # finance_2 는 idle → 제외 → 솔로


# ══════════════════════════════════════════════════════════════════════════
# _build_markdown / _build_json — 다른 활성 PM 절 렌더/생략
# ══════════════════════════════════════════════════════════════════════════

_BOARD = {"counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0}, "open_tickets": [], "lint": "clean"}
_GIT = {"branch": "main", "commits": [], "working_tree": "clean"}


def test_build_markdown_renders_dashboard_others(hf):
    """dashboard_others(dashboard 모드) → "### 다른 활성 PM" 절에 타 슬롯 섹션 dump."""
    bs = _load("pm_bootstrap")
    inst = bs.PmBootstrap(run_git_fn=lambda a: (0, ""))
    others = {"mode": "dashboard", "others": [
        {"session": "finance_2", "body": "- 차수: PM 7차 (2026-07-10)\n- wave: finWave"}]}
    md = inst._build_markdown(_BOARD, None, _GIT, None, "ts", None, others)
    assert "### 다른 활성 PM" in md
    assert "**finance_2**" in md
    assert "finWave" in md


def test_build_markdown_omits_section_when_none(hf):
    """dashboard_others None(솔로) → "다른 활성 PM" 절 자체 생략(무노이즈)."""
    bs = _load("pm_bootstrap")
    inst = bs.PmBootstrap(run_git_fn=lambda a: (0, ""))
    md = inst._build_markdown(_BOARD, None, _GIT, None, "ts", None, None)
    assert "### 다른 활성 PM" not in md


def test_build_markdown_lease_fallback_render(hf):
    """dashboard_others(lease 모드) → 세션·슬롯 목록 폴백 렌더."""
    bs = _load("pm_bootstrap")
    inst = bs.PmBootstrap(run_git_fn=lambda a: (0, ""))
    others = {"mode": "lease", "others": [{"session": "finance_2", "slot": "work/finance_2"}]}
    md = inst._build_markdown(_BOARD, None, _GIT, None, "ts", None, others)
    assert "### 다른 활성 PM" in md
    assert "`finance_2`" in md and "`work/finance_2`" in md


def test_build_json_carries_dashboard_others(hf):
    """JSON 렌더도 dashboard_others 를 그대로 실어 보낸다(소비자 일관성)."""
    bs = _load("pm_bootstrap")
    inst = bs.PmBootstrap(run_git_fn=lambda a: (0, ""))
    others = {"mode": "dashboard", "others": [{"session": "finance_2", "body": "- 차수: PM 7차"}]}
    data = inst._build_json(_BOARD, None, _GIT, None, "ts", None, others)
    assert data["dashboard_others"] == others


# ══════════════════════════════════════════════════════════════════════════
# 라운드트립 — handoff write → bootstrap read (실 DRY parse·자기 제외)
# ══════════════════════════════════════════════════════════════════════════

def test_roundtrip_handoff_write_bootstrap_read(tmp_path, monkeypatch):
    """handoff 가 두 슬롯 섹션을 write → bootstrap(슬롯A bound)이 A 제외·B dump(실 parse 재사용)."""
    hf = _load("pm_handoff")
    monkeypatch.setattr(hf, "REPO", tmp_path)
    inst_hf = hf.PmHandoff()
    inst_hf._write_dashboard_section("project_manager_1", 52, "mineWave", _DATE)
    inst_hf._write_dashboard_section("finance_2", 7, "finWave", _DATE)

    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    inst_bs = bs.PmBootstrap()
    inst_bs._bound_slot = "work/project_manager_1"
    result = inst_bs._collect_dashboard_others()
    assert result["mode"] == "dashboard"
    sessions = [o["session"] for o in result["others"]]
    assert sessions == ["finance_2"]  # 자기(project_manager_1) 제외·B 만
    assert "finWave" in result["others"][0]["body"]


# ══════════════════════════════════════════════════════════════════════════
# MF-1 (codex) — 대시보드 dump 는 활성(leased) 슬롯과 교집합(idle/released 배제)
# ══════════════════════════════════════════════════════════════════════════

def _write_leases(tmp_path, *entries: tuple[str, str]) -> Path:
    """(session, state) 목록으로 leases.json 을 쓴다 — slot 은 work/<session> 유도."""
    rows = ",".join(
        '{"slot":"work/%s","repo":"%s","session":"%s","state":"%s"}'
        % (sess, sess.rsplit("_", 1)[0], sess, state)
        for sess, state in entries
    )
    path = tmp_path / ".project_manager" / ".local" / "worktree-leases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"leases":[%s]}' % rows, encoding="utf-8")
    return path


def test_dashboard_dump_excludes_released_idle_slot(tmp_path, monkeypatch):
    """MF-1: 대시보드에 stale 섹션이 남아도, idle(released) 슬롯은 활성 교집합에서 배제된다.

    재현: finance_2 가 --done 으로 release 돼 idle 인데 대시보드 섹션은 그대로 남음. leased 인
    finance_3 만 dump 에 노출되어야 한다(release 누락/크래시에도 idle 노출 0)."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(
        tmp_path, hf,
        ("project_manager_1", 52, "mineWave"),  # 자기(bound)
        ("finance_2", 7, "idleWave"),           # release 됨 → idle (stale 섹션)
        ("finance_3", 4, "activeWave"),         # 여전히 leased
    )
    _write_leases(
        tmp_path,
        ("project_manager_1", "leased"),
        ("finance_2", "idle"),      # released → idle
        ("finance_3", "leased"),
    )
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    result = inst._collect_dashboard_others()
    sessions = [o["session"] for o in result["others"]]
    assert sessions == ["finance_3"]  # idle finance_2 배제·leased finance_3 만
    assert "finance_2" not in sessions


def test_dashboard_dump_all_idle_returns_none(tmp_path, monkeypatch):
    """MF-1: 타 슬롯이 전부 idle 이면(활성 교집합 공집합) 절 생략(None)."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(tmp_path, hf, ("project_manager_1", 52, "mine"), ("finance_2", 7, "idle"))
    _write_leases(tmp_path, ("project_manager_1", "leased"), ("finance_2", "idle"))
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    assert inst._collect_dashboard_others() is None


def test_dashboard_dump_no_leases_filter_noop(tmp_path, monkeypatch):
    """MF-1: 장부 판정불가(부재)면 활성 필터 no-op(현행 표시 보존 — 활성여부 알 수 없어 배제 안 함)."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(tmp_path, hf, ("project_manager_1", 52, "mine"), ("finance_2", 7, "fin"))
    # leases 파일 미생성 → _active_leased_sessions None → 필터 no-op.
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_1"
    result = inst._collect_dashboard_others()
    assert [o["session"] for o in result["others"]] == ["finance_2"]


def test_active_leased_sessions_none_when_ledger_absent(tmp_path, monkeypatch):
    """_active_leased_sessions — 장부 부재 → None(판정불가)·정상 read → leased 세션 집합."""
    bs = _make_bs(tmp_path, monkeypatch)
    inst = bs.PmBootstrap()
    assert inst._active_leased_sessions() is None
    _write_leases(tmp_path, ("project_manager_1", "leased"), ("finance_2", "idle"))
    assert inst._active_leased_sessions() == {"project_manager_1"}  # idle 제외


# ══════════════════════════════════════════════════════════════════════════
# MF-2 (codex) — 대시보드 read-modify-write 파일락 직렬화(lost update 차단)
# ══════════════════════════════════════════════════════════════════════════

def test_dashboard_lock_creates_lock_file(hf_repo):
    """_dashboard_lock 진입 → `.local/dashboard.lock` 생성·CM yield·정상 해제."""
    with hf_repo._dashboard_lock():
        assert hf_repo._dashboard_lock_file().exists()
    # 재진입(순차)도 무해 — 락 재획득·해제.
    with hf_repo._dashboard_lock():
        pass


def test_write_dashboard_section_goes_through_lock(hf_repo, monkeypatch):
    """MF-2: _write_dashboard_section 의 read-modify-write 가 `_dashboard_lock` 안에서 일어난다."""
    events: list[str] = []

    @contextlib.contextmanager
    def spy_lock():
        events.append("enter")
        yield
        events.append("exit")

    monkeypatch.setattr(hf_repo, "_dashboard_lock", spy_lock)
    inst = hf_repo.PmHandoff()
    inst._write_dashboard_section("project_manager_1", 1, "w", _DATE)
    assert events == ["enter", "exit"]  # write 가 락 구간 안에서 완결


def test_write_dashboard_reads_fresh_under_lock_no_lost_update(hf_repo):
    """MF-2: 두 다른 슬롯이 순차 write 해도 서로 안 덮는다 — 각 write 가 락 안에서 최신 파일 재-read.

    lost update 재현 시나리오: A 가 자기 섹션을 쓴 뒤, B(다른 슬롯)가 자기 섹션을 쓰고, A 가
    다시 자기 섹션을 갱신 — A 의 재-write 가 stale 캐시가 아니라 최신 파일을 읽어 B 섹션을
    보존해야 한다(직렬화·락이 이 read↔write 원자성을 프로세스 간에도 보장)."""
    inst = hf_repo.PmHandoff()
    inst._write_dashboard_section("project_manager_1", 51, "A_v1", _DATE)   # A
    inst._write_dashboard_section("finance_2", 7, "B_wave", _DATE)         # B (다른 슬롯)
    dash = _dash_path(hf_repo)
    b_before = dash.read_text(encoding="utf-8")
    b_region = b_before[b_before.index("## finance_2"):]
    inst._write_dashboard_section("project_manager_1", 52, "A_v2", _DATE)   # A 재-write
    after = dash.read_text(encoding="utf-8")
    assert after[after.index("## finance_2"):] == b_region  # B 섹션 lost update 0
    assert "A_v2" in after and "A_v1" not in after


# ══════════════════════════════════════════════════════════════════════════
# MF-3 (reviewer) — 대시보드는 git-untracked 파생물(.gitignore 배선)
# ══════════════════════════════════════════════════════════════════════════

def test_dashboard_gitignored():
    """MF-3: `.project_manager/.gitignore` 가 `wiki/log/dashboard.md` 를 무시(공개 제품 이물질 미출하)."""
    gitignore = REPO / ".project_manager" / ".gitignore"
    lines = [l.strip() for l in gitignore.read_text(encoding="utf-8").splitlines()]
    assert "wiki/log/dashboard.md" in lines


# ══════════════════════════════════════════════════════════════════════════
# codex suggestion — claimed_by YAML 따옴표 값 견고화
# ══════════════════════════════════════════════════════════════════════════

def test_claimed_tickets_quoted_claimed_by(hf, tmp_path):
    """claimed_by/id 가 YAML 따옴표 값이어도 세션 매칭·id 추출(따옴표 strip)."""
    tickets = tmp_path / "tickets"
    _write_ticket(tickets / "claimed" / "T-0260.md", '"T-0260"', "claimed", '"me@x/project_manager_1"')
    got = hf._claimed_tickets_for_session("project_manager_1", tickets_dir=tickets)
    assert got == ["T-0260"]  # 따옴표 벗겨 매칭·id 추출


# ══════════════════════════════════════════════════════════════════════════
# MF-1 (codex R2) — alloc 모드: 대시보드 수집이 정체성 확정(alloc) 뒤에 일어난다
# ══════════════════════════════════════════════════════════════════════════

class _AllocLease:
    def __init__(self, slot, repo):
        self.slot = slot
        self.repo = repo


class _AllocPool:
    """최소 worktree_pool mock — alloc/release + leased 장부 추적(alloc 순서·lease 누수 테스트)."""

    class NeedsCreate(Exception):
        def __init__(self, repo):
            self.repo = repo
            super().__init__(repo)

    def __init__(self, alloc_slot):
        self._alloc_slot = alloc_slot
        self.alloc_calls = 0
        self.release_calls: list[str] = []
        self.leased: set[str] = set()  # 현재 leased 슬롯(alloc→추가·release→제거).

    def alloc(self, repo, *, branch=None, resume=None, **_):
        self.alloc_calls += 1
        self.leased.add(self._alloc_slot)
        return _AllocLease(self._alloc_slot, repo)

    def release(self, slot, *, require_clean=True, **_):
        self.release_calls.append(slot)
        self.leased.discard(slot)
        return _AllocLease(slot, "project_manager")

    def slot_path(self, slot):
        return Path("/tmp/mp") / slot

    def current_branch(self, slot, *, git_runner=None):
        return "feat-branch"


def _stub_bootstrap(bs, tmp_path, pool, *, log_text: str = "# log\n"):
    """DI stub 로 격리된 PmBootstrap(alloc 모드 run 용) — board/git/pytest/log/areas/pm_state 주입."""
    areas = tmp_path / "areas.md"
    areas.write_text("| repo | prefix |\n|---|---|\n| project_manager | PM |\n", encoding="utf-8")
    log_file = tmp_path / "current.md"
    log_file.write_text(log_text, encoding="utf-8")
    pm_state = tmp_path / "pm_state.md"
    pm_state.write_text("", encoding="utf-8")

    def fake_board(args):
        if args[:1] == ["lint"]:
            return 0, "✓ no lint issues\n"
        return 0, "  [open   ] T-0001  x  pm  t\n"

    def fake_git(args):
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc commit\n"
        return 0, ""

    return bs.PmBootstrap(
        run_board_fn=fake_board,
        run_pytest_fn=lambda: (0, ""),
        run_git_fn=fake_git,
        log_file=log_file,
        areas_file=areas,
        worktree_pool=pool,
        pm_state_file=pm_state,
    )


def test_alloc_mode_dashboard_collected_after_identity(tmp_path, monkeypatch, capsys):
    """MF-1: --repo alloc 모드에서 기존 활성 PM 섹션이 누락 0 으로 dump·방금 alloc 한 자기 슬롯 제외.

    재현: 기존 leased 슬롯 project_manager_1(PM A)이 유일 leased. 새 PM 이 --repo project_manager
    로 alloc → project_manager_2 획득. 수정 전엔 `_bound_slot` 미설정 상태로 대시보드를 수집해
    `_bound_session_name` 이 project_manager_1 을 자기로 오인 → PM A 섹션 누락(dump 에서 사라짐).
    수정 후엔 alloc 이 대시보드 수집 전에 정체성(project_manager_2)을 확정 → PM A 정상 dump."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(tmp_path, hf, ("project_manager_1", 40, "PM A wave"))
    _write_leases(tmp_path, ("project_manager_1", "leased"))
    pool = _AllocPool("work/project_manager_2")
    inst = _stub_bootstrap(bs, tmp_path, pool)
    rc = inst.run(repo="project_manager")
    out = capsys.readouterr().out
    assert rc == 0
    assert pool.alloc_calls == 1  # alloc 정확히 1회(선-alloc·재-alloc 없음)
    assert "### 다른 활성 PM" in out
    dash_block = out.split("### 다른 활성 PM", 1)[1].split("\n###", 1)[0]
    assert "project_manager_1" in dash_block  # 기존 활성 PM A 누락 0
    assert "PM A wave" in dash_block
    # 방금 alloc 한 자기 슬롯(project_manager_2)은 대시보드에 없어 이 절에 안 나온다(자기 제외).
    assert "project_manager_2" not in dash_block


def test_alloc_mode_all_bound_collections_use_new_slot(tmp_path, monkeypatch, capsys):
    """MF R3(포괄): alloc 모드에서 log_entry·차수·대시보드 3개 전부가 새-슬롯 정체성을 본다(유입 0).

    기존 활성 PM A(project_manager_1)가 유일 leased·log 에 40차 handoff(본문 SENTINEL)·대시보드
    섹션. 새 PM 이 --repo project_manager alloc → project_manager_2. 수정 전엔 log_entry/handoff_
    context 가 alloc 전에 돌아 project_manager_1 을 자기로 오인 → PM A 의 log 본문·차수(41)를 자기
    컨텍스트로 표시(cross-slot 유입). 수정 후엔 셋 다 새 슬롯(project_manager_2·fresh) 기준."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_dashboard(tmp_path, hf, ("project_manager_1", 40, "PM A wave"))
    _write_leases(tmp_path, ("project_manager_1", "leased"))
    log_text = (
        "# log\n\n"
        "## [2026-07-01] handoff | PM 40차 (project_manager_1) → 다음 PM 세션\n\n"
        "- 읽기 범위: SENTINEL_PM_A_BODY 전용 인계\n"
        "- 메타 학습: 없음\n"
    )
    pool = _AllocPool("work/project_manager_2")
    inst = _stub_bootstrap(bs, tmp_path, pool, log_text=log_text)
    rc = inst.run(repo="project_manager")
    out = capsys.readouterr().out
    assert rc == 0 and pool.alloc_calls == 1
    # (a) log_entry: 기존 슬롯 handoff 본문 미표시(새 슬롯 fresh→None·cross-slot 유입 0).
    assert "SENTINEL_PM_A_BODY" not in out
    # (b) 차수: 기존 슬롯 40차 미상속 — 새 슬롯 fresh(1차).
    assert "PM 1차 부트스트랩" in out
    assert "41차" not in out
    # (c) 대시보드: 기존 슬롯을 '다른 활성 PM'으로·자기 새 슬롯 제외.
    dash_block = out.split("### 다른 활성 PM", 1)[1].split("\n###", 1)[0]
    assert "project_manager_1" in dash_block
    assert "project_manager_2" not in dash_block


def test_write_dashboard_section_atomic_no_tmp_left(hf_repo):
    """suggestion: 대시보드 write 는 tmp→os.replace 원자 교체 — tmp 잔여 없음·내용 온전."""
    inst = hf_repo.PmHandoff()
    out = inst._write_dashboard_section("project_manager_1", 5, "atomic wave", _DATE)
    # 원자 교체 완료 후 .tmp 사이드카가 남지 않는다.
    assert not out.with_suffix(out.suffix + ".tmp").exists()
    text = out.read_text(encoding="utf-8")
    assert "## project_manager_1" in text and "atomic wave" in text


# ══════════════════════════════════════════════════════════════════════════
# MF R4 — alloc 모드: cwd-의존 수집(freshness/git/pytest)도 새 슬롯 cwd 를 본다
# ══════════════════════════════════════════════════════════════════════════

def test_alloc_mode_cwd_uses_new_slot_not_existing(tmp_path, monkeypatch):
    """MF R4: alloc 모드에서 freshness/git/pytest 의 cwd(정체성)가 **새 슬롯**·기존 슬롯 아님.

    `_worktree_cwd(_bound_slot)` 로 cwd 를 잡는데, 정체성을 모든 수집보다 먼저 확정 안 하면
    `_worktree_cwd(None)` 자동해소로 기존 단일 leased 슬롯 cwd 에서 fetch/pull·pytest·branch/
    status 를 보고한다(cwd 유입). alloc 을 run() 맨 앞으로 올려 모든 cwd-의존 수집이 새 슬롯을
    본다. codex R5 방지 — 러너가 실제로 본 `-C` 경로 + 수집 시점 정체성을 단언한다."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    _write_leases(tmp_path, ("project_manager_1", "leased"))  # 기존 활성 슬롯
    pool = _AllocPool("work/project_manager_2")
    inst = _stub_bootstrap(bs, tmp_path, pool)

    git_c_dirs: list[str] = []
    bound_at_git: list = []

    def rec_git(args):
        bound_at_git.append(inst._bound_slot)  # git 수집 시점 정체성.
        if "-C" in args:
            git_c_dirs.append(args[args.index("-C") + 1])
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n"
        if args[:2] == ["log", "--oneline"]:
            return 0, "abc commit\n"
        return 0, ""

    bound_at_pytest: list = []

    def rec_pytest():
        bound_at_pytest.append(inst._bound_slot)  # pytest 수집 시점 정체성.
        return 0, "1 passed\n"

    inst._run_git_fn = rec_git
    inst._run_pytest_fn = rec_pytest
    rc = inst.run(repo="project_manager", with_pytest=True)
    assert rc == 0 and pool.alloc_calls == 1

    new_slot_cwd = str(tmp_path / "work" / "project_manager_2")
    existing_slot_cwd = str(tmp_path / "work" / "project_manager_1")
    # (a) freshness worktree scope 의 `-C` cwd = 새 슬롯·기존 슬롯 아님(observable·codex 지시).
    assert new_slot_cwd in git_c_dirs
    assert existing_slot_cwd not in git_c_dirs
    # (b) 모든 git/pytest 수집 시점의 정체성이 새 슬롯(None 자동해소 아님) — 순서 회귀 가드(non-vacuous).
    assert bound_at_git and set(bound_at_git) == {"work/project_manager_2"}
    assert bound_at_pytest == ["work/project_manager_2"]


# ══════════════════════════════════════════════════════════════════════════
# MF R5 — alloc 앞단 이동이 낳은 lease 누수: fail-fast 수집 실패 시 신규 lease release
# ══════════════════════════════════════════════════════════════════════════

def test_alloc_mode_lease_released_on_collection_failure(tmp_path, monkeypatch):
    """MF R5: alloc 후 fail-fast 수집(pytest 파싱 실패→SystemExit)이면 신규 lease 를 release.

    alloc 을 앞단에 두면(R4) lease 를 먼저 잡는데, 이후 board/pytest/git 파싱 실패로 abort 하면
    그 신규 lease 가 stale leased 로 남아 풀 고갈·'다른 활성 PM' 오표시를 낳는다. 예외/SystemExit
    시 그 lease 를 release 후 re-raise 해야 한다."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    pool = _AllocPool("work/project_manager_2")
    inst = _stub_bootstrap(bs, tmp_path, pool)
    inst._run_pytest_fn = lambda: (1, "boom unparseable")  # _collect_pytest → sys.exit(1)
    with pytest.raises(SystemExit):
        inst.run(repo="project_manager", with_pytest=True)
    assert pool.alloc_calls == 1
    assert "work/project_manager_2" in pool.release_calls  # 신규 lease release
    assert "work/project_manager_2" not in pool.leased      # 장부에서 leased 아님(누수 0)


def test_alloc_mode_lease_kept_on_success(tmp_path, monkeypatch, capsys):
    """MF R5: 정상 완료면 alloc 한 lease 유지(release 안 함 — 세션이 그 슬롯을 쓴다)."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch, load_tool_hf=hf)
    pool = _AllocPool("work/project_manager_2")
    inst = _stub_bootstrap(bs, tmp_path, pool)
    rc = inst.run(repo="project_manager")
    capsys.readouterr()
    assert rc == 0
    assert pool.alloc_calls == 1
    assert pool.release_calls == []                     # release 안 함
    assert "work/project_manager_2" in pool.leased      # leased 유지


# ══════════════════════════════════════════════════════════════════════════
# codex suggestions — 헤딩 키 안전화 + status 따옴표 인식
# ══════════════════════════════════════════════════════════════════════════

def test_render_section_sanitizes_session_key(hf):
    """suggestion1: session 키의 개행/`#` 위조를 안전화 — 가짜 헤딩/섹션 경계 못 만든다."""
    sec = hf.render_dashboard_section("evil\n## injected", session_num=1, wave_summary="w", date=_DATE)
    assert sec.startswith("## evil injected\n")  # 개행 접힘·`#` 마커 제거된 안전 키
    assert sec.count("\n## ") == 0  # 위조 섹션 경계 없음
    assert len(hf.parse_dashboard_sections(sec)) == 1  # 단일 섹션


def test_upsert_render_sanitize_consistent(hf):
    """suggestion1: render 헤딩과 upsert 검색이 같은 정규화 — 같은 raw 키에 일관 overwrite(중복 0)."""
    raw = "slot\nx"
    doc = hf.upsert_dashboard_section(
        "", raw, hf.render_dashboard_section(raw, session_num=1, wave_summary="v1", date=_DATE)
    )
    doc2 = hf.upsert_dashboard_section(
        doc, raw, hf.render_dashboard_section(raw, session_num=2, wave_summary="v2", date=_DATE)
    )
    assert doc2.count("## slot x") == 1  # 같은 raw 키 → 섹션 1개(중복 안 생김)
    assert "v2" in doc2 and "v1" not in doc2  # overwrite


def test_claimed_tickets_quoted_status_flat_layout(hf, tmp_path):
    """suggestion2: flat 레이아웃에서 status 따옴표 값(`status: "claimed"`)도 인식."""
    tickets = tmp_path / "tickets"
    _write_ticket(tickets / "T-0260.md", "T-0260", '"claimed"', "me@x/project_manager_1")
    got = hf._claimed_tickets_for_session("project_manager_1", tickets_dir=tickets)
    assert got == ["T-0260"]
