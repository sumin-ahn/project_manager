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

from pm_engine.infra import pm_paths
import pm_engine.domain.dashboard
import pm_engine.infra.session_store

from pm_engine.app import bootstrap_history
from pm_engine.cli import bootstrap as bootstrap_cli
from pm_engine.cli import bootstrap_report
from pm_engine.domain import dashboard
from pm_engine.infra import python_env

from _pool_seams import bind_pool

import contextlib
import importlib.util
from pathlib import Path

import pytest
from _pytest_summary import pytest_summary

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
    sec = pm_engine.domain.dashboard.render_dashboard_section(
        "project_manager_1", session_num=52, wave_summary="wave A", date=_DATE
    )
    assert sec.startswith("## project_manager_1\n\n")
    assert "- 차수: PM 52차 (2026-07-10)" in sec
    assert "- wave: wave A" in sec
    assert sec.endswith("\n") and not sec.endswith("\n\n")


def test_render_section_claimed_and_next_lines(hf):
    """claimed_tickets·next_plan 이 있으면 각 줄 추가(comma-join)."""
    sec = pm_engine.domain.dashboard.render_dashboard_section(
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
    sec = pm_engine.domain.dashboard.render_dashboard_section(
        "project_manager_1", session_num=1, wave_summary="w", claimed_tickets=[], date=_DATE
    )
    body = sec.split("\n\n", 1)[1]
    assert body.strip().count("\n") == 1  # 차수·wave 2줄
    assert "claimed:" not in sec
    assert "다음:" not in sec


def test_render_section_flattens_multiline_wave(hf):
    """다중행 wave_summary 는 한 줄로 평탄화 — 섹션 경계/추가 줄 위조 차단."""
    sec = pm_engine.domain.dashboard.render_dashboard_section(
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
    sec = pm_engine.domain.dashboard.render_dashboard_section(
        "project_manager_1", session_num=1, wave_summary=long_wave, date=_DATE
    )
    wave_line = [l for l in sec.splitlines() if l.startswith("- wave:")][0]
    value = wave_line[len("- wave: "):]
    assert value.endswith("…")
    assert len(value) <= pm_engine.domain.dashboard.DASHBOARD_LINE_MAX_CHARS


def test_render_section_max_lines_truncate(hf):
    """max_lines 상한 초과 본문줄은 drop(3~5줄 상한 truncate)."""
    sec = pm_engine.domain.dashboard.render_dashboard_section(
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
    assert 3 <= pm_engine.domain.dashboard.DASHBOARD_SECTION_MAX_LINES <= 5


# ══════════════════════════════════════════════════════════════════════════
# upsert_dashboard_section — lazy 생성·overwrite·타 섹션 byte 불변·멱등
# ══════════════════════════════════════════════════════════════════════════

def _sec(hf, session: str, num: int, wave: str) -> str:
    return pm_engine.domain.dashboard.render_dashboard_section(
        session, session_num=num, wave_summary=wave, date=_DATE
    )


def test_upsert_lazy_create_on_empty(hf):
    """빈 대시보드 → 헤더 + 자기 섹션 lazy 생성."""
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "w"))
    assert doc.startswith(pm_engine.domain.dashboard._DASHBOARD_HEADER)
    assert "## project_manager_1" in doc


def test_upsert_overwrite_own_section_replaces_content(hf):
    """자기 섹션 재 upsert → 새 내용으로 교체(옛 wave 사라짐·멱등적 갱신)."""
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "OLD"))
    doc2 = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "project_manager_1", _sec(hf, "project_manager_1", 2, "NEW"))
    assert "NEW" in doc2 and "OLD" not in doc2
    assert doc2.count("## project_manager_1") == 1  # 중복 섹션 안 생김


def test_upsert_idempotent_same_section(hf):
    """같은 section 재 upsert → 결과 동일(멱등)."""
    sec = _sec(hf, "project_manager_1", 1, "w")
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", sec)
    assert pm_engine.domain.dashboard.upsert_dashboard_section(doc, "project_manager_1", sec) == doc


