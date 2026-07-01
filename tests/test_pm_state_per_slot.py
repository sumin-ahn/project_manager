"""per-slot pm_state 경로 해소 + graceful 마이그레이션 + 솔로 폴백 (T-0166·ADR-0033 §3.1).

pm_state 를 *슬롯별*로 분리한다 — multi-PM 연속성(여러 PM 슬롯이 한 clone 공유 보드 위에서
각자 핸드오프 상태 유지·spike §1.3·§3.1). 경로 = `.project_manager/.local/slots/<slot>/pm_state.md`
(gitignored·per-slot). slot 키 = lease 장부 슬롯과 동형(`<repo>_<N>`·`_auto_slot`·T-0123 재사용).

검증 세 축 (pm_handoff·pm_bootstrap):
  - **per-slot read/write**: 슬롯 해소(`<repo>_<N>`) → `.local/slots/<slot>/pm_state.md`.
  - **솔로 폴백**: 슬롯 미해소(`_auto_slot` None) → legacy `wiki/pm_state.md`(현행 무변경).
  - **graceful 마이그레이션**: legacy 존재 + slot 경로 부재 → 첫 접근 시 slot 경로로 이동.

**hermetic 필수**: 각 도구의 모듈-레벨 `REPO`(import 시점 굳음)를 tmp 로 monkeypatch 한 fresh
모듈 인스턴스를 매 테스트마다 로드한다(test_board_root_external_tools 동류). 경로 해소가 *함수*
(호출 시점 REPO 추종)라 monkeypatch 된 tmp REPO 를 추종한다.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TOOLS = REPO / ".project_manager" / "tools"


def _load(name: str):
    """도구 모듈을 importlib 경로 로드 (test_board_root 동일 규약)."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_areas(path: Path, repo: str = "project_manager") -> None:
    """areas.md 레지스트리(repo 1개)를 쓴다 — _auto_slot 단일 self-host 전제."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        f"| {repo} | PM | g | pytest | me |\n",
        encoding="utf-8")


def _write_leases(path: Path, repo: str = "project_manager", n: int = 1) -> None:
    """worktree-leases.json(슬롯 1개)를 쓴다 — _auto_slot 단일 슬롯 전제."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"leases": [{"repo": "%s", "slot": "work/%s_%d"}]}' % (repo, repo, n),
        encoding="utf-8")


def _make_single_self_host(root: Path, repo: str = "project_manager", n: int = 1) -> None:
    """단일 self-host(repo 1개 + 슬롯 1개) 형상을 tmp REPO 에 만든다 → _auto_slot=(repo, n)."""
    _write_areas(root / ".project_manager" / "areas.md", repo)
    _write_leases(root / ".project_manager" / ".local" / "worktree-leases.json", repo, n)


# ══════════════════════════════════════════════════════════════════════════
# pm_handoff — _resolve_state_slot / _pm_state_path (read/write 주체)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def hf(tmp_path, monkeypatch):
    mod = _load("pm_handoff")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def _legacy(hf) -> Path:
    return hf._tmp / ".project_manager" / "wiki" / "pm_state.md"


def _slot_path(hf, slot: str = "project_manager_1") -> Path:
    return hf._tmp / ".project_manager" / ".local" / "slots" / slot / "pm_state.md"


# ── _resolve_state_slot: 명시 슬롯·자동해소·솔로 None ─────────────────────────

def test_resolve_state_slot_explicit_strips_work_prefix(hf):
    """명시 worktree_slot(`work/<repo>_<N>`) → 슬롯 키(`<repo>_<N>`·leading work/ 제거)."""
    assert hf._resolve_state_slot("work/project_manager_1") == "project_manager_1"


def test_resolve_state_slot_explicit_bare_key_unchanged(hf):
    """명시 슬롯이 이미 `<repo>_<N>`(work/ 없음) 면 그대로."""
    assert hf._resolve_state_slot("project_manager_2") == "project_manager_2"


def test_resolve_state_slot_auto_single_self_host(hf):
    """worktree_slot 미지정 + 단일 self-host → _auto_slot 으로 `<repo>_<N>` 자동해소."""
    _make_single_self_host(hf._tmp)
    assert hf._resolve_state_slot() == "project_manager_1"


def test_resolve_state_slot_solo_returns_none(hf):
    """등록 repo 0개(솔로/미분리·areas 부재) → None (legacy 폴백 신호)."""
    assert hf._resolve_state_slot() is None