def test_upsert_other_section_byte_invariant(hf):
    """자기 섹션 overwrite 시 **타 슬롯 섹션은 byte 불변** (ADR-0047 핵심·못박기)."""
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "A_old"))
    doc = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "finance_2", _sec(hf, "finance_2", 5, "B_wave"))

    def finance_region(text: str) -> str:
        idx = text.index("## finance_2")
        return text[idx:]

    before = finance_region(doc)
    doc2 = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "project_manager_1", _sec(hf, "project_manager_1", 2, "A_new"))
    after = finance_region(doc2)
    assert before == after  # finance_2 섹션 bytes 완전 동일
    assert "A_new" in doc2 and "A_old" not in doc2


def test_upsert_heading_exact_match_no_prefix_clobber(hf):
    """`project_manager_1` overwrite 가 `project_manager_10` 섹션을 오매치·훼손하지 않는다."""
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "one"))
    doc = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "project_manager_10", _sec(hf, "project_manager_10", 3, "ten"))
    ten_before = doc[doc.index("## project_manager_10"):]
    doc2 = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "project_manager_1", _sec(hf, "project_manager_1", 2, "one_new"))
    assert doc2[doc2.index("## project_manager_10"):] == ten_before
    assert "one_new" in doc2


def test_upsert_append_keeps_existing_sections(hf):
    """신규 슬롯 append 시 기존 섹션들 보존(마지막에 추가)."""
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "a"))
    doc2 = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "finance_2", _sec(hf, "finance_2", 2, "b"))
    keys = [k for k, _ in dashboard.parse_dashboard_sections(doc2)]
    assert keys == ["project_manager_1", "finance_2"]


# ══════════════════════════════════════════════════════════════════════════
# parse_dashboard_sections — 경계·round-trip·preamble
# ══════════════════════════════════════════════════════════════════════════

def test_parse_round_trip_two_sections(hf):
    """upsert 로 만든 2섹션 대시보드를 parse → 키·본문 왕복 일치(write/read 대칭)."""
    doc = pm_engine.domain.dashboard.upsert_dashboard_section("", "project_manager_1", _sec(hf, "project_manager_1", 1, "waveA"))
    doc = pm_engine.domain.dashboard.upsert_dashboard_section(doc, "finance_2", _sec(hf, "finance_2", 2, "waveB"))
    parsed = dict(dashboard.parse_dashboard_sections(doc))
    assert set(parsed) == {"project_manager_1", "finance_2"}
    assert "waveA" in parsed["project_manager_1"]
    assert "waveB" in parsed["finance_2"]
    # 본문은 헤딩 제외·surrounding 빈 줄 제거.
    assert not parsed["project_manager_1"].startswith("\n")
    assert "## project_manager_1" not in parsed["project_manager_1"]


def test_parse_ignores_preamble(hf):
    """첫 `## ` 앞 preamble(`# 헤더`)은 섹션으로 잡지 않는다."""
    doc = "# slot 대시보드\n\n## project_manager_1\n\n- 차수: PM 1차\n"
    parsed = dashboard.parse_dashboard_sections(doc)
    assert [k for k, _ in parsed] == ["project_manager_1"]


def test_parse_empty_returns_empty(hf):
    """섹션 없는 텍스트 → 빈 목록."""
    assert dashboard.parse_dashboard_sections("") == []
    assert dashboard.parse_dashboard_sections("# preamble only\n") == []


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
    'status-subdir(`claimed/`) 레이아웃 — 세션 claimed_by 매칭 티켓 id 반환.'
    home = pm_paths.home_at(tmp_path)
    tickets = tmp_path / 'tickets'
    _write_ticket(tickets / 'claimed' / 'T-0260.md', 'T-0260', 'claimed', 'me@x/project_manager_1')
    _write_ticket(tickets / 'claimed' / 'T-0299.md', 'T-0299', 'claimed', 'you@x/finance_2')
    got = pm_engine.infra.session_store._claimed_tickets_for_session(home, 'project_manager_1', tickets_dir=tickets)
    assert got == ['T-0260']