def test_resolve_state_slot_ambiguous_multi_returns_none(hf):
    """등록 repo ≥2 (진짜 모호) → None — `_resolve_session_slot` 의 SlotResolutionError 를
    display/preview fail-soft 로 catch (T-0178). 실제 write 는 run() 가드가 fail-loud 로 막음."""
    areas = hf._tmp / ".project_manager" / "areas.md"
    areas.parent.mkdir(parents=True, exist_ok=True)
    areas.write_text(
        "| repo | prefix | git | test_cmd | owner |\n"
        "|---|---|---|---|---|\n"
        "| repo_a | A | g | pytest | me |\n"
        "| repo_b | B | g | pytest | me |\n",
        encoding="utf-8")
    assert hf._resolve_state_slot() is None


def _write_leases_multi(path, repo: str, ns: list[int]) -> None:
    """worktree-leases.json — 한 repo 의 여러 슬롯(`work/<repo>_<N>`)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"repo": repo, "slot": f"work/{repo}_{n}"} for n in ns]
    path.write_text(json.dumps({"leases": leases}), encoding="utf-8")


def test_resolve_state_slot_default_1_when_slot1_present(hf):
    """repo 1개 + 슬롯 `{1,2}` → `<repo>_1`(default-1·T-0178 should-fix).

    이 갭의 핵심: 이전 `_auto_slot`(exactly-1)은 `{1,2}`→None→없는 legacy 로 새서 slot1
    continuity 를 끊었다. `_resolve_session_slot`(default-1) 경유로 slot1 로 라우팅된다."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_multi(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [1, 2])
    assert hf._resolve_state_slot() == "project_manager_1"


def test_resolve_state_slot_sole_non1_slot(hf):
    """repo 1개 + 슬롯 `{3}`(단독·1 아님) → `<repo>_3` (단독 규칙·현행 `_3`-only 보존)."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_multi(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [3])
    assert hf._resolve_state_slot() == "project_manager_3"


def test_resolve_state_slot_truly_ambiguous_slot1_absent_returns_none(hf):
    """repo 1개 + 슬롯 `{2,3}`(1 부재·비단독·진짜 모호) → None (SlotResolutionError catch·
    display fail-soft). 실제 write 는 run() 가드가 fail-loud 로 막음."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_multi(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [2, 3])
    assert hf._resolve_state_slot() is None


def _write_leases_states(path, repo: str, slots: list[tuple[int, str]]) -> None:
    """worktree-leases.json — 한 repo 의 (슬롯N, state) 목록 (idle 회귀용)."""
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = [{"repo": repo, "slot": f"work/{repo}_{n}", "state": st} for n, st in slots]
    path.write_text(json.dumps({"leases": leases}), encoding="utf-8")


def test_resolve_state_slot_idle_slot1_routes_continuity_to_leased_slot2(hf):
    """`{1:idle, 2:leased}` continuity → `project_manager_2` (idle 슬롯1 아님·codex must-fix).

    continuity 도 idle 필터된 활성 슬롯으로 라우팅 — 죽은 슬롯1 의 per-slot 으로 새지 않는다."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_states(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [(1, "idle"), (2, "leased")])
    assert hf._resolve_state_slot() == "project_manager_2"


def test_pm_state_path_idle_slot1_routes_to_slot2_per_slot(hf):
    """`{1:idle, 2:leased}` pm_state → `slots/project_manager_2/pm_state.md` (활성 슬롯2 per-slot)."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_states(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [(1, "idle"), (2, "leased")])
    assert hf._pm_state_path() == _slot_path(hf, "project_manager_2")


# ── _pm_state_path: per-slot / 솔로 폴백 / graceful 마이그레이션 ──────────────

def test_pm_state_path_solo_is_legacy(hf):
    """슬롯 미해소(솔로) → legacy `wiki/pm_state.md` (현행 무변경)."""
    assert hf._pm_state_path() == _legacy(hf)


def test_pm_state_path_slot_resolves_to_local_slots(hf):
    """단일 self-host → `.local/slots/<slot>/pm_state.md` (per-slot·legacy 부재)."""
    _make_single_self_host(hf._tmp)
    assert hf._pm_state_path() == _slot_path(hf)


def test_pm_state_path_explicit_slot_to_local_slots(hf):
    """명시 worktree_slot → 그 슬롯의 per-slot 경로 (auto 판정 불요)."""
    assert hf._pm_state_path("work/project_manager_3") == _slot_path(hf, "project_manager_3")