def test_claimed_tickets_flat_layout_filters_status(hf, tmp_path):
    'flat 레이아웃 — `status: claimed` 만·세션 매칭.'
    home = pm_paths.home_at(tmp_path)
    tickets = tmp_path / 'tickets'
    _write_ticket(tickets / 'T-0260.md', 'T-0260', 'claimed', 'me@x/project_manager_1')
    _write_ticket(tickets / 'T-0100.md', 'T-0100', 'done', 'me@x/project_manager_1')
    got = pm_engine.infra.session_store._claimed_tickets_for_session(home, 'project_manager_1', tickets_dir=tickets)
    assert got == ['T-0260']


def test_claimed_tickets_absent_dir_failsoft(hf, tmp_path):
    '티켓 디렉토리 부재 → 빈 목록(fail-soft·대시보드 claimed 줄 생략).'
    home = pm_paths.home_at(tmp_path)
    assert pm_engine.infra.session_store._claimed_tickets_for_session(home, 'project_manager_1', tickets_dir=tmp_path / 'nope') == []


# ══════════════════════════════════════════════════════════════════════════
# PmHandoff._write_dashboard_section — 파일 write 통합(overwrite·타 섹션 불변)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def hf_repo(tmp_path, monkeypatch):
    'pm_handoff — 모듈 REPO 를 tmp 로 monkeypatch(파일 write 격리).'
    (tmp_path / '.project_manager/board/tickets').mkdir(parents=True)
    home = pm_paths.home_at(tmp_path)
    return home


def _dash_path(home) -> Path:
    return home.root / ".project_manager" / "wiki" / "log" / "dashboard.md"


def test_write_dashboard_section_creates_file(hf_repo):
    """_write_dashboard_section — 대시보드 파일 lazy 생성·차수·wave·claimed 반영."""
    # 세션이 claim 한 티켓 1개 배치(claimed 줄 검증).
    _write_ticket(
        hf_repo.root / ".project_manager" / "board" / "tickets" / "claimed" / "T-0260.md",
        "T-0260", "claimed", "me@x/project_manager_1",
    )
    inst = pm_engine.cli.handoff.PmHandoff(hf_repo)
    out = inst._write_dashboard_section("project_manager_1", 52, "wave 요약 문장", _DATE)
    text = out.read_text(encoding="utf-8")
    assert out == _dash_path(hf_repo)
    assert "## project_manager_1" in text
    assert "- 차수: PM 52차 (2026-07-10)" in text
    assert "- wave: wave 요약 문장" in text
    assert "- claimed: T-0260" in text


def test_write_dashboard_section_overwrites_own_keeps_other(hf_repo):
    """_write_dashboard_section 재실행 → 자기 섹션 overwrite·타 슬롯 섹션 byte 불변(통합)."""
    inst = pm_engine.cli.handoff.PmHandoff(hf_repo)
    inst._write_dashboard_section("project_manager_1", 51, "waveOld", _DATE)
    # 타 슬롯 섹션을 파일에 주입.
    dash = _dash_path(hf_repo)
    injected = pm_engine.domain.dashboard.upsert_dashboard_section(
        dash.read_text(encoding="utf-8"),
        "finance_2",
        pm_engine.domain.dashboard.render_dashboard_section("finance_2", session_num=9, wave_summary="waveFin", date=_DATE),
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

def _make_bs(tmp_path, monkeypatch):
    """실행 패키지와 임시 Home을 사용하는 bootstrap 진입점."""
    return _load("pm_bootstrap")


def _write_dashboard(tmp_path, hf, *sections: tuple[str, int, str]) -> Path:
    doc = ""
    for session, num, wave in sections:
        doc = pm_engine.domain.dashboard.upsert_dashboard_section(
            doc, session, pm_engine.domain.dashboard.render_dashboard_section(session, session_num=num, wave_summary=wave, date=_DATE)
        )
    path = tmp_path / ".project_manager" / "wiki" / "log" / "dashboard.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc, encoding="utf-8")
    return path


def test_collect_dashboard_others_task_mode_excludes_own_task(tmp_path, monkeypatch):
    """task dashboard 키는 task 이름이므로 자기 task를 '다른 활성 PM'으로 다시 표시하지 않는다."""
    hf = _load("pm_handoff")
    bs = _make_bs(tmp_path, monkeypatch)
    _write_dashboard(
        tmp_path, hf,
        ("mytask", 3, "mineTaskWave"),
        ("other-task", 2, "otherTaskWave"),
    )
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path))
    inst._task_name = "mytask"
    result = inst._collect_dashboard_others()
    assert result is not None and result["mode"] == "dashboard"
    assert [o["session"] for o in result["others"]] == ["other-task"]
    assert "mineTaskWave" not in result["others"][0]["body"]


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
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path))
    inst._task_name = "mytask"
    result = inst._collect_dashboard_others()
    assert result["mode"] == "lease"
    assert [o["session"] for o in result["others"]] == ["other-task"]