def test_pm_state_path_existing_slot_path_used_as_is(hf):
    """slot 경로가 이미 있으면 그대로 반환 (마이그레이션 없음·이미 per-slot)."""
    _make_single_self_host(hf._tmp)
    sp = _slot_path(hf)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("기존 슬롯 상태", encoding="utf-8")
    assert hf._pm_state_path() == sp
    assert sp.read_text(encoding="utf-8") == "기존 슬롯 상태"  # 불변.


def test_pm_state_path_graceful_migration_moves_legacy(hf):
    """legacy 존재 + slot 경로 부재 → 첫 접근 시 legacy → slot 경로로 *이동*(graceful)."""
    _make_single_self_host(hf._tmp)
    legacy = _legacy(hf)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("이전 단일 pm_state 내용", encoding="utf-8")

    resolved = hf._pm_state_path()

    sp = _slot_path(hf)
    assert resolved == sp
    assert sp.exists() and sp.read_text(encoding="utf-8") == "이전 단일 pm_state 내용"
    assert not legacy.exists(), "graceful 마이그레이션은 legacy 를 *이동*(복사 아님) — 원본 제거."


def test_pm_state_path_slot_resolved_but_both_absent_returns_slot_path(hf):
    """슬롯 해소 + slot 경로·legacy 둘 다 부재 → slot 경로 반환(쓰기 시 생성·생성 안 함)."""
    _make_single_self_host(hf._tmp)
    sp = _slot_path(hf)
    assert hf._pm_state_path() == sp
    assert not sp.exists(), "반환만 — 파일을 새로 만들지 않는다(fail-soft)."


def test_pm_state_path_default_1_routes_to_slot1_not_legacy(hf):
    """`{1,2}` continuity → `slots/<repo>_1/pm_state.md` (legacy 아님·T-0178 should-fix 핵심).

    이 갭의 단언: default-1 셋업(`{1,2}`)에서 pm_state 가 *없는* legacy `wiki/pm_state.md`
    로 새지 않고 slot1 per-slot 경로로 해소됨을 박제 — run() 가드의 "slot 1" 판정과 정합."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_multi(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [1, 2])
    resolved = hf._pm_state_path()
    assert resolved == _slot_path(hf, "project_manager_1")
    assert resolved != _legacy(hf), "continuity 가 없는 legacy 로 새면 안 됨(이 갭의 회귀)."


def test_pm_state_path_truly_ambiguous_falls_back_to_legacy_display(hf):
    """`{2,3}`(진짜 모호) → display/preview fail-soft 로 legacy 표기 (write 는 run() 가드가 막음)."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_multi(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [2, 3])
    # SlotResolutionError catch → None → legacy(display fail-soft·크래시 안 함).
    assert hf._pm_state_path() == _legacy(hf)


def test_pm_state_path_solo_does_not_touch_legacy(hf):
    """솔로(슬롯 미해소) + legacy 존재 → legacy 그대로(마이그레이션 안 함·무변경)."""
    legacy = _legacy(hf)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("솔로 상태", encoding="utf-8")
    assert hf._pm_state_path() == legacy
    assert legacy.exists() and legacy.read_text(encoding="utf-8") == "솔로 상태"


# ── incidental(회귀 cwd) fail-soft 무변경 재확인 (T-0178 should-fix·continuity 와 비대칭) ──
# continuity(`_resolve_state_slot`)는 default-1(`{1,2}`→slot1)로 라우팅하지만, incidental
# `_regression_cwd` 는 여전히 `_auto_slot`(exactly-1)을 쓴다 — `{1,2}`→None→REPO 폴백 유지.
# 회귀 cwd 는 *슬롯 무관*(어느 슬롯이든 같은 worktree 트리)이라 REPO 폴백으로 충분하므로,
# 모호함으로 깨뜨리지 않는다(solo 도그푸딩 보존·최우선). 이 비대칭이 의도적임을 박제한다.

def test_regression_cwd_default_1_setup_falls_back_to_repo(hf):
    """`{1,2}` 셋업에서도 `_regression_cwd` 는 REPO 폴백(incidental fail-soft 불변).

    continuity 는 slot1 로 라우팅되지만 회귀 cwd 는 `_auto_slot`(exactly-1·미변경)→None→
    REPO — 모호함으로 회귀 러너를 깨지 않는다(should-fix 가 건드린 건 continuity 한정)."""
    _write_areas(hf._tmp / ".project_manager" / "areas.md")
    _write_leases_multi(
        hf._tmp / ".project_manager" / ".local" / "worktree-leases.json",
        "project_manager", [1, 2])
    # _regression_cwd 는 areas/leases 인자를 노출하므로 hermetic 호출.
    areas = hf._tmp / ".project_manager" / "areas.md"
    leases = hf._tmp / ".project_manager" / ".local" / "worktree-leases.json"
    assert hf._regression_cwd(None, areas, leases) == str(hf._tmp)


# ══════════════════════════════════════════════════════════════════════════
# pm_handoff — PmHandoff.__init__ per-slot 배선 (명시 주입 vs 프로덕션)
# ══════════════════════════════════════════════════════════════════════════

def test_handoff_init_explicit_pm_state_honored(hf, tmp_path):
    """명시 pm_state_file 주입(테스트/override) → 그 경로 고정·explicit 플래그 True."""
    explicit = tmp_path / "explicit" / "pm_state.md"
    inst = hf.PmHandoff(pm_state_file=explicit)
    assert inst._pm_state_file == explicit
    assert inst._pm_state_file_explicit is True


def test_handoff_init_default_is_legacy_until_run(hf):
    """미지정(프로덕션·None) → explicit 플래그 False·default 는 legacy(run 진입부서 재해소)."""
    inst = hf.PmHandoff()
    assert inst._pm_state_file_explicit is False
    assert inst._pm_state_file == hf._legacy_pm_state_file()


# ══════════════════════════════════════════════════════════════════════════
# pm_bootstrap — _pm_state_display_path (첫-turn 안내 경로)
# ══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def bs(tmp_path, monkeypatch):
    mod = _load("pm_bootstrap")
    monkeypatch.setattr(mod, "REPO", tmp_path)
    mod._tmp = tmp_path
    return mod


def test_bootstrap_display_path_solo_is_legacy(bs):
    """슬롯 미해소(솔로) → 안내 경로 = `pm_state.md`(현행 짧은 표기·무변경)."""
    assert bs._pm_state_display_path() == "pm_state.md"


def test_bootstrap_display_path_single_self_host_is_per_slot(bs):
    """단일 self-host → 안내 경로 = `.project_manager/.local/slots/<slot>/pm_state.md`."""
    _make_single_self_host(bs._tmp)
    assert bs._pm_state_display_path() == \
        ".project_manager/.local/slots/project_manager_1/pm_state.md"


def test_bootstrap_display_path_explicit_slot(bs):
    """명시 슬롯 tuple → 그 슬롯의 per-slot 경로 표기."""
    assert bs._pm_state_display_path(("project_manager", 2)) == \
        ".project_manager/.local/slots/project_manager_2/pm_state.md"


def test_bootstrap_instance_display_path_uses_bound_slot(bs):
    """PmBootstrap 인스턴스 — _bound_slot(`work/<repo>_<N>`) 파싱 → per-slot 안내 경로."""
    inst = bs.PmBootstrap()
    inst._bound_slot = "work/project_manager_5"
    assert inst._pm_state_display_path() == \
        ".project_manager/.local/slots/project_manager_5/pm_state.md"


def test_bootstrap_instance_display_path_solo_legacy(bs):
    """인스턴스 솔로(_bound_slot None·자동해소 None) → legacy 표기(무변경)."""
    inst = bs.PmBootstrap(areas_file=bs._tmp / ".project_manager" / "nonexistent-areas.md")
    inst._bound_slot = None
    assert inst._pm_state_display_path() == "pm_state.md"


def test_bootstrap_markdown_first_turn_shows_per_slot_path(bs, monkeypatch):
    """_build_markdown 첫-turn 안내가 per-slot 경로를 노출한다(단일 self-host)."""
    _make_single_self_host(bs._tmp)
    inst = bs.PmBootstrap(areas_file=bs._tmp / ".project_manager" / "areas.md")
    board = {"counts": {"done": 1, "open": 2, "claimed": 0, "blocked": 0},
             "open_tickets": ["T-0001"], "lint": "clean"}
    git = {"branch": "main", "commits": [("abc", "msg")], "no_commits": False,
           "working_tree": "clean"}
    md = inst._build_markdown(board, None, git, None, "2026-06-27 00:00 KST")
    assert ".project_manager/.local/slots/project_manager_1/pm_state.md" in md
    assert "세션 식별" in md