# ══════════════════════════════════════════════════════════════════════════
# _build_markdown / _build_json — 다른 활성 PM 절 렌더/생략
# ══════════════════════════════════════════════════════════════════════════

_BOARD = {"counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0}, "open_tickets": [], "lint": "clean"}
_GIT = {"branch": "main", "commits": [], "working_tree": "clean"}


def test_build_markdown_renders_dashboard_others(hf, tmp_path):
    """dashboard_others(dashboard 모드) → "### 다른 활성 PM" 절에 타 슬롯 섹션 dump."""
    bs = _load("pm_bootstrap")
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path), run_git_fn=lambda a: (0, ''))
    others = {"mode": "dashboard", "others": [
        {"session": "finance_2", "body": "- 차수: PM 7차 (2026-07-10)\n- wave: finWave"}]}
    md = bootstrap_report._build_markdown(_BOARD, None, _GIT, None, 'ts', None, others, environment=getattr(inst, '_command_environment', None) or python_env._detect_command_environment(pm_paths.home_at(tmp_path)), task_name=getattr(inst, '_task_name', None), state_path=inst._pm_state_display_path())
    assert "### 다른 활성 PM" in md
    assert "**finance_2**" in md
    assert "finWave" in md


def test_build_markdown_omits_section_when_none(hf, tmp_path):
    """dashboard_others None(솔로) → "다른 활성 PM" 절 자체 생략(무노이즈)."""
    bs = _load("pm_bootstrap")
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path), run_git_fn=lambda a: (0, ''))
    md = bootstrap_report._build_markdown(_BOARD, None, _GIT, None, 'ts', None, None, environment=getattr(inst, '_command_environment', None) or python_env._detect_command_environment(pm_paths.home_at(tmp_path)), task_name=getattr(inst, '_task_name', None), state_path=inst._pm_state_display_path())
    assert "### 다른 활성 PM" not in md


def test_build_markdown_lease_fallback_render(hf, tmp_path):
    """dashboard_others(lease 모드) → 세션·슬롯 목록 폴백 렌더."""
    bs = _load("pm_bootstrap")
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path), run_git_fn=lambda a: (0, ''))
    others = {"mode": "lease", "others": [{"session": "finance_2", "slot": "work/finance_2"}]}
    md = bootstrap_report._build_markdown(_BOARD, None, _GIT, None, 'ts', None, others, environment=getattr(inst, '_command_environment', None) or python_env._detect_command_environment(pm_paths.home_at(tmp_path)), task_name=getattr(inst, '_task_name', None), state_path=inst._pm_state_display_path())
    assert "### 다른 활성 PM" in md
    assert "`finance_2`" in md and "`work/finance_2`" in md


def test_build_json_carries_dashboard_others(hf, tmp_path):
    """JSON 렌더도 dashboard_others 를 그대로 실어 보낸다(소비자 일관성)."""
    bs = _load("pm_bootstrap")
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path), run_git_fn=lambda a: (0, ''))
    others = {"mode": "dashboard", "others": [{"session": "finance_2", "body": "- 차수: PM 7차"}]}
    data = bootstrap_report._build_json(_BOARD, None, _GIT, None, 'ts', None, others, environment=getattr(inst, '_command_environment', None) or python_env._detect_command_environment(pm_paths.home_at(tmp_path)), task_name=getattr(inst, '_task_name', None))
    assert data["dashboard_others"] == others


# ══════════════════════════════════════════════════════════════════════════
# 라운드트립 — handoff write → bootstrap read (실 DRY parse·자기 제외)
# ══════════════════════════════════════════════════════════════════════════

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


def test_active_leased_sessions_none_when_ledger_absent(tmp_path, monkeypatch):
    """_active_leased_sessions — 장부 부재 → None(판정불가)·정상 read → leased 세션 집합."""
    bs = _make_bs(tmp_path, monkeypatch)
    inst = bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path))
    assert bootstrap_history._active_leased_sessions(pm_paths.home_at(tmp_path)) is None
    _write_leases(tmp_path, ("project_manager_1", "leased"), ("finance_2", "idle"))
    assert bootstrap_history._active_leased_sessions(pm_paths.home_at(tmp_path)) == {"project_manager_1"}  # idle 제외


# ══════════════════════════════════════════════════════════════════════════
# MF-2 (codex) — 대시보드 read-modify-write 파일락 직렬화(lost update 차단)
# ══════════════════════════════════════════════════════════════════════════

def test_dashboard_lock_creates_lock_file(hf_repo):
    """_dashboard_lock 진입 → `.local/dashboard.lock` 생성·CM yield·정상 해제."""
    with pm_engine.infra.session_store._dashboard_lock(hf_repo):
        assert pm_paths._dashboard_lock_file(hf_repo).exists()
    # 재진입(순차)도 무해 — 락 재획득·해제.
    with pm_engine.infra.session_store._dashboard_lock(hf_repo):
        pass


def test_write_dashboard_section_goes_through_lock(hf_repo, monkeypatch):
    """MF-2: _write_dashboard_section 의 read-modify-write 가 `_dashboard_lock` 안에서 일어난다."""
    events: list[str] = []

    @contextlib.contextmanager
    def spy_lock(_home):
        events.append("enter")
        yield
        events.append("exit")

    monkeypatch.setattr(pm_engine.infra.session_store, "_dashboard_lock", spy_lock)
    inst = pm_engine.cli.handoff.PmHandoff(hf_repo)
    inst._write_dashboard_section("project_manager_1", 1, "w", _DATE)
    assert events == ["enter", "exit"]  # write 가 락 구간 안에서 완결


def test_write_dashboard_reads_fresh_under_lock_no_lost_update(hf_repo):
    """MF-2: 두 다른 슬롯이 순차 write 해도 서로 안 덮는다 — 각 write 가 락 안에서 최신 파일 재-read.

    lost update 재현 시나리오: A 가 자기 섹션을 쓴 뒤, B(다른 슬롯)가 자기 섹션을 쓰고, A 가
    다시 자기 섹션을 갱신 — A 의 재-write 가 stale 캐시가 아니라 최신 파일을 읽어 B 섹션을
    보존해야 한다(직렬화·락이 이 read↔write 원자성을 프로세스 간에도 보장)."""
    inst = pm_engine.cli.handoff.PmHandoff(hf_repo)
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
    'claimed_by/id 가 YAML 따옴표 값이어도 세션 매칭·id 추출(따옴표 strip).'
    home = pm_paths.home_at(tmp_path)
    tickets = tmp_path / 'tickets'
    _write_ticket(tickets / 'claimed' / 'T-0260.md', '"T-0260"', 'claimed', '"me@x/project_manager_1"')
    got = pm_engine.infra.session_store._claimed_tickets_for_session(home, 'project_manager_1', tickets_dir=tickets)
    assert got == ['T-0260']


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

    def release(self, slot, **_):
        self.release_calls.append(slot)
        self.leased.discard(slot)
        return _AllocLease(slot, "project_manager")

    def slot_path(self, slot):
        return Path("/tmp/mp") / slot

    def current_branch(self, slot, *, git_runner=None):
        return "feat-branch"

    def slot_git_status(self, slot, *, git_runner=None):
        # `_slot_era_info` 가 슬롯 바인딩마다 부르는 대역 — 이 스위트는 대시보드 수집만
        # 보므로 parent 없음(None)으로 고정한다(신 계약 모양만 맞춤).
        return {"slot": slot, "branch": "feat-branch", "head": None, "parent": None,
                "children": [], "stashes": 0, "submodules": [], "dirty": False}