def test_bootstrap_markdown_first_turn_solo_legacy_path(bs):
    """솔로(슬롯 미해소) → 첫-turn 안내가 현행 `pm_state.md` 표기(무변경)."""
    inst = bs.PmBootstrap(areas_file=bs._tmp / ".project_manager" / "areas.md")
    inst._bound_slot = None
    board = {"counts": {"done": 0, "open": 0, "claimed": 0, "blocked": 0},
             "open_tickets": [], "lint": "clean"}
    git = {"branch": "main", "commits": [], "no_commits": True, "working_tree": "clean"}
    md = inst._build_markdown(board, None, git, None, "2026-06-27 00:00 KST")
    assert "pm_state.md \"세션 식별\"" in md
    assert ".local/slots/" not in md


# ══════════════════════════════════════════════════════════════════════════
# pm_handoff — run() end-to-end per-slot read/write (프로덕션 경로·명시 주입 없음)
# ══════════════════════════════════════════════════════════════════════════

# 실 pm_state.md "세션 식별" 절 최소 형식(앵커·entry·포인터·다음 헤더) — sliding window 전제.
_SESSION_SECTION = (
    "# PM State\n\n"
    "## 세션 식별 (현재까지 사용된 이름)\n\n"
    "최근 N 차 (sliding window, 기본 3 차):\n"
    "  - **1차** (2026-06-11 · w): w.\n"
    "  - **2차** (2026-06-12 · w): w.\n"
    "  - **3차** (2026-06-13 · w): w.\n"
    "  - 이전 차 (PM 1차~1차) = `log/current.md` handoff entry 단일 진실.\n"
    "\n## 진행 중인 의사결정\n\n표.\n"
)


def _make_handoff_production(
    hf, *, with_legacy: bool = False, slot_seeded: bool = False,
    run_pytest_fn=None, run_shipping_test_fn=None,
):
    """명시 pm_state_file 주입 *없는*(프로덕션) PmHandoff — run() 이 per-slot 해소.

    단일 self-host 형상(areas+leases)을 monkeypatch 된 tmp REPO 에 깐다. log/playbook 은
    tmp(REPO 추종). subprocess 는 결정론 DI. with_legacy=True 면 legacy pm_state 를 미리
    seed(마이그레이션 케이스), slot_seeded=True 면 slot 경로를 미리 seed(이미 per-slot).
    run_pytest_fn/run_shipping_test_fn 주입 시 게이트 red 케이스(중단 시 무접촉 가드)에 쓴다.
    """
    tmp = hf._tmp
    _make_single_self_host(tmp)
    # log/playbook 은 REPO 하위 실 경로(모듈 LOG_FILE/PM_PLAYBOOK_FILE 이 REPO 추종 아님 →
    # 명시 주입). pm_state_file 은 *주입 안 함* → run() 이 per-slot 해소.
    log_file = tmp / "log.md"
    log_file.write_text("# log\n", encoding="utf-8")
    playbook_file = tmp / "playbook.md"
    playbook_file.write_text("# pm_playbook (no anchor)\n", encoding="utf-8")

    if with_legacy:
        legacy = tmp / ".project_manager" / "wiki" / "pm_state.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(_SESSION_SECTION, encoding="utf-8")
    if slot_seeded:
        sp = tmp / ".project_manager" / ".local" / "slots" / "project_manager_1" / "pm_state.md"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(_SESSION_SECTION, encoding="utf-8")

    inst = hf.PmHandoff(
        run_pytest_fn=run_pytest_fn or
            (lambda: (_ for _ in ()).throw(AssertionError("skip_pytest 인데 호출"))),
        run_git_fn=lambda args: (0, ""),
        run_shipping_test_fn=run_shipping_test_fn,
        log_file=log_file,
        pm_playbook_file=playbook_file,
        # pm_state_file 미주입 → per-slot 해소(프로덕션 경로).
    )
    return inst


def test_run_writes_to_per_slot_path_when_slot_resolved(hf):
    """run() 프로덕션 경로 — 단일 self-host + slot seeded → per-slot 경로에 sliding window write."""
    inst = _make_handoff_production(hf, slot_seeded=True)
    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=True)
    assert rc == 0
    sp = _slot_path(hf)
    assert sp.exists()
    assert "**4차**" in sp.read_text(encoding="utf-8"), "신규 세션 entry 가 per-slot pm_state 에 써짐."
    # legacy 는 건드리지 않음(slot 경로가 권위).
    assert not _legacy(hf).exists()


def test_run_migrates_legacy_then_writes_per_slot(hf):
    """run() 프로덕션 경로 — legacy 존재 + slot 부재 → 마이그레이션 후 per-slot 에 write."""
    inst = _make_handoff_production(hf, with_legacy=True)
    legacy = _legacy(hf)
    assert legacy.exists()  # 전제: legacy 존재.

    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=True)
    assert rc == 0

    sp = _slot_path(hf)
    assert sp.exists() and "**4차**" in sp.read_text(encoding="utf-8")
    assert not legacy.exists(), "legacy → slot 경로로 *이동*(마이그레이션) 후 원본 제거."


def test_run_solo_writes_legacy_unchanged(hf):
    """run() 솔로(슬롯 미해소·areas/leases 부재) → legacy `wiki/pm_state.md` write(현행 무변경)."""
    tmp = hf._tmp
    # 단일 self-host 형상을 *깔지 않음* → _auto_slot None → 솔로.
    log_file = tmp / "log.md"; log_file.write_text("# log\n", encoding="utf-8")
    playbook_file = tmp / "playbook.md"; playbook_file.write_text("# pb\n", encoding="utf-8")
    legacy = _legacy(hf)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(_SESSION_SECTION, encoding="utf-8")

    inst = hf.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("skip")),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file, pm_playbook_file=playbook_file,
    )
    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=True)
    assert rc == 0
    # 솔로 → legacy 에 써지고 slot 경로는 안 생긴다.
    assert "**4차**" in legacy.read_text(encoding="utf-8")
    assert not _slot_path(hf).exists()


def test_run_explicit_pm_state_not_redirected_to_slot(hf, tmp_path):
    """명시 pm_state_file 주입(테스트) → per-slot 재해소 안 함(명시 경로 고정·hermetic 보존)."""
    _make_single_self_host(hf._tmp)  # 슬롯 해소 가능한 형상이어도
    explicit = tmp_path / "explicit_state.md"
    explicit.write_text(_SESSION_SECTION, encoding="utf-8")
    log_file = tmp_path / "log.md"; log_file.write_text("# log\n", encoding="utf-8")
    playbook_file = tmp_path / "pb.md"; playbook_file.write_text("# pb\n", encoding="utf-8")

    inst = hf.PmHandoff(
        run_pytest_fn=lambda: (_ for _ in ()).throw(AssertionError("skip")),
        run_git_fn=lambda args: (0, ""),
        log_file=log_file, pm_playbook_file=playbook_file,
        pm_state_file=explicit,  # 명시 주입.
    )
    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=True)
    assert rc == 0
    # 명시 경로에 써지고 slot 경로로 redirect 안 됨.
    assert "**4차**" in explicit.read_text(encoding="utf-8")
    assert not _slot_path(hf).exists()


def test_pm_state_path_dry_run_no_migration(hf):
    """migrate=False(dry-run) → legacy 존재 + slot 부재면 이동 없이 legacy 반환(부작용 0)."""
    _make_single_self_host(hf._tmp)
    legacy = _legacy(hf)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("미리보기 내용", encoding="utf-8")

    resolved = hf._pm_state_path(migrate=False)

    assert resolved == legacy, "dry-run 은 현 읽기 위치(legacy)를 반환."
    assert legacy.exists(), "dry-run 은 파일 이동 안 함 — legacy 보존."
    assert not _slot_path(hf).exists(), "dry-run 은 slot 경로를 만들지 않음."


def test_run_dry_run_does_not_migrate_legacy(hf):
    """run(dry_run=True) 프로덕션 경로 — legacy 존재 시 마이그레이션(파일 이동) 안 함."""
    inst = _make_handoff_production(hf, with_legacy=True)
    legacy = _legacy(hf)
    assert legacy.exists()

    rc = inst.run(session_num=4, wave_summary="신규", dry_run=True, skip_pytest=True)
    assert rc == 0
    # dry-run — legacy 그대로, slot 경로 미생성.
    assert legacy.exists(), "dry-run 은 legacy 를 옮기지 않는다."
    assert not _slot_path(hf).exists()