def _stub_bootstrap(bs, tmp_path, pool, *, log_text: str='# log\n', monkeypatch):
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

    bind_pool(monkeypatch, pool)
    return bootstrap_cli.PmBootstrap(pm_paths.home_at(tmp_path), run_board_fn=fake_board, run_pytest_fn=lambda: (0, ''), run_git_fn=fake_git, log_file=log_file, areas_file=areas, pm_state_file=pm_state)


def test_write_dashboard_section_atomic_no_tmp_left(hf_repo):
    """suggestion: 대시보드 write 는 tmp→os.replace 원자 교체 — tmp 잔여 없음·내용 온전."""
    inst = pm_engine.cli.handoff.PmHandoff(hf_repo)
    out = inst._write_dashboard_section("project_manager_1", 5, "atomic wave", _DATE)
    # 원자 교체 완료 후 .tmp 사이드카가 남지 않는다.
    assert not out.with_suffix(out.suffix + ".tmp").exists()
    text = out.read_text(encoding="utf-8")
    assert "## project_manager_1" in text and "atomic wave" in text


# ══════════════════════════════════════════════════════════════════════════
# MF R4 — alloc 모드: cwd-의존 수집(freshness/git/pytest)도 새 슬롯 cwd 를 본다
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# MF R5 — alloc 앞단 이동이 낳은 lease 누수: fail-fast 수집 실패 시 신규 lease release
# ══════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════
# codex suggestions — 헤딩 키 안전화 + status 따옴표 인식
# ══════════════════════════════════════════════════════════════════════════

def test_render_section_sanitizes_session_key(hf):
    """suggestion1: session 키의 개행/`#` 위조를 안전화 — 가짜 헤딩/섹션 경계 못 만든다."""
    sec = pm_engine.domain.dashboard.render_dashboard_section("evil\n## injected", session_num=1, wave_summary="w", date=_DATE)
    assert sec.startswith("## evil injected\n")  # 개행 접힘·`#` 마커 제거된 안전 키
    assert sec.count("\n## ") == 0  # 위조 섹션 경계 없음
    assert len(dashboard.parse_dashboard_sections(sec)) == 1  # 단일 섹션


def test_upsert_render_sanitize_consistent(hf):
    """suggestion1: render 헤딩과 upsert 검색이 같은 정규화 — 같은 raw 키에 일관 overwrite(중복 0)."""
    raw = "slot\nx"
    doc = pm_engine.domain.dashboard.upsert_dashboard_section(
        "", raw, pm_engine.domain.dashboard.render_dashboard_section(raw, session_num=1, wave_summary="v1", date=_DATE)
    )
    doc2 = pm_engine.domain.dashboard.upsert_dashboard_section(
        doc, raw, pm_engine.domain.dashboard.render_dashboard_section(raw, session_num=2, wave_summary="v2", date=_DATE)
    )
    assert doc2.count("## slot x") == 1  # 같은 raw 키 → 섹션 1개(중복 안 생김)
    assert "v2" in doc2 and "v1" not in doc2  # overwrite


def test_claimed_tickets_quoted_status_flat_layout(hf, tmp_path):
    'suggestion2: flat 레이아웃에서 status 따옴표 값(`status: "claimed"`)도 인식.'
    home = pm_paths.home_at(tmp_path)
    tickets = tmp_path / 'tickets'
    _write_ticket(tickets / 'T-0260.md', 'T-0260', '"claimed"', 'me@x/project_manager_1')
    got = pm_engine.infra.session_store._claimed_tickets_for_session(home, 'project_manager_1', tickets_dir=tickets)
    assert got == ['T-0260']