# ══════════════════════════════════════════════════════════════════════════
# 트랜잭션 계약 — 중단 게이트(회귀·출하) red → legacy 미이동 (codex must-fix)
# ══════════════════════════════════════════════════════════════════════════
#
# 마이그레이션(legacy→slot replace())이 *모든 중단 게이트 통과 후*·pm_state 첫 접촉 직전에만
# 일어나야 한다 — 회귀 red·shipping red 로 중단되면 pm_state 무접촉(legacy 그대로·slot 미생성).

_PYTEST_RED = "1 passed, 1 failed in 0.1s"  # is_pytest_green → False(중단).
_PYTEST_GREEN = "2 passed in 0.1s"          # is_pytest_green → True(통과).


def test_run_pytest_red_does_not_migrate_legacy(hf):
    """회귀 red 로 run() 중단(return 1) → legacy 미이동·slot 경로 미생성(무접촉 계약)."""
    inst = _make_handoff_production(
        hf, with_legacy=True,
        run_pytest_fn=lambda: (1, _PYTEST_RED),  # red → [1/7] 에서 중단.
    )
    legacy = _legacy(hf)
    assert legacy.exists()

    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=False)
    assert rc == 1, "회귀 red → 핸드오프 중단."
    # 중단 시 pm_state 무접촉 — legacy 그대로, slot 경로 미생성.
    assert legacy.exists(), "회귀 red 중단인데 legacy 가 이동됐다(트랜잭션 계약 위반)."
    assert legacy.read_text(encoding="utf-8") == _SESSION_SECTION, "legacy 내용 불변."
    assert not _slot_path(hf).exists(), "회귀 red 중단인데 slot 경로가 생성됐다."


def test_run_shipping_red_does_not_migrate_legacy(hf):
    """출하 게이트 red 로 run() 중단 → legacy 미이동·slot 경로 미생성(무접촉 계약).

    회귀는 green(통과) 후 출하 step 에서 red(rc∉{0,5}) → 중단. shipping_test_override=True 로
    강제 발동시키고 run_shipping_test_fn red stub 으로 출하 게이트만 red 를 만든다.
    """
    inst = _make_handoff_production(
        hf, with_legacy=True,
        run_pytest_fn=lambda: (0, _PYTEST_GREEN),       # 회귀 green(통과).
        run_shipping_test_fn=lambda worktree: (1, "shipping red"),  # 출하 red(rc=1).
    )
    legacy = _legacy(hf)
    assert legacy.exists()

    rc = inst.run(
        session_num=4, wave_summary="신규", dry_run=False, skip_pytest=False,
        shipping_test_override=True,  # 출하 테스트 강제 발동.
    )
    assert rc == 1, "출하 게이트 red → 핸드오프 중단."
    # 회귀는 green 이지만 출하 red 로 중단 — pm_state 첫 접촉 전이라 legacy 무접촉.
    assert legacy.exists(), "출하 red 중단인데 legacy 가 이동됐다(트랜잭션 계약 위반)."
    assert legacy.read_text(encoding="utf-8") == _SESSION_SECTION, "legacy 내용 불변."
    assert not _slot_path(hf).exists(), "출하 red 중단인데 slot 경로가 생성됐다."


def test_run_gates_green_migrates_then_writes_per_slot(hf):
    """게이트(회귀·출하) green → 통과 후 legacy→slot 이동 + slot 에 sliding window 기록.

    test_run_migrates_legacy_then_writes_per_slot 의 *게이트 실행* 변형 — skip_pytest=False·
    회귀 green·출하 skip(override False·미push diff 없음 stub)로 게이트를 실제 통과시킨다.
    """
    inst = _make_handoff_production(
        hf, with_legacy=True,
        run_pytest_fn=lambda: (0, _PYTEST_GREEN),  # 회귀 green.
        # run_shipping_test_fn 미주입 — git stub 이 (0,"") 라 출하 변경 없음 → skip(발동 안 함).
    )
    legacy = _legacy(hf)
    assert legacy.exists()

    rc = inst.run(session_num=4, wave_summary="신규", dry_run=False, skip_pytest=False)
    assert rc == 0
    sp = _slot_path(hf)
    assert sp.exists() and "**4차**" in sp.read_text(encoding="utf-8"), \
        "게이트 통과 후 legacy→slot 이동 + slot 에 기록."
    assert not legacy.exists(), "게이트 green → legacy 는 slot 으로 이동(제거)."


